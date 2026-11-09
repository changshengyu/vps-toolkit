# 一键部署 3x-ui 节点

在 3x-ui 中一次生成 4 个入站和分流规则，省去在面板里逐项设置。

| 入站 | 默认端口 | 说明 |
|---|---|---|
| Hysteria2 | UDP 443 | TLS 证书 + salamander 混淆 + 端口跳跃 |
| VLESS + Reality + Vision | TCP 443 | |
| VLESS + Reality + XHTTP | TCP 30115 | 外加 VLESS Encryption（ML-KEM-768） |
| VLESS + WS + TLS | TCP 8443 | 经 Cloudflare CDN |

4 个入站共用一个客户端和订阅 ID。

> **当前进度：第一阶段**，只支持 `--dry-run` 预览，不改动系统。安装面板、申请证书、配置防火墙、写入面板将在第二阶段加入。

## 快速开始

在 VPS 上以 root 运行，一条命令即可：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh) deploy --dry-run
```

也可以用管道方式，参数写在 `--` 后面：

```bash
curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh | bash -s -- deploy --dry-run
```

两种方式都会下载完整仓库到临时目录运行，结束后自动清理；交互输入会从终端读取。预览文件输出在当前目录的 `deploy-preview/` 下。

已经克隆仓库时：

```bash
bash vps.sh deploy --dry-run
```

直接运行时，脚本会逐项询问必填参数，其余参数使用默认值或自动生成，最后显示汇总。

## 提供参数的方式

读取优先级：**命令行 > 环境变量 > 配置文件 > 上次生成 > 交互输入 / 默认值**。

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
- 正式部署时保存到 `/root/.vps-toolkit/state.env`（权限 600），重跑时复用，已导入客户端的节点不会失效；用 `--regen` 可全部重新生成

## 预览输出

默认输出到 `./deploy-preview/`，可以用 `--out 目录` 修改：

| 文件 | 内容 |
|---|---|
| `readable/` | 展开后的入站和路由，便于和备份对比 |
| `payload/` | 实际提交给面板 API 的内容（settings 等字段为 JSON 字符串） |
| `plan.txt` | 第二阶段将执行的操作 |

预览文件包含私钥和密码，核对后请删除。

## 路由规则

1. api → api
2. AI 网站 UDP 443 → 屏蔽（迫使客户端改用 TCP）
3. AI 网站 TCP → 六六云 SS
4. `geosite:cn` → 屏蔽
5. `geoip:cn`、`geoip:private` → 屏蔽
6. BT → 屏蔽

未启用 AI 分流时，去掉第 2、3 条和 SS 出站。

Xray 的一条规则里同时写 `domain` 和 `ip` 时，需要两者同时满足才会匹配，所以第 4、5 条分开写。
