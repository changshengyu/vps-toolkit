#!/usr/bin/env bash
# 一键部署 3x-ui 节点：Hysteria2、Reality+Vision、Reality+XHTTP、WS+TLS（CDN）
# 用法：bash scripts/deploy.sh [选项] [--参数名 值 ...]

set -Eeuo pipefail
# 任何未处理的失败都显示出错位置，避免静默退出
trap 'rc=$?; printf "错误：%s 第 %s 行命令失败（退出码 %s）：%s\n" "${BASH_SOURCE[0]##*/}" "$LINENO" "$rc" "$BASH_COMMAND" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"
for _lib in params keys render firewall cert panel; do
    # shellcheck source=/dev/null
    . "$ROOT_DIR/lib/deploy/$_lib.sh"
done
unset _lib

DEPLOY_HOME="${DEPLOY_HOME:-/root/.vps-toolkit}"
CONFIG_FILE="$DEPLOY_HOME/deploy.env"
STATE_FILE="$DEPLOY_HOME/state.env"
RESULT_FILE="${RESULT_FILE:-/root/vps-deploy-result.txt}"
DRY_RUN=0
ASK_ALL=0
ASSUME_YES=0
REGEN=0
REPLACE=""
CONFIG_ONLY=0
SKIP_FIREWALL=0
SKIP_HOP=0
OUT_DIR=""
PREVIEW_DIR=""
PUBLIC_IP=""
IMPORTED=()
INTERACTIVE=0
[ -t 0 ] && INTERACTIVE=1

show_help() {
    cat <<HELP
用法：bash vps.sh deploy [选项] [--参数名 值 ...]

选项：
  --dry-run            只生成配置并预览，不改动系统
  --config-only        只更新已有 3x-ui 的出站、路由和 geodata，不动入站、证书和面板
  --out 目录           预览输出目录（默认 ./deploy-preview）
  -i, --ask-all        逐项询问全部参数（回车使用默认值）
  -c, --config 文件    配置文件（默认 $CONFIG_FILE）
  --regen              忽略上次生成的值，重新生成全部密钥
  --replace            同端口已有入站时直接覆盖（默认询问；非交互时跳过）
  --no-replace         同端口已有入站时直接跳过
  --skip-firewall      不修改系统防火墙
  --skip-hop           不配置 hy2 端口跳跃
  -y, --yes            跳过确认
  -h, --help           显示帮助

参数（也可用同名大写环境变量或写进配置文件，如 REALITY_DOMAIN=...）：
HELP
    local spec
    for spec in "${PARAM_SPECS[@]}"; do
        printf '  --%-16s %s\n' "$(spec_field "$spec" 2)" "$(spec_field "$spec" 3)"
    done
    cat <<HELP

读取优先级：命令行 > 环境变量 > 配置文件 > 上次部署 > 交互输入 / 默认值
必填项缺失且在终端中运行时，会逐项询问。
HELP
}

parse_args() {
    local arg flag val var
    while [ "$#" -gt 0 ]; do
        arg="$1"
        case "$arg" in
            --dry-run) DRY_RUN=1 ;;
            --config-only) CONFIG_ONLY=1 ;;
            --out) OUT_DIR="${2:?--out 需要目录}"; shift ;;
            --out=*) OUT_DIR="${arg#*=}" ;;
            -i | --ask-all) ASK_ALL=1 ;;
            -c | --config) CONFIG_FILE="${2:?--config 需要文件}"; shift ;;
            --config=*) CONFIG_FILE="${arg#*=}" ;;
            --regen) REGEN=1 ;;
            --replace) REPLACE=y ;;
            --no-replace) REPLACE=n ;;
            --skip-firewall) SKIP_FIREWALL=1 ;;
            --skip-hop) SKIP_HOP=1 ;;
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

