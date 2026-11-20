#!/usr/bin/env bash
# 1Panel 可视化管理面板：调用官方安装器并提供安全的状态入口。
# 用法：bash scripts/1panel.sh [install|status|access|restart|uninstall|docker-proxy]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

INSTALL_URL="https://resource.fit2cloud.com/1panel/package/quick_start.sh"
PANEL_COMMAND="1pctl"

panel_ctl() {
    if command -v "$PANEL_COMMAND" >/dev/null 2>&1; then
        command -v "$PANEL_COMMAND"
    elif [ -x /usr/local/bin/1pctl ]; then
        printf '%s\n' /usr/local/bin/1pctl
    elif [ -x /usr/bin/1pctl ]; then
        printf '%s\n' /usr/bin/1pctl
    else
        return 1
    fi
}

panel_installed() {
    panel_ctl >/dev/null 2>&1
}

require_panel() {
    panel_installed || die "1Panel 未安装，请先运行：vt 1panel install"
}

install_panel() {
    require_root
    if panel_installed; then
        info "检测到 1Panel 已安装。"
        show_status
        return 0
    fi

    info "即将运行 1Panel 官方安装脚本："
    info "$INSTALL_URL"
    warn "安装器会安装 1Panel 及其依赖，通常包括 Docker 和 OpenResty。"
    warn "如果本机 TCP 443 已由 Reality 或其他服务占用，1Panel 的 HTTPS 反代入口需要改用空闲端口、第二个公网 IP，或先调整现有监听。"
    confirm "确认继续？" || {
        info "已取消。"
        return 0
    }
    run_remote_script "$INSTALL_URL"
}

show_status() {
    if ! panel_installed; then
        info "1Panel 未安装。"
        return 0
    fi
    "$(panel_ctl)" version 2>/dev/null || "$(panel_ctl)" --version 2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1; then
        systemctl status 1panel --no-pager 2>/dev/null || true
    fi
}

show_access() {
    require_panel
    info "请运行以下命令查看 1Panel 面板地址、端口和初始账号信息："
    info "1pctl user-info"
    "$(panel_ctl)" user-info
}

restart_panel() {
    require_root
    require_panel
    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart 1panel
    else
        "$(panel_ctl)" restart
    fi
    info "1Panel 已重启。"
}

uninstall_panel() {
    require_root
    require_panel
    warn "卸载 1Panel 会移除面板服务及其管理组件，Docker 项目和数据是否保留取决于官方卸载器提示。"
    confirm "确认卸载 1Panel？" || {
        info "已取消。"
        return 0
    }
    "$(panel_ctl)" uninstall
}

case "${1:-status}" in
    install) install_panel ;;
    status) show_status ;;
    access) show_access ;;
    restart) restart_panel ;;
    uninstall) uninstall_panel ;;
    docker-proxy)
        require_root
        require_command python3
        exec python3 "$ROOT_DIR/lib/onepanel/docker_proxy.py"
        ;;
    help|-h|--help)
        cat <<'HELP'
用法：vt 1panel [操作]
  install    运行 1Panel 官方安装器
  status     查看安装和服务状态
  access     查看面板地址、端口和账号信息
  restart    重启 1Panel 服务
  uninstall  确认后运行官方卸载器
  docker-proxy  按容器或网段放行宿主机代理（UFW）
HELP
        ;;
    *) die "未知操作：$1（可用：install、status、access、restart、uninstall、docker-proxy）" ;;
esac
