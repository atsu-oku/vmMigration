# -*- coding: utf-8 -*-
import os
import ssl
import getpass
import time
import json
import logging
import ipaddress
import urllib.request
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional, Set, Tuple
try:
    from pyVim.connect import SmartConnect, Disconnect
except ModuleNotFoundError:
    from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl
try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from vsphere_sdk_network import (
    VsphereGuestNetworkSDK,
    IPv4Config,
    DnsConfig,
    RouteConfig,
    find_interface_id_by_mac,
)
PRD_STATIC_ROUTE_SEGMENTS = {160, 161, 162, 163, 164}
NMCLI_FIELDS_WITH_TYPE = ['UUID', 'NAME', 'DEVICE', 'TYPE']
NMCLI_FIELDS_NO_TYPE = ['UUID', 'NAME', 'DEVICE']
SSH_ALLOWED_SOURCE_IP = "172.16.164.7"


@dataclass
class ConnectionCheckParams:
    max_attempts: int = 5
    wait_seconds: int = 3
    pre_ping_wait_seconds: int = 10
    ping_retry_count: int = 4
    ping_retry_delay: int = 2
    ping_timeout_seconds: int = 2

DEFAULT_CONN_CHECK_PARAMS = ConnectionCheckParams()

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
# 接続情報および移行設定
# ------------------------------------------------
# --- ソース vCenter ---
VCSA_HOST_SOURCE = 'vcsa01s.ipet.local'
VCSA_USER = 'administrator@vsphere.local'
VCSA_PORT = 443

# --- 宛先 vCenter ---
VCSA_HOST_DEST = 'vcsa01p.ipet.local'

# --- 移行リソース ---
# クローン先共有データストア
TARGET_DATASTORE_NAME = 'PMAX-COM-VOL1'
# 最終的な移行データストア
TARGET_DATASTORE_NAME_FINAL = 'PMAX-PRD-VOL1'
# コンピューティングリソースの移行先クラスタ
TARGET_CLUSTER_NAME = 'PRD-Cluster' 

# --- ゲスト OS 認証情報 ---
GUEST_ROOT_USER = 'root'
GUEST_ROOT_PWD = '' # スクリプト実行時に入力
GUEST_ADMIN_USER = 'admin' # フォールバック用ユーザー
GUEST_ADMIN_PWD = '' # スクリプト実行時に入力

# ------------------------------------------------
# Helper Functions
# ------------------------------------------------
def prefix_to_subnet_mask(prefix_length):
    """Convert a CIDR prefix length (0-32) to a dotted decimal subnet mask."""
    if not isinstance(prefix_length, int) or not (0 <= prefix_length <= 32):
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
        try:
            inventory = parser(stdout)
        except Exception as exc:
            LOGGER.debug("Failed to parse output of '%s': %s", command, exc, exc_info=True)
            continue
        if inventory:
            LOGGER.debug(
                "Discovered interfaces via '%s': %s",
                command,
                [entry["ifname"] for entry in inventory],
            )
            return inventory
    raise RuntimeError("ゲストOSのNIC情報を取得できませんでした。ip/ifconfigが利用できません。")

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
    if not connection_name:
        return

    config = params or DEFAULT_CONN_CHECK_PARAMS
    targets = []
    if ping_targets:
        for item in ping_targets:
            if item and item not in targets:
                targets.append(item)

    def _ping_target(target_address):
        ping_command = (
            "bash -c 'for i in $(seq 1 {retry}); do "
            "ping -c 1 -W {timeout} {target} && exit 0; "
            "sleep {delay}; "
            "done; exit 1'"
        ).format(
            retry=max(1, config.ping_retry_count),
            timeout=max(1, config.ping_timeout_seconds),
            target=target_address,
            delay=max(0, config.ping_retry_delay),
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
    print("   -> ファイアウォール設定を確認します...")
    if not source_ip:
        return
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
            f"--add-rich-rule='rule family=\"ipv4\" source address=\"{source_ip}\" "
            "service name=\"ssh\" accept'"
        )
        exit_code, _, cmd_err = command_executor(rich_rule, check_exit_code=False)
        if exit_code == 0:
            command_executor("firewall-cmd --reload", check_exit_code=False)
            print(f"      - firewalld: {zone} に SSH 許可ルールを追加しました ({source_ip})")
            return
        LOGGER.debug("Failed to add firewalld rich rule: %s", cmd_err)
    exit_code, _, _ = command_executor("command -v iptables", check_exit_code=False)
    if exit_code == 0:
        check_rule_cmd = (
            f"iptables -C INPUT -p tcp -s {source_ip} --dport 22 -j ACCEPT"
        )
        exit_code, _, _ = command_executor(check_rule_cmd, check_exit_code=False)
        if exit_code != 0:
            add_rule_cmd = (
                f"iptables -I INPUT 1 -p tcp -s {source_ip} --dport 22 -j ACCEPT"
            )
            command_executor(add_rule_cmd, check_exit_code=False)
            print(f"      - iptables: SSH 許可ルールを追加しました ({source_ip})")
            persist_cmds = [
                "service iptables save",
                "systemctl save iptables",
                "iptables-save > /etc/sysconfig/iptables",
            ]
            for cmd in persist_cmds:
                command_executor(cmd, check_exit_code=False)
            return
        print(f"      - iptables: 既に SSH 許可ルールが存在します ({source_ip})")
        return
    print("      - firewalld / iptables が有効ではないため、追加設定は行いません。")


def determine_prd_static_routes(nic_infos, default_gateway, original_routes):
    """Return PRD static routes with ownership metadata based on original STG routes."""
    routes: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, Optional[int]]] = set()
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
        owner_index: Optional[int] = route.get('owner_index')
        if owner_index is not None and (owner_index < 0 or owner_index >= len(nic_infos)):
            owner_index = None
        if gateway:
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
                        owner_index = idx
                        break
            except ipaddress.AddressValueError:
                owner_index = None
        if owner_index is None:
            for idx, nic in enumerate(nic_infos):
                if nic.get("is_gateway_nic"):
                    owner_index = idx
                    break
        if owner_index is None and nic_infos:
            owner_index = 0
        key = (str(prd_network), prd_gateway, owner_index)
        if key in seen:
            continue
        seen.add(key)
        routes.append({"network": str(prd_network), "gateway": prd_gateway, "owner_index": owner_index})
    return routes


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
    raise RuntimeError(f"宛先 vCenter に VM '{name}' が見つかりません (タイムアウト)。")


