"""Host-key confirmation preserves other nodes, rejects policies and pins retries."""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from test_monitoring import ROOT, MonitoringFixture

spec = importlib.util.spec_from_file_location('hostkeys', ROOT / 'lib/monitor/hostkeys.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HostKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.keys = []
        for name in ('old', 'new', 'other'):
            file = self.path / name
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(file)], check=True)
            kind, key = file.with_suffix('.pub').read_text().split()[:2]
            self.keys.append((kind, key))
        self.old, self.new, self.other = self.keys
        self.known = self.path / 'known_hosts'
        self.original = '# keep comment\nnode.example.test,other.example.test ' + ' '.join(self.old) + '\n'
        self.known.write_text(self.original)
        os.chmod(self.known, 0o600)
        self.pin, self.result = self.path / 'pin', self.path / 'result.json'
        self.log = self.path / 'ssh.log'
        self.log.write_text('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n'
                            'The fingerprint for the ED25519 key sent by the remote host is\n' +
                            h.fingerprint(*self.new) + '.\n')

    def review(self):
        h.review('node.example.test', 22, 'inet', self.log, self.known, self.pin, self.result)

    def test_cancel_does_not_change_any_trust_records(self):
        with patch.object(h, 'scan', return_value=self.new), patch('builtins.input', return_value=''), \
                self.assertRaisesRegex(h.Error, '已取消'):
            self.review()
        self.assertEqual(self.known.read_text(), self.original)
        self.assertFalse(self.pin.exists())
        self.assertFalse(list(self.path.glob('known_hosts.vps-toolkit-backup-*')))

    def test_confirm_preserves_alias_and_creates_private_pin_and_backup(self):
        with patch.object(h, 'scan', return_value=self.new), patch('builtins.input', return_value='yes'):
            self.review()
        changed = self.known.read_text()
        self.assertIn('# keep comment\n', changed)
        self.assertIn('other.example.test ' + ' '.join(self.old), changed)
        self.assertIn('node.example.test ' + ' '.join(self.new), changed)
        backup = list(self.path.glob('known_hosts.vps-toolkit-backup-*'))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_text(), self.original)
        self.assertEqual(backup[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pin.read_text(), 'node.example.test ' + ' '.join(self.new) + '\n')
        self.assertEqual(json.loads(self.result.read_text())['algorithms'], 'ssh-ed25519')

    def test_hashed_nonstandard_port_is_updated_without_touching_port_22(self):
        name = h.lookup_name('2001:db8::2', 2222)
        salt = b'fixed test salt'
        digest = hmac.new(salt, name.encode(), hashlib.sha1).digest()
        hashed = '|1|' + base64.b64encode(salt).decode() + '|' + base64.b64encode(digest).decode()
        original = '2001:db8::2 ' + ' '.join(self.old) + '\n' + hashed + ' ' + ' '.join(self.old) + '\n'
        updated, old = h.replacement(original, name, *self.new)
        self.assertTrue(updated.startswith('2001:db8::2 ' + ' '.join(self.old)))
        last = updated.splitlines()[-1].split()
        self.assertTrue(last[0].startswith('|1|'))
        self.assertTrue(h.matches(last[0], name))
        self.assertFalse(h.matches(last[0], '[2001:db8::2]:22'))
        self.assertEqual(last[1:], list(self.new))

    def test_revoked_ca_and_wildcard_records_are_never_auto_replaced(self):
        for host in ('@revoked node.example.test', '@cert-authority node.example.test', '*.example.test',
                     'node.example.test,!excluded.example.test'):
            with self.subTest(host=host), self.assertRaisesRegex(h.Error, '策略记录'):
                h.replacement(host + ' ' + ' '.join(self.old) + '\n', 'node.example.test', *self.new)

    def test_scan_must_match_observed_fingerprint(self):
        scanned = Mock(stdout='node.example.test ' + ' '.join(self.other) + '\n', returncode=0)
        with patch.object(h.subprocess, 'run', return_value=scanned), self.assertRaisesRegex(h.Error, '不一致'):
            h.scan('node.example.test', 22, 'inet', h.fingerprint(*self.new))
        self.assertEqual(self.known.read_text(), self.original)

    def test_concurrent_known_hosts_edit_is_preserved(self):
        with patch.object(h, 'scan', return_value=self.new):
            def confirm(_):
                self.known.write_text(self.original + 'new-other.example.test ' + ' '.join(self.other) + '\n')
                return 'yes'
            with patch('builtins.input', side_effect=confirm), self.assertRaisesRegex(h.Error, '已改变'):
                self.review()
        self.assertIn('new-other.example.test', self.known.read_text())
        self.assertNotIn('node.example.test ' + ' '.join(self.new), self.known.read_text())

    def test_symlink_target_is_never_overwritten(self):
        target = self.path / 'target'
        target.write_text(self.original)
        self.known.unlink()
        self.known.symlink_to(target)
        with self.assertRaises(h.Error):
            h.replace_known_host(self.known, self.original, 'replacement')
        self.assertEqual(target.read_text(), self.original)


class HostKeyConnectionTests(MonitoringFixture):
    def setup_code(self, mode='interactive'):
        return '''
deploy_work="$TMPDIR/connect"
mkdir "$deploy_work"
deploy_mode=''' + mode + '''
deploy_address=node.example.test; deploy_ssh_port=22; deploy_family=inet
deploy_destination=root@node.example.test
deploy_ssh_args=(-F /dev/null -p 22 -o StrictHostKeyChecking=ask)
ssh() {
    printf '%s\\n' "$*" >> "$CALL_LOG"
    if [ ! -e "$deploy_work/failed" ]; then
        touch "$deploy_work/failed"
        printf 'REMOTE HOST IDENTIFICATION HAS CHANGED!\\n' > "$deploy_work/ssh.log"
        return 255
    fi
    return 0
}
'''

    def test_batch_does_not_prompt_scan_or_modify_host_trust(self):
        result = self.shell(self.setup_code('batch') + '''
python3() { printf 'unexpected repair\\n' >> "$CALL_LOG"; return 99; }
connect_deployment
''', check=False)
        self.assertEqual(result.returncode, 255)
        self.assertNotIn('unexpected repair', self.log.read_text())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_interactive_retry_pins_the_confirmed_key_and_keeps_strict_checking(self):
        self.shell(self.setup_code() + '''
python3() { printf '{"algorithms":"ssh-ed25519"}' > "$deploy_work/hostkey.json"; }
connect_deployment
''')
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn('StrictHostKeyChecking=yes', calls[1])
        self.assertIn('UserKnownHostsFile=' + str(self.path / 'connect/pinned-host'), calls[1])
        self.assertIn('HostKeyAlgorithms=ssh-ed25519', calls[1])
        self.assertNotIn('StrictHostKeyChecking=no', '\n'.join(calls))
