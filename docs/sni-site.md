# ISP VPS 的 HTTPS 静态站点 / Reality SNI

主菜单 **8 → ISP HTTPS 静态站点（Reality SNI）**，或 `vt sni-site`。在 ISP 机器上运行，给**其他 VPS 的 Reality 入站**提供 HTTPS target/SNI。模块只部署一个静态网页，不安装 Shadowsocks、3x-ui 或修改 AI 分流。

## 部署前准备

1. 准备一个域名，例如 `myweb.example.com`，其 A / AAAA 记录直接解析到 ISP VPS；Cloudflare 必须为**灰云 / 仅 DNS**。没有可用 IPv6 时不要保留失效 AAAA。
2. 本机 TCP 443 必须空闲。模块会检查监听进程；已有 Nginx、Caddy、Xray/Reality 或面板占用时停止，不自动抢占或接管已有站点。已经有可用 HTTPS 网站时可以直接继续使用原站点，无需重复部署。
3. 云安全组与系统防火墙须允许公网入站 **TCP 443**，VPS 可以出站访问 HTTPS/DNS；不需要 TCP 80 或 UDP 443。
4. 系统要求 Linux + systemd、root，支持 x86_64 / ARM64，需有 Python 3、curl、`ip` 和 `ss`（后两者来自 iproute2）。

域名 A/AAAA 均需对应本机公开地址。工具优先核对本机接口；遇到 NAT 等情况，再用有超时限制的公网出口查询核对。其他机器、CDN 或旧 IPv6 地址会中止部署，避免将证书验证流量发往错误目标。不会创建或修改 Cloudflare DNS，也不需要 Cloudflare Token。

## 首次部署

```bash
vt sni-site install
```

按提示输入域名、可选的证书邮箱和页面名称。程序展示部署计划，确认后：

- 下载固定版本 **Caddy 2.11.9** 官方 Linux 发布包，校验内置的官方 SHA-256，再安装到本模块专用目录。
- 以专用非登录用户运行原生 systemd 服务 `vps-toolkit-sni-site`，开机自启；不需要 Docker，不修改已有全局 Caddy / Nginx 配置。
- 仅监听 **TCP 443**，提供 HTTP/1.1 和 HTTP/2；Caddy 默认支持 TLS 1.3，检查会验证它。HTTP/3 被关闭，因此不争用 Hysteria2 的 UDP 443。
- 用 Let's Encrypt 的 **TLS-ALPN-01** 验证申请公开受信证书，后续由 Caddy 自动续签。关闭 HTTP 验证和 HTTP→HTTPS 跳转，不监听 TCP 80。管理 API 仅使用服务私有目录中的 Unix socket，不监听 TCP 2019。
- 安装一个没有外部资源、没有后台的自包含静态页。公开路由仅为 `/` 和 `/index.html`，其他路径返回 404。
- 等待证书签发并检查本机首页 HTTP 200、证书域名与信任链、TLS 1.3 和 HTTP/2 ALPN，成功后显示配置提示。

服务资源上限为 192 MiB / 0.35 CPU，Go 软内存目标 96 MiB，均不是实际常驻占用承诺。TLS 流量很大或资源不足时，应查看日志及服务状态调整部署，而不是与代理争用同一 TCP 端口。

如果因网络、云安全组、CAA/AAAA 或 CA 限制暂时未签发成功，会明确报告尚未就绪，保留配置与证书状态，服务继续按 Caddy 的策略重试，不宣称部署已通过检查。修复后使用检查项或再次选择部署即可。安装中断可以重试；已存在的托管首页、域名和证书保留，不会每次重建密钥或覆盖自定义页面。

## 其他 VPS 的 Reality 怎么填写

在**其他 VPS 的 3x-ui Reality 入站**填写：

```text
SNI / serverNames：myweb.example.com
target / dest：myweb.example.com:443
```

客户端连接地址仍为各入口 VPS 的 IP / 域名，SNI 与服务端一致。此模块不会跨机器修改 3x-ui、客户端配置或 ISP 分流规则。注意目标域名不要再指回入口 VPS 的 Reality TCP 443，避免递归连接。

访问网页需要显式使用 `https://myweb.example.com/`。如果只是输入 `http://`，不会有本模块的重定向服务。建议从其他 VPS 再检查公网访问，确认防火墙及路由可达；本机检查不能代替所有来源的网络检查。

## 菜单及常用命令

```text
主菜单 8
1. 部署 / 恢复 HTTPS 静态站点
2. 查看地址与 Reality 填写方法
3. 检查服务 / 证书 / TLS 1.3 / HTTP2
4. 替换首页 HTML（无需重启）
5. 查看站点日志
6. 服务管理（启动 / 停止 / 卸载）>
```

```bash
vt sni-site access
vt sni-site check
vt sni-site page
vt sni-site logs
vt sni-site start
vt sni-site stop
vt sni-site restart
vt sni-site uninstall
```

更换页面时，将自己的**完整、自包含 HTML 文件**上传到 VPS，再输入其绝对路径（不超过 5 MiB）。程序先备份旧首页，再原子替换，无需重启。CSS、图片等需内嵌在此文件中，不额外公开其他文件。默认页可以直接使用，不含代理或部署说明。

停止会同时取消开机自启，可能影响使用此目标的 Reality 节点。卸载只移除本模块的服务，保留网页、配置、证书和独立二进制；重新选择部署可恢复。目录中不属于本模块的内容、同名外部服务、不安全写权限或符号链接会拒绝接管。

## 配置与备份

| 路径 | 用途 |
|---|---|
| `/etc/vps-toolkit-sni-site` | 域名记录、Caddy JSON、旧首页备份 |
| `/var/www/vps-toolkit-sni-site/index.html` | 公开静态首页 |
| `/var/lib/vps-toolkit-sni-site` | Caddy 的 ACME 账户和证书私钥，服务账号私有 |
| `/usr/local/lib/vps-toolkit-sni-site/caddy` | 模块专用 Web 服务二进制 |
| `/run/vps-toolkit-sni-site/admin.sock` | 私有管理 socket |

监控菜单的 Telegram 备份目前只覆盖监控系统，**不包含这个独立站点**。如需迁移站点，另行备份上述配置、页面和 Caddy 数据目录（含私钥，需要加密保护）；或者在新 ISP VPS 完成直连域名解析后重新部署。不要把私钥放进网页目录。

本模块不自动更改防火墙规则。手动使用已启用的 UFW 时可按实际需要执行 `ufw allow 443/tcp`；firewalld 则将 TCP 443 加到相应 zone 的运行和永久配置中。不要为此关闭防火墙、清空已有规则或开放管理 API。

依据：[Caddy 自动 HTTPS / TLS-ALPN](https://caddyserver.com/docs/automatic-https#tls-alpn-challenge)、[Caddy HTTP 验证开关](https://caddyserver.com/docs/caddyfile/directives/tls#acme)、[REALITY 目标要求](https://github.com/XTLS/REALITY#readme)。
