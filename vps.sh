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
  monitor [操作]             旧监控兼容命令（uninstall 卸载旧服务）
  komari [操作]              部署 / 更新 Komari 服务端、Agent / 域名入口
  sni-site [操作]            ISP HTTPS 静态站点 / Reality SNI（help 查看操作）
  1panel [操作]              安装 / 管理 1Panel 可视化面板
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
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ -z "${NO_COLOR+x}" ]; then
    C_CYAN=$'\033[36m' C_DIM=$'\033[2m'
    C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[31m' C_BOLD=$'\033[1m' C_RESET=$'\033[0m'
else
    C_CYAN="" C_DIM=""
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
    [ "$MENU_HOME" -eq 0 ] || return 0
    local answer
    printf '\n'
    read -r -p "回车继续；m 回主菜单；q 退出: " answer || exit 0
    case "$answer" in m|M) MENU_HOME=1 ;; q|Q) exit 0 ;; esac
}

# 运行子脚本，失败时提示并返回菜单
run() {
    local result=0
    ( "$@" ) || result=$?
    case "$result" in
        0) ;;
        90) MENU_HOME=1 ;;
        91) exit 0 ;;
        *) warn "操作未完成（退出码 ${result}）。" ;;
    esac
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
    if [ -f /opt/vps-toolkit-komari/config/server.json ]; then
        printf 'Komari 监控机'
    elif [ -f /opt/vps-toolkit-komari/config/agent.json ] || [ -f /etc/systemd/system/komari-agent.service ]; then
        printf 'Komari Agent'
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

opening_commands() {
    printf '  在线打开（可复制）：\n'
    printf 'bash <(curl -fsSL %s)\n' "$REPO_RAW_SCRIPT"
    printf '  快捷命令：%s\n' "$(status_shortcut)"
    printf '  常用命令（安装 vt 后）：vt komari  |  vt help\n'
}

menu_header() {
    local location="${1:-主菜单}"
    # Clear the visible screen only. Keep scrollback, noninteractive output and action logs.
    if [ -t 0 ] && [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ "${VPS_TOOLKIT_CLEAR:-1}" != 0 ]; then
        printf '\033[H\033[2J'
    fi
    if [ "$location" != 主菜单 ]; then
        printf '\n%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$C_CYAN" "$C_RESET"
        printf '%s  vps-toolkit%s  ·  当前位置：%s\n' "$C_BOLD" "$C_RESET" "$location"
        printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$C_CYAN" "$C_RESET"
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
    printf '\n'
    opening_commands
    printf '  %s提示：0 返回 · m 主菜单 · q 退出；运行结果按回车后才清屏。%s\n' "$C_DIM" "$C_RESET"
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
        case "$title:$i" in
            主菜单:1) printf '  %s── 代理节点 ──%s\n' "$C_DIM" "$C_RESET" ;;
            主菜单:4) printf '\n  %s── 系统与工具 ──%s\n' "$C_DIM" "$C_RESET" ;;
            主菜单:7) printf '\n  %s── 监控与站点 ──%s\n' "$C_DIM" "$C_RESET" ;;
        esac
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
        info "  每次先进入参数编辑器：输入编号修改，0 完成后看计划；q 取消。"
        info "  修改入站或域名选 4；只改 AI 分流 / SS 出站选 3。"
        menu_items "节点部署与更新" \
            "修改参数并预览（不改动系统）" \
            "编辑参数并部署节点" \
            "修改出站 / AI 分流参数（不改入站）" \
            "编辑参数并重部署（覆盖同端口入站）" \
            "查看部署结果（面板地址、分享链接）"
        menu_navigation
        read_choice 5
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/deploy.sh" --dry-run --edit ;;
            2) run bash "$ROOT_DIR/scripts/deploy.sh" --edit ;;
            3) run bash "$ROOT_DIR/scripts/deploy.sh" --config-only --edit ;;
            4) run bash "$ROOT_DIR/scripts/deploy.sh" --replace --edit ;;
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
  Komari：监控机选 7 → 1，被监控机选 7 → 2；更新服务端选 7 → 3。
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
            "服务管理（启动 / 停止 / 卸载）>" \
            "部署 Caddy 静态 SNI 后端（供 1Panel 反代）"
        menu_navigation
        read_choice 7
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/sni-site.sh" install ;;
            2) run bash "$ROOT_DIR/scripts/sni-site.sh" access ;;
            3) run bash "$ROOT_DIR/scripts/sni-site.sh" check ;;
            4) run bash "$ROOT_DIR/scripts/sni-site.sh" page ;;
            5) run bash "$ROOT_DIR/scripts/sni-site.sh" logs ;;
            6) sni_services_menu; continue ;;
            7) run bash "$ROOT_DIR/scripts/sni-site.sh" install --behind-1panel --port 18080 ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

