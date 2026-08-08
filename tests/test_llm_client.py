"""Tests for core/llm_client.py — factory, helpers, parse functions."""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from core.llm_client import (
    LLMClient,
    LLMAuthError,
    LLMConfig,
    LLMConfigurationError,
    LLMEnvironmentError,
    LLMProviderError,
    LLMQuotaExceeded,
    LLMTransientError,
    LLMTimeoutError,
    _agent_text_or_empty,
    _AntigravityCopyPolicy,
    _antigravity_copy_ignore,
    _antigravity_copy_policy,
    _antigravity_directory_snapshot,
    _antigravity_git_paths,
    _antigravity_gitlink_paths,
    _antigravity_log_diagnostic,
    _antigravity_log_line_diagnostic,
    _antigravity_marker_text,
    _antigravity_parse_version,
    _antigravity_reported_tokens,
    _antigravity_require_supported_version,
    _antigravity_redact_diagnostic,
    _antigravity_result_envelope,
    _antigravity_result_error,
    _antigravity_sanitize_readonly_output,
    _antigravity_snapshot_changed,
    _antigravity_validate_workspace_symlink,
    _antigravity_validate_workspace_symlinks,
    _antigravity_write_agent_prompt,
    _call_with_retry,
    _claude_result_envelope,
    _claude_write_settings,
    _codex_parse_text,
    _codex_reported_tokens,
    _codex_stream_error,
    _codex_subprocess_error,
    _git_exclude_file,
    _gemini_parse_response,
    _gemini_write_settings,
    _opencode_log_error,
    _opencode_stream_error,
    _provider_error,
    _run_agent_subprocess_streaming,
    _opencode_agent_env,
    _opencode_parse_text,
    _run_provider_cli,
    _run_opencode_streaming,
    _terminate_process,
    create_llm_client,
)
from core.llm_client import AntigravityClient, ClaudeClient, CodexClient, GeminiClient, OpenCodeClient
from core.subprocess_utils import (
    _resume_windows_process,
    _windows_batch_argument,
    attach_windows_process_job,
    release_windows_process_job,
    resolve_windows_batch_command,
    run_windows_shell_process,
    start_windows_process_job,
    terminate_windows_process_tree,
)


class TestCreateLlmClient:
    def test_claude_provider(self):
        cfg = LLMConfig(provider="claude")
        assert isinstance(create_llm_client(cfg), ClaudeClient)

    def test_opencode_provider(self):
        cfg = LLMConfig(provider="opencode")
        assert isinstance(create_llm_client(cfg), OpenCodeClient)

    def test_gemini_provider(self):
        cfg = LLMConfig(provider="gemini")
        assert isinstance(create_llm_client(cfg), GeminiClient)

    def test_codex_provider(self):
        cfg = LLMConfig(provider="codex")
        assert isinstance(create_llm_client(cfg), CodexClient)

    def test_antigravity_provider(self):
        cfg = LLMConfig(provider="antigravity")
        with patch("core.llm_client._antigravity_require_supported_version") as version_check:
            assert isinstance(create_llm_client(cfg), AntigravityClient)
        version_check.assert_not_called()

    def test_unknown_provider_raises(self):
        cfg = LLMConfig(provider="unknown")
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_llm_client(cfg)


class TestCallWithRetry:
    def test_returns_on_first_success(self):
        fn = MagicMock(return_value="ok")
        with patch("core.llm_client.time.sleep"):
            result = _call_with_retry("test", fn)
        assert result == "ok"
        assert fn.call_count == 1

    def test_retries_on_runtime_error(self):
        fn = MagicMock(side_effect=[LLMTransientError("fail"), LLMTransientError("fail"), "ok"])
        with patch("core.llm_client.time.sleep"):
            result = _call_with_retry("test", fn)
        assert result == "ok"
        assert fn.call_count == 3

    def test_notifies_retry_observer(self):
        observer = MagicMock()
        cfg = LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer)
        fn = MagicMock(side_effect=[LLMTransientError("fail"), "ok"])

        with patch("core.llm_client.time.sleep"):
            result = _call_with_retry("test", fn, cfg, "generate")

        assert result == "ok"
        observer.assert_called_once()
        event = observer.call_args.args[0]
        assert event["provider"] == "codex"
        assert event["model"] == "gpt-5.3-codex"
        assert event["operation"] == "generate"
        assert event["attempt"] == 1
        assert event["max_attempts"] == 4
        assert event["delay_s"] == 30
        assert event["error"] == "fail"
        assert event["error_type"] == "LLMTransientError"

    def test_records_each_provider_attempt_without_error_content(self):
        events = []
        cfg = LLMConfig(
            provider="codex",
            model="gpt-5.3-codex",
            usage_observer=events.append,
        )
        fn = MagicMock(side_effect=[LLMTransientError("PRIVATE_PROVIDER_ERROR"), "ok"])

        with patch("core.llm_client.time.sleep"):
            assert _call_with_retry("test", fn, cfg, "generate", input_chars=42) == "ok"

        assert [event["attempt"] for event in events] == [1, 2]
        assert [event["outcome"] for event in events] == ["retryable_error", "success"]
        assert all(event["input_chars"] == 42 for event in events)
        assert events[0]["error_type"] == "LLMTransientError"
        assert events[1]["output_chars"] == 2
        assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(events)

    def test_records_fatal_attempt_without_retrying(self):
        events = []
        cfg = LLMConfig(provider="codex", usage_observer=events.append)
        fn = MagicMock(side_effect=LLMQuotaExceeded("quota exhausted"))

        with pytest.raises(LLMQuotaExceeded, match="quota exhausted"):
            _call_with_retry("test", fn, cfg, "generate", input_chars=5)

        assert fn.call_count == 1
        assert len(events) == 1
        assert events[0]["outcome"] == "fatal_error"
        assert events[0]["error_type"] == "LLMQuotaExceeded"

    def test_records_timeout_and_retry_as_separate_attempts(self):
        events = []
        cfg = LLMConfig(provider="codex", usage_observer=events.append)
        fn = MagicMock(side_effect=[subprocess.TimeoutExpired("codex", 10), "ok"])

        with patch("core.llm_client.time.sleep"):
            assert _call_with_retry("test", fn, cfg, "generate", input_chars=5) == "ok"

        assert [event["outcome"] for event in events] == ["timeout", "success"]
        assert [event["attempt"] for event in events] == [1, 2]
        assert events[0]["error_type"] == "TimeoutExpired"

    def test_usage_observer_failure_does_not_change_provider_result(self):
        def broken_observer(event):
            raise RuntimeError("observer failed")

        cfg = LLMConfig(provider="codex", usage_observer=broken_observer)

        assert _call_with_retry("test", lambda: "ok", cfg, "generate", input_chars=1) == "ok"

    def test_retry_observer_error_keeps_head_and_tail_when_truncated(self):
        observer = MagicMock()
        cfg = LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer)
        fn = MagicMock(side_effect=[LLMTransientError("head-" + "x" * 1200 + "-tail-error"), "ok"])

        with patch("core.llm_client.time.sleep"):
            _call_with_retry("test", fn, cfg, "generate")

        error = observer.call_args.args[0]["error"]
        assert len(error) < 1100
        assert error.startswith("head-")
        assert "\n... [truncated] ...\n" in error
        assert error.endswith("-tail-error")

    def test_set_retry_observer_tolerates_custom_config_shape(self):
        class CustomClient(LLMClient):
            def __init__(self):
                self._config = {"provider": "custom"}

        client = CustomClient()
        observer = MagicMock()

        assert client.set_retry_observer(observer) is None
        assert client._retry_observer is observer
        assert client.set_retry_observer(None) is observer

    def test_retries_on_timeout_expired(self):
        fn = MagicMock(side_effect=[subprocess.TimeoutExpired("cmd", 30), "ok"])
        with patch("core.llm_client.time.sleep"):
            result = _call_with_retry("test", fn)
        assert result == "ok"
        assert fn.call_count == 2

    def test_notifies_retry_observer_on_timeout_expired(self):
        observer = MagicMock()
        cfg = LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer)
        fn = MagicMock(side_effect=[subprocess.TimeoutExpired("cmd", 30), "ok"])

        with patch("core.llm_client.time.sleep"):
            result = _call_with_retry("test", fn, cfg, "run_agent")

        assert result == "ok"
        observer.assert_called_once()
        event = observer.call_args.args[0]
        assert event["operation"] == "run_agent"
        assert event["error_type"] == "LLMTimeoutError"

    def test_raises_timeout_after_all_retries_exhausted(self):
        fn = MagicMock(side_effect=subprocess.TimeoutExpired("cmd", 30))

        with patch("core.llm_client.time.sleep"):
            with pytest.raises(LLMTimeoutError, match="timed out after 30s"):
                _call_with_retry("test", fn)

        assert fn.call_count == 4

    def test_raises_after_all_retries_exhausted(self):
        fn = MagicMock(side_effect=LLMTransientError("always fails"))
        with patch("core.llm_client.time.sleep"):
            with pytest.raises(LLMTransientError, match="always fails"):
                _call_with_retry("test", fn)
        assert fn.call_count == 4  # 1 initial + 3 retries

    def test_does_not_retry_fatal_provider_error(self):
        fn = MagicMock(side_effect=LLMQuotaExceeded("quota exhausted"))
        with (
            patch("core.llm_client.time.sleep") as sleep,
            pytest.raises(LLMQuotaExceeded, match="quota exhausted"),
        ):
            _call_with_retry("test", fn)
        assert fn.call_count == 1
        sleep.assert_not_called()

    def test_local_environment_os_error_is_fatal(self):
        fn = MagicMock(side_effect=PermissionError(errno.EACCES, "Permission denied", "codex"))
        with (
            patch("core.llm_client.time.sleep") as sleep,
            pytest.raises(LLMEnvironmentError, match="local environment error"),
        ):
            _call_with_retry("test", fn)

        assert fn.call_count == 1
        sleep.assert_not_called()

    def test_local_environment_os_error_is_observed_as_fatal(self):
        events = []
        cfg = LLMConfig(provider="codex", usage_observer=events.append)
        fn = MagicMock(side_effect=PermissionError(errno.EACCES, "Permission denied", "codex"))

        with pytest.raises(LLMEnvironmentError, match="local environment error"):
            _call_with_retry("generate", fn, cfg, "generate")

        assert fn.call_count == 1
        assert events[0]["outcome"] == "fatal_error"
        assert events[0]["error_type"] == "LLMEnvironmentError"

    def test_other_os_error_is_not_reclassified(self):
        fn = MagicMock(side_effect=OSError(errno.EINVAL, "bad file descriptor"))
        with pytest.raises(OSError, match="bad file descriptor"):
            _call_with_retry("test", fn)

        assert fn.call_count == 1

    def test_does_not_retry_plain_runtime_error(self):
        fn = MagicMock(side_effect=RuntimeError("not classified"))
        with pytest.raises(RuntimeError, match="not classified"):
            _call_with_retry("test", fn)
        assert fn.call_count == 1

    def test_does_not_retry_other_exceptions(self):
        fn = MagicMock(side_effect=ValueError("not retried"))
        with pytest.raises(ValueError):
            _call_with_retry("test", fn)
        assert fn.call_count == 1


class TestProviderErrorClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "failed to initialize in-process app-server client: Read-only file system",
            "could not create runtime socket: Permission denied",
            "provider cache write failed: No space left on device",
        ],
    )
    def test_provider_output_environment_phrases_remain_retryable(self, message: str):
        error = _provider_error("provider", "agent", message)

        assert isinstance(error, LLMTransientError)

    def test_temporary_provider_errors_remain_retryable(self):
        error = _provider_error("provider", "agent", "temporary policy error")

        assert isinstance(error, LLMTransientError)


class TestAgentTextOrEmpty:
    def test_returns_empty_for_no_text_transient_error(self):
        def parse(_output: str) -> str:
            raise LLMTransientError("codex output error: returned no text output")

        assert _agent_text_or_empty(parse, "") == ""

    def test_reraises_other_transient_errors(self):
        def parse(_output: str) -> str:
            raise LLMTransientError("temporary provider failure")

        with pytest.raises(LLMTransientError, match="temporary provider failure"):
            _agent_text_or_empty(parse, "")


class TestOpencodeParsText:
    def _line(self, event: dict) -> str:
        return json.dumps(event)

    def test_extracts_text_parts(self):
        ndjson = "\n".join(
            [
                self._line({"type": "text", "part": {"text": "Hello"}}),
                self._line({"type": "text", "part": {"text": "World"}}),
            ]
        )
        result = _opencode_parse_text(ndjson)
        assert "Hello" in result
        assert "World" in result

    def test_skips_non_text_events(self):
        ndjson = "\n".join(
            [
                self._line({"type": "tool_use", "name": "read_file"}),
                self._line({"type": "text", "part": {"text": "answer"}}),
            ]
        )
        assert _opencode_parse_text(ndjson) == "answer"

    def test_raises_on_error_event(self):
        ndjson = self._line({"type": "error", "error": {"name": "AuthError", "data": {"message": "no auth"}}})
        with pytest.raises(RuntimeError, match="no auth"):
            _opencode_parse_text(ndjson)

    def test_skips_invalid_json_lines(self):
        ndjson = "not json\n" + self._line({"type": "text", "part": {"text": "ok"}})
        assert _opencode_parse_text(ndjson) == "ok"

    def test_skips_non_object_json_lines(self):
        ndjson = json.dumps(["not", "an", "event"]) + "\n" + self._line({"type": "text", "part": {"text": "ok"}})
        assert _opencode_parse_text(ndjson) == "ok"

    def test_empty_text_parts_excluded(self):
        ndjson = self._line({"type": "text", "part": {"text": "   "}})
        assert _opencode_parse_text(ndjson) == ""

    def test_raises_on_string_error_event(self):
        ndjson = self._line({"type": "error", "error": "not authenticated"})
        with pytest.raises(LLMAuthError, match="not authenticated"):
            _opencode_parse_text(ndjson)

    @pytest.mark.parametrize("line", ["", "not json", json.dumps(["not", "object"])])
    def test_stream_error_ignores_non_error_lines(self, line: str):
        assert _opencode_stream_error(line) is None

    def test_stream_error_returns_provider_error(self):
        error = _opencode_stream_error(self._line({"type": "error", "error": {"data": {"message": "invalid model"}}}))
        assert isinstance(error, LLMConfigurationError)

    @pytest.mark.parametrize(
        ("event", "expected_error"),
        [
            ({"type": "error", "error": {"data": {"message": "not authenticated"}}}, LLMAuthError),
            ({"type": "error", "error": {"message": "not authenticated"}}, LLMAuthError),
            ({"type": "error", "error": {"data": {"message": "quota exceeded"}}}, LLMQuotaExceeded),
            ({"type": "error", "error": {"message": "quota exceeded"}}, LLMQuotaExceeded),
            ({"type": "error", "message": "quota exceeded"}, LLMQuotaExceeded),
            ({"type": "error", "error": {"data": {"message": "invalid model"}}}, LLMConfigurationError),
            ({"type": "error", "error": {"message": "invalid model"}}, LLMConfigurationError),
            ({"type": "error", "error": {"data": {"type": "usage_limit_reached"}}}, LLMQuotaExceeded),
            ({"type": "error", "error": {"data": {"code": "unsupported_value"}}}, LLMConfigurationError),
        ],
    )
    def test_stream_error_classifies_supported_message_shapes(
        self,
        event: dict,
        expected_error: type[LLMProviderError],
    ):
        error = _opencode_stream_error(self._line(event))
        assert isinstance(error, expected_error)

        with pytest.raises(expected_error):
            _opencode_parse_text(self._line(event))

    def test_stream_error_uses_credit_headers_without_exposing_raw_payload(self):
        event = {
            "type": "error",
            "error": {
                "data": {
                    "message": "SECRET_PROVIDER_MESSAGE",
                    "headers": {
                        "x-codex-credits-balance": "0",
                        "x-codex-credits-has-credits": "False",
                    },
                }
            },
        }

        error = _opencode_stream_error(self._line(event))

        assert isinstance(error, LLMQuotaExceeded)
        assert "quota exceeded" in str(error)
        assert "SECRET_PROVIDER_MESSAGE" not in str(error)


