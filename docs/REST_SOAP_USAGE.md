# REST / SOAP 連携詳細ドキュメント

本ドキュメントは、ゲスト操作ユーティリティ群が REST API および SOAP API をどのように使っているかを、具体的なコマンド列と共に「やること」「やらないこと」まで含めて整理したものです。

---

## REST API の利用フロー

### 1. REST セッション確立

```bash
curl -k -s -u "${VCSA_USER}:${VCSA_PASS}" \
  https://$VCSA_HOST/rest/com/vmware/cis/session \
  | jq -r '.value' > /tmp/vsphere_session.txt
```

- 取得したセッション ID (`vmware-api-session-id`) を以後の REST リクエストにヘッダーとして付加します。
- セッションが有効かどうかを随時確認し、無効になった場合は再取得します。

### 2. ゲスト NIC 状態の取得

```bash
SESSION=$(cat /tmp/vsphere_session.txt)
curl -k -s \
  -H "vmware-api-session-id: $SESSION" \
  https://$VCSA_HOST/rest/vcenter/vm/$VM_ID/guest/networking/interfaces \
  | jq '.value[]'
```

- 取得内容：NIC ID、MAC アドレス、IPv4 アドレス、ゾーン情報など。
- `firewalld_manager.apply_zone_ports()` が差分を計算するときの基礎データになります。
- NIC が REST API で列挙できない場合は、レガシーな SOAP（Guest Operations）から取得した情報にフォールバックします。

### 3. firewalld サービス情報の解決

```bash
curl -k -s \
  -H "vmware-api-session-id: $SESSION" \
  "https://$VCSA_HOST/rest/vcenter/vm/$VM_ID/guest/networking/service-ports?service=${SERVICE}" \
  | jq '.value.ports[]'
```

- サービス名 (`ssh`, `http`, など) からポート定義一覧を取得します。
- 取得したポートを `env_mapping.get_service_ports()` がマッピングし、firewalld ゾーンへ適用します。

### 4. REST 経由データの検証

- `config_parsers.SchemaValidator`（JSON Schema draft-07）で、取得データの必須キーと許容キーをチェックします。
- 不正な形のデータが来た場合はログへ出力し、差分計算から除外します。

### 5. REST 経由のログ送信（任意）

- `guest_commands.set_command_logger()` を利用し、REST エンドポイントへコマンドログを `POST /logs` といった形で送信できます。
- 例:

  ```python
  def logger(cmd: str) -> None:
      requests.post("https://example.internal/logs", json={"command": cmd}, timeout=5)

  set_command_logger(logger)
  ```

### REST API で「しないこと」

- REST エンドポイントの認証情報（API トークンや証明書）の発行・保管は行いません。利用者が管理してください。
- `firewall-cmd` や `nmcli` の代替として REST 経由で直接 firewalld 設定を書き換えることは行いません。ゲスト OS 内のコマンド実行が前提です。
- REST API が利用できない環境で自動的に代替手段を注入することはありません（SOAP フォールバックのみ）。
- プロキシ設定・TLS 証明書の配布・負荷分散など、REST 通信環境の整備は行いません（通知のみ）。

---

## SOAP (VMware Guest Operations) の利用フロー

### 1. VMware Tools の稼働確認

- `guest_commands.py` で SOAP セッションを開く前に `vm.guest.toolsRunningStatus` を確認し、`guestToolsRunning` 以外の場合は即中断します。

### 2. コマンド実行（root → admin の自動フェイルオーバー）

```python
executor = GuestCommandExecutor(
    guest_operations_manager,
    root_auth,
    admin_auth,
    admin_password,
)
exit_code, stdout, stderr = executor.run(vm, "firewall-cmd --reload")
```

- root 認証が失敗した場合：
  - `vim.fault.InvalidGuestLogin` や権限系のエラーを検知すると、root パスワード無効化フラグ (`ROOT_LOGIN_DISABLED`) を立て、admin 資格情報へ自動で切り替えます。
  - Admin 資格情報も無い場合は即座に例外を投げ、ログに記録します。

### 3. firewalld ゾーン設定の同期

```python
from firewalld_manager import apply_zone_ports, apply_zone_sources

# 望ましいポート/ソースは YAML や JSON から読み込み済みとする
desired_ports = ["80/tcp", "443/tcp"]
desired_sources = ["10.0.0.0/24", "10.0.1.0/24"]

apply_zone_ports("public", desired_ports, executor=executor.run)
apply_zone_sources("public", desired_sources, executor=executor.run)
```

