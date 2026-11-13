"""Cloudflare API and local Tunnel configuration. Python stdlib + bounded curl.

The API token is read from a temporary file, never argv, environment or logs.
Only resources journaled by this module may be removed. No global tunnel cleanup.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.parse


IMAGE = "cloudflare/cloudflared:2026.11.0"
REQUEST_TIMEOUT = 30


class Error(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def domain_name(value):
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 3 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels
    ) or labels[-1].isdigit():
        raise Error("请输入完整子域名，例如 monitor.example.com（不含 https:// 或路径）。")
    return value


def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def api_progress(method, path):
    if path.startswith("/zones?"):
        message = "识别域名所属账户和 Zone"
    elif "/dns_records" in path:
        message = {"GET": "检查 DNS 记录", "POST": "创建面板 CNAME", "DELETE": "移除本工具的 CNAME"}[method]
    elif path.endswith("/configurations"):
        message = "配置 Grafana 转发路由"
    elif path.endswith("/token"):
        message = "获取 Tunnel 连接凭据"
    else:
        message = {"GET": "检查 Tunnel 记录", "POST": "创建 Tunnel", "DELETE": "移除本工具的 Tunnel"}[method]
    print(f"Cloudflare：{message}（单次请求最多 30 秒）…", file=sys.stderr, flush=True)


class API:
    def __init__(self, token_file):
        self.token = Path(token_file).read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,512}", self.token):
            raise Error("API Token 格式无效。")

    def call(self, method, path, data=None):
        api_progress(method, path)
        # -q disables .curlrc (in particular verbose output and redirects).
        # Put the bearer header in curl's stdin config, never argv or env.
        # curl supplies dual-stack fallback and DNS/connect/transfer deadlines;
        # subprocess supplies a wall-clock cap even if the resolver stalls.
        config = 'header = "Authorization: Bearer ' + self.token + '"\n'
        config += 'header = "Content-Type: application/json"\n'
        if data is not None:
            config += "data-binary = " + json.dumps(json.dumps(data, ensure_ascii=True)) + "\n"
        args = ["curl", "-q", "--silent", "--show-error", "--connect-timeout", "8", "--max-time", "25",
                "--proto", "=https", "--config", "-", "--request", method,
                "--url", "https://api.cloudflare.com/client/v4" + path,
                "--write-out", "\n%{http_code}"]
        try:
            result = subprocess.run(args, input=config, text=True, encoding="utf-8",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=REQUEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Error("Cloudflare API 请求超过 30 秒，已终止；请检查 DNS 和出站 HTTPS 网络后重试。") from None
        except OSError:
            raise Error("无法运行 curl；请确认已安装 curl。") from None
        # Neither curl stderr nor response bodies may be echoed: both can contain
        # credentials. Report only the transport exit code or HTTP status.
        if result.returncode:
            if result.returncode == 28:
                raise Error("Cloudflare API 网络请求超时（DNS / 连接 / 传输）；请检查出站 HTTPS 网络后重试。")
            raise Error(f"Cloudflare API 网络请求失败（curl 退出码 {result.returncode}）；请检查 DNS、TLS 和出站 HTTPS 网络。")
        try:
            body, status_text = result.stdout.rsplit("\n", 1)
            status = int(status_text)
            if not 200 <= status < 300:
                raise Error(f"Cloudflare API HTTP {status}；请检查 Token 权限、资源范围及网络。", status)
            payload = json.loads(body)
            if not isinstance(payload, dict) or not payload.get("success"):
                raise Error("Cloudflare API 拒绝操作；请检查 Token 权限和资源范围。")
        except (ValueError, TypeError):
            raise Error("Cloudflare API 返回格式异常；请检查网络后重试。") from None
        return payload.get("result")


def identifier(value, uuid=False):
    pattern = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}" if uuid else r"[a-f0-9]{32}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise Error("Cloudflare 资源 ID 或本地记录无效，停止操作。")
    return value


def load_state(directory):
    state = json.loads((directory / "state.json").read_text())
    if state.get("managed_by") != "vps-toolkit-grafana-v1":
        raise Error("没有本工具管理的 Tunnel 记录。")
    domain_name(state["domain"])
    identifier(state["account"])
    identifier(state["zone"])
    if not re.fullmatch(r"vps-toolkit-grafana-[a-f0-9]{24}", state["name"]):
        raise Error("Tunnel 名称记录无效。")
    for key in ("tunnel", "dns"):
        if state.get(key):
            identifier(state[key], uuid=key == "tunnel")
    return state


def provision(api, directory, domain):
    domain = domain_name(domain)
    state_path = directory / "state.json"
    if state_path.exists():
        raise Error("已有 Tunnel 记录；请使用恢复或移除，不会覆盖。")
    zone = None
    labels = domain.split(".")
    for offset in range(1, len(labels) - 1):
        result = api.call("GET", "/zones?" + urllib.parse.urlencode({"name": ".".join(labels[offset:])}))
        matches = [item for item in result if item["name"] == ".".join(labels[offset:])]
        if matches:
            zone = matches[0]
            break
    if zone is None:
        raise Error("找不到已托管的 Cloudflare 域名；需要 Zone:Zone:Read 权限。")
    # Universal SSL reliably covers the zone and one subdomain level.
    if domain.count(".") != zone["name"].count(".") + 1:
        raise Error("请使用托管域名的一级子域名，例如 monitor.example.com，以匹配 Universal SSL。")
    account = identifier(zone["account"]["id"])
    zone_id = identifier(zone["id"])
    records_path = f"/zones/{zone_id}/dns_records"
    query = "?" + urllib.parse.urlencode({"name": domain})
    if api.call("GET", records_path + query):
        raise Error("这个域名已有 DNS 记录，未覆盖。请换一个新的面板子域名。")
    state = {"managed_by": "vps-toolkit-grafana-v1", "domain": domain,
             "account": account, "zone": zone_id,
             "name": "vps-toolkit-grafana-" + secrets.token_hex(12),
             "tunnel": None, "dns": None, "configured": False}
    atomic_json(state_path, state)
    base = f"/accounts/{account}/cfd_tunnel"
    result = api.call("POST", base, {"name": state["name"], "config_src": "cloudflare"})
    state["tunnel"] = identifier(result["id"], uuid=True)
    atomic_json(state_path, state)
    token = result.get("token") or api.call("GET", base + "/" + state["tunnel"] + "/token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.=-]{20,16384}", token):
        raise Error("Cloudflare 返回的连接 Token 无效。")
    token_path = directory / "connector-token"
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(token)
    os.chmod(token_path, 0o400)
    api.call("PUT", base + "/" + state["tunnel"] + "/configurations", {"config": {"ingress": [
        {"hostname": domain, "service": "http://127.0.0.1:3000"},
        {"service": "http_status:404"},
    ]}})
    result = api.call("POST", records_path, {
        "type": "CNAME", "name": domain, "content": state["tunnel"] + ".cfargotunnel.com",
        "proxied": True, "ttl": 1,
    })
    state["dns"] = identifier(result["id"])
    state["configured"] = True
    atomic_json(state_path, state)


def remove_remote(api, directory):
    state = load_state(directory)
    path = directory / "state.json"
    base = f'/accounts/{state["account"]}/cfd_tunnel'
    # Recover a response lost after POST, using our unguessable, journaled name.
    if not state.get("tunnel"):
        matches = api.call("GET", base + "?" + urllib.parse.urlencode({"name": state["name"], "is_deleted": "false"}))
        matches = [item for item in matches if item["name"] == state["name"]]
        if len(matches) > 1:
            raise Error("Tunnel 名称存在冲突，请人工检查 Cloudflare 控制台。")
        if matches:
            state["tunnel"] = identifier(matches[0]["id"], uuid=True)
            atomic_json(path, state)
    records_path = f'/zones/{state["zone"]}/dns_records'
    records = api.call("GET", records_path + "?" + urllib.parse.urlencode({"name": state["domain"]}))
    for record in records:
        owned = (record.get("type") == "CNAME" and record.get("name") == state["domain"]
                 and record.get("content", "").rstrip(".") == str(state["tunnel"]) + ".cfargotunnel.com")
        # If a record was changed by the user, leave it alone, including its ID.
        if owned and (not state.get("dns") or record["id"] == state["dns"]):
            api.call("DELETE", records_path + "/" + identifier(record["id"]))
    if state.get("tunnel"):
        try:
            match = api.call("GET", base + "/" + state["tunnel"])
        except Error as exc:
            if exc.status != 404:
                raise
            match = None
        if match:
            if match["name"] != state["name"]:
                raise Error("远端 Tunnel 归属信息已改变，未删除。")
            if not match.get("deleted_at"):
                api.call("DELETE", base + "/" + state["tunnel"])
    for name in ("state.json", "connector-token"):
        (directory / name).unlink(missing_ok=True)


def overlay(directory, output):
    state = load_state(directory)
    if not state.get("configured") or not (directory / "connector-token").is_file():
        raise Error("创建流程尚未完成，请先移除残留入口再重新配置。")
    document = {"services": {
        "grafana": {"environment": {
            "GF_SERVER_DOMAIN": state["domain"], "GF_SERVER_ROOT_URL": "https://" + state["domain"] + "/",
            "GF_SERVER_PROTOCOL": "http", "GF_SECURITY_COOKIE_SECURE": "true",
        }},
        "grafana-tunnel": {
            "image": IMAGE, "network_mode": "host", "user": "65532:65532",
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "restart": "unless-stopped", "mem_limit": "96m", "memswap_limit": "96m", "cpus": 0.20,
            "environment": {"GOMEMLIMIT": "64MiB", "GOMAXPROCS": "1"},
            "command": ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20245", "run",
                        "--token-file", "/run/secrets/tunnel-token"],
            "volumes": ["./tunnel/connector-token:/run/secrets/tunnel-token:ro"],
            "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}},
        },
    }}
    atomic_json(output, document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "remove", "overlay", "status", "domain"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("value", nargs="?")
    parser.add_argument("--token-file")
    args = parser.parse_args()
    try:
        if args.action == "create":
            provision(API(args.token_file), args.directory, args.value)
        elif args.action == "remove":
            remove_remote(API(args.token_file), args.directory)
        elif args.action == "overlay":
            overlay(args.directory, Path(args.value))
        else:
            state = load_state(args.directory)
            print(state["domain"] if args.action == "domain" else
                  f'Grafana：https://{state["domain"]}/\nTunnel：{state["tunnel"] or "创建未完成"}')
    except (Error, OSError, ValueError, KeyError, TypeError):
        exc = sys.exc_info()[1]
        print("错误：" + (str(exc) if isinstance(exc, Error) else "本地 Tunnel 配置无效或 API 返回异常。"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
