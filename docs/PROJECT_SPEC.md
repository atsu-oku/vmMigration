# vSphere 移行自動化 ― 機能仕様書

本書は STG→PRD 移行ツール群の全体像を説明します。目的、前提条件、処理フェーズ、ゲスト設定、関連モジュール、今後の拡張案を整理し、日本語で最新の実装に合わせています。

---

## 1. 概要

- **目的**: STG 側で稼働中の VM を PRD 環境へ移行し、必要なゲスト設定を PRD 規約に合わせる。
- **主要スクリプト**: `cloneAndVmotion.py` (エントリーポイント)、`network_utils.py`、`guest_commands.py`、`firewalld_manager.py`。
- **関連プロジェクト**: STG 差分抽出ツール `find_and_extract.sh` は姉妹リポジトリ `../find_and_extract_tool/` でメンテナンス。
- **対象ゲスト OS**: RHEL / CentOS 系 (firewalld, chrony/ntpd, yum, td-agent を想定)。

---

## 2. エンドツーエンドの流れ

### Phase 0 - 事前確認

- ソース / 宛先 vCenter へ認証し、対象 VM の存在と VMware Tools 状態 (`guestToolsRunning`) を確認。

### Phase 1 - データ収集

- NIC, IP, DNS, ルート、firewalld、NTP、yum レポジトリ、プロキシ設定を取得。
- JSON Schema (firewalld など) で構造検証し、不整合は警告として記録。

### Phase 2 - オペレーター確認

- 収集結果と差分プランを提示し、承認 (`y`) を取得してから変更フェーズへ進む。

### Phase 3 - クローン & 再登録

- ソース vCenter 上で VM をクローンし NIC を除去。
- クローンを宛先 vCenter へ登録し、PRD ネットワーク向けに NIC を再作成。
- Storage vMotion で最終データストアへ移動。

### Phase 4 - ゲスト構成

- 既定設定: firewalld rich-rule 追加と iptables の SSH 制御のみを変更。
- オプション: `--enable-standard-config-edits` を指定すると `/etc/hosts`、NTP、yum/td-agent レポジトリ、`/etc/profile` も PRD 化 (バックアップ付き)。
- NetworkManager が無い場合はシェルベースのレガシー手順へ自動切り替え。

### Phase 5 - 検証と完了

- REST SDK または `nmcli` で検証、`ping` による疎通確認、firewalld SSH 設定の再確認を実施。
- バックアップパスと `[OK]` / `[WARN]` / `[ERROR]` を含むサマリーを出力。

---

## 3. ゲスト構成タスク (_sync_prd_system_configuration)

| コンポーネント | 既定動作 | `--enable-standard-config-edits` 指定時 |
| --- | --- | --- |
| firewalld | `firewall-cmd --permanent` で Bastion 向け SSH rich-rule を追加し、必要時にリロード。`/etc/firewalld/zones/*.bak` を生成。 | 同左 |
| iptables | 既存 SSH 設定を確認し、必要なら `iptables -I` + `iptables-save > /etc/sysconfig/iptables` で反映。 | 同左 |
| `/etc/hosts` | 読み取りのみ。 | `transform_text_to_prd` / 差分確認後にバックアップ (`/etc/hosts-YYYYMMDD.bak`) を作成して書き換え。 |
| `/etc/profile` | 読み取りのみ。 | PRD プロキシを追加しバックアップ取得。 |
| NTP (`/etc/chrony.conf` 等) | 読み取りのみ。 | STG IP を PRD IP に変換し、バックアップを作成した上で更新。 |
| yum レポジトリ | 読み取りのみ。 | `vault.centos.org` へ書き換え、`*.repo-YYYYMMDD.bak` を保存。 |
| td-agent レポジトリ | 存在確認。 | v4 リポジトリを優先し、到達不可時は v3 へフォールバック。 |

> ファイルを書き換える際は `_write_guest_file` (mktemp + heredoc + mv) を使用し、既存パーミッションを維持。

---

## 4. 主なヘルパー (`network_utils.py`)

