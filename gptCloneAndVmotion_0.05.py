import atexit
import ssl
import getpass
import time
import tempfile
from datetime import datetime
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import os

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
TARGET_DATASTORE_NAME = 'PMAX-COM-VOL1'
TARGET_DATASTORE_NAME_FINAL = 'PMAX-PRD-VOL1'
TARGET_CLUSTER_NAME = 'PRD-Cluster'

# --- ゲストOS認証情報 ---
GUEST_ROOT_USER = 'root'
GUEST_ROOT_PWD = ''
GUEST_ADMIN_USER = 'admin'
GUEST_ADMIN_PWD = ''

# ------------------------------------------------
# Helper Functions
# ------------------------------------------------
def calculate_new_ip(ip_address):
    """第三オクテットから10を引いた新しいIPアドレスを計算する"""
    if not ip_address:
        return None
    try:
        parts = ip_address.split('.')
        if len(parts) == 4:
            parts[2] = str(int(parts[2]) - 10)
            return '.'.join(parts)
    except (ValueError, IndexError):
        return None
    return None

def prefix_to_subnet_mask(prefix_length):
    """CIDRプレフィックス長をサブネットマスクに変換する"""
    if not isinstance(prefix_length, int) or not (0 <= prefix_length <= 32):
        return None
    host_bits = 32 - prefix_length
    netmask = (1 << 32) - (1 << host_bits)
    return '.'.join([str((netmask >> i) & 0xff) for i in [24, 16, 8, 0]])

def generate_ip_config_script(new_ip):
    """Generate a shell script to configure the IP address."""
    return f"""
    i=1; max_retries=12; while [ $i -le $max_retries ]; do
        if ip addr show \"$DEVICE_NAME\" | grep -q '{new_ip}'; then
            echo 'IP address {new_ip} successfully set.'; break;
        fi;
        echo 'Waiting for IP address {new_ip} to be set... Attempt $i/$max_retries' >&2;
        i=$((i+1)); sleep 5;
    done;
    if [ $i -gt $max_retries ]; then
        echo 'Failed to set IP address {new_ip} after $max_retries attempts.' >&2; exit 1;
    fi
    """

