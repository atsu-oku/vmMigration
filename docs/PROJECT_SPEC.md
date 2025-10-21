# vSphere Migration Automation – 機能仕様書

## 概要

- **目的**: vSphere 上のステージング VM をプロダクション環境へ移行し、ゲスト OS 内設定を PRD 仕様に揃える。  
- **主要ファイル**: `cloneAndVmotion.py`, `network_utils.py`, 補助ツール `find_and_extract_3.2.4.0.sh`。  
- **対象 OS**: RHEL/CentOS 系（firewalld, chrony/ntpd, yum repo, td-agent 等を前提）。

---

## 全体フロー

┌───────────────────────────────────────────────────────────┐
│ Phase 0 : Pre-flight                                      │
│   • vCenter 認証テスト (source/destination)                │
│   • 対象 VM 存在確認 + VMware Tools 状態チェック           │
├───────────────────────────────────────────────────────────┤
│ Phase 1 : 情報収集                                         │
│   • NIC / IP / DNS / ルート / firewall / NTP / repo 等収集 │
│   • STG→PRD 変換ルール適合性の判定                         │
├───────────────────────────────────────────────────────────┤
│ Phase 2 : ユーザー確認                                     │
│   • 差分情報を表示し、移行実行を承認                      │
│   • find_and_extract (将来) で自動差分レポート             │
├───────────────────────────────────────────────────────────┤
│ Phase 3 : クローン & 登録                                  │
│   • クローン作成 → 登録 → NIC 再構成 → Storage vMotion     │
├───────────────────────────────────────────────────────────┤
│ Phase 4 : ゲスト設定整備（本書の主題）                     │
│   • /etc/hosts, firewalld, NTP, repo, proxy, iptables 等   │
├───────────────────────────────────────────────────────────┤
│ Phase 5 : 検証・後処理                                     │
│   • SDK 検証 / 警告集約                                    │
│   • バックアップとロールバック案内                         │
└───────────────────────────────────────────────────────────┘

---

## ゲスト設定整備フロー（`_sync_prd_system_configuration`）

1. **`/etc/hosts`**
   - `transform_text_to_prd` を複数回適用。  
   - STG パターンが残る場合は警告。  
   - `mktemp` → `mv` の安全手順で書き換え、`/etc/hosts-YYYYMMDD.bak` を保存。

2. **firewalld**
   - ゾーン XML を取得し、sources / rich rules を PRD アドレスへ変換。  
   - Heartbeat ゾーンの SSH ルールを撤去。  
   - ゾーンに紐付く interface を保持し、変換後に再アタッチ。  
   - `firewall-cmd --reload` で反映。

3. **chrony / ntpd**
   - 代表的な設定ファイル（`/etc/chrony.conf`, `/etc/ntp.conf` など）を走査。  
   - `transform_text_to_prd` + 正規表現置換で 172.16.17x.*を 172.16.16x.* へ強制変換。  
   - バックアップ作成後に書換え。

4. **CentOS repo**
   - `/etc/yum.repos.d/*.repo` をすべて読み込み、`mirrorlist` をコメント化。  
   - `baseurl` を `https://vault.centos.org/centos/` に統一。  
   - TLS エラー時は CA 更新 → `curl-openssl` 導入を試み、失敗時は警告のみ。

5. **td-agent repo (`/etc/yum.repos.d/td.repo`)**
   - releasever/arch を rpm マクロから動的取得。  
   - v4 リポジトリを curl 検査し、不可なら v3 にフォールバック。  
   - バックアップ→新規作成。

6. **iptables**
   - `/etc/sysconfig/iptables` を PRD 仕様へ変換。  
   - `systemctl reload iptables`（または `service iptables reload`）をリトライ実行。

7. **プロキシ設定 (`/etc/profile`)**
   - PRD 用 proxy export を追記。  
   - 反映後 `. /etc/profile` を呼び出し、`env | grep -i http` で検証。  
   - 未反映なら警告。

> すべての書換えは `mktemp` → `mv` 方式で行い、既存ファイルの権限・所有者を引き継ぐ。  
> バックアップは同じディレクトリに `-YYYYMMDD.bak` 形式で保存。

---

## `network_utils.py` 主な補助関数

