#!/usr/bin/env bash

node_inventory() { python3 "$ROOT_DIR/lib/monitor/nodes.py" "$MONITOR_DIR" "$@"; }

node_change() {
    node_inventory "$@" || return $?
    # Existing timers use a private toolkit snapshot; update it to honor paused nodes.
    install_renew_timer
}

select_node() {
    local rows count index
    rows="$(node_inventory list)"
    count="$(printf '%s' "$rows" | jq length)"
    if [ "$count" -eq 0 ]; then info "暂无节点；请先添加被监控节点。"; return 1; fi
    printf '\n  编号  节点名称 / 采集地址 / 状态\n'
    printf '%s' "$rows" | jq -r 'to_entries[] | "  \(.key + 1). \(.value.label)  ·  \(.value.address)  ·  \(if .value.paused then "已暂停" else "已登记" end)"'
    read -r -p "选择节点编号 [0 返回]: " index || return 1
    case "$index" in m|M) return 90 ;; q|Q) return 91 ;; esac
    [[ "$index" =~ ^[0-9]{1,6}$ ]] || { warn "请输入列表中的数字编号。"; return 1; }
    index=$((10#$index))
    [ "$index" -ne 0 ] || return 1
    [ "$index" -le "$count" ] || { warn "没有这个编号。"; return 1; }
    MONITOR_NODE_ID="$(printf '%s' "$rows" | jq -r --argjson i "$((index - 1))" '.[$i].id')"
    MONITOR_NODE_LABEL="$(printf '%s' "$rows" | jq -r --argjson i "$((index - 1))" '.[$i].label')"
}

nodes_list() {
    node_inventory list | jq -r 'if length == 0 then "暂无节点。" else to_entries[] | "\(.key + 1). \(.value.label)  ·  \(.value.address)  ·  \(if .value.paused then "已暂停" else "已登记（实际健康见检测）" end)" end'
}

node_details() {
    info "节点：$MONITOR_NODE_LABEL；内部编号：$MONITOR_NODE_ID"
    node_inventory list | jq --arg id "$MONITOR_NODE_ID" '.[] | select(.id == $id)'
    local result
    if result="$(prometheus_api /api/v1/targets)"; then
        printf '%s' "$result" | jq --arg label "$MONITOR_NODE_LABEL" '.data.activeTargets[] | select(.labels.node == $label) | {health,lastScrape,lastError}'
    else warn "Prometheus 不可达，以上仅为本地配置，不代表在线。"; fi
}

nodes_manage() {
    linux_root
    require_hub
    require_command jq
    local action label answer result=0 accent="" reset=""
    if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ -z "${NO_COLOR+x}" ]; then accent=$'\033[36m'; reset=$'\033[0m'; fi
    select_node || result=$?
    case "$result" in 0) ;; 90|91) return "$result" ;; *) return 0 ;; esac
    while true; do
        if [ -t 0 ] && [ -t 1 ] && [ "${TERM:-dumb}" != dumb ] && [ "${VPS_TOOLKIT_CLEAR:-1}" != 0 ]; then printf '\033[H\033[2J'; fi
        printf '\n%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$accent" "$reset"
        printf '节点管理  /  %s\n' "$MONITOR_NODE_LABEL"
        printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$accent" "$reset"
        cat <<'MENU'
  1. 查看配置与实时采集状态
  2. 修改显示名称
  3. 更换地址 / SSH 信息 / 重新部署
  4. 暂停资源采集
  5. 恢复资源采集
  6. 续签并同步节点证书
  7. 删除监控登记（保留远端服务与历史）
  0. 返回上级  |  m. 主菜单  |  q. 退出
MENU
        read -r -p "选择操作 [0-7]: " action || return 0
        case "$action" in
            1) node_details ;;
            2)
                read -r -p "新的显示名称: " label || return 0
                monitor_lock node_change rename "$MONITOR_NODE_ID" "$label" || continue
                MONITOR_NODE_LABEL="$(node_inventory list | jq -r --arg id "$MONITOR_NODE_ID" '.[] | select(.id == $id) | .label')"
                info "已更新名称；内部编号、证书和续期密钥保留。历史样本仍使用当时的名称。"
                ;;
            3) MONITOR_NODE_ID="$MONITOR_NODE_ID" bash "$ROOT_DIR/scripts/monitor.sh" deploy-node || warn "更新未完成，请检查上方输出。" ;;
            4) monitor_lock node_change pause "$MONITOR_NODE_ID" || continue; info "已暂停采集和自动续期；无需重启。" ;;
            5) monitor_lock node_change resume "$MONITOR_NODE_ID" || continue; info "已恢复采集和自动续期；目标发现通常在 30 秒内更新。" ;;
            6) MONITOR_NODE_ID="$MONITOR_NODE_ID" bash "$ROOT_DIR/scripts/monitor.sh" renew-node || warn "续期未完成。" ;;
            7) MONITOR_NODE_ID="$MONITOR_NODE_ID" bash "$ROOT_DIR/scripts/monitor.sh" remove-node || return 1
               if ! node_inventory list | jq -e --arg id "$MONITOR_NODE_ID" '.[] | select(.id == $id)' >/dev/null; then return 0; fi ;;
            0) return 0 ;;
            m|M) return 90 ;;
            q|Q) return 91 ;;
            *) warn "请输入 0～7。" ;;
        esac
        read -r -p "回车继续；m 主菜单；q 退出: " answer || return 0
        case "$answer" in m|M) return 90 ;; q|Q) return 91 ;; esac
    done
}
