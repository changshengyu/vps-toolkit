# 监控机自动备份与恢复（Telegram）

入口：**主菜单 7 → 监控管理 → 9 备份与恢复（Telegram）**。这是监控系统的恢复备份，不是整机镜像，不包括 3x-ui、Xray、网站或系统 SSH 配置。

## 保存哪些内容

| 内容 | 保存方式 |
|---|---|
| `/opt/vps-toolkit-monitor` | Compose、节点目标、CA/客户端证书、节点元数据、专用续期私钥、Tunnel 连接 Token 等；排除部署锁和可重新生成的证书传输包 |
| Grafana 数据 | 在线 SQLite 一致性快照，以及数据目录其他文件和插件；保留账号、密码哈希、仪表盘、告警、联系点等 |
| Grafana 加密设置 | 原镜像版本，以及运行时显式设置的 `GF_SECURITY_SECRET_KEY` / `GF_SECURITY_ENCRYPTION_PROVIDER`；恢复时使用独立 Compose 覆盖文件 |
| SSH 主机信任 | root 的 `known_hosts` / `known_hosts2`，用于继续校验节点 SSH 身份；不会打包 root 的其他 SSH 私钥 |
| 备份设置 | 加密公钥、发送用 Bot Token / Chat ID、时间，均放在加密包内部 |
| 不保存 | Prometheus 历史指标、解密私钥、整机系统与防火墙、云端安全组、其他业务数据 |

因此，重装后能恢复已有面板和监控接入信息，历史曲线从恢复后重新采集。默认 SQLite 数据库通过 Python SQLite 在线备份 API 制作一致性副本，不直接复制运行中的数据库或 WAL，不需要每天停 Grafana。自定义 MySQL/PostgreSQL、数据库路径、外部符号链接及旧版常驻 SSH 隧道的恢复不在此功能范围内。

## 第一次设置

在**现有监控机**运行：

```bash
vt monitor backup-setup
```

