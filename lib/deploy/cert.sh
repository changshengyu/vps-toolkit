# shellcheck shell=bash
# 一键部署：用 acme.sh 申请证书，路径与 3x-ui 菜单一致：/root/cert/<域名>/
# 续期由 acme.sh 的定时任务完成，续期后执行 systemctl restart x-ui。

ACME="${HOME:-/root}/.acme.sh/acme.sh"
CERT_RELOAD_CMD="systemctl restart x-ui"

cert_dir() { printf '/root/cert/%s' "$1"; }

# 证书存在、包含该域名且 30 天内不过期
cert_valid() {
    local dir
    dir="$(cert_dir "$1")"
    [ -s "$dir/fullchain.pem" ] && [ -s "$dir/privkey.pem" ] || return 1
    openssl x509 -in "$dir/fullchain.pem" -noout -checkend 2592000 >/dev/null 2>&1 || return 1
    openssl x509 -in "$dir/fullchain.pem" -noout -text 2>/dev/null | grep -Fq "DNS:$1"
}

ACME_READY=0

ensure_acme() {
    [ "$ACME_READY" = "1" ] && return 0
    if [ ! -x "$ACME" ]; then
        info "正在安装 acme.sh..."
        (
            cd "${HOME:-/root}"
            if [ -n "$ACME_EMAIL" ]; then
                run_remote_script "https://get.acme.sh" "email=$ACME_EMAIL"
            else
                run_remote_script "https://get.acme.sh"
            fi
        ) >/dev/null || die "安装 acme.sh 失败。"
        [ -x "$ACME" ] || die "安装 acme.sh 后未找到：$ACME"
    fi
    "$ACME" --set-default-ca --server letsencrypt >/dev/null
    if [ -n "$ACME_EMAIL" ]; then
        "$ACME" --register-account -m "$ACME_EMAIL" >/dev/null 2>&1 || true
    fi
    ACME_READY=1
}

# acme.sh 返回 2 表示证书未到续期时间，视为成功
acme_ok() { [ "$1" -eq 0 ] || [ "$1" -eq 2 ]; }

install_cert() {
    local domain="$1" dir
    shift
    dir="$(cert_dir "$domain")"
    mkdir -p "$dir"
    "$ACME" --installcert -d "$domain" "$@" \
        --key-file "$dir/privkey.pem" \
        --fullchain-file "$dir/fullchain.pem" \
        --reloadcmd "$CERT_RELOAD_CMD" >/dev/null ||
        die "安装证书失败：$domain"
    chmod 600 "$dir/privkey.pem"
    chmod 644 "$dir/fullchain.pem"
}

# HTTP 验证：acme.sh 临时监听 TCP 80
issue_cert_http() {
    local domain="$1" rc=0 listen=()
    ip -4 addr show scope global 2>/dev/null | grep -q 'inet ' || listen=(--listen-v6)

    ensure_acme
    info "申请证书（HTTP 验证）：$domain"
    "$ACME" --issue -d "$domain" --standalone --httpport 80 "${listen[@]}" || rc=$?
    acme_ok "$rc" || die "证书申请失败：$domain。请确认域名已解析到本机（灰云）且 TCP 80 可从公网访问。"
    install_cert "$domain"
}

# DNS 验证：Cloudflare API Token；wildcard=1 时同时申请 *.域名（与 x-ui 菜单一致）
issue_cert_dns() {
    local domain="$1" wildcard="${2:-0}" rc=0 names=(-d "$1")
    [ "$wildcard" = "1" ] && names+=(-d "*.$domain")

    ensure_acme
    info "申请证书（DNS 验证）：$domain"
    CF_Token="$CF_TOKEN" "$ACME" --issue --dns dns_cf "${names[@]}" || rc=$?
    acme_ok "$rc" || die "证书申请失败：$domain。请检查 CF Token 是否有该域名的 Zone:DNS:Edit 权限。"
    install_cert "$domain" "${names[@]:2}"
}

setup_certs() {
    if cert_valid "$HY2_DOMAIN"; then
        info "证书有效，跳过：$HY2_DOMAIN"
    elif [ "$HY2_CERT_MODE" = "dns" ]; then
        issue_cert_dns "$HY2_DOMAIN"
    else
        ensure_packages socat # acme.sh 的 standalone 模式依赖 socat
        issue_cert_http "$HY2_DOMAIN"
    fi

    if cert_valid "$CDN_DOMAIN"; then
        info "证书有效，跳过：$CDN_DOMAIN"
    else
        issue_cert_dns "$CDN_DOMAIN" 1
    fi
}

# 域名解析检查，只提醒不中断（CDN 域名开了小黄云，不检查）
check_dns() {
    local ip resolved
    ip="$(curl -4 -fsS --max-time 5 https://api4.ipify.org 2>/dev/null || true)"
    [ -n "$ip" ] || {
        warn "无法获取本机公网 IPv4，跳过域名解析检查。"
        return 0
    }
    PUBLIC_IP="$ip"
    resolved="$(getent ahostsv4 "$HY2_DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
    case " $resolved " in
        *" $ip "*) info "域名解析正确：$HY2_DOMAIN -> $ip" ;;
        *) warn "$HY2_DOMAIN 解析到 [${resolved:-无记录}]，本机 IP 为 $ip。hy2、Reality 和面板都依赖这个域名，请确认解析（灰云）。" ;;
    esac
}
