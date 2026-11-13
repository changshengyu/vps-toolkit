"""Static site scope, HTTPS-only config, ownership, DNS and safe page replacement."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_module', ROOT / 'lib/sni/site.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = s.Paths(self.root)
        self.site = s.Site(self.paths)

    def owned(self):
        for p in self.paths.directories():
            p.mkdir(parents=True)
            (p / '.vps-toolkit-owned').write_text(s.MARKER)
        s.json_save(self.paths.state, {'managed_by': s.MARKER, 'domain': 'site.example.com',
                                    'email': '', 'title': 'Test', 'version': s.VERSION})

    def test_only_https_tlsalpn_and_local_unix_administration(self):
        value = s.configuration('MyWeb.Example.COM.', self.paths)
        server = value['apps']['http']['servers']['site']
        self.assertEqual(server['listen'], [':443'])
        self.assertEqual(server['protocols'], ['h1', 'h2'])
        self.assertTrue(server['automatic_https']['disable_redirects'])
        self.assertTrue(value['admin']['listen'].startswith('unix//'))
        issuer = value['apps']['tls']['automation']['policies'][0]['issuers'][0]
        self.assertTrue(issuer['challenges']['http']['disabled'])
        self.assertNotIn('dns', issuer['challenges'])
        self.assertEqual(server['routes'][0]['match'][0]['host'], ['myweb.example.com'])
        self.assertEqual(server['routes'][0]['match'][0]['path'], ['/', '/index.html'])
        self.assertEqual(server['routes'][1]['handle'][0]['status_code'], 404)

    def test_domain_injection_and_local_names_rejected(self):
        for value in ('https://site.example.com', 'site.example.com:443', 'example.com/x', 'localhost',
                      '127.0.0.1', '*.example.com', 'example.com\nadmin :2019', 'host.test'):
            with self.subTest(domain=value), self.assertRaises(s.Error):
                s.domain_name(value)

    def test_site_title_is_html_escaped_and_has_no_external_assets(self):
        page = s.render_page('<script>"test"</script>')
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('src="http', page)
        self.assertNotIn('href="http', page)

    def test_existing_unmanaged_directory_and_service_are_not_overwritten(self):
        self.paths.config.mkdir(parents=True)
        (self.paths.config / 'other.conf').write_text('keep')
        with self.assertRaisesRegex(s.Error, '不属于'):
            self.site.owned_paths()
        self.assertEqual((self.paths.config / 'other.conf').read_text(), 'keep')
        (self.paths.config / '.vps-toolkit-owned').write_text(s.MARKER)
        self.paths.unit.parent.mkdir(parents=True)
        self.paths.unit.write_text('[Service]\nExecStart=/another/app\n')
        with self.assertRaisesRegex(s.Error, '服务不属于'):
            self.site.owned_paths()

    def test_port_conflict_aborts_before_any_stop_or_install(self):
        with patch.object(s, 'command', return_value='LISTEN 0 511 *:443 users:(("xray",pid=42))') as cmd:
            with self.assertRaisesRegex(s.Error, '443 已占用'):
                s.port_free()
        cmd.assert_called_once_with(['ss', '-lntHp', 'sport = :443'])
        self.assertFalse(self.paths.config.exists())

    def test_group_writable_binary_is_not_executed_as_root(self):
        self.owned()
        self.paths.binary.write_text('untrusted executable')
        self.paths.binary.chmod(0o777)
        with self.assertRaisesRegex(s.Error, '写入权限不安全'):
            self.site.owned_paths()

    def test_dns_rejects_stale_aaaa_and_proxy_addresses(self):
        replies = ['["8.8.8.8", "2606:4700::1111"]',
                   '[{"addr_info":[{"local":"8.8.8.8"}]}]', '2001:4860:4860::8888']
        with patch.object(s, 'command', side_effect=replies), self.assertRaisesRegex(s.Error, '部分 A/AAAA'):
            s.dns_preflight('site.example.com')

    def test_known_public_interface_does_not_need_egress_lookup(self):
        replies = ['["8.8.8.8"]', '[{"addr_info":[{"local":"8.8.8.8"}]}]']
        with patch.object(s, 'command', side_effect=replies) as cmd:
            self.assertEqual(s.dns_preflight('site.example.com'), ['8.8.8.8'])
        self.assertEqual(cmd.call_count, 2)

    def test_scope_filtered_empty_entries_and_multiple_interfaces(self):
        interfaces = [{'ifname': 'eth0', 'addr_info': [{'family': 'inet', 'local': '8.8.8.8'}, {}]},
                      {'ifname': 'eth1', 'addr_info': [{'family': 'inet', 'local': '10.0.161.20'}, {}]}]
        replies = ['["8.8.8.8"]', json.dumps(interfaces)]
        with patch.object(s, 'command', side_effect=replies) as cmd:
            self.assertEqual(s.dns_preflight('site.example.com'), ['8.8.8.8'])
        self.assertEqual(cmd.call_count, 2)  # No unnecessary external IP service.

    def test_ipv6_interface_is_normalized_while_empty_placeholders_are_ignored(self):
        replies = ['["2001:4860:4860::8888"]',
                   '[{"addr_info":[{}, {"local":"2001:4860:4860:0:0:0:0:8888"}, {"local":null}]}]']
        with patch.object(s, 'command', side_effect=replies) as cmd:
            self.assertEqual(s.dns_preflight('site.example.com'), ['2001:4860:4860::8888'])
        self.assertEqual(cmd.call_count, 2)

    def test_empty_interface_output_has_actionable_error(self):
        replies = ['["8.8.8.8"]', '[{"addr_info":[{}, {"local":null}]}]']
        with patch.object(s, 'command', side_effect=replies), self.assertRaisesRegex(s.Error, '未返回有效'):
            s.dns_preflight('site.example.com')

    def test_import_html_preserves_previous_page_without_restart(self):
        self.owned()
        target = self.paths.web / 'index.html'
        target.write_text('<html>old</html>')
        source = self.root / 'new.html'
        source.write_text('<html>new</html>')
        with patch('builtins.input', return_value=str(source)), patch.object(s, 'command') as cmd:
            self.site.import_page()
        cmd.assert_not_called()
        self.assertEqual(target.read_text(), '<html>new</html>')
        backup = next(self.paths.config.glob('index-backup-*.html'))
        self.assertEqual(backup.read_text(), '<html>old</html>')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        source.write_text('-----BEGIN PRIVATE KEY-----\nnot an HTML page')
        with patch('builtins.input', return_value=str(source)), self.assertRaisesRegex(s.Error, '完整 HTML'):
            self.site.import_page()
        self.assertEqual(target.read_text(), '<html>new</html>')

    def test_disabled_site_checks_port_before_start_and_preserves_cert_storage(self):
        self.owned()
        secret = self.paths.data / 'certificate-key'
        secret.write_text('keep')
        with patch.object(s.subprocess, 'run', return_value=Mock(returncode=3)), \
                patch.object(s, 'port_free') as check, patch.object(s, 'command') as cmd:
            self.site.manage('start')
        check.assert_called_once()
        self.assertEqual(cmd.call_args_list[-1].args[0], ['systemctl', 'start', s.SERVICE])
        self.assertEqual(secret.read_text(), 'keep')

    def test_unit_is_dedicated_unprivileged_and_reloads_only_this_site(self):
        text = s.service_unit(self.paths)
        self.assertIn('User=vps-sni-web', text)
        self.assertIn('CAP_NET_BIND_SERVICE', text)
        self.assertIn('WantedBy=multi-user.target', text)
        self.assertIn('StateDirectoryMode=0700', text)
        self.assertIn('unix/', text)
        self.assertNotIn('x-ui', text)
        self.assertNotIn('nginx', text)
        self.assertIn('MemoryMax=192M', text)

    def test_checksum_failure_does_not_install_binary(self):
        path = self.root / 'caddy'
        def download(args, **kwargs):
            Path(args[args.index('--output') + 1]).write_bytes(b'wrong archive')
            return ''
        with patch.object(s, 'command', side_effect=download), self.assertRaisesRegex(s.Error, '校验失败'):
            s.download_binary(path, machine='x86_64')
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
