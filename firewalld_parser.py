# -*- coding: utf-8 -*-
"""Parsing helpers for firewalld zone definitions."""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, List

from command_builders import (
    build_firewall_info_service,
    build_firewall_add_port,
    build_firewall_remove_port,
)
from guest_command_utils import run_guest_command, GuestCommandResult, GuestCommandExecutor


def _normalize_cidr(value: str) -> str:
    """Ensure value is expressed as CIDR notation."""
    value = value.strip().split()[0]
    if "/" in value:
        return value
    try:
        ip = ipaddress.IPv4Address(value)
        return f"{ip}/32"
    except ipaddress.AddressValueError:
        return value


@dataclass
class FirewalldZone:
    name: str
    interfaces: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    services: List[str] = field(default_factory=list)
    ports: List[str] = field(default_factory=list)
    rich_rules: List[str] = field(default_factory=list)

    def allowed_networks(self) -> List[str]:
        networks = []
        for source in self.sources:
            networks.append(_normalize_cidr(source))
        for rule in self.rich_rules:
            if "source address" in rule and "accept" in rule:
                try:
                    addr = rule.split('source address="')[1].split('"')[0]
                    networks.append(_normalize_cidr(addr))
                except (IndexError, ValueError):
                    continue
        return sorted(set(networks))


def parse_firewalld_zone(xml_text: str) -> FirewalldZone:
    """Parse a firewalld zone XML definition."""
    root = ET.fromstring(xml_text)
    name = root.get("name") or "unknown"
    zone = FirewalldZone(name=name)

    for iface in root.findall("./interface"):
        zone.interfaces.append(iface.get("name", "").strip())
    for source in root.findall("./source"):
        addr = source.get("address")
        if addr:
            zone.sources.append(addr.strip())
    for service in root.findall("./service"):
        svc_name = service.get("name")
        if svc_name:
            zone.services.append(svc_name.strip())
    for port in root.findall("./port"):
        port_spec = f"{port.get('port')}/{port.get('protocol')}"
        zone.ports.append(port_spec.strip())
    for rich_rule in root.findall("./rule"):
        text = ET.tostring(rich_rule, encoding="unicode")
        zone.rich_rules.append(text.strip())

    return zone


def resolve_service_ports(
    service_name: str,
    command_executor: Callable[..., tuple[int, str, str]],
) -> List[str]:
    """Use firewall-cmd --info-service to retrieve ports for a service."""
    command = " ".join(build_firewall_info_service(service_name))
    exit_code, stdout, stderr = command_executor(command, check_exit_code=False)
    if exit_code != 0:
        raise RuntimeError(f"Failed to query service info: {stderr or stdout}")
    ports: List[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ports:"):
            _, _, raw_ports = line.partition(":")
            for entry in raw_ports.split():
                entry = entry.strip()
                if entry:
                    ports.append(entry)
    if not ports:
        raise RuntimeError(f"No ports found for service {service_name}")
    return ports

