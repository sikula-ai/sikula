from __future__ import annotations

import ctypes
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core import subprocess_utils
from core.subprocess_utils import (
    _resume_windows_process,
    _terminate_batch_process,
    _terminate_suspended_windows_process,
    _windows_kernel32,
    attach_windows_process_job,
    resolve_windows_batch_command,
    run_windows_batch_process,
    start_windows_process_job,
    terminate_windows_process_tree,
    windows_pid_running,
)


class TestWindowsKernelBindings:
    def test_configures_kernel32_function_signatures(self):
        kernel32 = MagicMock()
        _windows_kernel32.cache_clear()
        try:
            with (
                patch("core.subprocess_utils.sys.platform", "win32"),
                patch.object(ctypes, "WinDLL", return_value=kernel32, create=True) as win_dll,
            ):
                assert _windows_kernel32() is kernel32

            win_dll.assert_called_once_with("kernel32", use_last_error=True)
            assert kernel32.CreateJobObjectW.restype is subprocess_utils.wintypes.HANDLE
            assert kernel32.AssignProcessToJobObject.restype is subprocess_utils.wintypes.BOOL
            assert kernel32.ResumeThread.restype is subprocess_utils.wintypes.DWORD
            assert kernel32.CloseHandle.restype is subprocess_utils.wintypes.BOOL
        finally:
            _windows_kernel32.cache_clear()


class TestWindowsPidRunning:
    def test_rejects_non_positive_pid(self):
        with patch("core.subprocess_utils.os.name", "nt"):
            assert windows_pid_running(0) is False

    def test_returns_false_without_kernel_api(self):
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=None),
        ):
            assert windows_pid_running(1234) is False

    def test_reports_access_denied_process_as_running(self):
        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 0
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
            patch.object(ctypes, "get_last_error", return_value=5, create=True),
        ):
            assert windows_pid_running(1234) is True

    def test_returns_false_when_exit_code_lookup_fails(self):
        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 42
        kernel32.GetExitCodeProcess.return_value = False
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert windows_pid_running(1234) is False
        kernel32.CloseHandle.assert_called_once_with(42)


class TestWindowsJobSetup:
    def test_attach_rejects_process_without_windows_handle(self):
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=MagicMock()),
        ):
            assert attach_windows_process_job(MagicMock(spec=[])) is False

    @pytest.mark.parametrize("job_handle", [0, object()])
    def test_attach_rejects_invalid_job_handle(self, job_handle):
        process = MagicMock(_handle=1234)
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = job_handle
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert attach_windows_process_job(process) is False

        if job_handle:
            kernel32.CloseHandle.assert_called_once_with(job_handle)

    def test_attach_closes_job_when_assignment_fails(self):
        process = MagicMock(_handle=1234)
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 42
        kernel32.SetInformationJobObject.return_value = True
        kernel32.AssignProcessToJobObject.return_value = False
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert attach_windows_process_job(process) is False
        kernel32.CloseHandle.assert_called_once_with(42)

    def test_resume_rejects_missing_pid_or_snapshot(self):
        kernel32 = MagicMock()
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert _resume_windows_process(MagicMock(spec=[])) is False
            kernel32.CreateToolhelp32Snapshot.return_value = 0
            assert _resume_windows_process(MagicMock(pid=1234)) is False

    def test_resume_returns_false_when_primary_thread_cannot_open(self):
        process = MagicMock(pid=1234)
        kernel32 = MagicMock()
        kernel32.CreateToolhelp32Snapshot.return_value = 42
        kernel32.OpenThread.return_value = 0

        def first_thread(_snapshot, entry_pointer):
            entry_pointer._obj.th32OwnerProcessID = 1234
            entry_pointer._obj.th32ThreadID = 5678
            return True

        kernel32.Thread32First.side_effect = first_thread
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert _resume_windows_process(process) is False
        kernel32.CloseHandle.assert_called_once_with(42)

    def test_resume_returns_false_without_owned_thread(self):
        process = MagicMock(pid=1234)
        kernel32 = MagicMock()
        kernel32.CreateToolhelp32Snapshot.return_value = 42

        def unrelated_thread(_snapshot, entry_pointer):
            entry_pointer._obj.th32OwnerProcessID = 9999
            return True

        kernel32.Thread32First.side_effect = unrelated_thread
        kernel32.Thread32Next.return_value = False
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert _resume_windows_process(process) is False
        kernel32.CloseHandle.assert_called_once_with(42)

    def test_terminate_suspended_process_escalates_to_kill(self):
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 5), 0]

        _terminate_suspended_windows_process(process)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        assert process.wait.call_count == 2

    def test_terminate_suspended_process_swallows_kill_failure(self):
        process = MagicMock()
        process.terminate.side_effect = OSError("terminate failed")
        process.kill.side_effect = OSError("kill failed")

        _terminate_suspended_windows_process(process)

        process.kill.assert_called_once()

    def test_start_without_handle_is_not_applicable(self):
        assert start_windows_process_job(MagicMock(spec=[])) is None

    def test_start_terminates_process_when_attach_fails(self):
        process = MagicMock(_handle=1234)
        with (
            patch("core.subprocess_utils.attach_windows_process_job", return_value=False),
            patch("core.subprocess_utils._terminate_suspended_windows_process") as terminate,
        ):
            assert start_windows_process_job(process) is False
        terminate.assert_called_once_with(process)

    def test_start_releases_job_when_resume_fails(self):
        process = MagicMock(_handle=1234)
        process.wait.side_effect = subprocess.TimeoutExpired("provider", 5)
        with (
            patch("core.subprocess_utils.attach_windows_process_job", return_value=True),
            patch("core.subprocess_utils._resume_windows_process", return_value=False),
            patch("core.subprocess_utils.release_windows_process_job") as release,
        ):
            assert start_windows_process_job(process) is False
        release.assert_called_once_with(process)


