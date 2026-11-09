#!/usr/bin/env bash
# 一键部署 3x-ui 节点：Hysteria2、Reality+Vision、Reality+XHTTP、WS+TLS（CDN）
# 用法：bash scripts/deploy.sh [选项] [--参数名 值 ...]
# 当前为第一阶段：仅支持 --dry-run（生成并预览配置，不改动系统）

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"
# shellcheck source=lib/deploy/params.sh
. "$ROOT_DIR/lib/deploy/params.sh"
# shellcheck source=lib/deploy/keys.sh
. "$ROOT_DIR/lib/deploy/keys.sh"
# shellcheck source=lib/deploy/render.sh
. "$ROOT_DIR/lib/deploy/render.sh"

DEPLOY_HOME="${DEPLOY_HOME:-/root/.vps-toolkit}"
CONFIG_FILE="$DEPLOY_HOME/deploy.env"
STATE_FILE="$DEPLOY_HOME/state.env"
DRY_RUN=0
ASK_ALL=0
ASSUME_YES=0
REGEN=0
OUT_DIR=""
INTERACTIVE=0
[ -t 0 ] && INTERACTIVE=1

show_help() {
    cat <<HELP
用法：bash vps.sh deploy [选项] [--参数名 值 ...]

选项：
  --dry-run            只生成配置并预览，不改动系统
  --out 目录           预览输出目录（默认 ./deploy-preview）
  -i, --ask-all        逐项询问全部参数（回车使用默认值）
  -c, --config 文件    配置文件（默认 $CONFIG_FILE）
  --regen              忽略上次生成的值，重新生成全部密钥
  -y, --yes            跳过确认
  -h, --help           显示帮助

参数（也可用同名大写环境变量或写进配置文件，如 REALITY_DOMAIN=...）：
HELP
    local spec
    for spec in "${PARAM_SPECS[@]}"; do
        printf '  --%-16s %s\n' "$(spec_field "$spec" 2)" "$(spec_field "$spec" 3)"
    done
    cat <<HELP

读取优先级：命令行 > 环境变量 > 配置文件 > 上次生成 > 交互输入 / 默认值
必填项缺失且在终端中运行时，会逐项询问。
HELP
}

parse_args() {
    local arg flag val var
    while [ "$#" -gt 0 ]; do
        arg="$1"
        case "$arg" in
            --dry-run) DRY_RUN=1 ;;
            --out) OUT_DIR="${2:?--out 需要目录}"; shift ;;
            --out=*) OUT_DIR="${arg#*=}" ;;
            -i | --ask-all) ASK_ALL=1 ;;
            -c | --config) CONFIG_FILE="${2:?--config 需要文件}"; shift ;;
            --config=*) CONFIG_FILE="${arg#*=}" ;;
            --regen) REGEN=1 ;;
            -y | --yes) ASSUME_YES=1 ;;
            -h | --help) show_help; exit 0 ;;
            --*)
                flag="${arg#--}"
                if [[ "$flag" == *=* ]]; then
                    val="${flag#*=}" flag="${flag%%=*}"
                else
                    [ "$#" -ge 2 ] || die "参数 $arg 缺少值。"
                    val="$2"
                    shift
                fi
                var="$(var_for_flag "$flag")" || die "未知参数：$arg（运行 --help 查看）"
                PARAM_CLI[$var]="$val"
                ;;
            *) die "未知参数：$arg（运行 --help 查看）" ;;
        esac
        shift
    done

    if [ "$ASK_ALL" = "1" ] && [ "$INTERACTIVE" != "1" ]; then
        die "--ask-all 需要在终端中运行。"
    fi
}

# 缺少命令时用系统包管理器安装（需要 root）
ensure_packages() {
    local missing=() cmd
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0

    [ "$(id -u)" -eq 0 ] || die "缺少命令：${missing[*]}，请先安装，或用 root 运行以便自动安装。"
    info "正在安装：${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y >/dev/null && apt-get install -y "${missing[@]}" >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y "${missing[@]}" >/dev/null
    elif command -v yum >/dev/null 2>&1; then
        yum install -y "${missing[@]}" >/dev/null
    else
        die "无法自动安装 ${missing[*]}，请手动安装后重试。"
    fi
    for cmd in "${missing[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || die "安装 $cmd 失败。"
    done
}

# 列出第二阶段将执行的操作，供预览核对
print_plan() {
    local hy2_cert="HTTP 验证（临时使用 TCP 80）"
    [ "$HY2_CERT_MODE" = "dns" ] && hy2_cert="DNS 验证（CF Token）"

    cat <<PLAN

==================== 部署计划（第二阶段执行） ====================
1. 安装 3x-ui：面板端口 $PANEL_PORT，访问路径 /$PANEL_PATH
   已安装则跳过，沿用现有面板账号
2. 证书：
   - $HY2_DOMAIN：$hy2_cert，同时设为面板证书
   - $CDN_DOMAIN：DNS 验证（CF Token），只给 WS+TLS 入站使用
   证书路径 /root/cert/<域名>/，续期后自动执行 x-ui restart
3. 防火墙放行（先放行当前 SSH 端口）：
   TCP $PANEL_PORT（面板） $REALITY_PORT（Reality） $XHTTP_PORT（XHTTP） $CDN_PORT（CDN）$([ "$HY2_CERT_MODE" = "http" ] && printf ' 80（证书）')
   UDP $HY2_PORT（hy2） $HOP_RANGE（端口跳跃）
4. 通过面板 API 导入 4 个入站，客户端 $CLIENT_EMAIL 共用订阅 ID $SUB_ID
5. 更新 Xray 模板：出站与路由（AI 分流：$([ "$ENABLE_AI_ROUTE" = "y" ] && printf '六六云 SS %s:%s' "$SS_ISP_ADDR" "$SS_ISP_PORT" || printf '未启用')）
6. 端口跳跃：UDP $HOP_RANGE -> $HY2_PORT（hy2-port-hopping.sh）
7. 输出面板地址、订阅链接和分享链接
提醒：云厂商安全组需要手动放行第 3 步中的端口。
=================================================================
PLAN
}

main() {
    parse_args "$@"

    if [ "$DRY_RUN" != "1" ]; then
        require_root
    fi
    ensure_packages jq openssl

    capture_env_params
    load_kv_file "$CONFIG_FILE" PARAM_FILE
    [ -r "$CONFIG_FILE" ] && info "已读取配置文件：$CONFIG_FILE"
    if [ "$REGEN" != "1" ] && [ -r "$STATE_FILE" ]; then
        load_kv_file "$STATE_FILE" STATE
        info "已读取上次生成的值：$STATE_FILE"
    fi

    collect_params
    validate_params
    print_summary

    if [ "$DRY_RUN" != "1" ]; then
        die "完整部署将在第二阶段实现，目前请使用 --dry-run 预览。"
    fi

    generate_secrets

    OUT_DIR="${OUT_DIR:-./deploy-preview}"
    local prev_umask
    prev_umask="$(umask)"
    umask 077
    render_all "$OUT_DIR"
    print_plan >"$OUT_DIR/plan.txt"
    umask "$prev_umask"

    print_plan
    info ""
    info "预览已生成：$OUT_DIR"
    info "  readable/  展开后的入站和路由，便于和备份对比"
    info "  payload/   实际提交给面板 API 的内容"
    info "  plan.txt   部署计划"
    warn "预览文件包含私钥和密码，核对后请删除。预览不会保存状态，正式部署时密钥会重新生成。"
}

main "$@"
