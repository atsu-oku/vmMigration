# -*- coding: utf-8 -*-
import os
import ssl
import getpass
import time
import threading
import json
import logging
import ipaddress
import urllib.request
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
try:
    from pyVim.connect import SmartConnect, Disconnect
except ModuleNotFoundError:
    from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl  # type: ignore[import]
try:
    import requests
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning
    urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
from vsphere_sdk_network import VsphereGuestNetworkSDK


@dataclass
class ConnectionCheckParams:
    """
    Configuration parameters controlling guest connectivity retries during
    migration operations.
    """
    max_attempts: int = 5
    wait_seconds: int = 3
    pre_ping_wait_seconds: int = 10
    ping_retry_count: int = 4
    ping_retry_delay: int = 2
    ping_timeout_seconds: int = 2


PRD_STATIC_ROUTE_SEGMENTS = {160, 161, 162, 163, 164}
NMCLI_FIELDS_WITH_TYPE = ['UUID', 'NAME', 'DEVICE', 'TYPE']
NMCLI_FIELDS_NO_TYPE = ['UUID', 'NAME', 'DEVICE']
SSH_ALLOWED_SOURCE_IP = "172.16.164.7"
DEFAULT_CONN_CHECK_PARAMS = ConnectionCheckParams()


class NmcliNotAvailableError(RuntimeError):
    """Raised when nmcli is not available inside the guest."""


# ------------------------------------------------
LOG_LEVEL_NAME = os.environ.get("VSPHERE_CLONE_LOG_LEVEL", "WARNING").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.WARNING)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("cloneAndVmotion")
LOGGER.setLevel(LOG_LEVEL)

ROOT_LOGIN_DISABLED = False

# ------------------------------------------------
# Connection and Migration Settings
# ------------------------------------------------
# --- Source vCenter ---
VCSA_HOST_SOURCE = 'vcsa01s.ipet.local'
VCSA_USER = 'administrator@vsphere.local'
VCSA_PORT = 443

# --- Destination vCenter ---
VCSA_HOST_DEST = 'vcsa01p.ipet.local'

# --- Migration Resources ---
# Datastore used to stage the clone
TARGET_DATASTORE_NAME = 'PMAX-COM-VOL1'
# Final datastore after migration
TARGET_DATASTORE_NAME_FINAL = 'PMAX-PRD-VOL1'
# Destination cluster for compute resources
TARGET_CLUSTER_NAME = 'PRD-Cluster'

# --- Guest OS Credentials ---
GUEST_ROOT_USER = 'root'
GUEST_ROOT_PWD = ''  # Prompted during script execution
GUEST_ADMIN_USER = 'admin'  # Fallback user account
GUEST_ADMIN_PWD = ''  # Prompted during script execution

# ------------------------------------------------
# Helper Functions
# ------------------------------------------------


def prefix_to_subnet_mask(prefix_length):
    """Convert a CIDR prefix length (0-32) to a dotted decimal subnet mask."""
    if not isinstance(prefix_length, int) or not 0 <= prefix_length <= 32:
        return None
    host_bits = 32 - prefix_length
    netmask = (0xffffffff << host_bits) & 0xffffffff
    return '.'.join([str((netmask >> i) & 0xff) for i in (24, 16, 8, 0)])


def calculate_ip_stg_to_prd(ip_address):
    """Map STG IPv4 addresses (third octet 170-179) to PRD range by subtracting 10."""
    if not ip_address:
        return None
    parts = ip_address.split('.')
    if len(parts) != 4:
        raise ValueError(f'Invalid IPv4 format: {ip_address}')
    try:
        octets = [int(x) for x in parts]
    except ValueError as exc:
        raise ValueError(f'Non-numeric value detected in IPv4 address: {ip_address}') from exc
    if any(o < 0 or o > 255 for o in octets):
        raise ValueError(f'IPv4 octet out of range 0-255: {ip_address}')
    if 170 <= octets[2] <= 179:
        octets[2] = octets[2] - 10
        return '.'.join(str(o) for o in octets)
    return ip_address


def mask_to_prefix(netmask):
    """Convert dotted IPv4 netmask to prefix length."""
    if not netmask:
        return None
    try:
        network = ipaddress.IPv4Network(f'0.0.0.0/{netmask}', strict=False)
        return network.prefixlen
    except (ipaddress.NetmaskValueError, ValueError):
        return None


STDERR_ERROR_LITERALS = ('\u30a8\u30e9\u30fc', '\u5931\u6557')
STDERR_ERROR_REGEXES = [
    re.compile(r'(^|\s)error\b', re.IGNORECASE),
    re.compile(r'(^|\s)failed\b', re.IGNORECASE),
    re.compile(r'(^|\s)fatal\b', re.IGNORECASE),
    re.compile(r'traceback \(most recent call last\)', re.IGNORECASE),
]
LEGACY_INTERFACE_PATTERN = re.compile(r'^(ens|eno|enp|enx|eth|em)[0-9a-z\-]*$', re.IGNORECASE)

VCENTER_KEEPALIVE_SECONDS = int(os.environ.get("VSPHERE_CLONE_KEEPALIVE_SECONDS", "240"))


def _start_keepalive_thread(
    service_instance,
    label: str,
    interval: int = VCENTER_KEEPALIVE_SECONDS,
) -> Optional[Tuple[threading.Thread, threading.Event]]:
    """Start a background thread that periodically calls CurrentTime on the service instance."""
    if not service_instance or interval <= 0:
        return None
    stop_event = threading.Event()

    def _keepalive_loop() -> None:
        while not stop_event.wait(interval):
            try:
                service_instance.CurrentTime()
            except Exception:  # pylint: disable=broad-exception-caught
                LOGGER.debug("Keep-alive ping failed for %s; stopping keep-alive thread.", label, exc_info=True)
                break
    thread = threading.Thread(target=_keepalive_loop, name=f"{label}-keepalive", daemon=True)
    thread.start()
    return thread, stop_event


def _stop_keepalive_thread(
    handle: Optional[Tuple[threading.Thread, threading.Event]],
    timeout: float = 5.0,
) -> None:
    """Signal the keep-alive thread to stop and wait briefly for it to exit."""
    if not handle:
        return
    thread, stop_event = handle
    stop_event.set()
    try:
        thread.join(timeout)
    # pylint: disable-next=broad-exception-caught
    except Exception:
        LOGGER.debug("Error while joining keep-alive thread", exc_info=True)


