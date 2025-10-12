# SETUP

このドキュメントでは、`gptCloneAndVmotion.py` を実行するための環境構築手順を説明します。主に Windows (PowerShell) を想定していますが、Linux/macOS 向けの参考情報も記載しています。

---

## 1. 事前準備 (Python / Git)

### 社内配布サイトからのインストール
- Python や Git は **社内の情シス用インストーラー** (例: \\fileserver\it-tools\installer) からダウンロードしてください。
- インターネット上の公式サイトから取得したインストーラーの利用は禁止されています。

#### Python 3.11 以上の導入
1. 情シス用インストーラポータルで "Python 3.11" 以上の Windows 64bit インストーラ (`python-3.11.x-amd64.exe`) を入手します。
2. インストール時に **"Add Python to PATH" (環境変数に追加)** にチェックを付けてからインストールしてください。
3. インストール後、PowerShell で以下を実行しバージョンを確認します。
   ```powershell
   python --version
   ```
   `Python 3.11.x` と表示されれば完了です。

#### Git の導入
1. 同じく情シス用ポータルから Git for Windows のインストーラ (`Git-2.x.x-64-bit.exe`) を取得します。
2. インストール時の "Adjusting your PATH environment" は **"Git from the command line and also from 3rd-party software"** を選択してください。
3. インストール後、PowerShell で以下を実行し、バージョンを確認します。
   ```powershell
   git --version
   ```
   `git version 2.x.x` と表示されれば準備完了です。

> **補足:** Linux / macOS 環境で作業する場合も、社内が配布しているパッケージまたは承認済みリポジトリを利用してください。`pyenv` や `brew` を使う場合は、情報システム部の指示に従ってください。

### リポジトリの取得 (git clone)
1. 作業場所とするフォルダを決め（例: `G:\マイドライブ\development\py`）、PowerShell で移動します。
   ```powershell
   cd G:\マイドライブ\development\py
   ```
2. リポジトリをクローンします（公開リポジトリのため認証情報は不要です）。
   ```powershell
   git clone https://github.com/atsu-oku/vmMigration.git
   ```
   社内プロキシ越しにアクセスする場合は、必要に応じて `git config --global http.proxy http://proxy.example.com:8080` などを設定してください。
3. クローン後、作業フォルダに移動します。
   ```powershell
   cd vmMigration
   ```

---

## 2. 仮想環境の作成 (推奨)
作業ディレクトリ (`gptCloneAndVmotion.py` が存在するフォルダ) で以下を実行します。

```powershell
# 仮想環境を作成
python -m venv .venv

# 仮想環境を有効化
. .\.venv\Scripts\Activate.ps1
```

- Linux/macOS の場合は `source .venv/bin/activate` で有効化します。
- 仮想環境を終了するには `deactivate` を実行します。

---

## 3. 必要な Python モジュールのインストール
仮想環境が有効になっている状態で、以下を実行して依存ライブラリをインストールします。

```powershell
pip install --upgrade pip
pip install pyVmomi requests
```

> **補足:** オフライン環境や認証が必要なネットワークの場合は、社内で許可された Python パッケージミラーやプロキシを利用してください。

---

## 4. vCenter 接続に必要な情報
実行時に以下の情報が必要です。

- ソース / 宛先 vCenter のホスト名または IP アドレス
- vCenter にアクセス可能なアカウント (例: `administrator@vsphere.local`) のユーザー名とパスワード
- ゲスト OS (対象 VM) の root および sudo 可能な admin アカウントのパスワード
- ステージング → 本番移行に利用するデータストア名、ネットワーク名、クラスタ名

---

## 5. スクリプトの実行
1. PowerShell で作業ディレクトリへ移動し、仮想環境を有効化します。
   ```powershell
   cd G:\マイドライブ\development\py\vSphere
   . .\.venv\Scripts\Activate.ps1
   ```
2. スクリプトを実行します。
   ```powershell
   python gptCloneAndVmotion.py
   ```
3. プロンプトに従って情報を入力し、各フェーズで `y` を入力すると処理が進みます。

---

## 6. よくあるトラブルと対策
- **ゲスト認証に失敗する**: VMware Tools が稼働しているか、Guest Operations が許可されているか確認してください。
- **疎通確認 (ping) が失敗する**: ネットワークポリシーやファイアウォールの設定を確認し、必要に応じて `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を設定して詳細ログを確認します。
- **証明書エラーで vCenter に接続できない**: 社内の証明書ストアにルート証明書を追加するなど、情報システム部の手順に従って対応してください。

---

## 7. 補足
- スクリプトの詳細なフローや今後の拡張計画は `README.md` を参照してください。
- 複数 VM を連続で処理する場合は、入力値を環境変数や設定ファイルで管理するなどの拡張を検討してください。

---

以上でセットアップは完了です。問題や改善案があれば Issue や Pull Request でお知らせください。
