#!/usr/bin/env python3
"""Verify ControlMaster, trusted host keys and the deployment stream against real sshd.

The disposable SSH server invokes a harmless installer stub, never host systemd.
Real Node Exporter/Prometheus mTLS is tested separately.
"""
import json
import hashlib
import shutil
from pathlib import Path
import subprocess
import time
import uuid

from test_monitoring import ROOT
from test_monitoring_tls import TLSFixture


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    fixture = TLSFixture()
    fixture.setUp()
    server = "vt-provision-ssh-" + uuid.uuid4().hex[:10]
    try:
        fixture.make_certificates("127.0.0.1", "127.0.0.1")
        keys = fixture.path / "ssh-test"
        keys.mkdir()
        for name in ("client", "host"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / name)], check=True)
        (keys / "authorized_keys").write_bytes((keys / "client.pub").read_bytes())
        (keys / "sshd_config").write_text("""Port 22
ListenAddress 0.0.0.0
HostKey /root/test-host-key
AuthorizedKeysFile /root/.ssh/authorized_keys
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PidFile /tmp/test-sshd.pid
""")
        docker("run", "--detach", "--name", server, "--publish", "127.0.0.1::22",
               "--mount", f"type=bind,src={keys},dst=/test,readonly", "alpine:3.22", "sh", "-ec", """
apk add --no-cache openssh-server bash openssl jq python3 >/dev/null
[ -e /usr/bin/bash ] || ln -s /bin/bash /usr/bin/bash
printf '#!/bin/sh\nexit 0\n' > /usr/bin/systemctl
chmod 755 /usr/bin/systemctl
echo 'root:unused-test-only-password' | chpasswd
mkdir -p /root/.ssh /run/sshd
cp /test/authorized_keys /root/.ssh/authorized_keys
cp /test/host /root/test-host-key
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys /root/test-host-key
exec /usr/sbin/sshd -D -e -f /test/sshd_config
""")
        port = int(docker("port", server, "22/tcp").rsplit(":", 1)[1])
        public = " ".join((keys / "host.pub").read_text().split()[:2])
        known_hosts = fixture.path / "known_hosts"
        known_hosts.write_text(f"[127.0.0.1]:{port} {public}\n")
        ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={known_hosts}", "-o", "IdentitiesOnly=yes", "-i", str(keys / "client"),
               "-o", "ConnectTimeout=1", "-p", str(port), "root@127.0.0.1", "true"]
        for attempt in range(90):
            if subprocess.run(ssh, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            if attempt == 89:
                raise AssertionError("test SSH server failed to start")
            time.sleep(1)
        connection = subprocess.check_output(ssh[:-1] + ['printf "%s" "$SSH_CONNECTION"'], text=True)
        source_ip = connection.split()[0]
        stub = fixture.path / "stub/scripts"
        stub.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/monitor.sh", stub / "monitor-real.sh")
        for relative in ["lib/common.sh", "lib/monitor/package.py", "lib/monitor/tls.sh", "lib/monitor/deploy.sh",
                         "lib/monitor/renew.sh", "lib/monitor/tunnel.sh", "lib/monitor/cloudflare.py", "lib/monitor/restricted-renew.sh", "lib/monitor/remote-install.sh"]:
            target = fixture.path / "stub" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        (stub / "monitor.sh").write_text('''#!/usr/bin/env bash
set -euo pipefail
if [ "$1" != node-install ]; then exec bash "$(dirname "$0")/monitor-real.sh" "$@"; fi
test "$EUID" = 0
test "$MONITOR_INSTALL_NONINTERACTIVE" = 1
test "$2" = --bundle
mkdir -p /etc/vps-toolkit-monitor-node
python3 "$(dirname "$0")/../lib/monitor/package.py" extract "$3" /etc/vps-toolkit-monitor-node >/dev/null
touch /etc/vps-toolkit-monitor-node/.vps-toolkit-managed
addgroup -S vps-exporter
adduser -S -D -H -G vps-exporter vps-exporter
printf STREAM_INSTALLED > /tmp/vps-monitor-provisioned
''')
        (fixture.hub / "prometheus/https-targets").mkdir()
        fixture.env["SSH_INTEGRATION_CONFIG"] = json.dumps({"schema": 1,
            "node": {"schema": 1, "name": "isp-exit", "address": "127.0.0.1", "monitor_ip": source_ip, "port": 19100},
            "ssh": {"user": "root", "port": port, "identity_file": str(keys / "client")}, "auto_renew": False})
        fixture.shell('''
chown() { :; }
ssh() { command ssh -o "UserKnownHostsFile=$TMPDIR/known_hosts" "$@"; }
build_toolkit_archive() { COPYFILE_DISABLE=1 tar -czf "$1" -C "$TMPDIR/stub" scripts lib; }
probe_https_node() { return 0; }
activate_https_job() { :; }
install_renew_timer() { :; }
apply_deployment "$SSH_INTEGRATION_CONFIG" interactive
''')
        assert docker("exec", server, "cat", "/tmp/vps-monitor-provisioned") == "STREAM_INSTALLED"
        state = json.loads((fixture.hub / "deploy/isp-exit.json").read_text())
        assert state["auto_renew"] is True
        print("PASS: real SSH ControlMaster transmits the installer/certificate stream and exits", flush=True)
        # The bootstrap credential is deliberately removed. All further work must use the restricted key.
        (keys / "client").unlink()
        forbidden = subprocess.run(["ssh", "-F", "/dev/null", "-o", "BatchMode=yes",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "IdentitiesOnly=yes",
            "-i", state["renewal_identity"], "-p", str(port), "root@127.0.0.1",
            "touch /tmp/vps-monitor-forbidden"], stdin=subprocess.DEVNULL, capture_output=True)
        assert forbidden.returncode != 0
        assert subprocess.run(["docker", "exec", server, "test", "-e", "/tmp/vps-monitor-forbidden"]).returncode != 0
        print("PASS: dedicated renewal key cannot execute an arbitrary command", flush=True)
        before = docker("exec", server, "sha256sum", "/etc/vps-toolkit-monitor-node/server.pem").split()[0]
        fixture.shell('''
chown() { :; }
ssh() { command ssh -o "UserKnownHostsFile=$TMPDIR/known_hosts" "$@"; }
probe_https_node() { return 0; }
renew_managed_node "$DEPLOY_DIR/isp-exit.json"
''')
        after = docker("exec", server, "sha256sum", "/etc/vps-toolkit-monitor-node/server.pem").split()[0]
        assert before != after
        assert after == hashlib.sha256((fixture.pki / "nodes/isp-exit/server.pem").read_bytes()).hexdigest()
        print("PASS: certificates renew and synchronize without the bootstrap password/key", flush=True)
        result = fixture.shell('''
chown() { :; }
ssh() { command ssh -o "UserKnownHostsFile=$TMPDIR/known_hosts" "$@"; }
mkdir "$TMPDIR/wrong-node"
jq '.port=19101' "$PKI_DIR/nodes/isp-exit/node.json" > "$TMPDIR/wrong-node/node.json"
build_node_package isp-exit "$TMPDIR/wrong-node" "$TMPDIR/wrong-node.tar.gz" >/dev/null
renewal_ssh "$DEPLOY_DIR/isp-exit.json" renew "$TMPDIR/wrong-node.tar.gz"
''', check=False)
        assert result.returncode != 0
        assert after == docker("exec", server, "sha256sum", "/etc/vps-toolkit-monitor-node/server.pem").split()[0]
        print("PASS: restricted renewal rejects changes to node identity, port and source configuration", flush=True)
    except Exception:
        subprocess.run(["docker", "logs", "--tail=30", server], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", server], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
