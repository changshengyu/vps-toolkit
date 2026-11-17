# Komari 监控

入口：**主菜单 9 → Komari 监控**，或直接运行 `vt komari`。现有主菜单 1～8 的编号不变，Prometheus/Grafana 功能保留。本模块是独立安装，不自动迁移、停止或卸载旧监控，也不修改 3x-ui、代理配置或防火墙。

## 部署顺序

### 1. 在监控机安装服务端

选择 **9 → 1 安装 / 恢复服务端并初始化管理员**，或：

```bash
vt komari server-install
```

支持 Ubuntu/Debian 等使用 systemd 的 Linux x86_64/ARM64。采用官方原生二进制，不依赖 Docker；固定 Komari **1.6.1**，下载后按官方 Release 提供的 SHA-256 校验。服务端使用独立非登录账号 `vps-komari`，默认仅监听 **127.0.0.1:25774**，首次可选择其他本机端口。

启动后在终端输入管理员用户名、密码及确认密码，通过 Komari 官方首次安装 API 完成本机初始化。密码需包含大写、小写和数字，长度为 8～256 个字符，不写入工具配置或日志；Komari 自己在数据库中管理账号。输入 `q` 可以暂缓初始化，此时仍只提供本机安装页面，重新选择第 1 项可继续。已有账号不会重置。

默认配置内存上限 256 MiB、Go 内存目标 128 MiB、CPU 上限 50%，这是限制值而非实际占用承诺。安装前检查 MemAvailable 至少 192 MiB。并行运行旧监控时，请先观察内存和 Swap；本工具不会自动停止其他服务。

### 2. 配置 HTTPS 域名

选择 **9 → 2**，或 `vt komari tunnel-install`。必须先完成管理员初始化。

1. 在 Cloudflare 控制台创建一个独立的 **remotely-managed Cloudflared Tunnel**。
2. 添加发布应用路由，使用新的子域名，例如 `komari.example.com`。
3. 服务类型选 **HTTP**，地址填 `127.0.0.1:25774`；如果第 1 步更换了端口，以菜单打印的端口为准。
4. 复制连接器安装命令中的 **Tunnel Token**。回到工具箱，输入 HTTPS 域名，隐藏粘贴完整 Token。

这里需要的是 Tunnel Token，不是 Cloudflare API Token，也不是 Agent Token。脚本会本地检查 Token 结构。DNS 和应用路由由 Cloudflare 控制台配置，工具箱不创建或修改 DNS、不接管已有 Grafana Tunnel。

连接器使用独立非登录账号 `vps-komari-tunnel` 和原生 systemd，固定 cloudflared **2026.11.0**。Token 存在独立私有目录，文件权限 0600，不出现在 unit 参数或命令行中；不监听公网端口。内部就绪接口为 `127.0.0.1:20246`，内存上限 96 MiB。默认使用 IPv4 连接 Cloudflare，避免不可用 IPv6 导致连接延迟。

菜单会等待最多 30 秒检查连接器是否已连上 Cloudflare。连接成功不代表 DNS、应用路由或网页登录已验证；请实际打开域名并检查 Agent 上报。连接未确认时配置会保留，连接器继续自动重连，可在菜单看日志或重新输入 Token。

如果为域名配置 Cloudflare Access，交互登录保护可能阻止 Agent 长连接；需要按官方 Agent 协议设计机器认证或单独的接入策略，不能把浏览器能登录等同于 Agent 能连接。

### 3. 在每台 VPS 安装 Agent

先在 Komari 网页后台添加服务器，复制该服务器的 Agent Token；也可使用后台自动发现密钥。

在**每台被监控 VPS**运行工具箱，选择 **9 → 3**，或：

```bash
vt komari agent-install
```

填写同一个 Komari HTTPS 域名，选择节点 Token 或自动发现密钥，隐藏输入凭据。默认每 5 秒上报，支持 1～300 秒。重复运行可以修改域名、认证方式、凭据和间隔；相同认证方式下留空保留原凭据。

固定 Agent **1.5.11**，root 运行以采集主机指标；默认开启 HTTPS 证书校验、关闭远程控制（网页终端和命令执行）、关闭自动更新，优先使用 IPv4。凭据只保存在本机 `agent/agent.json`（0600），不在启动命令或状态页显示。Agent 仅主动上报，不需要在该 VPS 开放新的监控入站端口。