- 現在の状態はゲスト OS 内で `firewall-cmd --zone=<zone> --list-ports/--list-sources` を実行して取得します（REST では取得しません）。
- `config_comparer.diff_firewalld_ports()` / `diff_firewalld_sources()` が削除・追加すべきエントリを計算します。
- このツールが実際に変更するのは **172.16.164.7 からの SSH 許可設定** のみです。その他のポートやソース定義は分析した上で「差分があっても書き換えず、ログに出す」方針です。
  - 例：`apply_zone_sources()` は差分に 172.16.164.7 が現れた場合だけ `firewall-cmd --add-source 172.16.164.7 --permanent` を実行します。
  - 他のソースが差分に出ても、 `removed_protected` として保持し、コンソールへ「保持した」旨を出力するだけで変更しません。
- 同様にポートも 22/TCP (SSH) の許可が必要な場合のみ `firewall-cmd --add-port 22/tcp --permanent` を実行し、それ以外の差分は通知のみで書き換えません。
  - 変更が発生した場合のみ `firewall-cmd --reload` を呼び出します。
  - リンクローカルアドレス（169.254.0.0/16）に該当するソースは削除対象から除外し、その旨をログに出力します。

### 4. nmcli / レガシー設定のフォールバック

```python
try:
    executor.run(vm, "nmcli connection up my-connection")
except GuestCommandError:
    configure_interface_without_nmcli(
        executor.run,
        "eth0",
        "10.0.0.10",
        24,
        gateway="10.0.0.1",
        routes=[...],
        dns=["8.8.8.8"],
    )
```

- `nmcli` が存在しない、あるいは `GuestCommandExecutor` が `NmcliNotAvailableError` を投げた場合は、自前の処理にフォールバックして `/etc/sysconfig/network-scripts` などへ設定を書き込みます。

### 5. ファイル更新のバックアップ

```python
from file_utils import write_text_with_backup
write_text_with_backup(Path("/etc/firewalld/zones/public.xml"), new_xml)
```

- `/etc/firewalld/zones/*.xml` を更新する前に、`.bak` 拡張子でタイムスタンプ付きバックアップを作成します。
- `_resolve_backup_path()` により、重複しないバックアップファイル名を自動生成します。

### 6. コマンドログとエラー処理

- `guest_commands.py` は実行したコマンドと出力（STDOUT / STDERR）を `[GUEST-CMD]` プレフィックス付きで標準出力へ出力します。
- `COMMAND_LOGGER` が設定されていれば、同じ文字列が Python のコールバックにも渡されます。
- エラー (`stderr` 内の "error"/"failed"/"fatal" 等) を検出した場合は `RuntimeError` を投げ、呼び出し元へ通知します。

### SOAP で「しないこと」

- vSphere のオブジェクト（データストア、仮想 NIC、リソースプールなど）を直接操作する SOAP 呼び出しは行いません。ゲスト OS 内のコマンド実行のみです。
- SOAP セッション延長以外の vCenter 設定変更やアラート連携は行いません。
- SOAP でイベントをサブスクライブしたり、長時間ポーリングしたりはしません。コマンド単位の同期処理だけを提供します。
- ゲスト OS がシャットダウン・再起動された場合や VMware Tools が停止した場合に、リトライの間隔や回数を自動調整する高度な制御は行いません（ログ出力と例外伝播のみ）。

---

## 関連モジュール

| モジュール | 役割 |
|------------|------|
| `guest_commands.py` | SOAP Guest Operations を用いたコマンド実行、ログ取得、root→admin フェイルオーバー処理 |
| `firewalld_manager.py` | firewalld ゾーン差分適用 (`apply_zone_ports`, `apply_zone_sources`, `apply_service_ports`) |
| `firewalld_parser.py` | firewalld ゾーン XML の解析・整形、ポート／リソース抽出 |
| `config_comparer.py` | ゾーン、iptables、Pacemaker クラスタの差分計算 |
| `env_mapping.py` | JSON マッピングからサービス名→ポート一覧を取得 (`get_service_ports`) |
| `file_utils.py` | バックアップ付きファイル書き換え (`write_text_with_backup`) |

これらのモジュールを組み合わせることで、STG から PRD への設定移行、ゲスト OS 内の firewalld / ネットワーク調整を自動化し、REST / SOAP 双方の利点を活かしたワークフローを構築しています。
