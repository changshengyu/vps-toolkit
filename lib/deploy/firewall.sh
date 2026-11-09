# shellcheck shell=bash
# 一键部署：检查端口占用，在已启用的系统防火墙上放行端口
# 只在已启用的防火墙上添加规则，不会主动启用防火墙。

FIREWALL=""

detect_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        FIREWALL="ufw"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        FIREWALL="firewalld"
    elif command -v iptables >/dev/null 2>&1 &&
        iptables -S INPUT 2>/dev/null | grep -Eq '^-P INPUT (DROP|REJECT)|-j (DROP|REJECT)$'; then
        FIREWALL="iptables"
    else
        FIREWALL="none"
    fi
}

# 当前 SSH 端口：优先取本次连接的端口，其次取 sshd 配置
detect_ssh_ports() {
    local ports=""
    if [ -n "${SSH_CONNECTION:-}" ]; then
        ports="$(awk '{print $4}' <<<"$SSH_CONNECTION")"
    fi
    if command -v sshd >/dev/null 2>&1; then
        ports="$ports $(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}' || true)"
    fi
    tr ' ' '\n' <<<"$ports" | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ' || true
}

# 用法：fw_allow tcp|udp 端口或 起始-结束 [说明]
fw_allow() {
    local proto="$1" port="$2" note="${3:-}" ipt_port="${2/-/:}"

    case "$FIREWALL" in
        ufw)
            ufw allow "${ipt_port}/${proto}" comment "vps-toolkit ${note}" >/dev/null
            ;;
        firewalld)
            firewall-cmd --permanent --add-port="${port}/${proto}" >/dev/null
            ;;
        iptables)
            local cmd
            for cmd in iptables ip6tables; do
                command -v "$cmd" >/dev/null 2>&1 || continue
                "$cmd" -C INPUT -p "$proto" --dport "$ipt_port" -j ACCEPT 2>/dev/null ||
                    "$cmd" -I INPUT -p "$proto" --dport "$ipt_port" -j ACCEPT 2>/dev/null || true
            done
            ;;
        *) return 0 ;;
    esac
    info "  已放行 ${proto^^} ${port}${note:+（$note）}"
}

fw_finish() {
    case "$FIREWALL" in
        firewalld) firewall-cmd --reload >/dev/null ;;
        iptables)
            if command -v netfilter-persistent >/dev/null 2>&1; then
                netfilter-persistent save >/dev/null 2>&1 || true
            elif command -v service >/dev/null 2>&1 && service iptables save >/dev/null 2>&1; then
                :
            else
                warn "iptables 规则未持久化，重启后会丢失。可安装 iptables-persistent 后重新运行。"
            fi
            ;;
    esac
}

setup_firewall() {
    local p

    detect_firewall
    if [ "$FIREWALL" = "none" ]; then
        info "未检测到已启用的系统防火墙，跳过。"
        return 0
    fi

    info "检测到防火墙：$FIREWALL"
    # 先放行 SSH，避免把自己锁在外面
    for p in $(detect_ssh_ports); do
        fw_allow tcp "$p" "SSH"
    done
    fw_allow tcp "$PANEL_PORT" "面板"
    fw_allow tcp "$REALITY_PORT" "Reality"
    fw_allow tcp "$XHTTP_PORT" "XHTTP"
    fw_allow tcp "$CDN_PORT" "CDN"
    [ "$HY2_CERT_MODE" = "http" ] && fw_allow tcp 80 "证书申请与续期"
    fw_allow udp "$HY2_PORT" "hy2"
    fw_allow udp "$HOP_RANGE" "端口跳跃"
    fw_finish
}

# 列出某端口的监听进程名；没有监听则输出空
port_owner() {
    local proto="$1" port="$2" flag="-tlnpH"
    [ "$proto" = "udp" ] && flag="-ulnpH"
    # 端口未被占用时 grep 无匹配，不能让 pipefail 中断脚本
    ss $flag "sport = :$port" 2>/dev/null | grep -o 'users:(("[^"]*"' | sed 's/users:(("//; s/"$//' | sort -u | tr '\n' ' ' || true
}

# 端口被 x-ui / xray 以外的程序占用时报错
check_ports_free() {
    local errors=() item proto port owner
    local items=("tcp $REALITY_PORT" "tcp $XHTTP_PORT" "tcp $CDN_PORT" "udp $HY2_PORT")
    # 证书仍有效时不需要 80 端口
    if [ "$HY2_CERT_MODE" = "http" ] && ! cert_valid "$HY2_DOMAIN"; then items+=("tcp 80"); fi
    [ "$PANEL_INSTALLED" = "1" ] || items+=("tcp $PANEL_PORT")

    command -v ss >/dev/null 2>&1 || {
        warn "缺少 ss 命令，跳过端口占用检查。"
        return 0
    }

    for item in "${items[@]}"; do
        read -r proto port <<<"$item"
        owner="$(port_owner "$proto" "$port")"
        [ -z "$owner" ] && continue
        case "$owner" in
            *xray* | *x-ui*) [ "$port" != "80" ] && continue ;;
        esac
        errors+=("${proto^^} $port 已被占用：$owner")
    done

    if [ "${#errors[@]}" -gt 0 ]; then
        warn "端口检查未通过："
        printf '  - %s\n' "${errors[@]}" >&2
        die "请释放端口或修改对应参数后重试。"
    fi
}
