# shellcheck shell=bash
# 一键部署：用 jq 模板渲染入站和 Xray 路由

TEMPLATE_DIR="$ROOT_DIR/templates"

# 入站模板，按顺序渲染
INBOUND_TEMPLATES=(hy2 reality-vision reality-xhttp cdn-ws)

render_inbound() {
    local name="$1" client sniffing
    client="$(jq -cn --arg email "$CLIENT_EMAIL" --arg sub "$SUB_ID" '{
        email: $email, enable: true, subId: $sub,
        limitIp: 0, totalGB: 0, expiryTime: 0, tgId: 0, comment: "", reset: 0
    }')"
    sniffing='{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":true,"ipsExcluded":[],"domainsExcluded":[]}'

    jq -n \
        --argjson client "$client" \
        --argjson sniffing "$sniffing" \
        --arg node "$NODE_NAME" \
        --arg uuid "$CLIENT_UUID" \
        --arg hy2_port "$HY2_PORT" \
        --arg hy2_domain "$HY2_DOMAIN" \
        --arg hy2_auth "$HY2_AUTH" \
        --arg salamander "$SALAMANDER_PASS" \
        --arg hop_range "$HOP_RANGE" \
        --arg reality_port "$REALITY_PORT" \
        --arg reality_domain "$REALITY_DOMAIN" \
        --arg reality_target "$REALITY_TARGET" \
        --arg tcp_priv "$REALITY_TCP_PRIV" \
        --arg tcp_pub "$REALITY_TCP_PUB" \
        --arg tcp_sids "$REALITY_TCP_SIDS" \
        --arg xhttp_port "$XHTTP_PORT" \
        --arg xhttp_path "$XHTTP_PATH" \
        --arg xhttp_priv "$REALITY_XHTTP_PRIV" \
        --arg xhttp_pub "$REALITY_XHTTP_PUB" \
        --arg xhttp_sids "$REALITY_XHTTP_SIDS" \
        --arg vless_dec "$VLESS_DEC" \
        --arg vless_enc "$VLESS_ENC" \
        --arg cdn_port "$CDN_PORT" \
        --arg cdn_domain "$CDN_DOMAIN" \
        --arg ws_path "$CDN_WS_PATH" \
        -f "$TEMPLATE_DIR/inbounds/$name.jq"
}

render_routing() {
    jq -n \
        --arg ai "$ENABLE_AI_ROUTE" \
        --arg ss_addr "$SS_ISP_ADDR" \
        --arg ss_port "${SS_ISP_PORT:-0}" \
        --arg ss_method "$SS_ISP_METHOD" \
        --arg ss_pass "$SS_ISP_PASS" \
        --arg geodata_cron "$GEODATA_CRON" \
        -f "$TEMPLATE_DIR/xray-routing.jq"
}

# 面板 API 要求 settings、streamSettings、sniffing 为 JSON 字符串
to_api_payload() {
    jq '.settings |= tojson | .streamSettings |= tojson | .sniffing |= tojson'
}

# 渲染全部内容到目录：readable/ 为便于阅读的展开版，payload/ 为 API 实际提交的内容
render_all() {
    local out="$1" name i=1 file
    mkdir -p "$out/readable" "$out/payload"
    for name in "${INBOUND_TEMPLATES[@]}"; do
        file="$(printf '%02d-%s.json' "$i" "$name")"
        render_inbound "$name" >"$out/readable/$file"
        to_api_payload <"$out/readable/$file" >"$out/payload/$file"
        i=$((i + 1))
    done
    render_routing >"$out/readable/xray-routing.json"
    cp "$out/readable/xray-routing.json" "$out/payload/xray-routing.json"
}