class TestStreamingProcessHelpers:
    def test_terminate_process_returns_when_process_already_exited(self):
        process = MagicMock()
        process.poll.return_value = 0

        _terminate_process(process)

        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_terminate_process_kills_when_graceful_wait_times_out(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("agent", 5), 0]

        _terminate_process(process)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        assert process.wait.call_count == 2

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signaling")
    def test_terminate_process_group_escalates_after_main_process_exited(self):
        process = MagicMock()
        process.poll.return_value = 0

        with (
            patch("core.llm_client._signal_process_group", return_value=True) as signal_group,
            patch("core.llm_client.time.sleep") as sleep,
        ):
            _terminate_process(process, process_group=True)

        assert signal_group.call_args_list == [
            ((process, signal.SIGTERM),),
            ((process, signal.SIGKILL),),
        ]
        sleep.assert_called_once_with(0.2)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_process_group_uses_taskkill(self):
        process = MagicMock(pid=1234)
        process.poll.return_value = None
        process.wait.return_value = 0
        taskkill_result = MagicMock(returncode=0)

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.subprocess.run", return_value=taskkill_result) as run,
        ):
            assert terminate_windows_process_tree(process) is True

        process.send_signal.assert_not_called()
        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )

    def test_windows_process_job_cleans_up_after_leader_exits(self):
        process = MagicMock(pid=1234)
        process._sikula_windows_job_handle = 42
        process.poll.return_value = 0
        process.wait.return_value = 0
        kernel32 = MagicMock()
        kernel32.TerminateJobObject.return_value = True
        kernel32.CloseHandle.return_value = True

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
            patch("core.subprocess_utils.subprocess.run") as run,
        ):
            assert terminate_windows_process_tree(process) is True

        assert kernel32.TerminateJobObject.call_args.args[0].value == 42
        assert kernel32.CloseHandle.call_args.args[0].value == 42
        assert process._sikula_windows_job_handle is None
        process.wait.assert_called_once_with(timeout=5)
        run.assert_not_called()

    def test_windows_process_job_is_attached_and_released(self):
        process = MagicMock(pid=1234, _handle=5678)
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 42
        kernel32.SetInformationJobObject.return_value = True
        kernel32.AssignProcessToJobObject.return_value = True
        kernel32.CloseHandle.return_value = True

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert attach_windows_process_job(process) is True
            assert process._sikula_windows_job_handle == 42
            assert release_windows_process_job(process) is True

        assert process._sikula_windows_job_handle is None
        assert kernel32.AssignProcessToJobObject.call_args.args[1].value == 5678
        assert kernel32.TerminateJobObject.call_args.args[0].value == 42
        assert kernel32.CloseHandle.call_args.args[0].value == 42

    def test_windows_suspended_process_resumes_primary_thread(self):
        process = MagicMock(pid=1234)
        kernel32 = MagicMock()
        kernel32.CreateToolhelp32Snapshot.return_value = 42
        kernel32.OpenThread.return_value = 43
        kernel32.ResumeThread.return_value = 1

        def first_thread(_snapshot, entry_pointer):
            entry_pointer._obj.th32OwnerProcessID = 1234
            entry_pointer._obj.th32ThreadID = 5678
            return True

        kernel32.Thread32First.side_effect = first_thread

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils._windows_kernel32", return_value=kernel32),
        ):
            assert _resume_windows_process(process) is True

        kernel32.OpenThread.assert_called_once_with(0x0002, False, 5678)
        kernel32.ResumeThread.assert_called_once_with(43)
        assert kernel32.CloseHandle.call_args_list == [call(43), call(42)]

    def test_windows_process_job_attaches_before_resume(self):
        process = MagicMock(_handle=1234)
        events: list[str] = []

        with (
            patch(
                "core.subprocess_utils.attach_windows_process_job",
                side_effect=lambda _process: events.append("attach") or True,
            ),
            patch(
                "core.subprocess_utils._resume_windows_process",
                side_effect=lambda _process: events.append("resume") or True,
            ),
        ):
            assert start_windows_process_job(process) is True

        assert events == ["attach", "resume"]

    def test_streaming_agent_popen_environment_os_error_is_fatal(self, tmp_path: Path):
        with (
            patch(
                "core.llm_client.subprocess.Popen",
                side_effect=PermissionError(errno.EACCES, "Permission denied", "provider"),
            ),
            pytest.raises(LLMEnvironmentError, match="provider agent local environment error"),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

    def test_streaming_agent_resolves_windows_batch_wrapper(self, tmp_path: Path):
        resolved = r"C:\Tools\provider.CMD"
        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.llm_client.subprocess.Popen", side_effect=FileNotFoundError) as popen,
            pytest.raises(LLMConfigurationError, match="provider CLI not found"),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

        assert popen.call_args.args[0].endswith(r'/c "%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0%"')
        assert popen.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert popen.call_args.kwargs["creationflags"] == 0x00000204
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == r"C:\Tools\provider.CMD"
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_ARG_0"] == "agent"

    def test_streaming_agent_pins_utf8_pipe_encoding(self, tmp_path: Path):
        # The prompt is written to stdin on the streaming path; the pipe must use
        # UTF-8 so a locale codec (e.g. cp1250) cannot fail to encode the prompt.
        with (
            patch("core.llm_client.subprocess.Popen", side_effect=FileNotFoundError) as popen,
            pytest.raises(LLMConfigurationError),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )
        assert popen.call_args.kwargs["encoding"] == "utf-8"
        assert popen.call_args.kwargs["errors"] == "replace"
        assert popen.call_args.kwargs["text"] is True

    def test_streaming_windows_process_starts_job_before_prompt(self, tmp_path: Path):
        class RecordingStdin:
            def __init__(self) -> None:
                self.writes: list[str] = []

            def write(self, value: str) -> int:
                self.writes.append(value)
                return len(value)

            def close(self) -> None:
                return None

        process = MagicMock(returncode=0)
        process.stdin = RecordingStdin()
        process.stdout = StringIO()
        process.stderr = StringIO()
        process.poll.return_value = 0
        resolved = r"C:\Tools\provider.CMD"

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.llm_client.subprocess.Popen", return_value=process),
            patch("core.llm_client.start_windows_process_job", return_value=True) as start_job,
            patch("core.llm_client.release_windows_process_job", return_value=True),
        ):
            result = _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
                stdin_text="prompt",
            )

        assert result.returncode == 0
        start_job.assert_called_once_with(process)
        assert process.stdin.writes == ["prompt"]

    def test_streaming_windows_native_process_starts_job(self, tmp_path: Path):
        process = MagicMock(returncode=0)
        process.stdin = StringIO()
        process.stdout = StringIO()
        process.stderr = StringIO()
        process.poll.return_value = 0

        with (
            patch("core.llm_client.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=r"C:\Tools\provider.exe"),
            patch("core.llm_client.subprocess.Popen", return_value=process) as popen,
            patch("core.llm_client.start_windows_process_job", return_value=True) as start_job,
        ):
            result = _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

        assert result.returncode == 0
        assert popen.call_args.kwargs["creationflags"] == 0x00000204
        start_job.assert_called_once_with(process)

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows job objects")
    def test_streaming_native_completion_terminates_descendant_after_leader_exits(self, tmp_path: Path):
        started = tmp_path / "child-started"
        survived = tmp_path / "child-survived"
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started')\n"
            "time.sleep(3)\n"
            "pathlib.Path(sys.argv[2]).write_text('survived')\n",
            encoding="utf-8",
        )
        parent_script = tmp_path / "parent.py"
        parent_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen(\n"
            "    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "started = pathlib.Path(sys.argv[2])\n"
            "deadline = time.monotonic() + 2\n"
            "while not started.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n",
            encoding="utf-8",
        )

        result = _run_agent_subprocess_streaming(
            [sys.executable, str(parent_script), str(child_script), str(started), str(survived)],
            cwd=tmp_path,
            env=None,
            timeout=5,
            provider="provider",
        )

        assert result.returncode == 0
        assert started.exists()
        time.sleep(2.5)
        assert not survived.exists()

    def test_streaming_agent_timeout_terminates_process(self, tmp_path: Path):
        class HangingProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO()
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = HangingProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=0,
                provider="provider",
            )

        assert processes[0].terminated is True

    def test_streaming_agent_interruption_terminates_process_group(self, tmp_path: Path):
        process = MagicMock(returncode=None)
        process.stdin = StringIO()
        process.stdout = StringIO()
        process.stderr = StringIO()

        with (
            patch("core.llm_client.subprocess.Popen", return_value=process),
            patch("core.llm_client.time.monotonic", side_effect=KeyboardInterrupt),
            patch("core.llm_client._terminate_process") as terminate,
            pytest.raises(KeyboardInterrupt),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

        terminate.assert_called_once_with(process, process_group=True)

    def test_streaming_agent_timeout_applies_while_stdin_write_blocks(self, tmp_path: Path):
        class BlockingStdin:
            def __init__(self, release: threading.Event) -> None:
                self._release = release
                self.closed = False

            def write(self, _text: str) -> int:
                self._release.wait(timeout=5)
                return 0

            def close(self) -> None:
                self.closed = True

        class StalledPromptProcess:
            def __init__(self, *args, **kwargs) -> None:
                self._release_stdin = threading.Event()
                self.stdin = BlockingStdin(self._release_stdin)
                self.stdout = StringIO()
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15
                self._release_stdin.set()

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9
                self._release_stdin.set()

        processes = []

        def fake_popen(*args, **kwargs):
            process = StalledPromptProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=0,
                provider="provider",
                stdin_text="large prompt",
            )

        assert processes[0].terminated is True

    def test_streaming_agent_timeout_applies_while_draining_large_output(self, tmp_path: Path):
        class VerboseHangingProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO("x" * 500_000)
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = VerboseHangingProcess(*args, **kwargs)
            processes.append(process)
            return process

        class ImmediateThread:
            def __init__(self, target, args=(), **_kwargs) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

            def is_alive(self) -> bool:
                return False

            def join(self, timeout=None) -> None:
                return None

        monotonic_values = iter([0.0, 0.0, 0.002, 0.003, 0.004])

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            patch("core.llm_client.threading.Thread", side_effect=ImmediateThread),
            patch("core.llm_client.time.monotonic", side_effect=lambda: next(monotonic_values, 0.005)),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=0.001,
                provider="provider",
            )

        assert processes[0].terminated is True

    def test_streaming_agent_parses_unterminated_pipe_error_before_timeout(self, tmp_path: Path):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"type":"error","error":"not authenticated"}')

        class LivePipeErrorProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = os.fdopen(read_fd, "r", encoding="utf-8")
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False
                self._write_fd_closed = False

            def poll(self):
                return self.returncode

            def _close_write_fd(self) -> None:
                if self._write_fd_closed:
                    return
                self._write_fd_closed = True
                try:
                    os.close(write_fd)
                except OSError:
                    pass

            def terminate(self):
                self.terminated = True
                self.returncode = -15
                self._close_write_fd()

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9
                self._close_write_fd()

        processes = []

        def fake_popen(*args, **kwargs):
            process = LivePipeErrorProcess(*args, **kwargs)
            processes.append(process)
            return process

        started_at = time.monotonic()
        try:
            with (
                patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
                pytest.raises(LLMAuthError, match="not authenticated"),
            ):
                _run_agent_subprocess_streaming(
                    ["provider", "agent"],
                    cwd=tmp_path,
                    env=None,
                    timeout=1,
                    provider="provider",
                    stdout_error_parser=_opencode_stream_error,
                )
        finally:
            if processes:
                processes[0]._close_write_fd()
            else:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

        assert processes[0].terminated is True
        assert time.monotonic() - started_at < 0.5

    def test_streaming_agent_parses_unterminated_real_subprocess_error_before_timeout(self, tmp_path: Path):
        script = (
            "import sys, time\n"
            'sys.stdout.write(\'{"type":"error","error":"not authenticated"}\')\n'
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )

        started_at = time.monotonic()
        with pytest.raises(LLMAuthError, match="not authenticated"):
            _run_agent_subprocess_streaming(
                [sys.executable, "-c", script],
                cwd=tmp_path,
                env=None,
                timeout=5,
                provider="provider",
                stdout_error_parser=_opencode_stream_error,
            )

        assert time.monotonic() - started_at < 1

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows process groups")
    def test_streaming_timeout_terminates_windows_descendant(self, tmp_path: Path):
        started = tmp_path / "child-started"
        survived = tmp_path / "child-survived"
        child_script = (
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started')\n"
            "time.sleep(3)\n"
            "pathlib.Path(sys.argv[2]).write_text('survived')\n"
        )
        parent_script = (
            "import pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child_script!r}, {str(started)!r}, {str(survived)!r}])\n"
            f"started = pathlib.Path({str(started)!r})\n"
            "deadline = time.monotonic() + 2\n"
            "while not started.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "time.sleep(30)\n"
        )

        with pytest.raises(subprocess.TimeoutExpired):
            _run_agent_subprocess_streaming(
                [sys.executable, "-c", parent_script],
                cwd=tmp_path,
                env=None,
                timeout=1,
                provider="provider",
            )

        assert started.exists()
        time.sleep(2.5)
        assert not survived.exists()

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows job objects")
    def test_streaming_completion_terminates_descendant_after_parent_exits(self, tmp_path: Path):
        started = tmp_path / "child-started"
        survived = tmp_path / "child-survived"
        child_script = (
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started')\n"
            "time.sleep(3)\n"
            "pathlib.Path(sys.argv[2]).write_text('survived')\n"
        )
        parent_script = (
            "import pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child_script!r}, {str(started)!r}, {str(survived)!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"started = pathlib.Path({str(started)!r})\n"
            "deadline = time.monotonic() + 2\n"
            "while not started.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
        )

        result = _run_agent_subprocess_streaming(
            [sys.executable, "-c", parent_script],
            cwd=tmp_path,
            env=None,
            timeout=5,
            provider="provider",
        )

        assert result.returncode == 0
        assert started.exists()
        time.sleep(2.5)
        assert not survived.exists()

    def test_streaming_agent_propagates_non_pipe_stdin_write_errors(self, tmp_path: Path):
        class FailingStdin:
            def write(self, _text: str) -> int:
                raise OSError(errno.EINVAL, "bad file descriptor")

            def close(self) -> None:
                pass

        class RunningProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = FailingStdin()
                self.stdout = StringIO()
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = RunningProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(OSError, match="bad file descriptor"),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
                stdin_text="prompt",
            )

        assert processes[0].terminated is True

    def test_streaming_agent_stops_on_stderr_structured_error(self, tmp_path: Path):
        class StderrErrorProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO()
                self.stderr = StringIO(json.dumps({"type": "error", "error": "not authenticated"}))
                self.returncode = None
                self.terminated = False
                self._polls = 0

            def poll(self):
                self._polls += 1
                if self._polls > 50 and self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = StderrErrorProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
                stderr_error_parser=_opencode_stream_error,
            )

        assert processes[0].terminated is True


