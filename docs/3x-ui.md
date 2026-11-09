# 3x-ui 面板

## 只安装面板

运行 3x-ui 官方安装脚本，不写入任何节点配置：

```bash
bash vps.sh 3x-ui install
```

安装时请留意脚本输出的登录地址、账号和密码。安装后运行 `x-ui` 可打开 3x-ui 自带的管理菜单。

如果要连同证书、入站和分流规则一起配置，请用[一键部署](deploy.md)，它会以无人值守方式调用同一个官方安装脚本。

## 已有面板的配置调整

以下命令通过面板 API 修改 Xray 模板，修改前都会备份到 `/root/.vps-toolkit/backup/`：

| 命令 | 作用 |
|---|---|
| `bash vps.sh deploy --config-only` | 出站、路由规则和 geodata 自动更新统一到当前版本，不动入站 |
| `bash vps.sh routing` | 只把路由域策略改为 `IPIfNonMatch` |
| `bash vps.sh geodata [cron\|off]` | 只开启 / 关闭 geodata 自动更新 |

脚本使用名为 `vps-toolkit` 的专用 API Token，每次运行会轮换它，不影响你在面板中创建的其他 Token。

## 安全建议

- 修改默认账号、密码和访问路径（一键部署会随机生成）
- 为面板启用 HTTPS（一键部署会把 hy2 域名的证书设为面板证书）
- 在云厂商安全组中限制面板端口的访问来源
- 官方安装脚本会把初始账号、密码和 API Token 写入 `/etc/x-ui/install-result.env`（权限 600），修改密码后可以删除这个文件

## 面板路由页面的注意事项

「路由 → 基本路由」中的「阻止域名」等选项，会直接修改第一条匹配的规则。例如一键部署的"阻止 AI 网站 QUIC"规则会显示在「阻止域名」中：在这里添加域名只会屏蔽它的 UDP 443，删除则会删掉整条规则。增删规则请在「路由规则」标签页中操作。

## 配合端口跳跃

在 3x-ui 中手动创建 Hysteria2 入站后，用入站端口配置跳跃规则（一键部署会自动完成这一步）：

```bash
HY2_PORT=入站端口 bash vps.sh hy2 install
```

然后在客户端填写跳跃端口范围，详见[端口跳跃文档](hy2-port-hopping.md)。
