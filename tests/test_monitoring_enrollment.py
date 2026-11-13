"""Display names stay out of paths; new targets are verified without restart."""
import json
import shutil
import unittest

from test_monitoring import ROOT
from test_monitoring_tls import TLSFixture, package


class EnrollmentTests(TLSFixture):
    def setUp(self):
        super().setUp()
        shutil.copyfile(ROOT / "templates/monitoring/prometheus/prometheus.yml", self.hub / "prometheus/prometheus.yml")
        self.display = '香港/CMI.01 "备用"'
        self.key = package.node_name(self.display)["name"]
        self.env["DISPLAY_NAME"] = self.display
        self.env["NODE_KEY"] = self.key

    def test_names_are_stable_and_legacy_ids_stay_unchanged(self):
        for name in (self.display, "美国.cn2 / isp", "../../etc", "/", "x" * 100):
            with self.subTest(name=name):
                value = package.node_name(name)
                self.assertRegex(value["name"], r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
                self.assertEqual(value, package.node_name(name))
                self.assertEqual(value["display_name"], name)
        for name in ("bytevirt-cn2gia", "isp-exit", "Node_01"):
            self.assertEqual(package.node_name(name)["name"], name)
        for name in ("", "  ", "line\nbreak", "line\n", "hidden\x1b", "client", "x" * 129):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package.node_name(name)

    def test_certificate_package_and_target_keep_the_display_name(self):
        self.shell('''
linux_root() { :; }; require_hub() { :; }; chown() { :; }
prepare_node "$DISPLAY_NAME"
enable_https_job
write_https_target "$NODE_KEY"
''', stdin="192.0.2.10\n192.0.2.20\n\n")
        archive = self.hub / "packages" / (self.key + ".tar.gz")
        meta = package.extract(archive, self.path / "unpacked")
        self.assertEqual(meta["name"], self.key)
        self.assertEqual(meta["display_name"], self.display)
        target = json.loads((self.hub / "prometheus/https-targets" / (self.key + ".json")).read_text())
        self.assertEqual(target[0]["labels"]["node"], self.display)
        self.assertEqual([p.name for p in (self.pki / "nodes").iterdir()], [self.key])
        self.shell('validate_bundle_certificates "$PKI_DIR/nodes/$NODE_KEY"')
        bad = dict(meta, name="../../escaped")
        with self.assertRaises(ValueError):
            package.metadata(bad)
        with self.assertRaises(ValueError):
            package.metadata(dict(meta, display_name="different/name"))

    def test_deployment_wizard_preserves_name_and_removal_accepts_it(self):
        self.shell('''
linux_root() { :; }; require_hub() { :; }; confirm() { return 0; }
package_meta() {
    if [ "$1" = resolve ]; then printf '["192.0.2.10"]\\n';
    else python3 "$ROOT_DIR/lib/monitor/package.py" "$@"; fi
}
apply_deployment() { printf '%s' "$1" > "$MONITOR_DIR/captured.json"; }
deploy_node
''', stdin=self.display + "\nnode.example.test\n192.0.2.20\n\n\n\n\n")
        meta = json.loads((self.hub / "captured.json").read_text())["node"]
        self.assertEqual((meta["name"], meta["display_name"]), (self.key, self.display))
        (self.hub / "prometheus/https-targets").mkdir()
        target = self.hub / "prometheus/https-targets" / (self.key + ".json")
        target.write_text("[]")
        self.shell('linux_root() { :; }; require_hub() { :; }; confirm() { return 0; }; remove_node', stdin=self.display + "\n")
        self.assertFalse(target.exists())

    def test_renewal_accepts_display_name(self):
        (self.hub / "deploy").mkdir()
        record = self.hub / "deploy" / (self.key + ".json")
        record.write_text("{}")
        r = self.shell('''
linux_root() { :; }; require_hub() { :; }; confirm() { return 0; }
renew_managed_node() { printf '%s\\n' "$1"; }
renew_node
''', stdin=self.display + "\n")
        self.assertIn(str(record), r.stdout)

    def test_existing_job_uses_discovery_without_reload_or_restart(self):
        self.shell('''
enable_https_job
compose() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
prometheus_wait_ready() { return 0; }; https_job_loaded() { return 0; }
activate_https_job
''')
        calls = self.log.read_text()
        self.assertIn("promtool check config", calls)
        self.assertNotIn("kill", calls)
        self.assertNotIn("restart", calls)

    def test_first_job_is_hot_loaded_without_restarting_other_services(self):
        self.shell('''
compose() {
    printf '%s\\n' "$*" >> "$CALL_LOG"
    case "$1" in kill) touch "$MONITOR_DIR/job-loaded";; esac
}
prometheus_wait_ready() { return 0; }
https_job_loaded() { test -f "$MONITOR_DIR/job-loaded"; }
activate_https_job
''')
        calls = self.log.read_text()
        self.assertEqual(calls.count("kill --signal SIGHUP prometheus"), 1)
        self.assertNotIn("restart", calls)
        self.assertNotIn("grafana", calls)

    def test_failed_signal_restores_previous_disk_configuration(self):
        original = (self.hub / "prometheus/prometheus.yml").read_text()
        r = self.shell('''
compose() { case "$1" in kill) return 1;; esac; }
prometheus_wait_ready() { return 0; }
activate_https_job
''', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.hub / "prometheus/prometheus.yml").read_text(), original)
        self.assertFalse((self.hub / "prometheus/https/remote.yml").exists())

    def test_display_name_is_safely_quoted_in_scrape_confirmation(self):
        directory = self.hub / "prometheus/https-targets"
        directory.mkdir()
        (directory / (self.key + ".json")).write_text(json.dumps([
            {"targets": ["node.example.test:19100"], "labels": {"node": self.display}}]))
        self.shell('''
prometheus_api() {
    printf '%s\\n' "$*" > "$MONITOR_DIR/query"
    printf '{"status":"success","data":{"result":[{"value":[1,"1"]}]}}'
}
wait_node_scrape "$NODE_KEY"
''')
        query = (self.hub / "query").read_text()
        self.assertIn(json.dumps(self.display, ensure_ascii=False), query)
        self.assertIn("timestamp(up{", query)
        self.assertIn('instance="node.example.test:19100"', query)


if __name__ == "__main__":
    unittest.main()
