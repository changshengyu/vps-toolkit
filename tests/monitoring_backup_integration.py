#!/usr/bin/env python3
"""Real Grafana -> online SQLite/age backup -> fresh named volume -> authenticated API.

Only a unique disposable Docker project is used. No Telegram or Cloudflare
account is contacted. The contact point uses an unreachable local webhook.
"""
import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('backup', ROOT / 'lib/monitor/backup.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def main():
    project = 'vt-backup-check-' + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='vt-backup-real-') as tmp:
        directory = Path(tmp)
        hub, data = directory / 'hub', directory / 'grafana-data'
        hub.mkdir()
        data.mkdir(mode=0o777)
        os.chmod(data, 0o777)
        config = directory / 'backup-config'
        storage = directory / 'storage'
        job = b.Backup(hub, config, storage, directory / 'systemd')
        (hub / '.vps-toolkit-managed').touch()
        document = {'name': project, 'services': {'grafana': {
            'image': 'grafana/grafana:13.3.1', 'ports': ['127.0.0.1::3000'],
            'user': '472:0' if os.geteuid() == 0 else f'{os.getuid()}:{os.getgid()}',
            'environment': {'GF_SECURITY_ADMIN_PASSWORD': password, 'GF_SECURITY_SECRET_KEY': 'integration-secret-key',
                            'GF_UNIFIED_ALERTING_EXECUTE_ALERTS': 'false', 'GF_PLUGINS_PREINSTALL_DISABLED': 'true',
                            'GF_ANALYTICS_CHECK_FOR_UPDATES': 'false', 'GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES': 'false'},
            'volumes': [str(data) + ':/var/lib/grafana']}}, 'volumes': {'grafana-data': {}}}
        (hub / 'compose.yaml').write_text(json.dumps(document))
        docker = b.Docker(hub)
        auth = 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode()
        def endpoint():
            return 'http://' + docker.compose('port', 'grafana', '3000').decode().strip()
        def api(origin, path, method='GET', body=None):
            req = urllib.request.Request(origin + path, method=method,
                headers={'Authorization': auth, 'Content-Type': 'application/json'},
                data=json.dumps(body).encode() if body is not None else None)
            with opener.open(req, timeout=10) as response:
                return json.load(response)
        def healthy():
            origin = endpoint()
            # Linux test container can reach sibling published ports through the host gateway.
            if os.environ.get('BACKUP_TEST_HOST'):
                origin = origin.replace('127.0.0.1', os.environ['BACKUP_TEST_HOST'])
            for _ in range(180):
                try:
                    if api(origin, '/api/health')['database'] == 'ok':
                        return origin
                except Exception:
                    time.sleep(1)
            raise AssertionError('Grafana failed to become healthy')
        try:
            docker.compose('up', '-d')
            origin = healthy()
            api(origin, '/api/v1/provisioning/contact-points', 'POST', {
                'uid': 'backup-test-contact', 'name': 'telegram_bot', 'type': 'webhook',
                'settings': {'url': 'http://127.0.0.1:1/no-delivery'}})
            api(origin, '/api/dashboards/db', 'POST', {'dashboard': {'id': None, 'uid': 'backup-test-dashboard',
                'title': 'Restored dashboard', 'schemaVersion': 39, 'panels': []}})
            api(origin, '/api/folders', 'POST', {'uid': 'backup-alert-folder', 'title': 'Backup alerts'})
            api(origin, '/api/v1/provisioning/alert-rules', 'POST', {
                'uid': 'backup-test-rule', 'title': 'Restored alert', 'folderUID': 'backup-alert-folder',
                'ruleGroup': 'test-group', 'condition': 'A', 'for': '2m', 'noDataState': 'NoData', 'execErrState': 'Error',
                'notification_settings': {'receiver': 'telegram_bot'}, 'data': [{
                    'refId': 'A', 'datasourceUid': '__expr__', 'relativeTimeRange': {'from': 0, 'to': 0},
                    'model': {'refId': 'A', 'type': 'math', 'expression': '1',
                              'datasource': {'type': '__expr__', 'uid': '__expr__'}}}]})
            # Bind mount exposes the live DB to this process. The recovery fixture
            # uses the standard named-volume layout, with a fresh volume on restore.
            document['services']['grafana']['volumes'] = ['grafana-data:/var/lib/grafana']
            (hub / 'compose.yaml').write_text(json.dumps(document))
            key = directory / 'offline-recovery-key.txt'
            b.run(['age-keygen', '-o', key])
            recipient = b.run(['age-keygen', '-y', key]).decode().strip()
            cfg = {'schema': b.FORMAT, 'token': '123456789:abcdefghijklmnopqrstuvw_test', 'chat_id': '123456789',
                   'recipient': recipient, 'time': '04:00'}
            b.save(job.config_file, cfg)
            grafana_dir, container = docker.grafana()
            manifest, parts = job.create(cfg, grafana_dir, container)
            docker.compose('down', '--volumes', '--remove-orphans')
            hub.rename(directory / 'old-hub')
            config.rename(directory / 'old-backup-config')
            # Restore must create a fresh volume and start Grafana with the same DB.
            # No host systemd changes or host SSH trust changes in this fixture.
            with patch.object(job, 'install_timer'), patch('builtins.input', return_value='RESTORE'):
                b.restore(job, manifest, key)
            origin = healthy()
            assert api(origin, '/api/user')['login'] == 'admin'
            assert api(origin, '/api/dashboards/uid/backup-test-dashboard')['dashboard']['title'] == 'Restored dashboard'
            contacts = api(origin, '/api/v1/provisioning/contact-points')
            assert any(c['name'] == 'telegram_bot' for c in contacts)
            assert api(origin, '/api/v1/provisioning/alert-rules/backup-test-rule')['title'] == 'Restored alert'
            mount = docker.inspect('grafana')['Mounts'][0]
            assert mount['Type'] == 'volume'
            assert not (hub / '.restore-incomplete').exists()
            print('PASS: real Grafana live SQLite snapshot, age encryption, fresh Docker volume restore, dashboard, contact point and login')
        except Exception:
            with contextlib.suppress(Exception):
                print(docker.compose('logs', '--tail=50').decode())
            raise
        finally:
            with contextlib.suppress(Exception):
                docker.compose('down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    main()
