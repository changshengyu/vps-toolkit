#!/usr/bin/env bash
# VPS 多功能工具箱：运行 everett7623/vps_scripts 的最新版本
# 用法：bash scripts/vps-tools.sh [上游参数]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

TOOLS_URL="https://raw.githubusercontent.com/everett7623/vps_scripts/main/vps.sh"

require_root

info "即将运行第三方工具箱的最新版本："
info "$TOOLS_URL"
warn "工具箱的部分功能会修改系统配置，并可能调用其他第三方脚本。"

confirm "确认继续？" || {
    info "已取消。"
    exit 0
}

run_remote_script "$TOOLS_URL" "$@"
