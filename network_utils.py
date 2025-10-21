# -*- coding: utf-8 -*-
"""Networking helper utilities for vSphere guest configuration workflows."""

from dataclasses import dataclass
import ipaddress
import json
import logging
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from vsphere_sdk_network import VsphereGuestNetworkSDK

LOGGER = logging.getLogger(__name__)

PRD_STATIC_ROUTE_SEGMENTS = {160, 161, 162, 163, 164}
NMCLI_FIELDS_WITH_TYPE = ["UUID", "NAME", "DEVICE", "TYPE"]
NMCLI_FIELDS_NO_TYPE = ["UUID", "NAME", "DEVICE"]
SSH_ALLOWED_SOURCE_IP = "172.16.164.7"
LEGACY_INTERFACE_PATTERN = re.compile(r"^(ens|eno|enp|enx|eth|em)[0-9a-z\-]*$", re.IGNORECASE)


@dataclass
class ConnectionCheckParams:
    """Configuration parameters controlling guest connectivity retries during migration."""

    max_attempts: int = 5
    wait_seconds: int = 3
    pre_ping_wait_seconds: int = 10
    ping_retry_count: int = 4
    ping_retry_delay: int = 2
    ping_timeout_seconds: int = 2


DEFAULT_CONN_CHECK_PARAMS = ConnectionCheckParams()


