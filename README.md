# vSphere 自動化スクリプト

`cloneAndVmotion.py` は、vSphere 上で STG 環境の VM をクローンし、宛先 vCenter/ネットワークへ登録・再構成したうえで、PRD 用データストアへ Storage vMotion で移行する作業を自動化するスクリプトです。ゲスト OS のネットワーク設定は `nmcli` を用いて自動適用・検証します。

---

## クイックスタート（推奨: venv → clone）
- Windows/PowerShell
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
  - `python cloneAndVmotion.py`
- macOS/Linux（bash）
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
  - `python cloneAndVmotion.py`

代替の手順（clone → venv）は `SETUP.md` を参照してください。

---

## 特長
- クローン → 宛先 vCenter 登録 → NIC 再構成 → IP/GW/DNS/ルート適用 → Storage vMotion まで自動化
- 設定後は `nmcli` で検証し、不整合があれば警告
- エラー時はロールバック（クローン削除・不要ファイル削除 など）を提案

## 対応環境・前提
- vCenter に API 到達できること
- 対象 VM に VMware Tools が導入・稼働し、Guest Operations が利用可能
- ゲスト OS 側で `nmcli`（NetworkManager）が利用可能（主に Linux）
- Python 3.11 以降

## 依存モジュール
- pyvmomi
- requests
- 詳細は `requirements.txt` と `SETUP.md` を参照

## 実行フロー（概要）
1. ソース vCenter から対象 VM の NIC 情報、GW、DNS などを取得
2. 一時データストアへクローン作成（クローン側 NIC を初期化/調整）
3. 宛先 vCenter へ登録し、PRD ネットワークに合わせて NIC 再構成
4. ゲスト OS 側で `nmcli` により IP/GW/DNS/ルートを適用・検証
5. 一時配置から最終 PRD 用データストアへ Storage vMotion で移行

## 使い方（対話の流れ）
- 実行: `python cloneAndVmotion.py`
- 対話で以下を入力
  - ソース/宛先 vCenter の認証情報
  - 移行対象 VM 名
  - ゲスト OS の認証情報（root または sudo 可能な admin）
- 各フェーズで確認メッセージが表示され、`y` で続行

## トラブルシューティング
- 疎通失敗: ネットワークポリシー/ファイアウォールで ICMP が遮断されていないか確認
- 認証失敗: VMware Tools 側で対象アカウントに Guest Operations 権限があるか確認
- 詳細ログ: `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を設定

## 開発メモ
- 依存は `requirements.txt` に記載（`pip install -r requirements.txt`）
- 詳細なセットアップは `SETUP.md` を参照
- Issue/PR による改善提案を歓迎

