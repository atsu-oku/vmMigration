# -*- coding: utf-8 -*-
"""Automates the staged-to-production VM migration workflow between vCenters."""
from __future__ import annotations
import argparse
import builtins
import os
import ssl
import sys
import getpass
import time
import threading
import logging
import socket
import ipaddress
import shlex
import uuid as uuid_module
import re
from dataclasses import dataclass, field
from datetime import datetime
import importlib.util
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple, Type, TypeVar, TYPE_CHECKING

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


T = TypeVar("T")

LINK_LOCAL_PREFIX = ipaddress.ip_network("169.254.0.0/16")


def _get_env_override(var_name: str, default: str) -> str:
    """Return an environment override when present, otherwise fall back to the default."""
    value = os.getenv(var_name)
    if value is None:
        return default
    candidate = value.strip()
    return candidate or default


def dedupe_preserving_order(values: Iterable[T]) -> List[T]:
    """Return a list with duplicates removed while preserving their original order."""
    seen: Set[T] = set()
    result: List[T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _is_link_local_network(network_value: str, prefix: int) -> bool:
    """Return True when the given network/prefix falls within the IPv4 link-local range."""
    try:
        network_spec = network_value if "/" in network_value else f"{network_value}/{prefix}"
        candidate = ipaddress.ip_network(network_spec, strict=False)
        return isinstance(candidate, ipaddress.IPv4Network) and candidate.subnet_of(LINK_LOCAL_PREFIX)
    except ValueError:
        return False


def _is_link_local_address(address: Optional[str]) -> bool:
    """Return True when the given IPv4 address lives inside the link-local range."""
    if not address:
        return False
    try:
        return ipaddress.ip_address(address) in LINK_LOCAL_PREFIX
    except ValueError:
        return False


SUCCESS_EVENTS: List[str] = []
FAILURE_EVENTS: List[str] = []


def log_success(message: str) -> None:
    """Record a success message for the execution summary."""
    normalized = message.strip()
    if normalized and normalized not in SUCCESS_EVENTS:
        SUCCESS_EVENTS.append(normalized)


def log_failure(message: str) -> None:
    """Record a failure or warning message for the execution summary."""
    normalized = message.strip()
    if normalized and normalized not in FAILURE_EVENTS:
        FAILURE_EVENTS.append(normalized)


_ORIGINAL_PRINT = builtins.print


def _tracking_print(*args, **kwargs) -> None:
    """Proxy print function that tracks notable success/failure messages."""
    sep = kwargs.get("sep", " ")
    message = sep.join(str(arg) for arg in args)
    normalized = message.lstrip()
    if normalized.startswith("[OK]"):
        log_success(normalized[len("[OK]") :].strip())
    elif normalized.startswith("[WARN]"):
        log_failure(normalized[len("[WARN]") :].strip())
    elif normalized.startswith("[ERROR]"):
        log_failure(normalized[len("[ERROR]") :].strip())
    _ORIGINAL_PRINT(*args, **kwargs)


builtins.print = _tracking_print


def _parse_cli_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the migration workflow."""
    parser = argparse.ArgumentParser(
        description="Automate the staged-to-production VM migration workflow between vCenters."
    )
    parser.add_argument(
        "-s",
        "--source-vm",
        dest="source_vm_name",
        help="Name of the staged/source VM to clone (skips interactive prompt when provided).",
    )
    return parser.parse_args(argv)



def _print_execution_summary() -> None:
    """Display a summary of successes and failures recorded during execution."""
    print("\n==== \u5b9f\u884c\u30b5\u30de\u30ea ====")
    if SUCCESS_EVENTS:
        print("\u6210\u529f\u3057\u305f\u3053\u3068:")
        for entry in SUCCESS_EVENTS:
            print(f"  - {entry}")
    else:
        print("\u6210\u529f\u3057\u305f\u3053\u3068: \u306a\u3057")
    if FAILURE_EVENTS:
        print("\u5931\u6557\u3057\u305f\u3053\u3068:")
        for entry in FAILURE_EVENTS:
            print(f"  - {entry}")
    else:
        print("\u5931\u6557\u3057\u305f\u3053\u3068: \u306a\u3057")



def _load_local_module(module_name: str, filename: str):
    module_path = PROJECT_ROOT / filename
    if not module_path.exists():
        raise ModuleNotFoundError(module_name)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load specification for {module_name} at {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


try:
    from nic_schema import NicPlan, NIC_PLAN_VALIDATOR
except ModuleNotFoundError as import_error:
    try:
        nic_schema = _load_local_module("nic_schema", "nic_schema.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    NicPlan = nic_schema.NicPlan
    NIC_PLAN_VALIDATOR = nic_schema.NIC_PLAN_VALIDATOR


try:
    from route_selector import build_static_route_entries
except ModuleNotFoundError as import_error:
    try:
        route_selector = _load_local_module("route_selector", "route_selector.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    build_static_route_entries = route_selector.build_static_route_entries


from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl  # type: ignore[import]


def _resolve_virtual_nic_class(device_type_value: Any) -> Type[vim.vm.device.VirtualEthernetCard]:
    """Return a callable NIC device class, defaulting to VirtualVmxnet3 when unresolved."""
    candidate: Any = None
    if isinstance(device_type_value, str):
        candidate = getattr(vim.vm.device, device_type_value, None)
    elif isinstance(device_type_value, type):
        candidate = device_type_value
    elif device_type_value is not None:
        candidate = device_type_value
    if candidate and callable(candidate):
        return candidate
    LOGGER.debug(
        "Falling back to VirtualVmxnet3 for NIC device type %r (resolved=%r)",
        device_type_value,
        candidate,
    )
    return vim.vm.device.VirtualVmxnet3
try:
    import requests  # pylint: disable=unused-import
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning
    urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
try:
    from vsphere_sdk_network import (  # type: ignore[import]
        VsphereGuestNetworkSDK,
        IPv4Config,
        DnsConfig,
        RouteConfig,
    )
except ModuleNotFoundError as import_error:
    try:
        vsphere_sdk_network = _load_local_module("vsphere_sdk_network", "vsphere_sdk_network.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    VsphereGuestNetworkSDK = vsphere_sdk_network.VsphereGuestNetworkSDK
    IPv4Config = vsphere_sdk_network.IPv4Config
    DnsConfig = vsphere_sdk_network.DnsConfig
    RouteConfig = vsphere_sdk_network.RouteConfig

if TYPE_CHECKING:
    from vsphere_sdk_network import VsphereGuestNetworkSDK as VsphereGuestNetworkSDKType
else:
    VsphereGuestNetworkSDKType = VsphereGuestNetworkSDK


try:
    from guest_commands import (  # type: ignore[import]
        execute_command_in_guest,
        NmcliNotAvailableError,
        reset_root_login_disabled,
    )
except ModuleNotFoundError as import_error:
    try:
        guest_commands = _load_local_module("guest_commands", "guest_commands.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    execute_command_in_guest = guest_commands.execute_command_in_guest
    NmcliNotAvailableError = guest_commands.NmcliNotAvailableError
    reset_root_login_disabled = guest_commands.reset_root_login_disabled

try:
    from network_utils import (  # type: ignore[import]
        NMCLI_FIELDS_NO_TYPE,
        NMCLI_FIELDS_WITH_TYPE,
        SSH_ALLOWED_SOURCE_IP,
        LEGACY_INTERFACE_PATTERN,
        calculate_ip_stg_to_prd,
        compact_interface_name,
        collect_interface_inventory,
        configure_interface_without_nmcli,
        determine_prd_static_routes,
        derive_fallback_gateway,
        derive_gateway_from_octet_rule,
        ensure_connection_activation,
        ensure_firewall_allows_ssh,
        find_gateway_owner_index,
        infer_gateway_from_routes,
        make_nmcli_detail_fetcher,
        mask_to_prefix,
        parse_nmcli_connection_output,
        prefix_to_subnet_mask,
        select_default_gateway_route,
        verify_destination_network_with_sdk,
        verify_nmcli_connection_settings,
        extract_mac_from_sdk_interface,
        extract_ipv4_from_sdk_interface,
        extract_dns_servers_from_state,
        extract_routes_from_sdk_payload,
        transform_text_to_prd,
    )
except ModuleNotFoundError as import_error:
    try:
        network_utils = _load_local_module("network_utils", "network_utils.py")
    except Exception as load_error:  # pylint: disable=broad-exception-caught
        raise import_error from load_error
    NMCLI_FIELDS_NO_TYPE = network_utils.NMCLI_FIELDS_NO_TYPE
    NMCLI_FIELDS_WITH_TYPE = network_utils.NMCLI_FIELDS_WITH_TYPE
    SSH_ALLOWED_SOURCE_IP = network_utils.SSH_ALLOWED_SOURCE_IP
    LEGACY_INTERFACE_PATTERN = network_utils.LEGACY_INTERFACE_PATTERN
    calculate_ip_stg_to_prd = network_utils.calculate_ip_stg_to_prd
    compact_interface_name = network_utils.compact_interface_name
    collect_interface_inventory = network_utils.collect_interface_inventory
    configure_interface_without_nmcli = network_utils.configure_interface_without_nmcli
    determine_prd_static_routes = network_utils.determine_prd_static_routes
    derive_fallback_gateway = network_utils.derive_fallback_gateway
    derive_gateway_from_octet_rule = network_utils.derive_gateway_from_octet_rule
    ensure_connection_activation = network_utils.ensure_connection_activation
    ensure_firewall_allows_ssh = network_utils.ensure_firewall_allows_ssh
    find_gateway_owner_index = network_utils.find_gateway_owner_index
    infer_gateway_from_routes = network_utils.infer_gateway_from_routes
    make_nmcli_detail_fetcher = network_utils.make_nmcli_detail_fetcher
    mask_to_prefix = network_utils.mask_to_prefix
    parse_nmcli_connection_output = network_utils.parse_nmcli_connection_output
    prefix_to_subnet_mask = network_utils.prefix_to_subnet_mask
    select_default_gateway_route = network_utils.select_default_gateway_route
    verify_destination_network_with_sdk = network_utils.verify_destination_network_with_sdk
    verify_nmcli_connection_settings = network_utils.verify_nmcli_connection_settings
    extract_mac_from_sdk_interface = network_utils.extract_mac_from_sdk_interface
    extract_ipv4_from_sdk_interface = network_utils.extract_ipv4_from_sdk_interface
    extract_dns_servers_from_state = network_utils.extract_dns_servers_from_state
    extract_routes_from_sdk_payload = network_utils.extract_routes_from_sdk_payload
    transform_text_to_prd = network_utils.transform_text_to_prd


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

workflow_had_warnings = False

NTP_CONFIG_PATHS: Tuple[str, ...] = (
    "/etc/chrony.conf",
    "/etc/chrony/chrony.conf",
    "/etc/ntp.conf",
    "/etc/ntp/ntp.conf",
)
NTP_SERVER_DIRECTIVE_PATTERN = re.compile(r"^\s*(?:server|pool)\s+(\S+)", re.IGNORECASE | re.MULTILINE)
CENTOS_REPO_GLOB = "/etc/yum.repos.d/*.repo"
CENTOS_REPO_DIR = "/etc/yum.repos.d"
CENTOS_VAULT_BASE = "https://vault.centos.org/centos/"
MIRRORLIST_PATTERN = re.compile(r"^(\s*)mirrorlist\s*=\s*(\S.*)$", re.IGNORECASE)
BASEURL_PATTERN = re.compile(r"^(\s*)#?\s*baseurl\s*=\s*(\S.*)$", re.IGNORECASE)
TD_AGENT_REPO_PATH = "/etc/yum.repos.d/td.repo"
TD_AGENT_GPG_KEY = "https://packages.treasuredata.com/GPG-KEY-td-agent"
TD_AGENT_BASEURL_TEMPLATE = "https://packages.treasuredata.com/{major}/redhat/$releasever/$basearch"


def authenticate_vcenter(
    host: str,
    user: str,
    password: str,
    ssl_ctx: ssl.SSLContext,
    *,
    host_env_var: Optional[str] = None,
):
    """Establish a SmartConnect session to the specified vCenter and return the service instance."""
    try:
        session = SmartConnect(host=host, user=user, pwd=password, port=VCSA_PORT, sslContext=ssl_ctx)
    except socket.gaierror as error:
        hint = (
            f"Set environment variable {host_env_var} to a resolvable hostname or IP address."
            if host_env_var
            else "Ensure the hostname resolves on this machine."
        )
        raise ConnectionError(f"Unable to resolve vCenter host '{host}'. {hint}") from error
    if not session:
        raise ConnectionError(f"Failed to authenticate to vCenter ({host}).")
    return session


# ------------------------------------------------
# Connection and Migration Settings
# ------------------------------------------------
ENV_SOURCE_HOST_VAR = "VSPHERE_CLONE_SOURCE_HOST"
ENV_DEST_HOST_VAR = "VSPHERE_CLONE_DEST_HOST"

# --- Source vCenter ---
VCSA_HOST_SOURCE = _get_env_override(ENV_SOURCE_HOST_VAR, 'vcsa01s.ipet.local')
VCSA_USER = 'administrator@vsphere.local'
VCSA_PORT = 443

# --- Destination vCenter ---
VCSA_HOST_DEST = _get_env_override(ENV_DEST_HOST_VAR, 'vcsa01p.ipet.local')

# Log chosen hosts after variables are defined
if VCSA_HOST_SOURCE != 'vcsa01s.ipet.local':
    LOGGER.debug(
        "Using source vCenter host override from %s: %s",
        ENV_SOURCE_HOST_VAR,
        VCSA_HOST_SOURCE,
    )
if VCSA_HOST_DEST != 'vcsa01p.ipet.local':
    LOGGER.debug(
        "Using destination vCenter host override from %s: %s",
        ENV_DEST_HOST_VAR,
        VCSA_HOST_DEST,
    )

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
                LOGGER.debug(
                    "Keep-alive ping failed for %s; stopping keep-alive thread.",
                    label,
                    exc_info=True,
                )
                break
    thread = threading.Thread(target=_keepalive_loop, name=f"{label}-keepalive", daemon=True)
    thread.start()
    return thread, stop_event


def _stop_keepalive_thread(
    handle: Optional[Tuple[threading.Thread, threading.Event]],
    timeout: float = 5.0,
    max_additional_wait: float = 20.0,
) -> bool:
    """Signal the keep-alive thread to stop and wait for it to exit safely.

    Returns True when the thread confirmed it exited, False otherwise.
    """
    if not handle:
        return True
    thread, stop_event = handle
    stop_event.set()
    if timeout is None or timeout <= 0:
        timeout = 0.0
    try:
        # Wait once for the requested timeout
        thread.join(timeout)
        # If the thread is still alive, wait in small increments up to max_additional_wait.
        if thread.is_alive() and max_additional_wait > 0:
            deadline = time.time() + max_additional_wait
            while thread.is_alive() and time.time() < deadline:
                remaining = deadline - time.time()
                thread.join(min(1.0, max(0.0, remaining)))
    # pylint: disable-next=broad-exception-caught
    except Exception:
        LOGGER.debug("Error while joining keep-alive thread", exc_info=True)
    if thread.is_alive():
        LOGGER.warning("Keep-alive thread '%s' failed to terminate cleanly.", thread.name)
        return False
    return True


def wait_for_task_completion(
    task,
    description: str,
    *,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
    progress_label: Optional[str] = None,
    raise_on_error: bool = True,
) -> str:
    """Block until a vSphere task completes or the timeout is exceeded."""
    deadline = time.time() + max(1.0, timeout)
    last_reported_progress: Optional[int] = None
    label = (progress_label or "").rstrip()
    label_prefix = f"{label}: " if label else ""
    while True:
        info = getattr(task, "info", None)
        state = getattr(info, "state", None)
        if state in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
            break
        now = time.time()
        if now >= deadline:
            raise TimeoutError(f"{description} did not complete within {int(timeout)} seconds.")
        if label:
            current_progress = getattr(info, "progress", None)
            if isinstance(current_progress, int) and current_progress != last_reported_progress:
                print(f"   {label_prefix}{current_progress}%", end='\r')
                last_reported_progress = current_progress
        time.sleep(max(0.1, poll_interval))
    if label and last_reported_progress is not None:
        # Clear the progress line to keep the console tidy.
        print(" " * 60, end='\r')
    if raise_on_error and state != vim.TaskInfo.State.success:
        error = getattr(info, "error", None)
        error_message = getattr(error, "msg", None) or str(error) if error else "unknown error"
        raise RuntimeError(f"{description} failed: {error_message}")
    return state


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


def _collect_interface_lookup(
    interface_inventory: Iterable[Dict[str, Any]],
) -> Tuple[Set[str], Set[str], Dict[str, Any]]:
    """Return interface name candidates, compact names, and MAC lookup from guest inventory."""
    iface_name_candidates: Set[str] = set()
    iface_name_compact_candidates: Set[str] = set()
    iface_mac_lookup: Dict[str, Any] = {}
    for entry in interface_inventory:
        ifname = entry.get("ifname")
        if not ifname:
            continue
        lowered_ifname = ifname.lower()
        iface_name_candidates.add(lowered_ifname)
        iface_name_compact_candidates.add(compact_interface_name(ifname))
        mac_candidate = (entry.get("mac") or "").lower()
        if mac_candidate:
            iface_mac_lookup[mac_candidate] = entry
    return iface_name_candidates, iface_name_compact_candidates, iface_mac_lookup


def _resolve_guest_device_name(
    iface_mac_lookup: Mapping[str, Any],
    target_new_mac: Optional[str],
    original_mac: Optional[str],
) -> str:
    """Return the best candidate interface name based on new or original MAC addresses."""
    new_mac_lower = (target_new_mac or "").lower()
    if new_mac_lower:
        matching_entry = iface_mac_lookup.get(new_mac_lower)
        if matching_entry:
            candidate_ifname = matching_entry.get("ifname", "")
            if candidate_ifname:
                return str(candidate_ifname)
    original_mac_lower = (original_mac or "").lower()
    if original_mac_lower:
        matching_entry = iface_mac_lookup.get(original_mac_lower)
        if matching_entry:
            candidate_ifname = matching_entry.get("ifname", "")
            if candidate_ifname:
                return str(candidate_ifname)
    return ""


def _maybe_rename_guest_interface(
    guest_executor,
    device_name: str,
    desired_ifname: Optional[str],
    target_new_mac: Optional[str],
    nic_details: Mapping[str, Any],
    iface_name_candidates: Set[str],
    iface_name_compact_candidates: Set[str],
) -> str:
    """Attempt to rename the guest interface to the desired name when feasible."""
    desired_ifname_clean = (desired_ifname or "").strip()
    if not desired_ifname_clean or desired_ifname_clean.lower() == device_name.lower():
        return device_name
    desired_lower = desired_ifname_clean.lower()
    if desired_lower in iface_name_candidates:
        print(
            f"   [WARN] Intended interface name '{desired_ifname_clean}' already in use; keeping '{device_name}'."
        )
        return device_name
    previous_device_name = device_name
    print(f"   -> Renaming guest interface '{device_name}' to '{desired_ifname_clean}' to match source.")
    rename_failed = False
    down_exit, _, down_stderr = guest_executor(
        f"ip link set {device_name} down",
        check_exit_code=False,
    )
    if down_exit != 0:
        rename_failed = True
        print(
            f"   [WARN] Failed to bring interface '{device_name}' down before rename "
            f"(exit={down_exit}, stderr='{down_stderr or '(none)'}')."
        )
    else:
        rename_exit, _, rename_stderr = guest_executor(
            f"ip link set {device_name} name {desired_ifname_clean}",
            check_exit_code=False,
        )
        if rename_exit != 0:
            rename_failed = True
            print(
                f"   [WARN] Unable to rename '{device_name}' to '{desired_ifname_clean}' "
                f"(exit={rename_exit}, stderr='{rename_stderr or '(none)'}')."
            )
            guest_executor(
                f"ip link set {device_name} up",
                check_exit_code=False,
            )
    if rename_failed:
        print("   -> Proceeding with the runtime interface name reported by the guest.")
        return previous_device_name
    guest_executor(
        f"ip link set {desired_ifname_clean} up",
        check_exit_code=False,
    )
    mac_upper = (target_new_mac or nic_details.get('mac_address') or "").upper()
    udev_rule_update = (
        "if [ -f /etc/udev/rules.d/70-persistent-net.rules ]; then "
        f"sed -i '/ATTR{{address}}==\"{mac_upper}\"/Is/"
        f"NAME=\"[^\"]*\"/NAME=\"{desired_ifname_clean}\"/' "
        "/etc/udev/rules.d/70-persistent-net.rules; "
        "fi"
    )
    guest_executor(
        udev_rule_update,
        check_exit_code=False,
    )
    nm_update_script = (
        f"for cfg in /etc/sysconfig/network-scripts/ifcfg-{previous_device_name} "
        f"/etc/sysconfig/network-scripts/ifcfg-{desired_ifname_clean}; do "
        "if [ -f \"$cfg\" ]; then "
        f"sed -i 's/^DEVICE=.*/DEVICE=\"{desired_ifname_clean}\"/' \"$cfg\"; "
        "fi; "
        "done"
    )
    guest_executor(
        nm_update_script,
        check_exit_code=False,
    )
    guest_executor(
        (
            "if [ -d /etc/sysconfig/network-scripts ]; then "
            f"if [ -f /etc/sysconfig/network-scripts/ifcfg-{previous_device_name} ]; then "
            f"if [ ! -f /etc/sysconfig/network-scripts/ifcfg-{desired_ifname_clean} ]; then "
            f"mv /etc/sysconfig/network-scripts/ifcfg-{previous_device_name} "
            f"/etc/sysconfig/network-scripts/ifcfg-{desired_ifname_clean}; "
            "else "
            f"cp /etc/sysconfig/network-scripts/ifcfg-{previous_device_name} "
            f"/etc/sysconfig/network-scripts/ifcfg-{desired_ifname_clean}; "
            f"rm -f /etc/sysconfig/network-scripts/ifcfg-{previous_device_name}; "
            "fi; "
            "fi; "
            "fi"
        ),
        check_exit_code=False,
    )
    iface_name_candidates.discard(previous_device_name.lower())
    iface_name_compact_candidates.discard(compact_interface_name(previous_device_name))
    iface_name_candidates.add(desired_lower)
    iface_name_compact_candidates.add(compact_interface_name(desired_ifname_clean))
    print(f"   -> Interface rename completed; proceeding with '{desired_ifname_clean}'.")
    return desired_ifname_clean


def prepare_guest_interface(  # pylint: disable=redefined-outer-name,too-many-locals
    _nic_index: int,
    nic_details: Dict[str, Any],
    guest_executor,
    target_new_mac: Optional[str],
) -> "GuestInterfaceContext":
    """
    Collect guest interface metadata for the specified NIC and perform any required renames.

    Returns a GuestInterfaceContext holding the resolved device name and interface inventories.
    """
    interface_inventory = collect_interface_inventory(guest_executor)
    (
        iface_name_candidates,
        iface_name_compact_candidates,
        iface_mac_lookup,
    ) = _collect_interface_lookup(interface_inventory)
    original_mac = nic_details.get('mac_address')
    device_name = _resolve_guest_device_name(
        iface_mac_lookup,
        target_new_mac,
        original_mac,
    )
    new_mac_lower = (target_new_mac or "").lower()
    original_mac_lower = (original_mac or "").lower()

    if not device_name:
        target_mac = target_new_mac or nic_details.get('mac_address') or '?'
        raise RuntimeError(f"Unable to locate guest interface matching MAC {target_mac}")

    LOGGER.debug(
        "Interface match: ifname=%s, new_mac=%s, original_mac=%s",
        device_name,
        target_new_mac,
        nic_details.get('mac_address'),
    )

    desired_ifname = nic_details.get('original_ifname')
    device_name = _maybe_rename_guest_interface(
        guest_executor,
        device_name,
        desired_ifname,
        target_new_mac,
        nic_details,
        iface_name_candidates,
        iface_name_compact_candidates,
    )

    return GuestInterfaceContext(
        device_name=device_name,
        interface_names=iface_name_candidates,
        interface_names_compact=iface_name_compact_candidates,
        mac_lookup=iface_mac_lookup,
        new_mac_lower=new_mac_lower,
        original_mac_lower=original_mac_lower,
    )


def _is_service_active(guest_executor, service_name: str) -> bool:
    """Return True if the specified service is active on the guest."""
    service_quoted = shlex.quote(service_name)
    exit_code, _, _ = guest_executor("command -v systemctl", check_exit_code=False)
    if exit_code == 0:
        exit_code, stdout, _ = guest_executor(f"systemctl is-active {service_quoted}", check_exit_code=False)
        if exit_code == 0 and (stdout or "").strip() == "active":
            return True
    exit_code, _, _ = guest_executor(f"service {service_quoted} status >/dev/null 2>&1", check_exit_code=False)
    return exit_code == 0


def _ensure_guest_write_privileges(guest_executor) -> bool:
    """Verify that the guest executor runs with root privileges."""
    exit_code, stdout, stderr = guest_executor("id -u", check_exit_code=False)
    if exit_code == 0 and (stdout or "").strip() == "0":
        return True
    probe_exit, _, probe_err = guest_executor("test -w /etc && printf ok", check_exit_code=False)
    if probe_exit == 0:
        return True
    detail = (stderr or "").strip() or (probe_err or "").strip() or f"id -u exit code {exit_code}"
    print(f"   [ERROR] Guest executor lacks root access to modify system configuration: {detail}")
    log_failure("Guest credentials do not provide root privileges; system configuration sync skipped.")
    return False


def _write_guest_file(guest_executor, remote_path: str, content: str) -> Tuple[int, str, str]:
    """Write content to a file inside the guest using a temporary file and atomic move."""
    token = f"__VSPHERE_EOF_{uuid_module.uuid4().hex}__"
    normalized = content.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    command = (
        "set -euo pipefail\n"
        f"remote_path={shlex.quote(remote_path)}\n"
        "remote_dir=$(dirname \"$remote_path\")\n"
        "if [ -z \"$remote_dir\" ]; then remote_dir='.'; fi\n"
        "if [ ! -d \"$remote_dir\" ]; then\n"
        "  echo \"E_NO_DIRECTORY:$remote_dir\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "tmpfile=$(mktemp \"$remote_dir/.vsphere_tmp.XXXXXX\" 2>/dev/null || mktemp /tmp/.vsphere_tmp.XXXXXX)\n"
        "if [ -z \"$tmpfile\" ]; then\n"
        "  echo \"E_MKTEMP_FAILED\" >&2\n"
        "  exit 1\n"
        "fi\n"
        f"if ! cat <<'{token}' > \"$tmpfile\"\n"
        f"{normalized}"
        f"{token}\n"
        "then\n"
        "  echo \"E_WRITE_TEMPFILE\" >&2\n"
        "  rm -f \"$tmpfile\"\n"
        "  exit 1\n"
        "fi\n"
        "perm_spec=644\n"
        "owner_spec=$(id -u)\n"
        "group_spec=$(id -g)\n"
        "if [ -e \"$remote_path\" ]; then\n"
        "  perm_spec=$(stat -c '%a' \"$remote_path\" 2>/dev/null || echo 644)\n"
        "  owner_spec=$(stat -c '%u' \"$remote_path\" 2>/dev/null || echo $(id -u))\n"
        "  group_spec=$(stat -c '%g' \"$remote_path\" 2>/dev/null || echo $(id -g))\n"
        "fi\n"
        "chmod \"$perm_spec\" \"$tmpfile\" 2>/dev/null || true\n"
        "chown \"$owner_spec\":\"$group_spec\" \"$tmpfile\" 2>/dev/null || true\n"
        "if ! mv \"$tmpfile\" \"$remote_path\" 2>/dev/null; then\n"
        "  err=$?\n"
        "  echo \"E_MOVE_FAILED:$remote_path\" >&2\n"
        "  rm -f \"$tmpfile\"\n"
        "  exit $err\n"
        "fi\n"
        "exit 0\n"
    )
    return guest_executor(command, check_exit_code=False)


def _extract_ntp_server_tokens(config_text: str) -> List[str]:
    """Return the list of server/pool targets from an NTP configuration."""
    tokens: List[str] = []
    for match in NTP_SERVER_DIRECTIVE_PATTERN.finditer(config_text or ""):
        token = match.group(1)
        if token:
            tokens.append(token.strip())
    return tokens


def _identify_stage_ntp_servers(tokens: Iterable[str]) -> List[str]:
    """Return NTP servers that still reside in the STG (170-179 third octet) address space."""
    stage_servers: List[str] = []
    for token in tokens:
        try:
            ipv4 = ipaddress.IPv4Address(token)
        except (ValueError, ipaddress.AddressValueError):
            continue
        third_octet = int(str(ipv4).split(".")[2])
        if 170 <= third_octet <= 179:
            stage_servers.append(str(ipv4))
    return stage_servers


def _rewrite_centos_repo_content(repo_content: str) -> Tuple[str, bool]:
    """Rewrite CentOS repo definitions to point to vault.centos.org."""
    lines = repo_content.splitlines()
    updated_lines: List[str] = []
    changed = False

    for line in lines:
        mirror_match = MIRRORLIST_PATTERN.match(line)
        if mirror_match:
            indent, definition = mirror_match.groups()
            disabled_line = f"{indent}# mirrorlist={definition} (disabled: vault.centos.org)"
            if disabled_line != line:
                updated_lines.append(disabled_line)
                changed = True
            else:
                updated_lines.append(line)
            continue
        base_match = BASEURL_PATTERN.match(line)
        if base_match:
            indent, url_definition = base_match.groups()
            lower_def = url_definition.lower()
            if "centos" in lower_def:
                new_definition = re.sub(
                    r"^https?://[^/]*centos\.org/centos/",
                    CENTOS_VAULT_BASE,
                    url_definition,
                    flags=re.IGNORECASE,
                )
                if not new_definition.lower().startswith(CENTOS_VAULT_BASE):
                    if not new_definition.startswith("$"):
                        new_definition = f"{CENTOS_VAULT_BASE}{new_definition.lstrip('/')}"
                new_line = f"{indent}baseurl={new_definition}"
                if new_line != line:
                    updated_lines.append(new_line)
                    changed = True
                else:
                    updated_lines.append(line)
                continue
        updated_lines.append(line)

    updated_content = "\n".join(updated_lines)
    transformed_content, ip_changed = transform_text_to_prd(updated_content)
    if ip_changed:
        updated_content = transformed_content
        changed = True
    if repo_content.endswith("\n"):
        updated_content += "\n"
    elif updated_content and not updated_content.endswith("\n"):
        updated_content += "\n"
    return updated_content, changed


def _is_tls_error(exit_code: int, stderr_text: Optional[str]) -> bool:
    """Return True if the curl exit code / stderr indicates a TLS handshake problem."""
    if exit_code in (35, 51, 52, 56, 58, 60, 77, 83):
        return True
    stderr_normalized = (stderr_text or "").lower()
    if not stderr_normalized:
        return False
    tls_markers = (
        "ssl",
        "tls",
        "certificate",
        "handshake",
        "unknown ca",
        "expired",
        "verify",
        "alert",
    )
    return any(marker in stderr_normalized for marker in tls_markers)


def _refresh_tls_trust_bundles(guest_executor) -> None:
    """Attempt to refresh CA trust stores and related packages."""
    commands = [
        "command -v update-ca-trust >/dev/null 2>&1 && update-ca-trust force-enable >/dev/null 2>&1",
        "command -v update-ca-trust >/dev/null 2>&1 && update-ca-trust extract >/dev/null 2>&1",
        (
            "command -v yum >/dev/null 2>&1 && "
            "yum -y reinstall ca-certificates nss curl >/dev/null 2>&1 || true"
        ),
        (
            "command -v yum >/dev/null 2>&1 && "
            "yum -y install ca-certificates --disablerepo='*' --enablerepo='base,updates,extras' >/dev/null 2>&1 || true"
        ),
    ]
    for repair_cmd in commands:
        guest_executor(repair_cmd, check_exit_code=False)


def _attempt_curl_openssl_upgrade(guest_executor) -> None:
    """Try to install an OpenSSL-enabled curl as a last resort."""
    commands = [
        (
            "command -v yum >/dev/null 2>&1 && "
            "yum -y install curl-openssl >/dev/null 2>&1 || true"
        ),
        (
            "command -v yum >/dev/null 2>&1 && "
            "yum -y reinstall curl --disablerepo='*' --enablerepo='base,updates,extras' "
            "--setopt=tsflags=reinstall >/dev/null 2>&1 || true"
        ),
    ]
    for repair_cmd in commands:
        guest_executor(repair_cmd, check_exit_code=False)


def _run_curl_with_tls_repairs(
    guest_executor,
    url: str,
    *,
    extra_args: Optional[str] = None,
    max_attempts: int = 3,
) -> Tuple[bool, Optional[str]]:
    """Run curl with basic TLS repair attempts for legacy environments."""
    attempted_ca_refresh = False
    attempted_openssl_upgrade = False
    args = extra_args or "--fail --location --head"
    last_error: Optional[str] = None
    for _ in range(max_attempts):
        curl_cmd = (
            f"curl -Is --max-time 10 --retry 2 --retry-delay 2 {args} {shlex.quote(url)} >/dev/null"
        )
        exit_code, _, stderr_text = guest_executor(curl_cmd, check_exit_code=False)
        if exit_code == 0:
            return True, None
        last_error = (stderr_text or "").strip() or f"curl exit status {exit_code}"
        if _is_tls_error(exit_code, stderr_text):
            if not attempted_ca_refresh:
                _refresh_tls_trust_bundles(guest_executor)
                attempted_ca_refresh = True
                continue
            if not attempted_openssl_upgrade:
                _attempt_curl_openssl_upgrade(guest_executor)
                attempted_openssl_upgrade = True
                continue
        break
    return False, last_error


def _sync_hosts_file_to_prd(guest_executor, timestamp: str) -> bool:
    """Update /etc/hosts to PRD equivalents."""
    exit_code, hosts_content, hosts_err = guest_executor("cat /etc/hosts", check_exit_code=False)
    if exit_code != 0:
        print(f"   [WARN] Unable to read /etc/hosts: {(hosts_err or '').strip() or exit_code}")
        return False
    updated_hosts = hosts_content
    changed = False
    for _ in range(3):
        updated_hosts, iteration_changed = transform_text_to_prd(updated_hosts)
        if iteration_changed:
            changed = True
        else:
            break
    if not changed:
        indicators = []
        if "ipet-ins" in hosts_content:
            indicators.append("stage domain tokens (ipet-ins)")
        if " line-" in hosts_content and re.search(r"\bline-[^ \t\n]*s\b", hosts_content):
            indicators.append("hostnames ending with 's'")
        if re.search(r"\b172\.16\.17[0-9]\.", hosts_content):
            indicators.append("stage IP ranges (172.16.17x.x)")
        if indicators:
            print(f"   [WARN] Detected residual STG markers in /etc/hosts ({', '.join(indicators)}).")
        else:
            print("   -> /etc/hosts already aligned with PRD entries.")
        return not indicators
    backup_path = f"/etc/hosts-{timestamp}.bak"
    backup_cmd = (
        f"[ -f {shlex.quote(backup_path)} ] || "
        f"cp /etc/hosts {shlex.quote(backup_path)}"
    )
    backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
    if backup_exit != 0:
        print(f"   [WARN] Unable to create /etc/hosts backup: {(backup_err or '').strip() or backup_exit}")
        return False
    write_exit, _, write_err = _write_guest_file(guest_executor, "/etc/hosts", updated_hosts)
    if write_exit != 0:
        print(f"   [WARN] Failed to update /etc/hosts: {(write_err or '').strip() or write_exit}")
        return False
    print(f"   -> Updated /etc/hosts for PRD environment (backup: {backup_path}).")
    return True


def _sync_firewalld_configuration_to_prd(
    guest_executor,
    timestamp: str,
    source_zone_interfaces: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """Update firewalld configuration to PRD equivalents when the service is active."""
    exit_code, _, _ = guest_executor("command -v firewall-cmd", check_exit_code=False)
    if exit_code != 0:
        print("   -> firewall-cmd not available; skipping firewalld configuration sync.")
        return True
    if not _is_service_active(guest_executor, "firewalld"):
        print("   -> firewalld inactive; skipping firewalld configuration sync.")
        return True
    zone_names: Set[str] = set()
    exit_code, zones_output, _ = guest_executor("firewall-cmd --get-zones", check_exit_code=False)
    if exit_code == 0 and zones_output:
        for token in zones_output.split():
            token_clean = token.strip()
            if token_clean:
                zone_names.add(token_clean)
    if not zone_names:
        exit_code, dir_listing, dir_err = guest_executor("ls /etc/firewalld/zones", check_exit_code=False)
        if exit_code == 0 and dir_listing:
            for line in dir_listing.splitlines():
                name = line.strip()
                if name.endswith(".xml"):
                    zone_names.add(name[:-4])
        else:
            print(f"   [WARN] Unable to enumerate firewalld zones: {(dir_err or dir_listing or '').strip() or exit_code}")
            return False
    overall_success = True
    zones_updated: List[str] = []
    zone_interface_overrides = source_zone_interfaces or {}

    for zone in sorted(zone_names):
        zone_file = f"/etc/firewalld/zones/{zone}.xml"
        backup_path = f"{zone_file}-{timestamp}.bak"
        backup_created = False

        def _ensure_zone_backup(
            backup_path: str = backup_path,
            zone_file_path: str = zone_file,
        ) -> bool:
            nonlocal backup_created, overall_success
            if backup_created:
                return True
            backup_cmd = (
                f"[ -f {shlex.quote(backup_path)} ] || "
                f"cp {shlex.quote(zone_file_path)} {shlex.quote(backup_path)}"
            )
            backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
            if backup_exit != 0:
                print(
                    f"   [WARN] Unable to back up firewalld zone file '{zone_file}': "
                    f"{(backup_err or '').strip() or backup_exit}"
                )
                overall_success = False
                return False
            backup_created = True
            return True

        zone_changed = False

        interfaces_exit, interfaces_stdout, interfaces_err = guest_executor(
            f"firewall-cmd --permanent --zone={shlex.quote(zone)} --list-interfaces",
            check_exit_code=False,
        )
        current_interfaces: List[str] = []
        if interfaces_exit == 0:
            current_interfaces = [
                entry.strip()
                for entry in (interfaces_stdout or "").split()
                if entry.strip()
            ]
        elif interfaces_err:
            LOGGER.debug("Failed to list firewalld interfaces for zone '%s': %s", zone, interfaces_err.strip())
        desired_interfaces = None
        if zone_interface_overrides:
            desired_interfaces = zone_interface_overrides.get(zone)
        current_interface_set: Set[str] = {iface for iface in current_interfaces if iface}
        if desired_interfaces is not None:
            desired_clean = [
                iface.strip()
                for iface in desired_interfaces
                if iface and iface.strip()
            ]
            desired_set: Set[str] = set(desired_clean)
            if desired_set != current_interface_set:
                if _ensure_zone_backup():
                    extras = sorted(current_interface_set - desired_set)
                    for interface_name in extras:
                        remove_exit, _, remove_err = guest_executor(
                            f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                            f"--remove-interface={shlex.quote(interface_name)}",
                            check_exit_code=False,
                        )
                        if remove_exit != 0:
                            LOGGER.debug(
                                "Failed to detach interface '%s' from zone '%s': %s",
                                interface_name,
                                zone,
                                (remove_err or "").strip() or remove_exit,
                            )
                            overall_success = False
                        else:
                            current_interface_set.discard(interface_name)
                            zone_changed = True
                    missing_interfaces = [
                        iface for iface in desired_clean if iface not in current_interface_set
                    ]
                    for interface_name in missing_interfaces:
                        add_exit, _, add_err = guest_executor(
                            f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                            f"--add-interface={shlex.quote(interface_name)}",
                            check_exit_code=False,
                        )
                        if add_exit != 0:
                            LOGGER.debug(
                                "Failed to attach interface '%s' to zone '%s': %s",
                                interface_name,
                                zone,
                                (add_err or "").strip() or add_exit,
                            )
                            overall_success = False
                        else:
                            current_interface_set.add(interface_name)
                            zone_changed = True
                else:
                    overall_success = False
            current_interfaces = list(current_interface_set)
        else:
            for interface_name in current_interfaces:
                if not interface_name:
                    continue
                query_iface_exit, _, _ = guest_executor(
                    f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                    f"--query-interface={shlex.quote(interface_name)}",
                    check_exit_code=False,
                )
                if query_iface_exit != 0:
                    if _ensure_zone_backup():
                        add_iface_exit, _, add_iface_err = guest_executor(
                            f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                            f"--add-interface={shlex.quote(interface_name)}",
                            check_exit_code=False,
                        )
                        if add_iface_exit == 0:
                            zone_changed = True
                        else:
                            LOGGER.debug(
                                "Failed to reattach interface '%s' to zone '%s': %s",
                                interface_name,
                                zone,
                                (add_iface_err or "").strip() or add_iface_exit,
                            )
                            overall_success = False

        # Synchronise sources
        sources_exit, sources_stdout, sources_err = guest_executor(
            f"firewall-cmd --permanent --zone={shlex.quote(zone)} --list-sources",
            check_exit_code=False,
        )
        if sources_exit != 0:
            if sources_err:
                LOGGER.debug("Failed to list firewalld sources for zone '%s': %s", zone, sources_err.strip())
        else:
            sources = [entry.strip() for entry in (sources_stdout or "").split() if entry.strip()]
            for source_entry in sources:
                transformed_source, transformed_changed = transform_text_to_prd(source_entry)
                transformed_source = transformed_source.strip()
                if not transformed_changed or not transformed_source or transformed_source == source_entry:
                    continue
                if not _ensure_zone_backup():
                    break
                remove_cmd = (
                    f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                    f"--remove-source={shlex.quote(source_entry)}"
                )
                remove_exit, _, remove_err = guest_executor(remove_cmd, check_exit_code=False)
                if remove_exit != 0:
                    LOGGER.debug(
                        "Failed to remove firewalld source '%s' from zone '%s': %s",
                        source_entry,
                        zone,
                        (remove_err or "").strip() or remove_exit,
                    )
                    overall_success = False
                    continue
                query_cmd = (
                    f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                    f"--query-source={shlex.quote(transformed_source)}"
                )
                query_exit, _, _ = guest_executor(query_cmd, check_exit_code=False)
                if query_exit != 0:
                    add_cmd = (
                        f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                        f"--add-source={shlex.quote(transformed_source)}"
                    )
                    add_exit, _, add_err = guest_executor(add_cmd, check_exit_code=False)
                    if add_exit != 0:
                        LOGGER.debug(
                            "Failed to add firewalld source '%s' to zone '%s': %s",
                            transformed_source,
                            zone,
                            (add_err or "").strip() or add_exit,
                        )
                        overall_success = False
                        continue
                zone_changed = True

        # Synchronise rich rules
        list_exit, list_stdout, list_err = guest_executor(
            f"firewall-cmd --permanent --zone={shlex.quote(zone)} --list-rich-rules",
            check_exit_code=False,
        )
        if list_exit != 0:
            if list_err:
                LOGGER.debug("Failed to list firewalld rich rules for zone '%s': %s", zone, list_err.strip())
            continue
        rich_rules = [rule.strip() for rule in (list_stdout or "").splitlines() if rule.strip()]
        for rule in rich_rules:
            transformed_rule, changed = transform_text_to_prd(rule)
            transformed_rule = transformed_rule.strip()
            if not changed or not transformed_rule or transformed_rule == rule:
                continue
            if not _ensure_zone_backup():
                break
            remove_cmd = (
                f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                f"--remove-rich-rule={shlex.quote(rule)}"
            )
            remove_exit, _, remove_err = guest_executor(remove_cmd, check_exit_code=False)
            if remove_exit != 0:
                LOGGER.debug(
                    "Failed to remove firewalld rich rule from zone '%s': %s",
                    zone,
                    (remove_err or "").strip() or remove_exit,
                )
                overall_success = False
                continue
            query_cmd = (
                f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                f"--query-rich-rule={shlex.quote(transformed_rule)}"
            )
            query_exit, _, _ = guest_executor(query_cmd, check_exit_code=False)
            if query_exit != 0:
                add_cmd = (
                    f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                    f"--add-rich-rule={shlex.quote(transformed_rule)}"
                )
                add_exit, _, add_err = guest_executor(add_cmd, check_exit_code=False)
                if add_exit != 0:
                    LOGGER.debug(
                        "Failed to add firewalld rich rule to zone '%s': %s",
                        zone,
                        (add_err or "").strip() or add_exit,
                    )
                    overall_success = False
                    continue
            zone_changed = True

        if zone.lower() in {"heartbeat"}:
            restricted_rule = f"rule family=\"ipv4\" source address=\"{SSH_ALLOWED_SOURCE_IP}\" service name=\"ssh\" accept"
            query_cmd = (
                f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                f"--query-rich-rule={shlex.quote(restricted_rule)}"
            )
            query_exit, _, _ = guest_executor(query_cmd, check_exit_code=False)
            if query_exit == 0:
                if _ensure_zone_backup():
                    remove_cmd = (
                        f"firewall-cmd --permanent --zone={shlex.quote(zone)} "
                        f"--remove-rich-rule={shlex.quote(restricted_rule)}"
                    )
                    remove_exit, _, remove_err = guest_executor(remove_cmd, check_exit_code=False)
                    if remove_exit != 0:
                        LOGGER.debug(
                            "Failed to remove SSH rich rule from restricted zone '%s': %s",
                            zone,
                            (remove_err or "").strip() or remove_exit,
                        )
                        overall_success = False
                    else:
                        zone_changed = True
                        if zone not in zones_updated:
                            zones_updated.append(zone)
        if zone_changed and zone not in zones_updated:
            zones_updated.append(zone)

    if zones_updated:
        reload_exit, _, reload_err = guest_executor("firewall-cmd --reload", check_exit_code=False)
        if reload_exit != 0:
            print(
                f"   [WARN] Firewalld reload failed after configuration updates: "
                f"{(reload_err or '').strip() or reload_exit}"
            )
            overall_success = False
        else:
            print(f"   -> Firewalld zones updated: {', '.join(sorted(zones_updated))}")
    else:
        print("   -> Firewalld zones already aligned with PRD entries.")
    return overall_success


def _sync_ntp_configuration_to_prd(guest_executor, timestamp: str) -> bool:
    """Update chrony/ntp configuration so NTP servers align with PRD DNS mappings."""
    success = True
    found_config = False
    for config_path in NTP_CONFIG_PATHS:
        exists_exit, _, _ = guest_executor(
            f"test -f {shlex.quote(config_path)}",
            check_exit_code=False,
        )
        if exists_exit != 0:
            continue
        exit_code, config_content, config_err = guest_executor(
            f"cat {shlex.quote(config_path)}",
            check_exit_code=False,
        )
        if exit_code != 0:
            print(f"   [WARN] Unable to read {config_path}: {(config_err or '').strip() or exit_code}")
            success = False
            break
        found_config = True
        server_tokens_before = _extract_ntp_server_tokens(config_content)
        stage_servers_before = _identify_stage_ntp_servers(server_tokens_before)
        expected_conversions: Dict[str, str] = {}
        conversion_anomalies: List[str] = []
        for server in stage_servers_before:
            converted = calculate_ip_stg_to_prd(server)
            if not converted or converted == server:
                conversion_anomalies.append(server)
            else:
                expected_conversions[server] = converted
        updated_config = config_content
        changed = False
        for _ in range(3):
            updated_config, iteration_changed = transform_text_to_prd(updated_config)
            if iteration_changed:
                changed = True
            else:
                break
        server_tokens_after = _extract_ntp_server_tokens(updated_config)
        stage_servers_after = _identify_stage_ntp_servers(server_tokens_after)
        if stage_servers_after and expected_conversions:
            for stage_ip in stage_servers_after:
                replacement = expected_conversions.get(stage_ip) or calculate_ip_stg_to_prd(stage_ip)
                if replacement and replacement != stage_ip:
                    pattern = re.compile(rf"\b{re.escape(stage_ip)}\b")
                    updated_config, count = pattern.subn(replacement, updated_config)
                    if count > 0:
                        changed = True
            server_tokens_after = _extract_ntp_server_tokens(updated_config)
            stage_servers_after = _identify_stage_ntp_servers(server_tokens_after)
        conversion_failures = [
            f"{original} expected {expected}"
            for original, expected in expected_conversions.items()
            if expected not in server_tokens_after
        ]
        if conversion_anomalies:
            print(
                f"   [WARN] {config_path} contains NTP addresses that cannot be converted automatically: "
                f"{', '.join(conversion_anomalies)}"
            )
            success = False
        if stage_servers_after:
            print(
                f"   [WARN] {config_path} still references STG NTP servers after transformation: "
                f"{', '.join(stage_servers_after)}"
            )
            success = False
        if conversion_failures:
            print(
                f"   [WARN] Failed to confirm NTP server updates in {config_path}: "
                f"{', '.join(conversion_failures)}"
            )
            success = False
        if not changed:
            if not stage_servers_after and not conversion_failures and not conversion_anomalies:
                print(f"   -> {config_path} already aligned with PRD entries.")
            break
        backup_path = f"{config_path}-{timestamp}.bak"
        backup_cmd = (
            f"[ -f {shlex.quote(backup_path)} ] || "
            f"cp {shlex.quote(config_path)} {shlex.quote(backup_path)}"
        )
        backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
        if backup_exit != 0:
            print(f"   [WARN] Unable to back up {config_path}: {(backup_err or '').strip() or backup_exit}")
            success = False
            break
        write_exit, _, write_err = _write_guest_file(guest_executor, config_path, updated_config)
        if write_exit != 0:
            print(f"   [WARN] Failed to update {config_path}: {(write_err or '').strip() or write_exit}")
            success = False
            break
        print(f"   -> Updated {config_path} NTP servers for PRD environment (backup: {backup_path}).")
        break
    if not found_config:
        print("   -> No chrony/ntp configuration found; skipping NTP sync.")
    return success


def _sync_centos_repo_configuration(guest_executor, timestamp: str) -> bool:
    """Rewrite CentOS repo files to use vault.centos.org."""
    repo_dir_check, _, _ = guest_executor(f"test -d {shlex.quote(CENTOS_REPO_DIR)}", check_exit_code=False)
    if repo_dir_check != 0:
        print("   -> /etc/yum.repos.d not present; skipping CentOS repo sync.")
        return True
    curl_ok, curl_error = _run_curl_with_tls_repairs(
        guest_executor,
        "https://vault.centos.org/centos/",
    )
    if not curl_ok:
        print(f"   [WARN] Unable to reach vault.centos.org even after TLS repairs: {curl_error or 'no details'}")
    list_cmd = f"ls -1 {CENTOS_REPO_GLOB} 2>/dev/null"
    ls_exit, ls_stdout, ls_err = guest_executor(list_cmd, check_exit_code=False)
    if ls_exit != 0 or not ls_stdout.strip():
        if ls_exit == 0:
            print("   -> No CentOS repo files detected; skipping repo sync.")
            return True
        detail = (ls_err or "").strip() or ls_exit
        print(f"   [WARN] Unable to enumerate CentOS repo files: {detail}")
        return False
    success = True
    for repo_path in ls_stdout.splitlines():
        repo_path = repo_path.strip()
        if not repo_path:
            continue
        exit_code, repo_content, repo_err = guest_executor(f"cat {shlex.quote(repo_path)}", check_exit_code=False)
        if exit_code != 0:
            print(f"   [WARN] Unable to read {repo_path}: {(repo_err or '').strip() or exit_code}")
            success = False
            continue
        updated_content, changed = _rewrite_centos_repo_content(repo_content)
        if not changed:
            continue
        backup_path = f"{repo_path}-{timestamp}.bak"
        backup_cmd = (
            f"[ -f {shlex.quote(backup_path)} ] || "
            f"cp {shlex.quote(repo_path)} {shlex.quote(backup_path)}"
        )
        backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
        if backup_exit != 0:
            print(f"   [WARN] Unable to back up {repo_path}: {(backup_err or '').strip() or backup_exit}")
            success = False
            continue
        write_exit, _, write_err = _write_guest_file(guest_executor, repo_path, updated_content)
        if write_exit != 0:
            print(f"   [WARN] Failed to update {repo_path}: {(write_err or '').strip() or write_exit}")
            success = False
            continue
        print(f"   -> Updated CentOS repo file '{repo_path}' to use vault.centos.org (backup: {backup_path}).")
    return success


def _ensure_td_agent_repo(guest_executor, timestamp: str) -> bool:
    """Ensure td-agent repo definition exists and points to a working major version."""
    guest_executor(f"mkdir -p {shlex.quote(CENTOS_REPO_DIR)}", check_exit_code=False)
    release_macros = ("%centos_ver", "%rhel")
    release_ver = "7"
    for macro in release_macros:
        exit_code, stdout, _ = guest_executor(f"rpm -E {macro}", check_exit_code=False)
        value = (stdout or "").strip()
        if exit_code == 0 and value and not value.startswith("%"):
            release_ver = value
            break
    arch_exit, arch_stdout, _ = guest_executor("rpm -E %_arch", check_exit_code=False)
    base_arch = (arch_stdout or "x86_64").strip() or "x86_64"

    def _test_major(major: int) -> bool:
        evaluated_url = f"https://packages.treasuredata.com/{major}/redhat/{release_ver}/{base_arch}/"
        ok, _ = _run_curl_with_tls_repairs(
            guest_executor,
            evaluated_url,
        )
        return ok

    selected_major = 4
    if not _test_major(selected_major):
        print("   [WARN] td-agent v4 repository unreachable; falling back to v3.")
        selected_major = 3
        if not _test_major(selected_major):
            print("   [WARN] td-agent v3 repository check failed as well; proceeding with v3 definition.")

    repo_content = (
        "[treasuredata]\n"
        "name=TreasureData\n"
        f"baseurl={TD_AGENT_BASEURL_TEMPLATE.format(major=selected_major)}\n"
        "gpgcheck=1\n"
        f"gpgkey={TD_AGENT_GPG_KEY}\n"
    )
    backup_path = f"{TD_AGENT_REPO_PATH}-{timestamp}.bak"
    backup_cmd = (
        f"if [ -f {shlex.quote(TD_AGENT_REPO_PATH)} ]; then "
        f"[ -f {shlex.quote(backup_path)} ] || "
        f"cp {shlex.quote(TD_AGENT_REPO_PATH)} {shlex.quote(backup_path)}; "
        "fi"
    )
    guest_executor(backup_cmd, check_exit_code=False)
    write_exit, _, write_err = _write_guest_file(guest_executor, TD_AGENT_REPO_PATH, repo_content)
    if write_exit != 0:
        print(f"   [WARN] Failed to write td-agent repo file: {(write_err or '').strip() or write_exit}")
        return False
    print(f"   -> td-agent repository configured for major v{selected_major} (path: {TD_AGENT_REPO_PATH}).")
    return True


def _sync_iptables_configuration_to_prd(guest_executor, timestamp: str) -> bool:
    """Update /etc/sysconfig/iptables to PRD equivalents when the iptables service is active."""
    if not _is_service_active(guest_executor, "iptables"):
        print("   -> iptables service inactive; skipping iptables configuration sync.")
        return True
    config_path = "/etc/sysconfig/iptables"
    exit_code, config_content, config_err = guest_executor(f"cat {shlex.quote(config_path)}", check_exit_code=False)
    if exit_code != 0:
        print(f"   [WARN] Unable to read iptables configuration '{config_path}': {(config_err or '').strip() or exit_code}")
        return False
    updated_config, changed = transform_text_to_prd(config_content)
    if not changed:
        print(f"   -> {config_path} already aligned with PRD entries.")
        return True
    backup_path = f"{config_path}-{timestamp}.bak"
    backup_cmd = (
        f"[ -f {shlex.quote(backup_path)} ] || "
        f"cp {shlex.quote(config_path)} {shlex.quote(backup_path)}"
    )
    backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
    if backup_exit != 0:
        print(f"   [WARN] Unable to back up iptables configuration '{config_path}': {(backup_err or '').strip() or backup_exit}")
        return False
    write_exit, _, write_err = _write_guest_file(guest_executor, config_path, updated_config)
    if write_exit != 0:
        print(f"   [WARN] Failed to update iptables configuration '{config_path}': {(write_err or '').strip() or write_exit}")
        return False
    reload_exit, _, reload_err = guest_executor("systemctl reload iptables", check_exit_code=False)
    if reload_exit != 0:
        reload_exit, _, reload_err = guest_executor("service iptables reload >/dev/null 2>&1", check_exit_code=False)
    if reload_exit != 0:
        print(f"   [WARN] Unable to reload iptables after configuration update: {(reload_err or '').strip() or reload_exit}")
        return False
    print(f"   -> Updated iptables configuration for PRD environment (backup: {backup_path}).")
    return True


def _sync_prd_system_configuration(
    guest_executor,
    source_zone_interfaces: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """Synchronise hosts and firewall configurations inside the guest with PRD expectations."""
    print("   -> Synchronising guest hosts and firewall configuration with PRD mappings...")
    timestamp = datetime.now().strftime("%Y%m%d")
    success = True
    if not _ensure_guest_write_privileges(guest_executor):
        return False
    if not _ensure_http_proxy_configuration(guest_executor, timestamp):
        success = False
    if not _sync_hosts_file_to_prd(guest_executor, timestamp):
        success = False
    if not _sync_firewalld_configuration_to_prd(guest_executor, timestamp, source_zone_interfaces):
        success = False
    if not _sync_ntp_configuration_to_prd(guest_executor, timestamp):
        success = False
    if not _sync_centos_repo_configuration(guest_executor, timestamp):
        success = False
    if not _ensure_td_agent_repo(guest_executor, timestamp):
        success = False
    if not _sync_iptables_configuration_to_prd(guest_executor, timestamp):
        success = False
    return success


def _ensure_http_proxy_configuration(guest_executor, timestamp: str) -> bool:
    """Ensure /etc/profile exports http/https proxy variables when they are missing."""
    proxy_url = "http://172.16.162.6:3128"
    profile_path = "/etc/profile"
    backup_path = f"{profile_path}-{timestamp}.bak"
    exists_exit, _, _ = guest_executor(
        f"test -f {shlex.quote(profile_path)}",
        check_exit_code=False,
    )
    profile_exists = exists_exit == 0
    if profile_exists:
        profile_exit, profile_content, profile_err = guest_executor(
            f"cat {shlex.quote(profile_path)}",
            check_exit_code=False,
        )
        if profile_exit != 0:
            print(f"   [WARN] Unable to read {profile_path}: {(profile_err or '').strip() or profile_exit}")
            return False
    else:
        profile_content = ""
    proxy_lines = [
        f"export http_proxy={proxy_url}",
        f"export https_proxy={proxy_url}",
        f"export HTTP_PROXY={proxy_url}",
        f"export HTTPS_PROXY={proxy_url}",
    ]
    profile_has_prd_proxies = all(line in profile_content for line in proxy_lines)
    env_exit, env_stdout, _ = guest_executor("env | grep -i http", check_exit_code=False)
    has_proxy_env = env_exit == 0 and bool(env_stdout and env_stdout.strip())
    transformed_profile, proxies_rewritten = transform_text_to_prd(profile_content)
    if profile_has_prd_proxies:
        if not has_proxy_env:
            guest_executor(f". {profile_path}", check_exit_code=False)
        return True
    if proxies_rewritten:
        if has_proxy_env:
            print("   -> Updating /etc/profile proxy declarations to PRD addresses.")
        backup_cmd = (
            f"[ -f {shlex.quote(backup_path)} ] || "
            f"cp {shlex.quote(profile_path)} {shlex.quote(backup_path)}"
        )
        backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
        if backup_exit != 0:
            print(f"   [WARN] Unable to back up {profile_path}: {(backup_err or '').strip() or backup_exit}")
            return False
        if not transformed_profile.endswith("\n"):
            transformed_profile += "\n"
        write_exit, _, write_err = _write_guest_file(guest_executor, profile_path, transformed_profile)
        if write_exit != 0:
            print(f"   [WARN] Failed to update {profile_path}: {(write_err or '').strip() or write_exit}")
            return False
        print(f"   -> Updated proxy configuration in {profile_path} (backup: {backup_path}).")
        reload_exit, _, reload_err = guest_executor(f". {profile_path}", check_exit_code=False)
        if reload_exit != 0:
            print(f"   [WARN] Reloading {profile_path} returned exit code {reload_exit}: {(reload_err or '').strip()}")
        return True
        return True
    if has_proxy_env:
        print("   -> Proxy environment variables detected but /etc/profile lacks persistent exports; adding block.")
    backup_cmd = (
        f"[ -f {shlex.quote(backup_path)} ] || "
        f"cp {shlex.quote(profile_path)} {shlex.quote(backup_path)}"
    )
    backup_exit, _, backup_err = guest_executor(backup_cmd, check_exit_code=False)
    if backup_exit != 0:
        print(f"   [WARN] Unable to back up {profile_path}: {(backup_err or '').strip() or backup_exit}")
        return False
    block = "# PRD proxy configuration\n" + "\n".join(proxy_lines) + "\n"
    base_profile = profile_content.rstrip("\n")
    if base_profile:
        base_profile += "\n\n"
    updated_profile = base_profile + block
    write_exit, _, write_err = _write_guest_file(guest_executor, profile_path, updated_profile)
    if write_exit != 0:
        print(f"   [WARN] Failed to update {profile_path}: {(write_err or '').strip() or write_exit}")
        return False
    verify_exit, verify_content, _ = guest_executor(f"cat {shlex.quote(profile_path)}", check_exit_code=False)
    if verify_exit != 0 or not all(line in (verify_content or "") for line in proxy_lines):
        print(f"   [WARN] Verification failed after updating {profile_path}; proxy exports may be missing.")
        return False
    guest_executor(f". {profile_path}", check_exit_code=False)
    print(f"   -> Added proxy exports to {profile_path} (backup: {backup_path}).")
    env_verify_exit, env_verify_stdout, _ = guest_executor("env | grep -i http", check_exit_code=False)
    if env_verify_exit != 0 or proxy_url not in (env_verify_stdout or ""):
        print("   [WARN] Proxy environment variables may not be active in the current session; please re-login.")
    return True


@dataclass
class WorkflowState:
    clone_name: Optional[str] = None
    vmx_path: Optional[str] = None
    new_vm_on_source: Optional[Any] = None
    migrated_vm_for_rollback: Optional[Any] = None
    migrated_vm_name_for_rollback: Optional[str] = None
    migrated_vm: Optional[Any] = None
    unregistered_from_source: bool = False
    original_nic_info: List[NicPlan] = field(default_factory=list)
    original_dns_servers: List[str] = field(default_factory=list)
    original_default_gateway: Optional[str] = None
    original_default_gateway_source: Optional[str] = None
    default_gateway_owner_idx: Optional[int] = None
    original_static_routes: List[Dict[str, Any]] = field(default_factory=list)
    prd_static_routes: List[Dict[str, Any]] = field(default_factory=list)
    sdk_network_client: Optional[VsphereGuestNetworkSDKType] = None
    source_firewalld_zone_interfaces: Dict[str, List[str]] = field(default_factory=dict)
    source_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    dest_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    target_datastore: Optional[Any] = None
    target_folder: Optional[Any] = None


@dataclass
class SourceVmDetails:
    nic_plans: List[NicPlan]
    dns_servers: List[str]
    default_gateway: Optional[str]
    default_gateway_source: Optional[str]
    default_gateway_owner_idx: Optional[int]
    static_routes: List[Dict[str, Any]]
    firewalld_zone_interfaces: Dict[str, List[str]]


def collect_source_vm_metadata(
    *,
    content: Any,
    target_vm: Any,
    vcsa_host: str,
    vcsa_user: str,
    vcsa_password: str,
    guest_root_pwd: str,
    guest_admin_pwd: Optional[str],
) -> SourceVmDetails:
    """Collect NIC, DNS, and routing metadata from the source VM."""
    if target_vm.guest.toolsRunningStatus != "guestToolsRunning":
        raise SystemError(
            "Source VM must be powered on with VMware Tools running to collect IP information."
        )

    print("   Verified that VMware Tools is running.")
    print("   Gathering NIC information from the source VM...")

    sdk_mac_lookup: Dict[str, Tuple[Dict[str, Any], int]] = {}
    source_routes: List[Dict[str, Any]] = []
    source_networking_state: Dict[str, Any] = {}
    sdk_client: Optional[VsphereGuestNetworkSDKType] = None
    interfaces: List[Dict[str, Any]] = []

    if REQUESTS_AVAILABLE:
        sdk_vm_id = getattr(target_vm, "_moId", None)
        if sdk_vm_id:
            try:
                sdk_client = VsphereGuestNetworkSDK(
                    host=vcsa_host,
                    username=vcsa_user,
                    password=vcsa_password,
                    verify_ssl=False,
                )
                interfaces = sdk_client.list_interfaces(sdk_vm_id)
                source_networking_state = sdk_client.get_networking_state(sdk_vm_id)
                source_routes_payload = sdk_client.list_routes(sdk_vm_id)
                source_routes = extract_routes_from_sdk_payload(source_routes_payload)
            except Exception as sdk_error:  # pylint: disable=broad-exception-caught
                LOGGER.warning("Failed to collect source VM network info via API: %s", sdk_error)
                interfaces = []
                source_networking_state = {}
                source_routes = []
            finally:
                if sdk_client:
                    try:
                        sdk_client.close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
            for idx, iface in enumerate(interfaces or []):
                mac_candidate = extract_mac_from_sdk_interface(iface)
                if mac_candidate:
                    mac_key = mac_candidate.strip().lower()
                    if mac_key:
                        sdk_mac_lookup[mac_key] = (iface, idx)
    else:
        interfaces = []
        source_networking_state = {}
        source_routes = []

    source_dns_servers = extract_dns_servers_from_state(source_networking_state)
    guest_net_lookup = {
        str(nic.macAddress).strip().lower(): nic
        for nic in target_vm.guest.net
        if getattr(nic, "macAddress", None)
    }
    nic_plans: List[NicPlan] = []
    missing_ipv4_macs: List[str] = []
    seen_macs: Set[str] = set()

    for device in getattr(target_vm.config.hardware, "device", []):
        if not isinstance(device, vim.vm.device.VirtualEthernetCard):
            continue
        mac_address_raw = getattr(device, "macAddress", None)
        if isinstance(mac_address_raw, str):
            mac_address = mac_address_raw.strip()
        else:
            mac_address = mac_address_raw
        if not mac_address:
            continue
        mac_lower = mac_address.lower()
        guest_iface = guest_net_lookup.get(mac_lower)
        # Guard against rare duplicate NIC entries by MAC
        if mac_lower in seen_macs:
            LOGGER.debug(
                "Skipping duplicate NIC device entry (label=%s, key=%s) with MAC %s",
                getattr(getattr(device, "deviceInfo", None), "label", "") or "",
                getattr(device, "key", None),
                mac_address,
            )
            continue
        seen_macs.add(mac_lower)
        network_name = ""
        backing = getattr(device, "backing", None)
        if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
            network_obj = getattr(backing, "network", None)
            network_name = getattr(network_obj, "name", "") if network_obj else ""
        elif isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
            if guest_iface and getattr(guest_iface, "network", None):
                network_name = guest_iface.network
        if not network_name and guest_iface and getattr(guest_iface, "network", None):
            network_name = guest_iface.network
        if not network_name:
            device_info = getattr(device, "deviceInfo", None)
            network_label = getattr(device_info, "label", None) if device_info else None
            summary = getattr(device_info, "summary", None) if device_info else None
            network_name = (network_label or summary or "Unknown Network")

        label = getattr(getattr(device, "deviceInfo", None), "label", "") or ""
        device_key = getattr(device, "key", None)
        device_type = type(device).__name__

        nic_plan = NicPlan(
            index=len(nic_plans),
            network_name=network_name,
            mac_address=mac_address,
            device_type=device_type,
            device_key=device_key,
            label=label,
        )
        NIC_PLAN_VALIDATOR.validate(nic_plan.to_dict())

        sdk_iface_entry = sdk_mac_lookup.get(mac_lower)
        nic_ip_address: Optional[str] = None
        prefix_len: Optional[int] = None
        sdk_interface_index: Optional[int] = None
        sdk_nic_id: Optional[Any] = None

        if sdk_iface_entry:
            iface_data, iface_idx = sdk_iface_entry
            sdk_interface_index = iface_idx
            sdk_nic_id = iface_data.get("nic")
            nic_ip_address, prefix_len = extract_ipv4_from_sdk_interface(iface_data)
            nic_plan.sdk_interface = iface_data

        ip_v4_info = None
        if guest_iface and getattr(guest_iface, "ipConfig", None) and getattr(guest_iface.ipConfig, "ipAddress", None):
            ip_v4_info = next(
                (ip for ip in guest_iface.ipConfig.ipAddress if "." in getattr(ip, "ipAddress", "")),
                None,
            )
            if ip_v4_info and getattr(ip_v4_info, "ipAddress", None) and not nic_ip_address:
                nic_ip_address = ip_v4_info.ipAddress

        subnet_mask = prefix_to_subnet_mask(prefix_len) if prefix_len is not None else None
        if subnet_mask is None and ip_v4_info and getattr(ip_v4_info, "prefixLength", None) is not None:
            subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
        if subnet_mask is None and ip_v4_info and getattr(ip_v4_info, "netmask", None):
            subnet_mask = ip_v4_info.netmask

        nic_plan.ip_address = nic_ip_address
        nic_plan.subnet_mask = subnet_mask
        nic_plan.subnet_prefix = prefix_len

        if sdk_interface_index is not None:
            nic_plan.sdk_interface_index = sdk_interface_index
        if sdk_nic_id:
            nic_plan.sdk_nic_id = str(sdk_nic_id)

        if not nic_ip_address:
            missing_ipv4_macs.append(mac_address)
        nic_plans.append(nic_plan)

    if missing_ipv4_macs:
        LOGGER.debug("NICs without IPv4 info from guest tools: %s", missing_ipv4_macs)

    source_interface_names: Dict[str, str] = {}
    firewalld_zone_interfaces: Dict[str, List[str]] = {}
    guest_operations_manager = getattr(content, "guestOperationsManager", None)
    if nic_plans and guest_operations_manager:
        try:
            reset_root_login_disabled()
            root_credentials = vim.vm.guest.NamePasswordAuthentication(
                username=GUEST_ROOT_USER,
                password=guest_root_pwd,
            )
            admin_credentials = None
            if GUEST_ADMIN_USER and guest_admin_pwd:
                admin_credentials = vim.vm.guest.NamePasswordAuthentication(
                    username=GUEST_ADMIN_USER,
                    password=guest_admin_pwd,
                )

            def source_guest_command_executor(command: str, check_exit_code: bool = True):
                return execute_command_in_guest(
                    guest_operations_manager,
                    target_vm,
                    root_credentials,
                    admin_credentials,
                    guest_admin_pwd,
                    command,
                    check_exit_code=check_exit_code,
                )

            try:
                source_inventory = collect_interface_inventory(source_guest_command_executor)
            except Exception as inventory_error:  # pylint: disable=broad-exception-caught
                LOGGER.debug(
                    "Unable to collect source interface inventory: %s",
                    inventory_error,
                    exc_info=True,
                )
            else:
                for entry in source_inventory:
                    mac_candidate = (entry.get("mac") or "").strip().lower()
                    ifname_candidate = entry.get("ifname")
                    if mac_candidate and ifname_candidate:
                        source_interface_names[mac_candidate] = ifname_candidate
                for nic_plan in nic_plans:
                    mac_lower = (nic_plan.get("mac_address") or "").strip().lower()
                    if mac_lower and mac_lower in source_interface_names:
                        nic_plan["original_ifname"] = source_interface_names[mac_lower]

            def _collect_firewalld_zone_interfaces() -> Dict[str, List[str]]:
                mapping: Dict[str, List[str]] = {}
                check_exit, _, _ = source_guest_command_executor(
                    "command -v firewall-cmd",
                    check_exit_code=False,
                )
                if check_exit != 0:
                    return mapping
                if not _is_service_active(source_guest_command_executor, "firewalld"):
                    return mapping
                zone_names: Set[str] = set()
                zones_exit, zones_stdout, _ = source_guest_command_executor(
                    "firewall-cmd --get-zones",
                    check_exit_code=False,
                )
                if zones_exit == 0 and zones_stdout:
                    for token in zones_stdout.split():
                        token_clean = token.strip()
                        if token_clean:
                            zone_names.add(token_clean)
                if not zone_names:
                    dir_exit, dir_stdout, _ = source_guest_command_executor(
                        "ls /etc/firewalld/zones",
                        check_exit_code=False,
                    )
                    if dir_exit == 0 and dir_stdout:
                        for line in dir_stdout.splitlines():
                            candidate = line.strip()
                            if candidate.endswith(".xml"):
                                zone_names.add(candidate[:-4])
                if not zone_names:
                    return mapping
                for zone_name in sorted(zone_names):
                    list_exit, list_stdout, list_err = source_guest_command_executor(
                        f"firewall-cmd --permanent --zone={shlex.quote(zone_name)} --list-interfaces",
                        check_exit_code=False,
                    )
                    if list_exit != 0:
                        if list_err:
                            LOGGER.debug(
                                "Failed to read firewalld interfaces for zone '%s': %s",
                                zone_name,
                                list_err.strip(),
                            )
                        continue
                    interfaces = [
                        entry.strip()
                        for entry in (list_stdout or "").split()
                        if entry.strip()
                    ]
                    mapping[zone_name] = interfaces
                return mapping

            firewalld_zone_interfaces = _collect_firewalld_zone_interfaces()
        finally:
            reset_root_login_disabled()

    original_default_gateway: Optional[str] = None
    original_default_gateway_source: Optional[str] = None
    default_gateway_owner_idx: Optional[int] = None
    original_static_routes: List[Dict[str, Any]] = []
    route_keys: Set[Tuple[str, int, str]] = set()

    if source_routes:
        (
            computed_routes,
            default_route_candidates,
            default_gateway_candidate,
            default_owner_candidate,
        ) = build_static_route_entries(source_routes, nic_plans)
        original_static_routes.extend(computed_routes)
        if default_gateway_candidate and original_default_gateway is None:
            original_default_gateway = default_gateway_candidate
            default_gateway_owner_idx = default_owner_candidate
            original_default_gateway_source = "rest-default-route"
        selected_default = select_default_gateway_route(default_route_candidates, nic_plans)
        if selected_default:
            original_default_gateway, default_gateway_owner_idx = selected_default
            for idx, nic_plan in enumerate(nic_plans):
                nic_plan["is_gateway_nic"] = idx == default_gateway_owner_idx
            owner_display = (
                default_gateway_owner_idx + 1 if default_gateway_owner_idx is not None else "?"
            )
            print(f"   -> Detected default gateway {original_default_gateway} (NIC {owner_display}).")
        elif default_route_candidates and original_default_gateway is not None:
            owner_candidate = default_gateway_owner_idx
            if owner_candidate is None:
                owner_candidate = default_route_candidates[0].get("owner_index")
                if owner_candidate is None and original_default_gateway:
                    owner_candidate = find_gateway_owner_index(nic_plans, original_default_gateway)
            if owner_candidate is not None and 0 <= owner_candidate < len(nic_plans):
                default_gateway_owner_idx = owner_candidate
                for idx, nic_plan in enumerate(nic_plans):
                    nic_plan["is_gateway_nic"] = idx == owner_candidate
                print(
                    f"   -> Detected default gateway {original_default_gateway} "
                    f"(NIC {owner_candidate + 1})."
                )

    ip_stack_objects = getattr(target_vm.guest, "ipStack", None)
    if ip_stack_objects:
        for stack in ip_stack_objects:
            route_config = getattr(stack, "ipRouteConfig", None)
            if not route_config:
                continue
            for route in getattr(route_config, "ipRoute", []):
                route_network = getattr(route, "network", None)
                prefix_length = getattr(route, "prefixLength", None)
                gateway_obj = getattr(route, "gateway", None)
                gateway_ip = getattr(gateway_obj, "ipAddress", None) if gateway_obj else None
                if route_network is None or prefix_length is None:
                    continue
                try:
                    prefix_int = int(prefix_length)
                except (TypeError, ValueError):
                    continue
                network_str = str(route_network)
                gateway_key = str(gateway_ip or "")
                key = (network_str, prefix_int, gateway_key)
                if key in route_keys:
                    continue
                if _is_link_local_network(network_str, prefix_int) or _is_link_local_address(gateway_ip):
                    continue
                owner_index = getattr(route, "interface", None)
                if owner_index is None and gateway_ip:
                    owner_index = find_gateway_owner_index(nic_plans, gateway_ip)
                entry = {
                    "network": network_str,
                    "prefix": prefix_int,
                    "gateway": gateway_ip,
                }
                if owner_index is not None:
                    entry["owner_index"] = owner_index
                original_static_routes.append(entry)
                route_keys.add(key)
                if (
                    network_str == "0.0.0.0"
                    and prefix_int == 0
                    and gateway_ip
                    and original_default_gateway is None
                ):
                    original_default_gateway = gateway_ip
                    default_gateway_owner_idx = owner_index
                    print(
                        f"   Default gateway '{original_default_gateway}' retrieved from VMware Tools."
                    )
                    original_default_gateway_source = "vmware-tools"

    if original_default_gateway is None:
        octet_gateway = derive_gateway_from_octet_rule(nic_plans)
        if octet_gateway:
            original_default_gateway, default_gateway_owner_idx = octet_gateway
            original_default_gateway_source = "octet-fallback"
    if original_default_gateway is None:
        inferred_gateway = infer_gateway_from_routes(nic_plans, original_static_routes)
        if inferred_gateway:
            original_default_gateway, default_gateway_owner_idx = inferred_gateway
            original_default_gateway_source = "route-inferred"
        else:
            chosen_gateway = derive_fallback_gateway(nic_plans)
            if chosen_gateway:
                original_default_gateway, default_gateway_owner_idx = chosen_gateway
                original_default_gateway_source = "nic-gateway"

    if (
        default_gateway_owner_idx is not None
        and 0 <= default_gateway_owner_idx < len(nic_plans)
    ):
        for idx, nic_plan in enumerate(nic_plans):
            is_owner = idx == default_gateway_owner_idx
            nic_plan["is_gateway_nic"] = is_owner
            if is_owner and original_default_gateway:
                nic_plan["gateway"] = original_default_gateway
        owner_display = default_gateway_owner_idx + 1
        if original_default_gateway_source == "octet-fallback":
            print(
                f"   -> Default gateway not reported; will assume {original_default_gateway} "
                f"(NIC {owner_display}) based on fallback rule."
            )
        elif original_default_gateway_source == "nic-gateway":
            print(
                f"   -> Default gateway not reported; using NIC {owner_display} gateway "
                f"{original_default_gateway} as fallback."
            )
        elif original_default_gateway_source == "route-inferred":
            print(
                f"   -> Default gateway not reported; inferred {original_default_gateway} "
                f"(NIC {owner_display}) from static routes."
            )
        else:
            print(
                "   -> Marked NIC "
                f"{owner_display} as default gateway owner ({original_default_gateway})."
            )
    elif original_default_gateway:
        print(
            "   -> Default gateway detected but owner NIC could not be resolved "
            f"({original_default_gateway})."
        )

    if original_static_routes:
        deduped_routes: List[Dict[str, Any]] = []
        seen_route_keys: Set[Tuple[str, int, str]] = set()
        for route_entry in original_static_routes:
            network_str = str(route_entry.get("network") or "")
            prefix_raw = route_entry.get("prefix")
            try:
                prefix_int = int(prefix_raw) if prefix_raw is not None else -1
            except (TypeError, ValueError):
                prefix_int = -1
            gateway_key = str(route_entry.get("gateway") or "")
            route_key = (network_str, prefix_int, gateway_key)
            if route_key in seen_route_keys:
                continue
            seen_route_keys.add(route_key)
            deduped_routes.append(route_entry)
        original_static_routes = deduped_routes

    if original_static_routes:
        print("   Retrieved static routes (STG):")
        for route in original_static_routes:
            gw_disp = route.get("gateway") or "(none)"
            prefix_value = route.get("prefix")
            destination = route["network"] if prefix_value is None else f"{route['network']}/{prefix_value}"
            print(f"      - {destination} via {gw_disp}")

    original_dns_servers = [
        dns for dns in source_dns_servers if dns and not str(dns).startswith("127.")
    ]
    if (
        not original_dns_servers
        and target_vm.guest.ipStack
        and target_vm.guest.ipStack[0].dnsConfig
    ):
        original_dns_servers = [
            dns
            for dns in target_vm.guest.ipStack[0].dnsConfig.ipAddress
            if not dns.startswith("127.")
        ]
    if original_dns_servers:
        original_dns_servers = dedupe_preserving_order(original_dns_servers)

    print(f"   [OK] Retrieved {len(nic_plans)} NIC configuration entries.")

    return SourceVmDetails(
        nic_plans=nic_plans,
        dns_servers=original_dns_servers,
        default_gateway=original_default_gateway,
        default_gateway_source=original_default_gateway_source,
        default_gateway_owner_idx=default_gateway_owner_idx,
        static_routes=original_static_routes,
        firewalld_zone_interfaces=firewalld_zone_interfaces,
    )


@dataclass
class GuestInterfaceContext:
    device_name: str
    interface_names: Set[str]
    interface_names_compact: Set[str]
    mac_lookup: Dict[str, Any]
    new_mac_lower: str
    original_mac_lower: str


class CloneAndVmotionWorkflow:
    """High-level coordinator that drives the clone and vMotion migration workflow."""
    def __init__(
        self,
        ctx: ssl.SSLContext,
        vcsa_pwd_source: str,
        vcsa_pwd_dest: str,
        target_vm_name: str,
        guest_root_pwd: str,
        guest_admin_pwd: str,
    ) -> None:
        self.ctx = ctx
        self.vcsa_pwd_source = vcsa_pwd_source
        self.vcsa_pwd_dest = vcsa_pwd_dest
        self.target_vm_name = target_vm_name
        self.guest_root_pwd = guest_root_pwd
        self.guest_admin_pwd = guest_admin_pwd
        self.state = WorkflowState()
        self.si_source = None
        self.si_dest = None
        self.content_source = None
        self.content_dest = None
        self.target_vm = None

    def run(self) -> None:
        """Execute the full staged-to-production migration workflow."""
        try:
            self._preflight_check()
            self._collect_source_vm_details()
            self._confirm_clone_plan()
            self._perform_source_clone_operations()
            self._register_destination_vm()
            self._recreate_destination_nics()
            self._configure_destination_network()
            self._perform_storage_vmotion()
            self._finalize_success()
        except Exception as error:  # pylint: disable=broad-exception-caught
            self._handle_error(error)
        finally:
            self._cleanup()

    def _preflight_check(self) -> None:
        print(
            "\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---"
        )
        print(
            f"   Attempting to connect to source vCenter ({VCSA_HOST_SOURCE})..."
        )
        source_session = authenticate_vcenter(
            VCSA_HOST_SOURCE,
            VCSA_USER,
            self.vcsa_pwd_source,
            self.ctx,
            host_env_var=ENV_SOURCE_HOST_VAR,
        )
        print("   [OK] Source vCenter authentication succeeded.")
        Disconnect(source_session)

        print(
            f"   Attempting to connect to destination vCenter ({VCSA_HOST_DEST})..."
        )
        dest_session = authenticate_vcenter(
            VCSA_HOST_DEST,
            VCSA_USER,
            self.vcsa_pwd_dest,
            self.ctx,
            host_env_var=ENV_DEST_HOST_VAR,
        )
        print("   [OK] Destination vCenter authentication succeeded.")
        Disconnect(dest_session)

    def _collect_source_vm_details(self) -> None:
        print("\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
        state = self.state
        self.si_source = authenticate_vcenter(
            VCSA_HOST_SOURCE,
            VCSA_USER,
            self.vcsa_pwd_source,
            self.ctx,
            host_env_var=ENV_SOURCE_HOST_VAR,
        )
        print("[OK] Connected to source vCenter.")
        self.state.source_keepalive_handle = _start_keepalive_thread(
            self.si_source,
            "source-vcenter",
        )

        self.content_source = self.si_source.RetrieveContent()
        container_view = self.content_source.viewManager.CreateContainerView(
            self.content_source.rootFolder,
            [vim.VirtualMachine],
            True,
        )
        try:
            self.target_vm = next(
                (vm for vm in container_view.view if vm.name == self.target_vm_name),
                None,
            )
        finally:
            container_view.Destroy()

        if not self.target_vm:
            raise FileNotFoundError(f"VM '{self.target_vm_name}' was not found.")
        print(f"[OK] Located VM '{self.target_vm.name}'.")

        details = collect_source_vm_metadata(
            content=self.content_source,
            target_vm=self.target_vm,
            vcsa_host=VCSA_HOST_SOURCE,
            vcsa_user=VCSA_USER,
            vcsa_password=self.vcsa_pwd_source,
            guest_root_pwd=self.guest_root_pwd,
            guest_admin_pwd=self.guest_admin_pwd,
        )

        state.original_nic_info = list(details.nic_plans)
        state.original_dns_servers = list(details.dns_servers)
        state.original_default_gateway = details.default_gateway
        state.original_default_gateway_source = details.default_gateway_source
        state.default_gateway_owner_idx = details.default_gateway_owner_idx
        state.original_static_routes = [dict(route) for route in details.static_routes]
        state.source_firewalld_zone_interfaces = {
            zone: list(interfaces)
            for zone, interfaces in details.firewalld_zone_interfaces.items()
        }

        datastore_view = self.content_source.viewManager.CreateContainerView(
            self.content_source.rootFolder,
            [vim.Datastore],
            True,
        )
        try:
            target_datastore = next(
                (ds for ds in datastore_view.view if ds.name == TARGET_DATASTORE_NAME),
                None,
            )
        finally:
            datastore_view.Destroy()
        if not target_datastore:
            raise FileNotFoundError(f"Datastore '{TARGET_DATASTORE_NAME}' was not found.")
        state.target_datastore = target_datastore
        state.target_folder = getattr(self.target_vm, "parent", None)
        print(f"   [OK] Datastore '{target_datastore.name}' confirmed.")

        if not state.clone_name:
            state.clone_name = f"{self.target_vm.name}-{datetime.now().strftime('%Y%m%d')}"

    def _confirm_clone_plan(self) -> None:
        planned_clone_name = self.state.clone_name or (
            f"{self.target_vm.name}-{datetime.now().strftime('%Y%m%d')}"
        )
        self.state.clone_name = planned_clone_name

        print("\n" + "=" * 25 + " Pre-execution Check (1/4) " + "=" * 25)
        print("Review the details below before creating the clone and starting the migration.")
        print("\n  [Source VM details]")
        print(f"    - VM name       : {self.target_vm.name}")
        print(f"    - OS            : {self.target_vm.summary.config.guestFullName}")
        print("\n  [Source NIC details]")
        if self.state.original_nic_info:
            for nic_index, nic_details in enumerate(self.state.original_nic_info):
                print(f"    - NIC {nic_index+1} ({nic_details['mac_address']})")
                print(f"      - Network     : {nic_details['network_name']}")
                print(f"      - IP Address  : {nic_details.get('ip_address', '(unknown)')}")
                print(f"      - Subnet Mask : {nic_details.get('subnet_mask', '(unknown)')}")
        else:
            print("    - No NIC information was found.")
        if self.state.original_default_gateway:
            gateway_source = self.state.original_default_gateway_source
            gateway_display = self.state.original_default_gateway
            if gateway_source in ("octet-fallback", "nic-gateway"):
                print(f"    - Gateway     : (not reported; fallback -> {gateway_display})")
            elif gateway_source == "route-inferred":
                print(f"    - Gateway     : (inferred) {gateway_display}")
            else:
                print(f"    - Gateway     : {gateway_display}")
        else:
            print("    - Default gateway not detected.")
        print("\n  [Clone VM specification]")
        print(f"    - New VM name   : {planned_clone_name}")
        print(f"    - Placement datastore: {TARGET_DATASTORE_NAME}")
        print("=" * 64)

        approval_response = input("\nProceed with this clone operation? (y/n): ")
        if approval_response.lower() != 'y':
            raise InterruptedError("Operation cancelled by the user.")

    def _perform_source_clone_operations(self) -> None:
        if not self.si_source:
            raise RuntimeError("Source vCenter connection is not available.")

        selected_datastore = self.state.target_datastore
        if not selected_datastore:
            raise RuntimeError("Target datastore information is missing.")

        planned_clone_name = self.state.clone_name
        target_folder = self.state.target_folder or getattr(self.target_vm, 'parent', None)

        source_relocate_spec = vim.vm.RelocateSpec(datastore=selected_datastore)
        source_clone_spec = vim.vm.CloneSpec(location=source_relocate_spec, powerOn=False, template=False)

        print("\nStarting clone task...")
        clone_task = self.target_vm.Clone(folder=target_folder, name=planned_clone_name, spec=source_clone_spec)
        wait_for_task_completion(
            clone_task,
            "Clone task",
            poll_interval=5.0,
            timeout=3600.0,
            progress_label="Clone progress",
        )
        print(f"\n[OK] Clone completed: '{planned_clone_name}'")

        cloned_vm = clone_task.info.result
        self.state.new_vm_on_source = cloned_vm

        print(f"   Removing NICs from cloned VM '{cloned_vm.name}'...")
        nic_devices = [
            dev for dev in cloned_vm.config.hardware.device
            if isinstance(dev, vim.vm.device.VirtualEthernetCard)
        ]
        if nic_devices:
            nic_removal_specs = [
                vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic)
                for nic in nic_devices
            ]
            removal_config_spec = vim.vm.ConfigSpec(deviceChange=nic_removal_specs)
            removal_task = cloned_vm.ReconfigVM_Task(spec=removal_config_spec)
            wait_for_task_completion(
                removal_task,
                "NIC removal task",
                poll_interval=2.0,
                timeout=600.0,
            )
            print("   [OK] Removed NICs.")
        else:
            print("   - No NICs found on cloned VM; skipping removal.")

        self.state.vmx_path = cloned_vm.config.files.vmPathName
        print(f"   Unregistering VM '{planned_clone_name}' from the source vCenter...")
        cloned_vm.UnregisterVM()
        self.state.unregistered_from_source = True
        print("   [OK] Unregistration completed.")

        _stop_keepalive_thread(self.state.source_keepalive_handle)
        self.state.source_keepalive_handle = None
        Disconnect(self.si_source)
        self.si_source = None
        self.content_source = None
        self.state.new_vm_on_source = None

    def _register_destination_vm(self) -> None:
        state = self.state
        if not state.clone_name or not state.vmx_path:
            raise RuntimeError("Clone metadata is incomplete; source operations may have failed.")

        print("\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
        self.si_dest = authenticate_vcenter(
            VCSA_HOST_DEST,
            VCSA_USER,
            self.vcsa_pwd_dest,
            self.ctx,
            host_env_var=ENV_DEST_HOST_VAR,
        )
        print("[OK] Connected to destination vCenter.")

        state.dest_keepalive_handle = _start_keepalive_thread(self.si_dest, "dest-vcenter")

        self.content_dest = self.si_dest.RetrieveContent()
        vm_view = self.content_dest.viewManager.CreateContainerView(
            self.content_dest.rootFolder,
            [vim.VirtualMachine],
            True,
        )
        try:
            clone_exists = any(vm for vm in vm_view.view if vm.name == state.clone_name)
        finally:
            vm_view.Destroy()
        if clone_exists:
            raise FileExistsError(
                f"A VM named '{state.clone_name}' already exists on the destination vCenter."
            )
        print("[OK] No conflicting VM found on destination vCenter.")

        print("\n--- [Phase 3/7] Destination vCenter: Register VM ---")
        cluster_view = self.content_dest.viewManager.CreateContainerView(
            self.content_dest.rootFolder,
            [vim.ClusterComputeResource],
            True,
        )
        try:
            dest_cluster = next(
                (cluster for cluster in cluster_view.view if cluster.name == TARGET_CLUSTER_NAME),
                None,
            )
        finally:
            cluster_view.Destroy()
        if not dest_cluster:
            raise FileNotFoundError(f"Destination cluster '{TARGET_CLUSTER_NAME}' was not found.")

        register_task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(
            path=state.vmx_path,
            name=state.clone_name,
            asTemplate=False,
            pool=dest_cluster.resourcePool,
        )
        wait_for_task_completion(
            register_task,
            "VM registration task",
            poll_interval=5.0,
            timeout=900.0,
        )

        state.migrated_vm = wait_for_vm_availability(self.content_dest, state.clone_name, retries=60, delay_seconds=2)
        state.migrated_vm_for_rollback = state.migrated_vm
        state.migrated_vm_name_for_rollback = state.clone_name
        print("[OK] VM registration completed.")

    def _recreate_destination_nics(self) -> None:
        state = self.state
        migrated_vm = state.migrated_vm
        if not migrated_vm or not self.content_dest:
            raise RuntimeError("Destination VM context is not available; registration may have failed.")

        print("\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
        if not state.original_nic_info:
            print("   - Skipping NIC reconfiguration because the original VM had no NICs.")
            return

        print("\n" + "=" * 25 + " Pre-execution Check (2/4) " + "=" * 25)
        print("Re-create NICs on the migrated VM and connect to these networks.")

        device_change_spec: List[vim.vm.device.VirtualDeviceSpec] = []
        dest_network_lookup: List[Tuple[str, Optional[Tuple[str, ...]]]] = []

        for index, nic_plan in enumerate(state.original_nic_info):
            original_network_name = nic_plan.get('network_name') or ''
            dest_network_name = original_network_name.replace('STG', 'PRD', 1)
            print(f"  - NIC {index + 1}: '{original_network_name}' -> '{dest_network_name}'")

            network_view = self.content_dest.viewManager.CreateContainerView(
                self.content_dest.rootFolder,
                [vim.Network],
                True,
            )
            try:
                dest_network = next(
                    (net for net in network_view.view if net.name == dest_network_name),
                    None,
                )
            finally:
                network_view.Destroy()

            nic_spec = vim.vm.device.VirtualDeviceSpec()
            nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            device_cls = nic_plan.get('device_type')
            device_class_obj = _resolve_virtual_nic_class(device_cls)
            device_instance = device_class_obj()
            device_instance.key = -(100 + index)

            if isinstance(dest_network, vim.dvs.DistributedVirtualPortgroup):
                portgroup_connection = vim.dvs.PortConnection()
                portgroup_connection.portgroupKey = dest_network.key
                portgroup_connection.switchUuid = dest_network.config.distributedVirtualSwitch.uuid
                device_instance.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
                device_instance.backing.port = portgroup_connection
            elif isinstance(dest_network, vim.Network):
                backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
                backing.network = dest_network
                backing.deviceName = dest_network_name
                device_instance.backing = backing
            else:
                raise FileNotFoundError(f"Destination network '{dest_network_name}' was not found.")

            device_instance.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=True,
                allowGuestControl=True,
            )
            nic_spec.device = device_instance
            device_change_spec.append(nic_spec)

            print("=" * 64)
            user_approval_nic = input("\nApply this NIC configuration? (y/n): ")
            if user_approval_nic.lower() != 'y':
                raise InterruptedError("NIC configuration was cancelled by the user.")

            if isinstance(dest_network, vim.dvs.DistributedVirtualPortgroup):
                dest_identifier = (
                    'dvs',
                    dest_network.key,
                    dest_network.config.distributedVirtualSwitch.uuid,
                )
            elif isinstance(dest_network, vim.Network):
                dest_identifier = ('network', getattr(dest_network, '_moId', None))
            else:
                dest_identifier = None
            dest_network_lookup.append((dest_network_name, dest_identifier))

        if not device_change_spec:
            print("   - Skipping NIC reconfiguration because no NIC specifications were prepared.")
            return

        print("\nApproved. Starting NIC reconfiguration task...")
        config_spec = vim.vm.ConfigSpec(deviceChange=device_change_spec)
        try:
            task = migrated_vm.ReconfigVM_Task(spec=config_spec)
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(self.content_dest, state.clone_name, retries=30, delay_seconds=2)
            state.migrated_vm = migrated_vm
            state.migrated_vm_for_rollback = migrated_vm
            state.migrated_vm_name_for_rollback = state.clone_name
            task = migrated_vm.ReconfigVM_Task(spec=config_spec)
        wait_for_task_completion(
            task,
            "NIC reconfiguration task",
            poll_interval=2.0,
            timeout=900.0,
        )
        print("   [OK] NIC reconfiguration completed.")
        print("   Fetching updated NIC information...")

        try:
            migrated_vm.Reload()
        except vmodl.fault.ManagedObjectNotFound:
            migrated_vm = wait_for_vm_availability(self.content_dest, state.clone_name, retries=30, delay_seconds=2)
            state.migrated_vm = migrated_vm
            state.migrated_vm_for_rollback = migrated_vm
            state.migrated_vm_name_for_rollback = state.clone_name
            migrated_vm.Reload()

        newly_added_nics = [
            dev
            for dev in migrated_vm.config.hardware.device
            if isinstance(dev, vim.vm.device.VirtualEthernetCard)
        ]
        if len(newly_added_nics) != len(state.original_nic_info):
            raise RuntimeError(
                "Recreated NIC count does not match the expected number "
                f"(expected {len(state.original_nic_info)}, found {len(newly_added_nics)})."
            )

        remaining_new_nics = list(newly_added_nics)
        for nic_plan, (dest_name, dest_identifier) in zip(state.original_nic_info, dest_network_lookup):
            matched_nic = None
            for nic_device in remaining_new_nics:
                backing = nic_device.backing
                actual_identifier = None
                actual_name = None
                if isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                    actual_identifier = (
                        'dvs',
                        getattr(backing.port, 'portgroupKey', None),
                        getattr(backing.port, 'switchUuid', None),
                    )
                elif isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                    network_obj = getattr(backing, 'network', None)
                    actual_identifier = ('network', getattr(network_obj, '_moId', None))
                    actual_name = getattr(backing, 'deviceName', None) or getattr(network_obj, 'name', None)
                else:
                    actual_name = getattr(backing, 'deviceName', None)

                if dest_identifier and actual_identifier == dest_identifier:
                    matched_nic = nic_device
                    break
                if actual_name and actual_name.lower() == dest_name.lower():
                    matched_nic = nic_device
                    break

            if matched_nic is None:
                if not remaining_new_nics:
                    raise RuntimeError("Unable to associate newly created NICs with destination networks.")
                matched_nic = remaining_new_nics[0]

            remaining_new_nics.remove(matched_nic)
            nic_plan['new_mac_address'] = matched_nic.macAddress

        print("   [OK] Associated new MAC addresses.")

    def _configure_destination_network(self) -> None:
        """Configure guest networking on the destination VM after registration."""
        raise NotImplementedError

    def _perform_storage_vmotion(self) -> None:
        raise NotImplementedError

    def _finalize_success(self) -> None:
        raise NotImplementedError

    def _handle_error(self, error: Exception) -> None:
        raise NotImplementedError

    def _cleanup(self) -> None:
        raise NotImplementedError

def main() -> None:
    global GUEST_ROOT_PWD, GUEST_ADMIN_PWD

    cli_args = _parse_cli_arguments()
    provided_source_vm_name = (cli_args.source_vm_name or "").strip()

    # ------------------------------------------------
    # 1. Enter passwords
    # ------------------------------------------------
    try:
        VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
        VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
    except (EOFError, KeyboardInterrupt) as error:
        print('ERROR:', error)
        log_failure(f"Credential prompt interrupted: {error}")
        _print_execution_summary()
        sys.exit(1)
    reset_root_login_disabled()

    # ------------------------------------------------
    # 2. Configure SSL context
    # ------------------------------------------------
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------
    # 3. Collect target VM name and guest credentials
    # ------------------------------------------------
    input_target_vm_name = provided_source_vm_name
    if not input_target_vm_name:
        input_target_vm_name = input("Enter the name of the VM to clone: ").strip()
        if not input_target_vm_name:
            print("No VM name provided. Aborting processing.")
            log_failure("No VM name provided.")
            _print_execution_summary()
            sys.exit(0)
    else:
        print(f"Using source VM '{input_target_vm_name}' provided via CLI argument.")
    try:
        GUEST_ROOT_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ROOT_USER}': ")
        GUEST_ADMIN_PWD = getpass.getpass(
            f"Password for Guest OS user '{GUEST_ADMIN_USER}' (for fallback): "
        )
    except (EOFError, KeyboardInterrupt) as error:
        print('ERROR:', error)
        log_failure(f"Guest credential prompt interrupted: {error}")
        _print_execution_summary()
        sys.exit(1)

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
    dest_vm_retained = False
    original_nic_info: List[NicPlan] = []
    original_dns_servers: List[str] = []
    original_default_gateway: str | None = None
    original_default_gateway_source: str | None = None
    original_static_routes: List[Dict[str, Any]] = []
    source_firewalld_zone_interfaces: Dict[str, List[str]] = {}
    si_source = None
    content_source = None
    si_dest = None
    sdk_network_client: Optional[VsphereGuestNetworkSDKType] = None
    source_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    dest_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    workflow: Optional[CloneAndVmotionWorkflow] = None
    cancellation_requested = False
    cancellation_message: Optional[str] = None
    fatal_error: Optional[BaseException] = None
    try:
        workflow = CloneAndVmotionWorkflow(
            ctx=ssl_context,
            vcsa_pwd_source=VCSA_PWD_SOURCE,
            vcsa_pwd_dest=VCSA_PWD_DEST,
            target_vm_name=input_target_vm_name,
            guest_root_pwd=GUEST_ROOT_PWD,
            guest_admin_pwd=GUEST_ADMIN_PWD,
        )
        si_source = authenticate_vcenter(
            VCSA_HOST_SOURCE,
            VCSA_USER,
            VCSA_PWD_SOURCE,
            ssl_context,
            host_env_var=ENV_SOURCE_HOST_VAR,
        )
        source_keepalive_handle = _start_keepalive_thread(si_source, "source-vcenter")
        content_source = si_source.RetrieveContent()
        target_vm = find_vm_by_name(content_source, input_target_vm_name)
        if not target_vm:
            raise FileNotFoundError(f"VM '{input_target_vm_name}' was not found.")
        print(f"[OK] Located VM '{target_vm.name}'.")
        if target_vm.guest.toolsRunningStatus != "guestToolsRunning":
            raise SystemError(
                "Source VM must be powered on with VMware Tools running to collect IP information."
            )
        source_details = collect_source_vm_metadata(
            content=content_source,
            target_vm=target_vm,
            vcsa_host=VCSA_HOST_SOURCE,
            vcsa_user=VCSA_USER,
            vcsa_password=VCSA_PWD_SOURCE,
            guest_root_pwd=GUEST_ROOT_PWD,
            guest_admin_pwd=GUEST_ADMIN_PWD,
        )
        original_nic_info = list(source_details.nic_plans)
        original_dns_servers = list(source_details.dns_servers)
        original_default_gateway = source_details.default_gateway
        original_default_gateway_source = source_details.default_gateway_source
        default_gateway_owner_idx = source_details.default_gateway_owner_idx
        original_static_routes = [dict(route) for route in source_details.static_routes]
        source_dns_servers = list(source_details.dns_servers)
        source_firewalld_zone_interfaces = {
            zone: list(interfaces)
            for zone, interfaces in source_details.firewalld_zone_interfaces.items()
        }
        datastore_view = content_source.viewManager.CreateContainerView(
            content_source.rootFolder,
            [vim.Datastore],
            True,
        )
        try:
            target_datastore = next(
                (ds for ds in datastore_view.view if ds.name == TARGET_DATASTORE_NAME),
                None,
            )
        finally:
            datastore_view.Destroy()
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
            if original_default_gateway_source in ("octet-fallback", "nic-gateway"):
                print(f"    - Gateway     : (not reported; fallback -> {original_default_gateway})")
            elif original_default_gateway_source == "route-inferred":
                print(f"    - Gateway     : (inferred) {original_default_gateway}")
            else:
                print(f"    - Gateway     : {original_default_gateway}")
        else:
            print("    - Default gateway not detected.")
        print("\n  [Clone VM specification]")
        print(f"    - New VM name   : {clone_name}")
        print(f"    - Placement datastore: {TARGET_DATASTORE_NAME}")
        print("=" * 64)
        user_approval = input("\nProceed with this clone operation? (y/n): ")
        if user_approval.lower() != 'y':
            cancellation_requested = True
            cancellation_message = "Operation cancelled by the user."
            print("\n[INFO] Operation cancelled by the user.")
            return

        # --- Clone, NIC removal, and unregister operations ---
        relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
        clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
        print("\nStarting clone task...")
        task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
        wait_for_task_completion(
            task,
            "Clone task",
            poll_interval=5.0,
            timeout=3600.0,
            progress_label="Clone progress",
        )
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
            wait_for_task_completion(
                task,
                "NIC removal task",
                poll_interval=2.0,
                timeout=600.0,
            )
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
        si_dest = authenticate_vcenter(
            VCSA_HOST_DEST,
            VCSA_USER,
            VCSA_PWD_DEST,
            ssl_context,
            host_env_var=ENV_DEST_HOST_VAR,
        )
        print("[OK] Connected to destination vCenter.")
        dest_keepalive_handle = _start_keepalive_thread(si_dest, "dest-vcenter")
        content_dest = si_dest.RetrieveContent()
        vm_view = content_dest.viewManager.CreateContainerView(
            content_dest.rootFolder,
            [vim.VirtualMachine],
            True,
        )
        try:
            clone_exists = any(vm for vm in vm_view.view if vm.name == clone_name)
        finally:
            vm_view.Destroy()
        if clone_exists:
            raise FileExistsError(
                f"A VM named '{clone_name}' already exists on the destination vCenter."
            )
        print("[OK] No conflicting VM found on destination vCenter.")
        print("\n--- [Phase 3/7] Destination vCenter: Register VM ---")
        cluster_view = content_dest.viewManager.CreateContainerView(
            content_dest.rootFolder,
            [vim.ClusterComputeResource],
            True,
        )
        try:
            dest_cluster = next(
                (cluster for cluster in cluster_view.view if cluster.name == TARGET_CLUSTER_NAME),
                None,
            )
        finally:
            cluster_view.Destroy()
        if not dest_cluster:
            raise FileNotFoundError(f"Destination cluster '{TARGET_CLUSTER_NAME}' was not found.")
        task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(
            path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
        wait_for_task_completion(
            task,
            "VM registration task",
            poll_interval=5.0,
            timeout=900.0,
        )
        migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=60, delay_seconds=2)
        migrated_vm_for_rollback = migrated_vm  # Preserve for rollback
        migrated_vm_name_for_rollback = clone_name
        print("[OK] VM registration completed.")
        print("\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
        if original_nic_info:
            print("\n" + "=" * 25 + " Pre-execution Check (2/4) " + "=" * 25)
            print("Re-create NICs on the migrated VM and connect to these networks.")
            device_change_spec: List[vim.vm.device.VirtualDeviceSpec] = []
            dest_network_lookup: List[Tuple[str, Optional[Tuple[str, ...]]]] = []
            for i, nic in enumerate(original_nic_info):
                original_network_name = nic['network_name']
                dest_network_name = original_network_name.replace('STG', 'PRD', 1)
                print(f"  - NIC {i+1}: '{original_network_name}' -> '{dest_network_name}'")

                network_view = content_dest.viewManager.CreateContainerView(
                    content_dest.rootFolder,
                    [vim.Network],
                    True,
                )
                try:
                    dest_network = next(
                        (net for net in network_view.view if net.name == dest_network_name),
                        None,
                    )
                finally:
                    network_view.Destroy()

                nic_spec = vim.vm.device.VirtualDeviceSpec()
                nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
                nic_device_class = _resolve_virtual_nic_class(nic.get('device_type'))
                nic_spec.device = nic_device_class()
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
                if isinstance(dest_network, vim.dvs.DistributedVirtualPortgroup):
                    dest_identifier = (
                        'dvs',
                        dest_network.key,
                        dest_network.config.distributedVirtualSwitch.uuid,
                    )
                elif isinstance(dest_network, vim.Network):
                    dest_identifier = ('network', getattr(dest_network, '_moId', None))
                else:
                    dest_identifier = None
                dest_network_lookup.append((dest_network_name, dest_identifier))

            if not device_change_spec:
                print("   - Skipping NIC reconfiguration because no NIC specifications were prepared.")
            else:
                print("\nApproved. Starting NIC reconfiguration task...")
                config_spec = vim.vm.ConfigSpec(deviceChange=device_change_spec)
                try:
                    task = migrated_vm.ReconfigVM_Task(spec=config_spec)
                except vmodl.fault.ManagedObjectNotFound:
                    migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
                    migrated_vm_for_rollback = migrated_vm
                    migrated_vm_name_for_rollback = clone_name
                    task = migrated_vm.ReconfigVM_Task(spec=config_spec)
                wait_for_task_completion(
                    task,
                    "NIC reconfiguration task",
                    poll_interval=2.0,
                    timeout=900.0,
                )
                print("   [OK] NIC reconfiguration completed.")
                print("   Fetching updated NIC information...")

            try:
                migrated_vm.Reload()
            except vmodl.fault.ManagedObjectNotFound:
                migrated_vm = wait_for_vm_availability(content_dest, clone_name, retries=30, delay_seconds=2)
                migrated_vm_for_rollback = migrated_vm
                migrated_vm_name_for_rollback = clone_name
                migrated_vm.Reload()
            newly_added_nics = [
                dev for dev in migrated_vm.config.hardware.device if isinstance(
                    dev, vim.vm.device.VirtualEthernetCard)
            ]
            if len(newly_added_nics) != len(original_nic_info):
                raise RuntimeError(
                    f"Recreated NIC count does not match the expected number "
                    f"(expected {len(original_nic_info)}, found {len(newly_added_nics)})."
                )

            remaining_new_nics = list(newly_added_nics)
            for nic_record, (dest_name, dest_identifier) in zip(original_nic_info, dest_network_lookup):
                matched_nic = None
                for nic_dev in remaining_new_nics:
                    backing = nic_dev.backing
                    actual_identifier = None
                    actual_name = None
                    if isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                        actual_identifier = (
                            'dvs',
                            getattr(backing.port, 'portgroupKey', None),
                            getattr(backing.port, 'switchUuid', None),
                        )
                    elif isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                        network_obj = getattr(backing, 'network', None)
                        actual_identifier = ('network', getattr(network_obj, '_moId', None))
                        actual_name = getattr(backing, 'deviceName', None) or getattr(network_obj, 'name', None)
                    else:
                        actual_name = getattr(backing, 'deviceName', None)

                    if dest_identifier and actual_identifier == dest_identifier:
                        matched_nic = nic_dev
                        break
                    if actual_name and actual_name.lower() == dest_name.lower():
                        matched_nic = nic_dev
                        break
                if matched_nic is None:
                    if not remaining_new_nics:
                        raise RuntimeError("Unable to associate newly created NICs with destination networks.")
                    matched_nic = remaining_new_nics[0]

                remaining_new_nics.remove(matched_nic)
                nic_record['new_mac_address'] = matched_nic.macAddress
            print("   [OK] Associated new MAC addresses.")
        else:
            print("   - Skipping NIC reconfiguration because the original VM had no NICs.")

        print("\n--- [Phase 5/7] Destination vCenter: Power On ---")
        print("\n" + "=" * 25 + " Pre-execution Check (3/4) " + "=" * 25)
        print("Powering on the VM and applying guest OS IP configuration.")

        new_default_gateway = (
            calculate_ip_stg_to_prd(original_default_gateway)
            if original_default_gateway
            else None
        )
        gateway_nic_present = any(nic.get('is_gateway_nic') for nic in original_nic_info)
        prd_static_routes: List[Dict[str, Any]] = []
        if original_nic_info:
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
            print("  - No DNS server changes required.")
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
        wait_for_task_completion(
            task,
            "Power-on task",
            poll_interval=2.0,
            timeout=600.0,
        )
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
                nonlocal migrated_vm, migrated_vm_for_rollback, migrated_vm_name_for_rollback
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
                new_ip = nic_info.get('prd_ip_address') or calculate_ip_stg_to_prd(nic_info['ip_address'])
                nic_info['prd_ip_address'] = new_ip
                subnet_mask = nic_info.get('subnet_mask') or ''
                prefix: Optional[int] = None
                if subnet_mask:
                    try:
                        prefix = sum(bin(int(part)).count('1') for part in subnet_mask.split('.'))
                    except ValueError:
                        prefix = mask_to_prefix(subnet_mask)
                if prefix is None and subnet_mask:
                    prefix = mask_to_prefix(subnet_mask)
                if prefix is None and new_ip:
                    prefix = 24

                new_mac = nic_info.get('new_mac_address')
                expected_gateway_value = (
                    new_default_gateway
                    if nic_info.get('is_gateway_nic') and new_default_gateway
                    else None
                )
                expected_dns_servers: List[str] = []
                applied_static_routes: List[str] = []

                print("\n" + "=" * 20 + f" NIC {i+1} Configuration " + "=" * 20)

                interface_ctx = prepare_guest_interface(i, nic_info, guest_command_executor, new_mac)
                device_name = interface_ctx.device_name
                con_name = device_name
                guest_iface_names = interface_ctx.interface_names
                guest_iface_names_compact = interface_ctx.interface_names_compact
                new_mac_lower = interface_ctx.new_mac_lower
                original_mac_lower = interface_ctx.original_mac_lower
                print(f"   -> Guest OS interface '{device_name}' located.")
                nmcli_check_exit, _, _ = guest_command_executor("command -v nmcli", check_exit_code=False)
                nmcli_supported = nmcli_check_exit == 0
                new_dns_servers: List[str] = []
                if i == 0 and original_dns_servers:
                    new_dns_servers = dedupe_preserving_order(
                        calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns
                    )
                    if new_dns_servers:
                        expected_dns_servers = new_dns_servers[:]
                        expected_dns_overall = dedupe_preserving_order(new_dns_servers)
                routes_for_nic: List[Tuple[int, Dict[str, Any]]] = []
                if prd_static_routes:
                    for route_idx, route_info in enumerate(prd_static_routes):
                        owner_index = route_info.get('owner_index')
                        if owner_index is not None and owner_index != i:
                            continue
                        if route_idx in configured_route_indices:
                            continue
                        routes_for_nic.append((route_idx, route_info))

                should_configure_routes = bool(routes_for_nic)
                if not should_configure_routes and new_default_gateway and new_ip and prefix is not None:
                    try:
                        nic_network = ipaddress.IPv4Interface(f"{new_ip}/{prefix}").network
                        if ipaddress.IPv4Address(new_default_gateway) in nic_network:
                            should_configure_routes = True
                    except (ValueError, ipaddress.AddressValueError):
                        pass
                if not should_configure_routes and not gateway_nic_present:
                    should_configure_routes = i == 0
                use_nmcli_connection = nmcli_supported
                rest_attempted = False
                rest_configured = False
                if (
                    use_sdk_networking
                    and sdk_network_client
                    and sdk_vm_id
                    and nic_info.get('sdk_nic_id')
                    and new_ip
                    and prefix is not None
                ):
                    rest_attempted = True
                    try:
                        ipv4_spec = IPv4Config(
                            address=new_ip,
                            prefix=prefix,
                            default_gateway=expected_gateway_value if should_configure_routes else None,
                        )
                        dns_spec = (
                            DnsConfig(dedupe_preserving_order(expected_dns_servers))
                            if expected_dns_servers
                            else None
                        )
                        route_specs: List[Any] = []
                        for route_idx, route_info in routes_for_nic:
                            route_network = route_info.get('network')
                            route_gateway = route_info.get('gateway')
                            route_prefix = route_info.get('prefix')
                            if not route_network or not route_gateway:
                                continue
                            try:
                                route_prefix_int = int(route_prefix) if route_prefix is not None else None
                            except (TypeError, ValueError):
                                route_prefix_int = None
                            route_specs.append(
                                RouteConfig(
                                    network=str(route_network),
                                    gateway=str(route_gateway),
                                    prefix=route_prefix_int,
                                )
                            )
                        sdk_network_client.update_interface(
                            sdk_vm_id,
                            str(nic_info['sdk_nic_id']),
                            ipv4_spec,
                            dns_spec,
                            route_specs if route_specs else None,
                        )
                        rest_configured = True
                        use_nmcli_connection = False
                        if expected_dns_servers and not expected_dns_overall:
                            expected_dns_overall = dedupe_preserving_order(expected_dns_servers)
                        for route_idx, _ in routes_for_nic:
                            configured_route_indices.add(route_idx)
                        print("   -> Updated NIC successfully via REST guest networking API.")
                    except Exception as sdk_update_error:  # pylint: disable=broad-exception-caught
                        LOGGER.warning(
                            "REST guest networking update failed; falling back to guest operations: %s",
                            sdk_update_error,
                        )
                        print("   -> REST update failed; falling back to guest operations for configuration.")
                        use_sdk_networking = False
                selected_route_indices: List[int] = []
                selected_route_lines: List[str] = []
                legacy_config_success = True
                legacy_verification_command: Optional[str] = None
                if rest_configured:
                    selected_route_indices = [route_idx for route_idx, _ in routes_for_nic]
                    selected_route_lines = []
                elif nmcli_supported:
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
                            uuid_to_device: Dict[str, str] = {}
                            for entry in parsed_connections:
                                normalized_entry = {}
                                for nmcli_field in nmcli_fields:
                                    normalized_entry[nmcli_field.lower()] = entry.get(nmcli_field, "")
                                existing_connections.append(normalized_entry)
                                uuid_value = (normalized_entry.get('uuid') or "").strip()
                                if uuid_value:
                                    uuid_to_device[uuid_value] = (normalized_entry.get('device') or "").strip().lower()
                            target_device_lower = device_name.lower()
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
                            get_connection_details = make_nmcli_detail_fetcher(
                                connection_detail_cache, guest_command_executor
                            )
                            for conn in existing_connections:
                                uuid = (conn.get('uuid') or "").strip()
                                if not uuid:
                                    continue
                                name_norm = (conn.get('name') or "").strip().lower()
                                device_norm = (conn.get('device') or "").strip().lower()
                                if device_norm and device_norm != target_device_lower:
                                    continue
                                type_norm = (conn.get('type') or "").strip().lower()
                                name_compact = compact_interface_name(name_norm)
                                device_compact = compact_interface_name(device_norm)
                                alias_match = False
                                if alias_targets:
                                    if device_norm in alias_targets or name_norm in alias_targets:
                                        alias_match = True
                                    elif device_compact and any(
                                        target in device_compact
                                        for target in alias_targets_compact
                                    ):
                                        alias_match = True
                                    elif name_compact and any(
                                        target in name_compact
                                        for target in alias_targets_compact
                                    ):
                                        alias_match = True
                                orphaned_interface = False
                                if not device_norm:
                                    if name_norm and LEGACY_INTERFACE_PATTERN.match(name_norm):
                                        if (
                                            guest_iface_names
                                            and name_norm not in guest_iface_names
                                            and name_compact not in guest_iface_names_compact
                                        ):
                                            orphaned_interface = True
                                    elif (
                                        alias_targets
                                        and name_compact
                                        and any(
                                            target in name_compact
                                            for target in alias_targets_compact
                                        )
                                    ):
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
                                    mapped_device = uuid_to_device.get(uuid, "")
                                    if mapped_device and mapped_device != target_device_lower:
                                        continue
                                    guest_command_executor(f"nmcli connection delete uuid {uuid}")
                            guest_command_executor(
                                f"ip addr flush dev {device_name}",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli connection add type ethernet con-name '{con_name}' "
                                f"ifname '{device_name}' autoconnect no"
                            )
                            if new_ip and prefix is not None:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.method manual "
                                    f"ipv4.addresses '{new_ip}/{prefix}'"
                                )
                            else:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.method manual "
                                    "ipv4.addresses ''"
                                )
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv6.method ignore",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv6.never-default yes",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv6.addresses ''",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv6.routes ''",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli connection modify '{con_name}' ipv6.dns ''",
                                check_exit_code=False,
                            )
                            if expected_gateway_value:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' "
                                    f"ipv4.gateway '{expected_gateway_value}'"
                                )
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.never-default no",
                                    check_exit_code=False,
                                )
                            else:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.gateway ''",
                                    check_exit_code=False,
                                )
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.never-default yes",
                                    check_exit_code=False,
                                )
                            if new_dns_servers:
                                deduped_dns = dedupe_preserving_order(new_dns_servers)
                                dns_str = ' '.join(deduped_dns)
                                guest_command_executor(f"nmcli connection modify '{con_name}' ipv4.dns '{dns_str}'")
                                expected_dns_servers = deduped_dns[:]
                                if not expected_dns_overall:
                                    expected_dns_overall = dedupe_preserving_order(deduped_dns)
                            else:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.dns ''",
                                    check_exit_code=False,
                                )
                            if should_configure_routes:
                                guest_command_executor(
                                    f"nmcli connection modify '{con_name}' ipv4.routes ''",
                                    check_exit_code=False,
                                )
                                for route_idx, route_info in routes_for_nic:
                                    gateway = route_info.get('gateway')
                                    prefix_value = route_info.get('prefix')
                                    network_base = route_info.get('network')
                                    if not gateway or prefix_value is None or not network_base:
                                        continue
                                    network_cidr = f"{network_base}/{prefix_value}"
                                    guest_command_executor(
                                        (
                                            f"nmcli connection modify '{con_name}' "
                                            f"+ipv4.routes '{network_cidr} {gateway}'"
                                        ),
                                        check_exit_code=False,
                                    )
                                    selected_route_indices.append(route_idx)
                                    selected_route_lines.append(f"{network_cidr} via {gateway}")
                                    print(f"      - Added: {network_cidr} via {gateway}")
                        except NmcliNotAvailableError:
                            print("   -> nmcli command unavailable; applying legacy network configuration.")
                            (
                                selected_route_indices,
                                selected_route_lines,
                                legacy_config_success,
                                legacy_verification_command,
                            ) = configure_interface_without_nmcli(
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
                        (
                            selected_route_indices,
                            selected_route_lines,
                            legacy_config_success,
                            legacy_verification_command,
                        ) = configure_interface_without_nmcli(
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
                    (
                        selected_route_indices,
                        selected_route_lines,
                        legacy_config_success,
                        legacy_verification_command,
                    ) = configure_interface_without_nmcli(
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
                if not use_nmcli_connection and not legacy_config_success:
                    raise RuntimeError("Legacy network configuration failed inside the guest OS. See log output above.")
                for route_idx in selected_route_indices:
                    configured_route_indices.add(route_idx)
                # 4. Bring up the new connection
                if use_nmcli_connection:
                    guest_command_executor(
                        f"nmcli connection modify '{con_name}' connection.autoconnect yes",
                        check_exit_code=False,
                    )
                    guest_command_executor(
                        f"nmcli device connect '{device_name}'",
                        check_exit_code=False,
                    )
                    up_exit, up_stdout, up_stderr = guest_command_executor(
                        f"nmcli connection up '{con_name}'",
                        check_exit_code=False,
                    )
                    if up_exit != 0:
                        combined_output = "\n".join(filter(None, (up_stdout, up_stderr))).lower()
                        if "base network connection was interrupted" in combined_output:
                            print("   -> nmcli reported an interrupted base connection; retrying activation.")
                            guest_command_executor(
                                f"nmcli device disconnect '{device_name}' || true",
                                check_exit_code=False,
                            )
                            guest_command_executor(
                                f"nmcli device connect '{device_name}'",
                                check_exit_code=False,
                            )
                            up_exit, up_stdout, up_stderr = guest_command_executor(
                                f"nmcli connection up '{con_name}'",
                                check_exit_code=False,
                            )
                        if up_exit != 0:
                            print(
                                "   [WARN] nmcli connection up returned a non-zero exit code; continuing with validation."
                            )
                guest_command_executor(f"ip -6 addr flush dev {device_name}", check_exit_code=False)
                guest_command_executor(
                    (
                        f"if [ -f /proc/sys/net/ipv6/conf/{device_name}/disable_ipv6 ]; then "
                        f"sysctl -w net.ipv6.conf.{device_name}.disable_ipv6=1; "
                        "fi"
                    ),
                    check_exit_code=False,
                )
                guest_command_executor(
                    (
                        f"if [ -f /proc/sys/net/ipv6/conf/{device_name}/autoconf ]; then "
                        f"sysctl -w net.ipv6.conf.{device_name}.autoconf=0; "
                        "fi"
                    ),
                    check_exit_code=False,
                )
                # 4.5. Broadcast gratuitous ARP to refresh neighbor caches
                if new_ip:
                    guest_command_executor(
                        (
                            "if command -v ip >/dev/null 2>&1; then "
                            f"ip link set {device_name} up; "
                            "elif command -v ifconfig >/dev/null 2>&1; then "
                            f"ifconfig {device_name} up; "
                            "fi"
                        ),
                        check_exit_code=False,
                    )
                    arping_commands = [
                        f"arping -c 3 -A -I {device_name} {new_ip}",
                        f"arping -c 3 -U -I {device_name} {new_ip}",
                    ]
                    for arping_cmd in arping_commands:
                        guest_command_executor(arping_cmd, check_exit_code=False)

                # 5. Final verification
                time.sleep(5)
                if new_ip:
                    verification_cmd = None
                    if use_nmcli_connection:
                        verification_cmd = f"ip addr show {device_name} | grep -q '{new_ip}'"
                    else:
                        verification_cmd = legacy_verification_command or f"ip addr show {device_name} | grep -q '{new_ip}'"
                    guest_command_executor(verification_cmd)
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
                    gw_candidate = ""
                    if len(parts) >= 3 and parts[1].lower() == 'via':
                        gw_candidate = parts[2]
                    elif parts:
                        gw_candidate = parts[-1]
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
                        workflow_had_warnings = True
                    else:
                        raise

                expected_ip_cidr = f"{new_ip}/{prefix}" if new_ip and prefix is not None else ""
                if use_nmcli_connection:
                    nmcli_validation_tasks.append((
                        con_name,
                        device_name,
                        expected_ip_cidr,
                        expected_gateway_value,
                        applied_static_routes.copy(),
                        expected_dns_servers[:] if expected_dns_servers else []
                    ))
            if not _sync_prd_system_configuration(
                guest_command_executor,
                source_firewalld_zone_interfaces,
            ):
                workflow_had_warnings = True
            ensure_firewall_allows_ssh(guest_command_executor, SSH_ALLOWED_SOURCE_IP)
            print("   [OK] Completed IP configuration for all NICs.")
        expected_dns_overall = dedupe_preserving_order(expected_dns_overall)
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
                    configured_route_payload = [
                        route_entry
                        for idx, route_entry in enumerate(prd_static_routes)
                        if idx in configured_route_indices
                    ]
                    sdk_verification_succeeded = verify_destination_network_with_sdk(
                        validation_client,
                        validation_vm_id,
                        original_nic_info,
                        expected_dns_overall,
                        configured_route_payload,
                    )
                except Exception as sdk_error:
                    LOGGER.warning("SDK verification encountered an error: %s", sdk_error)
                finally:
                    if created_validation_client and validation_client:
                        validation_client.close()
        if not sdk_verification_succeeded:
            workflow_had_warnings = True
            for (
                con_name,
                device_name,
                expected_ip_cidr,
                expected_gateway_value,
                routes_snapshot,
                dns_snapshot,
            ) in nmcli_validation_tasks:
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
        final_view = content_dest.viewManager.CreateContainerView(
            content_dest.rootFolder,
            [vim.Datastore],
            True,
        )
        try:
            final_datastore = next(
                (ds for ds in final_view.view if ds.name == TARGET_DATASTORE_NAME_FINAL),
                None,
            )
        finally:
            final_view.Destroy()
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

        wait_for_task_completion(
            task,
            "Storage vMotion task",
            poll_interval=5.0,
            timeout=3600.0,
            progress_label="Storage vMotion progress",
        )

        print("\n[OK] Storage vMotion completed successfully.")
        print("\n[OK] Migration workflow finished without errors.")
        _stop_keepalive_thread(dest_keepalive_handle)
        dest_keepalive_handle = None
        Disconnect(si_dest)
        si_dest = None
    except Exception as error:
        cancellation_requested = isinstance(error, InterruptedError)
        if cancellation_requested:
            cancellation_message = (str(error) or "").strip() or "Operation cancelled by the user."
            print(f"\n[INFO] {cancellation_message}")
        else:
            fatal_error = error
            print(f"\n[ERROR] An error occurred during processing: {error}")

        dest_vm_retained = False
        if migrated_vm_for_rollback:
            dest_vm_retained = True
            print("\n" + "=" * 20 + " Rollback Confirmation (Destination VM Removal) " + "=" * 20)
            print("The process stopped, leaving a partially migrated VM on the destination vCenter.")
            vm_name_display = migrated_vm_name_for_rollback or clone_name or "(unknown)"
            print(f"  - Target VM: {vm_name_display}")

            rollback_approval = input("\nDelete this VM to return to the pre-operation state? (y/n): ")
            if rollback_approval.lower() == 'y':
                try:
                    dest_vm_retained = False
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
                        try:
                            si_dest = authenticate_vcenter(
                                VCSA_HOST_DEST,
                                VCSA_USER,
                                VCSA_PWD_DEST,
                                ssl_context,
                                host_env_var=ENV_DEST_HOST_VAR,
                            )
                        except Exception as reconnect_error:
                            raise ConnectionError(
                                "Failed to reconnect to the destination vCenter."
                            ) from reconnect_error
                        print("   [OK] Reconnected successfully.")
                        dest_keepalive_handle = _start_keepalive_thread(si_dest, "dest-vcenter-cleanup")
                    content_dest_cleanup = si_dest.RetrieveContent()
                    vm_to_delete = find_vm_by_name(content_dest_cleanup, clone_name)
                    if not vm_to_delete:
                        print("   [INFO] Rollback target VM not found. It may already be deleted.")
                        unregistered_from_source = True
                        dest_vm_retained = False
                    else:
                        if vm_to_delete.runtime.powerState == 'poweredOn':
                            print(f"   Powering off VM '{vm_to_delete.name}'...")
                            task = vm_to_delete.PowerOffVM_Task()
                            poweroff_state = wait_for_task_completion(
                                task,
                                "Power-off task",
                                poll_interval=2.0,
                                timeout=600.0,
                                raise_on_error=False,
                            )
                            if poweroff_state == vim.TaskInfo.State.success:
                                print("   [OK] Power-off completed.")
                            else:
                                print(f"   [WARN] Power-off failed: {task.info.error.msg}. Continuing with deletion.")
                        print(f"   Deleting VM '{vm_to_delete.name}'...")
                        destroy_task = vm_to_delete.Destroy_Task()
                        destroy_state = wait_for_task_completion(
                            destroy_task,
                            "VM destruction task",
                            poll_interval=2.0,
                            timeout=900.0,
                            raise_on_error=False,
                        )
                        if destroy_state == vim.TaskInfo.State.success:
                            print("[OK] Rollback complete: deleted destination VM.")
                            unregistered_from_source = False
                            dest_vm_retained = False
                        else:
                            unregistered_from_source = True
                            dest_vm_retained = True
                            raise RuntimeError(f"Failed to delete VM: {destroy_task.info.error.msg}") from error
                except Exception as cleanup_error:
                    print(f"[WARN] Error during destination VM rollback: {cleanup_error}")
                    unregistered_from_source = True
                    dest_vm_retained = True
            else:
                print("Rollback was cancelled; the VM remains on the destination vCenter.")
                dest_vm_retained = True
        if unregistered_from_source and not dest_vm_retained:
            print("\n" + "=" * 20 + " Rollback Confirmation (Datastore Cleanup) " + "=" * 20)
            print("   Clone files remain on the source vCenter datastore and must be cleaned up.")
            print(f"   VM files may still exist on datastore '{TARGET_DATASTORE_NAME}'.")

            rollback_approval_files = input("\nConnect to the source vCenter and delete these files? (y/n): ")
            if rollback_approval_files.lower() == 'y':
                si_source_cleanup = None
                try:
                    print("\nApproved. Reconnecting to the source vCenter for cleanup...")
                    si_source_cleanup = authenticate_vcenter(
                        VCSA_HOST_SOURCE,
                        VCSA_USER,
                        VCSA_PWD_SOURCE,
                        ssl_context,
                        host_env_var=ENV_SOURCE_HOST_VAR,
                    )
                    print("   [OK] Reconnected successfully.")

                    content_cleanup = si_source_cleanup.RetrieveContent()
                    file_manager = content_cleanup.fileManager
                    vm_dir_path = os.path.dirname(vmx_path)
                    print(f"   Deleting directory '{vm_dir_path}' from the datastore...")
                    datacenter = content_cleanup.rootFolder.childEntity[0]
                    delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)
                    delete_state = wait_for_task_completion(
                        delete_task,
                        "Datastore cleanup task",
                        poll_interval=2.0,
                        timeout=900.0,
                        raise_on_error=False,
                    )
                    if delete_state == vim.TaskInfo.State.success:
                        print("[OK] Rollback complete: removed files from the datastore.")
                    else:
                        raise RuntimeError(f"Failed to delete files from datastore: {delete_task.info.error.msg}") from error
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
                    poweroff_state = wait_for_task_completion(
                        poweroff_task,
                        "Power-off task",
                        poll_interval=2.0,
                        timeout=600.0,
                        raise_on_error=False,
                    )
                    if poweroff_state == vim.TaskInfo.State.success:
                        print("   [OK] Power-off completed.")
                    else:
                        print(f"   [WARN] Power-off failed: {poweroff_task.info.error.msg}. Continuing with deletion.")
                task = new_vm_on_source.Destroy_Task()
                destroy_state = wait_for_task_completion(
                    task,
                    "Source VM destruction task",
                    poll_interval=2.0,
                    timeout=900.0,
                    raise_on_error=False,
                )
                if destroy_state == vim.TaskInfo.State.success:
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
        dest_keepalive_stopped = True
        try:
            if 'dest_keepalive_handle' in locals():
                dest_keepalive_stopped = _stop_keepalive_thread(dest_keepalive_handle)
        except Exception:
            dest_keepalive_stopped = True
            LOGGER.debug("Error while stopping destination keep-alive thread", exc_info=True)
        try:
            if 'si_dest' in locals() and si_dest:
                if dest_keepalive_stopped:
                    Disconnect(si_dest)
                else:
                    LOGGER.warning(
                        "Skipping disconnect from destination vCenter because keep-alive thread is still running."
                    )
        except Exception:
            LOGGER.debug("Error while disconnecting from destination vCenter", exc_info=True)
        source_keepalive_stopped = True
        try:
            if 'source_keepalive_handle' in locals():
                source_keepalive_stopped = _stop_keepalive_thread(source_keepalive_handle)
        except Exception:
            source_keepalive_stopped = True
            LOGGER.debug("Error while stopping source keep-alive thread", exc_info=True)
        try:
            if 'si_source' in locals() and si_source:
                if source_keepalive_stopped:
                    Disconnect(si_source)
                else:
                    LOGGER.warning(
                        "Skipping disconnect from source vCenter because keep-alive thread is still running."
                    )
        except Exception:
            LOGGER.debug("Error while disconnecting from source vCenter", exc_info=True)
        if cancellation_requested:
            final_message = cancellation_message or "Processing cancelled by the user."
        elif fatal_error is not None:
            final_message = "Processing finished with errors."
        else:
            final_message = "Processing finished."
        print(final_message)
        _print_execution_summary()

if __name__ == "__main__":
    main()