def extract_mac_from_sdk_interface(entry: Mapping[str, Any]) -> Optional[str]:
    """Return the MAC address from a guest networking interface entry (vSphere Automation API ≥ 8.0)."""
    if not isinstance(entry, Mapping):
        return None
    candidate_keys = ("mac_address", "macAddress", "mac", "hardware_address", "hardwareAddress")
    for key in candidate_keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    link_info = entry.get("link") or entry.get("link_info")
    if isinstance(link_info, Mapping):
        for key in candidate_keys:
            value = link_info.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_ipv4_from_sdk_interface(entry: Mapping[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    """Return (IPv4 address, prefix length) from a guest networking interface entry (API ≥ 8.0)."""
    if not isinstance(entry, Mapping):
        return None, None
    ip_sources: List[Mapping[str, Any]] = []
    ip_block = entry.get("ip")
    if isinstance(ip_block, Mapping):
        for key in ("ip_addresses", "addresses"):
            values = ip_block.get(key)
            if isinstance(values, list):
                ip_sources.extend(v for v in values if isinstance(v, Mapping))
        ipv4_block = ip_block.get("ipv4")
        if isinstance(ipv4_block, Mapping):
            for key in ("ip_addresses", "addresses"):
                values = ipv4_block.get(key)
                if isinstance(values, list):
                    ip_sources.extend(v for v in values if isinstance(v, Mapping))
    ipv4_block_top = entry.get("ipv4")
    if isinstance(ipv4_block_top, Mapping):
        for key in ("ip_addresses", "addresses"):
            values = ipv4_block_top.get(key)
            if isinstance(values, list):
                ip_sources.extend(v for v in values if isinstance(v, Mapping))
        single_address = ipv4_block_top.get("address")
        if isinstance(single_address, Mapping):
            ip_sources.append(single_address)
    for ip_entry in ip_sources:
        ip_value = ip_entry.get("ip_address") or ip_entry.get("ip") or ip_entry.get("address")
        if not ip_value or ":" in str(ip_value):
            continue
        prefix_value = (
            ip_entry.get("prefix_length")
            or ip_entry.get("prefix")
            or ip_entry.get("subnet_prefix_length")
            or ip_entry.get("prefix_len")
        )
        prefix_int: Optional[int] = None
        if prefix_value is not None:
            try:
                prefix_int = int(prefix_value)
            except (TypeError, ValueError):
                prefix_int = None
        return str(ip_value), prefix_int
    return None, None


def extract_dns_servers_from_state(state_payload: Any) -> List[str]:
    """Return DNS servers reported by the guest networking state endpoint (API ≥ 8.0)."""
    if isinstance(state_payload, Mapping):
        inner = state_payload.get("value", state_payload)
        if isinstance(inner, Mapping):
            dns_block = inner.get("dns")
            if isinstance(dns_block, Mapping):
                servers = dns_block.get("servers") or dns_block.get("ip_addresses") or dns_block.get("addresses") or []
                return [str(server) for server in servers if server]
    return []


def extract_routes_from_sdk_payload(payload: Any) -> List[Dict[str, Any]]:
    """Normalise guest route entries from the Automation API (8.0.3 compatible) into a uniform list."""
    if isinstance(payload, Mapping):
        payload = payload.get("value", payload)
    routes_iterable: Iterable[Any]
    if isinstance(payload, list):
        routes_iterable = payload
    elif isinstance(payload, tuple):
        routes_iterable = payload
    else:
        routes_iterable = []
    normalised: List[Dict[str, Any]] = []
    for entry in routes_iterable:
        if not isinstance(entry, Mapping):
            continue
        network = entry.get("network")
        prefix = entry.get("prefix_length") or entry.get("prefix")
        gateway = entry.get("gateway_address") or entry.get("gateway")
        owner_index = entry.get("interface_index") or entry.get("nic_index")
        destination = entry.get("destination")
        if isinstance(destination, Mapping):
            network = destination.get("network") or destination.get("cidr") or network
            prefix = destination.get("prefix_length") or destination.get("prefix") or prefix
        next_hop = entry.get("next_hop")
        if isinstance(next_hop, Mapping):
            gateway = next_hop.get("ip_address") or next_hop.get("ip") or gateway
            if owner_index is None:
                owner_index = next_hop.get("interface_index")
        if isinstance(network, str) and "/" in network and prefix is None:
            net_value, _, prefix_part = network.partition("/")
            try:
                prefix = int(prefix_part)
            except (TypeError, ValueError):
                prefix = None
            network = net_value
        prefix_int: Optional[int] = None
        if prefix is not None:
            try:
                prefix_int = int(prefix)
            except (TypeError, ValueError):
                prefix_int = None
        normalised.append(
            {
                "network": network,
                "prefix": prefix_int,
                "gateway": gateway,
                "owner_index": owner_index,
                "raw": entry,
            }
        )
    return normalised


def prefix_to_subnet_mask(prefix_length: Optional[int]) -> Optional[str]:
    """Convert a CIDR prefix length (0-32) to a dotted decimal subnet mask."""
    if not isinstance(prefix_length, int) or not 0 <= prefix_length <= 32:
        return None
    host_bits = 32 - prefix_length
    netmask = (0xFFFFFFFF << host_bits) & 0xFFFFFFFF
    return ".".join(str((netmask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def calculate_ip_stg_to_prd(ip_address: Optional[str]) -> Optional[str]:
    """Map STG IPv4 addresses (third octet 170-179) to PRD range by subtracting 10."""
    if not ip_address:
        return None
    parts = ip_address.split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid IPv4 format: {ip_address}")
    try:
        octets = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Non-numeric value detected in IPv4 address: {ip_address}") from exc
    if any(octet < 0 or octet > 255 for octet in octets):
        raise ValueError(f"IPv4 octet out of range 0-255: {ip_address}")
    if 170 <= octets[2] <= 179:
        octets[2] -= 10
        return ".".join(str(octet) for octet in octets)
    return ip_address


def mask_to_prefix(netmask: Optional[str]) -> Optional[int]:
    """Convert dotted IPv4 netmask to prefix length."""
    if not netmask:
        return None
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{netmask}", strict=False)
        return network.prefixlen
    except (ipaddress.NetmaskValueError, ValueError):
        return None


def make_nmcli_detail_fetcher(
    cache: Dict[str, Tuple[int, str]],
    executor: Callable[..., Tuple[int, str, str]],
) -> Callable[[str], Tuple[int, str]]:
    """Return a function that fetches and caches nmcli connection details."""

    def _get_connection_details(uuid_value: str) -> Tuple[int, str]:
        if uuid_value in cache:
            return cache[uuid_value]
        detail_cmd = f"nmcli connection show {uuid_value}"
        exit_code_nmcli, stdout_nmcli, _ = executor(detail_cmd, check_exit_code=False)
        cache[uuid_value] = (exit_code_nmcli, stdout_nmcli)
        return cache[uuid_value]

    return _get_connection_details


def compact_interface_name(name: Optional[str]) -> str:
    """Normalize interface name by stripping separators and casing."""
    lowered = (name or "").lower()
    return lowered.replace("-", "").replace("_", "").replace(" ", "")


def parse_ip_json_output(raw_text: str) -> List[Dict[str, Any]]:
    """Parse `ip -j -p addr` output into a list of interface dictionaries."""
    data = json.loads(raw_text)
    interfaces: List[Dict[str, Any]] = []
    for entry in data:
        ifname = entry.get("ifname")
        if not ifname:
            continue
        mac = entry.get("address") or ""
        ipv4_entries = []
        for addr_info in entry.get("addr_info", []):
            if addr_info.get("family") == "inet" and addr_info.get("local"):
                ipv4_entries.append(
                    {"address": addr_info.get("local"), "prefix_length": addr_info.get("prefixlen")}
                )
        interfaces.append({"ifname": ifname, "mac": mac, "ipv4": ipv4_entries})
    return interfaces


def parse_ip_addr_text(raw_text: str) -> List[Dict[str, Any]]:
    """Parse plain `ip addr` output."""
    interfaces: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in raw_text.splitlines():
        if not raw_line:
            continue
        if raw_line[0].isspace() is False:
            match = re.match(r"^\d+:\s*([^:]+):", raw_line)
            if match:
                current = {"ifname": match.group(1), "mac": "", "ipv4": []}
                interfaces.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        stripped = raw_line.strip()
        if stripped.startswith("link/"):
            parts = stripped.split()
            if len(parts) >= 2:
                mac_candidate = parts[1]
                if mac_candidate != "00:00:00:00:00:00":
                    current["mac"] = mac_candidate
        elif stripped.startswith("inet "):
            parts = stripped.split()
            if len(parts) >= 2:
                address_part = parts[1]
                if "/" in address_part:
                    ip_part, prefix_part = address_part.split("/", 1)
                    try:
                        prefix_len = int(prefix_part)
                    except ValueError:
                        prefix_len = None
                else:
                    ip_part = address_part
                    prefix_len = None
                current.setdefault("ipv4", []).append({"address": ip_part, "prefix_length": prefix_len})
    return interfaces


def parse_ifconfig_output(raw_text: str) -> List[Dict[str, Any]]:
    """Parse legacy ifconfig output."""
    interfaces: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in raw_text.splitlines():
        if not raw_line:
            continue
        if raw_line and not raw_line[0].isspace():
            parts = raw_line.split()
            if parts:
                current = {"ifname": parts[0], "mac": "", "ipv4": []}
                interfaces.append(current)
            continue
        if current is None:
            continue
        stripped = raw_line.strip()
        if stripped.lower().startswith("ether "):
            ether_parts = stripped.split()
            if len(ether_parts) >= 2:
                current["mac"] = ether_parts[1]
        elif stripped.startswith("inet "):
            parts = stripped.split()
            ip_value = None
            prefix_len: Optional[int] = None
            for idx, token in enumerate(parts):
                if token in ("inet", "inet4") and idx + 1 < len(parts):
                    ip_value = parts[idx + 1]
                if token == "netmask" and idx + 1 < len(parts):
                    netmask = parts[idx + 1]
                    prefix_len = mask_to_prefix(netmask)
            if ip_value:
                current.setdefault("ipv4", []).append({"address": ip_value, "prefix_length": prefix_len})
    return interfaces


def find_gateway_owner_index(nic_records: Sequence[Dict[str, Any]], gateway_ip: str) -> Optional[int]:
    """Return index of NIC owning the given gateway address."""
    try:
        gateway_addr = ipaddress.IPv4Address(gateway_ip)
    except (ValueError, ipaddress.AddressValueError):
        return None
    for idx, nic in enumerate(nic_records):
        nic_ip = nic.get("ip_address")
        nic_mask = nic.get("subnet_mask")
        if not nic_ip or not nic_mask:
            continue
        try:
            nic_network = ipaddress.IPv4Interface(f"{nic_ip}/{nic_mask}").network
        except (ValueError, ipaddress.AddressValueError):
            continue
        if gateway_addr in nic_network:
            return idx
    return None


def select_default_gateway_route(
    candidates: Iterable[Dict[str, Any]],
    nic_records: Sequence[Dict[str, Any]],
) -> Optional[Tuple[str, Optional[int]]]:
    """Choose the best default gateway from collected candidates."""
    best_candidate: Optional[Tuple[str, Optional[int]]] = None
    for entry in candidates:
        gateway = entry.get("gateway")
        if not gateway:
            continue
        owner_index = entry.get("owner_index")
        if owner_index is not None and 0 <= owner_index < len(nic_records):
            return gateway, owner_index
        if best_candidate is None:
            best_candidate = (gateway, owner_index)
    return best_candidate


def infer_gateway_from_routes(nic_records: Sequence[Dict[str, Any]], routes: Sequence[Dict[str, Any]]):
    """Infer default gateway candidate using static routes."""
    for route in routes:
        gateway_cand = route.get("gateway")
        network = route.get("network")
        prefix = route.get("prefix")
        if not gateway_cand or not network or prefix is None:
            continue
        try:
            prefix_int = int(prefix)
        except (TypeError, ValueError):
            continue
        network_str = str(network).lower()
        if network_str not in ("0.0.0.0", "default") or prefix_int != 0:
            continue
        owner_index = find_gateway_owner_index(nic_records, str(gateway_cand))
        if owner_index is not None:
            return gateway_cand, owner_index
    return None


def derive_fallback_gateway(nic_records: Sequence[Dict[str, Any]]) -> Optional[Tuple[str, Optional[int]]]:
    """Pick a fallback gateway using NIC metadata."""
    best_candidate: Optional[Tuple[str, Optional[int]]] = None
    for idx, nic in enumerate(nic_records):
        gateway = nic.get("gateway")
        if not gateway:
            continue
        if nic.get("is_gateway_nic"):
            return gateway, idx
        if best_candidate is None:
            best_candidate = (gateway, idx)
    return best_candidate


def derive_gateway_from_octet_rule(
    nic_records: Sequence[Dict[str, Any]],
    valid_octets: Sequence[int] = (160, 162),
) -> Optional[Tuple[str, Optional[int]]]:
    """Fallback gateway rule targeting specific third-octet segments."""
    for idx, nic in enumerate(nic_records):
        ip_value = nic.get("ip_address")
        subnet_mask = nic.get("subnet_mask")
        if not ip_value or not subnet_mask:
            continue
        prd_ip = calculate_ip_stg_to_prd(ip_value) or ip_value
        try:
            ip_interface = ipaddress.IPv4Interface(f"{prd_ip}/{subnet_mask}")
        except (ValueError, ipaddress.AddressValueError):
            continue
        octets = prd_ip.split(".")
        if len(octets) != 4:
            continue
        try:
            third_octet = int(octets[2])
        except ValueError:
            continue
        if third_octet not in valid_octets:
            continue
        first_host = next(ip_interface.network.hosts(), None)
        if first_host is None:
            try:
                first_host = ip_interface.network.network_address + 1  # type: ignore[operator]
            except TypeError:
                first_host = None
        if first_host is None:
            continue
        gateway_ip = str(first_host)
        return gateway_ip, idx
    return None


def collect_interface_inventory(command_executor: Callable[..., Tuple[int, str, str]]):
    """Return a list of interface metadata dictionaries for the guest OS."""
    attempts = [
        ("ip -j -p addr", parse_ip_json_output),
        ("ip -d addr", parse_ip_addr_text),
        ("ip addr", parse_ip_addr_text),
        ("ifconfig -a", parse_ifconfig_output),
    ]
    for command, parser in attempts:
        LOGGER.debug("Collecting NIC info via command: %s", command)
        exit_code, stdout, stderr = command_executor(command, check_exit_code=False)
        LOGGER.debug("Command '%s' exited with code %s", command, exit_code)
        if exit_code != 0 or not stdout:
            if stderr:
                LOGGER.debug("Command '%s' stderr: %s", command, stderr)
            continue
        try:
            inventory = parser(stdout)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.debug("Failed to parse output of '%s': %s", command, exc, exc_info=True)
            continue
        if inventory:
            LOGGER.debug(
                "Discovered interfaces via '%s': %s",
                command,
                [entry["ifname"] for entry in inventory],
            )
            return inventory
    raise RuntimeError("Could not retrieve guest OS NIC information because ip/ifconfig is unavailable.")


def split_nmcli_terse_line(line: str) -> List[str]:
    """Split an nmcli --terse output line taking escaped separators into account."""
    fields: List[str] = []
    buffer: List[str] = []
    escape = False
    for char in line:
        if escape:
            buffer.append(char)
            escape = False
        elif char == "\\":
            escape = True
        elif char == ":":
            fields.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    fields.append("".join(buffer))
    return fields


def parse_nmcli_connection_output(output: str, field_names: Sequence[str]) -> List[Dict[str, str]]:
    """Return a list of connection dicts parsed from nmcli terse output."""
    connections: List[Dict[str, str]] = []
    if not output:
        return connections
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = split_nmcli_terse_line(line)
        entry: Dict[str, str] = {}
        for idx, field in enumerate(field_names):
            value = parts[idx].strip() if idx < len(parts) else ""
            entry[field] = value
        connections.append(entry)
    return connections


def ensure_connection_activation(
    command_executor: Callable[..., Tuple[int, str, str]],
    connection_name: str,
    device_name: str,
    ping_targets: Optional[Iterable[str]] = None,
    params: Optional[ConnectionCheckParams] = None,
) -> None:
    """Ensure the specified connection is active and optional ping targets respond."""
    config = params or DEFAULT_CONN_CHECK_PARAMS
    targets: List[str] = []
    if ping_targets:
        for item in ping_targets:
            if item and item not in targets:
                targets.append(item)

    def _ping_target(target_address: str) -> bool:
        retry = max(1, config.ping_retry_count)
        timeout = max(1, config.ping_timeout_seconds)
        delay = max(0, config.ping_retry_delay)
        ping_command = (
            f"bash -c 'for i in $(seq 1 {retry}); do "
            f"ping -c 1 -W {timeout} {target_address} && exit 0; "
            f"sleep {delay}; "
            "done; exit 1'"
        )
        exit_code, cmd_stdout, cmd_stderr = command_executor(
            ping_command,
            check_exit_code=False,
        )
        if exit_code != 0:
            print(f"      - Ping to {target_address} failed (exit code {exit_code})")
            LOGGER.debug("Ping failure details: stdout=%s stderr=%s", cmd_stdout, cmd_stderr)
            return False
        LOGGER.debug("Ping to %s succeeded.", target_address)
        return True

    if not connection_name:
        if not targets:
            return
        for attempt in range(1, config.max_attempts + 1):
            print(
                f"   -> Verifying connectivity on '{device_name}' "
                f"(attempt {attempt}/{config.max_attempts})"
            )
            if all(_ping_target(target) for target in targets):
                return
            time.sleep(config.wait_seconds)
        summary = ", ".join(targets) if targets else "none"
        raise RuntimeError(
            f"Connectivity validation failed on device '{device_name}' (targets: {summary})."
        )

    for attempt in range(1, config.max_attempts + 1):
        print(
            f"   -> Ensuring nmcli connection '{connection_name}' is active "
            f"(attempt {attempt}/{config.max_attempts})"
        )
        list_cmd = "nmcli -t -f NAME connection show --active"
        exit_code, state_output, _ = command_executor(
            list_cmd,
            check_exit_code=False,
        )
        active_profiles = {line.strip() for line in (state_output or "").splitlines() if line.strip()}
        if exit_code != 0 or connection_name not in active_profiles:
            command_executor(
                f"nmcli connection up '{connection_name}'",
                check_exit_code=False,
            )
            time.sleep(config.wait_seconds)
            continue
        if not targets:
            return
        wait_before_ping = config.pre_ping_wait_seconds if attempt == 1 else config.wait_seconds
        if wait_before_ping > 0:
            LOGGER.debug(
                "Waiting %s seconds before pinging targets on %s (attempt %s)",
                wait_before_ping,
                device_name,
                attempt,
            )
            time.sleep(wait_before_ping)
        for target in targets:
            if not _ping_target(target):
                break
        else:
            return
        time.sleep(config.wait_seconds)
    summary = ", ".join(targets) if targets else "none"
    raise RuntimeError(
        f"Connection '{connection_name}' failed connectivity checks (targets: {summary})"
    )


def ensure_firewall_allows_ssh(
    command_executor: Callable[..., Tuple[int, str, str]],
    source_ip: Optional[str],
) -> None:
    """Ensure iptables/firewalld accepts SSH from the given source."""
    print("   -> Firewall configuration check...")
    if not source_ip:
        return
    firewalld_active = False
    exit_code, _, _ = command_executor("command -v systemctl", check_exit_code=False)
    if exit_code == 0:
        firewalld_state_output = ""
        for show_cmd in (
            "systemctl show firewalld.service --property=ActiveState",
            "systemctl show firewalld.service",
        ):
            show_exit, show_stdout, _ = command_executor(show_cmd, check_exit_code=False)
            if show_exit == 0 and show_stdout:
                firewalld_state_output = show_stdout
                break
        if firewalld_state_output:
            for line in firewalld_state_output.splitlines():
                if line.lower().startswith("activestate="):
                    state_value = line.split("=", 1)[1].strip().lower()
                    firewalld_active = state_value == "active"
                    break
        if not firewalld_active:
            exit_code, firewalld_status, _ = command_executor(
                "systemctl is-active firewalld",
                check_exit_code=False,
            )
            firewalld_active = exit_code == 0 and (firewalld_status or "").strip() == "active"
    if firewalld_active:
        _, default_zone, _ = command_executor(
            "firewall-cmd --get-default-zone",
            check_exit_code=False,
        )
        zone = (default_zone or "public").splitlines()[0].strip() or "public"
        rich_rule = (
            f"firewall-cmd --permanent --zone={zone} "
            f"--add-rich-rule='rule family=\"ipv4\" source address=\"{source_ip}\" service name=\"ssh\" accept'"
        )
        exit_code, _, cmd_err = command_executor(rich_rule, check_exit_code=False)
        if exit_code == 0:
            command_executor("firewall-cmd --reload", check_exit_code=False)
            print(f"      - firewalld: added SSH allow rule in zone '{zone}' ({source_ip})")
            return
        LOGGER.debug("Failed to add firewalld rich rule: %s", cmd_err)
    exit_code, _, _ = command_executor("command -v iptables", check_exit_code=False)
    if exit_code == 0:
        health_code, iptables_state, health_err = command_executor("iptables -S", check_exit_code=False)
        if health_code != 0:
            LOGGER.debug("iptables sanity check failed: %s", health_err)
        else:
            if not firewalld_active:
                lines = [line.strip() for line in (iptables_state or "").splitlines() if line.strip()]
                policy_lines = [line for line in lines if line.startswith("-P ")]
                rule_lines = [line for line in lines if line.startswith("-A ") or line.startswith("-I ")]
                policies_accept = policy_lines and all(line.upper().endswith(" ACCEPT") for line in policy_lines)
                if policies_accept and not rule_lines:
                    print("      - firewalld inactive and iptables policies ACCEPT; no firewall changes needed.")
                    return
            check_rule_cmd = f"iptables -C INPUT -p tcp -s {source_ip} --dport 22 -j ACCEPT"
            exit_code, _, _ = command_executor(check_rule_cmd, check_exit_code=False)
            if exit_code != 0:
                add_rule_cmd = f"iptables -I INPUT 1 -p tcp -s {source_ip} --dport 22 -j ACCEPT"
                command_executor(add_rule_cmd, check_exit_code=False)
                print(f"      - iptables: added SSH allow rule ({source_ip})")
                command_executor("iptables-save > /etc/sysconfig/iptables", check_exit_code=False)
                return
            print(f"      - iptables: SSH allow rule already present ({source_ip})")
            return
    print("      - firewalld / iptables unavailable; skipping firewall adjustments.")


def configure_interface_without_nmcli(
    command_executor: Callable[..., Tuple[int, str, str]],
    interface_name: str,
    new_ip: Optional[str],
    prefix: Optional[int],
    expected_gateway: Optional[str],
    routes_for_nic: Sequence[Tuple[int, Dict[str, Any]]],
    dns_servers: Optional[Sequence[str]] = None,
) -> Tuple[List[int], List[str], bool, Optional[str]]:
    """Configure a guest interface when nmcli is unavailable."""
    selected_route_indices: List[int] = []
    selected_route_lines: List[str] = []
    command_failures: List[Tuple[str, int, str, bool]] = []
    verification_command: Optional[str] = None

    deduped_dns_servers: List[str] = []
    if dns_servers:
        seen_dns: Set[str] = set()
        for server in dns_servers:
            if not server or server in seen_dns:
                continue
            deduped_dns_servers.append(server)
            seen_dns.add(server)

    netmask_from_prefix: Optional[str] = None
    if prefix is not None:
        try:
            netmask_from_prefix = prefix_to_subnet_mask(prefix)
        except ValueError:
            netmask_from_prefix = None

    def _run_guest_command(
        cmd: str,
        *,
        record_failure: bool = True,
        fatal: bool = True,
    ) -> Tuple[int, str, str]:
        exit_code, stdout, stderr = command_executor(cmd, check_exit_code=False)
        if record_failure:
            stderr_clean = (stderr or "").strip()
            if exit_code != 0 or stderr_clean:
                command_failures.append((cmd, exit_code, stderr_clean, fatal))
        return exit_code, stdout, stderr

    def _check_command_exists(cmd: str) -> bool:
        exit_result, _, _ = command_executor(f"command -v {cmd} >/dev/null 2>&1", check_exit_code=False)
        return exit_result == 0

    ip_available = _check_command_exists("ip")
    ifconfig_available = _check_command_exists("ifconfig")
    route_available = _check_command_exists("route")

    if ip_available:
        _run_guest_command(f"ip addr flush dev {interface_name}", record_failure=False, fatal=False)
        if new_ip and prefix is not None:
            _run_guest_command(f"ip addr replace {new_ip}/{prefix} dev {interface_name}")
            verification_command = f"ip addr show {interface_name} | grep -q '{new_ip}'"
        elif new_ip:
            _run_guest_command(f"ip addr add {new_ip} dev {interface_name}")
            verification_command = f"ip addr show {interface_name} | grep -q '{new_ip}'"
        else:
            _run_guest_command(f"ip addr flush dev {interface_name}", record_failure=False, fatal=False)
        _run_guest_command(f"ip link set {interface_name} up")
        if expected_gateway:
            _run_guest_command(f"ip route replace default via {expected_gateway} dev {interface_name}")
        for route_idx, route_info in routes_for_nic:
            gateway = route_info.get("gateway")
            prefix_value = route_info.get("prefix")
            network_base = route_info.get("network")
            if prefix_value is None or not gateway or not network_base:
                continue
            exit_code, _, _ = _run_guest_command(
                f"ip route replace {network_base}/{prefix_value} via {gateway} dev {interface_name}",
                fatal=False,
            )
            if exit_code == 0:
                selected_route_indices.append(route_idx)
                selected_route_lines.append(f"{network_base}/{prefix_value} via {gateway}")
    elif ifconfig_available:
        _run_guest_command(f"ifconfig {interface_name} down || true", record_failure=False, fatal=False)
        if new_ip:
            if netmask_from_prefix:
                _run_guest_command(f"ifconfig {interface_name} {new_ip} netmask {netmask_from_prefix} up")
            else:
                if prefix is not None:
                    print(f"   [WARN] Unable to derive netmask for prefix {prefix}; continuing without netmask.")
                _run_guest_command(f"ifconfig {interface_name} {new_ip} up")
            verification_command = f"ifconfig {interface_name} | grep -q '{new_ip}'"
        else:
            _run_guest_command(f"ifconfig {interface_name} up")
        if expected_gateway and route_available:
            _run_guest_command("route del default >/dev/null 2>&1 || true", record_failure=False, fatal=False)
            _run_guest_command(f"route add default gw {expected_gateway} dev {interface_name}")
        elif expected_gateway and not route_available:
            print("   [WARN] Unable to configure default gateway: 'route' command not available.")
        if routes_for_nic and not route_available:
            print("   [WARN] Static routes skipped: 'route' command not available.")
        for route_idx, route_info in routes_for_nic:
            gateway = route_info.get("gateway")
            prefix_value = route_info.get("prefix")
            network_base = route_info.get("network")
            if prefix_value is None or not gateway or not network_base:
                continue
            if route_available:
                try:
                    route_netmask = prefix_to_subnet_mask(prefix_value)
                except ValueError:
                    print(f"   [WARN] Skipping route {network_base}/{prefix_value}: invalid prefix.")
                    continue
                _run_guest_command(
                    f"route del -net {network_base} netmask {route_netmask} dev {interface_name} >/dev/null 2>&1 || true",
                    record_failure=False,
                    fatal=False,
                )
                exit_code, _, _ = _run_guest_command(
                    f"route add -net {network_base} netmask {route_netmask} gw {gateway} dev {interface_name}",
                    fatal=False,
                )
                if exit_code == 0:
                    selected_route_indices.append(route_idx)
                    selected_route_lines.append(f"{network_base}/{prefix_value} via {gateway}")
    else:
        print("   [WARN] Neither 'ip' nor 'ifconfig' is available; unable to configure interface.")
    if deduped_dns_servers:
        resolv_lines = "\n".join(f"nameserver {dns}" for dns in deduped_dns_servers if dns)
        if resolv_lines:
            resolv_payload = "\\n".join(resolv_lines.splitlines())
            command = (
                "printf '%b\\n' '" + resolv_payload.replace("'", "'\"'\"'") + "\\n' | tee /etc/resolv.conf >/dev/null"
            )
            _run_guest_command(command)
    route_dir = "/etc/sysconfig/network-scripts"
    if selected_route_lines:
        route_lines_with_dev = [
            f"{line} dev {interface_name}" if " dev " not in line else line
            for line in selected_route_lines
        ]
        route_payload = "\\n".join(route_lines_with_dev)
        write_routes_command = (
            f"if [ -d {route_dir} ]; then "
            f"printf '%b\\n' '{route_payload}\\n' | tee {route_dir}/route-{interface_name} >/dev/null; "
            "fi"
        )
        _run_guest_command(write_routes_command, fatal=False)
    else:
        cleanup_routes_command = (
            f"if [ -d {route_dir} ]; then "
            f"rm -f {route_dir}/route-{interface_name}; "
            "fi"
        )
        _run_guest_command(cleanup_routes_command, fatal=False)

    ifcfg_dir = "/etc/sysconfig/network-scripts"
    ifcfg_updates: List[str] = []
    if new_ip:
        ifcfg_updates.append(f"echo \"IPADDR={new_ip}\" >> \"$cfg\"; ")
        if prefix is not None:
            ifcfg_updates.append(f"echo \"PREFIX={prefix}\" >> \"$cfg\"; ")
        if netmask_from_prefix:
            ifcfg_updates.append(f"echo \"NETMASK={netmask_from_prefix}\" >> \"$cfg\"; ")
    if expected_gateway:
        ifcfg_updates.append(f"echo \"GATEWAY={expected_gateway}\" >> \"$cfg\"; ")
    if deduped_dns_servers:
        for dns_index, dns_value in enumerate(deduped_dns_servers, start=1):
            ifcfg_updates.append(f"echo \"DNS{dns_index}={dns_value}\" >> \"$cfg\"; ")
    if ifcfg_updates or (new_ip is None or expected_gateway is None or deduped_dns_servers):
        update_ifcfg_command = (
            f"if [ -d {ifcfg_dir} ]; then "
            f"cfg={ifcfg_dir}/ifcfg-{interface_name}; "
            "if [ -f \"$cfg\" ]; then "
            "sed -i '/^IPADDR=/d;/^PREFIX=/d;/^NETMASK=/d;/^GATEWAY=/d;/^DNS[0-9]*=/d' \"$cfg\"; "
            + "".join(ifcfg_updates)
            + "fi; "
            "fi"
        )
        _run_guest_command(update_ifcfg_command, fatal=False)

    fatal_failures = [entry for entry in command_failures if entry[3]]
    config_success = not fatal_failures
    if command_failures:
        print("   [WARN] Legacy command execution reported issues:")
        for failed_cmd, exit_code, stderr_text, fatal in command_failures:
            detail = f"exit={exit_code}"
            if stderr_text:
                detail += f", stderr='{stderr_text}'"
            severity = "fatal" if fatal else "non-fatal"
            print(f"      - {failed_cmd} ({detail}, {severity})")
    else:
        if ip_available or ifconfig_available:
            print("   -> Applied legacy network configuration (nmcli unavailable).")

    if new_ip:
        if verification_command:
            verify_exit, verify_stdout, verify_stderr = _run_guest_command(verification_command)
            if verify_exit == 0:
                print(f"   -> Confirmed IPv4 {new_ip} on {interface_name}.")
            else:
                verification_error = (verify_stderr or "").strip()
                verification_output = (verify_stdout or "").strip()
                detail = verification_error or verification_output or "verification command failed"
                config_success = False
                print(f"   [WARN] Unable to confirm IPv4 {new_ip} on {interface_name}: {detail}.")
        else:
            config_success = False
            print(f"   [WARN] Cannot verify IPv4 {new_ip}: no suitable command available.")

    return selected_route_indices, selected_route_lines, config_success, verification_command


def determine_prd_static_routes(
    nic_infos: Sequence[Dict[str, Any]],
    default_gateway: Optional[str],
    original_routes: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return PRD static routes with ownership metadata based on original STG routes."""
    routes: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, int, str, Optional[int]]] = set()
    local_networks: Dict[int, ipaddress.IPv4Network] = {}
    manage_nic_index: Optional[int] = None
    odd_octet_candidate: Optional[int] = None
    for idx, nic in enumerate(nic_infos):
        prd_ip = nic.get("prd_ip_address") or calculate_ip_stg_to_prd(nic.get("ip_address"))
        subnet_mask = nic.get("subnet_mask")
        if not prd_ip or not subnet_mask:
            continue
        try:
            network = ipaddress.IPv4Interface(f"{prd_ip}/{subnet_mask}").network
            local_networks[idx] = network
        except (ValueError, ipaddress.AddressValueError):
            continue
        dest_network_name = (nic.get("dest_network_name") or nic.get("network_name") or "").lower()
        if manage_nic_index is None and ("-mng-" in dest_network_name or "manage" in dest_network_name):
            manage_nic_index = idx
        if odd_octet_candidate is None:
            try:
                third_octet = int(str(ipaddress.IPv4Address(prd_ip)).split(".")[2])
                if third_octet % 2 == 1:
                    odd_octet_candidate = idx
            except (ValueError, ipaddress.AddressValueError, IndexError):
                pass
    if manage_nic_index is None and odd_octet_candidate is not None:
        manage_nic_index = odd_octet_candidate
    for route in original_routes:
        network = route.get("network")
        prefix = route.get("prefix")
        gateway = route.get("gateway")
        if not network or prefix is None:
            continue
        if str(network) == "0.0.0.0":
            try:
                if int(prefix) == 0:
                    continue
            except (TypeError, ValueError):
                pass
        try:
            stg_network = ipaddress.IPv4Network(f"{network}/{prefix}", strict=False)
        except ValueError:
            continue
        prd_network_address = calculate_ip_stg_to_prd(str(stg_network.network_address))
        if not prd_network_address:
            continue
        try:
            prd_network = ipaddress.IPv4Network(f"{prd_network_address}/{stg_network.prefixlen}", strict=False)
        except ValueError:
            continue
        if any(prd_network == net for net in local_networks.values()):
            continue
        prd_gateway = calculate_ip_stg_to_prd(gateway) if gateway else default_gateway
        if not prd_gateway:
            continue
        route_owner_idx: Optional[int] = route.get("owner_index")
        if route_owner_idx is not None and (route_owner_idx < 0 or route_owner_idx >= len(nic_infos)):
            route_owner_idx = None
            try:
                stg_gateway_addr = ipaddress.IPv4Address(gateway)
                for idx, nic in enumerate(nic_infos):
                    nic_ip = nic.get("ip_address")
                    nic_mask = nic.get("subnet_mask")
                    if not nic_ip or not nic_mask:
                        continue
                    try:
                        nic_network = ipaddress.IPv4Interface(f"{nic_ip}/{nic_mask}").network
                    except (ValueError, ipaddress.AddressValueError):
                        continue
                    if stg_gateway_addr in nic_network:
                        route_owner_idx = idx
                        break
            except (ValueError, ipaddress.AddressValueError):
                pass
        if manage_nic_index is not None:
            route_owner_idx = manage_nic_index
        route_key = (str(prd_network.network_address), prd_network.prefixlen, prd_gateway, route_owner_idx)
        if route_key in seen:
            continue
        seen.add(route_key)
        routes.append(
            {
                "network": str(prd_network.network_address),
                "prefix": prd_network.prefixlen,
                "gateway": prd_gateway,
                "owner_index": route_owner_idx,
            }
        )
    return routes


def verify_destination_network_with_sdk(
    sdk_client: VsphereGuestNetworkSDK,
    vm_id: str,
    nic_infos: Sequence[Dict[str, Any]],
    expected_dns_servers: Sequence[str],
    expected_routes: Sequence[Dict[str, Any]],
) -> bool:
    """Check destination guest networking state via the Automation SDK."""
    success = True
    try:
        interfaces_payload = sdk_client.list_interfaces(vm_id, retries=6, delay_seconds=5.0)
    except Exception as error:
        LOGGER.warning("SDK verification failed to read interfaces: %s", error)
        return False
    if isinstance(interfaces_payload, Mapping):
        value = interfaces_payload.get("value") or interfaces_payload.get("interfaces") or interfaces_payload.get("items")
        interfaces: Iterable[Any] = value if isinstance(value, list) else []
    elif isinstance(interfaces_payload, list):
        interfaces = interfaces_payload
    else:
        interfaces = []
    interface_map: Dict[str, Dict[str, Any]] = {}
    for entry in interfaces:
        mac_value = extract_mac_from_sdk_interface(entry) or ""
        if not mac_value:
            continue
        interface_map[mac_value.lower()] = dict(entry)
    print("   -> SDK verification snapshot (interfaces):")
    for nic in nic_infos:
        expected_ip = nic.get("prd_ip_address") or nic.get("ip_address")
        expected_mask = nic.get("subnet_mask")
        expected_prefix = mask_to_prefix(expected_mask) if expected_mask else None
        expected_mac = (nic.get("new_mac_address") or nic.get("mac_address") or "").lower()
        label = nic.get("network_name") or expected_mac
        actual_entry = interface_map.get(expected_mac)
        if not actual_entry:
            print(f"      [WARN] MAC {expected_mac} ({label}) was not found in the REST API.")
            success = False
            continue
        actual_ip, actual_prefix = extract_ipv4_from_sdk_interface(actual_entry)
        if expected_ip and actual_ip != expected_ip:
            print(f"      [WARN] MAC {expected_mac}: expected IP {expected_ip} / actual {actual_ip or '(not set)'}")
            success = False
        else:
            print(f"      [OK] MAC {expected_mac}: IP {actual_ip or '(not set)'}")
        if expected_prefix is not None and actual_prefix is not None and expected_prefix != actual_prefix:
            print(f"         [WARN] expected prefix {expected_prefix} / actual {actual_prefix}")
            success = False
    try:
        state_payload = sdk_client.get_networking_state(vm_id)
    except Exception as error:  # pylint: disable=broad-exception-caught
        LOGGER.warning("SDK verification failed to read DNS state: %s", error)
        state_payload = {}
        success = False
    actual_dns = extract_dns_servers_from_state(state_payload)
    expected_dns_set = {str(dns) for dns in expected_dns_servers or []}
    actual_dns_set = {str(dns) for dns in actual_dns if dns}
    if expected_dns_set:
        if expected_dns_set == actual_dns_set:
            print(f"   -> DNS: {sorted(actual_dns_set)}")
        else:
            print(f"   [WARN] DNS expected {sorted(expected_dns_set)} / actual {sorted(actual_dns_set)}")
            success = False
    elif actual_dns_set:
        print(f"   -> DNS: REST API reported {sorted(actual_dns_set)}")
    try:
        route_payload = sdk_client.list_routes(vm_id)
        if isinstance(route_payload, dict):
            route_payload = route_payload.get('value', route_payload)
    except Exception as error:
        LOGGER.warning("SDK verification failed to read routes: %s", error)
        route_payload = []
        success = False
    actual_route_set: Set[Tuple[str, int, str]] = set()
    for route in extract_routes_from_sdk_payload(route_payload):
        network_value = route.get("network")
        prefix_value = route.get("prefix")
        gateway_value = route.get("gateway")
        if network_value is None or prefix_value is None:
            continue
        actual_route_set.add((str(network_value), int(prefix_value), str(gateway_value or "")))
    expected_route_set: Set[Tuple[str, int, str]] = set()
    for route in expected_routes or []:
        network_value = route.get("network")
        prefix_value = route.get("prefix")
        gateway = route.get("gateway") or ""
        if network_value is None:
            continue
        network_str = str(network_value)
        if prefix_value is None and "/" in network_str:
            network_str, _, derived_prefix = network_str.partition("/")
            try:
                prefix_value = int(derived_prefix)
            except (TypeError, ValueError):
                prefix_value = None
        if prefix_value is None:
            continue
        try:
            prefix_int = int(prefix_value)
        except (TypeError, ValueError):
            continue
        expected_route_set.add((network_str, prefix_int, str(gateway)))
    missing_routes = expected_route_set - actual_route_set
    if missing_routes:
        print(f"   [WARN] Missing expected routes: {sorted(missing_routes)}")
        success = False
    return success


def verify_nmcli_connection_settings(
    command_executor: Callable[..., Tuple[int, str, str]],
    connection_name: str,
    device_name: str,
    expected_ip_cidr: str,
    expected_gateway: Optional[str],
    expected_routes: Sequence[str],
    expected_dns_servers: Optional[Sequence[str]] = None,
) -> None:
    """Validate that nmcli reports the expected configuration for the given connection."""
    print(f"   -> Validating nmcli settings (connection: {connection_name})")
    fields = [
        "connection.id",
        "connection.interface-name",
        "ipv4.method",
        "ipv4.addresses",
        "ipv4.gateway",
        "ipv4.dns",
        "ipv4.routes",
    ]
    nmcli_cmd = f"nmcli -g {','.join(fields)} connection show '{connection_name}'"
    exit_code, stdout, stderr = command_executor(nmcli_cmd, check_exit_code=False)
    if exit_code != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(f"nmcli validation error: failed to read settings for '{connection_name}': {detail}")
    values = stdout.splitlines()
    while len(values) < len(fields):
        values.append("")
    data = {field: values[idx].strip() for idx, field in enumerate(fields)}

    def _normalize_list(raw_value: str) -> List[str]:
        normalized = (raw_value or "").strip()
        if not normalized or normalized == "--":
            return []
        items: List[str] = []
        for token in normalized.replace(";", ",").split(","):
            token = token.strip()
            if token:
                items.append(token)
        return items

    def _normalize_route(route_value: str) -> str:
        return " ".join(route_value.split())

    method = data.get("ipv4.method", "").lower()
    if method != "manual":
        raise RuntimeError(
            f"nmcli validation error: ipv4.method expected 'manual' but got '{method or '(empty)'}'."
        )
    addresses = _normalize_list(data.get("ipv4.addresses", ""))
    if expected_ip_cidr and expected_ip_cidr not in addresses:
        raise RuntimeError(
            "nmcli validation error: IPv4 address mismatch. "
            f"expected '{expected_ip_cidr}', actual={addresses or ['(none)']}"
        )
    actual_gateway = data.get("ipv4.gateway", "")
    normalized_gateway = actual_gateway if actual_gateway not in ("--", "") else ""
    if expected_gateway:
        if normalized_gateway != expected_gateway:
            raise RuntimeError(
                "nmcli validation error: default gateway mismatch. "
                f"expected '{expected_gateway}', actual='{normalized_gateway or '(none)'}'"
            )
    else:
        if normalized_gateway:
            raise RuntimeError(
                "nmcli validation error: unexpected default gateway present. "
                f"actual='{normalized_gateway}'"
            )
    actual_routes = {_normalize_route(route) for route in _normalize_list(data.get("ipv4.routes", ""))}
    expected_route_set = {_normalize_route(route) for route in expected_routes}
    missing_routes = expected_route_set - actual_routes
    if missing_routes:
        raise RuntimeError(
            "nmcli validation error: missing static routes. "
            f"missing={sorted(missing_routes)}, actual={sorted(actual_routes) if actual_routes else ['(none)']}"
        )
    expected_dns = [dns for dns in (expected_dns_servers or []) if dns]
    if expected_dns:
        actual_dns = {entry for entry in _normalize_list(data.get("ipv4.dns", ""))}
        expected_dns_set = set(expected_dns)
        missing_dns = expected_dns_set - actual_dns
        if missing_dns:
            raise RuntimeError(
                "nmcli validation error: missing DNS servers. "
                f"missing={sorted(missing_dns)}, actual={sorted(actual_dns) if actual_dns else ['(none)']}"
            )
    if data.get("connection.interface-name") and data["connection.interface-name"] != device_name:
        LOGGER.debug(
            "nmcli connection interface-name mismatch (expected=%s, actual=%s)",
            device_name,
            data["connection.interface-name"],
        )
    print("   -> nmcli settings validation passed")
