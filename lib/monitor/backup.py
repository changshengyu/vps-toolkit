"""Encrypted, off-host monitoring recovery sets. No third-party Python packages."""
import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import urllib.request

UNIT = 'vps-toolkit-monitor-backup'
CHUNK = 45 * 1024 * 1024
MAX_EXPANDED = 4 * 1024 ** 3
FORMAT = 'vps-toolkit-monitor-backup-v1'
SAFE_FILE = re.compile(r'backup-[0-9TZ-]+-[a-f0-9]{8}\.tar\.gz\.age(?:\.part-[0-9]{4})?')


class Error(Exception):
    pass


def run(args, **kwargs):
    try:
        return subprocess.run([str(a) for a in args], check=True, capture_output=True,
                              timeout=kwargs.pop('timeout', 180), **kwargs).stdout
    except (OSError, subprocess.SubprocessError):
        # subprocess exceptions can contain command arguments and secret HTTP URLs.
        raise Error('本地命令执行失败：' + str(args[0]) + '；请检查依赖和服务状态。') from None


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Error('配置文件不能是符号链接。')
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temp, path)


def read_private(path):
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Error('私有文件缺失或权限过宽：' + str(path))
    return json.loads(path.read_text())


def validate_config(config):
    if config.get('schema') != FORMAT:
        raise Error('备份配置格式无效。')
    if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,120}', config.get('token', '')):
        raise Error('Bot Token 格式无效。')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', str(config.get('chat_id', ''))):
        raise Error('请输入个人聊天的数字 Chat ID。')
    if not re.fullmatch(r'age1[0-9a-z]{58}', config.get('recipient', '')):
        raise Error('age 加密公钥无效。')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', config.get('time', '')):
        raise Error('备份时间应为 HH:MM。')
    return config


class Telegram:
    """TLS streaming upload; Token never enters subprocess argv, URLs in logs or redirects."""
    def __init__(self, config):
        self.token = config['token']
        self.chat = str(config['chat_id'])

    def request(self, method, fields=None, document=None):
        boundary = 'vtk' + uuid.uuid4().hex
        fields = fields or {}
        prefix = b''
        for name, value in fields.items():
            prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
        if document:
            prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{document.name}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode()
        ending = (f'\r\n--{boundary}--\r\n').encode()
        for attempt in range(3):
            connection = http.client.HTTPSConnection('api.telegram.org', timeout=120)
            retry_after = 5 * (attempt + 1)
            try:
                connection.putrequest('POST', '/bot' + self.token + '/' + method)
                connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
                connection.putheader('Content-Length', str(len(prefix) + len(ending) + (document.stat().st_size if document else 0)))
                connection.endheaders()
                connection.send(prefix)
                if document:
                    with document.open('rb') as stream:
                        while True:
                            block = stream.read(128 * 1024)
                            if not block:
                                break
                            connection.send(block)
                connection.send(ending)
                response = connection.getresponse()
                value = json.loads(response.read(65536))
                if response.status == 200 and value.get('ok'):
                    return value['result']
                if response.status not in (429, 500, 502, 503, 504):
                    raise Error(f'Telegram API HTTP {response.status}；请检查 Bot Token、Chat ID、是否已 /start 以及文件限制。')
                retry_after = min(60, max(1, int(value.get('parameters', {}).get('retry_after', retry_after))))
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            if attempt < 2:
                time.sleep(retry_after)
        raise Error('Telegram 连接或上传失败，已重试三次；本地加密文件保留，未标记为发送成功。')

    def verify(self):
        self.request('getMe')
        chat = self.request('getChat', {'chat_id': self.chat})
        if chat.get('type') != 'private':
            raise Error('备份仅支持发送到个人聊天，请填写你与 Bot 对话的数字 Chat ID。')

    def send(self, path, caption):
        return self.request('sendDocument', {'chat_id': self.chat, 'caption': caption}, path)


