"""One-off SSH deployment protocol and DNS identity regression tests."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import unittest

from test_monitoring_tls import TLSFixture, package
from test_monitoring import ROOT


class DeploymentTests(TLSFixture):
    def config(self):
        return {"schema": 1, "node": {"schema": 1, "name": "isp-exit",
                "address": "node.example.test", "monitor_ip": "192.0.2.20", "port": 19100},
                "ssh": {"user": "root", "port": 2222, "identity_file": ""}, "auto_renew": False}

    def test_dns_identity_and_ssh_metadata_reject_injection(self):
        self.assertEqual(package.identity("Node.Example.Test."), {"kind": "DNS", "address": "node.example.test"})
        self.assertEqual(package.deployment(self.config())["node"]["address"], "node.example.test")
        for address in ["https://node.example.test", "node.example.test/x", "-oProxyCommand=x",
                        "node.example.test\nUser root", "999.1.2.3", "node..example.test"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                package.identity(address)
        for field, value in [("user", "root;reboot"), ("port", "22"), ("identity_file", "relative/key"),
                             ("identity_file", "/root/key\nProxyCommand")]:
            config = self.config()
            config["ssh"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                package.deployment(config)

    def test_domain_certificate_verifies_dns_san(self):
        self.make_certificates("node.example.test", "192.0.2.20")
        self.shell('validate_bundle_certificates "$PKI_DIR/nodes/isp-exit"')
        path = self.pki / "nodes/isp-exit/node.json"
        value = json.loads(path.read_text())
        value["address"] = "other.example.test"
        path.write_text(json.dumps(value))
        result = self.shell('validate_bundle_certificates "$PKI_DIR/nodes/isp-exit"', check=False)
        self.assertNotEqual(result.returncode, 0)

    def deployment_mocks(self, probe=True, authorized=True, install_success=True):
        self.make_certificates()
        (self.hub / "prometheus/https-targets").mkdir()
        self.env["DEPLOY_TEST_JSON"] = json.dumps(self.config())
        return '''
ensure_pki() { :; }
install_renew_timer() { printf 'timer installed\\n' >> "$CALL_LOG"; }
wait_node_scrape() { return 0; }
activate_https_job() { printf 'job activated\\n' >> "$CALL_LOG"; }
systemctl() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
ssh() {
    printf 'SSH %s\\n' "$*" >> "$CALL_LOG"
    case " $* " in
        *' -O exit '*) return 0 ;;
        *' -fNT '*) return 0 ;;
        *' monitor-renew-check '*)
            read -r protocol
            test "$protocol" = VPS_MONITOR_CHECK_V1
            ''' + ('printf VPS_MONITOR_RENEW_READY' if authorized else 'return 255') + '''
            ;;
        *) cat > "$TMPDIR/captured-payload.tar.gz"; return ''' + ("0" if install_success else "51") + ''' ;;
    esac
}
probe_https_node() { test -s "$PKI_DIR/nodes/$1/server-key.pem"; return ''' + ("0" if probe else "1") + '''; }
'''

    def test_failed_scrape_confirmation_keeps_target_renewal_and_legacy_record(self):
        mocks = self.deployment_mocks()
        legacy = self.hub / "prometheus/targets/isp-exit.json"
        legacy.write_text("old target")
        r = self.shell(mocks + '''
wait_node_scrape() { return 1; }
apply_deployment "$DEPLOY_TEST_JSON" interactive
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未确认", r.stderr)
        self.assertTrue((self.hub / "prometheus/https-targets/isp-exit.json").exists())
        self.assertTrue(json.loads((self.hub / "deploy/isp-exit.json").read_text())["auto_renew"])
        self.assertEqual(legacy.read_text(), "old target")
        self.assertIn("timer installed", self.log.read_text())

    def test_one_off_deployment_transports_snapshot_and_closes_connection(self):
        self.shell(self.deployment_mocks() + 'apply_deployment "$DEPLOY_TEST_JSON" interactive')
        target = self.hub / "prometheus/https-targets/isp-exit.json"
        self.assertEqual(json.loads(target.read_text())[0]["targets"], ["node.example.test:19100"])
        state = self.hub / "deploy/isp-exit.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertTrue(json.loads(state.read_text())["auto_renew"])
        self.assertNotIn("password", state.read_text())
        calls = self.log.read_text()
        self.assertIn("StrictHostKeyChecking=ask", calls)
        self.assertNotIn("StrictHostKeyChecking=no", calls)
        self.assertIn("IdentityAgent=none", calls)
        self.assertIn("-O exit", calls)
        self.assertNotIn("PRIVATE KEY", calls)
        with tarfile.open(self.path / "captured-payload.tar.gz") as outer:
            self.assertEqual(set(outer.getnames()), {"node.tar.gz", "toolkit.tar.gz", "renewal.pub", "renewal-user"})
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile("node.tar.gz").read())) as node:
                self.assertEqual(set(node.getnames()), package.FILES)
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile("toolkit.tar.gz").read())) as toolkit:
                self.assertIn("lib/monitor/deploy.sh", toolkit.getnames())
                self.assertIn("scripts/monitor.sh", toolkit.getnames())
                self.assertFalse(any("pki/" in name or "id_ed25519" in name for name in toolkit.getnames()))

    def test_bootstrap_without_a_private_key_still_enables_password_free_renewal(self):
        self.shell(self.deployment_mocks() + 'apply_deployment "$DEPLOY_TEST_JSON" interactive')
        state = json.loads((self.hub / "deploy/isp-exit.json").read_text())
        self.assertEqual(state["ssh"]["identity_file"], "")
        self.assertTrue(state["auto_renew"])
        self.assertTrue(Path(state["renewal_identity"]).is_file())

    def test_missing_restricted_key_authorization_prevents_registration(self):
        result = self.shell(self.deployment_mocks(authorized=False) + 'apply_deployment "$DEPLOY_TEST_JSON" interactive', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.hub / "deploy/isp-exit.json").exists())

    def test_failed_remote_install_preserves_target_and_closes_ssh(self):
        mocks = self.deployment_mocks(install_success=False)
        target = self.hub / "prometheus/https-targets/isp-exit.json"
        target.write_text("old target")
        old_key = (self.pki / "nodes/isp-exit/server-key.pem").read_bytes()
        result = self.shell(mocks + 'apply_deployment "$DEPLOY_TEST_JSON" interactive', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "old target")
        self.assertEqual((self.pki / "nodes/isp-exit/server-key.pem").read_bytes(), old_key)
        self.assertFalse((self.hub / "deploy/isp-exit.json").exists())
        self.assertIn("-O exit", self.log.read_text())

    def test_failed_https_probe_does_not_publish_attempted_configuration(self):
        result = self.shell(self.deployment_mocks(probe=False) + 'apply_deployment "$DEPLOY_TEST_JSON" interactive', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.hub / "deploy/isp-exit.json").exists())
        self.assertFalse((self.hub / "prometheus/https-targets/isp-exit.json").exists())
        self.assertIn("-O exit", self.log.read_text())

    def test_batch_mode_cannot_prompt_or_accept_new_host_keys(self):
        self.shell(self.deployment_mocks() + 'apply_deployment "$DEPLOY_TEST_JSON" batch')
        calls = self.log.read_text()
        self.assertIn("BatchMode=yes", calls)
        self.assertIn("StrictHostKeyChecking=yes", calls)
        self.assertNotIn("StrictHostKeyChecking=ask", calls)

    def test_wizard_applies_defaults_without_json_corruption(self):
        result = self.shell('''
linux_root() { :; }
require_hub() { :; }
ensure_packages() { :; }
confirm() { return 0; }
curl() { printf 192.0.2.20; }
package_meta() {
    if [ "$1" = resolve ]; then printf '["192.0.2.10"]'; else
        python3 "$ROOT_DIR/lib/monitor/package.py" "$@"
    fi
}
apply_deployment() { printf '%s' "$1" > "$TMPDIR/wizard-config.json"; }
deploy_node
''', "isp-exit\nnode.example.test\n\n\n\n\n\n")
        config = json.loads((self.path / "wizard-config.json").read_text())
        self.assertEqual(config["node"]["monitor_ip"], "192.0.2.20")
        self.assertEqual(config["ssh"]["port"], 22)

    def test_remote_command_is_posix_quoted_and_non_root_requires_sudo(self):
        root = shlex.split(self.shell('remote_install_command root').stdout)
        regular = shlex.split(self.shell('remote_install_command deploy').stdout)
        self.assertEqual(root[:2], ["bash", "-c"])
        self.assertEqual(regular[:4], ["sudo", "-n", "bash", "-c"])
        self.assertEqual(root[-1], (ROOT / "lib/monitor/remote-install.sh").read_text())

    def test_remote_protocol_installs_from_stdin_and_cleans_temporary_files(self):
        before = set(Path("/tmp").glob("vtm-node.*"))
        toolkit = io.BytesIO()
        stub = b'''#!/usr/bin/env bash
set -euo pipefail
test "$MONITOR_INSTALL_NONINTERACTIVE" = 1
case "$1" in
    node-install)
        test "$2" = --bundle
        test "$(cat "$3")" = TEST_NODE_BUNDLE
        ;;
    renewal-authorize)
        test "$2" = root
        test -s "$3"
        printf REMOTE_PROTOCOL_OK
        ;;
    *) exit 1 ;;
esac
'''
        with tarfile.open(fileobj=toolkit, mode="w:gz") as archive:
            info = tarfile.TarInfo("scripts/monitor.sh")
            info.size = len(stub)
            archive.addfile(info, io.BytesIO(stub))
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for name, data in [("toolkit.tar.gz", toolkit.getvalue()), ("node.tar.gz", b"TEST_NODE_BUNDLE"), ("renewal.pub", b"ssh-ed25519 TEST\n"), ("renewal-user", b"root\n")]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        result = subprocess.run(["bash", str(ROOT / "lib/monitor/remote-install.sh")],
                                input=payload.getvalue(), capture_output=True, check=True)
        self.assertEqual(result.stdout, b"REMOTE_PROTOCOL_OK")
        self.assertEqual(set(Path("/tmp").glob("vtm-node.*")), before)

    def test_stopped_monitoring_does_not_renew_or_start_anything(self):
        self.shell('''
linux_root() { :; }
require_hub() { :; }
ensure_packages() { :; }
compose() { return 0; }
cert_renew() { return 99; }
renew_due
''')

    def test_timer_snapshot_can_refresh_itself_without_copying_over_source_files(self):
        self.shell('''
systemctl() { :; }
install_renew_timer
ROOT_DIR="$MONITOR_DIR/toolkit"
install_renew_timer
''')
        self.assertTrue((self.hub / "toolkit/lib/monitor/deploy.sh").is_file())
        service = (self.units / "vps-toolkit-monitor-renew.service").read_text()
        self.assertIn("renew-due", service)

    def test_removing_node_also_removes_automatic_renewal_record(self):
        (self.hub / "prometheus/https-targets").mkdir()
        (self.hub / "prometheus/https-targets/isp-exit.json").write_text("target")
        (self.hub / "deploy").mkdir()
        (self.hub / "deploy/isp-exit.json").write_text(json.dumps(self.config()))
        self.shell('''
linux_root() { :; }
require_hub() { :; }
confirm() { return 0; }
remove_node
''', "isp-exit\n")
        self.assertFalse((self.hub / "deploy/isp-exit.json").exists())

    def test_renew_due_launches_only_eligible_nodes_in_separate_batch_process(self):
        config = self.config()
        config["auto_renew"] = True
        (self.hub / "deploy").mkdir()
        (self.hub / "deploy/isp-exit.json").write_text(json.dumps(config))
        self.shell('''
linux_root() { :; }
require_hub() { :; }
ensure_packages() { :; }
compose() { printf 'prometheus\\n'; }
openssl() { case "$*" in *nodes/isp-exit/server.pem*) return 1 ;; *) return 0 ;; esac; }
bash() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
renew_due
''')
        self.assertIn("renew-saved", self.log.read_text())
        self.assertIn("isp-exit.json", self.log.read_text())

    def test_revoke_renewal_key_preserves_unrelated_ssh_keys(self):
        self.node_config.mkdir()
        home = self.path / "account"
        (home / ".ssh").mkdir(parents=True)
        managed = 'from="192.0.2.20",restrict,command="fixed" ssh-ed25519 MANAGED'
        authorized = home / ".ssh/authorized_keys"
        authorized.write_text("ssh-ed25519 KEEP\n" + managed + "\n")
        (self.node_config / "renewal-authorized-line").write_text(managed + "\n")
        (self.node_config / "renewal-user").write_text("testuser\n")
        self.shell('''
getent() { printf 'testuser:x:1000:1000::%s/account:/bin/bash\\n' "$TMPDIR"; }
id() { printf 'testgroup\\n'; }
chown() { :; }
revoke_renewal_key
''')
        self.assertEqual(authorized.read_text(), "ssh-ed25519 KEEP\n")
        self.assertFalse((self.node_config / "renewal-user").exists())


if __name__ == "__main__":
    unittest.main()
