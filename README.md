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

菜单顶部显示 3x-ui、端口跳跃和部署结果的状态，输入数字选择功能，执行完回到菜单：

```
  节点部署                         3x-ui 面板
  1.  一键部署节点                 6.  安装 3x-ui
  2.  预览部署配置                 7.  查看 3x-ui 状态
  3.  重新部署（覆盖入站）         8.  路由域策略改为 IPIfNonMatch
  4.  旧机器只更新配置             9.  开启 geodata 自动更新
  5.  查看部署结果                 10. 自定义 geodata 更新时间
                                   11. 关闭 geodata 自动更新
  Hysteria2 端口跳跃               其他
  12. 安装或更新规则               15. VPS 多功能工具箱
  13. 查看状态                     16. 安装快捷命令 vt
  14. 卸载                         0.  退出
```

选 16 安装快捷命令后，以后直接输入 `vt` 打开菜单（每次运行仓库最新版），也可以带参数，如 `vt deploy --config-only`。

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

## 注意事项

- **第三方脚本：** 3x-ui、acme.sh 和工具箱的代码由上游维护，每次运行最新版，本仓库不包含它们的代码。
- **系统改动：** 一键部署会安装软件包、修改防火墙（仅在已启用时）和 nftables 规则；工具箱的部分功能会修改 SSH、BBR、swap 等配置。建议先创建快照。
- **敏感信息：** 这是公开仓库，不要提交密码、节点配置、证书、IP 或域名。本地文件 `deploy.env`、`state.env` 和预览目录已加入 `.gitignore`。
