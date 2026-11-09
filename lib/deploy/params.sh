# shellcheck shell=bash
# 一键部署：参数定义、收集与校验
# 读取优先级：命令行参数 > 环境变量 > 配置文件 > 上次生成的状态 > 交互输入 / 默认值

# 格式：变量名|命令行参数|说明|默认值|类型
# 默认值以 @ 开头表示自动生成。
# 类型：req 必填 / secret 必填且隐藏输入 / opt 可选 / ai、ai_secret 仅在启用 AI 分流时必填
PARAM_SPECS=(
    "REALITY_DOMAIN|reality-domain|Reality 的 SNI 域名||req"
    "REALITY_TARGET|reality-target|Reality target（域名:端口）|@reality_target|opt"
    "HY2_DOMAIN|hy2-domain|hy2 证书域名（同时作为面板证书）||req"
    "CDN_DOMAIN|cdn-domain|CDN（WS+TLS）域名||req"
    "CF_TOKEN|cf-token|Cloudflare API Token（Zone:DNS:Edit）||secret"
    "HY2_CERT_MODE|hy2-cert-mode|hy2 证书验证方式（http 或 dns）|http|opt"
    "ACME_EMAIL|acme-email|ACME 注册邮箱（可留空）||opt"
    "NODE_NAME|node-name|节点备注前缀|@node_name|opt"
    "ENABLE_AI_ROUTE|ai-route|AI 网站走六六云 SS 出站（y/n）|y|opt"
    "SS_ISP_ADDR|ss-addr|六六云 SS 地址||ai"
    "SS_ISP_PORT|ss-port|六六云 SS 端口||ai"
    "SS_ISP_PASS|ss-pass|六六云 SS 密码||ai_secret"
    "SS_ISP_METHOD|ss-method|六六云 SS 加密方式|2022-blake3-aes-256-gcm|ai_opt"
    "PANEL_PORT|panel-port|面板端口|@rand_port|opt"
    "PANEL_USER|panel-user|面板用户名|@rand_str10|opt"
    "PANEL_PASS|panel-pass|面板密码|@rand_str16|opt"
    "PANEL_PATH|panel-path|面板访问路径|@rand_str18|opt"
    "HY2_PORT|hy2-port|hy2 端口（UDP）|443|opt"
    "HOP_RANGE|hop-range|hy2 端口跳跃范围|20000-20100|opt"
    "REALITY_PORT|reality-port|Reality+Vision 端口（TCP）|443|opt"
    "XHTTP_PORT|xhttp-port|Reality+XHTTP 端口（TCP）|30115|opt"
    "CDN_PORT|cdn-port|WS+TLS 端口（TCP）|8443|opt"
    "CDN_WS_PATH|ws-path|WS 路径|@rand_path|opt"
    "XHTTP_PATH|xhttp-path|XHTTP 路径|@rand_path|opt"
    "CLIENT_EMAIL|client-email|客户端标识（email）|@rand_str10|opt"
    "GEODATA_CRON|geodata-cron|geodata 自动更新时间（cron，UTC；off 关闭）|0 20 * * *|opt"
)

# 摘要中只显示开头几位的参数
SECRET_PARAMS=" CF_TOKEN SS_ISP_PASS PANEL_PASS "

declare -A PARAM_CLI=()    # 命令行传入
declare -A PARAM_ENV=()    # 运行前已存在的环境变量
declare -A PARAM_FILE=()   # 配置文件
declare -A STATE=()        # 状态文件（上次生成的值）
declare -A PARAM_SOURCE=() # 每个参数的最终来源，用于摘要

spec_field() {
    # 用法：spec_field 规格 序号（1 起）
    local IFS='|'
    # shellcheck disable=SC2206
    local parts=($1)
    printf '%s' "${parts[$(($2 - 1))]:-}"
}

# 按命令行参数名查找变量名
var_for_flag() {
    local spec
    for spec in "${PARAM_SPECS[@]}"; do
        if [ "$(spec_field "$spec" 2)" = "$1" ]; then
            spec_field "$spec" 1
            return 0
        fi
    done
    return 1
}

# 在加载任何文件前记录环境变量
capture_env_params() {
    local spec var
    for spec in "${PARAM_SPECS[@]}"; do
        var="$(spec_field "$spec" 1)"
        if [ -n "${!var+x}" ]; then
            PARAM_ENV[$var]="${!var}"
        fi
    done
}

