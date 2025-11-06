# STG→PRD 移行機能ハイライト

> 旧英語版の内容を踏まえ、日本語を主とした最新版に更新しました。必要な箇所には簡潔な英語メモを添えています。

本書では `cloneAndVmotion.py` を中心とする移行フローの主要な強化点を整理し、ゲストコマンドの追跡、`/etc` ファイルの取り扱い、firewalld 同期の仕組みを説明します。

---

## 1. コマンド実行ログとサマリー

- `guest_commands.py` は、実行予定のコマンドを `set_command_logger(register_command_execution)` を通じて記録します。
- `register_command_execution` はコマンドと日本語の短い説明を `COMMAND_EXECUTION_LOG` に蓄積します。自動説明が曖昧な場合は `remember_command_description(command, description)` で上書き可能です。
- ワークフロー完了時に `_print_execution_summary()` が `[説明] | [コマンド]` 形式でコマンドを時系列に表示し、同時に `[OK]` / `[WARN]` / `[ERROR]` の集計とバックアップパスを出力します。
- `find_and_extract.sh` (姉妹リポジトリ) を併用した場合は、`transform` サブコマンドが `/tmp/<user>/find_and_extract/` に差分ログを保存し、`rollback` で個別復旧が可能です。

*English note:* Every guest command is captured with a human-friendly Japanese description. Override the text with `remember_command_description()` when necessary.

---

## 2. `/etc` ファイルと `_write_guest_file`

- `_write_guest_file` は `mktemp` → heredoc → `chmod`/`chown` → `mv` のシェル手順でファイルを書き換え、Python ペイロードや base64 を用いません。
- 既定では `DEFAULT_PROTECTED_GUEST_PATHS` により `/etc/hosts`, `/etc/profile`, NTP 設定, `/etc/yum.repos.d`, `/etc/yum.repos.d/td.repo` が保護され、読み取りのみを行います。
- `--enable-standard-config-edits` を指定すると保護リストを解除し、バックアップ (`YYYYMMDD.bak`) を作成した上で PRD 仕様へ置換します。

*English note:* Standard `/etc` files remain untouched unless `--enable-standard-config-edits` is passed, ensuring the workflow is safe by default.

---

## 3. firewalld ゾーン計画と SSH リッチルール

- ソース VM から収集した firewalld 状態は `FirewalldZonePlan` として保持され、`schemas/firewalld_zone_schema.json` で検証されます。
- `_sync_firewalld_configuration_to_prd` は計画に基づいてゾーンごとに SSH rich-rule を追加し、バックアップ (`/etc/firewalld/zones/<zone>.xml-YYYYMMDD.bak`) を生成します。
- `firewall-cmd --permanent` を使用して変更し、必要な場合のみ `firewall-cmd --reload` を呼び出します。既存ルールに競合がある場合は安全のためスキップします。

*English note:* Firewalld synchronization relies on `firewall-cmd` and avoids manual file edits, while still generating backups for traceability.

---

## 4. バックアップとロールバック

- `/etc/hosts`, `/etc/profile`, NTP 設定、yum/td-agent レポジトリ、firewalld ゾーン XML、iptables ( `iptables-save` ) などは変更前に `YYYYMMDD.bak` 形式でバックアップされます。
- 実行サマリーには各バックアップパスが記録され、トラブル時にログだけで復旧手順をたどれます。
- TLS 修復に失敗した場合は `[WARN]` を残しつつ元ファイルは変更せず、運用側でのフォローアップを促します。

---

## 5. レガシー環境と REST 非対応への配慮

- NetworkManager がないゲストでは `ip` / `ifconfig` / `route` / `sed` の存在を判別し、利用可能なコマンドで設定・検証を行います。実行したコマンドはすべてサマリーに表示されます。
- REST API が利用できない場合は自動的に SOAP Guest Operations へ切り替えます。REST 再試行や証明書展開は範囲外のため、環境側での整備が必要です。
- 長時間処理 (Storage vMotion など) 中も `VSPHERE_CLONE_KEEPALIVE_SECONDS` に基づいて keep-alive を送信し、セッション切断を防ぎます。

---

## 6. 運用上のヒント

- `remember_command_description()` を活用するとサマリー内の日本語説明が明確になり、共有が容易になります。
- `[WARN]` は実行順で一覧化されるため、優先順位を付けてフォローできます。
- `VSPHERE_CLONE_LOG_LEVEL=DEBUG` でログを詳細化すると REST/SOAP 呼び出しやゲートウェイ推定の内部状態が確認できます。
- Storage vMotion の所要時間に応じて keep-alive 間隔を短縮し、セッションタイムアウトを防止してください。

---

## 7. まとめ

最新フローでは、コマンド実行ログ、`/etc` ファイルの保護、firewalld rich-rule の同期、バックアップ戦略が強化されています。これにより、STG→PRD 移行の監査性とロールバック容易性が向上し、REST / SOAP / シェルベースの複数レイヤーで環境差異に柔軟に対応できます。追加のフィードバック (Issue / PR) は随時歓迎します。
