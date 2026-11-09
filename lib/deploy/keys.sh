# shellcheck shell=bash
# 一键部署：生成密钥并保存状态
# 状态文件保存生成的密钥和本次参数，重跑时复用，避免已导入客户端的节点失效。

# 需要持久化的生成值（包括 params.sh 中自动生成的参数）
STATE_KEYS=(
    CLIENT_UUID SUB_ID HY2_AUTH SALAMANDER_PASS
    REALITY_TCP_PRIV REALITY_TCP_PUB REALITY_TCP_SIDS
    REALITY_XHTTP_PRIV REALITY_XHTTP_PUB REALITY_XHTTP_SIDS
    VLESS_DEC VLESS_ENC
    PANEL_PORT PANEL_USER PANEL_PASS PANEL_PATH
    CDN_WS_PATH XHTTP_PATH CLIENT_EMAIL PANEL_INSTALLED_BY_US
)

XRAY_BIN="${XRAY_BIN:-}"
PANEL_INSTALLED_BY_US="${PANEL_INSTALLED_BY_US:-0}"

find_xray() {
    local f
    if [ -n "$XRAY_BIN" ]; then
        [ -x "$XRAY_BIN" ] || die "XRAY_BIN 不可执行：$XRAY_BIN"
        return 0
    fi
    for f in /usr/local/x-ui/bin/xray-linux-*; do
        if [ -x "$f" ]; then
            XRAY_BIN="$f"
            return 0
        fi
    done
    return 1
}

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

gen_uuid() {
    if [ -n "$XRAY_BIN" ]; then
        "$XRAY_BIN" uuid
    else
        cat /proc/sys/kernel/random/uuid
    fi
}

# 输出两行：私钥、公钥
gen_x25519() {
    local out priv pub tmp
    if [ -n "$XRAY_BIN" ]; then
        out="$("$XRAY_BIN" x25519)"
        # 新版输出 "PrivateKey: / Password (PublicKey):"，旧版输出 "Private key: / Public key:"
        priv="$(awk -F': ' 'tolower($1) ~ /^private ?key$/ {print $2; exit}' <<<"$out")"
        pub="$(awk -F': ' 'tolower($1) ~ /public ?key/ {print $2; exit}' <<<"$out")"
    else
        tmp="$(mktemp)"
        openssl genpkey -algorithm X25519 -outform DER -out "$tmp" 2>/dev/null
        priv="$(tail -c 32 "$tmp" | b64url)"
        pub="$(openssl pkey -inform DER -in "$tmp" -pubout -outform DER 2>/dev/null | tail -c 32 | b64url)"
        rm -f "$tmp"
    fi
    [ -n "$priv" ] && [ -n "$pub" ] || die "生成 X25519 密钥失败。"
    printf '%s\n%s\n' "$priv" "$pub"
}

# 输出两行：decryption、encryption（ML-KEM-768 抗量子版本）
gen_vlessenc() {
    local out dec enc
    if [ -z "$XRAY_BIN" ]; then
        printf '%s\n%s\n' "<需要 xray vlessenc 生成>" "<需要 xray vlessenc 生成>"
        return 0
    fi
    out="$("$XRAY_BIN" vlessenc)"
    dec="$(awk '/ML-KEM-768/ {f=1} f && /"decryption"/ {gsub(/.*: "|"$/, ""); print; exit}' <<<"$out")"
    enc="$(awk '/ML-KEM-768/ {f=1} f && /"encryption"/ {gsub(/.*: "|"$/, ""); print; exit}' <<<"$out")"
    [ -n "$dec" ] && [ -n "$enc" ] || die "解析 xray vlessenc 输出失败。"
    printf '%s\n%s\n' "$dec" "$enc"
}

# 8 个不同长度的 shortId，逗号分隔
gen_short_ids() {
    local len ids=()
    for len in 16 4 8 10 6 2 14 12; do
        ids+=("$(openssl rand -hex 8 | cut -c1-"$len")")
    done
    local IFS=','
    printf '%s' "${ids[*]}"
}

