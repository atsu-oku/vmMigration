# -*- coding: cp932 -*-
import ssl
import getpass
import time
from datetime import datetime
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from pyVmomi.vim import fault as vim_fault
import os
import json
import ipaddress

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ------------------------------------------------
# 謗･邯壽ュ蝣ｱ縺翫ｈ縺ｳ遘ｻ陦悟・險ｭ螳・
# ------------------------------------------------
# --- 繧ｽ繝ｼ繧ｹvCenter ---
VCSA_HOST_SOURCE = 'vcsa01s.ipet.local'
VCSA_USER = 'administrator@vsphere.local'
VCSA_PORT = 443

# --- 螳帛・vCenter ---
VCSA_HOST_DEST = 'vcsa01p.ipet.local'

# --- 遘ｻ陦後Μ繧ｽ繝ｼ繧ｹ ---
# 繧ｯ繝ｭ繝ｼ繝ｳ蜈医・蜈ｱ譛峨ョ繝ｼ繧ｿ繧ｹ繝医い
TARGET_DATASTORE_NAME = 'PMAX-COM-VOL1'
# 譛邨ら噪縺ｪ遘ｻ陦悟・繝・・繧ｿ繧ｹ繝医い
TARGET_DATASTORE_NAME_FINAL = 'PMAX-PRD-VOL1'
# 繧ｳ繝ｳ繝斐Η繝ｼ繝・ぅ繝ｳ繧ｰ繝ｪ繧ｽ繝ｼ繧ｹ縺ｮ遘ｻ陦悟・繧ｯ繝ｩ繧ｹ繧ｿ蜷・
TARGET_CLUSTER_NAME = 'PRD-Cluster' 

# --- 繧ｲ繧ｹ繝・S隱崎ｨｼ諠・ｱ ---
GUEST_ROOT_USER = 'root'
GUEST_ROOT_PWD = '' # 繧ｹ繧ｯ繝ｪ繝励ヨ螳溯｡梧凾縺ｫ蜈･蜉・
GUEST_ADMIN_USER = 'admin' # 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ逕ｨ繝ｦ繝ｼ繧ｶ繝ｼ
GUEST_ADMIN_PWD = '' # 繧ｹ繧ｯ繝ｪ繝励ヨ螳溯｡梧凾縺ｫ蜈･蜉・

# ------------------------------------------------
# Helper Functions
# ------------------------------------------------
def prefix_to_subnet_mask(prefix_length):
    """CIDRプレフィックス長をサブネットマスクに変換する"""
    if not isinstance(prefix_length, int) or not (0 <= prefix_length <= 32):
        return None
    host_bits = 32 - prefix_length
    netmask = (1 << 32) - (1 << host_bits)
    return '.'.join([str((netmask >> i) & 0xff) for i in [24, 16, 8, 0]])

