#!/usr/bin/env python3
"""Official pinned binaries: local initialization, restart persistence, Agent/Tunnel CLI.
No real domain, API token, live node or Cloudflare connection is used.
"""
import contextlib
import importlib.util
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('komari_module', ROOT / 'lib/komari/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def main():
    with tempfile.TemporaryDirectory(prefix='komari-check-') as temp:
        paths = m.Paths(Path(temp))
        app = m.Komari(paths)
        app.owned(create=True)
        binaries = {}
        supplied = os.environ.get('KOMARI_TEST_BIN_DIR')
        for role in m.VERSIONS:
            if supplied:
                binaries[role] = Path(supplied) / paths.binary(role).name
                import hashlib
                actual = hashlib.sha256(binaries[role].read_bytes()).hexdigest()
                assert actual == m.HASHES[role][m.architecture()], role + ' binary checksum mismatch'
            else:
                app.download(role)
                binaries[role] = paths.binary(role)
        for role, args, expected in [('agent', ['--help'], ['--config', '--disable-web-ssh', '--prefer-ip-version']),
                                     ('tunnel', ['tunnel', 'run', '--help'], ['--token-file'])]:
            result = subprocess.run([str(binaries[role]), *args], capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            for flag in expected:
                assert flag in result.stdout + result.stderr, role + ' missing required flag: ' + flag
        print('PASS: official pinned Agent and cloudflared accept the required config/token-file CLI', flush=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        work = paths.work('server')
        work.mkdir(mode=0o700)
        m.save(paths.state('server'), {'managed_by': m.MARKER, 'role': 'server', 'version': m.VERSIONS['server'], 'port': port})
        unit = m.service_unit(paths, 'server', {'port': port})
        assert f'--listen 127.0.0.1:{port}' in unit
        assert 'User=vps-komari' in unit and 'MemoryMax=256M' in unit
        log = Path(temp) / 'server.log'
        process = None

        def start():
            with log.open('a') as stream:
                result = subprocess.Popen([str(binaries['server']), 'server', '--listen', f'127.0.0.1:{port}'],
                    cwd=work, stdout=stream, stderr=subprocess.STDOUT,
                    env={**os.environ, 'GOMEMLIMIT': '128MiB', 'GOMAXPROCS': '1', 'GODEBUG': 'disablethp=1'})
            for _ in range(150):
                if result.poll() is not None:
                    raise AssertionError('Komari exited during startup: ' + log.read_text())
                try:
                    status, data = m.request(f'http://127.0.0.1:{port}/api/install/status')
                    if status in (200, 404):
                        return result, status
                except m.Error:
                    pass
                time.sleep(.2)
            result.terminate()
            result.wait(timeout=10)
            raise AssertionError('Komari never became ready: ' + log.read_text())

        try:
            process, status = start()
            assert status == 200
            with patch('builtins.input', return_value='fixture-admin'), patch.object(m.getpass, 'getpass', return_value='FixtureAdmin12345'), \
                    patch.object(app, 'active', side_effect=lambda role: process.poll() is None):
                app.initialize()
            for _ in range(100):
                try:
                    code, info = m.request(f'http://127.0.0.1:{port}/api/version')
                except m.Error:
                    time.sleep(.1)
                    continue
                if code == 200 and info and app.initialized():
                    break
                time.sleep(.1)
            else:
                raise AssertionError('First-run API did not leave installation mode')
            status, _ = m.request(f'http://127.0.0.1:{port}/')
            assert status == 200
            print('PASS: real Komari initializes its administrator through the local API and serves the UI', flush=True)
            process.terminate()
            process.wait(timeout=20)
            with contextlib.closing(sqlite3.connect(work / 'data/komari.db')) as db:
                assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
            process, status = start()
            assert app.initialized()
            with patch('builtins.input', side_effect=AssertionError('Must retain existing account')):
                app.initialize()
            assert process.poll() is None
            print('PASS: real Komari restarts with its existing account/database; initialization never resets it', flush=True)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=15)
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
