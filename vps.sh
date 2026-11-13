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
  sni-site [操作]            ISP HTTPS 静态站点 / Reality SNI（help 查看操作）
  shortcut                   安装快捷命令 vt
  tools [上游参数]           运行 VPS 多功能工具箱
  help                       显示帮助
HELP
}

RESULT_FILE="${RESULT_FILE:-/root/vps-deploy-result.txt}"
SHORTCUT="${VPS_TOOLKIT_SHORTCUT:-/usr/local/bin/vt}"
MENU_HOME=0
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
    local answer
    printf '\n'
    read -r -p "回车继续；m 回主菜单；q 退出: " answer || exit 0
    case "$answer" in m|M) MENU_HOME=1 ;; q|Q) exit 0 ;; esac
}

# 运行子脚本，失败时提示并返回菜单
run() {
    ( "$@" ) || warn "操作未完成（退出码 $?）。"
}

hy2_install_prompt() {
    local answer

    read -r -p "Hysteria2 端口 [443]: " answer || return 1
    HY2_PORT="${answer:-443}"
    read -r -p "跳跃端口范围 [20000-20100]: " answer || return 1
    HOP_RANGE="${answer:-20000-20100}"
    read -r -p "网卡名 [自动检测]: " answer || return 1
    IFACE="${answer:-}"

    HY2_PORT="$HY2_PORT" HOP_RANGE="$HOP_RANGE" IFACE="$IFACE" \
        bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" install
}

geodata_custom() {
    local cron
    read -r -p "cron 表达式（按服务器时区，通常为 UTC）[0 20 * * *]: " cron || return 1
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

# Menu summaries read local state only; never source .env or query external APIs.
status_monitor_role() {
    if [ -f "${MONITOR_DIR:-/opt/vps-toolkit-monitor}/.vps-toolkit-managed" ]; then
        printf '监控机'
    elif [ -f "/usr/local/lib/vps-toolkit-monitor/.vps-toolkit-managed" ]; then
        printf '被监控节点'
    else
        printf '未部署监控'
    fi
}

status_shortcut() {
    if [ -f "$SHORTCUT" ] && grep -q 'vps-toolkit' "$SHORTCUT" 2>/dev/null; then
        if [ -x "$SHORTCUT" ]; then printf '已安装，输入 vt'; else printf '需重新安装（6 → 1）'; fi
    elif [ -e "$SHORTCUT" ]; then
        printf 'vt 名称已被其他程序占用'
    else
        printf '未安装（6 → 1）'
    fi
}

machine_summary() {
    local os_name host_name
    host_name="$(hostname 2>/dev/null || printf '未知')"
    os_name="$(uname -s 2>/dev/null || printf '未知')"
    if [ -r /etc/os-release ]; then
        os_name="$(awk -F= '$1 == "PRETTY_NAME" {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' /etc/os-release)"
    fi
    printf '  主机：%s  系统：%s  架构：%s\n' "$host_name" "$os_name" "$(uname -m)"
    if [ -r /proc/meminfo ]; then
        awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {available=$2}
            END {if (total) printf "  内存可用：%.0f / %.0f MiB", available/1024, total/1024}' /proc/meminfo
        printf '  时区：%s\n' "$(date +%Z)"
    fi
    if [ -r /proc/uptime ]; then
        awk '{n=int($1); printf "  已运行：%d 天 %d 小时 %d 分钟\n", n/86400, (n%86400)/3600, (n%3600)/60}' /proc/uptime
    fi
}

configured_panel_summary() {
    local directory="${MONITOR_DIR:-/opt/vps-toolkit-monitor}" domain=""
    if [ -r "$directory/tunnel/state.json" ] && command -v jq >/dev/null 2>&1; then
        domain="$(jq -r 'select(.configured == true) | .domain // empty' "$directory/tunnel/state.json" 2>/dev/null || true)"
    fi
    if [[ "$domain" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
        if [ -f "$directory/compose.tunnel.yaml" ]; then
            printf '  Grafana 配置入口：https://%s/（健康状态见监控菜单）\n' "$domain"
        else
            printf '  Grafana 域名：https://%s/（入口已暂停）\n' "$domain"
        fi
    fi
}

opening_commands() {
    printf '  在线打开（可复制）：\n'
    printf 'bash <(curl -fsSL %s)\n' "$REPO_RAW_SCRIPT"
    printf '  快捷命令：%s\n' "$(status_shortcut)"
    printf '  常用命令（安装 vt 后）：vt monitor  |  vt help\n'
}

menu_header() {
    local location="${1:-主菜单}"
    if [ "$location" != 主菜单 ]; then
        printf '\n%s  当前位置：%s%s\n' "$C_BOLD" "$location" "$C_RESET"
        return 0
    fi
    cat <<HEAD

${C_BOLD}╔══════════════════════════════════════════════╗
║                 vps-toolkit                  ║
╚══════════════════════════════════════════════╝${C_RESET}
HEAD
    machine_summary
    printf '  3x-ui：%s  端口跳跃：%s  IPv6：%s  部署结果：%s\n' "$(status_xui)" "$(status_hop)" "$(status_ipv6)" "$(status_result)"
    printf '  监控角色：%s\n' "$(status_monitor_role)"
    configured_panel_summary
    printf '\n'
    opening_commands
    printf '  更新菜单：退出后重新打开；已打开的菜单不会自动更新。\n'
}

menu_navigation() {
    printf '  %s0.%s %s  |  m. 主菜单  |  q. 退出\n' "$C_GREEN" "$C_RESET" "${1:-返回上级}"
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
    read -r -p "$(printf '\n请输入选项 [0-%s / m / q]: ' "$1")" CHOICE || exit 0
    case "$CHOICE" in
        q|Q) exit 0 ;;
        m|M) MENU_HOME=1; CHOICE=0 ;;
    esac
    printf '\n'
}

deploy_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 节点部署与更新"
        info "  首次先预览，再正式部署；已有机器更新出站和路由请选择 3。"
        menu_items "节点部署与更新" \
            "预览部署配置（不改动系统）" \
            "一键部署节点" \
            "更新现有节点配置（不改入站）" \
            "重新部署并覆盖同端口入站" \
            "查看部署结果（面板地址、分享链接）"
        menu_navigation
        read_choice 5
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/deploy.sh" --dry-run ;;
            2) run bash "$ROOT_DIR/scripts/deploy.sh" ;;
            3) run bash "$ROOT_DIR/scripts/deploy.sh" --config-only ;;
            4) run bash "$ROOT_DIR/scripts/deploy.sh" --replace ;;
            5) run show_result ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

