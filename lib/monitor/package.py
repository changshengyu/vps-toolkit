#!/usr/bin/env python3
"""Validate monitoring metadata and unpack only the expected certificate files."""
import ipaddress
import json
from pathlib import Path
import re
import sys
import tarfile


FILES = {"node.json", "ca.pem", "server.pem", "server-key.pem"}


def metadata(value):
    if set(value) != {"schema", "name", "address", "monitor_ip", "port"} or value["schema"] != 1:
        raise ValueError("unsupported certificate package schema")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value["name"]):
        raise ValueError("invalid node name")
    if any("%" in value[k] for k in ("address", "monitor_ip")):
        raise ValueError("scoped IP addresses are not supported")
    address = ipaddress.ip_address(value["address"])
    source = ipaddress.ip_address(value["monitor_ip"])
    if address.version != source.version:
        raise ValueError("node and monitoring source IP must use the same address family")
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("port must be 1024..65535")
    value = dict(value)
    value.update(address=str(address), monitor_ip=str(source))
    return value


def extract(archive, destination):
    destination = Path(destination)
    with tarfile.open(archive, "r:gz") as bundle:
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


def main(args):
    if args[0] == "extract" and len(args) == 3:
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
