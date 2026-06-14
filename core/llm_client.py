"""LLM abstraction layer.

LLMClient defines three operations:
  generate(system, user) -> str           — single-shot text generation; used by PlannerAgent
  run_readonly_agent(prompt, cwd) -> str  — autonomous agent with read-only tools; returns text output
  run_agent(prompt, cwd) -> tuple[list[str], str] — autonomous agent with file tools;
                                              returns (changed file paths, agent text output)

Implementations:
  CodexClient    — provider: "codex"     — uses the codex CLI
  ClaudeClient   — provider: "claude"    — uses the claude CLI
  GeminiClient   — provider: "gemini"    — uses the gemini CLI
  OpenCodeClient — provider: "opencode"  — uses the opencode CLI (model: "provider/model")

To add another provider subclass LLMClient and register it in create_llm_client().
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger(__name__)

# Delays (seconds) between successive retry attempts: attempt 1→2, 2→3, 3→4.
# Total attempts = len(_RETRY_DELAYS) + 1.
_RETRY_DELAYS: tuple[int, ...] = (30, 60, 120)
_MAX_RETRY_ERROR_CHARS = 1000
_RETRY_ERROR_HEAD_CHARS = 350
_STREAM_READ_CHARS = 65536

RetryObserver = Callable[[dict[str, object]], None]


class LLMProviderError(RuntimeError):
    """Base class for LLM provider failures."""


class LLMTransientError(LLMProviderError):
    """Retryable provider failure."""


class LLMTimeoutError(LLMTransientError):
    """Retryable provider timeout."""


class LLMFatalError(LLMProviderError):
    """Non-retryable provider failure."""


class LLMQuotaExceeded(LLMFatalError):
    """Provider account quota, credits, or usage limit is exhausted."""


class LLMAuthError(LLMFatalError):
    """Provider authentication failed."""


class LLMConfigurationError(LLMFatalError):
    """Provider/model configuration is invalid."""


StreamErrorParser = Callable[[str], LLMProviderError | None]


@dataclass
class LLMConfig:
    provider: str = "codex"
    model: str = "gpt-5.3-codex"
    max_tokens: int = 16000  # used by API-based providers; CLI-backed providers may ignore this
    temperature: float = 0.0
    agent_timeout: int = 1800  # seconds; applies to run_agent and run_readonly_agent
    retry_observer: RetryObserver | None = None
    session_title: str | None = None


def _git_snapshot(cwd: Path) -> dict[str, str]:
    """Return {relative_path: sha256(content)} for all files modified or added vs HEAD.

    Hashing content (not just tracking presence) means a file that was already dirty
    before the agent ran is still detected as changed if the agent modifies it further.
    """
    modified = subprocess.run(
        ["git", "diff", "--name-only", "--relative", "HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    result: dict[str, str] = {}
    for line in (modified.stdout + "\n" + untracked.stdout).splitlines():
        path = line.strip()
        if not path:
            continue
        try:
            content = (cwd / path).read_bytes()
            result[path] = hashlib.sha256(content).hexdigest()
        except (FileNotFoundError, IsADirectoryError):
            result[path] = ""
    return result


def _retry_error_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if len(message) > _MAX_RETRY_ERROR_CHARS:
        tail_chars = _MAX_RETRY_ERROR_CHARS - _RETRY_ERROR_HEAD_CHARS
        message = message[:_RETRY_ERROR_HEAD_CHARS] + "\n... [truncated] ...\n" + message[-tail_chars:]
    return message


def _notify_retry(
    config: LLMConfig,
    operation: str,
    attempt: int,
    max_attempts: int,
    delay_s: int,
    exc: Exception,
) -> None:
    if not config.retry_observer:
        return
    try:
        config.retry_observer(
            {
                "provider": config.provider,
                "model": config.model,
                "operation": operation,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_s": delay_s,
                "error": _retry_error_message(exc),
                "error_type": exc.__class__.__name__,
            }
        )
    except Exception:
        log.exception("LLM retry observer failed")


def _provider_error(provider: str, operation: str, message: str) -> LLMProviderError:
    """Classify provider output into retryable vs fatal LLM errors."""
    text = message.strip() or "provider failed"
    lower = text.lower()
    prefix = f"{provider} {operation} error: "
    formatted = text if lower.startswith(f"{provider} ") else f"{prefix}{text}"

    quota_markers = (
        "usage_limit_reached",
        "usage limit has been reached",
        "credits balance: 0",
        "credits-balance: 0",
        "x-codex-credits-balance: 0",
        "credits has credits: false",
        "credits-has-credits: false",
        "x-codex-credits-has-credits: false",
        "quota exceeded",
        "quota_exceeded",
        "resource exhausted",
        "insufficient_quota",
        "credit balance is too low",
        "out of credits",
        "exceeded your current quota",
    )
    auth_markers = (
        "401",
        "unauthorized",
        "unauthenticated",
        "authentication failed",
        "not authenticated",
        "no auth",
        "login required",
        "not logged in",
        "api key",
        "api-key",
        "apikey",
        "invalid key",
        "missing key",
        "invalid token",
        "missing token",
    )
    config_markers = (
        "billing disabled",
        "billing account",
        "invalid model",
        "model not supported",
        "unsupported model",
        "unknown model",
        "unknown provider",
        "provider not found",
        "invalid provider",
        "invalid configuration",
        "configuration error",
        "not enabled for this account",
    )

    if any(marker in lower for marker in quota_markers):
        return LLMQuotaExceeded(formatted)
    if any(marker in lower for marker in auth_markers):
        return LLMAuthError(formatted)
    if any(marker in lower for marker in config_markers):
        return LLMConfigurationError(formatted)
    return LLMTransientError(formatted)


def _call_with_retry(label: str, fn, config: LLMConfig | None = None, operation: str | None = None):
    """Call fn() and retry only retryable LLM failures with exponential backoff."""
    total = len(_RETRY_DELAYS) + 1
    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return fn()
        except subprocess.TimeoutExpired as exc:
            last_exc = LLMTimeoutError(f"{label} timed out after {exc.timeout}s")
            if delay is None:
                break
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %ds",
                label,
                attempt + 1,
                total,
                last_exc,
                delay,
            )
            if config is not None:
                _notify_retry(config, operation or label, attempt + 1, total, delay, last_exc)
            time.sleep(delay)
        except LLMTransientError as exc:
            last_exc = exc
            if delay is None:
                break
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %ds",
                label,
                attempt + 1,
                total,
                exc,
                delay,
            )
            if config is not None:
                _notify_retry(config, operation or label, attempt + 1, total, delay, exc)
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _agent_text_or_empty(parse_fn: Callable[[str], str], output: str) -> str:
    try:
        return parse_fn(output)
    except LLMTransientError as exc:
        if "returned no text output" in str(exc):
            return ""
        raise


class LLMClient:
    """Abstract LLM client. All agents talk to this interface."""

    def set_retry_observer(self, observer: RetryObserver | None) -> RetryObserver | None:
        config = getattr(self, "_config", None)
        if config is not None and hasattr(config, "retry_observer"):
            previous = config.retry_observer
            config.retry_observer = observer
            return previous
        previous = getattr(self, "_retry_observer", None)
        self._retry_observer = observer
        return previous

    def set_session_title(self, title: str | None) -> str | None:
        config = getattr(self, "_config", None)
        if config is not None and hasattr(config, "session_title"):
            previous = config.session_title
            config.session_title = title
            return previous
        previous = getattr(self, "_session_title", None)
        self._session_title = title
        return previous

    def generate(self, system: str, user: str) -> str:
        raise NotImplementedError

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        """Run as an autonomous agent with read-only tools in `cwd`. Returns text output."""
        raise NotImplementedError

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        """Run as an autonomous agent with file tools in `cwd`.

        Returns (changed_file_paths, agent_text_output). Text output is best-effort
        and may be an empty string for providers that do not support structured output
        in write-agent mode.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Claude via CLI