geodata_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 3x-ui 面板 / geodata 更新"
        info "  定时任务按服务器时区执行，当前时区：$(date +%Z)。"
        menu_items "geodata 自动更新（geoip.dat、geosite.dat）" \
            "开启自动更新（服务器时间每天 20:00）" \
            "自定义更新时间" \
            "关闭自动更新"
        menu_navigation
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
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 3x-ui 面板"
        menu_items "3x-ui 面板" \
            "安装 3x-ui" \
            "查看 3x-ui 状态" \
            "路由域策略改为 IPIfNonMatch" \
            "geodata 自动更新 >"
        menu_navigation
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
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / Hysteria2 端口跳跃"
        menu_items "Hysteria2 端口跳跃" \
            "安装或更新规则" \
            "查看状态" \
            "卸载"
        menu_navigation
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

monitor_nodes_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / 节点与证书"
        info "  新增或重新部署节点：返回监控管理选择 2。"
        menu_items "节点与证书" \
            "查看节点证书有效期" \
            "续签并同步远端节点证书" \
            "续签监控机客户端证书" \
            "移除节点采集（保留远端服务与历史）"
        menu_navigation
        read_choice 4
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" cert-status ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" renew-node ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" cert-renew client ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" remove-node ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_manual_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / 高级手动接入"
        info "  手动顺序：监控机生成包 → 被监控机安装 → 监控机验证接入。"
        menu_items "高级手动接入（自行传输证书包）" \
            "生成节点证书包（在监控机）" \
            "安装采集器和证书包（在被监控机）" \
            "验证并接入已安装的节点（在监控机）"
        menu_navigation
        read_choice 3
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" prepare-node ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" node-install ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" add-node ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_status_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / 状态与日志"
        menu_items "状态与日志" "查看服务、节点与资源占用" "查看监控日志"
        menu_navigation
        read_choice 2
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" status ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" logs ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_services_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / 服务管理"
        menu_items "监控服务管理（本机）" \
            "启动" "停止" "重启" "卸载服务（保留数据）"
        menu_navigation
        read_choice 4
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" start ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" stop ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" restart ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" uninstall ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_access_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / Grafana 面板"
        info "  域名入口在监控管理第 4 项；这里管理登录和面板本身。"
        menu_items "Grafana 面板" \
            "查看面板地址与 SSH 访问方法" \
            "修改登录账号 / 密码" \
            "检查面板健康（502 / 无法连接）" \
            "调整 Grafana 资源 / 修复启动"
        menu_navigation
        read_choice 4
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" access ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" grafana-account ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" grafana-check ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" grafana-resources ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_tunnel_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / Cloudflare 域名入口"
        info "  入口转发到本机 Grafana；无需开放公网 3000 或占用 80/443。"
        menu_items "Cloudflare 域名入口" \
            "配置 Grafana 域名入口（自动创建 Tunnel）" \
            "查看域名入口状态" \
            "查看域名入口日志" \
            "恢复域名入口" \
            "暂停域名入口，恢复 SSH 访问" \
            "移除域名入口（保留监控数据）"
        menu_navigation
        read_choice 6
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-setup ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-status ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-logs ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-start ;;
            5) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-stop ;;
            6) run bash "$ROOT_DIR/scripts/monitor.sh" tunnel-remove ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_backup_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理 / 备份与恢复"
        info "  加密后发给 Telegram Bot；解密密钥另存 Mac。恢复仅用于全新监控机。"
        menu_items "备份与恢复（Telegram）" \
            "配置 Telegram 自动备份" \
            "立即备份并发送" \
            "查看备份状态" \
            "查看备份日志" \
            "从备份恢复（全新监控机）" \
            "暂停自动备份" \
            "恢复自动备份"
        menu_navigation
        read_choice 7
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" backup-setup ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" backup-run ;;
            3) run bash "$ROOT_DIR/scripts/monitor.sh" backup-status ;;
            4) run bash "$ROOT_DIR/scripts/monitor.sh" backup-logs ;;
            5) run bash "$ROOT_DIR/scripts/monitor.sh" backup-restore ;;
            6) run bash "$ROOT_DIR/scripts/monitor.sh" backup-pause ;;
            7) run bash "$ROOT_DIR/scripts/monitor.sh" backup-resume ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

