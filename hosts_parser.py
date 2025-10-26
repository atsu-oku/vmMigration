# -*- coding: utf-8 -*-
"""Parsing utilities for /etc/hosts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class HostsEntry:
    ip: str
    hostnames: List[str]


@dataclass
class HostsFile:
    entries: List[HostsEntry]


def parse_hosts(text: str) -> HostsFile:
    entries: List[HostsEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            entries.append(HostsEntry(ip=parts[0], hostnames=parts[1:]))
    return HostsFile(entries=entries)


def render_hosts(hosts: HostsFile) -> str:
    lines = []
    for entry in hosts.entries:
        lines.append("{} {}".format(entry.ip, " ".join(entry.hostnames)))
    return "\n".join(lines) + "\n"