def execute_command_in_guest(guest_op_manager, vm, root_auth, admin_auth, admin_pwd, command, check_exit_code=True):
    """Execute a guest command, preferring root and falling back to admin without exposing passwords."""
    process_manager = guest_op_manager.processManager
    file_manager = guest_op_manager.fileManager
    stdout_path = f"/tmp/stdout_{os.urandom(4).hex()}.log"
    stderr_path = f"/tmp/stderr_{os.urandom(4).hex()}.log"

    def _run_it(auth, cmd):
        locale_wrapped_cmd = (
            "{ orig_lang=${LANG-}; orig_lc_all=${LC_ALL-}; "
            "restore_locale() { "
            "if [ -n \"$orig_lc_all\" ]; then export LC_ALL=\"$orig_lc_all\"; else unset LC_ALL; fi; "
            "if [ -n \"$orig_lang\" ]; then export LANG=\"$orig_lang\"; else unset LANG; fi; "
            "}; "
            "trap restore_locale EXIT; "
            "export LC_ALL=C; "
            f"{cmd}; "
            "cmd_status=$?; "
            "trap - EXIT; restore_locale; "
            "exit $cmd_status; }"
        )
        redirected_cmd = f"{locale_wrapped_cmd} > {stdout_path} 2> {stderr_path}"
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/bash",
            arguments=f"-lc {shlex.quote(redirected_cmd)}",
        )
        pid = process_manager.StartProgramInGuest(vm=vm, auth=auth, spec=spec)

        exit_code = -1
        start_time = time.time()
        while time.time() - start_time < 300:
            procs = process_manager.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid])
            if procs and procs[0].exitCode is not None:
                exit_code = procs[0].exitCode
                break
            time.sleep(2)

        stdout_content, stderr_content = "", ""
        for fpath, content_var in [(stdout_path, "stdout_content"), (stderr_path, "stderr_content")]:
            data = ""
            try:
                file_info = file_manager.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=fpath)
                if REQUESTS_AVAILABLE:
                    resp = requests.get(file_info.url, verify=False)
                    if resp.status_code == 200:
                        data = resp.text
                else:
                    ctx_inner = ssl._create_unverified_context()
                    with urllib.request.urlopen(file_info.url, context=ctx_inner) as resp:
                        data = resp.read().decode("utf-8", errors="replace")
                if data:
                    if content_var == "stdout_content":
                        stdout_content = data
                    else:
                        stderr_content = data
            except vim.fault.FileNotFound:
                pass
            except Exception:
                pass
            finally:
                try:
                    file_manager.DeleteFileInGuest(vm=vm, auth=auth, filePath=fpath)
                except (vim.fault.FileNotFound, vim.fault.GuestOperationsFault):
                    pass
        return exit_code, stdout_content.strip(), stderr_content.strip()

    print("[GUEST-CMD] 実行予定コマンド:")
    print(f'  {command}')
    exit_code, stdout, stderr = -1, '', ''
    auth_used = None
    fallback_error = None

    global ROOT_LOGIN_DISABLED

    def _run_as_admin():
        nonlocal exit_code, stdout, stderr, auth_used
        temp_password = None
        ctx_inner = ssl._create_unverified_context()
        try:
            temp_password = file_manager.CreateTemporaryFileInGuest(
                vm=vm,
                auth=admin_auth,
                prefix="sudo_pass_",
                suffix=".tmp",
                directoryPath="/tmp",
            )
            password_bytes = (admin_pwd + "\n").encode("utf-8")
            file_attr = vim.vm.guest.FileManager.FileAttributes()
            upload_url = file_manager.InitiateFileTransferToGuest(
                vm=vm,
                auth=admin_auth,
                guestFilePath=temp_password,
                fileAttributes=file_attr,
                fileSize=len(password_bytes),
                overwrite=True,
            )
            password_request = urllib.request.Request(
                upload_url,
                data=password_bytes,
                method="PUT",
                headers={"Content-Type": "application/octet-stream"},
            )
            with urllib.request.urlopen(password_request, context=ctx_inner):
                pass

            def _sudo_command(use_script_wrapper=False):
                quoted_command = shlex.quote(command)
                base_cmd = f"sudo -S -p '' /bin/bash -lc {quoted_command}"
                if use_script_wrapper:
                    return (
                        f"cat {shlex.quote(temp_password)} | "
                        f"script -q -c {shlex.quote(base_cmd)} /dev/null"
                    )
                return f"cat {shlex.quote(temp_password)} | {base_cmd}"

            result = _run_it(admin_auth, _sudo_command())
            exit_code_candidate, _, stderr_candidate = result
            stderr_lower = (stderr_candidate or "").lower()
            if exit_code_candidate != 0 and (
                "no tty present" in stderr_lower or "must have a tty" in stderr_lower
            ):
                print("[GUEST-CMD] sudo が TTY を要求したため script 経由で再実行します。")
                result = _run_it(admin_auth, _sudo_command(use_script_wrapper=True))
        finally:
            for cleanup_path in (temp_password,):
                if cleanup_path:
                    try:
                        file_manager.DeleteFileInGuest(vm=vm, auth=admin_auth, filePath=cleanup_path)
                    except vim.fault.FileNotFound:
                        pass
                    except Exception as cleanup_error:
                        try:
                            rm_spec = vim.vm.guest.ProcessManager.ProgramSpec(
                                programPath="/bin/rm",
                                arguments=f"-f {shlex.quote(cleanup_path)}",
                            )
                            process_manager.StartProgramInGuest(vm=vm, auth=admin_auth, spec=rm_spec)
                        except Exception as rm_error:
                            print(
                                f"[GUEST-CMD] 注意: 一時ファイル '{cleanup_path}' の削除に失敗しました: "
                                f"{cleanup_error}; rm結果: {rm_error}"
                            )
        exit_code, stdout, stderr = result
        auth_used = "admin"
        stderr_lower = (stderr or "").lower()
        if exit_code != 0 and "sudo:" in stderr_lower:
            raise RuntimeError(
                "sudo 実行に失敗しました。詳細: "
                f"{(stderr or '').strip() or '標準エラー出力なし'}"
            )
        return result

    if not ROOT_LOGIN_DISABLED:
        try:
            auth_used = 'root'
            exit_code, stdout, stderr = _run_it(root_auth, command)
        except vim.fault.InvalidGuestLogin as error:
            fallback_error = error
            ROOT_LOGIN_DISABLED = True
            print("[GUEST-CMD] root認証に失敗しました -> adminで再試行します。")
            print("[GUEST-CMD] 今後のコマンドは admin ユーザーで実行します。")
            _run_as_admin()
        except vim.fault.GuestOperationsFault as error:
            message = (getattr(error, 'msg', '') or '').lower()
            if 'auth' in message or 'permission' in message:
                fallback_error = error
                ROOT_LOGIN_DISABLED = True
                print("[GUEST-CMD] root認証に失敗しました -> adminで再試行します。")
                print("[GUEST-CMD] 今後のコマンドは admin ユーザーで実行します。")
                _run_as_admin()
            else:
                raise

    if ROOT_LOGIN_DISABLED and auth_used != 'admin':
        print("[GUEST-CMD] root認証は無効と判定しました。adminでコマンドを実行します。")
        _run_as_admin()

    print(f"[GUEST-CMD] 実行ユーザー: {auth_used}")
    print(f"[GUEST-CMD] 終了コード: {exit_code}")
    print("[GUEST-CMD] 標準出力:\n---\n" + (stdout or "(なし)") + "\n---")
    print("[GUEST-CMD] 標準エラー:\n---\n" + (stderr or "(なし)") + "\n---")

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
        print("[GUEST-CMD] 結果: 成功")
    else:
        print("[GUEST-CMD] 結果: 失敗")
        if check_exit_code:
            if exit_code != 0:
                reason = (stderr or "").strip() or "終了コードが 0 ではありません"
            elif stderr_indicates_error:
                reason = (stderr or "").strip() or "標準エラー出力にエラーが含まれていました"
            else:
                reason = "原因不明のエラー"
            if fallback_error is not None and auth_used == "admin":
                raise RuntimeError(
                    f"admin ユーザーでのコマンド実行に失敗しました (終了コード {exit_code}, 理由: {reason})"
                ) from fallback_error
            raise RuntimeError(
                f"コマンド実行に失敗しました (終了コード {exit_code}, 理由: {reason})"
            )
    return exit_code, stdout, stderr

