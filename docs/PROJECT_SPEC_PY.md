# vSphere 移行自動化 ― Python 実装仕様

Python 実装の観点から、主要コンポーネント、データフロー、エラーハンドリング、ログ出力、今後の拡張を整理します。コードベースの挙動に合わせ、`/etc` 配下の扱いなど最新の設計方針を反映しています。

---

## 1. 目的と範囲

- **目的**: `cloneAndVmotion.py` がどのように STG→PRD 移行を実現するかを記述する。
- **エントリーポイント**: `cloneAndVmotion.py`
- **主要モジュール**: `guest_commands.py`, `network_utils.py`, `firewalld_manager.py`, `firewalld_schema.py`, `config_comparer.py`, `file_utils.py`
- **対象ゲスト**: RHEL / CentOS 系で firewalld・chrony/ntpd・yum・td-agent を想定し、VMware Tools の Guest Operations が有効な環境。

---

## 2. コードフロー

1. **Pre-flight (`_preflight_checks`)**  
   - `authenticate_vcenter` でソース/宛先 vCenter に接続。  
   - VMware Tools 状態を確認し `guestToolsRunning` でなければ中断。

2. **データ収集 (`_collect_source_state`)**  
   - REST SDK (requests) で NIC / DNS / ルート情報を取得し、利用不可の場合は SOAP Guest Operations へフォールバック。  
   - firewalld ゾーンは `firewalld_manager.collect_zone_plans` で収集し `FirewalldZoneSchemaValidator` で検証。  
   - NTP、yum、プロキシ設定を `guest_commands` 経由で取得。

3. **確認フェーズ (`_confirm_plan`)**  
   - 収集結果と計画差分を整形して表示し、`input("Proceed? [y/N]")` で承認を取得。

4. **クローン & 再登録 (`_clone_and_register`)**  
   - `CloneSpecBuilder` でクローン仕様を構築し、NIC 削除 → PRD vCenter 登録 → NIC 再作成 → Storage vMotion を実行。

5. **ゲスト設定 (`_configure_guest`)**  
   - `GuestCommandExecutor` でゲストコマンドを実行。root 認証が失敗すると自動で sudo ユーザーへ切り替え。  
   - 既定では firewalld rich-rule と iptables の SSH 制御のみ変更。  
   - `--enable-standard-config-edits` 指定時に限り `_write_guest_file` を用いて `/etc/hosts`・NTP・yum/td-agent レポジトリ・`/etc/profile` を更新。

6. **検証 (`_verify_and_summarise`)**  
   - REST SDK または `nmcli` による検証、`ensure_firewall_allows_ssh` による最終チェックを実施。  
   - `COMMAND_EXECUTION_LOG` と `[OK]/[WARN]/[ERROR]` を `_print_execution_summary` で整形。

---

## 3. ゲストコマンド実行 (`guest_commands.py`)

- `GuestCommandExecutor.run(vm, command, ..., check_exit_code)` が SOAP Guest Operations を利用してコマンド実行。  
- 実行前に `register_command_execution` でログに登録し、日本語説明 (`remember_command_description`) を紐付け。  
- `vim.fault.InvalidGuestLogin` など root 認証エラーを検知すると `ROOT_LOGIN_DISABLED` をセットし sudo ユーザーへフェイルオーバー。  
- 標準出力/標準エラーは `[GUEST-CMD]` 前置でロガーへ送信し、致命的なキーワードを検知したら `RuntimeError` を送出。

---

## 4. firewalld 同期 (`firewalld_schema.py` / `firewalld_manager.py`)

- `FirewalldZonePlan`: ゾーン名、インターフェース、ソース、ポート、リッチルールを保持するデータクラス。  
- `FirewalldZoneSchemaValidator`: `schemas/firewalld_zone_schema.json` を読み込み、収集したゾーンが想定スキーマに適合するか検証。  
- `_sync_firewalld_configuration_to_prd`:
  - ソース計画がある場合はリッチルール追加前にバックアップを取得し、`firewall-cmd --permanent` 経由で SSH rich-rule を追加。  
  - `firewall-cmd --reload` は変更が発生したゾーンに限り実行。  
  - ファイルを直接書き換えず、コマンド経由で反映。

