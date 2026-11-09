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
  shortcut                   安装快捷命令 vt
  tools [上游参数]           运行 VPS 多功能工具箱
  help                       显示帮助
HELP
}

RESULT_FILE="/root/vps-deploy-result.txt"
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

print_menu() {
    cat <<MENU

${C_BOLD}╔══════════════════════════════════════════════╗
║                 vps-toolkit                  ║
╚══════════════════════════════════════════════╝${C_RESET}
  3x-ui：$(status_xui)    端口跳跃：$(status_hop)    部署结果：$(status_result)

  ${C_BOLD}节点部署${C_RESET}
  ${C_GREEN}1.${C_RESET}  一键部署节点
  ${C_GREEN}2.${C_RESET}  预览部署配置（不改动系统）
  ${C_GREEN}3.${C_RESET}  重新部署（覆盖已有的同端口入站）
  ${C_GREEN}4.${C_RESET}  旧机器只更新配置（出站、路由、geodata）
  ${C_GREEN}5.${C_RESET}  查看部署结果（面板地址、分享链接）

  ${C_BOLD}3x-ui 面板${C_RESET}
  ${C_GREEN}6.${C_RESET}  安装 3x-ui
  ${C_GREEN}7.${C_RESET}  查看 3x-ui 状态
  ${C_GREEN}8.${C_RESET}  路由域策略改为 IPIfNonMatch
  ${C_GREEN}9.${C_RESET}  开启 geodata 自动更新（每天北京时间 04:00）
  ${C_GREEN}10.${C_RESET} 自定义 geodata 更新时间
  ${C_GREEN}11.${C_RESET} 关闭 geodata 自动更新

  ${C_BOLD}Hysteria2 端口跳跃${C_RESET}
  ${C_GREEN}12.${C_RESET} 安装或更新规则
  ${C_GREEN}13.${C_RESET} 查看状态
  ${C_GREEN}14.${C_RESET} 卸载

  ${C_BOLD}其他${C_RESET}
  ${C_GREEN}15.${C_RESET} VPS 多功能工具箱
  ${C_GREEN}16.${C_RESET} 安装快捷命令 vt
  ${C_GREEN}0.${C_RESET}  退出
MENU
}

main_menu() {
    local choice

    while true; do
        print_menu
        read -r -p "请输入选项 [0-16]: " choice || exit 0
        printf '\n'

        case "$choice" in
            1) run bash "$ROOT_DIR/scripts/deploy.sh" ;;
            2) run bash "$ROOT_DIR/scripts/deploy.sh" --dry-run ;;
            3) run bash "$ROOT_DIR/scripts/deploy.sh" --replace ;;
            4) run bash "$ROOT_DIR/scripts/deploy.sh" --config-only ;;
            5) show_result ;;
            6) run bash "$ROOT_DIR/scripts/3x-ui.sh" install ;;
            7) run bash "$ROOT_DIR/scripts/3x-ui.sh" status ;;
            8) run bash "$ROOT_DIR/scripts/xui-routing.sh" IPIfNonMatch ;;
            9) run bash "$ROOT_DIR/scripts/xui-geodata.sh" ;;
            10) run geodata_custom ;;
            11) run bash "$ROOT_DIR/scripts/xui-geodata.sh" off ;;
            12) run hy2_install_prompt ;;
            13) run bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" status ;;
            14)
                if confirm "确认卸载端口跳跃规则？"; then
                    run bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" uninstall
                fi
                ;;
            15) run bash "$ROOT_DIR/scripts/vps-tools.sh" ;;
            16) run install_shortcut ;;
            0) exit 0 ;;
            *)
                warn "无效选项：$choice"
                continue
                ;;
        esac
        pause
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