- `calculate_ip_stg_to_prd(ip)`: STG アドレス（第3オクテット 170–179）を PRD アドレスへ変換。  
- `transform_text_to_prd(text)`: テキスト内の STG IP／ホスト名末尾 s／`ipet-ins` ドメイン等を PRD 形へ。  
- `determine_prd_static_routes(...)`: NIC 情報と静的ルートから MNG セグメント NIC (第3オクテット 161/163) を優先的に選定。  
- `ensure_firewall_allows_ssh(exec, source_ip)`: SSH 許可ルールを追加し、Heartbeat ゾーンでは削除。  
- `ensure_connection_activation(...)`: nmcli 接続の up を保証し、ping で疎通確認。  
- その他、DNS 抽出や SDK 連携など移行後検証を支援する多数のユーティリティ。

---

## TLS エラー対策（`_run_curl_with_tls_repairs`）

1. curl 実行 → 失敗時に TLS エラー判定。  
2. `update-ca-trust` や `yum reinstall ca-certificates nss curl` で CA バンドルを更新。  
3. それでも失敗した場合、`curl-openssl` の導入や `curl` 再インストールを試行。  
4. すべて失敗した場合は警告を出して処理を継続。

---

## 補助ツール `find_and_extract_3.2.4.0.sh`

- 指定ディレクトリを走査し、**現行基盤** / **新基盤** の条件セットで差分を抽出。  
- stg/prod/other へ分類したヒットログを `/tmp/<user>/<script名>/` 配下に出力。  
- 将来的には「改修対象ファイルの特定 → 自動書換え」の前段として活用予定。  
- ログにはヒット箇所 (行番号付き)・条件名・カテゴリ別リストなどを含む。

---

## 成功・警告の扱い

- 変換対象ファイル未存在 → スキップ。  
- バックアップ不可・書き込み不可 → `[WARN]` を出し対象処理は失敗扱い（全体の成功フラグを下げる）。  
- 変換後も STG パターン残存 → `[WARN]` としてユーザー確認を促す。  
- TLS 到達不能 → CA 更新を試行後でもダメなら `[WARN]`; 処理は続行。  
- 環境変数反映できず → `[WARN] Proxy environment variables may not be active…`.

---

## 将来拡張アイデア

1. **書換え前の差分レポート**  
   - `find_and_extract` で抽出した差分を Python から表示し、対象範囲を限定。
2. **自動修正のプレビューモード**  
   - 変更予定 diff の提示 → ユーザー承認後に書換え実行。  
3. **ロールバック支援**  
   - 作成済みバックアップ一覧と復元コマンドを自動提示。  
4. **Python へのロジック移植**  
   - bash スクリプトのコア検索ロジックを Python モジュールとして再実装し、統一 CLI へ発展。  
5. **CI/CD 対応**  
   - 変換処理の dry-run・ユニットテスト・SDK レベルの検証自動化。

---

## ASCII Art Notes

 _______________________
|  VM Migration Engine  |
|-----------------------|
|  Clone + vMotion      |
|  Guest Reconfigure    |
|  Firewalld / NTP      |
|  Repo & Proxy Sync    |
|  Reports & Backups    |
|_______________________|

        /\
       /  \
      /____\   ← PRD-ready VM

## 付録: 主要出力物一覧

| ファイル | 内容 |
|-----------|------|
| `/etc/hosts-YYYYMMDD.bak` | 書換え前の hosts バックアップ |
| `/etc/firewalld/zones/*.bak` | firewalld ゾーン XML バックアップ |
| `/etc/sysconfig/iptables-YYYYMMDD.bak` | iptables バックアップ |
| `/etc/profile-YYYYMMDD.bak` | proxy 追加前のバックアップ |
| `/etc/yum.repos.d/*.repo-YYYYMMDD.bak` | CentOS repo バックアップ |
| `/etc/yum.repos.d/td.repo-YYYYMMDD.bak` | td-agent repo バックアップ |
| `/tmp/<user>/find_and_extract_3.2.4.0/` | 差分調査ログ |

---

## まとめ

本プロジェクトは vSphere VM の STG→PRD 移行を支援する自動化基盤であり、  
ネットワーク設定／ファイアウォール／NTP／リポジトリ／プロキシなど、  
ゲスト OS 内で必要な構成変更を包括的に行う。  
安全なバックアップ・TLS 再試行・警告通知を備え、将来的な差分検出／自動修正の拡張も視野に入っている。
   ________
  /  ____/\
 /__/ __\/ \
\  \ \_/\/
 \__\__\/  ← STG
   ||
   ||  (cloneAndVmotion.py)
   \/
  /\/\  ____
 / /\/ / __ \
 \/ / / / _` |
   \/ | | (_| |
       \ \__,_|
        \____/  ← PRD
```