# ---------------------------------------------------------------------------


def _add_git_exclude_entry(cwd: Path, entry: str, comment: str) -> None:
    exclude = _git_exclude_file(cwd)
    if exclude is None:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if entry not in existing:
        with exclude.open("a") as f:
            f.write(f"\n# {comment}\n{entry}\n")


def _claude_write_settings(cwd: Path) -> Path:
    """Write .claude/settings.json into the worktree and return its path.

    When inside a git repository, uses git's info/exclude so .claude/ stays out
    of git diff without touching any tracked file (e.g. the project's own
    .gitignore). Works in both regular repos and worktrees via _git_exclude_file.

    Settings are built with absolute paths — "~/" does not expand in JSON and
    "." is not reliably resolved by the Seatbelt/bubblewrap sandbox.
    The returned path is passed via --settings so Sikula's workspace boundary does not
    rely on project-level Claude settings.
    """
    # denyWrite blocks the home directory and filesystem root at the OS level
    # (Seatbelt on macOS, bubblewrap on Linux); allowWrite restricts writes to
    # the project working directory only.
    settings = {
        "sandbox": {
            "enabled": True,
            "filesystem": {
                "denyWrite": [str(Path.home()) + "/", "//"],
                "allowWrite": [str(cwd)],
            },
        }
    }
    claude_dir = cwd / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))

    _add_git_exclude_entry(cwd, ".claude/", "Sikula Claude settings")

    return settings_path


