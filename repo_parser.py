# -*- coding: utf-8 -*-
"""Parsing utilities for yum repo definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class RepoEntry:
    identifier: str
    baseurl: str
    enabled: bool = True
    gpgcheck: bool = True
    gpgkey: str | None = None


@dataclass
class RepoFile:
    entries: List[RepoEntry]


def parse_repo(text: str) -> RepoFile:
    entries: List[RepoEntry] = []
    current: RepoEntry | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if current:
                entries.append(current)
            identifier = stripped.strip("[]")
            current = RepoEntry(identifier=identifier, baseurl="")
        elif current and "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "baseurl":
                current.baseurl = value
            elif key == "enabled":
                current.enabled = value == "1"
            elif key == "gpgcheck":
                current.gpgcheck = value == "1"
            elif key == "gpgkey":
                current.gpgkey = value
    if current:
        entries.append(current)
    return RepoFile(entries=entries)


def render_repo(repo: RepoFile) -> str:
    lines = []
    for entry in repo.entries:
        lines.append(f"[{entry.identifier}]")
        lines.append(f"baseurl={entry.baseurl}")
        lines.append(f"enabled={'1' if entry.enabled else '0'}")
        lines.append(f"gpgcheck={'1' if entry.gpgcheck else '0'}")
        if entry.gpgkey:
            lines.append(f"gpgkey={entry.gpgkey}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
