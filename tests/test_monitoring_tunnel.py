"""Cloudflare API and lifecycle regressions, without network or real credentials."""
import contextlib
import http.server
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import ssl
import sys
import time
import threading
import unittest
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

    def make_api(self):
        token = self.path / "api-token"
        token.write_text("private-api-token-for-tests-only-1234")
        return cf.API(token)

    def test_api_errors_never_echo_bearer_or_response_body(self):
        api = self.make_api()
        for status, code in ((403, 0), (302, 0), (200, 60), (200, 28)):
            with self.subTest(status=status, code=code):
                response = subprocess.CompletedProcess([], code,
                    json.dumps({"secret": api.token}) + "\n" + str(status), api.token)
                messages = io.StringIO()
                with patch.object(cf.subprocess, "run", return_value=response) as transport, contextlib.redirect_stderr(messages):
                    with self.assertRaises(cf.Error) as result:
                        api.call("POST", "/accounts/example/cfd_tunnel")
                self.assertNotIn(api.token, str(result.exception) + messages.getvalue())
                self.assertNotIn(api.token, " ".join(transport.call_args.args[0]))
                self.assertEqual(transport.call_args.kwargs["timeout"], 30)
                self.assertIn(api.token, transport.call_args.kwargs["input"])
                self.assertIn("创建 Tunnel", messages.getvalue())

    def test_stalled_resolver_has_a_hard_deadline_and_sanitized_error(self):
        api = self.make_api()
        error = subprocess.TimeoutExpired(["curl"], 30, output=api.token, stderr=api.token)
        with patch.object(cf.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(cf.Error, "超过 30 秒") as result:
                api.call("GET", "/zones?name=example.com")
        self.assertNotIn(api.token, str(result.exception))

    def test_hung_transport_process_is_terminated_at_wall_deadline(self):
        api = self.make_api()
        real_run = subprocess.run

        def hung_transport(args, **kwargs):
            return real_run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

        started = time.monotonic()
        with patch.object(cf, "REQUEST_TIMEOUT", 0.15), patch.object(cf.subprocess, "run", side_effect=hung_transport):
            with self.assertRaises(cf.Error):
                api.call("GET", "/zones?name=example.com")
        self.assertLess(time.monotonic() - started, 3)

    def test_token_read_acknowledges_enter_without_echoing_secret(self):
        token = "private-test-token-not-for-use-12345"
        r = self.shell('cf_api_token="$MONITOR_DIR/temporary-token"; cf_read_token', stdin=token + "\n")
        self.assertIn("Token 已接收", r.stdout)
        self.assertNotIn(token, r.stdout + r.stderr)
        self.assertEqual((self.hub / "temporary-token").read_text(), token)

    def test_real_curl_sends_stdin_header_and_json_over_verified_tls(self):
        from test_monitoring_tls import TLSFixture
        fixture = TLSFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.make_certificates("127.0.0.1", "127.0.0.1")
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"success": True, "result": {"id": TUNNEL}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(fixture.pki / "nodes/isp-exit/server.pem", fixture.pki / "nodes/isp-exit/server-key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        api = self.make_api()
        real_run = subprocess.run

        def local_transport(args, **kwargs):
            args = list(args)
            args[args.index("--url") + 1] = f"https://127.0.0.1:{server.server_port}/"
            args += ["--cacert", str(fixture.pki / "ca.pem"), "--noproxy", "*"]
            return real_run(args, **kwargs)

        try:
            data = {"name": "a\\b\"c\nd", "unicode": "测试", "config": {"enabled": True}}
            with patch.object(cf.subprocess, "run", side_effect=local_transport):
                self.assertEqual(api.call("POST", "/accounts/example/cfd_tunnel", data), {"id": TUNNEL})
            self.assertEqual(requests, [("Bearer " + api.token, data)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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