def calculate_ip_stg_to_prd(ip_address):
    """STG→PRD向けに第三オクテット170?179を160?169へ写像する。

    - 入力が空なら None を返す
    - IPv4形式と各オクテットの範囲(0?255)を検証
    - 第三オクテットが 170?179 なら 10 減算して返す
    - それ以外は前提外として ValueError を送出
    """

    - 入力が空なら None を返す
    - IPv4形式と各オクテットの範囲(0?255)を検証
    - 第三オクテットが 170?179 なら 10 減算して返す
    - それ以外は前提外として ValueError を送出
    """
    if not ip_address:
        return None
    parts = ip_address.split('.')
    if len(parts) != 4:
        raise ValueError(f"IPv4蠖｢蠑上〒縺ｯ縺ゅｊ縺ｾ縺帙ｓ: {ip_address}")
    try:
        octets = [int(x) for x in parts]
    except ValueError as e:
        raise ValueError(f"謨ｰ蛟､蛹悶↓螟ｱ謨励＠縺ｾ縺励◆: {ip_address}") from e
    if any(o < 0 or o > 255 for o in octets):
        raise ValueError(f"蜷・が繧ｯ繝・ャ繝医・0?255縺ｮ遽・峇縺ｧ縺ゅｋ蠢・ｦ√′縺ゅｊ縺ｾ縺・ {ip_address}")
    if 170 <= octets[2] <= 179:
        octets[2] = octets[2] - 10
        return '.'.join(str(o) for o in octets)
    raise ValueError(f"隨ｬ荳峨が繧ｯ繝・ャ繝・{octets[2]} 縺ｯ諠ｳ螳壼､悶〒縺・譛溷ｾ・ 170?179)縲ょ・蜉・ {ip_address}")

def execute_command_in_guest(guest_op_manager, vm, root_auth, admin_auth, guest_admin_password, command, check_exit_code=True):
    """Run a guest command, falling back from root to admin(sudo).

    Returns (exit_code, stdout, stderr) and emits verbose logs for operators.
    """
    process_manager = guest_op_manager.processManager
    file_manager = guest_op_manager.fileManager
    stdout_path = f"/tmp/stdout_{os.urandom(4).hex()}.log"
    stderr_path = f"/tmp/stderr_{os.urandom(4).hex()}.log"

    def _run_it(auth, cmd):
        escaped_cmd = cmd.replace("'", "'\\''")
        wrapped_cmd = f"'{escaped_cmd}' > {stdout_path} 2> {stderr_path}"
        spec = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/bash", arguments=f"-c {wrapped_cmd}")
        _process_pid = process_manager.StartProgramInGuest(vm=vm, auth=auth, spec=spec)

        exit_code = -1
        start_time = time.time()
        while time.time() - start_time < 300:
            procs = process_manager.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid])
            if procs and procs[0].exitCode is not None:
                exit_code = procs[0].exitCode
                break
            time.sleep(2)

        stdout_content, stderr_content = "", ""
        if REQUESTS_AVAILABLE:
            for fpath, content_var in [(stdout_path, "stdout_content"), (stderr_path, "stderr_content")]:
                try:
                    fi = file_manager.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=fpath)
                    resp = requests.get(fi.url, verify=False)
                    if resp.status_code == 200:
                        if content_var == "stdout_content":
                            stdout_content = resp.text
                        else:
                            stderr_content = resp.text
                except (vim_fault.FileNotFound, requests.exceptions.RequestException):
                    pass
                finally:
                    try:
                        file_manager.DeleteFileInGuest(vm=vm, auth=auth, filePath=fpath)
                    except (vim_fault.FileNotFound, vim_fault.GuestOperationsFault):
                        pass

                return exit_code, stdout_content.strip(), stderr_content.strip(), stdout_content.strip(), stderr_content.strip()

    print("[GUEST-CMD] will run:")
    print(f"  {command}")

    exit_code, stdout, stderr = -1, "", ""
    auth_used = None

    try:
        auth_used = "root"
        exit_code, stdout, stderr = _run_it(root_auth, command)
    except vim_fault.InvalidGuestLogin as exc:
        print("[GUEST-CMD] root auth failed -> fallback to admin(sudo)")
        auth_used = "admin"
        sudo_command = f"echo '{guest_admin_password}' | sudo -S {command}"
        exit_code, stdout, stderr = _run_it(admin_auth, sudo_command)
        if exit_code != 0 and check_exit_code:
            print(f"[GUEST-CMD] user: {auth_used}")
            print(f"[GUEST-CMD] exit: {exit_code}")
            print("[GUEST-CMD] stdout:\n---\n" + (stdout or "(none)") + "\n---")
            print("[GUEST-CMD] stderr:\n---\n" + (stderr or "(none)") + "\n---")
            print("[GUEST-CMD] result: FAIL (reason: admin execution failed)")
            raise RuntimeError(f"admin sudo failed (exit={exit_code})") from exc

    print(f"[GUEST-CMD] user: {auth_used}")
    print(f"[GUEST-CMD] exit: {exit_code}")
    print("[GUEST-CMD] stdout:\n---\n" + (stdout or "(none)") + "\n---")
    print("[GUEST-CMD] stderr:\n---\n" + (stderr or "(none)") + "\n---")

    if exit_code == 0:
        print("[GUEST-CMD] result: OK")
    else:
        print("[GUEST-CMD] result: FAIL")
        if check_exit_code:
            reason = (stderr or '').strip() or 'non-zero exit code'
            raise RuntimeError(f"guest command failed (exit={exit_code}, reason={reason})")

    return exit_code, stdout, stderr

# ------------------------------------------------

def main():
    # Local state flags and setup
    unregistered_from_source = False
    # 1. 繝代せ繝ｯ繝ｼ繝牙・蜉・
    # ------------------------------------------------
    try:
        VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
        VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
    except Exception as error:
        print('ERROR:', error)
        exit(1)

    # ------------------------------------------------
    # 2. SSL繧ｳ繝ｳ繝・く繧ｹ繝医・險ｭ螳・
    # ------------------------------------------------
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------
    # 3. 謫堺ｽ懷ｯｾ雎｡縺ｮVM蜷阪→繧ｲ繧ｹ繝・S隱崎ｨｼ諠・ｱ繧貞・蜉・
    # ------------------------------------------------
    target_vm_name = input("繧ｯ繝ｭ繝ｼ繝ｳ繧剃ｽ懈・縺励◆縺ХM縺ｮ蜷榊燕繧貞・蜉帙＠縺ｦ縺上□縺輔＞: ")
    if not target_vm_name:
        print("VM蜷阪′蜈･蜉帙＆繧後∪縺帙ｓ縺ｧ縺励◆縲ょ・逅・ｒ邨ゆｺ・＠縺ｾ縺吶・)
        exit(0)
    try:
        GUEST_ROOT_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ROOT_USER}': ")
        GUEST_ADMIN_PWD = getpass.getpass(f"Password for Guest OS user '{GUEST_ADMIN_USER}' (for fallback): ")
    except Exception as error:
        print('ERROR:', error)
        exit(1)


    # ------------------------------------------------
    # 繝｡繧､繝ｳ蜃ｦ逅・
    # ------------------------------------------------
    clone_name = None
    vmx_path = None
    new_vm_on_source = None
    migrated_vm_for_rollback = None 
    unregistered_from_source = False
    original_nic_info = []
    original_dns_servers = []
    original_default_gateway = None 
    si_source = None
    si_dest = None

    try:
        # --- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---
        print("\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---")
        print(f"   繧ｽ繝ｼ繧ｹvCenter ({VCSA_HOST_SOURCE}) 縺ｫ謗･邯壹ｒ隧ｦ縺ｿ縺ｦ縺・∪縺・..")
        si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
        if not si_source: raise ConnectionError(f"繧ｽ繝ｼ繧ｹvCenter ({VCSA_HOST_SOURCE}) 縺ｸ縺ｮ隱崎ｨｼ縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
        print("   ? 繧ｽ繝ｼ繧ｹvCenter隱崎ｨｼ謌仙粥縲・)
        Disconnect(si_source)
        si_source = None

        print(f"   螳帛・vCenter ({VCSA_HOST_DEST}) 縺ｫ謗･邯壹ｒ隧ｦ縺ｿ縺ｦ縺・∪縺・..")
        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
        if not si_dest: raise ConnectionError(f"螳帛・vCenter ({VCSA_HOST_DEST}) 縺ｸ縺ｮ隱崎ｨｼ縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
        print("   ? 螳帛・vCenter隱崎ｨｼ謌仙粥縲・)
        Disconnect(si_dest)
        si_dest = None
        
        # --- [Phase 1/7] Source vCenter: Collect Info & Prepare ---
        print(f"\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
        si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
        if not si_source: raise ConnectionError(f"繧ｽ繝ｼ繧ｹvCenter ({VCSA_HOST_SOURCE}) 縺ｫ謗･邯壹〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)
        print("? 謗･邯壽・蜉・)
        
        content_source = si_source.RetrieveContent()
        
        target_vm = next((vm for vm in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.VirtualMachine], True).view if vm.name == target_vm_name), None)
        if not target_vm: raise FileNotFoundError(f"VM '{target_vm_name}' 縺ｯ隕九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        print(f"? VM '{target_vm.name}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺励◆縲・)

        if not target_vm.guest.toolsRunningStatus == 'guestToolsRunning':
            raise SystemError("IP繧｢繝峨Ξ繧ｹ蜿門ｾ励・縺溘ａ縲√た繝ｼ繧ｹVM縺ｮ髮ｻ貅舌′ON縺ｧ縺ゅｊ縲〃Mware Tools縺悟ｮ溯｡御ｸｭ縺ｧ縺ゅｋ蠢・ｦ√′縺ゅｊ縺ｾ縺吶・)
        print("   VMware Tools螳溯｡御ｸｭ繧堤｢ｺ隱阪＠縺ｾ縺励◆縲・)

        print("   繧ｯ繝ｭ繝ｼ繝ｳ蜈・・NIC諠・ｱ繧貞庶髮・ｸｭ...")
        guest_net_map = {nic.macAddress: nic for nic in target_vm.guest.net if nic.macAddress}
        for device in target_vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                mac = device.macAddress
                guest_nic = guest_net_map.get(mac)
                if guest_nic and guest_nic.ipConfig:
                    ip_v4_info = next((ip for ip in guest_nic.ipConfig.ipAddress if '.' in ip.ipAddress), None)
                    if ip_v4_info:
                        subnet_mask = prefix_to_subnet_mask(ip_v4_info.prefixLength)
                        network_name = None
                        if isinstance(device.backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                            network_name = device.backing.network.name
                        elif isinstance(device.backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                            network_name = guest_nic.network
                        
                        if network_name:
                            original_nic_info.append({
                                'device_type': type(device),
                                'mac_address': mac,
                                'network_name': network_name,
                                'ip_address': ip_v4_info.ipAddress,
                                'subnet_mask': subnet_mask,
                                'is_gateway_nic': False # 繝・ヵ繧ｩ繝ｫ繝医・False
                            })

        if target_vm.guest.ipStack and target_vm.guest.ipStack[0].ipRouteConfig:
            for route in target_vm.guest.ipStack[0].ipRouteConfig.ipRoute:
                if route.network == '0.0.0.0' and route.prefixLength == 0:
                    original_default_gateway = route.gateway.ipAddress
                    print(f"   繝・ヵ繧ｩ繝ｫ繝医ご繝ｼ繝医え繧ｧ繧､ '{original_default_gateway}' 繧貞叙蠕励＠縺ｾ縺励◆縲・)
                    # 繧ｲ繝ｼ繝医え繧ｧ繧､縺後←縺ｮNIC縺ｫ螻槭☆繧九°繧貞愛螳・
                    for nic in original_nic_info:
                        try:
                            nic_iface = ipaddress.IPv4Interface(f"{nic['ip_address']}/{nic['subnet_mask']}")
                            gw_addr = ipaddress.IPv4Address(original_default_gateway)
                            if gw_addr in nic_iface.network:
                                nic['is_gateway_nic'] = True
                                print(f"   -> NIC with IP {nic['ip_address']} 縺後ご繝ｼ繝医え繧ｧ繧､NIC縺ｨ蛻､螳壹＆繧後∪縺励◆縲・)
                                break
                        except (ValueError, ipaddress.AddressValueError):
                            continue
                    break
        
        if target_vm.guest.ipStack and target_vm.guest.ipStack[0].dnsConfig:
            original_dns_servers = [dns for dns in target_vm.guest.ipStack[0].dnsConfig.ipAddress if not dns.startswith('127.')]

        print(f"   ? {len(original_nic_info)}蛟九・NIC縺九ｉIP讒区・諠・ｱ繧貞庶髮・＠縺ｾ縺励◆縲・)

        target_datastore = next((ds for ds in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME), None)
        if not target_datastore: raise FileNotFoundError(f"繝・・繧ｿ繧ｹ繝医い '{TARGET_DATASTORE_NAME}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        print(f"? 繝・・繧ｿ繧ｹ繝医い '{target_datastore.name}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺励◆縲・)

        date_suffix = datetime.now().strftime('%Y%m%d')
        clone_name = f"{target_vm.name}-{date_suffix}"
        
        # --- 繧ｯ繝ｭ繝ｼ繝ｳ謫堺ｽ懊・謇ｿ隱・(1/4) ---
        print("\n" + "="*25 + " 謫・菴・遒ｺ 隱・(1/4) " + "="*25)
        print("莉･荳九・VM縺ｮ繧ｯ繝ｭ繝ｼ繝ｳ繧剃ｽ懈・縺励∫ｧｻ陦後ｒ髢句ｧ九＠縺ｾ縺吶ょ・螳ｹ繧偵ｈ縺上＃遒ｺ隱阪￥縺縺輔＞縲・)
        print(f"\n  [繧ｯ繝ｭ繝ｼ繝ｳ蜈シM縺ｮ諠・ｱ]")
        print(f"    - VM蜷・         : {target_vm.name}")
        print(f"    - OS蜷・         : {target_vm.summary.config.guestFullName}")
        print("\n  [繧ｯ繝ｭ繝ｼ繝ｳ蜈クIC縺ｮ諠・ｱ]")
        if original_nic_info:
            for i, nic in enumerate(original_nic_info):
                print(f"    - NIC {i+1} ({nic['mac_address']})")
                print(f"      - Network     : {nic['network_name']}")
                print(f"      - IP Address  : {nic['ip_address']}")
                print(f"      - Subnet Mask : {nic['subnet_mask']}")
        else: print("    - NIC諠・ｱ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        if original_default_gateway:
            print(f"    - Gateway     : {original_default_gateway}")
        else:
            print("    - 繝・ヵ繧ｩ繝ｫ繝医ご繝ｼ繝医え繧ｧ繧､縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)

        print("\n  [繧ｯ繝ｭ繝ｼ繝ｳ蜈・M縺ｮ莉墓ｧ肋")
        print(f"    - 譁ｰ縺励＞VM蜷・   : {clone_name}")
        print(f"    - 驟咲ｽｮ繝・・繧ｿ繧ｹ繝医い: {TARGET_DATASTORE_NAME}")
        print("=" * 64)

        user_approval = input(f"\n縺薙・繧ｯ繝ｭ繝ｼ繝ｳ謫堺ｽ懊ｒ螳溯｡後＠縺ｦ繧ゅｈ繧阪＠縺・〒縺吶°・・(y/n): ")
        if user_approval.lower() != 'y': raise InterruptedError("繝ｦ繝ｼ繧ｶ繝ｼ縺ｫ繧医▲縺ｦ謫堺ｽ懊′繧ｭ繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲・)
        
        # --- 繧ｯ繝ｭ繝ｼ繝ｳ縲¨IC蜑企勁縲∫匳骭ｲ隗｣髯､ ---
        relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
        clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
        print("\n繧ｯ繝ｭ繝ｼ繝ｳ菴懈・繧ｿ繧ｹ繧ｯ繧帝幕蟋九＠縺ｾ縺励◆...")
        task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: progress = task.info.progress or 0; print(f"   繧ｯ繝ｭ繝ｼ繝ｳ菴懈・縺ｮ騾ｲ謐・ {progress}%", end='\r'); time.sleep(5)
        print(" " * 40, end='\r')
        if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"繧ｯ繝ｭ繝ｼ繝ｳ菴懈・繧ｨ繝ｩ繝ｼ: {task.info.error.msg}")
        print(f"\n? 繧ｯ繝ｭ繝ｼ繝ｳ菴懈・謌仙粥: '{clone_name}'")
        
        new_vm_on_source = task.info.result
        # NIC蜑企勁蜃ｦ逅・
        print(f"   繧ｯ繝ｭ繝ｼ繝ｳ縺励◆VM '{new_vm_on_source.name}' 縺ｮNIC繧貞炎髯､縺励∪縺・..")
        nic_devices_to_remove = [dev for dev in new_vm_on_source.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        if nic_devices_to_remove:
            nic_change_spec = [vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic) for nic in nic_devices_to_remove]
            config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
            task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"NIC蜑企勁繧ｨ繝ｩ繝ｼ: {task.info.error.msg}")
            print("   ? NIC蜑企勁謌仙粥")
        
        vmx_path = new_vm_on_source.config.files.vmPathName
        print(f"   VM '{clone_name}' 繧偵た繝ｼ繧ｹvCenter縺九ｉ逋ｻ骭ｲ隗｣髯､縺励∪縺・..")
        new_vm_on_source.UnregisterVM()
        unregistered_from_source = True
        print("   ? 逋ｻ骭ｲ隗｣髯､謌仙粥")
        Disconnect(si_source)
        si_source = None
        new_vm_on_source = None 

        # --- [Phase 2/7] ~ [Phase 7/7]: 螳帛・vCenter縺ｧ縺ｮ蜃ｦ逅・---
        print(f"\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
        if not si_dest: raise ConnectionError(f"螳帛・vCenter ({VCSA_HOST_DEST}) 縺ｫ謗･邯壹〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)
        print("? 謗･邯壽・蜉・)
        content_dest = si_dest.RetrieveContent()
        if any(vm for vm in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name):
            raise FileExistsError(f"蜷悟錐縺ｮVM '{clone_name}' 縺悟ｮ帛・vCenter縺ｫ譌｢縺ｫ蟄伜惠縺励∪縺吶・)
        print(f"? 螳帛・vCenter縺ｫ蜷悟錐縺ｮVM縺ｯ蟄伜惠縺励∪縺帙ｓ縲・)

        print(f"\n--- [Phase 3/7] Destination vCenter: Register VM ---")
        dest_cluster = next((c for c in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.ClusterComputeResource], True).view if c.name == TARGET_CLUSTER_NAME), None)
        if not dest_cluster: raise FileNotFoundError(f"螳帛・繧ｯ繝ｩ繧ｹ繧ｿ '{TARGET_CLUSTER_NAME}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(5)
        if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"螳帛・vCenter縺ｧ縺ｮVM逋ｻ骭ｲ繧ｨ繝ｩ繝ｼ: {task.info.error.msg}")
        migrated_vm = task.info.result
        migrated_vm_for_rollback = migrated_vm # 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ逕ｨ縺ｫ菫晄戟
        print(f"? VM逋ｻ骭ｲ謌仙粥縲・)

        print(f"\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
        if original_nic_info:
            print("\n" + "="*25 + " 謫・菴・遒ｺ 隱・(2/4) " + "="*25)
            print("遘ｻ陦後＠縺欸M縺ｫNIC繧貞・菴懈・縺励∽ｻ･荳九・騾壹ｊ繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｫ謗･邯壹＠縺ｾ縺吶・)
            device_change_spec = []
            for i, nic in enumerate(original_nic_info):
                original_network_name = nic['network_name']
                dest_network_name = original_network_name.replace('STG', 'PRD', 1)
                print(f"  - NIC {i+1}: '{original_network_name}' 竊・'{dest_network_name}'")

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
                    raise FileNotFoundError(f"螳帛・繝阪ャ繝医Ρ繝ｼ繧ｯ '{dest_network_name}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)

                nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True)
                device_change_spec.append(nic_spec)
            
            print("=" * 64)
            user_approval_nic = input("\n縺薙・NIC險ｭ螳壹ｒ螳溯｡後＠縺ｦ繧ゅｈ繧阪＠縺・〒縺吶°・・(y/n): ")
            if user_approval_nic.lower() != 'y':
                raise InterruptedError("繝ｦ繝ｼ繧ｶ繝ｼ縺ｫ繧医▲縺ｦNIC險ｭ螳壹′繧ｭ繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲・)

            print("\n謇ｿ隱阪＆繧後∪縺励◆縲・IC縺ｮ蜀崎ｨｭ螳壹ち繧ｹ繧ｯ繧帝幕蟋九＠縺ｾ縺・..")
            config_spec = vim.vm.ConfigSpec(deviceChange=device_change_spec)
            task = migrated_vm.ReconfigVM_Task(spec=config_spec)
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"NIC縺ｮ蜀崎ｨｭ螳壹↓螟ｱ謨励＠縺ｾ縺励◆: {task.info.error.msg}")
            print("   ? NIC縺ｮ蜀崎ｨｭ螳壹′豁｣蟶ｸ縺ｫ螳御ｺ・＠縺ｾ縺励◆縲・)
            
            print("   譁ｰ縺励＞NIC縺ｮ諠・ｱ繧貞叙蠕嶺ｸｭ...")
            migrated_vm.Reload()
            newly_added_nics = [dev for dev in migrated_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
            if len(newly_added_nics) == len(original_nic_info):
                for i in range(len(original_nic_info)):
                     original_nic_info[i]['new_mac_address'] = newly_added_nics[i].macAddress
                print("   ? 譁ｰ縺励＞MAC繧｢繝峨Ξ繧ｹ縺ｮ髢｢騾｣莉倥￠縺悟ｮ御ｺ・＠縺ｾ縺励◆縲・)
            else:
                raise RuntimeError("NIC縺ｮ蜀堺ｽ懈・謨ｰ縺ｨ蜈・・NIC謨ｰ縺御ｸ閾ｴ縺励∪縺帙ｓ縲・)
        else:
            print("   - 蜈・・VM縺ｫNIC縺後↑縺九▲縺溘◆繧√¨IC縺ｮ蜀崎ｨｭ螳壹・繧ｹ繧ｭ繝・・縺輔ｌ縺ｾ縺励◆縲・)

        print(f"\n--- [Phase 5/7] Destination vCenter: Power On ---")
        print("\n" + "="*25 + " 謫・菴・遒ｺ 隱・(3/4) " + "="*25)
        print("VM繧偵ヱ繝ｯ繝ｼ繧ｪ繝ｳ縺励√ご繧ｹ繝・S縺ｮIP繧｢繝峨Ξ繧ｹ繧定ｨｭ螳壹＠縺ｾ縺吶・)
        if original_nic_info:
            new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
            for i, nic in enumerate(original_nic_info):
                new_ip = calculate_ip_stg_to_prd(nic['ip_address'])
                print(f"\n  - NIC {i+1} ({nic['new_mac_address']})")
                print(f"    - IP Address  : {nic['ip_address']} 竊・{new_ip}")
            if new_default_gateway:
                print(f"\n  [繝・ヵ繧ｩ繝ｫ繝医ご繝ｼ繝医え繧ｧ繧､縺ｮ險ｭ螳咯")
                print(f"    - Gateway     : {original_default_gateway} 竊・{new_default_gateway}")
            
            if original_dns_servers:
                print("\n  [DNS繧ｵ繝ｼ繝舌・縺ｮ險ｭ螳咯")
                new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
                for old_dns, new_dns in zip(original_dns_servers, new_dns_servers):
                    print(f"    - {old_dns} 竊・{new_dns}")
        else:
            print("  - NIC諠・ｱ縺後↑縺・◆繧√！P險ｭ螳壹・陦後ｏ繧後∪縺帙ｓ縲・)
        print("=" * 64)
        
        user_approval_ip = input("\n縺薙・IP險ｭ螳壹ｒ螳溯｡後＠縲〃M繧偵ヱ繝ｯ繝ｼ繧ｪ繝ｳ縺励∪縺吶°・・(y/n): ")
        if user_approval_ip.lower() != 'y': raise InterruptedError("繝ｦ繝ｼ繧ｶ繝ｼ縺ｫ繧医▲縺ｦIP險ｭ螳壹→繝代Ρ繝ｼ繧ｪ繝ｳ縺後く繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲・)

        print("\n謇ｿ隱阪＆繧後∪縺励◆縲７M繧偵ヱ繝ｯ繝ｼ繧ｪ繝ｳ縺励∪縺・..")
        task = migrated_vm.PowerOnVM_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"VM縺ｮ繝代Ρ繝ｼ繧ｪ繝ｳ縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {task.info.error.msg}")
        print("   ? VM縺ｯ豁｣蟶ｸ縺ｫ繝代Ρ繝ｼ繧ｪ繝ｳ縺輔ｌ縺ｾ縺励◆縲・)

        print("   繧ｲ繧ｹ繝・S謫堺ｽ懊お繝ｼ繧ｸ繧ｧ繝ｳ繝医・貅門ｙ繧貞ｾ・▲縺ｦ縺・∪縺・(譛螟ｧ5蛻・...")
        guest_op_manager = content_dest.guestOperationsManager
        agent_ready = False
        for i in range(10): 
            print(f"    - 隧ｦ陦・{i+1}/10...")
            try:
                creds_check = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
                process_manager = guest_op_manager.processManager
                spec_check = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/echo", arguments="ready")
                pid = process_manager.StartProgramInGuest(vm=migrated_vm, auth=creds_check, spec=spec_check)
                if pid >= 0:
                    agent_ready = True
                    break
            except vim_fault.InvalidGuestLogin:
                 agent_ready = True 
                 break
            except vim_fault.GuestOperationsUnavailable:
                if i < 9: 
                    time.sleep(30)
                continue 
            except Exception:
                if i < 9: 
                    time.sleep(30) 
                continue
        
        if not agent_ready:
            raise SystemError("繧ｿ繧､繝繧｢繧ｦ繝・ 繧ｲ繧ｹ繝・S謫堺ｽ懊お繝ｼ繧ｸ繧ｧ繝ｳ繝医′蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｫ縺ｪ繧翫∪縺帙ｓ縺ｧ縺励◆縲・)
        print("   ? 繧ｲ繧ｹ繝・S謫堺ｽ懊お繝ｼ繧ｸ繧ｧ繝ｳ繝域ｺ門ｙ螳御ｺ・・)

        print(f"\n--- [Phase 6/7] Destination vCenter: Set IP Address ---")
        
        if original_nic_info:
            root_auth = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
            admin_auth = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ADMIN_USER, password=GUEST_ADMIN_PWD)
            new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
            
            for i, nic_info in enumerate(original_nic_info):
                new_ip = calculate_ip_stg_to_prd(nic_info['ip_address'])
                subnet_mask_parts = nic_info['subnet_mask'].split('.')
                prefix = sum([bin(int(x)).count('1') for x in subnet_mask_parts])
                new_mac = nic_info['new_mac_address']
                
                con_name = f"prd-nic-{i}"
                
                print("\n" + "="*20 + f" NIC {i+1} 縺ｮ險ｭ螳・" + "="*20)
                
                # 1. Find device name using 'ip' command
                find_device_cmd = "ip -j -p addr"
                _, json_output, _ = execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, find_device_cmd, check_exit_code=False)
                
                device_name = ""
                if json_output:
                    try:
                        addr_data = json.loads(json_output)
                        for iface in addr_data:
                            if iface.get('address', '').lower() == new_mac.lower():
                                device_name = iface.get('ifname')
                                break
                    except json.JSONDecodeError as e:
                        raise RuntimeError(f"繧ｲ繧ｹ繝・S縺九ｉ縺ｮJSON蜃ｺ蜉帙・隗｣譫舌↓螟ｱ謨励＠縺ｾ縺励◆: {e}") from e

                if not device_name:
                    raise RuntimeError(f"MAC繧｢繝峨Ξ繧ｹ {new_mac} 縺ｫ蟇ｾ蠢懊☆繧九ョ繝舌う繧ｹ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
                print(f"   -> 繝・ヰ繧､繧ｹ '{device_name}' 繧堤音螳壹＠縺ｾ縺励◆縲・)
                
                # 2. Disconnect and delete existing connections
                execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli device disconnect {device_name} || true", check_exit_code=False)
                _, existing_conns, _ = execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli -g UUID,DEVICE connection show | grep -i {device_name} | cut -d: -f1", check_exit_code=False)
                if existing_conns:
                    for uuid in existing_conns.splitlines():
                        execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection delete uuid {uuid}")
                
                # 3. Add new connection and configure it
                execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection add type ethernet con-name '{con_name}' ifname '{device_name}'")
                execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection modify '{con_name}' ipv4.method manual ipv4.addresses '{new_ip}/{prefix}'")
                if nic_info.get('is_gateway_nic') and new_default_gateway:
                    execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection modify '{con_name}' ipv4.gateway '{new_default_gateway}'")
                
                if i == 0 and original_dns_servers: # Typically DNS is set on the primary NIC
                    new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
                    if new_dns_servers:
                        dns_str = ' '.join(new_dns_servers)
                        execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection modify '{con_name}' ipv4.dns '{dns_str}'")

                # 4. Bring up the new connection
                execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli connection up '{con_name}'")
                
                # 5. Final verification
                time.sleep(5)
                execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"ip addr show {device_name} | grep -q '{new_ip}'")
            
            print("   ? 蜈ｨ縺ｦ縺ｮNIC縺ｮIP險ｭ螳壹′螳御ｺ・＠縺ｾ縺励◆縲・)

        print(f"\n--- [Phase 7/7] Destination vCenter: Final Storage vMotion ---")
        print(f"譛邨ら噪縺ｪ繝・・繧ｿ繧ｹ繝医い '{TARGET_DATASTORE_NAME_FINAL}' 繧呈､懃ｴ｢荳ｭ...")
        final_datastore = next((ds for ds in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME_FINAL), None)
        if not final_datastore: raise FileNotFoundError(f"譛邨ゅョ繝ｼ繧ｿ繧ｹ繝医い '{TARGET_DATASTORE_NAME_FINAL}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        print(f"? 譛邨ゅョ繝ｼ繧ｿ繧ｹ繝医い '{final_datastore.name}' 縺瑚ｦ九▽縺九ｊ縺ｾ縺励◆縲・)

        print("\n" + "="*25 + " 謫・菴・遒ｺ 隱・(4/4) " + "="*25)
        print("VM縺ｮ繧ｹ繝医Ξ繝ｼ繧ｸ繧呈怙邨ら噪縺ｪPRD繝・・繧ｿ繧ｹ繝医い縺ｫ遘ｻ蜍輔＠縺ｾ縺吶・)
        print(f"  - 蟇ｾ雎｡VM: {migrated_vm.name}")
        print(f"  - 迴ｾ蝨ｨ縺ｮ繝・・繧ｿ繧ｹ繝医い: {', '.join([ds.name for ds in migrated_vm.datastore])}")
        print(f"  - 笘・ｧｻ陦悟・繝・・繧ｿ繧ｹ繝医い: {TARGET_DATASTORE_NAME_FINAL} 笘・)
        print("=" * 64)
        
        user_approval_svmotion = input("\n縺薙・繧ｹ繝医Ξ繝ｼ繧ｸvMotion謫堺ｽ懊ｒ螳溯｡後＠縺ｦ繧ゅｈ繧阪＠縺・〒縺吶°・・(y/n): ")
        if user_approval_svmotion.lower() != 'y': raise InterruptedError("繝ｦ繝ｼ繧ｶ繝ｼ縺ｫ繧医▲縺ｦ繧ｹ繝医Ξ繝ｼ繧ｸvMotion縺後く繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲・)

        print("\n謇ｿ隱阪＆繧後∪縺励◆縲ゅせ繝医Ξ繝ｼ繧ｸvMotion繧ｿ繧ｹ繧ｯ繧帝幕蟋九＠縺ｾ縺・..")
        relocate_spec_final = vim.vm.RelocateSpec(datastore=final_datastore)
        task = migrated_vm.RelocateVM_Task(spec=relocate_spec_final)
        
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            progress = task.info.progress or 0
            print(f"   繧ｹ繝医Ξ繝ｼ繧ｸvMotion縺ｮ騾ｲ謐・ {progress}%", end='\r')
            time.sleep(5)
        print(" " * 40, end='\r')
        
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"譛邨ら噪縺ｪ繧ｹ繝医Ξ繝ｼ繧ｸvMotion縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {task.info.error.msg}")
        
        print(f"\n? 繧ｹ繝医Ξ繝ｼ繧ｸvMotion縺梧ｭ｣蟶ｸ縺ｫ螳御ｺ・＠縺ｾ縺励◆縲・)
        print(f"\n? 蜈ｨ縺ｦ縺ｮ遘ｻ陦後・繝ｭ繧ｻ繧ｹ縺梧ｭ｣蟶ｸ縺ｫ螳御ｺ・＠縺ｾ縺励◆縲・)
        Disconnect(si_dest)
        si_dest = None


    except Exception as e:
        print(f"\n? 蜃ｦ逅・ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {e}")
        if migrated_vm_for_rollback:
            print("\n" + "="*20 + " 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ遒ｺ隱・(螳帛・VM蜑企勁) " + "="*20)
            print("蜃ｦ逅・′荳ｭ譁ｭ縺輔ｌ縺溘◆繧√∝ｮ帛・vCenter縺ｫ菴懈・騾比ｸｭ縺ｮVM縺梧ｮ九▲縺ｦ縺・∪縺吶・)
            print(f"  - 蟇ｾ雎｡VM: {migrated_vm_for_rollback.name}")
            
            rollback_approval = input("\n縺薙・VM繧貞炎髯､縺励※縲∵桃菴懊ｒ蜈・↓謌ｻ縺励∪縺吶°・・(y/n): ")
            if rollback_approval.lower() == 'y':
                try:
                    if si_dest is None or not si_dest.CurrentTime(): # 謗･邯壹′蛻・ｌ縺ｦ縺・ｋ蝣ｴ蜷医・蜀肴磁邯・
                        print("   繧ｯ繝ｪ繝ｼ繝ｳ繧｢繝・・縺ｮ縺溘ａ螳帛・vCenter縺ｫ蜀肴磁邯壹＠縺ｾ縺・..")
                        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
                        if not si_dest:
                            raise ConnectionError("螳帛・vCenter縺ｸ縺ｮ蜀肴磁邯壹↓螟ｱ謨励＠縺ｾ縺励◆縲・) from None
                        print("   ? 蜀肴磁邯壽・蜉溘・)

                    content_dest_cleanup = si_dest.RetrieveContent()
                    vm_to_delete = next((vm for vm in content_dest_cleanup.viewManager.CreateContainerView(content_dest_cleanup.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name), None)
                    if not vm_to_delete:
                        print("   ?? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ蟇ｾ雎｡縺ｮVM縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲ゅ♀縺昴ｉ縺乗里縺ｫ蜑企勁縺輔ｌ縺ｦ縺・∪縺吶・)
                        unregistered_from_source = True 
                    else:
                        if vm_to_delete.runtime.powerState == 'poweredOn':
                            print(f"   VM '{vm_to_delete.name}' 繧偵ヱ繝ｯ繝ｼ繧ｪ繝輔＠縺ｦ縺・∪縺・..")
                            task = vm_to_delete.PowerOffVM_Task()
                            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
                            if task.info.state == vim.TaskInfo.State.success:
                                print("   ? 繝代Ρ繝ｼ繧ｪ繝墓・蜉溘・)
                            else:
                                print(f"   ?? 繝代Ρ繝ｼ繧ｪ繝輔↓螟ｱ謨励＠縺ｾ縺励◆: {task.info.error.msg}縲ょ炎髯､繧定ｩｦ縺ｿ縺ｾ縺吶・)

                        print(f"   VM '{vm_to_delete.name}' 繧貞炎髯､縺励※縺・∪縺・..")
                        destroy_task = vm_to_delete.Destroy_Task()
                        while destroy_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
                        
                        if destroy_task.info.state == vim.TaskInfo.State.success:
                            print("? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ螳御ｺ・ 螳帛・VM縺ｯ豁｣蟶ｸ縺ｫ蜑企勁縺輔ｌ縺ｾ縺励◆縲・)
                            unregistered_from_source = False
                        else:
                            unregistered_from_source = True
                            raise RuntimeError(f"VM縺ｮ蜑企勁縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {destroy_task.info.error.msg}") from None

                except Exception as cleanup_error:
                    print(f"? 螳帛・VM縺ｮ繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ蜃ｦ逅・ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {cleanup_error}")
                    unregistered_from_source = True 

        if unregistered_from_source:
            print("\n" + "="*20 + " 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ遒ｺ隱・(繝輔ぃ繧､繝ｫ蜑企勁) " + "="*20)
            print("   繧ｽ繝ｼ繧ｹvCenter縺ｧ縺ｮ繧ｯ繝ｭ繝ｼ繝ｳ繝輔ぃ繧､繝ｫ縺ｮ繧ｯ繝ｪ繝ｼ繝ｳ繧｢繝・・縺悟ｿ・ｦ√〒縺吶・)
            print(f"   蟇ｾ雎｡VM縺ｮ繝輔ぃ繧､繝ｫ縺後ョ繝ｼ繧ｿ繧ｹ繝医い '{TARGET_DATASTORE_NAME}' 縺ｫ谿九▲縺ｦ縺・ｋ蜿ｯ閭ｽ諤ｧ縺後≠繧翫∪縺吶・)
            
            rollback_approval_files = input("\n繧ｽ繝ｼ繧ｹvCenter縺ｫ謗･邯壹＠縺ｦ縲√％繧後ｉ縺ｮ繝輔ぃ繧､繝ｫ繧貞炎髯､縺励∪縺吶°・・(y/n): ")
            if rollback_approval_files.lower() == 'y':
                si_source_cleanup = None
                try:
                    print("\n謇ｿ隱阪＆繧後∪縺励◆縲ゅけ繝ｪ繝ｼ繝ｳ繧｢繝・・縺ｮ縺溘ａ繧ｽ繝ｼ繧ｹvCenter縺ｫ蜀肴磁邯壹＠縺ｾ縺・..")
                    si_source_cleanup = SmartConnect(
                        host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
                    if not si_source_cleanup:
                        raise ConnectionError("繧ｽ繝ｼ繧ｹvCenter縺ｸ縺ｮ蜀肴磁邯壹↓螟ｱ謨励＠縺ｾ縺励◆縲・) from None
                    print("   ? 蜀肴磁邯壽・蜉・)
                    
                    content_cleanup = si_source_cleanup.RetrieveContent()
                    file_manager = content_cleanup.fileManager
                    
                    vm_dir_path = os.path.dirname(vmx_path)
                    
                    print(f"   繝・・繧ｿ繧ｹ繝医い縺九ｉ繝・ぅ繝ｬ繧ｯ繝医Μ '{vm_dir_path}' 繧貞炎髯､縺励∪縺・..")
                    datacenter = content_cleanup.rootFolder.childEntity[0]
                    delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)
                    
                    while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                        time.sleep(2)
                    
                    if delete_task.info.state == vim.TaskInfo.State.success:
                        print("? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ螳御ｺ・ 繝・・繧ｿ繧ｹ繝医い荳翫・繝輔ぃ繧､繝ｫ縺ｯ豁｣蟶ｸ縺ｫ蜑企勁縺輔ｌ縺ｾ縺励◆縲・)
                    else:
                        raise RuntimeError(f"繝・・繧ｿ繧ｹ繝医い縺ｮ繝輔ぃ繧､繝ｫ蜑企勁縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {delete_task.info.error.msg}") from None

                except Exception as cleanup_error:
                    print(f"? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ蜃ｦ逅・ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {cleanup_error}")
                    print("   縺頑焔謨ｰ縺ｧ縺吶′縲√ョ繝ｼ繧ｿ繧ｹ繝医い繝悶Λ繧ｦ繧ｶ縺九ｉ謇句虚縺ｧ繧ｯ繝ｪ繝ｼ繝ｳ繧｢繝・・縺励※縺上□縺輔＞縲・)
                finally:
                    if si_source_cleanup:
                        Disconnect(si_source_cleanup)
            else:
                print("繝ｦ繝ｼ繧ｶ繝ｼ縺ｫ繧医▲縺ｦ繝輔ぃ繧､繝ｫ繧ｯ繝ｪ繝ｼ繝ｳ繧｢繝・・縺後く繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲ゅヵ繧｡繧､繝ｫ縺ｯ繝・・繧ｿ繧ｹ繝医い荳翫↓谿九▲縺ｦ縺・∪縺吶・)
        elif new_vm_on_source:
            print("\n" + "="*20 + " 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ遒ｺ隱・(繧ｽ繝ｼ繧ｹVM蜑企勁) " + "="*20)
            print(f"菴懈・騾比ｸｭ縺縺｣縺欸M '{new_vm_on_source.name}' 縺後た繝ｼ繧ｹvCenter縺ｫ谿九▲縺ｦ縺・∪縺吶・)
            rollback_approval = input("\n縺薙・VM繧貞炎髯､縺励※謫堺ｽ懷燕縺ｮ迥ｶ諷九↓謌ｻ縺励∪縺吶°・・(y/n): ")
            if rollback_approval.lower() == 'y':
                task = new_vm_on_source.Destroy_Task()
                while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
                if task.info.state == vim.TaskInfo.State.success:
                    print("? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ螳御ｺ・ VM縺ｯ豁｣蟶ｸ縺ｫ蜑企勁縺輔ｌ縺ｾ縺励◆縲・)
                else:
                    print(f"? 繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ螟ｱ謨・ {task.info.error.msg}")
            else:
                print("繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ縺ｯ繧ｭ繝｣繝ｳ繧ｻ繝ｫ縺輔ｌ縺ｾ縺励◆縲７M縺ｯ繧ｽ繝ｼ繧ｹvCenter縺ｫ谿九▲縺ｦ縺・∪縺吶・)

    finally:
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
        print("蜃ｦ逅・ｒ邨ゆｺ・＠縺ｾ縺・)




if __name__ == "__main__":
    main()
