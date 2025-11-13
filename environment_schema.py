"""Utility helpers for loading environment definitions used by the workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List
import json


ENVIRONMENTS_FILENAME = "environments.json"


@dataclass(frozen=True)
class EnvironmentConfig:
    """Describes a deployment environment for the migration workflow."""

    id: str
    description: str
    cluster: str
    primary_datastore: str
    final_datastore: str
    proxy_url: str
    ssh_allowed_source_ip: str

    def select_datastore(self, *, use_final: bool = False) -> str:
        """Return the appropriate datastore identifier."""
        return self.final_datastore if use_final and self.final_datastore else self.primary_datastore


def _validate_record(record: Dict[str, str]) -> EnvironmentConfig:
    required_keys = {
        "id",
        "description",
        "cluster",
        "primary_datastore",
        "final_datastore",
        "proxy_url",
        "ssh_allowed_source_ip",
    }
    missing = required_keys - record.keys()
    if missing:
        raise ValueError(f"Environment definition missing keys: {sorted(missing)}")
    unknown = set(record.keys()) - required_keys
    if unknown:
        raise ValueError(f"Environment definition contains unknown keys: {sorted(unknown)}")
    return EnvironmentConfig(
        id=str(record["id"]),
        description=str(record["description"]),
        cluster=str(record["cluster"]),
        primary_datastore=str(record["primary_datastore"]),
        final_datastore=str(record["final_datastore"]),
        proxy_url=str(record["proxy_url"]),
        ssh_allowed_source_ip=str(record["ssh_allowed_source_ip"]),
    )


def load_environment_definitions(schema_dir: Path) -> Dict[str, EnvironmentConfig]:
    """Load and validate environment entries from the schema directory."""
    schema_path = schema_dir / ENVIRONMENTS_FILENAME
    raw_data = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, Iterable):
        raise ValueError("Environment schema must be an array of environment definitions.")
    environments: Dict[str, EnvironmentConfig] = {}
    for record in raw_data:
        if not isinstance(record, dict):
            raise ValueError("Environment definition entries must be objects.")
        config = _validate_record(record)
        if config.id in environments:
            raise ValueError(f"Duplicate environment id detected: {config.id}")
        environments[config.id] = config
    if not environments:
        raise ValueError("No environments defined in the schema.")
    return environments