class TestOpenCodeClientCommands:
    @staticmethod
    def _line(event: dict) -> str:
        return json.dumps(event)

    @classmethod
    def _run_result(cls, text: str = "ok", token_steps: list[dict[str, object]] | None = None):
        lines = [cls._line({"type": "text", "part": {"text": text}})]
        for tokens in token_steps or []:
            lines.append(cls._line({"type": "step_finish", "part": {"tokens": tokens}}))
        return MagicMock(
            returncode=0,
            stdout="\n".join(lines),
            stderr="",
        )

    def test_agent_env_writes_generated_agents_outside_repo(self, tmp_path: Path):
        repo_root = tmp_path / "repo"
        cwd = repo_root / ".sikula" / "worktrees" / "task"
        cwd.mkdir(parents=True)

        with _opencode_agent_env() as env:
            config_dir = Path(env["OPENCODE_CONFIG_DIR"])
            assert (config_dir / "agent" / "sikula-readonly.md").exists()
            assert (config_dir / "agent" / "sikula-implementer.md").exists()

        assert not (cwd / ".opencode").exists()
        assert not (repo_root / ".opencode").exists()

    def test_generate_passes_prompt_via_stdin(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex", agent_timeout=123))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["opencode", "run"]
        assert "system\n\nuser" not in cmd
        assert cmd[cmd.index("--title") + 1] == "sikula-generate"
        assert "--print-logs" in cmd
        assert cmd[cmd.index("--log-level") + 1] == "ERROR"
        assert mock_run.call_args.kwargs["timeout"] == 123
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"

    def test_generate_reports_aggregated_step_usage(self):
        events = []
        client = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                usage_observer=events.append,
            )
        )
        result = self._run_result(
            token_steps=[
                {"input": 10, "output": 2, "cache": {"read": 4, "write": 1}},
                {"input": 5, "output": 3, "cache": {"read": 2, "write": 0}},
                {"input": -1, "output": True, "cache": {"read": "invalid"}},
            ]
        )

        with patch("core.llm_client.subprocess.run", return_value=result):
            assert client.generate("system", "user") == "ok"

        assert events[0]["reported_tokens"] == {
            "input_tokens": 15,
            "output_tokens": 5,
            "cached_input_tokens": 6,
            "cache_creation_input_tokens": 1,
        }

    def test_readonly_and_write_agents_report_step_usage(self, tmp_path):
        tokens = {"input": 7, "output": 3, "cache": {"read": 2, "write": 1}}
        expected = {
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        }
        readonly_events = []
        readonly = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                usage_observer=readonly_events.append,
            )
        )
        write_events = []
        write = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                usage_observer=write_events.append,
            )
        )

        with (
            patch("core.llm_client.subprocess.run", return_value=self._run_result(token_steps=[tokens])),
            patch("core.llm_client._opencode_agent_env") as agent_env,
        ):
            agent_env.return_value.__enter__.return_value = {}
            assert readonly.run_readonly_agent("review", tmp_path) == "ok"

        with (
            patch("core.llm_client._run_opencode_streaming", return_value=self._run_result(token_steps=[tokens])),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._opencode_agent_env") as agent_env,
        ):
            agent_env.return_value.__enter__.return_value = {}
            assert write.run_agent("implement", tmp_path) == ([], "ok")

        assert readonly_events[0]["reported_tokens"] == expected
        assert write_events[0]["reported_tokens"] == expected

    def test_failed_generate_reports_completed_step_usage(self):
        events = []
        client = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                usage_observer=events.append,
            )
        )
        result = self._run_result(token_steps=[{"input": 7, "output": 2}])
        result.returncode = 1
        result.stderr = json.dumps(
            {
                "responseHeaders": {"x-codex-credits-has-credits": "False"},
                "responseBody": {"error": {"type": "usage_limit_reached"}},
            }
        )

        with (
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(LLMQuotaExceeded),
        ):
            client.generate("system", "user")

        assert events[0]["outcome"] == "fatal_error"
        assert events[0]["reported_tokens"] == {"input_tokens": 7, "output_tokens": 2}

    def test_generate_uses_sanitized_session_title_when_set(self):
        client = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                session_title="sikula planner abc123 JSON Numeric Fixtures!",
            )
        )
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("--title") + 1] == "sikula-planner-abc123-JSON-Numeric-Fixtures"

    def test_generate_warns_for_successful_run_with_provider_log_diagnostic(self, caplog):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = self._run_result()
        result.stderr = (
            'ERROR service=llm error={"error":{"message":"Unsupported value: '
            "'SECRET_PROMPT_PAYLOAD' is not supported with the 'gpt-5.3-codex-spark' model\","
            '"type":"invalid_request_error","param":"reasoning.effort","code":"unsupported_value"}} stream error'
        )

        with (
            caplog.at_level(logging.WARNING, logger="core.llm_client"),
            patch("core.llm_client.subprocess.run", return_value=result),
        ):
            assert client.generate("system", "user") == "ok"

        assert "opencode reported provider diagnostic" in caplog.text
        assert "configuration error" in caplog.text
        assert "SECRET_PROMPT_PAYLOAD" not in caplog.text
        assert "reasoning.effort" not in caplog.text
        assert "unsupported_value" not in caplog.text

    def test_run_readonly_agent_passes_prompt_via_stdin(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["opencode", "run"]
        assert "prompt" not in cmd
        assert "--agent" in cmd
        assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
        assert cmd[cmd.index("--title") + 1] == "sikula-readonly"
        assert "--print-logs" in cmd
        assert cmd[cmd.index("--log-level") + 1] == "ERROR"
        assert mock_run.call_args.kwargs["input"] == "prompt"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert "OPENCODE_CONFIG_DIR" in mock_run.call_args.kwargs["env"]

    def test_run_agent_passes_prompt_via_stdin(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        with (
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=self._run_result()) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["opencode", "run"]
        assert "prompt" not in cmd
        assert "--agent" in cmd
        assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
        assert cmd[cmd.index("--title") + 1] == "sikula-implementer"
        assert mock_run.call_args.kwargs["prompt"] == "prompt"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert "OPENCODE_CONFIG_DIR" in mock_run.call_args.kwargs["env"]
        assert "--print-logs" in cmd
        assert cmd[cmd.index("--log-level") + 1] == "ERROR"
        assert changed == []
        assert output == "ok"

    def test_run_agent_no_text_without_changes_raises_with_structured_diagnostic(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr=(
                'ERROR service=llm error={"error":{"message":"Unsupported value",'
                '"type":"invalid_request_error","param":"reasoning.effort","code":"unsupported_value"}}'
            ),
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            client.run_agent("prompt", tmp_path)

        message = str(exc_info.value)
        assert "returned no text output" in message
        assert "stderr: opencode provider diagnostic: configuration error" in message
        assert "unsupported_value" not in message
        assert "reasoning.effort" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_no_text_without_changes_summarizes_rejected_tool_call(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "grep",
                        "state": {
                            "status": "error",
                            "input": {"path": "/repo/.sikula/worktrees/task", "pattern": "FixtureInputMap"},
                            "error": "The user rejected permission to use this specific tool call.",
                        },
                    },
                }
            ),
            stderr=(
                "\x1b[93m\x1b[1m! \x1b[0mpermission requested: "
                "external_directory (/repo/.sikula/worktrees/*); auto-rejecting"
            ),
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=result),
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.run_agent("prompt", tmp_path)

        message = str(exc_info.value)
        assert "returned no text output" in message
        assert "safe diagnostic" in message
        assert "permission requested" not in message
        assert "external_directory" not in message
        assert "grep failed: permission rejected" in message
        assert "The user rejected permission to use this specific tool call." not in message
        assert "/repo/.sikula/worktrees/task" not in message
        assert "FixtureInputMap" not in message
        assert '"type": "tool_use"' not in message

    def test_run_agent_no_text_with_changes_returns_diagnostic_for_audit(self, tmp_path: Path):
        usage_events = []
        client = OpenCodeClient(
            LLMConfig(
                provider="opencode",
                model="openai/gpt-5.3-codex",
                usage_observer=usage_events.append,
            )
        )
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr="provider returned no content",
        )

        snapshots = [{"src/main.py": "before"}, {"src/main.py": "after"}]
        with (
            patch("core.llm_client._git_snapshot", side_effect=snapshots),
            patch("core.llm_client._run_opencode_streaming", return_value=result),
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == ["src/main.py"]
        assert "returned no text output" in output
        assert "safe diagnostic" in output
        assert "provider returned no content" not in output
        assert usage_events[0]["output_chars"] == 0

    def test_run_agent_no_text_does_not_persist_raw_stdout_events(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "input": {"path": "src/secret.py"},
                            "output": "SOURCE_CONTENT_SHOULD_NOT_BE_PERSISTED",
                        },
                    },
                }
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=result),
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.run_agent("prompt", tmp_path)

        message = str(exc_info.value)
        assert "returned no text output" in message
        assert "SOURCE_CONTENT_SHOULD_NOT_BE_PERSISTED" not in message
        assert "src/secret.py" not in message
        assert '"type": "tool_use"' not in message

    def test_run_agent_no_text_redacts_arbitrary_tool_error(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "error",
                            "input": {"path": "/repo/private/source.py"},
                            "error": "stderr contained SECRET_PROMPT_PAYLOAD and source excerpt",
                        },
                    },
                }
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=result),
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.run_agent("prompt", tmp_path)

        message = str(exc_info.value)
        assert "bash failed" in message
        assert "SECRET_PROMPT_PAYLOAD" not in message
        assert "/repo/private/source.py" not in message
        assert "source excerpt" not in message

    def test_generate_no_text_redacts_unstructured_stderr_diagnostic(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr="\x1b[93mwarning\x1b[0m provider returned no content",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode CLI error: returned no text output" in message
        assert "stderr:" in message
        assert "safe diagnostic" in message
        assert "warning provider returned no content" not in message

    def test_generate_no_text_classifies_fatal_provider_diagnostic(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr=(
                'ERROR service=llm error={"error":{"message":"Unsupported value",'
                '"type":"invalid_request_error","param":"reasoning.effort","code":"unsupported_value"}}'
            ),
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode CLI error: returned no text output" in message
        assert "stderr: opencode provider diagnostic: configuration error" in message
        assert "unsupported_value" not in message
        assert "reasoning.effort" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_no_text_redacts_structured_stderr_provider_payload(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr=(
                'ERROR service=llm responseBody={"error":{"message":"Unsupported value: '
                'SECRET_PROMPT_PAYLOAD",'
                '"type":"invalid_request_error","param":"reasoning.effort","code":"unsupported_value"}}'
            ),
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode CLI error: returned no text output" in message
        assert "stderr: opencode provider diagnostic: configuration error" in message
        assert "SECRET_PROMPT_PAYLOAD" not in message
        assert "responseBody" not in message
        assert "reasoning.effort" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_no_text_classifies_structured_credit_headers_as_fatal(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr=(
                'ERROR service=llm responseHeaders={"x-codex-credits-balance":"0",'
                '"x-codex-credits-has-credits":"False"}'
            ),
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode CLI error: returned no text output" in message
        assert "stderr: opencode provider diagnostic: quota exceeded" in message
        assert "responseHeaders" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_no_text_scans_later_structured_stderr_for_fatal_diagnostic(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line({"type": "message", "sessionID": "ses_123"}),
            stderr=(
                'ERROR service=llm responseHeaders={"x-request-id":"req_123"}\n'
                'ERROR service=llm responseHeaders={"x-codex-credits-balance":"0",'
                '"x-codex-credits-has-credits":"False"}'
            ),
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "quota exceeded" in message
        assert "responseHeaders" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_readonly_no_text_summarizes_rejected_tool_call(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=self._line(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "grep",
                        "state": {
                            "status": "error",
                            "input": {"path": "/repo/.sikula/worktrees/task"},
                            "error": "The user rejected permission to use this specific tool call.",
                        },
                    },
                }
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.run_readonly_agent("prompt", tmp_path)

        message = str(exc_info.value)
        assert "opencode agent error: returned no text output" in message
        assert "grep failed: permission rejected" in message
        assert "The user rejected permission to use this specific tool call." not in message
        assert "/repo/.sikula/worktrees/task" not in message
        assert '"type": "tool_use"' not in message

    def test_readonly_failure_reports_stdout_json_error(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=1,
            stdout=self._line({"type": "error", "error": {"name": "UnknownError", "data": {"message": "bad prompt"}}}),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(RuntimeError, match="bad prompt"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

    def test_readonly_usage_limit_reached_is_fatal_and_not_retried(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=1,
            stdout=self._line(
                {
                    "type": "error",
                    "error": {
                        "name": "ProviderError",
                        "data": {
                            "type": "usage_limit_reached",
                            "message": "The usage limit has been reached",
                            "headers": {
                                "x-codex-credits-balance": "0",
                                "x-codex-credits-has-credits": "False",
                            },
                        },
                    },
                }
            ),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="usage limit"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_readonly_prefers_stdout_json_error_over_stderr_noise(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=1,
            stdout=self._line(
                {
                    "type": "error",
                    "error": {
                        "name": "ProviderError",
                        "data": {"type": "usage_limit_reached", "message": "The usage limit has been reached"},
                    },
                }
            ),
            stderr="background log warning",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="usage limit"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_auth_failure_is_fatal_and_not_retried(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=1,
            stdout=self._line(
                {"type": "error", "error": {"name": "AuthError", "data": {"message": "not authenticated"}}}
            ),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_prefers_stdout_json_error_over_stderr_noise(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(
            returncode=1,
            stdout=self._line(
                {"type": "error", "error": {"name": "AuthError", "data": {"message": "not authenticated"}}}
            ),
            stderr="opencode warning before shutdown",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_invalid_model_is_fatal_and_not_retried(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/nope"))
        result = MagicMock(
            returncode=1,
            stdout="",
            stderr=(
                'ERROR service=llm responseBody={"error":{"message":"Invalid model SECRET_PROMPT_PAYLOAD",'
                '"type":"invalid_request_error","code":"unsupported_value"}}'
            ),
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode provider diagnostic: configuration error" in message
        assert "SECRET_PROMPT_PAYLOAD" not in message
        assert "responseBody" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_nonzero_unstructured_stderr_is_redacted_before_retry(self):
        observer = MagicMock()
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex", retry_observer=observer))
        result = MagicMock(returncode=1, stdout="", stderr="temporary upstream error SECRET_PROMPT_PAYLOAD")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "opencode CLI error:" in message
        assert "safe diagnostic" in message
        assert "SECRET_PROMPT_PAYLOAD" not in message
        assert mock_run.call_count == 4
        assert observer.call_count == 3
        assert all("SECRET_PROMPT_PAYLOAD" not in call.args[0]["error"] for call in observer.call_args_list)

    @pytest.mark.parametrize(
        ("stderr", "expected_error", "expected_label"),
        [
            ("invalid model: SECRET_MODEL_NAME", LLMConfigurationError, "configuration error"),
            ("not authenticated with SECRET_TOKEN", LLMAuthError, "authentication failed"),
            ("usage limit has been reached for SECRET_ACCOUNT", LLMQuotaExceeded, "quota exceeded"),
        ],
    )
    def test_generate_nonzero_plain_stderr_fatal_markers_are_redacted_and_not_retried(
        self,
        stderr: str,
        expected_error: type[LLMProviderError],
        expected_label: str,
    ):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(returncode=1, stdout="", stderr=stderr)
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(expected_error) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert f"opencode provider diagnostic: {expected_label}" in message
        assert "SECRET" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_empty_output_is_retried(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        empty = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", side_effect=[empty, self._run_result("ok")]) as mock_run,
        ):
            assert client.generate("system", "user") == "ok"

        assert mock_run.call_count == 2

    def test_readonly_empty_output_is_retried(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        empty = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", side_effect=[empty, self._run_result("ok")]) as mock_run,
        ):
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        assert mock_run.call_count == 2

    def test_run_agent_transient_error_is_still_retried(self, tmp_path: Path):
        observer = MagicMock()
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex", retry_observer=observer))
        failure = MagicMock(returncode=1, stdout="", stderr="temporary upstream error")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", side_effect=[failure, self._run_result()]) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        assert mock_run.call_count == 2
        observer.assert_called_once()
        assert observer.call_args.args[0]["error_type"] == "LLMTransientError"

    def test_run_agent_prefers_stdout_json_error_over_stderr_noise(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=self._line(
                {
                    "type": "error",
                    "error": {"name": "ModelError", "data": {"message": "unsupported model openai/nope"}},
                }
            ),
            stderr="opencode warning before shutdown",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_opencode_streaming", return_value=failure) as mock_run,
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_timeout_is_retried(self, tmp_path: Path):
        observer = MagicMock()
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex", retry_observer=observer))
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch(
                "core.llm_client._run_opencode_streaming",
                side_effect=[subprocess.TimeoutExpired("opencode", 30), self._run_result()],
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        assert mock_run.call_count == 2
        assert observer.call_args.args[0]["error_type"] == "LLMTimeoutError"

    def test_streaming_agent_stops_on_usage_limit_before_process_exit(self, tmp_path: Path):
        class HangingQuotaProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(
                    self._line(
                        {
                            "type": "error",
                            "error": {
                                "name": "ProviderError",
                                "data": {
                                    "type": "usage_limit_reached",
                                    "message": "The usage limit has been reached",
                                    "headers": {
                                        "x-codex-credits-balance": "0",
                                        "x-codex-credits-has-credits": "False",
                                    },
                                },
                            },
                        }
                    )
                )
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            @staticmethod
            def _line(event: dict) -> str:
                return json.dumps(event)

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = HangingQuotaProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            _run_opencode_streaming(
                ["opencode", "run", "--format", "json"],
                prompt="prompt",
                cwd=tmp_path,
                env={},
                timeout=30,
            )

        assert processes[0].terminated is True

    def test_opencode_streaming_agent_stops_on_usage_limit_in_printed_log_stream(self, tmp_path: Path):
        class HangingPrintedLogQuotaProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO()
                self.stderr = StringIO(
                    'ERROR service=llm responseHeaders={"x-codex-credits-balance":"0",'
                    '"x-codex-credits-has-credits":"False"} '
                    'responseBody={"error":{"type":"usage_limit_reached",'
                    '"message":"The usage limit has been reached"}}'
                )
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = HangingPrintedLogQuotaProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            _run_opencode_streaming(
                ["opencode", "run", "--format", "json"],
                prompt="prompt",
                cwd=tmp_path,
                env={},
                timeout=30,
            )

        assert processes[0].terminated is True

    def test_opencode_streaming_agent_stops_on_usage_limit_headers_only_printed_log(self, tmp_path: Path):
        class HangingPrintedLogHeaderQuotaProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO()
                self.stderr = StringIO(
                    'ERROR service=llm responseHeaders={"x-codex-credits-balance":"0",'
                    '"x-codex-credits-has-credits":"False"}'
                )
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = HangingPrintedLogHeaderQuotaProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            _run_opencode_streaming(
                ["opencode", "run", "--format", "json"],
                prompt="prompt",
                cwd=tmp_path,
                env={},
                timeout=30,
            )

        assert processes[0].terminated is True

    def test_streaming_agent_stops_on_structured_codex_error_event(
        self,
        tmp_path: Path,
    ):
        class HangingFatalProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(json.dumps({"type": "error", "message": "quota exceeded"}))
                self.stderr = StringIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = HangingFatalProcess(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            _run_agent_subprocess_streaming(
                ["codex", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="codex",
                stdout_error_parser=_codex_stream_error,
            )

        assert processes[0].terminated is True

    @pytest.mark.parametrize(
        ("stream_name", "stream_text", "stdout_parser", "stderr_parser", "expected_error", "match"),
        [
            (
                "stdout",
                json.dumps({"type": "error", "message": "quota exceeded"}),
                _codex_stream_error,
                None,
                LLMQuotaExceeded,
                "quota exceeded",
            ),
            (
                "stderr",
                'ERROR service=llm responseBody={"error":{"message":"not authenticated"}}',
                None,
                _opencode_log_error,
                LLMAuthError,
                "authentication failed",
            ),
        ],
    )
    def test_streaming_agent_parses_drained_output_after_fast_exit(
        self,
        tmp_path: Path,
        stream_name: str,
        stream_text: str,
        stdout_parser,
        stderr_parser,
        expected_error,
        match: str,
    ):
        class FastExitFatalProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(stream_text if stream_name == "stdout" else "")
                self.stderr = StringIO(stream_text if stream_name == "stderr" else "")
                self.returncode = 1

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        def fake_popen(*args, **kwargs):
            return FastExitFatalProcess(*args, **kwargs)

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(expected_error, match=match),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
                stdout_error_parser=stdout_parser,
                stderr_error_parser=stderr_parser,
            )

    def test_streaming_agent_drains_reader_threads_after_fast_exit(self, tmp_path: Path):
        class DelayedReadable:
            def __init__(self, text: str) -> None:
                self._text = text
                self._pos = 0
                self.closed = False

            def read(self, size: int = -1) -> str:
                if self._pos == 0:
                    time.sleep(1.2)
                if self._pos >= len(self._text):
                    return ""
                if size is None or size < 0:
                    end = len(self._text)
                else:
                    end = min(len(self._text), self._pos + size)
                chunk = self._text[self._pos : end]
                self._pos = end
                return chunk

            def close(self) -> None:
                self.closed = True

        class FastExitVerboseProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = DelayedReadable("final assistant message")
                self.stderr = DelayedReadable("structured shutdown log")
                self.returncode = 0

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        def fake_popen(*args, **kwargs):
            return FastExitVerboseProcess(*args, **kwargs)

        with patch("core.llm_client.subprocess.Popen", side_effect=fake_popen):
            result = _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

        assert result.returncode == 0
        assert result.stdout == "final assistant message"
        assert result.stderr == "structured shutdown log"

    def test_streaming_agent_enforces_timeout_while_draining_inherited_pipe(self, tmp_path: Path):
        class BlockingReadable:
            def __init__(self) -> None:
                self._release = threading.Event()
                self.closed = False

            def read(self, size: int = -1) -> str:
                self._release.wait(timeout=5)
                return ""

            def close(self) -> None:
                self.closed = True

            def release(self) -> None:
                self._release.set()

        class FastExitInheritedPipeProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = BlockingReadable()
                self.stderr = StringIO()
                self.returncode = 0

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15
                self.stdout.release()

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9
                self.stdout.release()

        processes = []

        def fake_popen(*args, **kwargs):
            process = FastExitInheritedPipeProcess(*args, **kwargs)
            processes.append(process)
            return process

        started_at = time.monotonic()
        with (
            patch("core.llm_client.subprocess.Popen", side_effect=fake_popen),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=0.05,
                provider="provider",
            )

        processes[0].stdout.release()
        assert time.monotonic() - started_at < 1

    @pytest.mark.parametrize(
        ("stream_name", "raw_output"),
        [
            ("stdout", "Error: not authenticated"),
            ("stderr", "Error: invalid model: gemini/nope"),
            ("stdout", "ERROR service=llm usage_limit_reached"),
            ("stderr", "FATAL provider error: unauthorized"),
        ],
    )
    def test_streaming_agent_ignores_raw_diagnostic_error_text(
        self,
        tmp_path: Path,
        stream_name: str,
        raw_output: str,
    ):
        class SuccessfulDiagnosticProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(raw_output if stream_name == "stdout" else "")
                self.stderr = StringIO(raw_output if stream_name == "stderr" else "")
                self.returncode = None
                self.terminated = False
                self._polls = 0

            def poll(self):
                self._polls += 1
                if self._polls > 2:
                    self.returncode = 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = SuccessfulDiagnosticProcess(*args, **kwargs)
            processes.append(process)
            return process

        with patch("core.llm_client.subprocess.Popen", side_effect=fake_popen):
            result = _run_agent_subprocess_streaming(
                ["provider", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="provider",
            )

        assert processes[0].terminated is False
        assert result.returncode == 0

    def test_opencode_printed_error_log_without_provider_payload_keeps_running(self, tmp_path: Path):
        raw_log = "ERROR service=default message=background warning without provider response payload"

        class SuccessfulPrintedLogProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(json.dumps({"type": "text", "part": {"text": "done"}}))
                self.stderr = StringIO(raw_log)
                self.returncode = None
                self.terminated = False
                self._polls = 0

            def poll(self):
                self._polls += 1
                if self._polls > 2:
                    self.returncode = 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = SuccessfulPrintedLogProcess(*args, **kwargs)
            processes.append(process)
            return process

        with patch("core.llm_client.subprocess.Popen", side_effect=fake_popen):
            result = _run_opencode_streaming(
                ["opencode", "run", "--format", "json", "--print-logs", "--log-level", "ERROR"],
                prompt="prompt",
                cwd=tmp_path,
                env={},
                timeout=30,
            )

        assert processes[0].terminated is False
        assert result.returncode == 0
        assert raw_log in result.stderr

    @pytest.mark.parametrize(
        ("stream_name", "benign_output"),
        [
            ("stdout", "Document how API key rotation handles 401 responses without stopping the task."),
            ("stderr", "Document how API key rotation handles 401 responses without stopping the task."),
            ("stdout", "Document an unauthorized response from an upstream API."),
            ("stderr", "Document an unauthorized response from an upstream API."),
            ("stdout", "Document invalid model and unsupported model provider cases."),
            ("stderr", "Document invalid model and unsupported model provider cases."),
            ("stdout", "Error handling docs for unauthorized and invalid model responses."),
            ("stderr", "Error handling docs for unauthorized and invalid model responses."),
        ],
    )
    def test_streaming_agent_ignores_benign_provider_error_terms_without_diagnostics(
        self,
        tmp_path: Path,
        stream_name: str,
        benign_output: str,
    ):

        class SuccessfulAuthDocsProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(benign_output if stream_name == "stdout" else "")
                self.stderr = StringIO(benign_output if stream_name == "stderr" else "")
                self.returncode = None
                self.terminated = False
                self._polls = 0

            def poll(self):
                self._polls += 1
                if self._polls > 2:
                    self.returncode = 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []

        def fake_popen(*args, **kwargs):
            process = SuccessfulAuthDocsProcess(*args, **kwargs)
            processes.append(process)
            return process

        with patch("core.llm_client.subprocess.Popen", side_effect=fake_popen):
            result = _run_agent_subprocess_streaming(
                ["codex", "agent"],
                cwd=tmp_path,
                env=None,
                timeout=30,
                provider="codex",
            )

        assert processes[0].terminated is False
        assert result.returncode == 0
        if stream_name == "stdout":
            assert benign_output in result.stdout
        else:
            assert benign_output in result.stderr

    def test_readonly_failure_falls_back_to_non_zero_exit(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        result = MagicMock(returncode=1, stdout="", stderr="")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(RuntimeError, match="non-zero exit"),
        ):
            client.run_readonly_agent("prompt", tmp_path)


class TestResolveWindowsBatchCommand:
    def test_preserves_command_outside_windows(self):
        command = ["claude", "--version"]

        with patch("core.subprocess_utils.os.name", "posix"):
            assert resolve_windows_batch_command(command) == (command, None, None)

    def test_windows_shell_process_uses_comspec_and_job_backed_runner(self):
        result = MagicMock()

        with (
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.run_windows_batch_process", return_value=result) as run,
        ):
            actual = run_windows_shell_process(
                "mvn test && echo done",
                capture_output=True,
                text=True,
                timeout=30,
            )

        assert actual is result
        run.assert_called_once_with(
            "mvn test && echo done",
            executable=r"C:\Windows\System32\cmd.exe",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @pytest.mark.parametrize("resolved", [None, r"C:\Tools\claude.exe"])
    def test_preserves_native_or_unresolved_windows_command(self, resolved):
        command = ["claude", "--version"]

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
        ):
            assert resolve_windows_batch_command(command) == (command, None, None)

    @pytest.mark.parametrize("suffix", ["cmd", "CMD", "bat", "BAT"])
    def test_routes_windows_batch_wrapper_through_comspec(self, suffix):
        resolved = rf"C:\Program Files\Claude\claude.{suffix}"
        command = ["claude", "-p", "--model", "claude-sonnet-4-6"]

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
        ):
            args, executable, batch_env = resolve_windows_batch_command(command)

        assert executable == r"C:\Windows\System32\cmd.exe"
        assert args == (
            r"C:\Windows\System32\cmd.exe /e:on /v:off /d /c "
            r'"%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0% %_SIKULA_BATCH_ARG_1% '
            r'%_SIKULA_BATCH_ARG_2%"'
        )
        assert batch_env is not None
        assert batch_env["_SIKULA_BATCH_COMMAND"] == f'"{resolved}"'
        assert [batch_env[f"_SIKULA_BATCH_ARG_{index}"] for index in range(3)] == [
            "-p",
            "--model",
            "claude-sonnet-4-6",
        ]

    def test_escapes_percent_signs_in_windows_batch_wrapper_path(self):
        resolved = r"C:\src\%TEMP%\provider.cmd"

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
        ):
            args, executable, batch_env = resolve_windows_batch_command([resolved, "--version"])

        assert executable == r"C:\Windows\System32\cmd.exe"
        assert args == (
            r"C:\Windows\System32\cmd.exe /e:on /v:off /d /c "
            r'"%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0%"'
        )
        assert batch_env is not None
        assert batch_env["_SIKULA_BATCH_COMMAND"] == r'"C:\src\%TEMP%\provider.cmd"'

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", "plain"),
            ("two words", '"two words"'),
            ("value&whoami", '"value&whoami"'),
            ("value|more", '"value|more"'),
            ('say "hello"', '"say ""hello"""'),
            ("%PATH%", '"%PATH%"'),
            ("trailing\\", '"trailing\\\\"'),
        ],
    )
    def test_quotes_windows_batch_arguments(self, value, expected):
        assert _windows_batch_argument(value) == expected

    @pytest.mark.parametrize("value", ["bad\0value", "bad\rvalue", "bad\nvalue"])
    def test_rejects_unrepresentable_windows_batch_arguments(self, value):
        with pytest.raises(ValueError, match="cannot contain"):
            _windows_batch_argument(value)

    def test_run_provider_cli_uses_resolved_batch_executable(self):
        resolved = r"C:\Users\developer\AppData\Roaming\npm\claude.CMD"
        process = MagicMock(returncode=0)
        process.communicate.return_value = ("", "")

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process) as popen,
            patch("core.subprocess_utils.start_windows_process_job", return_value=True) as start_job,
        ):
            result = _run_provider_cli(["claude", "--version"], text=True)

        assert result.returncode == 0
        start_job.assert_called_once_with(process)
        assert popen.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert popen.call_args.kwargs["creationflags"] == 0x00000204
        assert popen.call_args.args[0].endswith(r'/c "%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0%"')
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        process.communicate.assert_called_once_with(None, timeout=None)

    def test_batch_provider_timeout_terminates_process_group(self):
        resolved = r"C:\Users\developer\AppData\Roaming\npm\claude.CMD"
        process = MagicMock(returncode=-1)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("claude", 10),
            ("partial output", "partial error"),
        ]

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process),
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
            patch("core.subprocess_utils.terminate_windows_process_tree", return_value=True) as terminate,
            pytest.raises(subprocess.TimeoutExpired) as exc_info,
        ):
            _run_provider_cli(
                ["claude", "-p"],
                capture_output=True,
                input="prompt",
                text=True,
                timeout=10,
            )

        terminate.assert_called_once_with(process)
        assert exc_info.value.output == "partial output"
        assert exc_info.value.stderr == "partial error"

    def test_batch_provider_interruption_terminates_process_group(self):
        resolved = r"C:\Users\developer\AppData\Roaming\npm\claude.CMD"
        process = MagicMock(returncode=None)
        process.communicate.side_effect = KeyboardInterrupt

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process),
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
            patch("core.subprocess_utils.terminate_windows_process_tree", return_value=True) as terminate,
            pytest.raises(KeyboardInterrupt),
        ):
            _run_provider_cli(
                ["claude", "-p"],
                capture_output=True,
                input="prompt",
                text=True,
                timeout=10,
            )

        terminate.assert_called_once_with(process)

    def test_missing_provider_has_clear_error(self):
        with patch("core.llm_client.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(LLMConfigurationError, match="claude CLI not found"):
                _run_provider_cli(["claude", "--version"])

    def test_run_provider_cli_pins_utf8_for_text_mode(self):
        # The prompt is passed via input= in text mode; the pipe must use UTF-8 so
        # characters outside the process locale codec (e.g. cp1250) do not raise.
        result = MagicMock(returncode=0)
        with patch("core.llm_client.subprocess.run", return_value=result) as mock_run:
            _run_provider_cli(["claude", "-p"], text=True, input="arrow → dash —")
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"
        assert mock_run.call_args.kwargs["errors"] == "replace"

    def test_run_provider_cli_leaves_byte_mode_untouched(self):
        result = MagicMock(returncode=0)
        with patch("core.llm_client.subprocess.run", return_value=result) as mock_run:
            _run_provider_cli(["claude", "--version"])
        assert "encoding" not in mock_run.call_args.kwargs
        assert "errors" not in mock_run.call_args.kwargs

    def test_run_provider_cli_respects_explicit_encoding(self):
        result = MagicMock(returncode=0)
        with patch("core.llm_client.subprocess.run", return_value=result) as mock_run:
            _run_provider_cli(["claude", "-p"], text=True, encoding="latin-1")
        assert mock_run.call_args.kwargs["encoding"] == "latin-1"

    @pytest.mark.skipif(os.name != "nt", reason="requires the Windows command processor")
    @pytest.mark.parametrize("suffix", ["cmd", "bat"])
    def test_windows_batch_wrapper_round_trips_arguments(self, tmp_path, suffix):
        wrapper = tmp_path / f"provider.{suffix}"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" -c "import json,sys; print(json.dumps(sys.argv[1:]))" %*\r\n',
            encoding="utf-8",
        )
        arguments = [
            "two words",
            "value&whoami",
            "value|more",
            'say "hello"',
            "%PATH%",
            "trailing\\",
        ]

        result = _run_provider_cli(
            [str(wrapper), *arguments],
            capture_output=True,
            check=True,
            text=True,
        )

        assert json.loads(result.stdout) == arguments

    @pytest.mark.skipif(os.name != "nt", reason="requires the Windows command processor")
    @pytest.mark.parametrize("suffix", ["cmd", "bat"])
    def test_windows_batch_wrapper_round_trips_utf8_stdin(self, tmp_path, suffix):
        wrapper = tmp_path / f"provider.{suffix}"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" -c "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"\r\n',
            encoding="utf-8",
        )
        prompt = "Připrav změnu uživatelského profilu → hotovo — bez chyb"

        result = _run_provider_cli(
            [str(wrapper)],
            capture_output=True,
            check=True,
            input=prompt,
            text=True,
        )

        assert result.stdout == prompt

    @pytest.mark.skipif(os.name != "nt", reason="requires the Windows command processor")
    def test_windows_batch_wrapper_executes_from_percent_path(self, tmp_path):
        wrapper_dir = tmp_path / "%TEMP%"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "provider.cmd"
        wrapper.write_text("@echo off\r\necho percent-path-ok\r\n", encoding="utf-8")

        result = _run_provider_cli(
            [str(wrapper)],
            capture_output=True,
            check=True,
            text=True,
        )

        assert result.stdout.strip() == "percent-path-ok"


class TestClaudeWriteSettings:
    @staticmethod
    def _result(
        text: str = "",
        *,
        is_error: bool = False,
        subtype: str = "success",
        errors: list[str] | None = None,
        api_error_status: int | None = None,
        usage: dict[str, object] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": text,
        }
        if errors is not None:
            payload["errors"] = errors
        if api_error_status is not None:
            payload["api_error_status"] = api_error_status
        if usage is not None:
            payload["usage"] = usage
        return json.dumps(payload)

    def test_git_exclude_file_returns_none_outside_git_repo(self, tmp_path):
        assert _git_exclude_file(tmp_path) is None

    def test_writes_settings_json(self, tmp_path):
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True)
        exclude.write_text("")
        with patch("core.llm_client._git_exclude_file", return_value=exclude):
            settings_path = _claude_write_settings(tmp_path)
        assert settings_path == tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()

    def test_sandbox_denies_home_and_root(self, tmp_path):
        from pathlib import Path

        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True)
        exclude.write_text("")
        with patch("core.llm_client._git_exclude_file", return_value=exclude):
            settings_path = _claude_write_settings(tmp_path)
        deny = json.loads(settings_path.read_text())["sandbox"]["filesystem"]["denyWrite"]
        assert str(Path.home()) + "/" in deny
        assert "//" in deny

    def test_sandbox_allows_project_dir(self, tmp_path):
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True)
        exclude.write_text("")
        with patch("core.llm_client._git_exclude_file", return_value=exclude):
            settings_path = _claude_write_settings(tmp_path)
        allow = json.loads(settings_path.read_text())["sandbox"]["filesystem"]["allowWrite"]
        assert str(tmp_path) in allow

    def test_adds_claude_dir_to_git_exclude(self, tmp_path):
        (tmp_path / ".git" / "info").mkdir(parents=True)
        (tmp_path / ".git" / "info" / "exclude").write_text("")
        with patch("core.llm_client._git_exclude_file", return_value=tmp_path / ".git" / "info" / "exclude"):
            _claude_write_settings(tmp_path)
        exclude_content = (tmp_path / ".git" / "info" / "exclude").read_text()
        assert ".claude/" in exclude_content

    def test_does_not_duplicate_git_exclude_entry(self, tmp_path):
        (tmp_path / ".git" / "info").mkdir(parents=True)
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.write_text(".claude/\n")
        with patch("core.llm_client._git_exclude_file", return_value=exclude):
            _claude_write_settings(tmp_path)
            _claude_write_settings(tmp_path)
        assert exclude.read_text().count(".claude/") == 1

    def test_does_not_create_git_exclude_outside_git_repo(self, tmp_path):
        with patch("core.llm_client._git_exclude_file", return_value=None):
            settings_path = _claude_write_settings(tmp_path)

        assert settings_path.exists()
        assert not (tmp_path / ".git").exists()

    def test_run_readonly_agent_calls_write_settings(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = self._result("analysis done")
            m.stderr = ""
            return m

        with (
            patch("core.llm_client.subprocess.run", side_effect=fake_run) as mock_run,
            patch("core.llm_client._claude_write_settings") as mock_setup,
        ):
            client.run_readonly_agent("review this", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["claude", "-p"]
        assert cmd[2:4] == ["--output-format", "json"]
        assert "review this" not in cmd
        assert mock_run.call_args.kwargs["input"] == "review this"
        mock_setup.assert_called_once_with(tmp_path)

    def test_run_readonly_agent_wraps_windows_cmd_installation(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        process = MagicMock(returncode=0)
        process.communicate.return_value = (self._result("analysis done"), "")
        resolved = r"C:\Users\developer\AppData\Roaming\npm\claude.CMD"

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process) as popen,
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
            patch("core.llm_client._claude_write_settings"),
        ):
            assert client.run_readonly_agent("review this", tmp_path) == "analysis done"

        command_line = popen.call_args.args[0]
        assert command_line.startswith(r"C:\Windows\System32\cmd.exe /e:on /v:off /d /c ")
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        assert "acceptEdits" in popen.call_args.kwargs["env"].values()
        assert popen.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert popen.call_args.kwargs["creationflags"] == 0x00000204
        process.communicate.assert_called_once_with("review this", timeout=1800)

    def test_generate_wraps_windows_cmd_installation(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        process = MagicMock(returncode=0)
        process.communicate.return_value = (self._result("refined task"), "")
        resolved = r"C:\Users\developer\AppData\Roaming\npm\claude.CMD"

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process) as popen,
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
        ):
            assert client.generate("system", "task") == "refined task"

        assert popen.call_args.args[0].startswith(r"C:\Windows\System32\cmd.exe /e:on /v:off /d /c ")
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        assert popen.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert popen.call_args.kwargs["creationflags"] == 0x00000204
        process.communicate.assert_called_once_with("system\n\ntask", timeout=1800)

    def test_generate_uses_agent_timeout(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6", agent_timeout=123)
        client = ClaudeClient(cfg)
        result = MagicMock(returncode=0, stdout=self._result("ok"), stderr="")

        with patch("core.llm_client.subprocess.run", return_value=result) as mock_run:
            assert client.generate("system", "user") == "ok"

        assert mock_run.call_args.kwargs["timeout"] == 123

    def test_generate_reports_structured_usage_without_changing_result(self):
        events = []
        cfg = LLMConfig(
            provider="claude",
            model="claude-sonnet-4-6",
            usage_observer=events.append,
        )
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=0,
            stdout=self._result(
                "ok",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 3,
                    "total_cost_usd": 0.01,
                },
            ),
            stderr="",
        )

        with patch("core.llm_client.subprocess.run", return_value=result):
            assert client.generate("system", "user") == "ok"

        assert events[0]["output_chars"] == 2
        assert events[0]["reported_tokens"] == {
            "input_tokens": 10,
            "output_tokens": 2,
            "cached_input_tokens": 4,
            "cache_creation_input_tokens": 3,
        }

    def test_readonly_and_write_agents_report_structured_usage(self, tmp_path):
        usage = {"input_tokens": 7, "output_tokens": 3}
        readonly_events = []
        readonly = ClaudeClient(
            LLMConfig(provider="claude", model="claude-sonnet-4-6", usage_observer=readonly_events.append)
        )
        write_events = []
        write = ClaudeClient(
            LLMConfig(provider="claude", model="claude-sonnet-4-6", usage_observer=write_events.append)
        )

        with (
            patch(
                "core.llm_client.subprocess.run",
                return_value=MagicMock(returncode=0, stdout=self._result("review", usage=usage), stderr=""),
            ),
            patch("core.llm_client._claude_write_settings"),
        ):
            assert readonly.run_readonly_agent("review this", tmp_path) == "review"

        with (
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=subprocess.CompletedProcess([], 0, self._result("done", usage=usage), ""),
            ),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings"),
        ):
            assert write.run_agent("implement this", tmp_path) == ([], "done")

        assert readonly_events[0]["reported_tokens"] == usage
        assert write_events[0]["reported_tokens"] == usage

    def test_generate_failure_is_classified(self):
        usage_events = []
        cfg = LLMConfig(
            provider="claude",
            model="claude-sonnet-4-6",
            retry_observer=MagicMock(),
            usage_observer=usage_events.append,
        )
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "partial",
                is_error=True,
                subtype="error_during_execution",
                errors=["authentication failed while connecting"],
                api_error_status=401,
                usage={"input_tokens": 10, "output_tokens": 2},
            ),
            stderr="managed policy warning",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="authentication failed") as exc_info,
        ):
            client.generate("system", "user")

        assert "not authenticated" not in str(exc_info.value)
        assert mock_run.call_count == 1
        sleep.assert_not_called()
        cfg.retry_observer.assert_not_called()
        assert usage_events[0]["outcome"] == "fatal_error"
        assert usage_events[0]["output_chars"] == len("partial")
        assert usage_events[0]["reported_tokens"] == {"input_tokens": 10, "output_tokens": 2}

    def test_run_readonly_agent_failure_is_classified(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=["invalid model claude-nope"],
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            patch("core.llm_client._claude_write_settings"),
            pytest.raises(LLMConfigurationError, match="configuration invalid") as exc_info,
        ):
            client.run_readonly_agent("review this", tmp_path)

        assert "invalid model" not in str(exc_info.value)
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_calls_write_settings(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)

        with (
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=subprocess.CompletedProcess([], 0, self._result("done"), ""),
            ) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings") as mock_setup,
        ):
            client.run_agent("implement this", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["claude", "-p"]
        assert cmd[2:4] == ["--output-format", "json"]
        assert "implement this" not in cmd
        assert mock_run.call_args.kwargs["stdin_text"] == "implement this"
        mock_setup.assert_called_once_with(tmp_path)

    def test_run_agent_nonzero_exit_is_classified(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = subprocess.CompletedProcess(
            [],
            1,
            self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=["unsupported model claude-nope"],
            ),
            "",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings"),
            pytest.raises(LLMConfigurationError, match="configuration invalid") as exc_info,
        ):
            client.run_agent("implement this", tmp_path)

        assert "unsupported model" not in str(exc_info.value)
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_preserves_only_structured_failure_signals(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6", retry_observer=MagicMock())
        client = ClaudeClient(cfg)
        result = subprocess.CompletedProcess(
            [],
            1,
            self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=["connection reset for alice@example.com with token=secret"],
                api_error_status=500,
            ),
            "Permission allow rule from managed policy settings was ignored",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings"),
            pytest.raises(LLMTransientError, match="connection reset") as exc_info,
        ):
            client.run_agent("implement this", tmp_path)

        message = str(exc_info.value)
        assert "connection reset" in message
        assert "alice@example.com" not in message
        assert "token=secret" not in message
        assert "managed policy" not in message
        assert mock_run.call_count == 4
        assert cfg.retry_observer.call_count == 3

    def test_generate_accepts_verbose_json_result_array(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        output = json.dumps(
            [
                {"type": "assistant", "message": {"content": "partial"}},
                json.loads(self._result("final answer")),
            ]
        )

        with patch("core.llm_client.subprocess.run", return_value=MagicMock(returncode=0, stdout=output, stderr="")):
            assert client.generate("system", "user") == "final answer"

    def test_generate_classifies_api_status_from_provider_error_record(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=['API Error: 403 {"error":{"message":"organization policy"}}'],
            ),
            stderr="managed policy warning",
        )

        with (
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match=r"authentication failed \(HTTP 403"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1

    def test_generate_does_not_classify_partial_result_text(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "Document invalid model handling for the API.",
                is_error=True,
                subtype="error_during_execution",
                errors=["connection reset"],
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match="connection reset"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 4

    def test_generate_does_not_emit_unknown_result_subtype(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        secret_subtype = "sk-abcdefghijklmnopqrstuvwxyz"
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                is_error=True,
                subtype=secret_subtype,
                errors=["connection reset"],
                api_error_status=500,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match="connection reset") as exc_info,
        ):
            client.generate("system", "user")

        assert secret_subtype not in str(exc_info.value)
        assert mock_run.call_count == 4

    def test_generate_does_not_classify_partial_result_text_for_server_error(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "Document invalid model handling for the API.",
                is_error=True,
                subtype="error_during_execution",
                errors=["connection reset"],
                api_error_status=500,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match=r"connection reset.*HTTP 500"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 4

    def test_generate_classifies_allowlisted_result_only_login_failure(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "Not logged in \u00b7 Please run /login",
                is_error=True,
                subtype="error_during_execution",
                errors=[],
            ),
            stderr="managed policy warning",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="authentication failed"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize("status", [408, 409, 429])
    def test_generate_retries_transient_http_client_error(self, status):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "Document invalid model handling for the API.",
                is_error=True,
                subtype="error_during_execution",
                errors=[f"API Error: {status}"],
                api_error_status=status,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match=rf"HTTP {status}"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 4

    def test_generate_does_not_retry_quota_exhausted_rate_limit(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=["API Error: 429 quota exceeded"],
                api_error_status=429,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match=r"quota exhausted.*HTTP 429"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_generate_does_not_retry_terminal_http_client_error(self, status):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "The request was rejected.",
                is_error=True,
                subtype="error_during_execution",
                errors=[],
                api_error_status=status,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match=rf"configuration invalid.*HTTP {status}"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_does_not_retry_model_not_found(self):
        cfg = LLMConfig(provider="claude", model="claude-does-not-exist")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "The selected model may not exist or you may not have access to it.",
                is_error=True,
                subtype="error_during_execution",
                errors=[],
                api_error_status=404,
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match=r"configuration invalid.*HTTP 404"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize(
        "stderr",
        [
            "error: unknown option '--output-format'\nUsage: claude [options]",
            "unrecognized argument '--permission-mode'",
            "error: Found argument '--settings' which wasn't expected",
        ],
    )
    def test_generate_does_not_retry_rejected_required_cli_option(self, stderr):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=2,
            stdout="",
            stderr=stderr,
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="rejected a required option") as exc_info,
        ):
            client.generate("system", "user")

        assert "--output-format" not in str(exc_info.value)
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_does_not_trust_unstructured_pre_envelope_stderr(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout="",
            stderr="Managed policy warning: invalid option in a permission rule for alice@example.com",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match="invalid JSON result envelope") as exc_info,
        ):
            client.generate("system", "user")

        assert "alice@example.com" not in str(exc_info.value)
        assert mock_run.call_count == 4

    def test_generate_does_not_derive_status_from_partial_result_text(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                "API Error: 403 is documented in the requested example.",
                is_error=True,
                subtype="error_during_execution",
                errors=["connection reset"],
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match="connection reset"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 4

    @pytest.mark.parametrize(
        ("message", "expected_error", "expected_message"),
        [
            ("Credit balance is too low", LLMQuotaExceeded, "quota exhausted"),
            ("Invalid API key", LLMAuthError, "authentication failed"),
            ("Invalid model claude-nope", LLMConfigurationError, "configuration invalid"),
        ],
    )
    def test_generate_classifies_result_when_api_status_proves_provider_error(
        self,
        message,
        expected_error,
        expected_message,
    ):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                message,
                is_error=True,
                subtype="success",
                errors=[],
                api_error_status=400,
            ),
            stderr="managed policy warning",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(expected_error, match=expected_message),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_does_not_retry_terminal_limit_subtype(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=1,
            stdout=self._result(
                is_error=True,
                subtype="error_max_turns",
                errors=["Reached maximum number of turns"],
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="max turns"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_treats_zero_exit_error_envelope_as_failure(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(
            returncode=0,
            stdout=self._result(
                is_error=True,
                subtype="error_during_execution",
                errors=["quota exceeded"],
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exhausted"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1

    def test_generate_retries_unrecognized_json_without_classifying_assistant_text(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(returncode=1, stdout='{"error":"invalid model in task prose"}', stderr="")

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMTransientError, match="invalid JSON result envelope"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 4

    @pytest.mark.parametrize("output", ["{", "42"])
    def test_result_envelope_rejects_malformed_or_non_object_json(self, output):
        assert _claude_result_envelope(output) is None

    @pytest.mark.parametrize("error", [ValueError("oversized integer"), RecursionError("nested JSON")])
    def test_result_envelope_degrades_on_json_decoder_failures(self, error):
        with patch("core.llm_client.json.loads", side_effect=error):
            assert _claude_result_envelope("{}") is None

    def test_run_agent_timeout_is_retried(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6", retry_observer=MagicMock())
        client = ClaudeClient(cfg)

        with (
            patch("core.llm_client.time.sleep"),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[
                    subprocess.TimeoutExpired("claude", 30),
                    subprocess.CompletedProcess([], 0, self._result("done"), ""),
                ],
            ) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings"),
        ):
            changed, output = client.run_agent("implement this", tmp_path)

        assert changed == []
        assert output == "done"
        assert mock_run.call_count == 2
        assert cfg.retry_observer.call_args.args[0]["error_type"] == "LLMTimeoutError"


class TestCodexParseText:
    def _item_line(self, text: str) -> str:
        return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})

    def test_extracts_text_from_item_completed(self):
        lines = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc"}),
                json.dumps({"type": "turn.started"}),
                self._item_line("Hello."),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
            ]
        )
        assert _codex_parse_text(lines) == "Hello."

    def test_extracts_only_explicit_final_usage(self):
        lines = "\n".join(
            [
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "token_count", "info": {"total_tokens": 999}},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 20,
                            "output_tokens": 8,
                            "cached_input_tokens": 6,
                            "unsafe": "PRIVATE_PROVIDER_OUTPUT",
                        },
                    }
                ),
            ]
        )

        assert _codex_reported_tokens(lines) == {
            "input_tokens": 20,
            "output_tokens": 8,
            "cached_input_tokens": 6,
        }

    def test_concatenates_multiple_agent_messages(self):
        lines = "\n".join([self._item_line("Part one."), self._item_line("Part two.")])
        assert _codex_parse_text(lines) == "Part one.\nPart two."

    def test_skips_non_agent_message_items(self):
        lines = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "tool_call", "text": "ignored"}}),
                self._item_line("answer"),
            ]
        )
        assert _codex_parse_text(lines) == "answer"

    def test_raises_on_turn_failed_with_error_field(self):
        event = json.dumps({"type": "turn.failed", "error": {"message": "rate limit"}})
        with pytest.raises(RuntimeError, match="codex turn failed: rate limit"):
            _codex_parse_text(event)

    def test_raises_on_turn_failed_with_data_field(self):
        event = json.dumps({"type": "turn.failed", "data": "something failed"})
        with pytest.raises(RuntimeError, match="codex turn failed"):
            _codex_parse_text(event)

    def test_raises_on_error_event_with_nested_json(self):
        inner = json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {"type": "invalid_request_error", "message": "model not supported"},
            }
        )
        event = json.dumps({"type": "error", "message": inner})
        with pytest.raises(RuntimeError, match="model not supported"):
            _codex_parse_text(event)

    def test_raises_on_error_event_with_plain_message(self):
        event = json.dumps({"type": "error", "message": "quota exceeded"})
        with pytest.raises(RuntimeError, match="codex error"):
            _codex_parse_text(event)

    def test_raises_when_no_agent_messages(self):
        lines = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        with pytest.raises(RuntimeError, match="no text output"):
            _codex_parse_text(lines)

    def test_raises_on_empty_whitespace_items(self):
        event = self._item_line("   ")
        with pytest.raises(RuntimeError, match="no text output"):
            _codex_parse_text(event)

    def test_skips_invalid_json_lines(self):
        lines = "not json\n" + self._item_line("ok")
        assert _codex_parse_text(lines) == "ok"

    def test_skips_non_object_json_lines(self):
        lines = json.dumps(["not", "object"]) + "\n" + self._item_line("ok")
        assert _codex_parse_text(lines) == "ok"

    def test_extracts_task_complete_last_agent_message(self):
        line = json.dumps({"type": "task_complete", "payload": {"last_agent_message": "Done from task."}})
        assert _codex_parse_text(line) == "Done from task."

    @pytest.mark.parametrize("line", ["", "not json", json.dumps(["not", "object"])])
    def test_stream_error_ignores_non_error_lines(self, line: str):
        assert _codex_stream_error(line) is None

    def test_extracts_current_response_item_final_answer(self):
        line = json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            }
        )
        assert _codex_parse_text(line) == "Done."

    def test_extracts_current_event_msg_final_answer(self):
        line = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Final text.",
                },
            }
        )
        assert _codex_parse_text(line) == "Final text."

    def test_ignores_current_commentary_and_tool_output_without_final_answer(self):
        lines = "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "Reading source files.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "201: StrategyMode::Atomic => ScannerEvaluationRecord {",
                        },
                    }
                ),
            ]
        )
        with pytest.raises(RuntimeError, match="no text output"):
            _codex_parse_text(lines)

    def test_subprocess_error_does_not_report_raw_current_jsonl_without_error(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "201: StrategyMode::Atomic => ScannerEvaluationRecord {",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "token_count", "info": {"total_tokens": 306669}},
                    }
                ),
            ]
        )
        result = MagicMock(returncode=1, stdout=stdout, stderr="")

        error = _codex_subprocess_error(result)

        assert error == "codex exited before producing a final answer or structured error"
        assert "ScannerEvaluationRecord" not in error

    def test_subprocess_error_prefers_stderr_over_non_error_json_progress(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {"type": "response_item", "payload": {"type": "function_call_output", "output": "progress"}}
                ),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_tokens": 123}}}),
            ]
        )
        result = MagicMock(returncode=1, stdout=stdout, stderr="401 unauthorized")

        assert _codex_subprocess_error(result) == "401 unauthorized"

    def test_subprocess_error_keeps_plain_stdout_error(self):
        result = MagicMock(returncode=1, stdout="Error: failed to initialize app-server", stderr="")
        assert _codex_subprocess_error(result) == "Error: failed to initialize app-server"