也可用最新在线入口打开菜单：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/changshengyu/vps-toolkit/main/vps.sh)
```

1. 输入 Bot Token（隐藏输入）和你与 Bot **个人聊天**的数字 Chat ID。可以使用 Grafana 告警同一个 Bot，但 `telegram_bot` 是联系点名称，不能替代 Token；先在 Telegram 对 Bot 发送 `/start`。备份不发送到群组或频道。
2. 选择每天备份时间，默认 **北京时间 04:00**，最多随机延迟 5 分钟；不受 VPS 系统时区影响。使用 systemd timer，开机自动启用，错过的计划在恢复运行后补执行。没有常驻备份进程。
3. 把 `/etc/vps-toolkit-monitor-backup/recovery-key.txt` **另存到 Mac 或密码管理器**。设置向导会等待你输入 `SAVED`，然后才启用计划。

例如保持向导等待，在 **Mac 的另一个终端**运行（替换 SSH 端口和监控机 IP）：

```bash
scp -P SSH端口 root@监控机IP:/etc/vps-toolkit-monitor-backup/recovery-key.txt ~/Downloads/monitor-recovery-key.txt
chmod 600 ~/Downloads/monitor-recovery-key.txt
```

确认 Mac 上的文件已保存且可读，再回到 VPS 输入 `SAVED`。密钥丢失就无法解密备份，不能只放在即将重装的 VPS 或与备份相同的聊天中。保存成功后可以从 VPS 删除 `recovery-key.txt`，后续备份只使用公钥；请妥善保留离线副本。

Bot Token 保存在 `/etc/vps-toolkit-monitor-backup/config.json`，权限 0600，不作为命令参数或环境变量传递，不输出到日志。配置验证调用 `getMe` / `getChat`，不会发送备份。选择“立即备份并发送”做第一次发送：

```bash
vt monitor backup-run
vt monitor backup-status
vt monitor backup-logs
```

Telegram 连接优先使用 IPv4；IPv4 连接失败时才回退 IPv6。无需为了备份关闭 VPS 的 IPv6。验证请求单次最长 20 秒、上传单次最长 180 秒，连接阶段最长 6 秒，并有覆盖 DNS / 进程阻塞的硬超时。进度会显示当前 API 方法、IP 地址族和重试次数；错误区分 DNS、TCP、TLS、超时、非 JSON 响应与 Telegram API 权限 / 参数错误，不回显 Token、curl stderr 或响应正文。Token 无效、Chat ID 错误等明确的 API 拒绝不会通过换 IP 地址族重复尝试。

日常流程是：取得部署锁 → 制作一致性数据库副本 → 压缩与 age 公钥加密 → 释放部署锁 → 上传加密文件 → 最后上传校验清单。只有全部文件的发送 API 都返回成功，才记录整组发送成功。网络故障最多重试三次；部分发送失败时本地加密文件保留，清单不会先于分卷发送。超时重试可能出现重复附件，以同组文件名和校验清单为准。

Telegram 普通 Bot API 单文件目前上限 50 MB，本功能以 **45 MiB** 自动分卷。恢复时要下载同组的 `.manifest.json` 和**全部** `.age` / `.age.part-0001` 等附件，放在同一个目录。清单包含分卷顺序、大小和 SHA-256，不包含 Bot Token 或私钥；分卷合并解密后由 age 验证加密完整性。

本地 `/var/lib/vps-toolkit-monitor-backups` 在每次发送成功后保留最近 7 组本工具创建的备份，不删除 Telegram 聊天中的文件。备份不是常驻服务，定时任务 CPU 上限 35%、内存上限 192 MiB、最长 30 分钟；会占用临时磁盘和上传带宽。源数据上限 4 GiB，可用临时空间至少约为源数据三倍加 128 MiB。请用 `backup-status` 和收到的文件确认计划确实成功，未配置前不会自动发送。

## 暂停、恢复及日志

```bash
vt monitor backup-pause
vt monitor backup-resume
vt monitor backup-status
vt monitor backup-logs
```

暂停只停定时器，不强制杀死正在发送的备份。卸载监控服务会暂停备份计划，保留备份配置和加密文件。普通“停止监控服务”不会删除备份计划，仍可以备份停止状态的数据库。`backup-logs` 显示最近任务状态与定时服务的 journal；手动运行的详细进度直接显示在当前终端。

## 在重装后或另一台 VPS 恢复

**仅在全新监控机执行，不要先选择“安装监控机”。**恢复会拒绝覆盖现有监控目录、容器、Grafana / Prometheus 数据卷。需要 Linux、systemd、root；入口可以安装缺少的 Docker、Python 3 和 age。

1. 先停用旧监控机，避免两套监控同时采集、发送重复告警和使用同一 Tunnel。
2. 从 Telegram 下载某一组的清单及全部分卷，再和 Mac 上保存的解密密钥一起上传到新 VPS 的私有目录。
3. 打开最新菜单，选择 **7 → 9 → 5 从备份恢复**；或 `vt monitor backup-restore`。
4. 输入清单、解密密钥的绝对路径。工具先校验文件，再解密、检查归档路径和数据库完整性，展示目标目录及原源 IP；输入 `RESTORE` 后才写入监控数据。
5. 自动创建新的 Grafana 数据卷，恢复数据，再启动服务并等待 Grafana `/api/health` 的数据库检查通过。沿用旧的 Grafana 账号、密码、告警、联系点和 Cloudflare Tunnel，不重新创建云端 DNS。

恢复目录必须与原目录一致，默认 `/opt/vps-toolkit-monitor`，因为目标配置和续期记录包含绝对路径。归档拒绝路径穿越、软/硬链接、设备文件和超出上限的解压内容。恢复不安装或更改其他业务和云安全组。

**公网源 IP 没变：**确认面板和采集正常后执行 `vt monitor start`，重新生成并启用证书续期计划；执行 `vt monitor backup-resume` 恢复备份计划。

**公网源 IP 已变：**监控菜单选择“添加/部署被监控节点”，逐个使用**原名称**、节点域名和**新的监控机源 IP**更新。此操作需要节点正常 SSH 登录授权，可能需要再输入一次登录密码；旧续期密钥既限定旧源 IP，又只能更新证书，不能用它绕过限制修改防火墙或 SSH 授权。同步更新云安全组的精确源 IP 放行规则；检查每个节点 `up=1` 后恢复定时备份。证书和节点名称无需重建。

如果旧的普通 SSH 登录私钥未复制到新机，接入向导会提示并清除不可读的旧默认路径，允许填写新的私钥路径或留空使用默认密钥 / 密码。不会因此重建或丢弃备份中独立保存的专用续期密钥。

如果恢复中途失败，会保留已恢复的配置和本次新建的数据卷，不自动删除它们；`.restore-incomplete` 标记阻止误启动未完成恢复的监控。按实际错误检查 Docker、磁盘或权限；不能直接移除标记来掩盖数据库未恢复的问题。原备份附件不受影响，可在另一台全新机器重试。

本功能未增加自动接收 Bot 命令、公开下载接口或公网端口。Telegram Bot 对话不是端到端加密存储，因此备份始终先用 age 加密；私钥离线保存。

参考：[Telegram sendDocument](https://core.telegram.org/bots/api#senddocument)、[age 使用说明](https://github.com/FiloSottile/age)、[Python SQLite 在线备份接口](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)。