class TestWindowsCommandResolution:
    def test_rejects_unrepresentable_batch_path(self):
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.windows_batch_command_path", return_value='C:\\bad\\"provider.cmd'),
            pytest.raises(ValueError, match="not safely representable"),
        ):
            resolve_windows_batch_command(["provider"])

    def test_avoids_existing_transport_environment_prefix(self):
        env = {
            "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "_SIKULA_BATCH_COMMAND": "occupied",
        }
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.windows_batch_command_path", return_value=r"C:\Tools\provider.cmd"),
        ):
            command, _, child_env = resolve_windows_batch_command(["provider"], env=env)

        assert "%_SIKULA_BATCH__COMMAND%" in command
        assert child_env is not None
        assert child_env["_SIKULA_BATCH__COMMAND"] == r"C:\Tools\provider.cmd"


class TestWindowsProcessCleanup:
    def test_job_cleanup_reports_wait_timeout(self):
        process = MagicMock(pid=1234)
        process.wait.side_effect = subprocess.TimeoutExpired("provider", 5)
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._terminate_windows_process_job", return_value=True),
        ):
            assert terminate_windows_process_tree(process) is False

    def test_completed_process_needs_no_taskkill(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = 0
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._terminate_windows_process_job", return_value=False),
            patch("core.subprocess_utils.subprocess.run") as run,
        ):
            assert terminate_windows_process_tree(process) is True
        run.assert_not_called()

    def test_taskkill_failures_return_false(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._terminate_windows_process_job", return_value=False),
            patch("core.subprocess_utils.subprocess.run", side_effect=OSError("taskkill failed")),
        ):
            assert terminate_windows_process_tree(process) is False

    def test_nonzero_taskkill_for_running_process_returns_false(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        result = MagicMock(returncode=1)
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._terminate_windows_process_job", return_value=False),
            patch("core.subprocess_utils.subprocess.run", return_value=result),
        ):
            assert terminate_windows_process_tree(process) is False

    def test_taskkill_wait_timeout_returns_false(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("provider", 5)
        result = MagicMock(returncode=0)
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._terminate_windows_process_job", return_value=False),
            patch("core.subprocess_utils.subprocess.run", return_value=result),
        ):
            assert terminate_windows_process_tree(process) is False

    def test_batch_cleanup_escalates_to_kill(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 5), 0]
        with patch("core.subprocess_utils.terminate_windows_process_tree", return_value=False):
            _terminate_batch_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()


class TestWindowsBatchProcess:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"input": "prompt", "stdin": subprocess.PIPE},
            {"capture_output": True, "stdout": subprocess.PIPE},
        ],
    )
    def test_rejects_conflicting_stream_arguments(self, kwargs):
        with pytest.raises(ValueError):
            run_windows_batch_process("provider.cmd", executable="cmd.exe", **kwargs)

    def test_rejects_failed_process_isolation(self):
        process = MagicMock()
        with (
            patch("core.subprocess_utils.subprocess.Popen", return_value=process),
            patch("core.subprocess_utils.start_windows_process_job", return_value=False),
            pytest.raises(OSError, match="isolation could not be initialized"),
        ):
            run_windows_batch_process("provider.cmd", executable="cmd.exe")

    def test_check_raises_for_nonzero_exit(self):
        process = MagicMock(returncode=3)
        process.communicate.return_value = ("output", "error")
        with (
            patch("core.subprocess_utils.subprocess.Popen", return_value=process),
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
            patch("core.subprocess_utils.release_windows_process_job"),
            pytest.raises(subprocess.CalledProcessError),
        ):
            run_windows_batch_process(
                "provider.cmd",
                executable="cmd.exe",
                capture_output=True,
                check=True,
            )
