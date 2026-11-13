# 多 VPS 监控：Prometheus + Grafana

主菜单 **7 → 监控管理**，或直接运行 `vt monitor`。本模块与节点部署独立，不修改 3x-ui、Xray 和现有分流配置，不开放监控公网端口；首次安装 Docker 时，Docker 可能建立自身的网络规则。

## 每台机器安装什么

| 机器 | 组件 |
|---|---|
| 集中监控机（仅一台） | Prometheus、Grafana、Node Exporter，使用 Docker Compose v2 |
| 其他入口 VPS / ISP 落地机 | 仅 Node Exporter，使用二进制 + systemd，无需 Docker |
| 本地 Mac | 浏览器和 SSH，无需运行采集或数据库 |

监控机通过 SSH 主动连接各节点，再读取远端 `127.0.0.1:9100/metrics`。节点不向公网开放采集端口，Mac 不需要公网 IP。

安装要求：Linux + systemd、root；节点端支持 x86_64 / ARM64。需要能访问 GitHub Releases；集中端需要能拉取 Docker Hub 和 Quay 镜像。缺少 Docker 时会提示，并在确认后运行 Docker 官方安装脚本；已有 Docker 时不重装。未自动安装成功时，请按 Docker 官方对应发行版的文档安装 Engine 和 Compose v2。

## 1 GB 监控机的试运行配置

| 项目 | 默认值 |
|---|---|
| 采集间隔 | 60 秒 |
| 历史保留 | 7 天 |
| 历史块大小保留上限 | 2 GB；另留 WAL、当前块及压缩临时空间 |
| 最低磁盘可用空间检查 | /opt 下 5 GiB |
| 监控机容器内存上限 | Prometheus 256 MiB、Grafana 192 MiB、Node Exporter 48 MiB |
| 远端 Node Exporter 内存上限 | systemd MemoryMax=64M |
| 管理端口 | 127.0.0.1:3000、9090、9100 |

496 MiB 是集中端三个容器的合计硬上限，不是常驻占用，也不含 Docker、SSH 和现有代理。Grafana 限额低于官方最低内存建议，只适用于这次紧凑试运行，可能触发 OOM。安装 Docker 后可用内存低于 600 MiB 时会再次提示；启动 Prometheus 后不足 300 MiB 时，暂不启动 Grafana。

保留时间/大小主要控制磁盘，不等于内存限制或整个数据目录的绝对大小上限。减少指标数量、并发查询和目标数量更有助于控制内存。

## 安装顺序

### 1. 搬瓦工安装集中端

```bash
vt monitor hub-install
vt monitor status
vt monitor access
```

也可以使用 `bash vps.sh monitor ...`。未安装 `vt` 时，可通过仓库 README 的一行入口运行相同参数。

配置持久保存在 `/opt/vps-toolkit-monitor`，不会依赖一行入口下载的临时目录。再次执行 hub-install 会保留现有配置和密码；如果上次拉镜像或启动中断，排查后用 `vt monitor start` 恢复。

### 2. 从 Mac 查看

```bash
ssh -N -T -p SSH端口 \
  -L 127.0.0.1:13000:127.0.0.1:3000 \
  -L 127.0.0.1:19090:127.0.0.1:9090 \
  用户名@监控机IP
```

浏览器打开 http://127.0.0.1:13000 ，账号 `admin`。初始密码在监控机 `/opt/vps-toolkit-monitor/.env` 中，仅在自己的终端查看。修改该文件不会重置已初始化的 Grafana 密码。

数据源和 **VPS monitoring / VPS overview** 仪表盘已预置，包含 CPU、内存、根分区、网卡收发速率和采集状态。默认显示最近 1 小时，每分钟刷新。速率图需要积累至少两个样本。网卡分别显示，避免虚拟网卡与公网网卡相加重复统计。

Prometheus 目标状态：http://127.0.0.1:19090/targets 。Mac 关机不会停止服务器采集。

### 3. 授权远端节点

在监控机执行：

```bash
vt monitor public-key
```

复制输出的 `ssh-ed25519 ...` 公钥，然后在 ISP / 其他 VPS 执行：

```bash
vt monitor node-install --public-key 'ssh-ed25519 AAAA替换成真实公钥 vps-toolkit-monitor'
```

