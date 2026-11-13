#!/usr/bin/env bash
# 集中监控：Prometheus + Grafana；节点端：Node Exporter。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
. "$ROOT_DIR/lib/common.sh"

MONITOR_DIR="${MONITOR_DIR:-/opt/vps-toolkit-monitor}"
SSH_DIR="${MONITOR_SSH_DIR:-/var/lib/vps-toolkit-monitor-ssh}"
SSH_USER=vps-monitor-tunnel
NODE_USER=vps-monitor
NODE_DIR=/usr/local/lib/vps-toolkit-monitor
NODE_SERVICE=vps-toolkit-node-exporter
NODE_VERSION=1.12.1
TUNNEL_UNIT=vps-toolkit-monitor-tunnel@

show_help() {
    cat <<'HELP'
用法：vt monitor 操作 [参数]
  hub-install                    本机安装 Prometheus + Grafana + Node Exporter
  node-install [--public-key KEY] 本机仅安装 Node Exporter，可授权监控机公钥
  add-node                       在监控机交互接入远端节点
  remove-node                    在监控机移除远端采集（保留远端采集器）
  public-key                     显示监控机专用 SSH 公钥
  access                         显示 Mac 访问方法及密码文件位置
  status                         查看本机监控状态和资源
  logs                           查看本机监控日志
  start|stop|restart             管理本机监控服务
  uninstall                      卸载本机监控服务，保留历史、密码和 SSH 授权
  help                           显示帮助
集中监控端需要 Docker Compose v2；节点端仅使用二进制和 systemd。
HELP
}

linux_root() {
    require_root
    [ "$(uname -s)" = Linux ] || die "监控安装仅支持 Linux VPS。"
    require_command systemctl
}

