#!/usr/bin/env bash

ensure_renewal_key() {
    local name="$1" directory="$MONITOR_DIR/renewal-keys"
    valid_name "$name" || die "节点名称无效。"
    require_command ssh-keygen
    install -d -m 700 "$directory"
    if [ ! -f "$directory/$name" ] && [ ! -f "$directory/$name.pub" ]; then
        ssh-keygen -q -t ed25519 -N '' -C "vps-toolkit-monitor-renew-$name" -f "$directory/$name"
    fi
    [ -s "$directory/$name" ] && [ -s "$directory/$name.pub" ] || die "续期密钥不完整，请恢复备份。"
    chmod 600 "$directory/$name"
}

authorize_renewal_key() {
    linux_root
    local user="$1" public_file="$2" home group public source name forced line authorized temporary previous result
    valid_user "$user" || die "SSH 用户无效。"
    [ -f "$NODE_CONFIG_DIR/.vps-toolkit-managed" ] || die "未找到托管节点配置。"
    public="$(cat "$public_file")"
    [ "${#public}" -le 8192 ] && [[ "$public" != *$'\n'* ]] && [[ "$public" != *$'\r'* ]] || die "续期公钥必须是单行。"
    [[ "$public" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/]+={0,3}([[:space:]][A-Za-z0-9_.@-]+)?$ ]] || die "续期公钥无效。"
    ssh-keygen -lf "$public_file" >/dev/null
    home="$(getent passwd "$user" | cut -d: -f6)"
    [ -n "$home" ] && [ -d "$home" ] || die "SSH 用户主目录不存在。"
    group="$(id -gn "$user")"
    source="$(jq -r .monitor_ip "$NODE_CONFIG_DIR/node.json")"
    name="$(jq -r .name "$NODE_CONFIG_DIR/node.json")"
    package_meta validate "$NODE_CONFIG_DIR/node.json" >/dev/null
    install -d -m 755 "$NODE_DIR/renewal/lib/monitor"
    install -m 644 "$ROOT_DIR/lib/common.sh" "$NODE_DIR/renewal/lib/common.sh"
    install -m 644 "$ROOT_DIR/lib/monitor/package.py" "$NODE_DIR/renewal/lib/monitor/package.py"
    install -m 644 "$ROOT_DIR/lib/monitor/tls.sh" "$NODE_DIR/renewal/lib/monitor/tls.sh"
    install -m 755 "$ROOT_DIR/lib/monitor/restricted-renew.sh" "$NODE_DIR/renewal/restricted-renew.sh"
    forced="/usr/bin/bash $NODE_DIR/renewal/restricted-renew.sh"
    if [ "$user" != root ]; then forced="sudo -n $forced"; fi
    line="from=\"$source\",restrict,command=\"$forced\" $public"
    install -d -m 700 -o "$user" -g "$group" "$home/.ssh"
    authorized="$home/.ssh/authorized_keys"
    touch "$authorized"
    temporary="$(mktemp "$home/.ssh/.monitor-key.XXXXXXXX")"
    previous=""
    if [ -f "$NODE_CONFIG_DIR/renewal-authorized-line" ]; then previous="$(cat "$NODE_CONFIG_DIR/renewal-authorized-line")"; fi
    if [ -n "$previous" ]; then
        result=0
        grep -vxF -- "$previous" "$authorized" > "$temporary" || result=$?
        if [ "$result" -gt 1 ]; then rm -f "$temporary"; die "读取已有 SSH 公钥失败，未覆盖。"; fi
    else
        cat "$authorized" > "$temporary"
    fi
    grep -qxF -- "$line" "$temporary" || printf '%s\n' "$line" >> "$temporary"
    chown "$user:$group" "$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$authorized"
    (umask 077; printf '%s\n' "$line" > "$NODE_CONFIG_DIR/renewal-authorized-line")
    (umask 077; printf '%s\n' "$user" > "$NODE_CONFIG_DIR/renewal-user")
    info "已为 ${name} 配置专用证书续期公钥（限定源 IP、强制命令、禁止 Shell 和转发）。"
}

