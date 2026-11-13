"""Cloudflare API and local Tunnel configuration. Standard library only.

The API token is read from a temporary file, never argv, environment or logs.
Only resources journaled by this module may be removed. No global tunnel cleanup.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


IMAGE = "cloudflare/cloudflared:2026.11.0"


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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, token_file):
        self.token = Path(token_file).read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,512}", self.token):
            raise Error("API Token 格式无效。")
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, method, path, data=None):
        request = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never print response bodies: create-tunnel responses contain secrets.
            raise Error(f"Cloudflare API HTTP {exc.code}；请检查 Token 权限、资源范围及网络。", exc.code) from None
        except (OSError, ValueError):
            raise Error("Cloudflare API 请求失败；请检查网络后重试，已创建资源的记录会保留。") from None
        if not payload.get("success"):
            raise Error("Cloudflare API 拒绝操作；请检查 Token 权限和资源范围。")
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
