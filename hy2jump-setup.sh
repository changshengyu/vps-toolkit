#!/usr/bin/env bash
# Hysteria2 端口跳跃持久化脚本
# 用法：sudo bash hy2jump-setup.sh
# 卸载：sudo bash hy2jump-setup.sh uninstall
# 可选环境变量：IFACE=ens3 HY2_PORT=443 HOP_RANGE=20000-20100

set -euo pipefail

HY2_PORT="${HY2_PORT:-443}"
HOP_RANGE="${HOP_RANGE:-20000-20100}"
DEFAULT_IFACE="$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}')"
IFACE="${IFACE:-${DEFAULT_IFACE:-eth0}}"

NFT_FILE="/etc/hy2jump.nft"
SERVICE_FILE="/etc/systemd/system/hy2jump.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：请使用 root 运行。" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "错误：未检测到 systemd，此脚本不适用于当前系统。" >&2
    exit 1
fi

if [ "${1:-}" = "uninstall" ]; then
    systemctl disable --now hy2jump 2>/dev/null || true
    nft delete table inet hy2jump 2>/dev/null || true
    rm -f "$NFT_FILE" "$SERVICE_FILE"
    systemctl daemon-reload
    echo "已卸载端口跳跃规则。"
    exit 0
fi

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    echo "错误：网卡 $IFACE 不存在，请用 IFACE=网卡名 指定。" >&2
    exit 1
fi

if ! command -v nft >/dev/null 2>&1; then
    echo "正在安装 nftables..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y
        apt-get install -y nftables
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y nftables
    elif command -v yum >/dev/null 2>&1; then
        yum install -y nftables
    else
        echo "错误：无法自动安装 nftables，请手动安装后重试。" >&2
        exit 1
    fi
fi

NFT_BIN="$(command -v nft)"

# 删除旧版手动规则，避免重复
"$NFT_BIN" delete table ip hy2jump 2>/dev/null || true

cat > "$NFT_FILE" <<EOF
table inet hy2jump {}
delete table inet hy2jump

table inet hy2jump {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "$IFACE" udp dport $HOP_RANGE counter redirect to :$HY2_PORT
    }
}
EOF

# 写入服务前先校验规则语法
"$NFT_BIN" -c -f "$NFT_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hysteria2 port hopping rules
Wants=network-pre.target
Before=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$NFT_BIN -f $NFT_FILE
ExecStop=-$NFT_BIN delete table inet hy2jump

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hy2jump
systemctl restart hy2jump

# 本机防火墙只需放行重定向后的 Hysteria2 端口
if command -v ufw >/dev/null 2>&1 && ufw status | grep -qw active; then
    ufw allow "$HY2_PORT/udp"
fi

if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$HY2_PORT/udp"
    firewall-cmd --reload
fi

echo
echo "配置完成：$IFACE 上的 UDP $HOP_RANGE -> $HY2_PORT"
echo
"$NFT_BIN" list table inet hy2jump
echo

if ss -ulnp 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$HY2_PORT$"; then
    echo "检查通过：UDP $HY2_PORT 正在监听。"
else
    echo "警告：未检测到 UDP $HY2_PORT 监听，请确认 Hysteria2 已启动。"
fi

echo "提醒：云厂商安全组仍需手动放行 UDP $HOP_RANGE。"
