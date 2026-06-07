"""Tests for core/llm_client.py — factory, helpers, parse functions."""

from __future__ import annotations

import errno
import json
import subprocess
import threading
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.llm_client import (
    LLMClient,
    LLMAuthError,
    LLMConfig,
    LLMConfigurationError,
    LLMQuotaExceeded,
    LLMTransientError,
    LLMTimeoutError,
    _agent_text_or_empty,
    _call_with_retry,
    _claude_write_settings,
    _codex_parse_text,
    _codex_stream_error,
    _codex_subprocess_error,
    _git_exclude_file,
    _gemini_parse_response,
    _gemini_write_settings,
    _opencode_stream_error,
    _run_agent_subprocess_streaming,
    _opencode_agent_env,
    _opencode_parse_text,
    _run_opencode_streaming,
    _terminate_process,
    create_llm_client,
)
from core.llm_client import ClaudeClient, CodexClient, GeminiClient, OpenCodeClient


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
    def _run_result(cls, text: str = "ok"):
        return MagicMock(
            returncode=0,
            stdout=cls._line({"type": "text", "part": {"text": text}}),
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
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["opencode", "run"]
        assert "system\n\nuser" not in cmd
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"

    def test_run_readonly_agent_passes_prompt_via_stdin(self, tmp_path: Path):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/gpt-5.3-codex"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["opencode", "run"]
        assert "prompt" not in cmd
        assert "--agent" in cmd
        assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
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
        assert mock_run.call_args.kwargs["prompt"] == "prompt"
        assert mock_run.call_args.kwargs["cwd"] == tmp_path
        assert "OPENCODE_CONFIG_DIR" in mock_run.call_args.kwargs["env"]
        assert "--print-logs" in cmd
        assert cmd[cmd.index("--log-level") + 1] == "ERROR"
        assert changed == []
        assert output == "ok"

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

    def test_generate_invalid_model_is_fatal_and_not_retried(self):
        client = OpenCodeClient(LLMConfig(provider="opencode", model="openai/nope"))
        result = MagicMock(returncode=1, stdout="", stderr="invalid model: openai/nope")
        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMConfigurationError, match="invalid model"),
        ):
            client.generate("system", "user")

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
            pytest.raises(LLMQuotaExceeded, match="usage_limit"),
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
            pytest.raises(LLMQuotaExceeded, match="usage_limit"),
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
                if self._polls > len(raw_output) + 2:
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
                if self._polls > len(raw_log) + 5:
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
                if self._polls > len(benign_output) + 2:
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


class TestClaudeWriteSettings:
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
            m.stdout = "analysis done"
            m.stderr = ""
            return m

        with (
            patch("core.llm_client.subprocess.run", side_effect=fake_run) as mock_run,
            patch("core.llm_client._claude_write_settings") as mock_setup,
        ):
            client.run_readonly_agent("review this", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["claude", "-p"]
        assert "review this" not in cmd
        assert mock_run.call_args.kwargs["input"] == "review this"
        mock_setup.assert_called_once_with(tmp_path)

    def test_generate_failure_is_classified(self):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(returncode=1, stdout="", stderr="not authenticated")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            pytest.raises(LLMAuthError, match="not authenticated"),
        ):
            client.generate("system", "user")

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_readonly_agent_failure_is_classified(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = MagicMock(returncode=1, stdout="invalid model", stderr="")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client.subprocess.run", return_value=result) as mock_run,
            patch("core.llm_client._claude_write_settings"),
            pytest.raises(LLMConfigurationError, match="invalid model"),
        ):
            client.run_readonly_agent("review this", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_calls_write_settings(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)

        with (
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                return_value=subprocess.CompletedProcess([], 0, "done", ""),
            ) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings") as mock_setup,
        ):
            client.run_agent("implement this", tmp_path)

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["claude", "-p"]
        assert "implement this" not in cmd
        assert mock_run.call_args.kwargs["stdin_text"] == "implement this"
        mock_setup.assert_called_once_with(tmp_path)

    def test_run_agent_nonzero_exit_is_classified(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6")
        client = ClaudeClient(cfg)
        result = subprocess.CompletedProcess([], 1, "", "unsupported model")

        with (
            patch("core.llm_client.time.sleep") as sleep,
            patch("core.llm_client._run_agent_subprocess_streaming", return_value=result) as mock_run,
            patch("core.llm_client._git_snapshot", return_value={}),
            patch("core.llm_client._claude_write_settings"),
            pytest.raises(LLMConfigurationError, match="unsupported model"),
        ):
            client.run_agent("implement this", tmp_path)

        assert mock_run.call_count == 1
        sleep.assert_not_called()

    def test_run_agent_timeout_is_retried(self, tmp_path):
        cfg = LLMConfig(provider="claude", model="claude-sonnet-4-6", retry_observer=MagicMock())
        client = ClaudeClient(cfg)

        with (
            patch("core.llm_client.time.sleep"),
            patch(
                "core.llm_client._run_agent_subprocess_streaming",
                side_effect=[subprocess.TimeoutExpired("claude", 30), subprocess.CompletedProcess([], 0, "done", "")],
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
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:5] == ["codex", "exec", "--skip-git-repo-check", "--json", "--sandbox"]
        assert cmd[5] == "read-only"
        assert cmd[-1] == "-"
        assert "system\n\nuser" not in cmd
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"

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

    def test_run_readonly_agent_failure_reports_plain_stdout_error(self, tmp_path: Path):
        client = CodexClient(LLMConfig(provider="codex", model="gpt-5.3-codex"))
        failure = MagicMock(
            returncode=1,
            stdout="Error: failed to initialize in-process app-server client",
            stderr="",
        )
        with (
            patch("core.llm_client.time.sleep"),
            patch("core.llm_client.subprocess.run", return_value=failure),
            pytest.raises(RuntimeError, match="failed to initialize in-process app-server client"),
        ):
            client.run_readonly_agent("prompt", tmp_path)

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
    def _run_result(text: str = "ok"):
        return MagicMock(returncode=0, stdout=json.dumps({"response": text}), stderr="")

    def test_generate_skips_workspace_trust_check(self):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        with patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run:
            assert client.generate("system", "user") == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gemini", "--skip-trust", "--model"]
        assert "system\n\nuser" not in cmd
        assert mock_run.call_args.kwargs["input"] == "system\n\nuser"
        assert "--approval-mode" not in cmd

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

    def test_run_readonly_agent_skips_workspace_trust_check(self, tmp_path: Path):
        client = GeminiClient(LLMConfig(provider="gemini", model="gemini-2.5-pro"))
        with (
            patch("core.llm_client._gemini_write_settings"),
            patch("core.llm_client.subprocess.run", return_value=self._run_result()) as mock_run,
        ):
            assert client.run_readonly_agent("prompt", tmp_path) == "ok"

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gemini", "--skip-trust", "--model"]
        assert "prompt" not in cmd
        assert mock_run.call_args.kwargs["input"] == "prompt"
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
        assert "prompt" not in cmd
        assert mock_run.call_args.kwargs["stdin_text"] == "prompt"
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
