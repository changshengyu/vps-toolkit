"""Account updates and persistent resource/health lifecycle without real VPS."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

from test_monitoring import MonitoringFixture, ROOT

spec = importlib.util.spec_from_file_location("vt_grafana", ROOT / "lib/monitor/grafana.py")
grafana = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grafana)


class FakeGrafana:
    def __init__(self):
        self.login = "admin"
        self.password = "old-password-12345"
        self.calls = []
        self.fail_password = False
        self.conflict = False

    def factory(self, login, password):
        state = self

        class Client:
            def call(self, method, path, data=None):
                if (login, password) != (state.login, state.password):
                    raise grafana.Error("unauthorized", 401)
                state.calls.append((method, path, data))
                if path == "/api/user":
                    return {"id": 7, "isGrafanaAdmin": True, "login": state.login, "name": "Keep", "email": "keep@example.com"}
                if path.startswith("/api/users/lookup?"):
                    if state.conflict:
                        return {"id": 9}
                    raise grafana.Error("not found", 404)
                if path == "/api/users/7":
                    state.login = data["login"]
                elif path == "/api/user/password":
                    if state.fail_password:
                        raise grafana.Error("bad password", 400)
                    state.password = data["newPassword"]
                else:
                    raise AssertionError(path)
                return {"message": "ok"}

        return Client()


class GrafanaTests(MonitoringFixture):
    def test_rename_and_password_update_preserve_identity_and_hide_new_secret(self):
        fake = FakeGrafana()
        original_env = "GRAFANA_ADMIN_PASSWORD=" + "a" * 48 + "\n"
        (self.hub / ".env").write_text(original_env)
        grafana.change(self.hub, "admin", fake.password, "my-admin", "new-password-123456", fake.factory)
        value = grafana.account(self.hub)
        self.assertEqual((value["id"], value["login"]), (7, "my-admin"))
        self.assertTrue(value["initial_password_stale"])
        self.assertNotIn("new-password", (self.hub / "grafana-account.json").read_text())
        self.assertEqual((self.hub / ".env").read_text(), original_env)
        update = next(data for _, path, data in fake.calls if path == "/api/users/7")
        self.assertEqual((update["name"], update["email"]), ("Keep", "keep@example.com"))
        self.assertEqual(fake.password, "new-password-123456")

    def test_conflicting_username_does_not_change_any_credentials(self):
        fake = FakeGrafana()
        fake.conflict = True
        with self.assertRaisesRegex(grafana.Error, "已被"):
            grafana.change(self.hub, "admin", fake.password, "taken", "new-password-123456", fake.factory)
        self.assertFalse(any(method == "PUT" for method, _, _ in fake.calls))
        self.assertFalse((self.hub / "grafana-account.json").exists())

    def test_failed_password_step_reports_the_committed_username(self):
        fake = FakeGrafana()
        fake.fail_password = True
        with self.assertRaisesRegex(grafana.Error, "my-admin"):
            grafana.change(self.hub, "admin", fake.password, "my-admin", "new-password-123456", fake.factory)
        self.assertEqual(grafana.account(self.hub)["login"], "my-admin")
        self.assertEqual(fake.password, "old-password-12345")

    def test_blank_password_uses_only_exact_generated_env_value(self):
        fake = FakeGrafana()
        fake.password = "a" * 48
        (self.hub / ".env").write_text("GRAFANA_ADMIN_PASSWORD=" + fake.password + "\n")
        grafana.change(self.hub, "admin", "", "admin", "", fake.factory)
        self.assertFalse(grafana.account(self.hub)["initial_password_stale"])
        (self.hub / ".env").write_text("GRAFANA_ADMIN_PASSWORD=$(touch should-not-exist)\n")
        with self.assertRaisesRegex(grafana.Error, "手动输入"):
            grafana.change(self.hub, "admin", "", "admin", "", fake.factory)

    def test_invalid_input_does_not_call_api(self):
        for login, password in [("bad:name", "new-password-12345"), ("ok", "short"), ("ok", "x" * 129), ("ok", "x" * 12 + "\n")]:
            with self.subTest(login=login, size=len(password)), self.assertRaises(grafana.Error):
                grafana.change(self.hub, "admin", "old", login, password, lambda *args: self.fail("API called"))

    def test_resource_overlay_survives_tunnel_pause_composition(self):
        grafana.resources(self.hub, 320)
        self.shell('docker() { printf "%s\\n" "$*"; }; compose up -d --no-deps grafana')
        r = self.shell('docker() { printf "%s\\n" "$*"; }; compose config --quiet')
        self.assertIn("compose.grafana.yaml", r.stdout)
        document = json.loads((self.hub / "compose.grafana.yaml").read_text())
        self.assertEqual(document["services"]["grafana"]["mem_limit"], "320m")
        self.assertEqual(document["services"]["grafana"]["memswap_limit"], "320m")
        document.pop("x-vps-toolkit-grafana-resources")
        (self.hub / "compose.grafana.yaml").write_text(json.dumps(document))
        with self.assertRaisesRegex(grafana.Error, "未覆盖"):
            grafana.resources(self.hub, 256)

    def test_resource_wizard_checks_headroom_and_recreates_only_grafana(self):
        code = '''
compose() { printf '%s\\n' "$*" >> "$CALL_LOG"; case "$*" in 'ps --all --quiet grafana') printf 'id\\n';; esac; }
docker() { printf '201326592\\n'; }
confirm() { return 0; }; grafana_wait() { return 0; }
'''
        r = self.shell(code + "awk() { printf '100\\n'; }; grafana_resources", stdin="\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.hub / "compose.grafana.yaml").exists())
        self.log.unlink()
        self.shell(code + "awk() { printf '300\\n'; }; grafana_resources", stdin="\n")
        self.assertTrue((self.hub / "compose.grafana.yaml").exists())
        calls = self.log.read_text()
        self.assertIn("up -d --no-deps grafana", calls)
        self.assertNotIn("prometheus", calls)
        self.assertNotIn("down", calls)

    def test_account_wizard_uses_stdin_and_rejects_confirmation_mismatch(self):
        r = self.shell('''
grafana_ready() { return 0; }
grafana_meta() {
    if [ "$1" = login ]; then printf 'admin\\n'; else cat > "$MONITOR_DIR/private-pipe"; fi
}
confirm() { return 0; }
grafana_account
''', stdin="\nold-password-12345\nnew-admin\nnew-password-123456\nnew-password-123456\n")
        self.assertNotIn("password-123", r.stdout + r.stderr)
        self.assertEqual((self.hub / "private-pipe").read_bytes().split(b"\0")[:2], [b"admin", b"old-password-12345"])
        r = self.shell('grafana_ready() { :; }; grafana_meta() { printf admin; }; grafana_account',
            stdin="\nold\nnew\nnew-password-123456\nwrong\n", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不一致", r.stderr)

    def test_unhealthy_origin_is_reported_even_if_connector_is_ready(self):
        directory = self.hub / "tunnel"
        directory.mkdir()
        (directory / "state.json").write_text("{}")
        (self.hub / "compose.tunnel.yaml").write_text("{}")
        r = self.shell('''
require_hub() { :; }; cloudflare() { :; }; compose() { :; }
grafana_ready() { return 1; }; curl() { return 0; }
tunnel_status
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("502", r.stderr)

    def test_setup_waits_for_origin_after_container_recreation(self):
        (self.hub / "tunnel").mkdir()
        (self.hub / "tunnel/state.json").write_text("{}")
        r = self.shell('''
check_ports() { :; }; chown() { :; }
cloudflare() { if [ "$1" = overlay ]; then touch "$3"; fi; }
compose() { case "$*" in *'ps --status running --quiet grafana'*) printf 'running\\n';; esac; }
cf_wait_ready() { return 0; }; grafana_wait() { return 1; }
tunnel_start
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.hub / "compose.tunnel.yaml").exists())
        r = self.shell('monitor_lock() { printf "%s\\n" "$1"; }; main grafana-account')
        self.assertIn("grafana_account", r.stdout)
        r = subprocess.run(["bash", str(ROOT / "vps.sh")], input="7\n3\n0\n0\n0\n", env=self.env,
                           text=True, capture_output=True, check=True)
        self.assertIn("8. 修改 Grafana 登录账号 / 密码", r.stdout)
        self.assertIn("9. 调整 Grafana 资源 / 修复启动", r.stdout)


if __name__ == "__main__":
    unittest.main()