class ClaudeClient(LLMClient):
    """Calls Claude via the `claude -p` CLI. Requires Claude Code to be installed and authenticated."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(f"Calling LLM ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response...")

        def _call():
            result = subprocess.run(
                ["claude", "-p", "--model", self._config.model],
                capture_output=True,
                input=prompt,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise _provider_error("claude", "CLI", result.stderr.strip() or "non-zero exit")
            return result.stdout.strip()

        return _call_with_retry("generate", _call, self._config, "generate")

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        """Run Claude with read-only tools as an autonomous agent. Returns text output."""
        settings_path = _claude_write_settings(cwd)
        log.info(f"Running Claude read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    self._config.model,
                    "--permission-mode",
                    "acceptEdits",
                    "--settings",
                    str(settings_path),
                    "--allowedTools",
                    "Read,Bash(grep *),Bash(find *),Bash(ls *),LS,Glob",
                ],
                capture_output=True,
                input=prompt,
                text=True,
                cwd=cwd,
                timeout=self._config.agent_timeout,
            )
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip() or "non-zero exit"
                raise _provider_error("claude", "agent", err)
            return result.stdout.strip()

        return _call_with_retry("read-only agent", _call, self._config, "run_readonly_agent")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        """Run Claude with file tools as an autonomous agent. Returns changed file paths."""
        settings_path = _claude_write_settings(cwd)
        before = _git_snapshot(cwd)
        log.info(f"Running Claude agent ({self._config.model}) — waiting for completion...")

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                result = _run_agent_subprocess_streaming(
                    [
                        "claude",
                        "-p",
                        "--model",
                        self._config.model,
                        # acceptEdits: auto-approves file edits; --settings passes Sikula's
                        # generated sandbox config explicitly.
                        "--permission-mode",
                        "acceptEdits",
                        "--settings",
                        str(settings_path),
                        # Bash: read-only commands + git rm for file deletion (tracked by git,
                        # reversible, scoped to the git working tree).
                        "--allowedTools",
                        "Read,Edit,Write,Bash(grep *),Bash(find *),Bash(ls *),Bash(git rm *),LS,Glob",
                    ],
                    cwd=cwd,
                    env=None,
                    timeout=self._config.agent_timeout,
                    provider="claude",
                    stdin_text=prompt,
                )
                if result.returncode != 0:
                    err = result.stderr.strip() or result.stdout.strip() or "non-zero exit"
                    raise _provider_error("claude", "agent", err)
                after = _git_snapshot(cwd)
                changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
                return changed, result.stdout.strip()
            except subprocess.TimeoutExpired as exc:
                last_exc = LLMTimeoutError(f"claude agent timed out after {exc.timeout}s")
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    last_exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, last_exc)
                time.sleep(delay)
            except LLMTransientError as exc:
                last_exc = exc
                if delay is None:
                    break
                # Retry is safe only when the agent has not yet modified any files.
                # Partial changes on disk would cause a second run to operate on a
                # corrupted intermediate state.
                if _git_snapshot(cwd) != before:  # any content change means partial write
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, exc)
                time.sleep(delay)
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# opencode via CLI
# ---------------------------------------------------------------------------

_OPENCODE_READONLY_AGENT = "sikula-readonly"
_OPENCODE_IMPLEMENTER_AGENT = "sikula-implementer"
_OPENCODE_GENERATE_TITLE = "sikula-generate"
_OPENCODE_READONLY_TITLE = "sikula-readonly"
_OPENCODE_IMPLEMENTER_TITLE = "sikula-implementer"

_OPENCODE_READONLY_CONFIG = """\
---
description: Read-only agent for Sikula analysis and review tasks
mode: all
permission:
  bash: deny
  edit: deny
  webfetch: deny
  websearch: deny
  task: deny
  todowrite: deny
  skill: deny
---
"""

_OPENCODE_IMPLEMENTER_CONFIG = """\
---
description: Implementation agent for Sikula code changes
mode: all
permission:
  webfetch: deny
  websearch: deny
  task: deny
  todowrite: deny
  skill: deny
---
"""


def _git_exclude_file(cwd: Path) -> Path | None:
    """Return the path to git's info/exclude for a git working directory.

    Uses git rev-parse --git-common-dir which correctly resolves the common git
    directory from any subdirectory, including worktrees and nested paths.
    Returns None when cwd is not inside a git repository.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode == 0:
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = (cwd / common_dir).resolve()
        return common_dir / "info" / "exclude"
    return None


@contextmanager
def _opencode_agent_env() -> Iterator[dict[str, str]]:
    """Yield an environment that exposes Sikula OpenCode agents without writing into the repo."""
    with tempfile.TemporaryDirectory(prefix="sikula-opencode-") as tmp:
        config_dir = Path(tmp)
        agent_dir = config_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{_OPENCODE_READONLY_AGENT}.md").write_text(_OPENCODE_READONLY_CONFIG)
        (agent_dir / f"{_OPENCODE_IMPLEMENTER_AGENT}.md").write_text(_OPENCODE_IMPLEMENTER_CONFIG)
        env = os.environ.copy()
        env["OPENCODE_CONFIG_DIR"] = str(config_dir)
        yield env


