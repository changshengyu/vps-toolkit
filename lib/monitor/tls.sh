#!/usr/bin/env bash
# Loaded by scripts/monitor.sh. No action runs merely by sourcing this file.

PKI_DIR="${MONITOR_PKI_DIR:-$MONITOR_DIR/pki}"
NODE_CONFIG_DIR="${MONITOR_NODE_CONFIG_DIR:-/etc/vps-toolkit-monitor-node}"
FIREWALL_SERVICE=vps-toolkit-monitor-firewall
NFT_TABLE=vps_toolkit_monitor
SYSTEMD_DIR="${MONITOR_SYSTEMD_DIR:-/etc/systemd/system}"

package_meta() { python3 "$ROOT_DIR/lib/monitor/package.py" "$@"; }

sign_leaf() (
    local name="$1" purpose="$2" san="$3" output="$4"
    tls_work="$(mktemp -d)"
    trap 'rm -rf -- "$tls_work"' EXIT
    umask 077
    openssl ecparam -name prime256v1 -genkey -noout -out "$tls_work/key.pem"
    openssl req -new -key "$tls_work/key.pem" -subj "/CN=$name" -out "$tls_work/request.pem"
    cat > "$tls_work/extensions" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=$purpose
subjectAltName=$san
EXT
    openssl x509 -req -in "$tls_work/request.pem" -CA "$PKI_DIR/ca.pem" \
        -CAkey "$PKI_DIR/ca-key.pem" -set_serial "0x$(openssl rand -hex 16)" \
        -days 365 -sha256 -extfile "$tls_work/extensions" -out "$tls_work/cert.pem"
    install -m 600 "$tls_work/key.pem" "$output-key.pem"
    install -m 644 "$tls_work/cert.pem" "$output.pem"
)

ensure_pki() {
    require_command openssl
    install -d -m 700 "$PKI_DIR"
    if [ ! -f "$PKI_DIR/ca.pem" ] && [ ! -f "$PKI_DIR/ca-key.pem" ]; then
        (umask 077
            openssl ecparam -name prime256v1 -genkey -noout -out "$PKI_DIR/ca-key.pem"
            openssl req -new -x509 -key "$PKI_DIR/ca-key.pem" -days 3650 -sha256 \
                -subj /CN=VPS-Toolkit-Monitoring-CA \
                -addext basicConstraints=critical,CA:TRUE,pathlen:0 \
                -addext keyUsage=critical,keyCertSign,cRLSign -out "$PKI_DIR/ca.pem"
        )
    fi
    [ -f "$PKI_DIR/ca.pem" ] && [ -f "$PKI_DIR/ca-key.pem" ] || die "CA 文件不完整，请恢复备份；不会自动更换 CA。"
    openssl x509 -checkend 86400 -noout -in "$PKI_DIR/ca.pem" >/dev/null || die "CA 已过期或即将到期，请重新规划证书迁移。"
    install -d -m 755 "$MONITOR_DIR/prometheus/tls"
    install -m 644 "$PKI_DIR/ca.pem" "$MONITOR_DIR/prometheus/tls/ca.pem"
    if [ ! -f "$MONITOR_DIR/prometheus/tls/client.pem" ] && [ ! -f "$MONITOR_DIR/prometheus/tls/client-key.pem" ]; then
        sign_leaf monitor clientAuth URI:spiffe://vps-toolkit/monitor "$MONITOR_DIR/prometheus/tls/client"
    fi
    [ -f "$MONITOR_DIR/prometheus/tls/client.pem" ] && [ -f "$MONITOR_DIR/prometheus/tls/client-key.pem" ] || die "客户端证书不完整，请恢复完整备份后重试。"
    # The official Prometheus container runs as UID/GID 65534; CA private key is never mounted.
    chown 65534:65534 "$MONITOR_DIR/prometheus/tls/client-key.pem"
    chmod 600 "$MONITOR_DIR/prometheus/tls/client-key.pem"
}

