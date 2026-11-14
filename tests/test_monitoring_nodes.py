"""Node lifecycle across rename, pause, redeploy and delete; no real hosts."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib/monitor'))
spec = importlib.util.spec_from_file_location('monitor_nodes', ROOT / 'lib/monitor/nodes.py')
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)


class NodesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.nodes = nodes.Nodes(self.root)
        self.target = self.root / 'prometheus/https-targets/isp-exit.json'
        nodes.write(self.target, [{'targets': ['old.example.test:19100'], 'labels': {'node': 'isp-exit'}}], 0o644)
        nodes.write(self.root / 'deploy/isp-exit.json', {'original': 'certificate identity'})

    def test_rename_pause_resume_delete_preserve_identity_and_clear_renewal(self):
        self.nodes.rename('isp-exit', '美国/ISP.01')
        self.assertEqual(self.nodes.resolve('美国/ISP.01'), 'isp-exit')
        self.assertEqual(self.nodes.resolve('isp-exit'), 'isp-exit')
        self.assertEqual(json.loads(self.target.read_text())[0]['labels']['node'], '美国/ISP.01')
        self.nodes.pause('isp-exit', True)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.nodes.get('isp-exit')['paused'])
        self.nodes.rename('isp-exit', '备用 / ISP')
        self.nodes.pause('isp-exit', False)
        self.assertEqual(json.loads(self.target.read_text())[0]['labels']['node'], '备用 / ISP')
        self.assertFalse(self.nodes.get('isp-exit')['paused'])
        self.nodes.pause('isp-exit', True)
        self.nodes.forget('isp-exit')
        self.assertEqual(self.nodes.inventory(), [])
        self.assertFalse((self.root / 'deploy/isp-exit.json').exists())
        self.assertFalse(any(p.exists() for p in self.nodes.paused_paths('isp-exit')))

    def test_redeploy_updates_address_keeps_alias_and_resumes(self):
        self.nodes.rename('isp-exit', '上海 / 新名称')
        self.nodes.pause('isp-exit', True)
        nodes.write(self.target, [{'targets': ['new.example.test:19100'], 'labels': {'node': 'isp-exit'}}])
        self.nodes.relabel('isp-exit')
        item = self.nodes.get('isp-exit')
        self.assertEqual(item['label'], '上海 / 新名称')
        self.assertEqual(item['address'], 'new.example.test:19100')
        self.assertFalse(item['paused'])
        self.assertFalse(any(p.exists() for p in self.nodes.paused_paths('isp-exit')))

    def test_rename_rejects_duplicate_control_characters_and_traversal_id(self):
        nodes.write(self.root / 'prometheus/targets/other.json', [{'targets': [], 'labels': {'node': '已有'}}])
        for label in ('已有', 'other', 'bad\x1b[2J', 'bad\nname'):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.nodes.rename('isp-exit', label)
        with self.assertRaises(ValueError):
            self.nodes.forget('../escape')

    def test_failed_registry_write_rolls_back_target_move(self):
        before = self.target.read_bytes()
        with patch.object(nodes, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.nodes.pause('isp-exit', True)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(any(p.exists() for p in self.nodes.paused_paths('isp-exit')))

    def test_corrupt_target_is_visible_and_can_be_removed(self):
        self.target.write_text('broken')
        self.assertIn('配置损坏', self.nodes.inventory()[0]['address'])
        self.nodes.forget('isp-exit')
        self.assertEqual(self.nodes.inventory(), [])

    def test_state_panel_uses_instant_but_trends_keep_range(self):
        dashboard = json.loads((ROOT / 'templates/monitoring/grafana/provisioning/dashboards/overview.json').read_text())
        target = dashboard['panels'][0]['targets'][0]
        self.assertTrue(target['instant'])
        self.assertFalse(target['range'])
        self.assertTrue(dashboard['panels'][1]['targets'][0]['range'])


if __name__ == '__main__':
    unittest.main()
