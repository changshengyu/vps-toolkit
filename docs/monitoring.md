# 多 VPS 监控：Prometheus + Grafana + HTTPS mTLS

主菜单 **7 → 监控管理**，或运行 `vt monitor`。仅一台机器安装集中端，其他 VPS 只安装采集器。推荐在监控机依次选择 **1 安装监控机 → 2 自动添加节点 → 3 Grafana 面板**。填节点域名和 SSH 登录信息后，签发证书、传输、远端安装及接入全部自动完成，无需手工传包。日常采集使用 HTTPS mTLS，部署完成后关闭 SSH 会话。

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
| 集中端容器内存上限 | Prometheus 256 MiB、Grafana 256 MiB、Node Exporter 48 MiB |
| 远端采集器内存上限 | systemd MemoryMax=64M |
| 集中端管理端口 | 仅 127.0.0.1:3000、9090、9100 |
| 远端 HTTPS 采集端口 | 默认 TCP 19100，仅指定源 IP + 客户端证书可访问 |

560 MiB 是三个集中端容器合计硬上限，不是实际常驻占用，也不包含 Docker、现有代理等。Grafana 的 256 MiB 低于官方最低建议，仅供紧凑试运行，可能 OOM。安装 Docker 后 available 低于 600 MiB 会提示；启动 Prometheus 后不足 300 MiB 时暂不启动 Grafana。

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

按提示填节点显示名称、直连域名或 IP、监控机公网源 IP、采集端口（默认 19100）、SSH 用户和端口，以及监控机上的 SSH 私钥路径（可留空）。源 IP 会尝试自动检测，NAT 或 IPv6 环境请核对。

节点显示名称支持 **中文、空格、点号和斜杠**，例如 `香港/CMI.01`、`US.CN2 / 备用`，最长 128 个字符（最多 512 字节），不接受换行或控制字符，`client` 为证书操作保留名称。Grafana 使用完整显示名称；工具自动生成稳定的内部标识存储文件和证书，不把显示名称当作路径。原有简短字母数字名称继续使用原标识。添加、移除、续签和手动接入都可以输入同一个显示名称，也可用状态中显示的内部标识定位。

已有节点继续输入**原名称**即可更新，不需要重装；此功能用于放宽新名称输入，不把另一个名称自动识别为原节点的改名。高级手动安装包的文件名使用内部标识，以脚本打印的路径为准。

SSH 私钥路径是**监控机本地**的路径，不是 Mac 路径。留空会尝试默认密钥，也支持由 SSH 自身提示输入密码。密码不经过脚本参数，不会保存。root 可直接安装；非 root 账号需要免密 sudo。

首次连接需核对节点 SSH 指纹；如果使用密码登录，只在此次初始接入时输入一次。随后脚本自动：

1. 签发该节点 DNS/IP SAN 证书。
2. 将证书和当前工具代码快照通过同一 SSH 会话传入远端。
3. 远端自动安装 / 更新 Node Exporter、mTLS 及独立源 IP 防火墙规则。
4. 自动安装专用受限续期公钥，并验证它能独立登录，不依赖首次密码、原密钥或 SSH agent。
5. 验证 HTTPS 指标请求，校验 Prometheus 配置；需要首次加载 HTTPS 任务时通过 SIGHUP 热加载并确认运行中的任务池已生效，已有任务则直接使用文件发现，不重启 Prometheus / Grafana / Tunnel。
6. 登记采集目标和续期记录，等待 Prometheus 产生本次新的 `up=1` 样本后确认接入；删除传输临时文件并关闭 SSH 连接，后续只通过 HTTPS 采集。

不会把 CA 私钥、客户端私钥或 SSH 私钥传给节点。新增的续期公钥限定监控机源 IP，只能运行固定的证书更新程序，禁止 Shell、端口转发和 PTY。根据信任的 SSH 连接完成证书引导，无需额外手动比对证书包或在两台机器之间切换菜单。

