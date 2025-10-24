# -*- coding: utf-8 -*-
"""Utilities for executing commands inside a vSphere guest OS."""

import os
import re
import shlex
import ssl
import time
import urllib.request
from typing import List, Optional, Tuple

try:
    import requests
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning

    urllib3.disable_warnings(InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from pyVmomi import vim  # type: ignore[import]

STDERR_ERROR_LITERALS = ("エラー", "失敗")
STDERR_ERROR_REGEXES = (
    re.compile(r"(^|\s)error\b", re.IGNORECASE),
    re.compile(r"(^|\s)failed\b", re.IGNORECASE),
    re.compile(r"(^|\s)fatal\b", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
)

ROOT_LOGIN_DISABLED = False


class NmcliNotAvailableError(RuntimeError):
    """Raised when nmcli is unavailable inside the guest OS."""


class GuestCommandExecutor:
    """Helper that executes shell commands inside a powered-on VM."""

    def __init__(self, guest_op_manager, root_auth, admin_auth, admin_pwd):
        self.process_manager = guest_op_manager.processManager
        self.file_manager = guest_op_manager.fileManager
        self.root_auth = root_auth
        self.admin_auth = admin_auth
        self.admin_pwd = admin_pwd

    def run(self, vm, command, check_exit_code: bool = True):
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
                self.root_auth = None
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
                    self.root_auth = None
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
            return exit_code, stdout, stderr
        if not check_exit_code:
            status_tokens: List[str] = ["success"]
            if exit_code != 0:
                status_tokens.append(f"exit={exit_code}")
            if stderr_indicates_error:
                status_tokens.append("stderr-noted")
            print(f"[GUEST-CMD] Result: {'; '.join(status_tokens)}")
            return exit_code, stdout, stderr
        print("[GUEST-CMD] Result: failure")
        combined_cli_output = ((stderr or "") + "\n" + (stdout or "")).lower()
        reason = (stderr or "").strip() or "Unknown error"
        if "nmcli" in command and ("command not found" in combined_cli_output or exit_code == 127):
            raise NmcliNotAvailableError(command)
        if exit_code != 0:
            reason = (stderr or "").strip() or "Exit code was not 0"
        elif stderr_indicates_error:
            reason = (stderr or "").strip() or "Error text found in standard error output"
        if fallback_error is not None and auth_used == "admin":
            raise RuntimeError(
                f"Failed to run command as admin user (exit code {exit_code}, reason: {reason})"
            ) from fallback_error
        raise RuntimeError(f"Failed to execute command (exit code {exit_code}, reason: {reason})")

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
            'if [ -n \"$orig_lc_all\" ]; then export LC_ALL=\"$orig_lc_all\"; else unset LC_ALL; fi; '
            'if [ -n \"$orig_lang\" ]; then export LANG=\"$orig_lang\"; else unset LANG; fi; '
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
            return f"cat {shlex.quote(temp_password)} | script -q -c {shlex.quote(base_cmd)} /dev/null"
        return f"cat {shlex.quote(temp_password)} | {base_cmd}"

    @staticmethod
    def _requires_tty_retry(exit_code: int, stderr: str) -> bool:
        stderr_lower = (stderr or "").lower()
        return exit_code != 0 and ("no tty present" in stderr_lower or "must have a tty" in stderr_lower)


def execute_command_in_guest(
    guest_op_manager,
    vm,
    root_auth,
    admin_auth,
    admin_pwd,
    command,
    check_exit_code: bool = True,
):
    executor = GuestCommandExecutor(guest_op_manager, root_auth, admin_auth, admin_pwd)
    return executor.run(vm, command, check_exit_code=check_exit_code)


def reset_root_login_disabled() -> None:
    """Reset the module-level flag that tracks root authentication failures."""
    global ROOT_LOGIN_DISABLED
    ROOT_LOGIN_DISABLED = False
