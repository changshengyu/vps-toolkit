#!/usr/bin/env bash
# Standalone ISP HTTPS static site; other VPSes may use its domain as Reality target/SNI.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT_DIR/lib/common.sh"
case "${1:-help}" in
    help|-h|--help)
        printf '%s\n' '用法：vt sni-site install|access|check|page|logs|start|stop|restart|uninstall'
        exit 0
        ;;
esac
require_root
[ "$(uname -s)" = Linux ] || die "仅支持 Linux + systemd VPS。"
require_command systemctl
ensure_packages python3 curl
python3 "$ROOT_DIR/lib/sni/site.py" "$@"