# ------------------------------------------------
# 1. パスワード入力
# ------------------------------------------------
try:
    VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
    VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
except Exception as error:
    print('ERROR:', error)
    exit(1)

ROOT_LOGIN_DISABLED = False

# ------------------------------------------------
# 2. SSL コンテキスト設定
# ------------------------------------------------
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# ------------------------------------------------
# 3. 操作対象の VM 名とゲスト認証情報を取得
# ------------------------------------------------
target_vm_name = input("クローンを作成したい VM の名前を入力してください: ")
if not target_vm_name:
    print("VM名が入力されませんでした。処理を終了します。")
    exit(0)
try:
    GUEST_ROOT_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ROOT_USER}': ")
    GUEST_ADMIN_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ADMIN_USER}' (for fallback): ")
except Exception as error:
    print('ERROR:', error)
    exit(1)


# ------------------------------------------------
# メイン処琁E
# ------------------------------------------------
clone_name = None
vmx_path = None
new_vm_on_source = None
migrated_vm_for_rollback = None 
migrated_vm_name_for_rollback = None
unregistered_from_source = False
original_nic_info: List[Dict[str, Any]] = []
original_dns_servers: List[str] = []
original_default_gateway: str | None = None 
original_static_routes: List[Dict[str, Any]] = []
si_source = None
si_dest = None
sdk_network_client: VsphereGuestNetworkSDK | None = None

