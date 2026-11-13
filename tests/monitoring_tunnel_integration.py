#!/usr/bin/env python3
"""Validate actual Compose merging, cloudflared CLI and Grafana login/SSH fallback.

No Cloudflare account or live tunnel is used. All containers and volumes belong
only to this disposable project; Grafana's test port binds to 127.0.0.1.
"""
import base64
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from test_monitoring_tunnel import cf, FakeAPI, CONNECTOR
from test_monitoring import ROOT
from test_monitoring_grafana import grafana
from unittest.mock import patch


def run(*args):
    return subprocess.check_output(list(args), text=True, stderr=subprocess.STDOUT).strip()


def main():
    with tempfile.TemporaryDirectory(prefix="vt-tunnel-check-") as tmp:
        directory = Path(tmp)
        shutil.copytree(ROOT / "templates/monitoring", directory, dirs_exist_ok=True)
        (directory / "tunnel").mkdir()
        cf.provision(FakeAPI(), directory / "tunnel", "monitor.example.com")
        overlay = directory / "compose.tunnel.yaml"
        cf.overlay(directory / "tunnel", overlay)
        (directory / ".env").write_text("GRAFANA_ADMIN_PASSWORD=" + secrets.token_hex(20) + "\n")
        project = "vt-tunnel-check-" + uuid.uuid4().hex[:10]
        base = ["docker", "compose", "--project-name", project, "--project-directory", tmp,
                "-f", str(directory / "compose.yaml")]
        active = base + ["-f", str(overlay)]
        merged = json.loads(run(*active, "config", "--format", "json"))
        assert "ports" not in merged["services"]["grafana"]
        assert merged["services"]["grafana"]["environment"]["GF_SERVER_HTTP_ADDR"] == "127.0.0.1"
        connector = merged["services"]["grafana-tunnel"]
        assert "ports" not in connector
        assert connector["command"][-2:] == ["--token-file", "/run/secrets/tunnel-token"]
        assert CONNECTOR not in json.dumps(merged)
        assert merged["services"]["prometheus"]["command"] == json.loads(run(*base, "config", "--format", "json"))["services"]["prometheus"]["command"]
        help_text = run("docker", "run", "--rm", "--network", "none", "--user", "65532:65532",
                        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                        cf.IMAGE, *connector["command"][:-2], "--help")
        assert "--token-file" in help_text
        # Exercise the production 256 MiB / 0.5 CPU budget. Only this disposable
        # fixture changes the listen address so Docker can publish it locally.
        test_file = directory / "test.json"
        test_file.write_text(json.dumps({"services": {"grafana": {
            "network_mode": "bridge", "ports": ["127.0.0.1::3000"],
            "environment": {"GF_SERVER_HTTP_ADDR": "0.0.0.0", "GF_PLUGINS_PREINSTALL_DISABLED": "true",
                            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false", "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false"}
        }}}))
        active_test = active + ["-f", str(test_file)]
        plain_test = base + ["-f", str(test_file)]
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        login = "admin"
        password = (directory / ".env").read_text().strip().split("=", 1)[1]

        def endpoint(command):
            port = run(*command, "port", "grafana", "3000").rsplit(":", 1)[1]
            return "http://127.0.0.1:" + port

        def request(url, data=None):
            headers = {"Host": "monitor.example.com", "X-Forwarded-Proto": "https"}
            if url.endswith("/api/frontend/settings"):
                headers["Authorization"] = "Basic " + base64.b64encode((login + ":" + password).encode()).decode()
            if data is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data).encode()
            return client.open(urllib.request.Request(url, data=data, headers=headers), timeout=5)

        def wait(url):
            for attempt in range(240):
                try:
                    with request(url + "/api/health") as response:
                        if json.load(response)["database"] == "ok":
                            return
                except (OSError, ValueError, urllib.error.URLError):
                    pass
                if attempt % 10 == 0:
                    container = run(*active_test, "ps", "--all", "--quiet", "grafana")
                    state = json.loads(run("docker", "inspect", container))[0]
                    if state["RestartCount"] or state["State"].get("OOMKilled"):
                        raise AssertionError("Grafana restarted during startup: " + json.dumps({
                            "restarts": state["RestartCount"], "state": state["State"]}))
                time.sleep(1)
            raise AssertionError("Grafana did not become healthy within 240 seconds")

        try:
            run(*active_test, "up", "-d", "--no-deps", "grafana")
            address = endpoint(active_test)
            wait(address)
            with request(address + "/api/frontend/settings") as response:
                assert json.load(response)["appUrl"] == "https://monitor.example.com/"
            with request(address + "/login", {"user": login, "password": password}) as response:
                cookies = response.headers.get_all("Set-Cookie")
                assert any("grafana_session=" in cookie and "; Secure" in cookie for cookie in cookies), cookies
            # Change the real existing account, not the bootstrap .env values.
            new_password = secrets.token_urlsafe(24) + '"中文'
            with patch.object(grafana, "ORIGIN", address):
                grafana.change(directory, login, password, "monitor-admin", new_password)
            old_password = password
            login, password = "monitor-admin", new_password
            try:
                request(address + "/login", {"user": "admin", "password": old_password})
                raise AssertionError("old credentials still work")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            # Apply a persistent resources override, then pause the domain mode.
            grafana.resources(directory, 256)
            resource = ["-f", str(directory / "compose.grafana.yaml")]
            active_test = active + resource + ["-f", str(test_file)]
            plain_test = base + resource + ["-f", str(test_file)]
            # Pause removes only the domain override; the updated login survives.
            run(*plain_test, "up", "-d", "--no-deps", "grafana")
            address = endpoint(plain_test)
            wait(address)
            with request(address + "/api/frontend/settings") as response:
                assert json.load(response)["appUrl"] == "http://localhost:3000/"
            with request(address + "/login", {"user": login, "password": password}) as response:
                cookies = response.headers.get_all("Set-Cookie")
                assert any("grafana_session=" in cookie and "; Secure" not in cookie for cookie in cookies), cookies
            print("PASS: production Grafana budget, account rename/password API, rejected old login, HTTPS cookie, persistent resources and SSH fallback")
        except Exception:
            print(run(*active_test, "logs", "--tail=2000", "grafana"))
            raise
        finally:
            run(*active_test, "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
