#!/usr/bin/env bash
# 3x-ui 面板：调用官方安装脚本
# 用法：bash scripts/3x-ui.sh [install|status]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

INSTALL_URL="https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"

install_panel() {
    require_root

    info "即将运行 3x-ui 官方安装脚本的最新版本："
    info "$INSTALL_URL"

    if command -v x-ui >/dev/null 2>&1; then
        warn "检测到已安装 x-ui。如只需管理面板，请直接运行 x-ui。"
    fi

    confirm "确认继续？" || {
        info "已取消。"
        return 0
    }

    run_remote_script "$INSTALL_URL"
}

show_status() {
    if ! command -v x-ui >/dev/null 2>&1; then
        info "3x-ui 未安装。"
        return 0
    fi

    systemctl status x-ui --no-pager || true
    info "运行 x-ui 可打开管理菜单。"
}

case "${1:-install}" in
    install) install_panel ;;
    status) show_status ;;
    *) die "未知操作：$1（可用：install、status）" ;;
esac
