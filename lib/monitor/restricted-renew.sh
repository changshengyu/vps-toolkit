#!/usr/bin/env bash
# Installed root-owned and invoked only by the dedicated forced-command SSH key.
# Client input is certificate data, never shell code or an installer/toolkit archive.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$ROOT_DIR/lib/common.sh"
require_root
MONITOR_DIR=/opt/vps-toolkit-monitor
NODE_CONFIG_DIR=/etc/vps-toolkit-monitor-node
NODE_DIR=/usr/local/lib/vps-toolkit-monitor
NODE_SERVICE=vps-toolkit-node-exporter
unset MONITOR_NODE_CONFIG_DIR MONITOR_PKI_DIR MONITOR_SYSTEMD_DIR
. "$ROOT_DIR/lib/monitor/tls.sh"
[ -f "$NODE_CONFIG_DIR/.vps-toolkit-managed" ] || die "未找到托管节点配置。"
IFS= read -r -n 64 RENEW_ACTION || die "缺少续期协议头。"
case "$RENEW_ACTION" in
    VPS_MONITOR_CHECK_V1)
        [ -s "$NODE_CONFIG_DIR/ca.pem" ] && [ -s "$NODE_CONFIG_DIR/node.json" ]
        printf 'VPS_MONITOR_RENEW_READY\n'
        exit 0
        ;;
    VPS_MONITOR_RENEW_V1) ;;
    *) die "仅支持监控证书续期协议。" ;;
esac
umask 077
RENEW_WORK="$(mktemp -d /tmp/vtm-renew.XXXXXXXX)"
RENEW_CHANGED=0
finish_renewal() {
    local code=$?
    trap - EXIT
    if [ "$code" -ne 0 ] && [ "$RENEW_CHANGED" -eq 1 ]; then
        cp -a "$RENEW_WORK/old-server.pem" "$NODE_CONFIG_DIR/server.pem"
        cp -a "$RENEW_WORK/old-server-key.pem" "$NODE_CONFIG_DIR/server-key.pem"
        systemctl restart "$NODE_SERVICE" || true
    fi
    rm -rf -- "$RENEW_WORK"
    exit "$code"
}
trap finish_renewal EXIT
timeout 30 head -c 1048577 > "$RENEW_WORK/node.tar.gz"
[ "$(wc -c < "$RENEW_WORK/node.tar.gz")" -le 1048576 ] || die "证书包过大。"
package_meta extract "$RENEW_WORK/node.tar.gz" "$RENEW_WORK/package" >/dev/null
# Never accept a new trust root, destination, port, firewall source or node identity through this key.
CURRENT_CA="$(openssl x509 -in "$NODE_CONFIG_DIR/ca.pem" -outform DER | sha256sum)"
NEW_CA="$(openssl x509 -in "$RENEW_WORK/package/ca.pem" -outform DER | sha256sum)"
[ "$CURRENT_CA" = "$NEW_CA" ] || die "续期不能更换 CA。"
[ "$(package_meta validate "$NODE_CONFIG_DIR/node.json" | jq -Sc .)" = "$(package_meta validate "$RENEW_WORK/package/node.json" | jq -Sc .)" ] || die "续期不能更改节点配置。"
validate_bundle_certificates "$RENEW_WORK/package"
cp -a "$NODE_CONFIG_DIR/server.pem" "$RENEW_WORK/old-server.pem"
cp -a "$NODE_CONFIG_DIR/server-key.pem" "$RENEW_WORK/old-server-key.pem"
RENEW_CHANGED=1
install -m 640 -o root -g vps-exporter "$RENEW_WORK/package/server-key.pem" "$NODE_CONFIG_DIR/server-key.pem"
install -m 640 -o root -g vps-exporter "$RENEW_WORK/package/server.pem" "$NODE_CONFIG_DIR/server.pem"
systemctl restart "$NODE_SERVICE"
sleep 1
systemctl is-active --quiet "$NODE_SERVICE"
printf 'VPS_MONITOR_RENEWED\n'
