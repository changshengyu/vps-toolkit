"""Pinned Komari binaries and isolated native systemd services; stdlib only."""
import argparse
import base64
import contextlib
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MARKER = 'vps-toolkit-komari-v1'
VERSIONS = {'server': '1.6.1', 'agent': '1.5.11', 'tunnel': '2026.11.0'}
HASHES = {
    'server': {'amd64': '3deeb5607a229c506fa1e4fba06aae2083c9f20b7909cbf485c7b60c118c739e',
               'arm64': 'a05fc3d27b3f93a81cf9129e3674e9d8541fafabed65742572c5a63b1a4a1e06'},
    'agent': {'amd64': '78c28d89e523816baea010c0ed0714f245f508ffdaca0540f5c9f230f7053c8c',
              'arm64': 'ff8e33dc12ef2b5738fa291a58498bd15dcbf8ef51966ab65fd67679a0abc87d'},
    'tunnel': {'amd64': '51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd',
               'arm64': 'abe54a006acf9faf2d19ee9d953cb0d0804e92f59c5382a162e57b84d086f56f'},
}
ASSET_IDS = {'server': {'amd64': 677083030, 'arm64': 677085117},
             'agent': {'amd64': 569642242, 'arm64': 569642206},
             'tunnel': {'amd64': 739395030, 'arm64': 739395569}}
USERS = {'server': 'vps-komari', 'agent': 'root', 'tunnel': 'vps-komari-tunnel'}
MEMORY = {'server': (256, 128, 50), 'agent': (64, 48, 15), 'tunnel': (96, 64, 20)}


OFFICIAL_UNIT = 'komari-agent.service'
INSTALLER_COMMIT = 'c7bafb79b1a73ed9e14e2c248ae4d4163e506036'
INSTALLER_SHA256 = '17294dbdbdde3d6a88d8e21323804c9f2814cc6f217c63ee8054db1788ecd446'


class Error(Exception):
    pass


def command(args, timeout=30, check=True):
    try:
        result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Error('命令无法运行或超时：' + str(args[0])) from None
    if check and result.returncode:
        # Do not relay credential-bearing command output into diagnostics.
        if str(args[0]) in ('systemctl', 'systemd-analyze'):
            action = ' '.join(str(a) for a in args[:2])
            detail = redact('\n'.join((result.stderr or result.stdout).splitlines()[-8:]))
            raise Error(f'{action} 执行失败（退出码 {result.returncode}）' + ('：\n' + detail if detail else '；请查看本模块日志。'))
        raise Error(f'{args[0]} 执行失败（退出码 {result.returncode}）；请查看本模块日志。')
    return result


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def working_directory(value):
    # This scalar systemd directive does not remove shell-style surrounding quotes.
    value = str(value)
    if not value.startswith('/') or any(c in value for c in '\r\n\0'):
        raise Error('systemd 工作目录必须为不含换行的绝对路径。')
    return value.replace('%', '%%')


def redact(value):
    value = re.sub(r'(?i)(token|password|secret|authorization)(["\s:=]+)[^\s,;]+', r'\1\2[REDACTED]', value)
    return re.sub(r'(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', value)


def atomic(path, value, mode=0o600):
    if path.is_symlink():
        raise Error('拒绝覆盖符号链接：' + str(path))
    data = value if isinstance(value, bytes) else value.encode()
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=True, indent=2) + '\n')


def port_number(value):
    if not re.fullmatch(r'[0-9]{1,5}', str(value)) or not 1024 <= int(value) <= 65535:
        raise Error('端口必须为 1024～65535。')
    return int(value)


def endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ('', '/') or parsed.port not in (None, 443)):
            raise ValueError()
        if any(ord(c) < 33 for c in value) or not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
            raise ValueError()
        if len(host) > 253 or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', p) for p in host.split('.')):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
            raise Error('请使用 HTTPS 域名，Agent 不使用裸 IP 入口。')
        except ValueError:
            pass
        if '.' not in host:
            raise ValueError()
        return 'https://' + host.lower()
    except ValueError:
        raise Error('请输入完整 HTTPS 域名，如 https://komari.example.com，不含路径、凭据或查询参数。') from None


def credential(value):
    if not 8 <= len(value) <= 8192 or any(c.isspace() or ord(c) < 33 for c in value):
        raise Error('Token / 自动发现密钥格式无效，需为不含空白的完整字符串。')
    return value


def tunnel_credential(value):
    credential(value)
    try:
        value_json = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), validate=True))
        if (not isinstance(value_json, dict) or not all(isinstance(value_json.get(k), str) and value_json[k] for k in ('a', 't', 's'))
                or not re.fullmatch(r'[a-fA-F0-9]{32}', value_json['a'])
                or not re.fullmatch(r'[a-fA-F0-9-]{36}', value_json['t'])):
            raise ValueError()
    except (ValueError, TypeError):
        raise Error('不是有效的 Cloudflare Tunnel Token；请复制连接器安装命令中的完整 Token，不是 API Token。') from None
    return value