def run_command_in_guest_with_fallback(guest_op_manager, vm, root_auth, admin_auth, admin_pwd, script_text, nic_index):
    """
    ゲストOS内で、まず/tmpにシェルスクリプトファイルとしてアップロードし、
    そのシェルスクリプトを実行する処理。
    ※スクリプト内容の詳細（set -x等）および実行結果のログを取得します。
    """
    process_manager = guest_op_manager.processManager
    file_manager = guest_op_manager.fileManager
    # それぞれのNIC用にスクリプトファイル名とログファイル名を決定
    guest_script_path = f"/tmp/vm_migration_script_{nic_index}.sh"
    guest_log_path = f"/tmp/vm_migration_debug_{nic_index}.log"

    # スクリプト内容の先頭にセットするデバッグ行
    debug_header = "\n".join([
        "set -x",
        "echo '### スクリプト開始 ###'"
    ])
    full_script = debug_header + "\n" + script_text

    # --- 1. ローカル一時ファイルにスクリプトを書き込み ---
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_script:
        temp_script.write(full_script)
        local_script_path = temp_script.name

    # --- 2. ローカルスクリプトファイルの内容をアップロード ---
    file_size = os.path.getsize(local_script_path)
    # guest用ファイル属性の設定（空の属性でOK）
    file_attributes = vim.vm.guest.FileManager.FileAttributes()

    try:
        transfer_info = file_manager.InitiateFileTransferToGuest(
            vm=vm,
            auth=root_auth,
            guestFilePath=guest_script_path,
            fileSize=file_size,
            overwrite=True,
            fileAttributes=file_attributes
        )
    except vim.fault.InvalidGuestLogin:
        # root認証でうまくいかない場合は、admin認証で試行
        transfer_info = file_manager.InitiateFileTransferToGuest(
            vm=vm,
            auth=admin_auth,
            guestFilePath=guest_script_path,
            fileSize=file_size,
            overwrite=True,
            fileAttributes=file_attributes
        )

    # アップロード：PUTリクエスト
    with open(local_script_path, "rb") as f:
        response = requests.put(transfer_info.url, data=f, verify=False)
    if response.status_code != 200:
        os.unlink(local_script_path)
        raise RuntimeError(f"スクリプトファイルのアップロードに失敗しました。Status Code: {response.status_code}")

    os.unlink(local_script_path)  # ローカル一時ファイル削除

    # --- 3. ゲスト上のスクリプトを実行 ---
    # 実行コマンド：/bin/bash guest_script_path > guest_log_path 2>&1
    exec_command = f"/bin/bash {guest_script_path} > {guest_log_path} 2>&1"
    print(f"DEBUG: ゲストで実行するコマンド: {exec_command}")
    auth_attempted = root_auth
    try:
        spec = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/bash", arguments=f"-c \"{exec_command}\"")
        pid = process_manager.StartProgramInGuest(vm=vm, auth=root_auth, spec=spec)
    except vim.fault.InvalidGuestLogin:
        print(f"   -> {root_auth.username}での認証に失敗。{admin_auth.username}で再試行します...")
        auth_attempted = admin_auth
        spec = vim.vm.guest.ProcessManager.ProgramSpec(programPath="/bin/bash", arguments=f"-c \"{exec_command}\"")
        pid = process_manager.StartProgramInGuest(vm=vm, auth=admin_auth, spec=spec)

    # --- 4. コマンド完了を待機 ---
    exit_code = -1
    start_time = time.time()
    while time.time() - start_time < 300:
        procs = process_manager.ListProcessesInGuest(vm=vm, auth=auth_attempted, pids=[pid])
        if procs and procs[0].exitCode is not None:
            exit_code = procs[0].exitCode
            break
        time.sleep(5)

    # --- 5. ゲスト上のログファイル取得 ---
    print("\n" + "="*20 + f" NIC {nic_index+1} ゲストOS内コマンド実行ログ " + "="*20)
    if REQUESTS_AVAILABLE:
        try:
            transfer_info = file_manager.InitiateFileTransferFromGuest(
                vm=vm, auth=auth_attempted, guestFilePath=guest_log_path)
            resp = requests.get(transfer_info.url, verify=False)
            if resp.status_code == 200:
                print("=== ログ内容 Start ===")
                print(resp.text)
                print("=== ログ内容 End ===")
            else:
                print(f"   ログ取得に失敗。HTTP Status Code: {resp.status_code}")
        except vim.fault.FileNotFound:
            print(f"   ゲスト内のログファイル '{guest_log_path}' が見つかりませんでした。")
        except Exception as log_e:
            print(f"   ログ取得中のエラー: {log_e}")
    else:
        print("   'requests'ライブラリ未インストールのため、ログは取得できませんでした。")
    print("=" * (42 + len(str(nic_index+1))) + "\n")
    
    # --- 6. 作成したスクリプトファイルとログの削除 ---
    try:
        file_manager.DeleteFileInGuest(vm=vm, auth=auth_attempted, filePath=guest_script_path)
    except Exception:
        pass
    try:
        file_manager.DeleteFileInGuest(vm=vm, auth=auth_attempted, filePath=guest_log_path)
    except Exception:
        pass

    if exit_code != 0:
        raise RuntimeError(f"ゲスト内スクリプト実行に失敗。Exit Code: {exit_code}")

