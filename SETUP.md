# セットアップガイド

---

## リポジトリ取得 (git clone)
このリポジトリは、仮想環境（venv）の作成順序により2通りの進め方があります。組織ポリシーで「venv を先に作成」が求められる場合はパターンBを使用してください。

パターンA: 先に clone（一般的な流れ）
- HTTPS: `git clone https://github.com/atsu-oku/vmMigration.git`
- SSH: `git clone git@github.com:atsu-oku/vmMigration.git`
- ディレクトリへ移動: `cd vmMigration`

パターンB: 先に venv を作成・有効化してから clone（ポリシー準拠）
- Windows/PowerShell（例）
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `pip install -r requirements.txt`
- macOS/Linux（bash）
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `pip install -r requirements.txt`

**前提条件**
- Python 3.11 以上
- vCenter API へ到達可能なネットワーク環境
- 対象 VM に VMware Tools が導入・稼働し、Guest Operations が利用可能
- ゲスト OS に `nmcli`（NetworkManager）が利用可能（主に Linux）

**インストール（推奨: 仮想環境 venv）**
- 依存モジュールは `requirements.txt` で管理します。
- PowerShell (Windows)
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- bash (macOS/Linux)
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`

**グローバル環境でのセットアップ（venv を使わない場合）**
- 既に Python をグローバルに導入している場合でも、可能ならユーザー領域にインストールしてください（権限衝突や他プロジェクトへの影響を避けるため）。
- Windows（PowerShell）
  - Python 確認: `python --version` または `py -3.11 --version`
  - ユーザー領域へ導入: `python -m pip install --user -r requirements.txt`
  - `--user` で入れたスクリプトは通常 `%USERPROFILE%\AppData\Roaming\Python\Python311\Scripts` に配置されます。必要に応じて PATH に追加してください。
  - システム全体への導入が必要な場合は管理者権限の PowerShell で実行しますが、推奨はしません。
- macOS/Linux（bash）
  - Python 確認: `python3 --version`
  - ユーザー領域へ導入: `python3 -m pip install --user -r requirements.txt`
  - ユーザー領域の実行ファイルは通常 `~/.local/bin` に配置されます。PATH に `~/.local/bin` を追加してください。
- 動作確認
  - `python -c "import pyVmomi, requests; print('deps ok')"`
- 注意点
  - `sudo pip install` は極力避けてください（システム領域を汚染しやすいため）。やむを得ない場合は正確な要件のもとで実施し、影響を把握してください。
  - 依存衝突が発生した場合は仮想環境（venv）の使用に切り替えることを推奨します。

**設定（任意）**
- ログ詳細度: `VSPHERE_CLONE_LOG_LEVEL`
  - 永続設定: `setx VSPHERE_CLONE_LOG_LEVEL DEBUG`（PowerShell。新しいシェルで有効）
  - 一時設定: `set VSPHERE_CLONE_LOG_LEVEL=DEBUG`（PowerShell） / `export VSPHERE_CLONE_LOG_LEVEL=DEBUG`（bash）

**実行**
- `python gptCloneAndVmotion.py`
- スクリプト実行中に、ソース/宛先 vCenter の認証情報、対象 VM 名、ゲスト OS 認証情報を入力します。
- 各フェーズで確認プロンプトが表示され、`y` で続行します。

**トラブルシューティング**
- 疎通失敗: ネットワークポリシー/ファイアウォールで ICMP が遮断されていないか確認
- 認証失敗: VMware Tools 側で対象アカウントに Guest Operations 許可があるか確認
- 詳細ログ: `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を設定

**補足**
- `requirement.txt` も同梱していますが、通常は `requirements.txt` の使用を推奨します。