# 读取 KEY=VALUE 文件，不执行其中的任何代码
# 用法：load_kv_file 文件 关联数组名
load_kv_file() {
    local file="$1" line key val
    local -n __map="$2"

    [ -r "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ ! "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            warn "忽略无法识别的行（$file）：$line"
            continue
        fi
        key="${BASH_REMATCH[2]}"
        val="${BASH_REMATCH[3]}"
        if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
            val="${BASH_REMATCH[1]}"
        fi
        __map[$key]="$val"
    done <"$file"
}

rand_str() {
    local len="$1" out=""
    while [ "${#out}" -lt "$len" ]; do
        out+="$(openssl rand -base64 48 | tr -dc 'a-zA-Z0-9')"
    done
    printf '%s' "${out:0:$len}"
}

rand_lower() {
    local len="$1" out=""
    while [ "${#out}" -lt "$len" ]; do
        out+="$(openssl rand -base64 48 | tr -dc 'a-z0-9')"
    done
    printf '%s' "${out:0:$len}"
}

port_in_use() {
    command -v ss >/dev/null 2>&1 || return 1
    ss -tulnH 2>/dev/null | awk '{print $5}' | grep -Eq "[:.]$1$"
}

rand_port() {
    local port i
    for i in $(seq 1 50); do
        port=$((10000 + $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 50000))
        # 避开默认跳跃范围和常用端口
        if [ "$port" -ge 20000 ] && [ "$port" -le 20100 ]; then continue; fi
        case " 30115 8443 " in *" $port "*) continue ;; esac
        port_in_use "$port" && continue
        printf '%s' "$port"
        return 0
    done
    die "无法生成可用的随机端口。"
}

# 根据默认值描述生成实际值
resolve_default() {
    case "$1" in
        @reality_target) printf '%s:443' "${REALITY_DOMAIN:-}" ;;
        @node_name) printf '%s' "${HY2_DOMAIN%%.*}" ;;
        @rand_port) rand_port ;;
        @rand_str10) rand_str 10 ;;
        @rand_str16) rand_str 16 ;;
        @rand_str18) rand_str 18 ;;
        @rand_path) printf '/%s' "$(rand_lower 12)" ;;
        *) printf '%s' "$1" ;;
    esac
}

mask_value() {
    local v="$1"
    if [ -z "$v" ]; then
        printf '（空）'
    elif [ "${#v}" -le 6 ]; then
        printf '***'
    else
        printf '%s***' "${v:0:3}"
    fi
}

is_required() {
    case "$1" in
        req | secret) return 0 ;;
        ai | ai_secret) [ "${ENABLE_AI_ROUTE:-y}" = "y" ] ;;
        *) return 1 ;;
    esac
}

is_secret_kind() {
    case "$1" in secret | ai_secret) return 0 ;; esac
    case "$SECRET_PARAMS" in *" $2 "*) return 0 ;; esac
    return 1
}

prompt_value() {
    # 用法：prompt_value 变量名 说明 默认值 是否隐藏
    local var="$1" desc="$2" def="$3" hidden="$4" answer="" shown

    if [ -n "$def" ]; then
        if [ "$hidden" = "1" ]; then shown="$(mask_value "$def")"; else shown="$def"; fi
        desc="$desc [$shown]"
    fi

    if [ "$hidden" = "1" ]; then
        read -r -s -p "$desc: " answer || true
        printf '\n' >&2
    else
        read -r -p "$desc: " answer || true
    fi
    printf -v "$var" '%s' "${answer:-$def}"
}

