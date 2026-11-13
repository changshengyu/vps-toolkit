# 多 VPS 监控：Prometheus + Grafana + HTTPS mTLS

主菜单 **7 → 监控管理**，或运行 `vt monitor`。仅一台机器安装集中端，其他 VPS 只安装采集器。推荐在监控机依次选择 **1 安装监控机 → 2 自动添加节点 → 3 面板访问**。填节点域名和 SSH 登录信息后，签发证书、传输、远端安装及接入全部自动完成，无需手工传包。日常采集使用 HTTPS mTLS，部署完成后关闭 SSH 会话。

## 组件与网络

| 机器 | 组件及连接 |
|---|---|
| 集中监控机 | Docker Compose：Prometheus、Grafana、本机 Node Exporter |
| 入口 VPS / ISP 落地机 | Node Exporter 二进制、systemd、独立 nftables 源 IP 规则 |
| 本地 Mac | 通过 HTTPS 域名或 SSH 隧道打开 Grafana；不参与持续采集 |

```text
监控机 Prometheus ── HTTPS + 客户端证书 ──→ 节点直连域名:19100
浏览器 ── HTTPS ──→ Cloudflare ── 加密 Tunnel ──→ 监控机 Grafana
本地 Mac ── SSH 隧道 ──→ 监控机 Grafana（备用）
```

监控机验证节点证书链和 DNS / IP SAN；节点验证客户端证书、clientAuth 用途及监控身份 SAN。节点采集端口仅允许指定的监控机公网源 IP。支持直连域名、IPv4 或 IPv6。域名必须是指向对应 VPS 的 A/AAAA 记录（Cloudflare 灰云），不能使用 CDN 代理或多节点轮询。已有域名可直接使用：工具自动签发专用私有 CA 证书，不改现有网站证书，不需要 ACME 验证，也不需要释放 80/443。源 IP 是监控机的公网出口 IP，不是 Mac 的 IP。

安装要求：Linux + systemd、root、OpenSSL 1.1.1 或更新版本；节点端支持 x86_64 / ARM64。脚本使用 Python 3 标准库验证证书包，无常驻 Python 服务。节点还需要内核支持 nftables；无法安装或应用访问规则时会中止，不会降级成开放采集端口。

不修改 3x-ui、Xray、ISP 分流、sshd 配置文件或默认路由；保留原有登录公钥，只新增专用受限续期授权。采集使用独立端口，不占用 Reality / 静态站点的 443。首次安装 Docker 可能建立 Docker 自身网络规则。

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

## 推荐部署顺序（只在监控机操作）

### 1. 安装监控机

主菜单 7 → 监控管理 → **1 安装监控机**：

```bash
vt monitor hub-install
```

只在一台机器执行。配置保存在 `/opt/vps-toolkit-monitor`，不依赖入口下载的临时目录。重复执行不轮换密码和 CA；其他 VPS 不安装 Prometheus/Grafana。

### 2. 自动添加 / 部署节点

监控管理 → **2 添加 / 部署被监控节点**：

```bash
vt monitor deploy-node
```

按提示填节点名称、直连域名或 IP、监控机公网源 IP、采集端口（默认 19100）、SSH 用户和端口，以及监控机上的 SSH 私钥路径（可留空）。源 IP 会尝试自动检测，NAT 或 IPv6 环境请核对。

SSH 私钥路径是**监控机本地**的路径，不是 Mac 路径。留空会尝试默认密钥，也支持由 SSH 自身提示输入密码。密码不经过脚本参数，不会保存。root 可直接安装；非 root 账号需要免密 sudo。

首次连接需核对节点 SSH 指纹；如果使用密码登录，只在此次初始接入时输入一次。随后脚本自动：

1. 签发该节点 DNS/IP SAN 证书。
2. 将证书和当前工具代码快照通过同一 SSH 会话传入远端。
3. 远端自动安装 / 更新 Node Exporter、mTLS 及独立源 IP 防火墙规则。
4. 自动安装专用受限续期公钥，并验证它能独立登录，不依赖首次密码、原密钥或 SSH agent。
5. 验证 HTTPS 指标请求，校验 Prometheus 配置，再登记采集目标。
6. 删除传输临时文件并关闭 SSH 连接；后续只通过 HTTPS 采集。

