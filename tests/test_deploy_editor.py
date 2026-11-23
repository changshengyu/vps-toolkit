"""Parameter editing with real collection/rendering and isolated fake apply steps."""
import base64
import json
import os
import pty
import select
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        shutil.copytree(ROOT / 'lib', self.repo / 'lib')
        shutil.copytree(ROOT / 'templates', self.repo / 'templates')
        (self.repo / 'scripts').mkdir()
        script = (ROOT / 'scripts/deploy.sh').read_text()
        self.script = self.repo / 'scripts/deploy.sh'
        self.script.write_text(script.rsplit('main "$@"', 1)[0])
        self.home = self.root / 'deploy'
        self.home.mkdir()
        self.state = self.home / 'state.env'
        self.values = {
            'REALITY_DOMAIN': 'sni.example.test', 'REALITY_TARGET': 'sni.example.test:443',
            'HY2_DOMAIN': 'old.example.test', 'CDN_DOMAIN': 'cdn.example.test',
            'CF_TOKEN': 'old-private-cf-token-123456', 'HY2_CERT_MODE': 'http', 'ACME_EMAIL': '',
            'NODE_NAME': 'old-node', 'ENABLE_AI_ROUTE': 'y', 'SS_ISP_ADDR': 'isp.example.test',
            'SS_ISP_PORT': '8388', 'SS_ISP_METHOD': '2022-blake3-aes-256-gcm',
            'SS_ISP_PASS': base64.b64encode(b'1' * 32).decode(), 'PANEL_PORT': '12247',
            'PANEL_USER': 'admin', 'PANEL_PASS': 'old-panel-password', 'PANEL_PATH': 'old-path',
            'HY2_PORT': '443', 'HOP_RANGE': '20000-20100', 'REALITY_PORT': '443',
            'XHTTP_PORT': '30115', 'CDN_PORT': '8443', 'CDN_WS_PATH': '/ws-old',
            'XHTTP_PATH': '/xhttp-old', 'CLIENT_EMAIL': 'client-old', 'GEODATA_CRON': '0 20 * * *',
            'CLIENT_UUID': 'd94030c8-1af7-44ab-9b85-9d08d08e3fac', 'SUB_ID': 'old-subscription',
            'HY2_AUTH': 'old-hy-auth', 'SALAMANDER_PASS': 'old-salamander',
            'REALITY_TCP_PRIV': 'old-tcp-private', 'REALITY_TCP_PUB': 'old-tcp-public',
            'REALITY_TCP_SIDS': 'abcdef1234567890', 'REALITY_XHTTP_PRIV': 'old-http-private',
            'REALITY_XHTTP_PUB': 'old-http-public', 'REALITY_XHTTP_SIDS': 'abcdef1234567890',
            'VLESS_DEC': 'old-vless-dec', 'VLESS_ENC': 'old-vless-enc', 'PANEL_INSTALLED_BY_US': '1'}
        self.state.write_text(''.join(k + '=' + v + '\n' for k, v in self.values.items()))
        self.original = self.state.read_bytes()
        self.env = {**os.environ, 'DEPLOY_HOME': str(self.home), 'EDIT_TEST_SCRIPT': str(self.script),
                    'OUT_DIR_TEST': str(self.root / 'preview'), 'XUI_BIN': str(self.root / 'no-xui')}
        for key in self.values:
            self.env.pop(key, None)

    def shell(self, code, stdin='', check=True):
        result = subprocess.run(['bash', '-c', '. "$EDIT_TEST_SCRIPT"\nINTERACTIVE=1\n' + code],
            input=stdin, text=True, capture_output=True, env=self.env, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_edit_selected_token_and_domain_renders_new_values_preserves_keys_and_state(self):
        result = self.shell('main --dry-run --edit --out "$OUT_DIR_TEST"',
            'CF_TOKEN\nnew-private-cf-token-654321\nHY2_DOMAIN\nnew.example.test\n0\n')
        self.assertNotIn('new-private-cf-token-654321', result.stdout + result.stderr)
        self.assertNotIn(self.values['CF_TOKEN'], result.stdout + result.stderr)
        self.assertIn('new.example.test', result.stdout)
        self.assertIn('本次修改', result.stdout)
        self.assertEqual(self.state.read_bytes(), self.original)
        payloads = list((self.root / 'preview').rglob('*.json'))
        self.assertTrue(payloads)
        combined = '\n'.join(p.read_text() for p in payloads)
        self.assertIn(self.values['CLIENT_UUID'], combined)
        self.assertIn(self.values['REALITY_TCP_PRIV'], combined)
        self.assertIn('new.example.test', (self.root / 'preview/plan.txt').read_text())

    def test_cancel_or_eof_never_calls_apply_or_writes_state(self):
        for value in ('q\n', ''):
            with self.subTest(value=value):
                result = self.shell('require_root() { :; }; run_deploy() { touch "$DEPLOY_HOME/applied"; }; main --edit', value)
                self.assertIn('已取消', result.stdout)
                self.assertFalse((self.home / 'applied').exists())
                self.assertEqual(self.state.read_bytes(), self.original)

    def test_numbered_edit_and_plan_confirmation_precede_application(self):
        result = self.shell('''
require_root() { :; }
run_deploy() {
    [ "$CDN_DOMAIN" = new-cdn.example.test ]
    [ "$REPLACE" = y ]
    generate_secrets
    save_state "$STATE_FILE"
    touch "$DEPLOY_HOME/applied"
}
main --replace --edit
''', '4\nnew-cdn.example.test\n0\ny\n')
        self.assertTrue((self.home / 'applied').exists())
        saved = dict(line.split('=', 1) for line in self.state.read_text().splitlines() if line and not line.startswith('#'))
        self.assertEqual(saved['CDN_DOMAIN'], 'new-cdn.example.test')
        self.assertEqual(saved['CLIENT_UUID'], self.values['CLIENT_UUID'])
        self.assertIn('new-cdn.example.test', result.stdout)

    def test_existing_panel_fields_are_explicitly_readonly(self):
        result = self.shell('''
panel_installed() { return 0; }
detect_existing_panel() { PANEL_INSTALLED=1; }
main --dry-run --edit --out "$OUT_DIR_TEST"
''', 'PANEL_PORT\n0\n')
        self.assertIn('[只读]', result.stdout)
        self.assertIn('由 3x-ui 管理', result.stdout)
        self.assertIn('该值来自现有', result.stderr)
        self.assertIn('12247', result.stdout)

    def test_ai_enable_after_collection_fills_defaults_and_checks_missing_secrets(self):
        self.values['ENABLE_AI_ROUTE'] = 'n'
        for key in ('SS_ISP_METHOD', 'SS_ISP_ADDR', 'SS_ISP_PORT', 'SS_ISP_PASS'):
            self.values.pop(key)
        self.state.write_text(''.join(k + '=' + v + '\n' for k, v in self.values.items()))
        result = self.shell('main --dry-run --edit --out "$OUT_DIR_TEST"',
            'ENABLE_AI_ROUTE\ny\n0\nSS_ISP_ADDR\nisp.example.test\nSS_ISP_PORT\n8388\nSS_ISP_PASS\n'
            + base64.b64encode(b'2' * 32).decode() + '\n0\n')
        self.assertIn('SS_ISP_PASS 不能为空', result.stderr)
        self.assertIn('2022-blake3-aes-256-gcm', result.stdout)

    def test_config_only_save_keeps_unrelated_keys_and_does_not_save_on_failure(self):
        code = '''
require_root() { :; }; panel_installed() { return 0; }; detect_existing_panel() { :; }
HY2_DOMAIN=old.example.test
panel_cert_domain() { printf old.example.test; }
init_api() { :; }; wait_panel_ready() { :; }; backup_xray_template() { printf backup; }
update_xray_template() { :; }; restart_xray() { :; }; configure_subscription() { :; }
main --config-only --edit
'''
        self.shell(code, 'SS_ISP_PORT\n9443\n0\ny\n')
        saved = dict(line.split('=', 1) for line in self.state.read_text().splitlines() if line and not line.startswith('#'))
        self.assertEqual(saved['SS_ISP_PORT'], '9443')
        for key in ('CLIENT_UUID', 'REALITY_TCP_PRIV', 'PANEL_PASS', 'CF_TOKEN'):
            self.assertEqual(saved[key], self.values[key])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        previous = self.state.read_bytes()
        result = self.shell(code.replace('restart_xray() { :; }', 'restart_xray() { return 1; }'),
            'SS_ISP_PORT\n9553\n0\ny\n', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state.read_bytes(), previous)

    def test_sni_target_change_requires_explicit_confirmation(self):
        for confirm, expected in [('y', 'new-sni.example.test:443'), ('n', 'sni.example.test:443')]:
            with self.subTest(confirm=confirm):
                result = self.shell('main --dry-run --edit --out "$OUT_DIR_TEST"',
                    'REALITY_DOMAIN\nnew-sni.example.test\n' + confirm + '\n0\n')
                self.assertIn(expected, result.stdout)

    def test_cli_without_edit_remains_noninteractive_and_edit_rejects_non_tty(self):
        self.shell('INTERACTIVE=0; main --dry-run --out "$OUT_DIR_TEST"')
        result = self.shell('INTERACTIVE=0; main --dry-run --edit', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('需要在终端', result.stderr)

    def test_declining_plan_does_not_apply_edited_parameters(self):
        result = self.shell('require_root() { :; }; run_deploy() { touch "$DEPLOY_HOME/applied"; }; main --edit',
            'CDN_DOMAIN\nnew-cdn.example.test\n0\nn\n')
        self.assertIn('new-cdn.example.test', result.stdout)
        self.assertFalse((self.home / 'applied').exists())
        self.assertEqual(self.state.read_bytes(), self.original)

    def test_real_terminal_does_not_echo_replacement_token(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen(['bash', '-c', '. "$EDIT_TEST_SCRIPT"; main --dry-run --edit'],
            stdin=slave, stdout=slave, stderr=slave, env=self.env)
        os.close(slave)
        output = b''

        def receive(marker):
            nonlocal output
            block = b''
            deadline = time.monotonic() + 8
            while marker not in block and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        piece = os.read(master, 65536)
                    except OSError:
                        break
                    if not piece:
                        break
                    block += piece
                    output += piece
            self.assertIn(marker, block, block.decode(errors='replace'))

        try:
            receive('选择参数 [0 完成 / q 取消]: '.encode())
            os.write(master, b'CF_TOKEN\n')
            receive('位）]: '.encode())
            os.write(master, b'never-echo-this-new-token-987654\n')
            receive('选择参数 [0 完成 / q 取消]: '.encode())
            os.write(master, b'q\n')
            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn(b'never-echo-this-new-token-987654', output)
            self.assertEqual(self.state.read_bytes(), self.original)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)


if __name__ == '__main__':
    unittest.main()
