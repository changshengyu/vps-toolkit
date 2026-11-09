#!/usr/bin/env bash
# 个人 VPS 工具集入口
# 用法：bash vps.sh [deploy|hy2|3x-ui|tools|help] [操作]

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
  tools [上游参数]           运行 VPS 多功能工具箱
  help                       显示帮助
HELP
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

hy2_menu() {
    local choice

    cat <<MENU

Hysteria2 端口跳跃
1. 安装或更新规则
2. 查看状态
3. 卸载
0. 返回
MENU
    read -r -p "请选择: " choice

    case "$choice" in
        1) hy2_install_prompt ;;
        2) bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" status ;;
        3)
            if confirm "确认卸载端口跳跃规则？"; then
                bash "$ROOT_DIR/scripts/hy2-port-hopping.sh" uninstall
            fi
            ;;
        0) ;;
        *) warn "无效选择。" ;;
    esac
}

xui_menu() {
    local choice

    cat <<MENU

3x-ui 面板
1. 安装
2. 查看状态
0. 返回
MENU
    read -r -p "请选择: " choice

    case "$choice" in
        1) bash "$ROOT_DIR/scripts/3x-ui.sh" install ;;
        2) bash "$ROOT_DIR/scripts/3x-ui.sh" status ;;
        0) ;;
        *) warn "无效选择。" ;;
    esac
}

deploy_menu() {
    local choice

    cat <<MENU

一键部署 3x-ui 节点
1. 预览配置（不改动系统）
2. 正式部署
0. 返回
MENU
    read -r -p "请选择: " choice

    case "$choice" in
        1) bash "$ROOT_DIR/scripts/deploy.sh" --dry-run ;;
        2) bash "$ROOT_DIR/scripts/deploy.sh" ;;
        0) ;;
        *) warn "无效选择。" ;;
    esac
}

main_menu() {
    local choice

    while true; do
        cat <<MENU

VPS 工具集
1. 一键部署 3x-ui 节点
2. Hysteria2 端口跳跃
3. 3x-ui 面板
4. VPS 多功能工具箱
0. 退出
MENU
        read -r -p "请选择: " choice || exit 0

        # 子脚本失败时返回菜单，不退出整个工具
        case "$choice" in
            1) deploy_menu || warn "操作失败。" ;;
            2) hy2_menu || warn "操作失败。" ;;
            3) xui_menu || warn "操作失败。" ;;
            4) bash "$ROOT_DIR/scripts/vps-tools.sh" || warn "工具箱异常退出。" ;;
            0) exit 0 ;;
            *) warn "无效选择。" ;;
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
