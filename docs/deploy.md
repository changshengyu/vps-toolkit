# 一键部署 3x-ui 节点

在 3x-ui 中一次生成 4 个入站和分流规则，省去在面板里逐项设置。

| 入站 | 默认端口 | 说明 |
|---|---|---|
| Hysteria2 | UDP 443 | TLS 证书 + salamander 混淆 + 端口跳跃 |
| VLESS + Reality + Vision | TCP 443 | |
| VLESS + Reality + XHTTP | TCP 30115 | 外加 VLESS Encryption（ML-KEM-768） |
| VLESS + WS + TLS | TCP 8443 | 经 Cloudflare CDN |

4 个入站共用一个客户端和订阅 ID。

## 快速开始

在 VPS 上以 root 运行。建议先预览，确认无误后再正式部署：

```bash
# 预览：只生成配置，不改动系统
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy --dry-run

# 正式部署
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy
```

也可以用管道方式，参数写在 `--` 后面：

```bash
curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh | bash -s -- deploy
```

两种方式都会下载完整仓库到临时目录运行，结束后自动清理；交互输入会从终端读取。

已经克隆仓库时：`bash vps.sh deploy`。

直接运行时，脚本会逐项询问必填参数，其余参数使用默认值或自动生成，最后显示汇总和部署计划，确认后执行。

## 部署流程

| 步骤 | 内容 |
|---|---|
| 检查环境 | 端口是否被其他程序占用；hy2 域名是否解析到本机（只提醒） |
| 防火墙 | 检测已启用的 ufw / firewalld / iptables，先放行 SSH 端口，再放行面板和节点端口；没有启用防火墙时跳过，不会主动开启 |
| 安装 3x-ui | 未安装时用官方脚本无人值守安装；已安装则沿用现有面板的端口、路径和账号 |
| 生成密钥 | 连同参数保存到 `/root/.vps-toolkit/state.env` |
| 申请证书 | hy2 域名：HTTP 验证（或 `HY2_CERT_MODE=dns`），并设为面板证书；CDN 域名：CF Token DNS 验证，同时申请 `*.域名`；证书仍有效时跳过 |
| 导入入站 | 通过面板 API 导入；同端口已有入站时询问是否覆盖 |
| 更新模板 | 备份当前 Xray 模板到 `/root/.vps-toolkit/backup/`，替换出站和路由规则，其他字段不变，然后重启 Xray 并检查状态 |
| 端口跳跃 | 调用 `hy2-port-hopping.sh` |
| 输出结果 | 面板地址、账号和分享链接，保存到 `/root/vps-deploy-result.txt`（权限 600） |

脚本通过 `x-ui setting -getApiToken -tokenName vps-toolkit` 获取专用 API Token，每次运行会轮换这个 Token，不影响你在面板中创建的其他 Token。

### 常用选项

| 选项 | 作用 |
|---|---|
| `-y` | 跳过确认（非交互运行时必须加） |
| `--replace` / `--no-replace` | 同端口已有入站时直接覆盖 / 直接跳过（默认询问，非交互时跳过） |
| `--skip-firewall` | 不修改系统防火墙 |
| `--skip-hop` | 不配置端口跳跃 |
| `--regen` | 重新生成全部密钥（已导入客户端的节点会失效） |

### 重新运行

参数和密钥都保存在 `/root/.vps-toolkit/state.env`，重跑时不用再输入，生成的密钥也保持不变。比如只想重新导入入站：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy --replace
```

重装系统后 `state.env` 会丢失。想让节点保持不变，重装前把它备份出来，重装后放回 `/root/.vps-toolkit/` 再运行。

### 部署后

- 云厂商安全组需要手动放行：TCP 面板端口、443、30115、8443（HTTP 验证时还有 80，续期也要用）；UDP 443、20000-20100
- Cloudflare 中 CDN 域名开启小黄云，SSL/TLS 模式选"完全（严格）"；hy2 域名保持灰云

## 提供参数的方式

读取优先级：**命令行 > 环境变量 > 配置文件 > 上次部署 > 交互输入 / 默认值**。

可以混用，例如把固定不变的参数写进配置文件，每台机器不同的参数在命令行里传入。

### 1. 交互输入

```bash
bash vps.sh deploy --dry-run          # 只询问缺少的必填项
bash vps.sh deploy --dry-run -i       # 逐项询问全部参数，回车使用默认值
```

### 2. 命令行参数

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy --dry-run \
  --reality-domain myweb.example.com \
  --hy2-domain vps01.example.com \
  --cdn-domain cdn01.example.com \
  --cf-token xxxx \
  --ss-addr isp.example.com --ss-port 8388 --ss-pass xxxx
```

运行 `bash vps.sh deploy --help` 查看全部参数。

### 3. 环境变量

