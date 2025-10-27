#!/usr/bin/env bash
###############################################################################
#  search_and_log.sh
#
#  指定したパス以下をスキャンし、特定の文字列を含むファイルを検出するスクリプト。
#  検出結果は「新基盤定義ログ」と「現行基盤定義ログ」の2種類に出力される。
#
# -----------------------------------------------------------------------------
#  改変履歴
# -----------------------------------------------------------------------------
#  バージョン履歴はリポジトリの CHANGELOG_find_and_extract.md を参照してください。
###############################################################################

# --- スクリプトバージョン ---
SCRIPT_VERSION="3.4.0.0"

# === 出力ディレクトリを /tmp に固定 ==================================
# スクリプト名から拡張子(.sh)を除いた部分を取得
SCRIPT_NAME_BASENAME=$(basename "$0" .sh)
# 出力先を "/tmp/<ユーザー名>/<スクリプト名>" に設定。USER変数が未定義の場合は 'nobody' を使用
OUTPUT_DIR="/tmp/${USER:-nobody}/${SCRIPT_NAME_BASENAME}"
# -p オプションで、親ディレクトリが存在しない場合も再帰的に作成
mkdir -p "${OUTPUT_DIR}" || { echo "Error: cannot create ${OUTPUT_DIR}" >&2; exit 1; }
# 作成したディレクトリのパーミッションを700に設定し、所有者のみがアクセスできるようにする
chmod 700 "${OUTPUT_DIR}" 2>/dev/null || true
# =====================================================================

# --- 初期ロケール設定と復元準備 ---
# スクリプト実行前のロケール設定を退避
ORIGINAL_LANG="${LANG:-}"
ORIGINAL_LC_ALL="${LC_ALL:-}"

# --- 一時ファイル変数 ---
# スクリプト全体で使用する一時ファイルのパスを保持する変数を初期化
CURRENT_INFRA_HITS_TEMP=""
NEW_INFRA_HITS_TEMP=""
NEW_INFRA_STG_HITS_TEMP=""
NEW_INFRA_PROD_HITS_TEMP=""
NEW_INFRA_OTHER_HITS_TEMP=""
BOTH_HITS_TEMP=""
PERMISSION_CHECK_TEST_FILE_TEMP=""

# --- 後片付け関数 ---
# スクリプト終了時に呼び出され、作成した一時ファイルをすべて削除する
cleanup() {
    rm -f "$CURRENT_INFRA_HITS_TEMP" "$NEW_INFRA_HITS_TEMP" \
          "$NEW_INFRA_STG_HITS_TEMP" "$NEW_INFRA_PROD_HITS_TEMP" "$NEW_INFRA_OTHER_HITS_TEMP" \
          "$BOTH_HITS_TEMP" "$PERMISSION_CHECK_TEST_FILE_TEMP" 2>/dev/null
    # サマリー表示用に生成される中間ファイルも削除
    rm -f "$NEW_INFRA_OTHER_HITS_TEMP.final" "$NEW_INFRA_OTHER_HITS_TEMP.paths" "$NEW_INFRA_OTHER_HITS_TEMP.finalpaths" 2>/dev/null
    for temp_path in "${TRANSFORM_TEMP_PATHS[@]}"; do
        if [ -n "$temp_path" ]; then
            rm -f "$temp_path" 2>/dev/null
        fi
    done
    for diff_path in "${TRANSFORM_DIFF_PATHS[@]}"; do
        if [ -n "$diff_path" ]; then
            rm -f "$diff_path" 2>/dev/null
        fi
    done
    # 退避しておいたロケール設定を復元
    export LANG="$ORIGINAL_LANG"
    export LC_ALL="$ORIGINAL_LC_ALL"
}
# trapコマンドは、一時ファイルが確実に作成された後に設定する

# --- ロケール設定 ---
# grepやsortの挙動を安定させるため、ロケールをC(ASCII)に設定
export LC_ALL=C

# --- 言語設定とメッセージ定義 ---
# 環境変数LANGが "ja_JP" で始まる場合、メッセージを日本語に設定
if [[ "$ORIGINAL_LANG" == ja_JP* ]]; then
    # 日本語メッセージ
    printf -v MSG_USAGE_LINE1_EXTENDED "使用方法:\n  %s [scan|transform|rollback] [オプション] <対象パス>\n  %s --deletelogs\n\nサブコマンド:\n  scan        現行/新基盤定義をスキャン (従来動作)\n  transform   新基盤(STG)の値をPRDに変換 (デフォルトはドライラン)\n  rollback    変換ログを基にロールバックを実行\n\n主なオプション:\n  -v, --verbose          詳細なログを表示\n  --skip-backup-files   バックアップと思われるファイルを除外\n  --dry-run             変換を出力のみで実施 (transformのデフォルト)\n  --apply               変換を実際に適用 (確認プロンプトあり)" "$0" "$0"
    MSG_ERROR_INVALID_OPTION="無効なオプションまたはオプションの組み合わせです。"
    MSG_ERROR_INVALID_INPUT="入力が不正です。yes または no を入力してください。"
    MSG_ERROR_INVALID_IP_FORMAT="ファイル「%s」の %s 行目に不正な形式のIPアドレス \"%s\" が見つかりました。\n"
    MSG_VERBOSE_PREFIX="[詳細] "
    MSG_VERBOSE_SCANNING_FILE="ファイルをスキャン中: "
    MSG_VERBOSE_SKIPPING_BACKUP_FILE="バックアップファイルをスキップ: "
    MSG_SUMMARY_TOTAL_FILES_SCANNED="スキャンした総ファイル数: %d"
    MSG_ERROR_PREFIX="エラー: "
    MSG_ERROR_BASH_VERSION_TOO_LOW="このスクリプトの実行にはBashバージョン %s.0 以上が必要です。現在のバージョンは %s.%s です。\n"
    MSG_SEARCH_PATH_NOT_SPECIFIED="検索対象パスが指定されていません。"
    MSG_SEARCH_PATH_NOT_EXIST_OR_DIR="検索対象パス '%s' が存在しないか、ディレクトリではありません。\n"
    MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION="引数が多すぎるか、無効な組み合わせです (--deletelogs は単独で指定してください)。"
    MSG_TEMP_FILE_CREATION_FAILED="一時ファイルの作成に失敗しました (%s)。\n"
    MSG_ERROR_WRITE_PERMISSION_DENIED="${OUTPUT_DIR} への書き込み権限がありません"
    MSG_SCRIPT_INFO_HEADER="--- スクリプト情報 ---"
    MSG_SCRIPT_NAME_LABEL="スクリプト名:"
    MSG_SCRIPT_VERSION_LABEL="スクリプトバージョン:"
    MSG_EXECUTION_DATETIME_LABEL="実行日時:"
    MSG_SEARCH_START="検索を開始します..."
    MSG_SEARCH_PATH_LABEL="検索対象パス: "
    MSG_NEW_INFRA_LOG_FILE_LABEL="新基盤定義ログファイル: "
    MSG_CURRENT_INFRA_LOG_FILE_LABEL="現行基盤定義ログファイル: "
    MSG_LOG_GENERATED_LOCATION="ログの出力先： ${OUTPUT_DIR}"
    MSG_LOG_DELETE_MODE="ログファイル削除モードです。"
    MSG_LOG_DELETE_NOT_FOUND="削除対象のログファイルは見つかりませんでした。"
    MSG_LOG_DELETE_FOUND="以下のログファイルが見つかりました："
    MSG_LOG_DELETE_CONFIRM="これらのログファイルを削除しますか？ (yes/no): "
    MSG_LOG_DELETED="削除しました: "
    MSG_LOG_DELETE_FAILED="エラー: %s の削除に失敗しました。\n"
    MSG_LOG_DELETE_COMPLETED="ログファイルの削除処理が完了しました。"
    MSG_LOG_DELETE_CANCELLED="ログファイルの削除はキャンセルされました。"
    MSG_DELETELLOGS_NOT_ALLOWED="--deletelogs は transform サブコマンドでは使用できません。"
    MSG_ERROR_APPLY_ONLY_TRANSFORM="--apply は transform サブコマンドでのみ利用できます。"
    MSG_ERROR_DRY_RUN_ONLY_TRANSFORM="--dry-run は transform サブコマンドでのみ利用できます。"
    MSG_TRANSFORM_MODE_HEADER="--- 変換モード ---"
    MSG_TRANSFORM_DRY_RUN_NOTICE="ドライランで実行します。ファイルには変更を加えません。"
    MSG_TRANSFORM_APPLY_NOTICE="変更点を検出し、確認後に適用します。"
    MSG_TRANSFORM_SUMMARY_HEADER="--- 変換サマリー ---"
    MSG_TRANSFORM_FILE_WOULD_CHANGE="変更予定: %s"
    MSG_TRANSFORM_FILE_CHANGED="変更しました: %s (バックアップ: %s)"
    MSG_TRANSFORM_FILE_FAILED="エラー: %s の変更に失敗しました。"
    MSG_TRANSFORM_APPLY_CONFIRM="変更対象 %d 件に対して更新を適用しますか？ (yes/no): "
    MSG_TRANSFORM_APPLY_CANCELLED="変更はキャンセルされました。"
    MSG_TRANSFORM_NO_CHANGES="変更対象のファイルは見つかりませんでした。"
    MSG_TRANSFORM_DRY_RUN_COMPLETED="ドライランが完了しました。"
    MSG_TRANSFORM_APPLY_COMPLETED="変換を完了しました。"
    MSG_TRANSFORM_APPLY_SKIPPED="適用処理をスキップしました。"
    MSG_TRANSFORM_FAILED_HEADER="--- 失敗したファイル ---"
    MSG_TRANSFORM_DIFF_HEADER="--- 差分: %s ---"
    MSG_ERROR_TRANSFORM_EXECUTION="変換処理でエラーが発生しました: %s"
    MSG_TRANSFORM_TOTAL_CHANGED="変更対象: %d 件"
    MSG_TRANSFORM_TOTAL_FAILED="失敗: %d 件"
    MSG_TRANSFORM_NEWPROD_BASE_MISSING="期待する基底ディレクトリ %s が見つかりません。"
    MSG_TRANSFORM_NEWPROD_DIR_NOT_FOUND="/var/www/com/ipet-ins/ 配下に newproduction 設定ディレクトリが見つかりません。"
    MSG_TRANSFORM_NEWPROD_FILES_MISSING="%s に必要な設定ファイルが不足しています: %s"
    MSG_TRANSFORM_LOG_SAVED="ロールバックログを %s に保存しました。"
    MSG_ROLLBACK_MODE_HEADER="--- ロールバックモード ---"
    MSG_ROLLBACK_LOG_NOT_FOUND="指定されたロールバックログが見つかりません: %s"
    MSG_ROLLBACK_INVALID_LINE="ロールバックログの形式が不正です: %s"
    MSG_ROLLBACK_BACKUP_MISSING="バックアップが存在しません: %s"
    MSG_ROLLBACK_FILE_RESTORED="復元しました: %s"
    MSG_ROLLBACK_FILE_FAILED="復元に失敗しました: %s"
    MSG_ROLLBACK_NO_ENTRIES="ロールバックログに復元対象がありません。"
    MSG_ROLLBACK_SUMMARY="復元: %d 件 / 失敗: %d 件"
    MSG_SEARCH_COMPLETED_PRIMARY="検索が完了しました。"
    MSG_CHECK_NEW_INFRA_LOG="新基盤の定義に関するログは %s を確認してください。\n"
    MSG_CHECK_CURRENT_INFRA_LOG="現行基盤の定義に関するログは %s を確認してください。\n"
    MSG_CURRENT_INFRA_HITS_HEADER="--- 現行基盤の定義を含むファイル ---"
    MSG_NEW_INFRA_HITS_HEADER_STG="--- 新基盤の定義を含むファイル (Staging ホスト名) ---"
    MSG_NEW_INFRA_HITS_HEADER_PROD="--- 新基盤の定義を含むファイル (Production ホスト名) ---"
    MSG_NEW_INFRA_HITS_HEADER_BY_COND="--- 新基盤の定義を含むファイル (%s) ---"
    MSG_BOTH_LOGS_HITS_HEADER="--- 注意: 以下のファイルは現行・新基盤の両方の定義を含んでいます ---"
    MSG_NO_HITS="該当なし"
    MSG_FILE_LABEL="ファイル: "
    MSG_CONDITION_LABEL_CURRENT="条件(現行基盤)"
    MSG_CONDITION_LABEL_NEW="条件(新基盤)"
    MSG_IP_CONDITION_CURRENT_DESC="IPアドレス (現行)"
    MSG_IP_AWS_CONDITION_CURRENT_DESC="IPアドレス (AWS)"
    MSG_ENV_CONDITION_CURRENT_DESC="環境文字列 (現行)"
    MSG_HOSTNAME_CONDITION_CURRENT_DESC="ホスト名パターン (現行)"
    MSG_IPTP_CONDITION_CURRENT_DESC="文字列 \"iptp\""
    MSG_PFS_CONDITION_CURRENT_DESC="文字列 \"pfs01\" または \"pfs02\""
    MSG_CONV_CONDITION_CURRENT_DESC="文字列 \"dconv\""
    MSG_CONV_CONDITION_NEW_DESC="文字列 \"conv0\""
    MSG_IP_CONDITION_NEW_DESC="IPアドレス (新)"
    MSG_HOSTNAME_STG_CONDITION_NEW_DESC="新ホスト名 (Staging)"
    MSG_HOSTNAME_PROD_CONDITION_NEW_DESC="新ホスト名 (Production)"
    MSG_ENV_CONDITION_NEW_DESC="環境文字列 (新)"
    MSG_IPETFS_CONDITION_NEW_DESC="文字列 \"ipet-fs\""
    MSG_COMMENT_IGNORED_LABEL="(コメント行(#)は無視)"
    MSG_MATCHES_LABEL="該当箇所"
    MSG_CONTEXT_LINES_LABEL="前後5行を含む"
    MSG_HIT_LINE_PREFIX="ヒット行"
    MSG_MYHOSTNAME="実行サーバ名:"
    SEPARATOR_LINE_LONG="================================================================================"
    SEPARATOR_LINE_SHORT="--------------------------------------------------------------------------------"