valid_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; }
valid_host() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]]; }
valid_user() { [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; }
valid_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$((10#$1))" -ge 1 ] && [ "$((10#$1))" -le 65535 ]
}

hub_installed() { [ -f "$MONITOR_DIR/.vps-toolkit-managed" ]; }
node_installed() { [ -f "$NODE_DIR/.vps-toolkit-managed" ]; }
require_hub() { hub_installed || die "请先在本机安装监控机。"; }
compose() { docker compose --project-directory "$MONITOR_DIR" -f "$MONITOR_DIR/compose.yaml" "$@"; }

check_ports() {
    local port
    require_command ss
    for port in "$@"; do
        if ss -lntH | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
            die "TCP $port 已被占用，未改动已有服务。"
        fi
    done
}

setup_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        info "需要安装 Docker Engine 和 Compose v2，将运行 Docker 官方安装脚本。"
        confirm "安装 Docker？" || return 1
        run_remote_script https://get.docker.com
        systemctl enable --now docker
    fi
    docker info >/dev/null 2>&1 || die "Docker 服务不可用，请先检查 Docker。"
    docker compose version >/dev/null 2>&1 || die "缺少 Compose v2，请安装 docker-compose-plugin 后重试。"
}

hub_install() {
    linux_root
    if hub_installed; then
        info "已安装；保留现有配置和密码。可用 start 启动、restart 重启。"
        return 0
    fi
    [ ! -e "$MONITOR_DIR" ] || die "$MONITOR_DIR 已存在且不属于本模块，未覆盖。"
    check_ports 3000 9090 9100
    info "计划：本机部署 3 个容器，60 秒采集、7 天历史、历史块上限 2 GB。"
    info "内存上限合计 496 MiB，不含 Docker 等开销；Grafana 192 MiB 为紧凑试运行值，可能 OOM。"
    info "仅监听本机地址，通过 SSH 访问，不改 3x-ui 和 Xray 配置；首次安装 Docker 可能建立 Docker 自身的网络规则。"
    confirm "确认安装监控机？" || return 0
    ensure_packages curl openssl
    setup_docker
    local available free_kb
    available="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
    if [ "${available:-0}" -lt 600 ]; then
        warn "当前可用内存 ${available:-未知} MiB，低于试运行建议的 600 MiB。"
        confirm "仍要试运行？" || return 0
    fi
    free_kb="$(df -Pk /opt | awk 'END {print $4}')"
    [ "$free_kb" -ge 5242880 ] || die "/opt 至少需要 5 GiB 可用空间。"
    install -d -m 755 "$MONITOR_DIR"
    cp -R "$ROOT_DIR/templates/monitoring/." "$MONITOR_DIR/"
    install -d -m 755 "$MONITOR_DIR/prometheus/targets"
    # Never emit credentials through compose config or logs.
    (umask 077; printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$MONITOR_DIR/.env")
    touch "$MONITOR_DIR/.vps-toolkit-managed"
    compose config --quiet
    compose pull
    compose run --rm --no-deps --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml
    compose up -d node-exporter prometheus
    local attempt
    for attempt in {1..30}; do
        if curl -fsS http://127.0.0.1:9090/-/ready >/dev/null; then break; fi
        sleep 2
    done
    curl -fsS http://127.0.0.1:9090/-/ready >/dev/null || die "Prometheus 未就绪，请查看 monitor logs。"
    available="$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
    if [ "${available:-0}" -lt 300 ]; then
        warn "当前 available 仅 ${available:-未知} MiB，暂不启动 Grafana。请释放内存或将 Grafana 移到 Mac。"
        return 1
    fi
    compose up -d grafana
    info "监控机已启动。VPS overview 仪表盘和 Prometheus 数据源已配置。"
    show_access
}

authorize_key() {
    local key="$1" keyfile
    # Accept only a plain Ed25519 public key, never arbitrary authorized_keys options.
    [[ "$key" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/]+={0,3}([[:space:]][A-Za-z0-9_.@-]+)?$ ]] || die "请输入监控机生成的完整 Ed25519 公钥。"
    keyfile="$(mktemp)"
    printf '%s\n' "$key" > "$keyfile"
    if ! ssh-keygen -lf "$keyfile" >/dev/null 2>&1; then
        rm -f "$keyfile"
        die "SSH 公钥无效。"
    fi
    rm -f "$keyfile"
    local home_dir
    home_dir="$(getent passwd "$NODE_USER" | cut -d: -f6)"
    if [ -z "$home_dir" ]; then
        useradd --user-group --create-home --shell /bin/sh "$NODE_USER"
        home_dir="$(getent passwd "$NODE_USER" | cut -d: -f6)"
        touch "$NODE_DIR/.ssh-account-created"
    elif [ ! -f "$NODE_DIR/.ssh-account-created" ]; then
        die "用户 $NODE_USER 已存在且不属于本模块，未修改 SSH 授权。"
    fi
    install -d -m 700 -o "$NODE_USER" -g "$NODE_USER" "$home_dir/.ssh"
    local authorized="$home_dir/.ssh/authorized_keys" line
    line="restrict,port-forwarding,permitopen=\"127.0.0.1:9100\",command=\"/bin/false\" $key"
    touch "$authorized"
    grep -qxF "$line" "$authorized" || printf '%s\n' "$line" >> "$authorized"
    chown "$NODE_USER:$NODE_USER" "$authorized"
    chmod 600 "$authorized"
    info "已授权监控机公钥；SSH 用户：${NODE_USER}。"
}

download_node() (
    # A subshell EXIT trap also cleans up when a download/checksum fails.
    local arch="$1" base
    download_work="$(mktemp -d)"
    trap 'rm -rf -- "$download_work"' EXIT
    base="node_exporter-${NODE_VERSION}.linux-${arch}"
    curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NODE_VERSION}/${base}.tar.gz" -o "$download_work/$base.tar.gz"
    curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NODE_VERSION}/sha256sums.txt" -o "$download_work/sha256sums.txt"
    (cd "$download_work"; awk -v file="$base.tar.gz" '$2 == file {print}' sha256sums.txt > checksum; [ -s checksum ]; sha256sum -c checksum)
    tar -xzf "$download_work/$base.tar.gz" -C "$download_work"
    install -d -m 755 "$NODE_DIR"
    install -m 755 "$download_work/$base/node_exporter" "$NODE_DIR/node_exporter"
    touch "$NODE_DIR/.vps-toolkit-managed"
)

