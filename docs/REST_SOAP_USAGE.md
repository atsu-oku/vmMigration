# REST / SOAP 連携詳細ドキュメント

`cloneAndVmotion.py` が VMware vCenter の REST API と SOAP (Guest Operations) をどのように併用しているかを説明します。実行例・ベストプラクティス・禁止している操作をまとめ、既定で `/etc` 配下を変更しない設計も補足します。

---

## 1. REST API の利用フロー

### 1.1 セッション取得

```bash
curl -k -s -u "${VCSA_USER}:${VCSA_PASS}" \
  https://$VCSA_HOST/rest/com/vmware/cis/session \
  | jq -r '.value' > /tmp/vsphere_session.txt
```

- 応答値は `vmware-api-session-id`。以降の REST リクエストでは HTTP ヘッダーに付与します。
- 長時間処理でセッションが失効する場合は再取得が必要です。スクリプトは keep-alive を送信して延命しますが、時間超過には注意してください。

### 1.2 ゲスト NIC 状態の取得

```bash
SESSION=$(cat /tmp/vsphere_session.txt)
curl -k -s \
  -H "vmware-api-session-id: $SESSION" \
  https://$VCSA_HOST/rest/vcenter/vm/$VM_ID/guest/networking/interfaces \
  | jq '.value[]'
```

- NIC ID、MAC、IPv4/IPv6、接続状態を取得し、firewalld ゾーン同期や NIC 再構成の基礎データとして利用します。
- REST が利用できない場合は SOAP Guest Operations で同等の情報を取得します。

### 1.3 firewalld サービスのポート定義取得

```bash
curl -k -s \
  -H "vmware-api-session-id: $SESSION" \
  "https://$VCSA_HOST/rest/vcenter/vm/$VM_ID/guest/networking/service-ports?service=${SERVICE}" \
  | jq '.value.ports[]'
```

- `ssh` や `http` などのサービス名から推奨ポート定義を取得します。
- 取得した値は `env_mapping.get_service_ports()` がローカルキャッシュし、firewalld 同期ロジックで使用します。

### 1.4 JSON Schema による検証

- 収集した REST 応答は `firewalld_schema.py` の `FirewalldZoneSchemaValidator` や `config_parsers.SchemaValidator` で検証されます。
- 想定外のキーや値が含まれている場合は警告を出し、差分計算から除外します。

### REST API で実施しないこと

- REST 経由で firewalld や NetworkManager の設定を書き換えません。ゲスト側で `firewall-cmd` / `nmcli` を実行することが前提です。
- セッショントークンを恒久保存しません。必要な処理の間だけ保持し、完了後は破棄します。
- プロキシ設定や TLS 証明書の配布は自動化していません。環境側での整備が必要です。

---

## 2. SOAP (VMware Guest Operations) の利用フロー

### 2.1 VMware Tools 状態確認

- `guest_commands.py` は SOAP セッションを開く前に `vm.guest.toolsRunningStatus` を確認します。
- `guestToolsRunning` 以外の場合は処理を中断し、オペレーターへ通知します。

### 2.2 コマンド実行と root → admin フェイルオーバー

```python
executor = GuestCommandExecutor(
    guest_operations_manager,
    root_auth,
    admin_auth,
    admin_password,
)
exit_code, stdout, stderr = executor.run(vm, "firewall-cmd --reload")
```

- root 認証が失敗 (`vim.fault.InvalidGuestLogin` など) すると `ROOT_LOGIN_DISABLED` を設定し、sudo ユーザーへ切り替えます。
- sudo ユーザーでも認証できない場合は例外を送出し、ログに詳細を残します。
- 実行ログは `[GUEST-CMD]` プレフィックス付きで標準出力へ記録され、コマンドロガーにも転送されます。

### 2.3 firewalld ゾーン同期

```python
from firewalld_manager import apply_zone_ports, apply_zone_sources

apply_zone_ports("public", ["80/tcp", "443/tcp"], executor=executor.run)
apply_zone_sources("public", ["10.0.0.0/24"], executor=executor.run)
```

- 現在のゾーン状態はゲスト側で `firewall-cmd --zone=<zone> --list-*` を実行して取得します。
- `config_comparer.diff_firewalld_*` が差分を算出し、必要な追加・削除コマンドのみ発行します。
- 変更があった場合は `firewall-cmd --reload` を実行し、結果をログへ記録します。
- 既存の SSH rich-rule と競合する場合は安全のため更新をスキップします。

### 2.4 `nmcli` とレガシー手順の切り替え

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

- `nmcli` が利用できない場合は `_configure_interface_without_nmcli` を呼び出し、`/etc/sysconfig/network-scripts` などを直接更新します。
- 実行したコマンドはすべてサマリーに記録され、手動再現や監査が容易です。

### 2.5 `/etc` ファイルの扱い

- `_write_guest_file` は `mktemp` → heredoc → `mv` で安全に置換しますが、`DEFAULT_PROTECTED_GUEST_PATHS` に含まれる `/etc` パスは既定で書き換えません。
- CLI の `--enable-standard-config-edits` を指定すると保護リストを解除し、バックアップ (`YYYYMMDD.bak`) を作成した上で `/etc/hosts` などを更新します。

### SOAP で実施しないこと

- データストアやリソースプールなど vSphere オブジェクトを直接変更しません。ゲスト OS 内のコマンド実行に限定します。
- イベントサブスクリプションや長時間のポーリングを行いません。必要なコマンドを同期的に実行します。
- ゲスト再起動・シャットダウン時の自動再試行は行わず、例外を呼び出し元へ伝えます。

---

## 3. 関連モジュール

| モジュール | 役割 |
| --- | --- |
| `guest_commands.py` | SOAP Guest Operations によるコマンド実行とログ取得、root→admin フェイルオーバー |
| `firewalld_manager.py` | firewalld ゾーンの差分計算と適用 (`apply_zone_ports`, `apply_zone_sources`, `apply_service_ports`) |
| `firewalld_parser.py` | ゾーン XML の解析と整形 |
| `config_comparer.py` | firewalld / iptables / Pacemaker 構成の差分計算 |
| `env_mapping.py` | サービス名からポート一覧を取得 |
| `file_utils.py` | バックアップ付きのファイル書き換え (`write_text_with_backup`) |

---

## 4. 運用上の注意

- REST / SOAP のどちらか一方が利用できない場合でもフォールバックが働きますが、通信環境と認証情報の整備は別途必要です。
- API 認証情報やトークンは処理完了後に破棄し、ログへ秘匿情報を残さないようにしてください。
- 長時間の Storage vMotion 実行中は keep-alive によるセッション維持を確認し、切断兆候が出た場合は間隔を調整します。
- 不要な再試行は vCenter 側の負荷やアカウントロックを招くため、失敗理由をサマリーで確認した上で環境側の改善を優先します。

---

## 5. まとめ

`cloneAndVmotion.py` は REST API を利用した情報取得と SOAP Guest Operations によるゲスト設定を組み合わせ、STG→PRD 移行を安全に実現します。既定では `/etc` 配下を変更せず、必要に応じて CLI オプションで明示的に開放できる設計です。詳細なログとバックアップにより監査・ロールバックが容易であり、本ドキュメントを参考に API 利用のルールを把握してください。
