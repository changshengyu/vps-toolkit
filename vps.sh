#!/usr/bin/env bash
# 个人 VPS 工具集入口
# 用法：bash vps.sh [命令] [操作]，无参数时打开菜单

set -euo pipefail

REPO_ARCHIVE="${VPS_TOOLKIT_ARCHIVE:-https://github.com/changshengyu/vps-toolkit/archive/refs/heads/main.tar.gz}"
# 通过 curl | bash 运行时没有脚本路径，ROOT_DIR 留空以触发下载完整仓库
ROOT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# 单独下载 vps.sh 时，先获取完整仓库再运行
if [ ! -f "$ROOT_DIR/lib/common.sh" ]; then
    command -v curl >/dev/null 2>&1 || {
        echo "错误：缺少命令：curl" >&2
        exit 1
    }
    command -v tar >/dev/null 2>&1 || {
        echo "错误：缺少命令：tar" >&2
        exit 1
    }

    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "$WORK_DIR"' EXIT
    curl -fsSL "$REPO_ARCHIVE" | tar -xz -C "$WORK_DIR" --strip-components=1

    # 通过 curl | bash 运行时，标准输入是脚本本身，改从终端读取，交互输入才能正常工作
    rc=0
    if [ ! -t 0 ] && { : </dev/tty; } 2>/dev/null; then
        bash "$WORK_DIR/vps.sh" "$@" </dev/tty || rc=$?
    else
        bash "$WORK_DIR/vps.sh" "$@" || rc=$?
    fi
    exit "$rc"
fi

# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

show_help() {
    cat <<HELP
用法：bash vps.sh [命令] [操作]

命令：
  无参数                     打开菜单
  deploy [选项]              一键部署 3x-ui 节点（--help 查看选项）
  hy2 install|status|uninstall
                             管理 Hysteria2 端口跳跃
  3x-ui install|status       安装 3x-ui 或查看状态
  routing [策略]             修改 3x-ui 路由域策略（默认 IPIfNonMatch）
  geodata [cron|off]         开启 / 关闭 3x-ui 的 geodata 自动更新
  result                     查看上次部署结果
  ipv6 status|enable|disable 查看 / 开启 / 关闭系统 IPv6
  monitor [操作]             管理 Prometheus / Grafana 监控（help 查看操作）
  shortcut                   安装快捷命令 vt
  tools [上游参数]           运行 VPS 多功能工具箱
  help                       显示帮助
HELP
}

RESULT_FILE="${RESULT_FILE:-/root/vps-deploy-result.txt}"
SHORTCUT="/usr/local/bin/vt"
REPO_RAW_SCRIPT="https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh"

