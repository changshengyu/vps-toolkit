# 多 VPS 监控：Prometheus + Grafana + HTTPS mTLS

主菜单 **7 → 监控管理**，或运行 `vt monitor`。仅一台机器安装集中端，其他 VPS 只安装采集器。新节点采用 HTTPS 双向证书认证，日常采集不依赖 SSH 长连接；现有 SSH 目标可逐台迁移。

## 组件与网络

| 机器 | 组件及连接 |
|---|---|
| 集中监控机 | Docker Compose：Prometheus、Grafana、本机 Node Exporter |
| 入口 VPS / ISP 落地机 | Node Exporter 二进制、systemd、独立 nftables 源 IP 规则 |
| 本地 Mac | 通过 SSH 隧道打开 Grafana；不参与持续采集 |

```text
监控机 Prometheus ── HTTPS + 客户端证书 ──→ 节点公网 IP:19100
本地 Mac ── SSH 隧道 ──→ 监控机 Grafana
```

监控机验证节点证书链和 IP SAN；节点验证客户端证书、clientAuth 用途及监控身份 SAN。节点采集端口仅允许指定的监控机公网源 IP。支持 IPv4 或 IPv6，目标和源 IP 须使用同一地址族；本版直接配置 IP，不依赖公网域名和公共 CA。

安装要求：Linux + systemd、root、OpenSSL 1.1.1 或更新版本；节点端支持 x86_64 / ARM64。脚本使用 Python 3 标准库验证证书包，无常驻 Python 服务。节点还需要内核支持 nftables；无法安装或应用访问规则时会中止，不会降级成开放采集端口。

不修改 3x-ui、Xray、ISP 分流、现有 SSH 配置或默认路由。采集使用独立端口，不占用 Reality / 静态站点的 443。首次安装 Docker 可能建立 Docker 自身网络规则。

## 1 GB 监控机的试运行限制

| 项目 | 默认值 |
|---|---|
| 采集间隔 / 历史 | 60 秒 / 7 天 |
| 历史块保留上限 | 2 GB；WAL、当前块及压缩临时文件需另外留空间 |
| 磁盘检查 | /opt 至少 5 GiB 可用 |
| 集中端容器内存上限 | Prometheus 256 MiB、Grafana 192 MiB、Node Exporter 48 MiB |
| 远端采集器内存上限 | systemd MemoryMax=64M |
| 集中端管理端口 | 仅 127.0.0.1:3000、9090、9100 |
| 远端 HTTPS 采集端口 | 默认 TCP 19100，仅指定源 IP + 客户端证书可访问 |

496 MiB 是三个集中端容器合计硬上限，不是实际常驻占用，也不包含 Docker、现有代理等。Grafana 的 192 MiB 低于官方最低建议，仅供紧凑试运行，可能 OOM。安装 Docker 后 available 低于 600 MiB 会提示；启动 Prometheus 后不足 300 MiB 时暂不启动 Grafana。

## 安装顺序

### 1. 搬瓦工安装集中端

```bash
vt monitor hub-install
vt monitor status
vt monitor access
```

也可用 `bash vps.sh monitor ...`。未安装 vt 时，通过 README 的一行入口传入同样参数。

配置保存在 `/opt/vps-toolkit-monitor`，不依赖入口脚本临时目录。重复 hub-install 不轮换密码和 CA；启动中断后排查，再用 `vt monitor start` 恢复。

### 2. 生成每台节点的证书包

在监控机运行菜单第 8 项，或：

```bash
vt monitor prepare-node
```

输入节点名称、节点公网 IP、监控机访问此节点时的公网源 IP、采集端口（默认 19100）。名称最多 32 字符，用英文字母、数字、下划线或短横线；`client` 是续签保留名。

监控机若使用 NAT，请填写实际公网出口地址，而非系统网卡私网地址。IPv6 请填写实际出站源地址，不要填会变化的临时隐私地址。

生成 `/opt/vps-toolkit-monitor/packages/节点名.tar.gz`，并显示 CA 的 SHA256 指纹。包中只有节点元数据、公开 CA、该节点服务器证书及私钥，**没有 CA 私钥和监控机客户端私钥**。不要把整个 pki 目录传给节点。

### 3. 安全传输并安装节点

通过已有 SSH / SCP 将对应证书包传到该 VPS 的 root 私有目录，例如从监控机执行（替换 SSH 信息）：

```bash
scp -P SSH端口 /opt/vps-toolkit-monitor/packages/节点名.tar.gz 用户名@节点IP:/安全目录/
```

远端以 root 执行菜单第 2 项，或：

```bash
vt monitor node-install --bundle /安全目录/节点名.tar.gz
```

安装前核对 CA 指纹。脚本只解包预期的四个普通文件，拒绝路径穿越、链接和重复文件，并验证证书链、serverAuth 用途、IP SAN、有效期及密钥匹配。

采集器以非特权用户运行。本模块创建 `vps-toolkit-monitor-firewall.service`，在 exporter 启动前加载独立的 `inet vps_toolkit_monitor` 表，只限制该采集端口，不 flush 现有规则。不支持 nftables 或启动失败时，恢复原采集器配置。

**已有 UFW / firewalld 或云安全组的默认拒绝规则仍然有效。**需要另外精确放行“监控机源 IP → 节点 TCP 19100”，不要对所有来源开放。脚本不覆盖这些已有访问策略。

