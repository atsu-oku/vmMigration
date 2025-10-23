# -*- coding: utf-8 -*-
"""Parsing helpers for iptables-save style configurations."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import List


def _normalize_network(value: str) -> str:
    addr, _, mask = value.partition("/")
    if not mask:
        mask = "255.255.255.255"
    try:
        network = ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
        return str(network)
    except ipaddress.AddressValueError:
        return value


@dataclass
class IptablesRule:
    raw: str

    def allows(self) -> bool:
        return "-j ACCEPT" in self.raw

    def network_targets(self) -> List[str]:
        targets: List[str] = []
        if "-s " in self.raw:
            src = self.raw.split("-s ", 1)[1].split()[0]
            targets.append(_normalize_network(src))
        if "-d " in self.raw:
            dst = self.raw.split("-d ", 1)[1].split()[0]
            targets.append(_normalize_network(dst))
        return targets


@dataclass
class IptablesChain:
    name: str
    policy: str | None
    rules: List[IptablesRule] = field(default_factory=list)


@dataclass
class IptablesConfig:
    table: str
    chains: List[IptablesChain]

    def allowed_networks(self) -> List[str]:
        networks: List[str] = []
        for chain in self.chains:
            for rule in chain.rules:
                if rule.allows():
                    networks.extend(rule.network_targets())
        return sorted(set(networks))


def parse_iptables_config(lines: List[str]) -> IptablesConfig:
    table = "filter"
    chains: List[IptablesChain] = []
    current_chain: IptablesChain | None = None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            table = line[1:]
        elif line.startswith(":"):
            name, policy, *_ = line[1:].split()
            current_chain = IptablesChain(name=name, policy=None if policy == "-" else policy)
            chains.append(current_chain)
        elif line.startswith("-A") and current_chain is not None:
            current_chain.rules.append(IptablesRule(raw=line))
    return IptablesConfig(table=table, chains=chains)

