"""Run menus with harmless child scripts; never contact services or install to /usr."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ONLINE = "bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh)"


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.repo = self.path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "lib").mkdir()
        shutil.copyfile(ROOT / "vps.sh", self.repo / "vps.sh")
        # Limit the root override to this disposable fixture and shortcut path.
        (self.repo / "lib/common.sh").write_text((ROOT / "lib/common.sh").read_text() + '\nrequire_root() { return 0; }\n')
        stub = '''#!/usr/bin/env bash
python3 - "$MENU_CALLS" "${0##*/}" "$@" <<'CODE'
import json, os, sys
with open(sys.argv[1], "a") as stream:
    stream.write(json.dumps({"script": sys.argv[2], "args": sys.argv[3:],
        "port": os.environ.get("HY2_PORT"), "range": os.environ.get("HOP_RANGE")}) + "\\n")
CODE
exit "${MENU_ACTION_EXIT:-0}"
'''
        for name in ("deploy.sh", "monitor.sh", "3x-ui.sh", "xui-routing.sh", "xui-geodata.sh",
                     "hy2-port-hopping.sh", "ipv6.sh", "vps-tools.sh"):
            (self.repo / "scripts" / name).write_text(stub)
        denied = self.path / "bin"
        denied.mkdir()
        for name in ("curl", "docker", "ssh"):
            target = denied / name
            target.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$MENU_NETWORK_CALLS"\nexit 91\n')
            target.chmod(0o755)
        self.monitor = self.path / "monitor"
        self.monitor.mkdir()
        self.calls = self.path / "calls.jsonl"
        self.shortcut = self.path / "vt"
        self.env = {**os.environ, "MONITOR_DIR": str(self.monitor), "RESULT_FILE": str(self.path / "result"),
                    "VPS_TOOLKIT_SHORTCUT": str(self.shortcut), "MENU_CALLS": str(self.calls),
                    "MENU_NETWORK_CALLS": str(self.path / "network"), "PATH": str(denied) + os.pathsep + os.environ["PATH"]}

    def run_menu(self, stdin, *args, extra=None):
        result = subprocess.run(["bash", str(self.repo / "vps.sh"), *args], input=stdin,
            capture_output=True, text=True, env={**self.env, **(extra or {})}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.path / "network").exists(), "menu contacted an external service")
        return result

    def records(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_main_information_is_local_and_never_sources_or_prints_secrets(self):
        (self.monitor / ".vps-toolkit-managed").touch()
        (self.monitor / "tunnel").mkdir()
        (self.monitor / "tunnel/state.json").write_text(json.dumps({"domain": "monitor.example.com", "configured": True,
                                                                "secret": "must-not-be-rendered"}))
        (self.monitor / "compose.tunnel.yaml").touch()
        (self.monitor / ".env").write_text('GRAFANA_ADMIN_PASSWORD=must-not-be-rendered\ntouch "$MONITOR_DIR/should-not-exist"\n')
        result = self.run_menu("q\n")
        self.assertIn(ONLINE, result.stdout)
        self.assertIn("主机：", result.stdout)
        self.assertIn("监控角色：监控机", result.stdout)
        self.assertIn("未安装（6 → 1）", result.stdout)
        self.assertIn("https://monitor.example.com/", result.stdout)
        self.assertNotIn("must-not-be-rendered", result.stdout + result.stderr)
        self.assertFalse((self.monitor / "should-not-exist").exists())
        self.assertEqual(self.records(), [])

    def test_all_monitor_routes_dispatch_the_expected_operation(self):
        routes = {
            (1,): ["hub-install"], (2,): ["deploy-node"],
            (3, 1): ["access"], (3, 2): ["grafana-account"], (3, 3): ["grafana-check"], (3, 4): ["grafana-resources"],
            (4, 1): ["tunnel-setup"], (4, 2): ["tunnel-status"], (4, 3): ["tunnel-logs"],
            (4, 4): ["tunnel-start"], (4, 5): ["tunnel-stop"], (4, 6): ["tunnel-remove"],
            (5, 1): ["status"], (5, 2): ["logs"],
            (6, 1): ["cert-status"], (6, 2): ["renew-node"], (6, 3): ["cert-renew", "client"], (6, 4): ["remove-node"],
            (7, 1): ["prepare-node"], (7, 2): ["node-install"], (7, 3): ["add-node"],
            (8, 1): ["start"], (8, 2): ["stop"], (8, 3): ["restart"], (8, 4): ["uninstall"],
        }
        for path, expected in routes.items():
            with self.subTest(path=path):
                self.calls.unlink(missing_ok=True)
                result = self.run_menu("7\n" + "\n".join(map(str, path)) + "\n\nq\n")
                self.assertEqual([(r["script"], r["args"]) for r in self.records()], [("monitor.sh", expected)])
                self.assertIn("当前位置：主菜单 / 监控管理", result.stdout)

    def test_existing_proxy_and_network_actions_keep_correct_arguments(self):
        routes = [
            ("1\n1\n", "deploy.sh", ["--dry-run"]), ("1\n2\n", "deploy.sh", []),
            ("1\n3\n", "deploy.sh", ["--config-only"]), ("1\n4\n", "deploy.sh", ["--replace"]),
            ("2\n1\n", "3x-ui.sh", ["install"]), ("2\n2\n", "3x-ui.sh", ["status"]),
            ("2\n3\n", "xui-routing.sh", ["IPIfNonMatch"]), ("2\n4\n1\n", "xui-geodata.sh", []),
            ("2\n4\n2\n0 3 * * 1\n", "xui-geodata.sh", ["0 3 * * 1"]),
            ("2\n4\n3\n", "xui-geodata.sh", ["off"]),
            ("3\n1\n8443\n10000:20000\neth0\n", "hy2-port-hopping.sh", ["install"]),
            ("3\n2\n", "hy2-port-hopping.sh", ["status"]), ("3\n3\ny\n", "hy2-port-hopping.sh", ["uninstall"]),
            ("4\n1\n", "ipv6.sh", ["status"]), ("4\n2\n", "ipv6.sh", ["enable"]),
            ("4\n3\ny\n", "ipv6.sh", ["disable"]), ("5\n", "vps-tools.sh", []),
        ]
        for input_path, script, args in routes:
            with self.subTest(path=input_path):
                self.calls.unlink(missing_ok=True)
                self.run_menu(input_path + "\nq\n")
                self.assertEqual([(r["script"], r["args"]) for r in self.records()], [(script, args)])
                if script == "hy2-port-hopping.sh" and args == ["install"]:
                    self.assertEqual((self.records()[0]["port"], self.records()[0]["range"]), ("8443", "10000:20000"))

    def test_home_unwinds_nested_menus_without_running_commands(self):
        result = self.run_menu("2\n4\nm\nq\n")
        self.assertEqual(result.stdout.count("  主菜单\n"), 2)
        self.assertEqual(result.stdout.count("  3x-ui 面板\n"), 1)
        self.assertEqual(self.records(), [])
        result = self.run_menu("m\nq\n")
        self.assertEqual(result.stdout.count("  主菜单\n"), 2)

    def test_back_returns_one_level_and_direct_monitor_can_go_home(self):
        result = self.run_menu("7\n4\n0\n0\nq\n")
        self.assertEqual(result.stdout.count("  监控管理（Prometheus + Grafana）\n"), 2)
        result = self.run_menu("3\nm\nq\n", "monitor")
        self.assertIn("当前位置：主菜单 / 监控管理 / Grafana 面板", result.stdout)
        self.assertEqual(result.stdout.count("  主菜单\n"), 1)
        self.assertEqual(self.records(), [])

    def test_failed_actions_stay_on_their_page_and_pause_supports_home(self):
        result = self.run_menu("7\n3\n1\n\nq\n", extra={"MENU_ACTION_EXIT": "42"})
        self.assertIn("退出码 42", result.stderr)
        self.assertEqual(result.stdout.count("当前位置：主菜单 / 监控管理 / Grafana 面板"), 2)
        self.calls.unlink()
        result = self.run_menu("7\n3\n1\nm\nq\n")
        self.assertEqual(result.stdout.count("  主菜单\n"), 2)
        self.assertEqual(len(self.records()), 1)

    def test_help_installs_only_the_fixture_shortcut_and_preserves_unrelated_files(self):
        result = self.run_menu("6\n1\n\nq\n")
        self.assertIn("已安装", result.stdout)
        self.assertTrue(os.access(self.shortcut, os.X_OK))
        text = self.shortcut.read_text()
        self.assertIn('"$@"', text)
        self.assertIn("vps-toolkit/main/vps.sh", text)
        self.shortcut.write_text("unrelated program")
        result = self.run_menu("6\n1\n\nq\n")
        self.assertIn("未覆盖", result.stderr)
        self.assertEqual(self.shortcut.read_text(), "unrelated program")
        self.assertGreaterEqual(result.stdout.count("当前位置：主菜单 / 使用帮助与快捷命令"), 2)

    def test_eof_or_quit_never_starts_an_action_and_invalid_choice_can_recover(self):
        for input_path in ("", "7\n", "7\n3\nq\n", "2\n4\nq\n", "999\n7\n3\n0\n0\nq\n"):
            with self.subTest(input=input_path):
                self.run_menu(input_path)
                self.assertEqual(self.records(), [])
        # EOF during these prompts cancels rather than installing with defaults.
        self.run_menu("3\n1\n")
        self.assertEqual(self.records(), [])

    def test_cli_passthrough_retains_literal_arguments(self):
        self.run_menu("", "deploy", "--config-only", "--example", "two words")
        self.assertEqual(self.records()[0]["args"], ["--config-only", "--example", "two words"])
        self.calls.unlink()
        self.run_menu("", "monitor", "cert-renew", "client")
        self.assertEqual(self.records()[0]["args"], ["cert-renew", "client"])


if __name__ == "__main__":
    unittest.main()
