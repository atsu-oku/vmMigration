# Version History

## 2025-10-17
- Hardened guest command error handling with clearer exit-code reasons and CLI output capture.
- Fixed newline injection when staging admin passwords and ensured nmcli fallback logic short-circuits correctly.
- Added vCenter keep-alive threading to prevent session expiry during long storage vMotion sequences.

## 2025-10-16
- Added PRD static-route ownership tracking so each route is bound to the correct NIC during migration.
- Added nmcli post-configuration validation to confirm IP/gateway/routes/DNS after vMotion.

## 2025-10-12
- ロケール固定と出力の一貫性を改善。nmcli/ping の挙動が想定どおりになるよう調整。
- sudo 実行時のフォールバック処理を強化し、主要コマンドの再実行性を改善。
- README.md / SETUP.md を再構成して、セットアップ手順と使い方を明確化。

## 2025-10-11
- クローン → IP 設定 → Storage vMotion までのフローを整備。
- ゲスト OS で nmcli / ping を用いた疎通確認とロールバック処理を追加。

## 2025-10-10
- プロジェクト初期化。ソース vCenter と宛先 vCenter を跨ぐ移行スクリプトの土台を作成。
