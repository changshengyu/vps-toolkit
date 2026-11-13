#!/usr/bin/env bash
# Public Grafana access; independent of metrics collection and node certificates.
CF_DIR="$MONITOR_DIR/tunnel"
CF_OVERLAY="$MONITOR_DIR/compose.tunnel.yaml"

cloudflare() { python3 "$ROOT_DIR/lib/monitor/cloudflare.py" "$@"; }

cf_read_token() {
    local token
    info "在 Cloudflare 创建自定义 API Token，限定到此账户和域名："
    info "Account / Cloudflare Tunnel / Edit；Zone / DNS / Edit；Zone / Zone / Read。"
    read -r -s -p "Cloudflare API Token（隐藏输入，仅本次操作使用）: " token
    printf '\n'
    [ -n "$token" ] || die "Token 不能为空。"
    (umask 077; printf '%s' "$token" > "$cf_api_token")
    unset token
}

cf_wait_ready() {
    local attempt
    for attempt in {1..30}; do
        if curl -fsS --max-time 2 http://127.0.0.1:20245/ready >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    warn "Tunnel 未就绪。请检查出站 TCP/UDP 7844；初次配置失败会回滚，恢复失败可再次 tunnel-start。"
    return 1
}

cf_pause() {
    if [ -f "$CF_OVERLAY" ]; then
        local running
        running="$(compose ps --status running --quiet grafana)" || return 1
        compose stop grafana-tunnel || return 1
        compose rm -f grafana-tunnel || return 1
        rm -f "$CF_OVERLAY" || return 1
        if [ -n "$running" ]; then
            compose up -d --no-deps grafana
        else
            # Removing an entry after monitor stop/uninstall must not start it.
            compose create grafana
        fi
    fi
}

tunnel_setup() (
    ensure_packages python3 curl
    [ ! -e "$CF_DIR/state.json" ] || die "已有域名入口记录，请选择状态、恢复或移除。"
    [ ! -e "$CF_OVERLAY" ] || die "已有 Tunnel Compose 配置，未覆盖。"
    curl -fsS --max-time 5 http://127.0.0.1:3000/api/health >/dev/null || die "请先启动 Grafana。"
    check_ports 20245
    local domain available
    # EXIT traps outlive function locals on errexit (including Bash 3 on macOS).
    cf_complete=0; cf_attempted=0
    read -r -p "新的 Grafana 子域名（例如 monitor.example.com）: " domain
    info "域名须由 Cloudflare 托管，使用一级子域名，不要预先创建 A/CNAME。"
    info "自动创建 Tunnel、HTTPS 路由和 CNAME；Grafana 会短暂重建容器，数据保留。"
    info "新增 cloudflared 容器内存上限 96 MiB；不监听公网端口，不修改代理。"
    available="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
    [ "${available:-0}" -ge 160 ] || die "可用内存不足 160 MiB，请先释放内存。"
    confirm "配置此域名入口？" || return 0
    install -d -m 700 "$CF_DIR"
    cf_api_token="$(mktemp)"
    chmod 600 "$cf_api_token"
    # Recovery is ownership-scoped; keep the journal if the API cannot clean up.
    trap 'rc=$?; trap - EXIT; if [ "$cf_complete" = 0 ] && [ "$cf_attempted" = 1 ]; then
        if [ -f "$CF_OVERLAY" ]; then compose logs --tail=30 grafana-tunnel >&2 || true; fi
        cf_pause || warn "本地服务未能恢复，请检查 Docker 后用 tunnel-stop 重试。"
        if [ -f "$CF_DIR/state.json" ]; then
            cloudflare remove "$CF_DIR" --token-file "$cf_api_token" || warn "残留记录已保留，请用 tunnel-remove 重试清理。"
        fi
    fi; rm -f "$cf_api_token"; exit "$rc"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    cf_read_token
    cf_attempted=1
    cloudflare create "$CF_DIR" "$domain" --token-file "$cf_api_token"
    chown 65532:65532 "$CF_DIR/connector-token"
    cloudflare overlay "$CF_DIR" "$CF_OVERLAY"
    compose config --quiet
    compose pull grafana-tunnel
    compose up -d --no-deps grafana grafana-tunnel
    cf_wait_ready
    cf_complete=1
    show_access
    info "Cloudflare 托管入口 HTTPS 证书并自动续签；无需在 VPS 安装入口证书。"
    info "首次边缘证书生效可能需要等待。公网入口仍需 Grafana 登录，可在 Cloudflare Access 额外限制访问。"
)

tunnel_start() (
    [ -f "$CF_DIR/state.json" ] || die "尚未配置域名入口。"
    cf_previously_active=0; cf_complete=0
    if [ -f "$CF_OVERLAY" ]; then cf_previously_active=1; else check_ports 20245; fi
    trap 'rc=$?; if [ "$cf_complete" = 0 ] && [ "$cf_previously_active" = 0 ]; then cf_pause || warn "恢复 SSH 配置失败，请检查 Docker 后用 tunnel-stop 重试。"; fi; exit "$rc"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    chown 65532:65532 "$CF_DIR/connector-token"
    cloudflare overlay "$CF_DIR" "$CF_OVERLAY"
    compose config --quiet
    compose up -d --no-deps grafana grafana-tunnel
    cf_wait_ready
    cf_complete=1
    show_access
)

tunnel_stop() {
    [ -f "$CF_DIR/state.json" ] || die "尚未配置域名入口。"
    cf_pause
    info "域名入口已暂停；DNS 和 Tunnel 记录保留，Grafana 已恢复 SSH HTTP 访问。"
}

tunnel_remove() (
    [ -f "$CF_DIR/state.json" ] || die "没有本工具管理的域名入口。"
    cloudflare status "$CF_DIR"
    confirm "移除本工具的 Cloudflare Tunnel 和对应 CNAME？监控数据保留。" || return 0
    cf_api_token="$(mktemp)"
    chmod 600 "$cf_api_token"
    trap 'rm -f "$cf_api_token"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    cf_read_token
    cf_pause
    # Cloudflare may take a few seconds to observe that the connector stopped.
    sleep 3
    cloudflare remove "$CF_DIR" --token-file "$cf_api_token"
    info "域名入口已移除；Grafana、Prometheus、密码和历史数据保留。"
    show_access
)

tunnel_status() {
    require_hub
    if [ ! -f "$CF_DIR/state.json" ]; then info "尚未配置 Cloudflare Tunnel。"; return 0; fi
    cloudflare status "$CF_DIR"
    if [ -f "$CF_OVERLAY" ]; then
        compose ps grafana-tunnel
        curl -fsS --max-time 3 http://127.0.0.1:20245/ready || warn "Tunnel 尚未连接。"
    else
        info "状态：已暂停或创建未完成，可恢复或移除。"
    fi
}

tunnel_logs() {
    require_hub
    [ -f "$CF_OVERLAY" ] || die "域名入口未启用。"
    compose logs --tail=100 grafana-tunnel
}