不会把 CA 私钥、客户端私钥或 SSH 私钥传给节点。新增的续期公钥限定监控机源 IP，只能运行固定的证书更新程序，禁止 Shell、端口转发和 PTY。根据信任的 SSH 连接完成证书引导，无需额外手动比对证书包或在两台机器之间切换菜单。

**已有 UFW / firewalld 或云安全组的默认拒绝规则仍然有效。**需要精确放行“监控机源 IP → 节点 TCP 19100”；工具不覆盖其他防火墙策略，也不会忽略 HTTPS 验证失败。接入失败时保留旧采集目标，关闭本次 SSH 会话；如果远端已安装而云安全组未放行，修复后再次执行第 2 项即可。

同一名称再次执行可更新域名、源 IP、SSH 信息，或为重装后的机器重新部署。以后增加新节点只重复第 2 项。

### 3. 配置 / 打开监控面板

监控管理 → **3 面板访问（域名 / SSH）**。选择第 1 项自动配置 Cloudflare Tunnel，或第 2 项查看 SSH 访问方法：

```bash
vt monitor access
```

域名入口配置完成后直接用浏览器打开打印出的 HTTPS 地址，不需要 Mac 保持 SSH。监控管理第 4 项检查资源状态，第 5 项查看日志。

## 监控菜单顺序

```text
1. 安装监控机（首次，仅一台）
2. 添加 / 部署被监控节点（自动流程）
3. 面板访问（域名 / SSH）>
4. 查看状态和资源占用
5. 查看日志
6. 节点管理 / 手动接入 >
7. 证书管理 >
8. 启动 / 停止 / 重启 / 卸载 >
0. 返回
```

## 打开 Grafana

### 推荐：Cloudflare Tunnel 域名入口

在监控机运行 `vt monitor tunnel-setup`，或 **主菜单 7 → 3 面板访问 → 1 配置 Grafana 域名入口**。

1. 准备一个由 Cloudflare 托管的新子域名，例如 `monitor.yuchangsheng.top`。使用托管域名的一级子域名，以匹配 Universal SSL。**不用预先添加解析到 VPS IP 的 A/AAAA 记录**；已有同名记录会拒绝配置，不会覆盖。
2. 在 Cloudflare 创建自定义 API Token，限制到所选账户及域名：`Account / Cloudflare Tunnel / Edit`、`Zone / DNS / Edit`、`Zone / Zone / Read`。最后一项用于自动识别 Zone 和账户。不要使用 Global API Key。
3. 在 VPS 终端按提示输入域名、确认后隐藏输入 Token。脚本自动创建远程管理的 Tunnel、仅到 `http://127.0.0.1:3000` 的路由及橙云 CNAME，启动固定版本 `cloudflare/cloudflared:2026.11.0`。
4. 通过 `https://monitor.yuchangsheng.top/` 登录 Grafana；用户名 admin，已有密码不变。初始密码保存在 `/opt/vps-toolkit-monitor/.env`，只在自己的终端查看。

Cloudflare 自动签发和续签浏览器入口 HTTPS 证书；VPS 不需要 ACME、开放入站端口或占用 80/443。首次启用可能需要等待边缘证书生效。监控机需能出站连接 Cloudflare TCP/UDP 7844；默认优先 QUIC，受限时回退 HTTP/2。浏览器到 Cloudflare、Cloudflare 到 cloudflared 均加密，cloudflared 到 Grafana 仅走本机 HTTP。Prometheus 和 Node Exporter 不公开。

API Token 临时保存为 root 私有文件，操作结束删除，不进入命令参数、环境变量或 Compose。运行时仅保留该 Tunnel 的连接 Token，文件权限 0400、挂载只读，容器以 UID 65532 运行。`tunnel/` 目录权限 0700，包含资源归属记录，勿公开或删除；移除入口时会再次要求 API Token，日常运行、重启和 Cloudflare 证书续签不要求密码或 API Token。

新增 cloudflared 容器内存硬上限 96 MiB（不是实际占用承诺），CPU 上限 0.20 核；四个监控容器内存上限合计 592 MiB，不含 Docker 和代理。配置时要求 MemAvailable 至少 160 MiB。运行状态和日志可在菜单查看；断网 / 重启后容器自动重连。此入口仍需 Grafana 登录；如需仅本人可达，可在 Cloudflare 控制台额外配置 Access 应用和访问策略，本模块不会自动创建 Access 策略。