def _make_nmcli_detail_fetcher(
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


def _compact_interface_name(name):
    lowered = (name or "").lower()
    return lowered.replace('-', '').replace('_', '').replace(' ', '')


def _parse_ip_json_output(raw_text):
    data = json.loads(raw_text)
    interfaces = []
    for entry in data:
        ifname = entry.get("ifname")
        if not ifname:
            continue
        mac = entry.get("address") or ""
        ipv4_entries = []
        for addr_info in entry.get("addr_info", []):
            if addr_info.get("family") == "inet" and addr_info.get("local"):
                ipv4_entries.append(
                    {
                        "address": addr_info.get("local"),
                        "prefix_length": addr_info.get("prefixlen"),
                    }
                )
        interfaces.append(
            {
                "ifname": ifname,
                "mac": mac,
                "ipv4": ipv4_entries,
            }
        )
    return interfaces


def _parse_ip_addr_text(raw_text):
    interfaces = []
    current = None
    for raw_line in raw_text.splitlines():
        if not raw_line:
            continue
        if raw_line and not raw_line[0].isspace():
            match = re.match(r'^\d+:\s*([^:]+):', raw_line)
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
                if '/' in address_part:
                    ip_part, prefix_part = address_part.split('/', 1)
                    try:
                        prefix_len = int(prefix_part)
                    except ValueError:
                        prefix_len = None
                else:
                    ip_part = address_part
                    prefix_len = None
                current["ipv4"].append({"address": ip_part, "prefix_length": prefix_len})
    return interfaces


def _parse_ifconfig_output(raw_text):
    interfaces = []
    current = None
    for raw_line in raw_text.splitlines():
        if not raw_line:
            continue
        if not raw_line.startswith((" ", "\t")):
            if ":" in raw_line:
                ifname = raw_line.split(":", 1)[0].strip()
            else:
                ifname = raw_line.strip()
            current = {"ifname": ifname, "mac": "", "ipv4": []}
            interfaces.append(current)
            continue
        if current is None:
            continue
        stripped = raw_line.strip()
        if stripped.startswith(("ether ", "HWaddr ")):
            parts = stripped.split()
            if len(parts) >= 2:
                current["mac"] = parts[1]
        elif stripped.startswith("inet "):
            parts = stripped.split()
            address_value = None
            prefix_len = None
            for idx, token in enumerate(parts):
                if token == "inet" and idx + 1 < len(parts):
                    address_value = parts[idx + 1]
                elif token in ("netmask", "Mask") and idx + 1 < len(parts):
                    prefix_len = mask_to_prefix(parts[idx + 1])
            if address_value:
                current["ipv4"].append({"address": address_value, "prefix_length": prefix_len})
    return interfaces


def _find_gateway_owner_index(
    nic_records: List[Dict[str, Any]],
    gateway_ip: Optional[str],
) -> Optional[int]:
    """Return the index of the NIC whose STG network contains the provided gateway."""
    if not gateway_ip:
        return None
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


def _select_default_gateway_route(
    candidates: List[Dict[str, Any]],
    nic_records: List[Dict[str, Any]],
) -> Optional[Tuple[str, int]]:
    """Choose the most appropriate default gateway entry among the provided candidates."""
    network_matches: List[Tuple[str, int]] = []
    owner_only_matches: List[Tuple[str, int]] = []
    for route_entry in candidates:
        gateway_address = route_entry.get("gateway")
        owner_idx: Optional[int] = None
        if gateway_address:
            owner_idx = route_entry.get("owner_index")
            if owner_idx is not None and not 0 <= owner_idx < len(nic_records):
                owner_idx = None
            if owner_idx is None:
                owner_idx = _find_gateway_owner_index(nic_records, gateway_address)
        if gateway_address and owner_idx is not None:
            owner_nic = nic_records[owner_idx]
            nic_ip = owner_nic.get("ip_address")
            nic_mask = owner_nic.get("subnet_mask")
            gateway_in_network = False
            if nic_ip and nic_mask:
                try:
                    owner_network = ipaddress.IPv4Interface(f"{nic_ip}/{nic_mask}").network
                    gateway_in_network = ipaddress.IPv4Address(gateway_address) in owner_network
                except (ValueError, ipaddress.AddressValueError):
                    gateway_in_network = False
            if gateway_in_network:
                network_matches.append((gateway_address, owner_idx))
            else:
                owner_only_matches.append((gateway_address, owner_idx))
    if network_matches:
        return network_matches[0]
    if owner_only_matches:
        return owner_only_matches[0]
    return None


def _infer_gateway_from_routes(
    nic_records: List[Dict[str, Any]],
    static_routes: List[Dict[str, Any]],
) -> Optional[Tuple[str, int]]:
    """Infer a default gateway by analysing non-default static routes pointing at remote gateways."""
    usage: Dict[Tuple[str, int], int] = {}
    for route in static_routes:
        gateway = route.get("gateway")
        if not gateway:
            continue

        owner_idx = route.get("owner_index")
        if owner_idx is not None and not 0 <= owner_idx < len(nic_records):
            owner_idx = None
        if owner_idx is None:
            owner_idx = _find_gateway_owner_index(nic_records, gateway)
        if owner_idx is None:
            continue

        key = (gateway, owner_idx)
        usage[key] = usage.get(key, 0) + 1

    if not usage:
        return None

    # Stable ordering: highest usage first, then lowest owner index, lastly lexical gateway
    sorted_usage = sorted(
        usage.items(),
        key=lambda item: (-item[1], item[0][1], item[0][0]),
    )
    (gateway, owner_idx), _ = sorted_usage[0]
    return gateway, owner_idx


def _derive_fallback_gateway(nic_records: List[Dict[str, Any]]) -> Optional[Tuple[str, int]]:
    """Derive a plausible STG default gateway and owning NIC when none was supplied."""
    best_candidate: Optional[Tuple[str, int]] = None
    for idx, nic in enumerate(nic_records):
        ip_addr = nic.get("ip_address")
        subnet_mask = nic.get("subnet_mask")
        if not ip_addr or not subnet_mask:
            continue
        try:
            stg_iface = ipaddress.IPv4Interface(f"{ip_addr}/{subnet_mask}")
        except (ValueError, ipaddress.AddressValueError):
            continue
        network = stg_iface.network
        hosts_iter = network.hosts()
        try:
            gateway_addr = next(hosts_iter)
            if str(gateway_addr) == ip_addr:
                gateway_addr = next(hosts_iter)
        except StopIteration:
            continue
        if gateway_addr is None:
            continue
        gateway_str = str(gateway_addr)
        if nic.get("is_gateway_nic"):
            return gateway_str, idx
        if best_candidate is None:
            best_candidate = (gateway_str, idx)
    return best_candidate


def collect_interface_inventory(command_executor):
    """Return a list of interface metadata dictionaries for the guest OS."""
    attempts = [
        ("ip -j -p addr", _parse_ip_json_output),
        ("ip -d addr", _parse_ip_addr_text),
        ("ip addr", _parse_ip_addr_text),
        ("ifconfig -a", _parse_ifconfig_output),
    ]
    for command, parser in attempts:
        LOGGER.debug("Collecting NIC info via command: %s", command)
        exit_code, stdout, stderr = command_executor(command, check_exit_code=False)
        LOGGER.debug("Command '%s' exited with code %s", command, exit_code)
        if exit_code != 0 or not stdout:
            if stderr:
                LOGGER.debug("Command '%s' stderr: %s", command, stderr)
            continue
        # Attempt to parse the command output; best-effort fallback for unexpected formats.
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


def split_nmcli_terse_line(line):
    """Split an nmcli --terse output line taking escaped separators into account."""
    fields = []
    buffer = []
    escape = False
    for char in line:
        if escape:
            buffer.append(char)
            escape = False
        elif char == '\\':
            escape = True
        elif char == ':':
            fields.append(''.join(buffer))
            buffer = []
        else:
            buffer.append(char)
    fields.append(''.join(buffer))
    return fields


def parse_nmcli_connection_output(output, field_names):
    """Return a list of connection dicts parsed from nmcli terse output."""
    connections = []
    if not output:
        return connections
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = split_nmcli_terse_line(line)
        entry = {}
        for idx, field in enumerate(field_names):
            value = parts[idx].strip() if idx < len(parts) else ''
            entry[field] = value
        connections.append(entry)
    return connections


def ensure_connection_activation(
    command_executor,
    connection_name,
    device_name,
    ping_targets=None,
    params: ConnectionCheckParams | None = None,
):
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
        summary = ', '.join(targets) if targets else 'none'
        raise RuntimeError(
            f"Interface '{device_name}' failed connectivity checks (targets: {summary})"
        )

    for attempt in range(1, config.max_attempts + 1):
        print(
            f"   -> Verifying connection '{connection_name}' "
            f"(attempt {attempt}/{config.max_attempts})"
        )
        _, state_output, _ = command_executor(
            f"nmcli -t -f GENERAL.STATE connection show '{connection_name}'",
            check_exit_code=False,
        )
        state_normalized = (state_output or '').strip().lower()
        if 'activated' not in state_normalized:
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
    summary = ', '.join(targets) if targets else 'none'
    message = (
        f"Connection '{connection_name}' failed connectivity checks "
        f"(targets: {summary})"
    )
    raise RuntimeError(message)

def ensure_firewall_allows_ssh(command_executor, source_ip):
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
            firewalld_active = exit_code == 0 and (firewalld_status or '').strip() == 'active'
    if firewalld_active:
        _, default_zone, _ = command_executor(
            "firewall-cmd --get-default-zone",
            check_exit_code=False,
        )
        zone = (default_zone or 'public').splitlines()[0].strip() or 'public'
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
            check_rule_cmd = (
                f"iptables -C INPUT -p tcp -s {source_ip} --dport 22 -j ACCEPT"
            )
            exit_code, _, _ = command_executor(check_rule_cmd, check_exit_code=False)
            if exit_code != 0:
                add_rule_cmd = (
                    f"iptables -I INPUT 1 -p tcp -s {source_ip} --dport 22 -j ACCEPT"
                )
                command_executor(add_rule_cmd, check_exit_code=False)
                print(f"      - iptables: added SSH allow rule ({source_ip})")
                command_executor("iptables-save > /etc/sysconfig/iptables", check_exit_code=False)
                return
            print(f"      - iptables: SSH allow rule already present ({source_ip})")
            return
    print("      - firewalld / iptables unavailable; skipping firewall adjustments.")

def configure_interface_without_nmcli(
    command_executor,
    interface_name: str,
    new_ip: str,
    prefix: int,
    expected_gateway: Optional[str],
    routes_for_nic: List[Tuple[int, Dict[str, Any]]],
    dns_servers: Optional[List[str]] = None,
) -> Tuple[List[int], List[str]]:
    """Configure a guest interface when nmcli is unavailable."""
    selected_route_indices: List[int] = []
    selected_route_lines: List[str] = []
    route_commands: List[str] = []
    for route_idx, route_info in routes_for_nic:
        gateway = route_info.get('gateway')
        route_prefix = route_info.get('prefix')
        network_base = route_info.get('network')
        if not network_base or not gateway:
            continue
        network_cidr = f"{network_base}/{route_prefix}" if route_prefix is not None else network_base
        route_commands.append(f"ip route replace {network_cidr} via {gateway} dev {interface_name}")
        selected_route_indices.append(route_idx)
        selected_route_lines.append(f"{network_cidr} {gateway}")
        script_lines = [
            "set -e",
            f"ip link set {interface_name} down || true",
            f"ip addr flush dev {interface_name} || true",
            f"ip -6 addr flush dev {interface_name} || true",
            f"sysctl -w net.ipv6.conf.{interface_name}.disable_ipv6=1 || true",
            f"ip addr add {new_ip}/{prefix} dev {interface_name}",
            f"ip link set {interface_name} up",
        ]
    if expected_gateway:
        script_lines.append(f"ip route replace default via {expected_gateway} dev {interface_name}")
        script_lines.extend(route_commands)
        command_executor("\n".join(script_lines))
    if dns_servers:
        dns_content = "\n".join(f"nameserver {dns}" for dns in dns_servers if dns) + "\n"
        dns_script = (
            "set -e\n"
            "cp /etc/resolv.conf /etc/resolv.conf.vsphere.bak 2>/dev/null || true\n"
            "cat <<'EOF' > /etc/resolv.conf\n"
            f"{dns_content}"
            "EOF\n"
        )
        command_executor(dns_script, check_exit_code=False)
        ifcfg_lines = [
            f"DEVICE={interface_name}",
            "BOOTPROTO=none",
            "ONBOOT=yes",
            "NM_CONTROLLED=no",
            "PEERDNS=no",
            f"IPADDR={new_ip}",
            f"PREFIX={prefix}",
        ]
    if expected_gateway:
        ifcfg_lines.append(f"GATEWAY={expected_gateway}")
        ifcfg_lines.append("DEFROUTE=yes")
    else:
        ifcfg_lines.append("DEFROUTE=no")
    if dns_servers:
        for index, dns in enumerate([dns for dns in dns_servers if dns], start=1):
            ifcfg_lines.append(f"DNS{index}={dns}")
            ifcfg_content = "\n".join(ifcfg_lines) + "\n"
            persist_ifcfg_script = (
                "if [ -d /etc/sysconfig/network-scripts ]; then\n"
                f"  cat <<'EOF' > /etc/sysconfig/network-scripts/ifcfg-{interface_name}\n"
                f"{ifcfg_content}"
                "EOF\n"
                "fi\n"
            )
    command_executor(persist_ifcfg_script, check_exit_code=False)
    if selected_route_lines:
        route_content = "\n".join(
            f"{line} dev {interface_name}" if " dev " not in line else line for line in selected_route_lines
        ) + "\n"
        persist_routes_script = (
            "if [ -d /etc/sysconfig/network-scripts ]; then\n"
            f"  cat <<'EOF' > /etc/sysconfig/network-scripts/route-{interface_name}\n"
            f"{route_content}"
            "EOF\n"
            "fi\n"
        )
        command_executor(persist_routes_script, check_exit_code=False)
    else:
        cleanup_routes_script = (
            "if [ -d /etc/sysconfig/network-scripts ]; then\n"
            f"  rm -f /etc/sysconfig/network-scripts/route-{interface_name}\n"
            "fi\n"
        )
        command_executor(cleanup_routes_script, check_exit_code=False)
    print("   -> Applied legacy network configuration (nmcli unavailable).")
    return selected_route_indices, selected_route_lines


def determine_prd_static_routes(nic_infos, default_gateway, original_routes):
    """Return PRD static routes with ownership metadata based on original STG routes."""
    routes: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, int, str, Optional[int]]] = set()
    local_networks: Dict[int, ipaddress.IPv4Network] = {}
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
    for route in original_routes:
        network = route.get("network")
        prefix = route.get("prefix")
        gateway = route.get("gateway")
        if not network or prefix is None:
            continue
        if str(network) == "0.0.0.0":
            try:
                if int(prefix) == 0:
                    # Default routes are handled separately via default gateway logic
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
        route_owner_idx: Optional[int] = route.get('owner_index')
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
            except ipaddress.AddressValueError:
                route_owner_idx = None
        if route_owner_idx is None:
            for idx, nic in enumerate(nic_infos):
                if nic.get("is_gateway_nic"):
                    route_owner_idx = idx
                    break
        if route_owner_idx is None and nic_infos:
            route_owner_idx = 0
        network_address = str(prd_network.network_address)
        prefix_length = prd_network.prefixlen
        key = (network_address, prefix_length, prd_gateway, owner_index)
        if key in seen:
            continue
        seen.add(key)
        routes.append(
            {
                "network": network_address,
                "prefix": prefix_length,
                "gateway": prd_gateway,
                "owner_index": owner_index,
            }
        )
    return routes


