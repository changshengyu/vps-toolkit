# vps-toolkit

个人 VPS 配置工具集，通过一个菜单管理常用脚本。

| 功能 | 说明 |
|---|---|
| 一键部署节点 | 安装 3x-ui、申请证书、放行端口，生成 hy2、Reality、XHTTP、CDN 四个入站和分流规则 |
| Hysteria2 端口跳跃 | 用 nftables 重定向端口，重启后自动恢复 |
| 3x-ui 面板 | 运行 [3x-ui](https://github.com/MHSanaei/3x-ui) 官方安装脚本 |
| VPS 多功能工具箱 | 运行 [vps_scripts](https://github.com/everett7623/vps_scripts) |

3x-ui 和工具箱每次都运行上游最新版，本仓库不包含它们的代码。

## 快速开始

一键部署节点（详见[文档](docs/deploy.md)，可先加 `--dry-run` 预览）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy
```

打开菜单，以 root 运行：

```bash
curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh -o /tmp/vps.sh && bash /tmp/vps.sh
```

也可以克隆仓库后运行：

```bash
git clone https://github.com/changshengyu/vps-toolkit.git
cd vps-toolkit
bash vps.sh
```

## 命令行用法

```bash
bash vps.sh deploy            # 一键部署节点（加 --dry-run 只预览）
bash vps.sh hy2 install     # 安装或更新端口跳跃规则
bash vps.sh hy2 status      # 查看状态
bash vps.sh hy2 uninstall   # 卸载
bash vps.sh 3x-ui install   # 安装 3x-ui
bash vps.sh 3x-ui status    # 查看 3x-ui 状态
bash vps.sh tools           # 打开工具箱
```

使用其他配置时，可以设置环境变量：

```bash
HY2_PORT=8443 HOP_RANGE=30000-30100 IFACE=ens3 bash vps.sh hy2 install
```

## 文档

- [一键部署节点](docs/deploy.md)
- [Hysteria2 端口跳跃](docs/hy2-port-hopping.md)
- [3x-ui 面板](docs/3x-ui.md)

## 注意事项

- **第三方脚本：** 3x-ui 和工具箱的代码由上游维护，运行前会显示来源并要求确认。
- **系统改动：** 工具箱的部分功能会修改 SSH、BBR、swap 等配置，建议先创建快照。
- **敏感信息：** 这是公开仓库，不要提交密码、节点配置、证书、IP 或域名。
