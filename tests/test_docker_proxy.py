import subprocess
import unittest
from unittest.mock import patch

from lib.onepanel import docker_proxy as proxy


class DockerProxyTests(unittest.TestCase):
    def run_wizard(self, *, reply="y", address_changed=False):
        endpoint = ("br-example", "172.18.0.4", "172.18.0.1")
        after = ("br-example", "172.18.0.5", "172.18.0.1") if address_changed else endpoint
        with patch.object(proxy, "output", return_value="Status: active"), \
             patch.object(proxy, "docker", return_value="sub2api abc"), \
             patch.object(proxy, "networks", return_value={"1panel-network": {"IPAddress": endpoint[1], "Gateway": endpoint[2]}}), \
             patch.object(proxy, "bridge_endpoint", side_effect=[endpoint, after]), \
             patch("builtins.input", side_effect=["", reply]), patch("builtins.print"):
            proxy.main()

    @patch.object(subprocess, "run")
    def test_rule_is_limited_to_endpoint(self, run):
        self.run_wizard()
        run.assert_called_once_with(["ufw", "allow", "in", "on", "br-example", "proto", "tcp",
                                     "from", "172.18.0.4", "to", "172.18.0.1", "port", "10809"], check=True)

    @patch.object(subprocess, "run")
    def test_cancel_or_changed_address_never_applies(self, run):
        self.run_wizard(reply="n")
        with self.assertRaises(ValueError):
            self.run_wizard(address_changed=True)
        run.assert_not_called()

    def test_bridge_resolution_checks_local_gateway(self):
        ep = {"NetworkID": "id", "IPAddress": "172.18.0.4", "Gateway": "172.18.0.1"}
        with patch.object(proxy, "networks", return_value={"net": ep}), \
             patch.object(proxy, "docker", return_value="bridge"), \
             patch.object(proxy, "output", side_effect=['[{"dev":"br0"}]', '[{"addr_info":[{"local":"172.18.0.1"}]}]',
                                                         '[{"dev":"eth0"}]', '[{"addr_info":[{"local":"192.0.2.1"}]}]']):
            self.assertEqual(proxy.bridge_endpoint("abc", "net"), ("br0", "172.18.0.4", "172.18.0.1"))
            with self.assertRaises(ValueError):
                proxy.bridge_endpoint("abc", "net")