print_plan() {
    local hy2_cert="HTTP 验证（临时使用 TCP 80）" panel_line
    [ "$HY2_CERT_MODE" = "dns" ] && hy2_cert="DNS 验证（CF Token）"
    if [ "$PANEL_INSTALLED" = "1" ]; then
        panel_line="已安装，沿用现有面板（端口 $PANEL_PORT）"
    else
        panel_line="安装最新版：端口 $PANEL_PORT，访问路径 /$PANEL_PATH"
    fi

    # HTTP 验证模式下，证书续期也要用 TCP 80，需要一直放行
    local port80=""
    [ "$HY2_CERT_MODE" = "http" ] && port80=" 80（证书申请与续期）"

    cat <<PLAN

==================== 部署计划 ====================
1. 3x-ui：$panel_line
2. 防火墙放行（先放行当前 SSH 端口）：
   TCP $PANEL_PORT（面板） $REALITY_PORT（Reality） $XHTTP_PORT（XHTTP） $CDN_PORT（CDN）$port80
   UDP $HY2_PORT（hy2） $HOP_RANGE（端口跳跃）
3. 证书（/root/cert/<域名>/，续期后自动重启 x-ui）：
   - $HY2_DOMAIN：$hy2_cert，同时设为面板证书
   - $CDN_DOMAIN：DNS 验证（CF Token），只给 WS+TLS 入站使用
4. 通过面板 API 导入 4 个入站，客户端 $CLIENT_EMAIL 共用订阅 ID ${SUB_ID:-（部署时生成）}
5. 更新 Xray 模板：出站与路由（AI 分流：$([ "$ENABLE_AI_ROUTE" = "y" ] && printf '六六云 SS %s:%s' "$SS_ISP_ADDR" "$SS_ISP_PORT" || printf '未启用')）
   geodata 自动更新：$([ "$GEODATA_CRON" = "off" ] || [ -z "$GEODATA_CRON" ] && printf '关闭' || printf 'cron "%s"（UTC）' "$GEODATA_CRON")
6. 端口跳跃：UDP $HOP_RANGE -> $HY2_PORT
7. 输出面板地址和分享链接到 $RESULT_FILE
提醒：云厂商安全组需要手动放行第 2 步中的端口。
==================================================
PLAN
}