try:
    # --- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---
    print("\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---")
    print(f"   ソース vCenter ({VCSA_HOST_SOURCE}) に接続を試みています...")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source:
        raise ConnectionError(f"ソース vCenter ({VCSA_HOST_SOURCE}) への認証に失敗しました。")
    print("   ✓ ソース vCenter 認証に成功しました。")
    Disconnect(si_source)
    si_source = None

    print(f"   宛先 vCenter ({VCSA_HOST_DEST}) に接続を試みています...")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest:
        raise ConnectionError(f"宛先 vCenter ({VCSA_HOST_DEST}) への認証に失敗しました。")
    print("   ✓ 宛先 vCenter 認証に成功しました。")
    Disconnect(si_dest)
    si_dest = None
    
    # --- [Phase 1/7] Source vCenter: Collect Info & Prepare ---
    print("\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source:
        raise ConnectionError(f"ソース vCenter ({VCSA_HOST_SOURCE}) に接続できませんでした。")
    print("✓ ソース vCenter への接続に成功しました。")
    
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
        raise FileNotFoundError(f"VM '{target_vm_name}' は見つかりませんでした。")
    print(f"✓ VM '{target_vm.name}' を確認しました。")

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

    print("   VMware Tools が稼働中であることを確認しました。")

    print("   クローン元 VM の NIC 情報を収集しています...")
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
        ip_address = None
        prefix_len = None
        sdk_interface_index = None
        sdk_nic_id = None
        if sdk_iface_entry:
            iface_data, iface_idx = sdk_iface_entry
            sdk_interface_index = iface_idx
            sdk_nic_id = iface_data.get('nic')
            ip_data = iface_data.get('ip') or {}
            ip_entries = ip_data.get('ip_addresses') or []
            for entry in ip_entries:
                if isinstance(entry, dict) and entry.get('ip_address') and '.' in entry.get('ip_address'):
                    ip_address = entry.get('ip_address')
                    prefix_len = entry.get('prefix_length')
                    break
        subnet_mask = prefix_to_subnet_mask(prefix_len) if prefix_len is not None else None
        if not ip_address and guest_nic and guest_nic.ipConfig and getattr(guest_nic.ipConfig, 'ipAddress', None):
            ip_v4_info = next((ip for ip in guest_nic.ipConfig.ipAddress if '.' in ip.ipAddress), None)
            if ip_v4_info and getattr(ip_v4_info, 'ipAddress', None):
                ip_address = ip_v4_info.ipAddress
                if subnet_mask is None:
                    subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
        if not ip_address:
            missing_ipv4_messages.append(f"NIC {mac} ({network_name}) data could not be retrieved from API/VMware Tools.")
            continue
        if subnet_mask is None and guest_nic and guest_nic.ipConfig and getattr(guest_nic.ipConfig, 'ipAddress', None):
            ip_v4_info = next((ip for ip in guest_nic.ipConfig.ipAddress if '.' in ip.ipAddress), None)
            if ip_v4_info:
                subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
        if subnet_mask is None:
            missing_ipv4_messages.append(f"NIC {mac} ({network_name}) does not provide a subnet mask.")
            continue

        nic_record = {
            'device_type': type(device),
            'mac_address': mac,
            'network_name': network_name,
            'ip_address': ip_address,
            'subnet_mask': subnet_mask,
            'is_gateway_nic': False,
        }
        if sdk_interface_index is not None:
            nic_record['sdk_interface_index'] = sdk_interface_index
        if sdk_nic_id:
            nic_record['sdk_nic_id'] = sdk_nic_id
        original_nic_info.append(nic_record)

    if source_routes:
        original_static_routes.clear()
        for route in source_routes:
            network = route.get('network')
            prefix = route.get('prefix_length')
            gateway = route.get('gateway_address')
            interface_index = route.get('interface_index')
            if network is None or prefix is None:
                continue
            entry = {
                'network': network,
                'prefix': prefix,
                'gateway': gateway,
            }
            if interface_index is not None:
                entry['owner_index'] = interface_index
            original_static_routes.append(entry)
            if gateway and (network == '0.0.0.0' or prefix == 0):
                original_default_gateway = gateway
                if interface_index is not None and 0 <= interface_index < len(original_nic_info):
                    original_nic_info[interface_index]['is_gateway_nic'] = True
        if not original_default_gateway:
            for route in source_routes:
                gateway = route.get('gateway_address')
                interface_index = route.get('interface_index')
                if gateway and interface_index is not None and 0 <= interface_index < len(original_nic_info):
                    original_default_gateway = gateway
                    original_nic_info[interface_index]['is_gateway_nic'] = True
                    break

    if not source_routes and target_vm.guest.ipStack and target_vm.guest.ipStack[0].ipRouteConfig:
        for route in target_vm.guest.ipStack[0].ipRouteConfig.ipRoute:
            if route.network == '0.0.0.0' and route.prefixLength == 0:
                original_default_gateway = route.gateway.ipAddress
                print(f"   デフォルトゲートウェイ '{original_default_gateway}' を取得しました。")
                # ゲートウェイがどのNICに属するかを判定
                for nic in original_nic_info:
                    try:
                        nic_iface = ipaddress.IPv4Interface(f"{nic['ip_address']}/{nic['subnet_mask']}")
                        gw_addr = ipaddress.IPv4Address(original_default_gateway)
                        if gw_addr in nic_iface.network:
                            nic['is_gateway_nic'] = True
                            print(f"   -> IP {nic['ip_address']} のNICをゲートウェイNICと判定しました。")
                            break
                    except (ValueError, ipaddress.AddressValueError):
                        continue
            else:
                network = getattr(route, 'network', None)
                prefix = getattr(route, 'prefixLength', None)
                gateway_obj = getattr(route, 'gateway', None)
                gateway = getattr(gateway_obj, 'ipAddress', None) if gateway_obj else None
                if network and prefix is not None:
                    original_static_routes.append({
                        'network': network,
                        'prefix': prefix,
                        'gateway': gateway,
                    })
    if original_static_routes:
        print("   取得したスタティックルート(STG):")
        for route in original_static_routes:
            gw_disp = route['gateway'] or '(none)'
            print(
                f"      - {route['network']}/{route['prefix']} via {gw_disp}"
            )
    
    if source_networking_state:
        dns_info = source_networking_state.get('dns') or {}
        original_dns_servers = [
            dns for dns in (dns_info.get('ip_addresses') or [])
            if dns and not str(dns).startswith('127.')
        ]

    if not original_dns_servers and target_vm.guest.ipStack and target_vm.guest.ipStack[0].dnsConfig:
        original_dns_servers = [dns for dns in target_vm.guest.ipStack[0].dnsConfig.ipAddress if not dns.startswith('127.')]

    print(f"   ✓ {len(original_nic_info)}件のNIC構成を取得しました。")

    target_datastore = next((ds for ds in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME), None)
    if not target_datastore:
        raise FileNotFoundError(f"データストア '{TARGET_DATASTORE_NAME}' が見つかりませんでした。")
    print(f"   ✓ データストア '{target_datastore.name}' を確認しました。")

    date_suffix = datetime.now().strftime('%Y%m%d')
    clone_name = f"{target_vm.name}-{date_suffix}"
    
    # --- クローン操作の承認 (1/4) ---
    print("\n" + "=" * 25 + " 実行前確認 (1/4) " + "=" * 25)
    print("以下の内容でクローンを作成し、移行を開始します。内容をご確認ください。")
    print(f"\n  [クローン元 VM 情報]")
    print(f"    - VM名          : {target_vm.name}")
    print(f"    - OS            : {target_vm.summary.config.guestFullName}")
    print("\n  [クローン元 NIC 情報]")
    if original_nic_info:
        for i, nic in enumerate(original_nic_info):
            print(f"    - NIC {i+1} ({nic['mac_address']})")
            print(f"      - Network     : {nic['network_name']}")
            print(f"      - IP Address  : {nic['ip_address']}")
            print(f"      - Subnet Mask : {nic['subnet_mask']}")
    else:
        print("    - NIC情報が見つかりませんでした。")
    if original_default_gateway:
        print(f"    - Gateway     : {original_default_gateway}")
    else:
        print("    - デフォルトゲートウェイが見つかりませんでした。")

    print("\n  [クローン先 VM の仕様]")
    print(f"    - 新しい VM 名  : {clone_name}")
    print(f"    - 配置データストア: {TARGET_DATASTORE_NAME}")
    print("=" * 64)

    user_approval = input("\nこのクローン操作を実行してもよろしいですか？ (y/n): ")
    if user_approval.lower() != 'y':
        raise InterruptedError("ユーザーによって操作がキャンセルされました。")
    
    # --- クローン、NIC削除、登録解除 ---
    relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
    clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
    print("\nクローンタスクを開始します...")
    task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        progress = task.info.progress or 0
        print(f"   クローン進捗: {progress}%", end='\r')
        time.sleep(5)
    print(" " * 40, end='\r')
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"クローン作成エラー: {task.info.error.msg}")
    print(f"\n✓ クローン作成成功: '{clone_name}'")
    
    new_vm_on_source = task.info.result
    # NIC削除処琁E
    print(f"   クローンした VM '{new_vm_on_source.name}' の NIC を削除します...")
    nic_devices_to_remove = [dev for dev in new_vm_on_source.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
    if nic_devices_to_remove:
        nic_change_spec = [vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic) for nic in nic_devices_to_remove]
        config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
        task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"NIC削除エラー: {task.info.error.msg}")
        print("   ✓ NIC を削除しました。")
    
    vmx_path = new_vm_on_source.config.files.vmPathName
    print(f"   VM '{clone_name}' をソース vCenter から登録解除します...")
    new_vm_on_source.UnregisterVM()
    unregistered_from_source = True
    print("   ✓ 登録解除完了。")
    Disconnect(si_source)
    si_source = None
    new_vm_on_source = None 

    # --- [Phase 2/7] ~ [Phase 7/7]: 宛先 vCenter での処理 ---
    print("\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest:
        raise ConnectionError(f"宛先 vCenter ({VCSA_HOST_DEST}) に接続できませんでした。")
    print("✓ 宛先 vCenter への接続に成功しました。")
    content_dest = si_dest.RetrieveContent()
    if any(vm for vm in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name):
        raise FileExistsError(f"同名の VM '{clone_name}' が宛先 vCenter に既に存在します。")
    print("✓ 宛先 vCenter に同名の VM は存在しません。")

    print("\n--- [Phase 3/7] Destination vCenter: Register VM ---")
    dest_cluster = next((c for c in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.ClusterComputeResource], True).view if c.name == TARGET_CLUSTER_NAME), None)
    if not dest_cluster:
        raise FileNotFoundError(f"宛先クラスタ '{TARGET_CLUSTER_NAME}' が見つかりませんでした。")
    task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        time.sleep(5)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"宛先 vCenter での VM 登録エラー: {task.info.error.msg}")
    migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=60, delay_seconds=2)
    migrated_vm_for_rollback = migrated_vm  # ロールバック用に保持
    migrated_vm_name_for_rollback = clone_name
    print("✓ VM の登録が完了しました。")

    print("\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
    if original_nic_info:
        print("\n" + "=" * 25 + " 実行前確認 (2/4) " + "=" * 25)
        print("移行した VM に NIC を再作成し、次のネットワークに接続します。")
        device_change_spec = []
        for i, nic in enumerate(original_nic_info):
            original_network_name = nic['network_name']
            dest_network_name = original_network_name.replace('STG', 'PRD', 1)
            print(f"  - NIC {i+1}: '{original_network_name}' -> '{dest_network_name}'")

            dest_network = next((net for net in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.Network], True).view if net.name == dest_network_name), None)
            
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
                raise FileNotFoundError(f"宛先ネットワーク '{dest_network_name}' が見つかりませんでした。")

            nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True)
            device_change_spec.append(nic_spec)
        
        print("=" * 64)
        user_approval_nic = input("\nこの NIC 設定を実行してもよろしいですか？ (y/n): ")
        if user_approval_nic.lower() != 'y':
            raise InterruptedError("ユーザーによってNIC設定がキャンセルされました。")

        print("\n承認されました。NIC 再設定タスクを開始します...")
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
            raise RuntimeError(f"NICの再設定に失敗しました: {task.info.error.msg}")
        print("   ✓ NIC の再設定が完了しました。")
        
        print("   新しい NIC 情報を取得中...")
        try:
            migrated_vm.Reload()
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
            migrated_vm_for_rollback = migrated_vm
            migrated_vm_name_for_rollback = clone_name
            migrated_vm.Reload()
        newly_added_nics = [dev for dev in migrated_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        if len(newly_added_nics) == len(original_nic_info):
            for nic_entry, new_nic in zip(original_nic_info, newly_added_nics):
                nic_entry['new_mac_address'] = new_nic.macAddress
            print("   ✓ 新しい MAC アドレスを紐付けました。")
        else:
            raise RuntimeError("再作成した NIC 数が想定数と一致しません。")
    else:
        print("   - 元の VM に NIC がなかったため、NIC の再設定はスキップされました。")

    print("\n--- [Phase 5/7] Destination vCenter: Power On ---")
    print("\n" + "=" * 25 + " 実行前確認 (3/4) " + "=" * 25)
    print("VM をパワーオンし、ゲスト OS の IP アドレスを設定します。")
    if original_nic_info:
        new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
        gateway_nic_present = any(nic.get('is_gateway_nic') for nic in original_nic_info)
        prd_static_routes = determine_prd_static_routes(
            original_nic_info,
            new_default_gateway,
            original_static_routes,
        )
        if prd_static_routes:
            print("   -> PRD向けスタティックルート候補:")
            for route_info in prd_static_routes:
                owner_index = route_info.get('owner_index')
                owner_label = f"NIC #{owner_index + 1}" if owner_index is not None else '任意のNIC'
                print(f"      - {route_info['network']} via {route_info['gateway']} ({owner_label})")
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
        if new_default_gateway:
            print("\n  [デフォルトゲートウェイの設定]")
            print(f"    - Gateway     : {original_default_gateway} -> {new_default_gateway}")
        
        if original_dns_servers:
            print("\n  [DNSサーバーの設定]")
            new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
            for old_dns, new_dns in zip(original_dns_servers, new_dns_servers):
                print(f"    - {old_dns} -> {new_dns}")
    else:
        print("  - NIC 情報がないため、IP 設定は行われません。")
    print("=" * 64)
    
    user_approval_ip = input("\nこの IP 設定を実施し、VM をパワーオンしますか？ (y/n): ")
    if user_approval_ip.lower() != 'y':
        raise InterruptedError("ユーザーによって IP 設定とパワーオンがキャンセルされました。")

    print("\n承認されました。VM をパワーオンします...")
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
        raise RuntimeError(f"VM のパワーオンに失敗しました: {task.info.error.msg}")
    print("   ✓ VM を正常にパワーオンしました。")

    print("   ゲスト OS 操作エージェントの準備を確認しています (最大5分)...")
    guest_operations_manager = content_dest.guestOperationsManager
    agent_ready = False
    for i in range(10): 
        print(f"    - 試行 {i+1}/10...")
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
        raise SystemError("タイムアウト: ゲスト OS 操作エージェントが利用可能になりませんでした。")
    print("   ✓ ゲスト OS 操作エージェントの準備が整いました。")

    print(f"\n--- [Phase 6/7] Destination vCenter: Set IP Address ---")
    
    if original_nic_info:
        root_credentials = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
        admin_credentials = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ADMIN_USER, password=GUEST_ADMIN_PWD)

        def guest_command_executor(command, check_exit_code=True):
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
                migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
                migrated_vm_for_rollback = migrated_vm
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
                LOGGER.warning("SDK ベースのネットワーク再構成を初期化できませんでした: %s", sdk_error)
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
            expected_gateway_value = new_default_gateway if nic_info.get('is_gateway_nic') and new_default_gateway else None
            expected_dns_servers: List[str] = []
            applied_static_routes: List[str] = []
            
            print("\n" + "=" * 20 + f" NIC {i+1} の設定 " + "=" * 20)
            
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

            sdk_success = False
            sdk_dns_servers: List[str] = []
            if use_sdk_networking and sdk_network_client and sdk_vm_id:
                nic_identifier = find_interface_id_by_mac(sdk_interfaces, new_mac) or find_interface_id_by_mac(
                    sdk_interfaces, nic_info.get('mac_address')
                )
                if nic_identifier:
                    if i == 0 and original_dns_servers:
                        sdk_dns_servers = [
                            calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns
                        ]
                    route_specs: List[RouteConfig] = []
                    applied_static_routes = []
                    for route_idx, route_info in enumerate(prd_static_routes or []):
                        owner_index = route_info.get('owner_index')
                        if owner_index is not None and owner_index != i:
                            continue
                        if route_idx in configured_route_indices:
                            continue
                        route_network = route_info.get('network')
                        route_gateway = route_info.get('gateway')
                        if not route_network or not route_gateway:
                            continue
                        route_specs.append(RouteConfig(network=route_network, gateway=route_gateway))
                        configured_route_indices.add(route_idx)
                        applied_static_routes.append(f"{route_network} {route_gateway}")
                    ipv4_spec = IPv4Config(
                        address=new_ip,
                        prefix=prefix,
                        default_gateway=expected_gateway_value,
                    )
                    dns_spec = DnsConfig(sdk_dns_servers) if sdk_dns_servers else None
                    try:
                        sdk_network_client.update_interface(
                            vm_id=sdk_vm_id,
                            nic_id=nic_identifier,
                            ipv4=ipv4_spec,
                            dns=dns_spec,
                            routes=route_specs,
                        )
                        sdk_interfaces = sdk_network_client.list_interfaces(sdk_vm_id)
                        if sdk_dns_servers:
                            expected_dns_servers = sdk_dns_servers
                        sdk_success = True
                        if applied_static_routes:
                            print("   -> Configured static routes via SDK.")
                            for route_line in applied_static_routes:
                                print(f"      - Added: {route_line}")
                        print("   -> Completed NIC reconfiguration via SDK.")
                        time.sleep(5)
                        if new_ip:
                            guest_command_executor(f"ip addr show {device_name} | grep -q '{new_ip}'", check_exit_code=False)
                        if new_ip:
                            arping_commands = [
                                f"arping -c 3 -A -I {device_name} {new_ip}",
                                f"arping -c 3 -U -I {device_name} {new_ip}",
                            ]
                            for arping_cmd in arping_commands:
                                guest_command_executor(arping_cmd, check_exit_code=False)
                        candidate_gateways = []
                        if expected_gateway_value:
                            candidate_gateways.append(expected_gateway_value)
                        for route_entry in applied_static_routes:
                            try:
                                _, gw_val = route_entry.split()
                            except ValueError:
                                continue
                            if gw_val and gw_val not in candidate_gateways:
                                candidate_gateways.append(gw_val)
                        unique_targets = []
                        for target in candidate_gateways:
                            if target and target not in unique_targets:
                                unique_targets.append(target)
                        for target in unique_targets:
                            guest_command_executor(
                                f"bash -c 'for i in $(seq 1 3); do ping -c 1 -W 2 {target} && exit 0; sleep 2; done; exit 1'",
                                check_exit_code=False,
                            )
                    except Exception as sdk_update_error:
                        print(f"   -> SDK update failed; falling back to nmcli: {sdk_update_error}")
                        sdk_success = False
                else:
                    print("   -> SDK could not match a NIC by MAC; applying settings with nmcli.")

            if sdk_success:
                continue

            # 2. Disconnect and delete existing connections
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
            alias_targets_compact = {value.replace('-', '').replace('_', '').replace(' ', '') for value in alias_targets}
            mac_targets = set()
            for value in (mac_normalized, old_mac_normalized):
                if value:
                    mac_targets.add(value)
                    mac_targets.add(value.replace(':', ''))
                    mac_targets.add(value.replace(':', '-'))
            stale_connection_uuids = set()
            connection_detail_cache = {}

            def get_connection_details(uuid_value):
                if uuid_value in connection_detail_cache:
                    return connection_detail_cache[uuid_value]
                detail_cmd = f"nmcli connection show {uuid_value}"
                detail_exit, detail_stdout, _ = guest_command_executor(detail_cmd, check_exit_code=False)
                connection_detail_cache[uuid_value] = (detail_exit, detail_stdout)
                return connection_detail_cache[uuid_value]

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
                print(f"   -> 古い nmcli 接続を削除します ({len(stale_connection_uuids)} 件)。")
                for uuid in sorted(stale_connection_uuids):
                    guest_command_executor(f"nmcli connection delete uuid {uuid}")
            # 3. Add new connection and configure it
            guest_command_executor(f"nmcli connection add type ethernet con-name '{con_name}' ifname '{device_name}'")
            guest_command_executor(f"nmcli connection modify '{con_name}' ipv4.method manual ipv4.addresses '{new_ip}/{prefix}'")
            if nic_info.get('is_gateway_nic') and new_default_gateway:
                guest_command_executor(f"nmcli connection modify '{con_name}' ipv4.gateway '{new_default_gateway}'")
            
            duplicate_detected = False
            duplicate_exit = None
            duplicate_stdout = ""
            duplicate_stderr = ""
            if new_ip:
                duplicate_cmd = f"arping -D -c 3 -I {device_name} {new_ip}"
                duplicate_exit, duplicate_stdout, duplicate_stderr = guest_command_executor(duplicate_cmd, check_exit_code=False)
                LOGGER.debug(
                    "Duplicate IP check for %s on %s returned exit=%s",
                    new_ip,
                    device_name,
                    duplicate_exit,
                )
                if duplicate_exit != 0:
                    duplicate_detected = True
                    print("\n[WARN] 重複IPアドレスが検出されました。")
                    print(f"    - 対象NIC : {device_name}")
                    print(f"    - IP      : {new_ip}")
                    if duplicate_stdout:
                        print("    - arping 標準出力")
                        for line in duplicate_stdout.splitlines():
                            print(f"        {line}")
                    if duplicate_stderr:
                        print("    - arping標準エラー:")
                        for line in duplicate_stderr.splitlines():
                            print(f"        {line}")
                    guest_command_executor(f"nmcli connection delete '{con_name}'", check_exit_code=False)
                    guest_command_executor(f"ip addr del {new_ip}/{prefix} dev {device_name}", check_exit_code=False)
                    decision = input("\nこの NIC の設定をスキップして続行しますか？ (c=続行 / a=中断): ").strip().lower()
                    if decision == 'c':
                        print("   -> ユーザー指示により当該 NIC の設定をスキップしました。")
                        continue
                    raise InterruptedError(f"IPアドレス {new_ip} の重複が検出されたため処理を中断しました。")
            
            if i == 0 and original_dns_servers: # Typically DNS is set on the primary NIC
                new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
                if new_dns_servers:
                    dns_str = ' '.join(new_dns_servers)
                    guest_command_executor(f"nmcli connection modify '{con_name}' ipv4.dns '{dns_str}'")
                    expected_dns_servers = new_dns_servers

            should_configure_routes = nic_info.get('is_gateway_nic')
            if not should_configure_routes and new_default_gateway and new_ip:
                try:
                    nic_network = ipaddress.IPv4Interface(f"{new_ip}/{prefix}").network
                    if ipaddress.IPv4Address(new_default_gateway) in nic_network:
                        should_configure_routes = True
                except (ValueError, ipaddress.AddressValueError):
                    pass
            if not should_configure_routes and not gateway_nic_present:
                should_configure_routes = (i == 0)

            if should_configure_routes and prd_static_routes:
                added_routes = False
                for route_idx, route_info in enumerate(prd_static_routes):
                    owner_index = route_info.get('owner_index')
                    if owner_index is not None and owner_index != i:
                        continue
                    if route_idx in configured_route_indices:
                        continue
                    network_cidr = route_info['network']
                    gateway = route_info['gateway']
                    try:
                        network_obj = ipaddress.IPv4Network(network_cidr, strict=False)
                        if new_ip and ipaddress.IPv4Address(new_ip) in network_obj:
                            continue
                    except (ValueError, ipaddress.AddressValueError):
                        pass
                    guest_command_executor(f"nmcli connection modify '{con_name}' +ipv4.routes '{network_cidr} {gateway}'")
                    applied_static_routes.append(f"{network_cidr} {gateway}")
                    configured_route_indices.add(route_idx)
                    if not added_routes:
                        print("   -> PRD向けスタティックルートを設定します。")
                        added_routes = True
                    print(f"      - 追加: {network_cidr} via {gateway}")

            # 4. Bring up the new connection
            guest_command_executor(f"nmcli connection up '{con_name}'")
            
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
            if new_default_gateway:
                candidate_gateways.append(new_default_gateway)
            if new_ip and prefix:
                try:
                    iface = ipaddress.IPv4Interface(f"{new_ip}/{prefix}")
                    first_host = next(iface.network.hosts(), None)
                    if first_host:
                        candidate_gateways.append(str(first_host))
                except ValueError:
                    LOGGER.debug("Failed to derive fallback gateway from %s/%s", new_ip, prefix, exc_info=True)
            for candidate in candidate_gateways:
                if candidate and candidate != new_ip and candidate not in ping_targets:
                    ping_targets.append(candidate)
            LOGGER.debug("Connectivity targets for %s (%s): %s", device_name, new_ip, ping_targets or "[none]")
            try:
                ensure_connection_activation(
                    guest_command_executor,
                    con_name,
                    device_name,
                    ping_targets=ping_targets,
                )
            except RuntimeError as activation_error:
                print(f"\n[WARN] 接続 '{con_name}' の疎通確認に失敗しました: {activation_error}")
                decision = input("疎通失敗のまま続行しますか？ (c=続行 / a=中断): ").strip().lower()
                if decision == 'c':
                    print("   -> ユーザー指示により疎通検証失敗を無視して続行します。")
                else:
                    raise
        
        expected_ip_cidr = f"{new_ip}/{prefix}" if new_ip else ""
        try:
            verify_nmcli_connection_settings(
                guest_command_executor,
                con_name,
                device_name,
                expected_ip_cidr,
                expected_gateway_value,
                applied_static_routes,
                expected_dns_servers,
            )
        except RuntimeError as validation_error:
            print(f"\n[WARN] nmcli validation failed: {validation_error}")
            decision = input("Continue despite nmcli validation failure? (c=continue / a=abort): ").strip().lower()
            if decision == 'c':
                print("   -> Proceeding despite nmcli validation failure per user request.")
            else:
                raise

        ensure_firewall_allows_ssh(guest_command_executor, SSH_ALLOWED_SOURCE_IP)
        print("   ✓ 全ての NIC の IP 設定が完了しました。")

    print("\n--- [Phase 7/7] Destination vCenter: Final Storage vMotion ---")
    print(f"最終データストア '{TARGET_DATASTORE_NAME_FINAL}' を検索中...")
    final_datastore = next((ds for ds in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME_FINAL), None)
    if not final_datastore:
        raise FileNotFoundError(f"最終データストア '{TARGET_DATASTORE_NAME_FINAL}' が見つかりませんでした。")
    print(f"✓ 最終データストア '{final_datastore.name}' を確認しました。")

    print("\n" + "=" * 25 + " 実行前確認 (4/4) " + "=" * 25)
    print("VM のストレージを最終的な PRD データストアに移動します。")
    print(f"  - 対象VM: {clone_name}")
    try:
        current_datastores = ', '.join([ds.name for ds in migrated_vm.datastore])
    except vmodl.fault.ManagedObjectNotFound:
        migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
        migrated_vm_for_rollback = migrated_vm
        migrated_vm_name_for_rollback = clone_name
        current_datastores = ', '.join([ds.name for ds in migrated_vm.datastore])
    print(f"  - 現在のデータストア: {current_datastores}")
    print(f"  - 移行先データストア: {TARGET_DATASTORE_NAME_FINAL}")
    print("=" * 64)
    
    user_approval_svmotion = input("\nこのストレージ vMotion を実行してもよろしいですか？ (y/n): ")
    if user_approval_svmotion.lower() != 'y':
        raise InterruptedError("ユーザーによってストレージ vMotion がキャンセルされました。")

    print("\n承認されました。ストレージ vMotion タスクを開始します...")
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
        print(f"   ストレージ vMotion の進捗: {progress}%", end='\r')
        time.sleep(5)
    print(" " * 40, end='\r')
    
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"最終的なストレージvMotionに失敗しました: {task.info.error.msg}")
    
    print("\n✓ ストレージ vMotion が正常に完了しました。")
    print("\n✓ すべての移行プロセスが正常に完了しました。")
    Disconnect(si_dest)
    si_dest = None