**已有 UFW / firewalld 或云安全组的默认拒绝规则仍然有效。**需要精确放行“监控机源 IP → 节点 TCP 19100”；工具不覆盖其他防火墙策略，也不会忽略 HTTPS 验证失败。接入失败时保留旧采集目标，关闭本次 SSH 会话；如果远端已安装而云安全组未放行，修复后再次执行第 2 项即可。

同一名称再次执行可更新域名、源 IP、SSH 信息，或为重装后的机器重新部署。以后增加新节点只重复第 2 项，**不需要手动重启整套监控服务**。

默认每 60 秒采集一次。脚本等待最长约 120 秒确认对应节点和地址的本次 `up=1`，不会把旧样本当成本次成功。CPU / 网络等速率图至少需要两次采集，再加 Grafana 自动刷新，接入成功后可能还需等 1～2 分钟看到完整图表。

若直接 HTTPS 验证成功但 Prometheus 未确认采集成功，会明确报错并显示目标 health / lastError，目标和自动续期记录保留供排查；已有旧目标在新采集确认前保留，不会通过反复重启 Grafana 掩盖问题。首次热加载失败会恢复先前磁盘配置并尝试重新加载。

### 3. 配置 / 打开监控面板

监控管理 → **3 Grafana 面板 → 1 查看面板地址**。需要配置 HTTPS 域名时，选择监控管理 **4 Cloudflare 域名入口 → 1 配置**：

```bash
vt monitor access
```

域名入口配置完成后直接用浏览器打开打印出的 HTTPS 地址，不需要 Mac 保持 SSH。监控管理第 5 项集中查看状态与日志。

## 监控菜单顺序

```text
1. 安装监控机（首次，仅一台）
2. 添加 / 部署被监控节点（自动流程）
3. Grafana 面板（访问 / 账号 / 诊断）>
4. Cloudflare 域名入口 >
5. 状态与日志 >
6. 节点与证书 >
7. 高级手动接入 >
8. 启动 / 停止 / 重启 / 卸载 >
9. 备份与恢复（Telegram）>
0. 返回
```

所有页面支持 `0` 返回上级、`m` 回主菜单、`q` 退出，当前位置显示在页头。自动接入集中在第 2 项；证书与移除操作集中在第 6 项；手动传包流程集中在第 7 项。

监控管理第 **9** 项提供加密 Telegram 自动备份、立即发送、状态 / 日志、全新监控机恢复和定时开关。备份包含 Grafana 账号、告警、联系点、节点与证书配置，默认不含 Prometheus 历史指标；解密密钥必须另存到 Mac。详见 [自动备份与恢复指南](monitoring-backup.md)。

## 打开 Grafana

### 推荐：Cloudflare Tunnel 域名入口

在监控机运行 `vt monitor tunnel-setup`，或 **主菜单 7 → 4 Cloudflare 域名入口 → 1 配置 Grafana 域名入口**。

1. 准备一个由 Cloudflare 托管的新子域名，例如 `monitor.yuchangsheng.top`。使用托管域名的一级子域名，以匹配 Universal SSL。**不用预先添加解析到 VPS IP 的 A/AAAA 记录**；已有同名记录会拒绝配置，不会覆盖。
2. 在 Cloudflare 创建自定义 API Token，限制到所选账户及域名：`Account / Cloudflare Tunnel / Edit`、`Zone / DNS / Edit`、`Zone / Zone / Read`。最后一项用于自动识别 Zone 和账户。不要使用 Global API Key。
3. 在 VPS 终端按提示输入域名、确认后粘贴 Token 并按一次回车；输入期间不显示字符，随后提示“Token 已接收”。每个 API 步骤显示进度，网络请求有 30 秒硬超时（含 DNS / 连接 / 响应等待），curl 支持 IPv4 / IPv6 回退。脚本自动创建远程管理的 Tunnel、仅到 `http://127.0.0.1:3000` 的路由及橙云 CNAME，启动固定版本 `cloudflare/cloudflared:2026.11.0`。
4. 通过 `https://monitor.yuchangsheng.top/` 登录 Grafana；用户名 admin，已有密码不变。初始密码保存在 `/opt/vps-toolkit-monitor/.env`，只在自己的终端查看。

