"""Certificate, migration and firewall regressions without changing the host."""
import importlib.util
import io
import gzip
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from test_monitoring import MonitoringFixture, ROOT

spec = importlib.util.spec_from_file_location("monitor_package", ROOT / "lib/monitor/package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_compressed_package_has_a_decoded_size_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            archive = path / "large.tar.gz"
            archive.write_bytes(gzip.compress(b"\0" * 2097152))
            with self.assertRaises(ValueError):
                package.extract(archive, path / "unpacked")
            self.assertFalse((path / "unpacked").exists())

    def test_ipv4_and_ipv6_metadata(self):
        for address, source in [("192.0.2.10", "192.0.2.20"), ("2001:db8::10", "2001:db8::20")]:
            value = package.metadata({"schema": 1, "name": "isp-exit", "address": address,
                                      "monitor_ip": source, "port": 19100})
            self.assertEqual(value["address"], address)

    def test_metadata_rejects_mixed_families_injection_and_reserved_ports(self):
        base = {"schema": 1, "name": "isp-exit", "address": "192.0.2.10",
                "monitor_ip": "192.0.2.20", "port": 19100}
        for key, value in [("address", "1.2.3.4; reboot"), ("name", "../../etc"),
                           ("monitor_ip", "::1"), ("port", 443), ("port", True),
                           ("port", 65536), ("address", "fe80::1%eth0")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                package.metadata({**base, key: value})

    def test_archive_rejects_links_duplicates_and_traversal(self):
        for attack in ("../../escape", "link", "duplicate"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                archive = path / "node.tar.gz"
                with tarfile.open(archive, "w:gz") as bundle:
                    for name in sorted(package.FILES):
                        member = tarfile.TarInfo(name)
                        data = b"test"
                        member.size = len(data)
                        bundle.addfile(member, io.BytesIO(data))
                    member = tarfile.TarInfo("node.json" if attack == "duplicate" else attack)
                    member.size = 1
                    if attack == "link":
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                    bundle.addfile(member, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    package.extract(archive, path / "unpacked")
                self.assertFalse((path / "unpacked").exists())


class TLSFixture(MonitoringFixture):
    def setUp(self):
        super().setUp()
        self.pki = self.hub / "pki"
        self.node_config = self.path / "node-config"
        self.units = self.path / "units"
        self.units.mkdir()
        self.env.update(MONITOR_NODE_CONFIG_DIR=str(self.node_config),
                        MONITOR_SYSTEMD_DIR=str(self.units))
        # The installer targets Linux/OpenSSL, not macOS's older LibreSSL CLI.
        brew_openssl = Path("/opt/homebrew/opt/openssl@3/bin")
        if (brew_openssl / "openssl").is_file():
            self.env["PATH"] = str(brew_openssl) + os.pathsep + self.env["PATH"]

    def make_certificates(self, address="192.0.2.10", source="192.0.2.20"):
        self.shell('''
chown() { :; }
ensure_pki
mkdir -p "$PKI_DIR/nodes/isp-exit" "$MONITOR_DIR/packages"
package_meta create isp-exit "''' + address + '" "' + source + '''" 19100 > "$PKI_DIR/nodes/isp-exit/node.json"
build_node_package isp-exit
''')
        return self.hub / "packages/isp-exit.tar.gz"


class TLSTests(TLSFixture):
    def test_certificate_roles_san_and_bundle_separation(self):
        bundle = self.make_certificates()
        data = self.path / "unpacked"
        package.extract(bundle, data)
        self.assertEqual(set(p.name for p in data.iterdir()), package.FILES)
        self.assertEqual((bundle.stat().st_mode & 0o777), 0o600)
        self.shell('validate_bundle_certificates "$MONITOR_DIR/packages/../pki/nodes/isp-exit"')
        self.shell('openssl verify -purpose sslclient -CAfile "$PKI_DIR/ca.pem" "$MONITOR_DIR/prometheus/tls/client.pem"')
        wrong = self.shell('openssl verify -purpose sslclient -CAfile "$PKI_DIR/ca.pem" "$PKI_DIR/nodes/isp-exit/server.pem"', check=False)
        self.assertNotEqual(wrong.returncode, 0)
        ca_key = (self.pki / "ca-key.pem").read_bytes()
        client_key = (self.hub / "prometheus/tls/client-key.pem").read_bytes()
        self.assertNotIn(ca_key, bundle.read_bytes())
        self.assertNotIn(client_key, bundle.read_bytes())

    def test_wrong_ip_and_key_are_rejected(self):
        self.make_certificates()
        metadata = self.pki / "nodes/isp-exit/node.json"
        value = json.loads(metadata.read_text())
        value["address"] = "192.0.2.99"
        metadata.write_text(json.dumps(value))
        result = self.shell('validate_bundle_certificates "$PKI_DIR/nodes/isp-exit"', check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_https_target_supports_ipv6_and_is_readable(self):
        directory = self.pki / "nodes/isp-exit"
        directory.mkdir(parents=True)
        (self.hub / "prometheus/https-targets").mkdir()
        (directory / "node.json").write_text(json.dumps({"schema": 1, "name": "isp-exit",
            "address": "2001:db8::10", "monitor_ip": "2001:db8::20", "port": 19100}))
        self.shell('write_https_target isp-exit')
        target = self.hub / "prometheus/https-targets/isp-exit.json"
        self.assertEqual(json.loads(target.read_text())[0]["targets"], ["[2001:db8::10]:19100"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_firewall_is_scoped_and_service_requires_it(self):
        directory = self.path / "render"
        directory.mkdir()
        metadata = directory / "node.json"
        metadata.write_text(json.dumps({"schema": 1, "name": "isp-exit", "address": "192.0.2.10",
                                      "monitor_ip": "192.0.2.20", "port": 19100}))
        self.shell('nft() { :; }; render_node_config "$TMPDIR/render" "$TMPDIR/render/node.json"')
        firewall = (directory / "firewall.nft").read_text()
        self.assertIn("ip saddr 192.0.2.20 tcp dport 19100 accept", firewall)
        self.assertIn("tcp dport 19100 drop", firewall)
        self.assertNotIn("flush ruleset", firewall)
        service = (directory / "exporter.service").read_text()
        self.assertIn("Requires=vps-toolkit-monitor-firewall.service", service)
        self.assertIn("--web.listen-address=0.0.0.0:19100", service)
        self.assertIn("RequireAndVerifyClientCert", (directory / "web.yml").read_text())

    def migration_mocks(self, success=True):
        directory = self.pki / "nodes/isp-exit"
        directory.mkdir(parents=True)
        (directory / "node.json").write_text("{}")
        (self.hub / "prometheus/https-targets").mkdir()
        (self.hub / "prometheus/targets/isp-exit.json").write_text("legacy")
        (self.ssh / ".vps-toolkit-managed").touch()
        (self.ssh / "config.d/isp-exit").write_text("legacy SSH")
        return '''
linux_root() { :; }
require_hub() { :; }
ensure_packages() { :; }
ensure_pki() { :; }
confirm() { return 0; }
wait_node_scrape() { return 0; }
activate_https_job() { :; }
write_https_target() { printf 'https' > "$MONITOR_DIR/prometheus/https-targets/$1.json"; }
systemctl() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
''' + ('probe_https_node() { return 0; }\n' if success else 'probe_https_node() { return 1; }\n')

    def test_failed_tls_probe_preserves_legacy_target_and_tunnel(self):
        result = self.shell(self.migration_mocks(False) + 'add_https_node', "isp-exit\n", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.hub / "prometheus/targets/isp-exit.json").read_text(), "legacy")
        self.assertTrue((self.ssh / "config.d/isp-exit").exists())
        self.assertFalse((self.hub / "prometheus/https-targets/isp-exit.json").exists())
        self.assertFalse(self.log.exists())

    def test_successful_tls_migration_retires_legacy_tunnel(self):
        self.shell(self.migration_mocks(True) + 'add_https_node', "isp-exit\n")
        self.assertFalse((self.hub / "prometheus/targets/isp-exit.json").exists())
        self.assertFalse((self.ssh / "config.d/isp-exit").exists())
        self.assertTrue((self.hub / "prometheus/https-targets/isp-exit.json").exists())
        self.assertIn("disable --now vps-toolkit-monitor-tunnel@isp-exit", self.log.read_text())

    def test_invalid_prometheus_config_is_rolled_back(self):
        main = self.hub / "prometheus/prometheus.yml"
        main.write_text("old config\n")
        result = self.shell('''
enable_https_job() {
    printf 'bad config' > "$MONITOR_DIR/prometheus/prometheus.yml"
    mkdir -p "$MONITOR_DIR/prometheus/https"
    printf 'bad job' > "$MONITOR_DIR/prometheus/https/remote.yml"
}
compose() { return 1; }
activate_https_job
''', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(main.read_text(), "old config\n")
        self.assertFalse((self.hub / "prometheus/https/remote.yml").exists())

    def test_failed_node_restart_restores_previous_service(self):
        bundle = self.make_certificates()
        old_unit = self.units / "vps-toolkit-node-exporter.service"
        old_unit.write_text("old loopback unit\n")
        result = self.shell('''
confirm() { return 0; }
check_ports() { :; }
install_nft() { :; }
nft() { :; }
sleep() { :; }
install() {
    local args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o | -g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
systemctl() {
    printf '%s\\n' "$*" >> "$CALL_LOG"
    if [ "$*" = 'restart vps-toolkit-node-exporter' ]; then return 1; fi
}
install_node_bundle "$MONITOR_DIR/packages/isp-exit.tar.gz"
''', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(old_unit.read_text(), "old loopback unit\n")
        self.assertFalse(self.node_config.exists())
        self.assertIn("start vps-toolkit-node-exporter", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
