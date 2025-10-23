# -*- coding: utf-8 -*-
"""Data models describing NIC migration plans."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    device_type: Optional[str] = None
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "network_name": self.network_name,
            "mac_address": self.mac_address,
            "label": self.label,
            "device_key": self.device_key,
            "device_type": self.device_type,
            "ip_address": self.ip_address,
            "subnet_mask": self.subnet_mask,
            "subnet_prefix": self.subnet_prefix,
            "prd_ip_address": self.prd_ip_address,
            "prd_ip_segment": self.prd_ip_segment,
            "new_mac_address": self.new_mac_address,
            "is_gateway_nic": self.is_gateway_nic,
            "original_ifname": self.original_ifname,
            "desired_ifname": self.desired_ifname,
            "sdk_interface_index": self.sdk_interface_index,
            "sdk_nic_id": self.sdk_nic_id,
            "sdk_interface": self.sdk_interface,
            "dns_servers": list(self.dns_servers),
            "routes": [route.__dict__ for route in self.routes],
            "gateway": self.gateway,
        }

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], validator: "NicSchemaValidator") -> "NicPlan":
        validator.validate(raw)
        return cls(
            index=raw["index"],
            network_name=raw["network_name"],
            mac_address=raw["mac_address"],
            label=raw.get("label", ""),
            device_key=raw.get("device_key"),
            device_type=raw.get("device_type"),
            ip_address=raw.get("ip_address"),
            subnet_mask=raw.get("subnet_mask"),
            subnet_prefix=raw.get("subnet_prefix"),
            prd_ip_address=raw.get("prd_ip_address"),
            prd_ip_segment=raw.get("prd_ip_segment"),
            new_mac_address=raw.get("new_mac_address"),
            is_gateway_nic=raw.get("is_gateway_nic", False),
            original_ifname=raw.get("original_ifname"),
            desired_ifname=raw.get("desired_ifname"),
            sdk_interface_index=raw.get("sdk_interface_index"),
            sdk_nic_id=raw.get("sdk_nic_id"),
            sdk_interface=raw.get("sdk_interface"),
            dns_servers=list(raw.get("dns_servers", [])),
            routes=[],
            gateway=raw.get("gateway"),
        )



class NicSchemaValidator:
    """Simple validator based on a JSON schema definition."""

    def __init__(self, schema_path: Path):
        with schema_path.open(encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.required_keys = set(self.schema.get("required", []))
        self.allowed_keys = set(self.schema.get("properties", {}).keys())

    def validate(self, data: Dict[str, Any]) -> None:
        missing = self.required_keys - data.keys()
        if missing:
            raise ValueError(f"NIC data missing required keys: {sorted(missing)}")
        unknown = set(data.keys()) - self.allowed_keys
        if unknown:
            raise ValueError(f"NIC data contains unsupported keys: {sorted(unknown)}")


def load_environment_mapping(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


SCHEMA_DIR = (Path(__file__).resolve().parent / 'schemas')
NIC_PLAN_SCHEMA_PATH = SCHEMA_DIR / 'nic_plan_schema.json'
ENV_MAPPING_PATH = SCHEMA_DIR / 'environment_mapping.json'

NIC_PLAN_VALIDATOR = NicSchemaValidator(NIC_PLAN_SCHEMA_PATH)
ENVIRONMENT_MAPPING = load_environment_mapping(ENV_MAPPING_PATH)


