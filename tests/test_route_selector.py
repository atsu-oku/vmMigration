# -*- coding: utf-8 -*-
import unittest

from route_selector import build_static_route_entries


class RouteSelectorTests(unittest.TestCase):
    def test_build_static_routes(self) -> None:
        raw_routes = [
            {"network": "0.0.0.0", "prefix": 0, "gateway": "172.16.162.1", "owner_index": 0},
            {"network": "172.16.163.0", "prefix": 24, "gateway": "172.16.162.1", "owner_index": 0},
        ]
        nic_records = [
            {"network_name": "PRD-DMZ-Manage-seg"},
            {"network_name": "PRD-DMZ-seg"},
        ]
        routes, defaults, gateway, owner = build_static_route_entries(raw_routes, nic_records)
        self.assertEqual(len(routes), 2)
        self.assertEqual(len(defaults), 1)
        self.assertEqual(gateway, "172.16.162.1")
        self.assertEqual(owner, 0)


if __name__ == "__main__":
    unittest.main()