node_install() {
    linux_root
    local key="" arch
    if [ "${1:-}" = --public-key ] && [ "$#" -eq 2 ]; then
        key="$2"
    elif [ "$#" -ne 0 ]; then
        die "用法：monitor node-install [--public-key 'ssh-ed25519 ...']"
    fi
    hub_installed && die "本机已是监控机，自带 Node Exporter，无需重复安装。"
    if [ -z "$key" ]; then
        read -r -p "监控机 Ed25519 公钥（可留空，稍后授权）: " key || return 1
    fi
    info "节点端仅安装 Node Exporter（systemd 服务），监听 127.0.0.1:9100。"
    confirm "确认安装或配置本机采集器？" || return 0
    ensure_packages curl tar
    require_command sha256sum
    require_command ssh-keygen
    if ! node_installed; then
        [ ! -e "$NODE_DIR" ] || die "$NODE_DIR 已存在且不属于本模块，未覆盖。"
        [ ! -e "/etc/systemd/system/$NODE_SERVICE.service" ] || die "发现已有同名服务，未覆盖。"
        check_ports 9100
        case "$(uname -m)" in
            x86_64) arch=amd64 ;;
            aarch64 | arm64) arch=arm64 ;;
            *) die "仅支持 x86_64 / ARM64。" ;;
        esac
        download_node "$arch"
    elif ! systemctl is-active --quiet "$NODE_SERVICE"; then
        check_ports 9100
    fi
    if ! id vps-exporter >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin vps-exporter
    fi
    cat > "/etc/systemd/system/$NODE_SERVICE.service" <<UNIT
[Unit]
Description=VPS toolkit Node Exporter
After=network.target
[Service]
User=vps-exporter
ExecStart=$NODE_DIR/node_exporter --web.listen-address=127.0.0.1:9100
Restart=on-failure
RestartSec=5
MemoryMax=64M
CPUQuota=10%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    if [ -n "$key" ]; then authorize_key "$key"; fi
    systemctl enable --now "$NODE_SERVICE"
    info "采集器已安装；可用 monitor status 查看状态。"
    info "在监控机运行 monitor add-node，SSH 账号可使用已有账号或公钥授权后的 ${NODE_USER}。"
}

setup_ssh() {
    require_command ssh
    require_command runuser
    if ! id "$SSH_USER" >/dev/null 2>&1; then
        [ ! -e "$SSH_DIR" ] || die "$SSH_DIR 已存在，未覆盖。"
        useradd --system --user-group --create-home --home-dir "$SSH_DIR" --shell /usr/sbin/nologin "$SSH_USER"
        touch "$SSH_DIR/.vps-toolkit-managed"
    fi
    [ -f "$SSH_DIR/.vps-toolkit-managed" ] || die "SSH 账号或目录不属于本模块，未覆盖。"
    chmod 700 "$SSH_DIR"
    if [ ! -f "$SSH_DIR/id_ed25519" ]; then
        runuser -u "$SSH_USER" -- ssh-keygen -q -t ed25519 -N '' -C vps-toolkit-monitor -f "$SSH_DIR/id_ed25519"
    fi
    install -d -m 700 -o "$SSH_USER" -g "$SSH_USER" "$SSH_DIR/config.d"
    # %i is always a validated node alias, never user-supplied shell code.
    cat > "/etc/systemd/system/${TUNNEL_UNIT}.service" <<UNIT
[Unit]
Description=VPS toolkit monitoring tunnel to %i
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
User=$SSH_USER
ExecStart=/usr/bin/ssh -F $SSH_DIR/config.d/%i -N -T %i
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
}

public_key() {
    linux_root
    require_hub
    setup_ssh
    cat "$SSH_DIR/id_ed25519.pub"
}

write_target() {
    local name="$1" port="$2"
    valid_name "$name" && valid_port "$port" || die "节点名称或端口无效。"
    jq -n --arg name "$name" --arg target "127.0.0.1:$port" \
        '[{targets:[$target],labels:{node:$name}}]' > "$MONITOR_DIR/prometheus/targets/$name.json.tmp"
    chmod 644 "$MONITOR_DIR/prometheus/targets/$name.json.tmp"
    mv "$MONITOR_DIR/prometheus/targets/$name.json.tmp" "$MONITOR_DIR/prometheus/targets/$name.json"
}