```text
面板访问子菜单（7 → 3）
1. 配置 Grafana 域名入口
2. 查看面板地址和 SSH 方法
3. 查看域名入口状态
4. 查看域名入口日志
5. 暂停域名入口，恢复 SSH 访问
6. 恢复域名入口
7. 移除域名入口（保留监控数据）
```

```bash
vt monitor tunnel-setup
vt monitor tunnel-status
vt monitor tunnel-logs
vt monitor tunnel-stop      # 停 cloudflared，恢复 Grafana 本机 HTTP 配置
vt monitor tunnel-start     # 使用已保存的 Tunnel Token 恢复，不重建 DNS
vt monitor tunnel-remove    # 再次输入 API Token，删除本工具的 CNAME 和 Tunnel
```

域名模式启用 Grafana HTTPS 根地址和 Secure Cookie。需要通过下面的 SSH HTTP 地址登录时，先运行 `tunnel-stop`；Prometheus 的 SSH 访问不受影响。配置 / 暂停会重建 Grafana 容器，有短暂面板中断，不删除数据卷、不重启 Prometheus、不改变采集、3x-ui 或 Xray。

创建失败时自动停止本次入口并尝试清理本次新建的云端资源；API 不可达或 Tunnel 仍显示在线时保留归属记录，可稍后 `tunnel-remove` 重试。清理只处理本工具记录的 Tunnel 及仍指向它的 CNAME，不删除后来改成其他目标的 DNS；Tunnel 被手工改名时停止删除并要求检查。不要在配置过程中手工删除本地 `tunnel/state.json`。集中端 `stop/uninstall` 停止连接器但保留云端记录和配置；彻底删除入口需要单独 `tunnel-remove`，Grafana / Prometheus 历史均保留。

### 备用：SSH 访问

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

不需要重装集中端，数据卷、密码和 CA 均保留。在监控机选择第 2 项，**使用原来的节点名称**，填写对应域名和 SSH 信息，工具自动安装并迁移。

远端切换监听期间会有短暂采集缺口。只有 HTTPS 验证及 Prometheus 配置校验成功后，才删除旧目标并停用该名称对应的 SSH 隧道；失败会保留旧目标和隧道配置，但远端已切到 HTTPS 时，旧 HTTP 目标暂时无法采集，需修复新连接或恢复旧配置。

已有自定义 scrape_config_files 时，脚本不会覆盖；按提示添加 `/etc/prometheus/https/*.yml`。旧预置仪表盘和用户修改会保留，另提供 **VPS overview (HTTPS)**，可同时显示旧 `node` 和新 `node-https` 任务。

旧 SSH 专用账号、公钥及 known_hosts 不自动删除。全部迁移完成后，可在远端手动撤销旧授权；新方案不再需要这些监控 SSH 凭据。

## 证书维护与自动续签

```bash
vt monitor cert-status          # 查看有效期
vt monitor renew-node           # 一键重新签发并同步远端节点
vt monitor cert-renew client     # 续签监控机客户端证书
```

CA 默认 10 年，服务器和客户端证书默认 1 年。成功接入后，安装每日检查的 systemd timer，30 天内到期才续签。运行代码快照保存在监控机持久目录中，不依赖临时下载目录，也不会在后台自动拉取未审阅的新代码。

所有自动接入的节点都会安装独立的 Ed25519 续期公钥；监控机私钥权限为 600，目录为 700。即使首次是密码登录，也不保存密码，后续证书续期不再需要输入密码。每天检查，距到期 30 天时自动签发、同步并验证；客户端证书也会自动续签。

远端续期入口只接收大小受限的证书包，校验原有 CA、节点身份、域名、端口及源 IP 完全一致，只能替换服务器证书和密钥，不能上传或执行安装脚本，不能改变防火墙配置。无法认证或验证时失败退出并记录日志，不会退回密码提示或跳过 SSH 指纹验证。

查看续期任务：`systemctl status vps-toolkit-monitor-renew.timer`；日志：`journalctl -u vps-toolkit-monitor-renew.service --no-pager`。监控机停止监控时会停用 timer；Prometheus 未运行时自动任务也会跳过，不会自行恢复停止的监控。