def confirm(message):
    return input(message + ' [y/N]: ').strip().lower() in ('y', 'yes')


def architecture():
    value = platform.machine()
    if value in ('x86_64', 'amd64'):
        return 'amd64'
    if value in ('aarch64', 'arm64'):
        return 'arm64'
    raise Error('仅支持 Linux x86_64 / ARM64。')


def asset(role, arch):
    if role == 'server':
        repo, name = 'komari-monitor/komari', 'komari-linux-' + arch
    elif role == 'agent':
        repo, name = 'komari-monitor/komari-agent', 'komari-agent-linux-' + arch
    else:
        repo, name = 'cloudflare/cloudflared', 'cloudflared-linux-' + arch
    return f'https://github.com/{repo}/releases/download/{VERSIONS[role]}/{name}', HASHES[role][arch]


def request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with client.open(req, timeout=5) as response:
            body = response.read(1048577)
            if len(body) > 1048576:
                raise Error('本机接口响应过大。')
            try:
                value = json.loads(body) if body else {}
            except ValueError:
                value = {}
            return response.status, value
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (OSError, ValueError, urllib.error.URLError):
        raise Error('本机接口暂未就绪或连接失败。') from None


class Paths:
    def __init__(self, prefix=Path('/')):
        self.prefix = Path(prefix)
        self.base = self.prefix / 'opt/vps-toolkit-komari'
        self.bin = self.base / 'bin'
        self.config = self.base / 'config'
        self.units = self.prefix / 'etc/systemd/system'

    def unit(self, role):
        return self.units / ('vps-toolkit-komari-' + role + '.service')

    def work(self, role):
        return self.base / role

    def binary(self, role):
        return self.bin / {'server': 'komari', 'agent': 'komari-agent', 'tunnel': 'cloudflared'}[role]

    def state(self, role):
        return self.config / (role + '.json')


def service_unit(paths, role, config):
    work = paths.work(role)
    if role == 'server':
        args = f'server --listen 127.0.0.1:{port_number(config["port"])}'
    elif role == 'agent':
        args = '--config ' + quote(work / 'agent.json')
    else:
        args = ('tunnel --no-autoupdate --edge-ip-version 4 --metrics 127.0.0.1:20246 '
                'run --token-file ' + quote(work / 'token'))
    hard, go, cpu = MEMORY[role]
    return f'''# {MARKER}
[Unit]
Description=VPS Toolkit Komari {role}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User={USERS[role]}
Group={USERS[role]}
WorkingDirectory={working_directory(work)}
ExecStart={quote(paths.binary(role))} {args}
Environment=GOMEMLIMIT={go}MiB
Environment=GOMAXPROCS=1
Environment=GODEBUG=disablethp=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
MemoryMax={hard}M
CPUQuota={cpu}%
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={quote(work)}
CapabilityBoundingSet={'CAP_NET_RAW' if role == 'agent' else ''}
UMask=0077
LogRateLimitIntervalSec=30s
LogRateLimitBurst=100
[Install]
WantedBy=multi-user.target
'''