Cloudflare 自动签发和续签浏览器入口 HTTPS 证书；VPS 不需要 ACME、开放入站端口或占用 80/443。首次启用可能需要等待边缘证书生效。监控机需能出站连接 Cloudflare TCP/UDP 7844；默认优先 QUIC，受限时回退 HTTP/2。浏览器到 Cloudflare、Cloudflare 到 cloudflared 均加密，cloudflared 到 Grafana 仅走本机 HTTP。Prometheus 和 Node Exporter 不公开。

API Token 临时保存为 root 私有文件，操作结束删除，不进入命令参数、环境变量或 Compose。运行时仅保留该 Tunnel 的连接 Token，文件权限 0400、挂载只读，容器以 UID 65532 运行。`tunnel/` 目录权限 0700，包含资源归属记录，勿公开或删除；移除入口时会再次要求 API Token，日常运行、重启和 Cloudflare 证书续签不要求密码或 API Token。

新增 cloudflared 容器内存硬上限 96 MiB（不是实际占用承诺），CPU 上限 0.20 核；四个监控容器内存上限合计 656 MiB，不含 Docker 和代理。配置时要求 MemAvailable 至少 160 MiB。运行状态和日志可在菜单查看；断网 / 重启后容器自动重连。此入口仍需 Grafana 登录；如需仅本人可达，可在 Cloudflare 控制台额外配置 Access 应用和访问策略，本模块不会自动创建 Access 策略。

```text
Grafana 面板（7 → 3）
1. 查看面板地址与 SSH 访问方法
2. 修改登录账号 / 密码
3. 检查面板健康（502 / 无法连接）
4. 调整 Grafana 资源 / 修复启动

Cloudflare 域名入口（7 → 4）
1. 配置 Grafana 域名入口
2. 查看域名入口状态
3. 查看域名入口日志
4. 恢复域名入口
5. 暂停域名入口，恢复 SSH 访问
6. 移除域名入口（保留监控数据）
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

### Grafana 登录信息与 502 排查

**监控管理 → 3 Grafana 面板 → 2 修改登录账号 / 密码**，或 `vt monitor grafana-account`。输入当前用户名、当前密码、新用户名及新密码；用户名或新密码留空表示保留，修改密码时需要再次确认。当前密码留空时只尝试安装时生成的初始密码；已在网页改过密码时需要输入当前密码。新密码至少 12 个字符、最多 128 字节。

脚本调用 Grafana **本机** User API 修改已有管理员，并验证新凭据，不靠修改 `.env` 重置数据库。密码通过 stdin 传给 Python，只在进程内用于本机认证，不作为命令参数、环境变量或账户记录保存。`grafana-account.json` 只保存用户名、用户 ID、初始密码是否已失效；修改后“查看面板地址”显示记录的用户名并提示使用当前密码。若用户名步骤成功、密码步骤失败，会保留新用户名记录并明确提示当前状态；修改前检查用户名冲突。

Cloudflare Tunnel 的 502 常见于 `cloudflared` 已连接，但 Grafana 未接受本机连接。**不用对公网开放 3000**：本模块两个容器都使用宿主机网络，转发目标是 `http://127.0.0.1:3000`。排查时先运行：

```bash
vt monitor grafana-check
vt monitor tunnel-status
```

这会检查本机 HTTP 接口、容器重启次数、最近 OOM 标志和资源上限；`OOM=false` 不足以排除之前发生过内存压力。Tunnel 的连接就绪与 Grafana 就绪分别检查，只有两者通过才报告入口设置完成。初次重建 Grafana 后会等待约 4 分钟，再按失败流程清理或提示。

旧安装的 Grafana 上限为 192 MiB，曾在实际 VPS / amd64 测试中出现启动重启。新版新安装改为 **256 MiB、0.5 CPU**；已有安装保留原配置，可选择 **3 Grafana 面板 → 4 调整 Grafana 资源 / 修复启动**，或执行：

