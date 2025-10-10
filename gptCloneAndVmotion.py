# -*- coding: cp932 -*-
import atexit
import ssl
import getpass
import time
from datetime import datetime
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import os, sys
import json
import ipaddress
import urllib.request

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ------------------------------------------------
# 接続情報および移行先設定
# ------------------------------------------------
# --- ソースvCenter ---
VCSA_HOST_SOURCE = 'vcsa01s.ipet.local'
VCSA_USER = 'administrator@vsphere.local'
VCSA_PORT = 443

# --- 宛先vCenter ---
VCSA_HOST_DEST = 'vcsa01p.ipet.local'

# --- 移行リソース ---
# クローン先の共有データストア
TARGET_DATASTORE_NAME = 'PMAX-COM-VOL1'
# 最終的な移行先データストア
TARGET_DATASTORE_NAME_FINAL = 'PMAX-PRD-VOL1'
# コンピューティングリソースの移行先クラスタ名
TARGET_CLUSTER_NAME = 'PRD-Cluster' 

# --- ゲストOS認証情報 ---
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
    """STG→PRD向けに第三オクテット 170?179 を 160?169 へ写像する。

    - 入力が空なら None を返す
    - IPv4形式と各オクテットの範囲(0?255)を検証
    - 第三オクテットが 170?179 なら 10 減算して返す
    - それ以外は前提外として ValueError を送出
    """
    if not ip_address:
        return None
    parts = ip_address.split('.')
    if len(parts) != 4:
        raise ValueError(f"IPv4形式ではありません: {ip_address}")
    try:
        octets = [int(x) for x in parts]
    except ValueError as e:
        raise ValueError(f"数値化に失敗しました: {ip_address}") from e
    if any(o < 0 or o > 255 for o in octets):
        raise ValueError(f"各オクテットは0?255の範囲である必要があります: {ip_address}")
    if 170 <= octets[2] <= 179:
        octets[2] = octets[2] - 10
        return '.'.join(str(o) for o in octets)
    raise ValueError(f"第三オクテット {octets[2]} は想定外です(期待: 170?179)。入力: {ip_address}")

def execute_command_in_guest(guest_op_manager, vm, root_auth, admin_auth, admin_pwd, command, check_exit_code=True):
    """
    ゲストOS内で単一のコマンドを実行し、root/adminフォールバックを処理し、
    (終了コード, 標準出力, 標準エラー出力) を返す。
    """
    process_manager = guest_op_manager.processManager
    file_manager = guest_op_manager.fileManager
    stdout_path = f"/tmp/stdout_{os.urandom(4).hex()}.log"
    stderr_path = f"/tmp/stderr_{os.urandom(4).hex()}.log"

    def _run_it(auth, cmd):
        escaped_cmd = cmd.replace("'", "'\\''")
        wrapped_cmd = f"'{escaped_cmd}' > {stdout_path} 2> {stderr_path}"
        spec = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/bash", arguments=f"-c {wrapped_cmd}")
        pid = process_manager.StartProgramInGuest(vm=vm, auth=auth, spec=spec)
        
        exit_code = -1
        start_time = time.time()
        while time.time() - start_time < 300: # 5 min timeout
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
                    ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(file_info.url, context=ctx) as resp:
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

    # ---- Main logic of the function ----
    # Present command before issuing
    print("[GUEST-CMD] \u5b9f\u884c\u4e88\u5b9a\u30b3\u30de\u30f3\u30c9:")
    print(f"  {command}")
    exit_code, stdout, stderr = -1, "", ""
    auth_used = None
    fallback_error = None
    try:
        # Try with root first
        auth_used = "root"
        exit_code, stdout, stderr = _run_it(root_auth, command)
    except vim.fault.InvalidGuestLogin as e:
        # Fallback to admin with sudo
        fallback_error = e
        print("[GUEST-CMD] root\u8a8d\u8a3c\u5931\u6557 -> admin\u3078\u30d5\u30a9\u30fc\u30eb\u30d0\u30c3\u30af")
        auth_used = "admin"
        sudo_command = f"echo '{admin_pwd}' | sudo -S {command}"
        exit_code, stdout, stderr = _run_it(admin_auth, sudo_command)

    # After issuing: always show OS returns
    print(f"[GUEST-CMD] \u5b9f\u884c\u30e6\u30fc\u30b6: {auth_used}")
    print(f"[GUEST-CMD] \u7d42\u4e86\u30b3\u30fc\u30c9: {exit_code}")
    print("[GUEST-CMD] \u6a19\u6e96\u51fa\u529b:\n---\n" + (stdout or "(\u306a\u3057)") + "\n---")
    print("[GUEST-CMD] \u6a19\u6e96\u30a8\u30e9\u30fc:\n---\n" + (stderr or "(\u306a\u3057)") + "\n---")

    # Success/Failure summary
    if exit_code == 0:
        print("[GUEST-CMD] \u7d50\u679c: \u6210\u529f")
    else:
        print("[GUEST-CMD] \u7d50\u679c: \u5931\u6557")
        if check_exit_code:
            reason = (stderr or '').strip() or '\u7d42\u4e86\u30b3\u30fc\u30c9\u304c\u975e\u30bc\u30ed'
            if fallback_error is not None and auth_used == "admin":
                raise RuntimeError(f"admin\u3068\u3057\u3066\u306e\u30b3\u30de\u30f3\u30c9\u5b9f\u884c\u306b\u5931\u6557 (Exit Code: {exit_code}, Reason: {reason})") from fallback_error
            raise RuntimeError(f"\u30b3\u30de\u30f3\u30c9\u5b9f\u884c\u306b\u5931\u6557 (Exit Code: {exit_code}, Reason: {reason})")
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

