# vSphere マイグレーション自動化ツール

cloneAndVmotion.py は、STG 環境の VM をクローンし、宛先 vCenter / PRD ネットワークへ再登録してから Storage vMotion で最終データストアへ移行する一連の作業を自動化するスクリプトです。ゲスト OS のネットワーク設定は nmcli（利用不可の場合はレガシー方式）で適用し、vSphere Automation SDK を用いた検証で設定差異を可視化します。

---

## クイックスタート

> 前提: Python 3.11 以上がインストールされていること。

### 1. 仮想環境の作成・有効化

`powershell
python -m venv .venv
.\.venv\Scripts\Activate
`

`bash
python3 -m venv .venv
source .venv/bin/activate
`

### 2. 依存パッケージの導入

`bash
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m pip install --upgrade pip
pip install -r requirements.txt
`

### 3. スクリプトの実行

`bash
python cloneAndVmotion.py
`

> 仮想環境を先に作成するフローなど、他のセットアップ方法は docs/SETUP_JA.md を参照してください。

---

## 特長

- クローン作成 → 宛先 vCenter 登録 → NIC 再構成 → IP/DNS/ルート適用 → Storage vMotion を一括自動化。
- vSphere Automation SDK REST API を用いてゲスト側の IP/DNS/ルートを取得し、設定値との差分を確認（利用不可の場合は nmcli で検証）。
- デフォルトゲートウェイ推定ロジックを強化し、STG ルート情報とゲスト情報から所有 NIC を正確に判断。
- DNS 構成の比較結果を正規化し、誤った警告を抑制。
- nmcli 接続を自動接続状態に保ち、静的ルートの二重登録を防止。
- 失敗時には VM 削除やクリーンアップ手順など、ロールバックの選択肢を案内。

---

## 前提条件

- ソース/宛先 vCenter への API・Guest Operations アクセスが可能であること。
- 対象 VM に VMware Tools が導入・稼働し、Guest Operations 権限が付与されていること。
- ゲスト OS に nmcli/NetworkManager が導入されていること（未導入の場合はレガシー方式で適用）。
- Python 3.11 以上、および requirements.txt に記載の依存をインストール済みであること。

---

## 実行フロー概要

1. ソース vCenter の VM から NIC 情報、既存ルート、DNS を取得し、ゲートウェイ所有 NIC を解析。
2. 作業用データストアへクローンを作成し、NIC を削除して初期化。
3. 宛先 vCenter へ登録し、PRD ネットワークに合わせて NIC を再作成。
4. ゲスト OS で IP/GW/DNS/静的ルートを適用（nmcli またはレガシー方式）。設定後は SDK / nmcli で検証。
5. 検証成功後、PRD データストアへ Storage vMotion を実施し、完了ログを出力。

長時間処理では vCenter セッション維持のため定期的に keep-alive を送信します。間隔は VSPHERE_CLONE_KEEPALIVE_SECONDS で調整可能です。

---

## 使い方のポイント

- python cloneAndVmotion.py を実行し、プロンプトに従って以下を入力します:
  - ソース / 宛先 vCenter の認証情報
  - 対象 VM 名
  - ゲスト OS の認証情報（root または sudo 可能な管理ユーザー）
- 各フェーズの確認メッセージで y を入力すると処理を継続します。
- ログレベルを上げたい場合は VSPHERE_CLONE_LOG_LEVEL=DEBUG を設定します。
- 検証は SDK > nmcli の順に優先され、SDK が利用できない場合は自動的に nmcli チェックへ切り替わります。

---

## トラブルシューティング

- **疎通判定に失敗する場合**: ping 宛先や ICMP の許可設定を確認してください。ログには実行コマンドが記録されます。
- **Guest Operations 認証に失敗する場合**: VMware Tools 側で対象アカウントの権限を確認してください。
- **DNS 警告が出る場合**: ログに表示される期待値と実際値を比較してください。両方空のときは警告は表示されません。
- **追加ルートが表示される場合**: 0.0.0.0/0 など既定ルートは除外され、差分がある静的ルートのみが記録されます。
- **ロールバックが必要な場合**: 途中で失敗すると VM 削除やファイル片付けなどの選択肢が提示されるので、指示に従って環境を整えてください。

---

## 参考資料

- docs/SETUP_JA.md: 日本語版セットアップ手順。
- CHANGELOG.md: 変更履歴。
- docs/TODO_JA.md: 今後のタスク一覧。

改善提案は Issue や Pull Request で気軽にお寄せください。
