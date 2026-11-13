"""Cloudflare API and lifecycle regressions, without network or real credentials."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
import urllib.error
from unittest.mock import patch

from test_monitoring import MonitoringFixture, ROOT

spec = importlib.util.spec_from_file_location("vt_cloudflare", ROOT / "lib/monitor/cloudflare.py")
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)
ACCOUNT, ZONE, RECORD = "a" * 32, "b" * 32, "c" * 32
TUNNEL = "11111111-2222-3333-4444-555555555555"
CONNECTOR = "connector-secret-for-test-only-12345"


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.records = []
        self.tunnels = []
        self.fail = None
        self.lost_response = None

    def call(self, method, path, data=None):
        self.calls.append((method, path, data))
        if self.fail and self.fail in path:
            raise cf.Error("simulated API error")
        if path.startswith("/zones?name="):
            return [{"id": ZONE, "name": "example.com", "account": {"id": ACCOUNT}}] if path.endswith("=example.com") else []
        if "/dns_records?" in path:
            return self.records.copy()
        if path.endswith("/dns_records") and method == "POST":
            record = {**data, "id": RECORD}
            self.records.append(record)
            if self.lost_response == "dns":
                raise cf.Error("lost DNS response")
            return record
        if "/dns_records/" in path and method == "DELETE":
            self.records = [r for r in self.records if r["id"] != path.rsplit("/", 1)[1]]
            return {}
        if path.endswith("/cfd_tunnel") and method == "POST":
            tunnel = {**data, "id": TUNNEL, "token": CONNECTOR}
            self.tunnels.append(tunnel)
            if self.lost_response == "tunnel":
                raise cf.Error("lost Tunnel response")
            return tunnel
        if "/cfd_tunnel?" in path:
            return self.tunnels.copy()
        if path.endswith("/configurations") and method == "PUT":
            return data
        if path.endswith("/" + TUNNEL):
            if method == "DELETE":
                self.tunnels = []
                return {}
            if self.tunnels:
                return self.tunnels[0]
            raise cf.Error("not found", 404)
        raise AssertionError((method, path, data))


class TunnelTests(MonitoringFixture):
    def setUp(self):
        super().setUp()
        self.directory = self.hub / "tunnel"
        self.directory.mkdir()
        self.api = FakeAPI()

    def create(self):
        cf.provision(self.api, self.directory, "monitor.example.com")

    def test_provision_scopes_ingress_and_dns_to_grafana(self):
        self.create()
        state = cf.load_state(self.directory)
        self.assertEqual(state["domain"], "monitor.example.com")
        self.assertEqual(self.api.records[0]["content"], TUNNEL + ".cfargotunnel.com")
        self.assertTrue(self.api.records[0]["proxied"])
        config = next(data for method, path, data in self.api.calls if method == "PUT")
        self.assertEqual(config["config"]["ingress"], [
            {"hostname": "monitor.example.com", "service": "http://127.0.0.1:3000"},
            {"service": "http_status:404"}])
        self.assertNotIn(CONNECTOR, (self.directory / "state.json").read_text())
        self.assertEqual((self.directory / "connector-token").stat().st_mode & 0o777, 0o400)
        self.assertEqual((self.directory / "state.json").stat().st_mode & 0o777, 0o600)

    def test_existing_dns_is_never_overwritten(self):
        self.api.records = [{"id": RECORD, "type": "A", "content": "192.0.2.1"}]
        with self.assertRaisesRegex(cf.Error, "已有 DNS"):
            self.create()
        self.assertFalse(any(method != "GET" for method, _, _ in self.api.calls))
        self.assertFalse((self.directory / "state.json").exists())

    def test_domain_validation_and_edge_certificate_coverage(self):
        for value in ("https://monitor.example.com", "monitor.example.com/path", "*.example.com", "x.1.2.3", "a\nb.example.com"):
            with self.assertRaises(cf.Error):
                cf.domain_name(value)
        with self.assertRaisesRegex(cf.Error, "一级子域名"):
            cf.provision(self.api, self.directory, "deep.monitor.example.com")
        self.assertFalse((self.directory / "state.json").exists())

    def test_overlay_has_loopback_origin_and_file_secret(self):
        self.create()
        output = self.hub / "compose.tunnel.yaml"
        cf.overlay(self.directory, output)
        text = output.read_text()
        services = json.loads(text)["services"]
        self.assertTrue(services["grafana"]["environment"]["GF_SECURITY_COOKIE_SECURE"] == "true")
        connector = services["grafana-tunnel"]
        self.assertNotIn("ports", connector)
        self.assertIn("--token-file", connector["command"])
        self.assertNotIn(CONNECTOR, text)
        self.assertNotIn("TUNNEL_TOKEN", connector["environment"])
        self.assertEqual(connector["mem_limit"], "96m")
        self.assertEqual(connector["user"], "65532:65532")

    def test_rollback_recovers_lost_tunnel_and_dns_responses(self):
        for lost in ("tunnel", "dns"):
            with self.subTest(lost=lost):
                self.api = FakeAPI()
                self.api.lost_response = lost
                with self.assertRaises(cf.Error):
                    self.create()
                cf.remove_remote(self.api, self.directory)
                self.assertEqual(self.api.records, [])
                self.assertEqual(self.api.tunnels, [])
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_cleanup_keeps_changed_dns_and_unrelated_tunnels(self):
        self.create()
        self.api.records[0]["content"] = "user-changed.example.net"
        self.api.tunnels.append({"id": "unrelated", "name": "unrelated"})
        cf.remove_remote(self.api, self.directory)
        self.assertEqual(self.api.records[0]["content"], "user-changed.example.net")
        # Check the deletion request precisely; FakeAPI's delete implementation clears its list.
        self.assertEqual([p for m, p, _ in self.api.calls if m == "DELETE"],
                         [f"/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}"])

    def test_failed_cleanup_preserves_retry_record(self):
        self.create()
        self.api.fail = "/cfd_tunnel/"
        with self.assertRaises(cf.Error):
            cf.remove_remote(self.api, self.directory)
        self.assertTrue((self.directory / "state.json").exists())
        self.assertTrue((self.directory / "connector-token").exists())
        self.api.fail = None
        cf.remove_remote(self.api, self.directory)
        self.assertFalse((self.directory / "state.json").exists())

    def test_renamed_tunnel_is_not_deleted_or_forgotten(self):
        self.create()
        self.api.tunnels[0]["name"] = "user-renamed"
        with self.assertRaisesRegex(cf.Error, "归属"):
            cf.remove_remote(self.api, self.directory)
        self.assertTrue((self.directory / "state.json").exists())
        self.assertFalse(any(m == "DELETE" and "/cfd_tunnel/" in p for m, p, _ in self.api.calls))

    def test_api_errors_never_echo_bearer_or_response_body(self):
        token = self.path / "api-token"
        token.write_text("private-api-token-for-tests-only-1234")
        api = cf.API(token)
        error = urllib.error.HTTPError("https://api.cloudflare.com/", 403,
                                      "forbidden " + token.read_text(), {}, None)
        with patch.object(api.opener, "open", side_effect=error):
            with self.assertRaises(cf.Error) as result:
                api.call("POST", "/accounts/example/cfd_tunnel")
        self.assertNotIn(token.read_text(), str(result.exception))
        self.assertIn("403", str(result.exception))
        self.assertIsNone(cf.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_pause_restores_base_and_preserves_secrets_and_data(self):
        self.create()
        cf.overlay(self.directory, self.hub / "compose.tunnel.yaml")
        (self.hub / ".env").write_text("keep-password")
        r = self.shell('''
docker() { printf '%s\\n' "$*" >> "$CALL_LOG"; case "$*" in *'ps --status running --quiet grafana'*) printf 'running-id\\n';; esac; }
tunnel_stop
''')
        self.assertIn("恢复 SSH", r.stdout)
        self.assertFalse((self.hub / "compose.tunnel.yaml").exists())
        self.assertEqual((self.hub / ".env").read_text(), "keep-password")
        self.assertTrue((self.directory / "state.json").exists())
        lines = self.log.read_text().splitlines()
        self.assertIn("stop grafana-tunnel", lines[1])
        self.assertIn("compose.tunnel.yaml", lines[0])
        self.assertIn("up -d --no-deps grafana", lines[-1])
        self.assertNotIn("compose.tunnel.yaml", lines[-1])
        self.assertFalse(any("down" in line or "-v" in line for line in lines))

    def test_pause_does_not_start_previously_stopped_grafana(self):
        self.create()
        cf.overlay(self.directory, self.hub / "compose.tunnel.yaml")
        self.shell('''
docker() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
tunnel_stop
''')
        calls = self.log.read_text()
        self.assertIn("create grafana", calls)
        self.assertNotIn("up -d", calls)
        self.assertFalse((self.hub / "compose.tunnel.yaml").exists())

    def test_failed_resume_restores_ssh_mode(self):
        self.create()
        r = self.shell('''
check_ports() { :; }; chown() { :; }
docker() { printf '%s\\n' "$*" >> "$CALL_LOG"; case "$*" in *'ps --status running --quiet grafana'*) printf 'running-id\\n';; esac; }
cf_wait_ready() { return 1; }
tunnel_start
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.hub / "compose.tunnel.yaml").exists())
        self.assertTrue((self.directory / "connector-token").exists())
        self.assertIn("up -d --no-deps grafana", self.log.read_text().splitlines()[-1])

    def test_wizard_failure_cleans_ephemeral_token_and_restores_ssh(self):
        self.create()
        # The create stub represents resources successfully created before Docker fails.
        saved = self.directory / "state.saved"
        (self.directory / "state.json").rename(saved)
        r = self.shell('''
ensure_packages() { :; }; check_ports() { :; }; confirm() { return 0; }
curl() { return 0; }; chown() { :; }
awk() { printf '600\\n'; }
cf_read_token() { printf 'test-private-api-token' > "$cf_api_token"; printf '%s' "$cf_api_token" > "$MONITOR_DIR/token-path"; }
cloudflare() {
    if [ "$1" = create ]; then mv "$CF_DIR/state.saved" "$CF_DIR/state.json";
    elif [ "$1" = remove ]; then printf 'cleanup\\n' >> "$CALL_LOG";
    else python3 "$ROOT_DIR/lib/monitor/cloudflare.py" "$@"; fi
}
docker() {
    printf '%s\\n' "$*" >> "$CALL_LOG"
    case "$*" in *'pull grafana-tunnel'*) return 1;; esac
}
tunnel_setup
''', stdin="monitor.example.com\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(Path((self.hub / "token-path").read_text()).exists())
        self.assertFalse((self.hub / "compose.tunnel.yaml").exists())
        self.assertIn("cleanup", self.log.read_text())
        self.assertNotIn("test-private-api-token", r.stdout + r.stderr + self.log.read_text())

    def test_access_submenu_and_cli_routes(self):
        r = subprocess.run(["bash", str(ROOT / "vps.sh")], input="7\n3\n0\n0\n0\n",
                           text=True, capture_output=True, env=self.env, check=True)
        self.assertIn("1. 配置 Grafana 域名入口", r.stdout)
        self.assertIn("7. 移除域名入口", r.stdout)
        for action in ("setup", "start", "stop", "remove"):
            r = self.shell('monitor_lock() { printf "%s\\n" "$1"; }; main tunnel-' + action)
            self.assertEqual(r.stdout.strip(), "tunnel_" + action)


if __name__ == "__main__":
    unittest.main()
