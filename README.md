# vSphere 自動化スクリプト

`gptCloneAndVmotion.py` は、vSphere 上で STG 環境の VM をクローンし、宛先 vCenter/ネットワークへ登録・再構成したうえで、PRD 用データストアへ Storage vMotion で移行する作業を自動化するスクリプトです。ゲスト OS のネットワーク設定は `nmcli` を用いて自動適用・検証します。

---

## リポジトリ取得 (git clone)
- HTTPS: `git clone https://github.com/atsu-oku/vmMigration.git`
- SSH: `git clone git@github.com:atsu-oku/vmMigration.git`
- ディレクトリへ移動: `cd vmMigration`

**特長**
- クローン作成 → 宛先 vCenter 登録 → NIC 再構成 → IP/GW/DNS/ルート適用 → Storage vMotion までの流れを自動化
- 設定後は `nmcli` で検証し、不整合があれば警告
- エラー時はロールバック（クローン削除・不要ファイル削除 など）を提案

**対応環境・前提**
- vCenter に API 到達できること
- 対象 VM に VMware Tools が導入・稼働し、Guest Operations が利用可能
- ゲスト OS 側で `nmcli`（NetworkManager）が利用可能であること（主に Linux）
- Python 3.11 以降

**依存モジュール**
- pyvmomi
- requests
- 導入方法は `requirements.txt` を参照。詳細なセットアップは `SETUP.md` を参照してください。

---

## 1. インストール・クイックスタート
- Windows/PowerShell の例
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- 実行
  - `python gptCloneAndVmotion.py`
- グローバル環境での導入や macOS/Linux の手順は `SETUP.md` を参照してください。

## 2. 主な設定値（スクリプト内）
以下は `gptCloneAndVmotion.py` 冒頭の定数で保持します。環境に合わせて編集してください。

- vCenter 接続
  - `VCSA_HOST_SOURCE`: ソース vCenter の FQDN/IP
  - `VCSA_HOST_DEST`: 宛先 vCenter の FQDN/IP
  - `VCSA_USER`: 接続ユーザー（例: `administrator@vsphere.local`）
  - `VCSA_PORT`: 443（既定）
- リソース/配置
  - `TARGET_DATASTORE_NAME`: クローンの一時配置先データストア（STG 側）
  - `TARGET_DATASTORE_NAME_FINAL`: 最終配置先（PRD 用データストア）
  - `TARGET_CLUSTER_NAME`: 宛先のクラスタ名
- ゲスト OS 認証
  - `GUEST_ROOT_USER` / `GUEST_ADMIN_USER`
  - パスワードは実行時に対話入力（スクリプトに平文保存しません）
- ログ詳細度（環境変数）
  - `VSPHERE_CLONE_LOG_LEVEL`（`INFO`/`DEBUG`）

## 3. 実行フロー（概要）
1. ソース vCenter から対象 VM の NIC 情報、GW、DNS などを取得
2. 一時データストアへクローン作成（クローン側 NIC を初期化/調整）
3. 宛先 vCenter へ登録し、PRD ネットワークに合わせて NIC 再構成
4. ゲスト OS 側で `nmcli` により IP/GW/DNS/ルートを適用・検証
5. 一時配置から最終 PRD 用データストアへ Storage vMotion で移行

## 4. 使い方（対話の流れ）
- 実行: `python gptCloneAndVmotion.py`
- 対話で以下を入力
  - ソース/宛先 vCenter の認証情報
  - 移行対象 VM 名
  - ゲスト OS の認証情報（root または sudo 可能な admin）
- 各フェーズで確認メッセージが表示され、`y` で続行します。

## 5. よくある質問（FAQ）
- Q. Windows のゲストでも動きますか？
  - A. いいえ。ネットワーク設定に `nmcli` を用いるため、Linux（NetworkManager 利用）を想定しています。
- Q. ルーティングはどのように適用されますか？
  - A. ゲートウェイのセグメントと紐づく NIC に対してスタティックルートを適用し、適用後に検証します。
- Q. 途中で失敗した場合は？
  - A. 可能な範囲でクローン VM の削除や不要ファイル削除などのロールバック手順を提案します。

## 6. トラブルシューティング
- 疎通が失敗する
  - ネットワークポリシー/ファイアウォールで ICMP が遮断されていないか確認
  - 宛先ネットワークの VLAN/セグメント設定を確認
- ゲスト OS 認証に失敗する
  - VMware Tools 側で該当アカウントに Guest Operations の権限があるか確認
  - sudo 設定（パスワード要否など）を確認
- 詳細ログを見たい
  - 実行前に `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を設定

## 7. 開発メモ
- 依存は `requirements.txt` に記載（`pip install -r requirements.txt`）
- 詳細なセットアップは `SETUP.md` を参照
- Issue/PR による改善提案を歓迎します