enable_https_job() {
    local config="$MONITOR_DIR/prometheus/prometheus.yml"
    install -d -m 755 "$MONITOR_DIR/prometheus/https" "$MONITOR_DIR/prometheus/https-targets"
    if grep -q '^scrape_config_files:' "$config" && ! grep -qF '/etc/prometheus/https/*.yml' "$config"; then
        die "已有自定义 scrape_config_files；请在其中添加 /etc/prometheus/https/*.yml 后重试，未覆盖配置。"
    fi
    if ! grep -qF '/etc/prometheus/https/*.yml' "$config"; then
        printf '\nscrape_config_files:\n  - /etc/prometheus/https/*.yml\n' >> "$config"
    fi
    if [ ! -f "$MONITOR_DIR/prometheus/https/remote.yml" ]; then
        cp "$ROOT_DIR/templates/monitoring/prometheus/https.yml.template" "$MONITOR_DIR/prometheus/https/remote.yml"
    fi
    chmod 644 "$MONITOR_DIR/prometheus/https/remote.yml"
    # Preserve the old dashboard and any saved edits; provision a separate HTTPS overview on migration.
    local dashboard="$MONITOR_DIR/grafana/provisioning/dashboards/overview.json"
    if [ -f "$dashboard" ] && ! grep -q 'node-https' "$dashboard" && [ ! -f "${dashboard%/*}/overview-https.json" ]; then
        jq '.uid="vps-overview-https" | .title="VPS overview (HTTPS)"' \
            "$ROOT_DIR/templates/monitoring/grafana/provisioning/dashboards/overview.json" > "${dashboard%/*}/overview-https.json"
        chmod 644 "${dashboard%/*}/overview-https.json"
    fi
}

