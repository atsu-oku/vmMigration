# -*- coding: utf-8 -*-
import unittest

from pcs_parser import ClusterStatus, parse_cluster_status

SAMPLE = """{
  \"cluster_name\": \"prod-cluster\",
  \"stonith_enabled\": true,
  \"stonith_resources\": [
    {\"id\": \"stonith-node1\", \"type\": \"fence_ipmilan\", \"state\": \"Started\"}
  ],
  \"nodes\": [
    {\"name\": \"node1\", \"state\": \"Online\"},
    {\"name\": \"node2\", \"state\": \"Online\"}
  ],
  \"resources\": [
    {\"id\": \"vip\", \"type\": \"ocf:heartbeat:IPaddr2\", \"state\": \"Started\"}
  ]
}\n"""


class PcsParserTests(unittest.TestCase):
    def test_parse_cluster_status(self) -> None:
        status = parse_cluster_status(SAMPLE)
        self.assertIsInstance(status, ClusterStatus)
        self.assertTrue(status.stonith_enabled)
        self.assertEqual(len(status.nodes), 2)
        self.assertEqual(status.stonith_resources[0].resource_type, "fence_ipmilan")


if __name__ == "__main__":
    unittest.main()