# 终端支持时输出颜色
if [ -t 1 ]; then
    C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[31m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'
else
    C_GREEN="" C_YELLOW="" C_RED="" C_BOLD="" C_RESET=""
fi

status_xui() {
    if [ ! -x /usr/local/x-ui/x-ui ]; then
        printf '%s未安装%s' "$C_YELLOW" "$C_RESET"
    elif systemctl is-active --quiet x-ui 2>/dev/null; then
        printf '%s运行中%s' "$C_GREEN" "$C_RESET"
    else
        printf '%s未运行%s' "$C_RED" "$C_RESET"
    fi
}

status_hop() {
    if systemctl is-active --quiet hy2jump 2>/dev/null; then
        printf '%s已启用%s' "$C_GREEN" "$C_RESET"
    else
        printf '%s未启用%s' "$C_YELLOW" "$C_RESET"
    fi
}

status_ipv6() {
    if [ ! -d /proc/sys/net/ipv6 ] || [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" = "1" ]; then
        printf '%s已关闭%s' "$C_YELLOW" "$C_RESET"
    else
        printf '%s已开启%s' "$C_GREEN" "$C_RESET"
    fi
}

status_result() {
    if [ -f "$RESULT_FILE" ]; then
        printf '%s有%s' "$C_GREEN" "$C_RESET"
    else
        printf '无'
    fi
}

pause() {
    printf '\n'
    read -r -p "按回车返回菜单..." _ || exit 0
}

# 运行子脚本，失败时提示并返回菜单
run() {
    "$@" || warn "操作未完成（退出码 $?）。"
}

hy2_install_prompt() {
    local answer

    read -r -p "Hysteria2 端口 [443]: " answer
    HY2_PORT="${answer:-443}"
    read -r -p "跳跃端口范围 [20000-20100]: " answer
    HOP_RANGE="${answer:-20000-20100}"
    read -r -p "网卡名 [自动检测]: " answer
    IFACE="${answer:-}"

    HY2_PORT="$HY2_PORT" HOP_RANGE="$HOP_RANGE" IFACE="$IFACE" \
        bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" install
}

geodata_custom() {
    local cron
    read -r -p "cron 表达式（按服务器时区，通常为 UTC）[0 20 * * *]: " cron
    bash "$ROOT_DIR/scripts/xui-geodata.sh" "${cron:-0 20 * * *}"
}

show_result() {
    if [ -f "$RESULT_FILE" ]; then
        cat "$RESULT_FILE"
    else
        info "还没有部署结果，请先运行一键部署。"
    fi
}

# 安装快捷命令：之后输入 vt 即可打开菜单（每次运行仓库最新版）
install_shortcut() {
    require_root
    if [ -e "$SHORTCUT" ] && ! grep -q "vps-toolkit" "$SHORTCUT" 2>/dev/null; then
        die "$SHORTCUT 已存在且不是本工具创建的，未覆盖。"
    fi
    cat >"$SHORTCUT" <<SCRIPT
#!/usr/bin/env bash
# vps-toolkit 快捷命令：运行仓库最新版
exec bash <(curl -fsSL $REPO_RAW_SCRIPT) "\$@"
SCRIPT
    chmod 755 "$SHORTCUT"
    info "已安装快捷命令：输入 vt 打开菜单，也可以直接带参数，如 vt deploy --config-only。"
}

# 菜单标题和状态栏
menu_header() {
    cat <<HEAD

${C_BOLD}╔══════════════════════════════════════════════╗
║                 vps-toolkit                  ║
╚══════════════════════════════════════════════╝${C_RESET}
  3x-ui：$(status_xui)  端口跳跃：$(status_hop)  IPv6：$(status_ipv6)  部署结果：$(status_result)
HEAD
}

# 打印编号选项：menu_items "标题" "选项1" "选项2" ...（0 由调用方决定是“返回”还是“退出”）
menu_items() {
    local title="$1" i=1 item
    shift
    printf '\n  %s%s%s\n' "$C_BOLD" "$title" "$C_RESET"
    for item in "$@"; do
        printf '  %s%d.%s %s\n' "$C_GREEN" "$i" "$C_RESET" "$item"
        i=$((i + 1))
    done
}

# 读取选项；输入结束（Ctrl+D）时退出
read_choice() {
    read -r -p "$(printf '\n请输入选项 [0-%s]: ' "$1")" CHOICE || exit 0
    printf '\n'
}

deploy_menu() {
    while true; do
        menu_header
        menu_items "节点部署" \
            "一键部署节点" \
            "预览部署配置（不改动系统）" \
            "重新部署（覆盖已有的同端口入站）" \
            "旧机器只更新配置（出站、路由、geodata）" \
            "查看部署结果（面板地址、分享链接）"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 5
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/deploy.sh" ;;
            2) run bash "$ROOT_DIR/scripts/deploy.sh" --dry-run ;;
            3) run bash "$ROOT_DIR/scripts/deploy.sh" --replace ;;
            4) run bash "$ROOT_DIR/scripts/deploy.sh" --config-only ;;
            5) show_result ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

geodata_menu() {
    while true; do
        menu_items "geodata 自动更新（geoip.dat、geosite.dat）" \
            "开启自动更新（每天北京时间 04:00）" \
            "自定义更新时间" \
            "关闭自动更新"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 3
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/xui-geodata.sh" ;;
            2) run geodata_custom ;;
            3) run bash "$ROOT_DIR/scripts/xui-geodata.sh" off ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

xui_menu() {
    while true; do
        menu_header
        menu_items "3x-ui 面板" \
            "安装 3x-ui" \
            "查看 3x-ui 状态" \
            "路由域策略改为 IPIfNonMatch" \
            "geodata 自动更新 >"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 4
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/3x-ui.sh" install ;;
            2) run bash "$ROOT_DIR/scripts/3x-ui.sh" status ;;
            3) run bash "$ROOT_DIR/scripts/xui-routing.sh" IPIfNonMatch ;;
            4) geodata_menu; continue ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

