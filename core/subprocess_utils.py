from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_WINDOWS_JOB_HANDLE_ATTR = "_sikula_windows_job_handle"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_DWORD = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


@lru_cache(maxsize=1)
def _windows_kernel32() -> Any | None:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def windows_pid_running(pid: int) -> bool | None:
    """Return Windows process liveness, or None outside Windows."""
    if os.name != "nt":
        return None
    if pid <= 0:
        return False
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    process_handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process_handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(process_handle)


def attach_windows_process_job(process: subprocess.Popen[str]) -> bool:
    """Attach a Windows process to a kill-on-close job object."""
    kernel32 = _windows_kernel32()
    process_handle = getattr(process, "_handle", None)
    if os.name != "nt" or kernel32 is None or not isinstance(process_handle, int):
        return False

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        return False
    job_handle_value = job_handle if isinstance(job_handle, int) else getattr(job_handle, "value", None)
    if not isinstance(job_handle_value, int):
        kernel32.CloseHandle(job_handle)
        return False
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job_handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(job_handle, wintypes.HANDLE(process_handle))
    if not assigned:
        kernel32.CloseHandle(job_handle)
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, job_handle_value)
    return True


def _resume_windows_process(process: subprocess.Popen[str]) -> bool:
    kernel32 = _windows_kernel32()
    pid = getattr(process, "pid", None)
    if os.name != "nt" or kernel32 is None or not isinstance(pid, int):
        return False

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return False
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    return False
                try:
                    return kernel32.ResumeThread(thread) != _INVALID_DWORD
                finally:
                    kernel32.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def _terminate_suspended_windows_process(process: subprocess.Popen[str]) -> None:
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def start_windows_process_job(process: subprocess.Popen[str]) -> bool | None:
    """Assign and resume a suspended process, or return None without a Windows handle."""
    if not isinstance(getattr(process, "_handle", None), int):
        return None
    if not attach_windows_process_job(process):
        _terminate_suspended_windows_process(process)
        return False
    if _resume_windows_process(process):
        return True
    release_windows_process_job(process)
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def release_windows_process_job(process: subprocess.Popen[str]) -> bool:
    """Terminate descendants left in a process job and close its handle."""
    kernel32 = _windows_kernel32()
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    if kernel32 is None or not isinstance(job_handle, int):
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    handle = wintypes.HANDLE(job_handle)
    terminated = bool(kernel32.TerminateJobObject(handle, 1))
    closed = bool(kernel32.CloseHandle(handle))
    return terminated and closed


def _terminate_windows_process_job(process: subprocess.Popen[str]) -> bool:
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    if not isinstance(job_handle, int):
        return False
    return release_windows_process_job(process)


def _windows_batch_argument(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows batch arguments cannot contain NUL, CR, or LF characters")

    safe_unquoted = "#$*+-./:?@\\_"
    quote = (
        not value
        or value.endswith("\\")
        or any(
            (char.isascii() and not (char.isalnum() or char in safe_unquoted))
            or ord(char) < 32
            or 127 <= ord(char) <= 159
            for char in value
        )
    )
    parts = ['"'] if quote else []
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            parts.append("\\" * backslashes)
            parts.append('"')
        parts.append("\\" * backslashes)
        parts.append(char)
        backslashes = 0
    parts.append("\\" * (backslashes * 2 if quote else backslashes))
    if quote:
        parts.append('"')
    return "".join(parts)


def _windows_env_value(env: Mapping[str, str], name: str) -> str | None:
    expected = name.upper()
    return next((value for key, value in env.items() if key.upper() == expected), None)


def windows_batch_command_path(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the resolved batch wrapper path for a Windows command."""
    if os.name != "nt" or not command:
        return None
    process_env = env if env is not None else os.environ
    resolved = shutil.which(command[0], path=_windows_env_value(process_env, "PATH"))
    if resolved is None or not resolved.lower().endswith((".bat", ".cmd")):
        return None
    return resolved


def resolve_windows_batch_command(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str] | str, str | None, dict[str, str] | None]:
    """Return a batch-safe command, cmd.exe path, and child environment."""
    if os.name != "nt" or not command:
        return command, None, None

    process_env = env if env is not None else os.environ
    resolved = windows_batch_command_path(command, env=process_env)
    if resolved is None:
        return command, None, None
    if '"' in resolved or resolved.endswith("\\"):
        raise ValueError("Windows batch command path is not safely representable")

    comspec = _windows_env_value(process_env, "COMSPEC") or "cmd.exe"
    transport_env = dict(process_env)
    prefix = "_SIKULA_BATCH_"
    existing_names = {name.upper() for name in transport_env}
    while any(name.startswith(prefix) for name in existing_names):
        prefix += "_"
    command_name = f"{prefix}COMMAND"
    transport_env[command_name] = _windows_batch_argument(resolved)
    argument_names: list[str] = []
    for index, argument in enumerate(command[1:]):
        name = f"{prefix}ARG_{index}"
        transport_env[name] = _windows_batch_argument(argument)
        argument_names.append(name)

    command_line = [
        subprocess.list2cmdline([comspec]),
        "/e:on",
        "/v:off",
        "/d",
        "/c",
        f'"%{command_name}%',
        *(f"%{name}%" for name in argument_names),
    ]
    return " ".join(command_line) + '"', comspec, transport_env


def terminate_windows_process_tree(process: subprocess.Popen[str]) -> bool:
    """Terminate a Windows process group and its descendants."""
    pid = getattr(process, "pid", None)
    if os.name != "nt" or not isinstance(pid, int):
        return False
    if _terminate_windows_process_job(process):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        return True
    if process.poll() is not None:
        return True

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 and process.poll() is None:
        return False
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate_batch_process(process: subprocess.Popen[str]) -> None:
    if terminate_windows_process_tree(process) or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_windows_batch_process(
    args: str,
    *,
    executable: str,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a resolved Windows batch command with process-tree-aware timeouts."""
    input_value = kwargs.pop("input", None)
    capture_output = bool(kwargs.pop("capture_output", False))
    timeout = kwargs.pop("timeout", None)
    check = bool(kwargs.pop("check", False))
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    kwargs["executable"] = executable
    kwargs["creationflags"] = (
        int(kwargs.get("creationflags", 0))
        | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        | int(getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
    )
    process = subprocess.Popen(args, **kwargs)
    if not start_windows_process_job(process):
        raise OSError("Windows batch process isolation could not be initialized")
    try:
        stdout, stderr = process.communicate(input_value, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_batch_process(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    except BaseException:
        _terminate_batch_process(process)
        raise
    finally:
        release_windows_process_job(process)

    result = subprocess.CompletedProcess(args, process.returncode or 0, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def run_windows_shell_process(
    command: str,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a trusted shell command with Windows process-tree cleanup."""
    process_env = kwargs.get("env") or os.environ
    comspec = _windows_env_value(process_env, "COMSPEC") or "cmd.exe"
    return run_windows_batch_process(
        command,
        executable=comspec,
        shell=True,
        **kwargs,
    )
