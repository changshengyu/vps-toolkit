"""Dedicated Caddy HTTPS site, native systemd, TCP 443 only. Python stdlib."""
import argparse
import fcntl
import grp
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time

VERSION = '2.11.9'
ARCHIVE_SHA256 = {
    'amd64': '5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb',
    'arm64': '5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165',
}
SERVICE = 'vps-toolkit-sni-site'
USER = 'vps-sni-web'
MARKER = 'vps-toolkit-sni-site-v1'
ROOT = Path(__file__).resolve().parents[2]


class Error(Exception):
    pass


def command(args, timeout=30):
    try:
        result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Error('命令无法运行或超时：' + str(args[0])) from None
    if result.returncode:
        detail = ' '.join((result.stderr or result.stdout).splitlines()[-3:])
        detail = ''.join(c if c.isprintable() else ' ' for c in detail)[:500]
        raise Error(f'命令 {args[0]} 失败（退出码 {result.returncode}）' + ('：' + detail if detail else '；请检查站点日志或依赖。'))
    return result.stdout


def domain_name(value):
    name = value.strip().lower().rstrip('.')
    parts = name.split('.')
    if (len(name) > 253 or len(parts) < 2 or not re.search('[a-z]', parts[-1])
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in parts)
            or parts[-1] in ('test', 'invalid', 'localhost', 'local')):
        raise Error('请输入公开域名，不含 https://、端口、路径或通配符。')
    return name


def atomic(path, value, mode=0o644):
    if path.is_symlink():
        raise Error('拒绝覆盖符号链接：' + str(path))
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value if isinstance(value, bytes) else value.encode())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def json_save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class Paths:
    def __init__(self, prefix=Path('/')):
        prefix = Path(prefix)
        self.config = prefix / 'etc' / SERVICE
        self.data = prefix / 'var/lib' / SERVICE
        self.web = prefix / 'var/www' / SERVICE
        self.binary_dir = prefix / 'usr/local/lib' / SERVICE
        self.binary = self.binary_dir / 'caddy'
        self.runtime = prefix / 'run' / SERVICE
        self.unit = prefix / 'etc/systemd/system' / (SERVICE + '.service')
        self.state = self.config / 'state.json'
        self.document = self.config / 'caddy.json'

    def directories(self):
        return (self.config, self.data, self.web, self.binary_dir)


def configuration(domain, paths, email=''):
    domain = domain_name(domain)
    issuer = {'module': 'acme', 'ca': 'https://acme-v02.api.letsencrypt.org/directory',
              'challenges': {'http': {'disabled': True}}}
    if email:
        issuer['email'] = email
    return {
        'admin': {'listen': 'unix/' + str(paths.runtime / 'admin.sock')},
        'apps': {
            'http': {'servers': {'site': {
                'listen': [':443'], 'protocols': ['h1', 'h2'],
                'automatic_https': {'disable_redirects': True},
                'tls_connection_policies': [{}],
                'routes': [{'match': [{'host': [domain], 'path': ['/', '/index.html']}], 'handle': [
                    {'handler': 'file_server', 'root': str(paths.web), 'index_names': ['index.html']}], 'terminal': True},
                    {'handle': [{'handler': 'static_response', 'status_code': 404}], 'terminal': True}],
            }}},
            'tls': {'automation': {'policies': [{'subjects': [domain], 'issuers': [issuer], 'key_type': 'p256'}]}},
        },
    }


def service_unit(paths):
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return f'''# {MARKER}
[Unit]
Description=ISP HTTPS static site / Reality SNI target
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
User={USER}
Group={USER}
ExecStart={quote(paths.binary)} run --config {quote(paths.document)}
ExecReload={quote(paths.binary)} reload --config {quote(paths.document)} --address {quote('unix/' + str(paths.runtime / 'admin.sock'))}
Environment="XDG_DATA_HOME={paths.data}"
Environment="XDG_CONFIG_HOME={paths.data}/config"
Environment=GOMEMLIMIT=96MiB
StateDirectory={SERVICE}
StateDirectoryMode=0700
RuntimeDirectory={SERVICE}
RuntimeDirectoryMode=0700
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={quote(paths.data)} {quote(paths.runtime)}
Restart=on-failure
RestartSec=5s
TimeoutStopSec=30s
MemoryMax=192M
CPUQuota=35%
UMask=0077
[Install]
WantedBy=multi-user.target
'''


def render_page(title):
    title = title.strip() or '云间手记'
    if len(title) > 80 or any(ord(c) < 32 for c in title):
        raise Error('站点名称限 80 个可见字符。')
    return (ROOT / 'templates/sni-site/index.html').read_text().replace('__SITE_TITLE__', html.escape(title))


