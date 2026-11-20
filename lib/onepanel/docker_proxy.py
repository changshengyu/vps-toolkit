"""Allow a Docker container or bridge subnet to reach a TCP proxy on its host."""
import ipaddress
import json
import os
import shlex
import subprocess
import sys


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def docker(*args):
    return output("docker", "--host", "unix:///var/run/docker.sock", *args)


def choose(label, options):
    if not options:
        raise ValueError(f"没有可选的{label}")
    for number, option in enumerate(options, 1):
        print(f"  {number}. {option}")
    if len(options) == 1:
        return options[0]
    index = int(input(f"选择{label}编号："))
    if not 1 <= index <= len(options):
        raise ValueError("编号超出范围")
    return options[index - 1]


def networks(container):
    raw = docker("container", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container)
    return json.loads(raw)


def bridge_endpoint(container, network, whole_network=False):
    endpoint = networks(container).get(network)
    if not endpoint:
        raise ValueError("容器的网络连接已变化，请重新选择")
    if docker("network", "inspect", "--format", "{{.Driver}}", endpoint["NetworkID"]) != "bridge":
        raise ValueError("请选择本机 Docker bridge 网络")
    source = str(ipaddress.IPv4Address(endpoint["IPAddress"]))
    gateway = str(ipaddress.IPv4Address(endpoint["Gateway"]))
    route = json.loads(output("ip", "-j", "-4", "route", "get", source))[0]
    device = route["dev"]
    addresses = json.loads(output("ip", "-j", "-4", "addr", "show", "dev", device))
    if "gateway" in route or not any(
        addr["local"] == gateway for interface in addresses for addr in interface["addr_info"]
    ):
        raise ValueError("宿主机网卡与容器网关不匹配，请检查 Docker 网络")
    if whole_network:
        prefix = int(endpoint["IPPrefixLen"])
        subnet = ipaddress.IPv4Network((source, prefix), strict=False)
        if prefix == 0 or ipaddress.IPv4Address(gateway) not in subnet or not any(
            addr["local"] == gateway and addr["prefixlen"] == prefix
            for interface in addresses for addr in interface["addr_info"]
        ):
            raise ValueError("容器与网桥的 IPv4 网段不匹配")
        source = str(subnet)
    return device, source, gateway


def main():
    if "Status: active" not in output("ufw", "status").splitlines():
        raise ValueError("此功能需要已启用的 UFW")
    rows = docker("ps", "--format", "{{.Names}} {{.ID}}").splitlines()
    containers = dict(row.split() for row in rows)
    container = containers[choose("容器", sorted(containers))]
    attached = networks(container)
    network = choose("容器网络", sorted(name for name, ep in attached.items()
                                       if ep.get("IPAddress") and ep.get("Gateway")))
    network_scope = "所选网络的整个 IPv4 子网（包括以后加入的容器）"
    whole_network = choose("放行范围", ["仅此容器当前 IP", network_scope]) == network_scope
    device, source, gateway = bridge_endpoint(container, network, whole_network)
    port = int(input("宿主机代理 TCP 端口 [10809]：").strip() or "10809")
    if not 1 <= port <= 65535:
        raise ValueError("端口须在 1 到 65535 之间")
    rule = ["allow", "in", "on", device, "proto", "tcp", "from", source, "to", gateway, "port", str(port)]
    print(f"即将放行 {network}（{device}）上的 {source} → {gateway}:{port}/tcp")
    if input("应用规则？[y/N] ").strip().lower() not in ("y", "yes"):
        print("已取消")
        return
    if (device, source, gateway) != bridge_endpoint(container, network, whole_network):
        raise ValueError("容器地址已变化，请重新选择")
    subprocess.run(["ufw", *rule], check=True)
    print(f"在应用中填写 HTTP 代理：http://{gateway}:{port}")
    print("撤销此规则的命令：")
    print(" ".join(shlex.quote(arg) for arg in ["ufw", "delete", *rule]))


if __name__ == "__main__":
    os.environ["LC_ALL"] = "C"
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        sys.exit("操作已中止")
    except (ValueError, OSError, KeyError, IndexError, subprocess.CalledProcessError) as error:
        sys.exit(f"配置失败：{error}")
