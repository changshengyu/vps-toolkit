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

# shellcheck source=../lib/monitor/tls.sh
. "$ROOT_DIR/lib/monitor/tls.sh"
# shellcheck source=../lib/monitor/deploy.sh
. "$ROOT_DIR/lib/monitor/deploy.sh"
# shellcheck source=../lib/monitor/renew.sh
. "$ROOT_DIR/lib/monitor/renew.sh"

show_help() {
    cat <<'HELP'
用法：vt monitor 操作 [参数]
推荐顺序：在监控机执行 hub-install → deploy-node → access
  hub-install                    安装监控机（仅一台）
  deploy-node                    填域名和 SSH 信息，自动安装并接入节点
  access                         查看面板访问方法
  status|logs                    查看本机状态或日志
  renew-node                     无密码续签并同步远端证书
  cert-status                    查看证书有效期
  start|stop|restart             管理本机监控服务
  uninstall                      卸载服务，保留历史；节点续期公钥会撤销
  help                           显示帮助
高级手动接入：
  prepare-node                   手动生成证书包
  node-install --bundle FILE     在远端手动安装证书包
  add-node                       验证已安装的节点并登记目标
  remove-node                    移除采集与自动续期记录
  cert-renew [client|节点名]      续签客户端证书或只生成节点安装包
集中端需要 Docker Compose v2；节点端仅运行 Node Exporter。
自动部署后配置受限续期密钥，节点和客户端证书到期前自动更新。
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
    ensure_packages curl openssl jq python3
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
    ensure_pki
    enable_https_job
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
    local bundle="" arch
    if [ "${1:-}" = --bundle ] && [ "$#" -eq 2 ]; then
        bundle="$2"
    elif [ "$#" -ne 0 ]; then
        die "用法：monitor node-install --bundle /安全路径/节点名.tar.gz"
    fi
    hub_installed && die "本机已是监控机，自带 Node Exporter，无需重复安装。"
    if [ -z "$bundle" ]; then read -r -p "节点证书包的绝对路径: " bundle; fi
    [ -f "$bundle" ] || die "证书包不存在，请先在监控机运行 prepare-node。"
    info "节点端将启用 HTTPS 双向证书认证及独立 nftables 源 IP 限制，不修改代理配置。"
    confirm "确认安装或迁移本机采集器？" || return 0
    ensure_packages curl tar openssl jq python3
    require_command sha256sum
    if ! node_installed; then
        [ ! -e "$NODE_DIR" ] || die "$NODE_DIR 已存在且不属于本模块，未覆盖。"
        [ ! -e "$SYSTEMD_DIR/$NODE_SERVICE.service" ] || die "发现已有同名服务，未覆盖。"
        case "$(uname -m)" in
            x86_64) arch=amd64 ;;
            aarch64 | arm64) arch=arm64 ;;
            *) die "仅支持 x86_64 / ARM64。" ;;
        esac
        download_node "$arch"
    fi
    if ! id vps-exporter >/dev/null 2>&1; then
        useradd --system --user-group --no-create-home --shell /usr/sbin/nologin vps-exporter
    fi
    install_node_bundle "$bundle"
}