def main():
    # ------------------------------------------------
    # 1. パスワード入力
    # ------------------------------------------------
    try:
        VCSA_PWD_SOURCE = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_SOURCE}: ")
        VCSA_PWD_DEST = getpass.getpass(f"Password for {VCSA_USER} on {VCSA_HOST_DEST}: ")
    except Exception as error:
        print("ERROR:", error)
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
        print("ERROR:", error)
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
    si_source = None
    si_dest = None
    
    try:
        # --- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---
        print("\n--- [Phase 0/7] Pre-flight Check: Authenticating to vCenters ---")
        print(f"   ソースvCenter ({VCSA_HOST_SOURCE}) に接続を試みています...")
        si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
        if not si_source:
            raise ConnectionError(f"ソースvCenter ({VCSA_HOST_SOURCE}) への認証に失敗しました。")
        print("   ✅ ソースvCenter認証成功。")
        Disconnect(si_source)
        si_source = None
    
        print(f"   宛先vCenter ({VCSA_HOST_DEST}) に接続を試みています...")
        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
        if not si_dest:
            raise ConnectionError(f"宛先vCenter ({VCSA_HOST_DEST}) への認証に失敗しました。")
        print("   ✅ 宛先vCenter認証成功。")
        Disconnect(si_dest)
        si_dest = None
    
        # --- [Phase 1/7] Source vCenter: Collect Info & Prepare ---
        print("\n--- [Phase 1/7] Source vCenter: Collect Info & Prepare ---")
        si_source = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
        if not si_source:
            raise ConnectionError(f"ソースvCenter ({VCSA_HOST_SOURCE}) に接続できませんでした。")
        print("✅ 接続成功")
    
        content_source = si_source.RetrieveContent()
        target_vm = next((vm for vm in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.VirtualMachine], True).view if vm.name == target_vm_name), None)
        if not target_vm:
            raise FileNotFoundError(f"VM '{target_vm_name}' は見つかりませんでした。")
        print(f"✅ VM '{target_vm.name}' が見つかりました。")
    
        if target_vm.guest.toolsRunningStatus != 'guestToolsRunning':
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
                                'gateway': (target_vm.guest.ipStack[0].ipRouteConfig.ipRoute[0].gateway.ipAddress 
                                            if target_vm.guest.ipStack and target_vm.guest.ipStack[0].ipRouteConfig and target_vm.guest.ipStack[0].ipRouteConfig.ipRoute 
                                            else None)
                            })
        if target_vm.guest.ipStack and target_vm.guest.ipStack[0].dnsConfig:
            original_dns_servers = target_vm.guest.ipStack[0].dnsConfig.ipAddress
        print(f"   ✅ {len(original_nic_info)}個のNICからIP構成情報を収集しました。")
    
        target_datastore = next((ds for ds in content_source.viewManager.CreateContainerView(content_source.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME), None)
        if not target_datastore:
            raise FileNotFoundError(f"データストア '{TARGET_DATASTORE_NAME}' が見つかりませんでした。")
        print(f"✅ データストア '{target_datastore.name}' が見つかりました。")
    
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
                print(f"      - Gateway     : {nic['gateway']}")
        else:
            print("    - NIC情報が見つかりませんでした。")
        print("\n  [クローン先VMの仕様]")
        print(f"    - 新しいVM名    : {clone_name}")
        print(f"    - 配置データストア: {TARGET_DATASTORE_NAME}")
        print("=" * 64)
    
        user_approval = input("\nこのクローン操作を実行してもよろしいですか？ (y/n): ")
        if user_approval.lower() != 'y':
            raise InterruptedError("ユーザーによって操作がキャンセルされました。")
    
        # --- クローン、NIC削除、登録解除 ---
        relocate_spec = vim.vm.RelocateSpec(datastore=target_datastore)
        clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)
        print("\nクローン作成タスクを開始しました...")
        task = target_vm.Clone(folder=target_vm.parent, name=clone_name, spec=clone_spec)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            progress = task.info.progress or 0
            print(f"   クローン作成の進捗: {progress}%", end='\r')
            time.sleep(5)
        print(" " * 40, end='\r')
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"クローン作成エラー: {task.info.error.msg}")
        print(f"\n✅ クローン作成成功: '{clone_name}'")
    
        new_vm_on_source = task.info.result
        # NIC削除処理
        print(f"   クローンしたVM '{new_vm_on_source.name}' のNICを削除します...")
        nic_devices_to_remove = [dev for dev in new_vm_on_source.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
        if nic_devices_to_remove:
            nic_change_spec = [vim.vm.device.VirtualDeviceSpec(operation='remove', device=nic) for nic in nic_devices_to_remove]
            config_spec = vim.vm.ConfigSpec(deviceChange=nic_change_spec)
            task = new_vm_on_source.ReconfigVM_Task(spec=config_spec)
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"NIC削除エラー: {task.info.error.msg}")
            print("   ✅ NIC削除成功")
    
        vmx_path = new_vm_on_source.config.files.vmPathName
        print(f"   VM '{clone_name}' をソースvCenterから登録解除します...")
        new_vm_on_source.UnregisterVM()
        unregistered_from_source = True
        print("   ✅ 登録解除成功")
        Disconnect(si_source)
        si_source = None
        new_vm_on_source = None
    
        # --- [Phase 2/7] Destination vCenter: Connect & Pre-check ---
        print("\n--- [Phase 2/7] Destination vCenter: Connect & Pre-check ---")
        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
        if not si_dest:
            raise ConnectionError(f"宛先vCenter ({VCSA_HOST_DEST}) に接続できませんでした。")
        print("✅ 接続成功")
        content_dest = si_dest.RetrieveContent()
        if any(vm for vm in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name):
            raise FileExistsError(f"同名のVM '{clone_name}' が宛先vCenterに既に存在します。")
        print("✅ 宛先vCenterに同名のVMは存在しません。")
    
        print("\n--- [Phase 3/7] Destination vCenter: Register VM ---")
        dest_cluster = next((c for c in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.ClusterComputeResource], True).view if c.name == TARGET_CLUSTER_NAME), None)
        if not dest_cluster:
            raise FileNotFoundError(f"宛先クラスタ '{TARGET_CLUSTER_NAME}' が見つかりませんでした。")
        task = dest_cluster.parent.parent.vmFolder.RegisterVM_Task(path=vmx_path, name=clone_name, asTemplate=False, pool=dest_cluster.resourcePool)
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(5)
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"宛先vCenterでのVM登録エラー: {task.info.error.msg}")
        migrated_vm = task.info.result
        migrated_vm_for_rollback = migrated_vm
        print("✅ VM登録成功。")
    
        print("\n--- [Phase 4/7] Destination vCenter: Reconfigure NICs ---")
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
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                time.sleep(2)
            if task.info.state != vim.TaskInfo.State.success:
                raise RuntimeError(f"NICの再設定に失敗しました: {task.info.error.msg}")
            print("   ✅ NICの再設定が正常に完了しました。")
            print("   新しいNICの情報を取得中...")
            migrated_vm.Reload()
            newly_added_nics = [dev for dev in migrated_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualEthernetCard)]
            if len(newly_added_nics) == len(original_nic_info):
                for i in range(len(original_nic_info)):
                    original_nic_info[i]['new_mac_address'] = newly_added_nics[i].macAddress
                print("   ✅ 新しいMACアドレスの関連付けが完了しました。")
            else:
                raise RuntimeError("NICの再作成数と元のNIC数が一致しません。")
        else:
            print("   - 元のVMにNICがなかったため、NICの再設定はスキップされました。")
    
        print("\n--- [Phase 5/7] Destination vCenter: Power On ---")
        print("\n" + "="*25 + " 操 作 確 認 (3/4) " + "="*25)
        print("VMをパワーオンし、ゲストOSのIPアドレスを設定します。")
        if original_nic_info:
            for i, nic in enumerate(original_nic_info):
                new_ip = calculate_new_ip(nic['ip_address'])
                new_gateway = calculate_new_ip(nic['gateway'])
                print(f"\n  - NIC {i+1} ({nic['new_mac_address']})")
                print(f"    - IP Address  : {nic['ip_address']} → {new_ip}")
                print(f"    - Gateway     : {nic['gateway']} → {new_gateway}")
        else:
            print("  - NIC情報がないため、IP設定は行われません。")
        print("=" * 64)
    
        user_approval_ip = input("\nこのIP設定を実行し、VMをパワーオンしますか？ (y/n): ")
        if user_approval_ip.lower() != 'y':
            raise InterruptedError("ユーザーによってIP設定とパワーオンがキャンセルされました。")
    
        print("\n承認されました。VMをパワーオンします...")
        task = migrated_vm.PowerOnVM_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(2)
        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"VMのパワーオンに失敗しました: {task.info.error.msg}")
        print("   ✅ VMは正常にパワーオンされました。")
    
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
        print("   ✅ ゲストOS操作エージェント準備完了。")
    
        print("\n--- [Phase 6/7] Destination vCenter: Set IP Address ---")
        if original_nic_info:
            root_auth = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ROOT_USER, password=GUEST_ROOT_PWD)
            admin_auth = vim.vm.guest.NamePasswordAuthentication(username=GUEST_ADMIN_USER, password=GUEST_ADMIN_PWD)
            for i, nic_info in enumerate(original_nic_info):
                new_ip = calculate_new_ip(nic_info['ip_address'])
                new_gateway = calculate_new_ip(nic_info['gateway'])
                subnet_mask_parts = nic_info['subnet_mask'].split('.')
                prefix = sum([bin(int(x)).count('1') for x in subnet_mask_parts])
                new_mac = nic_info['new_mac_address']
    
                # 発行するシェルスクリプト内容（各コマンドの詳細な出力を含む）
                # Commands to configure NIC settings in the guest OS
                script_cmds = []
                # Enable debug mode for the shell script to log all executed commands
                script_cmds.append("set -x")
                script_cmds.append("echo '### スクリプト開始 ###'")
                script_cmds.append(f"i=1; max_retries=24; while [ $i -le $max_retries ]; do "
                                   f"DEVICE_NAME=$(nmcli -g DEVICE,HWADDR device | awk -F: -v mac=\"{new_mac.lower()}\" 'tolower($2) == mac {{print $1}}'); "
                                   f"if [ ! -z \"$DEVICE_NAME\" ]; then break; fi; "
                                   f"echo 'Waiting for device {new_mac} to appear... Attempt $i/$max_retries' >&2; "
                                   f"i=$((i+1)); sleep 5; "
                                   f"done; if [ -z \"$DEVICE_NAME\" ]; then "
                                   f"echo 'Error: Device for MAC {new_mac} not found after $max_retries attempts.' >&2; exit 1; fi")
                script_cmds.append(f'if [ -z "$DEVICE_NAME" ]; then '
                                   f'echo "### DIAGNOSTIC INFO START ###" >&2; '
                                   f'echo "### DEVICE INFO ###" >&2; nmcli dev show >&2; '
                                   f'echo "### CONNECTION INFO ###" >&2; nmcli connection show >&2; '
                                   f'echo "### DIAGNOSTIC INFO END ###" >&2; '
                                   f'echo "Error: Device for MAC {new_mac} not found after maximum retries." >&2; exit 1; fi')
                script_cmds.append(f"if nmcli -g GENERAL.STATE device show \"$DEVICE_NAME\" | grep -q 'connected'; then "
                                   f"nmcli device disconnect \"$DEVICE_NAME\" || echo 'Failed to disconnect device' >&2; "
                                   f"fi")
                script_cmds.append(f'EXISTING_CONNS=$(nmcli -g UUID,DEVICE connection show | grep -i "$DEVICE_NAME" | cut -d: -f1 || echo "")')
                script_cmds.append('if [ -z "$EXISTING_CONNS" ]; then echo "No existing connections found for device $DEVICE_NAME" >&2; fi')
                script_cmds.append('if [ ! -z "$EXISTING_CONNS" ]; then '
                                   '[ ! -z "$EXISTING_CONNS" ] && for uuid in $EXISTING_CONNS; do '
                                   'nmcli connection delete uuid "$uuid" && echo "Deleted existing connection: $uuid" >&2; '
                                   'done')
                con_name = f"prd-nic-{i}"
                add_cmd = f"nmcli connection add type ethernet con-name \"{con_name}\" ifname \"$DEVICE_NAME\""
                script_cmds.append(add_cmd)
                mod_cmd_parts = [
                    f"nmcli connection modify \"{con_name}\" ipv4.method manual",
                    f"ipv4.addresses {new_ip}/{prefix}"
                ]
                # Assign the gateway only to the first NIC (i == 0) as it is typically the primary NIC for routing.
                if i == 0 and new_gateway:
                    mod_cmd_parts.append(f"ipv4.gateway {new_gateway}")
                if i == 0 and isinstance(original_dns_servers, list) and all(isinstance(dns, str) for dns in original_dns_servers):
                    dns_str = ' '.join(original_dns_servers)
                    mod_cmd_parts.append(f'ipv4.dns "{dns_str}"')
                script_cmds.append(" ".join(mod_cmd_parts))
                # This command activates the newly created network connection.
                script_cmds.append(f"nmcli connection up {con_name} || echo 'Failed to bring up connection' >&2")
                # Adding a delay to ensure the network connection is properly established before verifying the IP address
                script_cmds.append("sleep 5")
                script_cmds.append(generate_ip_config_script(new_ip))
    
                script_text = " && ".join(script_cmds)
    
                run_command_in_guest_with_fallback(
                    guest_op_manager,
                    migrated_vm,
                    root_auth,
                    admin_auth,
                    GUEST_ADMIN_PWD,
                    script_text,
                    i
                )
            print("   ✅ 全てのNICのIP設定が完了しました。")
    
        print("\n--- [Phase 7/7] Destination vCenter: Final Storage vMotion ---")
        print(f"最終的なデータストア '{TARGET_DATASTORE_NAME_FINAL}' を検索中...")
        final_datastore = next((ds for ds in content_dest.viewManager.CreateContainerView(content_dest.rootFolder, [vim.Datastore], True).view if ds.name == TARGET_DATASTORE_NAME_FINAL), None)
        if not final_datastore:
            raise FileNotFoundError(f"最終データストア '{TARGET_DATASTORE_NAME_FINAL}' が見つかりませんでした。")
        print(f"✅ 最終データストア '{final_datastore.name}' が見つかりました。")
    
        print("\n" + "="*25 + " 操 作 確 認 (4/4) " + "="*25)
        print("VMのストレージを最終的なPRDデータストアに移動します。")
        print(f"  - 対象VM: {migrated_vm.name}")
        print(f"  - 現在のデータストア: {', '.join([ds.name for ds in migrated_vm.datastore])}")
        print(f"  - ★移行先データストア: {TARGET_DATASTORE_NAME_FINAL} ★")
        print("=" * 64)
    
        user_approval_svmotion = input("\nこのストレージvMotion操作を実行してもよろしいですか？ (y/n): ")
        if user_approval_svmotion.lower() != 'y':
            raise InterruptedError("ユーザーによってストレージvMotionがキャンセルされました。")
    
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
        print(f"\n✅ ストレージvMotionが正常に完了しました。")
        print(f"\n🎉 全ての移行プロセスが正常に完了しました。")
        Disconnect(si_dest)
        si_dest = None
    
    except Exception as e:
        print(f"\n❌ 処理中にエラーが発生しました: {e}")
        if migrated_vm_for_rollback:
            print("\n" + "="*20 + " ロールバック確認 (宛先VM削除) " + "="*20)
            print("処理が中断されたため、宛先vCenterに作成途中のVMが残っています。")
            print(f"  - 対象VM: {migrated_vm_for_rollback.name}")
            rollback_approval = input("\nこのVMを削除して、操作を元に戻しますか？ (y/n): ")
            if rollback_approval.lower() == 'y':
                try:
                    if si_dest is None or not si_dest.CurrentTime():
                        print("   クリーンアップのため宛先vCenterに再接続します...")
                        si_dest = SmartConnect(host=VCSA_HOST_DEST, user=VCSA_USER, pwd=VCSA_PWD_DEST, port=VCSA_PORT, sslContext=ctx)
                        if not si_dest:
                            raise ConnectionError("宛先vCenterへの再接続に失敗しました。")
                        print("   ✅ 再接続成功。")
                    content_dest_cleanup = si_dest.RetrieveContent()
                    vm_to_delete = next((vm for vm in content_dest_cleanup.viewManager.CreateContainerView(content_dest_cleanup.rootFolder, [vim.VirtualMachine], True).view if vm.name == clone_name), None)
                    if not vm_to_delete:
                        print("   ⚠️ ロールバック対象のVMが見つかりませんでした。おそらく既に削除されています。")
                        unregistered_from_source = True
                    else:
                        if vm_to_delete.runtime.powerState == 'poweredOn':
                            print(f"   VM '{vm_to_delete.name}' をパワーオフしています...")
                            task = vm_to_delete.PowerOffVM_Task()
                            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                                time.sleep(2)
                            if task.info.state == vim.TaskInfo.State.success:
                                print("   ✅ パワーオフ成功。")
                            else:
                                print(f"   ⚠️ パワーオフに失敗しました: {task.info.error.msg}。削除を試みます。")
                        print(f"   VM '{vm_to_delete.name}' を削除しています...")
                        destroy_task = vm_to_delete.Destroy_Task()
                        while destroy_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                            time.sleep(2)
                        if destroy_task.info.state == vim.TaskInfo.State.success:
                            print("✅ ロールバック完了: 宛先VMは正常に削除されました。")
                            unregistered_from_source = False
                        else:
                            unregistered_from_source = True
                            raise RuntimeError(f"VMの削除に失敗しました: {destroy_task.info.error.msg}。手動でのファイル削除が必要になる可能性があります。")
                except Exception as cleanup_error:
                    print(f"❌ 宛先VMのロールバック処理中にエラーが発生しました: {cleanup_error}")
                    unregistered_from_source = True
        if unregistered_from_source:
            print("\n" + "="*20 + " ロールバック確認 (ファイル削除) " + "="*20)
            print(f"   対象VMのファイルがデータストア '{TARGET_DATASTORE_NAME}' に残っている可能性があります。")
            rollback_approval_files = input("\nソースvCenterに接続して、これらのファイルを削除しますか？ (y/n): ")
            if rollback_approval_files.lower() == 'y':
                si_source_cleanup = None
                try:
                    print("\n承認されました。クリーンアップのためソースvCenterに再接続します...")
                    si_source_cleanup = SmartConnect(host=VCSA_HOST_SOURCE, user=VCSA_USER, pwd=VCSA_PWD_SOURCE, port=VCSA_PORT, sslContext=ctx)
                    if not si_source_cleanup:
                        raise ConnectionError("ソースvCenterへの再接続に失敗しました。")
                    print("   ✅ 再接続成功")
                    content_cleanup = si_source_cleanup.RetrieveContent()
                    file_manager = content_cleanup.fileManager
                    vm_dir_path = os.path.dirname(vmx_path)
                    print(f"   データストアからディレクトリ '{vm_dir_path}' を削除します...")
                    datacenter = content_cleanup.rootFolder.childEntity[0]
                    delete_task = file_manager.DeleteDatastoreFile_Task(name=vm_dir_path, datacenter=datacenter)
                    while delete_task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                        time.sleep(2)
                    if delete_task.info.state == vim.TaskInfo.State.success:
                        print("✅ ロールバック完了: データストア上のファイルは正常に削除されました。")
                    else:
                        raise RuntimeError(f"データストアのファイル削除に失敗しました: {delete_task.info.error.msg}")
                except Exception as cleanup_error:
                    print(f"❌ ロールバック処理中にエラーが発生しました: {cleanup_error}")
                    print("   お手数ですが、データストアブラウザから手動でクリーンアップしてください。")
                finally:
                    if si_source_cleanup:
                        Disconnect(si_source_cleanup)
            else:
                print("ユーザーによってファイルクリーンアップがキャンセルされました。")
        elif new_vm_on_source:
            print("\n" + "="*20 + " ロールバック確認 (ソースVM削除) " + "="*20)
            print(f"作成途中だったVM '{new_vm_on_source.name}' がソースvCenterに残っています。")
            rollback_approval = input("\nこのVMを削除して操作前の状態に戻しますか？ (y/n): ")
            if rollback_approval.lower() == 'y':
                task = new_vm_on_source.Destroy_Task()
                while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                    time.sleep(2)
                if task.info.state == vim.TaskInfo.State.success:
                    print("✅ ロールバック完了: VMは正常に削除されました。")
                else:
                    print(f"❌ ロールバック失敗: {task.info.error.msg}")
            else:
                print("ロールバックはキャンセルされました。VMはソースvCenterに残っています。")
    
    finally:
        if 'si_dest' in locals() and si_dest and si_dest.CurrentTime():
            Disconnect(si_dest)
        if 'si_source' in locals() and si_source and si_source.CurrentTime():
            Disconnect(si_source)
        print("処理を終了します。")
    
if __name__ == "__main__":
    main()