class Docker:
    def __init__(self, directory):
        self.directory = directory

    def compose(self, *args):
        files = ['compose.yaml', 'compose.grafana.yaml', 'compose.tunnel.yaml', 'compose.restore.yaml']
        cmd = ['docker', 'compose', '--project-directory', str(self.directory)]
        for name in files:
            if (self.directory / name).is_file():
                cmd += ['-f', str(self.directory / name)]
        return run(cmd + list(args))

    def inspect(self, service):
        name = self.compose('ps', '--all', '--quiet', service).decode().strip()
        if not name or '\n' in name:
            raise Error('未找到唯一的监控容器：' + service)
        return json.loads(run(['docker', 'inspect', name]))[0]

    def grafana(self):
        data = self.inspect('grafana')
        for item in data['Config'].get('Env', []):
            if item.startswith('GF_DATABASE_TYPE=') and item != 'GF_DATABASE_TYPE=sqlite3':
                raise Error('当前备份仅支持工具箱默认的 SQLite 数据库。')
            if item.startswith('GF_DATABASE_PATH=') and item != 'GF_DATABASE_PATH=/var/lib/grafana/grafana.db':
                raise Error('自定义数据库路径不支持自动备份。')
        mounts = [m for m in data['Mounts'] if m['Destination'] == '/var/lib/grafana']
        if len(mounts) != 1 or not (Path(mounts[0]['Source']) / 'grafana.db').is_file():
            raise Error('未找到 Grafana 默认数据目录；自定义数据库路径不支持自动备份。')
        return Path(mounts[0]['Source']), data


def files_under(directory, excluded=(), include_dirs=False):
    if directory.is_symlink():
        raise Error('备份源不能是符号链接。')
    if include_dirs:
        yield directory
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in list(dirs):
            path = Path(root) / name
            if path.relative_to(directory).as_posix() in excluded:
                dirs.remove(name)
            elif path.is_symlink():
                raise Error('备份源含符号链接，未跟随：' + str(path))
            elif include_dirs:
                yield path
        for name in files:
            path = Path(root) / name
            if path.relative_to(directory).as_posix() in excluded:
                continue
            if path.is_symlink() or not path.is_file():
                raise Error('备份源含非普通文件，已停止：' + str(path))
            yield path