def dns_preflight(domain):
    # Bound getaddrinfo in its own process, including a stalled system resolver.
    code = 'import socket,json,sys;print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))'
    try:
        resolved = json.loads(command([sys.executable, '-c', code, domain], timeout=10))
    except (ValueError, TypeError):
        raise Error('域名解析返回格式异常，请检查本机 DNS 解析器。') from None
    if not resolved or any(not ipaddress.ip_address(ip).is_global for ip in resolved):
        raise Error('域名必须解析到本机公开 A/AAAA 地址，不能使用本地或保留地址。')
    local = set()
    try:
        interfaces = json.loads(command(['ip', '-j', 'addr', 'show', 'scope', 'global']))
    except (ValueError, TypeError):
        raise Error('无法解析 ip -j 接口地址输出，请检查 iproute2。') from None
    if not isinstance(interfaces, list):
        raise Error('ip -j 接口地址输出应为数组，请检查 iproute2。')
    for item in interfaces:
        if not isinstance(item, dict) or not isinstance(item.get('addr_info'), list):
            continue
        for entry in item['addr_info']:
            # Some iproute2 versions retain {} placeholders after scope filtering.
            address = entry.get('local') if isinstance(entry, dict) else None
            if not isinstance(address, str) or not address:
                continue
            try:
                local.add(str(ipaddress.ip_address(address)))
            except ValueError:
                continue
    if not local:
        raise Error('ip -j 未返回有效的本机接口地址，请检查网卡地址或 iproute2 输出。')
    missing = set(resolved) - local
    if missing:
        # Public egress lookup is needed for NAT or otherwise non-local DNS addresses.
        for family, url in [('-4', 'https://api4.ipify.org'), ('-6', 'https://api6.ipify.org')]:
            if not any(ipaddress.ip_address(ip).version == (4 if family == '-4' else 6) for ip in missing):
                continue
            try:
                value = command(['curl', '-q', '--noproxy', '*', '-fsS', family, '--connect-timeout', '3',
                                 '--max-time', '5', url], timeout=7).strip()
                local.add(str(ipaddress.ip_address(value)))
            except (Error, ValueError):
                pass
    print('域名解析：' + '、'.join(resolved), flush=True)
    if set(resolved) - local:
        raise Error('部分 A/AAAA 记录不属于本机。请改为直连灰云解析，删除过期 AAAA，或检查 NAT 公网出口。')
    return resolved


def port_free():
    listeners = command(['ss', '-lntHp', 'sport = :443']).strip()
    if listeners:
        print(listeners, file=sys.stderr)
        raise Error('TCP 443 已占用；未停止或修改现有代理 / Web 服务。此模块需要独占本机 TCP 443；已有 HTTPS 网站可继续作为目标，无需重复部署。')


def download_binary(path, machine=None):
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine or platform.machine())
    if not arch:
        raise Error('仅支持 Linux x86_64 / ARM64。')
    with tempfile.TemporaryDirectory(prefix='vt-sni-download-') as temp:
        archive = Path(temp) / 'caddy.tar.gz'
        url = f'https://github.com/caddyserver/caddy/releases/download/v{VERSION}/caddy_{VERSION}_linux_{arch}.tar.gz'
        print(f'下载官方 Caddy {VERSION} ({arch}) 并校验 SHA-256…', flush=True)
        command(['curl', '-q', '-fsSL', '--noproxy', '*', '--proto', '=https', '--connect-timeout', '8',
                 '--max-time', '120', '--output', archive, url], timeout=125)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != ARCHIVE_SHA256[arch]:
            raise Error('Caddy 发布文件校验失败，未安装。')
        with tarfile.open(archive, 'r:gz') as tar:
            members = [m for m in tar.getmembers() if m.name == 'caddy']
            if len(members) != 1 or not members[0].isfile() or not 1 <= members[0].size <= 150 * 1024 ** 2:
                raise Error('Caddy 发布包结构异常。')
            with tar.extractfile(members[0]) as source:
                atomic(path, source.read(), 0o755)


def probe(domain, port=443, address='127.0.0.1', context=None):
    def connect(protocols):
        ctx = context or ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.set_alpn_protocols(protocols)
        return ctx.wrap_socket(socket.create_connection((address, port), timeout=5), server_hostname=domain)
    with connect(['h2', 'http/1.1']) as stream:
        if stream.version() != 'TLSv1.3' or stream.selected_alpn_protocol() != 'h2':
            raise Error('目标没有同时提供 TLS 1.3 和 HTTP/2，尚未通过 Reality 目标检查。')
        cert = stream.getpeercert()
    with connect(['http/1.1']) as stream:
        stream.sendall(f'GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n'.encode())
        response = stream.recv(4096).split(b'\r\n', 1)[0]
        if not response.startswith(b'HTTP/1.1 200 '):
            raise Error('首页未返回 HTTP 200；请检查首页文件和域名路由。')
    return cert.get('notAfter', '')


