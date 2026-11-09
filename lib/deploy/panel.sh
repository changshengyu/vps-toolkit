# shellcheck shell=bash
# 一键部署：安装 / 检测 3x-ui，并通过面板 API（Bearer Token）写入配置

XUI_BIN="${XUI_BIN:-/usr/local/x-ui/x-ui}"
XUI_INSTALL_URL="https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"
API_TOKEN_NAME="vps-toolkit"

PANEL_INSTALLED=0
API_BASE=""
API_TOKEN=""
API_HEADER_FILE=""
CURL_OPTS=()

panel_installed() { [ -x "$XUI_BIN" ]; }

# 读取已安装面板的端口和访问路径，覆盖参数中的值
detect_existing_panel() {
    local settings port path
    panel_installed || return 0
    PANEL_INSTALLED=1

    settings="$("$XUI_BIN" setting -show true 2>/dev/null)" || die "读取面板设置失败：x-ui setting -show"
    port="$(awk '/^port: / {print $2}' <<<"$settings")"
    path="$(awk '/^webBasePath:/ {sub(/^webBasePath: ?/, ""); print}' <<<"$settings")"
    path="${path#/}" path="${path%/}"
    [ -n "$port" ] || die "无法读取面板端口。"

    PANEL_PORT="$port"
    PANEL_PATH="$path"
    PARAM_SOURCE[PANEL_PORT]="现有面板"
    PARAM_SOURCE[PANEL_PATH]="现有面板"
    # 账号密码只有当初由本脚本安装时才知道
    if [ "${STATE[PANEL_INSTALLED_BY_US]:-0}" != "1" ]; then
        PANEL_USER="" PANEL_PASS=""
        PARAM_SOURCE[PANEL_USER]="未使用"
        PARAM_SOURCE[PANEL_PASS]="未使用"
    fi
}

install_panel() {
    if [ "$PANEL_INSTALLED" = "1" ]; then
        info "3x-ui 已安装，跳过安装。"
        return 0
    fi

    info "正在安装 3x-ui（无人值守）..."
    (
        export XUI_NONINTERACTIVE=1
        export XUI_PANEL_PORT="$PANEL_PORT"
        export XUI_USERNAME="$PANEL_USER"
        export XUI_PASSWORD="$PANEL_PASS"
        export XUI_WEB_BASE_PATH="$PANEL_PATH"
        export XUI_SSL_MODE=none # 证书由本脚本申请后再设为面板证书
        [ -n "$PUBLIC_IP" ] && export XUI_SERVER_IP="$PUBLIC_IP"
        run_remote_script "$XUI_INSTALL_URL"
    ) </dev/null || die "3x-ui 安装失败。"

    panel_installed || die "安装后未找到 $XUI_BIN。"
    PANEL_INSTALLED=1
    PANEL_INSTALLED_BY_US=1
}

# 把 hy2 证书设为面板证书
set_panel_cert() {
    local cert key current
    cert="$(cert_dir "$HY2_DOMAIN")/fullchain.pem"
    key="$(cert_dir "$HY2_DOMAIN")/privkey.pem"
    current="$("$XUI_BIN" setting -getCert true 2>/dev/null | awk -F': ' '/^cert:/ {print $2}' | tr -d '[:space:]' || true)"

    if [ "$current" = "$cert" ]; then
        info "面板证书已是 $HY2_DOMAIN，跳过。"
        return 0
    fi
    if [ -n "$current" ]; then
        warn "面板当前证书为 $current，将改为 $cert。"
    fi
    "$XUI_BIN" cert -webCert "$cert" -webCertKey "$key" >/dev/null || die "设置面板证书失败。"
    systemctl restart x-ui
    info "已将 $HY2_DOMAIN 的证书设为面板证书。"
}

panel_scheme() {
    local current
    current="$("$XUI_BIN" setting -getCert true 2>/dev/null | awk -F': ' '/^cert:/ {print $2}' | tr -d '[:space:]' || true)"
    if [ -n "$current" ]; then printf 'https'; else printf 'http'; fi
}

# 生成本脚本专用的 API Token（只轮换名为 vps-toolkit 的 Token，不影响其他 Token）
init_api() {
    local out scheme path_part=""

    out="$("$XUI_BIN" setting -getApiToken -tokenName "$API_TOKEN_NAME" -tokenScope admin 2>&1)" || true
    API_TOKEN="$(awk '/^apiToken: / {print $2}' <<<"$out" | tail -1)"
    [ -n "$API_TOKEN" ] || die "获取面板 API Token 失败：$out"

    API_HEADER_FILE="$(mktemp)"
    chmod 600 "$API_HEADER_FILE"
    printf 'Authorization: Bearer %s\n' "$API_TOKEN" >"$API_HEADER_FILE"
    trap 'rm -f "$API_HEADER_FILE"' EXIT

    [ -n "$PANEL_PATH" ] && path_part="/$PANEL_PATH"
    scheme="$(panel_scheme)"
    if [ "$scheme" = "https" ]; then
        # 用证书域名访问本机，证书校验才能通过
        API_BASE="https://$HY2_DOMAIN:$PANEL_PORT$path_part"
        CURL_OPTS=(--noproxy "*" --resolve "$HY2_DOMAIN:$PANEL_PORT:127.0.0.1")
    else
        API_BASE="http://127.0.0.1:$PANEL_PORT$path_part"
        CURL_OPTS=(--noproxy "*")
    fi
}

# 用法：api 方法 路径 [curl 参数...]，输出响应体；HTTP 或 success=false 时报错
api() {
    local method="$1" path="$2" body
    shift 2
    body="$(curl -sS --max-time 60 -X "$method" "${CURL_OPTS[@]}" -H @"$API_HEADER_FILE" \
        "$API_BASE$path" "$@")" || die "请求面板失败：$method $path"
    if ! jq -e '.success == true' >/dev/null 2>&1 <<<"$body"; then
        die "面板返回错误（$method $path）：$(jq -r '.msg // empty' 2>/dev/null <<<"$body" || printf '%s' "${body:0:200}")"
    fi
    printf '%s' "$body"
}