monitor_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 监控管理"
        info "  首次：1 安装监控机 → 2 添加节点 → 3 查看面板；配置 HTTPS 域名进入 4。"
        menu_items "监控管理（Prometheus + Grafana）" \
            "安装监控机（首次，仅一台）" \
            "添加 / 部署被监控节点（自动流程）" \
            "Grafana 面板（访问 / 账号 / 诊断）>" \
            "Cloudflare 域名入口 >" \
            "状态与日志 >" \
            "节点与证书 >" \
            "高级手动接入 >" \
            "启动 / 停止 / 重启 / 卸载 >" \
            "备份与恢复（Telegram）>"
        menu_navigation
        read_choice 9
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/monitor.sh" hub-install ;;
            2) run bash "$ROOT_DIR/scripts/monitor.sh" deploy-node ;;
            3) monitor_access_menu; continue ;;
            4) monitor_tunnel_menu; continue ;;
            5) monitor_status_menu; continue ;;
            6) monitor_nodes_menu; continue ;;
            7) monitor_manual_menu; continue ;;
            8) monitor_services_menu; continue ;;
            9) monitor_backup_menu; continue ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

help_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 使用帮助与快捷命令"
        opening_commands
        menu_items "使用帮助与快捷命令" \
            "安装 / 更新快捷命令 vt" \
            "查看全部命令帮助" \
            "查看常见操作路径"
        menu_navigation
        read_choice 3
        case "$CHOICE" in
            1) run install_shortcut ;;
            2) show_help ;;
            3)
                cat <<'GUIDE'
  新建代理节点：主菜单 1 → 1 预览 → 2 部署。
  更新现有节点：主菜单 1 → 3 更新配置。
  监控首次部署：主菜单 7 → 1 安装 → 2 添加节点。
  配置面板域名：主菜单 7 → 4 → 1。
  查看面板地址：主菜单 7 → 3 → 1。
  修改面板账号：主菜单 7 → 3 → 2。
  排查面板 502：主菜单 7 → 3 → 3；需要调整资源时选择 4。
  所有菜单：0 返回上级，m 回主菜单，q 退出。
