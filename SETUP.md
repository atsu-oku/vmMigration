# セットアップガイド

本ドキュメントは README のクイックスタートを補完し、環境構築・依存インストール・設定・ログ活用までを詳細に記載しています。運用ポリシーやネットワーク制約に合わせて手順を選択してください。

---

## 1. リポジトリ入手パターン

### パターン A: リポジトリ取得後に仮想環境を作成

```bash
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m venv .venv           # macOS/Linux は python3
.\.venv\Scripts\Activate       # macOS/Linux は source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### パターン B: 仮想環境を先に作成 (ポリシー制約下での運用例)

```bash
python -m venv .venv
.\.venv\Scripts\Activate       # または source .venv/bin/activate
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
pip install -r requirements.txt
```

どちらの手順でも結果は同じです。パターン B は「仮想環境外への書き込みを禁止する」ポリシーに従う際に有効です。

---

## 2. 必要要件

- Python 3.11 以上。
- ソース / 宛先 vCenter へ API・Guest Operations でアクセス可能なネットワーク環境。
- VMware Tools が稼働し、Guest Operations の権限が付与されたアカウント。
- ゲスト OS の root または sudo 権限ユーザー。
- `nmcli` / NetworkManager が利用可能であれば推奨。非搭載の場合はシェルフォールバックが自動適用されます。

---

## 3. 依存パッケージのインストール

`requirements.txt` に必要ライブラリを定義しています。仮想環境内またはユーザースコープでインストールしてください。

```bash
pip install -r requirements.txt
```

導入確認:

```bash
python -c "import pyVmomi, requests; print('dependencies ok')"
```

> **注意**: `sudo pip install` は極力避けてください。どうしても仮想環境外で導入する場合は `pip install --user` を利用しユーザースコープに留めます。

---

## 4. 設定 (任意)

環境変数でログレベルや keep-alive 間隔を調整できます。

| 変数名 | 役割 | 初期値 |
| --- | --- | --- |
| `VSPHERE_CLONE_LOG_LEVEL` | ログ出力レベル (`DEBUG`/`INFO`/`WARNING`/…) | `WARNING` |
| `VSPHERE_CLONE_KEEPALIVE_SECONDS` | vCenter keep-alive の送信間隔 | `240` |
| `REQUESTS_AVAILABLE` | REST SDK を強制利用 (`1`) / 強制無効化 (`0`) | 未設定 |

PowerShell 例:

```powershell
setx VSPHERE_CLONE_LOG_LEVEL DEBUG     # 恒久設定
$env:VSPHERE_CLONE_LOG_LEVEL = "DEBUG" # セッション限定
```

bash 例:

```bash
export VSPHERE_CLONE_LOG_LEVEL=DEBUG
```

---

## 5. スクリプトの実行

```bash
python cloneAndVmotion.py
```

実行中に求められる情報:

- ソース / 宛先 vCenter の URL・ユーザー・パスワード
- 対象 VM 名
- ゲスト OS の root 認証情報および sudo 権限ユーザー

各フェーズで概要が表示され `y` で続行します。完了後は `[OK]` / `[WARN]` / `[ERROR]` とゲストコマンドの実行履歴を含むサマリーを確認してください。

---

## 6. ログ運用のヒント

- `remember_command_description(command, text)` で実行コマンドの説明文を事前登録すると、サマリー表示が読みやすくなります。
- `VSPHERE_CLONE_LOG_LEVEL=DEBUG` を有効にすると REST / SOAP 両経路の詳細ログが出力され、疎通確認や API 呼び出しの診断に役立ちます。
- Storage vMotion 等で 10 分以上処理が継続する場合は keep-alive が届いているか確認し、必要に応じて `VSPHERE_CLONE_KEEPALIVE_SECONDS` を短縮してください。

---

## 7. トラブルシューティング

- **通信失敗**: ゲスト → 判定対象への ICMP / TCP 許可を確認。ログには使用した `ping` / `curl` コマンドが記録されます。
- **認証エラー**: VMware Tools の Guest Operations 権限やアカウント情報を再確認。root が失敗すると自動で sudo ユーザーに切り替わります。
- **DNS / ルート差分**: サマリーには期待値と実測値が表示されます。差分が残る場合はゲストでの手動確認が必要です。
- **セッション切断**: keep-alive インターバルを短縮し、REST/SOAP 要求がタイムアウトしないか監視します。

---

## 8. 関連ドキュメント

- `README.md`: プロジェクト概要、ワークフロー解説、代表的なオプション。
- `CHANGELOG.md`: 変更履歴と改善内容。
- `TODO.md`: 近々対応予定のタスクと中長期課題。
- `docs/PROJECT_SPEC.md`: アーキテクチャ全体像と業務フロー。
- `docs/PROJECT_SPEC_PY.md`: Python 実装視点での詳細仕様。
- `docs/REST_SOAP_USAGE.md`: REST / SOAP API を活用した操作手順。

環境ごとのベストプラクティスがあれば Issue や PR で共有いただけると、ドキュメントの改善に繋がります。