wait_panel_ready() {
    local i
    for i in $(seq 1 30); do
        if curl -sS --max-time 5 "${CURL_OPTS[@]}" -H @"$API_HEADER_FILE" \
            "$API_BASE/panel/api/server/status" 2>/dev/null | jq -e '.success == true' >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    die "面板在 60 秒内未就绪：$API_BASE"
}

backup_xray_template() {
    local dir="$DEPLOY_HOME/backup" file
    mkdir -p "$dir"
    chmod 700 "$dir"
    file="$dir/xray-template-$(date +%Y%m%d-%H%M%S).json"
    get_xray_template | jq . >"$file"
    chmod 600 "$file"
    printf '%s' "$file"
}

# 入站使用的传输层，用于判断端口冲突
inbound_transport() {
    case "$1" in hysteria | wireguard | amneziawg | tuic) printf 'udp' ;; *) printf 'tcp' ;; esac
}

# 导入入站；同端口同传输层或同 tag 的已有入站，按 REPLACE 决定覆盖或跳过
import_inbounds() {
    local list file port proto tag transport conflicts id remark answer
    list="$(api GET /panel/api/inbounds/list | jq -c '.obj // []')"

    for file in "$PREVIEW_DIR"/payload/0*.json; do
        port="$(jq -r .port "$file")"
        proto="$(jq -r .protocol "$file")"
        tag="$(jq -r .tag "$file")"
        remark="$(jq -r .remark "$file")"
        transport="$(inbound_transport "$proto")"

        conflicts="$(jq -c --argjson port "$port" --arg tag "$tag" --arg tr "$transport" '
            [.[] | select(.tag == $tag or (.port == $port and
                ((if (.protocol | IN("hysteria","wireguard","amneziawg","tuic")) then "udp" else "tcp" end) == $tr)))
             | {id, remark, port, protocol}]' <<<"$list")"

        if [ "$(jq length <<<"$conflicts")" -gt 0 ]; then
            info "入站 $remark 与已有入站冲突：$(jq -r 'map("\(.remark)（\(.protocol) \(.port)）") | join("、")' <<<"$conflicts")"
            answer="$REPLACE"
            if [ -z "$answer" ]; then
                if [ "$INTERACTIVE" = "1" ]; then
                    confirm "删除已有入站并导入新的？" && answer=y || answer=n
                else
                    answer=n
                fi
            fi
            if [ "$answer" != "y" ]; then
                warn "已跳过 $remark。"
                continue
            fi
            for id in $(jq -r '.[].id' <<<"$conflicts"); do
                api POST "/panel/api/inbounds/del/$id" >/dev/null
            done
        fi

        api POST /panel/api/inbounds/import --data-urlencode "data@$file" >/dev/null
        info "  已导入：$remark"
        IMPORTED+=("$remark")
    done
}

# 读取面板当前的 Xray 模板（JSON 对象）
get_xray_template() {
    api POST /panel/api/xray/ | jq -c '.obj | if type == "string" then fromjson else . end
        | .xraySetting | if type == "string" then fromjson else . end'
}

# 保存 Xray 模板，参数为模板 JSON
save_xray_template() {
    local file
    file="$(mktemp)"
    chmod 600 "$file"
    printf '%s' "$1" >"$file"
    api POST /panel/api/xray/update --data-urlencode "xraySetting@$file" >/dev/null
    rm -f "$file"
}

update_xray_template() {
    local current patch merged
    current="$(get_xray_template)"
    patch="$(cat "$PREVIEW_DIR/payload/xray-routing.json")"

    # 出站、路由规则和 geodata 自动更新整体替换，其余字段（log、api、policy 等）保持不变
    merged="$(jq -c --argjson p "$patch" '
        .outbounds = $p.outbounds
        | .routing = ((.routing // {}) + {domainStrategy: $p.routing.domainStrategy, rules: $p.routing.rules})
        | if $p.geodata then .geodata = $p.geodata else del(.geodata) end
    ' <<<"$current")"

    save_xray_template "$merged"
    info "已更新 Xray 模板（出站、路由与 geodata 自动更新）。"
}

restart_xray() {
    local i status state
    api POST /panel/api/server/restartXrayService >/dev/null
    for i in $(seq 1 15); do
        sleep 2
        status="$(api GET /panel/api/server/status)"
        state="$(jq -r '.obj.xray.state' <<<"$status")"
        case "$state" in
            running)
                info "Xray 运行正常（$(jq -r '.obj.xray.version' <<<"$status")）。"
                return 0
                ;;
            error) die "Xray 启动失败：$(jq -r '.obj.xray.errorMsg' <<<"$status")" ;;
        esac
    done
    die "Xray 未能在 30 秒内启动，当前状态：$state"
}

fetch_links() {
    api GET /panel/api/inbounds/allLinks | jq -r '.obj[]?'
}

# 从面板证书中取一个域名，用于本机 HTTPS 访问面板
panel_cert_domain() {
    local cert domain
    cert="$("$XUI_BIN" setting -getCert true 2>/dev/null | awk -F': ' '/^cert:/ {print $2}' | tr -d '[:space:]' || true)"
    [ -n "$cert" ] && [ -r "$cert" ] || return 0
    domain="$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null |
        grep -o 'DNS:[^,[:space:]]*' | head -1 | cut -c5- || true)"
    # 通配符证书用任意子域名访问即可
    printf '%s' "${domain/#\*./vps-toolkit.}"
}