def sqlite_snapshot(source, target):
    source_uri = source.resolve().as_uri() + '?mode=ro'
    deadline = time.monotonic() + 120
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise Error('Grafana 数据库在线备份超过 120 秒，已中止。')
    with contextlib.closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as src:
        with contextlib.closing(sqlite3.connect(str(target))) as dst:
            src.backup(dst, pages=256, progress=progress)
            if dst.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise Error('Grafana 数据库一致性检查未通过。')
    os.chmod(target, 0o600)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(128 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def split_encrypted(source, limit=CHUNK):
    if source.stat().st_size <= limit:
        return [source]
    parts = []
    with source.open('rb') as stream:
        index = 1
        while stream.peek(1):
            if index > 9999:
                raise Error('分卷数量超出上限。')
            path = source.with_name(source.name + f'.part-{index:04d}')
            with path.open('xb') as target:
                remaining = limit
                while remaining:
                    block = stream.read(min(128 * 1024, remaining))
                    if not block:
                        break
                    target.write(block)
                    remaining -= len(block)
            parts.append(path)
            index += 1
    source.unlink()
    return parts


class Backup:
    def __init__(self, monitor, config, storage, systemd=Path('/etc/systemd/system')):
        self.monitor, self.config, self.storage, self.systemd = map(Path, (monitor, config, storage, systemd))
        self.config_file = self.config / 'config.json'

    def configuration(self):
        return validate_config(read_private(self.config_file))

    def install_timer(self, config, enable=True):
        self.systemd.mkdir(parents=True, exist_ok=True)
        runner = self.config / 'backup.py'
        if Path(__file__).resolve() != runner.resolve():
            shutil.copyfile(__file__, runner)
        os.chmod(runner, 0o600)
        # All paths are fixed by the root-owned CLI, and spaces are escaped for systemd.
        def quote(path):
            return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        args = ' '.join(quote(p) for p in (runner, '--monitor', self.monitor, '--config', self.config,
                                          '--storage', self.storage, 'run'))
        service = f'''[Unit]
Description=Encrypted monitoring backup to Telegram
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {args}
UMask=0077
Nice=15
IOSchedulingClass=idle
CPUQuota=35%
MemoryMax=192M
Environment=GOMEMLIMIT=64MiB
TimeoutStartSec=30min
'''
        timer = f'''[Unit]
Description=Daily monitoring backup (Asia/Shanghai)
[Timer]
OnCalendar=*-*-* {config['time']}:00 Asia/Shanghai
RandomizedDelaySec=5min
Persistent=true
[Install]
WantedBy=timers.target
'''
        for suffix, text in [('service', service), ('timer', timer)]:
            path = self.systemd / (UNIT + '.' + suffix)
            if path.is_symlink():
                raise Error('定时单元不能是符号链接。')
            path.write_text(text)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable' if enable else 'disable', '--now', UNIT + '.timer'])

    def setup(self):
        if not (self.monitor / '.vps-toolkit-managed').is_file():
            raise Error('请先安装监控机。')
        self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config, 0o700)
        existing = self.configuration() if self.config_file.exists() else None
        print('备份发送到你与 Bot 的个人聊天。Token 仅保存在本机 root 私有配置，不写入日志。')
        token = getpass.getpass('Bot Token（隐藏输入' + ('，留空保留' if existing else '') + '）: ') or (existing or {}).get('token', '')
        chat = input('个人聊天数字 Chat ID' + (f" [{existing['chat_id']}]" if existing else '') + ': ').strip() or (existing or {}).get('chat_id', '')
        clock = input('每日备份时间（北京时间，默认 04:00）: ').strip() or '04:00'
        key = self.config / 'recovery-key.txt'
        if existing:
            recipient = existing['recipient']
        else:
            if not key.exists():
                run(['age-keygen', '-o', key])
            if key.is_symlink() or stat.S_IMODE(key.stat().st_mode) & 0o077:
                raise Error('恢复密钥权限无效。')
            recipient = run(['age-keygen', '-y', key]).decode().strip()
        config = validate_config({'schema': FORMAT, 'token': token, 'chat_id': str(chat), 'recipient': recipient, 'time': clock})
        print('验证 Bot 和个人聊天（不会发送备份）…', flush=True)
        Telegram(config).verify()
        print('请把恢复密钥保存到 Mac 或密码管理器：' + str(key) if key.exists() else '请确认原来的恢复密钥仍保存在 Mac 或密码管理器中。')
        print('不要只保存在此 VPS；备份包不包含解密私钥。保存后可删除 VPS 上的 recovery-key.txt，定时备份只需公钥。')
        if input('已另存解密密钥请输入 SAVED（其他输入取消）: ').strip() != 'SAVED':
            raise Error('未启用自动备份；恢复密钥保留在上述路径，可另存后重新配置。')
        save(self.config_file, config)
        self.install_timer(config)
        print('已启用每日 ' + clock + '（北京时间，随机延迟最多 5 分钟）备份。重启后仍有效；现在可选择“立即备份并发送”。')

    def create(self, config, grafana_dir, container):
        if (self.monitor / '.restore-incomplete').exists():
            raise Error('恢复尚未完成，不能生成新的恢复集。')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name = 'backup-' + stamp + '-' + uuid.uuid4().hex[:8]
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.storage / name
        target.mkdir(mode=0o700)
        (target / '.owned').write_text(FORMAT)
        source_files = list(files_under(self.monitor, excluded=('.deploy.lock', 'packages')))
        data_files = list(files_under(grafana_dir, excluded=('grafana.db', 'grafana.db-wal', 'grafana.db-shm')))
        size = sum(p.stat().st_size for p in source_files + data_files) + (grafana_dir / 'grafana.db').stat().st_size
        if size > MAX_EXPANDED or shutil.disk_usage(self.storage).free < size * 3 + 128 * 1024 ** 2:
            raise Error('备份数据超过 4 GiB 或临时磁盘空间不足（需约源数据三倍 + 128 MiB）。')
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=self.storage) as temp:
            snapshot = Path(temp) / 'grafana.db'
            print('正在生成 Grafana 在线数据库快照…', flush=True)
            sqlite_snapshot(grafana_dir / 'grafana.db', snapshot)
            env = dict(item.split('=', 1) for item in container['Config'].get('Env', []) if '=' in item)
            security = {k: env[k] for k in ('GF_SECURITY_SECRET_KEY', 'GF_SECURITY_ENCRYPTION_PROVIDER') if k in env}
            metadata = {'schema': FORMAT, 'monitor_dir': str(self.monitor), 'created_utc': stamp,
                        'grafana_image': container['Config']['Image'], 'grafana_security': security,
                        'project': container['Config'].get('Labels', {}).get('com.docker.compose.project', 'vps-toolkit-monitor'),
                        'prometheus_history': False, 'uid': (grafana_dir / 'grafana.db').stat().st_uid,
                        'gid': (grafana_dir / 'grafana.db').stat().st_gid}
            meta_path = Path(temp) / 'manifest.json'
            save(meta_path, metadata)
            archive = target / (name + '.tar.gz.age')
            print('正在压缩并加密配置和 Grafana 数据…', flush=True)
            process = subprocess.Popen(['age', '-r', config['recipient'], '-o', str(archive)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with gzip.GzipFile(fileobj=process.stdin, mode='wb', compresslevel=3) as zipped:
                    with tarfile.open(fileobj=zipped, mode='w|') as tar:
                        tar.dereference = True
                        tar.add(meta_path, arcname='manifest.json', recursive=False)
                        for path in files_under(self.monitor, excluded=('.deploy.lock', 'packages'), include_dirs=True):
                            relative = path.relative_to(self.monitor).as_posix()
                            tar.add(path, arcname='monitor' + ('/' + relative if relative != '.' else ''), recursive=False)
                        for path in files_under(grafana_dir, excluded=('grafana.db', 'grafana.db-wal', 'grafana.db-shm'), include_dirs=True):
                            relative = path.relative_to(grafana_dir).as_posix()
                            tar.add(path, arcname='grafana' + ('/' + relative if relative != '.' else ''), recursive=False)
                        info = tar.gettarinfo(str(snapshot), arcname='grafana/grafana.db')
                        info.uid, info.gid = metadata['uid'], metadata['gid']
                        with snapshot.open('rb') as stream:
                            tar.addfile(info, stream)
                        tar.add(self.config_file, arcname='backup-config.json', recursive=False)
                        for filename in ('known_hosts', 'known_hosts2'):
                            path = Path('/root/.ssh') / filename
                            if path.is_file() and not path.is_symlink():
                                tar.add(path, arcname='ssh/' + filename, recursive=False)
                process.stdin.close()
                if process.wait(timeout=120) != 0:
                    raise Error('age 加密失败。')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            parts = split_encrypted(archive)
            manifest = {'schema': FORMAT, 'created_utc': stamp, 'parts': [
                {'name': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)} for path in parts]}
            manifest_path = target / (name + '.manifest.json')
            save(manifest_path, manifest)
            return manifest_path, parts

    def execute(self, docker=None, telegram=None):
        config = self.configuration()
        docker = docker or Docker(self.monitor)
        telegram = telegram or Telegram(config)
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.monitor / '.deploy.lock').open('a') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error('正在添加节点、续期或备份，请稍后重试。') from None
            save(self.config / 'status.json', {'ok': False, 'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                                              'message': '任务已开始，尚未全部发送成功；若进程退出请检查日志。'})
            try:
                grafana_dir, container = docker.grafana()
                manifest, parts = self.create(config, grafana_dir, container)
                # Release deployment lock before slow network uploads.
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                for index, path in enumerate(parts, 1):
                    print(f'正在发送加密备份 {index}/{len(parts)}…', flush=True)
                    telegram.send(path, f'监控备份 {manifest.stem}：{index}/{len(parts)}。恢复时下载全部分卷和清单，解密密钥另存。')
                telegram.send(manifest, '备份发送完成：请与同名全部加密分卷一起保存此校验清单。Prometheus 历史指标未包含。')
                save(self.config / 'status.json', {'ok': True, 'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                                                  'manifest': str(manifest), 'parts': len(parts)})
                # Retain seven complete/local sets, only after a successful upload.
                sets = sorted(p for p in self.storage.glob('backup-*') if p.is_dir() and not p.is_symlink()
                              and (p / '.owned').is_file() and not (p / '.owned').is_symlink()
                              and (p / '.owned').read_text() == FORMAT)
                for old in sets[:-7]:
                    shutil.rmtree(old)
                print('备份已全部发送并验证 API 成功。本地保留最近 7 组。')
            except (Exception, KeyboardInterrupt) as exc:
                save(self.config / 'status.json', {'ok': False, 'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                                                  'message': str(exc) if isinstance(exc, Error) else '备份或发送失败，请查看本地日志；未标记成功。'})
                raise


def verify_parts(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    items = manifest.get('parts', [])
    if manifest.get('schema') != FORMAT or not 1 <= len(items) <= 9999:
        raise Error('备份清单格式无效。')
    parts, total, names = [], 0, set()
    for item in items:
        name = item['name']
        if not SAFE_FILE.fullmatch(name) or name in names:
            raise Error('备份分卷名称无效或重复。')
        names.add(name)
        path = manifest_path.parent / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise Error('分卷缺失或校验失败：' + name)
        total += path.stat().st_size
        parts.append(path)
    if total > MAX_EXPANDED:
        raise Error('加密备份超过恢复大小上限。')
    if len(parts) > 1:
        stem = re.sub(r'\.part-[0-9]{4}$', '', parts[0].name)
        if [p.name for p in parts] != [stem + f'.part-{i:04d}' for i in range(1, len(parts) + 1)]:
            raise Error('分卷顺序不完整。')
    return parts


def unpack(archive, directory):
    """Validate the entire archive before extracting any files; reject links and traversal."""
    with tarfile.open(archive, 'r:gz') as tar:
        members, names, total = [], set(), 0
        for item in tar:
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or item.name in names or str(path) != item.name.rstrip('/'):
                raise Error('备份包含不安全或重复路径。')
            if path.parts[0] not in ('monitor', 'grafana', 'ssh', 'manifest.json', 'backup-config.json'):
                raise Error('备份包含未知目录。')
            if not (item.isfile() or item.isdir()) or item.size < 0:
                raise Error('备份仅允许普通文件和目录，不允许链接或设备。')
            total += item.size
            if total > MAX_EXPANDED or len(members) >= 100000:
                raise Error('解压大小或文件数量超出恢复上限。')
            names.add(item.name)
            members.append(item)
        if not {'manifest.json', 'monitor/.vps-toolkit-managed', 'monitor/compose.yaml', 'grafana/grafana.db', 'backup-config.json'} <= names:
            raise Error('备份缺少关键配置或数据库。')
        if shutil.disk_usage(directory).free < total + 128 * 1024 ** 2:
            raise Error('解压空间不足。')
        for item in members:
            path = directory / item.name
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(item) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target, 128 * 1024)
            os.chmod(path, stat.S_IMODE(item.mode) & 0o777)
            if os.geteuid() == 0:
                os.chown(path, item.uid, item.gid)
        for item in reversed(members):
            if item.isdir():
                path = directory / item.name
                os.chmod(path, stat.S_IMODE(item.mode) & 0o777)
                if os.geteuid() == 0:
                    os.chown(path, item.uid, item.gid)


def wait_grafana(docker):
    state = docker.inspect('grafana')
    bindings = state.get('NetworkSettings', {}).get('Ports', {}).get('3000/tcp')
    port = int(bindings[0]['HostPort']) if bindings else 3000
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(180):
        try:
            with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
                if json.load(response).get('database') == 'ok':
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise Error('数据库已恢复，但 Grafana 未就绪；请检查内存、端口和容器日志。恢复未标记完成。')


def restore(backup, manifest, identity):
    if backup.monitor.exists() and (backup.monitor.is_symlink() or any(backup.monitor.iterdir())):
        raise Error('恢复仅支持空目录或全新监控机；不会覆盖已有配置。')
    if backup.config_file.exists():
        raise Error('本机已有备份配置，恢复已停止；请使用全新监控机。')
    run(['docker', 'info'])
    parts = verify_parts(manifest)
    backup.monitor.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.vtm-restore-', dir=backup.monitor.parent) as temp:
        temp = Path(temp)
        archive = temp / 'archive.tar.gz'
        process = subprocess.Popen(['age', '-d', '-i', str(identity), '-o', str(archive)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for part in parts:
                with part.open('rb') as stream:
                    shutil.copyfileobj(stream, process.stdin, 128 * 1024)
            process.stdin.close()
            if process.wait(timeout=120) != 0:
                raise Error('解密失败，请检查恢复密钥和全部分卷。')
        except BrokenPipeError:
            raise Error('解密失败，请检查恢复密钥。') from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        extracted = temp / 'unpacked'
        extracted.mkdir(mode=0o700)
        unpack(archive, extracted)
        metadata = json.loads((extracted / 'manifest.json').read_text())
        if metadata.get('schema') != FORMAT or metadata.get('monitor_dir') != str(backup.monitor):
            raise Error('备份目录与目标目录不同，不能直接恢复绝对路径引用。')
        project = metadata.get('project', 'vps-toolkit-monitor')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', project):
            raise Error('备份中的 Compose 项目名称无效。')
        volume_name = project + '_grafana-data'
        for name in (volume_name, project + '_prometheus-data'):
            result = subprocess.run(['docker', 'volume', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                raise Error('已有监控数据卷，恢复已停止，不覆盖数据。')
        if run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=' + project, '--format', '{{.ID}}']).strip():
            raise Error('已有监控容器，恢复仅用于全新机器。')
        for port in (3000, 9090, 9100) + ((20245,) if (extracted / 'monitor/compose.tunnel.yaml').exists() else ()):
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                except OSError:
                    raise Error(f'本机端口 {port} 已占用，恢复已停止。') from None
        config = validate_config(read_private(extracted / 'backup-config.json'))
        snapshot = extracted / 'grafana/grafana.db'
        with contextlib.closing(sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise Error('恢复数据库一致性检查失败。')
        # Recheck target before committing any user data.
        if backup.monitor.exists():
            backup.monitor.rmdir()
        print('将恢复：监控配置、证书、Grafana 账号/告警/联系点及 Tunnel。历史指标重新采集。')
        print('旧监控机应先停用；新公网 IP 需要重新授权节点，恢复后会显示操作方法。')
        sources = set()
        for record in (extracted / 'monitor/deploy').glob('*.json'):
            sources.add(str(json.loads(record.read_text())['node']['monitor_ip']))
        if sources:
            print('备份中的监控机源 IP：' + '、'.join(sorted(sources)))
        if input('输入 RESTORE 开始恢复到 ' + str(backup.monitor) + ': ').strip() != 'RESTORE':
            raise Error('已取消，未恢复数据。')
        os.replace(extracted / 'monitor', backup.monitor)
        (backup.monitor / '.restore-incomplete').touch(mode=0o600)
        # No service starts until the SQLite snapshot is in place.
        docker = Docker(backup.monitor)
        overlay = {'services': {'grafana': {'image': metadata['grafana_image'],
                                          'environment': metadata.get('grafana_security', {})}}}
        save(backup.monitor / 'compose.restore.yaml', overlay)
        docker.compose('config', '--quiet')
        docker.compose('create', 'grafana')
        container = docker.inspect('grafana')
        mounts = [m for m in container['Mounts'] if m['Destination'] == '/var/lib/grafana' and m['Type'] == 'volume']
        if len(mounts) != 1 or mounts[0].get('Name') != volume_name:
            raise Error('数据卷名称不符合默认配置；已保留恢复文件，但未启动任何服务。')
        ident = container['Id']
        helper = ['docker', 'run', '--rm', '--network', 'none', '--user', '0', '--volumes-from', ident,
                  '--entrypoint', '/bin/sh', metadata['grafana_image']]
        if run(helper + ['-c', 'find /var/lib/grafana \\( -type f -o -type l \\) -print -quit']).strip():
            raise Error('新数据卷非空，已停止；恢复文件保留，未覆盖数据。')
        run(['docker', 'cp', '-a', str(extracted / 'grafana') + '/.', ident + ':/var/lib/grafana'])
        owner = str(int(metadata['uid'])) + ':' + str(int(metadata['gid']))
        run(helper + ['-c', 'chown -R ' + owner + ' /var/lib/grafana'])
        backup.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        save(backup.config_file, config)
        for name in ('known_hosts', 'known_hosts2'):
            source = extracted / 'ssh' / name
            if source.is_file():
                directory = Path('/root/.ssh')
                directory.mkdir(mode=0o700, exist_ok=True)
                target = directory / name
                if directory.is_symlink() or target.is_symlink():
                    raise Error('SSH 信任目录含符号链接，恢复已停止。')
                previous = target.read_text().splitlines() if target.exists() else []
                added = [line for line in source.read_text().splitlines() if line not in previous]
                with target.open('a') as stream:
                    if previous:
                        stream.write('\n')
                    stream.write('\n'.join(added) + '\n')
                os.chmod(target, 0o600)
        docker.compose('up', '-d')
        print('等待 Grafana 数据库健康检查…', flush=True)
        wait_grafana(docker)
        # Timers are deliberately paused after migration, so no job assumes the old IP works.
        backup.install_timer(config, enable=False)
        (backup.monitor / '.restore-incomplete').unlink()
        print('恢复完成。面板沿用原账号密码、告警和 Tunnel；检查 vt monitor grafana-check。')
        print('证书续期与自动备份尚未启用。公网 IP 未变：vt monitor start，再运行 vt monitor backup-resume。')
        print('公网 IP 已变：监控菜单选择“添加/部署节点”，逐个使用原名称和新源 IP 更新防火墙及受限 SSH 授权；需正常 SSH 登录，不能借旧受限密钥绕过。')
        print('随后逐个检查采集状态、云安全组和备份发送；解密密钥继续另存在 Mac。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor', type=Path, default=Path('/opt/vps-toolkit-monitor'))
    parser.add_argument('--config', type=Path, default=Path('/etc/vps-toolkit-monitor-backup'))
    parser.add_argument('--storage', type=Path, default=Path('/var/lib/vps-toolkit-monitor-backups'))
    parser.add_argument('action', choices=['setup', 'run', 'status', 'logs', 'pause', 'resume', 'restore'])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise Error('请使用 root 运行。')
        if args.monitor.is_symlink() or args.config.is_symlink() or args.storage.is_symlink():
            raise Error('监控、配置和备份目录不能是符号链接。')
        backup = Backup(args.monitor, args.config, args.storage)
        backup.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (backup.config / '.lock').open('a') as lock:
            if args.action not in ('status', 'logs', 'pause'):
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Error('已有备份、设置或恢复任务在运行。') from None
            if args.action == 'setup':
                backup.setup()
            elif args.action == 'run':
                backup.execute()
            elif args.action == 'restore':
                manifest = Path(input('全部分卷所在目录中的 .manifest.json 绝对路径: ').strip())
                identity = Path(input('另存的 recovery-key.txt 绝对路径: ').strip())
                if not manifest.is_absolute() or not identity.is_absolute():
                    raise Error('请输入绝对路径。')
                restore(backup, manifest, identity)
            elif args.action in ('pause', 'resume'):
                config = backup.configuration()
                if args.action == 'resume':
                    backup.install_timer(config)
                    print('自动备份已恢复。')
                else:
                    run(['systemctl', 'disable', '--now', UNIT + '.timer'])
                    print('自动备份已暂停；正在执行的备份不强制中断。')
            elif args.action == 'logs':
                status = backup.config / 'status.json'
                if status.exists():
                    record = json.loads(status.read_text())
                    print('最近执行：' + record.get('utc', '') + '；' +
                          ('全部发送成功' if record.get('ok') else record.get('message', '失败')), flush=True)
                subprocess.run(['journalctl', '-u', UNIT + '.service', '-n', '60', '--no-pager'])
            else:
                if not backup.config_file.exists():
                    print('尚未配置 Telegram 自动备份。')
                    return 0
                config = backup.configuration()
                print('计划：每天北京时间 ' + config['time'] + '，最多随机延迟 5 分钟；本地保留最近 7 组。')
                print('解密密钥不包含在备份内，请另存 Mac；日志和状态不显示 Token。')
                status = backup.config / 'status.json'
                if status.exists():
                    record = json.loads(status.read_text())
                    print('最近一次：' + ('全部发送成功' if record.get('ok') else '失败') + '；UTC：' + record.get('utc', ''))
                subprocess.run(['systemctl', 'list-timers', UNIT + '.timer', '--no-pager'])
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError, sqlite3.Error, tarfile.TarError):
        exc = sys.exc_info()[1]
        print('错误：' + (str(exc) if isinstance(exc, Error) else '备份操作失败；请检查文件、数据库、磁盘和服务。敏感信息未输出。'), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