def verify_destination_network_with_sdk(
    sdk_client: VsphereGuestNetworkSDK,
    vm_id: str,
    nic_infos: List[Dict[str, Any]],
    expected_dns_servers: List[str],
    expected_routes: List[Dict[str, Any]],
) -> bool:
    """Check destination guest networking state via the Automation SDK."""
    success = True
    try:
        interfaces = sdk_client.list_interfaces(vm_id, retries=6, delay_seconds=5.0)
    except Exception as error:
        LOGGER.warning("SDK verification failed to read interfaces: %s", error)
        return False
    interface_map: Dict[str, Dict[str, Any]] = {}
    candidate_keys = (
        'mac_address',
        'mac',
        'macAddress',
    )
    for entry in interfaces:
        mac_value = ''
        for key in candidate_keys:
            value = entry.get(key)
            if isinstance(value, str) and value:
                mac_value = value
                break
        if not mac_value:
            link_info = entry.get('link') or entry.get('link_info') or {}
            if isinstance(link_info, dict):
                for key in candidate_keys:
                    value = link_info.get(key)
                    if isinstance(value, str) and value:
                        mac_value = value
                        break
        if not mac_value:
            continue
        interface_map[mac_value.lower()] = dict(entry)
    print("   -> SDK verification snapshot (interfaces):")
    for nic in nic_infos:
        expected_ip = nic.get('prd_ip_address') or nic.get('ip_address')
        expected_mask = nic.get('subnet_mask')
        expected_prefix = mask_to_prefix(expected_mask) if expected_mask else None
        expected_mac = (nic.get('new_mac_address') or nic.get('mac_address') or '').lower()
        label = nic.get('network_name') or expected_mac
        actual_entry = interface_map.get(expected_mac)
        if not actual_entry:
            print(f"      [WARN] MAC {expected_mac} ({label}) was not found in the REST API.")
            success = False
            continue
        ip_data = actual_entry.get('ip') or {}
        actual_ip = None
        actual_prefix = None
        for entry in ip_data.get('ip_addresses') or []:
            if isinstance(entry, dict) and entry.get('ip_address') and '.' in entry.get('ip_address'):
                actual_ip = entry.get('ip_address')
                actual_prefix = entry.get('prefix_length')
                break
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
    actual_dns = []
    if isinstance(state_payload, dict):
        actual_dns = state_payload.get('dns', {}).get('ip_addresses') or []
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
    except Exception as error:
        LOGGER.warning("SDK verification failed to read routes: %s", error)
        route_payload = []
        success = False
        actual_route_set = set()
        for route in route_payload or []:
            network = route.get('network')
            prefix = route.get('prefix_length')
            gateway = route.get('gateway_address') or ''
        if network is None or prefix is None:
            continue
        try:
            prefix_int = int(prefix)
        except (TypeError, ValueError):
            continue
        actual_route_set.add((str(network), prefix_int, str(gateway)))
        expected_route_set = set()
        for route in expected_routes or []:
            network_value = route.get('network')
            prefix_value = route.get('prefix')
            gateway = route.get('gateway') or ''
        if network_value is None:
            continue
        network_str = str(network_value)
        if prefix_value is None and '/' in network_str:
            network_str, _, derived_prefix = network_str.partition('/')
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
        extra_routes = actual_route_set - expected_route_set
        if missing_routes:
            print(f"   [WARN] Missing expected routes: {sorted(missing_routes)}")
            success = False
        if extra_routes:
            print(f"   -> Additional routes (for reference): {sorted(extra_routes)}")
        return success

def verify_nmcli_connection_settings(
    command_executor,
    connection_name: str,
    device_name: str,
    expected_ip_cidr: str,
    expected_gateway: Optional[str],
    expected_routes: List[str],
    expected_dns_servers: Optional[List[str]] = None,
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


def find_vm_by_name(content, name):
    """Return a VM object by name (or None)."""
    if not name:
        return None
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        for vm in view.view:
            if vm.name == name:
                return vm
    finally:
        view.Destroy()
    return None


def wait_for_vm_availability(content, name, retries=30, delay_seconds=2):
    """Wait until the VM with the given name becomes available; return the VM or raise."""
    for _ in range(max(1, retries)):
        vm = find_vm_by_name(content, name)
        if vm is not None:
            return vm
        time.sleep(max(1, delay_seconds))
    raise RuntimeError(f"Destination vCenter did not contain VM '{name}' (timed out).")


class GuestCommandExecutor:
    def __init__(self, guest_op_manager, root_auth, admin_auth, admin_pwd):
        self.process_manager = guest_op_manager.processManager
        self.file_manager = guest_op_manager.fileManager
        self.root_auth = root_auth
        self.admin_auth = admin_auth
        self.admin_pwd = admin_pwd

    def run(self, vm, command, check_exit_code=True):
        global ROOT_LOGIN_DISABLED

        print("[GUEST-CMD] Planned command:")
        print(f"  {command}")

        exit_code: int = -1
        stdout: str = ""
        stderr: str = ""
        auth_used: Optional[str] = None
        fallback_error: Optional[Exception] = None

        if not ROOT_LOGIN_DISABLED and self.root_auth:
            try:
                auth_used = "root"
                exit_code, stdout, stderr = self._run_command(vm, self.root_auth, command)
            except vim.fault.InvalidGuestLogin as error:
                fallback_error = error
                ROOT_LOGIN_DISABLED = True
                if self._can_use_admin():
                    print("[GUEST-CMD] root authentication failed -> retrying as admin user.")
                    exit_code, stdout, stderr = self._run_as_admin(vm, command)
                    auth_used = "admin"
                else:
                    raise RuntimeError(
                        "Root authentication failed and no admin fallback credentials were provided."
                    ) from error
            except vim.fault.GuestOperationsFault as error:
                message = (getattr(error, "msg", "") or "").lower()
                if "auth" in message or "permission" in message:
                    fallback_error = error
                    ROOT_LOGIN_DISABLED = True
                    if self._can_use_admin():
                        print("[GUEST-CMD] root authentication failed -> retrying as admin user.")
                        exit_code, stdout, stderr = self._run_as_admin(vm, command)
                        auth_used = "admin"
                    else:
                        raise RuntimeError(
                            "Root authentication failed and no admin fallback credentials were provided."
                        ) from error
                else:
                    raise

        if auth_used is None:
            if self._can_use_admin():
                if not self.root_auth or ROOT_LOGIN_DISABLED:
                    print("[GUEST-CMD] root authentication disabled; running command as admin user.")
                exit_code, stdout, stderr = self._run_as_admin(vm, command)
                auth_used = "admin"
            else:
                raise RuntimeError("Root authentication is disabled and admin credentials are unavailable.")

        print("[GUEST-CMD] STDOUT:\n---\n" + (stdout or "(none)") + "\n---")
        print("[GUEST-CMD] STDERR:\n---\n" + (stderr or "(none)") + "\n---")

        stderr_indicates_error = False
        if stderr:
            if any(literal in stderr for literal in STDERR_ERROR_LITERALS):
                stderr_indicates_error = True
            else:
                for regex in STDERR_ERROR_REGEXES:
                    if regex.search(stderr):
                        stderr_indicates_error = True
                        break

        command_success = (exit_code == 0) and not stderr_indicates_error
        if command_success:
            print("[GUEST-CMD] Result: success")
        else:
            print("[GUEST-CMD] Result: failure")
            combined_cli_output = ((stderr or "") + "\n" + (stdout or "")).lower()
            reason = (stderr or "").strip() or "Unknown error"
            if "nmcli" in command and ("command not found" in combined_cli_output or exit_code == 127):
                raise NmcliNotAvailableError(command)
            if not check_exit_code:
                return exit_code, stdout, stderr
            if exit_code != 0:
                reason = (stderr or "").strip() or "Exit code was not 0"
            elif stderr_indicates_error:
                reason = (stderr or "").strip() or "Error text found in standard error output"
            if fallback_error is not None and auth_used == "admin":
                raise RuntimeError(
                    f"Failed to run command as admin user (exit code {exit_code}, reason: {reason})"
                ) from fallback_error
            raise RuntimeError(
                f"Failed to execute command (exit code {exit_code}, reason: {reason})"
            )

        return exit_code, stdout, stderr

    def _can_use_admin(self) -> bool:
        return bool(self.admin_auth and self.admin_pwd)

    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        ctx_inner = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx_inner.check_hostname = False
        ctx_inner.verify_mode = ssl.CERT_NONE
        return ctx_inner

    def _wrap_command(self, command: str) -> str:
        return (
            "{ orig_lang=${LANG-}; orig_lc_all=${LC_ALL-}; "
            "restore_locale() { "
            'if [ -n "$orig_lc_all" ]; then export LC_ALL="$orig_lc_all"; else unset LC_ALL; fi; '
            'if [ -n "$orig_lang" ]; then export LANG="$orig_lang"; else unset LANG; fi; '
            "}; "
            "trap restore_locale EXIT; "
            "export LC_ALL=C; "
            f"{command}; "
            "cmd_status=$?; "
            "trap - EXIT; restore_locale; "
            "exit $cmd_status; }"
        )

    def _run_command(self, vm, auth, command: str) -> Tuple[int, str, str]:
        stdout_path = f"/tmp/stdout_{os.urandom(4).hex()}.log"
        stderr_path = f"/tmp/stderr_{os.urandom(4).hex()}.log"
        redirected_cmd = f"{self._wrap_command(command)} > {stdout_path} 2> {stderr_path}"
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/bash",
            arguments=f"-lc {shlex.quote(redirected_cmd)}",
        )
        try:
            pid = self.process_manager.StartProgramInGuest(vm=vm, auth=auth, spec=spec)
            exit_code = self._wait_for_exit(vm, auth, pid)
            stdout_data = self._download_guest_file(vm, auth, stdout_path)
            stderr_data = self._download_guest_file(vm, auth, stderr_path)
            return exit_code, stdout_data.strip(), stderr_data.strip()
        finally:
            self._cleanup_guest_files(vm, auth, (stdout_path, stderr_path))

    def _wait_for_exit(self, vm, auth, pid, timeout_seconds: int = 300) -> int:
        exit_code = -1
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            procs = self.process_manager.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid])
            if procs and procs[0].exitCode is not None:
                exit_code = procs[0].exitCode
                break
            time.sleep(2)
        return exit_code

    def _download_guest_file(self, vm, auth, guest_path: str) -> str:
        try:
            file_info = self.file_manager.InitiateFileTransferFromGuest(
                vm=vm,
                auth=auth,
                guestFilePath=guest_path,
            )
            if REQUESTS_AVAILABLE:
                response = requests.get(file_info.url, verify=False, timeout=30)
                if response.status_code == 200:
                    return response.text
            else:
                ctx_inner = self._create_ssl_context()
                with urllib.request.urlopen(file_info.url, context=ctx_inner) as resp:
                    return resp.read().decode("utf-8", errors="replace")
        except vim.fault.FileNotFound:
            return ""
        except Exception:
            return ""
        return ""

    def _cleanup_guest_files(self, vm, auth, guest_paths: Tuple[str, ...]) -> None:
        for path in guest_paths:
            try:
                self.file_manager.DeleteFileInGuest(vm=vm, auth=auth, filePath=path)
            except (vim.fault.FileNotFound, vim.fault.GuestOperationsFault):
                pass

    def _run_as_admin(self, vm, command: str) -> Tuple[int, str, str]:
        if not self._can_use_admin():
            raise RuntimeError("Admin credentials are not available for guest operations.")
        temp_password = None
        try:
            temp_password = self._create_temp_password_file(vm)
            result = self._run_command(vm, self.admin_auth, self._build_sudo_command(command, temp_password))
            retry_exit_code, _, retry_stderr = result
            if self._requires_tty_retry(retry_exit_code, retry_stderr):
                print("[GUEST-CMD] sudo requires a TTY; retrying via script wrapper.")
                result = self._run_command(
                    vm,
                    self.admin_auth,
                    self._build_sudo_command(command, temp_password, use_script_wrapper=True),
                )
            return result
        finally:
            if temp_password:
                self._cleanup_temp_file(vm, temp_password, self.admin_auth)

    def _create_temp_password_file(self, vm) -> str:
        temp_password = self.file_manager.CreateTemporaryFileInGuest(
            vm=vm,
            auth=self.admin_auth,
            prefix="sudo_pass_",
            suffix=".tmp",
            directoryPath="/tmp",
        )
        password_bytes = (self.admin_pwd + "\n").encode("utf-8")
        file_attr = vim.vm.guest.FileManager.FileAttributes()
        upload_url = self.file_manager.InitiateFileTransferToGuest(
            vm=vm,
            auth=self.admin_auth,
            guestFilePath=temp_password,
            fileAttributes=file_attr,
            fileSize=len(password_bytes),
            overwrite=True,
        )
        request = urllib.request.Request(
            upload_url,
            data=password_bytes,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(request, context=self._create_ssl_context()):
            pass
        return temp_password

    def _cleanup_temp_file(self, vm, guest_path: str, auth) -> None:
        try:
            self.file_manager.DeleteFileInGuest(vm=vm, auth=auth, filePath=guest_path)
        except vim.fault.FileNotFound:
            pass
        except Exception:
            try:
                rm_spec = vim.vm.guest.ProcessManager.ProgramSpec(
                    programPath="/bin/rm",
                    arguments=f"-f {shlex.quote(guest_path)}",
                )
                self.process_manager.StartProgramInGuest(vm=vm, auth=auth, spec=rm_spec)
            except Exception:
                pass

    @staticmethod
    def _build_sudo_command(command: str, temp_password: str, use_script_wrapper: bool = False) -> str:
        quoted_command = shlex.quote(command)
        base_cmd = f"sudo -S -p '' /bin/bash -lc {quoted_command}"
        if use_script_wrapper:
            return (
                f"cat {shlex.quote(temp_password)} | "
                f"script -q -c {shlex.quote(base_cmd)} /dev/null"
            )
        return f"cat {shlex.quote(temp_password)} | {base_cmd}"

    @staticmethod
    def _requires_tty_retry(exit_code: int, stderr: str) -> bool:
        stderr_lower = (stderr or "").lower()
        return exit_code != 0 and (
            "no tty present" in stderr_lower or "must have a tty" in stderr_lower
        )

def execute_command_in_guest(
    guest_op_manager,
    vm,
    root_auth,
    admin_auth,
    admin_pwd,
    command,
    check_exit_code=True,
):
    executor = GuestCommandExecutor(guest_op_manager, root_auth, admin_auth, admin_pwd)
    return executor.run(vm, command, check_exit_code=check_exit_code)

# ------------------------------------------------
# 1. Enter passwords
# ------------------------------------------------
try:
    VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
    VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
except Exception as error:
    print('ERROR:', error)
    exit(1)
ROOT_LOGIN_DISABLED = False

# ------------------------------------------------
# 2. Configure SSL context
# ------------------------------------------------
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# ------------------------------------------------
# 3. Collect target VM name and guest credentials
# ------------------------------------------------
target_vm_name = input("Enter the name of the VM to clone: ")
if not target_vm_name:
    print("No VM name provided. Aborting processing.")
    exit(0)
try:
    GUEST_ROOT_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ROOT_USER}': ")
    GUEST_ADMIN_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ADMIN_USER}' (for fallback): ")