else
    # English Messages
    printf -v MSG_USAGE_LINE1_EXTENDED "Usage:\n  %s [scan|transform|rollback] [options] <target>\n  %s --deletelogs\n\nSubcommands:\n  scan        Scan for matching definitions (default)\n  transform   Rewrite STG values to PRD (dry-run by default)\n  rollback    Restore files using a transform log\n\nKey options:\n  -v, --verbose        Display verbose logging\n  --skip-backup-files Skip files that look like backups\n  --dry-run           Run without applying changes (transform default)\n  --apply             Apply replacements after confirmation" "$0" "$0"
    MSG_ERROR_INVALID_OPTION="Invalid option or combination of options."
    MSG_ERROR_INVALID_INPUT="Invalid input. Please enter yes or no."
    MSG_ERROR_INVALID_IP_FORMAT="Error: Invalid IP address format \"%s\" found in file \"%s\" on line %s.\n"
    MSG_VERBOSE_PREFIX="[VERBOSE] "
    MSG_VERBOSE_SCANNING_FILE="Scanning file: "
    MSG_VERBOSE_SKIPPING_BACKUP_FILE="Skipping backup file: "
    MSG_SUMMARY_TOTAL_FILES_SCANNED="Total files scanned: %d"
    MSG_ERROR_PREFIX="Error: "
    MSG_ERROR_BASH_VERSION_TOO_LOW="This script requires Bash version %s.0 or higher. Your version is %s.%s.\n"
    MSG_SEARCH_PATH_NOT_SPECIFIED="Search path not specified."
    MSG_SEARCH_PATH_NOT_EXIST_OR_DIR="Search path '%s' does not exist or is not a directory.\n"
    MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION="Too many arguments or invalid combination (--deletelogs must be specified alone)."
    MSG_TEMP_FILE_CREATION_FAILED="Failed to create temporary file (%s).\n"
    MSG_ERROR_WRITE_PERMISSION_DENIED="No write permission to the output directory: ${OUTPUT_DIR}"
    MSG_SCRIPT_INFO_HEADER="--- Script Information ---"
    MSG_SCRIPT_NAME_LABEL="Script Name:"
    MSG_SCRIPT_VERSION_LABEL="Script Version:"
    MSG_EXECUTION_DATETIME_LABEL="Execution Datetime:"
    MSG_SEARCH_START="Starting search..."
    MSG_SEARCH_PATH_LABEL="Search Path: "
    MSG_NEW_INFRA_LOG_FILE_LABEL="New Infra Defs Log: "
    MSG_CURRENT_INFRA_LOG_FILE_LABEL="Current Infra Defs Log: "
    MSG_LOG_GENERATED_LOCATION="(generated in ${OUTPUT_DIR})"
    MSG_LOG_DELETE_MODE="Log file deletion mode."
    MSG_LOG_DELETE_NOT_FOUND="No log files found to delete."
    MSG_LOG_DELETE_FOUND="Found the following script-generated log files:"
    MSG_LOG_DELETE_CONFIRM="Delete these log files? (yes/no): "
    MSG_LOG_DELETED="Deleted: "
    MSG_LOG_DELETE_FAILED="Error: Failed to delete %s.\n"
    MSG_LOG_DELETE_COMPLETED="Log file deletion process completed."
    MSG_LOG_DELETE_CANCELLED="Log file deletion cancelled."
    MSG_DELETELLOGS_NOT_ALLOWED="--deletelogs cannot be used with the transform subcommand."
    MSG_ERROR_APPLY_ONLY_TRANSFORM="--apply is available only with the transform subcommand."
    MSG_ERROR_DRY_RUN_ONLY_TRANSFORM="--dry-run is available only with the transform subcommand."
    MSG_TRANSFORM_MODE_HEADER="--- Transform Mode ---"
    MSG_TRANSFORM_DRY_RUN_NOTICE="Running in dry-run mode. No files will be modified."
    MSG_TRANSFORM_APPLY_NOTICE="Detected changes will be reviewed and applied after confirmation."
    MSG_TRANSFORM_SUMMARY_HEADER="--- Transform Summary ---"
    MSG_TRANSFORM_FILE_WOULD_CHANGE="Would change: %s"
    MSG_TRANSFORM_FILE_CHANGED="Updated: %s (backup: %s)"
    MSG_TRANSFORM_FILE_FAILED="Error: failed to update %s."
    MSG_TRANSFORM_APPLY_CONFIRM="Apply updates to %d file(s)? (yes/no): "
    MSG_TRANSFORM_APPLY_CANCELLED="Changes have been cancelled."
    MSG_TRANSFORM_NO_CHANGES="No files require changes."
    MSG_TRANSFORM_DRY_RUN_COMPLETED="Dry-run completed."
    MSG_TRANSFORM_APPLY_COMPLETED="Transform operation completed."
    MSG_TRANSFORM_APPLY_SKIPPED="Apply phase skipped."
    MSG_TRANSFORM_FAILED_HEADER="--- Failed Files ---"
    MSG_TRANSFORM_DIFF_HEADER="--- Diff: %s ---"
    MSG_ERROR_TRANSFORM_EXECUTION="Transform execution failed: %s"
    MSG_TRANSFORM_TOTAL_CHANGED="Pending updates: %d"
    MSG_TRANSFORM_TOTAL_FAILED="Failures: %d"
    MSG_TRANSFORM_NEWPROD_BASE_MISSING="Expected base directory %s was not found."
    MSG_TRANSFORM_NEWPROD_DIR_NOT_FOUND="No newproduction configuration directories were found under /var/www/com/ipet-ins/."
    MSG_TRANSFORM_NEWPROD_FILES_MISSING="Configuration directory %s is missing files: %s"
    MSG_TRANSFORM_LOG_SAVED="Rollback log saved to %s."
    MSG_ROLLBACK_MODE_HEADER="--- Rollback Mode ---"
    MSG_ROLLBACK_LOG_NOT_FOUND="Rollback log not found: %s"
    MSG_ROLLBACK_INVALID_LINE="Invalid rollback log entry: %s"
    MSG_ROLLBACK_BACKUP_MISSING="Backup file is missing: %s"
    MSG_ROLLBACK_FILE_RESTORED="Restored: %s"
    MSG_ROLLBACK_FILE_FAILED="Failed to restore: %s"
    MSG_ROLLBACK_NO_ENTRIES="No entries to restore in the rollback log."
    MSG_ROLLBACK_SUMMARY="Restored: %d  /  Failed: %d"
    MSG_SEARCH_COMPLETED_PRIMARY="Search completed."
    MSG_CHECK_NEW_INFRA_LOG="For new infrastructure definitions, please check: %s\n"
    MSG_CHECK_CURRENT_INFRA_LOG="For current infrastructure definitions, please check: %s\n"
    MSG_CURRENT_INFRA_HITS_HEADER="--- Files with Current Infrastructure Definitions ---"
    MSG_NEW_INFRA_HITS_HEADER_STG="--- Files with New Infrastructure Definitions (Staging Hostnames) ---"
    MSG_NEW_INFRA_HITS_HEADER_PROD="--- Files with New Infrastructure Definitions (Production Hostnames) ---"
    MSG_NEW_INFRA_HITS_HEADER_BY_COND="--- Files with New Infrastructure Definitions (%s) ---"
    MSG_BOTH_LOGS_HITS_HEADER="--- Warning: The following files contain definitions for both current and new infrastructures ---"
    MSG_NO_HITS="None"
    MSG_FILE_LABEL="File: "
    MSG_CONDITION_LABEL_CURRENT="Condition(Current Infra)"
    MSG_CONDITION_LABEL_NEW="Condition(New Infra)"
    MSG_IP_CONDITION_CURRENT_DESC="IP Address (Current)"
    MSG_IP_AWS_CONDITION_CURRENT_DESC="IP Address (AWS)"
    MSG_ENV_CONDITION_CURRENT_DESC="Environment string (Current)"
    MSG_HOSTNAME_CONDITION_CURRENT_DESC="Hostname pattern (Current)"
    MSG_IPTP_CONDITION_CURRENT_DESC="String \"iptp\""
    MSG_PFS_CONDITION_CURRENT_DESC="String \"pfs01\" or \"pfs02\""
    MSG_CONV_CONDITION_CURRENT_DESC="String \"dconv\""
    MSG_CONV_CONDITION_NEW_DESC="String \"conv0\""
    MSG_IP_CONDITION_NEW_DESC="IP Address (New)"
    MSG_HOSTNAME_STG_CONDITION_NEW_DESC="New Hostname (Staging)"
    MSG_HOSTNAME_PROD_CONDITION_NEW_DESC="New Hostname (Production)"
    MSG_ENV_CONDITION_NEW_DESC="Environment string (New)"
    MSG_IPETFS_CONDITION_NEW_DESC="String \"ipet-fs\""
    MSG_COMMENT_IGNORED_LABEL="(comments ignored (#))"
    MSG_MATCHES_LABEL="Matches"
    MSG_CONTEXT_LINES_LABEL="including 5 lines of context"
    MSG_HIT_LINE_PREFIX="HIT Line"
    MSG_MYHOSTNAME="Server Name:"
    SEPARATOR_LINE_LONG="================================================================================"
    SEPARATOR_LINE_SHORT="--------------------------------------------------------------------------------"