prepare_node() {
    linux_root
    require_hub
    ensure_packages openssl jq python3
    local name="${1:-}" address source_ip port metadata
    if [ -z "$name" ]; then read -r -p "节点名称: " name; fi
    valid_name "$name" || die "节点名称无效。"
    [ "$name" != client ] || die "client 是客户端续签保留名称，请换一个节点名称。"
    read -r -p "节点公网 IP（IPv4 或 IPv6，不含端口）: " address
    read -r -p "监控机访问该节点时的公网源 IP（同一地址族）: " source_ip
    read -r -p "节点 HTTPS 采集端口 [19100]: " port; port="${port:-19100}"
    valid_port "$port" || die "端口无效。"
    metadata="$(package_meta create "$name" "$address" "$source_ip" "$((10#$port))")"
    if [ -e "$PKI_DIR/nodes/$name" ]; then
        confirm "该名称已有证书包；更新地址/源 IP 并重签证书？旧采集目标暂时保留。" || return 0
    fi
    ensure_pki
    install -d -m 700 "$PKI_DIR/nodes" "$PKI_DIR/nodes/$name" "$MONITOR_DIR/packages"
    printf '%s\n' "$metadata" > "$PKI_DIR/nodes/$name/node.json"
    build_node_package "$name"
}

build_node_package() (
    local name="$1"
    valid_name "$name" || die "节点名称无效。"
    local directory="$PKI_DIR/nodes/$name" address
    package_meta validate "$directory/node.json" >/dev/null
    address="$(jq -r .address "$directory/node.json")"
    sign_leaf "$name" serverAuth "IP:$address" "$directory/server"
    install -m 644 "$PKI_DIR/ca.pem" "$directory/ca.pem"
    (umask 077; COPYFILE_DISABLE=1 tar -czf "$MONITOR_DIR/packages/$name.tar.gz.tmp" -C "$directory" node.json ca.pem server.pem server-key.pem)
    chmod 600 "$MONITOR_DIR/packages/$name.tar.gz.tmp"
    mv "$MONITOR_DIR/packages/$name.tar.gz.tmp" "$MONITOR_DIR/packages/$name.tar.gz"
    info "已生成 $MONITOR_DIR/packages/$name.tar.gz（含该节点私钥，请通过现有 SSH/SCP 安全传输）。"
    info "远端执行：vt monitor node-install --bundle /安全路径/$name.tar.gz"
    openssl x509 -in "$PKI_DIR/ca.pem" -noout -fingerprint -sha256
    info "证书包不含 CA 私钥和监控机客户端私钥；节点安装时请核对上面的 CA 指纹。"
)

write_https_target() {
    local name="$1" metadata="$PKI_DIR/nodes/$1/node.json" address port target
    valid_name "$name" || die "节点名称无效。"
    package_meta validate "$metadata" >/dev/null
    address="$(jq -r .address "$metadata")"; port="$(jq -r .port "$metadata")"
    if [[ "$address" == *:* ]]; then target="[$address]:$port"; else target="$address:$port"; fi
    jq -n --arg name "$name" --arg target "$target" \
        '[{targets:[$target],labels:{node:$name}}]' > "$MONITOR_DIR/prometheus/https-targets/$name.json.tmp"
    chmod 644 "$MONITOR_DIR/prometheus/https-targets/$name.json.tmp"
    mv "$MONITOR_DIR/prometheus/https-targets/$name.json.tmp" "$MONITOR_DIR/prometheus/https-targets/$name.json"
}

probe_https_node() {
    local name="$1" address port family target
    address="$(jq -r .address "$PKI_DIR/nodes/$name/node.json")"
    port="$(jq -r .port "$PKI_DIR/nodes/$name/node.json")"
    if [[ "$address" == *:* ]]; then family=-6; target="[$address]"; else family=-4; target="$address"; fi
    # --interface is not used: the configured public source may be NATed by the provider.
    curl "$family" --noproxy '*' --connect-timeout 5 --max-time 15 -fsS \
        --cacert "$MONITOR_DIR/prometheus/tls/ca.pem" \
        --cert "$MONITOR_DIR/prometheus/tls/client.pem" \
        --key "$MONITOR_DIR/prometheus/tls/client-key.pem" \
        "https://$target:$port/metrics" | grep '^node_exporter_build_info' >/dev/null
}

cleanup_legacy_node() {
    local name="$1"
    if [ -f "$SSH_DIR/.vps-toolkit-managed" ] && [ -f "$SSH_DIR/config.d/$name" ]; then
        systemctl disable --now "${TUNNEL_UNIT}$name" || return 1
        rm -f "$SSH_DIR/config.d/$name"
    fi
    rm -f "$MONITOR_DIR/prometheus/targets/$name.json"
}

activate_https_job() (
    hub_tx_work="$(mktemp -d)"
    hub_tx_had_job=0
    cp "$MONITOR_DIR/prometheus/prometheus.yml" "$hub_tx_work/main.yml"
    if [ -f "$MONITOR_DIR/prometheus/https/remote.yml" ]; then
        hub_tx_had_job=1
        cp "$MONITOR_DIR/prometheus/https/remote.yml" "$hub_tx_work/remote.yml"
    fi
    rollback_hub() {
        local code=$?
        trap - EXIT
        if [ "$code" -ne 0 ]; then
            cp "$hub_tx_work/main.yml" "$MONITOR_DIR/prometheus/prometheus.yml"
            if [ "$hub_tx_had_job" -eq 1 ]; then
                cp "$hub_tx_work/remote.yml" "$MONITOR_DIR/prometheus/https/remote.yml"
            else
                rm -f "$MONITOR_DIR/prometheus/https/remote.yml"
            fi
        fi
        rm -rf -- "$hub_tx_work"
        exit "$code"
    }
    trap rollback_hub EXIT
    enable_https_job
    compose exec -T prometheus /bin/promtool check config /etc/prometheus/prometheus.yml
    compose restart prometheus
)

add_https_node() {
    linux_root
    require_hub
    ensure_packages curl jq openssl python3
    local name
    read -r -p "节点名称（新节点先生成证书包；迁移请用原名称）: " name
    valid_name "$name" || die "节点名称无效。"
    if [ ! -f "$PKI_DIR/nodes/$name/node.json" ]; then prepare_node "$name"; fi
    info "请把 $MONITOR_DIR/packages/$name.tar.gz 传到远端，运行 node-install --bundle 安装。"
    confirm "远端已安装并精确放行监控机源 IP，开始 HTTPS 验证？" || return 0
    ensure_pki
    if ! probe_https_node "$name"; then
        die "HTTPS 验证失败；未改变旧采集目标或隧道。请检查证书、源 IP、安全组、防火墙和节点日志。"
    fi
    # Start the validated HTTPS job before removing a legacy target.
    activate_https_job
    write_https_target "$name"
    cleanup_legacy_node "$name"
    info "已接入 ${name}：HTTPS + mTLS，后续采集不依赖 SSH。"
}

install_nft() {
    if command -v nft >/dev/null 2>&1; then return 0; fi
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y
        apt-get install -y nftables
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y nftables
    else
        die "请先安装 nftables；未将采集器暴露到公网。"
    fi
}

render_node_config() {
    local directory="$1" metadata="$2" address source_ip port family bind
    address="$(jq -r .address "$metadata")"; source_ip="$(jq -r .monitor_ip "$metadata")"; port="$(jq -r .port "$metadata")"
    if [[ "$source_ip" == *:* ]]; then family=ip6; bind="[::]:$port"; else family=ip; bind="0.0.0.0:$port"; fi
    cat > "$directory/web.yml" <<WEB
tls_server_config:
  cert_file: $NODE_CONFIG_DIR/server.pem
  key_file: $NODE_CONFIG_DIR/server-key.pem
  client_ca_file: $NODE_CONFIG_DIR/ca.pem
  client_auth_type: RequireAndVerifyClientCert
  client_allowed_sans:
    - spiffe://vps-toolkit/monitor
  min_version: TLS12
WEB
    # A separate table: never flush the host's existing firewall/routing tables.
    cat > "$directory/firewall.nft" <<NFT
table inet $NFT_TABLE {}
delete table inet $NFT_TABLE
table inet $NFT_TABLE {
    chain input {
        type filter hook input priority -10; policy accept;
        $family saddr $source_ip tcp dport $port accept
        tcp dport $port drop
    }
}
NFT
    cat > "$directory/firewall.service" <<UNIT
[Unit]
Description=VPS toolkit monitoring source IP restriction
Before=$NODE_SERVICE.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $NODE_CONFIG_DIR/firewall.nft
ExecReload=/usr/sbin/nft -f $NODE_CONFIG_DIR/firewall.nft
ExecStop=-/usr/sbin/nft delete table inet $NFT_TABLE
[Install]
WantedBy=multi-user.target
UNIT
    # nft may be installed in /usr/bin on some distributions.
    sed "s|/usr/sbin/nft|$(command -v nft)|g" "$directory/firewall.service" > "$directory/firewall.service.tmp"
    mv "$directory/firewall.service.tmp" "$directory/firewall.service"
    cat > "$directory/exporter.service" <<UNIT
[Unit]
Description=VPS toolkit Node Exporter (HTTPS mTLS)
Requires=$FIREWALL_SERVICE.service
After=network.target $FIREWALL_SERVICE.service
[Service]
User=vps-exporter
ExecStart=$NODE_DIR/node_exporter --web.listen-address=$bind --web.config.file=$NODE_CONFIG_DIR/web.yml
Restart=on-failure
RestartSec=5
MemoryMax=64M
CPUQuota=10%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
}

validate_bundle_certificates() {
    local directory="$1" address
    address="$(jq -r .address "$directory/node.json")"
    openssl verify -purpose sslserver -CAfile "$directory/ca.pem" "$directory/server.pem" >/dev/null
    openssl x509 -in "$directory/server.pem" -checkip "$address" -noout >/dev/null
    openssl x509 -in "$directory/server.pem" -checkend 86400 -noout >/dev/null
    [ "$(openssl x509 -in "$directory/server.pem" -pubkey -noout)" = "$(openssl pkey -in "$directory/server-key.pem" -pubout)" ] || die "节点证书与私钥不匹配。"
}

install_node_bundle() (
    node_tx_work="$(mktemp -d)"
    node_tx_changed=0
    node_tx_was_active=0
    node_tx_was_enabled=0
    rollback_node() {
        local code=$?
        trap - EXIT
        if [ "$code" -ne 0 ] && [ "$node_tx_changed" -eq 1 ]; then
            warn "安装失败，恢复原有采集器配置。"
            systemctl disable --now "$NODE_SERVICE" || true
            systemctl disable --now "$FIREWALL_SERVICE" || true
            rm -rf -- "$NODE_CONFIG_DIR"
            if [ -d "$node_tx_work/old-config" ]; then cp -a "$node_tx_work/old-config" "$NODE_CONFIG_DIR"; fi
            rm -f "$SYSTEMD_DIR/$NODE_SERVICE.service" "$SYSTEMD_DIR/$FIREWALL_SERVICE.service"
            if [ -f "$node_tx_work/old-exporter.service" ]; then cp -a "$node_tx_work/old-exporter.service" "$SYSTEMD_DIR/$NODE_SERVICE.service"; fi
            if [ -f "$node_tx_work/old-firewall.service" ]; then cp -a "$node_tx_work/old-firewall.service" "$SYSTEMD_DIR/$FIREWALL_SERVICE.service"; fi
            systemctl daemon-reload || true
            if [ -f "$node_tx_work/old-firewall.service" ]; then systemctl enable --now "$FIREWALL_SERVICE" || true; fi
            if [ "$node_tx_was_enabled" -eq 1 ]; then systemctl enable "$NODE_SERVICE" || true; fi
            if [ "$node_tx_was_active" -eq 1 ]; then systemctl start "$NODE_SERVICE" || true; fi
        fi
        rm -rf -- "$node_tx_work"
        exit "$code"
    }
    trap rollback_node EXIT
    package_meta extract "$1" "$node_tx_work/package" >/dev/null
    validate_bundle_certificates "$node_tx_work/package"
    openssl x509 -in "$node_tx_work/package/ca.pem" -noout -fingerprint -sha256
    confirm "已通过可信渠道核对 CA 指纹，安装此证书包？" || exit 0
    local port old_port=""
    port="$(jq -r .port "$node_tx_work/package/node.json")"
    if [ -f "$NODE_CONFIG_DIR/node.json" ]; then old_port="$(jq -r .port "$NODE_CONFIG_DIR/node.json")"; fi
    if [ "$port" != "$old_port" ] || ! systemctl is-active --quiet "$NODE_SERVICE"; then check_ports "$port"; fi
    if [ -d "$NODE_CONFIG_DIR" ] && [ ! -f "$NODE_CONFIG_DIR/.vps-toolkit-managed" ]; then die "节点配置目录已存在且不属于本模块，未覆盖。"; fi
    if [ -f "$SYSTEMD_DIR/$FIREWALL_SERVICE.service" ] && [ ! -f "$NODE_CONFIG_DIR/.vps-toolkit-managed" ]; then die "已有同名防火墙服务，未覆盖。"; fi
    install_nft
    render_node_config "$node_tx_work/package" "$node_tx_work/package/node.json"
    nft -c -f "$node_tx_work/package/firewall.nft"
    if [ -d "$NODE_CONFIG_DIR" ]; then cp -a "$NODE_CONFIG_DIR" "$node_tx_work/old-config"; fi
    if [ -f "$SYSTEMD_DIR/$NODE_SERVICE.service" ]; then cp -a "$SYSTEMD_DIR/$NODE_SERVICE.service" "$node_tx_work/old-exporter.service"; fi
    if [ -f "$SYSTEMD_DIR/$FIREWALL_SERVICE.service" ]; then cp -a "$SYSTEMD_DIR/$FIREWALL_SERVICE.service" "$node_tx_work/old-firewall.service"; fi
    if systemctl is-active --quiet "$NODE_SERVICE"; then node_tx_was_active=1; fi
    if systemctl is-enabled --quiet "$NODE_SERVICE"; then node_tx_was_enabled=1; fi
    node_tx_changed=1
    systemctl stop "$NODE_SERVICE" || true
    install -d -m 750 -o root -g vps-exporter "$NODE_CONFIG_DIR"
    local file
    for file in ca.pem server.pem server-key.pem web.yml node.json firewall.nft; do
        install -m 640 -o root -g vps-exporter "$node_tx_work/package/$file" "$NODE_CONFIG_DIR/$file"
    done
    touch "$NODE_CONFIG_DIR/.vps-toolkit-managed"
    install -m 644 "$node_tx_work/package/exporter.service" "$SYSTEMD_DIR/$NODE_SERVICE.service"
    install -m 644 "$node_tx_work/package/firewall.service" "$SYSTEMD_DIR/$FIREWALL_SERVICE.service"
    systemctl daemon-reload
    systemctl enable "$FIREWALL_SERVICE" "$NODE_SERVICE"
    systemctl restart "$FIREWALL_SERVICE"
    systemctl restart "$NODE_SERVICE"
    sleep 2
    systemctl is-active --quiet "$NODE_SERVICE"
    info "HTTPS mTLS 采集器已启动，仅允许 $(jq -r .monitor_ip "$NODE_CONFIG_DIR/node.json") 访问 TCP $port。"
    info "已有 UFW/firewalld 或云安全组时，还需精确放行该源 IP 到此端口；请勿向所有 IP 开放。"
    info "确认监控机接入成功后，请安全删除传输用的证书包。"
)

cert_status() {
    require_hub
    local cert
    for cert in "$PKI_DIR/ca.pem" "$MONITOR_DIR/prometheus/tls/client.pem" "$PKI_DIR"/nodes/*/server.pem; do
        [ -f "$cert" ] || continue
        printf '\n%s\n' "$cert"
        openssl x509 -in "$cert" -noout -enddate
        if ! openssl x509 -in "$cert" -checkend 2592000 -noout >/dev/null; then warn "证书已过期或将在 30 天内到期。"; fi
    done
}

cert_renew() {
    linux_root
    require_hub
    local name="${1:-}"
    if [ -z "$name" ]; then read -r -p "续签节点名称，或输入 client 续签监控机客户端证书: " name; fi
    ensure_packages openssl jq python3
    ensure_pki
    if [ "$name" = client ]; then
        sign_leaf monitor clientAuth URI:spiffe://vps-toolkit/monitor "$MONITOR_DIR/prometheus/tls/client"
        chown 65534:65534 "$MONITOR_DIR/prometheus/tls/client-key.pem"
        compose restart prometheus
        info "客户端证书已续签，无需更新节点 CA。"
    else
        valid_name "$name" && [ -f "$PKI_DIR/nodes/$name/node.json" ] || die "没有这个节点。"
        build_node_package "$name"
        info "将新的证书包传到远端并再次 node-install --bundle 安装。"
    fi
}