# 生成缺失的密钥；REGEN=1 时全部重新生成
generate_secrets() {
    local pair need_xray=0

    find_xray || true
    if [ -z "$XRAY_BIN" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            warn "未找到 xray，预览中用 openssl 生成密钥，VLESS Encryption 用占位符代替。"
        else
            need_xray=1
        fi
    fi

    _keep() { [ "$REGEN" != "1" ] && [ -n "${STATE[$1]:-}" ] && [[ "${STATE[$1]}" != "<"* ]]; }

    if _keep CLIENT_UUID; then CLIENT_UUID="${STATE[CLIENT_UUID]}"; else CLIENT_UUID="$(gen_uuid)"; fi
    if _keep SUB_ID; then SUB_ID="${STATE[SUB_ID]}"; else SUB_ID="$(rand_lower 16)"; fi
    if _keep HY2_AUTH; then HY2_AUTH="${STATE[HY2_AUTH]}"; else HY2_AUTH="$(rand_str 16)"; fi
    if _keep SALAMANDER_PASS; then SALAMANDER_PASS="${STATE[SALAMANDER_PASS]}"; else SALAMANDER_PASS="$(rand_str 16)"; fi

    if _keep REALITY_TCP_PRIV && _keep REALITY_TCP_PUB; then
        REALITY_TCP_PRIV="${STATE[REALITY_TCP_PRIV]}" REALITY_TCP_PUB="${STATE[REALITY_TCP_PUB]}"
    else
        pair="$(gen_x25519)"
        REALITY_TCP_PRIV="$(sed -n 1p <<<"$pair")" REALITY_TCP_PUB="$(sed -n 2p <<<"$pair")"
    fi
    if _keep REALITY_XHTTP_PRIV && _keep REALITY_XHTTP_PUB; then
        REALITY_XHTTP_PRIV="${STATE[REALITY_XHTTP_PRIV]}" REALITY_XHTTP_PUB="${STATE[REALITY_XHTTP_PUB]}"
    else
        pair="$(gen_x25519)"
        REALITY_XHTTP_PRIV="$(sed -n 1p <<<"$pair")" REALITY_XHTTP_PUB="$(sed -n 2p <<<"$pair")"
    fi
    if _keep REALITY_TCP_SIDS; then REALITY_TCP_SIDS="${STATE[REALITY_TCP_SIDS]}"; else REALITY_TCP_SIDS="$(gen_short_ids)"; fi
    if _keep REALITY_XHTTP_SIDS; then REALITY_XHTTP_SIDS="${STATE[REALITY_XHTTP_SIDS]}"; else REALITY_XHTTP_SIDS="$(gen_short_ids)"; fi

    if _keep VLESS_DEC && _keep VLESS_ENC; then
        VLESS_DEC="${STATE[VLESS_DEC]}" VLESS_ENC="${STATE[VLESS_ENC]}"
    else
        [ "$need_xray" = "1" ] && die "生成 VLESS Encryption 密钥需要 xray，请先安装 3x-ui 或用 XRAY_BIN 指定路径。"
        pair="$(gen_vlessenc)"
        VLESS_DEC="$(sed -n 1p <<<"$pair")" VLESS_ENC="$(sed -n 2p <<<"$pair")"
    fi
}

# 写入状态文件（权限 600）
save_state() {
    local file="$1" key prev_umask
    prev_umask="$(umask)"
    umask 077
    mkdir -p "$(dirname "$file")"
    {
        printf '# vps-toolkit 一键部署生成的值，重跑时复用。删除本文件或使用 --regen 可重新生成。\n'
        printf '# 生成时间：%s\n' "$(date '+%F %T')"
        # 生成的值和本次使用的全部参数，重跑时无需再次输入
        local spec seen=" "
        for key in "${STATE_KEYS[@]}"; do
            printf '%s=%s\n' "$key" "${!key}"
            seen+="$key "
        done
        for spec in "${PARAM_SPECS[@]}"; do
            key="$(spec_field "$spec" 1)"
            case "$seen" in *" $key "*) continue ;; esac
            printf '%s=%s\n' "$key" "${!key}"
        done
    } >"$file.tmp"
    mv "$file.tmp" "$file"
    chmod 600 "$file"
    umask "$prev_umask"
}
