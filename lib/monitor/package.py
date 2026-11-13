#!/usr/bin/env python3
"""Validate monitoring metadata and unpack only the expected certificate files."""
import ipaddress
import gzip
import io
import json
from pathlib import Path
import re
import socket
import sys
import tarfile


FILES = {"node.json", "ca.pem", "server.pem", "server-key.pem"}


def identity(value):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("invalid address")
    try:
        address = ipaddress.ip_address(value)
        return {"kind": "IP", "address": str(address), "version": address.version}
    except ValueError:
        domain = value[:-1] if value.endswith(".") else value
        labels = domain.split(".")
        if (len(domain) > 253 or len(labels) < 2
                or not re.search(r"[A-Za-z]", labels[-1])
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in labels)):
            raise ValueError("invalid IP address or DNS hostname")
        return {"kind": "DNS", "address": domain.lower()}


def metadata(value):
    if set(value) != {"schema", "name", "address", "monitor_ip", "port"} or value["schema"] != 1:
        raise ValueError("unsupported certificate package schema")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value["name"]):
        raise ValueError("invalid node name")
    if any("%" in value[k] for k in ("address", "monitor_ip")):
        raise ValueError("scoped IP addresses are not supported")
    address = identity(value["address"])
    source = ipaddress.ip_address(value["monitor_ip"])
    if address["kind"] == "IP" and address["version"] != source.version:
        raise ValueError("node and monitoring source IP must use the same address family")
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("port must be 1024..65535")
    value = dict(value)
    value.update(address=address["address"], monitor_ip=str(source))
    return value


def extract(archive, destination):
    destination = Path(destination)
    with gzip.open(archive, "rb") as stream:
        decoded = stream.read(1048577)
    if len(decoded) > 1048576:
        raise ValueError("decoded certificate package is too large")
    with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:") as bundle:
        members = bundle.getmembers()
        names = [entry.name for entry in members]
        if len(names) != len(FILES) or set(names) != FILES:
            raise ValueError("unexpected, duplicate or missing files in certificate package")
        if any(not entry.isfile() or not 0 < entry.size <= 65536 for entry in members):
            raise ValueError("package entries must be small regular files")
        contents = {entry.name: bundle.extractfile(entry).read() for entry in members}
    meta = metadata(json.loads(contents["node.json"]))
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in contents.items():
        # No extractall: links and archive paths can never choose the destination.
        target = destination / name
        if target.exists() or target.is_symlink():
            raise ValueError("destination must be empty")
        with target.open("xb") as handle:
            handle.write(data)
        target.chmod(0o600)
    return meta


def deployment(value):
    if set(value) not in ({"schema", "node", "ssh", "auto_renew"}, {"schema", "node", "ssh", "auto_renew", "renewal_identity"}) or value["schema"] != 1:
        raise ValueError("invalid deployment schema")
    node = metadata(value["node"])
    ssh = value["ssh"]
    if set(ssh) != {"user", "port", "identity_file"}:
        raise ValueError("invalid SSH configuration")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", ssh["user"]):
        raise ValueError("invalid SSH user")
    if type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65535:
        raise ValueError("invalid SSH port")
    key = ssh["identity_file"]
    if not isinstance(key, str) or any(ord(c) < 32 for c in key) or (key and not key.startswith("/")):
        raise ValueError("identity file must be an absolute path")
    if type(value["auto_renew"]) is not bool:
        raise ValueError("invalid automatic renewal setting")
    renewal_key = value.get("renewal_identity", "")
    if not isinstance(renewal_key, str) or any(ord(c) < 32 for c in renewal_key) or (renewal_key and not renewal_key.startswith("/")):
        raise ValueError("invalid renewal key path")
    return {**value, "node": node}


def main(args):
    if args[0] == "deployment" and len(args) == 2:
        result = deployment(json.loads(Path(args[1]).read_text()))
    elif args[0] == "resolve" and len(args) == 3:
        address = identity(args[1])["address"]
        family = socket.AF_INET6 if ipaddress.ip_address(args[2]).version == 6 else socket.AF_INET
        result = sorted({item[4][0] for item in socket.getaddrinfo(address, None, family, socket.SOCK_STREAM)})
        if not result:
            raise ValueError("no address records for the monitoring source IP family")
    elif args[0] == "identity" and len(args) == 2:
        result = identity(args[1])
    elif args[0] == "extract" and len(args) == 3:
        result = extract(args[1], args[2])
    elif args[0] == "validate" and len(args) == 2:
        result = metadata(json.loads(Path(args[1]).read_text()))
    elif args[0] == "create" and len(args) == 5:
        result = metadata({"schema": 1, "name": args[1], "address": args[2],
                           "monitor_ip": args[3], "port": int(args[4])})
    else:
        raise ValueError("usage: package.py create NAME ADDRESS SOURCE PORT | validate FILE | extract TAR DIR")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, IndexError) as error:
        print(f"Invalid monitoring package: {error}", file=sys.stderr)
        sys.exit(1)
