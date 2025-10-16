# -*- coding: cp932 -*-
"""Utilities for configuring guest networking via the vSphere Automation SDK REST APIs.

This module encapsulates just enough of the /rest/vcenter/vm/guest/networking
endpoints to replace the former nmcli-based provisioning logic.  It deliberately
uses the official Automation API session workflow so that callers do not have
to manage cookies or CSRF headers by hand.

All requests default to skipping SSL verification to mirror the behaviour
of the legacy pyVmomi connections; callers may opt-in to verification by
passing ``verify_ssl=True`` to :class:`VsphereGuestNetworkSDK`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional

import requests
from requests import Session
from requests.packages.urllib3.exceptions import InsecureRequestWarning


@dataclass
class IPv4Config:
    """Desired IPv4 configuration for a single guest NIC."""

    address: str
    prefix: int
    default_gateway: Optional[str] = None

    def as_dict(self) -> Mapping[str, object]:
        data: dict[str, object] = {
            "type": "STATIC",
            "address": self.address,
            "prefix": self.prefix,
        }
        if self.default_gateway:
            data["default_gateway"] = self.default_gateway
        return data


@dataclass
class DnsConfig:
    """Desired DNS configuration."""

    servers: Iterable[str]

    def as_dict(self) -> Mapping[str, object]:
        server_list = [server for server in self.servers if server]
        return {"type": "STATIC", "servers": server_list} if server_list else {}


@dataclass
class RouteConfig:
    """Definition of a single static route."""

    network: str
    gateway: str

    def as_dict(self) -> Mapping[str, object]:
        return {"network": self.network, "gateway": self.gateway}


class VsphereGuestNetworkSDK:
    """Minimal REST client for the vSphere Automation guest networking APIs."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
    ) -> None:
        base = host.strip()
        if base.startswith("https://"):
            base = base[len("https://") :]
        self._host = base.rstrip("/")
        self._base_url = f"https://{self._host}/rest"
        self._session: Session = requests.Session()
        self._session.verify = verify_ssl
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        self._authenticate(username, password)

    def _authenticate(self, username: str, password: str) -> None:
        login_url = self._url("com/vmware/cis/session")
        response = self._session.post(login_url, auth=(username, password))
        self._raise_for_status(response, "Failed to create vSphere REST session")
        payload = response.json()
        session_id = payload.get("value")
        if not session_id:
            raise RuntimeError("vSphere REST session did not return an ID")
        self._session.headers.update({"vmware-api-session-id": session_id})

    def close(self) -> None:
        try:
            logout_url = self._url("com/vmware/cis/session")
            self._session.delete(logout_url)
        finally:
            self._session.close()

    def list_interfaces(self, vm_id: str) -> List[Mapping[str, object]]:
        url = self._url(f"vcenter/vm/{vm_id}/guest/networking/interfaces")
        response = self._session.get(url)
        self._raise_for_status(response, f"Failed to list guest interfaces for {vm_id}")
        payload = response.json()
        return payload.get("value", [])

    def update_interface(
        self,
        vm_id: str,
        nic_id: str,
        ipv4: Optional[IPv4Config],
        dns: Optional[DnsConfig],
        routes: Optional[Iterable[RouteConfig]] = None,
    ) -> None:
        spec: dict[str, object] = {}
        if ipv4:
            spec["ipv4"] = ipv4.as_dict()
        if dns:
            dns_payload = dns.as_dict()
            if dns_payload:
                spec["dns"] = dns_payload
        route_items = [route.as_dict() for route in routes or [] if route.network and route.gateway]
        if route_items:
            spec.setdefault("ipv4", {}).setdefault("routes", route_items)
        payload = {"spec": spec}
        url = self._url(f"vcenter/vm/{vm_id}/guest/networking/interfaces/{nic_id}?action=update")
        response = self._session.post(url, data=json.dumps(payload))
        self._raise_for_status(
            response,
            f"Failed to update guest interface {nic_id} for VM {vm_id}",
        )

    def _url(self, suffix: str) -> str:
        trimmed = suffix[1:] if suffix.startswith("/") else suffix
        return f"{self._base_url}/{trimmed}"

    @staticmethod
    def _raise_for_status(response: requests.Response, message: str) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            detail = ""
            try:
                payload = response.json()
                detail = json.dumps(payload)
            except Exception:
                detail = response.text
            raise RuntimeError(f"{message}: {error} ({detail})") from error


def find_interface_id_by_mac(
    interfaces: Iterable[Mapping[str, object]],
    mac_address: str,
) -> Optional[str]:
    mac_normalized = (mac_address or "").lower()
    mac_compact = mac_normalized.replace(":", "").replace("-", "")
    for entry in interfaces:
        entry_mac = (entry.get("mac") or "").lower()
        if not entry_mac:
            continue
        entry_compact = entry_mac.replace(":", "").replace("-", "")
        if entry_compact == mac_compact or entry_mac == mac_normalized:
            return entry.get("nic") or entry.get("interface")
    return None

