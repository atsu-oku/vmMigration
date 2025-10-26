# -*- coding: utf-8 -*-
"""Data models describing firewalld zone assignments."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class FirewalldZonePlan:
    """Represents the desired state of a single firewalld zone."""

    name: str
    interfaces: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    ports: List[str] = field(default_factory=list)
    rich_rules: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "interfaces": list(self.interfaces),
            "sources": list(self.sources),
            "ports": list(self.ports),
            "rich_rules": list(self.rich_rules),
        }

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], validator: "FirewalldZoneSchemaValidator") -> "FirewalldZonePlan":
        validator.validate(raw)
        return cls(
            name=raw["name"],
            interfaces=list(raw.get("interfaces", [])),
            sources=list(raw.get("sources", [])),
            ports=list(raw.get("ports", [])),
            rich_rules=list(raw.get("rich_rules", [])),
        )


class FirewalldZoneSchemaValidator:
    """Minimal JSON schema validator for firewalld zone definitions."""

    def __init__(self, schema_path: Path):
        with schema_path.open(encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.required_keys = set(self.schema.get("required", []))
        self.allowed_keys = set(self.schema.get("properties", {}).keys())

    def validate(self, data: Dict[str, Any]) -> None:
        missing = self.required_keys - data.keys()
        if missing:
            raise ValueError(f"Zone data missing required keys: {sorted(missing)}")
        unknown = set(data.keys()) - self.allowed_keys
        if unknown:
            raise ValueError(f"Zone data contains unsupported keys: {sorted(unknown)}")


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
FIREWALLD_ZONE_SCHEMA_PATH = SCHEMA_DIR / "firewalld_zone_schema.json"

FIREWALLD_ZONE_VALIDATOR = FirewalldZoneSchemaValidator(FIREWALLD_ZONE_SCHEMA_PATH)