add_node() {
    linux_root
    require_hub
    ensure_packages jq curl
    setup_ssh
    local name host ssh_port remote_user local_port attempt
    read -r -p "节点名称（英文字母/数字/下划线/短横线，最多 32 字符）: " name
    valid_name "$name" || die "节点名称无效。"
    [ ! -e "$SSH_DIR/config.d/$name" ] && [ ! -e "$MONITOR_DIR/prometheus/targets/$name.json" ] || die "节点已存在，请先移除后重新接入。"
    read -r -p "远端 VPS IP 或域名: " host
    valid_host "$host" || die "IP 或域名格式无效。"
    read -r -p "远端 SSH 端口 [22]: " ssh_port; ssh_port="${ssh_port:-22}"
    valid_port "$ssh_port" || die "SSH 端口无效。"
    read -r -p "远端 SSH 用户 [$NODE_USER]: " remote_user; remote_user="${remote_user:-$NODE_USER}"
    valid_user "$remote_user" || die "SSH 用户名无效。"
    read -r -p "本地隧道端口 [19101，其他节点用 19102 等]: " local_port; local_port="${local_port:-19101}"
    valid_port "$local_port" && [ "$((10#$local_port))" -ge 1024 ] || die "本地端口须为 1024～65535。"
    # Normalize leading zeros before serializing ports into config/JSON.
    local_port="$((10#$local_port))"; ssh_port="$((10#$ssh_port))"
    case "$local_port" in 3000 | 9090 | 9100) die "本地端口与监控服务冲突。" ;; esac
    if grep -lF "LocalForward 127.0.0.1:$local_port " "$SSH_DIR"/config.d/* >/dev/null 2>&1; then
        die "该端口已被另一个节点配置占用。"
    fi
    check_ports "$local_port"
    info "请在远端安装采集器并授权以下公钥（公钥可复制，私钥请勿外传）："
    cat "$SSH_DIR/id_ed25519.pub"
    printf '远端执行：vt monitor node-install --public-key %q\n' "$(cat "$SSH_DIR/id_ed25519.pub")"
    info "如果使用其他 SSH 账号，请把此公钥授权给该账号，并允许到 127.0.0.1:9100 的 TCP 转发。"
    confirm "远端已配置好，开始连接？" || return 0
    info "首次连接请核对 SSH 主机密钥指纹，可在可信的远端终端用 ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 查看。"
    # OpenSSH performs normal TOFU fingerprint verification. Never disable host-key checking.
    runuser -u "$SSH_USER" -- ssh -F /dev/null -T \
        -i "$SSH_DIR/id_ed25519" -o IdentitiesOnly=yes \
        -o "UserKnownHostsFile=$SSH_DIR/known_hosts" -o StrictHostKeyChecking=ask \
        -o ConnectTimeout=10 -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
        -p "$ssh_port" "$remote_user@$host" true || true
    # The dedicated remote account has a forced false command. Verify auth with a real tunnel below.
    cat > "$SSH_DIR/config.d/$name" <<CONFIG
Host $name
    HostName $host
    Port $ssh_port
    User $remote_user
    IdentityFile $SSH_DIR/id_ed25519
    UserKnownHostsFile $SSH_DIR/known_hosts
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    LocalForward 127.0.0.1:$local_port 127.0.0.1:9100
CONFIG
    chown "$SSH_USER:$SSH_USER" "$SSH_DIR/config.d/$name"
    chmod 600 "$SSH_DIR/config.d/$name"
    if ! systemctl enable --now "${TUNNEL_UNIT}$name"; then
        systemctl disable --now "${TUNNEL_UNIT}$name" || true
        rm -f "$SSH_DIR/config.d/$name"
        die "隧道服务未能启动；未添加采集目标。"
    fi
    for attempt in {1..10}; do
        if curl --noproxy '*' --max-time 3 -fsS "http://127.0.0.1:$local_port/metrics" | grep '^node_exporter_build_info' >/dev/null; then
            write_target "$name" "$local_port"
            info "已接入 ${name}，Prometheus 将自动发现（约 30～90 秒）。"
            return 0
        fi
        sleep 2
    done
    systemctl disable --now "${TUNNEL_UNIT}$name" || true
    rm -f "$SSH_DIR/config.d/$name"
    die "未能通过隧道获取指标；未添加采集目标。请检查 SSH、公钥授权、转发权限和远端采集器。"
}

remove_node() {
    linux_root
    require_hub
    local name
    read -r -p "要移除的节点名称: " name
    valid_name "$name" || die "节点名称无效。"
    [ -f "$SSH_DIR/config.d/$name" ] || [ -f "$MONITOR_DIR/prometheus/targets/$name.json" ] || die "没有这个节点。"
    confirm "移除 $name 的采集和隧道？远端服务与历史数据将保留。" || return 0
    systemctl disable --now "${TUNNEL_UNIT}$name" || true
    rm -f "$SSH_DIR/config.d/$name" "$MONITOR_DIR/prometheus/targets/$name.json"
    info "已移除；Prometheus 将自动更新目标。"
}

show_access() {
    require_hub
    cat <<ACCESS
在 Mac 终端运行（替换用户名、IP 和 SSH 端口）：
ssh -N -T -p SSH端口 -L 127.0.0.1:13000:127.0.0.1:3000 -L 127.0.0.1:19090:127.0.0.1:9090 用户名@监控机IP
Grafana：http://127.0.0.1:13000 （admin）
Prometheus：http://127.0.0.1:19090/targets
Grafana 初始密码保存在 $MONITOR_DIR/.env，仅在自己的终端查看。
Mac 断开不影响采集；修改 .env 不会重置已有 Grafana 数据库的密码。
ACCESS
}

show_status() {
    if hub_installed; then
        compose ps
        compose stats --no-stream
        info "远端节点："
        local file
        for file in "$MONITOR_DIR"/prometheus/targets/*.json; do
            [ -f "$file" ] || continue
            printf '  %s\n' "$(basename "$file" .json)"
            systemctl is-active "${TUNNEL_UNIT}$(basename "$file" .json)" || true
        done
    elif node_installed; then
        systemctl status "$NODE_SERVICE" --no-pager || true
    else
        info "本机未安装本工具管理的监控服务。"
    fi
    if command -v free >/dev/null 2>&1; then free -h; fi
}

manage() {
    linux_root
    local action="$1" file
    if hub_installed; then
        case "$action" in
            start) compose up -d ;;
            stop) compose stop ;;
            restart) compose up -d; compose restart ;;
        esac
        for file in "$SSH_DIR"/config.d/*; do
            [ -f "$file" ] || continue
            systemctl "$action" "${TUNNEL_UNIT}$(basename "$file")"
        done
    elif node_installed; then
        systemctl "$action" "$NODE_SERVICE"
    else
        die "本机未安装监控服务。"
    fi
}

show_logs() {
    if hub_installed; then
        compose logs --tail=100
        info "隧道日志：journalctl -u 'vps-toolkit-monitor-tunnel@*' -n 50 --no-pager"
    elif node_installed; then
        journalctl -u "$NODE_SERVICE" -n 100 --no-pager
    else
        info "本机未安装监控服务。"
    fi
}

uninstall() {
    linux_root
    confirm "移除本工具的监控服务？配置、密码、历史数据和 SSH 授权将保留。" || return 0
    local file
    if hub_installed; then
        compose down
        for file in "$SSH_DIR"/config.d/*; do
            [ -f "$file" ] || continue
            systemctl disable --now "${TUNNEL_UNIT}$(basename "$file")" || true
        done
        info "容器已移除，Docker 数据卷和 $MONITOR_DIR 均保留，start 可恢复。"
    elif node_installed; then
        systemctl disable --now "$NODE_SERVICE"
        rm -f "/etc/systemd/system/$NODE_SERVICE.service"
        systemctl daemon-reload
        # Keep the binary/ownership marker for reinstallation and to avoid removing user accounts.
        info "采集器服务已移除，SSH 用户、公钥授权及二进制保留。"
        info "运行 node-install 可重新创建并启用服务。"
    else
        info "本机未安装监控服务。"
    fi
}

main() {
    case "${1:-help}" in
        hub-install) hub_install ;;
        node-install) shift; node_install "$@" ;;
        add-node) add_node ;;
        remove-node) remove_node ;;
        public-key) public_key ;;
        access) show_access ;;
        status) show_status ;;
        logs) show_logs ;;
        start | stop | restart) manage "$1" ;;
        uninstall) uninstall ;;
        help | -h | --help) show_help ;;
        *) die "未知操作：$1；使用 monitor help 查看用法。" ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