except Exception as e:
    print(f"\n[ERROR] 処理中にエラーが発生しました: {e}")
    if migrated_vm_for_rollback:
        print("\n" + "=" * 20 + " ロールバック確認 (宛先 VM 削除) " + "=" * 20)
        print("処理が中断されたため、宛先 vCenter に作業途中の VM が残っています。")
        vm_name_display = migrated_vm_name_for_rollback or clone_name or "(不明)"
        print(f"  - 対象VM: {vm_name_display}")
        
        rollback_approval = input("\nこの VM を削除して操作前の状態に戻しますか？ (y/n): ")
        if rollback_approval.lower() == 'y':
            try:
                if si_dest is None or not si_dest.CurrentTime(): # 接続が切れている場合は再接続
                    print("   クリーンアップのため宛先 vCenter に再接続します...")
                    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
                    if not si_dest:
                        raise ConnectionError("宛先 vCenter への再接続に失敗しました。") from None
                    print("   ✓ 再接続に成功しました。")

                content_dest_cleanup = si_dest.RetrieveContent()
                vm_to_delete = find_vm_by_name(content_dest_cleanup, clone_name)
                if not vm_to_delete:
                    print("   ⚠ ロールバック対象の VM が見つかりません。既に削除済みの可能性があります。")
                    unregistered_from_source = True 
                else:
                    if vm_to_delete.runtime.powerState == 'poweredOn':
                        print(f"   VM '{vm_to_delete.name}' をパワーオフします...")
                        task = vm_to_delete.PowerOffVM_Task()
                        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                            time.sleep(2)
                        if task.info.state == vim.TaskInfo.State.success:
                            print("   ✓ パワーオフ完了。")
                        else:
                            print(f"   ⚠ パワーオフに失敗しました: {task.info.error.msg}。削除を継続します。")

                    print(f"   VM '{vm_to_delete.name}' を削除します...")
                    destroy_task = vm_to_delete.Destroy_Task()
                    while destroy_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                        time.sleep(2)
                    
                    if destroy_task.info.state == vim.TaskInfo.State.success:
                        print("✓ ロールバック完了: 宛先 VM を削除しました。")
                        unregistered_from_source = False
                    else:
                        unregistered_from_source = True
                        raise RuntimeError(f"VMの削除に失敗しました: {destroy_task.info.error.msg}") from None

            except Exception as cleanup_error:
                print(f"⚠ 宛先 VM のロールバック中にエラーが発生しました: {cleanup_error}")
                unregistered_from_source = True 

    if unregistered_from_source:
        print("\n" + "=" * 20 + " ロールバック確認 (ファイル削除) " + "=" * 20)
        print("   ソース vCenter 側のクローンファイルをクリーンアップする必要があります。")
        print(f"   対象 VM のファイルがデータストア '{TARGET_DATASTORE_NAME}' に残っている可能性があります。")
        
        rollback_approval_files = input("\nソース vCenter に接続してこれらのファイルを削除しますか？ (y/n): ")
        if rollback_approval_files.lower() == 'y':
            si_source_cleanup = None
            try:
                print("\n承認されました。クリーンアップのためソース vCenter に再接続します...")
                si_source_cleanup = SmartConnect(
                    host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
                if not si_source_cleanup:
                    raise ConnectionError("ソース vCenter への再接続に失敗しました。") from None
                print("   ✓ 再接続に成功しました。")
                
                content_cleanup = si_source_cleanup.RetrieveContent()
                file_manager = content_cleanup.fileManager

                vm_dir_path = os.path.dirname(vmx_path)

                print(f"   データストアからディレクトリ '{vm_dir_path}' を削除します...")
                datacenter = content_cleanup.rootFolder.childEntity[0]
                delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)

                while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)

                if delete_task.info.state == vim.TaskInfo.State.success:
                    print("✓ ロールバック完了: データストア上のファイルを削除しました。")
                else:
                    raise RuntimeError(f"データストアのファイル削除に失敗しました: {delete_task.info.error.msg}") from None

            except Exception as cleanup_error:
                print(f"⚠ ロールバック処理中にエラーが発生しました: {cleanup_error}")
                print("   お手数ですが、データストア ブラウザから手動でクリーンアップしてください。")
            finally:
                if si_source_cleanup:
                    Disconnect(si_source_cleanup)
        else:
            print("ユーザーによってファイルクリーンアップがキャンセルされました。ファイルはデータストア上に残っています。")
    elif new_vm_on_source:
        print("\n" + "=" * 20 + " ロールバック確認 (ソース VM 削除) " + "=" * 20)
        print(f"作業途中だった VM '{new_vm_on_source.name}' がソース vCenter に残っています。")
        rollback_approval = input("\nこの VM を削除して操作前の状態に戻しますか？ (y/n): ")
        if rollback_approval.lower() == 'y':
            if new_vm_on_source.runtime.powerState == 'poweredOn':
                print(f"   VM '{new_vm_on_source.name}' をパワーオフします...")
                poweroff_task = new_vm_on_source.PowerOffVM_Task()
                while poweroff_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)
                if poweroff_task.info.state == vim.TaskInfo.State.success:
                    print("   ✓ パワーオフ完了。")
                else:
                    print(f"   ⚠ パワーオフに失敗しました: {poweroff_task.info.error.msg}。削除を継続します。")
            task = new_vm_on_source.Destroy_Task()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state == vim.TaskInfo.State.success:
                print("✓ ロールバック完了: ソース VM を削除しました。")
            else:
                print(f"⚠ ロールバック失敗: {task.info.error.msg}")
        else:
            print("ロールバックはキャンセルされました。VM はソース vCenter に残ります。")

finally:
    try:
        if 'sdk_network_client' in locals() and sdk_network_client:
            sdk_network_client.close()
    except Exception:
        pass
    try:
        if 'si_dest' in locals() and si_dest:
            Disconnect(si_dest)
    except Exception:
        pass
    try:
        if 'si_source' in locals() and si_source:
            Disconnect(si_source)
    except Exception:
        pass
    print("処理を終了します。")
