#!/usr/bin/env bash
# One-off SSH provisioning; metrics always use HTTPS, never an SSH tunnel.
DEPLOY_DIR="$MONITOR_DIR/deploy"
RENEW_SERVICE=vps-toolkit-monitor-renew

monitor_lock() (
    linux_root
    require_hub
    require_command flock
    exec 9>"$MONITOR_DIR/.deploy.lock"
    flock -n 9 || die "已有监控部署或续签任务在运行，请稍后重试。"
    "$@"
)

build_toolkit_archive() {
    local archive="$1"
    COPYFILE_DISABLE=1 tar -czf "$archive" -C "$ROOT_DIR" \
        lib/common.sh lib/monitor/package.py lib/monitor/tls.sh \
        lib/monitor/deploy.sh lib/monitor/renew.sh lib/monitor/tunnel.sh lib/monitor/cloudflare.py lib/monitor/grafana.sh lib/monitor/grafana.py lib/monitor/backup.sh lib/monitor/backup.py lib/monitor/restricted-renew.sh lib/monitor/remote-install.sh scripts/monitor.sh
}

remote_install_command() {
    # POSIX shell quoting also works when the remote login shell is /bin/sh.
    python3 - "$ROOT_DIR/lib/monitor/remote-install.sh" "$1" <<'PY'
from pathlib import Path
import shlex, sys
command = ["bash", "-c", Path(sys.argv[1]).read_text()]
if sys.argv[2] != "root":
    command = ["sudo", "-n"] + command
print(" ".join(shlex.quote(arg) for arg in command))
PY
}

