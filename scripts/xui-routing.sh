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

# 从面板证书中取一个域名，用于本机 HTTPS 访问面板
panel_cert_domain() {
    local cert domain
    cert="$("$XUI_BIN" setting -getCert true 2>/dev/null | awk -F': ' '/^cert:/ {print $2}' | tr -d '[:space:]' || true)"
    [ -n "$cert" ] && [ -r "$cert" ] || return 0
    domain="$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null |
        grep -o 'DNS:[^,[:space:]]*' | head -1 | cut -c5- || true)"
    # 通配符证书用任意子域名访问即可
    printf '%s' "${domain/#\*./vps-toolkit.}"
}

main() {
    local current old merged file backup

    require_root
    ensure_packages jq openssl curl
    panel_installed || die "未安装 3x-ui。"

    detect_existing_panel
    HY2_DOMAIN="$(panel_cert_domain)"
    init_api
    wait_panel_ready

    current="$(api POST /panel/api/xray/ | jq -c '.obj | if type == "string" then fromjson else . end
        | .xraySetting | if type == "string" then fromjson else . end')"
    old="$(jq -r '.routing.domainStrategy // "AsIs"' <<<"$current")"
    if [ "$old" = "$STRATEGY" ]; then
        info "路由域策略已是 $STRATEGY，无需修改。"
        return 0
    fi

    backup="$(backup_xray_template)"
    info "已备份当前 Xray 模板：$backup"

    merged="$(jq -c --arg s "$STRATEGY" '.routing = ((.routing // {}) + {domainStrategy: $s})' <<<"$current")"
    file="$(mktemp)"
    chmod 600 "$file"
    printf '%s' "$merged" >"$file"
    api POST /panel/api/xray/update --data-urlencode "xraySetting@$file" >/dev/null
    rm -f "$file"
    restart_xray

    info "路由域策略：$old -> $STRATEGY"
    info "可在面板「路由 → 路由测试」中验证，例如 baidu.com 应命中 blocked（若有 geoip:cn 屏蔽规则）。"
}

main "$@"