---

## 5. ネットワークユーティリティ (`network_utils.py`)

- `calculate_ip_stg_to_prd` / `transform_text_to_prd`: STG IP・ドメインを PRD 形式へ変換。  
- `determine_prd_static_routes`: STG 状態とルート情報から PRD の静的ルートを推定。  
- `ensure_firewall_allows_ssh`: firewalld と iptables の組み合わせで SSH 例外を確認・追加。  
- `ensure_connection_activation`: `nmcli` 実行後の疎通検証を自動化。  
- その他、REST SDK 検証や DNS 抽出、ルート比較などのヘルパーを提供。

---

## 6. ファイル更新 (`file_utils.py`)

- `write_text_with_backup(path, content)` が `_write_guest_file` を呼び出し、以下の順序で実行:  
  1. `mktemp` で一時ファイル作成 (ターゲットディレクトリ優先)。  
  2. `bash -lc` + heredoc で内容を書き込み。  
  3. 既存ファイルがあれば `stat -c '%a %u %g'` でパーミッション/所有者を継承。  
  4. `mv` で原子的に置換し、失敗時は一時ファイルを削除。  
- `DEFAULT_PROTECTED_GUEST_PATHS` に含まれる `/etc` パスは既定で書き換え対象外。`--enable-standard-config-edits` で解除可能。追加保護は `--protect-guest-file`。

---

## 7. コマンドログとサマリー

- `COMMAND_EXECUTION_LOG`: `(command, description)` の順で記録。  
- `_describe_command` が代表的なコマンドの日本語説明を自動生成し、未知コマンドはそのまま記録。  
- `_print_execution_summary` が `[OK]/[WARN]/[ERROR]` とコマンド一覧、バックアップパスをまとめて出力。

---

## 8. エラーハンドリング方針

- vCenter 操作の失敗 (クローン、登録、Storage vMotion) は即時中断し、必要なロールバック手順を提示。  
- firewalld/iptables 以外の `/etc` 操作は保護が有効な限り読み取りのみ。保護解除後に書き換えに失敗した場合は `[WARN]` を記録し、次工程へ進む。  
- TLS 修復がすべて失敗した場合も `[WARN]` として処理を継続し、元ファイルは触れない。  
- REST API が利用できない場合は SOAP Guest Operations へフォールバックするが、証明書やプロキシ設定の整備は範囲外。

---

## 9. テスト方針

- **単体テスト**: `GuestCommandExecutor`, `transform_text_to_prd`, `determine_prd_static_routes` などをモック化して検証。  
- **統合テスト**: 疎通確認フロー、firewalld rich-rule 追加、シェルフォールバックを仮想ゲストで実行。  
- **回帰テスト**: デフォルトゲートウェイ欠落ケース、firewalld ゾーン無しケース、TLS エラー再現を含む。  
- **機能ガード**: `--enable-standard-config-edits` の有無で `/etc` 書き換えの有無が変わることをテストで確認。

---

## 10. 将来拡張

1. 差分プレビューの組み込み (`find_and_extract` 連携)。  
2. HTML / JSON 形式のサマリーレポート出力。  
3. RHEL 8/9, Ubuntu LTS など追加ディストリビューションでの E2E 検証。  
4. `_write_guest_file` や firewalld 同期の自動テスト整備。  
5. REST / SOAP SDK ラッパー整備によるテスト容易性向上。

---

## 11. まとめ

`cloneAndVmotion.py` は vCenter API と Guest Operations を組み合わせ、STG→PRD 移行を高い追跡性で実現します。既定では firewalld と iptables の調整に留め、`--enable-standard-config-edits` を指定した場合のみ `/etc` の主要設定を安全に書き換えます。バックアップ生成と詳細ログにより監査性とロールバック容易性を確保しつつ、将来的な自動化拡張に備えています。