def _opencode_parse_text(output: str) -> str:
    """Extract assistant text from opencode --format json NDJSON output.

    Each line is a JSON event; text events carry the model's response chunks.
    Raises LLMProviderError if the output contains a session error event.
    """
    parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        provider_error = _opencode_error_from_event(event)
        if provider_error is not None:
            raise provider_error
        if event.get("type") == "text":
            text = event.get("part", {}).get("text", "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _opencode_error_from_event(event: dict) -> LLMProviderError | None:
    if event.get("type") != "error":
        return None
    err = event.get("error", {})
    if isinstance(err, dict):
        data = err.get("data", {}) or {}
        headers = data.get("headers") if isinstance(data, dict) else None
        if _opencode_headers_indicate_quota_exhausted(headers):
            if isinstance(data, dict) and data.get("type") == "usage_limit_reached":
                return LLMQuotaExceeded("opencode event error: quota exceeded; usage limit (usage_limit)")
            return LLMQuotaExceeded("opencode event error: quota exceeded")
        msg = data.get("message") if isinstance(data, dict) else None
        msg = msg or err.get("message")
        msg = msg or event.get("message")
        if not msg and isinstance(data, dict):
            markers = []
            for key in ("type", "code"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    markers.append(f"{key}: {value.strip()}")
            msg = "; ".join(markers)
        msg = msg or err.get("name") or "provider error"
    else:
        msg = str(err)
    return _opencode_diagnostic_provider_error("event", msg)


def _opencode_stream_error(line: str) -> LLMProviderError | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return _opencode_error_from_event(event)


def _opencode_stdout_diagnostic(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") != "error":
            continue
        error = str(state.get("error") or "").strip().lower()
        tool = part.get("tool") or "tool"
        if "rejected permission" in error or ("permission" in error and "reject" in error):
            return f"{tool} failed: permission rejected"
        return f"{tool} failed"
    return ""


def _provider_error_label(provider_error: LLMProviderError) -> str:
    if isinstance(provider_error, LLMQuotaExceeded):
        return "quota exceeded"
    if isinstance(provider_error, LLMAuthError):
        return "authentication failed"
    if isinstance(provider_error, LLMConfigurationError):
        return "configuration error"
    return "provider error"


@dataclass(frozen=True)
class _OpenCodeDiagnostic:
    public_message: str
    error_type: type[LLMProviderError] | None = None

    def to_provider_error(self, operation: str) -> LLMProviderError:
        message = f"opencode {operation} error: {self.public_message}"
        if self.error_type is not None:
            return self.error_type(message)
        return LLMTransientError(message)


def _opencode_stderr_diagnostic(
    stderr: str,
    operation: str,
    *,
    classify_plain_fatal: bool = False,
) -> _OpenCodeDiagnostic | None:
    first_structured_diagnostic: _OpenCodeDiagnostic | None = None
    for line in stderr.splitlines():
        structured_parts = _opencode_log_structured_parts(line, include_error_payload=True)
        if not structured_parts:
            continue
        headers = structured_parts.get("responseHeaders")
        if _opencode_headers_indicate_quota_exhausted(headers):
            response_body = structured_parts.get("responseBody")
            body_error = response_body.get("error") if isinstance(response_body, dict) else None
            if isinstance(body_error, dict) and body_error.get("type") == "usage_limit_reached":
                return _OpenCodeDiagnostic(
                    "opencode provider diagnostic: quota exceeded; usage limit (usage_limit)",
                    LLMQuotaExceeded,
                )
            return _OpenCodeDiagnostic("opencode provider diagnostic: quota exceeded", LLMQuotaExceeded)
        provider_error = _opencode_diagnostic_provider_error("stderr", json.dumps(structured_parts))
        error_type = type(provider_error) if isinstance(provider_error, LLMFatalError) else None
        diagnostic = _OpenCodeDiagnostic(
            f"opencode provider diagnostic: {_provider_error_label(provider_error)}",
            error_type,
        )
        if diagnostic.error_type is not None:
            return diagnostic
        if first_structured_diagnostic is None:
            first_structured_diagnostic = diagnostic
    if first_structured_diagnostic is not None:
        return first_structured_diagnostic
    if stderr.strip():
        if classify_plain_fatal:
            provider_error = _provider_error("opencode", operation, stderr)
            if isinstance(provider_error, LLMFatalError):
                return _OpenCodeDiagnostic(
                    f"opencode provider diagnostic: {_provider_error_label(provider_error)}",
                    type(provider_error),
                )
        return _OpenCodeDiagnostic("stderr without a safe diagnostic")
    return None


def _opencode_error(result: subprocess.CompletedProcess[str], operation: str) -> LLMProviderError:
    """Return a safe typed error from an opencode subprocess result."""
    stderr_diagnostic = _opencode_stderr_diagnostic(result.stderr or "", operation, classify_plain_fatal=True)
    if stderr_diagnostic is not None:
        return stderr_diagnostic.to_provider_error(operation)
    if result.stdout.strip():
        return LLMTransientError(f"opencode {operation} error: non-zero exit with stdout but no safe diagnostic")
    return LLMTransientError(f"opencode {operation} error: non-zero exit")


def _opencode_result_error(result: subprocess.CompletedProcess[str], operation: str) -> LLMProviderError:
    """Return the best typed error from an opencode subprocess result."""
    stdout = result.stdout.strip()
    if stdout:
        try:
            _opencode_parse_text(stdout)
        except LLMProviderError as exc:
            return exc
    return _opencode_error(result, operation)


def _subprocess_output_excerpt(result: subprocess.CompletedProcess[str]) -> str:
    parts = []
    stderr = _opencode_stderr_diagnostic(result.stderr or "", "diagnostic")
    if stderr is not None:
        parts.append(f"stderr: {stderr.public_message}")

    stdout_diagnostic = _opencode_stdout_diagnostic(result.stdout or "")
    if stdout_diagnostic:
        parts.append(f"stdout: {stdout_diagnostic}")
    return "\n".join(parts)


def _opencode_no_text_message(result: subprocess.CompletedProcess[str], context: str) -> str:
    message = f"opencode {context} error: returned no text output"
    excerpt = _subprocess_output_excerpt(result)
    if excerpt:
        message = f"{message}\n{excerpt}"
    return message


def _opencode_diagnostic_provider_error(operation: str, message: str) -> LLMProviderError:
    lower = message.lower()
    if "invalid_request_error" in lower or "unsupported_value" in lower or "unsupported value" in lower:
        return LLMConfigurationError(message)
    return _provider_error("opencode", operation, message)


def _opencode_no_text_error(result: subprocess.CompletedProcess[str], context: str) -> LLMProviderError:
    message = _opencode_no_text_message(result, context)
    stderr_diagnostic = _opencode_stderr_diagnostic(result.stderr or "", context)
    if stderr_diagnostic is not None and stderr_diagnostic.error_type is not None:
        return stderr_diagnostic.error_type(message)
    provider_error = _opencode_diagnostic_provider_error(context, message)
    if isinstance(provider_error, LLMFatalError):
        return type(provider_error)(message)
    return LLMTransientError(message)


def _signal_process_group(process: subprocess.Popen[str], sig: int) -> bool:
    pid = getattr(process, "pid", None)
    if os.name != "posix" or not isinstance(pid, int):
        return False
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return True
    return True


def _terminate_process(process: subprocess.Popen[str], *, process_group: bool = False) -> None:
    if process_group:
        if _signal_process_group(process, signal.SIGTERM):
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.2)
            _signal_process_group(process, signal.SIGKILL)
            return

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _json_object_after_marker(text: str, marker: str) -> object | None:
    marker_pos = text.find(marker)
    if marker_pos < 0:
        return None
    start = text.find("{", marker_pos + len(marker))
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : pos + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _opencode_log_structured_parts(line: str, *, include_error_payload: bool = False) -> dict[str, object]:
    structured_parts: dict[str, object] = {}
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        event = None
    if isinstance(event, dict):
        if "responseBody" in event:
            structured_parts["responseBody"] = event["responseBody"]
        if "responseHeaders" in event:
            structured_parts["responseHeaders"] = event["responseHeaders"]

    response_body = _json_object_after_marker(line, "responseBody=")
    if response_body is not None:
        structured_parts["responseBody"] = response_body
    response_headers = _json_object_after_marker(line, "responseHeaders=")
    if response_headers is not None:
        structured_parts["responseHeaders"] = response_headers

    if include_error_payload:
        error_payload = _json_object_after_marker(line, "error=")
        if error_payload is not None:
            structured_parts["error"] = error_payload

    return structured_parts


def _opencode_log_error(line: str) -> LLMProviderError | None:
    diagnostic = _opencode_stderr_diagnostic(line, "agent")
    if diagnostic is not None and diagnostic.error_type is not None:
        return diagnostic.to_provider_error("agent")
    return None


def _opencode_headers_indicate_quota_exhausted(headers: object) -> bool:
    if not isinstance(headers, dict):
        return False
    normalized = {str(key).lower(): value for key, value in headers.items()}
    balance = normalized.get("x-codex-credits-balance")
    has_credits = normalized.get("x-codex-credits-has-credits")
    return str(balance).strip() == "0" or str(has_credits).strip().lower() == "false"


def _opencode_log_diagnostic_error(line: str) -> LLMProviderError | None:
    diagnostic = _opencode_stderr_diagnostic(line, "log")
    if diagnostic is None or diagnostic.error_type is None:
        return None
    return diagnostic.to_provider_error("log")


def _warn_opencode_success_diagnostics(stderr: str) -> None:
    for line in stderr.splitlines():
        provider_error = _opencode_log_diagnostic_error(line)
        if provider_error is not None:
            log.warning("opencode reported provider diagnostic: %s", _provider_error_label(provider_error))


def _opencode_error_log_args() -> list[str]:
    return ["--print-logs", "--log-level", "ERROR"]


def _opencode_title(value: str | None, fallback: str) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return title[:80].strip("-") or fallback


def _run_agent_subprocess_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: int,
    provider: str,
    stdin_text: str | None = None,
    stdout_error_parser: StreamErrorParser | None = None,
    stderr_error_parser: StreamErrorParser | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an agent subprocess while watching provider-owned events for fatal errors."""
    popen_kwargs = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **popen_kwargs)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    chunks: queue.Queue[tuple[str, str]] = queue.Queue()
    writer_errors: queue.Queue[OSError] = queue.Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_line_buffer = ""
    stderr_line_buffer = ""

    def _parse_stream_buffer(buffer: str, parser: StreamErrorParser | None) -> tuple[str, LLMProviderError | None]:
        if parser is None:
            return buffer, None
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            provider_error = parser(line)
            if provider_error is not None:
                return buffer, provider_error
        provider_error = parser(buffer)
        return buffer, provider_error

    def _record_chunk(name: str, chunk: str) -> LLMProviderError | None:
        nonlocal stdout_line_buffer, stderr_line_buffer
        if name == "stdout":
            stdout_parts.append(chunk)
            stdout_line_buffer, provider_error = _parse_stream_buffer(stdout_line_buffer + chunk, stdout_error_parser)
            return provider_error
        stderr_parts.append(chunk)
        stderr_line_buffer, provider_error = _parse_stream_buffer(stderr_line_buffer + chunk, stderr_error_parser)
        return provider_error

    def _record_or_raise(name: str, chunk: str) -> None:
        provider_error = _record_chunk(name, chunk)
        if provider_error is not None:
            _terminate_process(process, process_group=True)
            raise provider_error

    def _drain_ready_chunks(deadline: float | None = None) -> None:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                _raise_timeout()
            try:
                name, chunk = chunks.get_nowait()
            except queue.Empty:
                break
            _record_or_raise(name, chunk)
            process.poll()

    def _raise_timeout() -> None:
        _terminate_process(process, process_group=True)
        _drain_ready_chunks()
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(stdout_parts), stderr="".join(stderr_parts))

    def _check_writer_error() -> None:
        try:
            writer_error = writer_errors.get_nowait()
        except queue.Empty:
            return
        _terminate_process(process, process_group=True)
        raise writer_error

    def _reader(name: str, stream) -> None:
        try:
            read_chunk = getattr(stream, "readline", None)
            if not callable(read_chunk):
                read_chunk = stream.read
            while True:
                chunk = read_chunk(_STREAM_READ_CHARS)
                if not chunk:
                    break
                chunks.put((name, chunk))
        finally:
            stream.close()

    def _writer() -> None:
        try:
            if stdin_text is not None:
                process.stdin.write(stdin_text)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except OSError as exc:
            if exc.errno != errno.EPIPE:
                writer_errors.put(exc)

    threads = [
        threading.Thread(target=_reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    deadline = time.monotonic() + timeout
    while True:
        _drain_ready_chunks(deadline)
        _check_writer_error()
        if process.poll() is not None and not any(thread.is_alive() for thread in threads) and chunks.empty():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_timeout()
        try:
            name, chunk = chunks.get(timeout=min(0.2, remaining))
        except queue.Empty:
            continue
        _record_or_raise(name, chunk)

    while writer_thread.is_alive():
        _check_writer_error()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_timeout()
        writer_thread.join(timeout=min(0.2, remaining))
    _check_writer_error()

    return subprocess.CompletedProcess(cmd, process.returncode or 0, "".join(stdout_parts), "".join(stderr_parts))


def _run_opencode_streaming(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run opencode while watching output for provider errors before process exit."""
    return _run_agent_subprocess_streaming(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        provider="opencode",
        stdin_text=prompt,
        stdout_error_parser=_opencode_stream_error,
        stderr_error_parser=_opencode_log_error,
    )


class OpenCodeClient(LLMClient):
    """Calls opencode via the `opencode run` CLI.

    Requires opencode to be installed and authenticated.
    Model must be in provider/model format, e.g. openai/gpt-5.3-codex.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(
            f"Calling LLM via opencode ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response..."
        )

        def _call():
            result = subprocess.run(
                [
                    "opencode",
                    "run",
                    "--model",
                    self._config.model,
                    "--title",
                    _opencode_title(self._config.session_title, _OPENCODE_GENERATE_TITLE),
                    "--format",
                    "json",
                    *_opencode_error_log_args(),
                ],
                capture_output=True,
                input=prompt,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise _opencode_result_error(result, "CLI")
            _warn_opencode_success_diagnostics(result.stderr)
            text = _opencode_parse_text(result.stdout)
            if not text:
                raise _opencode_no_text_error(result, "CLI")
            return text

        return _call_with_retry("generate", _call, self._config, "generate")

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        log.info(f"Running opencode read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            with _opencode_agent_env() as env:
                result = subprocess.run(
                    [
                        "opencode",
                        "run",
                        "--dir",
                        str(cwd),
                        "--model",
                        self._config.model,
                        "--agent",
                        _OPENCODE_READONLY_AGENT,
                        "--title",
                        _opencode_title(self._config.session_title, _OPENCODE_READONLY_TITLE),
                        "--format",
                        "json",
                        *_opencode_error_log_args(),
                    ],
                    capture_output=True,
                    input=prompt,
                    text=True,
                    cwd=cwd,
                    env=env,
                    timeout=self._config.agent_timeout,
                )
            if result.returncode != 0:
                raise _opencode_result_error(result, "agent")
            _warn_opencode_success_diagnostics(result.stderr)
            text = _opencode_parse_text(result.stdout)
            if not text:
                raise _opencode_no_text_error(result, "agent")
            return text

        return _call_with_retry("read-only agent", _call, self._config, "run_readonly_agent")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        before = _git_snapshot(cwd)
        log.info(f"Running opencode agent ({self._config.model}) — waiting for completion...")

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                with _opencode_agent_env() as env:
                    result = _run_opencode_streaming(
                        [
                            "opencode",
                            "run",
                            "--dir",
                            str(cwd),
                            "--model",
                            self._config.model,
                            "--agent",
                            _OPENCODE_IMPLEMENTER_AGENT,
                            "--title",
                            _opencode_title(self._config.session_title, _OPENCODE_IMPLEMENTER_TITLE),
                            "--format",
                            "json",
                            *_opencode_error_log_args(),
                        ],
                        prompt=prompt,
                        cwd=cwd,
                        env=env,
                        timeout=self._config.agent_timeout,
                    )
                if result.returncode != 0:
                    raise _opencode_result_error(result, "agent")
                _warn_opencode_success_diagnostics(result.stderr)
                after = _git_snapshot(cwd)
                changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
                text = _agent_text_or_empty(_opencode_parse_text, result.stdout)
                if not text:
                    message = _opencode_no_text_message(result, "agent")
                    if not changed:
                        raise _opencode_no_text_error(result, "agent")
                    text = message
                return changed, text
            except subprocess.TimeoutExpired as exc:
                last_exc = LLMTimeoutError(f"opencode agent timed out after {exc.timeout}s")
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    last_exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, last_exc)
                time.sleep(delay)
            except LLMTransientError as exc:
                last_exc = exc
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, exc)
                time.sleep(delay)
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Gemini via CLI
# ---------------------------------------------------------------------------

_GEMINI_SETTINGS_READONLY = {
    "tools": {
        # tools.core is a full allowlist — anything not listed is DENY.
        # update_topic is a built-in meta-tool for session context; model calls it on unexpected
        # events (build errors, test failures). Harmless; must be allowed to avoid tool errors.
        "core": [
            "read_file",
            "read_many_files",
            "glob",
            "grep_search",
            "list_directory",
            "update_topic",
        ]
    }
}

_GEMINI_SETTINGS_IMPLEMENTER = {
    "tools": {
        # tools.core is a full allowlist — anything not listed is DENY.
        # update_topic: see comment in _GEMINI_SETTINGS_READONLY above.
        "core": [
            "read_file",
            "read_many_files",
            "glob",
            "grep_search",
            "list_directory",
            "write_file",
            "replace",
            "run_shell_command",
            "update_topic",
        ]
    }
}


def _gemini_write_settings(cwd: Path, settings: dict) -> None:
    """Write .gemini/settings.json into the worktree and hide it from git.

    When inside a git repository, uses git's info/exclude so .gemini/ stays out
    of git diff without touching any tracked file (e.g. the project's own
    .gitignore). Works in both regular repos and worktrees via _git_exclude_file.
    """
    gemini_dir = cwd / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    (gemini_dir / "settings.json").write_text(json.dumps(settings, indent=2))

    _add_git_exclude_entry(cwd, ".gemini/", "Sikula Gemini settings")


def _gemini_parse_response(output: str) -> str:
    """Parse JSON output from gemini --output-format json.

    Raises LLMProviderError if the response contains an error field.
    Falls back to returning raw output if JSON parsing fails.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return output.strip()
    if data.get("error"):
        err = data["error"]
        msg = err.get("message") or str(err) if isinstance(err, dict) else str(err)
        raise _provider_error("gemini", "response", msg)
    text = data.get("response", "").strip()
    if not text:
        raise LLMTransientError("gemini response error: returned no text output")
    return text


def _gemini_result_error(result: subprocess.CompletedProcess[str], operation: str) -> LLMProviderError:
    stdout = result.stdout.strip()
    if stdout:
        try:
            _gemini_parse_response(stdout)
        except LLMProviderError as exc:
            return exc
    return _provider_error("gemini", operation, result.stderr.strip() or stdout or "non-zero exit")


class GeminiClient(LLMClient):
    """Calls Gemini via the `gemini -p` CLI.

    Requires gemini to be installed and authenticated.
    Model must be a valid Gemini model ID, e.g. gemini-2.5-pro.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def _cmd(self, prompt: str, extra: list[str] | None = None) -> list[str]:
        return [
            "gemini",
            "--skip-trust",
            "--model",
            self._config.model,
            "-p",
            prompt,
            *(extra or []),
            "--output-format",
            "json",
        ]

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(f"Calling LLM via Gemini ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response...")

        def _call():
            result = subprocess.run(
                self._cmd(prompt),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise _gemini_result_error(result, "CLI")
            return _gemini_parse_response(result.stdout)

        return _call_with_retry("generate", _call, self._config, "generate")

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        _gemini_write_settings(cwd, _GEMINI_SETTINGS_READONLY)
        log.info(f"Running Gemini read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = subprocess.run(
                self._cmd(prompt, ["--approval-mode", "yolo"]),
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=self._config.agent_timeout,
            )
            if result.returncode != 0:
                raise _gemini_result_error(result, "agent")
            return _gemini_parse_response(result.stdout)

        return _call_with_retry("read-only agent", _call, self._config, "run_readonly_agent")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        _gemini_write_settings(cwd, _GEMINI_SETTINGS_IMPLEMENTER)
        before = _git_snapshot(cwd)
        log.info(f"Running Gemini agent ({self._config.model}) — waiting for completion...")

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                result = _run_agent_subprocess_streaming(
                    self._cmd(prompt, ["--approval-mode", "yolo"]),
                    cwd=cwd,
                    env=None,
                    timeout=self._config.agent_timeout,
                    provider="gemini",
                )
                if result.returncode != 0:
                    raise _gemini_result_error(result, "agent")
                after = _git_snapshot(cwd)
                changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
                text = _agent_text_or_empty(_gemini_parse_response, result.stdout)
                return changed, text
            except subprocess.TimeoutExpired as exc:
                last_exc = LLMTimeoutError(f"gemini agent timed out after {exc.timeout}s")
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    last_exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, last_exc)
                time.sleep(delay)
            except LLMTransientError as exc:
                last_exc = exc
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, exc)
                time.sleep(delay)
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CodexClient
# ---------------------------------------------------------------------------


def _codex_parse_text(output: str) -> str:
    """Extract assistant text from codex exec --json JSONL output.

    Collects final assistant text from both legacy item.completed events and
    current Codex session JSONL response/event messages.
    Raises LLMProviderError on error/turn.failed events or when no text was produced.
    """
    parts: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        provider_error = _codex_event_error(event)
        if provider_error is not None:
            raise provider_error
        etype = event.get("type")
        if etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    parts.append(text)
        elif etype == "response_item":
            payload = event.get("payload", {})
            if payload.get("type") == "message" and payload.get("phase") in (None, "final_answer"):
                text = _codex_message_content_text(payload).strip()
                if text:
                    parts.append(text)
        elif etype == "event_msg":
            payload = event.get("payload", {})
            if payload.get("type") == "agent_message" and payload.get("phase") in (None, "final_answer"):
                text = str(payload.get("message", "")).strip()
                if text:
                    parts.append(text)
        elif etype == "task_complete":
            msg = event.get("payload", {}).get("last_agent_message", "")
            text = str(msg).strip()
            if text and not parts:
                parts.append(text)
    if not parts:
        raise LLMTransientError("codex output error: returned no text output")
    return "\n".join(parts)


def _codex_stream_error(line: str) -> LLMProviderError | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return _codex_event_error(event)


def _codex_event_error(event: dict) -> LLMProviderError | None:
    etype = event.get("type")
    if etype == "turn.failed":
        msg = _codex_error_message(event.get("error") or event.get("data") or event)
        return _provider_error("codex", "turn", f"codex turn failed: {msg}")
    if etype == "error":
        msg = _codex_error_message(event.get("error") or event.get("message") or event.get("data") or event)
        return _provider_error("codex", "event", f"codex error: {msg}")
    if etype == "event_msg":
        payload = event.get("payload", {})
        if isinstance(payload, dict) and payload.get("type") in ("error", "turn.failed"):
            msg = _codex_error_message(payload.get("error") or payload.get("message") or payload)
            return _provider_error("codex", "event", f"codex error: {msg}")
    return None


def _codex_message_content_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("output_text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _codex_error_message(raw: object) -> str:
    """Extract a readable error message from Codex JSON event payloads."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        nested = _codex_error_message(parsed)
        return nested or text
    if isinstance(raw, dict):
        message = raw.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        for key in ("error", "data"):
            nested = _codex_error_message(raw.get(key))
            if nested:
                return nested
        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return json.dumps(raw)
    return str(raw).strip()


def _codex_subprocess_error(result: subprocess.CompletedProcess[str]) -> str:
    """Return the most useful error text from a codex subprocess result."""
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        saw_json_event = _codex_output_has_json_events(stdout)
        try:
            _codex_parse_text(stdout)
        except RuntimeError as exc:
            message = str(exc)
            if "returned no text output" in message:
                if stderr:
                    return stderr
                if saw_json_event:
                    return "codex exited before producing a final answer or structured error"
                return stdout
            return message
        return stdout
    if stderr:
        return stderr
    return "non-zero exit"


def _codex_output_has_json_events(output: str) -> bool:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type"):
            return True
    return False


class CodexClient(LLMClient):
    """Calls Codex via the `codex exec` CLI.

    Requires codex to be installed and authenticated (codex login).
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def _exec_cmd(self, sandbox: str) -> list[str]:
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            sandbox,
            "-m",
            self._config.model,
            "-",
        ]

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(f"Calling LLM via Codex ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response...")

        def _call():
            result = subprocess.run(
                self._exec_cmd("read-only"),
                capture_output=True,
                input=prompt,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                raise _provider_error("codex", "CLI", _codex_subprocess_error(result))
            return _codex_parse_text(result.stdout)

        return _call_with_retry("generate", _call, self._config, "generate")

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        log.info(f"Running Codex read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = subprocess.run(
                self._exec_cmd("read-only"),
                capture_output=True,
                input=prompt,
                text=True,
                cwd=cwd,
                timeout=self._config.agent_timeout,
            )
            if result.returncode != 0:
                raise _provider_error("codex", "agent", _codex_subprocess_error(result))
            return _codex_parse_text(result.stdout)

        return _call_with_retry("read-only agent", _call, self._config, "run_readonly_agent")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        before = _git_snapshot(cwd)
        log.info(f"Running Codex agent ({self._config.model}) — waiting for completion...")

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                result = _run_agent_subprocess_streaming(
                    self._exec_cmd("workspace-write"),
                    cwd=cwd,
                    env=None,
                    timeout=self._config.agent_timeout,
                    provider="codex",
                    stdin_text=prompt,
                    stdout_error_parser=_codex_stream_error,
                )
                if result.returncode != 0:
                    raise _provider_error("codex", "agent", _codex_subprocess_error(result))
                after = _git_snapshot(cwd)
                changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
                text = _agent_text_or_empty(_codex_parse_text, result.stdout)
                return changed, text
            except subprocess.TimeoutExpired as exc:
                last_exc = LLMTimeoutError(f"codex agent timed out after {exc.timeout}s")
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    last_exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, last_exc)
                time.sleep(delay)
            except LLMTransientError as exc:
                last_exc = exc
                if delay is None:
                    break
                if _git_snapshot(cwd) != before:
                    log.warning("Agent failed after partial file changes — not retrying")
                    break
                log.warning(
                    "Agent call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    total,
                    exc,
                    delay,
                )
                _notify_retry(self._config, "run_agent", attempt + 1, total, delay, exc)
                time.sleep(delay)
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_llm_client(config: LLMConfig) -> LLMClient:
    if config.provider == "codex":
        return CodexClient(config)
    if config.provider == "claude":
        return ClaudeClient(config)
    if config.provider == "gemini":
        return GeminiClient(config)
    if config.provider == "opencode":
        return OpenCodeClient(config)
    raise ValueError(f"Unknown LLM provider: {config.provider!r}. Add it to llm_client.py.")
