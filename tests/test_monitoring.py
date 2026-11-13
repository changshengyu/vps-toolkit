"""Offline regression tests; never run installers or real SSH/systemd commands."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/monitor.sh"


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.hub = self.path / "hub"
        self.ssh = self.path / "ssh"
        (self.hub / "prometheus/targets").mkdir(parents=True)
        (self.ssh / "config.d").mkdir(parents=True)
        self.log = self.path / "calls"
        self.env = {
            **os.environ,
            "MONITOR_DIR": str(self.hub),
            "MONITOR_SSH_DIR": str(self.ssh),
            "CALL_LOG": str(self.log),
            "MONITOR_SCRIPT": str(SCRIPT),
            "TMPDIR": str(self.path),
        }

    def shell(self, code, stdin="", check=True):
        result = subprocess.run(
            ["bash", "-c", '. "$MONITOR_SCRIPT"\n' + code],
            env=self.env, input=stdin, text=True, errors="replace", capture_output=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_help_and_cli_forwarding(self):
        help_result = subprocess.run(
            ["bash", str(ROOT / "vps.sh"), "help"], text=True, capture_output=True, check=True
        )
        self.assertIn("monitor", help_result.stdout)
        # Run the real dispatcher against a harmless child script.
        repo = self.path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "lib").mkdir()
        shutil.copy(ROOT / "vps.sh", repo / "vps.sh")
        shutil.copy(ROOT / "lib/common.sh", repo / "lib/common.sh")
        (repo / "scripts/monitor.sh").write_text('printf "%s\\n" "$@"\n')
        args = ["node-install", "--public-key", "ssh-ed25519 PUBLIC KEY"]
        r = subprocess.run(["bash", str(repo / "vps.sh"), "monitor", *args],
                           text=True, capture_output=True, check=True)
        self.assertEqual(r.stdout.splitlines(), args)

    def test_new_menu_entry_preserves_existing_numbers(self):
        r = subprocess.run(["bash", str(ROOT / "vps.sh")], input="7\n0\n0\n",
                           text=True, capture_output=True, env=self.env, check=True)
        self.assertIn("6. 安装快捷命令 vt", r.stdout)
        self.assertIn("7. 监控管理", r.stdout)
        self.assertIn("本机作为被监控节点", r.stdout)

    def test_rejects_path_traversal_and_config_injection(self):
        self.shell('''
valid_name isp-exit
valid_port 00022
valid_host 2001:db8::1
valid_user vps-monitor
! valid_name ../../etc/passwd
! valid_name 'bad/name'
! valid_host '-oProxyCommand=evil'
! valid_host $'host\\nUser root'
! valid_user 'root;id'
! valid_port 65536
! valid_port 00000
''')
        r = self.shell('write_target ../../escape 19101', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list((self.hub / "prometheus/targets").iterdir()), [])

    def test_target_is_valid_json_and_readable_to_container(self):
        self.shell('write_target isp-exit 19101')
        target = self.hub / "prometheus/targets/isp-exit.json"
        self.assertEqual(json.loads(target.read_text()), [
            {"targets": ["127.0.0.1:19101"], "labels": {"node": "isp-exit"}}
        ])
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_existing_install_does_not_rotate_password(self):
        (self.hub / ".vps-toolkit-managed").touch()
        secret = self.hub / ".env"
        secret.write_text("GRAFANA_ADMIN_PASSWORD=keep-this\n")
        self.shell('linux_root() { :; }; hub_install')
        self.assertEqual(secret.read_text(), "GRAFANA_ADMIN_PASSWORD=keep-this\n")

    def test_unmanaged_directory_is_not_overwritten(self):
        sentinel = self.hub / "existing"
        sentinel.write_text("user data")
        r = self.shell('linux_root() { :; }; hub_install', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sentinel.read_text(), "user data")

    def test_hub_cannot_install_second_exporter(self):
        (self.hub / ".vps-toolkit-managed").touch()
        r = self.shell('linux_root() { :; }; node_install', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("无需重复安装", r.stderr)

    def add_node_mocks(self, success):
        (self.ssh / "id_ed25519.pub").write_text("ssh-ed25519 TEST vps-toolkit-monitor\n")
        return '''
linux_root() { :; }
require_hub() { :; }
ensure_packages() { :; }
setup_ssh() { :; }
check_ports() { :; }
confirm() { return 0; }
runuser() { return 1; }
chown() { :; }
sleep() { :; }
systemctl() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
''' + ('curl() { printf "node_exporter_build_info{version=\\"1\\"} 1\\n"; }\n'
       if success else 'curl() { return 7; }\n')

    def test_failed_probe_rolls_back_tunnel_and_target(self):
        r = self.shell(self.add_node_mocks(False) + 'add_node',
                       "isp-exit\n192.0.2.10\n22\n\n19101\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.ssh / "config.d/isp-exit").exists())
        self.assertFalse((self.hub / "prometheus/targets/isp-exit.json").exists())
        self.assertIn("disable --now vps-toolkit-monitor-tunnel@isp-exit", self.log.read_text())

    def test_successful_probe_publishes_target(self):
        self.shell(self.add_node_mocks(True) + 'add_node',
                   "isp-exit\n192.0.2.10\n00022\n\n19101\n")
        config = (self.ssh / "config.d/isp-exit").read_text()
        self.assertIn("StrictHostKeyChecking yes", config)
        self.assertIn("Port 22", config)
        self.assertIn("LocalForward 127.0.0.1:19101 127.0.0.1:9100", config)
        self.assertTrue((self.hub / "prometheus/targets/isp-exit.json").exists())

    def test_configured_port_cannot_be_reused_when_tunnel_is_down(self):
        (self.ssh / "config.d/another").write_text("    LocalForward 127.0.0.1:19101 127.0.0.1:9100\n")
        r = self.shell(self.add_node_mocks(True) + 'add_node',
                       "isp-exit\n192.0.2.10\n22\n\n19101\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.ssh / "config.d/isp-exit").exists())

    def test_uninstall_keeps_data_and_disables_tunnels(self):
        (self.hub / ".vps-toolkit-managed").touch()
        (self.ssh / "config.d/isp-exit").write_text("keep config")
        (self.hub / ".env").write_text("keep password")
        self.shell('''
linux_root() { :; }
confirm() { return 0; }
compose() { printf 'compose %s\\n' "$*" >> "$CALL_LOG"; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$CALL_LOG"; }
uninstall
''')
        calls = self.log.read_text()
        self.assertIn("compose down\n", calls)
        self.assertNotIn("-v", calls)
        self.assertIn("disable --now vps-toolkit-monitor-tunnel@isp-exit", calls)
        self.assertEqual((self.hub / ".env").read_text(), "keep password")
        self.assertTrue((self.ssh / "config.d/isp-exit").exists())

    def test_bad_checksum_leaves_no_installed_binary_or_temp_download(self):
        dest = self.path / "binary"
        r = self.shell('''
NODE_DIR="$TMPDIR/binary"
curl() {
    local output="${@: -1}"
    case "$output" in
        */sha256sums.txt) printf 'bad archive.tar.gz\\n' > "$output" ;;
        *) printf 'bad archive' > "$output" ;;
    esac
}
download_node amd64
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(dest.exists())
        self.assertEqual(sorted(p.name for p in self.path.iterdir()), ["hub", "ssh"])

    @unittest.skipUnless(shutil.which("docker"), "Compose is not installed")
    def test_compose_limits_and_loopback_listeners(self):
        result = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "templates/monitoring/compose.yaml"),
             "config", "--format", "json"],
            env={**self.env, "GRAFANA_ADMIN_PASSWORD": "validation-only"},
            capture_output=True, text=True, check=True,
        )
        services = json.loads(result.stdout)["services"]
        self.assertEqual(sum(int(s["mem_limit"]) for s in services.values()), 496 * 1024**2)
        for service in services.values():
            self.assertEqual(service["network_mode"], "host")
            self.assertFalse(service.get("ports"))
            self.assertEqual(service["mem_limit"], service["memswap_limit"])
        self.assertIn("--web.listen-address=127.0.0.1:9090", services["prometheus"]["command"])
        self.assertIn("--web.listen-address=127.0.0.1:9100", services["node-exporter"]["command"])
        self.assertEqual(services["grafana"]["environment"]["GF_SERVER_HTTP_ADDR"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
