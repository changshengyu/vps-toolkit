# Komari 监控

入口：**主菜单 7 → Komari 监控**，或直接运行 `vt komari`。本模块是独立安装，不自动迁移、停止或卸载旧监控，也不修改 3x-ui、代理配置或防火墙。

## 部署顺序

### 1. 在监控机安装服务端

选择 **7 → 1 安装 / 恢复服务端并初始化管理员**，或：

```bash
vt komari setup-install
```

支持 Ubuntu/Debian 等使用 systemd 的 Linux x86_64/ARM64。采用官方原生二进制，不依赖 Docker；固定 Komari **1.6.1**，下载后按官方 Release 提供的 SHA-256 校验。服务端使用独立非登录账号 `vps-komari`，默认仅监听 **127.0.0.1:25774**，首次可选择其他本机端口。

启动后在终端输入管理员用户名、密码及确认密码，通过 Komari 官方首次安装 API 完成本机初始化。密码需包含大写、小写和数字，长度为 8～256 个字符，不写入工具配置或日志；Komari 自己在数据库中管理账号。输入 `q` 可以暂缓初始化，此时仍只提供本机安装页面，重新选择第 1 项可继续。已有账号不会重置。

默认配置内存上限 256 MiB、Go 内存目标 128 MiB、CPU 上限 50%，这是限制值而非实际占用承诺。安装前检查 MemAvailable 至少 192 MiB。并行运行旧监控时，请先观察内存和 Swap；本工具不会自动停止其他服务。

### 2. 配置 HTTPS 域名

在监控机选择 **7 → 1** 即可连续完成管理员初始化及 HTTPS 接入。输入尚无 DNS 记录的一级子域名和 Cloudflare API Token（账户 Tunnel 编辑、指定 Zone 的 DNS 编辑及 Zone 读取）。脚本创建独立 Tunnel、指向本机实际端口的路由和代理 CNAME。域名所在 Zone 需启用 Universal SSL，浏览器证书由 Cloudflare 签发并续期，VPS 不需要证书文件。API Token 不持久保存，已配置的域名会复用。旧版手动连接器也会保留；手动安装仍可用 `vt komari tunnel-install`。

连接器使用独立非登录账号 `vps-komari-tunnel` 和原生 systemd，固定 cloudflared **2026.11.0**。Token 存在独立私有目录，文件权限 0600，不出现在 unit 参数或命令行中；不监听公网端口。内部就绪接口为 `127.0.0.1:20246`，内存上限 96 MiB。默认使用 IPv4 连接 Cloudflare，避免不可用 IPv6 导致连接延迟。

菜单会等待最多 30 秒检查连接器是否已连上 Cloudflare。连接成功不代表 DNS、应用路由或网页登录已验证；请实际打开域名并检查 Agent 上报。连接未确认时配置会保留，连接器继续自动重连，可在菜单看日志或重新输入 Token。

如果为域名配置 Cloudflare Access，交互登录保护可能阻止 Agent 长连接；需要按官方 Agent 协议设计机器认证或单独的接入策略，不能把浏览器能登录等同于 Agent 能连接。

### 3. 在每台 VPS 安装 Agent

菜单 **7 → 2** 或 `vt komari agent-install`：粘贴面板提供的官方 wget/curl 一键安装命令，回车后核对面板地址并确认。命令隐藏输入，含密钥的安装输出不展示。只接受官方 GitHub main/install.sh 来源及受支持参数，按参数列表执行，不使用 eval。官方脚本决定安装版本、网页终端等行为，使用默认 /opt/komari 和 komari-agent.service；工具箱不再写 Agent 配置或身份目录。

已有官方服务由官方脚本更新，其原节点身份保留。若检测到旧 vps-toolkit Agent 服务会停止本次安装，先在菜单 5 卸载旧 Agent 服务，再复制面板已有节点的 Token 命令安装；自动发现可能创建新记录。状态查看仍同时识别两套服务。

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

菜单 **7 → 5** 可选择服务端、Agent 或连接器，再启动、停止、重启、看脱敏日志或卸载。启动会开启开机自启，停止会取消自启；异常退出后 systemd 自动重启。卸载只停止并移除本模块对应 unit，保留配置、数据库和二进制；再次执行对应安装命令可恢复。卸载 Agent 不会删除 Komari 后台的服务器记录，需要在网页后台删除。停止服务端会中断所有 Agent 上报。

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