run_dry_run() {
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

step() { info ""; info "==> $*"; }

write_result() {
    local scheme="https" path_part="" prev_umask
    [ -n "$PANEL_PATH" ] && path_part="/$PANEL_PATH"
    [ "$(panel_scheme)" = "http" ] && scheme="http"
    prev_umask="$(umask)"
    umask 077
    {
        printf '# vps-toolkit 一键部署结果（%s）\n\n' "$(date '+%F %T')"
        printf '面板地址：%s://%s:%s%s\n' "$scheme" "$HY2_DOMAIN" "$PANEL_PORT" "$path_part"
        if [ -n "$PANEL_USER" ]; then
            printf '面板用户：%s\n面板密码：%s\n' "$PANEL_USER" "$PANEL_PASS"
        else
            printf '面板账号：沿用现有账号\n'
        fi
        printf '\n客户端：%s，订阅 ID：%s\n\n分享链接：\n' "$CLIENT_EMAIL" "$SUB_ID"
        fetch_links
        printf '\n云厂商安全组需放行：TCP %s %s %s %s%s；UDP %s %s\n' \
            "$PANEL_PORT" "$REALITY_PORT" "$XHTTP_PORT" "$CDN_PORT" \
            "$([ "$HY2_CERT_MODE" = "http" ] && printf ' 80' || true)" "$HY2_PORT" "$HOP_RANGE"
    } >"$RESULT_FILE"
    chmod 600 "$RESULT_FILE"
    umask "$prev_umask"
}

print_config_plan() {
    cat <<PLAN

==================== 更新计划（只更新配置） ====================
将替换已有 3x-ui 的 Xray 模板中以下部分，入站、证书、面板设置不变：
- 出站：direct、blocked$([ "$ENABLE_AI_ROUTE" = "y" ] && printf '、六六云 SS %s:%s' "$SS_ISP_ADDR" "$SS_ISP_PORT")（其他出站会被移除）
- 路由：IPIfNonMatch；$([ "$ENABLE_AI_ROUTE" = "y" ] && printf 'AI 分流、')回国流量按 geoip:cn 屏蔽、BT 屏蔽（原有规则会被替换）
- geodata 自动更新：$([ "$GEODATA_CRON" = "off" ] || [ -z "$GEODATA_CRON" ] && printf '关闭' || printf 'cron "%s"' "$GEODATA_CRON")
修改前会备份当前模板到 $DEPLOY_HOME/backup/。
================================================================
PLAN
}

# --config-only：只更新已有面板的出站、路由和 geodata
run_config_only() {
    local backup

    panel_installed || die "未安装 3x-ui，--config-only 只能用于已有面板。"
    HY2_DOMAIN="$(panel_cert_domain)"

    step "连接面板 API"
    init_api
    wait_panel_ready
    info "面板就绪：$API_BASE"

    PREVIEW_DIR="$(mktemp -d)"
    mkdir -p "$PREVIEW_DIR/payload"
    render_routing >"$PREVIEW_DIR/payload/xray-routing.json"

    step "更新 Xray 模板"
    backup="$(backup_xray_template)"
    info "已备份当前 Xray 模板：$backup"
    update_xray_template
    rm -rf "$PREVIEW_DIR"
    restart_xray

    info ""
    info "配置已更新。可在面板「路由 → 路由测试」中验证：baidu.com 应为 blocked，www.gstatic.com 应为 direct。"
}

run_deploy() {
    local backup

    step "检查环境"
    require_command systemctl
    ensure_packages curl tar
    check_ports_free
    check_dns

    if [ "$SKIP_FIREWALL" = "1" ]; then
        step "防火墙：已按 --skip-firewall 跳过"
    else
        step "配置防火墙"
        setup_firewall
    fi

    step "安装 3x-ui"
    install_panel

    step "生成密钥"
    generate_secrets
    save_state "$STATE_FILE"
    info "已保存到 $STATE_FILE"

    step "申请证书"
    setup_certs
    set_panel_cert

    step "连接面板 API"
    init_api
    wait_panel_ready
    info "面板就绪：$API_BASE"

    PREVIEW_DIR="$DEPLOY_HOME/last-render"
    (umask 077 && render_all "$PREVIEW_DIR")

    step "导入入站"
    backup="$(backup_xray_template)"
    info "已备份当前 Xray 模板：$backup"
    import_inbounds

    step "更新 Xray 模板"
    update_xray_template
    restart_xray

    if [ "$SKIP_HOP" = "1" ]; then
        step "端口跳跃：已按 --skip-hop 跳过"
    else
        step "配置 hy2 端口跳跃"
        HY2_PORT="$HY2_PORT" HOP_RANGE="$HOP_RANGE" IFACE="${IFACE:-}" \
            bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" install
    fi

    step "生成结果"
    write_result

    info ""
    info "==================== 部署完成 ===================="
    cat "$RESULT_FILE"
    info "=================================================="
    info "以上内容已保存到 $RESULT_FILE（权限 600）。"
    if [ "${#IMPORTED[@]}" -lt 4 ]; then
        warn "有入站被跳过，只导入了：${IMPORTED[*]:-无}"
    fi
}

main() {
    parse_args "$@"

    if [ "$DRY_RUN" != "1" ]; then
        require_root
    fi
    ensure_packages jq openssl

    capture_env_params
    load_kv_file "$CONFIG_FILE" PARAM_FILE
    if [ -r "$CONFIG_FILE" ]; then info "已读取配置文件：$CONFIG_FILE"; fi
    if [ "$REGEN" != "1" ] && [ -r "$STATE_FILE" ]; then
        load_kv_file "$STATE_FILE" STATE
        info "已读取上次部署的参数和密钥：$STATE_FILE"
    fi
    PANEL_INSTALLED_BY_US="${STATE[PANEL_INSTALLED_BY_US]:-0}"

    if [ "$CONFIG_ONLY" = "1" ]; then
        [ "$DRY_RUN" != "1" ] || die "--config-only 不能和 --dry-run 同时使用。"
        panel_installed || die "未安装 3x-ui，--config-only 只能用于已有面板。"
        collect_params
        detect_existing_panel
        validate_config_params
        print_summary
        print_config_plan
        if [ "$ASSUME_YES" != "1" ]; then
            [ "$INTERACTIVE" = "1" ] || die "非交互运行时请加 -y 确认。"
            confirm "确认更新配置？" || {
                info "已取消。"
                exit 0
            }
        fi
        run_config_only
        return 0
    fi

    collect_params
    detect_existing_panel
    validate_params
    print_summary

    if [ "$DRY_RUN" = "1" ]; then
        run_dry_run
        return 0
    fi

    print_plan
    if [ "$ASSUME_YES" != "1" ]; then
        [ "$INTERACTIVE" = "1" ] || die "非交互运行时请加 -y 确认部署。"
        confirm "确认按以上计划部署？" || {
            info "已取消。"
            exit 0
        }
    fi
    run_deploy
}

main "$@"
