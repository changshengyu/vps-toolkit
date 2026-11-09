#!/usr/bin/env bash
# 为已安装的 3x-ui 开启 / 关闭 geodata 自动更新（Xray-core 的 geodata 功能），不改动入站和路由规则
# 用法：bash scripts/xui-geodata.sh [cron 表达式|off]

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
GEODATA_CRON="${1:-0 20 * * *}"

case "$GEODATA_CRON" in
    -h | --help)
        cat <<HELP
用法：bash vps.sh geodata [cron 表达式|off]

为 3x-ui 开启 geodata 自动更新：Xray 按 cron 定时下载 geoip.dat 和 geosite.dat
（Loyalsoldier/v2ray-rules-dat）并热加载，无需重启。入站和路由规则保持不变。

  默认 "0 20 * * *"：每天 UTC 20:00（北京时间 04:00）
  off：关闭自动更新

cron 按服务器时区解析，VPS 通常为 UTC。修改前会备份当前 Xray 模板到 $DEPLOY_HOME/backup/。
HELP
        exit 0
        ;;
    off) ;;
    *) is_cron "$GEODATA_CRON" || die "cron 格式不正确：$GEODATA_CRON（示例：\"0 20 * * *\"）" ;;
esac

main() {
    local current old new merged backup patch

    require_root
    ensure_packages jq openssl curl
    panel_installed || die "未安装 3x-ui。"

    detect_existing_panel
    HY2_DOMAIN="$(panel_cert_domain)"
    init_api
    wait_panel_ready

    # 与一键部署使用同一份模板，保证配置一致
    patch="$(jq -n --arg ai n --arg ss_addr "" --arg ss_port 0 --arg ss_method "" --arg ss_pass "" \
        --arg geodata_cron "$GEODATA_CRON" -f "$ROOT_DIR/templates/xray-routing.jq")"

    current="$(get_xray_template)"
    old="$(jq -c '.geodata // null' <<<"$current")"
    new="$(jq -c '.geodata' <<<"$patch")"
    if [ "$old" = "$new" ]; then
        info "geodata 自动更新已是目标配置，无需修改。"
        return 0
    fi

    backup="$(backup_xray_template)"
    info "已备份当前 Xray 模板：$backup"

    merged="$(jq -c --argjson g "$new" 'if $g then .geodata = $g else del(.geodata) end' <<<"$current")"
    save_xray_template "$merged"
    restart_xray

    if [ "$GEODATA_CRON" = "off" ]; then
        info "已关闭 geodata 自动更新。"
    else
        info "已开启 geodata 自动更新：cron \"$GEODATA_CRON\"（服务器时区：$(date +%Z)）。"
    fi
}

main "$@"
