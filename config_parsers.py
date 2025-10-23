# -*- coding: utf-8 -*-
"""Validators for configuration files using JSON schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class SchemaValidator:
    """Simple validator enforcing required keys and disallowing extras."""

    def __init__(self, schema_path: Path):
        with schema_path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        self.required = set(schema.get("required", []))
        self.allowed = set(schema.get("properties", {}).keys())

    def validate(self, data: Dict[str, Any]) -> None:
        missing = self.required - data.keys()
        if missing:
            raise ValueError(f"Missing required keys: {sorted(missing)}")
        unknown = set(data.keys()) - self.allowed
        if unknown:
            raise ValueError(f"Unsupported keys present: {sorted(unknown)}")


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
HOSTS_SCHEMA_VALIDATOR = SchemaValidator(SCHEMA_DIR / "hosts_schema.json")
PROFILE_SCHEMA_VALIDATOR = SchemaValidator(SCHEMA_DIR / "profile_schema.json")
REPO_SCHEMA_VALIDATOR = SchemaValidator(SCHEMA_DIR / "repo_schema.json")
