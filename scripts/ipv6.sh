#!/usr/bin/env bash
# 开启 / 关闭系统 IPv6（sysctl），重启后保持
# 用法：bash scripts/ipv6.sh status|enable|disable

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

SYSCTL_FILE="${SYSCTL_FILE:-/etc/sysctl.d/99-vps-toolkit-ipv6.conf}"
PROC_IPV6="${PROC_IPV6:-/proc/sys/net/ipv6}"

ipv6_disabled() {
    [ "$(cat "$PROC_IPV6/conf/all/disable_ipv6" 2>/dev/null || echo 1)" = "1" ]
}

has_global_ipv4() {
    ip -4 addr show scope global 2>/dev/null | grep -q 'inet '
}

# 其他配置文件中也设置了 disable_ipv6 时，重启后可能覆盖本脚本的设置
other_sysctl_settings() {
    grep -rls 'disable_ipv6' /etc/sysctl.conf /etc/sysctl.d/ 2>/dev/null | grep -vx "$SYSCTL_FILE" || true
}

status() {
    local addrs others
    if [ ! -d "$PROC_IPV6" ]; then
        info "IPv6：内核未启用（可能在启动参数中关闭了 ipv6.disable=1）"
        return 0
    fi
    if ipv6_disabled; then
        info "IPv6：已关闭"
    else
        info "IPv6：已开启"
        addrs="$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/ {print "  " $2}')"
        if [ -n "$addrs" ]; then
            info "公网 IPv6 地址："
            printf '%s\n' "$addrs"
        else
            info "没有公网 IPv6 地址。"
        fi
    fi
    [ -f "$SYSCTL_FILE" ] && info "持久化配置：$SYSCTL_FILE"
    others="$(other_sysctl_settings)"
    if [ -n "$others" ]; then
        warn "以下文件中也设置了 disable_ipv6，重启后可能覆盖本设置："
        printf '  %s\n' $others >&2
    fi
}

ipv6_disable() {
    require_root
    [ -d "$PROC_IPV6" ] || {
        info "内核未启用 IPv6，无需关闭。"
        return 0
    }
    # 纯 IPv6 机器关闭后会失联
    has_global_ipv4 || die "本机没有公网 IPv4，关闭 IPv6 会导致无法连接，已取消。"

    # all 会作用于所有网卡（包括 lo），之后单独保留 lo，避免依赖 ::1 的本地服务出错
    cat >"$SYSCTL_FILE" <<CONF
# vps-toolkit：关闭 IPv6（保留本地回环 ::1）
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 0
CONF
    sysctl -p "$SYSCTL_FILE" >/dev/null
    info "已关闭 IPv6，重启后保持。"
    info "提示：节点的 IPv6 地址将不可用，客户端请使用 IPv4 或域名（解析到 IPv4）连接。"
}

ipv6_enable() {
    require_root
    [ -d "$PROC_IPV6" ] || die "内核未启用 IPv6（可能在启动参数中设置了 ipv6.disable=1），需修改 GRUB 后重启。"

    rm -f "$SYSCTL_FILE"
    sysctl -w net.ipv6.conf.all.disable_ipv6=0 >/dev/null
    sysctl -w net.ipv6.conf.default.disable_ipv6=0 >/dev/null
    info "已开启 IPv6。"
    if ! ip -6 addr show scope global 2>/dev/null | grep -q 'inet6'; then
        warn "暂未获取到公网 IPv6 地址。静态配置的地址可能需要重启网络服务或重启系统后才会恢复。"
    fi
    local others
    others="$(other_sysctl_settings)"
    if [ -n "$others" ]; then
        warn "以下文件中仍设置了 disable_ipv6，重启后可能再次关闭 IPv6，请检查："
        printf '  %s\n' $others >&2
    fi
}

case "${1:-status}" in
    status) status ;;
    enable) ipv6_enable ;;
    disable) ipv6_disable ;;
    *) die "用法：bash vps.sh ipv6 status|enable|disable" ;;
esac
