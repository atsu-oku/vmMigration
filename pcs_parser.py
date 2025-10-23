# -*- coding: utf-8 -*-
"""Parsing helpers for pcs/corosync cluster status."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class ClusterNode:
    name: str
    state: str


@dataclass
class ClusterResource:
    identifier: str
    resource_type: str
    state: str


@dataclass
class ClusterStatus:
    cluster_name: str
    stonith_enabled: bool
    stonith_resources: List[ClusterResource] = field(default_factory=list)
    nodes: List[ClusterNode] = field(default_factory=list)
    resources: List[ClusterResource] = field(default_factory=list)


def parse_cluster_status(json_text: str) -> ClusterStatus:
    data = json.loads(json_text)
    cluster = ClusterStatus(
        cluster_name=data.get("cluster_name", "unknown"),
        stonith_enabled=data.get("stonith_enabled", False),
    )
    for entry in data.get("stonith_resources", []):
        cluster.stonith_resources.append(
            ClusterResource(
                identifier=entry.get("id", ""),
                resource_type=entry.get("type", ""),
                state=entry.get("state", ""),
            )
        )
    for node in data.get("nodes", []):
        cluster.nodes.append(
            ClusterNode(name=node.get("name", ""), state=node.get("state", "")),
        )
    for resource in data.get("resources", []):
        cluster.resources.append(
            ClusterResource(
                identifier=resource.get("id", ""),
                resource_type=resource.get("type", ""),
                state=resource.get("state", ""),
            )
        )
    return cluster

