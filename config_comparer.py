# -*- coding: utf-8 -*-
"""Comparison helpers for firewall and cluster configurations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Set

try:  # pragma: no cover - packaging differences
    from .firewalld_parser import FirewalldZone  # type: ignore[import]
    from .iptables_parser import IptablesConfig  # type: ignore[import]
    from .pcs_parser import ClusterStatus  # type: ignore[import]
except ImportError:  # pragma: no cover - lint/static execution
    from firewalld_parser import FirewalldZone
    from iptables_parser import IptablesConfig
    from pcs_parser import ClusterStatus


def _normalize_set(values: Iterable[str]) -> Set[str]:
    """Build a set of trimmed, non-empty string values."""
    return {value.strip() for value in values if value.strip()}


def diff_firewalld_ports(
    current_zone: FirewalldZone,
    desired_ports: Iterable[str],
) -> Dict[str, List[str]]:
    """Calculate ports to add/remove so the zone matches the desired port list."""
    current = _normalize_set(current_zone.ports)
    desired = _normalize_set(desired_ports)
    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)
    return {"add": to_add, "remove": to_remove}


def diff_firewalld_sources(
    current_zone: FirewalldZone,
    desired_sources: Iterable[str],
) -> Dict[str, List[str]]:
    """Calculate source networks to add/remove for the zone."""
    current = _normalize_set(current_zone.sources)
    desired = _normalize_set(desired_sources)
    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)
    return {"add": to_add, "remove": to_remove}


def diff_iptables_allowed_networks(
    current_config: IptablesConfig,
    desired_networks: Iterable[str],
) -> Dict[str, List[str]]:
    """Determine which networks to add/remove from iptables to meet expectations."""
    current = _normalize_set(current_config.allowed_networks())
    desired = _normalize_set(desired_networks)
    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)
    return {"add": to_add, "remove": to_remove}


@dataclass
class ClusterDifferences:
    """Differences between the current and desired Pacemaker cluster state."""

    missing_nodes: List[str]
    extra_nodes: List[str]
    missing_resources: List[str]
    extra_resources: List[str]
    stonith_enabled_mismatch: bool


def diff_cluster_status(
    current: ClusterStatus,
    desired: ClusterStatus,
) -> ClusterDifferences:
    """Compare two pcs cluster states and report missing/extra nodes and resources."""
    current_nodes = {node.name for node in current.nodes}
    desired_nodes = {node.name for node in desired.nodes}
    current_resources = {resource.identifier for resource in current.resources}
    desired_resources = {resource.identifier for resource in desired.resources}

    return ClusterDifferences(
        missing_nodes=sorted(desired_nodes - current_nodes),
        extra_nodes=sorted(current_nodes - desired_nodes),
        missing_resources=sorted(desired_resources - current_resources),
        extra_resources=sorted(current_resources - desired_resources),
        stonith_enabled_mismatch=(current.stonith_enabled != desired.stonith_enabled),
    )
