#!/usr/bin/env bash
# Local admin operations. No passwords in process arguments or persistent records.
grafana_meta() { python3 "$ROOT_DIR/lib/monitor/grafana.py" "$@"; }

grafana_ready() {
    curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:3000/api/health >/dev/null 2>&1
}

grafana_wait() {
    local deadline=$((SECONDS + 240))
    info "等待 Grafana 本机接口就绪（约 4 分钟超时）…"
    while [ "$SECONDS" -lt "$deadline" ]; do
        if grafana_ready; then info "Grafana 本机接口已就绪。"; return 0; fi
        sleep 3
    done
    warn "Grafana 尚未就绪，域名可能返回 502。请运行 vt monitor grafana-check。"
    return 1
}

grafana_check() {
    require_hub
    compose ps --all grafana
    local cid
    cid="$(compose ps --all --quiet grafana)"
    if [ -n "$cid" ]; then
        docker inspect "$cid" --format '状态={{.State.Status}} 最近OOM={{.State.OOMKilled}} 重启={{.RestartCount}} 内存上限字节={{.HostConfig.Memory}}'
        docker stats --no-stream "$cid" || true
    fi
    if grafana_ready; then
        info "Grafana 本机 HTTP：就绪。"
    else
        warn "127.0.0.1:3000 未就绪；请先修复 Grafana，开放公网 3000 无法解决此问题。"
        compose logs --tail=50 grafana
        info "可运行 vt monitor grafana-resources，持久调整 Grafana 上限后验证启动。"
        return 1
    fi
}

grafana_account() (
    grafana_ready || die "Grafana 尚未就绪，请先运行 grafana-check / grafana-resources。"
    local login current_password new_login new_password repeat
    login="$(grafana_meta login "$MONITOR_DIR")"
    read -r -p "当前 Grafana 用户名 [$login]: " new_login
    login="${new_login:-$login}"
    read -r -s -p "当前密码（留空尝试安装时初始密码）: " current_password; printf '\n'
    read -r -p "新用户名（留空保留 ${login}）: " new_login
    new_login="${new_login:-$login}"
    read -r -s -p "新密码（留空保留；修改时至少 12 字符）: " new_password; printf '\n'
    if [ -n "$new_password" ]; then
        read -r -s -p "再次输入新密码: " repeat; printf '\n'
        [ "$new_password" = "$repeat" ] || die "两次新密码不一致，未修改。"
    fi
    confirm "更新此 Grafana 管理员的登录信息？" || return 0
    printf '%s\0' "$login" "$current_password" "$new_login" "$new_password" |
        grafana_meta change "$MONITOR_DIR"
)

grafana_resources() (
    local memory available current=0 cid increase
    read -r -p "Grafana 内存上限 MiB（256 / 320 / 384 / 512）[256]: " memory
    memory="${memory:-256}"
    case "$memory" in 256|320|384|512) ;; *) die "请选择 256、320、384 或 512。" ;; esac
    cid="$(compose ps --all --quiet grafana)"
    if [ -n "$cid" ]; then current="$(docker inspect "$cid" --format '{{.HostConfig.Memory}}')"; fi
    [[ "$current" =~ ^[0-9]+$ ]] || die "无法读取当前资源上限。"
    increase=$((memory - current / 1048576)); if [ "$increase" -lt 0 ]; then increase=0; fi
    available="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
    [ "${available:-0}" -ge "$((increase + 96))" ] || die "当前可用内存不足以安全提高上限，请先释放内存；查看 free -h。"
    info "仅重建 Grafana：内存 ${memory} MiB，CPU 0.5 核；数据卷、账号和历史保留。"
    confirm "应用并启动 Grafana？" || return 0
    grafana_meta resources "$MONITOR_DIR" "$memory"
    compose config --quiet
    compose up -d --no-deps grafana
    grafana_wait || { grafana_check; return 1; }
    info "Grafana 资源配置已持久保存；后续重启和域名模式切换会继续使用。"
    if [ -f "$CF_OVERLAY" ]; then tunnel_status; fi
)
