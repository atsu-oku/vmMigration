# -*- coding: utf-8 -*-
"""Management helpers for applying firewalld configuration."""

from __future__ import annotations

import ipaddress
from typing import Callable, Iterable, List

from command_builders import (
    build_firewall_add_port,
    build_firewall_list_ports,
    build_firewall_list_sources,
    build_firewall_reload,
    build_firewall_remove_port,
)
from config_comparer import diff_firewalld_ports, diff_firewalld_sources
from firewalld_parser import FirewalldZone, resolve_service_ports

LINK_LOCAL_PREFIX = ipaddress.ip_network("169.254.0.0/16")


def _run_command(
    executor: Callable[..., tuple[int, str, str]], command_parts: List[str]
) -> tuple[int, str, str]:
    command = " ".join(command_parts)
    exit_code, stdout, stderr = executor(command, check_exit_code=False)
    if exit_code != 0:
        raise RuntimeError(f"Command failed ({command}): {stderr or stdout}")
    return exit_code, stdout, stderr


def _is_link_local_source(source: str) -> bool:
    """Return True when the firewalld source entry belongs to the link-local prefix."""
    candidate = source.strip()
    if not candidate:
        return False
    try:
        if "/" in candidate:
            network = ipaddress.ip_network(candidate, strict=False)
            return isinstance(network, ipaddress.IPv4Network) and network.subnet_of(
                LINK_LOCAL_PREFIX
            )
        address = ipaddress.ip_address(candidate)
        return (
            isinstance(address, ipaddress.IPv4Address) and address in LINK_LOCAL_PREFIX
        )
    except ValueError:
        return False


def get_zone_ports(
    zone: str, executor: Callable[..., tuple[int, str, str]]
) -> List[str]:
    """Return the current firewalld port list for a zone."""
    _, stdout, _ = _run_command(executor, build_firewall_list_ports(zone))
    if not stdout.strip():
        return []
    return [entry.strip() for entry in stdout.split() if entry.strip()]


def get_zone_sources(
    zone: str, executor: Callable[..., tuple[int, str, str]]
) -> List[str]:
    """Return the current firewalld source list for a zone."""
    _, stdout, _ = _run_command(executor, build_firewall_list_sources(zone))
    if not stdout.strip():
        return []
    return [entry.strip() for entry in stdout.split() if entry.strip()]


def apply_zone_ports(
    zone: str,
    desired_ports: Iterable[str],
    executor: Callable[..., tuple[int, str, str]],
) -> None:
    """Apply add/remove operations so the zone's ports match the desired list."""
    current_ports = get_zone_ports(zone, executor)
    diff = diff_firewalld_ports(
        FirewalldZone(name=zone, ports=current_ports), desired_ports
    )
    for port_spec in diff["remove"]:
        _run_command(executor, build_firewall_remove_port(zone, port_spec))
    for port_spec in diff["add"]:
        _run_command(executor, build_firewall_add_port(zone, port_spec))
    if diff["add"] or diff["remove"]:
        _run_command(executor, build_firewall_reload())


def apply_zone_sources(
    zone: str,
    desired_sources: Iterable[str],
    executor: Callable[..., tuple[int, str, str]],
) -> None:
    """Apply add/remove operations to align zone sources with desired networks."""
    current_sources = get_zone_sources(zone, executor)
    diff = diff_firewalld_sources(
        FirewalldZone(name=zone, sources=current_sources), desired_sources
    )
    removed_protected: List[str] = []
    change_applied = False
    for source in diff["remove"]:
        if _is_link_local_source(source):
            removed_protected.append(source)
            continue
        _run_command(
            executor,
            ["firewall-cmd", "--zone", zone, "--remove-source", source, "--permanent"],
        )
        change_applied = True
    for source in diff["add"]:
        _run_command(
            executor,
            ["firewall-cmd", "--zone", zone, "--add-source", source, "--permanent"],
        )
        change_applied = True
    if change_applied:
        _run_command(executor, build_firewall_reload())
    if removed_protected:
        print(
            f"   -> firewalld: retained link-local sources in zone '{zone}' ({', '.join(sorted(removed_protected))})."
        )


def apply_service_ports(
    zone: str,
    services: Iterable[str],
    executor: Callable[..., tuple[int, str, str]],
) -> None:
    """Resolve service definitions and apply their ports to the zone."""
    consolidated: List[str] = []
    for service in services:
        consolidated.extend(resolve_service_ports(service, executor))
    apply_zone_ports(zone, consolidated, executor)