确认整个连接已接入后，安全删除远端用于传输的证书包；运行所需的证书副本已保存于 `/etc/vps-toolkit-monitor-node`。

### 4. 验证并接入

回到监控机：

```bash
vt monitor add-node
```

输入刚才的节点名称。脚本使用真实客户端证书请求远端 HTTPS `/metrics`，验证通过后激活 HTTPS 采集任务。目标通过 file_sd 自动发现，约 30～90 秒后出现。

新建节点也可直接运行 add-node，按提示先生成证书包；在另一终端完成远端安装后，再回到提示处继续。

## 打开 Grafana

Mac 终端运行（替换 SSH 信息）：

```bash
ssh -N -T -p SSH端口 \
  -L 127.0.0.1:13000:127.0.0.1:3000 \
  -L 127.0.0.1:19090:127.0.0.1:9090 \
  用户名@监控机IP
```

打开 http://127.0.0.1:13000 ，账号 admin，初始密码在监控机 `/opt/vps-toolkit-monitor/.env`；修改 .env 不会重置已初始化数据库的密码。不要发送密码或私钥给他人。

仪表盘位于 **VPS monitoring / VPS overview**，包括 CPU、内存、根分区、网卡收发速率和采集状态。Prometheus 状态页为 http://127.0.0.1:19090/targets 。Mac 关闭只影响查看，不停止采集。

## 从旧 SSH 方案迁移

不需要重装集中端，数据卷、密码和 CA 均保留。对每台旧节点：

1. 在监控机 prepare-node，**使用原来的节点名称**。
2. 将证书包传到远端，node-install --bundle 把旧的回环 HTTP 采集器迁到 HTTPS 19100。
3. 在监控机 add-node，仍输入原名称。

远端切换监听期间会有短暂采集缺口。只有 HTTPS 验证及 Prometheus 配置校验成功后，才删除旧目标并停用该名称对应的 SSH 隧道；失败会保留旧目标和隧道配置，但远端已切到 HTTPS 时，旧 HTTP 目标暂时无法采集，需修复新连接或恢复旧配置。

已有自定义 scrape_config_files 时，脚本不会覆盖；按提示添加 `/etc/prometheus/https/*.yml`。旧预置仪表盘和用户修改会保留，另提供 **VPS overview (HTTPS)**，可同时显示旧 `node` 和新 `node-https` 任务。

旧 SSH 专用账号、公钥及 known_hosts 不自动删除。全部迁移完成后，可在远端手动撤销旧授权；新方案不再需要这些监控 SSH 凭据。

## 证书维护

```bash
vt monitor cert-status          # 查看本地签发副本有效期，30 天内到期会提示
vt monitor cert-renew client    # 续签客户端证书，并重启 Prometheus
vt monitor cert-renew 节点名    # 生成新的节点证书包
```

CA 默认 10 年，服务器和客户端证书默认 1 年；不提供无人值守续签，建议定期检查。节点续签后，必须把新包传过去，再次 node-install --bundle；仅在监控机生成新包不会更新远端。证书状态显示本地签发副本，不代表远端已安装新证书。

CA 私钥在 `/opt/vps-toolkit-monitor/pki`（root 私有，未挂载进容器），客户端密钥仅由监控机使用。妥善加密备份这些文件；不要提交到公开仓库。CA 文件不完整时停止，不会静默生成另一套 CA。CA 泄露或到期需要重新规划全部节点信任迁移，不属于普通叶证书续签。

监控机源 IP 改变时：在 prepare-node 中填写原节点名称，确认更新地址/源 IP 并生成新包；远端重新安装以更新源 IP 限制，然后 add-node 验证连接；重装节点仍可重新安装同一套未过期证书包。证书包包含私钥，泄露后需按影响范围更换证书/信任。

## 日常管理与停止

```bash
vt monitor status
vt monitor logs
vt monitor stop
vt monitor start
vt monitor restart
vt monitor remove-node
vt monitor uninstall
```

集中端卸载只执行 docker compose down，不删数据卷、配置和证书。节点卸载先停止采集器，再移除本模块防火墙服务与规则，保留证书和二进制；原有 UFW、firewalld 和云安全组规则不动。remove-node 仅停止采集，不撤销证书，远端不再使用时应在那里卸载。

初期逐台接入约 3～5 台，观察 24～48 小时。available 持续低于约 150 MiB、Swap 持续换入换出、容器重启或代理体验变差时，先只停 Grafana：

```bash
cd /opt/vps-toolkit-monitor
docker compose stop grafana
```

VPS 关机或网络断开期间仍然无法采集，Node Exporter 不补传历史。此模块仅监控主机状态，不自动验证 Reality、ISP 分流、AI 请求或用户级流量。通知需另配 Grafana 告警规则和 Contact point。

## 验证与资料

离线回归：`python3 -m unittest discover -s tests -v`。真实 mTLS 集成：`python3 tests/monitoring_https_integration.py`，使用自动清理的临时 Docker 容器。防火墙集成在 CI 的独立网络命名空间中执行，验证指定源可连、其他源被丢弃。

- [Node Exporter](https://github.com/prometheus/node_exporter)
- [Exporter Toolkit TLS 配置](https://github.com/prometheus/exporter-toolkit/blob/master/docs/web-configuration.md)
- [Prometheus 配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
- [Grafana 资源要求](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)
