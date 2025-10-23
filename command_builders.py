# -*- coding: utf-8 -*-
"""Builders for command strings executed inside the guest."""

from __future__ import annotations

from typing import List


def build_firewall_info_service(service_name: str) -> List[str]:
    return ["firewall-cmd", "--info-service", service_name]


def build_firewall_list_services(zone: str) -> List[str]:
    return ["firewall-cmd", "--zone", zone, "--list-services"]


def build_firewall_list_ports(zone: str) -> List[str]:
    return ["firewall-cmd", "--zone", zone, "--list-ports"]


def build_firewall_list_sources(zone: str) -> List[str]:
    return ["firewall-cmd", "--zone", zone, "--list-sources"]


def build_firewall_reload() -> List[str]:
    return ["firewall-cmd", "--reload"]
def build_firewall_add_port(zone: str, port_spec: str) -> List[str]:
    return ["firewall-cmd", "--zone", zone, "--add-port", port_spec, "--permanent"]


def build_firewall_remove_port(zone: str, port_spec: str) -> List[str]:
    return ["firewall-cmd", "--zone", zone, "--remove-port", port_spec, "--permanent"]
def build_iptables_append(chain: str, rule: str) -> List[str]:
    return ["iptables", "-A", chain] + rule.split()


def build_pcs_resource_create(resource_id: str, resource_type: str, **options) -> List[str]:
    command = ["pcs", "resource", "create", resource_id, resource_type]
    for key, value in options.items():
        command.append(f"{key}={value}")
    return command


def build_pcs_stonith_enable(enable: bool = True) -> List[str]:
    return ["pcs", "property", "set", f"stonith-enabled={'true' if enable else 'false'}"]
