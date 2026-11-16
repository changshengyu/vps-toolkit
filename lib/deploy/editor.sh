# shellcheck shell=bash
# In-memory edits only; applying the plan or writing a preview happens afterwards.

panel_parameter_locked() {
    case "$1" in
        PANEL_PORT|PANEL_USER|PANEL_PASS|PANEL_PATH) panel_installed ;;
        *) return 1 ;;
    esac
}

parameter_used() {
    local var="$1" kind="$2"
    if [ "${CONFIG_ONLY:-0}" = 1 ] && [[ "$CONFIG_ONLY_PARAMS" != *" $var "* ]]; then return 1; fi
    if [[ "$kind" == ai* ]] && [ "${ENABLE_AI_ROUTE:-y}" != y ]; then return 1; fi
    return 0
}

refresh_edit_usage() {
    local spec var kind def
    for spec in "${PARAM_SPECS[@]}"; do
        var="$(spec_field "$spec" 1)" kind="$(spec_field "$spec" 5)" def="$(spec_field "$spec" 4)"
        if ! parameter_used "$var" "$kind"; then PARAM_SOURCE[$var]="未使用"; continue; fi
        if [ "${PARAM_SOURCE[$var]:-}" = 未使用 ]; then
            if [ -z "${!var}" ] && [ -n "$def" ]; then printf -v "$var" '%s' "$(resolve_default "$def")"; fi
            PARAM_SOURCE[$var]="已有值 / 默认值"
        fi
    done
}

edit_params() {
    local spec var kind value choice index desc hidden answer old_domain problem
    local rows=()
    while true; do
        refresh_edit_usage
        rows=()
        printf '\n==================== 修改部署参数 ====================\n'
        info "输入编号或参数名修改；回车保留原值；0 完成并查看计划；q 取消。"
        if panel_installed; then info "已有面板的端口、账号及访问路径由 3x-ui 管理，这里标为只读。"; fi
        for spec in "${PARAM_SPECS[@]}"; do
            var="$(spec_field "$spec" 1)" kind="$(spec_field "$spec" 5)"
            parameter_used "$var" "$kind" || continue
            rows+=("$spec")
            value="${!var}"
            is_secret_kind "$kind" "$var" && value="$(mask_value "$value")"
            desc=""
            panel_parameter_locked "$var" && desc=" [只读]"
            printf '  %2d. %-16s %s%s\n' "${#rows[@]}" "$var" "${value:-（空）}" "$desc"
        done
        read -r -p "选择参数 [0 完成 / q 取消]: " choice || return 1
        case "$choice" in 0) return 0 ;; q|Q) return 1 ;; esac
        index=-1
        if [[ "$choice" =~ ^[0-9]{1,3}$ ]]; then
            index=$((10#$choice - 1))
        else
            for ((index=0; index<${#rows[@]}; index++)); do
                var="$(spec_field "${rows[$index]}" 1)"
                [ "$choice" = "$var" ] || [ "${choice#--}" = "$(spec_field "${rows[$index]}" 2)" ] || continue
                break
            done
        fi
        if [ "$index" -lt 0 ] || [ "$index" -ge "${#rows[@]}" ]; then warn "请选择列表中的编号或参数名。"; continue; fi
        spec="${rows[$index]}"
        var="$(spec_field "$spec" 1)" desc="$(spec_field "$spec" 3)" kind="$(spec_field "$spec" 5)"
        if panel_parameter_locked "$var"; then warn "该值来自现有 3x-ui 面板；请在 3x-ui 管理中修改，不会假装修改后再覆盖。"; continue; fi
        hidden=0
        is_secret_kind "$kind" "$var" && hidden=1
        value="${!var}"
        old_domain="${REALITY_DOMAIN:-}"
        if ! prompt_value "$var" "$desc（:clear 清空，回车保留）" "$value" "$hidden"; then return 1; fi
        answer="${!var}"
        if [ "$answer" = :clear ]; then answer=""; fi
        printf -v "$var" '%s' "$answer"
        problem="$(check_param "$var" "$answer")"
        if [[ "$answer" == *$'\n'* || "$answer" == *$'\r'* ]]; then problem="参数不能包含换行。"; fi
        if is_required "$kind" && [ -z "$answer" ]; then problem="此参数为必填项，不能清空。"; fi
        if [ -n "$problem" ]; then
            warn "$problem"
            printf -v "$var" '%s' "$value"
            continue
        fi
        if [ "$answer" != "$value" ]; then PARAM_SOURCE[$var]="本次修改"; fi
        if [ "$var" = REALITY_DOMAIN ] && [ "$answer" != "$value" ] && [ "$REALITY_TARGET" = "$old_domain:443" ]; then
            if confirm "是否同时把 Reality target 更新为 $answer:443？"; then
                REALITY_TARGET="$answer:443"
                PARAM_SOURCE[REALITY_TARGET]="随 SNI 更新"
            fi
        fi
    done
}

validate_edited_params() {
    local spec var kind invalid=0
    for spec in "${PARAM_SPECS[@]}"; do
        var="$(spec_field "$spec" 1)" kind="$(spec_field "$spec" 5)"
        parameter_used "$var" "$kind" || continue
        if is_required "$kind" && [ -z "${!var}" ]; then warn "$var 不能为空。"; invalid=1; fi
    done
    [ "$invalid" -eq 0 ] || return 1
    if [ "${CONFIG_ONLY:-0}" = 1 ]; then validate_config_params; else validate_params; fi
}

prepare_deploy_params() {
    collect_params
    detect_existing_panel
    if [ "${EDIT_PARAMS:-0}" = 1 ]; then
        while true; do
            edit_params || { info "已取消，未应用本次修改。"; exit 0; }
            if (validate_edited_params); then break; fi
            warn "请在编辑器中修正以上参数，或输入 q 取消。"
        done
    elif [ "${CONFIG_ONLY:-0}" = 1 ]; then validate_config_params
    else validate_params; fi
    print_summary
}

save_config_state() {
    # Config-only must preserve UUIDs, private keys and unrelated deployment fields.
    local file="$1" key tmp
    local -A saved=()
    load_kv_file "$file" saved
    for key in $CONFIG_ONLY_PARAMS; do saved[$key]="${!key}"; done
    mkdir -p "$(dirname "$file")"
    tmp="$(mktemp "${file}.XXXXXX")"
    chmod 600 "$tmp"
    {
        printf '# vps-toolkit saved deployment parameters; updated routing parameters only.\n'
        for key in "${!saved[@]}"; do printf '%s=%s\n' "$key" "${saved[$key]}"; done
    } > "$tmp"
    mv "$tmp" "$file"
}