hy2_menu() {
    while true; do
        menu_header
        menu_items "Hysteria2 端口跳跃" \
            "安装或更新规则" \
            "查看状态" \
            "卸载"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 3
        case "$CHOICE" in
            1) run hy2_install_prompt ;;
            2) run bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" status ;;
            3)
                if confirm "确认卸载端口跳跃规则？"; then
                    run bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" uninstall
                fi
                ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_menu() {
    while true; do
        menu_header
        menu_items "监控管理（Prometheus + Grafana）" \
            "本机作为监控机：安装完整套件" \
            "本机作为被监控节点：只安装采集器" \
            "验证并接入 HTTPS 节点 / 迁移旧 SSH 节点" \
            "移除远端节点（在监控机操作）" \
            "查看状态和资源占用" \
            "查看日志" \
            "查看面板访问方法" \
            "生成节点 HTTPS 证书包（在监控机操作）" \
            "启动监控服务" \
            "停止监控服务" \
            "重启监控服务" \
            "卸载监控服务（保留数据）" \
            "查看证书有效期" \
            "续签客户端或节点证书"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 14
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" hub-install ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" node-install ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" add-node ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" remove-node ;;
            5) run bash "$ROOT_DIR/scripts/monitor.sh" status ;;
            6) run bash "$ROOT_DIR/scripts/monitor.sh" logs ;;
            7) run bash "$ROOT_DIR/scripts/monitor.sh" access ;;
            8) run bash "$ROOT_DIR/scripts/monitor.sh" prepare-node ;;
            9) run bash "$ROOT_DIR/scripts/monitor.sh" start ;;
            10) run bash "$ROOT_DIR/scripts/monitor.sh" stop ;;
            11) run bash "$ROOT_DIR/scripts/monitor.sh" restart ;;
            12) run bash "$ROOT_DIR/scripts/monitor.sh" uninstall ;;
            13) run bash "$ROOT_DIR/scripts/monitor.sh" cert-status ;;
            14) run bash "$ROOT_DIR/scripts/monitor.sh" cert-renew ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

system_menu() {
    while true; do
        menu_header
        menu_items "系统设置" \
            "查看 IPv6 状态" \
            "开启 IPv6" \
            "关闭 IPv6"
        printf '  %s0.%s 返回\n' "$C_GREEN" "$C_RESET"
        read_choice 3
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/ipv6.sh" status ;;
            2) run bash "$ROOT_DIR/scripts/ipv6.sh" enable ;;
            3)
                if confirm "关闭 IPv6 后，节点的 IPv6 地址将不可用。确认关闭？"; then
                    run bash "$ROOT_DIR/scripts/ipv6.sh" disable
                fi
                ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

main_menu() {
    while true; do
        menu_header
        menu_items "主菜单" \
            "节点部署 >" \
            "3x-ui 面板 >" \
            "Hysteria2 端口跳跃 >" \
            "系统设置（IPv6）>" \
            "VPS 多功能工具箱" \
            "安装快捷命令 vt" \
            "监控管理（Prometheus + Grafana）>"
        printf '  %s0.%s 退出\n' "$C_GREEN" "$C_RESET"
        read_choice 7
        case "$CHOICE" in
            1) deploy_menu ;;
            2) xui_menu ;;
            3) hy2_menu ;;
            4) system_menu ;;
            5) run bash "$ROOT_DIR/scripts/vps-tools.sh"; pause ;;
            6) run install_shortcut; pause ;;
            7) monitor_menu ;;
            0) exit 0 ;;
            *) warn "无效选项：$CHOICE" ;;
        esac
    done
}

case "${1:-menu}" in
    menu) main_menu ;;
    deploy)
        shift
        bash "$ROOT_DIR/scripts/deploy.sh" "$@"
        ;;
    hy2) bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" "${2:-status}" ;;
    3x-ui) bash "$ROOT_DIR/scripts/3x-ui.sh" "${2:-status}" ;;
    routing) bash "$ROOT_DIR/scripts/xui-routing.sh" "${2:-IPIfNonMatch}" ;;
    geodata) bash "$ROOT_DIR/scripts/xui-geodata.sh" "${2:-0 20 * * *}" ;;
    result) show_result ;;
    ipv6) bash "$ROOT_DIR/scripts/ipv6.sh" "${2:-status}" ;;
    monitor)
        shift
        if [ "$#" -eq 0 ]; then
            monitor_menu
        else
            bash "$ROOT_DIR/scripts/monitor.sh" "$@"
        fi
        ;;
    shortcut) install_shortcut ;;
    tools)
        shift
        bash "$ROOT_DIR/scripts/vps-tools.sh" "$@"
        ;;
    help | -h | --help) show_help ;;
    *)
        show_help >&2
        exit 1
        ;;
esac