fi

# --- オプションと引数処理 ---
DELETE_LOGS_MODE=0
SEARCH_PATH=""
VERBOSE_MODE=0
SKIP_BACKUP_FILES_MODE=0
MIN_BASH_MAJOR_VERSION=4
SUBCOMMAND="scan"
TRANSFORM_DRY_RUN=1
declare -A TRANSFORM_TEMP_PATHS=()
declare -A TRANSFORM_DIFF_PATHS=()
declare -A TRANSFORM_FAILURE_MESSAGES=()
declare -a TRANSFORM_CHANGED_FILES=()
declare -a TRANSFORM_FAILED_FILES=()

# Bashのバージョンが4未満の場合、エラーを出力して終了
if [[ -z "${BASH_VERSINFO[0]}" ]] || [[ "${BASH_VERSINFO[0]}" -lt "$MIN_BASH_MAJOR_VERSION" ]]; then
    current_major_version="${BASH_VERSINFO[0]:-unknown}"
    current_minor_version="${BASH_VERSINFO[1]:-unknown}"
    printf "エラー: このスクリプトの実行にはBashバージョン %s.0 以上が必要です。現在のバージョンは %s.%s です。\n" "$MIN_BASH_MAJOR_VERSION" "$current_major_version" "$current_minor_version" >&2
    exit 1
fi

if [[ $# -gt 0 ]]; then
    case "$1" in
        scan)
            SUBCOMMAND="scan"
            shift
            ;;
        transform)
            SUBCOMMAND="transform"
            shift
            ;;
        rollback)
            SUBCOMMAND="rollback"
            shift
            ;;
    esac
fi

# 引数を解析し、オプションと検索パスを分離
declare -a remaining_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) VERBOSE_MODE=1; shift ;;
        --skip-backup-files) SKIP_BACKUP_FILES_MODE=1; shift ;;
        --deletelogs)
            if [ "$SUBCOMMAND" = "transform" ]; then
                printf "${MSG_ERROR_PREFIX}${MSG_DELETELLOGS_NOT_ALLOWED}\n" >&2
                printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1
            fi
            if [ "$#" -gt 1 ] || [ ${#remaining_args[@]} -ne 0 ]; then
                printf "${MSG_ERROR_PREFIX}${MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION}\n" >&2
                printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1; fi
            DELETE_LOGS_MODE=1; shift ;;
        --apply)
            if [ "$SUBCOMMAND" != "transform" ]; then
                printf "${MSG_ERROR_PREFIX}${MSG_ERROR_APPLY_ONLY_TRANSFORM}\n" >&2
                printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1
            fi
            TRANSFORM_DRY_RUN=0
            shift ;;
        --dry-run)
            if [ "$SUBCOMMAND" != "transform" ]; then
                printf "${MSG_ERROR_PREFIX}${MSG_ERROR_DRY_RUN_ONLY_TRANSFORM}\n" >&2
                printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1
            fi
            TRANSFORM_DRY_RUN=1
            shift ;;
        -*) printf "${MSG_ERROR_PREFIX}${MSG_ERROR_INVALID_OPTION}: %s\n" "$1" >&2
            printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1 ;;
        *) remaining_args+=("$1"); shift ;;
    esac
done

