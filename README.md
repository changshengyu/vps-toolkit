# vps-toolkit

个人 VPS 配置工具集。一条命令把一台新 VPS 部署成可用的 3x-ui 节点：安装面板、申请证书、放行端口、导入入站和分流规则，最后输出分享链接。

## 功能

| 功能 | 命令 | 说明 |
|---|---|---|
| 一键部署节点 | `deploy` | 安装 3x-ui，生成 4 个入站和分流规则，详见[文档](docs/deploy.md) |
| 旧机器更新配置 | `deploy --config-only` | 把已有 3x-ui 的出站、路由和 geodata 统一到当前版本，不动入站 |
| 路由域策略 | `routing` | 只把已有 3x-ui 的路由域策略改为 `IPIfNonMatch` |
| geodata 自动更新 | `geodata` | 只为已有 3x-ui 开启 / 关闭 geoip、geosite 自动更新 |
| Hysteria2 端口跳跃 | `hy2` | 用 nftables 重定向端口，重启后自动恢复，详见[文档](docs/hy2-port-hopping.md) |
| 3x-ui 面板 | `3x-ui` | 运行 [3x-ui](https://github.com/MHSanaei/3x-ui) 官方安装脚本，详见[文档](docs/3x-ui.md) |
| 多 VPS 监控 | `monitor` | 集中端 Prometheus + Grafana、节点端 Node Exporter，HTTPS 双向证书认证、源 IP 限制，详见[文档](docs/monitoring.md) |
| 监控备份与恢复 | `monitor backup-setup` | 加密后自动发送配置和 Grafana 数据到 Telegram，支持全新监控机恢复，详见[文档](docs/monitoring-backup.md) |
| ISP HTTPS 静态站点 | `sni-site` | 给其他 VPS 的 Reality 提供 target/SNI，公开证书自动续签，仅 TCP 443，详见[文档](docs/sni-site.md) |
| IPv6 开关 | `ipv6` | 查看 / 开启 / 关闭系统 IPv6，重启后保持；纯 IPv6 机器不允许关闭 |
| VPS 多功能工具箱 | `tools` | 运行 [vps_scripts](https://github.com/everett7623/vps_scripts) |

一键部署生成的入站：

| 入站 | 默认端口 | 说明 |
|---|---|---|
| Hysteria2 | UDP 443 | TLS 证书 + salamander 混淆 + 端口跳跃 20000-20100 |
| VLESS + Reality + Vision | TCP 443 | |
| VLESS + Reality + XHTTP | TCP 30115 | 外加 VLESS Encryption（ML-KEM-768） |
| VLESS + WS + TLS | TCP 8443 | 经 Cloudflare CDN |

4 个入站共用一个客户端和订阅 ID。分流规则：AI 网站走 ISP 出站，屏蔽回国流量（`geoip:cn`）、内网地址和 BT。geoip、geosite 每天北京时间 04:00 自动更新。

## 快速开始

以 root 在 VPS 上运行，支持 Debian / Ubuntu / RHEL 系等使用 systemd 的系统。

### 部署前准备

1. **域名解析**
   - hy2 域名：解析到本机，**灰云**（证书和面板都用它）
   - CDN 域名：解析到本机，**开启小黄云**，SSL/TLS 模式选"完全（严格）"
2. **云厂商安全组放行**
   - TCP：80（证书验证和续期）、443、30115、8443、面板端口
   - UDP：443、20000-20100
3. **准备好**：Cloudflare API Token（权限 Zone:DNS:Edit），以及 ISP 出站的 SS 地址、端口和密码

面板端口默认随机生成，想提前在安全组放行，可以加 `--panel-port 端口号` 指定。

### 部署

```bash
# 预览：只生成配置，不改动系统
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy --dry-run

# 正式部署
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy
```

脚本会询问必填参数，显示部署计划，确认后执行。完成后面板地址、账号和分享链接保存在 `/root/vps-deploy-result.txt`。

参数也可以通过命令行、环境变量或配置文件提供，详见[部署文档](docs/deploy.md#提供参数的方式)。

### 打开菜单

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh)
```

主菜单显示主机、系统、架构、可用内存、运行时长、监控角色与服务概况，并提供完整在线打开命令、`vt` 安装状态和常用命令。这些信息只读取本地状态，不请求外网、不读取 `.env` 或密码。子菜单使用简洁的当前位置提示，执行完成后返回当前页。

```text
主菜单
├─ 1. 节点部署 >        1 预览 → 2 部署；3 更新配置；4 覆盖重部署；5 查看结果
├─ 2. 3x-ui 面板 >      安装 / 状态 / 路由 / geodata 更新
├─ 3. Hysteria2 端口跳跃 >  安装或更新 / 状态 / 卸载
├─ 4. 系统设置（IPv6）>    查看状态 / 开启 / 关闭
├─ 5. VPS 多功能工具箱
├─ 6. 使用帮助 / 快捷命令 >  安装 vt / 命令帮助 / 常见操作路径
├─ 7. 监控管理 >
│  ├─ 1. 安装监控机
│  ├─ 2. 添加 / 部署被监控节点（自动流程）
│  ├─ 3. Grafana 面板 >      地址 / 账号密码 / 健康检查 / 资源调整
│  ├─ 4. Cloudflare 域名入口 >  配置 / 状态 / 日志 / 恢复 / 暂停 / 移除
│  ├─ 5. 状态与日志 >
│  ├─ 6. 节点与证书 >        有效期 / 节点续签 / 客户端续签 / 移除采集
│  ├─ 7. 高级手动接入 >      生成证书包 → 远端安装 → 监控机验证
│  └─ 8. 启动 / 停止 / 重启 / 卸载 >
└─ 0. 退出
```

所有菜单统一支持：**0 返回上级、m 回主菜单、q 退出**（主菜单 0 退出）。操作结束的提示也可输入 m / q；`vt monitor` 直达监控菜单后同样可用 m 回主菜单。子菜单最多三层，普通自动添加只需 **7 → 2**。

首次监控：**7 → 1 安装 → 2 添加节点 → 3 查看面板**；需要 HTTPS 域名时进入 **7 → 4 → 1**。Grafana 的账号和资源管理位于 **7 → 3**，与 Cloudflare 连接器管理分开。已有节点域名可直接使用，支持中文、空格、`.`、`/` 显示名称；自动受限续期和新采集验证无需手工传包或重启监控服务。

选择 **6 → 1** 安装快捷命令，以后直接输入 `vt` 打开菜单，也可运行 `vt monitor`、`vt help`、`vt deploy --config-only`。在线入口每次重新打开会下载 main；已打开的旧菜单不会自动切换代码，合并更新后请退出重开。原有 CLI 命令不变。

也可以克隆仓库后运行：

```bash
git clone https://github.com/changshengyu/vps-toolkit.git
cd vps-toolkit
bash vps.sh
```

## 命令行用法

```bash
bash vps.sh deploy              # 一键部署节点
bash vps.sh deploy --dry-run    # 只预览，不改动系统
bash vps.sh deploy --replace    # 重新部署，覆盖已有的同端口入站
bash vps.sh deploy --config-only  # 旧机器只更新出站、路由和 geodata，不动入站
bash vps.sh deploy --help       # 查看全部参数和选项
bash vps.sh routing             # 已有 3x-ui 的路由域策略改为 IPIfNonMatch
bash vps.sh geodata             # 已有 3x-ui 开启 geodata 自动更新（off 关闭）
bash vps.sh hy2 install         # 安装或更新端口跳跃规则
bash vps.sh hy2 status          # 查看端口跳跃状态
bash vps.sh hy2 uninstall       # 卸载端口跳跃规则
bash vps.sh 3x-ui install       # 只安装 3x-ui
bash vps.sh 3x-ui status        # 查看 3x-ui 状态
bash vps.sh result              # 查看上次部署结果
bash vps.sh shortcut            # 安装快捷命令 vt
bash vps.sh ipv6 disable        # 关闭 IPv6（enable 开启，status 查看）
bash vps.sh monitor             # 打开监控管理菜单
bash vps.sh monitor hub-install # 仅在一台监控机安装 Prometheus + Grafana
bash vps.sh monitor deploy-node # 填节点域名和 SSH 信息，自动安装并接入
bash vps.sh monitor tunnel-setup # 第 3 步：自动配置 Grafana HTTPS 域名入口
bash vps.sh monitor access      # 查看域名地址或备用 SSH 访问方法
bash vps.sh monitor grafana-account # 修改 Grafana 实际登录账号 / 密码
bash vps.sh monitor grafana-check # 检查本机面板就绪、重启及资源
bash vps.sh monitor grafana-resources # 持久调整资源，迁移旧 192 MiB 配置
bash vps.sh monitor renew-node  # 一键续签并同步，无需手工传证书包
bash vps.sh monitor cert-status # 查看证书有效期
bash vps.sh monitor help        # 查看全部监控操作
bash vps.sh tools               # 打开工具箱
```

用 `curl` 一行运行时，把 `bash vps.sh` 换成 `bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh)` 即可。

## 重装系统前

参数和密钥保存在 `/root/.vps-toolkit/state.env`（权限 600，包含 Reality 私钥、CF Token 和 SS 密码，请妥善保管），重跑脚本时直接复用，客户端里的节点保持可用。重装系统前把它备份出来，重装后放回原处再运行 `deploy`，就不用重新导入节点。

想更换全部密钥（例如链接泄露后），运行 `deploy --regen --replace`。

## 目录结构

```
vps.sh                  入口和菜单
scripts/                各功能脚本
lib/common.sh           公共函数
lib/deploy/             一键部署的各模块：参数、密钥、证书、防火墙、面板 API、渲染
templates/              入站和路由模板（jq，只含占位符）
docs/                   文档和配置示例
```

## 文档

- [一键部署节点](docs/deploy.md)：部署流程、参数、路由规则说明
- [Hysteria2 端口跳跃](docs/hy2-port-hopping.md)
- [3x-ui 面板](docs/3x-ui.md)
- [多 VPS 监控](docs/monitoring.md)：安装角色、1 GB 试运行参数、域名接入、SSH 自动部署、HTTPS mTLS、自动续签及旧版本迁移

## 注意事项

- **第三方脚本：** 3x-ui、acme.sh 和工具箱的代码由上游维护，每次运行最新版，本仓库不包含它们的代码。
- **系统改动：** 一键部署会安装软件包、修改防火墙（仅在已启用时）和 nftables 规则；工具箱的部分功能会修改 SSH、BBR、swap 等配置。建议先创建快照。
- **敏感信息：** 这是公开仓库，不要提交密码、节点配置、证书、IP 或域名。本地文件 `deploy.env`、`state.env` 和预览目录已加入 `.gitignore`。
