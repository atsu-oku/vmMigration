# -*- coding: utf-8 -*-
import unittest

from iptables_parser import IptablesConfig, parse_iptables_config


class IptablesParserTests(unittest.TestCase):
    def test_allowed_networks(self) -> None:
        lines = [
            "*filter",
            ":INPUT ACCEPT [0:0]",
            "-A INPUT -s 172.16.163.0/24 -j ACCEPT",
            "-A INPUT -s 10.0.0.1 -j ACCEPT",
        ]
        config = parse_iptables_config(lines)
        self.assertIsInstance(config, IptablesConfig)
        self.assertEqual(config.allowed_networks(), ["10.0.0.1/32", "172.16.163.0/24"])


if __name__ == "__main__":
    unittest.main()