```bash
vt monitor grafana-resources
# 默认 256 MiB，也可选 320 / 384 / 512。
```

操作前检查当前 MemAvailable，至少留出“上限增加量 + 96 MiB”的余量，再确认应用。只重建 Grafana，等待本机接口就绪，不停止 Prometheus 或修改代理。配置写入独立 `compose.grafana.yaml`，后续主菜单启动、域名暂停 / 恢复都继续加载，账号和数据卷保留。已存在的非本工具同名配置拒绝覆盖。

256 MiB 仍是低配试运行值，不能保证所有负载下稳定；资源不足时先减少负载或分离面板，不自动提高上限。若本机接口已正常而仍 502，再检查 Tunnel 日志中的 origin 地址和错误，不通过开放公网端口处理。

### Token 输入后看似卡住时

先确认已按回车。新版会立即显示“Token 已接收”和当前 API 步骤；网络超时会明确报错，已有资源仍按归属记录清理或保留重试。不要在未看到 shell 提示符时反复输入 Token 或粘贴命令，也不要删除 `tunnel/state.json`。

可在另一个 SSH 窗口执行只读检查，不需要提供 Token：

```bash
pgrep -af '[p]ython3 .*/lib/monitor/cloudflare.py' || true
curl -4 -sS --connect-timeout 5 --max-time 15 -o /dev/null \
  -w 'HTTP=%{http_code} IP=%{remote_ip} 耗时=%{time_total}s\n' \
  https://api.cloudflare.com/client/v4/
```

curl 此处未认证，收到 401/403/404 仍能确认 HTTPS 收到响应，不能据此判断 API Token 权限。输出 curl 错误码 6 是解析失败、28 是网络超时；下一步再检查 DNS 或出站 HTTPS。旧版长时间无输出时可按一次 Ctrl+C，等待回滚结束再用新版本查看 `tunnel-status`；中断后的残留记录用 `tunnel-remove` 清理，勿直接覆盖。

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

位于“7 高级手动接入”，保留原来的 prepare-node、node-install、add-node 命令。手动流程仍可生成证书包、自行安全传输、在远端核对 CA 指纹安装，再在监控机验证接入。普通使用无需进入此流程。

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

离线回归：`python3 -m unittest discover -s tests -v`。真实 mTLS 集成：`python3 tests/monitoring_https_integration.py`，使用自动清理的临时 Docker 容器，验证首次 SIGHUP 热加载、运行中新增 / 移除特殊名称目标及新 `up=1` 样本，检查容器启动时间及重启次数保持不变。SSH 传输集成：`python3 tests/monitoring_ssh_integration.py`，使用含中文、点号、斜杠显示名称的真实 sshd 和无害安装桩验证一次认证与安装流，不操作真实 VPS。Tunnel 回归使用模拟 API 验证 DNS 冲突、失败回滚、断网重试及凭据隐藏；`python3 tests/monitoring_tunnel_integration.py` 使用真实 cloudflared CLI 和 Grafana 容器验证 Compose 合并、HTTPS 根地址 / Cookie、暂停后的 SSH 登录和密码保留，不使用真实 Cloudflare 账户。该功能集成测试使用与生产相同的 256 MiB / 0.5 CPU 上限，验证真实账户改名 / 改密、旧密码拒绝、新凭据在域名暂停后保留，以及持久资源覆盖；低配机器仍需观察内存和重启情况。防火墙集成在 CI 的独立网络命名空间中执行，验证指定源可连、其他源被丢弃。

- [Node Exporter](https://github.com/prometheus/node_exporter)
- [Exporter Toolkit TLS 配置](https://github.com/prometheus/exporter-toolkit/blob/master/docs/web-configuration.md)
- [Prometheus 配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
- [Grafana 资源要求](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)

- [Cloudflare API 自动创建 Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel-api/)
- [cloudflared 运行参数](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)
- [Grafana 反向代理配置](https://grafana.com/tutorials/run-grafana-behind-a-proxy/)