也可从菜单选择“只安装采集器”，按提示粘贴公钥。留空则只装采集器，稍后可再次运行 node-install 授权。

节点端下载固定版本的官方二进制，校验官方 SHA256 清单，创建 `vps-toolkit-node-exporter.service`。监听 `127.0.0.1:9100`，不用开安全组端口。已有采集器占用 9100 时会停止安装，避免覆盖。

授权公钥会创建专用远端用户 `vps-monitor`，给该公钥配置本地转发目标限制，并禁止执行交互命令。请勿复制 Mac 私钥或监控机私钥到远端。

### 4. 集中端接入节点

```bash
vt monitor add-node
```

依次输入：

- 名称：如 `isp-exit`、`entry-us`；最多 32 字符，只用英文字母、数字、下划线和短横线。
- IP / 域名：远端 VPS 的地址。
- SSH 端口：默认 22，按实际配置修改。
- SSH 用户：默认 `vps-monitor`；也可使用已经授权该公钥、允许 TCP 转发的现有账号。
- 本地隧道端口：第一台 19101，后续 19102、19103，不能重复。

首次连接需要核对 SSH 主机密钥指纹；可在可信的远端终端执行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 查看。脚本不跳过主机密钥验证。远端 SSH 若配置了 AllowUsers、Match 或 DisableForwarding，需要允许专用账号及此转发用途。

隧道通过 systemd 保持并自动重连。只有成功读取真实 Node Exporter 指标后，才写入 Prometheus 目标文件；失败会清理本次隧道配置。约 30～90 秒后目标出现在面板，不必重启 Prometheus。

## 日常操作

```bash
vt monitor status       # 容器、隧道、内存状态
vt monitor logs         # 最近 100 行日志
vt monitor access       # 访问命令与密码文件位置，不直接输出密码
vt monitor stop         # 停止本机监控和本模块的隧道
vt monitor start        # 启动 / 恢复本机监控及隧道
vt monitor restart      # 重启
vt monitor remove-node  # 移除某远端采集及隧道，不卸载远端软件
vt monitor uninstall    # 移除本机服务，保留配置、数据和 SSH 授权
```

集中端卸载使用 `docker compose down`，不加 `-v`；可通过 start 恢复。节点端卸载停用并删除 systemd unit，保留二进制和专用账号，以便重装。卸载或移除节点不会自动撤销远端 SSH 公钥，需要在远端删除对应 authorized_keys 行才能撤销授权。

## 验证及资源不足时

先只监控本机，然后接 ISP，再接其他节点，初期约 3～5 台。观察 24～48 小时，覆盖代理使用高峰、图表查询、数据压缩和重启。注意：

- `vt monitor status` 中 available 是否持续低于约 150 MiB。
- `vmstat 1 10` 的 si / so 是否持续非零。
- 容器是否频繁重启，日志中是否出现 OOM、查询超时。
- 原有代理体验是否变差。

内存紧张先只停 Grafana，Prometheus 可继续采集：

```bash
cd /opt/vps-toolkit-monitor
docker compose stop grafana
```

如果 Prometheus 自身仍 OOM，先减少目标或指标，再评估更大监控机；不要仅扩大限额挤占代理内存。不要通过全局 Docker prune 清理空间，以免影响其他业务。

## 当前监控范围

本模块采集主机指标，不自动采集 3x-ui 用户流量，也不验证 Reality 握手、ISP 分流和 AI 请求。`up=0` 表示采集失败，可能是隧道、SSH 或网络故障，不能直接断言远端宕机。日志默认保留有限大小。

通知需要在 Grafana 中另配告警规则和 Contact point；本次不额外部署 Alertmanager、Loki 或容器采集，以控制资源。可从采集失败持续 3 分钟、内存使用率超过 85% 持续 5 分钟、磁盘超过 85% 持续 5 分钟开始配置。

固定版本见 `templates/monitoring/compose.yaml`，升级时先阅读上游发布说明，再修改已有部署中的镜像版本并拉取、重建。模板变动不会自动覆盖已部署的用户配置。

## 参考

- [Prometheus 存储及保留限制](https://prometheus.io/docs/prometheus/latest/storage/)
- [Grafana 安装与资源要求](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)
- [Node Exporter](https://github.com/prometheus/node_exporter)
- [Docker 官方安装文档](https://docs.docker.com/engine/install/)
