# -*- coding: utf-8 -*-
"""Parsing utilities for /etc/profile exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class ExportEntry:
    name: str
    value: str


@dataclass
class ProfileDefinition:
    exports: List[ExportEntry]


def parse_profile(text: str) -> ProfileDefinition:
    exports: List[ExportEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            assignment = stripped[len("export "):]
            if "=" in assignment:
                name, _, value = assignment.partition("=")
                exports.append(ExportEntry(name=name.strip(), value=value.strip()))
    return ProfileDefinition(exports=exports)


def render_profile(profile: ProfileDefinition) -> str:
    lines = []
    for entry in profile.exports:
        lines.append(f"export {entry.name}={entry.value}")
    return "\n".join(lines) + "\n"
