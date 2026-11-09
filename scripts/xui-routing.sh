#!/usr/bin/env bash
# 修改已安装 3x-ui 的 Xray 路由域策略（domainStrategy），不改动入站和路由规则
# 用法：bash scripts/xui-routing.sh [IPIfNonMatch|AsIs|IPOnDemand]

set -Eeuo pipefail
trap 'rc=$?; printf "错误：%s 第 %s 行命令失败（退出码 %s）：%s\n" "${BASH_SOURCE[0]##*/}" "$LINENO" "$rc" "$BASH_COMMAND" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"
for _lib in params cert panel; do
    # shellcheck source=/dev/null
    . "$ROOT_DIR/lib/deploy/$_lib.sh"
done
unset _lib

DEPLOY_HOME="${DEPLOY_HOME:-/root/.vps-toolkit}"
STRATEGY="${1:-IPIfNonMatch}"

case "$STRATEGY" in
    AsIs | IPIfNonMatch | IPOnDemand) ;;
    -h | --help)
        cat <<HELP
用法：bash vps.sh routing [策略]

修改 3x-ui 的 Xray 路由域策略，入站和路由规则保持不变。
策略：IPIfNonMatch（默认）、AsIs、IPOnDemand
修改前会备份当前 Xray 模板到 $DEPLOY_HOME/backup/。
HELP
        exit 0
        ;;
    *) die "未知策略：$STRATEGY（可用：IPIfNonMatch、AsIs、IPOnDemand）" ;;
esac

main() {
    local current old merged backup

    require_root
    ensure_packages jq openssl curl
    panel_installed || die "未安装 3x-ui。"

    detect_existing_panel
    HY2_DOMAIN="$(panel_cert_domain)"
    init_api
    wait_panel_ready

    current="$(get_xray_template)"
    old="$(jq -r '.routing.domainStrategy // "AsIs"' <<<"$current")"
    if [ "$old" = "$STRATEGY" ]; then
        info "路由域策略已是 $STRATEGY，无需修改。"
        return 0
    fi

    backup="$(backup_xray_template)"
    info "已备份当前 Xray 模板：$backup"

    merged="$(jq -c --arg s "$STRATEGY" '.routing = ((.routing // {}) + {domainStrategy: $s})' <<<"$current")"
    save_xray_template "$merged"
    restart_xray

    info "路由域策略：$old -> $STRATEGY"
    info "可在面板「路由 → 路由测试」中验证，例如 baidu.com 应命中 blocked（若有 geoip:cn 屏蔽规则）。"
}

main "$@"
