# -*- coding: utf-8 -*-
"""Data models describing NIC migration plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type


@dataclass
class StaticRoutePlan:
    """Represents a single static route that should exist on a NIC."""

    network: str
    prefix: int
    gateway: Optional[str]
    owner_index: Optional[int] = None
    configured: bool = False


@dataclass
class NicPlan:
    """Complete description of how a NIC should be configured for PRD."""

    index: int
    network_name: str
    mac_address: str
    device_type: Optional[Type] = None
    device_key: Optional[int] = None
    label: str = ""
    ip_address: Optional[str] = None
    subnet_mask: Optional[str] = None
    subnet_prefix: Optional[int] = None

    prd_ip_address: Optional[str] = None
    prd_ip_segment: Optional[int] = None
    new_mac_address: Optional[str] = None
    is_gateway_nic: bool = False
    gateway: Optional[str] = None

    original_ifname: Optional[str] = None
    desired_ifname: Optional[str] = None

    sdk_interface_index: Optional[int] = None
    sdk_nic_id: Optional[str] = None
    sdk_interface: Optional[Dict[str, Any]] = None  # type: ignore[name-defined]

    dns_servers: List[str] = field(default_factory=list)
    routes: List[StaticRoutePlan] = field(default_factory=list)

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __setitem__(self, key: str, value):
        setattr(self, key, value)

    def get(self, key: str, default=None):
        return getattr(self, key, default)
