# -*- coding: utf-8 -*-
import unittest
from unittest import mock

from firewalld_parser import FirewalldZone, parse_firewalld_zone, resolve_service_ports

ZONE_XML = """<zone name=\"public\">\n  <interface name=\"ens160\"/>\n  <source address=\"172.16.163.0/24\"/>\n  <rule family=\"ipv4\"><source address=\"10.0.0.1\"/>\n    <port protocol=\"tcp\" port=\"22\"/>\n    <accept/>\n  </rule>\n</zone>"""


class FirewalldParserTests(unittest.TestCase):
    def test_parse_zone(self) -> None:
        zone = parse_firewalld_zone(ZONE_XML)
        self.assertIsInstance(zone, FirewalldZone)
        self.assertEqual(zone.allowed_networks(), ["10.0.0.1/32", "172.16.163.0/24"])

    def test_resolve_service_ports(self) -> None:
        executor = mock.Mock(return_value=(0, "ports: 80/tcp 443/tcp\n", ""))
        ports = resolve_service_ports("https", executor)
        executor.assert_called_once()
        self.assertEqual(ports, ["80/tcp", "443/tcp"])


if __name__ == "__main__":
    unittest.main()
