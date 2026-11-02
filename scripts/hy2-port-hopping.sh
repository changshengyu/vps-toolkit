#!/usr/bin/env bash
# Hysteria2 端口跳跃：用 nftables 重定向端口，并通过 systemd 持久化
# 用法：bash scripts/hy2-port-hopping.sh [install|status|uninstall]
# 环境变量：IFACE=eth0 HY2_PORT=443 HOP_RANGE=20000-20100

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/common.sh
. "$ROOT_DIR/lib/common.sh"

HY2_PORT="${HY2_PORT:-443}"
HOP_RANGE="${HOP_RANGE:-20000-20100}"
IFACE="${IFACE:-}"

NFT_FILE="/etc/hy2jump.nft"
SERVICE_FILE="/etc/systemd/system/hy2jump.service"

detect_iface() {
    ip -4 route show default 2>/dev/null |
        awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}' || true
}

is_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$((10#$1))" -ge 1 ] && [ "$((10#$1))" -le 65535 ]
}

validate_config() {
    local start end

    is_port "$HY2_PORT" || die "端口无效：$HY2_PORT"

    if [[ ! "$HOP_RANGE" =~ ^([0-9]{1,5})-([0-9]{1,5})$ ]]; then
        die "端口范围无效：$HOP_RANGE（示例：20000-20100）"
    fi

    start="${BASH_REMATCH[1]}"
    end="${BASH_REMATCH[2]}"
    is_port "$start" && is_port "$end" && [ "$((10#$start))" -lt "$((10#$end))" ] ||
        die "端口范围无效：$HOP_RANGE（示例：20000-20100）"

    if [ "$((10#$HY2_PORT))" -ge "$((10#$start))" ] && [ "$((10#$HY2_PORT))" -le "$((10#$end))" ]; then
        die "Hysteria2 端口 $HY2_PORT 不能位于跳跃范围 $HOP_RANGE 内。"
    fi

    [[ "$IFACE" =~ ^[A-Za-z0-9_.:@-]+$ ]] || die "网卡名无效：$IFACE"
    ip link show "$IFACE" >/dev/null 2>&1 || die "网卡 $IFACE 不存在，请用 IFACE=网卡名 指定。"
}

install_nftables() {
    command -v nft >/dev/null 2>&1 && return 0

    info "正在安装 nftables..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y
        apt-get install -y nftables
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y nftables
    elif command -v yum >/dev/null 2>&1; then
        yum install -y nftables
    else
        die "无法自动安装 nftables，请手动安装后重试。"
    fi
}

allow_firewall_port() {
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -qw active; then
        ufw allow "$HY2_PORT/udp"
    fi

    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="$HY2_PORT/udp"
        firewall-cmd --reload
    fi
}

check_listening() {
    if ss -ulnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$HY2_PORT$"; then
        info "检查通过：UDP $HY2_PORT 正在监听。"
    else
        warn "未检测到 UDP $HY2_PORT 监听。如果入站由 3x-ui 管理，请确认它已启用。"
    fi
}

install_rules() {
    local nft_bin

    require_root
    require_command systemctl

    IFACE="${IFACE:-$(detect_iface)}"
    IFACE="${IFACE:-eth0}"
    validate_config

    install_nftables
    nft_bin="$(command -v nft)"

    # 删除早期手动创建的规则，避免重复
    "$nft_bin" delete table ip hy2jump 2>/dev/null || true

    cat >"$NFT_FILE" <<RULES
table inet hy2jump {}
delete table inet hy2jump

table inet hy2jump {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "$IFACE" udp dport $HOP_RANGE counter redirect to :$HY2_PORT
    }
}
RULES

    # 写入服务前先校验规则语法
    "$nft_bin" -c -f "$NFT_FILE"

    cat >"$SERVICE_FILE" <<SERVICE
[Unit]
Description=Hysteria2 port hopping rules
Wants=network-pre.target
Before=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$nft_bin -f $NFT_FILE
ExecStop=-$nft_bin delete table inet hy2jump

[Install]
WantedBy=multi-user.target
SERVICE

    systemctl daemon-reload
    systemctl enable hy2jump
    systemctl restart hy2jump

    allow_firewall_port

    info ""
    info "配置完成：$IFACE 上的 UDP $HOP_RANGE -> $HY2_PORT"
    info ""
    "$nft_bin" list table inet hy2jump
    info ""
    check_listening
    info "提醒：云厂商安全组仍需手动放行 UDP $HOP_RANGE。"
}

show_status() {
    require_root
    require_command systemctl

    info "服务状态：$(systemctl is-active hy2jump 2>/dev/null || true)"
    info "开机自启：$(systemctl is-enabled hy2jump 2>/dev/null || true)"

    if command -v nft >/dev/null 2>&1 && nft list table inet hy2jump 2>/dev/null; then
        return 0
    fi

    info "未找到端口跳跃规则。"
}

uninstall_rules() {
    require_root
    require_command systemctl

    systemctl disable --now hy2jump 2>/dev/null || true
    if command -v nft >/dev/null 2>&1; then
        nft delete table inet hy2jump 2>/dev/null || true
    fi
    rm -f "$NFT_FILE" "$SERVICE_FILE"
    systemctl daemon-reload
    info "已卸载端口跳跃规则。"
}

case "${1:-install}" in
    install) install_rules ;;
    status) show_status ;;
    uninstall) uninstall_rules ;;
    *) die "未知操作：$1（可用：install、status、uninstall）" ;;
esac
