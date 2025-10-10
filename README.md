# vmMigration

vCenter 上の VM を STG 環境から PRD 環境へ移行するためのスクリプト群です。gptCloneAndVmotion.py がメインスクリプトです。

## 主な機能
- vCenter(ソース/宛先)への認証と事前チェック
- VM クローンの作成と登録解除（ソース側）→ 宛先 vCenter で登録
- NIC 再作成とネットワーク再接続（STG→PRD の命名変換に対応）
- ゲスト OS の IP/DNS/GW 設定（NetworkManager/nmcli または ifcfg-*）
- 最終ストレージ vMotion
- 途中失敗時のロールバック処理（宛先 VM 削除、ソース側ファイル削除の案内）

## 前提
- Python 3.8+
- 主要ライブラリ: pyvmomi、（任意）equests
- vCenter API への接続権限
- ゲスト OS: RHEL(CentOS) 5〜9 を想定

## 設定（スクリプト先頭の定数）
- VCSA_HOST_SOURCE, VCSA_HOST_DEST, VCSA_USER, VCSA_PORT
- TARGET_DATASTORE_NAME, TARGET_DATASTORE_NAME_FINAL, TARGET_CLUSTER_NAME
- GUEST_ROOT_USER, GUEST_ADMIN_USER（パスワードは実行時入力）

## 実行とフェーズ
1. 認証（ソース/宛先）
2. 情報収集（NIC, ルート, DNS, データストア）
3. クローン作成 → NIC 削除 → 登録解除（ソース）
4. 宛先 vCenter で登録
5. NIC 再作成・接続（STG→PRD のネットワーク名変換）
6. ゲスト OS の IP 設定
7. 最終ストレージ vMotion

## STG → PRD 変換ルール（IP）
- 第三オクテット 170–179 を 160–169 に 10 減算して写像（例: 192.168.170.10 → 192.168.160.10）
- デフォルトゲートウェイおよび DNS も同様に変換
- 170–179 以外の値が入力された場合は ValueError を送出

## ゲスト OS のネットワーク設定
- RHEL/CentOS 7–9（NetworkManager/nmcli）
  - 旧接続プロファイル（connection.id / connection.interface-name / mac-address）を網羅削除
  - /etc/NetworkManager/system-connections/ の該当キー・ファイルも .YYYYmmdd_HHMM.bak にバックアップの上で削除
  - 接続 ID は ifname と同一に作成し、utoconnect yes、ipv4.ignore-auto-dns yes を付与
- RHEL/CentOS 5–6（ifcfg-*）
  - 旧 ifcfg-* を同ディレクトリに .YYYYmmdd_HHMM.bak でバックアップ → 新規 ifcfg-<ifname> を生成
  - oute-<ifname> を生成（default via <GW> dev <IFACE> を含む）。必要に応じて既存ルートを STG→PRD 変換し追記
  - ifdown/ifup もしくは 
etwork 再起動で反映

## コマンド実行の表示ポリシー（execute_command_in_guest）
- 発行前にコマンドを提示
- 発行後に実行ユーザー・終了コード・標準出力/標準エラーを提示
- 結果の明示（正常終了/異常終了）。異常時は理由（stderr 等）を表示し例外送出

## ロールバック
- 宛先側 VM の削除（必要に応じてパワーオフ後）
- ソース側データストアのクローン残骸は、同意の上で削除タスクを実行

## 既知の注意点
- 文字コード（SJIS/UTF-8）の混在でログが文字化けする場合があります。実行コンソールを UTF-8 に設定することを推奨
- udev 永続 NIC 名や cloud-init が存在する環境では、再起動時に設定が上書きされる可能性があります（別途無効化が必要）

## 開発・PR
- リファクタ（トップレベル処理を main() に集約）: PR #1
  - https://github.com/atsu-oku/vmMigration/pull/1

## インストール（推奨手順）
- Python 仮想環境
  - Windows: py -3 -m venv .venv && .venv\\Scripts\\activate
  - Linux/macOS: python3 -m venv .venv && source .venv/bin/activate
- 依存パッケージ
  - pip install --upgrade pip
  - pip install pyvmomi requests

## 実行例
`ash
# Windows (PowerShell)
py -3 gptCloneAndVmotion.py
# Linux
python3 gptCloneAndVmotion.py
`
- 実行時に以下を対話で入力します
  - vCenter(ソース/宛先)のパスワード
  - クローン元 VM 名
  - ゲスト OS の root/admin パスワード
- 主要フェーズはコンソールに逐次表示されます（NIC再作成、IP適用、vMotion 等）。

## 権限と前提
- vCenter: クローン/登録/移行が可能な権限
- ゲスト OS: root または sudo 可能な admin
- VMware Tools: 実行中（Guest Operations 利用のため）

## 既知の制約/注意点
- NetworkManager (RHEL/CentOS 7–9)
  - 
mcli が必要。該当する既存接続は UUID/ID/ifname/mac で網羅削除します
  - キー・ファイル（nmconnection）は .YYYYmmdd_HHMM.bak で同ディレクトリにバックアップ
- ifcfg 系 (RHEL/CentOS 5–6)
  - ifcfg-<ifname> と oute-<ifname> を生成。既存は .YYYYmmdd_HHMM.bak でバックアップ
  - ifdown/ifup または 
etwork 再起動で反映
- 永続 NIC 名/udev ルールや cloud-init が有効な環境では再起動で上書きされることがあります
- DVS/ポートグループ名の STG→PRD 変換は命名規則に依存（STG→PRD）
- 文字化け対策
  - Windows ターミナル: chcp 65001 または UTF-8 設定

## トラブルシュート
- ゲスト操作エージェント未準備（GuestOperationsUnavailable）
  - VMware Tools の起動を確認し、数十秒待って再試行
- 認証失敗（InvalidGuestLogin）
  - root/admin 資格情報を再確認。admin は sudo 権限が必要
- IP/DNS が戻る
  - 旧プロファイル/ifcfgが残存していないか確認（.bak が生成されているか、nmcli の再読み込み済みか）
- ルートが不足
  - nmcli 環境では ipv4.routes、ifcfg 環境では oute-<ifname> を確認し、必要な経路を追記

## セキュリティ
- 文字列やファイルにパスワードを残さない運用を推奨（対話入力）
- リポジトリに機密情報をコミットしないでください