deploy_node() {
    linux_root
    require_hub
    ensure_packages openssl jq python3 curl tar
    require_command ssh
    local name display_name address source_ip port user ssh_port identity previous="{}" value detected="" metadata config temporary
    info "节点显示名称支持中文、空格、. 和 /；已有节点使用原名称即可更新。"
    read -r -p "节点名称（已有节点同名即更新）: " display_name
    name="$(node_key "$display_name")"
    if [ -f "$DEPLOY_DIR/$name.json" ]; then
        previous="$(package_meta deployment "$DEPLOY_DIR/$name.json")"
        if [ "$display_name" = "$name" ]; then display_name="$(printf '%s' "$previous" | jq -r '.node.display_name // .node.name')"; fi
    fi
    info "节点：${display_name}"
    value="$(printf '%s' "$previous" | jq -r '.node.address // empty')"
    read -r -p "节点直连域名或公网 IP${value:+ [$value]}: " address; address="${address:-$value}"
    package_meta identity "$address" >/dev/null
    detected="$(printf '%s' "$previous" | jq -r '.node.monitor_ip // empty')"
    if [ -z "$detected" ]; then
        detected="$(curl -4 -fsS --max-time 5 https://api4.ipify.org 2>/dev/null || true)"
        if [ -z "$detected" ]; then detected="$(curl -6 -fsS --max-time 5 https://api6.ipify.org 2>/dev/null || true)"; fi
    fi
    read -r -p "监控机公网源 IP${detected:+ [$detected]}: " source_ip; source_ip="${source_ip:-$detected}"
    value="$(printf '%s' "$previous" | jq -r '.node.port // 19100')"
    read -r -p "节点 HTTPS 端口 [$value]: " port; port="${port:-$value}"
    valid_port "$port" || die "端口无效。"
    metadata="$(package_meta create "$display_name" "$address" "$source_ip" "$((10#$port))")"
    package_meta resolve "$address" "$source_ip" >/dev/null || die "域名没有匹配源 IP 地址族的解析，请确认直连 A/AAAA 记录。"
    value="$(printf '%s' "$previous" | jq -r '.ssh.user // "root"')"
    read -r -p "远端 SSH 用户 [$value]: " user; user="${user:-$value}"
    value="$(printf '%s' "$previous" | jq -r '.ssh.port // 22')"
    read -r -p "远端 SSH 端口 [$value]: " ssh_port; ssh_port="${ssh_port:-$value}"
    valid_port "$ssh_port" || die "SSH 端口无效。"
    value="$(printf '%s' "$previous" | jq -r '.ssh.identity_file // empty')"
    if [ -n "$value" ] && [ ! -r "$value" ]; then
        warn "原 SSH 登录私钥在本机不可读；可填写新的私钥路径，或留空使用默认密钥/密码。专用续期密钥仍单独保留。"
        value=""
    fi
    read -r -p "监控机上的 SSH 私钥绝对路径${value:+ [$value]}（留空使用默认密钥/密码）: " identity
    identity="${identity:-$value}"
    if [ -n "$identity" ]; then [ -r "$identity" ] || die "无法读取私钥文件；请使用监控机上的路径，不是 Mac 路径。"; fi
    temporary="$(mktemp)"
    jq -n --argjson node "$metadata" --arg user "$user" --argjson port "$((10#$ssh_port))" --arg key "$identity" \
        '{schema:1,node:$node,ssh:{user:$user,port:$port,identity_file:$key},auto_renew:false}' > "$temporary"
    if ! config="$(package_meta deployment "$temporary")"; then rm -f "$temporary"; return 1; fi
    rm -f "$temporary"
    info "将在 $(printf '%s' "$metadata" | jq -r .address) 以 $user 身份部署/更新采集器，自动签发、传输证书并验证 HTTPS。"
    info "非 root 账号需要免密 sudo。首次连接核对 SSH 指纹；密码只由 SSH 提示输入，不会保存。"
    info "本次登录后会自动安装仅能续期证书的受限公钥；以后自动续签，无需再次输入密码。"
    confirm "确认开始？" || return 0
    apply_deployment "$config" interactive
}

apply_deployment() (
    # Globals within this subshell remain available to its EXIT trap on Bash 3/4/5.
    deploy_config="$1"
    deploy_mode="${2:-interactive}"
    deploy_work="$(mktemp -d /tmp/vtm-hub.XXXXXXXX)"
    deploy_connected=0
    cleanup_deployment() {
        local code=$?
        trap - EXIT
        if [ "$deploy_connected" -eq 1 ]; then
            ssh "${deploy_ssh_args[@]}" -S "$deploy_work/ssh.sock" -O exit -- "$deploy_destination" >/dev/null 2>&1 || true
        fi
        rm -rf -- "$deploy_work"
        exit "$code"
    }
    trap cleanup_deployment EXIT
    umask 077
    printf '%s\n' "$deploy_config" > "$deploy_work/config.json"
    package_meta deployment "$deploy_work/config.json" >/dev/null
    deploy_name="$(jq -r .node.name "$deploy_work/config.json")"
    deploy_label="$(jq -r '.node.display_name // .node.name' "$deploy_work/config.json")"
    deploy_address="$(jq -r .node.address "$deploy_work/config.json")"
    deploy_source="$(jq -r .node.monitor_ip "$deploy_work/config.json")"
    deploy_user="$(jq -r .ssh.user "$deploy_work/config.json")"
    deploy_ssh_port="$(jq -r .ssh.port "$deploy_work/config.json")"
    deploy_key="$(jq -r .ssh.identity_file "$deploy_work/config.json")"
    deploy_destination="$deploy_user@$deploy_address"
    if [[ "$deploy_source" == *:* ]]; then deploy_family=inet6; else deploy_family=inet; fi
    deploy_ssh_args=(-F /dev/null -p "$deploy_ssh_port" -o "AddressFamily=$deploy_family" \
        -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
    if [ -n "$deploy_key" ]; then deploy_ssh_args+=(-i "$deploy_key" -o IdentitiesOnly=yes); fi
    if [ "$deploy_mode" = batch ]; then
        deploy_ssh_args+=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentityAgent=none)
    else
        deploy_ssh_args+=(-o StrictHostKeyChecking=ask)
    fi
    info "连接 ${deploy_destination}（仅本次安装/更新使用 SSH）..."
    ssh "${deploy_ssh_args[@]}" -M -S "$deploy_work/ssh.sock" -o ControlPersist=60 -fNT -- "$deploy_destination"
    deploy_connected=1
    ensure_pki
    ensure_renewal_key "$deploy_name"
    jq --arg key "$MONITOR_DIR/renewal-keys/$deploy_name" '.renewal_identity=$key' "$deploy_work/config.json" > "$deploy_work/config.next.json"
    mv "$deploy_work/config.next.json" "$deploy_work/config.json"
    cp "$MONITOR_DIR/renewal-keys/$deploy_name.pub" "$deploy_work/renewal.pub"
    printf '%s\n' "$deploy_user" > "$deploy_work/renewal-user"
    mkdir "$deploy_work/node"
    jq .node "$deploy_work/config.json" > "$deploy_work/node/node.json"
    build_node_package "$deploy_name" "$deploy_work/node" "$deploy_work/node.tar.gz" >/dev/null
    build_toolkit_archive "$deploy_work/toolkit.tar.gz"
    COPYFILE_DISABLE=1 tar -czf "$deploy_work/payload.tar.gz" -C "$deploy_work" node.tar.gz toolkit.tar.gz renewal.pub renewal-user
    deploy_command="$(remote_install_command "$deploy_user")"
    ssh "${deploy_ssh_args[@]}" -S "$deploy_work/ssh.sock" -o ControlMaster=no -o BatchMode=yes -T \
        -- "$deploy_destination" "$deploy_command" < "$deploy_work/payload.tar.gz"
    # Keep current targets untouched until actual HTTPS verification succeeds.
    deploy_previous_pki="$PKI_DIR"
    mkdir -p "$deploy_work/verify/nodes"
    cp -R "$deploy_work/node" "$deploy_work/verify/nodes/$deploy_name"
    PKI_DIR="$deploy_work/verify"
    if ! probe_https_node "$deploy_name"; then
        die "远端已安装，但 HTTPS 验证失败；旧采集目标保留。请检查精确源 IP、UFW/firewalld 和云安全组。"
    fi
    PKI_DIR="$deploy_previous_pki"
    renewal_ssh "$deploy_work/config.json" check >/dev/null
    activate_https_job
    install -d -m 700 "$PKI_DIR/nodes" "$PKI_DIR/nodes/$deploy_name" "$MONITOR_DIR/packages" "$DEPLOY_DIR"
    cp "$deploy_work/node/"* "$PKI_DIR/nodes/$deploy_name/"
    install -m 600 "$deploy_work/node.tar.gz" "$MONITOR_DIR/packages/$deploy_name.tar.gz"
    jq '.auto_renew=true' "$deploy_work/config.json" > "$DEPLOY_DIR/$deploy_name.json.tmp"
    chmod 600 "$DEPLOY_DIR/$deploy_name.json.tmp"
    mv "$DEPLOY_DIR/$deploy_name.json.tmp" "$DEPLOY_DIR/$deploy_name.json"
    write_https_target "$deploy_name"
    install_renew_timer
    wait_node_scrape "$deploy_name" || die "节点已部署，目标和续期记录已保留，但未确认 Prometheus 采集成功；请按上面的错误排查。"
    cleanup_legacy_node "$deploy_name"
    info "已接入 ${deploy_label}，后续只用 HTTPS 采集，部署 SSH 连接即将关闭。"
    info "已验证专用受限续期密钥；证书将在到期前自动续签并同步，无需再次输入登录密码。"
    info "CPU / 网络速率图需要至少两次采集；Grafana 页面自动刷新后显示。"
)

renew_node() {
    linux_root
    require_hub
    local name label
    read -r -p "要续签并同步的节点名称: " name
    name="$(node_key "$name")"
    [ -f "$DEPLOY_DIR/$name.json" ] || die "没有该节点的自动部署记录，请先用添加/部署节点接入。"
    label="$(jq -r '.node.display_name // .node.name' "$DEPLOY_DIR/$name.json")"
    confirm "重新签发并自动同步 ${label} 的证书？" || return 0
    renew_managed_node "$DEPLOY_DIR/$name.json"
}

install_renew_timer() {
    install -d -m 755 "$MONITOR_DIR/toolkit/scripts" "$MONITOR_DIR/toolkit/lib/monitor" "$MONITOR_DIR/toolkit/templates/monitoring"
    local path
    if [ ! "$ROOT_DIR" -ef "$MONITOR_DIR/toolkit" ]; then
        for path in lib/common.sh scripts/monitor.sh lib/monitor/tls.sh lib/monitor/package.py lib/monitor/deploy.sh lib/monitor/renew.sh lib/monitor/tunnel.sh lib/monitor/cloudflare.py lib/monitor/grafana.sh lib/monitor/grafana.py lib/monitor/backup.sh lib/monitor/backup.py lib/monitor/restricted-renew.sh lib/monitor/remote-install.sh; do
            install -m 644 "$ROOT_DIR/$path" "$MONITOR_DIR/toolkit/$path"
        done
        cp -R "$ROOT_DIR/templates/monitoring/." "$MONITOR_DIR/toolkit/templates/monitoring/"
    fi
    cat > "$SYSTEMD_DIR/$RENEW_SERVICE.service" <<UNIT
[Unit]
Description=Renew and synchronize VPS monitoring certificates
After=network-online.target docker.service
[Service]
Type=oneshot
Environment=MONITOR_DIR=$MONITOR_DIR
ExecStart=/usr/bin/bash $MONITOR_DIR/toolkit/scripts/monitor.sh renew-due
UNIT
    cat > "$SYSTEMD_DIR/$RENEW_SERVICE.timer" <<UNIT
[Unit]
Description=Daily VPS monitoring certificate renewal check
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now "$RENEW_SERVICE.timer"
}

renew_due() {
    linux_root
    require_hub
    ensure_packages openssl jq python3 curl tar
    require_command ssh
    if ! compose ps --status running --services | grep -qx prometheus; then
        info "Prometheus 未运行，跳过本次自动续期。"
        return 0
    fi
    if ! openssl x509 -in "$MONITOR_DIR/prometheus/tls/client.pem" -checkend 2592000 -noout >/dev/null; then cert_renew client; fi
    local config name automatic failures=0
    for config in "$DEPLOY_DIR"/*.json; do
        [ -f "$config" ] || continue
        package_meta deployment "$config" >/dev/null
        name="$(jq -r .node.name "$config")"
        if openssl x509 -in "$PKI_DIR/nodes/$name/server.pem" -checkend 2592000 -noout >/dev/null; then continue; fi
        automatic="$(jq -r .auto_renew "$config")"
        if [ "$automatic" != true ]; then
            warn "${name} 缺少自动续期授权，请重新执行自动部署以安装受限续期公钥。"
            failures=$((failures + 1))
            continue
        fi
        # A separate Bash process preserves errexit/rollback even when its status is inspected here.
        if ! bash "$ROOT_DIR/scripts/monitor.sh" renew-saved "$config"; then
            warn "${name} 自动续签失败，请检查 SSH 和节点状态。"
            failures=$((failures + 1))
        fi
    done
    [ "$failures" -eq 0 ] || return 1
}
