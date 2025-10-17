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
import logging
import time
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
        self._logger = logging.getLogger("cloneAndVmotion")
        self._rest_base_url = f"https://{self._host}/rest"
        self._api_base_url = f"https://{self._host}/api"
        self._session: Session = requests.Session()
        self._session.verify = verify_ssl
        self._session.headers.update({"Accept": "application/json"})
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

    def list_interfaces(
        self,
        vm_id: str,
        *,
        retries: int = 12,
        delay_seconds: float = 5.0,
    ) -> List[Mapping[str, object]]:
        """Return guest NIC metadata, polling a few times if necessary."""
        for attempt in range(1, max(1, retries) + 1):
            api_url = self._url(f"vcenter/vm/{vm_id}/guest/networking/interfaces", use_api=True)
            response = self._session.get(api_url)
            if response.status_code == 404:
                rest_url = self._url(f"vcenter/vm/{vm_id}/guest/networking/interfaces")
                response = self._session.get(rest_url)
            self._raise_for_status(response, f"Failed to list guest interfaces for {vm_id}")
            payload = response.json()
            self._logger.debug(
                "Guest networking API attempt %s/%s response: %s",
                attempt,
                retries,
                payload,
            )
            interfaces: List[Mapping[str, object]] = []
            if isinstance(payload, list):
                interfaces = payload
            elif isinstance(payload, dict):
                value = payload.get("value")
                if isinstance(value, list):
                    interfaces = value
                else:
                    alt = payload.get("interfaces") or payload.get("items")
                    if isinstance(alt, list):
                        interfaces = alt
            if interfaces:
                return interfaces
            if attempt < retries:
                self._logger.debug(
                    "Guest networking API returned no NICs for %s (attempt %s); retrying in %.1fs",
                    vm_id,
                    attempt,
                    delay_seconds,
                )
                time.sleep(max(0.0, delay_seconds))
        self._logger.warning(
            "Guest networking API returned no interfaces for %s after %s attempts.",
            vm_id,
            retries,
        )
        return []

    def get_networking_state(self, vm_id: str) -> Mapping[str, object]:
        """Return aggregated networking state (DNS, host name, etc.)."""
        api_url = self._url(f"vcenter/vm/{vm_id}/guest/networking", use_api=True)
        response = self._session.get(api_url)
        if response.status_code == 404:
            rest_url = self._url(f"vcenter/vm/{vm_id}/guest/networking")
            response = self._session.get(rest_url)
        if response.status_code in (204, 202):
            return {}
        if not response.content:
            return {}
        self._raise_for_status(response, f"Failed to retrieve guest networking state for {vm_id}")
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        self._logger.debug("Guest networking state response: %s", payload)
        return payload

    def list_routes(self, vm_id: str) -> List[Mapping[str, object]]:
        """Return guest routing table entries."""
        api_url = self._url(f"vcenter/vm/{vm_id}/guest/networking/routes", use_api=True)
        response = self._session.get(api_url)
        if response.status_code == 404:
            rest_url = self._url(f"vcenter/vm/{vm_id}/guest/networking/routes")
            response = self._session.get(rest_url)
        if response.status_code in (204, 202):
            return []
        if not response.content:
            return []
        self._raise_for_status(response, f"Failed to list guest routes for {vm_id}")
        payload = response.json()
        self._logger.debug("Guest networking routes response: %s", payload)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            value = payload.get("value")
            if isinstance(value, list):
                return value
            routes = payload.get("routes") or payload.get("items")
            if isinstance(routes, list):
                return routes
        return []

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
        spec["ipv6"] = {"type": "DISABLED"}
        route_items = [route.as_dict() for route in routes or [] if route.network and route.gateway]
        if route_items:
            spec.setdefault("ipv4", {}).setdefault("routes", route_items)
        payload = {"spec": spec}
        api_url = self._url(
            f"vcenter/vm/{vm_id}/guest/networking/interfaces/{nic_id}?action=update",
            use_api=True,
        )
        response = self._session.post(api_url, json=payload)
        if response.status_code == 404:
            rest_url = self._url(
                f"vcenter/vm/{vm_id}/guest/networking/interfaces/{nic_id}?action=update"
            )
            response = self._session.post(rest_url, data=json.dumps(payload))
        self._raise_for_status(
            response,
            f"Failed to update guest interface {nic_id} for VM {vm_id}",
        )

    def _url(self, suffix: str, *, use_api: bool = False) -> str:
        trimmed = suffix[1:] if suffix.startswith("/") else suffix
        base = self._api_base_url if use_api else self._rest_base_url
        return f"{base}/{trimmed}"

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
    candidate_keys = (
        "mac",
        "mac_address",
        "macAddress",
        "hardware_address",
        "hardwareAddress",
    )
    for entry in interfaces:
        entry_mac_value = ""
        for key in candidate_keys:
            value = entry.get(key)
            if isinstance(value, str) and value:
                entry_mac_value = value
                break
        if not entry_mac_value:
            # Some payloads embed the MAC inside a nested "link" structure.
            link = entry.get("link") or entry.get("link_info") or {}
            if isinstance(link, Mapping):
                for key in candidate_keys:
                    value = link.get(key)
                    if isinstance(value, str) and value:
                        entry_mac_value = value
                        break
        entry_mac = (entry_mac_value or "").lower()
        if not entry_mac:
            continue
        entry_compact = entry_mac.replace(":", "").replace("-", "")
        if entry_compact == mac_compact or entry_mac == mac_normalized:
            return entry.get("nic") or entry.get("interface") or entry.get("id")
    return None