- `calculate_ip_stg_to_prd`: 第 3 オクテット 170-179 → 160-169 へ変換。
- `transform_text_to_prd`: STG の IP/ホスト名/ドメインを PRD 形式へ置換。
- `determine_prd_static_routes`: STG 情報から PRD 向け静的ルートを推定 (MNG セグメント優先)。
- `ensure_firewall_allows_ssh`: firewalld/iptables の SSH 例外を確認し、必要なら追加。
- `ensure_connection_activation`: `nmcli` 実行後の疎通確認を自動化。
- その他: DNS 抽出・REST SDK 検証・ルート比較などを提供。

---

## 5. TLS エラー対処 (_run_curl_with_tls_repairs)

1. curl 実行結果から TLS 由来の失敗か判定。
2. `update-ca-trust` / `yum reinstall ca-certificates nss curl` による CA 更新を試行。
3. 必要に応じて `curl-openssl` の導入や curl 再インストールを実行。
4. すべて失敗した場合は `[WARN]` を記録し、既存ファイルは変更せず次工程へ進む。

---

## 6. シェルツール `find_and_extract.sh`

- `scan` / `transform` / `rollback` サブコマンドで STG アーティファクトを検出・変換・復旧。
- 既定はドライランで差分プレビューを提示し、承認後に適用。
- 1 ファイルごとにバックアップとロールバックログ (TSV) を生成。
- Treasure Data (td-agent) リポジトリは v4 を優先し、到達不可時に v3 へ切り替え。
- `/tmp/<user>/find_and_extract/` に結果を保存。詳細は姉妹リポジトリのドキュメントを参照。

---

## 7. 例外・警告の扱い

- 対象ファイルが存在しない → スキップし、処理は継続。
- バックアップ失敗 / 書き込み失敗 → `[WARN]` を記録し、対象ファイルの更新を中止。
- 変換後も STG 情報が残る → `[WARN]` で通知し、手動確認を促す。
- TLS 修復が完了しない → `[WARN]` を残しつつ処理を継続。
- 重大な vCenter 操作失敗 (クローン、再登録など) → 即時中断しロールバック手順を案内。

---

## 8. 将来拡張案

1. **差分プレビュー統合**: `find_and_extract` の結果を Python ワークフローに統合し、自動承認フローを実装。
2. **プレビュー承認モード**: 変更予定を提示し、明示承認後に実適用。
3. **ロールバック支援強化**: 生成バックアップの一覧と復旧コマンドを自動提示。
4. **Python への移植**: bash ロジックを Python モジュールへ移し、統一 CLI を提供。
5. **CI/CD 連携**: ドライランや REST/SOAP 検証をパイプラインに組み込み、自動テストを拡充。

---

## 9. 主な成果物 / バックアップ

| パス | 内容 |
| --- | --- |
| `/etc/firewalld/zones/<zone>.xml-YYYYMMDD.bak` | firewalld ゾーン XML のバックアップ |
| `/etc/sysconfig/iptables-YYYYMMDD.bak` | iptables の保存結果 (ルール追加時) |
| `/etc/hosts-YYYYMMDD.bak` | `--enable-standard-config-edits` 指定時のバックアップ |
| `/etc/profile-YYYYMMDD.bak` | 同上 (プロキシ更新) |
| `/etc/yum.repos.d/*.repo-YYYYMMDD.bak` | 同上 (yum レポジトリ更新) |
| `/etc/yum.repos.d/td.repo-YYYYMMDD.bak` | 同上 (td-agent レポジトリ更新) |
| `/tmp/<user>/find_and_extract/` | 差分解析およびロールバックログ |

---

## 10. まとめ

このプロジェクトは vCenter API 操作とゲスト内コマンド実行を組み合わせ、STG→PRD 移行を自動化します。既定では firewalld と iptables の調整に留め、`--enable-standard-config-edits` を明示した場合のみ `/etc` 配下の標準構成を安全に書き換えます。バックアップと詳細ログ、警告のサマリー化により、監査性とロールバック容易性を両立しつつ今後の自動化拡張に備えています。