服务运行不代表 Token 有效或已接入成功，请在 Komari 后台确认在线、名称和指标。节点名称、分组、删除、通知及插件都在 Komari 网页管理；本菜单负责本机服务部署和生命周期。

**监控机本身也需要安装一个 Agent，才会出现在 Komari 的服务器列表中。**

## 访问与管理

```bash
vt komari access
vt komari status
vt komari logs server
vt komari logs agent
vt komari logs tunnel
vt komari start server
vt komari stop agent
vt komari restart tunnel
vt komari uninstall agent
```

菜单 **9 → 6** 可选择服务端、Agent 或连接器，再启动、停止、重启、看脱敏日志或卸载。启动会开启开机自启，停止会取消自启；异常退出后 systemd 自动重启。卸载只停止并移除本模块对应 unit，保留配置、数据库和二进制；再次执行对应安装命令可恢复。卸载 Agent 不会删除 Komari 后台的服务器记录，需要在网页后台删除。停止服务端会中断所有 Agent 上报。

无需域名时可用 SSH 访问页面；在 Mac 运行（替换实际 SSH 端口和监控机 IP）：

```bash
ssh -N -T -p SSH端口 -L 127.0.0.1:15774:127.0.0.1:25774 root@监控机IP
```

浏览器打开 `http://127.0.0.1:15774/`。SSH 适合临时访问页面，远端 Agent 仍需要可访问的 HTTPS 接入域名。不要为此向公网开放 25774。

## 数据、备份与迁移

本模块目录为 `/opt/vps-toolkit-komari`：

- `bin/`：已校验的官方二进制。
- `config/`：角色、版本、端口及入口记录（不含管理员密码或 Agent Token）。
- `server/data/`：Komari 数据库、监控历史、主题、插件和插件数据。
- `agent/agent.json`：Agent 凭据与配置。
- `tunnel/token`：Cloudflare Tunnel 连接 Token。

systemd unit 名称分别为 `vps-toolkit-komari-server`、`vps-toolkit-komari-agent`、`vps-toolkit-komari-tunnel`。已有 `/opt/komari`、官方 `komari.service` 或其他非本模块目录不会被接管。

安装会先使用本机 `systemd-analyze verify` 校验生成的 unit（系统未提供该命令时会提示跳过）。启动失败时先显示 systemd 状态和最近的脱敏日志，再回退本次 unit 与管理配置。修复原因后重新选择对应安装项即可重试，已有账号、数据库和已校验二进制会保留。若看到 `WorkingDirectory= path is not absolute` 且路径带双引号，请更新工具后重试安装；不需要开放公网端口。

**现有工具箱的 Prometheus/Grafana 加密 Telegram 自动备份不包含 Komari，本模块尚未新增自动备份/恢复功能。** Komari 1.6.1 的内置备份白名单存在 `plugin-data` 拼写问题，安装插件后应单独确认备份覆盖范围和恢复结果。做完整文件备份时先停止本模块服务端，备份整个 `server/` 目录以保证数据库一致性；备份含敏感配置，应加密后保存到监控机之外，再启动服务。

建议先接入 1～2 台节点，验证上线/离线、指标、Telegram 通知插件及资源告警，再考虑停用旧监控。Grafana 告警、联系点、Prometheus 历史和证书不能直接导入 Komari。旧工具箱也不会因新模块安装而自动删除它们。

## 验证

单元与菜单回归：`python3 -m unittest discover -s tests -v`。真实程序验证：`python3 tests/komari_integration.py`，下载并校验官方固定版本，测试首次初始化 API、页面访问、数据库重启保留和 Agent/连接器 CLI 参数，不连接真实节点或 Cloudflare。

真实 unit 解析验证：`python3 tests/komari_systemd_validation.py`，覆盖三种角色、普通路径及含空格/百分号路径，并验证旧的引号写法被拒绝；CI 同时在 Ubuntu 22.04 的 systemd 249 和当前 runner 的 systemd 上运行。不启动服务、不连接真实节点。

参考：[Komari](https://github.com/komari-monitor/komari)、[Agent](https://github.com/komari-monitor/komari-agent)、[官方首次安装接口](https://github.com/komari-monitor/komari/blob/1.6.1/web/install/install.go)。
