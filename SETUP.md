# SETUP

このドキュメントでは、gptCloneAndVmotion.py を実行するための環境構築手順を説明します。主に Windows (PowerShell) を想定していますが、Linux/macOS 向けの参考情報も記載しています。

---

## 1. Python のインストール

### Windows
1. [公式サイト](https://www.python.org/downloads/) から **Python 3.11 以上** のインストーラ (Windows x86-64 executable installer) をダウンロードします。
2. インストーラを起動し、最初の画面で **"Add Python to PATH"** にチェックを入れてから "Install Now" をクリックします。
3. インストール完了後、PowerShell を開いて次を実行し、バージョンを確認します。
   `powershell
   python --version
   `
   Python 3.11.x などが表示されれば成功です。

### Linux / macOS (参考)
- ほとんどのディストリビューションでは python3 --version でバージョンを確認できます。
- 必要に応じて pyenv や各 OS のパッケージマネージャ (pt, dnf, rew など) で Python 3.11 を導入してください。

---

## 2. 仮想環境の作成 (推奨)
作業ディレクトリ (gptCloneAndVmotion.py が存在するフォルダ) で以下を実行します。

`powershell
# 仮想環境を作成
python -m venv .venv

# 仮想環境を有効化
. .\.venv\Scripts\Activate.ps1
`

- Linux/macOS の場合は source .venv/bin/activate で有効化します。
- 仮想環境を終了するには deactivate を実行します。

---

## 3. 必要な Python モジュールのインストール
仮想環境が有効になっている状態で、以下を実行して依存ライブラリをインストールします。

`powershell
pip install --upgrade pip
pip install pyVmomi requests
`

オフライン環境など追加の認証が必要な場合は、あらかじめ社内リポジトリの設定を行ってください。

---

## 4. vCenter 接続に必要な情報
スクリプト実行時に以下の情報が必要です。

- ソース vCenter / 宛先 vCenter のホスト名または IP アドレス
- dministrator@vsphere.local など、vCenter へアクセス可能なアカウントのユーザー名・パスワード
- ゲスト OS (対象 VM) の root および sudo 可能な admin アカウントのパスワード
- ステージング → 本番移行時に使用するデータストア名、ネットワーク名、クラスタ名

---

## 5. 実行方法
1. PowerShell で作業ディレクトリへ移動し、必要に応じて仮想環境を有効化します。
   `powershell
   cd G:\マイドライブ\development\py\vSphere
   . .\.venv\Scripts\Activate.ps1
   `
2. スクリプトを実行します。
   `powershell
   python gptCloneAndVmotion.py
   `
3. プロンプトに従って情報を入力し、各フェーズで y を入力して処理を進めてください。

---

## 6. よくあるトラブルと対策
- **ゲスト認証に失敗する**: VMware Tools が稼働しているか、Guest Operations に必要な権限がアカウントに付与されているか確認してください。
- **疎通確認が失敗する**: ping がセキュリティポリシーで制限されていないか確認し、必要に応じて VSPHERE_CLONE_LOG_LEVEL=DEBUG を設定して詳細ログを確認します。
- **証明書エラーで接続できない**: 社内環境に合わせて Python の SSL 設定、またはプロキシ/証明書の設定を行ってください。

---

## 7. 補足
- スクリプトの機能詳細や今後の拡張計画は README.md を参照してください。
- 複数 VM をバッチ処理する場合や自動化パイプラインへ組み込む場合は、入力部分を環境変数に置き換えるなどの拡張を検討してください。

---

以上でセットアップは完了です。何か問題があれば Issue や Pull Request でお知らせください。
