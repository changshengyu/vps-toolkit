# hy2-port-hopping

为 Hysteria2 服务端配置端口跳跃，并通过 systemd 让规则在重启后自动恢复。

脚本使用 nftables，把公网网卡收到的一段 UDP 端口重定向到 Hysteria2 的实际监听端口。规则放在独立的 `inet hy2jump` 表中，同时覆盖 IPv4 和 IPv6，不影响其他 nftables 规则。

默认配置：

| 项目 | 默认值 |
|---|---|
| Hysteria2 端口 | `443` |
| 跳跃端口范围 | `20000-20100` |
| 网卡 | 默认路由对应的网卡，检测不到时用 `eth0` |

## 环境要求

- 使用 systemd 的 Linux 系统
- root 权限
- Hysteria2 服务端已安装并在监听 UDP 端口
- VPS 支持 nftables NAT，OpenVZ、LXC 等容器型 VPS 可能无法使用

未安装 nftables 时，脚本会尝试通过 `apt-get`、`dnf` 或 `yum` 自动安装。

## 安装

下载脚本后以 root 运行：

```bash
curl -fsSLO https://raw.githubusercontent.com/changshengyu/hy2-port-hopping/main/hy2jump-setup.sh
bash hy2jump-setup.sh
```

如果配置不同，可以用环境变量覆盖默认值：

```bash
IFACE=ens3 HY2_PORT=8443 HOP_RANGE=30000-30100 bash hy2jump-setup.sh
```

脚本会执行以下操作：

1. 删除手动创建的旧表 `ip hy2jump`
2. 生成规则文件 `/etc/hy2jump.nft`
3. 创建并启用 `hy2jump.service`，开机时自动加载规则
4. 如果 ufw 或 firewalld 正在运行，放行 Hysteria2 的 UDP 端口

## 安装后的配置

### 放行云厂商安全组

脚本无法修改云厂商安全组，需要在控制台手动放行：

- UDP `20000-20100`
- UDP `443`（用于直连，建议一并放行）

### 修改客户端

服务端继续监听 `443`，客户端改连跳跃端口范围。

Hysteria 官方客户端：

```yaml
server: example.com:20000-20100
transport:
  udp:
    hopInterval: 30s
```

sing-box：

```json
{
  "server_ports": ["20000:20100"],
  "hop_interval": "30s"
}
```

Mihomo：

```yaml
ports: 20000-20100
hop-interval: 30
```

不同版本的字段可能略有差异，请以所用版本的文档为准。

## 验证

```bash
systemctl status hy2jump
```

用客户端连接并产生流量后，查看规则计数：

```bash
nft list table inet hy2jump
```

`counter` 的数值持续增长，说明流量已命中规则。如果一直为 0，请依次检查：

- 云厂商安全组是否放行了跳跃端口
- 网卡名是否正确，可用 `ip route show default` 查看
- 客户端的端口范围是否与服务端一致

## 注意事项

跳跃范围内的其他入站 UDP 服务会被重定向到 Hysteria2。安装前可用 `ss -ulnp` 检查端口占用。

如果其他脚本已添加过跳跃规则，可用 `nft list ruleset` 和 `iptables -t nat -S PREROUTING` 检查，避免规则重复。

## 卸载

```bash
bash hy2jump-setup.sh uninstall
```

卸载会停用服务，并删除规则和相关文件。之前在 ufw 或 firewalld 中放行的端口不会被移除。
