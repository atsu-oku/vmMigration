# -*- coding: utf-8 -*-
"""Helpers for constructing static route plans."""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple


LINK_LOCAL_PREFIX = ipaddress.ip_network("169.254.0.0/16")


def build_static_route_entries(
    raw_routes: Iterable[Dict[str, Any]],
    nic_records: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str | None, int | None]:
    """
    Convert raw route records into normalized entries.

    Returns a tuple of (routes, default_route_candidates, default_gateway, owner_index).
    """
    routes: List[Dict[str, Any]] = []
    default_candidates: List[Dict[str, Any]] = []
    default_gateway: str | None = None
    default_owner: int | None = None
    route_keys: Set[Tuple[str, int, str]] = set()

    for route in raw_routes:
        network = route.get("network")
        prefix = route.get("prefix")
        gateway = route.get("gateway")
        interface_index = route.get("owner_index")
        if interface_index is None:
            raw_entry = route.get("raw")
            if isinstance(raw_entry, dict):
                interface_index = raw_entry.get("interface_index")
        if network is None or prefix is None:
            continue
        try:
            prefix_int = int(prefix)
        except (TypeError, ValueError):
            continue
        network_str = str(network)
        gateway_key = str(gateway or "")
        key = (network_str, prefix_int, gateway_key)
        if key in route_keys:
            continue
        route_keys.add(key)

        skip_route = False
        try:
            network_spec = network_str if "/" in network_str else f"{network_str}/{prefix_int}"
            network_obj = ipaddress.ip_network(network_spec, strict=False)
            if isinstance(network_obj, ipaddress.IPv4Network) and network_obj.subnet_of(LINK_LOCAL_PREFIX):
                skip_route = True
        except ValueError:
            pass
        if not skip_route and gateway:
            try:
                gateway_obj = ipaddress.ip_address(gateway)
                if isinstance(gateway_obj, ipaddress.IPv4Address) and gateway_obj in LINK_LOCAL_PREFIX:
                    skip_route = True
            except ValueError:
                pass
        if skip_route:
            continue

        resolved_owner = None
        if interface_index is not None and 0 <= interface_index < len(nic_records):
            resolved_owner = interface_index
        entry = {
            "network": network_str,
            "prefix": prefix_int,
            "gateway": gateway,
        }
        if resolved_owner is not None:
            entry["owner_index"] = resolved_owner
        routes.append(entry)
        if gateway and (network == "0.0.0.0" or prefix_int == 0):
            default_candidates.append({"gateway": gateway, "owner_index": resolved_owner})
            if default_gateway is None:
                default_gateway = gateway
                default_owner = resolved_owner
    return routes, default_candidates, default_gateway, default_owner
