#!/usr/bin/env bash
# Run in CI with sudo. unshare ensures no host firewall or routes are modified.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT_DIR/scripts/monitor.sh"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEST_DIR"' EXIT
printf '%s\n' '{"schema":1,"name":"test","address":"192.0.2.10","monitor_ip":"192.0.2.20","port":19100}' > "$TEST_DIR/node.json"
render_node_config "$TEST_DIR" "$TEST_DIR/node.json"
unshare --net bash -s -- "$TEST_DIR/firewall.nft" <<'NAMESPACE'
set -euo pipefail
ip link set lo up
ip address add 192.0.2.10/32 dev lo
ip address add 192.0.2.20/32 dev lo
ip address add 192.0.2.21/32 dev lo
nft -c -f "$1"
nft -f "$1"
python3 - <<'PY'
import socket
server = socket.socket()
server.bind(('0.0.0.0', 19100))
server.listen()
allowed = socket.socket()
allowed.settimeout(2)
allowed.bind(('192.0.2.20', 0))
allowed.connect(('192.0.2.10', 19100))
accepted, _ = server.accept()
accepted.close()
allowed.close()
denied = socket.socket()
denied.settimeout(1)
denied.bind(('192.0.2.21', 0))
try:
    denied.connect(('192.0.2.10', 19100))
except (TimeoutError, socket.timeout):
    print('PASS: source IP restriction accepts monitoring IP and drops other IPs')
else:
    raise AssertionError('unexpected source was accepted')
finally:
    denied.close()
    server.close()
PY
NAMESPACE
