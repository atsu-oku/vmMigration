# セットアップガイド（日本語版）

この文書では、リポジトリ取得から依存インストール、環境変数の設定方法までを日本語でまとめています。英語版は SETUP.md を参照してください。

---

## リポジトリ取得パターン

### パターン A: clone → venv 作成
`bash
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m venv .venv         # macOS/Linux は python3 -m venv .venv
.\.venv\Scripts\Activate    # macOS/Linux は source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
`

### パターン B: 先に venv を作成（ポリシー準拠）
`bash
python -m venv .venv
.\.venv\Scripts\Activate    # macOS/Linux は source .venv/bin/activate
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
pip install -r requirements.txt
`

---

## 前提条件
- Python 3.11 以上
- ソース/宛先 vCenter への API & Guest Operations アクセス
- VMware Tools が導入され Guest Operations 権限が付与されていること
- ゲスト OS に nmcli / NetworkManager が導入されていること（未導入でも動作しますが検証が限定されます）

---

## 依存モジュールのインストール

`bash
pip install -r requirements.txt
python -c "import pyVmomi, requests; print('dependencies ok')"
`

> 可能な限り sudo pip install は避け、仮想環境または --user を利用してください。

---

## 主要な環境変数

| 変数 | 説明 | 既定値 |
| --- | --- | --- |
| VSPHERE_CLONE_LOG_LEVEL | ログ出力レベル（DEBUG / INFO / WARNING 等） | WARNING |
| VSPHERE_CLONE_KEEPALIVE_SECONDS | vCenter への keep-alive 送信間隔（秒） | 240 |

PowerShell 例:
`powershell
setx VSPHERE_CLONE_LOG_LEVEL = "DEBUG"  # 永続設定
set VSPHERE_CLONE_LOG_LEVEL = "DEBUG"   # 現在のセッションのみ
`

bash 例:
`bash
export VSPHERE_CLONE_LOG_LEVEL=DEBUG
`

---

## 実行手順

`bash
python cloneAndVmotion.py
`

スクリプト実行中に以下を入力します:
- ソース / 宛先 vCenter の認証情報
- 対象 VM 名
- ゲスト OS の認証情報（root または sudo 可能な管理ユーザー）

各フェーズでは要約が表示され、y を入力すると処理が続行されます。

---

## トラブルシューティング

- **疎通判定に失敗**: ping 宛先や ICMP 設定を確認。必要に応じてスクリプト内のターゲットリストを調整してください。
- **Guest Operations 認証エラー**: VMware Tools 側の権限を確認。
- **DNS やルートの警告**: ログに表示される期待値/実際値を確認し、差分があればゲスト設定を見直してください。
- **セッション切断**: VSPHERE_CLONE_KEEPALIVE_SECONDS を短くすると keep-alive が頻繁になります。

---

## 参考
- README.md / docs/README_JA.md: プロジェクト概要
- CHANGELOG.md: 変更履歴
- docs/TODO_JA.md: 残タスク
