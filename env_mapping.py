# -*- coding: utf-8 -*-
"""Environment mapping helpers for STG<->PRD conversions and service lookups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
ENV_MAPPING_PATH = SCHEMA_DIR / "environment_mapping.json"

with ENV_MAPPING_PATH.open(encoding="utf-8") as handle:
    ENV_MAPPING: Dict[str, object] = json.load(handle)

_SERVICE_PORTS = {
    key: value for key, value in ENV_MAPPING.get("service_ports", {}).items()
}


def get_service_ports(service_name: str) -> List[str]:
    """Return the list of mapped ports for a service, raising when undefined."""
    ports = _SERVICE_PORTS.get(service_name, [])
    if not ports:
        raise KeyError(f"No port mapping defined for service '{service_name}'")
    return list(ports)
