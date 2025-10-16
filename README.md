# vSphere Automation Scripts

- `gptCloneAndVmotion.py`: vSphere VM clone and Storage vMotion を自動化するスクリプト

---

**概要**
- STG 環境の VM をクローンし、宛先 vCenter/ネットワークへ登録・再構成後、PRD 用データストアへ Storage vMotion で移行します。
- ゲスト OS の IP/GW/DNS/ルート設定は `nmcli` を用いて自動適用し、検証まで行います。

**要件**
- `Python 3.11+`
- 依存モジュール: `pyvmomi`, `requests`（`requirements.txt` 参照）

**クイックスタート**
- 依存関係のインストール（PowerShell）
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- 実行
  - `python gptCloneAndVmotion.py`

より詳しいセットアップやトラブルシュートは `SETUP.md` を参照してください。
# vSphere 自動化スクリプト

- `gptCloneAndVmotion.py`: vSphere 上で VM のクローン作成から宛先 vCenter への登録、ネットワーク再設定、Storage vMotion までを自動化するスクリプト。

---

**概要**
- STG 環境の VM をクローンし、宛先 vCenter/ネットワークへ登録・再構成後、PRD 用データストアへ Storage vMotion で移行します。
- ゲスト OS の IP/GW/DNS/ルート設定は `nmcli` で自動適用し、検証まで行います。

**要件**
- Python 3.11 以上
- 依存モジュール: `pyvmomi`, `requests`（`requirements.txt` を参照）

**クイックスタート**
- 依存関係のインストール（Windows/PowerShell）
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- 実行
  - `python gptCloneAndVmotion.py`

詳細な手順やトラブルシューティングは `SETUP.md` を参照してください。