# 逐项确定参数值
collect_params() {
    local spec var flag desc def kind value source hidden missing=()

    for spec in "${PARAM_SPECS[@]}"; do
        var="$(spec_field "$spec" 1)"
        flag="$(spec_field "$spec" 2)"
        desc="$(spec_field "$spec" 3)"
        def="$(spec_field "$spec" 4)"
        kind="$(spec_field "$spec" 5)"
        value=""
        source=""

        if [ -n "${PARAM_CLI[$var]+x}" ]; then
            value="${PARAM_CLI[$var]}" source="命令行"
        elif [ -n "${PARAM_ENV[$var]+x}" ]; then
            value="${PARAM_ENV[$var]}" source="环境变量"
        elif [ -n "${PARAM_FILE[$var]+x}" ]; then
            value="${PARAM_FILE[$var]}" source="配置文件"
        elif [ -n "${STATE[$var]+x}" ] && [ "$REGEN" != "1" ]; then
            value="${STATE[$var]}" source="上次部署"
        fi

        # 未启用 AI 分流时跳过 SS 参数
        if [[ "$kind" == ai* ]] && [ "${ENABLE_AI_ROUTE:-y}" != "y" ]; then
            printf -v "$var" '%s' "$value"
            PARAM_SOURCE[$var]="未使用"
            continue
        fi

        hidden=0
        is_secret_kind "$kind" "$var" && hidden=1

        if [ -z "$source" ] || [ "$ASK_ALL" = "1" ]; then
            [ -z "$value" ] && value="$(resolve_default "$def")"
            if [ "$INTERACTIVE" = "1" ] && { [ "$ASK_ALL" = "1" ] || { is_required "$kind" && [ -z "$value" ]; }; }; then
                prompt_value "$var" "$desc" "$value" "$hidden"
                value="${!var}"
                [ -n "$source" ] || source="交互输入"
            elif [ -z "$source" ]; then
                if [[ "$def" == @* ]]; then source="自动生成"; else source="默认值"; fi
            fi
        fi

        if is_required "$kind" && [ -z "$value" ]; then
            missing+=("--$flag（$desc）")
        fi

        printf -v "$var" '%s' "$value"
        PARAM_SOURCE[$var]="$source"
    done

    if [ "${#missing[@]}" -gt 0 ]; then
        warn "缺少必填参数："
        printf '  %s\n' "${missing[@]}" >&2
        die "请通过命令行参数、环境变量或配置文件提供，或在终端中运行以便交互输入。"
    fi
}