GUIDE
                ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

system_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 系统设置 / IPv6"
        menu_items "系统设置" \
            "查看 IPv6 状态" \
            "开启 IPv6" \
            "关闭 IPv6"
        menu_navigation
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

sni_services_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / ISP HTTPS 静态站点 / 服务管理"
        info "  停止站点会影响使用此域名的 Reality 目标；卸载保留页面和证书。"
        menu_items "静态站点服务" "启动（开机自启）" "停止（取消自启）" "重启" "卸载服务（保留配置）"
        menu_navigation
        read_choice 4
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/sni-site.sh" start ;;
            2) run bash "$ROOT_DIR/scripts/sni-site.sh" stop ;;
            3) run bash "$ROOT_DIR/scripts/sni-site.sh" restart ;;
            4) run bash "$ROOT_DIR/scripts/sni-site.sh" uninstall ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

sni_site_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / ISP HTTPS 静态站点"
        info "  在 ISP 机器部署；其他 VPS 使用它的域名作为 Reality target/SNI。"
        info "  仅 TCP 443，证书自动续签；域名需灰云直连本机。"
        menu_items "ISP HTTPS 静态站点（Reality SNI）" \
            "部署 / 恢复 HTTPS 静态站点" \
            "查看地址与 Reality 填写方法" \
            "检查服务 / 证书 / TLS 1.3 / HTTP2" \
            "替换首页 HTML（无需重启）" \
            "查看站点日志" \
            "服务管理（启动 / 停止 / 卸载）>"
        menu_navigation
        read_choice 6
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/sni-site.sh" install ;;
            2) run bash "$ROOT_DIR/scripts/sni-site.sh" access ;;
            3) run bash "$ROOT_DIR/scripts/sni-site.sh" check ;;
            4) run bash "$ROOT_DIR/scripts/sni-site.sh" page ;;
            5) run bash "$ROOT_DIR/scripts/sni-site.sh" logs ;;
            6) sni_services_menu; continue ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

main_menu() {
    while true; do
        MENU_HOME=0
        menu_header
        menu_items "主菜单" \
            "节点部署 >" \
            "3x-ui 面板 >" \
            "Hysteria2 端口跳跃 >" \
            "系统设置（IPv6）>" \
            "VPS 多功能工具箱" \
            "使用帮助 / 快捷命令 >" \
            "监控管理（Prometheus + Grafana）>" \
            "ISP HTTPS 静态站点（Reality SNI）>"
        menu_navigation 退出
        read_choice 8
        case "$CHOICE" in
            1) deploy_menu ;;
            2) xui_menu ;;
            3) hy2_menu ;;
            4) system_menu ;;
            5) run bash "$ROOT_DIR/scripts/vps-tools.sh"; pause ;;
            6) help_menu ;;
            7) monitor_menu ;;
            8) sni_site_menu ;;
            0) if [ "$MENU_HOME" -eq 1 ]; then continue; fi; exit 0 ;;
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
            if [ "$MENU_HOME" -eq 1 ]; then main_menu; fi
        else
            bash "$ROOT_DIR/scripts/monitor.sh" "$@"
        fi
        ;;
    sni-site)
        shift
        if [ "$#" -eq 0 ]; then
            sni_site_menu
            if [ "$MENU_HOME" -eq 1 ]; then main_menu; fi
        else
            bash "$ROOT_DIR/scripts/sni-site.sh" "$@"
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