class Site:
    def __init__(self, paths=None):
        self.paths = paths or Paths()

    def state(self):
        p = self.paths
        if p.state.is_symlink() or not p.state.is_file():
            raise Error('尚未部署本模块站点。')
        value = json.loads(p.state.read_text())
        if value.get('managed_by') != MARKER or domain_name(value['domain']) != value['domain']:
            raise Error('站点配置记录无效。')
        return value

    def owned_paths(self):
        p = self.paths
        for directory in p.directories():
            marker = directory / '.vps-toolkit-owned'
            if directory.is_symlink() or (directory.exists() and (not marker.is_file() or marker.read_text() != MARKER)):
                raise Error('发现不属于本模块的目录，未覆盖：' + str(directory))
            if directory.exists():
                info = directory.stat()
                allowed = {os.geteuid()}
                if directory == p.data:
                    try:
                        allowed.add(pwd.getpwnam(USER).pw_uid)
                    except KeyError:
                        pass
                if info.st_uid not in allowed or info.st_mode & 0o022:
                    raise Error('站点目录所有者或写入权限不安全：' + str(directory))
        for path in (p.state, p.document, p.binary, p.unit):
            if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022)):
                raise Error('站点管理文件所有者或写入权限不安全：' + str(path))
        if p.unit.is_symlink() or (p.unit.exists() and MARKER not in p.unit.read_text()):
            raise Error('同名系统服务不属于本模块，未覆盖。')

    def provision_user(self):
        p = self.paths
        try:
            user = pwd.getpwnam(USER)
        except KeyError:
            command(['useradd', '--system', '--user-group', '--home-dir', p.data,
                     '--no-create-home', '--shell', '/usr/sbin/nologin', USER])
            user = pwd.getpwnam(USER)
        if (user.pw_uid == 0 or user.pw_dir != str(p.data)
                or grp.getgrnam(USER).gr_gid != user.pw_gid or user.pw_gid == 0):
            raise Error('专用服务账号已被其他用途占用。')
        os.chown(p.data, user.pw_uid, user.pw_gid)
        os.chmod(p.data, 0o700)

    def write_unit(self):
        p = self.paths
        p.unit.parent.mkdir(parents=True, exist_ok=True)
        atomic(p.unit, service_unit(p))
        command(['systemctl', 'daemon-reload'])

    def provision_files(self, value):
        p = self.paths
        self.provision_user()
        if not p.binary.exists():
            download_binary(p.binary)
        if not p.document.exists():
            json_save(p.document, configuration(value['domain'], p, value['email']))
        if not (p.web / 'index.html').exists():
            atomic(p.web / 'index.html', render_page(value['title']))
        with tempfile.TemporaryDirectory(prefix='vt-sni-validate-') as temp:
            env = dict(os.environ, XDG_DATA_HOME=temp, XDG_CONFIG_HOME=temp)
            result = subprocess.run([str(p.binary), 'validate', '--config', str(p.document)],
                                    capture_output=True, text=True, timeout=30, env=env)
            if result.returncode:
                detail = ' '.join(result.stderr.splitlines()[-2:])[:500]
                raise Error('Caddy 配置验证失败，尚未启动：' + detail)
        self.write_unit()

    def wait_ready(self, domain):
        last = ''
        deadline, notice = time.monotonic() + 150, 0
        while time.monotonic() < deadline:
            try:
                expiry = probe(domain)
                print('本机 HTTPS / TLS 1.3 / HTTP/2 检查通过；证书到期（GMT）：' + expiry)
                return
            except (OSError, Error, ValueError) as exc:
                last = str(exc)
                if time.monotonic() >= notice:
                    print('等待公开证书签发（TLS-ALPN 验证仅用 TCP 443）…', flush=True)
                    notice = time.monotonic() + 15
                time.sleep(3)
        raise Error('服务配置已保留，但公开证书或首页未就绪：' + last + '。请检查灰云 A/AAAA、入站 TCP 443 和日志；Caddy 会继续尝试申请。')

    def install(self):
        p = self.paths
        self.owned_paths()
        if p.state.exists():
            current = self.state()
            print('已有站点，保留域名、首页和证书：' + current['domain'])
            self.provision_files(current)
            self.manage('start')
            self.wait_ready(current['domain'])
            self.access()
            return
        if shutil.which('ss') is None or shutil.which('ip') is None:
            raise Error('缺少 ss / ip，请先安装 iproute2。')
        try:
            pwd.getpwnam(USER)
        except KeyError:
            pass
        else:
            raise Error('专用服务账号已存在但没有托管记录，未接管。')
        try:
            grp.getgrnam(USER)
        except KeyError:
            pass
        else:
            raise Error('专用服务组已存在但没有托管记录，未接管。')
        domain = domain_name(input('HTTPS 站点域名（灰云直连，例如 myweb.example.com）: '))
        email = input('证书联系邮箱（可留空）: ').strip()
        if email and (not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254):
            raise Error('邮箱格式无效。')
        title = input('静态页面名称 [云间手记]: ').strip() or '云间手记'
        page = render_page(title)
        port_free()
        print('检查域名 A/AAAA 是否指向本机…', flush=True)
        dns_preflight(domain)
        print('部署计划：独立 Caddy + systemd，自启、自动续签；仅 TCP 443，TLS 1.3 / HTTP/2。')
        print('现有 ISP/SS、3x-ui 和路由配置保留。不会使用 TCP 80、UDP 443 或开放管理 API。')
        print('请在云安全组及系统防火墙放行 TCP 443（签发和续期也需要）；本模块不更改防火墙策略。')
        if input('部署此 HTTPS 静态站点？[y/N]: ').strip().lower() not in ('y', 'yes'):
            print('已取消。')
            return
        for directory in p.directories():
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            atomic(directory / '.vps-toolkit-owned', MARKER)
        value = {'managed_by': MARKER, 'domain': domain, 'email': email, 'title': title, 'version': VERSION}
        json_save(p.state, value)
        atomic(p.web / 'index.html', page)
        self.provision_files(value)
        self.manage('start')
        self.wait_ready(domain)
        self.access()

    def access(self):
        value = self.state()
        domain = value['domain']
        print(f'网页：https://{domain}/')
        print(f'其他 VPS 的 Reality SNI / serverNames：{domain}')
        print(f'其他 VPS 的 Reality target / dest：{domain}:443')
        print('客户端节点地址仍填入口 VPS 的地址。请在其他 VPS 的 3x-ui 中填写以上值。')
        print('证书由本服务自动续签；TCP 443 和直连 A/AAAA 必须持续可用。')

    def manage(self, action):
        self.state()
        self.owned_paths()
        if action in ('start', 'restart'):
            active = subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE]).returncode == 0
            if not active:
                port_free()
            command(['systemctl', 'enable', SERVICE])
            command(['systemctl', action, SERVICE], timeout=40)
        elif action in ('stop', 'uninstall'):
            command(['systemctl', 'disable', '--now', SERVICE], timeout=40)
            if action == 'uninstall':
                self.paths.unit.unlink(missing_ok=True)
                command(['systemctl', 'daemon-reload'])
                print('已移除本模块服务；站点、证书和页面保留，重新部署可恢复。')

    def check(self):
        value = self.state()
        status = subprocess.run(['systemctl', 'status', SERVICE, '--no-pager', '--lines=0'])
        if status.returncode:
            raise Error('本站点服务未运行，请在服务管理中启动。')
        expiry = probe(value['domain'])
        print('本机首页 HTTP 200、可信证书、TLS 1.3 和 HTTP/2 均通过；到期 GMT：' + expiry)
        print('还需从其他 VPS 检查公网可达性，以及云安全组 TCP 443。')
        self.access()

    def import_page(self):
        self.state()
        source = Path(input('新首页 HTML 文件绝对路径（自包含单文件）: ').strip())
        if not source.is_absolute() or source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 5 * 1024 ** 2:
            raise Error('请输入不超过 5 MiB 的普通 HTML 文件绝对路径。')
        value = source.read_text(encoding='utf-8')
        if '<html' not in value.lower() or '</html>' not in value.lower():
            raise Error('文件不是完整 HTML，未公开上传。')
        target = self.paths.web / 'index.html'
        if target.is_symlink():
            raise Error('首页是符号链接，未覆盖。')
        backup = self.paths.config / ('index-backup-' + str(time.time_ns()) + '.html')
        atomic(backup, target.read_bytes(), 0o600)
        atomic(target, value)
        print('首页已替换，无需重启；原首页备份：' + str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'access', 'check', 'page', 'logs', 'start', 'stop', 'restart', 'uninstall'])
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise Error('请使用 root 运行。')
        site = Site()
        if args.action in ('access', 'check', 'logs'):
            if args.action == 'logs':
                site.state()
                subprocess.run(['journalctl', '-u', SERVICE, '-n', '80', '--no-pager'])
            else:
                getattr(site, args.action)()
        else:
            site.owned_paths()
            # Stable lock lives outside directories removed/recreated by installers.
            with open('/run/' + SERVICE + '.lock', 'a') as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Error('已有站点部署或管理任务在运行。') from None
                if args.action in ('install', 'page'):
                    site.install() if args.action == 'install' else site.import_page()
                else:
                    site.manage(args.action)
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.TimeoutExpired):
        exc = sys.exc_info()[1]
        print('错误：' + (str(exc) if isinstance(exc, (Error, ssl.SSLError)) else '站点操作失败，请检查配置、权限、网络和日志。'), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
