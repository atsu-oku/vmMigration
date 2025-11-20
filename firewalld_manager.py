# -*- coding: utf-8 -*-
"""Management helpers for applying firewalld configuration."""

from __future__ import annotations

import importlib.util
import ipaddress
import sys
from pathlib import Path
from typing import Callable, Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_local_module(module_name: str, filename: str):
    """Load a sibling module without depending on PYTHONPATH."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module_path = PROJECT_ROOT / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to locate '{filename}' relative to {PROJECT_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


try:
    from command_builders import (  # type: ignore[import]
        build_firewall_add_port,
        build_firewall_list_ports,
        build_firewall_list_sources,
        build_firewall_reload,
        build_firewall_remove_port,
    )
except ModuleNotFoundError as import_error:
    try:
        command_builders = _load_local_module("command_builders", "command_builders.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    build_firewall_add_port = command_builders.build_firewall_add_port
    build_firewall_list_ports = command_builders.build_firewall_list_ports
    build_firewall_list_sources = command_builders.build_firewall_list_sources
    build_firewall_reload = command_builders.build_firewall_reload
    build_firewall_remove_port = command_builders.build_firewall_remove_port

try:
    from config_comparer import (  # type: ignore[import]
        diff_firewalld_ports,
        diff_firewalld_sources,
    )
except ModuleNotFoundError as import_error:
    try:
        config_comparer = _load_local_module("config_comparer", "config_comparer.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    diff_firewalld_ports = config_comparer.diff_firewalld_ports
    diff_firewalld_sources = config_comparer.diff_firewalld_sources

try:
    from firewalld_parser import (  # type: ignore[import]
        FirewalldZone,
        resolve_service_ports,
    )
except ModuleNotFoundError as import_error:
    try:
        firewalld_parser = _load_local_module("firewalld_parser", "firewalld_parser.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    FirewalldZone = firewalld_parser.FirewalldZone
    resolve_service_ports = firewalld_parser.resolve_service_ports

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
