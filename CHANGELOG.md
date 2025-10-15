# Version History

## 2025-10-16
- Added PRD static-route ownership tracking so each route is bound to the correct NIC during migration.
- Added nmcli post-configuration validation to confirm IP/gateway/routes/DNS after vMotion.

## 2025-10-12
- locale の強制・復元ロジックを追加し、nmcli/ping の判定がロケールに依存しないよう改善。
- sudo 実行まわりのフォールバック処理を再実装し、補助コマンド(script) 併用を含めた安定化を実施。
- README.md / SETUP.md を日本語で詳細化し、セットアップ手順と使用方法を明文化。

## 2025-10-11
- クローン～登録～IP 再設定～Storage vMotion までのフローを全面改修。
- ゲスト OS での nmcli / ping を用いた疎通確認とロールバック処理を実装。

## 2025-10-10
- リポジトリ初期化。ソース vCenter から宛先 vCenter への手動移行スクリプトを取り込み。
