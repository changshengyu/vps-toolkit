"""Exercise real SQLite/age recovery sets, safe extraction and private Telegram uploads."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('backup', ROOT / 'lib/monitor/backup.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
TOKEN = '123456789:abcdefghijklmnopqrstuvw_test'


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = self.root / 'hub'
        self.config = self.root / 'config'
        self.storage = self.root / 'storage'
        self.grafana = self.root / 'grafana'
        for path in (self.hub, self.grafana, self.config):
            path.mkdir(mode=0o755)
        self.job = b.Backup(self.hub, self.config, self.storage, self.root / 'systemd')
        (self.hub / '.vps-toolkit-managed').touch()
        (self.hub / 'compose.yaml').write_text('name: vps-toolkit-monitor\nservices: {}\n')
        (self.hub / 'prometheus/targets').mkdir(parents=True, mode=0o755)
        (self.hub / 'pki').mkdir(mode=0o700)
        (self.hub / 'pki/ca-key.pem').write_text('private-ca-test')
        os.chmod(self.hub / 'pki/ca-key.pem', 0o600)
        db = sqlite3.connect(self.grafana / 'grafana.db')
        db.execute('CREATE TABLE recovery (kind text, value text)')
        db.executemany('INSERT INTO recovery VALUES (?, ?)', [('login', 'changed-admin'), ('alert', 'node failed'), ('contact', 'telegram_bot')])
        db.commit()
        db.close()
        self.container = {'Config': {'Image': 'grafana/grafana:13.3.1',
                                    'Env': ['GF_SECURITY_SECRET_KEY=encryption-key-test']}}
        self.cfg = {'schema': b.FORMAT, 'token': TOKEN, 'chat_id': '123456789',
                    'recipient': 'age1' + 'a' * 58, 'time': '04:00'}

    def test_online_sqlite_snapshot_contains_committed_wal_data(self):
        live = sqlite3.connect(self.grafana / 'grafana.db')
        self.addCleanup(live.close)
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('INSERT INTO recovery VALUES (?, ?)', ('wal', 'committed'))
        live.commit()
        target = self.root / 'snapshot.db'
        b.sqlite_snapshot(self.grafana / 'grafana.db', target)
        with sqlite3.connect(target) as saved:
            self.assertIn(('wal', 'committed'), saved.execute('SELECT * FROM recovery').fetchall())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_private_config_and_recipient_validation(self):
        b.save(self.job.config_file, self.cfg)
        self.assertEqual(self.job.configuration()['token'], TOKEN)
        os.chmod(self.job.config_file, 0o644)
        with self.assertRaisesRegex(b.Error, '权限'):
            self.job.configuration()
        for key, value in [('chat_id', '-100123'), ('time', '04:00\nOnBootSec=1'), ('recipient', '-o command')]:
            with self.subTest(key=key), self.assertRaises(b.Error):
                b.validate_config(dict(self.cfg, **{key: value}))

    def test_chunking_checksums_order_and_tamper_detection(self):
        p = self.root / 'backup-20261113T000000Z-1234abcd.tar.gz.age'
        p.write_bytes(b'abcdef' * 100)
        parts = b.split_encrypted(p, limit=200)
        self.assertEqual([x.stat().st_size for x in parts], [200, 200, 200])
        manifest = self.root / 'set.manifest.json'
        value = {'schema': b.FORMAT, 'parts': [{'name': f.name, 'bytes': f.stat().st_size, 'sha256': b.digest(f)} for f in parts]}
        b.save(manifest, value)
        self.assertEqual(b.verify_parts(manifest), parts)
        value['parts'].reverse()
        b.save(manifest, value)
        with self.assertRaisesRegex(b.Error, '顺序'):
            b.verify_parts(manifest)
        value['parts'].reverse()
        b.save(manifest, value)
        parts[0].write_bytes(b'x' * 200)
        with self.assertRaisesRegex(b.Error, '校验'):
            b.verify_parts(manifest)

    def test_archive_traversal_links_and_expansion_rejected_before_extraction(self):
        for name, kind in [('../../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('monitor/link', tarfile.SYMTYPE), ('monitor/hardlink', tarfile.LNKTYPE)]:
            with self.subTest(name=name):
                archive = self.root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    info = tarfile.TarInfo(name)
                    info.type, info.linkname = kind, '/etc/passwd'
                    tar.addfile(info)
                output = self.root / 'extracted'
                output.mkdir(exist_ok=True)
                with self.assertRaises(b.Error):
                    b.unpack(archive, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_timer_is_persistent_and_uses_explicit_shanghai_timezone(self):
        b.save(self.job.config_file, self.cfg)
        with patch.object(b, 'run', return_value=b'') as command:
            self.job.install_timer(self.cfg)
        timer = (self.job.systemd / (b.UNIT + '.timer')).read_text()
        service = (self.job.systemd / (b.UNIT + '.service')).read_text()
        self.assertIn('04:00:00 Asia/Shanghai', timer)
        self.assertIn('Persistent=true', timer)
        self.assertIn('MemoryMax=192M', service)
        self.assertNotIn(TOKEN, service + timer)
        self.assertIn('enable', command.call_args_list[-1].args[0])

    def key_and_config(self):
        key = self.root / 'recovery-key.txt'
        subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
        self.cfg['recipient'] = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
        b.save(self.job.config_file, self.cfg)
        return key

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'requires age')
    def test_setup_waits_for_offline_key_ack_and_never_echoes_bot_token(self):
        output = io.StringIO()
        with patch.object(b.getpass, 'getpass', return_value=TOKEN), \
                patch('builtins.input', side_effect=['123456789', '', 'NO']), \
                patch.object(b.Telegram, 'verify'), patch.object(self.job, 'install_timer') as timer, \
                contextlib.redirect_stdout(output), self.assertRaisesRegex(b.Error, '未启用'):
            self.job.setup()
        timer.assert_not_called()
        self.assertFalse(self.job.config_file.exists())
        self.assertTrue((self.config / 'recovery-key.txt').exists())
        self.assertNotIn(TOKEN, output.getvalue())

    def test_retention_only_removes_sets_owned_by_this_tool(self):
        b.save(self.job.config_file, self.cfg)
        self.storage.mkdir()
        for i in range(9):
            path = self.storage / ('backup-' + str(i))
            path.mkdir()
            (path / '.owned').write_text(b.FORMAT)
        unrelated = self.storage / 'backup-personal'
        unrelated.mkdir()
        (unrelated / 'keep').touch()
        manifest = self.storage / 'backup-8/set.manifest.json'
        manifest.write_text('{}')
        part = self.storage / 'backup-8/set.age'
        part.write_bytes(b'encrypted')
        docker = Mock()
        docker.grafana.return_value = self.grafana, self.container
        with patch.object(self.job, 'create', return_value=(manifest, [part])):
            self.job.execute(docker, Mock())
        self.assertTrue((unrelated / 'keep').exists())
        self.assertFalse((self.storage / 'backup-0').exists())
        self.assertEqual(len(list(self.storage.glob('backup-*'))), 8)

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'requires age')
    def test_real_age_round_trip_and_default_permissions(self):
        key = self.key_and_config()
        manifest, parts = self.job.create(self.cfg, self.grafana, self.container)
        archive = self.root / 'archive.tar.gz'
        subprocess.run(['age', '-d', '-i', str(key), '-o', str(archive), str(parts[0])], check=True, capture_output=True)
        extracted = self.root / 'unpacked'
        extracted.mkdir()
        b.unpack(archive, extracted)
        self.assertEqual((extracted / 'monitor/pki/ca-key.pem').read_text(), 'private-ca-test')
        self.assertTrue((extracted / 'monitor/prometheus/targets').is_dir())
        self.assertEqual(stat.S_IMODE((extracted / 'monitor').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((extracted / 'monitor/pki').stat().st_mode), 0o700)
        self.assertFalse(list(extracted.rglob('recovery-key.txt')))
        self.assertNotIn(TOKEN, manifest.read_text())
        metadata = json.loads((extracted / 'manifest.json').read_text())
        self.assertEqual(metadata['grafana_security']['GF_SECURITY_SECRET_KEY'], 'encryption-key-test')
        self.assertFalse(metadata['prometheus_history'])

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'requires age')
    def test_failed_delivery_is_not_recorded_as_success_or_pruned(self):
        self.key_and_config()
        docker = Mock()
        docker.grafana.return_value = self.grafana, self.container
        sender = Mock()
        sender.send.side_effect = b.Error('upload failed')
        with self.assertRaises(b.Error):
            self.job.execute(docker, sender)
        self.assertFalse(json.loads((self.config / 'status.json').read_text())['ok'])
        self.assertTrue(list(self.storage.glob('backup-*/*.age')))

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'requires age')
    def test_full_restore_to_fresh_machine_preserves_data_and_pauses_timers(self):
        key = self.key_and_config()
        manifest, _ = self.job.create(self.cfg, self.grafana, self.container)
        self.hub.rename(self.root / 'old-hub')
        self.config.rename(self.root / 'old-config')
        volume = self.root / 'volume'
        volume.mkdir()
        (volume / 'plugins').mkdir()  # Docker may initialize empty directories.
        docker = Mock()
        docker.inspect.return_value = {'Id': 'test-grafana-id', 'Mounts': [{'Destination': '/var/lib/grafana', 'Source': str(volume),
                                                  'Type': 'volume', 'Name': 'vps-toolkit-monitor_grafana-data'}]}
        def command(args, **kwargs):
            if args[:3] == ['docker', 'cp', '-a']:
                shutil.copytree(Path(args[3]), volume, dirs_exist_ok=True)
            return b''
        with patch.object(b, 'Docker', return_value=docker), patch.object(b, 'run', side_effect=command), \
                patch.object(b.subprocess, 'run', return_value=Mock(returncode=1)), \
                patch.object(self.job, 'install_timer') as timer, patch.object(b, 'wait_grafana'), patch('builtins.input', return_value='RESTORE'):
            b.restore(self.job, manifest, key)
        with sqlite3.connect(volume / 'grafana.db') as db:
            self.assertIn(('contact', 'telegram_bot'), db.execute('SELECT * FROM recovery').fetchall())
        self.assertEqual((self.hub / 'pki/ca-key.pem').read_text(), 'private-ca-test')
        self.assertEqual(self.job.configuration()['recipient'], self.cfg['recipient'])
        self.assertEqual(json.loads((self.hub / 'compose.restore.yaml').read_text())['services']['grafana']['environment']['GF_SECURITY_SECRET_KEY'], 'encryption-key-test')
        timer.assert_called_once()
        self.assertIn(unittest.mock.call('up', '-d'), docker.compose.call_args_list)
        with self.assertRaisesRegex(b.Error, '不会覆盖'):
            b.restore(self.job, manifest, key)


class TelegramTests(unittest.TestCase):
    def test_streaming_multipart_does_not_put_token_in_command_or_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            document = Path(temp) / 'backup-test.age'
            document.write_bytes(b'x' * (1024 * 1024))
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
            with patch.object(b.http.client, 'HTTPSConnection', return_value=connection) as factory:
                result = b.Telegram({'token': TOKEN, 'chat_id': '123456789'}).send(document, 'encrypted backup')
            self.assertEqual(result['message_id'], 1)
            factory.assert_called_once_with('api.telegram.org', timeout=120)
            chunks = [call.args[0] for call in connection.send.call_args_list]
            self.assertLessEqual(max(map(len, chunks)), 128 * 1024)
            length = next(call.args[1] for call in connection.putheader.call_args_list if call.args[0] == 'Content-Length')
            self.assertEqual(int(length), sum(map(len, chunks)))
            self.assertIn(b'name="chat_id"', chunks[0])

    def test_server_errors_never_expose_bot_token_or_response_text(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 400
        response.read.return_value = json.dumps({'ok': False, 'description': TOKEN}).encode()
        with patch.object(b.http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(b.Error) as result:
            b.Telegram({'token': TOKEN, 'chat_id': '123456789'}).request('getMe')
        self.assertNotIn(TOKEN, str(result.exception))


if __name__ == '__main__':
    unittest.main()
