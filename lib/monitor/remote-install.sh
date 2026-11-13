#!/usr/bin/env bash
# Receives a trusted deployment archive over an authenticated, host-key-checked SSH connection.
# Both archives are constructed by our controller, never obtained from a remote HTTP upload.
set -euo pipefail
umask 077
REMOTE_WORK="$(mktemp -d /tmp/vtm-node.XXXXXXXX)"
trap 'rm -rf -- "$REMOTE_WORK"' EXIT
tar -xzf - -C "$REMOTE_WORK"
[ -s "$REMOTE_WORK/node.tar.gz" ] && [ -s "$REMOTE_WORK/toolkit.tar.gz" ]
mkdir "$REMOTE_WORK/toolkit"
tar -xzf "$REMOTE_WORK/toolkit.tar.gz" -C "$REMOTE_WORK/toolkit"
# The caller has explicitly authorized remote installation and transported its own CA via SSH.
export MONITOR_INSTALL_NONINTERACTIVE=1
export DEBIAN_FRONTEND=noninteractive
bash "$REMOTE_WORK/toolkit/scripts/monitor.sh" node-install --bundle "$REMOTE_WORK/node.tar.gz"
IFS= read -r RENEWAL_USER < "$REMOTE_WORK/renewal-user"
bash "$REMOTE_WORK/toolkit/scripts/monitor.sh" renewal-authorize "$RENEWAL_USER" "$REMOTE_WORK/renewal.pub"
