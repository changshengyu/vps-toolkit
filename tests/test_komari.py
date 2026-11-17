"""Komari provisioning with fake systemd/downloads; never contacts user services."""
import base64
import importlib.util
import io
import json
import os
import hashlib
import http.server
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('komari_manage', ROOT / 'lib/komari/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def tunnel_token():
    return base64.b64encode(json.dumps({'a': 'a' * 32, 't': '11111111-1111-1111-1111-111111111111', 's': 'fixture-secret'}).encode()).decode()


class KomariTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.p = m.Paths(self.root)
        self.app = m.Komari(self.p)
        self.calls = []
        self.running = set()
        self.enabled = set()
        self.fail_restart = None

        def command(args, timeout=30, check=True):
            args = [str(a) for a in args]
            self.calls.append(args)
            code = 0
            if args[0] == 'systemctl':
                unit = args[-1]
                if args[1] == 'is-active':
                    code = 0 if unit in self.running else 3
                elif args[1] == 'is-enabled':
                    code = 0 if unit in self.enabled else 1
                elif args[1] in ('start', 'restart'):
                    if args[1] == 'restart' and self.fail_restart == unit:
                        raise m.Error('fixture restart failed')
                    self.running.add(unit)
                elif args[1] == 'stop':
                    self.running.discard(unit)
                elif args[1] == 'enable':
                    self.enabled.add(unit)
                    if '--now' in args:
                        self.running.add(unit)
                elif args[1] == 'disable':
                    self.enabled.discard(unit)
                    if '--now' in args:
                        self.running.discard(unit)
            if check and code:
                raise m.Error('fixture command failure')
            return subprocess.CompletedProcess(args, code, '', '')

        self.command = patch.object(m, 'command', side_effect=command)
        self.command.start()
        self.addCleanup(self.command.stop)
        self.users = patch.object(self.app, 'ensure_user', return_value=(os.geteuid(), os.getegid()))
        self.users.start()
        self.addCleanup(self.users.stop)
        self.download = patch.object(self.app, 'download', side_effect=self.fake_download)
        self.download.start()
        self.addCleanup(self.download.stop)
        self.requests = patch.object(m, 'request', return_value=(404, {}))
        self.requests.start()
        self.addCleanup(self.requests.stop)
        self.initialized = patch.object(self.app, 'initialized', return_value=True)
        self.initialized.start()
        self.addCleanup(self.initialized.stop)

    def fake_download(self, role):
        m.atomic(self.p.binary(role), '#!/bin/sh\nexit 0\n', 0o755)

    def test_working_directory_is_an_absolute_scalar_for_all_roles(self):
        paths = m.Paths(self.root / 'path with spaces % sign')
        for role in ('server', 'agent', 'tunnel'):
            text = m.service_unit(paths, role, {'port': 25774})
            line = next(line for line in text.splitlines() if line.startswith('WorkingDirectory='))
            self.assertEqual(line, 'WorkingDirectory=' + str(paths.work(role)).replace('%', '%%'))
            self.assertNotIn('WorkingDirectory="', text)
        for value in ('relative/path', '/absolute\nInjected=yes', '/absolute\rInjected=yes', '/bad\0path'):
            with self.subTest(value=value), self.assertRaises(m.Error):
                m.working_directory(value)

    def test_unit_prevalidation_failure_does_not_stop_or_replace_existing_service(self):
        self.install('agent', {'agent.json': '{"token":"existing-private-token"}'})
        before = {p: p.read_bytes() for p in (self.p.unit('agent'), self.p.state('agent'), self.p.work('agent') / 'agent.json')}
        self.calls.clear()
        with patch.object(m.shutil, 'which', return_value='/usr/bin/systemd-analyze'), \
                patch.object(m, 'command', side_effect=m.Error('invalid unit')), self.assertRaises(m.Error):
            self.app.install_service('agent', {'endpoint': 'https://new.example.com'}, {'agent.json': '{"token":"replacement"}'})
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn(self.p.unit('agent').name, self.running)
        self.assertEqual(self.calls, [])

    def test_activation_failure_is_diagnosed_before_new_unit_is_removed(self):
        self.app.owned(create=True)
        self.fail_restart = self.p.unit('agent').name
        with patch.object(self.app, 'activation_diagnostics') as diagnose:
            diagnose.side_effect = lambda role: self.assertTrue(self.p.unit(role).exists())
            with self.assertRaises(m.Error):
                self.app.install_service('agent', {'endpoint': 'https://komari.example.com'}, {'agent.json': '{"token":"fixture"}'})
        diagnose.assert_called_once_with('agent')
        self.assertFalse(self.p.unit('agent').exists())
        self.assertFalse(self.p.state('agent').exists())

    def test_systemd_failure_exposes_action_and_sanitized_reason(self):
        self.command.stop()
        failure = subprocess.CompletedProcess([], 1, '', 'WorkingDirectory= path is not absolute\ntoken=private-value\n')
        try:
            with patch.object(m.subprocess, 'run', return_value=failure), self.assertRaises(m.Error) as error:
                m.command(['systemd-analyze', 'verify', '/tmp/example.service'])
            self.assertIn('systemd-analyze verify', str(error.exception))
            self.assertIn('path is not absolute', str(error.exception))
            self.assertNotIn('private-value', str(error.exception))
        finally:
            self.command.start()

    def install(self, role='server', files=None):
        self.app.owned(create=True)
        self.app.install_service(role, {'port': 25774} if role == 'server' else {'endpoint': 'https://komari.example.com'}, files)

    def test_server_is_loopback_unprivileged_autostart_and_idempotent(self):
        with patch('builtins.input', side_effect=['', 'y']), patch('sys.stdout', new_callable=io.StringIO):
            self.app.server_install()
        text = self.p.unit('server').read_text()
        self.assertIn('server --listen 127.0.0.1:25774', text)
        self.assertIn('User=vps-komari\n', text)
        self.assertIn('MemoryMax=256M', text)
        self.assertIn('WantedBy=multi-user.target', text)
        self.assertTrue(self.app.active('server'))
        before = self.p.state('server').read_bytes()
        database = self.p.work('server') / 'data'
        database.mkdir()
        (database / 'komari.db').write_bytes(b'existing account and nodes')
        with patch('builtins.input', side_effect=AssertionError('must not prompt again')), patch('sys.stdout', new_callable=io.StringIO):
            self.app.server_install()
        self.assertEqual(self.p.state('server').read_bytes(), before)
        self.assertEqual((database / 'komari.db').read_bytes(), b'existing account and nodes')
        self.assertFalse(any('docker' in c or 'ufw' in c or 'x-ui' in c for c in self.calls))

    def test_initialization_posts_to_local_api_without_persisting_password(self):
        self.install()
        password = 'Strong-Secret-123'
        with patch.object(m, 'request', side_effect=[(200, {'data': {'required': True}}), (200, {})]) as api, \
                patch.object(self.app, 'initialized', side_effect=[False, True]), \
                patch.object(self.app, 'wait_server'), \
                patch('builtins.input', return_value='test-admin'), \
                patch.object(m.getpass, 'getpass', side_effect=[password, password]), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.app.initialize()
        args = api.call_args_list[-1].args
        self.assertEqual(args[0], 'http://127.0.0.1:25774/api/install/complete')
        self.assertEqual(args[1]['password'], password)
        self.assertEqual(args[1]['metric_dsn'], './data/metrics.db')
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(password, self.p.state('server').read_text())

    def test_agent_config_is_private_and_disables_remote_control_and_updates(self):
        self.app.owned(create=True)
        secret = 'agent-fixture-token-1234'
        with patch('builtins.input', side_effect=['komari.example.com', '', '', '', 'y']), \
                patch.object(m.getpass, 'getpass', return_value=secret), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.app.agent_install()
        cfg_path = self.p.work('agent') / 'agent.json'
        cfg = json.loads(cfg_path.read_text())
        self.assertEqual(cfg['endpoint'], 'https://komari.example.com')
        self.assertTrue(cfg['disable_web_ssh'])
        self.assertTrue(cfg['disable_auto_update'])
        self.assertFalse(cfg['ignore_unsafe_cert'])
        self.assertEqual(cfg['token'], secret)
        self.assertEqual(cfg['interval'], 5)
        self.assertEqual(cfg_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(secret, self.p.unit('agent').read_text())
        self.assertNotIn(secret, self.p.state('agent').read_text())
        self.assertNotIn(secret, output.getvalue())
        self.assertIn('--config', self.p.unit('agent').read_text())
        with patch('builtins.input', side_effect=['', '', '', '', 'y']), patch.object(m.getpass, 'getpass', return_value=''), patch('sys.stdout', new_callable=io.StringIO):
            self.app.agent_install()
        self.assertEqual(json.loads(cfg_path.read_text())['token'], secret)

    def test_auto_discovery_mode_is_retained_on_reconfigure(self):
        self.app.owned(create=True)
        with patch('builtins.input', side_effect=['https://komari.example.com', '2', '10', '2', 'y']), \
                patch.object(m.getpass, 'getpass', return_value='discovery-secret-123'), patch('sys.stdout', new_callable=io.StringIO):
            self.app.agent_install()
        with patch('builtins.input', side_effect=['', '', '', '', 'y']), patch.object(m.getpass, 'getpass', return_value=''), patch('sys.stdout', new_callable=io.StringIO):
            self.app.agent_install()
        cfg = json.loads((self.p.work('agent') / 'agent.json').read_text())
        self.assertFalse(cfg['disable_web_ssh'])
        self.assertEqual(cfg['auto_discovery_key'], 'discovery-secret-123')
        self.assertNotIn('token', cfg)

    def test_tunnel_requires_initialized_server_and_uses_token_file(self):
        self.install()
        with patch.object(self.app, 'initialized', return_value=False), self.assertRaises(m.Error):
            self.app.tunnel_install()
        token = tunnel_token()
        with patch('builtins.input', side_effect=['https://komari.example.com', 'y']), \
                patch.object(m.getpass, 'getpass', return_value=token), \
                patch.object(m, 'request', return_value=(200, {})), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.app.tunnel_install()
        unit = self.p.unit('tunnel').read_text()
        self.assertIn('--token-file', unit)
        self.assertIn('127.0.0.1:20246', unit)
        self.assertNotIn(token, unit + output.getvalue())
        self.assertEqual((self.p.work('tunnel') / 'token').stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.app.active('server'))

    def test_failure_rolls_back_agent_config_and_keeps_other_service(self):
        self.install('agent', {'agent.json': '{"token":"old-private-token"}'})
        before = self.p.state('agent').read_bytes()
        unit = self.p.unit('agent').read_bytes()
        self.fail_restart = self.p.unit('agent').name
        with self.assertRaises(m.Error):
            self.app.install_service('agent', {'endpoint': 'https://new.example.com'}, {'agent.json': '{"token":"new-private-token"}'})
        self.assertEqual(self.p.state('agent').read_bytes(), before)
        self.assertEqual(self.p.unit('agent').read_bytes(), unit)
        self.assertEqual((self.p.work('agent') / 'agent.json').read_text(), '{"token":"old-private-token"}')
        self.assertTrue(self.app.active('agent'))

    def test_uninstall_preserves_data_and_reinstallation_restores_service(self):
        self.install()
        target = self.p.work('server') / 'important.db'
        target.write_bytes(b'keep database')
        with patch('builtins.input', return_value='y'), patch('sys.stdout', new_callable=io.StringIO):
            self.app.service('server', 'uninstall')
        self.assertFalse(self.p.unit('server').exists())
        self.assertTrue(self.p.state('server').exists())
        self.assertEqual(target.read_bytes(), b'keep database')
        with patch('sys.stdout', new_callable=io.StringIO):
            self.app.server_install()
        self.assertTrue(self.p.unit('server').exists())
        self.assertEqual(target.read_bytes(), b'keep database')

    def test_unmanaged_paths_and_units_are_not_overwritten(self):
        self.p.base.mkdir(parents=True)
        with self.assertRaises(m.Error):
            self.app.owned(create=True)
        m.atomic(self.p.base / '.vps-toolkit-owned', m.MARKER, 0o644)
        self.p.units.mkdir(parents=True)
        self.p.unit('server').write_text('[Service]\nExecStart=/somebody/else\n')
        with self.assertRaises(m.Error):
            self.app.owned(create=True)
        self.assertIn('/somebody/else', self.p.unit('server').read_text())

    def test_low_memory_or_conflicting_port_aborts_before_install(self):
        self.app.owned(create=True)
        proc = self.root / 'proc'
        proc.mkdir()
        (proc / 'meminfo').write_text('MemAvailable: 1024 kB\n')
        with self.assertRaises(m.Error):
            self.app.headroom(192)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            with self.assertRaises(m.Error):
                self.app.free_port(sock.getsockname()[1])
        self.assertEqual(self.calls, [])

    def test_bad_download_is_rejected_before_binary_install(self):
        self.app.owned(create=True)
        self.download.stop()
        def bad(args, **kwargs):
            Path(args[args.index('--output') + 1]).write_bytes(b'not the release binary')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(m, 'command', side_effect=bad), self.assertRaises(m.Error):
            self.app.download('server')
        self.assertFalse(self.p.binary('server').exists())
        self.assertFalse(list(self.p.bin.glob('.download-*')))
        self.download.start()

    def test_endpoint_and_tunnel_token_validation_do_not_echo_credentials(self):
        for url in ('http://example.com', 'https://user:password@example.com', 'https://example.com/?token=secret',
                    'https://example.com/path', 'https://127.0.0.1', 'https://example.com\nX-Test:1'):
            with self.subTest(url=url), self.assertRaises(m.Error) as error:
                m.endpoint(url)
            self.assertNotIn('password', str(error.exception))
            self.assertNotIn('token=secret', str(error.exception))
        self.assertEqual(m.endpoint('https://Komari.Example.com/'), 'https://komari.example.com')
        self.assertEqual(m.tunnel_credential(tunnel_token()), tunnel_token())
        with self.assertRaises(m.Error):
            m.tunnel_credential('not-a-tunnel-api-token')

    def test_log_view_redacts_tokens_and_password_fields(self):
        self.install('agent', {'agent.json': json.dumps({'token': 'private-agent-token-987'})})
        with patch.object(m, 'command', return_value=subprocess.CompletedProcess([], 0, 'token=private-agent-token-987 password=privatePass\n', '')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.app.logs('agent')
        self.assertNotIn('private-agent-token-987', output.getvalue())
        self.assertNotIn('privatePass', output.getvalue())

    def test_download_can_retry_through_official_asset_api_with_same_hash(self):
        self.app.owned(create=True)
        self.download.stop()
        payload = b'fixture-official-binary'
        calls = []
        def fetch(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 28, '', 'timeout')
            Path(args[args.index('--output') + 1]).write_bytes(payload)
            return subprocess.CompletedProcess(args, 0, '', '')
        hashes = {**m.HASHES, 'server': {'amd64': hashlib.sha256(payload).hexdigest()}}
        with patch.object(m, 'architecture', return_value='amd64'), patch.object(m, 'HASHES', hashes), \
                patch.object(m, 'command', side_effect=fetch), patch('sys.stdout', new_callable=io.StringIO):
            self.app.download('server')
        self.assertEqual(len(calls), 2)
        self.assertIn('api.github.com', calls[1][-1])
        self.assertEqual(self.p.binary('server').read_bytes(), payload)
        self.download.start()

    def test_plain_text_cloudflared_readiness_response_is_accepted(self):
        self.requests.stop()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'200 OK\n')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = m.request(f'http://127.0.0.1:{server.server_port}/ready')
            self.assertEqual(status, 200)
            self.assertEqual(body, {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.requests.start()

    def test_initialized_reads_account_count_without_changing_database(self):
        self.initialized.stop()
        path = self.p.work('server') / 'data/komari.db'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE users (username TEXT)')
        self.assertFalse(self.app.initialized())
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO users VALUES (?)', ('fixture-admin',))
        before = path.read_bytes()
        self.assertTrue(self.app.initialized())
        self.assertEqual(path.read_bytes(), before)
        self.initialized.start()


if __name__ == '__main__':
    unittest.main()
