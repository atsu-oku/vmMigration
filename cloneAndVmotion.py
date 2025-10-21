# -*- coding: utf-8 -*-
import os
import ssl
import getpass
import time
import threading
import logging
import ipaddress
import urllib.request
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
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
from vsphere_sdk_network import (
    VsphereGuestNetworkSDK,
    IPv4Config,
    DnsConfig,
    RouteConfig,
)
from guest_commands import (
    execute_command_in_guest,
    NmcliNotAvailableError,
    reset_root_login_disabled,
)
from network_utils import (
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
)


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


def authenticate_vcenter(host: str, user: str, password: str, ssl_ctx: ssl.SSLContext):
    """Establish a SmartConnect session to the specified vCenter and return the service instance."""
    session = SmartConnect(host=host, user=user, pwd=password, port=VCSA_PORT, sslContext=ssl_ctx)
    if not session:
        raise ConnectionError(f"Failed to authenticate to vCenter ({host}).")
    return session

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




@dataclass
class WorkflowState:
    clone_name: Optional[str] = None
    vmx_path: Optional[str] = None
    new_vm_on_source: Optional[Any] = None
    migrated_vm_for_rollback: Optional[Any] = None
    migrated_vm_name_for_rollback: Optional[str] = None
    migrated_vm: Optional[Any] = None
    unregistered_from_source: bool = False
    original_nic_info: List[Dict[str, Any]] = field(default_factory=list)
    original_dns_servers: List[str] = field(default_factory=list)
    original_default_gateway: Optional[str] = None
    original_static_routes: List[Dict[str, Any]] = field(default_factory=list)
    prd_static_routes: List[Dict[str, Any]] = field(default_factory=list)
    sdk_network_client: Optional[VsphereGuestNetworkSDK] = None
    source_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    dest_keepalive_handle: Optional[Tuple[threading.Thread, threading.Event]] = None
    target_datastore: Optional[Any] = None
    target_folder: Optional[Any] = None