remove_node() {
    linux_root
    require_hub
    local name
    read -r -p "要移除的节点名称: " name
    valid_name "$name" || die "节点名称无效。"
    [ -f "$MONITOR_DIR/prometheus/https-targets/$name.json" ] || [ -f "$MONITOR_DIR/prometheus/targets/$name.json" ] || die "没有这个采集目标。"
    confirm "移除 $name 的采集目标？远端服务、历史和证书保留。" || return 0
    cleanup_legacy_node "$name"
    rm -f "$MONITOR_DIR/prometheus/https-targets/$name.json"
    rm -f "$DEPLOY_DIR/$name.json"
    info "已移除；远端若不再使用，请在那里卸载采集器。"
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
        for file in "$MONITOR_DIR"/prometheus/targets/*.json "$MONITOR_DIR"/prometheus/https-targets/*.json; do
            [ -f "$file" ] || continue
            printf '  %s\n' "$(basename "$file" .json)"
            if [[ "$file" == */https-targets/* ]]; then
                info "  HTTPS mTLS（实际采集结果请看 Prometheus targets）"
            else
                systemctl is-active "${TUNNEL_UNIT}$(basename "$file" .json)" || true
            fi
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
        if [ "$action" = stop ]; then
            if [ -f "$SYSTEMD_DIR/$RENEW_SERVICE.timer" ]; then systemctl disable --now "$RENEW_SERVICE.timer"; fi
        elif compgen -G "$DEPLOY_DIR/*.json" >/dev/null; then
            install_renew_timer
        fi
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
    confirm "移除本工具的监控服务？保留配置、密码和历史；节点专用续期授权会撤销。" || return 0
    local file
    if hub_installed; then
        if [ -f "$SYSTEMD_DIR/$RENEW_SERVICE.timer" ]; then
            systemctl disable --now "$RENEW_SERVICE.timer"
            rm -f "$SYSTEMD_DIR/$RENEW_SERVICE.timer" "$SYSTEMD_DIR/$RENEW_SERVICE.service"
            systemctl daemon-reload
        fi
        compose down
        for file in "$SSH_DIR"/config.d/*; do
            [ -f "$file" ] || continue
            systemctl disable --now "${TUNNEL_UNIT}$(basename "$file")" || true
        done
        info "容器已移除，Docker 数据卷和 $MONITOR_DIR 均保留，start 可恢复。"
    elif node_installed; then
        systemctl disable --now "$NODE_SERVICE"
        revoke_renewal_key
        if [ -f "$NODE_CONFIG_DIR/.vps-toolkit-managed" ]; then
            systemctl disable --now "$FIREWALL_SERVICE"
            rm -f "$SYSTEMD_DIR/$FIREWALL_SERVICE.service"
        fi
        rm -f "$SYSTEMD_DIR/$NODE_SERVICE.service"
        systemctl daemon-reload
        # Keep the binary/ownership marker for reinstallation and to avoid removing user accounts.
        info "采集器和本模块 IP 限制服务已移除，证书及二进制保留；旧 SSH 授权需自行撤销。"
        info "运行 node-install 可重新创建并启用服务。"
    else
        info "本机未安装监控服务。"
    fi
}

main() {
    case "${1:-help}" in
        hub-install) hub_install ;;
        node-install) shift; node_install "$@" ;;
        deploy-node) monitor_lock deploy_node ;;
        renew-node) monitor_lock renew_node ;;
        renew-due) monitor_lock renew_due ;;
        renew-saved)
            linux_root
            require_hub
            [ "$#" -eq 2 ] || die "缺少部署记录。"
            renew_managed_node "$2"
            ;;
        renewal-authorize)
            [ "$#" -eq 3 ] || die "缺少续期授权参数。"
            authorize_renewal_key "$2" "$3"
            ;;
        prepare-node) prepare_node ;;
        add-node) add_https_node ;;
        remove-node) remove_node ;;
        public-key) die "新节点已改用 HTTPS mTLS；请使用 prepare-node，不再生成 SSH 公钥。" ;;
        cert-status) cert_status ;;
        cert-renew) shift; cert_renew "${1:-}" ;;
        access) show_access ;;
        status) show_status ;;
        logs) show_logs ;;
        start | stop | restart) manage "$1" ;;
        uninstall) uninstall ;;
        help | -h | --help) show_help ;;
        *) die "未知操作：$1；使用 monitor help 查看用法。" ;;
    esac
}

if [ "${MONITOR_INSTALL_NONINTERACTIVE:-0}" = 1 ] && [ "${1:-}" = node-install ]; then
    # Only the explicit node installer may skip confirmation after authenticated SSH provisioning.
    confirm() { return 0; }
fi
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
