# -*- coding: utf-8 -*-
"""Management helpers for applying firewalld configuration."""

from __future__ import annotations

from typing import Callable, Iterable, List

from command_builders import (
    build_firewall_add_port,
    build_firewall_list_ports,
    build_firewall_list_sources,
    build_firewall_reload,
    build_firewall_remove_port,
)
from firewalld_parser import FirewalldZone, resolve_service_ports
from config_comparer import diff_firewalld_ports, diff_firewalld_sources


def _run_command(executor: Callable[..., tuple[int, str, str]], command_parts: List[str]) -> tuple[int, str, str]:
    command = " ".join(command_parts)
    exit_code, stdout, stderr = executor(command, check_exit_code=False)
    if exit_code != 0:
        raise RuntimeError(f"Command failed ({command}): {stderr or stdout}")
    return exit_code, stdout, stderr


def get_zone_ports(zone: str, executor: Callable[..., tuple[int, str, str]]) -> List[str]:
    _, stdout, _ = _run_command(executor, build_firewall_list_ports(zone))
    if not stdout.strip():
        return []
    return [entry.strip() for entry in stdout.split() if entry.strip()]


def get_zone_sources(zone: str, executor: Callable[..., tuple[int, str, str]]) -> List[str]:
    _, stdout, _ = _run_command(executor, build_firewall_list_sources(zone))
    if not stdout.strip():
        return []
    return [entry.strip() for entry in stdout.split() if entry.strip()]


def apply_zone_ports(
    zone: str,
    desired_ports: Iterable[str],
    executor: Callable[..., tuple[int, str, str]],
) -> None:
    current_ports = get_zone_ports(zone, executor)
    diff = diff_firewalld_ports(FirewalldZone(name=zone, ports=current_ports), desired_ports)
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
    current_sources = get_zone_sources(zone, executor)
    diff = diff_firewalld_sources(FirewalldZone(name=zone, sources=current_sources), desired_sources)
    for source in diff["remove"]:
        _run_command(executor, ["firewall-cmd", "--zone", zone, "--remove-source", source, "--permanent"])
    for source in diff["add"]:
        _run_command(executor, ["firewall-cmd", "--zone", zone, "--add-source", source, "--permanent"])
    if diff["add"] or diff["remove"]:
        _run_command(executor, build_firewall_reload())


def apply_service_ports(
    zone: str,
    services: Iterable[str],
    executor: Callable[..., tuple[int, str, str]],
) -> None:
    consolidated: List[str] = []
    for service in services:
        consolidated.extend(resolve_service_ports(service, executor))
    apply_zone_ports(zone, consolidated, executor)