class TestCodexClientCommands:
    @staticmethod
    def _run_result(text: str = "ok"):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
            stderr="",
        )

    def test_generate_uses_read_only_sandbox(self):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex", agent_timeout=123))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:5] == ["codex", "exec", "--skip-git-repo-check", "--json", "--sandbox"]
        assert cmd[5] == "read-only"
        assert cmd[-1] == "-"
        assert "system\n\nuser" not in cmd
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"
        assert mock_run.call_args.kwargs["timeout"] == 123

    def test_generate_reports_structured_usage_without_changing_result(self):
        events = []
        client = CodexClient(
            LLMConfig(
                provider="codex",
                model="gpt-5.3-codex",
                usage_observer=events.append,
            )
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
            ]
        )

        with patch("core.llm_client.subprocess.run", return_value=MagicMock(returncode=0, stdout=stdout, stderr="")):
            assert client.generate("system", "user") == "ok"

        assert len(events) == 1
        assert events[0]["outcome"] == "success"
        assert events[0]["input_chars"] == len("system\n\nuser")
        assert events[0]["output_chars"] == 2
        assert events[0]["reported_tokens"] == {"input_tokens": 10, "output_tokens": 2}

    def test_generate_failure_reports_stdout_json_error(self):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "turn.failed", "error": {"message": "model unavailable"}}),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=failure),
            pytest.raises(RuntimeError, match="codex turn failed: model unavailable"),
        ):
            client.generate("system", "user")

    def test_generate_usage_limit_is_fatal_and_not_retried(self):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(returncode=1, stdout=json.dumps({"type": "error", "message": "quota exceeded"}), stderr="")
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=failure) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_prefers_stdout_json_error_over_stderr_warning(self):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "error", "message": "quota exceeded"}),
            stderr="warning: sandbox setup degraded",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=failure) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_prefers_stderr_over_non_error_json_progress(self):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        stdout = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_tokens": 123}}})
        failure = MagicMock(returncode=1, stdout=stdout, stderr="401 unauthorized")
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=failure) as mock_run,
            pytest.raises(LLMAuthError, match="401 unauthorized"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_readonly_agent_uses_read_only_sandbox(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:5] == ["codex", "exec", "--skip-git-repo-check", "--json", "--sandbox"]
        assert cmd[5] == "read-only"
        assert cmd[-1] == "-"
        assert "prompt" not in cmd
        assert mock_run.call_args.kwargs["input"] == "prompt"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path

    def test_run_readonly_agent_failure_reports_stdout_json_error(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "turn.failed", "error": {"message": "app-server timeout"}}),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=failure),
            pytest.raises(RuntimeError, match="codex turn failed: app-server timeout"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

    def test_readonly_prefers_stdout_json_error_over_stderr_warning(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "error", "message": "not authenticated"}),
            stderr="warning: sandbox setup degraded",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=failure) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_readonly_agent_plain_stdout_environment_text_remains_retryable(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout="Error: failed to initialize in-process app-server client: Read-only file system",
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=failure) as mock_run,
            pytest.raises(LLMTransientError, match="failed to initialize in-process app-server client"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 4

    def test_run_agent_uses_workspace_write_sandbox(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        with (
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=self._run_result()) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:5] == ["codex", "exec", "--skip-git-repo-check", "--json", "--sandbox"]
        assert cmd[5] == "workspace-write"
        assert cmd[-1] == "-"
        assert "prompt" not in cmd
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert mock_run.call_args.kwargs["stdin_text"] == "prompt"
        assert changed == []
        assert output == "ok"

    def test_run_agent_notifies_retry_observer(self, tmp_path: Path):
        observer = MagicMock()
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer))
        failure = MagicMock(returncode=1, stdout="", stderr="temporary policy error")
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", side_effect=[failure, self._run_result()]),
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        observer.assert_called_once()
        event = observer.call_args.args[0]
        assert event["provider"] == "codex"
        assert event["model"] == "gpt-5.3-codex"
        assert event["operation"] == "run_agent"
        assert event["attempt"] == 1
        assert event["max_attempts"] == 4
        assert event["delay_s"] == 30
        assert event["error"] == "codex agent error: temporary policy error"

    def test_run_agent_notifies_retry_observer_with_stdout_json_error(self, tmp_path: Path):
        observer = MagicMock()
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "turn.failed", "error": {"message": "policy denied"}}),
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", side_effect=[failure, self._run_result()]),
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        observer.assert_called_once()
        event = observer.call_args.args[0]
        assert event["operation"] == "run_agent"
        assert event["error"] == "codex turn failed: policy denied"

    def test_run_agent_prefers_stdout_json_error_over_stderr_warning(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout=json.dumps({"type": "error", "message": "quota exceeded"}),
            stderr="warning: sandbox setup degraded",
        )
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=failure) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_success_exit_stdout_json_error_is_not_ignored(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        result = MagicMock(
            returncode=0,
            stdout=json.dumps({"type": "error", "message": "quota exceeded"}),
            stderr="",
        )
        with (
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result),
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.run_agent("prompt", tmp_path)

    def test_run_agent_classifies_provider_error_after_broken_stdin_pipe(self, tmp_path: Path):
        class BrokenPipeStdin(StringIO):
            def write(self, s):
                raise BrokenPipeError()

        class ExitedBeforePromptProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = BrokenPipeStdin()
                self.stdout = StringIO()
                self.stderr = StringIO("not authenticated")
                self.returncode = 1

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))

        with (
            patch("core.llm_client.subprocess.Popen", side_effect=ExitedBeforePromptProcess),
            patch("core.llm_client._git_snapshot", return_value={}),
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.run_agent("prompt", tmp_path)

    def test_run_agent_timeout_is_retried(self, tmp_path: Path):
        observer = MagicMock()
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex", retry_observer=observer))
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[subprocess.TimeoutExpired("codex", 30), self._run_result()],
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        assert mock_run.call_count == 2
        assert observer.call_args.args[0]["error_type"] == "LLMTimeoutError"


class TestGeminiParseResponse:
    def test_returns_response_field(self):
        output = json.dumps({"response": "result text"})
        assert _gemini_parse_response(output) == "result text"

    def test_raises_on_error_field(self):
        output = json.dumps({"error": {"message": "quota exceeded"}})
        with pytest.raises(RuntimeError, match="quota exceeded"):
            _gemini_parse_response(output)

    def test_raises_on_string_error_field(self):
        output = json.dumps({"error": "not authenticated"})
        with pytest.raises(LLMAuthError, match="not authenticated"):
            _gemini_parse_response(output)

    def test_raises_on_empty_response(self):
        output = json.dumps({"response": "   "})
        with pytest.raises(RuntimeError, match="no text output"):
            _gemini_parse_response(output)

    def test_falls_back_to_raw_on_invalid_json(self):
        result = _gemini_parse_response("plain text response")
        assert result == "plain text response"

    def test_write_settings_does_not_create_git_exclude_outside_git_repo(self, tmp_path: Path):
        with patch("core.llm_client._git_exclude_file", return_value=None):
            _gemini_write_settings(tmp_path, {"tools": {}})

        assert (tmp_path / ".gemini" / "settings.json").exists()
        assert not (tmp_path / ".git").exists()


class TestGeminiClientCommands:
    @staticmethod
    def _run_result(text: str = "ok", model_tokens: dict[str, dict[str, object]] | None = None):
        payload: dict[str, object] = {"response": text}
        if model_tokens is not None:
            payload["stats"] = {
                "models": {model: {"tokens": tokens} for model, tokens in model_tokens.items()},
            }
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    def test_generate_skips_workspace_trust_check(self):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro", agent_timeout=123))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gemini", "--skip-trust", "--model"]
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "system\n\nuser"
        assert mock_run.call_args.kwargs["timeout"] == 123
        assert "input" not in mock_run.call_args.kwargs
        assert "--approval-mode" not in cmd

    def test_generate_reports_aggregated_model_usage(self):
        events = []
        client = GeminiClient(
            LLMConfig(
                provider="gemini",
                model="gemini-2.5-pro",
                usage_observer=events.append,
            )
        )
        result = self._run_result(
            model_tokens={
                "gemini-2.5-pro": {
                    "input": 10,
                    "candidates": 2,
                    "cached": 4,
                    "total": 18,
                },
                "gemini-2.5-flash": {
                    "input": 5,
                    "candidates": 3,
                    "cached": 0,
                    "total": 9,
                },
                "invalid": {
                    "input": -1,
                    "candidates": True,
                    "cached": "invalid",
                    "total": None,
                },
            }
        )

        with patch("core.llm_client.subprocess.run", return_value=result):
            assert client.generate("system", "user") == "ok"

        assert events[0]["reported_tokens"] == {
            "input_tokens": 15,
            "output_tokens": 5,
            "cached_input_tokens": 4,
            "total_tokens": 27,
        }

    def test_readonly_and_write_agents_report_model_usage(self, tmp_path):
        model_tokens = {
            "gemini-2.5-pro": {
                "input": 7,
                "candidates": 3,
                "cached": 2,
                "total": 12,
            }
        }
        expected = {
            "input_tokens": 7,
            "output_tokens": 3,
            "cached_input_tokens": 2,
            "total_tokens": 12,
        }
        readonly_events = []
        readonly = GeminiClient(
            LLMConfig(provider="gemini", model="gemini-2.5-pro", usage_observer=readonly_events.append)
        )
        write_events = []
        write = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro", usage_observer=write_events.append))

        with (
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client.subprocess.run", return_value=self._run_result(model_tokens=model_tokens)),
        ):
            assert readonly.run_readonly_agent("review", tmp_path) == "ok"

        with (
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=self._run_result(model_tokens=model_tokens),
            ),
        ):
            assert write.run_agent("implement", tmp_path) == ([], "ok")

        assert readonly_events[0]["reported_tokens"] == expected
        assert write_events[0]["reported_tokens"] == expected

    def test_error_response_reports_model_usage(self):
        events = []
        client = GeminiClient(
            LLMConfig(
                provider="gemini",
                model="gemini-2.5-pro",
                usage_observer=events.append,
            )
        )
        result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "error": {"message": "quota exceeded"},
                    "stats": {"models": {"gemini-2.5-pro": {"tokens": {"input": 7, "candidates": 2, "total": 9}}}},
                }
            ),
            stderr="",
        )

        with (
            patch("core.llm_client.subprocess.run", return_value=result),
            pytest.raises(LLMQuotaExceeded),
        ):
            client.generate("system", "user")

        assert events[0]["outcome"] == "fatal_error"
        assert events[0]["reported_tokens"] == {
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
        }

    def test_generate_uses_stdin_for_windows_batch_wrapper(self):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        resolved = r"C:\Users\developer\AppData\Roaming\npm\gemini.CMD"
        process = MagicMock(returncode=0)
        process.communicate.return_value = (json.dumps({"response": "ok"}), "")

        with (
            patch("core.subprocess_utils.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("core.subprocess_utils.subprocess.Popen", return_value=process) as popen,
            patch("core.subprocess_utils.start_windows_process_job", return_value=True),
        ):
            assert client.generate("system", "user") == "ok"

        command_line = popen.call_args.args[0]
        assert command_line.startswith(r"C:\Windows\System32\cmd.exe /e:on /v:off /d /c ")
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_ARG_3"] == "-p"
        assert popen.call_args.kwargs["env"]["_SIKULA_BATCH_ARG_4"] == '""'
        assert "system\n\nuser" not in popen.call_args.kwargs["env"].values()
        process.communicate.assert_called_once_with("system\n\nuser", timeout=1800)

    @pytest.mark.parametrize("extra", [None, ["--approval-mode", "yolo"]])
    def test_windows_batch_invocation_keeps_headless_prompt_flag(self, extra):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))

        with patch(
            "core.llm_client.windows_batch_command_path",
            return_value=r"C:\Users\developer\AppData\Roaming\npm\gemini.CMD",
        ):
            command, stdin_text = client._invocation("prompt", extra)

        assert command[command.index("-p") + 1] == ""
        assert stdin_text == "prompt"
        assert "prompt" not in command
        if extra is not None:
            assert command[command.index("--approval-mode") + 1] == "yolo"

    def test_generate_failure_is_classified(self):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-nope"))
        result = MagicMock(returncode=1, stdout="", stderr="invalid model")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="invalid model"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_prefers_stdout_json_error_over_stderr_warning(self):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        result = MagicMock(
            returncode=1,
            stdout=json.dumps({"error": {"message": "quota exceeded"}}),
            stderr="warning: sandbox setup degraded",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_readonly_agent_skips_workspace_trust_check(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        with (
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run,
        ):
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gemini", "--skip-trust", "--model"]
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "prompt"
        assert "input" not in mock_run.call_args.kwargs
        assert cmd[cmd.index("--approval-mode") + 1] == "yolo"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path

    def test_run_readonly_agent_failure_is_classified(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        result = MagicMock(returncode=1, stdout="not authenticated", stderr="")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_readonly_prefers_stdout_json_error_over_stderr_warning(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        result = MagicMock(
            returncode=1,
            stdout=json.dumps({"error": {"message": "not authenticated"}}),
            stderr="warning: sandbox setup degraded",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_skips_workspace_trust_check(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        with (
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=self._run_result()) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gemini", "--skip-trust", "--model"]
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "prompt"
        assert mock_run.call_args.kwargs["stdin_text"] is None
        assert cmd[cmd.index("--approval-mode") + 1] == "yolo"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert changed == []
        assert output == "ok"

    def test_run_agent_nonzero_exit_is_classified(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-nope"))
        result = subprocess.CompletedProcess([], 1, "", "unsupported model")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_prefers_stdout_json_error_over_stderr_warning(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        result = subprocess.CompletedProcess(
            [],
            1,
            json.dumps({"error": {"message": "quota exceeded"}}),
            "warning: sandbox setup degraded",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            pytest.raises(LLMQuotaExceeded, match="quota exceeded"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_timeout_is_retried(self, tmp_path: Path):
        observer = MagicMock()
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro", retry_observer=observer))

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client._git_snapshot", return_value={}),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[subprocess.TimeoutExpired("gemini", 30), self._run_result()],
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == []
        assert output == "ok"
        assert mock_run.call_count == 2
        assert observer.call_args.args[0]["error_type"] == "LLMTimeoutError"


class TestAntigravityClientCommands:
    @pytest.fixture(autouse=True)
    def _supported_antigravity_version(self):
        with patch("core.llm_client._antigravity_require_supported_version"):
            yield

    def test_readonly_output_sanitizes_standard_windows_file_uri(self):
        workspace = MagicMock(spec=Path)
        workspace.__str__.return_value = r"C:\Users\runner\AppData\Local\Temp\sikula-workspace"
        workspace.as_posix.return_value = "C:/Users/runner/AppData/Local/Temp/sikula-workspace"
        workspace.resolve.return_value = workspace
        output = "See [client](file:///C:/Users/runner/AppData/Local/Temp/sikula-workspace/core/llm_client.py#L12)."

        with patch("core.llm_client.os.name", "nt"):
            sanitized = _antigravity_sanitize_readonly_output(output, workspace)

        assert sanitized == "See [client](core/llm_client.py#L12)."

    def test_readonly_output_sanitizes_encoded_windows_file_uri(self):
        workspace = MagicMock(spec=Path)
        workspace.__str__.return_value = r"C:\Users\Jane Doe\AppData\Local\Temp\sikula-workspace"
        workspace.as_posix.return_value = "C:/Users/Jane Doe/AppData/Local/Temp/sikula-workspace"
        workspace.resolve.return_value = workspace
        output = "See [client](file:///C:/Users/Jane%20Doe/AppData/Local/Temp/sikula-workspace/core/llm_client.py#L12)."

        with patch("core.llm_client.os.name", "nt"):
            sanitized = _antigravity_sanitize_readonly_output(output, workspace)

        assert sanitized == "See [client](core/llm_client.py#L12)."

    @staticmethod
    def _result(
        text: str = "ok",
        *,
        status: str = "SUCCESS",
        usage: dict[str, object] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "status": status,
            "response": text,
        }
        if usage is not None:
            payload["usage"] = usage
        return json.dumps(payload)

    @classmethod
    def _run_result(
        cls,
        text: str = "ok",
        *,
        status: str = "SUCCESS",
        usage: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["agy"], 0, cls._result(text, status=status, usage=usage), "")

    def test_parse_version_extracts_semver(self):
        assert _antigravity_parse_version("agy 1.0.13") == (1, 0, 13)
        assert _antigravity_parse_version("Antigravity CLI version 10.2.3") == (10, 2, 3)
        assert _antigravity_parse_version("dev build") is None

    def test_client_checks_supported_version_on_first_use_only(self):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._antigravity_require_supported_version") as version_check,
            patch("core.llm_client.subprocess.run", return_value=self._run_result("answer")),
        ):
            assert client.generate("system", "user") == "answer"
            assert client.generate("system", "user") == "answer"

        version_check.assert_called_once()

    def test_require_supported_version_accepts_minimum_version(self):
        with patch(
            "core.llm_client.subprocess.run",
            return_value=subprocess.CompletedProcess(["agy", "--version"], 0, "1.1.8\n", ""),
        ) as mock_run:
            _antigravity_require_supported_version()

        mock_run.assert_called_once_with(
            ["agy", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )

    def test_require_supported_version_rejects_old_version(self):
        with (
            patch(
                "core.llm_client.subprocess.run",
                return_value=subprocess.CompletedProcess(["agy", "--version"], 0, "1.1.7\n", ""),
            ),
            pytest.raises(LLMConfigurationError, match="agy 1.1.8 or newer"),
        ):
            _antigravity_require_supported_version()

    def test_require_supported_version_rejects_unparseable_output(self):
        with (
            patch(
                "core.llm_client.subprocess.run",
                return_value=subprocess.CompletedProcess(["agy", "--version"], 0, "dev build\n", ""),
            ),
            pytest.raises(LLMConfigurationError, match="could not parse"),
        ):
            _antigravity_require_supported_version()

    def test_require_supported_version_redacts_failed_check_output(self):
        with (
            patch(
                "core.llm_client.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["agy", "--version"],
                    1,
                    "",
                    "not authenticated OPENAI_API_KEY=secret",
                ),
            ),
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            _antigravity_require_supported_version()

        message = str(exc_info.value)
        assert "OPENAI_API_KEY=<redacted>" in message
        assert "secret" not in message

    def test_require_supported_version_reports_missing_cli(self):
        with (
            patch("core.llm_client.subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(LLMConfigurationError, match="CLI not found"),
        ):
            _antigravity_require_supported_version()

    def test_require_supported_version_reports_timeout(self):
        with (
            patch("core.llm_client.subprocess.run", side_effect=subprocess.TimeoutExpired("agy", 10)),
            pytest.raises(LLMConfigurationError, match="timed out"),
        ):
            _antigravity_require_supported_version()

    def test_result_envelope_normalizes_usage(self):
        envelope = _antigravity_result_envelope(
            self._result(
                " answer \n",
                usage={
                    "input_tokens": 17,
                    "output_tokens": 5,
                    "thinking_tokens": 3,
                    "cache_read_tokens": 11,
                    "total_tokens": 22,
                },
            ),
            "CLI",
        )

        assert envelope.response == "answer"
        assert envelope.reported_tokens == {
            "input_tokens": 17,
            "output_tokens": 5,
            "cached_input_tokens": 11,
            "total_tokens": 22,
        }

    def test_reported_tokens_ignore_invalid_and_unsupported_fields(self):
        assert _antigravity_reported_tokens(
            {
                "input_tokens": True,
                "output_tokens": -1,
                "cache_read_tokens": "4",
                "total_tokens": 9,
                "thinking_tokens": 7,
            }
        ) == {"total_tokens": 9}
        assert _antigravity_reported_tokens(None) == {}

    @pytest.mark.parametrize("output", ["", "not json", "[]", '"text"'])
    def test_result_envelope_rejects_invalid_json_shapes(self, output: str):
        with pytest.raises(LLMTransientError, match="invalid JSON result envelope"):
            _antigravity_result_envelope(output, "CLI")

    def test_result_envelope_rejects_unsuccessful_and_empty_results_with_usage(self):
        usage = {"input_tokens": 7, "output_tokens": 2}

        with pytest.raises(LLMTransientError, match="unsuccessful structured result") as unsuccessful:
            _antigravity_result_envelope(self._result("partial", status="ERROR", usage=usage), "CLI")
        assert unsuccessful.value.output_chars == len("partial")
        assert unsuccessful.value.reported_tokens == usage

        with pytest.raises(LLMTransientError, match="returned no text output") as empty:
            _antigravity_result_envelope(self._result(" \n", usage=usage), "CLI")
        assert empty.value.output_chars == 0
        assert empty.value.reported_tokens == usage

    def test_diagnostic_helpers_cover_empty_long_json_and_log_limit(self, tmp_path: Path):
        assert _antigravity_marker_text("") is None
        assert _antigravity_marker_text("all good") is None
        assert _antigravity_log_line_diagnostic("   ") is None
        assert _antigravity_log_line_diagnostic("{not json") is None
        assert _antigravity_log_line_diagnostic('{"event":"ok"}') is None

        long_text = "x" * 180 + " unsupported model token=secret " + "y" * 380
        marker = _antigravity_marker_text(long_text)
        assert marker is not None
        assert marker.startswith("...")
        assert marker.endswith("...")
        assert "token=<redacted>" in marker
        assert "secret" not in marker

        redacted = _antigravity_redact_diagnostic("ERROR " + "x" * 700 + " OPENAI_API_KEY=secret")
        assert redacted.endswith("...")
        assert len(redacted) <= 503

        list_diagnostic = _antigravity_log_line_diagnostic(
            '{"errors": ["quota exceeded token=secret", {"message": "not authenticated"}]}'
        )
        assert list_diagnostic == "errors: quota exceeded token=<redacted>"

        log_file = tmp_path / "agy.log"
        log_file.write_text("\n".join(f"ERROR unsupported model {index}" for index in range(8)))
        diagnostic = _antigravity_log_diagnostic(log_file)
        assert diagnostic.splitlines() == [f"ERROR unsupported model {index}" for index in range(2, 8)]

    def test_result_error_covers_transient_fallbacks(self):
        with_stdout = _antigravity_result_error(
            subprocess.CompletedProcess(["agy"], 1, "partial stdout", ""),
            "agent",
        )
        assert isinstance(with_stdout, LLMTransientError)
        assert "stdout but no safe diagnostic" in str(with_stdout)

        without_output = _antigravity_result_error(
            subprocess.CompletedProcess(["agy"], 1, "", ""),
            "agent",
        )
        assert isinstance(without_output, LLMTransientError)
        assert str(without_output).endswith("non-zero exit")

        stderr_error = _antigravity_result_error(
            subprocess.CompletedProcess(["agy"], 1, "", "temporary outage"),
            "agent",
            "ERROR failed upstream",
        )
        assert isinstance(stderr_error, LLMTransientError)
        assert "temporary outage" in str(stderr_error)
        assert "log diagnostic" not in str(stderr_error)

    def test_result_error_classifies_long_stderr_before_truncating(self):
        error = _antigravity_result_error(
            subprocess.CompletedProcess(
                ["agy"],
                1,
                "",
                "ERROR " + "x" * 800 + " unsupported model token=secret",
            ),
            "agent",
        )

        message = str(error)
        assert isinstance(error, LLMConfigurationError)
        assert "unsupported model" in message
        assert "token=<redacted>" in message
        assert "secret" not in message
        assert "x" * 600 not in message

    def test_result_error_redacts_multi_token_authorization_headers(self):
        error = _antigravity_result_error(
            subprocess.CompletedProcess(
                ["agy"],
                1,
                "",
                "ERROR Authorization: Basic dXNlcjpwYXNz; Proxy-Authorization = Digest abc def",
            ),
            "agent",
        )

        message = str(error)
        assert isinstance(error, LLMProviderError)
        assert "Authorization: <redacted>" in message
        assert "Proxy-Authorization = <redacted>" in message
        assert "Basic" not in message
        assert "dXNlcjpwYXNz" not in message
        assert "Digest" not in message
        assert "abc def" not in message

    def test_git_path_helpers_handle_failures_and_text_stdout(self, tmp_path: Path):
        with patch(
            "core.llm_client.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 1, "", ""),
        ):
            assert _antigravity_git_paths(tmp_path, ["ls-files", "-z"]) is None
            assert _antigravity_gitlink_paths(tmp_path) is None

        with patch(
            "core.llm_client.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, "one.txt\0dir/\0two.txt\0", ""),
        ):
            assert _antigravity_git_paths(tmp_path, ["ls-files", "-z"]) == {"one.txt", "two.txt"}

        gitlink_stdout = "160000 abcdef\tlibs/api\0not-a-gitlink\tREADME.md\0"
        with patch(
            "core.llm_client.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, gitlink_stdout, ""),
        ):
            assert _antigravity_gitlink_paths(tmp_path) == {"libs/api"}

    def test_copy_policy_returns_none_when_git_listing_fails(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        with patch("core.llm_client._antigravity_git_paths", return_value=None):
            assert _antigravity_copy_policy(repo) is None

    def test_copy_ignore_handles_outside_root_and_preserved_ignored_ancestors(self, tmp_path: Path):
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        ignore_without_policy = _antigravity_copy_ignore(root, None)
        assert ignore_without_policy(str(outside), [".git", ".venv", "keep.txt"]) == {".git", ".venv"}

        dist = root / "dist"
        dist.mkdir()
        (dist / "nested").mkdir()
        (dist / "keep.js").write_text("tracked")
        (dist / "drop.js").write_text("ignored")
        policy = _AntigravityCopyPolicy(
            preserved_paths=frozenset({"dist/keep.js"}),
            preserved_dirs=frozenset({"dist"}),
            ignored_paths=frozenset({"dist/ignored-explicit.js"}),
            ignored_dirs=frozenset({"dist"}),
            gitlink_paths=frozenset(),
        )

        ignore = _antigravity_copy_ignore(root, policy)
        assert ignore(str(dist), ["keep.js", "drop.js", "nested"]) == {"drop.js", "nested"}

    def test_validate_workspace_symlinks_allows_internal_symlink_directories(self, tmp_path: Path):
        target = tmp_path / "target"
        target.mkdir()
        try:
            (tmp_path / "target-link").symlink_to("target")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        _antigravity_validate_workspace_symlinks(tmp_path, prune_ignored_paths=False)

    def test_validate_workspace_symlink_reports_uninspectable_links(self, tmp_path: Path):
        link = tmp_path / "broken-link"

        with (
            patch("core.llm_client.os.readlink", side_effect=OSError("denied")),
            pytest.raises(LLMConfigurationError, match="cannot inspect symlink broken-link"),
        ):
            _antigravity_validate_workspace_symlink(link, tmp_path, Path("broken-link"))

    def test_directory_snapshot_records_symlinks_and_ignored_paths(self, tmp_path: Path):
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "ignored.txt").write_text("ignored")
        (tmp_path / "target-dir").mkdir()
        (tmp_path / "target.txt").write_text("target")
        try:
            (tmp_path / "dir-link").symlink_to("target-dir")
            (tmp_path / "file-link").symlink_to("target.txt")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        snapshot = _antigravity_directory_snapshot(tmp_path)

        assert snapshot[".venv"] == "ignored-dir"
        assert ".venv/ignored.txt" not in snapshot
        assert snapshot["dir-link"] == "symlink-dir:target-dir"
        assert snapshot["file-link"] == "symlink:target.txt"
        assert _antigravity_snapshot_changed({"file-link": "old"}, snapshot)
        assert not _antigravity_snapshot_changed(snapshot, dict(snapshot))

    def test_directory_snapshot_handles_unreadable_symlinks_and_files(self, tmp_path: Path):
        (tmp_path / "target-dir").mkdir()
        (tmp_path / "target.txt").write_text("target")
        (tmp_path / "regular.txt").write_text("regular")
        try:
            (tmp_path / "dir-link").symlink_to("target-dir")
            (tmp_path / "file-link").symlink_to("target.txt")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        real_readlink = os.readlink

        def _readlink(path):
            if Path(path).name in {"dir-link", "file-link"}:
                raise OSError("denied")
            return real_readlink(path)

        with patch("core.llm_client.os.readlink", side_effect=_readlink):
            snapshot = _antigravity_directory_snapshot(tmp_path)

        assert snapshot["dir-link"] == "symlink-dir:<unreadable>"
        assert snapshot["file-link"] == "symlink:<unreadable>"

        real_read_bytes = Path.read_bytes

        def _read_bytes(path):
            if Path(path).name == "regular.txt":
                raise PermissionError("denied")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", _read_bytes):
            snapshot = _antigravity_directory_snapshot(tmp_path)

        assert snapshot["regular.txt"] == "<unavailable>"

    def test_write_agent_prompt_is_idempotent(self, tmp_path: Path):
        prompt = _antigravity_write_agent_prompt("do work", tmp_path)

        assert _antigravity_write_agent_prompt(prompt, tmp_path) == prompt

    def test_generate_uses_stdin_print_mode(self):
        events = []
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                agent_timeout=123,
                usage_observer=events.append,
            )
        )
        usage = {
            "input_tokens": 17,
            "output_tokens": 5,
            "thinking_tokens": 2,
            "cache_read_tokens": 11,
            "total_tokens": 22,
        }

        with patch(
            "core.llm_client.subprocess.run",
            return_value=self._run_result("answer", usage=usage),
        ) as mock_run:
            assert client.generate("system", "user") == "answer"

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["agy", "--new-project"]
        assert "--add-dir" not in cmd
        assert cmd[cmd.index("--model") + 1] == "Gemini 3.5 Flash (High)"
        assert "--sandbox" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[cmd.index("--print-timeout") + 1] == "123s"
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert cmd[cmd.index("--print") + 1] == "-"
        assert cmd.index("--model") < cmd.index("--print")
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"
        assert mock_run.call_args.kwargs["timeout"] == 123
        assert "system\n\nuser" not in cmd
        assert events[0]["output_chars"] == len("answer")
        assert events[0]["reported_tokens"] == {
            "input_tokens": 17,
            "output_tokens": 5,
            "cached_input_tokens": 11,
            "total_tokens": 22,
        }

    def test_run_readonly_agent_uses_disposable_workspace(self, tmp_path: Path):
        (tmp_path / "repo.txt").write_text("source")
        events = []
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                usage_observer=events.append,
            )
        )
        seen: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            workspace = Path(kwargs["cwd"])
            seen["cmd"] = cmd
            seen["cwd"] = workspace
            seen["input"] = kwargs["input"]
            assert (workspace / "repo.txt").read_text() == "source"
            output = (
                f"See [sikula.py](file://{workspace.resolve()}/sikula.py#L12), "
                f"{workspace}/core/llm_client.py, and /tmp/unrelated.py."
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                self._result(output, usage={"input_tokens": 9, "output_tokens": 4}),
                "",
            )

        with patch("core.llm_client._run_provider_cli", side_effect=_fake_run):
            output = client.run_readonly_agent("prompt", tmp_path)

        cmd = seen["cmd"]
        assert isinstance(cmd, list)
        workspace = seen["cwd"]
        assert isinstance(workspace, Path)
        assert workspace != tmp_path
        assert cmd[cmd.index("--add-dir") + 1] == str(workspace)
        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert cmd[cmd.index("--print") + 1] == "-"
        assert seen["input"] == "prompt"
        assert (tmp_path / "repo.txt").read_text() == "source"
        assert str(workspace) not in output
        assert str(workspace.resolve()) not in output
        assert "file://" not in output
        assert "[sikula.py](sikula.py#L12)" in output
        assert "core/llm_client.py" in output
        assert "/tmp/unrelated.py" in output
        assert events[0]["output_chars"] == len(output)
        assert events[0]["reported_tokens"] == {"input_tokens": 9, "output_tokens": 4}

    def test_run_readonly_agent_rejects_disposable_workspace_writes(self, tmp_path: Path):
        (tmp_path / "repo.txt").write_text("source")
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            Path(kwargs["cwd"], "repo.txt").write_text("changed in copy")
            return subprocess.CompletedProcess(cmd, 0, self._result("analysis"), "")

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._run_provider_cli", side_effect=_fake_run) as mock_provider,
            pytest.raises(LLMTransientError, match="disposable workspace"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_provider.call_count == 4
        assert (tmp_path / "repo.txt").read_text() == "source"

    def test_run_readonly_agent_rejects_new_soft_ignored_directories(self, tmp_path: Path):
        (tmp_path / "repo.txt").write_text("source")
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            package_dir = Path(kwargs["cwd"], "node_modules", "pkg")
            package_dir.mkdir(parents=True)
            (package_dir / "index.js").write_text("generated dependency")
            return subprocess.CompletedProcess(cmd, 0, self._result("analysis"), "")

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client._run_provider_cli", side_effect=_fake_run) as mock_provider,
            pytest.raises(LLMTransientError, match="disposable workspace"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_provider.call_count == 4
        assert not (tmp_path / "node_modules").exists()

    def test_run_readonly_agent_preserves_tracked_files_in_ignored_dirs(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".gitignore").write_text("dist/noise.js\n")
        dist = repo / "dist"
        dist.mkdir()
        (dist / "bundle.js").write_text("tracked bundle")
        (dist / "noise.js").write_text("ignored noise")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", ".gitignore", "dist/bundle.js"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        seen: dict[str, Path] = {}
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            workspace = Path(kwargs["cwd"])
            seen["workspace"] = workspace
            assert (workspace / "dist" / "bundle.js").read_text() == "tracked bundle"
            assert not (workspace / "dist" / "noise.js").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed dist/bundle.js"), "")

        with patch("core.llm_client.subprocess.run", side_effect=_fake_run):
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed dist/bundle.js"
        assert seen["workspace"] != repo

    def test_run_readonly_agent_excludes_gitignored_secret_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".gitignore").write_text(".env\n")
        (repo / "README.md").write_text("tracked docs")
        (repo / ".env").write_text("OPENAI_API_KEY=secret")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            workspace = Path(kwargs["cwd"])
            assert (workspace / "README.md").read_text() == "tracked docs"
            assert not (workspace / ".env").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed"), "")

        with patch("core.llm_client.subprocess.run", side_effect=_fake_run):
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed"

    def test_run_readonly_agent_treats_submodule_gitlinks_as_opaque(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        submodule = repo / "libs" / "api"
        submodule.mkdir(parents=True)
        (submodule / ".gitignore").write_text(".env\n")
        (submodule / ".env").write_text("OPENAI_API_KEY=submodule-secret")
        (submodule / "README.md").write_text("submodule docs")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "160000", "1" * 40, "libs/api"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            workspace = Path(kwargs["cwd"])
            assert not (workspace / "libs" / "api" / ".env").exists()
            assert not (workspace / "libs" / "api" / "README.md").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed"), "")

        with patch("core.llm_client.subprocess.run", side_effect=_fake_run):
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed"

    def test_run_readonly_agent_preserves_gitignored_presync_generated_sources(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".gitignore").write_text("build/\ntarget/\n.env\n")
        (repo / "README.md").write_text("tracked docs")
        gradle_generated = repo / "build" / "generated" / "source" / "openapi" / "Dto.java"
        gradle_generated.parent.mkdir(parents=True)
        gradle_generated.write_text("class Dto {}")
        maven_generated = repo / "target" / "generated-sources" / "openapi" / "Model.java"
        maven_generated.parent.mkdir(parents=True)
        maven_generated.write_text("class Model {}")
        ignored_class = repo / "build" / "classes" / "Secret.java"
        ignored_class.parent.mkdir(parents=True)
        ignored_class.write_text("class Secret {}")
        (repo / ".env").write_text("OPENAI_API_KEY=secret")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            workspace = Path(kwargs["cwd"])
            assert (workspace / "build" / "generated" / "source" / "openapi" / "Dto.java").exists()
            assert (workspace / "target" / "generated-sources" / "openapi" / "Model.java").exists()
            assert not (workspace / "build" / "classes" / "Secret.java").exists()
            assert not (workspace / ".env").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed generated sources"), "")

        with patch("core.llm_client.subprocess.run", side_effect=_fake_run):
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed generated sources"

    def test_run_readonly_agent_detects_new_files_in_preserved_ignored_dirs(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        dist = repo / "dist"
        dist.mkdir()
        (dist / "bundle.js").write_text("tracked bundle")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "dist/bundle.js"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        real_run = subprocess.run
        agy_calls = 0

        def _fake_run(cmd, **kwargs):
            nonlocal agy_calls
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            agy_calls += 1
            workspace = Path(kwargs["cwd"])
            (workspace / "dist" / "generated.js").write_text("should be detected")
            return subprocess.CompletedProcess(cmd, 0, self._result("analysis"), "")

        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", side_effect=_fake_run) as mock_run,
            pytest.raises(LLMTransientError, match="disposable workspace"),
        ):
            client.run_readonly_agent("prompt", repo)

        assert agy_calls == 4
        assert mock_run.call_count >= agy_calls
        assert not (repo / "dist" / "generated.js").exists()

    def test_run_readonly_agent_classifies_nonzero_before_workspace_mutation(self, tmp_path: Path):
        (tmp_path / "repo.txt").write_text("source")
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            workspace = Path(kwargs["cwd"])
            (workspace / "repo.txt").write_text("changed in copy")
            log_file = Path(cmd[cmd.index("--log-file") + 1])
            log_file.write_text("ERROR unsupported model: Gemini")
            return subprocess.CompletedProcess(cmd, 1, "", "see log for details")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._run_provider_cli", side_effect=_fake_run) as mock_provider,
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

        assert mock_provider.call_count == 1
        sleep.assert_not_called()
        assert (tmp_path / "repo.txt").read_text() == "source"

    def test_run_readonly_agent_rejects_external_symlinks_before_copying(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external.txt"
        external.write_text("outside")
        try:
            (repo / "external-link.txt").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._run_provider_cli") as mock_provider,
            pytest.raises(LLMConfigurationError, match="external symlink external-link.txt"),
        ):
            client.run_readonly_agent("prompt", repo)

        mock_provider.assert_not_called()
        assert external.read_text() == "outside"

    def test_run_readonly_agent_prunes_ignored_top_level_symlink_before_copying(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external-node-modules"
        external.mkdir()
        try:
            (repo / "node_modules").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            workspace = Path(kwargs["cwd"])
            assert not (workspace / "node_modules").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed"), "")

        with patch("core.llm_client._run_provider_cli", side_effect=_fake_run) as mock_provider:
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed"
        assert mock_provider.called

    def test_run_readonly_agent_prunes_gitignored_symlink_before_copying(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "local-sdk"
        external.mkdir()
        (repo / ".gitignore").write_text("local-sdk\n")
        try:
            (repo / "local-sdk").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            workspace = Path(kwargs["cwd"])
            assert not (workspace / "local-sdk").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed"), "")

        with patch("core.llm_client.subprocess.run", side_effect=_fake_run) as mock_run:
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed"
        assert mock_run.called

    def test_run_readonly_agent_prunes_symlinks_inside_ignored_directories_before_copying(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        venv_bin = repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            (venv_bin / "python3").symlink_to("/usr/bin/python3")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            workspace = Path(kwargs["cwd"])
            assert not (workspace / ".venv").exists()
            return subprocess.CompletedProcess(cmd, 0, self._result("reviewed"), "")

        with patch("core.llm_client._run_provider_cli", side_effect=_fake_run) as mock_provider:
            output = client.run_readonly_agent("prompt", repo)

        assert output == "reviewed"
        assert mock_provider.called

    def test_run_agent_adds_workspace_and_detects_changed_files(self, tmp_path: Path):
        events = []
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                usage_observer=events.append,
            )
        )
        workspace = tmp_path.resolve()

        with (
            patch("core.llm_client._git_snapshot", side_effect=[{}, {"src/app.ts": "hash"}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=self._run_result(
                    "done",
                    usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                ),
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["agy", "--new-project"]
        assert cmd[cmd.index("--add-dir") + 1] == str(workspace)
        assert cmd[cmd.index("--model") + 1] == "Gemini 3.5 Flash (High)"
        assert "--sandbox" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert cmd[cmd.index("--print") + 1] == "-"
        assert mock_run.call_args.kwargs["cwd"] == workspace
        stdin_text = mock_run.call_args.kwargs["stdin_text"]
        assert "ANTIGRAVITY WORKSPACE BOUNDARY" in stdin_text
        assert stdin_text.count("ANTIGRAVITY WORKSPACE BOUNDARY") == 1
        assert f"The only project root for this task is: {workspace.as_posix()}" in stdin_text
        assert "Do not search for, inspect, or modify any other checkout or repository path" in stdin_text
        assert stdin_text.endswith("prompt")
        assert changed == ["src/app.ts"]
        assert output == "done"
        assert events[0]["output_chars"] == len("done")
        assert events[0]["reported_tokens"] == {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
        }

    @pytest.mark.parametrize("response", ["", " \r\n"])
    def test_run_agent_accepts_empty_success_response(self, tmp_path: Path, response: str):
        events = []
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                usage_observer=events.append,
            )
        )

        with (
            patch("core.llm_client._git_snapshot", side_effect=[{}, {"src/app.ts": "hash"}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=self._run_result(
                    response,
                    usage={"input_tokens": 12, "output_tokens": 0, "total_tokens": 12},
                ),
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        assert changed == ["src/app.ts"]
        assert output == ""
        assert events[0]["outcome"] == "success"
        assert events[0]["output_chars"] == 0
        assert events[0]["reported_tokens"] == {
            "input_tokens": 12,
            "output_tokens": 0,
            "total_tokens": 12,
        }

    def test_run_agent_rejects_external_symlinks_before_starting_provider(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external.txt"
        external.write_text("outside")
        try:
            (repo / "external-link.txt").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._run_agent_subprocess_streaming") as mock_run,
            pytest.raises(LLMConfigurationError, match="external symlink external-link.txt"),
        ):
            client.run_agent("prompt", repo)

        mock_run.assert_not_called()
        assert external.read_text() == "outside"

    def test_run_agent_prunes_ignored_top_level_symlink(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "external-node-modules"
        external.mkdir()
        try:
            (repo / "node_modules").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}]),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=self._run_result("done")) as mock_run,
        ):
            changed, output = client.run_agent("prompt", repo)

        assert changed == []
        assert output == "done"
        assert mock_run.called

    def test_run_agent_prunes_gitignored_symlink(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        external = tmp_path / "local-sdk"
        external.mkdir()
        (repo / ".gitignore").write_text("local-sdk\n")
        try:
            (repo / "local-sdk").symlink_to(os.path.relpath(external, repo))
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}]),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=self._run_result("done")) as mock_run,
        ):
            changed, output = client.run_agent("prompt", repo)

        assert changed == []
        assert output == "done"
        assert mock_run.called

    def test_run_agent_validates_tracked_symlinks_inside_soft_ignored_directories(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        node_modules = repo / "node_modules"
        node_modules.mkdir()
        try:
            (node_modules / "hack").symlink_to(tmp_path.parent / "antigravity-outside-target")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "node_modules/hack"], cwd=repo, check=True, capture_output=True)

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._run_agent_subprocess_streaming") as mock_run,
            pytest.raises(LLMConfigurationError, match="absolute symlink node_modules/hack"),
        ):
            client.run_agent("prompt", repo)

        mock_run.assert_not_called()

    def test_run_agent_rejects_submodule_gitlinks(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        submodule = repo / "libs" / "api"
        submodule.mkdir(parents=True)
        (submodule / "README.md").write_text("submodule docs")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "160000", "1" * 40, "libs/api"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._run_agent_subprocess_streaming") as mock_run,
            pytest.raises(LLMConfigurationError, match="git submodules: libs/api"),
        ):
            client.run_agent("prompt", repo)

        mock_run.assert_not_called()

    def test_run_agent_rejects_unpopulated_submodule_gitlinks(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "160000", "1" * 40, "libs/api"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._run_agent_subprocess_streaming") as mock_run,
            pytest.raises(LLMConfigurationError, match="git submodules: libs/api"),
        ):
            client.run_agent("prompt", repo)

        mock_run.assert_not_called()

    def test_run_agent_prunes_symlinks_inside_ignored_directories(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        venv_bin = repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            (venv_bin / "python3").symlink_to("/usr/bin/python3")
        except OSError as exc:
            pytest.skip(f"symlinks are not available: {exc}")

        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}]),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=self._run_result("done")) as mock_run,
        ):
            changed, output = client.run_agent("prompt", repo)

        assert changed == []
        assert output == "done"
        assert mock_run.called

    def test_generate_nonzero_exit_reports_structured_usage(self):
        events = []
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="gemini-nope",
                usage_observer=events.append,
            )
        )
        usage = {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 4,
            "total_tokens": 12,
        }
        result = subprocess.CompletedProcess(
            ["agy"],
            1,
            self._result("partial", status="ERROR", usage=usage),
            "unsupported model",
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()
        assert events[0]["outcome"] == "fatal_error"
        assert events[0]["output_chars"] == len("partial")
        assert events[0]["reported_tokens"] == {
            "input_tokens": 10,
            "output_tokens": 2,
            "cached_input_tokens": 4,
            "total_tokens": 12,
        }

    def test_generate_nonzero_exit_uses_antigravity_log_diagnostic(self):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            log_file = Path(cmd[cmd.index("--log-file") + 1])
            log_file.write_text(
                "ERROR not authenticated "
                "token=supersecret "
                'api_key="quotedsecret" '
                "OPENAI_API_KEY=envsecret "
                "access_token=accesssecret "
                'refresh_token="refreshsecret" '
                '{"token":"jsonsecret","client_secret":"clientsecret"}'
            )
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", side_effect=_fake_run) as mock_run,
            pytest.raises(LLMAuthError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "log diagnostic" in message
        assert "not authenticated" in message
        assert "token=<redacted>" in message
        assert 'api_key="<redacted>"' in message
        assert "OPENAI_API_KEY=<redacted>" in message
        assert "access_token=<redacted>" in message
        assert 'refresh_token="<redacted>"' in message
        assert '"token":"<redacted>"' in message
        assert '"client_secret":"<redacted>"' in message
        assert "supersecret" not in message
        assert "quotedsecret" not in message
        assert "envsecret" not in message
        assert "accesssecret" not in message
        assert "refreshsecret" not in message
        assert "jsonsecret" not in message
        assert "clientsecret" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_nonzero_exit_redacts_antigravity_stderr(self):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        stderr = "ERROR not authenticated OPENAI_API_KEY=envsecret client_secret=clientsecret"

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch(
                "core.llm_client.subprocess.run",
                return_value=subprocess.CompletedProcess(["agy"], 1, "", stderr),
            ) as mock_run,
            pytest.raises(LLMAuthError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "OPENAI_API_KEY=<redacted>" in message
        assert "client_secret=<redacted>" in message
        assert "envsecret" not in message
        assert "clientsecret" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_generate_nonzero_exit_uses_log_diagnostic_when_stderr_is_generic(self):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_run(cmd, **kwargs):
            log_file = Path(cmd[cmd.index("--log-file") + 1])
            log_file.write_text("ERROR not authenticated token=supersecret")
            return subprocess.CompletedProcess(cmd, 1, "", "see log for details")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", side_effect=_fake_run) as mock_run,
            pytest.raises(LLMAuthError) as exc_info,
        ):
            client.generate("system", "user")

        message = str(exc_info.value)
        assert "see log for details" in message
        assert "not authenticated" in message
        assert "token=<redacted>" in message
        assert "supersecret" not in message
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_nonzero_exit_is_classified(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="gemini-nope"))
        result = subprocess.CompletedProcess(["agy"], 1, "", "unsupported model")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_nonzero_exit_uses_antigravity_log_diagnostic(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        def _fake_streaming(cmd, **kwargs):
            log_file = Path(cmd[cmd.index("--log-file") + 1])
            log_file.write_text('{"error": {"message": "unsupported model: Gemini"}}')
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._run_agent_subprocess_streaming", side_effect=_fake_streaming) as mock_run,
            pytest.raises(LLMConfigurationError) as exc_info,
        ):
            client.run_agent("prompt", tmp_path)

        assert "unsupported model" in str(exc_info.value)
        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_retries_timeout_then_succeeds(self, tmp_path: Path):
        observer = MagicMock()
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                retry_observer=observer,
            )
        )

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}, {"src/app.ts": "hash"}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="agy", timeout=12),
                    self._run_result("done"),
                ],
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == ["src/app.ts"]
        assert output == "done"
        assert mock_run.call_count == 2
        sleep.assert_called_once()
        assert observer.call_args.args[0]["error_type"] == "LLMTimeoutError"

    def test_run_agent_timeout_stops_when_partial_changes_exist(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {"src/app.ts": "hash"}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=subprocess.TimeoutExpired(cmd="agy", timeout=12),
            ) as mock_run,
            pytest.raises(LLMTimeoutError, match="timed out after 12s"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_timeout_raises_after_final_attempt(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}, {}, {}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[
                    subprocess.TimeoutExpired(cmd="agy", timeout=12),
                    subprocess.TimeoutExpired(cmd="agy", timeout=12),
                    subprocess.TimeoutExpired(cmd="agy", timeout=12),
                    subprocess.TimeoutExpired(cmd="agy", timeout=12),
                ],
            ) as mock_run,
            pytest.raises(LLMTimeoutError, match="timed out after 12s"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 4
        assert sleep.call_count == 3

    def test_run_agent_retries_transient_error_then_succeeds(self, tmp_path: Path):
        observer = MagicMock()
        client = AntigravityClient(
            LLMConfig(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                retry_observer=observer,
            )
        )
        transient = subprocess.CompletedProcess(["agy"], 1, "partial stdout", "")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}, {"src/app.ts": "hash"}]),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[transient, self._run_result("done")],
            ) as mock_run,
        ):
            changed, output = client.run_agent("prompt", tmp_path)

        assert changed == ["src/app.ts"]
        assert output == "done"
        assert mock_run.call_count == 2
        sleep.assert_called_once()
        assert observer.call_args.args[0]["error_type"] == "LLMTransientError"

    def test_run_agent_transient_error_stops_when_partial_changes_exist(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        transient = subprocess.CompletedProcess(["agy"], 1, "partial stdout", "")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {"src/app.ts": "hash"}]),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=transient) as mock_run,
            pytest.raises(LLMTransientError, match="stdout but no safe diagnostic"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_transient_error_raises_after_final_attempt(self, tmp_path: Path):
        client = AntigravityClient(LLMConfig(provider="antigravity", model="Gemini 3.5 Flash (High)"))
        transient = subprocess.CompletedProcess(["agy"], 1, "partial stdout", "")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._git_snapshot", side_effect=[{}, {}, {}, {}]),
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=transient) as mock_run,
            pytest.raises(LLMTransientError, match="stdout but no safe diagnostic"),
        ):
            client.run_agent("prompt", tmp_path)

        assert mock_run.call_count == 4
        assert sleep.call_count == 3