revoke_renewal_key() {
    local user home authorized previous temporary group result
    [ -f "$NODE_CONFIG_DIR/renewal-authorized-line" ] && [ -f "$NODE_CONFIG_DIR/renewal-user" ] || return 0
    user="$(cat "$NODE_CONFIG_DIR/renewal-user")"
    valid_user "$user" || die "续期授权记录中的用户无效。"
    home="$(getent passwd "$user" | cut -d: -f6 || true)"
    authorized="$home/.ssh/authorized_keys"
    if [ -n "$home" ] && [ -f "$authorized" ]; then
        previous="$(cat "$NODE_CONFIG_DIR/renewal-authorized-line")"
        group="$(id -gn "$user")"
        temporary="$(mktemp "$home/.ssh/.monitor-revoke.XXXXXXXX")"
        result=0
        grep -vxF -- "$previous" "$authorized" > "$temporary" || result=$?
        if [ "$result" -gt 1 ]; then rm -f "$temporary"; die "读取已有 SSH 公钥失败，未覆盖。"; fi
        chown "$user:$group" "$temporary"
        chmod 600 "$temporary"
        mv "$temporary" "$authorized"
    fi
    rm -f "$NODE_CONFIG_DIR/renewal-authorized-line" "$NODE_CONFIG_DIR/renewal-user"
}

renewal_ssh() {
    local config="$1" mode="$2" bundle="${3:-}" address source user port key family destination
    package_meta deployment "$config" >/dev/null
    address="$(jq -r .node.address "$config")"
    source="$(jq -r .node.monitor_ip "$config")"
    user="$(jq -r .ssh.user "$config")"
    port="$(jq -r .ssh.port "$config")"
    key="$(jq -r '.renewal_identity // empty' "$config")"
    [ -n "$key" ] && [ -r "$key" ] || die "缺少专用续期密钥，请重新自动部署一次。"
    if [[ "$source" == *:* ]]; then family=inet6; else family=inet; fi
    destination="$user@$address"
    local options=(-F /dev/null -p "$port" -i "$key" -o IdentitiesOnly=yes -o IdentityAgent=none \
        -o BatchMode=yes -o StrictHostKeyChecking=yes -o "AddressFamily=$family" \
        -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -T)
    case "$mode" in
        check)
            printf 'VPS_MONITOR_CHECK_V1\n' | ssh "${options[@]}" -- "$destination" monitor-renew-check | grep -x VPS_MONITOR_RENEW_READY
            ;;
        renew)
            { printf 'VPS_MONITOR_RENEW_V1\n'; cat "$bundle"; } | ssh "${options[@]}" -- "$destination" monitor-renew | grep -x VPS_MONITOR_RENEWED
            ;;
        *) die "无效续期操作。" ;;
    esac
}

renew_managed_node() (
    renewal_work="$(mktemp -d /tmp/vtm-renew-hub.XXXXXXXX)"
    trap 'rm -rf -- "$renewal_work"' EXIT
    renewal_config="$(package_meta deployment "$1")"
    printf '%s\n' "$renewal_config" > "$renewal_work/config.json"
    renewal_name="$(jq -r .node.name "$renewal_work/config.json")"
    ensure_pki
    mkdir "$renewal_work/node"
    jq .node "$renewal_work/config.json" > "$renewal_work/node/node.json"
    build_node_package "$renewal_name" "$renewal_work/node" "$renewal_work/node.tar.gz" >/dev/null
    renewal_ssh "$renewal_work/config.json" renew "$renewal_work/node.tar.gz" >/dev/null
    renewal_original_pki="$PKI_DIR"
    mkdir -p "$renewal_work/verify/nodes"
    cp -R "$renewal_work/node" "$renewal_work/verify/nodes/$renewal_name"
    PKI_DIR="$renewal_work/verify"
    probe_https_node "$renewal_name" || die "远端已更新证书，但 HTTPS 验证失败，请检查节点状态。"
    PKI_DIR="$renewal_original_pki"
    cp "$renewal_work/node/"* "$PKI_DIR/nodes/$renewal_name/"
    install -m 600 "$renewal_work/node.tar.gz" "$MONITOR_DIR/packages/$renewal_name.tar.gz"
    info "已自动续签并同步 ${renewal_name}，无需登录密码。"
)
