#!/usr/bin/env python3
"""Exercise the actual exporter and Prometheus in disposable Docker containers."""
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from test_monitoring_tls import TLSTests
from test_monitoring import ROOT


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def opener(context=None):
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def main():
    fixture = TLSTests()
    fixture.setUp()
    exporter = "vt-mtls-exporter-" + uuid.uuid4().hex[:10]
    prometheus = "vt-mtls-prometheus-" + uuid.uuid4().hex[:10]
    try:
        fixture.make_certificates("127.0.0.1", "127.0.0.1")
        fixture.shell('NODE_CONFIG_DIR=/test; nft() { :; }; render_node_config "$PKI_DIR/nodes/isp-exit" "$PKI_DIR/nodes/isp-exit/node.json"')
        directory = fixture.pki / "nodes/isp-exit"
        docker("run", "--detach", "--rm", "--name", exporter,
               "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
               "--publish", "127.0.0.1::19100", "--publish", "127.0.0.1::9090",
               "--mount", f"type=bind,src={directory},dst=/test,readonly",
               "quay.io/prometheus/node-exporter:v1.12.1",
               "--web.listen-address=:19100", "--web.config.file=/test/web.yml")
        port = int(docker("port", exporter, "19100/tcp").rsplit(":", 1)[1])
        url = f"https://127.0.0.1:{port}/metrics"
        tls = fixture.hub / "prometheus/tls"
        context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        context.load_cert_chain(tls / "client.pem", tls / "client-key.pem")
        client = opener(context)
        for attempt in range(30):
            try:
                with client.open(url, timeout=2) as response:
                    assert b"node_exporter_build_info" in response.read()
                break
            except (OSError, urllib.error.URLError):
                if attempt == 29:
                    raise
                time.sleep(0.5)
        print("PASS: real Node Exporter accepts the authorized mTLS client", flush=True)

        no_client = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        fixture.shell('sign_leaf rogue clientAuth URI:spiffe://vps-toolkit/not-monitor "$PKI_DIR/rogue"')
        wrong_san = ssl.create_default_context(cafile=str(tls / "ca.pem"))
        wrong_san.load_cert_chain(fixture.pki / "rogue.pem", fixture.pki / "rogue-key.pem")
        for label, denied in [("missing certificate", no_client), ("wrong client SAN", wrong_san)]:
            try:
                with opener(denied).open(url, timeout=3) as response:
                    response.read()
            except (OSError, urllib.error.URLError):
                print(f"PASS: real Node Exporter rejects {label}", flush=True)
            else:
                raise AssertionError(f"exporter accepted {label}")

        shutil.copyfile(ROOT / "templates/monitoring/prometheus/prometheus.yml",
                        fixture.hub / "prometheus/prometheus.yml")
        (fixture.hub / "prometheus/targets").mkdir(exist_ok=True)
        fixture.shell('enable_https_job; write_https_target isp-exit')
        config = fixture.hub / "prometheus/prometheus.yml"
        config.write_text(config.read_text().replace("scrape_interval: 60s", "scrape_interval: 2s").replace("scrape_timeout: 10s", "scrape_timeout: 1s"))
        docker("run", "--detach", "--name", prometheus,
               "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "256m",
               "--network", "container:" + exporter,
               "--mount", f"type=bind,src={fixture.hub / 'prometheus'},dst=/etc/prometheus,readonly",
               "prom/prometheus:v3.16.0", "--config.file=/etc/prometheus/prometheus.yml",
               "--storage.tsdb.path=/tmp/prometheus", "--web.listen-address=:9090")
        api_port = int(docker("port", exporter, "9090/tcp").rsplit(":", 1)[1])
        query = f"http://127.0.0.1:{api_port}/api/v1/query?query=up%7Bjob%3D%22node-https%22%7D"
        for attempt in range(60):
            if docker("inspect", "--format", "{{.State.Running}}", prometheus) != "true":
                raise AssertionError("Prometheus container exited during startup")
            try:
                with opener().open(query, timeout=2) as response:
                    result = json.load(response)["data"]["result"]
                if result and result[0]["value"][1] == "1":
                    break
            except (OSError, urllib.error.URLError, KeyError):
                pass
            if attempt == 59:
                raise AssertionError("Prometheus never successfully scraped the HTTPS target")
            time.sleep(1)
        print("PASS: real Prometheus scrapes Node Exporter over mTLS (up=1)", flush=True)
        fixture.shell('''
jq '.address="node.example.test"' "$PKI_DIR/nodes/isp-exit/node.json" > "$PKI_DIR/nodes/isp-exit/node.json.tmp"
mv "$PKI_DIR/nodes/isp-exit/node.json.tmp" "$PKI_DIR/nodes/isp-exit/node.json"
build_node_package isp-exit >/dev/null
''')
        for domain, expected in [("node.example.test", 0), ("wrong.example.test", 60)]:
            result = subprocess.run(["curl", "--noproxy", "*", "--max-time", "10", "-fsS",
                "--resolve", f"{domain}:{port}:127.0.0.1", "--cacert", str(tls / "ca.pem"),
                "--cert", str(tls / "client.pem"), "--key", str(tls / "client-key.pem"),
                f"https://{domain}:{port}/metrics"], capture_output=True)
            assert result.returncode == expected, result.stderr.decode(errors="replace")
        print("PASS: DNS SAN certificates accept the correct domain and reject a different domain", flush=True)
    except Exception:
        for name in (exporter, prometheus):
            subprocess.run(["docker", "logs", "--tail=40", name], check=False)
        raise
    finally:
        for name in (prometheus, exporter):
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