```bash
REALITY_DOMAIN=myweb.example.com CDN_PORT=2053 bash vps.sh deploy --dry-run
```

### 4. 配置文件

默认读取 `/root/.vps-toolkit/deploy.env`，也可以用 `-c 文件` 指定。格式参考 [deploy.env.example](deploy.env.example)：

```bash
mkdir -p /root/.vps-toolkit
cp docs/deploy.env.example /root/.vps-toolkit/deploy.env
chmod 600 /root/.vps-toolkit/deploy.env
```

配置文件按 `KEY=VALUE` 逐行读取，**不会作为脚本执行**。值可以加引号，但不支持行尾注释。

## 参数

| 参数 | 必填 | 默认值 |
|---|---|---|
| `REALITY_DOMAIN` | 是 | |
| `REALITY_TARGET` | | `REALITY_DOMAIN:443` |
| `HY2_DOMAIN` | 是 | 证书同时用作面板证书 |
| `CDN_DOMAIN` | 是 | |
| `CF_TOKEN` | 是 | 权限 Zone:DNS:Edit |
| `HY2_CERT_MODE` | | `http`（HTTP 验证，需要 TCP 80）；`dns` 用 CF Token |
| `ACME_EMAIL` | | 空 |
| `NODE_NAME` | | `HY2_DOMAIN` 的第一段，用作入站备注前缀 |
| `ENABLE_AI_ROUTE` | | `y`，AI 网站走六六云 SS |
| `SS_ISP_ADDR` / `SS_ISP_PORT` / `SS_ISP_PASS` | 启用 AI 分流时 | |
| `SS_ISP_METHOD` | | `2022-blake3-aes-256-gcm` |
| `PANEL_PORT` / `PANEL_USER` / `PANEL_PASS` / `PANEL_PATH` | | 随机生成 |
| `HY2_PORT` / `HOP_RANGE` | | `443` / `20000-20100` |
| `REALITY_PORT` / `XHTTP_PORT` / `CDN_PORT` | | `443` / `30115` / `8443` |
| `CDN_WS_PATH` / `XHTTP_PATH` | | 随机路径 |
| `CLIENT_EMAIL` | | 随机生成 |

## 自动生成的密钥

UUID、订阅 ID、两套 Reality 密钥对和 shortIds、VLESS Encryption 密钥、hy2 认证串、salamander 密码。

- 优先使用面板自带的 xray 生成（`/usr/local/x-ui/bin/xray-linux-*`），也可以用 `XRAY_BIN` 指定
- 没有 xray 时，预览用 openssl 生成 Reality 密钥，VLESS Encryption 用占位符代替
- 正式部署时连同参数保存到 `/root/.vps-toolkit/state.env`（权限 600），重跑时复用，已导入客户端的节点不会失效；用 `--regen` 可全部重新生成

## 预览输出

默认输出到 `./deploy-preview/`，可以用 `--out 目录` 修改：

| 文件 | 内容 |
|---|---|
| `readable/` | 展开后的入站和路由，便于和备份对比 |
| `payload/` | 实际提交给面板 API 的内容（settings 等字段为 JSON 字符串） |
| `plan.txt` | 第二阶段将执行的操作 |

预览文件包含私钥和密码，核对后请删除。

## 路由规则

`domainStrategy` 为 `IPIfNonMatch`：一轮匹配没有命中时，把域名解析成 IP 再匹配一次。

1. api → api
2. AI 网站 UDP 443 → 屏蔽（迫使客户端改用 TCP）
3. AI 网站 TCP → 六六云 SS
4. `geoip:private`、`geoip:cn` → 屏蔽（防止回国流量）
5. BT → 屏蔽

未启用 AI 分流时，去掉第 2、3 条和 SS 出站。

### 为什么按 IP 而不是按域名屏蔽回国流量

第 4 条参考 Xray 官方服务端教程，按 `geoip:cn` 屏蔽。配合 `IPIfNonMatch`，访问国内网站（域名解析到国内 IP）会被屏蔽，代价是未命中规则的域名需要在服务器上多解析一次。

**不要按域名屏蔽 `geosite:cn`**：这个列表包含 `www.gstatic.com`、`connectivitycheck.gstatic.com`，而 mihomo 默认用 `https://www.gstatic.com/generate_204` 测速，屏蔽后所有节点都会显示不可用（Xray 客户端不受影响）。这些域名在海外服务器上解析到的是海外 IP，按 IP 屏蔽不会误伤。

### 旧机器修改路由域策略

一键部署的机器已经是 `IPIfNonMatch`。之前手动配置的 3x-ui，可以只修改这一项，入站和路由规则保持不变：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) routing
```

修改前会备份当前 Xray 模板到 `/root/.vps-toolkit/backup/`。在面板中对应「Xray 配置 → 基础配置 → 配置路由域策略」。
