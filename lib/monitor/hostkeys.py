"""Explicit SSH host-key replacement for interactive node deployment only."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile


class Error(Exception):
    pass


def lookup_name(host, port):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,253}', host) or not 1 <= port <= 65535:
        raise Error('节点地址或 SSH 端口无效。')
    return host if port == 22 else f'[{host}]:{port}'


def fingerprint(kind, encoded):
    try:
        blob = base64.b64decode(encoded, validate=True)
        length = int.from_bytes(blob[:4], 'big')
        if not 1 <= length <= 128 or blob[4:4 + length].decode('ascii') != kind:
            raise ValueError()
        return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    except (ValueError, UnicodeError):
        raise Error('SSH 公钥格式无效。') from None


def matches(pattern, host):
    if pattern.startswith('|1|'):
        try:
            _, version, salt, value = pattern.split('|')
            expected = hmac.new(base64.b64decode(salt, validate=True), host.encode(), hashlib.sha1).digest()
            return hmac.compare_digest(expected, base64.b64decode(value, validate=True))
        except (ValueError, TypeError):
            return False
    regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return re.fullmatch(regex, host, re.I) is not None


def replacement(original, host, new_kind, new_key):
    retained, previous, hashed = [], [], False
    for line in original.splitlines(keepends=True):
        fields = line.split()
        if not fields or line.lstrip().startswith('#'):
            retained.append(line)
            continue
        marker = fields[0] if fields[0].startswith('@') else None
        offset = 1 if marker else 0
        if len(fields) < offset + 3:
            retained.append(line)
            continue
        hosts = fields[offset].split(',')
        if any(p.startswith('!') and matches(p[1:], host) for p in hosts):
            retained.append(line)
            continue
        selected = [p for p in hosts if not p.startswith('!') and matches(p, host)]
        if not selected:
            retained.append(line)
            continue
        if marker or any('*' in p or '?' in p or p.startswith('!') for p in hosts):
            raise Error('匹配到撤销、CA、通配符或策略记录，不能自动替换；请手动检查 known_hosts。')
        previous.append((fields[offset + 1], fingerprint(fields[offset + 1], fields[offset + 2])))
        hashed = hashed or any(p.startswith('|1|') for p in selected)
        remaining = [p for p in hosts if p not in selected]
        if remaining:
            # Keep other aliases from a shared record; ssh-keygen -R removes the whole line.
            retained.append(','.join(remaining) + ' ' + ' '.join(fields[offset + 1:]) + '\n')
    if not previous:
        raise Error('未在当前用户 known_hosts 找到可替换记录；全局文件或其他策略记录请手动核对。')
    saved_host = host
    if hashed:
        salt = secrets.token_bytes(20)
        value = hmac.new(salt, host.encode(), hashlib.sha1).digest()
        saved_host = '|1|' + base64.b64encode(salt).decode() + '|' + base64.b64encode(value).decode()
    text = ''.join(retained)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + f'{saved_host} {new_kind} {new_key}\n', sorted(set(previous))


def scan(host, port, family, observed):
    try:
        result = subprocess.run(['ssh-keyscan', '-4' if family == 'inet' else '-6', '-T', '5',
                                 '-p', str(port), '-t', 'ed25519,ecdsa,rsa', host],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise Error('读取新 SSH 主机公钥失败或超过 15 秒，旧记录保留。') from None
    if len(result.stdout) > 65536:
        raise Error('主机公钥响应过大，已停止。')
    choices = set()
    allowed = {'ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521'}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] not in allowed or len(fields[2]) > 16384:
            continue
        if fingerprint(fields[1], fields[2]) == observed:
            choices.add((fields[1], fields[2]))
    if len(choices) != 1:
        raise Error('扫描到的公钥与 SSH 刚报告的新指纹不一致；可能发生再次变更或多地址解析，旧记录保留。')
    return choices.pop()


def replace_known_host(path, original, updated):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise Error('known_hosts 必须是普通文件，不能是符号链接。')
    current = path.stat()
    parent = path.parent.stat()
    if (current.st_uid != os.geteuid() or parent.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) & 0o022 or stat.S_IMODE(parent.st_mode) & 0o022):
        raise Error('known_hosts 所有者或写入权限不安全，已停止。')
    if path.read_text() != original:
        raise Error('等待确认期间 known_hosts 已改变，未覆盖；请重新执行。')
    fd, backup_name = tempfile.mkstemp(prefix=path.name + '.vps-toolkit-backup-', dir=path.parent)
    with os.fdopen(fd, 'w') as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.vtm-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(current.st_mode))
        if os.geteuid() == 0:
            os.chown(temporary, current.st_uid, current.st_gid)
        if path.read_text() != original:
            raise Error('known_hosts 已被其他进程修改，未覆盖；旧快照已保留。')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return Path(backup_name)


def review(host, port, family, log, known_hosts, pin, result_path):
    text = log.read_text(errors='replace')
    if len(text) > 65536 or 'REMOTE HOST IDENTIFICATION HAS CHANGED!' not in text:
        raise Error('这不是受支持的 SSH 指纹变更错误，未修改记录。')
    values = set(re.findall(r'fingerprint for the [^\n]{1,80} key sent by the remote host is\s+(SHA256:[A-Za-z0-9+/]{43})(?:\.|\s|$)', text))
    if len(values) != 1:
        raise Error('无法唯一识别 SSH 报告的新指纹，旧记录保留。')
    observed = values.pop()
    host_name = lookup_name(host, port)
    if known_hosts.is_symlink() or not known_hosts.is_file():
        raise Error('没有可安全更新的用户 known_hosts 文件。')
    original = known_hosts.read_text()
    print('检测到当前节点的 SSH 主机身份变更；可能是换机、重装，也可能连接到了错误机器。', flush=True)
    print('正在读取新主机公钥并与本次 SSH 报告的指纹比对（最多 15 秒）…', flush=True)
    kind, key = scan(host, port, family, observed)
    updated, previous = replacement(original, host_name, kind, key)
    print(f'节点：{host}；SSH 端口：{port}')
    for old_kind, old_fingerprint in previous:
        print(f'原指纹：{old_kind} {old_fingerprint}')
    print(f'新指纹：{kind} {observed}')
    print('请通过新 VPS 的商家控制台 / VNC 核对指纹。网络扫描只能读取公钥，不能证明它属于你的机器。')
    key_name = 'ed25519' if kind == 'ssh-ed25519' else 'rsa' if kind == 'ssh-rsa' else 'ecdsa'
    print(f'控制台核对命令：ssh-keygen -lf /etc/ssh/ssh_host_{key_name}_key.pub -E sha256')
    print('确认后会备份 known_hosts，仅替换当前地址和端口的记录，并固定这把新密钥重试一次。')
    if input('已核对并信任此新指纹，继续？输入 yes 确认，回车取消: ').strip() != 'yes':
        raise Error('已取消；旧 SSH 信任记录和监控配置保持原样。')
    # Pin before changing the persistent store. Never retry with StrictHostKeyChecking=no.
    if pin.exists() or pin.is_symlink() or result_path.exists() or result_path.is_symlink():
        raise Error('临时固定文件已存在，已停止。')
    with pin.open('x') as stream:
        stream.write(f'{host_name} {kind} {key}\n')
    os.chmod(pin, 0o600)
    algorithms = 'rsa-sha2-512,rsa-sha2-256' if kind == 'ssh-rsa' else kind
    with result_path.open('x') as stream:
        json.dump({'algorithms': algorithms, 'fingerprint': observed}, stream)
    os.chmod(result_path, 0o600)
    backup = replace_known_host(known_hosts, original, updated)
    print('已更新当前节点信任记录；原文件备份：' + str(backup), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('host', 'port', 'family', 'log', 'known-hosts', 'pin', 'result'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.family not in ('inet', 'inet6'):
            raise Error('SSH 地址族无效。')
        review(args.host, int(args.port), args.family, Path(args.log), Path(args.known_hosts), Path(args.pin), Path(args.result))
        return 0
    except (Error, OSError, ValueError):
        exc = sys.exc_info()[1]
        print('错误：' + (str(exc) if isinstance(exc, Error) else '无法处理 SSH 主机身份变更，未自动继续部署。'), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消，未继续连接。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