# ------------------------------------------------
# 2. SSLコンテキストの設定
# ------------------------------------------------
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# ------------------------------------------------
# 3. 操作対象のVM名とゲストOS認証情報を入力
# ------------------------------------------------
target_vm_name = input("クローンを作成したいVMの名前を入力してください: ")
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
# メイン処理
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
    print(f"   ソースvCenter ({VCSA_HOST_SOURCE}) に接続を試みています...")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source: raise ConnectionError(f"ソースvCenter ({VCSA_HOST_SOURCE}) への認証に失敗しました。")
    print("   ? ソースvCenter認証成功。")
    Disconnect(si_source)
    si_source = None

    print(f"   宛先vCenter ({VCSA_HOST_DEST}) に接続を試みています...")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest: raise ConnectionError(f"宛先vCenter ({VCSA_HOST_DEST}) への認証に失敗しました。")
    print("   ? 宛先vCenter認証成功。")
    Disconnect(si_dest)
    si_dest = None
    
    # --- [Phase 1/7] Source vCenter: Collect Info & Prepare ---
    print(f"\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
    si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
    if not si_source: raise ConnectionError(f"ソースvCenter ({VCSA_HOST_SOURCE}) に接続できませんでした。")
    print("? 接続成功")
    
    content_source = si_source.RetrieveContent()
    
    target_vm = next((vm for vm in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.VirtualMachine], True).view if vm.name == target_vm_name), None)
    if not target_vm: raise FileNotFoundError(f"VM '{target_vm_name}' は見つかりませんでした。")
    print(f"? VM '{target_vm.name}' が見つかりました。")

    if not target_vm.guest.toolsRunningStatus == 'guestToolsRunning':
        raise SystemError("IPアドレス取得のため、ソースVMの電源がONであり、VMware Toolsが実行中である必要があります。")
    print("   VMware Tools実行中を確認しました。")

    print("   クローン元のNIC情報を収集中...")
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
                            'is_gateway_nic': False # デフォルトはFalse
                        })

    if target_vm.guest.ipStack and target_vm.guest.ipStack[0].ipRouteConfig:
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
                            print(f"   -> NIC with IP {nic['ip_address']} がゲートウェイNICと判定されました。")
                            break
                    except (ValueError, ipaddress.AddressValueError):
                        continue
                break
    
    if target_vm.guest.ipStack and target_vm.guest.ipStack[0].dnsConfig:
        original_dns_servers = [dns for dns in target_vm.guest.ipStack[0].dnsConfig.ipAddress if not dns.startswith('127.')]

    print(f"   ? {len(original_nic_info)}個のNICからIP構成情報を収集しました。")

    target_datastore = next((ds for ds in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME), None)
    if not target_datastore: raise FileNotFoundError(f"データストア '{TARGET_DATASTORE_NAME}' が見つかりませんでした。")
    print(f"? データストア '{target_datastore.name}' が見つかりました。")

    date_suffix = datetime.now().strftime('%Y%m%d')
    clone_name = f"{target_vm.name}-{date_suffix}"
    
    # --- クローン操作の承認 (1/4) ---
    print("\n" + "="*25 + " 操 作 確 認 (1/4) " + "="*25)
    print("以下のVMのクローンを作成し、移行を開始します。内容をよくご確認ください。")
    print(f"\n  [クローン元VMの情報]")
    print(f"    - VM名          : {target_vm.name}")
    print(f"    - OS名          : {target_vm.summary.config.guestFullName}")
    print("\n  [クローン元NICの情報]")
    if original_nic_info:
        for i, nic in enumerate(original_nic_info):
            print(f"    - NIC {i+1} ({nic['mac_address']})")
            print(f"      - Network     : {nic['network_name']}")
            print(f"      - IP Address  : {nic['ip_address']}")
            print(f"      - Subnet Mask : {nic['subnet_mask']}")
    else: print("    - NIC情報が見つかりませんでした。")
    if original_default_gateway:
        print(f"    - Gateway     : {original_default_gateway}")
    else:
        print("    - デフォルトゲートウェイが見つかりませんでした。")

    print("\n  [クローン先VMの仕様]")
    print(f"    - 新しいVM名    : {clone_name}")
    print(f"    - 配置データストア: {TARGET_DATASTORE_NAME}")
    print("=" * 64)

    user_approval = input(f"\nこのクローン操作を実行してもよろしいですか？ (y/n): ")
    if user_approval.lower() != 'y': raise InterruptedError("ユーザーによって操作がキャンセルされました。")
    
    # --- クローン、NIC削除、登録解除 ---
    relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
    clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
    print("\nクローン作成タスクを開始しました...")
    task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: progress = task.info.progress or 0; print(f"   クローン作成の進捗: {progress}%", end='\r'); time.sleep(5)
    print(" " * 40, end='\r')
    if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"クローン作成エラー: {task.info.error.msg}")
    print(f"\n? クローン作成成功: '{clone_name}'")
    
    new_vm_on_source = task.info.result
    # NIC削除処理
    print(f"   クローンしたVM '{new_vm_on_source.name}' のNICを削除します...")
    nic_devices_to_remove = [dev for dev in new_vm_on_source.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
    if nic_devices_to_remove:
        nic_change_spec = [vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic) for nic in nic_devices_to_remove]
        config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
        task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"NIC削除エラー: {task.info.error.msg}")
        print("   ? NIC削除成功")
    
    vmx_path = new_vm_on_source.config.files.vmPathName
    print(f"   VM '{clone_name}' をソースvCenterから登録解除します...")
    new_vm_on_source.UnregisterVM()
    unregistered_from_source = True
    print("   ? 登録解除成功")
    Disconnect(si_source)
    si_source = None
    new_vm_on_source = None 

    # --- [Phase 2/7] ~ [Phase 7/7]: 宛先vCenterでの処理 ---
    print(f"\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
    if not si_dest: raise ConnectionError(f"宛先vCenter ({VCSA_HOST_DEST}) に接続できませんでした。")
    print("? 接続成功")
    content_dest = si_dest.RetrieveContent()
    if any(vm for vm in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name):
        raise FileExistsError(f"同名のVM '{clone_name}' が宛先vCenterに既に存在します。")
    print(f"? 宛先vCenterに同名のVMは存在しません。")

    print(f"\n--- [Phase 3/7] Destination vCenter: Register VM ---")
    dest_cluster = next((c for c in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.ClusterComputeResource], True).view if c.name == TARGET_CLUSTER_NAME), None)
    if not dest_cluster: raise FileNotFoundError(f"宛先クラスタ '{TARGET_CLUSTER_NAME}' が見つかりませんでした。")
    task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(5)
    if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"宛先vCenterでのVM登録エラー: {task.info.error.msg}")
    migrated_vm = task.info.result
    migrated_vm_for_rollback = migrated_vm # ロールバック用に保持
    print(f"? VM登録成功。")

    print(f"\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
    if original_nic_info:
        print("\n" + "="*25 + " 操 作 確 認 (2/4) " + "="*25)
        print("移行したVMにNICを再作成し、以下の通りネットワークに接続します。")
        device_change_spec = []
        for i, nic in enumerate(original_nic_info):
            original_network_name = nic['network_name']
            dest_network_name = original_network_name.replace('STG', 'PRD', 1)
            print(f"  - NIC {i+1}: '{original_network_name}' → '{dest_network_name}'")

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
        user_approval_nic = input("\nこのNIC設定を実行してもよろしいですか？ (y/n): ")
        if user_approval_nic.lower() != 'y':
            raise InterruptedError("ユーザーによってNIC設定がキャンセルされました。")

        print("\n承認されました。NICの再設定タスクを開始します...")
        config_spec = vim.vm.ConfigSpec(deviceChange=device_change_spec)
        task = migrated_vm.ReconfigVM_Task(spec=config_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"NICの再設定に失敗しました: {task.info.error.msg}")
        print("   ? NICの再設定が正常に完了しました。")
        
        print("   新しいNICの情報を取得中...")
        migrated_vm.Reload()
        newly_added_nics = [dev for dev in migrated_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        if len(newly_added_nics) == len(original_nic_info):
            for i in range(len(original_nic_info)):
                 original_nic_info[i]['new_mac_address'] = newly_added_nics[i].macAddress
            print("   ? 新しいMACアドレスの関連付けが完了しました。")
        else:
            raise RuntimeError("NICの再作成数と元のNIC数が一致しません。")
    else:
        print("   - 元のVMにNICがなかったため、NICの再設定はスキップされました。")

    print(f"\n--- [Phase 5/7] Destination vCenter: Power On ---")
    print("\n" + "="*25 + " 操 作 確 認 (3/4) " + "="*25)
    print("VMをパワーオンし、ゲストOSのIPアドレスを設定します。")
    if original_nic_info:
        new_default_gateway = calculate_ip_stg_to_prd(original_default_gateway)
        for i, nic in enumerate(original_nic_info):
            new_ip = calculate_ip_stg_to_prd(nic['ip_address'])
            print(f"\n  - NIC {i+1} ({nic['new_mac_address']})")
            print(f"    - IP Address  : {nic['ip_address']} → {new_ip}")
        if new_default_gateway:
            print(f"\n  [デフォルトゲートウェイの設定]")
            print(f"    - Gateway     : {original_default_gateway} → {new_default_gateway}")
        
        if original_dns_servers:
            print("\n  [DNSサーバーの設定]")
            new_dns_servers = [calculate_ip_stg_to_prd(dns) for dns in original_dns_servers if dns]
            for old_dns, new_dns in zip(original_dns_servers, new_dns_servers):
                print(f"    - {old_dns} → {new_dns}")
    else:
        print("  - NIC情報がないため、IP設定は行われません。")
    print("=" * 64)
    
    user_approval_ip = input("\nこのIP設定を実行し、VMをパワーオンしますか？ (y/n): ")
    if user_approval_ip.lower() != 'y': raise InterruptedError("ユーザーによってIP設定とパワーオンがキャンセルされました。")

    print("\n承認されました。VMをパワーオンします...")
    task = migrated_vm.PowerOnVM_Task()
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
    if task.info.state != vim.TaskInfo.State.success: raise RuntimeError(f"VMのパワーオンに失敗しました: {task.info.error.msg}")
    print("   ? VMは正常にパワーオンされました。")

    print("   ゲストOS操作エージェントの準備を待っています (最大5分)...")
    guest_op_manager = content_dest.guestOperationsManager
    agent_ready = False
    for i in range(10): 
        print(f"    - 試行 {i+1}/10...")
        try:
            creds_check = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
            process_manager = guest_op_manager.processManager
            spec_check = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/echo", arguments="ready")
            pid = process_manager.StartProgramInGuest(vm=migrated_vm, auth=creds_check, spec=spec_check)
            if pid >= 0:
                agent_ready = True
                break
        except vim.fault.InvalidGuestLogin:
             agent_ready = True 
             break
        except vim.fault.GuestOperationsUnavailable:
            if i < 9: 
                time.sleep(30)
            continue 
        except Exception:
            if i < 9: 
                time.sleep(30) 
            continue
    
    if not agent_ready:
        raise SystemError("タイムアウト: ゲストOS操作エージェントが利用可能になりませんでした。")
    print("   ? ゲストOS操作エージェント準備完了。")

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
            
            print("\n" + "="*20 + f" NIC {i+1} の設定 " + "="*20)
            
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
                    raise RuntimeError(f"ゲストOSからのJSON出力の解析に失敗しました: {e}") from e

            if not device_name:
                raise RuntimeError(f"MACアドレス {new_mac} に対応するデバイスが見つかりませんでした。")
            print(f"   -> デバイス '{device_name}' を特定しました。")
            
            # 2. Disconnect and delete existing connections
            execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, f"nmcli device disconnect {device_name} || true", check_exit_code=False)
            device_name_normalized = device_name.lower()
            mac_normalized = new_mac.lower() if new_mac else ""
            existing_conns_cmd = (
                "nmcli -t --separator '|' -f UUID,NAME,DEVICE,connection.interface-name,802-3-ethernet.mac-address "
                "connection show | awk -F'|' "
                f"-v target='{device_name_normalized}' -v target_mac='{mac_normalized}' "
                "'{name=tolower($2); dev=tolower($3); iface=tolower($4); mac=tolower($5); "
                "if ((target != \"\" && (name == target || dev == target || iface == target)) || "
                "(target_mac != \"\" && mac == target_mac)) print $1}'"
            )
            _, existing_conns, _ = execute_command_in_guest(guest_op_manager, migrated_vm, root_auth, admin_auth, GUEST_ADMIN_PWD, existing_conns_cmd, check_exit_code=False)
            if existing_conns.strip():
                for uuid in existing_conns.splitlines():
                    uuid = uuid.strip()
                    if not uuid:
                        continue
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
        
        print("   ? 全てのNICのIP設定が完了しました。")

    print(f"\n--- [Phase 7/7] Destination vCenter: Final Storage vMotion ---")
    print(f"最終的なデータストア '{TARGET_DATASTORE_NAME_FINAL}' を検索中...")
    final_datastore = next((ds for ds in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME_FINAL), None)
    if not final_datastore: raise FileNotFoundError(f"最終データストア '{TARGET_DATASTORE_NAME_FINAL}' が見つかりませんでした。")
    print(f"? 最終データストア '{final_datastore.name}' が見つかりました。")

    print("\n" + "="*25 + " 操 作 確 認 (4/4) " + "="*25)
    print("VMのストレージを最終的なPRDデータストアに移動します。")
    print(f"  - 対象VM: {migrated_vm.name}")
    print(f"  - 現在のデータストア: {', '.join([ds.name for ds in migrated_vm.datastore])}")
    print(f"  - ★移行先データストア: {TARGET_DATASTORE_NAME_FINAL} ★")
    print("=" * 64)
    
    user_approval_svmotion = input("\nこのストレージvMotion操作を実行してもよろしいですか？ (y/n): ")
    if user_approval_svmotion.lower() != 'y': raise InterruptedError("ユーザーによってストレージvMotionがキャンセルされました。")

    print("\n承認されました。ストレージvMotionタスクを開始します...")
    relocate_spec_final = vim.vm.RelocateSpec(datastore=final_datastore)
    task = migrated_vm.RelocateVM_Task(spec=relocate_spec_final)
    
    while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
        progress = task.info.progress or 0
        print(f"   ストレージvMotionの進捗: {progress}%", end='\r')
        time.sleep(5)
    print(" " * 40, end='\r')
    
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"最終的なストレージvMotionに失敗しました: {task.info.error.msg}")
    
    print(f"\n? ストレージvMotionが正常に完了しました。")
    print(f"\n? 全ての移行プロセスが正常に完了しました。")
    Disconnect(si_dest)
    si_dest = None


