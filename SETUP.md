# セットアップガイド

---

**前提条件**
- `Python 3.11+`
- vCenter API へ到達可能なネットワーク環境
- 対象 VM に VMware Tools が導入・稼働し、Guest Operations が利用可能
- ゲスト OS の管理者資格情報（`root` または `sudo` 可能な `admin`）

**インストール（推奨: 仮想環境）**
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
- 既に Python をグローバルインストールしている場合でも、可能ならユーザー領域にインストールしてください（権限衝突や他プロジェクトへの影響を避けるため）。
- Windows（PowerShell）
  - Python 確認: `python --version` または `py -3.11 --version`
  - ユーザー領域へ導入: `python -m pip install --user -r requirements.txt`
  - `--user` で入れたスクリプトは通常 `%USERPROFILE%\AppData\Roaming\Python\Python311\Scripts` に配置されます。必要に応じて PATH に追加してください。
  - システム全体に導入が必要な場合は管理者権限の PowerShell で実行しますが、推奨はしません。
- macOS/Linux（bash）
  - Python 確認: `python3 --version`
  - ユーザー領域へ導入: `python3 -m pip install --user -r requirements.txt`
  - ユーザー領域の実行ファイルは通常 `~/.local/bin` に配置されます。PATH に `~/.local/bin` を追加してください。
- 動作確認
  - `python -c "import pyVmomi, requests; print('deps ok')"`
- 注意点
  - `sudo pip install` は極力避けてください（OS 管理領域を汚染しやすいため）。必要なら明確な理由のもとで実施し、影響を把握してください。
  - 依存衝突が発生した場合は仮想環境の使用に切り替えることを推奨します。

**設定（任意）**
- ログ詳細度: `VSPHERE_CLONE_LOG_LEVEL`
  - 例: `setx VSPHERE_CLONE_LOG_LEVEL DEBUG` (PowerShell、新しいシェルで有効)
  - 実行中のみ: `set VSPHERE_CLONE_LOG_LEVEL=DEBUG` (PowerShell) / `export VSPHERE_CLONE_LOG_LEVEL=DEBUG` (bash)

**実行**
- `python gptCloneAndVmotion.py`
- スクリプト実行中に、ソース/宛先 vCenter の認証情報、対象 VM 名、ゲスト OS 認証情報を順に入力
- 各フェーズで確認プロンプトが表示され、`y` で続行

**トラブルシューティング**
- 疎通失敗: ネットワークポリシー/ファイアウォールで ICMP が遮断されていないか確認
- 認証失敗: VMware Tools 側で対象アカウントの Guest Operations 許可を確認
- 詳細ログ: `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を設定

**補足**
- `requirement.txt` も同梱しています（`-r requirements.txt` を参照）。通常は `requirements.txt` の使用を推奨します。
