#!/usr/bin/env bash
# Dedicated Komari installation; does not migrate or remove existing monitoring.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT_DIR/lib/common.sh"
case "${1:-help}" in
    help|-h|--help)
        cat <<'HELP'
用法：vt komari [操作] [server|agent|tunnel]
  setup-install            安装监控机并自动创建 Cloudflare HTTPS 域名入口
  server-install           在监控机安装 / 恢复服务端并初始化管理员
  server-update            更新本工具管理的 Komari 服务端并保留数据
  tunnel-install           配置独立 Cloudflare Tunnel 域名入口
  agent-install            在每台被监控 VPS 安装 / 更新 Agent 配置
  access|status            查看访问方法 / 本机状态和资源
  logs [角色]              查看脱敏日志
  start|stop|restart|uninstall [角色]
                           管理本模块服务；卸载保留数据
无参数 vt komari 打开菜单。默认不修改代理、旧监控或防火墙。
HELP
        exit 0
        ;;
esac
require_root
[ "$(uname -s)" = Linux ] || die "Komari 部署仅支持 Linux + systemd VPS。"
require_command systemctl
ensure_packages python3 curl
python3 "$ROOT_DIR/lib/komari/manage.py" "$@"
