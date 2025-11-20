"""Utility helpers for loading and validating workflow phase schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List
import json


WORKFLOW_PHASES_FILENAME = "workflow_phases.json"


def _validate_workflow_phases(phases: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Ensure workflow phase entries conform to the expected minimal schema."""
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("Each workflow phase must be a JSON object.")
        extra_keys = set(phase.keys()) - {"id", "handler"}
        if extra_keys:
            raise ValueError(f"Unexpected keys in workflow phase: {sorted(extra_keys)}")
        if "id" not in phase or "handler" not in phase:
            raise ValueError(f"Missing required keys in workflow phase: {phase}")
        if not isinstance(phase["id"], str) or not isinstance(phase["handler"], str):
            raise ValueError(f"Workflow phase entries must be strings: {phase}")
    return phases


def load_workflow_phase_schema(schema_dir: Path) -> List[Dict[str, str]]:
    """Load and validate the workflow phase schema from the schema directory."""
    schema_path = schema_dir / WORKFLOW_PHASES_FILENAME
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    return _validate_workflow_phases(data)