komari_role_menu() {
    local role="$1" title="$2"
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / Komari 监控 / $title"
        case "$role" in
            server) info "  停止服务端会中断所有节点上报；卸载保留数据库和配置。" ;;
            agent) info "  仅管理本台采集器；后台服务器记录在 Komari 网页中删除。" ;;
            tunnel) info "  停止连接器会中断域名访问与 Agent 上报；页面仍可通过 SSH 访问。" ;;
        esac
        menu_items "$title" "启动（开机自启）" "停止（取消自启）" "重启" "查看日志（隐藏 Token）" "卸载服务（保留数据）"
        menu_navigation
        read_choice 5
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/komari.sh" start "$role" ;;
            2) run bash "$ROOT_DIR/scripts/komari.sh" stop "$role" ;;
            3) run bash "$ROOT_DIR/scripts/komari.sh" restart "$role" ;;
            4) run bash "$ROOT_DIR/scripts/komari.sh" logs "$role" ;;
            5) run bash "$ROOT_DIR/scripts/komari.sh" uninstall "$role" ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

komari_services_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / Komari 监控 / 服务与日志"
        menu_items "选择本机角色" "服务端 >" "旧版工具箱 Agent >" "Cloudflare 连接器 >"
        menu_navigation
        read_choice 3
        case "$CHOICE" in
            1) komari_role_menu server 服务端 ;;
            2) komari_role_menu agent Agent采集器 ;;
            3) komari_role_menu tunnel Cloudflare连接器 ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE" ;;
        esac
    done
}

onepanel_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / 1Panel 可视化面板"
        info "  1Panel 负责 Docker 项目、域名反代和证书的可视化管理。"
        info "  Reality 或 Caddy 静态 SNI 站点若占用 443，需要先调整端口、IP 或入口。"
        menu_items "1Panel 可视化面板" \
            "安装 1Panel" \
            "查看 1Panel 状态" \
            "查看面板地址与账号信息" \
            "重启 1Panel" \
            "卸载 1Panel（保留与否由官方卸载器提示）" \
            "放行 Docker 容器访问宿主机代理（UFW）"
        menu_navigation
        read_choice 6
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/1panel.sh" install ;;
            2) run bash "$ROOT_DIR/scripts/1panel.sh" status ;;
            3) run bash "$ROOT_DIR/scripts/1panel.sh" access ;;
            4) run bash "$ROOT_DIR/scripts/1panel.sh" restart ;;
            5) run bash "$ROOT_DIR/scripts/1panel.sh" uninstall ;;
            6) run bash "$ROOT_DIR/scripts/1panel.sh" docker-proxy ;;
            0) return 0 ;;
            *) warn "无效选项：$CHOICE"; continue ;;
        esac
        pause
    done
}

komari_menu() {
    while [ "$MENU_HOME" -eq 0 ]; do
        menu_header "主菜单 / Komari 监控"
        info "  监控机：1 安装并配置 HTTPS；每台 VPS：2 安装 Agent。"
        info "  原生 systemd，重启自启；不卸载旧监控，不修改代理与防火墙。"
        menu_items "Komari 监控" \
            "安装 / 恢复监控机（管理员 + 自动 HTTPS 域名）" \
            "安装 / 更新 Agent（粘贴官方一键命令）" \
            "更新 Komari 服务端" \
            "查看访问地址与接入指南" \
            "查看本机状态与资源占用（官方 / 工具箱）" \
            "服务与日志（启动 / 停止 / 卸载）>"
        menu_navigation
        read_choice 6
        case "$CHOICE" in
            1) run bash "$ROOT_DIR/scripts/komari.sh" setup-install ;;
            2) run bash "$ROOT_DIR/scripts/komari.sh" agent-install ;;
            3) run bash "$ROOT_DIR/scripts/komari.sh" server-update ;;
            4) run bash "$ROOT_DIR/scripts/komari.sh" access ;;
            5) run bash "$ROOT_DIR/scripts/komari.sh" status ;;
            6) komari_services_menu; continue ;;
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
            "Komari 监控（服务端 / Agent / 域名）>" \
            "ISP HTTPS 静态站点（Reality SNI）>" \
            "1Panel 可视化面板 >"
        menu_navigation 退出
        read_choice 9
        case "$CHOICE" in
            1) deploy_menu ;;
            2) xui_menu ;;
            3) hy2_menu ;;
            4) system_menu ;;
            5) run bash "$ROOT_DIR/scripts/vps-tools.sh"; pause ;;
            6) help_menu ;;
            7) komari_menu ;;
            8) sni_site_menu ;;
            9) onepanel_menu ;;
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
            info "旧监控已退出菜单。卸载旧服务：vt monitor uninstall；当前监控：vt komari。"
        else
            bash "$ROOT_DIR/scripts/monitor.sh" "$@"
        fi
        ;;
    1panel)
        shift
        if [ "$#" -eq 0 ]; then
            onepanel_menu
            if [ "$MENU_HOME" -eq 1 ]; then main_menu; fi
        else
            bash "$ROOT_DIR/scripts/1panel.sh" "$@"
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
    komari)
        shift
        if [ "$#" -eq 0 ]; then
            komari_menu
            if [ "$MENU_HOME" -eq 1 ]; then main_menu; fi
        else
            bash "$ROOT_DIR/scripts/komari.sh" "$@"
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
