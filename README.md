# vSphere 移行自動化スクリプト集

`cloneAndVmotion.py` を中心とするツール群は、ステージング (STG) vCenter 上の VM を本番 (PRD) vCenter へ移行する作業を自動化します。クローン作成から PRD ネットワーク向け NIC 再構成、ゲスト設定調整、Storage vMotion までを一連のフローとして実行します。

---

## 概要

- **対象読者**: vSphere を用いた STG→PRD 切り替えを担当するインフラエンジニア / SRE。
- **主な構成要素**: `cloneAndVmotion.py`, `guest_commands.py`, `firewalld_manager.py`, `file_utils.py`, `schemas/` 以下の JSON Schema。
- **特徴**
  - クローン → 宛先 vCenter 登録 → NIC 再作成 → PRD 向けゲスト調整 → Storage vMotion をノンストップで実行。
  - firewalld ゾーンの計画を JSON Schema で検証し、PRD 側へ正確に同期。
  - ゲストの `/etc` 配下は既定で参照のみ。`--enable-standard-config-edits` を付与した場合に限り `/etc/hosts`・NTP・yum/td-agent レポジトリ・`/etc/profile` を書き換える。
  - ゲストコマンドを日本語説明付きでログ化し、完了時にサマリー表示。

---

## 事前準備

- Python 3.11 以上。
- `pip install -r requirements.txt` で導入可能な依存パッケージ。
- ソース / 宛先 vCenter への API 到達性と Guest Operations 権限。
- ゲスト OS の root または sudo 権限ユーザー。
- (任意) `nmcli` が利用可能な NetworkManager 環境。非対応時は自動的にシェルベースのフォールバックを使用。

代表的な環境変数:

| 変数名 | 役割 | 既定値 |
| --- | --- | --- |
| `VSPHERE_CLONE_LOG_LEVEL` | ログレベル (`DEBUG`/`INFO`/`WARNING` など) | `WARNING` |
| `VSPHERE_CLONE_KEEPALIVE_SECONDS` | vCenter keep-alive の送信間隔 (秒) | `240` |
| `REQUESTS_AVAILABLE` | REST SDK を強制利用 (`1`) / 強制無効化 (`0`) | 未設定 (ライブラリ有無で自動判定) |

---