is_domain() {
    [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

is_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$((10#$1))" -ge 1 ] && [ "$((10#$1))" -le 65535 ]
}

# 粗略检查 5 段 cron 表达式，准确校验由面板和 Xray 完成
is_cron() {
    [[ "$1" =~ ^[0-9*/,-]+([[:space:]]+[0-9A-Za-z*/,-]+){4}$ ]]
}

validate_params() {
    local errors=() p start end

    for p in REALITY_DOMAIN HY2_DOMAIN CDN_DOMAIN; do
        is_domain "${!p}" || errors+=("$p 不是有效域名：${!p}")
    done

    if [[ "$REALITY_TARGET" =~ ^(.+):([0-9]{1,5})$ ]]; then
        # is_domain 会覆盖 BASH_REMATCH，先保存
        local target_host="${BASH_REMATCH[1]}" target_port="${BASH_REMATCH[2]}"
        is_domain "$target_host" || errors+=("REALITY_TARGET 的主机不是有效域名：$REALITY_TARGET")
        is_port "$target_port" || errors+=("REALITY_TARGET 的端口无效：$REALITY_TARGET")
    else
        errors+=("REALITY_TARGET 格式应为 域名:端口：$REALITY_TARGET")
    fi

    case "$HY2_CERT_MODE" in http | dns) ;; *) errors+=("HY2_CERT_MODE 只能是 http 或 dns：$HY2_CERT_MODE") ;; esac
    case "$ENABLE_AI_ROUTE" in y | n) ;; *) errors+=("ENABLE_AI_ROUTE 只能是 y 或 n：$ENABLE_AI_ROUTE") ;; esac

    for p in PANEL_PORT HY2_PORT REALITY_PORT XHTTP_PORT CDN_PORT; do
        is_port "${!p}" || errors+=("$p 不是有效端口：${!p}")
    done

    if [ "$ENABLE_AI_ROUTE" = "y" ]; then
        is_port "$SS_ISP_PORT" || errors+=("SS_ISP_PORT 不是有效端口：$SS_ISP_PORT")
        [[ "$SS_ISP_ADDR" =~ ^[A-Za-z0-9.:-]+$ ]] || errors+=("SS_ISP_ADDR 格式无效：$SS_ISP_ADDR")
    fi

    # TCP 端口不能重复（hy2 用 UDP，可以和 Reality 共用 443）
    local tcp_ports="$PANEL_PORT $REALITY_PORT $XHTTP_PORT $CDN_PORT"
    if [ "$(tr ' ' '\n' <<<"$tcp_ports" | sort | uniq -d)" != "" ]; then
        errors+=("TCP 端口有重复：面板 $PANEL_PORT / Reality $REALITY_PORT / XHTTP $XHTTP_PORT / CDN $CDN_PORT")
    fi
    if [ "$HY2_CERT_MODE" = "http" ] && [ "$PANEL_PORT" = "80" ]; then
        errors+=("http 验证需要占用 TCP 80，面板端口不能是 80")
    fi

    if [[ "$HOP_RANGE" =~ ^([0-9]{1,5})-([0-9]{1,5})$ ]]; then
        start="${BASH_REMATCH[1]}" end="${BASH_REMATCH[2]}"
        if ! is_port "$start" || ! is_port "$end" || [ "$((10#$start))" -ge "$((10#$end))" ]; then
            errors+=("HOP_RANGE 无效：$HOP_RANGE")
        elif is_port "$HY2_PORT" && [ "$((10#$HY2_PORT))" -ge "$((10#$start))" ] && [ "$((10#$HY2_PORT))" -le "$((10#$end))" ]; then
            errors+=("hy2 端口 $HY2_PORT 不能位于跳跃范围 $HOP_RANGE 内")
        fi
    else
        errors+=("HOP_RANGE 格式应为 起始-结束：$HOP_RANGE")
    fi

    for p in CDN_WS_PATH XHTTP_PATH; do
        [[ "${!p}" =~ ^/[A-Za-z0-9._~/-]*$ ]] || errors+=("$p 必须以 / 开头，且只含字母、数字和 ._~-/：${!p}")
    done
    # 面板已安装时，端口和路径取自现有面板，账号密码不使用
    if [ "${PANEL_INSTALLED:-0}" != "1" ]; then
        [[ "$PANEL_PATH" =~ ^[A-Za-z0-9._~-]+$ ]] || errors+=("PANEL_PATH 只能包含字母、数字和 ._~-：$PANEL_PATH")
        [[ "$PANEL_USER" =~ ^[A-Za-z0-9._-]+$ ]] || errors+=("PANEL_USER 只能包含字母、数字和 ._-：$PANEL_USER")
        [ "${#PANEL_PASS}" -ge 8 ] || errors+=("PANEL_PASS 至少 8 位")
    fi
    [[ "$CLIENT_EMAIL" =~ ^[A-Za-z0-9._@-]+$ ]] || errors+=("CLIENT_EMAIL 只能包含字母、数字和 ._@-：$CLIENT_EMAIL")
    [[ "$NODE_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || errors+=("NODE_NAME 只能包含字母、数字和 ._-：$NODE_NAME")

    if [ "$GEODATA_CRON" != "off" ] && [ -n "$GEODATA_CRON" ] && ! is_cron "$GEODATA_CRON"; then
        errors+=("GEODATA_CRON 应为 5 段 cron 表达式（如 \"0 20 * * *\"）或 off：$GEODATA_CRON")
    fi

    if [ "${#errors[@]}" -gt 0 ]; then
        warn "参数校验未通过："
        printf '  - %s\n' "${errors[@]}" >&2
        die "请修正后重试。"
    fi

    # 非致命提醒
    case " 443 2053 2083 2087 2096 8443 " in
        *" $CDN_PORT "*) ;;
        *) warn "CDN 端口 $CDN_PORT 不在 Cloudflare 支持的 HTTPS 端口内（443/2053/2083/2087/2096/8443），开小黄云后将无法连接。" ;;
    esac
    if [ "$REALITY_DOMAIN" != "${REALITY_TARGET%:*}" ]; then
        warn "Reality 的 SNI（$REALITY_DOMAIN）和 target 主机（${REALITY_TARGET%:*}）不一致，请确认 target 的证书包含该 SNI。"
    fi
}

print_summary() {
    local spec var desc value src kind

    info ""
    info "==================== 部署参数 ===================="
    for spec in "${PARAM_SPECS[@]}"; do
        var="$(spec_field "$spec" 1)"
        desc="$(spec_field "$spec" 3)"
        kind="$(spec_field "$spec" 5)"
        src="${PARAM_SOURCE[$var]:-}"
        [ "$src" = "未使用" ] && continue
        value="${!var}"
        is_secret_kind "$kind" "$var" && value="$(mask_value "$value")"
        printf '  %-16s %-28s （%s）\n' "$var" "${value:-（空）}" "$src"
    done
    info "=================================================="
}