except Exception as e:
    print(f"\n? 処理中にエラーが発生しました: {e}")
    if migrated_vm_for_rollback:
        print("\n" + "="*20 + " ロールバック確認 (宛先VM削除) " + "="*20)
        print("処理が中断されたため、宛先vCenterに作成途中のVMが残っています。")
        print(f"  - 対象VM: {migrated_vm_for_rollback.name}")
        
        rollback_approval = input("\nこのVMを削除して、操作を元に戻しますか？ (y/n): ")
        if rollback_approval.lower() == 'y':
            try:
                if si_dest is None or not si_dest.CurrentTime(): # 接続が切れている場合は再接続
                    print("   クリーンアップのため宛先vCenterに再接続します...")
                    si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
                    if not si_dest:
                        raise ConnectionError("宛先vCenterへの再接続に失敗しました。") from None
                    print("   ? 再接続成功。")

                content_dest_cleanup = si_dest.RetrieveContent()
                vm_to_delete = next((vm for vm in content_dest_cleanup.viewManager.CreateContainerView(content_dest_cleanup.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name), None)
                if not vm_to_delete:
                    print("   ?? ロールバック対象のVMが見つかりませんでした。おそらく既に削除されています。")
                    unregistered_from_source = True 
                else:
                    if vm_to_delete.runtime.powerState == 'poweredOn':
                        print(f"   VM '{vm_to_delete.name}' をパワーオフしています...")
                        task = vm_to_delete.PowerOffVM_Task()
                        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
                        if task.info.state == vim.TaskInfo.State.success:
                            print("   ? パワーオフ成功。")
                        else:
                            print(f"   ?? パワーオフに失敗しました: {task.info.error.msg}。削除を試みます。")

                    print(f"   VM '{vm_to_delete.name}' を削除しています...")
                    destroy_task = vm_to_delete.Destroy_Task()
                    while destroy_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
                    
                    if destroy_task.info.state == vim.TaskInfo.State.success:
                        print("? ロールバック完了: 宛先VMは正常に削除されました。")
                        unregistered_from_source = False
                    else:
                        unregistered_from_source = True
                        raise RuntimeError(f"VMの削除に失敗しました: {destroy_task.info.error.msg}") from None

            except Exception as cleanup_error:
                print(f"? 宛先VMのロールバック処理中にエラーが発生しました: {cleanup_error}")
                unregistered_from_source = True 

    if unregistered_from_source:
        print("\n" + "="*20 + " ロールバック確認 (ファイル削除) " + "="*20)
        print("   ソースvCenterでのクローンファイルのクリーンアップが必要です。")
        print(f"   対象VMのファイルがデータストア '{TARGET_DATASTORE_NAME}' に残っている可能性があります。")
        
        rollback_approval_files = input("\nソースvCenterに接続して、これらのファイルを削除しますか？ (y/n): ")
        if rollback_approval_files.lower() == 'y':
            si_source_cleanup = None
            try:
                print("\n承認されました。クリーンアップのためソースvCenterに再接続します...")
                si_source_cleanup = SmartConnect(
                    host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
                if not si_source_cleanup:
                    raise ConnectionError("ソースvCenterへの再接続に失敗しました。") from None
                print("   ? 再接続成功")
                
                content_cleanup = si_source_cleanup.RetrieveContent()
                file_manager = content_cleanup.fileManager
                
                vm_dir_path = os.path.dirname(vmx_path)
                
                print(f"   データストアからディレクトリ '{vm_dir_path}' を削除します...")
                datacenter = content_cleanup.rootFolder.childEntity[0]
                delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)
                
                while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)
                
                if delete_task.info.state == vim.TaskInfo.State.success:
                    print("? ロールバック完了: データストア上のファイルは正常に削除されました。")
                else:
                    raise RuntimeError(f"データストアのファイル削除に失敗しました: {delete_task.info.error.msg}") from None

            except Exception as cleanup_error:
                print(f"? ロールバック処理中にエラーが発生しました: {cleanup_error}")
                print("   お手数ですが、データストアブラウザから手動でクリーンアップしてください。")
            finally:
                if si_source_cleanup:
                    Disconnect(si_source_cleanup)
        else:
            print("ユーザーによってファイルクリーンアップがキャンセルされました。ファイルはデータストア上に残っています。")
    elif new_vm_on_source:
        print("\n" + "="*20 + " ロールバック確認 (ソースVM削除) " + "="*20)
        print(f"作成途中だったVM '{new_vm_on_source.name}' がソースvCenterに残っています。")
        rollback_approval = input("\nこのVMを削除して操作前の状態に戻しますか？ (y/n): ")
        if rollback_approval.lower() == 'y':
            task = new_vm_on_source.Destroy_Task()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]: time.sleep(2)
            if task.info.state == vim.TaskInfo.State.success:
                print("? ロールバック完了: VMは正常に削除されました。")
            else:
                print(f"? ロールバック失敗: {task.info.error.msg}")
        else:
            print("ロールバックはキャンセルされました。VMはソースvCenterに残っています。")

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
    print("処理を終了します。")
