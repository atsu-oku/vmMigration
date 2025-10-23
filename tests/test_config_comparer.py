# -*- coding: utf-8 -*-
import unittest

from config_comparer import (
    ClusterDifferences,
    diff_cluster_status,
    diff_firewalld_ports,
    diff_firewalld_sources,
    diff_iptables_allowed_networks,
)
from firewalld_parser import FirewalldZone
from iptables_parser import IptablesConfig, IptablesChain, IptablesRule
from pcs_parser import ClusterNode, ClusterResource, ClusterStatus


class ConfigComparerTests(unittest.TestCase):
    def test_firewalld_ports_diff(self) -> None:
        zone = FirewalldZone(name="public", ports=["80/tcp"])
        diff = diff_firewalld_ports(zone, ["80/tcp", "443/tcp"])
        self.assertEqual(diff["add"], ["443/tcp"])
        self.assertEqual(diff["remove"], [])

    def test_firewalld_sources_diff(self) -> None:
        zone = FirewalldZone(name="public", sources=["172.16.163.0/24"])
        diff = diff_firewalld_sources(zone, ["172.16.163.0/24", "10.0.0.0/8"])
        self.assertEqual(diff["add"], ["10.0.0.0/8"])

    def test_iptables_diff(self) -> None:
        chain = IptablesChain(name="INPUT", policy="ACCEPT", rules=[IptablesRule(raw="-A INPUT -s 10.0.0.0/8 -j ACCEPT")])
        config = IptablesConfig(table="filter", chains=[chain])
        diff = diff_iptables_allowed_networks(config, ["10.0.0.0/8", "172.16.163.0/24"])
        self.assertEqual(diff["add"], ["172.16.163.0/24"])

    def test_cluster_status_diff(self) -> None:
        current = ClusterStatus(
            cluster_name="prod",
            stonith_enabled=True,
            nodes=[ClusterNode(name="node1", state="Online")],
            resources=[ClusterResource(identifier="vip", resource_type="IP", state="Started")],
        )
        desired = ClusterStatus(
            cluster_name="prod",
            stonith_enabled=False,
            nodes=[ClusterNode(name="node1", state="Online"), ClusterNode(name="node2", state="Online")],
            resources=[
                ClusterResource(identifier="vip", resource_type="IP", state="Started"),
                ClusterResource(identifier="db", resource_type="service", state="Started"),
            ],
        )
        diff = diff_cluster_status(current, desired)
        self.assertEqual(diff.missing_nodes, ["node2"])
        self.assertEqual(diff.missing_resources, ["db"])
        self.assertTrue(diff.stonith_enabled_mismatch)


if __name__ == "__main__":
    unittest.main()