class CloneAndVmotionWorkflow:
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
        print("\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---")
        print(f"   Attempting to connect to source vCenter ({VCSA_HOST_SOURCE})...")
        si_source = authenticate_vcenter(VCSA_HOST_SOURCE, VCSA_USER, self.vcsa_pwd_source, self.ctx)
        print("   [OK] Source vCenter authentication succeeded.")
        Disconnect(si_source)

        print(f"   Attempting to connect to destination vCenter ({VCSA_HOST_DEST})...")
        si_dest = authenticate_vcenter(VCSA_HOST_DEST, VCSA_USER, self.vcsa_pwd_dest, self.ctx)
        print("   [OK] Destination vCenter authentication succeeded.")
        Disconnect(si_dest)

    def _collect_source_vm_details(self) -> None:
        print("\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
        self.si_source = authenticate_vcenter(VCSA_HOST_SOURCE, VCSA_USER, self.vcsa_pwd_source, self.ctx)
        print("[OK] Connected to source vCenter.")
        self.state.source_keepalive_handle = _start_keepalive_thread(self.si_source, "source-vcenter")

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

        if self.target_vm.guest.toolsRunningStatus != "guestToolsRunning":
            raise SystemError("Source VM must be powered on with VMware Tools running to collect IP information.")

        sdk_interfaces_by_mac: Dict[str, Tuple[Dict[str, Any], int]] = {}
        sdk_vm_id_source = getattr(self.target_vm, "_moId", None)
        if sdk_vm_id_source and REQUESTS_AVAILABLE:
            try:
                sdk_source_client = VsphereGuestNetworkSDK(
                    host=VCSA_HOST_SOURCE,
                    username=VCSA_USER,
                    password=self.vcsa_pwd_source,
                    verify_ssl=False,
                )
                interfaces = sdk_source_client.list_interfaces(sdk_vm_id_source)
                networking_state = sdk_source_client.get_networking_state(sdk_vm_id_source)
                routes = sdk_source_client.list_routes(sdk_vm_id_source)
            except Exception as sdk_error:  # pylint: disable=broad-exception-caught
                LOGGER.warning("Failed to collect source VM network info via API: %s", sdk_error)
                interfaces = []
                networking_state = {}
                routes = []
            finally:
                if 'sdk_source_client' in locals():
                    try:
                        sdk_source_client.close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
            for idx, iface in enumerate(interfaces or []):
                mac_candidate = (iface.get("mac_address") or iface.get("mac") or "").lower()
                if mac_candidate:
                    sdk_interfaces_by_mac[mac_candidate] = (iface, idx)
        else:
            networking_state = {}
            routes = []

        guest_net_map = {nic.macAddress: nic for nic in self.target_vm.guest.net if nic.macAddress}
        original_nic_info: List[Dict[str, Any]] = []
        missing_ipv4_messages: List[str] = []

        for device in self.target_vm.config.hardware.device:
            if not isinstance(device, vim.vm.device.VirtualEthernetCard):
                continue
            mac_address = device.macAddress
            nic_info: Dict[str, Any] = {
                "mac_address": mac_address,
                "label": getattr(device.deviceInfo, "label", ""),
                "network_name": getattr(device.backing, "network", None).name if getattr(device.backing, "network", None) else "",
                "device_key": device.key,
                "device_type": type(device),
            }
            if mac_address in guest_net_map:
                guest_nic = guest_net_map[mac_address]
                if guest_nic.ipConfig and guest_nic.ipConfig.ipAddress:
                    for ip_entry in guest_nic.ipConfig.ipAddress:
                        if ':' in ip_entry.ipAddress:
                            continue
                        nic_info["ip_address"] = ip_entry.ipAddress
                        nic_info["prefix"] = ip_entry.prefixLength
                        nic_info["subnet_mask"] = prefix_to_subnet_mask(ip_entry.prefixLength)
                        break
                    else:
                        missing_ipv4_messages.append(mac_address)
                else:
                    missing_ipv4_messages.append(mac_address)
            if mac_address.lower() in sdk_interfaces_by_mac:
                iface_info, iface_idx = sdk_interfaces_by_mac[mac_address.lower()]
                nic_info["sdk_interface_index"] = iface_idx
                nic_info["sdk_interface"] = iface_info
            original_nic_info.append(nic_info)

        if missing_ipv4_messages:
            LOGGER.debug("NICs without IPv4 info from guest tools: %s", missing_ipv4_messages)

        default_gateway = None
        static_routes = []
        dns_servers = []

        if networking_state:
            dns_config = (networking_state.get("dns") or {}).get("ip_addresses")
            if dns_config:
                dns_servers = [dns for dns in dns_config if dns and not str(dns).startswith("127.")]
            ip_stack = networking_state.get("ip_stack") or {}
            ipv4 = (ip_stack.get("ipv4") or {})
            default_gateway = ipv4.get("default_gateway")
            static_routes = ipv4.get("routes") or []
        elif self.target_vm.guest.ipStack:
            stack = self.target_vm.guest.ipStack[0]
            if stack.dnsConfig:
                dns_servers = [dns for dns in stack.dnsConfig.ipAddress if not dns.startswith("127.")]
            if stack.route:
                static_routes = [
                    {
                        "network": route.network,
                        "prefix": getattr(route, "prefixLength", None),
                        "gateway": route.gateway.ipAddress if route.gateway else None,
                        "owner_index": getattr(route, "owner", None),
                    }
                    for route in stack.route
                ]
            if stack.ipRouteConfig and stack.ipRouteConfig.defaultGateway:
                default_gateway = stack.ipRouteConfig.defaultGateway.ipAddress

        self.state.original_nic_info = original_nic_info
        self.state.original_dns_servers = dns_servers
        self.state.original_default_gateway = default_gateway
        self.state.original_static_routes = static_routes

    def _confirm_clone_plan(self) -> None:
        clone_name = self.state.clone_name or f"{self.target_vm.name}-{datetime.now().strftime('%Y%m%d')}"
        self.state.clone_name = clone_name

        print("\n" + "=" * 25 + " Pre-execution Check (1/4) " + "=" * 25)
        print("Review the details below before creating the clone and starting the migration.")
        print("\n  [Source VM details]")
        print(f"    - VM name       : {self.target_vm.name}")
        print(f"    - OS            : {self.target_vm.summary.config.guestFullName}")
        print("\n  [Source NIC details]")
        if self.state.original_nic_info:
            for i, nic in enumerate(self.state.original_nic_info):
                print(f"    - NIC {i+1} ({nic['mac_address']})")
                print(f"      - Network     : {nic['network_name']}")
                print(f"      - IP Address  : {nic.get('ip_address', '(unknown)')}")
                print(f"      - Subnet Mask : {nic.get('subnet_mask', '(unknown)')}")
        else:
            print("    - No NIC information was found.")
        if self.state.original_default_gateway:
            print(f"    - Gateway     : {self.state.original_default_gateway}")
        else:
            print("    - Default gateway not detected.")
        print("\n  [Clone VM specification]")
        print(f"    - New VM name   : {clone_name}")
        print(f"    - Placement datastore: {TARGET_DATASTORE_NAME}")
        print("=" * 64)

        user_approval = input("\nProceed with this clone operation? (y/n): ")
        if user_approval.lower() != 'y':
            raise InterruptedError("Operation cancelled by the user.")

    def _perform_source_clone_operations(self) -> None:
        if not self.si_source:
            raise RuntimeError("Source vCenter connection is not available.")

        target_datastore = self.state.target_datastore
        if not target_datastore:
            raise RuntimeError("Target datastore information is missing.")

        clone_name = self.state.clone_name
        target_folder = self.state.target_folder or getattr(self.target_vm, 'parent', None)

        relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
        clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)

        print("\nStarting clone task...")
        task = self.target_vm.Clone(folder=target_folder, name=clone_name, spec=clone_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            progress = task.info.progress or 0
            print(f"   Clone progress: {progress}%", end='\r')
            time.sleep(5)
        print(" " * 40, end='\r')
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"Clone task failed: {task.info.error.msg}")
        print(f"\n[OK] Clone completed: '{clone_name}'")

        new_vm_on_source = task.info.result
        self.state.new_vm_on_source = new_vm_on_source

        print(f"   Removing NICs from cloned VM '{new_vm_on_source.name}'...")
        nic_devices = [
            dev for dev in new_vm_on_source.config.hardware.device
            if isinstance(dev, vim.vm.device.VirtualEthernetCard)
        ]
        if nic_devices:
            nic_change_spec = [
                vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic)
                for nic in nic_devices
            ]
            config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
            task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"NIC removal failed: {task.info.error.msg}")
            print("   [OK] Removed NICs.")
        else:
            print("   - No NICs found on cloned VM; skipping removal.")

        self.state.vmx_path = new_vm_on_source.config.files.vmPathName
        print(f"   Unregistering VM '{clone_name}' from the source vCenter...")
        new_vm_on_source.UnregisterVM()
        self.state.unregistered_from_source = True
        print("   [OK] Unregistration completed.")

        _stop_keepalive_thread(self.state.source_keepalive_handle)
        self.state.source_keepalive_handle = None
        Disconnect(self.si_source)
        self.si_source = None
        self.content_source = None
        self.state.new_vm_on_source = None

    def _register_destination_vm(self) -> None:
        raise NotImplementedError

    def _recreate_destination_nics(self) -> None:
        raise NotImplementedError

    def _configure_destination_network(self) -> None:
        raise NotImplementedError

    def _perform_storage_vmotion(self) -> None:
        raise NotImplementedError

    def _finalize_success(self) -> None:
        raise NotImplementedError

    def _handle_error(self, error: Exception) -> None:
        raise NotImplementedError

    def _cleanup(self) -> None:
        raise NotImplementedError

# ------------------------------------------------
# 1. Enter passwords
# ------------------------------------------------
try:
    VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
    VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
except Exception as error:
    print('ERROR:', error)
    exit(1)
reset_root_login_disabled()

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
workflow = CloneAndVmotionWorkflow(
    ctx=ctx,
    vcsa_pwd_source=VCSA_PWD_SOURCE,
    vcsa_pwd_dest=VCSA_PWD_DEST,
    target_vm_name=target_vm_name,
    guest_root_pwd=GUEST_ROOT_PWD,
    guest_admin_pwd=GUEST_ADMIN_PWD,
)

workflow.run()