## クイックスタート

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
git clone https://github.com/atsu-oku/vmMigration.git
cd vmMigration
python -m pip install --upgrade pip
pip install -r requirements.txt
python cloneAndVmotion.py
```

対話プロンプトでは以下を入力します。

- ソース / 宛先 vCenter の URL・ユーザー・パスワード
- 移行対象 VM 名
- ゲスト OS の root 認証情報および sudo 権限ユーザー

仮想環境を先に作成する等のパターンは `SETUP.md` を参照してください。

---

## ワークフロー

1. **Phase 0: 事前確認**  
   vCenter 認証、対象 VM の存在、VMware Tools 状態 (`guestToolsRunning`) を検査。
2. **Phase 1: データ収集**  
   REST SDK と SOAP Guest Operations を併用し、NIC / DNS / ルート / firewalld / NTP / yum / プロキシ情報を取得。JSON Schema で基本検証を実施。
3. **Phase 2: オペレーター確認**  
   収集結果と計画差分を提示し、承認 (`y`) を得てから変更フェーズへ進む。
4. **Phase 3: クローン & 再登録**  
   STG 側でクローン作成 → NIC 削除 → 宛先 vCenter への登録 → PRD ネットワークに合わせて NIC を再作成 → Storage vMotion。
5. **Phase 4: ゲスト設定**  
   既定では firewalld (rich-rule 追加) と iptables の SSH 制御のみを調整。`--enable-standard-config-edits` 指定時は `/etc/hosts`・NTP・yum/td-agent レポジトリ・`/etc/profile` も PRD 仕様へ整備し、各ファイルを `YYYYMMDD.bak` 名でバックアップ。
6. **Phase 5: 検証 & サマリー**  
   REST SDK または `nmcli` による設定検証、`ping` による疎通確認、bastion 用 SSH 設定の最終確認を実施し、結果を `[OK]` / `[WARN]` / `[ERROR]` とゲストコマンド一覧で出力。

---

## 主な機能

- **firewalld ゾーン同期**: STG で収集したゾーン定義を `FirewalldZonePlan` として検証し、PRD 側へ差分適用。必要に応じて `firewall-cmd --permanent` とバックアップを組み合わせて更新。
- **ゲートウェイ推定ロジック**: REST ルート情報とゲスト側のルーティングテーブルを突き合わせ、デフォルトゲートウェイと担当 NIC を推定。データ欠落時は PRD セグメント規則 (第 3 オクテット 160/162) に自動フォールバック。
- **コマンド実行ログ**: すべてのゲストコマンドを記録し、日本語の短い説明文とともにサマリーへ出力。`remember_command_description()` で説明を上書き可能。
- **段階的な設定更新**: `/etc` 配下の標準ファイルは既定で保護され、確認のみを行う。`--enable-standard-config-edits` を指定した場合に限り、`_write_guest_file` (mktemp + heredoc) を使用して PRD 仕様に書き換える。
- **柔軟な保護機構**: デフォルト保護対象 (`/etc/profile`, `/etc/hosts`, NTP 設定, `/etc/yum.repos.d`, `/etc/yum.repos.d/td.repo`) は手動で外さない限り書き換えない。追加保護は `--protect-guest-file` で登録可能。
- **ロールバック支援**: エラー時は VM 削除やデータストア清掃などのガイダンスを表示し、バックアップパスをサマリーに記録。

---

## 代表的な CLI オプション

```bash
python cloneAndVmotion.py \
  --source-vm web-stg-01 \
  --protect-guest-file /etc/yum.repos.d/local.repo \
  --enable-standard-config-edits
```

- `--source-vm`: 対話なしで STG VM 名を指定。
- `--protect-guest-file`: 追加で保護したいパスを登録。
- `--enable-standard-config-edits`: 既定の保護パス ( `/etc/hosts`, `/etc/profile`, NTP, yum/td-agent レポジトリ) の書き換えを許可。

---

## トラブルシューティング

- **疎通チェックが失敗する**: ログに記録された `ping` コマンドを再実行し、ゲスト→検証先のファイアウォール設定を確認。
- **ゲスト認証に失敗する**: VMware Tools の Guest Operations 権限と資格情報を再確認。root 認証が一度失敗すると自動で sudo ユーザーへ切り替えます。
- **DNS ミスマッチが表示される**: 期待値と実測値を比較し、差分が残る場合はゲスト側で再確認。双方空値であれば警告は出力されません。
- **vCenter セッションが切断される**: `VSPHERE_CLONE_KEEPALIVE_SECONDS` を短縮し、長時間の Storage vMotion 中もセッションが維持されるよう調整。
- **REST API が利用できない**: 自動的に SOAP Guest Operations へフォールバックします。REST の証明書やプロキシ設定は別途整備してください。

---

## 検証済みプラットフォーム

- RHEL 7.9 (NetworkManager + `nmcli`) - 2025-10-19 時点でエンドツーエンド移行を確認。

追加の検証結果があれば Issue / PR で共有してください。

---

## 参考資料

- `SETUP.md`: 詳細なセットアップ手順と環境変数。
- `CHANGELOG.md`: 変更履歴。
- `TODO.md`: 近々対応予定とバックログ。
- `docs/PROJECT_SPEC.md`: アーキテクチャ仕様 (日本語)。
- `docs/PROJECT_SPEC_PY.md`: Python 実装仕様 (日本語)。
- `docs/MIGRATION_FEATURES_EN.md`: 機能強化の解説 (日本語主体、英語補足)。
- `docs/REST_SOAP_USAGE.md`: REST / SOAP API 連携の詳細。

フィードバックや改善提案は Issue / PR でお寄せください。
