# find_and_extract.sh - Operational Guide

このドキュメントでは、`find_and_extract.sh`（シェル版ツール）の用途と主なワークフローについて概要を説明します。Pythonベースの vSphere 移行仕様書と切り分けるため、シェルツール単体で必要となる最低限の情報のみを記載しています。詳細な利用手順は `docs/FIND_AND_EXTRACT_TOOL.md` を参照してください。

> **使用形態について**
> `find_and_extract.sh` は Git から取得後、担当者が対象 VM に手動コピーして利用する運用を想定しています。SCP などで `/tmp` または任意ディレクトリへ転送し、権限を確認した上で `scan` / `transform` / `rollback` の各サブコマンドを実行してください。

## 概要

- **目的**: STG 環境で使用されている定義を検出し、PRD 用に変換・ロールバックできる形で整理すること。
- **想定利用シーン**: 事前調査（`scan`）、自動変換（`transform`）、変換結果の即時復元（`rollback`）。
- **ログ出力先**: `/tmp/<ユーザー名>/find_and_extract/` 配下にスキャン結果・変換ログ・ロールバック用ログを出力。

## サブコマンド

| サブコマンド | 概要 |
| --- | --- |
| `scan` (デフォルト) | 指定ディレクトリ以下を走査し、STG/PRD の定義が混在するファイルを抽出してレポートを生成。 |
| `transform` | STG 文字列や IP / ホスト名のパターンを PRD 向けに変換。`--apply` 指定時にバックアップ＆ロールバックログを作成。 |
| `rollback` | `transform --apply` が出力したログを指定し、ファイルを元に戻す。`--file` で対象を限定可能。 |

### 代表的なオプション

- `-v / --verbose` : 詳細ログを表示。
- `--skip-backup-files` : バックアップらしきファイルをスキャン対象から除外。
- `--dry-run` : 変換結果のみ表示し、ファイルは変更しない（デフォルト）。
- `--apply` : 変換を実際に適用（適用前に yes/no で確認）。
- `--file <path>` : `rollback` 時に特定ファイルのみを復元。

## 変換（transform）時の挙動

- STG 範囲の IP（例: `172.16.170.x`）を PRD 範囲に変換。
- ホスト名末尾 `s` -> `p`、および `stg` を含むトークンを `prd` に置換。
- HTTPD fuel/app/config/newproduction の存在および `app.php` 等 5 ファイルを検証。
- 実際に書き換える場合はバックアップを同じディレクトリに `*.bak_<timestamp>` 形式で生成。
- 変換ログ（ロールバック用）を `/tmp/<ユーザー名>/find_and_extract/<hostname>_<timestamp>_transform.log` に出力。

## ロールバック（rollback）

- ログを指定することでバックアップから復元。
- `--file` を使うとログ中の特定ファイルのみ復元可能。
- 復元可否はコンソールにサマリーとして表示。

## 注意事項

- 変換ログとバックアップファイルは別々に管理されるため、必要に応じて結果を保全しておくこと。
- スキャン／変換対象に `/var` を指定すると、`/var/www/com/ipet-ins/<system>/fuel/app/config/newproduction` の存在チェックも実施。
- 誤変更防止のため、`/etc/nginx/nginx.conf` および `/etc/httpd/httpd.conf` は自動処理から除外される。

```bash
./find_and_extract.sh scan /etc
./find_and_extract.sh transform --dry-run /etc
./find_and_extract.sh transform --apply /var
./find_and_extract.sh rollback --file /etc/hosts /tmp/<user>/find_and_extract/<host>_<ts>_transform.log
```

詳細なワークフローやログ構造については `FIND_AND_EXTRACT_TOOL.md` を参照してください。