except Exception as error:
    print('ERROR:', error)
    exit(1)

# ------------------------------------------------
# Main processing
# ------------------------------------------------
clone_name = None
vmx_path = None
new_vm_on_source = None
migrated_vm_for_rollback = None
migrated_vm_name_for_rollback = None
migrated_vm: Any | None = None
unregistered_from_source = False
original_nic_info: List[Dict[str, Any]] = []
original_dns_servers: List[str] = []
original_default_gateway: str | None = None
original_static_routes: List[Dict[str, Any]] = []
si_source = None
si_dest = None
sdk_network_client: VsphereGuestNetworkSDK | None = None
source_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
dest_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
try:
    # --- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---
    print("\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---")
    print(f"   Attempting to connect to source vCenter ({VCSA_HOST_SOURCE})...")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source:
        raise ConnectionError(f"Failed to authenticate to source vCenter ({VCSA_HOST_SOURCE}).")
    print("   [OK] Source vCenter authentication succeeded.")
    Disconnect(si_source)
    si_source = None
    print(f"   Attempting to connect to destination vCenter ({VCSA_HOST_DEST})...")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest:
        raise ConnectionError(f"Failed to authenticate to destination vCenter ({VCSA_HOST_DEST}).")
    print("   [OK] Destination vCenter authentication succeeded.")
    Disconnect(si_dest)
    si_dest = None

    # --- [Phase 1/7] Source vCenter: Collect Info & Prepare ---
    print("\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source:
        raise ConnectionError(f"Unable to connect to source vCenter ({VCSA_HOST_SOURCE}).")
    print("[OK] Connected to source vCenter.")
    source_keepalive_handle = _start_keepalive_thread(si_source, "source-vcenter")

    content_source = si_source.RetrieveContent()

    target_vm = next(
        (
            vm
            for vm in content_source.viewManager.CreateContainerView(
                content_source.rootFolder, [vim.VirtualMachine], True
            ).view
            if vm.name == target_vm_name
        ),
        None,
    )
    if not target_vm:
        raise FileNotFoundError(f"VM '{target_vm_name}' was not found.")
    print(f"[OK] Located VM '{target_vm.name}'.")
    if target_vm.guest.toolsRunningStatus != "guestToolsRunning":
        raise SystemError("Source VM must be powered on with VMware Tools running to collect IP information.")
    sdk_source_client = None
    source_interfaces: List[Dict[str, Any]] = []
    source_networking_state: Dict[str, Any] = {}
    source_routes: List[Dict[str, Any]] = []
    sdk_interfaces_by_mac: Dict[str, Tuple[Dict[str, Any], int]] = {}
    if REQUESTS_AVAILABLE:
        sdk_vm_id_source = getattr(target_vm, "_moId", None)
        if sdk_vm_id_source:
            try:
                sdk_source_client = VsphereGuestNetworkSDK(
                    host=VCSA_HOST_SOURCE,
                    username=VCSA_USER,
                    password=VCSA_PWD_SOURCE,
                    verify_ssl=False,
                )
                source_interfaces = sdk_source_client.list_interfaces(sdk_vm_id_source)
                source_networking_state = sdk_source_client.get_networking_state(sdk_vm_id_source)
                source_routes = sdk_source_client.list_routes(sdk_vm_id_source)
            except Exception as sdk_error:
                LOGGER.warning("Failed to collect source VM network info via API: %s", sdk_error)
            finally:
                if sdk_source_client:
                    sdk_source_client.close()
            for idx, iface in enumerate(source_interfaces):
                mac_candidate = (iface.get("mac_address") or iface.get("mac") or "").lower()
                if mac_candidate:
                    sdk_interfaces_by_mac[mac_candidate] = (iface, idx)
    print("   Verified that VMware Tools is running.")
    print("   Gathering NIC information from the source VM...")
    guest_net_map = {nic.macAddress: nic for nic in target_vm.guest.net if nic.macAddress}
    missing_ipv4_messages = []
    for device in target_vm.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualEthernetCard):
            continue
        mac = device.macAddress
        mac_lower = (mac or '').lower()
        guest_nic = guest_net_map.get(mac)
        network_name = None
        if isinstance(device.backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
            network_name = device.backing.network.name
        elif isinstance(device.backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
            if guest_nic and getattr(guest_nic, 'network', None):
                network_name = guest_nic.network
        if not network_name and guest_nic and getattr(guest_nic, 'network', None):
            network_name = guest_nic.network
        if not network_name:
            device_info = getattr(device, 'deviceInfo', None)
            label = getattr(device_info, 'label', None) if device_info else None
            summary = getattr(device_info, 'summary', None) if device_info else None
            network_name = label or summary or 'Unknown Network'
            sdk_iface_entry = sdk_interfaces_by_mac.get(mac_lower)
            nic_ip_address = None
            prefix_len = None
            sdk_interface_index = None
            sdk_nic_id = None
        if sdk_iface_entry:
            iface_data, iface_idx = sdk_iface_entry
            sdk_interface_index = iface_idx
            sdk_nic_id = iface_data.get('nic')
            ip_data = iface_data.get('ip') or {}
            ip_entries = ip_data.get('ip_addresses') or []
        for ip_entry in ip_entries:
            if isinstance(ip_entry, dict) and ip_entry.get('ip_address') and '.' in ip_entry.get('ip_address'):
                nic_ip_address = ip_entry.get('ip_address')
                prefix_len = ip_entry.get('prefix_length')
                break
            subnet_mask = prefix_to_subnet_mask(prefix_len) if prefix_len is not None else None
            if not nic_ip_address and guest_nic and guest_nic.ipConfig and getattr(guest_nic.ipConfig, 'ipAddress', None):
                ip_v4_info = next((ip for ip in guest_nic.ipConfig.ipAddress if '.' in ip.ipAddress), None)
            if ip_v4_info and getattr(ip_v4_info, 'ipAddress', None):
                nic_ip_address = ip_v4_info.ipAddress
            if subnet_mask is None:
                subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
            if nic_ip_address:
                if subnet_mask is None and guest_nic and guest_nic.ipConfig and getattr(guest_nic.ipConfig, 'ipAddress', None):
                    ip_v4_info = next((ip for ip in guest_nic.ipConfig.ipAddress if '.' in ip.ipAddress), None)
                if ip_v4_info:
                    subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
                if subnet_mask is None:
                    missing_ipv4_messages.append(f"NIC {mac} ({network_name}) does not provide a subnet mask.")
                else:
                    nic_record = {
                        'device_type': type(device),
                        'mac_address': mac,
                        'network_name': network_name,
                        'ip_address': nic_ip_address,
                        'subnet_mask': subnet_mask,
                        'is_gateway_nic': False,
                    }
                if sdk_interface_index is not None:
                    nic_record['sdk_interface_index'] = sdk_interface_index
                if sdk_nic_id:
                    nic_record['sdk_nic_id'] = sdk_nic_id
                original_nic_info.append(nic_record)
            else:
                missing_ipv4_messages.append(
                    f"NIC {mac} ({network_name}) data could not be retrieved from API/VMware Tools.")
                default_gateway_owner_idx: Optional[int] = None
        if source_routes:
            original_static_routes.clear()
            default_route_candidates: List[Dict[str, Any]] = []
        for route in source_routes:
            network = route.get('network')
            prefix = route.get('prefix_length')
            gateway = route.get('gateway_address')
            interface_index = route.get('interface_index')
            if network is None or prefix is None:
                continue
            resolved_owner: Optional[int] = None
            if interface_index is not None and 0 <= interface_index < len(original_nic_info):
                resolved_owner = interface_index
            elif gateway:
                resolved_owner = _find_gateway_owner_index(original_nic_info, gateway)
            entry = {
                'network': route_network,
                'prefix': prefix,
                'gateway': gateway,
            }
            if resolved_owner is not None:
                entry['owner_index'] = resolved_owner
            original_static_routes.append(entry)
            if gateway and (network == '0.0.0.0' or prefix == 0):
                default_route_candidates.append({'gateway': gateway, 'owner_index': resolved_owner})
                if original_default_gateway is None:
                    original_default_gateway = gateway
                    default_gateway_owner_idx = resolved_owner
        selected_default = _select_default_gateway_route(default_route_candidates, original_nic_info)
        if selected_default:
            original_default_gateway, default_gateway_owner_idx = selected_default
            for idx, nic in enumerate(original_nic_info):
                nic['is_gateway_nic'] = (idx == default_gateway_owner_idx)
            owner_display = default_gateway_owner_idx + 1 if default_gateway_owner_idx is not None else "?"
            print(f"   -> Detected default gateway {original_default_gateway} (NIC {owner_display}).")
        elif default_route_candidates and original_default_gateway is not None:
            owner_candidate = default_gateway_owner_idx
            if owner_candidate is None:
                owner_candidate = default_route_candidates[0].get('owner_index')
                if owner_candidate is None and original_default_gateway:
                    owner_candidate = _find_gateway_owner_index(original_nic_info, original_default_gateway)
            if owner_candidate is not None and 0 <= owner_candidate < len(original_nic_info):
                default_gateway_owner_idx = owner_candidate
                for idx, nic in enumerate(original_nic_info):
                    nic['is_gateway_nic'] = (idx == owner_candidate)
                print(f"   -> Detected default gateway {original_default_gateway} (NIC {owner_candidate + 1}).")
    if not source_routes and target_vm.guest.ipStack and target_vm.guest.ipStack[0].ipRouteConfig:
        for route in target_vm.guest.ipStack[0].ipRouteConfig.ipRoute:
            if route.network == '0.0.0.0' and route.prefixLength == 0:
                original_default_gateway = route.gateway.ipAddress
                print(f"   Default gateway '{original_default_gateway}' retrieved from VMware Tools.")
                for idx, nic in enumerate(original_nic_info):
                    try:
                        nic_iface = ipaddress.IPv4Interface(f"{nic['ip_address']}/{nic['subnet_mask']}")
                        gw_addr = ipaddress.IPv4Address(original_default_gateway)
                        if gw_addr in nic_iface.network:
                            for j, nic_record in enumerate(original_nic_info):
                                nic_record['is_gateway_nic'] = (j == idx)
                            print(f"   -> Marked NIC with IP {nic['ip_address']} as the gateway owner.")
                            break
                    except (ValueError, ipaddress.AddressValueError):
                        continue
            else:
                route_network = getattr(route, 'network', None)
                prefix = getattr(route, 'prefixLength', None)
                gateway_obj = getattr(route, 'gateway', None)
                gateway = getattr(gateway_obj, 'ipAddress', None) if gateway_obj else None
                if route_network and prefix is not None:
                    original_static_routes.append({
                        'network': route_network,
                        'prefix': prefix,
                        'gateway': gateway,
                    })
                if original_default_gateway is None:
                    inferred_gateway = _infer_gateway_from_routes(original_nic_info, original_static_routes)
                    chosen_gateway = inferred_gateway or _derive_fallback_gateway(original_nic_info)
                if chosen_gateway:
                    original_default_gateway, default_gateway_owner_idx = chosen_gateway
                for idx, nic in enumerate(original_nic_info):
                    nic['is_gateway_nic'] = (idx == default_gateway_owner_idx)
                    print(
                        f"   -> Default gateway not reported; inferred {original_default_gateway} (NIC {default_gateway_owner_idx + 1}).")
                if original_static_routes:
                    print("   Retrieved static routes (STG):")
            for route in original_static_routes:
                gw_disp = route['gateway'] or '(none)'
                print(f"      - {route['network']}/{route['prefix']} via {gw_disp}")

        if source_networking_state:
            dns_info = source_networking_state.get('dns') or {}
            original_dns_servers = [
                dns for dns in (dns_info.get('ip_addresses') or [])
                if dns and not str(dns).startswith('127.')
            ]
    if not original_dns_servers and target_vm.guest.ipStack and target_vm.guest.ipStack[0].dnsConfig:
        original_dns_servers = [
            dns for dns in target_vm.guest.ipStack[0].dnsConfig.ipAddress if not dns.startswith('127.')]
    print(f"   [OK] Retrieved {len(original_nic_info)} NIC configuration entries.")
    target_datastore = next((ds for ds in content_source.viewManager.CreateContainerView(
        content_source.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME), None)
    if not target_datastore:
        raise FileNotFoundError(f"Datastore '{TARGET_DATASTORE_NAME}' was not found.")
    print(f"   [OK] Datastore '{target_datastore.name}' confirmed.")
    date_suffix = datetime.now().strftime('%Y%m%d')
    clone_name = f"{target_vm.name}-{date_suffix}"

    # --- Pre-execution confirmation (1/4) ---
    print("\n" + "=" * 25 + " Pre-execution Check (1/4) " + "=" * 25)
    print("Review the details below before creating the clone and starting the migration.")
    print("\n  [Source VM details]")
    print(f"    - VM name       : {target_vm.name}")
    print(f"    - OS            : {target_vm.summary.config.guestFullName}")
    print("\n  [Source NIC details]")
    if original_nic_info:
        for i, nic in enumerate(original_nic_info):
            print(f"    - NIC {i+1} ({nic['mac_address']})")
            print(f"      - Network     : {nic['network_name']}")
            print(f"      - IP Address  : {nic['ip_address']}")
            print(f"      - Subnet Mask : {nic['subnet_mask']}")
    else:
        print("    - No NIC information was found.")
    if original_default_gateway:
        print(f"    - Gateway     : {original_default_gateway}")
    else:
        print("    - Default gateway not detected.")
    print("\n  [Clone VM specification]")
    print(f"    - New VM name   : {clone_name}")
    print(f"    - Placement datastore: {TARGET_DATASTORE_NAME}")
    print("=" * 64)
    user_approval = input("\nProceed with this clone operation? (y/n): ")
    if user_approval.lower() != 'y':
        raise InterruptedError("Operation cancelled by the user.")

    # --- Clone, NIC removal, and unregister operations ---
    relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
    clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
    print("\nStarting clone task...")
    task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        progress = task.info.progress or 0
        print(f"   Clone progress: {progress}%", end='\r')
        time.sleep(5)
    print(" " * 40, end='\r')
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"Clone task failed: {task.info.error.msg}")
    print(f"\n[OK] Clone completed: '{clone_name}'")

    new_vm_on_source = task.info.result

    # NIC removal
    print(f"   Removing NICs from cloned VM '{new_vm_on_source.name}'...")
    nic_devices_to_remove = [dev for dev in new_vm_on_source.config.hardware.device if isinstance(
        dev, vim.vm.device.VirtualEthernetCard)]
    if nic_devices_to_remove:
        nic_change_spec = [vim.vm.device.VirtualDeviceSpec(
            operation='remove', device=nic) for nic in nic_devices_to_remove]
        config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
        task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"NIC removal failed: {task.info.error.msg}")
        print("   [OK] Removed NICs.")

    vmx_path = new_vm_on_source.config.files.vmPathName
    print(f"   Unregistering VM '{clone_name}' from the source vCenter...")
    new_vm_on_source.UnregisterVM()
    unregistered_from_source = True
    print("   [OK] Unregistration completed.")
    _stop_keepalive_thread(source_keepalive_handle)
    source_keepalive_handle = None
    Disconnect(si_source)
    si_source = None
    new_vm_on_source = None

    # --- [Phase 2/7] ~ [Phase 7/7]: Destination vCenter operations ---
    print("\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest:
        raise ConnectionError(f"Unable to connect to destination vCenter ({VCSA_HOST_DEST}).")
    print("[OK] Connected to destination vCenter.")
    dest_keepalive_handle = _start_keepalive_thread(si_dest, "dest-vcenter")
    content_dest = si_dest.RetrieveContent()
    if any(vm for vm in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name):
        raise FileExistsError(f"A VM named '{clone_name}' already exists on the destination vCenter.")
    print("[OK] No conflicting VM found on destination vCenter.")
    print("\n--- [Phase 3/7] Destination vCenter: Register VM ---")
    dest_cluster = next((c for c in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [
                        vim.ClusterComputeResource], True).view if c.name == TARGET_CLUSTER_NAME), None)
    if not dest_cluster:
        raise FileNotFoundError(f"Destination cluster '{TARGET_CLUSTER_NAME}' was not found.")
    task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(
        path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(5)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"Failed to register VM on destination vCenter: {task.info.error.msg}")
    migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=60, delay_seconds=2)
    migrated_vm_for_rollback = migrated_vm  # Preserve for rollback
    migrated_vm_name_for_rollback = clone_name
    print("[OK] VM registration completed.")
    print("\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
    if original_nic_info:
        print("\n" + "=" * 25 + " Pre-execution Check (2/4) " + "=" * 25)
        print("Re-create NICs on the migrated VM and connect to these networks.")
        device_change_spec = []
        for i, nic in enumerate(original_nic_info):
            original_network_name = nic['network_name']
            dest_network_name = original_network_name.replace('STG', 'PRD', 1)
            print(f"  - NIC {i+1}: '{original_network_name}' -> '{dest_network_name}'")

            dest_network = next(
                (
                    net
                    for net in content_dest.viewManager.CreateContainerView(
                        content_dest.rootFolder,
                        [vim.Network],
                        True,
                    ).view
                    if net.name == dest_network_name
                ),
                None,
            )
            nic_spec = vim.vm.device.VirtualDeviceSpec()
            nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            nic_spec.device = nic['device_type']()
            nic_spec.device.key = -(100 + i)

            if isinstance(dest_network, vim.dvs.DistributedVirtualPortgroup):
                portgroup_connection = vim.dvs.PortConnection()
                portgroup_connection.portgroupKey = dest_network.key
                portgroup_connection.switchUuid = dest_network.config.distributedVirtualSwitch.uuid
                nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
                nic_spec.device.backing.port = portgroup_connection
            elif isinstance(dest_network, vim.Network):
                nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
                nic_spec.device.backing.network = dest_network
                nic_spec.device.backing.deviceName = dest_network_name
            else:
                raise FileNotFoundError(f"Destination network '{dest_network_name}' was not found.")
            nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=True, allowGuestControl=True)
            device_change_spec.append(nic_spec)

            print("=" * 64)
            user_approval_nic = input("\nApply this NIC configuration? (y/n): ")
            if user_approval_nic.lower() != 'y':
                raise InterruptedError("NIC configuration was cancelled by the user.")
                print("\nApproved. Starting NIC reconfiguration task...")
            config_spec = vim.vm.ConfigSpec(deviceChange=device_change_spec)
            try:
                task = migrated_vm.ReconfigVM_Task(spec=config_spec)
            except vmodl.fault.ManagedObjectNotFound:
                migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
                migrated_vm_for_rollback = migrated_vm
                migrated_vm_name_for_rollback = clone_name
                task = migrated_vm.ReconfigVM_Task(spec=config_spec)
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"Failed to reconfigure NICs: {task.info.error.msg}")
            print("   [OK] NIC reconfiguration completed.")
            print("   Fetching updated NIC information...")
        try:
            migrated_vm.Reload()
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
            migrated_vm_for_rollback = migrated_vm
            migrated_vm_name_for_rollback = clone_name
            migrated_vm.Reload()
        newly_added_nics = [dev for dev in migrated_vm.config.hardware.device if isinstance(
            dev, vim.vm.device.VirtualEthernetCard)]
        if len(newly_added_nics) == len(original_nic_info):
            for nic_entry, new_nic in zip(original_nic_info, newly_added_nics):
                nic_entry['new_mac_address'] = new_nic.macAddress
            print("   [OK] Associated new MAC addresses.")
        else:
            raise RuntimeError("Recreated NIC count does not match the expected number.")
    else:
        print("   - Skipping NIC reconfiguration because the original VM had no NICs.")
        print("\n--- [Phase 5/7] Destination vCenter: Power On ---")
        print("\n" + "=" * 25 + " Pre-execution Check (3/4) " + "=" * 25)
        print("Powering on the VM and applying guest OS IP configuration.")
        if original_nic_info:
            new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
            gateway_nic_present = any(nic.get('is_gateway_nic') for nic in original_nic_info)
            prd_static_routes = determine_prd_static_routes(
                original_nic_info,
                new_default_gateway,
                original_static_routes,
            )
        if prd_static_routes:
            print("   -> PRD static route candidates:")
            for route_info in prd_static_routes:
                owner_index = route_info.get('owner_index')
                owner_label = f"NIC #{owner_index + 1}" if owner_index is not None else 'Any NIC'
                destination = route_info['network']
                prefix_value = route_info.get('prefix')
                if prefix_value is not None:
                    destination = f"{destination}/{prefix_value}"
                print(f"      - {destination} via {route_info['gateway']} ({owner_label})")
        configured_route_indices: Set[int] = set()
        for i, nic in enumerate(original_nic_info):
            new_ip = calculate_ip_stg_to_prd(nic['ip_address'])
            nic['prd_ip_address'] = new_ip
            try:
                nic['prd_ip_segment'] = int(new_ip.split('.')[2]) if new_ip else None
            except (ValueError, IndexError):
                nic['prd_ip_segment'] = None
            print(f"\n  - NIC {i+1} ({nic['new_mac_address']})")
            print(f"    - IP Address  : {nic['ip_address']} -> {new_ip}")
            mask_value = nic.get('subnet_mask') or '(unknown)'
            print(f"    - Subnet Mask : {mask_value} -> {mask_value}")
        if new_default_gateway:
            print("\n  [Default gateway configuration]")
            print(f"    - Gateway     : {original_default_gateway} -> {new_default_gateway}")

        if original_dns_servers:
            print("\n  [DNS server configuration]")
            new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
            for old_dns, new_dns in zip(original_dns_servers, new_dns_servers):
                print(f"    - {old_dns} -> {new_dns}")
        else:
            print("  - Skipping IP configuration because no NIC information is available.")
            print("=" * 64)

        user_approval_ip = input("\nApply this IP configuration and power on the VM? (y/n): ")
        if user_approval_ip.lower() != 'y':
            raise InterruptedError("IP configuration and power-on were cancelled by the user.")
        print("\nApproved. Powering on the VM...")
        try:
            task = migrated_vm.PowerOnVM_Task()
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
            migrated_vm_for_rollback = migrated_vm
            migrated_vm_name_for_rollback = clone_name
            task = migrated_vm.PowerOnVM_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"Failed to power on the VM: {task.info.error.msg}")
            print("   [OK] VM powered on successfully.")
    print("   Verifying guest operations agent readiness (up to 5 minutes)...")
    guest_operations_manager = content_dest.guestOperationsManager
    agent_ready = False
    for i in range(10):
        print(f"    - Attempt {i+1}/10...")
        try:
            creds_check = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
            process_manager = guest_operations_manager.processManager
            spec_check = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/echo", arguments="ready")
            pid = process_manager.StartProgramInGuest(vm=migrated_vm, auth=creds_check, spec=spec_check)
            if pid >= 0:
                agent_ready = True
                break
        except vim.fault.InvalidGuestLogin:
            agent_ready = True
            break
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
            migrated_vm_for_rollback = migrated_vm
            migrated_vm_name_for_rollback = clone_name
            continue
        except vim.fault.GuestOperationsUnavailable:
            if i < 9:
                time.sleep(30)
            continue
        except Exception:
            if i < 9:
                time.sleep(30)
            continue

    if not agent_ready:
        raise SystemError("Timeout: guest operations agent was not ready in time.")
    print("   [OK] Guest operations agent is ready.")
    print("\n--- [Phase 6/7] Destination vCenter: Set IP Address ---")

    if original_nic_info:
        root_credentials = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
        admin_credentials = None
        if GUEST_ADMIN_USER and GUEST_ADMIN_PWD:
            admin_credentials = vim.vm.guest.NamePasswordAuthentication(
                username=GUEST_ADMIN_USER,
                password=GUEST_ADMIN_PWD,
            )

        def guest_command_executor(command, check_exit_code=True):
            """Execute a guest command, reloading the VM handle if it becomes invalid."""
            global migrated_vm, migrated_vm_for_rollback, migrated_vm_name_for_rollback
            try:
                return execute_command_in_guest(
                    guest_operations_manager,
                    migrated_vm,
                    root_credentials,
                    admin_credentials,
                    GUEST_ADMIN_PWD,
                    command,
                    check_exit_code=check_exit_code,
                )
            except vmodl.fault.ManagedObjectNotFound:
                evaluated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
                migrated_vm = evaluated_vm
                migrated_vm_for_rollback = evaluated_vm
                migrated_vm_name_for_rollback = clone_name
                return execute_command_in_guest(
                    guest_operations_manager,
                    migrated_vm,
                    root_credentials,
                    admin_credentials,
                    GUEST_ADMIN_PWD,
                    command,
                    check_exit_code=check_exit_code,
                )
        new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
        sdk_interfaces: List[Dict[str, Any]] = []
        sdk_vm_id: Optional[str] = None
        use_sdk_networking = False
        expected_dns_overall: List[str] = []
        nmcli_validation_tasks: List[Tuple[str, str, str, Optional[str], List[str], List[str]]] = []
        if REQUESTS_AVAILABLE:
            try:
                sdk_network_client = VsphereGuestNetworkSDK(
                    host=VCSA_HOST_DEST,
                    username=VCSA_USER,
                    password=VCSA_PWD_DEST,
                    verify_ssl=False,
                )
                sdk_vm_id = getattr(migrated_vm, "_moId", None)
                if sdk_vm_id:
                    sdk_interfaces = sdk_network_client.list_interfaces(sdk_vm_id)
                    if sdk_interfaces:
                        use_sdk_networking = True
                        print("   -> Reconfiguring guest networking via the vSphere Automation SDK.")
                    else:
                        print("   -> SDK enumerated no guest NICs; using the legacy nmcli workflow.")
            except Exception as sdk_error:
                LOGGER.warning("Unable to initialise SDK-based network reconfiguration: %s", sdk_error)
                sdk_network_client = None
                use_sdk_networking = False
            for i, nic_info in enumerate(original_nic_info):
                new_ip = nic_info.get('prd_ip_address')
                if not new_ip:
                    new_ip = calculate_ip_stg_to_prd(nic_info['ip_address'])
                    nic_info['prd_ip_address'] = new_ip
                subnet_mask_parts = nic_info['subnet_mask'].split('.')
            prefix = sum([bin(int(x)).count('1') for x in subnet_mask_parts])
            new_mac = nic_info['new_mac_address']
            expected_gateway_value = new_default_gateway if nic_info.get(
                'is_gateway_nic') and new_default_gateway else None
            expected_dns_servers: List[str] = []
            applied_static_routes: List[str] = []

            print("\n" + "=" * 20 + f" NIC {i+1} Configuration " + "=" * 20)

            # 1. Discover device name within the guest
            interface_inventory = collect_interface_inventory(guest_command_executor)
            device_name = ""
            available_interfaces = set()
            available_interfaces_compact = set()
            interfaces_by_mac = {}
            new_mac_lower = (new_mac or "").lower()
            original_mac_lower = (nic_info.get('mac_address') or "").lower()
            for entry in interface_inventory:
                ifname = entry.get("ifname")
                if not ifname:
                    continue
                lowered_ifname = ifname.lower()
                available_interfaces.add(lowered_ifname)
                available_interfaces_compact.add(_compact_interface_name(ifname))
                mac_candidate = (entry.get("mac") or "").lower()
                if mac_candidate:
                    interfaces_by_mac[mac_candidate] = entry
                    if new_mac_lower and mac_candidate == new_mac_lower:
                        device_name = ifname
            if not device_name and original_mac_lower:
                match_entry = interfaces_by_mac.get(original_mac_lower)
                if match_entry:
                    device_name = match_entry.get("ifname", "")
            if not device_name:
                target_mac = new_mac or nic_info.get('mac_address') or '?'
                raise RuntimeError(f"Unable to locate guest interface matching MAC {target_mac}")
            LOGGER.debug(
                "Interface match: ifname=%s, new_mac=%s, original_mac=%s",
                device_name,
                new_mac,
                nic_info.get('mac_address'),
            )
            con_name = device_name
            print(f"   -> Guest OS interface '{device_name}' located.")
            nmcli_check_exit, _, _ = guest_command_executor("command -v nmcli", check_exit_code=False)
            nmcli_supported = nmcli_check_exit == 0
            new_dns_servers: List[str] = []
            if i == 0 and original_dns_servers:
                new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
                if new_dns_servers:
                    expected_dns_servers = new_dns_servers
                    expected_dns_overall = new_dns_servers[:]
            routes_for_nic: List[Tuple[int, Dict[str, Any]]] = []
            if prd_static_routes:
                for route_idx, route_info in enumerate(prd_static_routes):
                    owner_index = route_info.get('owner_index')
                    if owner_index is not None and owner_index != i:
                        continue
                    if route_idx in configured_route_indices:
                        continue
                    routes_for_nic.append((route_idx, route_info))
            should_configure_routes = False
            if not should_configure_routes and new_default_gateway and new_ip:
                try:
                    nic_network = ipaddress.IPv4Interface(f"{new_ip}/{prefix}").network
                    if ipaddress.IPv4Address(new_default_gateway) in nic_network:
                        should_configure_routes = True
                except (ValueError, ipaddress.AddressValueError):
                    pass
            if not should_configure_routes and not gateway_nic_present:
                should_configure_routes = (i == 0)
            if not should_configure_routes and routes_for_nic:
                should_configure_routes = True
            use_nmcli_connection = nmcli_supported
            selected_route_indices: List[int] = []
            selected_route_lines: List[str] = []
            if nmcli_supported:
                try:
                    try:
                        guest_command_executor(f"nmcli device disconnect {device_name} || true", check_exit_code=False)
                        device_name_normalized = device_name.lower()
                        mac_normalized = new_mac_lower.replace('-', ':') if new_mac_lower else ""
                        old_mac_normalized = original_mac_lower.replace('-', ':')
                        nmcli_fields = NMCLI_FIELDS_WITH_TYPE
                        nmcli_list_cmd = f"nmcli -t -f {','.join(nmcli_fields)} connection show"
                        try:
                            _, nmcli_output, _ = guest_command_executor(nmcli_list_cmd)
                        except RuntimeError:
                            nmcli_fields = NMCLI_FIELDS_NO_TYPE
                            nmcli_list_cmd = f"nmcli -t -f {','.join(nmcli_fields)} connection show"
                            _, nmcli_output, _ = guest_command_executor(nmcli_list_cmd)
                        parsed_connections = parse_nmcli_connection_output(nmcli_output, nmcli_fields)
                        existing_connections = []
                        for entry in parsed_connections:
                            normalized_entry = {}
                            for field in nmcli_fields:
                                normalized_entry[field.lower()] = entry.get(field, "")
                            existing_connections.append(normalized_entry)
                        alias_targets = {value for value in (device_name_normalized, con_name.lower()) if value}
                        alias_targets_compact = {value.replace(
                            '-', '').replace('_', '').replace(' ', '') for value in alias_targets}
                        mac_targets = set()
                        for value in (mac_normalized, old_mac_normalized):
                            if value:
                                mac_targets.add(value)
                                mac_targets.add(value.replace(':', ''))
                                mac_targets.add(value.replace(':', '-'))
                        stale_connection_uuids = set()
                        connection_detail_cache: Dict[str, Tuple[int, str]] = {}
                        get_connection_details = _make_nmcli_detail_fetcher(
                            connection_detail_cache, guest_command_executor
                        )
                        for conn in existing_connections:
                            uuid = (conn.get('uuid') or "").strip()
                            if not uuid:
                                continue
                            name_norm = (conn.get('name') or "").strip().lower()
                            device_norm = (conn.get('device') or "").strip().lower()
                            type_norm = (conn.get('type') or "").strip().lower()
                            name_compact = name_norm.replace('-', '').replace('_', '').replace(' ', '')
                            device_compact = device_norm.replace('-', '').replace('_', '').replace(' ', '')
                            alias_match = False
                            if alias_targets:
                                if device_norm in alias_targets or name_norm in alias_targets:
                                    alias_match = True
                                elif device_compact and any(target in device_compact for target in alias_targets_compact):
                                    alias_match = True
                                elif name_compact and any(target in name_compact for target in alias_targets_compact):
                                    alias_match = True
                            orphaned_interface = False
                            if not device_norm:
                                if name_norm and LEGACY_INTERFACE_PATTERN.match(name_norm):
                                    if available_interfaces and name_norm not in available_interfaces and name_compact not in available_interfaces_compact:
                                        orphaned_interface = True
                                elif alias_targets and name_compact and any(target in name_compact for target in alias_targets_compact):
                                    orphaned_interface = True
                            mac_match = False
                            if mac_targets and not (alias_match or orphaned_interface):
                                detail_exit, detail_stdout = get_connection_details(uuid)
                                if detail_exit == 0 and detail_stdout:
                                    detail_lower = detail_stdout.lower()
                                    for target_mac in mac_targets:
                                        if target_mac and target_mac in detail_lower:
                                            mac_match = True
                                            break
                            if type_norm and type_norm not in ('802-3-ethernet', 'ethernet'):
                                if not mac_match:
                                    continue
                            if alias_match or orphaned_interface or mac_match:
                                stale_connection_uuids.add(uuid)
                        if stale_connection_uuids:
                            print(f"   -> Removing stale nmcli connections ({len(stale_connection_uuids)} entries).")
                            for uuid in sorted(stale_connection_uuids):
                                guest_command_executor(f"nmcli connection delete uuid {uuid}")
                        guest_command_executor(
                            f"nmcli connection add type ethernet con-name '{con_name}' ifname '{device_name}' autoconnect no"
                        )
                        if new_ip and prefix:
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv4.method manual ipv4.addresses '{new_ip}/{prefix}'"
                            )
                        else:
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv4.method manual ipv4.addresses ''"
                            )
                        guest_command_executor(
                            f"nmcli connection modify '{con_name}' ipv6.method disabled", check_exit_code=False)
                        guest_command_executor(
                            f"nmcli connection modify '{con_name}' ipv6.never-default yes", check_exit_code=False)
                        guest_command_executor(
                            f"nmcli connection modify '{con_name}' ipv6.addresses ''", check_exit_code=False)
                        guest_command_executor(
                            f"nmcli connection modify '{con_name}' ipv6.routes ''", check_exit_code=False)
                        guest_command_executor(
                            f"nmcli connection modify '{con_name}' ipv6.dns ''", check_exit_code=False)
                        if nic_info.get('is_gateway_nic') and new_default_gateway:
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv4.gateway '{new_default_gateway}'")
                        if new_dns_servers:
                            dns_str = ' '.join(new_dns_servers)
                            guest_command_executor(f"nmcli connection modify '{con_name}' ipv4.dns '{dns_str}'")
                        if should_configure_routes and routes_for_nic:
                            added_routes = False
                            for route_idx, route_info in routes_for_nic:
                                gateway = route_info['gateway']
                                prefix_value = route_info.get('prefix')
                                network_base = route_info['network']
                                network_cidr = f"{network_base}/{prefix_value}" if prefix_value is not None else network_base
                                try:
                                    network_obj = ipaddress.IPv4Network(network_cidr, strict=False)
                                    if new_ip and ipaddress.IPv4Address(new_ip) in network_obj:
                                        continue
                                except (ValueError, ipaddress.AddressValueError):
                                    continue
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' +ipv4.routes '{network_cidr} {gateway}'"
                                )
                                selected_route_indices.append(route_idx)
                                selected_route_lines.append(f"{network_cidr} {gateway}")
                                if not added_routes:
                                    print("   -> Applied PRD static routes via nmcli.")
                                    added_routes = True
                                print(f"      - Added: {network_cidr} via {gateway}")
                    except NmcliNotAvailableError:
                        print("   -> nmcli command unavailable; applying legacy network configuration.")
                        selected_route_indices, selected_route_lines = configure_interface_without_nmcli(
                            guest_command_executor,
                            device_name,
                            new_ip,
                            prefix,
                            expected_gateway_value if should_configure_routes else None,
                            routes_for_nic if should_configure_routes else [],
                            new_dns_servers if new_dns_servers else None,
                        )
                        use_nmcli_connection = False
                except NmcliNotAvailableError:
                    print("   -> nmcli command unavailable; applying legacy network configuration.")
                    selected_route_indices, selected_route_lines = configure_interface_without_nmcli(
                        guest_command_executor,
                        device_name,
                        new_ip,
                        prefix,
                        expected_gateway_value if should_configure_routes else None,
                        routes_for_nic if should_configure_routes else [],
                        new_dns_servers if new_dns_servers else None,
                    )
                    use_nmcli_connection = False
            else:
                print("   -> nmcli unavailable; applying legacy network configuration.")
                selected_route_indices, selected_route_lines = configure_interface_without_nmcli(
                    guest_command_executor,
                    device_name,
                    new_ip,
                    prefix,
                    expected_gateway_value if should_configure_routes else None,
                    routes_for_nic if should_configure_routes else [],
                    new_dns_servers if new_dns_servers else None,
                )
                use_nmcli_connection = False
            if selected_route_lines:
                applied_static_routes.extend(selected_route_lines)
            for route_idx in selected_route_indices:
                configured_route_indices.add(route_idx)
            # 4. Bring up the new connection
            if use_nmcli_connection:
                guest_command_executor(
                    f"nmcli connection modify '{con_name}' connection.autoconnect yes",
                    check_exit_code=False,
                )
                guest_command_executor(f"nmcli connection up '{con_name}'")
            guest_command_executor(f"ip -6 addr flush dev {device_name}", check_exit_code=False)
            # 4.5. Broadcast gratuitous ARP to refresh neighbor caches
            if new_ip:
                arping_commands = [
                    f"arping -c 3 -A -I {device_name} {new_ip}",
                    f"arping -c 3 -U -I {device_name} {new_ip}",
                ]
                for arping_cmd in arping_commands:
                    guest_command_executor(arping_cmd, check_exit_code=False)

            # 5. Final verification
            time.sleep(5)
            guest_command_executor(f"ip addr show {device_name} | grep -q '{new_ip}'")
            ping_targets = []
            candidate_gateways = []
            if expected_gateway_value:
                candidate_gateways.append(expected_gateway_value)
            elif new_default_gateway and not gateway_nic_present:
                candidate_gateways.append(new_default_gateway)
            if new_ip and prefix:
                try:
                    iface = ipaddress.IPv4Interface(f"{new_ip}/{prefix}")
                    first_host = next(iface.network.hosts(), None)
                    if first_host:
                        candidate_gateways.append(str(first_host))
                except ValueError:
                    LOGGER.debug("Failed to derive fallback gateway from %s/%s", new_ip, prefix, exc_info=True)
            for route_line in applied_static_routes:
                parts = route_line.split()
                if len(parts) >= 2:
                    gw_candidate = parts[1]
                    if gw_candidate and gw_candidate not in candidate_gateways:
                        candidate_gateways.append(gw_candidate)
            for candidate in candidate_gateways:
                if candidate and candidate != new_ip and candidate not in ping_targets:
                    ping_targets.append(candidate)
            LOGGER.debug("Connectivity targets for %s (%s): %s", device_name, new_ip, ping_targets or "[none]")
            try:
                ensure_connection_activation(
                    guest_command_executor,
                    con_name if use_nmcli_connection else None,
                    device_name,
                    ping_targets=ping_targets,
                )
            except RuntimeError as activation_error:
                print(f"\n[WARN] Connectivity verification failed for '{con_name}': {activation_error}")
                decision = input("Continue despite the connectivity failure? (c=continue / a=abort): ").strip().lower()
                if decision == 'c':
                    print("   -> Continuing despite failed connectivity checks per user direction.")
                else:
                    raise

        expected_ip_cidr = f"{new_ip}/{prefix}" if new_ip else ""
        if use_nmcli_connection:
            nmcli_validation_tasks.append((
                con_name,
                device_name,
                expected_ip_cidr,
                expected_gateway_value,
                applied_static_routes.copy(),
                expected_dns_servers[:] if expected_dns_servers else []
            ))
        ensure_firewall_allows_ssh(guest_command_executor, SSH_ALLOWED_SOURCE_IP)
        print("   [OK] Completed IP configuration for all NICs.")
    sdk_verification_succeeded = False
    if REQUESTS_AVAILABLE:
        validation_client = sdk_network_client
        created_validation_client = False
        validation_vm_id = sdk_vm_id or getattr(migrated_vm, "_moId", None)
        if validation_vm_id and validation_client is None:
            try:
                validation_client = VsphereGuestNetworkSDK(
                    host=VCSA_HOST_DEST,
                    username=VCSA_USER,
                    password=VCSA_PWD_DEST,
                    verify_ssl=False,
                )
                created_validation_client = True
            except Exception as sdk_error:
                LOGGER.warning("Failed to initialise SDK client for post-migration verification: %s", sdk_error)
        if validation_vm_id and validation_client:
            try:
                sdk_verification_succeeded = verify_destination_network_with_sdk(
                    validation_client,
                    validation_vm_id,
                    original_nic_info,
                    expected_dns_overall,
                    prd_static_routes,
                )
            except Exception as sdk_error:
                LOGGER.warning("SDK verification encountered an error: %s", sdk_error)
            finally:
                if created_validation_client and validation_client:
                    validation_client.close()
    if not sdk_verification_succeeded:
        for (con_name, device_name, expected_ip_cidr, expected_gateway_value, routes_snapshot, dns_snapshot) in nmcli_validation_tasks:
            try:
                verify_nmcli_connection_settings(
                    guest_command_executor,
                    con_name,
                    device_name,
                    expected_ip_cidr,
                    expected_gateway_value,
                    routes_snapshot,
                    dns_snapshot,
                )
            except RuntimeError as validation_error:
                print(f"\n[WARN] nmcli validation failed: {validation_error}")
                decision = input("Continue despite nmcli validation failure? (c=continue / a=abort): ").strip().lower()
                if decision == 'c':
                    print("   -> Proceeding despite nmcli validation failure per user request.")
                else:
                    raise
    print("\n--- [Phase 7/7] Destination vCenter: Final Storage vMotion ---")
    print(f"Searching for final datastore '{TARGET_DATASTORE_NAME_FINAL}'...")
    final_datastore = next((ds for ds in content_dest.viewManager.CreateContainerView(
        content_dest.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME_FINAL), None)
    if not final_datastore:
        raise FileNotFoundError(f"Final datastore '{TARGET_DATASTORE_NAME_FINAL}' was not found.")
    print(f"[OK] Confirmed final datastore '{final_datastore.name}'.")
    print("\n" + "=" * 25 + " Pre-execution Check (4/4) " + "=" * 25)
    print("Move the VM storage to the final PRD datastore.")
    print(f"  - Target VM       : {clone_name}")
    try:
        current_datastores = ', '.join([ds.name for ds in migrated_vm.datastore])
    except vmodl.fault.ManagedObjectNotFound:
        migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
        migrated_vm_for_rollback = migrated_vm
        migrated_vm_name_for_rollback = clone_name
        current_datastores = ', '.join([ds.name for ds in migrated_vm.datastore])
    print(f"  - Current datastores : {current_datastores}")
    print(f"  - Destination datastore: {TARGET_DATASTORE_NAME_FINAL}")
    print("=" * 64)

    user_approval_svmotion = input("\nProceed with this storage vMotion? (y/n): ")
    if user_approval_svmotion.lower() != 'y':
        raise InterruptedError("Storage vMotion was cancelled by the user.")
    print("\nApproved. Starting storage vMotion task...")
    relocate_spec_final = vim.vm.RelocateSpec(datastore=final_datastore)
    try:
        task = migrated_vm.RelocateVM_Task(spec=relocate_spec_final)
    except vmodl.fault.ManagedObjectNotFound:
        migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
        migrated_vm_for_rollback = migrated_vm
        migrated_vm_name_for_rollback = clone_name
        task = migrated_vm.RelocateVM_Task(spec=relocate_spec_final)

    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        progress = task.info.progress or 0
        print(f"   Storage vMotion progress: {progress}%", end='\r')
        time.sleep(5)
    print(" " * 40, end='\r')

    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"Final storage vMotion failed: {task.info.error.msg}")

    print("\n[OK] Storage vMotion completed successfully.")
    print("\n[OK] Migration workflow finished without errors.")
    _stop_keepalive_thread(dest_keepalive_handle)
    dest_keepalive_handle = None
    Disconnect(si_dest)
    si_dest = None
    except Exception as e:
        print(f"\n[ERROR] An error occurred during processing: {e}")

    if migrated_vm_for_rollback:
        print("\n" + "=" * 20 + " Rollback Confirmation (Destination VM Removal) " + "=" * 20)
        print("The process stopped, leaving a partially migrated VM on the destination vCenter.")
        vm_name_display = migrated_vm_name_for_rollback or clone_name or "(unknown)"
        print(f"  - Target VM: {vm_name_display}")

        rollback_approval = input("\nDelete this VM to return to the pre-operation state? (y/n): ")
        if rollback_approval.lower() == 'y':
            try:
                if si_dest is None:
                    connection_alive = False
                else:
                    try:
                        si_dest.CurrentTime()
                        connection_alive = True
                    except Exception:
                        connection_alive = False
                if not connection_alive:  # Reconnect if the session has dropped
                    print("   Reconnecting to the destination vCenter for cleanup...")
                    _stop_keepalive_thread(dest_keepalive_handle)
                    dest_keepalive_handle = None
                    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER,
                                           pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
                    if not si_dest:
                        raise ConnectionError("Failed to reconnect to the destination vCenter.")
                    print("   [OK] Reconnected successfully.")
                    dest_keepalive_handle = _start_keepalive_thread(si_dest, "dest-vcenter-cleanup")
                content_dest_cleanup = si_dest.RetrieveContent()
                vm_to_delete = find_vm_by_name(content_dest_cleanup, clone_name)
                if not vm_to_delete:
                    print("   [INFO] Rollback target VM not found. It may already be deleted.")
                    unregistered_from_source = True
                else:
                    if vm_to_delete.runtime.powerState == 'poweredOn':
                        print(f"   Powering off VM '{vm_to_delete.name}'...")
                        task = vm_to_delete.PowerOffVM_Task()
                        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                            time.sleep(2)
                        if task.info.state == vim.TaskInfo.State.success:
                            print("   [OK] Power-off completed.")
                        else:
                            print(f"   [WARN] Power-off failed: {task.info.error.msg}. Continuing with deletion.")
                    print(f"   Deleting VM '{vm_to_delete.name}'...")
                    destroy_task = vm_to_delete.Destroy_Task()
                    while destroy_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                        time.sleep(2)

                    if destroy_task.info.state == vim.TaskInfo.State.success:
                        print("[OK] Rollback complete: deleted destination VM.")
                        unregistered_from_source = False
                    else:
                        unregistered_from_source = True
                        raise RuntimeError(f"Failed to delete VM: {destroy_task.info.error.msg}")
            except Exception as cleanup_error:
                print(f"[WARN] Error during destination VM rollback: {cleanup_error}")
                unregistered_from_source = True
    if unregistered_from_source:
        print("\n" + "=" * 20 + " Rollback Confirmation (Datastore Cleanup) " + "=" * 20)
        print("   Clone files remain on the source vCenter datastore and must be cleaned up.")
        print(f"   VM files may still exist on datastore '{TARGET_DATASTORE_NAME}'.")

        rollback_approval_files = input("\nConnect to the source vCenter and delete these files? (y/n): ")
        if rollback_approval_files.lower() == 'y':
            si_source_cleanup = None
            try:
                print("\nApproved. Reconnecting to the source vCenter for cleanup...")
                si_source_cleanup = SmartConnect(
                    host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
                if not si_source_cleanup:
                    raise ConnectionError("Failed to reconnect to the source vCenter.")
                print("   [OK] Reconnected successfully.")

                content_cleanup = si_source_cleanup.RetrieveContent()
                file_manager = content_cleanup.fileManager
                vm_dir_path = os.path.dirname(vmx_path)
                print(f"   Deleting directory '{vm_dir_path}' from the datastore...")
                datacenter = content_cleanup.rootFolder.childEntity[0]
                delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)
                while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)
                if delete_task.info.state == vim.TaskInfo.State.success:
                    print("[OK] Rollback complete: removed files from the datastore.")
                else:
                    raise RuntimeError(f"Failed to delete files from datastore: {delete_task.info.error.msg}")
            except Exception as cleanup_error:
                print(f"[WARN] Error during datastore cleanup: {cleanup_error}")
                print("   Please clean up manually via the datastore browser if needed.")
            finally:
                if si_source_cleanup:
                    Disconnect(si_source_cleanup)
        else:
            print("File cleanup was cancelled by the user; files remain on the datastore.")
    elif new_vm_on_source:
        print("\n" + "=" * 20 + " Rollback Confirmation (Source VM Removal) " + "=" * 20)
        print(f"VM '{new_vm_on_source.name}' remains on the source vCenter from an interrupted run.")
        rollback_approval = input("\nDelete this VM to restore the pre-operation state? (y/n): ")
        if rollback_approval.lower() == 'y':
            if new_vm_on_source.runtime.powerState == 'poweredOn':
                print(f"   Powering off VM '{new_vm_on_source.name}'...")
                poweroff_task = new_vm_on_source.PowerOffVM_Task()
                while poweroff_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)
                if poweroff_task.info.state == vim.TaskInfo.State.success:
                    print("   [OK] Power-off completed.")
                else:
                    print(f"   [WARN] Power-off failed: {poweroff_task.info.error.msg}. Continuing with deletion.")
            task = new_vm_on_source.Destroy_Task()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state == vim.TaskInfo.State.success:
                print("[OK] Rollback complete: deleted source VM.")
            else:
                print(f"[WARN] Rollback failed: {task.info.error.msg}")
        else:
            print("Rollback was cancelled; the VM remains on the source vCenter.")
finally:
    try:
        if 'sdk_network_client' in locals() and sdk_network_client:
            sdk_network_client.close()
    except Exception:
        pass
    try:
        if 'dest_keepalive_handle' in locals():
            _stop_keepalive_thread(dest_keepalive_handle)
    except Exception:
        pass
    try:
        if 'si_dest' in locals() and si_dest:
            Disconnect(si_dest)
    except Exception:
        pass
    try:
        if 'source_keepalive_handle' in locals():
            _stop_keepalive_thread(source_keepalive_handle)
    except Exception:
        pass
    try:
        if 'si_source' in locals() and si_source:
            Disconnect(si_source)
    except Exception:
        pass
    print("Processing finished.")