class Komari:
    def __init__(self, paths=None):
        self.paths = paths or Paths()

    def owned(self, create=False):
        p = self.paths
        if not p.base.exists() and not p.base.is_symlink():
            if not create:
                raise Error('本机尚未通过本工具部署 Komari。')
            p.base.mkdir(parents=True, mode=0o755)
            atomic(p.base / '.vps-toolkit-owned', MARKER, 0o644)
        if (p.base.is_symlink() or not (p.base / '.vps-toolkit-owned').is_file()
                or (p.base / '.vps-toolkit-owned').read_text() != MARKER
                or p.base.stat().st_uid != os.geteuid() or p.base.stat().st_mode & 0o022):
            raise Error('目录不属于本模块或权限异常，未覆盖：' + str(p.base))
        for path in (p.bin, p.config):
            if path.is_symlink() or (path.exists() and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022)):
                raise Error('管理目录权限异常，未继续：' + str(path))
            if create:
                path.mkdir(exist_ok=True, mode=0o755 if path == p.bin else 0o700)
        for role in VERSIONS:
            for path in (p.unit(role), p.binary(role), p.state(role)):
                if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022)):
                    raise Error('管理文件权限异常，未继续：' + str(path))
            if p.unit(role).exists() and not p.unit(role).read_text().startswith('# ' + MARKER + '\n'):
                raise Error('同名 systemd 服务不属于本模块，未覆盖。')
            if p.work(role).is_symlink():
                raise Error('工作目录不能为符号链接。')

    @contextlib.contextmanager
    def lock(self):
        self.owned(create=True)
        path = self.paths.base / '.lock'
        if path.is_symlink():
            raise Error('锁文件不能为符号链接。')
        with path.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error('已有 Komari 管理任务运行，请稍后重试。') from None
            yield

    def state(self, role):
        path = self.paths.state(role)
        if not path.is_file():
            raise Error('尚未配置此角色：' + role)
        value = json.loads(path.read_text())
        if value.get('managed_by') != MARKER or value.get('role') != role or value.get('version') != VERSIONS[role]:
            raise Error('配置不符合本工具管理的固定版本，未修改。')
        if role == 'server':
            port_number(value['port'])
        return value

    def active(self, role):
        return command(['systemctl', 'is-active', '--quiet', self.paths.unit(role).name], check=False).returncode == 0

    def free_port(self, port):
        with socket.socket() as stream:
            try:
                stream.bind(('127.0.0.1', port))
            except OSError:
                raise Error(f'本机 TCP {port} 已被占用；未停止其他服务。') from None

    def headroom(self, minimum):
        path = self.paths.prefix / 'proc/meminfo'
        if path.exists():
            match = re.search(r'^MemAvailable:\s+(\d+) kB', path.read_text(), re.M)
            if match and int(match.group(1)) < minimum * 1024:
                raise Error(f'当前可用内存不足 {minimum} MiB，请先释放资源再安装；不会自动停止旧监控或代理。')

    def ensure_user(self, role):
        name = USERS[role]
        if name == 'root':
            return 0, 0
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            command(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', self.paths.work(role),
                     '--shell', '/usr/sbin/nologin', name])
            account = pwd.getpwnam(name)
        if account.pw_uid == 0:
            raise Error('专用服务账号不能使用 UID 0。')
        return account.pw_uid, account.pw_gid

    def download(self, role):
        p = self.paths
        url, expected = asset(role, architecture())
        if p.binary(role).exists():
            with p.binary(role).open('rb') as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() == expected:
                return
            raise Error('已有二进制与固定版本校验值不一致；未覆盖，请检查文件来源。')
        print(f'下载官方 {role} {VERSIONS[role]} 并校验 SHA-256（每次最多 60 秒）…', flush=True)
        fd, temp = tempfile.mkstemp(prefix='.download-', dir=p.bin)
        os.close(fd)
        try:
            options = ['curl', '-q', '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--proto-redir', '=https',
                     '--tlsv1.2', '--connect-timeout', '10', '--max-time', '60', '--retry', '1',
                     '--retry-max-time', '100', '--output', temp]
            result = command(options + [url], timeout=125, check=False)
            if result.returncode:
                repo = {'server': 'komari-monitor/komari', 'agent': 'komari-monitor/komari-agent', 'tunnel': 'cloudflare/cloudflared'}[role]
                alternative = f'https://api.github.com/repos/{repo}/releases/assets/{ASSET_IDS[role][architecture()]}'
                print('直链下载失败，改用同一官方 Release 的 API 下载地址（仍校验相同 SHA-256）…', flush=True)
                result = command(options + ['--continue-at', '-', '--header', 'Accept: application/octet-stream', alternative], timeout=125, check=False)
                if result.returncode:
                    raise Error(f'下载官方 {role} {VERSIONS[role]} 失败（curl 退出码 {result.returncode}）；请检查到 GitHub 的网络后重试，未安装下载文件。')
            with open(temp, 'rb') as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise Error('官方文件 SHA-256 校验失败，未安装。')
            os.chmod(temp, 0o755)
            os.replace(temp, p.binary(role))
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def install_service(self, role, config, files=None):
        p = self.paths
        if p.state(role).exists():
            self.state(role)
        self.download(role)
        uid, gid = self.ensure_user(role)
        work = p.work(role)
        if work.exists() and (work.stat().st_uid not in (os.geteuid(), uid) or work.stat().st_mode & 0o022):
            raise Error('工作目录所有者或权限异常，未继续。')
        work.mkdir(mode=0o700, exist_ok=True)
        os.chown(work, uid, gid)
        os.chmod(work, 0o700)
        if role == 'agent' and files and json.loads(files['agent.json']).get('auto_discovery_key'):
            self.prepare_discovery_cache()
        p.units.mkdir(parents=True, exist_ok=True)
        unit_text = service_unit(p, role, config)
        self.verify_unit(role, unit_text)
        changed = [p.unit(role), p.state(role)] + [work / name for name in (files or {})]
        previous = {}
        for path in changed:
            if path.is_symlink():
                raise Error('配置文件不能为符号链接。')
            previous[path] = (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_uid, path.stat().st_gid) if path.exists() else None
        running = self.active(role)
        enabled = command(['systemctl', 'is-enabled', '--quiet', p.unit(role).name], check=False).returncode == 0
        try:
            for name, value in (files or {}).items():
                atomic(work / name, value)
                os.chown(work / name, uid, gid)
            save(p.state(role), {'managed_by': MARKER, 'role': role, 'version': VERSIONS[role], **config})
            atomic(p.unit(role), unit_text, 0o644)
            command(['systemctl', 'daemon-reload'])
            command(['systemctl', 'enable', p.unit(role).name])
            command(['systemctl', 'restart', p.unit(role).name])
            if role == 'server':
                self.wait_server(config['port'])
            elif not self.active(role):
                raise Error('服务启动后未保持运行。')
        except Exception:
            self.activation_diagnostics(role)
            command(['systemctl', 'stop', p.unit(role).name], check=False)
            for path, old in previous.items():
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, old[0], old[1])
                    os.chown(path, old[2], old[3])
            command(['systemctl', 'daemon-reload'], check=False)
            if not enabled:
                command(['systemctl', 'disable', p.unit(role).name], check=False)
            if running:
                command(['systemctl', 'start', p.unit(role).name], check=False)
            raise

    def verify_unit(self, role, text):
        if not shutil.which('systemd-analyze'):
            print('提示：未安装 systemd-analyze，跳过本机 unit 预校验。')
            return
        with tempfile.TemporaryDirectory(prefix='vt-komari-unit-') as folder:
            path = Path(folder) / self.paths.unit(role).name
            atomic(path, text, 0o644)
            command(['systemd-analyze', 'verify', '--man=no', path])

    def activation_diagnostics(self, role):
        # Collect before rollback removes a newly-created unit.
        print('启动失败诊断（随后回退本次 unit 和管理配置）：', file=sys.stderr)
        try:
            result = command(['systemctl', 'show', self.paths.unit(role).name,
                              '--property=LoadState,ActiveState,SubState,Result,ExecMainStatus'], check=False)
            print(redact(result.stdout), file=sys.stderr)
            self.logs(role, limit=20)
        except (Error, OSError, ValueError, KeyError):
            print('自动诊断未完成；可运行 journalctl -u ' + self.paths.unit(role).name + ' -b -n 60 --no-pager。', file=sys.stderr)

    def wait_server(self, port, initialized=False):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                route = '/api/version' if initialized else '/api/install/status'
                status, body = request(f'http://127.0.0.1:{port}' + route)
                ready = status == 200 and bool(body) if initialized else status in (200, 404)
                if ready and self.active('server'):
                    return
            except Error:
                pass
            time.sleep(1)
        raise Error('Komari 60 秒内未就绪，请查看服务端日志和当前服务状态。')

    def initialize(self):
        cfg = self.state('server')
        base = f'http://127.0.0.1:{cfg["port"]}'
        if self.initialized():
            print('已有管理员账号；保留账号、密码和监控数据。')
            return
        status, result = request(base + '/api/install/status')
        data = result.get('data', result.get('obj', {}))
        if status != 200 or not isinstance(data, dict) or not data.get('required'):
            raise Error('初始化状态不明确，请通过 SSH 转发访问本机页面检查。')
        print('首次初始化管理员（仅调用本机接口；密码不保存到工具配置或日志）。')
        username = input('管理员用户名 [admin]，输入 q 取消初始化: ').strip() or 'admin'
        if username.lower() == 'q':
            print('已保留本机安装向导；可重新选择安装/初始化，或通过 SSH 完成。')
            return
        password = getpass.getpass('管理员密码（8～256 字符，含大写、小写和数字）: ')
        again = getpass.getpass('再次输入管理员密码: ')
        if (password != again or not 8 <= len(password) <= 256 or not any(c.isupper() for c in password)
                or not any(c.islower() for c in password) or not any(c.isdigit() for c in password)):
            raise Error('密码不一致或强度不满足要求；服务保留在本机，可重新初始化。')
        if not 1 <= len(username) <= 64 or any(ord(c) < 32 for c in username):
            raise Error('管理员用户名需为 1～64 个可见字符。')
        status, _ = request(base + '/api/install/complete', {
            'username': username, 'password': password, 'sitename': 'VPS Monitor',
            'description': '', 'metric_dsn': './data/metrics.db'})
        if status != 200:
            raise Error(f'初始化接口返回 HTTP {status}，账号未确认创建；请查看本机安装页面。')
        deadline = time.monotonic() + 15
        while not self.initialized():
            if time.monotonic() >= deadline:
                raise Error('初始化请求已提交，但未确认数据库中已创建管理员；请通过 SSH 检查本机页面。')
            time.sleep(.2)
        self.wait_server(cfg['port'], initialized=True)
        print('管理员已初始化；请用刚设置的账号密码登录。')

    def initialized(self):
        # The regular SPA can answer /api/install/status with HTML HTTP 200.
        # Read only the account count; never read credentials or trust that status alone.
        path = self.paths.work('server') / 'data/komari.db'
        if path.is_symlink():
            raise Error('服务端数据库不能为符号链接。')
        if not path.exists():
            return False
        try:
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                return db.execute('SELECT COUNT(*) FROM users').fetchone()[0] > 0
        except sqlite3.Error:
            raise Error('无法只读确认管理员账号，请检查数据库和服务端日志；未发布域名入口。') from None

    def server_install(self):
        p = self.paths
        self.owned(create=True)
        if p.state('server').exists():
            cfg = self.state('server')
            if not self.active('server') or not p.binary('server').exists() or not p.unit('server').exists():
                if not self.active('server'):
                    self.free_port(cfg['port'])
                self.install_service('server', {'port': cfg['port']})
            self.initialize()
            self.access()
            return
        port = port_number(input('Komari 本机监听端口 [25774]: ').strip() or '25774')
        self.free_port(port)
        self.headroom(192)
        print(f'使用原生 systemd，固定服务端 {VERSIONS["server"]}，仅监听 127.0.0.1:{port}。')
        print('不占用 80/443，不停止代理或旧监控；服务内存上限 256 MiB（不是实际占用值）。')
        if not confirm('安装并设置开机自启？'):
            return
        self.install_service('server', {'port': port})
        self.initialize()
        self.access()

    def setup_install(self):
        self.server_install()
        if not self.active('server') or not self.initialized():
            print('完成本机管理员初始化后，再选本项开通 HTTPS。')
            return
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'monitor'))
        import cloudflare as cf
        directory = self.paths.config / 'cloudflare'
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            state = cf.load_state(directory, 'komari') if (directory / 'state.json').exists() else {}
            if not state and self.paths.state('tunnel').exists():
                print('已保留原有连接器和域名：' + self.state('tunnel')['endpoint'])
                return
            if not self.active('tunnel'):
                self.free_port(20246)
                self.headroom(96)
            if not state.get('configured'):
                domain = state.get('domain') or cf.domain_name(input('新的 Komari 一级子域名（例如 komari.example.com）: ').strip())
                print('使用 Cloudflare Universal SSL 自动签发及续期。请确认该 Zone 已启用 Universal SSL。')
                print('API Token 限定所属账户和域名：Account / Cloudflare Tunnel / Edit；Zone / DNS / Edit；Zone / Zone / Read。')
                if not confirm('为 ' + domain + ' 创建独立 Tunnel 和 DNS 记录？'):
                    return
                with tempfile.TemporaryDirectory() as tmp:
                    key = Path(tmp) / 'api-token'
                    atomic(key, getpass.getpass('Cloudflare API Token（隐藏输入，仅本次使用）: ').strip())
                    api = cf.API(key)
                if state:
                    cf.remove_remote(api, directory, 'komari')
                cf.provision(api, directory, domain, 'komari', port_number(self.state('server')['port']))
                state = cf.load_state(directory, 'komari')
            url = 'https://' + state['domain']
            token = (directory / 'connector-token').read_text().strip()
            tunnel_credential(token)
            self.connect_tunnel(url, token)
            probe = command(['curl', '-q', '--noproxy', '*', '--proto', '=https', '--silent',
                             '--fail', '--max-time', '10', url + '/api/version'], check=False)
            try:
                verified = probe.returncode == 0 and bool(json.loads(probe.stdout))
            except ValueError:
                verified = False
            print('公网 HTTPS 已验证。' if verified else
                  '连接器已启动，公网 HTTPS 尚未验证。请等待 DNS 生效，并在 Cloudflare → SSL/TLS → 边缘证书确认 Universal SSL 已激活。')
        except cf.Error as exc:
            raise Error(str(exc)) from None

    def tunnel_install(self):
        cfg = self.state('server')
        if not self.active('server'):
            raise Error('请先启动 Komari 服务端。')
        if not self.initialized():
            raise Error('请先完成管理员初始化，再发布域名入口。')
        old = self.state('tunnel') if self.paths.state('tunnel').exists() else {}
        print('在 Cloudflare 控制台创建独立的 remotely-managed Cloudflared Tunnel：')
        print(f'发布应用路由 → 子域名 → HTTP 服务 http://127.0.0.1:{cfg["port"]}。')
        print('复制安装连接器命令中的 Tunnel Token（不是账户 API Token）；不要修改现有 Grafana Tunnel。')
        raw = input('Komari HTTPS 域名' + (' [' + old['endpoint'] + ']' if old else '') + ': ').strip()
        url = endpoint(raw or old.get('endpoint', ''))
        token = getpass.getpass('Tunnel Token（隐藏输入；已有配置留空保留）: ').strip()
        if self.paths.work('tunnel').joinpath('token').is_symlink():
            raise Error('Token 文件不能为符号链接。')
        if not token and self.paths.work('tunnel').joinpath('token').is_file():
            token = self.paths.work('tunnel').joinpath('token').read_text().strip()
        tunnel_credential(token)
        if not self.active('tunnel'):
            self.free_port(20246)
            self.headroom(96)
        if not confirm('保存 Token 并启动本工具的独立 Cloudflare 连接器？'):
            return
        self.connect_tunnel(url, token)

    def connect_tunnel(self, url, token):
        self.install_service('tunnel', {'endpoint': url}, {'token': token})
        print('连接器服务已启动，正在等待 Cloudflare 连接（最多 30 秒）…', flush=True)
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            try:
                status, _ = request('http://127.0.0.1:20246/ready')
                if status == 200:
                    ready = True
                    break
            except Error:
                pass
            time.sleep(1)
        print('入口地址：' + url + '/')
        print('请在浏览器验证 HTTPS 登录及节点上报。')
        if not ready:
            raise Error('连接器已配置，但未确认连上 Cloudflare；配置保留供自动重连，请查看 tunnel 日志。')
        print('连接器已连上 Cloudflare。')

    def prepare_discovery_cache(self):
        # Agent 1.5.11 opens this exact path beside its executable, including
        # on first registration. Only the private work directory is writable
        # inside its systemd mount namespace.
        source = self.paths.bin / 'auto-discovery.json'
        target = self.paths.work('agent') / source.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise Error('自动发现身份路径异常，未重新注册。')
        if source.is_symlink():
            if os.readlink(source) != str(target):
                raise Error('程序目录存在未知的自动发现身份链接，未覆盖。')
            return
        if source.exists():
            if not source.is_file():
                raise Error('自动发现身份不是普通文件，未覆盖。')
            data = source.read_bytes()
            if target.exists() and target.read_bytes() != data:
                raise Error('存在两份不同的自动发现身份，未覆盖。')
            atomic(target, data)
        # Never create an empty JSON identity. Upstream registers only if
        # there is no usable identity; its first write follows this link.
        with tempfile.TemporaryDirectory(dir=self.paths.bin) as tmp:
            link = Path(tmp) / source.name
            link.symlink_to(target)
            os.replace(link, source)

    @staticmethod
    def agent_summary(info):
        memory = info.get('MemoryCurrent', '')
        return ('运行=' + info.get('ActiveState', '?') + '/' + info.get('SubState', '?')
                + '；开机启动=' + info.get('UnitFileState', '?') + '；当前内存='
                + (f'{int(memory) / 1048576:.1f} MiB' if memory.isdigit() and int(memory) < 2**63 else '未知'))

    def official_info(self):
        # systemctl show may return a nonzero status for a missing unit.
        res = command(['systemctl', 'show', OFFICIAL_UNIT,
                       '--property=LoadState,ActiveState,SubState,UnitFileState,MemoryCurrent'], check=False)
        info = dict(line.split('=', 1) for line in res.stdout.splitlines() if '=' in line)
        if info.get('LoadState') == 'not-found':
            return {}
        if res.returncode or not info.get('LoadState'):
            raise Error('无法确认官方 Agent 服务状态；未尝试安装。')
        return info

    def install_official(self, config, existing):
        p = self.paths
        cfg = p.config / 'official-agent.json'
        base = p.prefix / 'opt/komari'
        if existing:
            old = cfg.read_bytes()
            save(cfg, config)
            try:
                command(['systemctl', 'restart', OFFICIAL_UNIT])
            except Error:
                atomic(cfg, old)
                command(['systemctl', 'restart', OFFICIAL_UNIT], check=False)
                raise
        else:
            if base.is_symlink() or (base.exists() and (base.stat().st_uid != os.geteuid() or any(base.iterdir()))):
                raise Error('官方安装目录已有内容；为保留原节点身份，未覆盖。请先检查 /opt/komari。')
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / 'install.sh'
                command(['curl', '-q', '-fsSL', '--proto', '=https', '--max-time', '60', '-o', script,
                         'https://raw.githubusercontent.com/komari-monitor/komari-agent/' + INSTALLER_COMMIT + '/install.sh'], timeout=65)
                if hashlib.sha256(script.read_bytes()).hexdigest() != INSTALLER_SHA256:
                    raise Error('官方安装脚本校验失败。')
                base.mkdir(mode=0o700, exist_ok=True)
                os.chmod(base, 0o700)
                save(cfg, config)
                command(['env', 'SUDO_USER=root', 'sh', script, '--install-no-mirror',
                         '--install-version', VERSIONS['agent'], '--config', cfg], timeout=300)
        info = self.official_info()
        if info.get('ActiveState') != 'active' or info.get('UnitFileState') != 'enabled':
            raise Error('官方服务未确认已运行并开机自启；请用菜单 4 查看状态。')

    def agent_install(self):
        p = self.paths
        official = self.official_info()
        legacy = p.state('agent').exists()
        if not legacy and p.unit('agent').exists():
            raise Error('旧版服务仍存在但缺少管理记录，未安装第二个 Agent。')
        if legacy and official:
            raise Error('同时检测到官方与工具箱 Agent；请用菜单 4 核对状态，不会重装或停止任何一套。')
        cfg = p.config / 'official-agent.json'
        if official:
            unit = p.units / OFFICIAL_UNIT
            expected = 'ExecStart=/opt/komari/agent --config ' + str(cfg)
            if not cfg.is_file() or not unit.is_file() or expected not in unit.read_text().splitlines():
                print('已识别官方安装，无需重复安装；保留其配置及自动发现身份。')
                print(OFFICIAL_UNIT + '：' + self.agent_summary(official))
                return
        old = {}
        if legacy:
            self.state('agent')
            cfg = p.work('agent') / 'agent.json'
            print('保留旧版服务、配置和节点身份；本次只更新原 Agent。')
        if cfg.is_symlink():
            raise Error('Agent 配置不能为符号链接。')
        if cfg.is_file():
            old = json.loads(cfg.read_text())
        if not official and not self.active('agent'):
            self.headroom(64)
        print('在本台被监控 VPS 安装 Agent；监控机也需要安装一个 Agent 才会显示自身。')
        print('先在 Komari 后台添加服务器并复制 Agent Token，或使用后台的自动发现密钥。')
        raw = input('Komari 面板域名（例如 komari.example.com）' + (' [' + urllib.parse.urlsplit(old['endpoint']).hostname + ']' if old else '') + ': ').strip()
        value = raw or old.get('endpoint', '')
        url = endpoint(value if '://' in value else 'https://' + value)
        default_mode = '2' if old.get('auto_discovery_key') else '1'
        mode = input('认证方式：1 节点 Token，2 自动发现密钥 [' + default_mode + ']: ').strip() or default_mode
        if mode not in ('1', '2'):
            raise Error('请选择 1 或 2。')
        field = 'token' if mode == '1' else 'auto_discovery_key'
        token = getpass.getpass('Agent Token / 自动发现密钥（隐藏输入；同方式留空保留）: ').strip() or old.get(field, '')
        credential(token)
        interval = input('上报间隔秒数 [' + str(old.get('interval', 5)) + ']: ').strip() or str(old.get('interval', 5))
        if not re.fullmatch(r'[0-9]{1,3}', interval) or not 1 <= int(interval) <= 300:
            raise Error('上报间隔必须为 1～300 秒。')
        default_terminal = '1' if old.get('disable_web_ssh', True) else '2'
        print('网页终端通过 Agent 建立会话，不使用 sshd。开启后，面板也可用 Agent 的 root 身份执行命令（受 systemd 限制）。')
        terminal = input('面板远程访问：1 关闭，2 开启网页终端和命令执行 [' + default_terminal + ']: ').strip() or default_terminal
        if terminal not in ('1', '2'):
            raise Error('请选择 1 或 2。')
        print('网页终端和命令执行：' + ('开启' if terminal == '2' else '关闭') + '；HTTPS 证书验证开启。')
        if not confirm('安装 / 更新本机 Agent 配置并开机自启？'):
            return
        config = {'endpoint': url, field: token, 'interval': int(interval), 'disable_web_ssh': terminal == '1',
                  'disable_auto_update': True, 'ignore_unsafe_cert': False, 'max_retries': 3,
                  'reconnect_interval': 5, 'info_report_interval': 5, 'prefer_ip_version': '4'}
        if not legacy:
            self.install_official(config, bool(official))
            print('官方 Agent 已启动；请到面板确认节点在线。')
            return
        self.install_service('agent', {'endpoint': url}, {'agent.json': json.dumps(config) + '\n'})
        print('Agent 服务已启动；接入是否成功请在 Komari 后台确认，不以进程运行代替在线验证。')

    def access(self):
        p = self.paths
        if p.state('server').exists():
            cfg = self.state('server')
            print(f'本机地址：http://127.0.0.1:{cfg["port"]}/')
            print('Mac SSH 访问（替换 SSH 端口与监控机 IP）：')
            print(f'ssh -N -T -p SSH端口 -L 127.0.0.1:15774:127.0.0.1:{cfg["port"]} root@监控机IP')
            print('浏览器：http://127.0.0.1:15774/；首次若未初始化，访问 /install。')
            print('服务端完整数据和插件目录：' + str(p.work('server')))
        if p.state('tunnel').exists():
            print('配置的域名入口：' + self.state('tunnel')['endpoint'] + '/')
        if p.state('agent').exists():
            print('Agent 上报地址：' + self.state('agent')['endpoint'])
        print('接入顺序：监控机安装服务端并初始化 → 配置 HTTPS 域名 → 每台 VPS 安装 Agent。')
        print('节点改名、分组、删除及通知插件在 Komari 网页后台管理。')
        print('旧 Prometheus/Grafana 的 Telegram 备份不包含本模块；请为 Komari 单独备份。')

    def status(self):
        official = self.official_info()
        print('官方 Agent（komari-agent.service）：' + (self.agent_summary(official) if official else '未安装'))
        if official.get('ActiveState') == 'active' and self.active('agent'):
            print('注意：官方和工具箱 Agent 同时运行，请核对面板上报来源。')
        for role in VERSIONS:
            if not self.paths.state(role).exists():
                print(('旧版工具箱 Agent' if role == 'agent' else role) + '：未配置')
                continue
            cfg = self.state(role)
            result = command(['systemctl', 'show', self.paths.unit(role).name, '--property=ActiveState,SubState,MemoryCurrent,UnitFileState'], check=False)
            print(f'{role}（{cfg["version"]}）：' + '; '.join(result.stdout.strip().splitlines()))
        self.access()

    def logs(self, role, limit=60):
        self.state(role)
        result = command(['journalctl', '-u', self.paths.unit(role).name, '-n', str(limit), '--no-pager'], check=False)
        secrets = []
        path = self.paths.work('agent') / 'agent.json'
        if path.is_file() and not path.is_symlink():
            cfg = json.loads(path.read_text())
            secrets += [cfg.get('token', ''), cfg.get('auto_discovery_key', '')]
        path = self.paths.work('tunnel') / 'token'
        if path.is_file() and not path.is_symlink():
            secrets.append(path.read_text().strip())
        output = result.stdout
        for secret in secrets:
            if secret:
                output = output.replace(secret, '[REDACTED]')
        print(redact(output))

    def service(self, role, action):
        self.state(role)
        p = self.paths
        if action == 'uninstall':
            if not confirm('停止并移除本模块 ' + role + ' 服务？保留配置、数据库和二进制。'):
                return
            command(['systemctl', 'disable', '--now', p.unit(role).name], check=False)
            if self.active(role):
                raise Error('服务仍在运行，未移除 unit 文件。')
            p.unit(role).unlink(missing_ok=True)
            command(['systemctl', 'daemon-reload'])
            print('已移除服务，配置与数据保留；Agent 的后台服务器记录需在 Komari 网页中删除。')
            return
        if not p.unit(role).exists():
            raise Error('服务已卸载，请重新执行该角色的安装恢复。')
        if action in ('start', 'restart') and not self.active(role):
            if role == 'server':
                self.free_port(self.state(role)['port'])
            elif role == 'tunnel':
                self.free_port(20246)
        if action == 'start':
            command(['systemctl', 'enable', '--now', p.unit(role).name])
        elif action == 'stop':
            command(['systemctl', 'disable', '--now', p.unit(role).name])
        else:
            command(['systemctl', 'restart', p.unit(role).name])
        if role == 'server' and action != 'stop':
            self.wait_server(self.state(role)['port'])
        print('已执行 ' + role + ' ' + action)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['setup-install', 'server-install', 'agent-install', 'tunnel-install', 'access', 'status', 'logs', 'start', 'stop', 'restart', 'uninstall'])
    parser.add_argument('role', nargs='?', choices=list(VERSIONS), default='server')
    args = parser.parse_args()
    manager = Komari()
    if args.action in ('access', 'status', 'logs'):
        if args.action != 'status' or manager.paths.base.exists():
            manager.owned()
        if args.action == 'logs':
            manager.logs(args.role)
        else:
            getattr(manager, args.action)()
    else:
        with manager.lock():
            if args.action.endswith('-install'):
                getattr(manager, args.action.replace('-', '_'))()
            else:
                manager.service(args.role, args.action)


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\n已取消；已完成的安装和数据保留。', file=sys.stderr)
        sys.exit(1)
    except (Error, OSError, ValueError, KeyError) as e:
        # Avoid relaying raw JSON/config data in parse errors.
        print('错误：' + (str(e) if isinstance(e, Error) else 'Komari 操作失败，请检查配置、权限和日志。'), file=sys.stderr)
        sys.exit(1)