# 動作モードに応じて引数のバリデーションを行う
if [ "$SUBCOMMAND" = "rollback" ]; then
    if [ "$DELETE_LOGS_MODE" -eq 1 ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_DELETELLOGS_NOT_ALLOWED}\n" >&2
        printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1
    fi
    if [ ${#remaining_args[@]} -ne 1 ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION}\n" >&2
        printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1
    fi
    SEARCH_PATH="${remaining_args[0]}"
    if [ ! -f "$SEARCH_PATH" ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_ROLLBACK_LOG_NOT_FOUND}\n" "$SEARCH_PATH" >&2
        exit 1
    fi
elif [ "$DELETE_LOGS_MODE" -eq 1 ]; then
    # 削除モードの場合、他の引数は許可しない
    if [ ${#remaining_args[@]} -ne 0 ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION}\n" >&2
        printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1; fi
else
    # 検索/変換モードの場合、検索パスが1つだけ指定されていることを確認
    if [ ${#remaining_args[@]} -eq 0 ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_SEARCH_PATH_NOT_SPECIFIED}\n" >&2
        printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1;
    elif [ ${#remaining_args[@]} -gt 1 ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_TOO_MANY_ARGS_OR_INVALID_COMBINATION}\n" >&2
        printf "${MSG_USAGE_LINE1_EXTENDED}\n" >&2; exit 1;
    else
        SEARCH_PATH="${remaining_args[0]}"
        # 検索パスがディレクトリとして存在するか確認
        if [ ! -d "$SEARCH_PATH" ]; then
            printf "${MSG_ERROR_PREFIX}${MSG_SEARCH_PATH_NOT_EXIST_OR_DIR}" "$SEARCH_PATH" >&2; exit 1; fi
    fi
fi


# --- 書き込み権限チェック関数 ---
# スクリプトが出力ディレクトリに書き込み可能かを確認する
check_write_permission() {
    # mktempで一時ファイルを作成できれば、書き込み権限があると判断
    PERMISSION_CHECK_TEST_FILE_TEMP=$(mktemp "${OUTPUT_DIR}/.perm_check_XXXXXX") || {
        printf "%s\n" "${MSG_ERROR_PREFIX}${MSG_ERROR_WRITE_PERMISSION_DENIED}" >&2
        exit 1
    }
    # 確認用のファイルはすぐに削除
    rm -f "$PERMISSION_CHECK_TEST_FILE_TEMP" 2>/dev/null
    PERMISSION_CHECK_TEST_FILE_TEMP=""
}

# --- ログ削除モードの処理 ---
if [ "$DELETE_LOGS_MODE" -eq 1 ]; then
    printf "%s\n" "$MSG_LOG_DELETE_MODE"
    HOSTNAME_VAR=$(hostname)
    TIMESTAMP_PATTERN_GLOB="[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]"
    # 削除対象のログファイル名のパターンを定義
    LOG_PATTERN_NEW_GLOB="${HOSTNAME_VAR}_${TIMESTAMP_PATTERN_GLOB}_new_infra.log"
    LOG_PATTERN_CURRENT_GLOB="${HOSTNAME_VAR}_${TIMESTAMP_PATTERN_GLOB}_current_infra.log"
    declare -a files_to_delete=()
    # findコマンドでパターンに一致するログファイルを検索
    find "${OUTPUT_DIR}" -maxdepth 1 -type f \
         \( -name "$LOG_PATTERN_NEW_GLOB" -o -name "$LOG_PATTERN_CURRENT_GLOB" \) \
         -print0 | while IFS= read -r -d '' file; do
        files_to_delete+=("$file")
    done

    if [ ${#files_to_delete[@]} -eq 0 ]; then
        printf "%s\n" "$MSG_LOG_DELETE_NOT_FOUND"
        exit 0
    fi
    printf "%s\n" "$MSG_LOG_DELETE_FOUND"
    for file_to_remove in "${files_to_delete[@]}"; do
        printf "  %s\n" "$file_to_remove"
    done

    # ユーザーに削除の確認を求める
    read -r -p "$MSG_LOG_DELETE_CONFIRM" confirmation
    if [[ "$(echo "$confirmation" | tr '[:upper:]' '[:lower:]')" =~ ^(yes|y)$ ]]; then
        for file_to_remove_confirmed in "${files_to_delete[@]}"; do
            if rm "$file_to_remove_confirmed"; then
                printf "%s%s\n" "$MSG_LOG_DELETED" "$file_to_remove_confirmed"
            else
                printf "${MSG_ERROR_PREFIX}${MSG_LOG_DELETE_FAILED}" "$file_to_remove_confirmed" >&2
            fi
        done
        printf "%s\n" "$MSG_LOG_DELETE_COMPLETED"
    else
        printf "%s\n" "$MSG_LOG_DELETE_CANCELLED"
    fi
    exit 0
fi

if [ "$SUBCOMMAND" = "scan" ] || [ "$DELETE_LOGS_MODE" -ne 0 ]; then

# --- 通常実行時の初期化 ---
check_write_permission
HOSTNAME_VAR=$(hostname)
CURRENT_TIMESTAMP_FOR_FILENAME=$(date +%Y%m%d_%H%M%S)
CURRENT_DATETIME_FOR_LOG=$(date "+%Y-%m-%d %H:%M:%S")
NEW_INFRA_OUTPUT_FILE="${OUTPUT_DIR}/${HOSTNAME_VAR}_${CURRENT_TIMESTAMP_FOR_FILENAME}_new_infra.log"
CURRENT_INFRA_OUTPUT_FILE="${OUTPUT_DIR}/${HOSTNAME_VAR}_${CURRENT_TIMESTAMP_FOR_FILENAME}_current_infra.log"
# ログファイルを空の状態で作成
: > "$NEW_INFRA_OUTPUT_FILE"
: > "$CURRENT_INFRA_OUTPUT_FILE"

# --- ヘッダー生成と出力 ---
# スクリプトの基本情報をヘッダー文字列として一度だけ生成する
script_name=$(basename "$0")
# printfの-vオプション(bash 4+)で、フォーマットされた文字列を変数に格納する
printf -v header_info "\n%s\n%s %s\n%s %s\n%s %s\n%s %s\n----------------------------------------\n" \
    "${MSG_SCRIPT_INFO_HEADER}" \
    "${MSG_MYHOSTNAME}" "${HOSTNAME_VAR}" \
    "${MSG_SCRIPT_NAME_LABEL}" "$script_name" \
    "${MSG_SCRIPT_VERSION_LABEL}" "$SCRIPT_VERSION" \
    "${MSG_EXECUTION_DATETIME_LABEL}" "$CURRENT_DATETIME_FOR_LOG"

# 標準出力にヘッダーを表示
printf "%s" "$header_info"

# 各ログファイルにヘッダーを書き込む (リダイレクトは上書きの'>'で良い)
printf "%s" "$header_info" > "$NEW_INFRA_OUTPUT_FILE"
printf "%s" "$header_info" > "$CURRENT_INFRA_OUTPUT_FILE"


# --- 一時ファイル作成 ---
CURRENT_INFRA_HITS_TEMP=$(mktemp) || { printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "CURRENT_INFRA_HITS_TEMP" >&2; exit 1; }
NEW_INFRA_HITS_TEMP=$(mktemp) || { cleanup; printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "NEW_INFRA_HITS_TEMP" >&2; exit 1; }
NEW_INFRA_STG_HITS_TEMP=$(mktemp) || { cleanup; printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "NEW_INFRA_STG_HITS_TEMP" >&2; exit 1; }
NEW_INFRA_PROD_HITS_TEMP=$(mktemp) || { cleanup; printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "NEW_INFRA_PROD_HITS_TEMP" >&2; exit 1; }
NEW_INFRA_OTHER_HITS_TEMP=$(mktemp) || { cleanup; printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "NEW_INFRA_OTHER_HITS_TEMP" >&2; exit 1; }
BOTH_HITS_TEMP=$(mktemp)      || { cleanup; printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "BOTH_HITS_TEMP" >&2; exit 1; }

# スクリプト終了時(EXIT)、中断(INT)、強制終了(TERM)のシグナルを捕捉し、cleanup関数を実行
trap cleanup EXIT INT TERM

# --- 実行開始メッセージ ---
printf "%s\n" "$MSG_SEARCH_START"
printf "${MSG_SEARCH_PATH_LABEL}%s\n" "$SEARCH_PATH"
printf "${MSG_LOG_GENERATED_LOCATION}\n"

fi


# --- ヘルパー関数の事前定義 ---
# --- is_backup_file_name: ファイル名がバックアップファイルか判定 ---
# 引数: ファイル名
# 戻り値: バックアップファイルなら0(成功)、そうでなければ1(失敗)
is_backup_file_name() {
    local filename="$1"
    # 正規表現で、.bak, .old, ~, .swp, タイムスタンプなどのパターンに一致するか確認
    if [[ "$filename" =~ \.bak$ || "$filename" =~ \.old$ || "$filename" =~ ~$ || "$filename" =~ \.swp$ || "$filename" =~ \.swo$ || "$filename" =~ [._][0-9]{8}_[0-9]{6}(\..*)?$ || "$filename" =~ [._][0-9]{8}(\..*)?$ || "$filename" =~ \#[0-9]+\#$ || "$filename" =~ ^\.# || "$filename" =~ \.[0-9]{8}_[0-9]{6}$ || "$filename" =~ \.[0-9]{8}$ ]]; then
        return 0 # true (バックアップファイル)
    else
        return 1 # false
    fi
}
# --- filter_grep_output: grepの出力を整形・フィルタリング ---
# コメント行(#)を除外し、ヒット行にプレフィックスを付けるなどの処理を行う
# 引数1: grepの生出力, 引数2: 特殊フィルタタイプ, 引数3: ヒット行のプレフィックス
filter_grep_output() {
    local input_text="$1"
    local filter_type="$2"
    local lang_hit_prefix="$3"
    local production_flag=0
    if [ "$filter_type" = "production_special" ]; then
        production_flag=1
    fi
    if [ -z "$input_text" ]; then
        return
    fi
    # awkを使ってパイプで受け取ったgrep出力を処理
    printf '%s\n' "$input_text" | awk -v prefix="$lang_hit_prefix" -v prod_flag="$production_flag" '
    # BEGINブロック: awkの処理開始前に一度だけ実行される。変数の初期化を行う。
    BEGIN { block_lines_count = 0; current_block_has_valid_hit = 0; any_valid_block_printed = 0; }
    # 小文字変換用の関数
    function to_lowercase(s) { return tolower(s); }
    # grep -C の区切り文字"--"が来たときに、保持していたブロックを出力する関数
    function print_current_block() {
        # 有効なヒット行がブロック内に存在する場合のみ出力
        if (current_block_has_valid_hit == 1 && block_lines_count > 0) {
            # 2つ目以降のブロックの場合、見やすくするために区切り線を出力
            if (any_valid_block_printed == 1) {
                if (block_lines_count > 0 && last_block_was_empty == 0) { print ""; print "--"; print ""; }
                else if (block_lines_count > 0) { print "--"; }
            }
            # ブロック内の各行を処理
            for (i = 1; i <= block_lines_count; i++) {
                line = block_lines[i]
                # grep -n の出力形式 (行番号:内容 or 行番号-内容) に一致するかチェック
                if (match(line, /^([0-9]+)([:-])(.*)$/, arr)) {
                    line_num = arr[1]; sep = arr[2]; content = arr[3];
                    # ヒット行(:)であり、かつコメント行(#)でない場合
                    if (sep == ":" && content !~ /^[[:space:]]*#/) {
                        is_formatted_as_hit = 0;
                        # "production" の特別フィルタリング
                        if (prod_flag == 1) {
                            lcontent = to_lowercase(content);
                            if (lcontent ~ /production/ && lcontent !~ /production / && lcontent !~ /productions/) { is_formatted_as_hit = 1; }
                        } else { is_formatted_as_hit = 1; }
                        # ヒット行として整形して出力
                        if (is_formatted_as_hit == 1) { printf("[%s %s]:%s\n", prefix, line_num, content); }
                        else { print line; }
                    } else { print line; } # コンテキスト行(-)やコメント行はそのまま出力
                } else { print line; } # 行番号がない行もそのまま出力
            }
            any_valid_block_printed = 1; last_block_was_empty = 0;
        } else { last_block_was_empty = 1; }
        # 次のブロックのために変数をリセット
        block_lines_count = 0; current_block_has_valid_hit = 0;
    }
    # メインの処理ブロック: 1行ずつ読み込んで処理
    {
        if ($0 == "--") { print_current_block(); } # 区切り行が来たらブロックを出力
        else {
            block_lines[++block_lines_count] = $0; # 行をブロックに溜める
            # ヒット行(:)かつ非コメント行かを確認し、フラグを立てる
            if (match($0, /^([0-9]+):(.*)$/, arr_check)) {
                content_check = arr_check[2];
                if (content_check !~ /^[[:space:]]*#/) {
                    if (prod_flag == 1) {
                        lcontent_check = to_lowercase(content_check);
                        if (lcontent_check ~ /production/ && lcontent_check !~ /production / && lcontent_check !~ /productions/) { current_block_has_valid_hit = 1; }
                    } else { current_block_has_valid_hit = 1; }
                }
            }
        }
    }
    # ENDブロック: 全ての行を読み終わった後に実行される。最後のブロックを出力する。
    END { print_current_block(); }'
}
# --- IPアドレス検証用ヘルパー関数群 ---
# is_valid_octet: 1つのオクテット(0-255)が有効か検証
is_valid_octet() {
    local octet=$1
    # 数字のみで構成され、0-255の範囲内か
    if ! [[ "$octet" =~ ^[0-9]+$ ]] || (( octet < 0 || octet > 255 )); then return 1; fi
    # "01"のような先行ゼロを許可しない (ただし"0"は許可)
    if [[ "$octet" =~ ^0[0-9]+$ ]]; then return 1; fi
    return 0
}
# is_valid_ip: IPアドレス文字列全体が有効か検証
is_valid_ip() {
    local ip=$1; local ifs_bak=$IFS; IFS='.'; local -a octets=(); read -r -a octets <<< "$ip"; IFS=$ifs_bak
    # ドットで4つの部分に分かれているか
    if [[ ${#octets[@]} -ne 4 ]]; then return 1; fi
    # 各オクテットが有効かチェック
    for octet in "${octets[@]}"; do if ! is_valid_octet "$octet"; then return 1; fi; done
    return 0
}
# check_for_invalid_ips: ファイル内の不正フォーマットIPを検出しエラー表示
check_for_invalid_ips() {
    local filepath="$1"; local content="$2"
    # grep -o でIPアドレスのような文字列だけを抜き出す
    grep -n -o -E '[0-9]{1,3}(\.[0-9]{1,3}){3}' <<< "$content" | while IFS=: read -r line_num potential_ip; do
        # 抜き出した文字列が有効なIPアドレスか検証
        if ! is_valid_ip "$potential_ip"; then
            printf "${MSG_ERROR_PREFIX}${MSG_ERROR_INVALID_IP_FORMAT}" "$filepath" "$line_num" "$potential_ip" >&2
        fi
    done
}
# --- print_filtered_hit_list: サマリー用のファイルリストを出力 ---
print_filtered_hit_list() {
    local temp_hit_file="$1"; local items_actually_printed=0
    if [ ! -f "$temp_hit_file" ] || [ ! -s "$temp_hit_file" ]; then printf "%s\n" "$MSG_NO_HITS"; return; fi
    # mapfile(readarray)でファイル内容を配列に読み込み、sort -u で重複を除外
    declare -a sorted_hit_files=(); mapfile -t sorted_hit_files < <(sort -u "$temp_hit_file")
    if [ ${#sorted_hit_files[@]} -eq 0 ]; then printf "%s\n" "$MSG_NO_HITS"; return; fi
    for line_entry in "${sorted_hit_files[@]}"; do
        local file_path_to_check="$line_entry"
        if [ "$SKIP_BACKUP_FILES_MODE" -eq 1 ] && is_backup_file_name "$(basename "$file_path_to_check")"; then continue; fi
        printf "  %s\n" "$file_path_to_check"; items_actually_printed=1
    done
    if [ "$items_actually_printed" -eq 0 ]; then printf "%s\n" "$MSG_NO_HITS"; fi
}
# --- print_detailed_other_hits: 「その他条件」のサマリーを詳細に出力 ---
print_detailed_other_hits() {
    local temp_hit_file="$1"
    if [ ! -f "$temp_hit_file" ] || [ ! -s "$temp_hit_file" ]; then return; fi
    # 連想配列を使い、ヒットした条件ごとにファイルパスをまとめる
    declare -A hits_by_condition
    while IFS=: read -r condition_name filepath; do
        hits_by_condition["$condition_name"]+="${filepath}"$'\n'
    done < <(sort "$temp_hit_file")
    if [ ${#hits_by_condition[@]} -eq 0 ]; then return; fi
    # 条件ごとにループし、ファイルリストを出力
    for condition_name in "${!hits_by_condition[@]}"; do
        mapfile -t files < <(printf '%s' "${hits_by_condition[$condition_name]}" | sort -u)
        if [ ${#files[@]} -gt 0 ] && [ -n "${files[0]}" ]; then
            printf "\n"
            printf -- "${MSG_NEW_INFRA_HITS_HEADER_BY_COND}" "${NEW_INFRA_CONDITIONS_DESC[$condition_name]}"
            printf "\n"
            for file in "${files[@]}"; do
                printf "  %s\n" "$file"
            done
        fi
    done
}
# --- print_both_logs_hit_list: 両方のログにヒットしたファイルリストを出力 ---
print_both_logs_hit_list() {
    local temp_hit_file="$1"; local items_actually_printed=0
    if [ ! -f "$temp_hit_file" ] || [ ! -s "$temp_hit_file" ]; then printf "%s\n" "$MSG_NO_HITS"; return; fi
    while IFS= read -r file_path; do
        if [ "$SKIP_BACKUP_FILES_MODE" -eq 1 ] && is_backup_file_name "$(basename "$file_path")"; then continue; fi
        printf "  %s\n" "$file_path"; items_actually_printed=1
    done < "$temp_hit_file"
    if [ "$items_actually_printed" -eq 0 ]; then printf "%s\n" "$MSG_NO_HITS"; fi
}

check_newproduction_structure() {
    local base_dir="/var/www/com/ipet-ins"
    local system_dir=""
    local config_dir=""
    local found_dir=0
    local expected_files=(app.php config.php db.php email.php session.php)

    if [ ! -d "$base_dir" ]; then
        TRANSFORM_FAILED_FILES+=("$base_dir")
        TRANSFORM_FAILURE_MESSAGES["$base_dir"]=$(printf "%s" "$(printf "$MSG_TRANSFORM_NEWPROD_BASE_MISSING" "$base_dir")")
        return 1
    fi

    for system_dir in "$base_dir"/*; do
        [ -d "$system_dir" ] || continue
        config_dir="$system_dir/fuel/app/config/newproduction"
        if [ -d "$config_dir" ]; then
            found_dir=1
            local missing_files=()
            local expected=""
            for expected in "${expected_files[@]}"; do
                if [ ! -f "$config_dir/$expected" ]; then
                    missing_files+=("$expected")
                fi
            done
            if [ ${#missing_files[@]} -gt 0 ]; then
                local joined=""
                local saved_ifs=$IFS
                IFS=', '; joined="${missing_files[*]}"
                IFS=$saved_ifs
                TRANSFORM_FAILED_FILES+=("$config_dir")
                TRANSFORM_FAILURE_MESSAGES["$config_dir"]=$(printf "$MSG_TRANSFORM_NEWPROD_FILES_MISSING" "$config_dir" "$joined")
            fi
        fi
    done

    if [ "$found_dir" -eq 0 ]; then
        TRANSFORM_FAILED_FILES+=("$base_dir")
        TRANSFORM_FAILURE_MESSAGES["$base_dir"]="$MSG_TRANSFORM_NEWPROD_DIR_NOT_FOUND"
        return 1
    fi

    return 0
}

run_transform() {
    printf "%s\n" "$MSG_TRANSFORM_MODE_HEADER"
    if [ "$TRANSFORM_DRY_RUN" -eq 1 ]; then
        printf "%s\n" "$MSG_TRANSFORM_DRY_RUN_NOTICE"
    else
        printf "%s\n" "$MSG_TRANSFORM_APPLY_NOTICE"
    fi

    for key in "${!TRANSFORM_TEMP_PATHS[@]}"; do unset 'TRANSFORM_TEMP_PATHS[$key]'; done
    for key in "${!TRANSFORM_DIFF_PATHS[@]}"; do unset 'TRANSFORM_DIFF_PATHS[$key]'; done
    for key in "${!TRANSFORM_FAILURE_MESSAGES[@]}"; do unset 'TRANSFORM_FAILURE_MESSAGES[$key]'; done
    TRANSFORM_CHANGED_FILES=()
    TRANSFORM_FAILED_FILES=()

    local total_files_scanned_transform=0
    local transform_output=""
    local status=0
    local temp_transformed=""
    local diff_file=""
    local change_log=""
    local host_for_log="$(hostname 2>/dev/null || echo unknown)"

    if [ "$SEARCH_PATH" = "/var" ] || [ "$SEARCH_PATH" = "/var/" ]; then
        check_newproduction_structure
    fi

    while IFS= read -r -d $'\0' filepath; do
        total_files_scanned_transform=$((total_files_scanned_transform + 1))
        if [ "$SKIP_BACKUP_FILES_MODE" -eq 1 ] && is_backup_file_name "$(basename "$filepath")"; then
            if [ "$VERBOSE_MODE" -eq 1 ]; then
                printf "%s%s\n" "${MSG_VERBOSE_PREFIX}" "${MSG_VERBOSE_SKIPPING_BACKUP_FILE}$filepath"
            fi
            continue
        fi
        if [ "$VERBOSE_MODE" -eq 1 ]; then
            printf "%s%s\n" "${MSG_VERBOSE_PREFIX}" "${MSG_VERBOSE_SCANNING_FILE}$filepath"
        fi

        temp_transformed=$(mktemp) || {
            printf "${MSG_ERROR_PREFIX}${MSG_TEMP_FILE_CREATION_FAILED}" "transform" >&2
            return 1
        }

        transform_output=$(awk -v target_path="$filepath" -f - "$filepath" > "$temp_transformed" 2>&1 <<'AWK'
BEGIN {
    ip_regex = "[0-9]{1,3}(\\.[0-9]{1,3}){3}(/[0-9]{1,2})?"
    changed = 0
}

function convert_ip(token, arr, base, suffix, i) {
    suffix = ""
    if (token ~ /\//) {
        split(token, arr, "/")
        base = arr[1]
        suffix = "/" arr[2]
    } else {
        base = token
    }
    split(base, arr, ".")
    if (length(arr) != 4) {
        return token
    }
    for (i = 1; i <= 4; i++) {
        if (arr[i] !~ /^[0-9]+$/) {
            return token
        }
        arr[i] += 0
        if (arr[i] < 0 || arr[i] > 255) {
            return token
        }
    }
    if (arr[3] >= 170 && arr[3] <= 179) {
        arr[3] -= 10
        changed = 1
        return arr[1] "." arr[2] "." arr[3] "." arr[4] suffix
    }
    return token
}
function rebuild_with_ip(str, rem, result, prefix, token, converted) {
    rem = str
    result = ""
    while (match(rem, ip_regex)) {
        prefix = substr(rem, 1, RSTART - 1)
        token = substr(rem, RSTART, RLENGTH)
        converted = convert_ip(token)
        result = result prefix converted
        rem = substr(rem, RSTART + RLENGTH)
        if (converted != token) {
            changed = 1
        }
    }
    return result rem
}
function replace_zero_ns(str, rem, result, prefix, next_char) {
    rem = str
    result = ""
    while (match(rem, /0[0-9]+s/)) {
        prefix = substr(rem, 1, RSTART - 1)
        next_char = substr(rem, RSTART + RLENGTH, 1)
        if (next_char != "" && next_char ~ /[[:alnum:]_]/) {
            result = result substr(rem, 1, RSTART + RLENGTH - 1)
            rem = substr(rem, RSTART + RLENGTH)
            continue
        }
        result = result prefix "0np"
        rem = substr(rem, RSTART + RLENGTH)
        changed = 1
    }
    return result rem
}
function replace_trailing_s(str, rem, result, prefix, digit, next_char) {
    rem = str
    result = ""
    while (match(rem, /[0-9]s/)) {
        prefix = substr(rem, 1, RSTART - 1)
        digit = substr(rem, RSTART, 1)
        next_char = substr(rem, RSTART + RLENGTH, 1)
        if (next_char != "" && next_char ~ /[[:alnum:]_]/) {
            result = result substr(rem, 1, RSTART + RLENGTH - 1)
            rem = substr(rem, RSTART + RLENGTH)
            continue
        }
        result = result prefix digit "p"
        rem = substr(rem, RSTART + RLENGTH)
        changed = 1
    }
    return result rem
}
function replace_stg_tokens(str, prefix, suffix, before_char, after_char, result) {
    result = ""
    while (match(str, /stg/)) {
        prefix = substr(str, 1, RSTART - 1)
        suffix = substr(str, RSTART + RLENGTH)
        before_char = (RSTART > 1) ? substr(str, RSTART - 1, 1) : ""
        after_char = (length(str) >= RSTART + RLENGTH) ? substr(str, RSTART + RLENGTH, 1) : ""
        if ((before_char == "" || before_char !~ /[[:alnum:]]/) && (after_char == "" || after_char !~ /[[:alnum:]]/)) {
            result = result prefix "prd"
            str = suffix
            changed = 1
        } else {
            result = result prefix substr(str, RSTART, RLENGTH)
            str = suffix
        }
    }
    return result str
}
{
    is_httpd_conf = (target_path ~ /\.conf($|\.)/ || target_path ~ /\/etc\/httpd\//)
    line = rebuild_with_ip($0)
    updated = replace_zero_ns(line)
    if (updated != line) {
        line = updated
    }
    line = updated
    updated = replace_trailing_s(line)
    if (updated != line) {
        line = updated
    }
    if (line ~ /^[[:space:]]*([0-9]{1,3}\.){3}[0-9]{1,3}/ || line ~ /Hostname[[:space:]]*=/ || line ~ /PRETTY_HOSTNAME[[:space:]]*=/) {
        line = replace_stg_tokens(line)
    }
    if (is_httpd_conf && gsub(/newstaging/, "newproduction", line) > 0) {
        changed = 1
    }
    print line
}
END {
    if (changed) {
        exit 0
    }
    exit 3
}
AWK
        )
        status=$?
        if [ $status -eq 0 ]; then
            TRANSFORM_TEMP_PATHS["$filepath"]="$temp_transformed"
            TRANSFORM_CHANGED_FILES+=("$filepath")
            if [ "$VERBOSE_MODE" -eq 1 ] || [ "$TRANSFORM_DRY_RUN" -eq 1 ]; then
                diff_file=$(mktemp) || diff_file=""
                if [ -n "$diff_file" ]; then
                    diff -u "$filepath" "$temp_transformed" > "$diff_file" 2>/dev/null || true
                    TRANSFORM_DIFF_PATHS["$filepath"]="$diff_file"
                fi
            fi
        elif [ $status -eq 3 ]; then
            rm -f "$temp_transformed" 2>/dev/null
        else
            rm -f "$temp_transformed" 2>/dev/null
            TRANSFORM_FAILED_FILES+=("$filepath")
            if [ -z "$transform_output" ]; then
                transform_output="(no additional details)"
            fi
            TRANSFORM_FAILURE_MESSAGES["$filepath"]="$transform_output"
            printf "${MSG_ERROR_PREFIX}${MSG_ERROR_TRANSFORM_EXECUTION}\n" "$transform_output" >&2
        fi
    done < <(find "$SEARCH_PATH" -path '*/selinux/*' -prune -o -type f -not -name '*#*' -print0)

    printf "\n%s\n" "$MSG_TRANSFORM_SUMMARY_HEADER"
    printf "${MSG_SUMMARY_TOTAL_FILES_SCANNED}\n" "$total_files_scanned_transform"
    printf "${MSG_TRANSFORM_TOTAL_CHANGED}\n" "${#TRANSFORM_CHANGED_FILES[@]}"
    printf "${MSG_TRANSFORM_TOTAL_FAILED}\n" "${#TRANSFORM_FAILED_FILES[@]}"

    if [ "${#TRANSFORM_FAILED_FILES[@]}" -gt 0 ]; then
        printf "\n%s\n" "$MSG_TRANSFORM_FAILED_HEADER"
        for file in "${TRANSFORM_FAILED_FILES[@]}"; do
            printf "  %s\n" "$file"
            if [ -n "${TRANSFORM_FAILURE_MESSAGES[$file]}" ]; then
                printf "    %s\n" "${TRANSFORM_FAILURE_MESSAGES[$file]}"
            fi
        done
    fi

    if [ "${#TRANSFORM_CHANGED_FILES[@]}" -eq 0 ]; then
        printf "%s\n" "$MSG_TRANSFORM_NO_CHANGES"
        if [ "$TRANSFORM_DRY_RUN" -eq 1 ]; then
            printf "%s\n" "$MSG_TRANSFORM_DRY_RUN_COMPLETED"
        else
            printf "%s\n" "$MSG_TRANSFORM_APPLY_SKIPPED"
        fi
        return 0
    fi

    local -a sorted_changed_files=()
    mapfile -t sorted_changed_files < <(printf '%s\n' "${TRANSFORM_CHANGED_FILES[@]}" | sort -u)
    for file in "${sorted_changed_files[@]}"; do
        printf "$MSG_TRANSFORM_FILE_WOULD_CHANGE\n" "$file"
        if [ "$VERBOSE_MODE" -eq 1 ]; then
            diff_file="${TRANSFORM_DIFF_PATHS[$file]:-}"
            if [ -n "$diff_file" ] && [ -s "$diff_file" ]; then
                printf "$MSG_TRANSFORM_DIFF_HEADER\n" "$file"
                cat "$diff_file"
            fi
        fi
    done

    if [ "$TRANSFORM_DRY_RUN" -eq 1 ]; then
        printf "%s\n" "$MSG_TRANSFORM_DRY_RUN_COMPLETED"
        for file in "${sorted_changed_files[@]}"; do
            diff_file="${TRANSFORM_DIFF_PATHS[$file]:-}"
            temp_transformed="${TRANSFORM_TEMP_PATHS[$file]:-}"
            if [ -n "$diff_file" ]; then rm -f "$diff_file" 2>/dev/null; fi
            if [ -n "$temp_transformed" ]; then rm -f "$temp_transformed" 2>/dev/null; fi
            unset 'TRANSFORM_DIFF_PATHS[$file]'
            unset 'TRANSFORM_TEMP_PATHS[$file]'
        done
        TRANSFORM_CHANGED_FILES=()
        return 0
    fi

    printf "$MSG_TRANSFORM_APPLY_CONFIRM" "${#sorted_changed_files[@]}"
    local apply_confirmation
    read -r apply_confirmation
    local lower_response
    lower_response=$(echo "$apply_confirmation" | tr '[:upper:]' '[:lower:]')
    if [[ ! "$lower_response" =~ ^(yes|y)$ ]]; then
        printf "%s\n" "$MSG_TRANSFORM_APPLY_CANCELLED"
        printf "%s\n" "$MSG_TRANSFORM_APPLY_SKIPPED"
        return 0
    fi

    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local apply_failures=0
    if [ "$TRANSFORM_DRY_RUN" -eq 0 ]; then
        change_log="${OUTPUT_DIR}/${host_for_log}_${timestamp}_transform.log"
        printf "# original_path\tbackup_path\toriginal_mode\toriginal_owner\toriginal_group\n" > "$change_log"
    fi

    for file in "${sorted_changed_files[@]}"; do
        local temp_file="${TRANSFORM_TEMP_PATHS[$file]}"
        local diff_saved="${TRANSFORM_DIFF_PATHS[$file]:-}"
        local backup_path="${file}.bak_${timestamp}"
        local orig_mode orig_owner orig_group

        orig_mode=$(stat -c '%a' "$file" 2>/dev/null)
        orig_owner=$(stat -c '%u' "$file" 2>/dev/null)
        orig_group=$(stat -c '%g' "$file" 2>/dev/null)

        if ! cp -p "$file" "$backup_path" 2>/dev/null; then
            if ! cp "$file" "$backup_path" 2>/dev/null; then
                printf "${MSG_ERROR_PREFIX}${MSG_TRANSFORM_FILE_FAILED}\n" "$file" >&2
                apply_failures=$((apply_failures + 1))
                if [ -n "$temp_file" ]; then rm -f "$temp_file" 2>/dev/null; fi
                if [ -n "$diff_saved" ]; then rm -f "$diff_saved" 2>/dev/null; fi
                unset 'TRANSFORM_TEMP_PATHS[$file]'
                unset 'TRANSFORM_DIFF_PATHS[$file]'
                continue
            fi
        fi

        if ! cat "$temp_file" > "$file"; then
            printf "${MSG_ERROR_PREFIX}${MSG_TRANSFORM_FILE_FAILED}\n" "$file" >&2
            cp "$backup_path" "$file" 2>/dev/null
            apply_failures=$((apply_failures + 1))
            if [ -n "$temp_file" ]; then rm -f "$temp_file" 2>/dev/null; fi
            if [ -n "$diff_saved" ]; then rm -f "$diff_saved" 2>/dev/null; fi
            unset 'TRANSFORM_TEMP_PATHS[$file]'
            unset 'TRANSFORM_DIFF_PATHS[$file]'
            continue
        fi

        if [ -n "$orig_mode" ]; then chmod "$orig_mode" "$file" 2>/dev/null || true; fi
        if [ -n "$orig_owner" ] && [ -n "$orig_group" ]; then
            chown "$orig_owner:$orig_group" "$file" 2>/dev/null || true
        fi

        printf "$MSG_TRANSFORM_FILE_CHANGED\n" "$file" "$backup_path"
        if [ -n "$temp_file" ]; then rm -f "$temp_file" 2>/dev/null; fi
        if [ -n "$diff_saved" ]; then rm -f "$diff_saved" 2>/dev/null; fi
        unset 'TRANSFORM_TEMP_PATHS[$file]'
        unset 'TRANSFORM_DIFF_PATHS[$file]'
        if [ -n "$change_log" ]; then
            printf "%s\t%s\t%s\t%s\t%s\n" \
                "$file" "$backup_path" "${orig_mode:-}" "${orig_owner:-}" "${orig_group:-}" >> "$change_log"
        fi
    done

    TRANSFORM_CHANGED_FILES=()
    if [ $apply_failures -eq 0 ]; then
        printf "%s\n" "$MSG_TRANSFORM_APPLY_COMPLETED"
        if [ -n "$change_log" ] && [ -f "$change_log" ]; then
            printf "$MSG_TRANSFORM_LOG_SAVED\n" "$change_log"
        fi
        return 0
    fi

    printf "${MSG_TRANSFORM_TOTAL_FAILED}\n" "$apply_failures"
    if [ -n "$change_log" ] && [ -f "$change_log" ]; then
        printf "$MSG_TRANSFORM_LOG_SAVED\n" "$change_log"
    fi
    return 1
}

run_rollback() {
    local log_file="$SEARCH_PATH"
    local restored_count=0
    local failed_count=0

    printf "%s\n" "$MSG_ROLLBACK_MODE_HEADER"

    if [ ! -f "$log_file" ]; then
        printf "${MSG_ERROR_PREFIX}${MSG_ROLLBACK_LOG_NOT_FOUND}\n" "$log_file" >&2
        exit 1
    fi

    while IFS=$'\t' read -r original_path backup_path original_mode original_owner original_group; do
        if [[ "$original_path" =~ ^# ]] || { [ -z "$original_path" ] && [ -z "$backup_path" ]; }; then
            continue
        fi

        if [ -z "$original_path" ] || [ -z "$backup_path" ]; then
            printf "${MSG_ERROR_PREFIX}${MSG_ROLLBACK_INVALID_LINE}\n" "$original_path" >&2
            failed_count=$((failed_count + 1))
            continue
        fi

        if [ ! -f "$backup_path" ]; then
            printf "${MSG_ERROR_PREFIX}${MSG_ROLLBACK_BACKUP_MISSING}\n" "$backup_path" >&2
            failed_count=$((failed_count + 1))
            continue
        fi

        if ! cp -p "$backup_path" "$original_path" 2>/dev/null; then
            if ! cp "$backup_path" "$original_path" 2>/dev/null; then
                printf "${MSG_ERROR_PREFIX}${MSG_ROLLBACK_FILE_FAILED}\n" "$original_path" >&2
                failed_count=$((failed_count + 1))
                continue
            fi
        fi

        if [ -n "$original_mode" ]; then chmod "$original_mode" "$original_path" 2>/dev/null || true; fi
        if [ -n "$original_owner" ] && [ -n "$original_group" ]; then
            chown "$original_owner:$original_group" "$original_path" 2>/dev/null || true
        fi

        printf "$MSG_ROLLBACK_FILE_RESTORED\n" "$original_path"
        restored_count=$((restored_count + 1))
    done < "$log_file"

    if [ $restored_count -eq 0 ] && [ $failed_count -eq 0 ]; then
        printf "%s\n" "$MSG_ROLLBACK_NO_ENTRIES"
    fi

    printf "$MSG_ROLLBACK_SUMMARY\n" "$restored_count" "$failed_count"

    if [ $failed_count -ne 0 ]; then
        return 1
    fi
    return 0
}

if [ "$SUBCOMMAND" = "transform" ] && [ "$DELETE_LOGS_MODE" -eq 0 ]; then
    trap cleanup EXIT INT TERM
    run_transform
    exit $?
fi

if [ "$SUBCOMMAND" = "rollback" ]; then
    trap cleanup EXIT INT TERM
    run_rollback
    exit $?
fi

# === 検索条件の定義 =================================
# --- 現行基盤の定義 ---
declare -A CURRENT_INFRA_CONDITIONS
# 現行基盤のIPアドレス範囲
pattern_ip_100_100_main='100\.100\.([0-9]{1,3})\.([0-9]{1,3})'
pattern_ip_172_16_slash17_main="172\.16\.(12[0-7]|1[01][0-9]|[0-9]?[0-9])\.([0-9]{1,3})"
pattern_ip_172_other_third_octets_main='(1|10|13|15|16|22|25|51|100|101|103)'
pattern_ip_172_specific_main="172\.([0-9]{1,3})\.${pattern_ip_172_other_third_octets_main}\.([0-9]{1,3})"
pattern_ip_aws_1='172\.29\.([0-9]{1,3})\.([0-9]{1,3})'
pattern_ip_aws_2='169\.254\.[0-3]\.([0-9]{1,3})'
pattern_ip_toyosu='172\.17\.([0-9]{1,3})\.([0-9]{1,3})'
pattern_ip_hb_1='10\.0\.[01]\.([0-9]{1,3})'
pattern_ip_hb_2='200\.(12[0-7]|1[01][0-9]|[0-9]?[0-9])\.([0-9]{1,3})\.([0-9]{1,3})'
pattern_ip_subnet_ws2='172\.28\.25[2-5]\.([0-9]{1,3})'
pattern_ip_regional_1='192\.168\.44\.([0-9]{1,3})'
pattern_ip_regional_2='192\.168\.172\.([0-9]{1,3})'
pattern_ip_regional_3='192\.168\.33\.([0-9]{1,3})'
pattern_ip_infra='192\.168\.174\.([0-9]{1,3})'

# 現行基盤のホスト名パターンを配列で定義
hostname_patterns_main=( 'ipetdchq2nd001' 'ipetdc00[12]' 'iptad(?:con001|aud01|mg02)' 'ipetad011(?:_.*)?' 'ipetbkserv00[24]' 'iptpavm0[1245]' 'iptpdns0[12]' '(?:dapp|linp|evep|srmp|mulp|clbp|dlagp|ndpp|pswp|pnrp|mypp|vmap|psop|agcp|vmpp|pfts|crmp|ansp|dwap|dlip|dolp|anap)(?:ap|db|lb)[0-9]{2,}' 'c[a-z]{2,}[0-9]{3}p' '(?:ceset|piifs|niafs|astjb|bstar|confl|cdesk|cdocs|biz|dmp|xdp|ltt|dwa|elk|cesrs|euc|rst|ifo|infox|pbs|cpmve|cprxy|creps|cscgv|shptd|ptl|adssp|appsc|csmtp|clicw|crpmg|kzk|clpas|cndsm|eucpc|kddfs|kmsds|pep|stogw|uilai|uilam)[a-z0-9]*[0-9]{2,}[sp]' 'c(jkns|bgip|fmdw|fmdl|fubi|batc)[0-9]{2,}[sp]' 'cv(csa|rom)[a-z0-9]*[0-9]{2,}[sp]' 'czbbx[0-9]{2,}[sp]' 'iptp(?:eset|hul|if|ci|mi|rm|bp|fs|bk|om|ml|log)[a-z0-9]*' '(?:eik|ndp)pci[0-9]{2,}' '(?:gvrp|drip)lb[0-9]{2,}' 'frntsb[0-9]{2,}' 'dmppts[0-9]{2,}' 'iptdec00[239]' 'ipet-link[0-9]{2,}' 'suppap[0-9]{2,}' 'ipt(?:dsm|ome|vddk|ss|sdpls|pxypac|sdz|ness)[a-z0-9]*' 'welcome-db(?:02)?\.ipet-ins\.com' 'welcome\.ipet-ins\.com' 'TESTDEPLOY' 'mailman' 'ipet-contact' 'ipet-marketing-wiki' 'lb-act' 'iptwork[0-9]{2,}' 'cbtrustvc[0-9]{3,}' 'TEMP_Win10' 'dmp(?:pmb|pci|plb|pap|dts)[0-9]+' '(?:dmp|ltt|biz)(?:p?db|p?ap)[0-9]+' )
# 配列の要素を'|'で結合して、grepで使える単一の正規表現パターンを生成
new_hostname_pattern_main="\b($(IFS='|'; echo "${hostname_patterns_main[*]}"))\b"
# 連想配列に、条件名と正規表現パターンを格納
CURRENT_INFRA_CONDITIONS=(
    ["IP_current"]="\\b(${pattern_ip_100_100_main}|${pattern_ip_172_16_slash17_main}|${pattern_ip_172_specific_main}|${pattern_ip_toyosu}|${pattern_ip_hb_1}|${pattern_ip_hb_2}|${pattern_ip_subnet_ws2}|${pattern_ip_regional_1}|${pattern_ip_regional_2}|${pattern_ip_regional_3}|${pattern_ip_infra})\\b"
    ["IP_AWS_current"]="\\b(${pattern_ip_aws_1}|${pattern_ip_aws_2})\\b"
    ["ENV_current"]='\b(staging|production)[/.;]'
    ["HOSTNAME_current"]="$new_hostname_pattern_main"
    ["IPTP_current"]='\biptp\b'
    ["PFS_current"]='pfs0[1-2]'
    ["DCONV_current"]='dconv'
)
# 各条件の説明文を連想配列に格納
declare -A CURRENT_INFRA_CONDITIONS_DESC
CURRENT_INFRA_CONDITIONS_DESC=(
    ["IP_current"]="$MSG_IP_CONDITION_CURRENT_DESC"
    ["IP_AWS_current"]="$MSG_IP_AWS_CONDITION_CURRENT_DESC"
    ["ENV_current"]="$MSG_ENV_CONDITION_CURRENT_DESC"
    ["HOSTNAME_current"]="$MSG_HOSTNAME_CONDITION_CURRENT_DESC"
    ["IPTP_current"]="$MSG_IPTP_CONDITION_CURRENT_DESC"
    ["PFS_current"]="$MSG_PFS_CONDITION_CURRENT_DESC"
    ["DCONV_current"]="$MSG_CONV_CONDITION_CURRENT_DESC"
)

# --- 新基盤の定義 ---
declare -A NEW_INFRA_CONDITIONS
# 新基盤のIPアドレス範囲 (172.16.128.0/17)
pattern_ip_172_16_slash17_upper_fixed="172\.16\.(12[89]|1[3-9][0-9]|2[0-4][0-9]|25[0-5])\.([0-9]{1,3})"
# 新基盤のホスト名パターンを配列で定義
hostname_patterns=( '(?:line|event|mul|psweb|petname|recept|iposco|crm)-(?:lb|ap|db)[0-9]+[sp]' 'XXX-(?:ap|db|lb)[0-9]+[sp]' 'ipet-lb[0-9]+[sp]' 'ipetclub-(?:lb|ap|db)[0-9]+[sp]' 'dlagency-(?:lb|ap|db)[0-9]+[sp]' 'agency-(?:lb|ap|db)[0-9]+[sp]' 'epc(?:ds)?[0-9]+[sp]' 'docomo-(?:lb|ap|db)[0-9]+[sp]' 'ipet-(?:fs|bk)[0-9]+[sp]' 'sdp[0-9]+[sp]' 'myp-(?:ap|db)[0-9]+[sp]' 'smtp[0-9]+[sp]' 'mailman-ap[0-9]+[sp]' 'log[0-9]+[sp]' 'wel-(?:ap|db)[0-9]+[sp]' 'inext-(?:ap|db)[0-9]+[sp]' '(?:grafana|metabase)[0-9]+p' 'proxy[0-9]+[sp]' 'asteria-ap[0-9]+[sp]' 'infoone-(?:doc|ap)[0-9]+[sp]' 'biz-(?:ap|db)[0-9]+[sp]' 'dmp-(?:ap|ci|lb|mb)[0-9]+[sp]' 'xdp-(?:ap|conv|db)[0-9]+[sp]' 'ltt-(?:ap|db)[0-9]+[sp]' 'dwa-db[0-9]+[sp]' 'jenkins[0-9]+[sp]' '(?:bigip|repos|zabbix|scansv|pmp)[0-9]+[sp]' 'report-[0-9]+[sp]' 'jumpw[0-9]+[sp]' 'jumpl[0-9]+[sp]' 'fubi-db[0-9]+[sp]' 'bat[0-9]+[sp]' 'dwhapi-(?:ap|db)[0-9]+[sp]' '(?:eset|hulft|iFilter)[0-9]+[sp]' 'jobarg[0-9]+' '[a-z0-9]+-(?:lb|ap|db|fs|bk|conv|ci|mb)[0-9]+[sp]' )
new_hostname_pattern_fixed="\b($(IFS='|'; echo "${hostname_patterns[*]}"))\b"
NEW_INFRA_CONDITIONS=(
    ["IP_new"]="\\b${pattern_ip_172_16_slash17_upper_fixed}\\b"
    ["ENV_new"]='\b(newstaging|newproduction)[/.;]'
    ["HOSTNAME_new"]="$new_hostname_pattern_fixed"
    ["IPETFS_new"]='.*ipet-fs.*'
    ["CONV0_new"]='conv0'
)
declare -A NEW_INFRA_CONDITIONS_DESC
NEW_INFRA_CONDITIONS_DESC=(
    ["IP_new"]="$MSG_IP_CONDITION_NEW_DESC"
    ["ENV_new"]="$MSG_ENV_CONDITION_NEW_DESC"
    ["HOSTNAME_stg_new"]="$MSG_HOSTNAME_STG_CONDITION_NEW_DESC"
    ["HOSTNAME_prod_new"]="$MSG_HOSTNAME_PROD_CONDITION_NEW_DESC"
    ["IPETFS_new"]="$MSG_IPETFS_CONDITION_NEW_DESC"
    ["CONV0_new"]="$MSG_CONV_CONDITION_NEW_DESC"
)
# =====================================================================


# --- メイン処理 ---
TOTAL_FILES_SCANNED=0
declare -A current_infra_hits_written
declare -A new_infra_hits_written
declare -A new_infra_stg_hits_written
declare -A new_infra_prod_hits_written
declare -A new_infra_other_hits_written

SECONDS=0
# findコマンドでスキャン対象ファイルをリストアップし、whileループで1ファイルずつ処理
# findの-print0とreadの-d $'\0' を使うことで、ファイル名にスペースや特殊文字が含まれていても安全に扱える
while IFS= read -r -d $'\0' filepath; do
    TOTAL_FILES_SCANNED=$((TOTAL_FILES_SCANNED + 1))
    abs_filepath=$(readlink -f "$filepath" 2>/dev/null || echo "$filepath")

    # パフォーマンス向上のため、ファイル内容を一度だけメモリ上の配列に読み込む
    IFS= readarray -t file_lines < "$filepath" || continue
    # 配列を改行区切りの単一の文字列変数に変換
    file_content=$(printf '%s\n' "${file_lines[@]}")

    # ファイル内の不正なIPアドレスフォーマットをチェック
    check_for_invalid_ips "$filepath" "$file_content"

    # バックアップファイルスキップオプションが有効な場合、バックアップファイルをスキップ
    if [ "$SKIP_BACKUP_FILES_MODE" -eq 1 ] && is_backup_file_name "$(basename "$filepath")"; then
        if [ "$VERBOSE_MODE" -eq 1 ]; then printf "%s%s\n" "${MSG_VERBOSE_PREFIX}" "${MSG_VERBOSE_SKIPPING_BACKUP_FILE}$filepath"; fi
        continue
    fi

    if [ "$VERBOSE_MODE" -eq 1 ]; then printf "%s%s\n" "${MSG_VERBOSE_PREFIX}" "${MSG_VERBOSE_SCANNING_FILE}$filepath"; fi

    current_infra_log_header_printed=0
    current_infra_match_found=0
    new_infra_log_header_printed=0
    new_infra_match_found=0

    # === 現行基盤定義の検索ループ ===
    for condition_name in "${!CURRENT_INFRA_CONDITIONS[@]}"; do
        pattern="${CURRENT_INFRA_CONDITIONS[$condition_name]}"
        description="${CURRENT_INFRA_CONDITIONS_DESC[$condition_name]}"
        grep_options="-I -E -i -n -C 5 --color=never"
        filter_type=""
        # メモリ上のファイル内容に対してgrepを実行
        raw_matches=$(grep $grep_options -- "$pattern" <<<"$file_content" 2>/dev/null)
        filtered_matches=$(filter_grep_output "$raw_matches" "$filter_type" "$MSG_HIT_LINE_PREFIX")
        if [ -n "$filtered_matches" ]; then
            if [ "$current_infra_log_header_printed" -eq 0 ]; then
                printf "%s\n" "$SEPARATOR_LINE_LONG" >> "$CURRENT_INFRA_OUTPUT_FILE"
                printf "${MSG_FILE_LABEL}\"%s\"\n" "$filepath" >> "$CURRENT_INFRA_OUTPUT_FILE"
                current_infra_log_header_printed=1
            fi
            printf "%s\n" "$SEPARATOR_LINE_SHORT" >> "$CURRENT_INFRA_OUTPUT_FILE"
            printf "${MSG_CONDITION_LABEL_CURRENT}: %s %s\n" "$description" "$MSG_COMMENT_IGNORED_LABEL" >> "$CURRENT_INFRA_OUTPUT_FILE"
            printf "${MSG_MATCHES_LABEL} (%s):\n" "$MSG_CONTEXT_LINES_LABEL" >> "$CURRENT_INFRA_OUTPUT_FILE"
            printf '%s\n' "$filtered_matches" >> "$CURRENT_INFRA_OUTPUT_FILE"
            current_infra_match_found=1
        fi
    done

    if [ "$current_infra_match_found" -ne 0 ]; then
        if [ -z "${current_infra_hits_written["$abs_filepath"]}" ]; then
            echo "$abs_filepath" >> "$CURRENT_INFRA_HITS_TEMP"
            current_infra_hits_written["$abs_filepath"]=1
        fi
        printf "\n" >> "$CURRENT_INFRA_OUTPUT_FILE"
    fi

    # === 新基盤定義の検索ループ ===
    for condition_name in "${!NEW_INFRA_CONDITIONS[@]}"; do
        pattern="${NEW_INFRA_CONDITIONS[$condition_name]}"
        description=""
        grep_options="-I -E -i -n -C 5 --color=never"
        if [[ "$condition_name" == "IP_new" ]]; then grep_options="-I -E -n -C 5 --color=never"; fi
        raw_matches=$(grep $grep_options -- "$pattern" <<<"$file_content" 2>/dev/null)
        if [ -n "$raw_matches" ]; then
            hit_category=""
            if [[ "$condition_name" == "HOSTNAME_new" ]]; then
                stg_hit_found=false
                prod_hit_found=false
                
                # grep -o でマッチしたホスト名だけを正確に抽出し、誤判定を防ぐ
                extracted_hosts=$(grep -o -i -E "$new_hostname_pattern_fixed" <<< "$file_content")
                
                for host in $extracted_hosts; do
                    if [[ "$host" =~ s$ || "$host" =~ jobarg[0-9]+$ ]]; then
                        stg_hit_found=true
                    fi
                    if [[ "$host" =~ p$ ]]; then
                        prod_hit_found=true
                    fi
                done

                if [[ "$stg_hit_found" == true ]]; then
                    hit_category="stg"
                    description=${NEW_INFRA_CONDITIONS_DESC["HOSTNAME_stg_new"]}
                fi
                if [[ "$prod_hit_found" == true ]]; then
                    hit_category="${hit_category}prod"
                    if [ -n "$description" ]; then
                        description+=", ${NEW_INFRA_CONDITIONS_DESC["HOSTNAME_prod_new"]}"
                    else
                        description=${NEW_INFRA_CONDITIONS_DESC["HOSTNAME_prod_new"]}
                    fi
                fi
            else
                hit_category="other"
                description=${NEW_INFRA_CONDITIONS_DESC[$condition_name]}
            fi
            
            filtered_matches=$(filter_grep_output "$raw_matches" "" "$MSG_HIT_LINE_PREFIX")
            if [ -n "$filtered_matches" ]; then
                if [ "$new_infra_log_header_printed" -eq 0 ]; then
                    printf "%s\n" "$SEPARATOR_LINE_LONG" >> "$NEW_INFRA_OUTPUT_FILE"
                    printf "${MSG_FILE_LABEL}\"%s\"\n" "$filepath" >> "$NEW_INFRA_OUTPUT_FILE"
                    new_infra_log_header_printed=1
                fi
                printf "%s\n" "$SEPARATOR_LINE_SHORT" >> "$NEW_INFRA_OUTPUT_FILE"
                printf "${MSG_CONDITION_LABEL_NEW}: %s %s\n" "$description" "$MSG_COMMENT_IGNORED_LABEL" >> "$NEW_INFRA_OUTPUT_FILE"
                printf "${MSG_MATCHES_LABEL} (%s):\n" "$MSG_CONTEXT_LINES_LABEL" >> "$NEW_INFRA_OUTPUT_FILE"
                printf '%s\n' "$filtered_matches" >> "$NEW_INFRA_OUTPUT_FILE"
                new_infra_match_found=1

                if [[ "$hit_category" == *"stg"* ]] && [ -z "${new_infra_stg_hits_written[$abs_filepath]}" ]; then
                    echo "$abs_filepath" >> "$NEW_INFRA_STG_HITS_TEMP"; new_infra_stg_hits_written[$abs_filepath]=1
                fi
                if [[ "$hit_category" == *"prod"* ]] && [ -z "${new_infra_prod_hits_written[$abs_filepath]}" ]; then
                    echo "$abs_filepath" >> "$NEW_INFRA_PROD_HITS_TEMP"; new_infra_prod_hits_written[$abs_filepath]=1
                fi
                if [[ "$hit_category" == "other" ]] && [ -z "${new_infra_other_hits_written["$condition_name:$abs_filepath"]}" ]; then
                     echo "$condition_name:$abs_filepath" >> "$NEW_INFRA_OTHER_HITS_TEMP"; new_infra_other_hits_written["$condition_name:$abs_filepath"]=1
                fi
            fi
        fi
    done

    if [ "$new_infra_match_found" -ne 0 ]; then
        if [ -z "${new_infra_hits_written["$abs_filepath"]}" ]; then
            echo "$abs_filepath" >> "$NEW_INFRA_HITS_TEMP"
            new_infra_hits_written["$abs_filepath"]=1
        fi
        printf "\n" >> "$NEW_INFRA_OUTPUT_FILE"
    fi

done < <(find "$SEARCH_PATH" -path '*/selinux/*' -prune -o -type f -not -name '*#*' -print0)

# --- 実行結果のサマリー表示 ---
printf -- "\n%s\n" "$SEPARATOR_LINE_SHORT"
printf "%s\n" "$MSG_SEARCH_COMPLETED_PRIMARY"
printf -- "${MSG_CHECK_NEW_INFRA_LOG}" "$NEW_INFRA_OUTPUT_FILE"
printf -- "${MSG_CHECK_CURRENT_INFRA_LOG}" "$CURRENT_INFRA_OUTPUT_FILE"
printf -- "${MSG_SUMMARY_TOTAL_FILES_SCANNED}\n" "$TOTAL_FILES_SCANNED"

# commコマンドで、両方の基盤定義にヒットしたファイルを抽出
if [ -s "$CURRENT_INFRA_HITS_TEMP" ] && [ -s "$NEW_INFRA_HITS_TEMP" ]; then
    comm -12 <(sort -u "$CURRENT_INFRA_HITS_TEMP") <(sort -u "$NEW_INFRA_HITS_TEMP") > "$BOTH_HITS_TEMP"
fi

chmod -R 777 "${OUTPUT_DIR}"

printf "\n%s\n" "$MSG_CURRENT_INFRA_HITS_HEADER"
print_filtered_hit_list "$CURRENT_INFRA_HITS_TEMP"

# --- 分類されたサマリー表示 ---
printf "\n%s\n" "$MSG_NEW_INFRA_HITS_HEADER_STG"
print_filtered_hit_list "$NEW_INFRA_STG_HITS_TEMP"

printf "\n%s\n" "$MSG_NEW_INFRA_HITS_HEADER_PROD"
print_filtered_hit_list "$NEW_INFRA_PROD_HITS_TEMP"

# 「その他条件」のヒットから、ホスト名でのヒットを重複して表示しないように除外
if [ -s "$NEW_INFRA_OTHER_HITS_TEMP" ]; then
    cut -d: -f2- "$NEW_INFRA_OTHER_HITS_TEMP" | sort -u > "$NEW_INFRA_OTHER_HITS_TEMP.paths"
    comm -23 "$NEW_INFRA_OTHER_HITS_TEMP.paths" <(sort -u "$NEW_INFRA_STG_HITS_TEMP" "$NEW_INFRA_PROD_HITS_TEMP") > "$NEW_INFRA_OTHER_HITS_TEMP.finalpaths"
    if [ -s "$NEW_INFRA_OTHER_HITS_TEMP.finalpaths" ]; then
        grep -F -f "$NEW_INFRA_OTHER_HITS_TEMP.finalpaths" "$NEW_INFRA_OTHER_HITS_TEMP" > "$NEW_INFRA_OTHER_HITS_TEMP.final"
    else
        : > "$NEW_INFRA_OTHER_HITS_TEMP.final"
    fi
fi

print_detailed_other_hits "$NEW_INFRA_OTHER_HITS_TEMP.final"

printf "\n%s\n" "$MSG_BOTH_LOGS_HITS_HEADER"
print_both_logs_hit_list "$BOTH_HITS_TEMP"

printf "\n処理にかかった時間: %d 秒\n" "$SECONDS"

# End of script