部署记录仅保存地址、用户名、SSH 端口和密钥路径，不保存密码或私钥内容。专用续期私钥保存在监控机 `/opt/vps-toolkit-monitor/renewal-keys`，请与 CA 一同妥善备份。SSH 指纹发生变化时不会跳过验证，需要手动核对后更新 known_hosts。证书状态显示本地签发副本，不代表手动安装的远端一定已更新。

CA 私钥在 `/opt/vps-toolkit-monitor/pki`（root 私有，未挂载进容器），客户端密钥仅由监控机使用。妥善加密备份这些文件；不要提交到公开仓库。CA 文件不完整时停止，不会静默生成另一套 CA。CA 泄露或到期需要重新规划全部节点信任迁移，不属于普通叶证书续签。

监控机源 IP 改变时：重新选择第 2 项，填写原节点名称并更新源 IP，工具自动重新部署并验证；重装节点仍可重新安装同一套未过期证书包。证书包包含私钥，泄露后需按影响范围更换证书/信任。

## 高级：不授予 SSH 安装权限时手动接入

位于“6 节点管理 / 手动接入”，保留原来的 prepare-node、node-install、add-node 命令。手动流程仍可生成证书包、自行安全传输、在远端核对 CA 指纹安装，再在监控机验证接入。普通使用无需进入此流程。

```bash
vt monitor prepare-node
# 将生成的节点证书包安全传到对应远端后执行：
vt monitor node-install --bundle /安全目录/节点名.tar.gz
# 回到监控机验证：
vt monitor add-node
```

节点安装校验证书链、用途、DNS/IP SAN、有效期及密钥匹配，拒绝路径穿越和链接；默认使用独立 `inet vps_toolkit_monitor` 表限制指定源 IP。手动安装的节点证书需要手动传包续签，或用 deploy-node 转为自动管理。

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

集中端卸载会停用并移除续期 timer，执行 docker compose down，不删数据卷、配置、证书和部署记录。节点卸载先停止采集器、撤销本工具的续期公钥，再移除本模块防火墙服务与规则，保留证书和二进制；原有 UFW、firewalld 和云安全组规则不动。remove-node 停止采集并删除自动续期记录，不撤销证书，远端不再使用时应在那里卸载。

初期逐台接入约 3～5 台，观察 24～48 小时。available 持续低于约 150 MiB、Swap 持续换入换出、容器重启或代理体验变差时，先只停 Grafana：

```bash
cd /opt/vps-toolkit-monitor
docker compose stop grafana
```

VPS 关机或网络断开期间仍然无法采集，Node Exporter 不补传历史。此模块仅监控主机状态，不自动验证 Reality、ISP 分流、AI 请求或用户级流量。通知需另配 Grafana 告警规则和 Contact point。

## 验证与资料

离线回归：`python3 -m unittest discover -s tests -v`。真实 mTLS 集成：`python3 tests/monitoring_https_integration.py`，使用自动清理的临时 Docker 容器。SSH 传输集成：`python3 tests/monitoring_ssh_integration.py`，使用真实 sshd 和无害的安装桩验证一次认证与安装流，不操作真实 VPS。Tunnel 回归使用模拟 API 验证 DNS 冲突、失败回滚、断网重试及凭据隐藏；`python3 tests/monitoring_tunnel_integration.py` 使用真实 cloudflared CLI 和 Grafana 容器验证 Compose 合并、HTTPS 根地址 / Cookie、暂停后的 SSH 登录和密码保留，不使用真实 Cloudflare 账户。该功能集成测试容器单独使用 256 MiB / 1 CPU 以覆盖首次初始化开销，不代表 192 MiB 紧凑试运行参数的容量保证；低配机器仍需观察内存和重启情况。防火墙集成在 CI 的独立网络命名空间中执行，验证指定源可连、其他源被丢弃。

- [Node Exporter](https://github.com/prometheus/node_exporter)
- [Exporter Toolkit TLS 配置](https://github.com/prometheus/exporter-toolkit/blob/master/docs/web-configuration.md)
- [Prometheus 配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
- [Grafana 资源要求](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)

- [Cloudflare API 自动创建 Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/)
- [cloudflared 运行参数](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)
- [Grafana 反向代理配置](https://grafana.com/tutorials/run-grafana-behind-a-proxy/)
