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
  AntigravityClient — provider: "antigravity" — uses the agy CLI

To add another provider subclass LLMClient and register it in create_llm_client().
"""

from __future__ import annotations

import codecs
import errno
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote

from core.subprocess_utils import (
    release_windows_process_job,
    resolve_windows_batch_command,
    run_windows_batch_process,
    start_windows_process_job,
    terminate_windows_process_tree,
    windows_batch_command_path,
)

log = logging.getLogger(__name__)

# Delays (seconds) between successive retry attempts: attempt 1→2, 2→3, 3→4.
# Total attempts = len(_RETRY_DELAYS) + 1.
_RETRY_DELAYS: tuple[int, ...] = (30, 60, 120)
_MAX_RETRY_ERROR_CHARS = 1000
_RETRY_ERROR_HEAD_CHARS = 350
_STREAM_READ_CHARS = 65536
_LOCAL_ENVIRONMENT_ERRNOS = frozenset(
    err
    for err in (
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
        errno.ENOSPC,
        getattr(errno, "EDQUOT", None),
    )
    if err is not None
)

RetryObserver = Callable[[dict[str, object]], None]
UsageObserver = Callable[[dict[str, object]], None]


class LLMProviderError(RuntimeError):
    """Base class for LLM provider failures."""

    def __init__(
        self,
        *args: object,
        output_chars: int | None = None,
        reported_tokens: dict[str, int] | None = None,
    ) -> None:
        super().__init__(*args)
        self.output_chars = output_chars
        self.reported_tokens = reported_tokens


class LLMTransientError(LLMProviderError):
    """Retryable provider failure."""


class LLMTimeoutError(LLMTransientError):
    """Retryable provider timeout."""


class LLMFatalError(LLMProviderError):
    """Non-retryable provider failure."""


class LLMReadOnlyViolation(LLMFatalError):
    """Provider violated an enforced read-only boundary."""


class LLMQuotaExceeded(LLMFatalError):
    """Provider account quota, credits, or usage limit is exhausted."""


class LLMAuthError(LLMFatalError):
    """Provider authentication failed."""


class LLMConfigurationError(LLMFatalError):
    """Provider/model configuration is invalid."""


class LLMEnvironmentError(LLMFatalError):
    """Local provider CLI runtime environment is unusable."""


StreamErrorParser = Callable[[str], LLMProviderError | None]


@dataclass
class LLMConfig:
    provider: str = "codex"
    model: str = "gpt-5.3-codex"
    max_tokens: int = 16000  # used by API-based providers; CLI-backed providers may ignore this
    temperature: float = 0.0
    agent_timeout: int = 1800  # seconds; applies to CLI-backed provider subprocess calls
    retry_observer: RetryObserver | None = None
    usage_observer: UsageObserver | None = None
    session_title: str | None = None


@dataclass
class _LLMCallValue:
    value: Any
    output_chars: int | None = None
    reported_tokens: dict[str, int] | None = None


@dataclass
class _LLMAttemptObservation:
    output_chars: int | None = None
    reported_tokens: dict[str, int] | None = None

    def complete(self, value: object, *, reported_tokens: dict[str, int] | None = None) -> None:
        if isinstance(value, str):
            self.output_chars = len(value)
        self.reported_tokens = reported_tokens


def _notify_usage(
    config: LLMConfig,
    *,
    operation: str,
    attempt: int,
    max_attempts: int,
    outcome: str,
    elapsed_s: float,
    input_chars: int,
    observation: _LLMAttemptObservation,
    error_type: str | None = None,
) -> None:
    if not config.usage_observer:
        return
    event: dict[str, object] = {
        "provider": config.provider,
        "model": config.model,
        "operation": operation,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "outcome": outcome,
        "elapsed_s": elapsed_s,
        "input_chars": input_chars,
    }
    if observation.output_chars is not None:
        event["output_chars"] = observation.output_chars
    if observation.reported_tokens:
        event["reported_tokens"] = dict(observation.reported_tokens)
    if error_type:
        event["error_type"] = error_type
    try:
        config.usage_observer(event)
    except Exception:
        log.exception("LLM usage observer failed")


def _usage_outcome(exc: BaseException) -> str:
    if isinstance(exc, (subprocess.TimeoutExpired, LLMTimeoutError)):
        return "timeout"
    if isinstance(exc, LLMTransientError):
        return "retryable_error"
    if isinstance(exc, LLMFatalError):
        return "fatal_error"
    return "error"


@contextmanager
def _observe_llm_attempt(
    config: LLMConfig,
    *,
    operation: str,
    attempt: int,
    max_attempts: int,
    input_chars: int,
) -> Iterator[_LLMAttemptObservation]:
    started = time.perf_counter()
    observation = _LLMAttemptObservation()
    try:
        yield observation
    except BaseException as exc:
        if isinstance(exc, LLMProviderError):
            observation.output_chars = exc.output_chars
            observation.reported_tokens = exc.reported_tokens
        _notify_usage(
            config,
            operation=operation,
            attempt=attempt,
            max_attempts=max_attempts,
            outcome=_usage_outcome(exc),
            elapsed_s=time.perf_counter() - started,
            input_chars=input_chars,
            observation=observation,
            error_type=exc.__class__.__name__,
        )
        raise
    else:
        _notify_usage(
            config,
            operation=operation,
            attempt=attempt,
            max_attempts=max_attempts,
            outcome="success",
            elapsed_s=time.perf_counter() - started,
            input_chars=input_chars,
            observation=observation,
        )


def _call_observed(
    config: LLMConfig,
    *,
    operation: str,
    attempt: int,
    max_attempts: int,
    input_chars: int,
    fn,
):
    with _observe_llm_attempt(
        config,
        operation=operation,
        attempt=attempt,
        max_attempts=max_attempts,
        input_chars=input_chars,
    ) as observation:
        value = fn()
        if isinstance(value, _LLMCallValue):
            observation.output_chars = value.output_chars
            observation.reported_tokens = value.reported_tokens
            return value.value
        observation.complete(value)
        return value


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


def _local_environment_error_from_os_error(label: str, exc: OSError) -> LLMEnvironmentError | None:
    if exc.errno not in _LOCAL_ENVIRONMENT_ERRNOS:
        return None
    message = str(exc).strip() or exc.__class__.__name__
    return LLMEnvironmentError(f"{label} local environment error: {message}")


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


def _call_with_retry(
    label: str,
    fn,
    config: LLMConfig | None = None,
    operation: str | None = None,
    *,
    input_chars: int = 0,
    before_attempt: Callable[[], None] | None = None,
):
    """Call fn() and retry only retryable LLM failures with exponential backoff."""
    total = len(_RETRY_DELAYS) + 1
    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        if before_attempt is not None:
            before_attempt()
        try:
            if config is None:
                return fn()

            def _observed_call():
                try:
                    return fn()
                except OSError as exc:
                    environment_error = _local_environment_error_from_os_error(label, exc)
                    if environment_error is not None:
                        raise environment_error from exc
                    raise

            return _call_observed(
                config,
                operation=operation or label,
                attempt=attempt + 1,
                max_attempts=total,
                input_chars=input_chars,
                fn=_observed_call,
            )
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
        except OSError as exc:
            environment_error = _local_environment_error_from_os_error(label, exc)
            if environment_error is not None:
                raise environment_error from exc
            raise
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

    def set_usage_observer(self, observer: UsageObserver | None) -> UsageObserver | None:
        config = getattr(self, "_config", None)
        if config is not None and hasattr(config, "usage_observer"):
            previous = config.usage_observer
            config.usage_observer = observer
            return previous
        previous = getattr(self, "_usage_observer", None)
        self._usage_observer = observer
        return previous

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

    def prepare_agent_prompt(self, prompt: str, cwd: Path) -> str:
        """Return the effective prompt that will be sent to run_agent.

        Providers with mandatory wrapper instructions can override this so
        agents record the same prompt that the provider subprocess receives.
        """
        return prompt

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


def _run_provider_cli(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    env = kwargs.get("env")
    args, executable, batch_env = resolve_windows_batch_command(
        command,
        env=env if isinstance(env, dict) else None,
    )
    # Pin UTF-8 for text-mode calls so the prompt (passed via input=) is not
    # encoded with the process locale codec (e.g. cp1250 on Windows), which
    # raises UnicodeEncodeError and never reaches the provider. Byte-mode calls
    # are left untouched.
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    try:
        if executable is not None:
            kwargs["env"] = batch_env
            return run_windows_batch_process(args, executable=executable, **kwargs)
        return subprocess.run(args, **kwargs)
    except FileNotFoundError as exc:
        raise LLMConfigurationError(
            f"{command[0]} CLI not found: install and authenticate the configured provider"
        ) from exc


_CLAUDE_API_ERROR_STATUS_RE = re.compile(r"^\s*API Error:\s*(?P<status>[1-5]\d{2})\b", re.IGNORECASE)
_CLAUDE_RESULT_LOGIN_RE = re.compile(
    r"^\s*not logged in\s*(?:\u00b7|[-:])?\s*please run /login[.!]?\s*$",
    re.IGNORECASE,
)
_CLAUDE_TERMINAL_LIMIT_SUBTYPES = frozenset(
    {
        "error_max_budget_usd",
        "error_max_structured_output_retries",
        "error_max_turns",
    }
)
_CLAUDE_RESULT_SUBTYPES = _CLAUDE_TERMINAL_LIMIT_SUBTYPES | {
    "error_during_execution",
    "success",
}
_CLAUDE_RETRYABLE_CLIENT_STATUSES = frozenset({408, 409, 429})
_CLAUDE_CLI_ARGUMENT_ERROR_RE = re.compile(
    r"^(?:error:\s*)?(?:"
    r"(?:invalid|unknown|unrecognized|unsupported|unexpected)\s+(?:argument|option|flag)\b"
    r"|(?:found\s+)?(?:argument|option|flag)\b.{0,200}\b(?:was not|wasn't)\s+expected\b"
    r")",
    re.IGNORECASE,
)
_CLAUDE_QUOTA_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
    "out of credits",
    "quota exceeded",
    "usage limit has been reached",
    "usage_limit_reached",
)
_CLAUDE_AUTH_MARKERS = (
    "authentication failed",
    "invalid api key",
    "invalid key",
    "invalid token",
    "login required",
    "missing api key",
    "missing key",
    "missing token",
    "not authenticated",
    "not logged in",
    "unauthenticated",
    "unauthorized",
)
_CLAUDE_CONFIG_MARKERS = (
    "billing disabled",
    "invalid configuration",
    "invalid model",
    "model not supported",
    "not enabled for this account",
    "unknown model",
    "unsupported model",
)
_CLAUDE_TRANSIENT_MARKERS = (
    ("connection reset", "connection reset"),
    ("connection refused", "connection refused"),
    ("timed out", "request timed out"),
    ("timeout", "request timed out"),
)


@dataclass(frozen=True)
class _ClaudeResultEnvelope:
    subtype: str
    is_error: bool
    result: str
    errors: tuple[str, ...]
    api_error_status: int | None
    reported_tokens: dict[str, int] | None


def _claude_result_envelope(output: str) -> _ClaudeResultEnvelope | None:
    """Parse Claude's documented JSON result event, including verbose arrays."""
    try:
        payload = json.loads(output)
    except (ValueError, RecursionError, TypeError):
        return None

    if isinstance(payload, dict):
        candidates = [payload]
    elif isinstance(payload, list):
        candidates = [item for item in payload if isinstance(item, dict)]
    else:
        return None
    event = next((item for item in reversed(candidates) if item.get("type") == "result"), None)
    if event is None or not isinstance(event.get("is_error"), bool):
        return None

    raw_subtype = event.get("subtype")
    subtype = raw_subtype if isinstance(raw_subtype, str) and raw_subtype in _CLAUDE_RESULT_SUBTYPES else "unknown"
    raw_result = event.get("result")
    result = raw_result if isinstance(raw_result, str) else ""
    raw_errors = event.get("errors")
    errors = tuple(item for item in raw_errors[:8] if isinstance(item, str)) if isinstance(raw_errors, list) else ()
    raw_status = event.get("api_error_status")
    status = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool) and 100 <= raw_status <= 599
        else None
    )
    reported_tokens = {}
    raw_usage = event.get("usage")
    if isinstance(raw_usage, dict):
        for source_key, target_key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cached_input_tokens"),
            ("cache_creation_input_tokens", "cache_creation_input_tokens"),
        ):
            value = raw_usage.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                reported_tokens[target_key] = value
    return _ClaudeResultEnvelope(
        subtype=subtype,
        is_error=event["is_error"],
        result=result,
        errors=errors,
        api_error_status=status,
        reported_tokens=reported_tokens or None,
    )


def _claude_error_signal(envelope: _ClaudeResultEnvelope, *, include_result: bool = False) -> str:
    fields = list(envelope.errors)
    if include_result and envelope.result:
        fields.append(envelope.result)
    return "\n".join(field[:1000] for field in fields[:8] if field).lower()


def _claude_error_status(envelope: _ClaudeResultEnvelope) -> int | None:
    if envelope.api_error_status is not None:
        return envelope.api_error_status
    for field in envelope.errors:
        match = _CLAUDE_API_ERROR_STATUS_RE.search(field[:1000])
        if match:
            return int(match.group("status"))
    return None


def _claude_process_error(
    operation: str,
    envelope: _ClaudeResultEnvelope | None,
) -> LLMProviderError:
    """Classify only Claude-owned result fields and expose safe diagnostics."""
    if envelope is None:
        return LLMTransientError(f"claude {operation} error: invalid JSON result envelope")

    signal = _claude_error_signal(envelope)
    status = _claude_error_status(envelope)
    if envelope.subtype in _CLAUDE_TERMINAL_LIMIT_SUBTYPES:
        error_type: type[LLMProviderError] = LLMConfigurationError
        reason = envelope.subtype.removeprefix("error_").replace("_", " ")
    elif status in {401, 403}:
        error_type = LLMAuthError
        reason = "authentication failed"
    elif status == 402:
        error_type = LLMQuotaExceeded
        reason = "quota exhausted"
    elif any(marker in signal for marker in _CLAUDE_AUTH_MARKERS) or _CLAUDE_RESULT_LOGIN_RE.fullmatch(envelope.result):
        error_type = LLMAuthError
        reason = "authentication failed"
    elif any(marker in signal for marker in _CLAUDE_QUOTA_MARKERS):
        error_type = LLMQuotaExceeded
        reason = "quota exhausted"
    elif any(marker in signal for marker in _CLAUDE_CONFIG_MARKERS):
        error_type = LLMConfigurationError
        reason = "configuration invalid"
    elif status in _CLAUDE_RETRYABLE_CLIENT_STATUSES or (status is not None and 500 <= status < 600):
        error_type = LLMTransientError
        reason = next(
            (label for marker, label in _CLAUDE_TRANSIENT_MARKERS if marker in signal),
            "provider execution failed",
        )
    elif status is not None and 400 <= status < 500:
        client_signal = _claude_error_signal(envelope, include_result=True)
        if any(marker in client_signal for marker in _CLAUDE_QUOTA_MARKERS):
            error_type = LLMQuotaExceeded
            reason = "quota exhausted"
        elif any(marker in client_signal for marker in _CLAUDE_AUTH_MARKERS):
            error_type = LLMAuthError
            reason = "authentication failed"
        else:
            error_type = LLMConfigurationError
            reason = "configuration invalid"
    else:
        error_type = LLMTransientError
        reason = next(
            (label for marker, label in _CLAUDE_TRANSIENT_MARKERS if marker in signal),
            "provider execution failed",
        )

    context: list[str] = []
    if status is not None:
        context.append(f"HTTP {status}")
    if envelope.subtype not in {"success", "unknown"}:
        context.append(envelope.subtype)
    suffix = f" ({', '.join(context)})" if context else ""
    return error_type(
        f"claude {operation} error: {reason}{suffix}",
        output_chars=len(envelope.result.strip()),
        reported_tokens=envelope.reported_tokens,
    )


def _claude_cli_startup_error(operation: str, stderr: str) -> LLMConfigurationError | None:
    """Recognize a bounded CLI argument-parser failure without exposing stderr."""
    for line in stderr[:4096].splitlines()[:32]:
        if _CLAUDE_CLI_ARGUMENT_ERROR_RE.match(line[:500].strip()):
            return LLMConfigurationError(
                f"claude {operation} error: Claude CLI rejected a required option; update Claude CLI"
            )
    return None


def _claude_completed_result(
    result: subprocess.CompletedProcess[str],
    operation: str,
) -> _ClaudeResultEnvelope:
    envelope = _claude_result_envelope(result.stdout or "")
    if envelope is None and result.returncode != 0:
        startup_error = _claude_cli_startup_error(operation, result.stderr or "")
        if startup_error is not None:
            raise startup_error
    if result.returncode != 0 or envelope is None or envelope.is_error:
        raise _claude_process_error(operation, envelope)
    return envelope


class ClaudeClient(LLMClient):
    """Calls Claude via the `claude -p` CLI. Requires Claude Code to be installed and authenticated."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(f"Calling LLM ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response...")

        def _call():
            result = _run_provider_cli(
                ["claude", "-p", "--output-format", "json", "--model", self._config.model],
                capture_output=True,
                input=prompt,
                text=True,
                timeout=self._config.agent_timeout,
            )
            envelope = _claude_completed_result(result, "CLI")
            output = envelope.result.strip()
            return _LLMCallValue(
                output,
                output_chars=len(output),
                reported_tokens=envelope.reported_tokens,
            )

        return _call_with_retry("generate", _call, self._config, "generate", input_chars=len(prompt))

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        """Run Claude with read-only tools as an autonomous agent. Returns text output."""
        settings_path = _claude_write_settings(cwd)
        log.info(f"Running Claude read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = _run_provider_cli(
                [
                    "claude",
                    "-p",
                    "--output-format",
                    "json",
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
            envelope = _claude_completed_result(result, "agent")
            output = envelope.result.strip()
            return _LLMCallValue(
                output,
                output_chars=len(output),
                reported_tokens=envelope.reported_tokens,
            )

        return _call_with_retry(
            "read-only agent",
            _call,
            self._config,
            "run_readonly_agent",
            input_chars=len(prompt),
        )

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        """Run Claude with file tools as an autonomous agent. Returns changed file paths."""
        settings_path = _claude_write_settings(cwd)
        before = _git_snapshot(cwd)
        log.info(f"Running Claude agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = _run_agent_subprocess_streaming(
                [
                    "claude",
                    "-p",
                    "--output-format",
                    "json",
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
            envelope = _claude_completed_result(result, "agent")
            output = envelope.result.strip()
            after = _git_snapshot(cwd)
            changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
            return _LLMCallValue(
                (changed, output),
                output_chars=len(output),
                reported_tokens=envelope.reported_tokens,
            )

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return _call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt + 1,
                    max_attempts=total,
                    input_chars=len(prompt),
                    fn=_call,
                )
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


def _opencode_reported_tokens(output: str) -> dict[str, int]:
    """Aggregate explicit usage from OpenCode step-finish events."""
    reported: dict[str, int] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict):
            continue
        for source_key, target_key in (("input", "input_tokens"), ("output", "output_tokens")):
            value = tokens.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                reported[target_key] = reported.get(target_key, 0) + value
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for source_key, target_key in (
                ("read", "cached_input_tokens"),
                ("write", "cache_creation_input_tokens"),
            ):
                value = cache.get(source_key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    reported[target_key] = reported.get(target_key, 0) + value
    return reported


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
        if terminate_windows_process_tree(process):
            return
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
    launch_cmd, executable, batch_env = resolve_windows_batch_command(cmd, env=env)
    popen_kwargs = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        # Provider CLIs speak UTF-8 on stdin/stdout. Pin the pipe encoding so the
        # prompt is not encoded with the process locale codec (e.g. cp1250 on
        # Windows), which raises UnicodeEncodeError on characters outside that
        # codepage and never delivers the prompt. "replace" keeps decoding of
        # provider output resilient to any stray non-UTF-8 bytes.
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)) | int(
            getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    if executable is not None:
        popen_kwargs["executable"] = executable
        popen_kwargs["env"] = batch_env
    try:
        process = subprocess.Popen(launch_cmd, **popen_kwargs)
    except FileNotFoundError as exc:
        raise LLMConfigurationError(
            f"{cmd[0]} CLI not found: install and authenticate the configured provider"
        ) from exc
    except OSError as exc:
        environment_error = _local_environment_error_from_os_error(f"{provider} agent", exc)
        if environment_error is not None:
            raise environment_error from exc
        raise
    if os.name == "nt":
        job_started = start_windows_process_job(process)
        if job_started is False:
            raise LLMEnvironmentError(f"{provider} agent could not initialize Windows process isolation")
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
        environment_error = _local_environment_error_from_os_error(f"{provider} agent", writer_error)
        if environment_error is not None:
            raise environment_error from writer_error
        raise writer_error

    def _reader(name: str, stream) -> None:
        try:
            try:
                fd = stream.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None

            if fd is not None:
                encoding = getattr(stream, "encoding", None) or "utf-8"
                try:
                    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
                except LookupError:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while True:
                    data = os.read(fd, _STREAM_READ_CHARS)
                    if not data:
                        break
                    chunk = decoder.decode(data, final=False)
                    if chunk:
                        chunks.put((name, chunk))
                tail = decoder.decode(b"", final=True)
                if tail:
                    chunks.put((name, tail))
                return

            while True:
                chunk = stream.read(_STREAM_READ_CHARS)
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

    try:
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

        return subprocess.CompletedProcess(
            launch_cmd,
            process.returncode or 0,
            "".join(stdout_parts),
            "".join(stderr_parts),
        )
    except BaseException:
        _terminate_process(process, process_group=True)
        raise
    finally:
        release_windows_process_job(process)


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
            result = _run_provider_cli(
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
                timeout=self._config.agent_timeout,
            )
            reported_tokens = _opencode_reported_tokens(result.stdout)
            if result.returncode != 0:
                error = _opencode_result_error(result, "CLI")
                error.reported_tokens = reported_tokens or None
                raise error
            _warn_opencode_success_diagnostics(result.stderr)
            try:
                text = _opencode_parse_text(result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            if not text:
                error = _opencode_no_text_error(result, "CLI")
                error.reported_tokens = reported_tokens or None
                raise error
            return _LLMCallValue(text, output_chars=len(text), reported_tokens=reported_tokens or None)

        return _call_with_retry("generate", _call, self._config, "generate", input_chars=len(prompt))

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        log.info(f"Running opencode read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            with _opencode_agent_env() as env:
                result = _run_provider_cli(
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
            reported_tokens = _opencode_reported_tokens(result.stdout)
            if result.returncode != 0:
                error = _opencode_result_error(result, "agent")
                error.reported_tokens = reported_tokens or None
                raise error
            _warn_opencode_success_diagnostics(result.stderr)
            try:
                text = _opencode_parse_text(result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            if not text:
                error = _opencode_no_text_error(result, "agent")
                error.reported_tokens = reported_tokens or None
                raise error
            return _LLMCallValue(text, output_chars=len(text), reported_tokens=reported_tokens or None)

        return _call_with_retry(
            "read-only agent",
            _call,
            self._config,
            "run_readonly_agent",
            input_chars=len(prompt),
        )

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        before = _git_snapshot(cwd)
        log.info(f"Running opencode agent ({self._config.model}) — waiting for completion...")

        def _call():
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
            reported_tokens = _opencode_reported_tokens(result.stdout)
            if result.returncode != 0:
                error = _opencode_result_error(result, "agent")
                error.reported_tokens = reported_tokens or None
                raise error
            _warn_opencode_success_diagnostics(result.stderr)
            after = _git_snapshot(cwd)
            changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
            try:
                text = _agent_text_or_empty(_opencode_parse_text, result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            output_chars = len(text)
            if not text:
                message = _opencode_no_text_message(result, "agent")
                if not changed:
                    error = _opencode_no_text_error(result, "agent")
                    error.reported_tokens = reported_tokens or None
                    raise error
                text = message
            return _LLMCallValue(
                (changed, text),
                output_chars=output_chars,
                reported_tokens=reported_tokens or None,
            )

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return _call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt + 1,
                    max_attempts=total,
                    input_chars=len(prompt),
                    fn=_call,
                )
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


def _gemini_reported_tokens(output: str) -> dict[str, int]:
    """Aggregate explicit per-model usage from Gemini JSON output."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    stats = data.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict):
        return {}

    reported: dict[str, int] = {}
    for model_stats in models.values():
        tokens = model_stats.get("tokens") if isinstance(model_stats, dict) else None
        if not isinstance(tokens, dict):
            continue
        for source_key, target_key in (
            ("input", "input_tokens"),
            ("candidates", "output_tokens"),
            ("cached", "cached_input_tokens"),
            ("total", "total_tokens"),
        ):
            value = tokens.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                reported[target_key] = reported.get(target_key, 0) + value
    return reported


def _gemini_result_error(result: subprocess.CompletedProcess[str], operation: str) -> LLMProviderError:
    stdout = result.stdout.strip()
    reported_tokens = _gemini_reported_tokens(stdout)
    if stdout:
        try:
            _gemini_parse_response(stdout)
        except LLMProviderError as exc:
            exc.reported_tokens = reported_tokens or None
            return exc
    error = _provider_error("gemini", operation, result.stderr.strip() or stdout or "non-zero exit")
    error.reported_tokens = reported_tokens or None
    return error


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

    def _invocation(self, prompt: str, extra: list[str] | None = None) -> tuple[list[str], str | None]:
        command = self._cmd(prompt, extra)
        if windows_batch_command_path(command) is None:
            return command, None
        return self._cmd("", extra), prompt

    def generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n{user}"
        log.info(f"Calling LLM via Gemini ({self._config.model}, ~{len(prompt) // 4} tokens) — waiting for response...")

        def _call():
            command, stdin_text = self._invocation(prompt)
            run_kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": self._config.agent_timeout,
            }
            if stdin_text is not None:
                run_kwargs["input"] = stdin_text
            result = _run_provider_cli(command, **run_kwargs)
            if result.returncode != 0:
                raise _gemini_result_error(result, "CLI")
            reported_tokens = _gemini_reported_tokens(result.stdout)
            try:
                text = _gemini_parse_response(result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            return _LLMCallValue(
                text,
                output_chars=len(text),
                reported_tokens=reported_tokens or None,
            )

        return _call_with_retry("generate", _call, self._config, "generate", input_chars=len(prompt))

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        _gemini_write_settings(cwd, _GEMINI_SETTINGS_READONLY)
        log.info(f"Running Gemini read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            command, stdin_text = self._invocation(prompt, ["--approval-mode", "yolo"])
            run_kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "cwd": cwd,
                "timeout": self._config.agent_timeout,
            }
            if stdin_text is not None:
                run_kwargs["input"] = stdin_text
            result = _run_provider_cli(command, **run_kwargs)
            if result.returncode != 0:
                raise _gemini_result_error(result, "agent")
            reported_tokens = _gemini_reported_tokens(result.stdout)
            try:
                text = _gemini_parse_response(result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            return _LLMCallValue(
                text,
                output_chars=len(text),
                reported_tokens=reported_tokens or None,
            )

        return _call_with_retry(
            "read-only agent",
            _call,
            self._config,
            "run_readonly_agent",
            input_chars=len(prompt),
        )

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        _gemini_write_settings(cwd, _GEMINI_SETTINGS_IMPLEMENTER)
        before = _git_snapshot(cwd)
        log.info(f"Running Gemini agent ({self._config.model}) — waiting for completion...")

        def _call():
            command, stdin_text = self._invocation(prompt, ["--approval-mode", "yolo"])
            result = _run_agent_subprocess_streaming(
                command,
                cwd=cwd,
                env=None,
                timeout=self._config.agent_timeout,
                provider="gemini",
                stdin_text=stdin_text,
            )
            if result.returncode != 0:
                raise _gemini_result_error(result, "agent")
            after = _git_snapshot(cwd)
            changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
            reported_tokens = _gemini_reported_tokens(result.stdout)
            try:
                text = _agent_text_or_empty(_gemini_parse_response, result.stdout)
            except LLMProviderError as exc:
                exc.reported_tokens = reported_tokens or None
                raise
            return _LLMCallValue(
                (changed, text),
                output_chars=len(text),
                reported_tokens=reported_tokens or None,
            )

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return _call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt + 1,
                    max_attempts=total,
                    input_chars=len(prompt),
                    fn=_call,
                )
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


def _codex_reported_tokens(output: str) -> dict[str, int]:
    """Read the final explicit token usage object from Codex JSONL."""
    reported: dict[str, int] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        candidate = {}
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                candidate[key] = value
        if candidate:
            reported = candidate
    return reported


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
            result = _run_provider_cli(
                self._exec_cmd("read-only"),
                capture_output=True,
                input=prompt,
                text=True,
                timeout=self._config.agent_timeout,
            )
            if result.returncode != 0:
                raise _provider_error("codex", "CLI", _codex_subprocess_error(result))
            text = _codex_parse_text(result.stdout)
            return _LLMCallValue(
                text,
                output_chars=len(text),
                reported_tokens=_codex_reported_tokens(result.stdout),
            )

        return _call_with_retry("generate", _call, self._config, "generate", input_chars=len(prompt))

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        log.info(f"Running Codex read-only agent ({self._config.model}) — waiting for completion...")

        def _call():
            result = _run_provider_cli(
                self._exec_cmd("read-only"),
                capture_output=True,
                input=prompt,
                text=True,
                cwd=cwd,
                timeout=self._config.agent_timeout,
            )
            if result.returncode != 0:
                raise _provider_error("codex", "agent", _codex_subprocess_error(result))
            text = _codex_parse_text(result.stdout)
            return _LLMCallValue(
                text,
                output_chars=len(text),
                reported_tokens=_codex_reported_tokens(result.stdout),
            )

        return _call_with_retry(
            "read-only agent",
            _call,
            self._config,
            "run_readonly_agent",
            input_chars=len(prompt),
        )

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        before = _git_snapshot(cwd)
        log.info(f"Running Codex agent ({self._config.model}) — waiting for completion...")

        def _call():
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
            return _LLMCallValue(
                (changed, text),
                output_chars=len(text),
                reported_tokens=_codex_reported_tokens(result.stdout),
            )

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return _call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt + 1,
                    max_attempts=total,
                    input_chars=len(prompt),
                    fn=_call,
                )
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
# AntigravityClient
# ---------------------------------------------------------------------------

_ANTIGRAVITY_HARD_IGNORED_COPY_DIRS = {
    ".git",
    ".hg",
    ".svn",
}
_ANTIGRAVITY_SOFT_IGNORED_COPY_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "DerivedData",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_ANTIGRAVITY_IGNORED_COPY_PATHS = {
    ".sikula/state",
    ".sikula/worktrees",
}
_ANTIGRAVITY_IGNORED_COPY_DIRS = _ANTIGRAVITY_HARD_IGNORED_COPY_DIRS | _ANTIGRAVITY_SOFT_IGNORED_COPY_DIRS
_ANTIGRAVITY_GENERATED_SOURCE_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".groovy",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".m",
    ".mm",
    ".proto",
    ".py",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
)
_ANTIGRAVITY_LOG_DIAGNOSTIC_BYTES = 65536
_ANTIGRAVITY_LOG_DIAGNOSTIC_LINES = 6
_ANTIGRAVITY_LOG_DIAGNOSTIC_LINE_CHARS = 500
_ANTIGRAVITY_MIN_VERSION = (1, 1, 12)
_ANTIGRAVITY_HOOK_PREFLIGHT_TIMEOUT = 60
_ANTIGRAVITY_READONLY_TOOLS = (
    "view_file",
    "list_dir",
    "find_by_name",
    "grep_search",
)
_ANTIGRAVITY_LOG_DIAGNOSTIC_MARKERS = (
    "401",
    "api key",
    "api-key",
    "apikey",
    "auth",
    "billing",
    "configuration",
    "error",
    "exception",
    "failed",
    "invalid key",
    "invalid model",
    "invalid token",
    "login",
    "missing key",
    "missing token",
    "model not supported",
    "not authenticated",
    "not enabled",
    "not logged in",
    "out of credits",
    "quota",
    "resource exhausted",
    "unauthenticated",
    "unauthorized",
    "unknown model",
    "unsupported model",
)
_ANTIGRAVITY_PROVIDER_ERROR_MARKERS = (
    "usage_limit_reached",
    "usage limit has been reached",
    "quota exceeded",
    "quota_exceeded",
    "resource exhausted",
    "insufficient_quota",
    "credit balance is too low",
    "out of credits",
    "exceeded your current quota",
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
_ANTIGRAVITY_LOG_DIAGNOSTIC_KEYS = {
    "code",
    "diagnostic",
    "details",
    "error",
    "errors",
    "exception",
    "message",
    "msg",
    "reason",
    "status",
}
_ANTIGRAVITY_SECRET_KEY_PATTERN = (
    r"[A-Za-z0-9_. -]*(?:api[_ -]?key|apikey|authorization|password|secret|token)[A-Za-z0-9_. -]*"
)
_ANTIGRAVITY_AUTHORIZATION_KEY_PATTERN = r"[A-Za-z0-9_. -]*authorization[A-Za-z0-9_. -]*"


@dataclass(frozen=True)
class _AntigravityCopyPolicy:
    preserved_paths: frozenset[str]
    preserved_dirs: frozenset[str]
    ignored_paths: frozenset[str]
    ignored_dirs: frozenset[str]
    gitlink_paths: frozenset[str]


@dataclass(frozen=True)
class _AntigravityResultEnvelope:
    status: str
    response: str
    reported_tokens: dict[str, int] | None


def _antigravity_reported_tokens(usage: object) -> dict[str, int]:
    """Normalize explicit Antigravity usage into Sikula's shared token fields."""
    if not isinstance(usage, dict):
        return {}
    reported: dict[str, int] = {}
    for source_key, target_key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_tokens", "cached_input_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            reported[target_key] = value
    return reported


def _antigravity_result_payload(output: str) -> dict[str, object] | None:
    try:
        payload = json.loads(output)
    except (ValueError, RecursionError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _antigravity_result_observation(output: str) -> tuple[int | None, dict[str, int] | None]:
    payload = _antigravity_result_payload(output)
    if payload is None:
        return None, None
    response = payload.get("response")
    output_chars = len(response.strip()) if isinstance(response, str) else None
    reported_tokens = _antigravity_reported_tokens(payload.get("usage"))
    return output_chars, reported_tokens or None


def _antigravity_result_envelope(
    output: str,
    context: str,
    *,
    allow_empty_response: bool = False,
    log_diagnostic: str = "",
) -> _AntigravityResultEnvelope:
    payload = _antigravity_result_payload(output)
    if payload is None:
        raise LLMTransientError(f"antigravity {context} error: invalid JSON result envelope")

    status = payload.get("status")
    response = payload.get("response")
    text = response.strip() if isinstance(response, str) else ""
    reported_tokens = _antigravity_reported_tokens(payload.get("usage")) or None
    if not isinstance(status, str) or status != "SUCCESS":
        diagnostic = log_diagnostic.strip()
        error = (
            _provider_error("antigravity", context, f"log diagnostic:\n{diagnostic}")
            if diagnostic
            else LLMTransientError(f"antigravity {context} error: unsuccessful structured result")
        )
        error.output_chars = len(text)
        error.reported_tokens = reported_tokens
        raise error
    if not text and (not allow_empty_response or not isinstance(response, str)):
        raise LLMTransientError(
            f"antigravity {context} error: returned no text output",
            output_chars=0,
            reported_tokens=reported_tokens,
        )
    return _AntigravityResultEnvelope(
        status=status,
        response=text,
        reported_tokens=reported_tokens,
    )


def _antigravity_redact_diagnostic(text: str) -> str:
    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text)
    redacted = re.sub(
        rf"(?i)\b({_ANTIGRAVITY_AUTHORIZATION_KEY_PATTERN})(\s*[:=]\s*)([^\r\n,;\"']+)",
        r"\1\2<redacted>",
        redacted,
    )
    redacted = re.sub(
        rf"(?i)([\"'])({_ANTIGRAVITY_SECRET_KEY_PATTERN})\1(\s*:\s*)([\"'])([^\"']+)([\"'])",
        r"\1\2\1\3\4<redacted>\6",
        redacted,
    )
    redacted = re.sub(
        rf"(?i)\b({_ANTIGRAVITY_SECRET_KEY_PATTERN})(\s*[:=]\s*)([\"'])([^\"']+)([\"'])",
        r"\1\2\3<redacted>\5",
        redacted,
    )
    redacted = re.sub(
        rf"(?i)\b({_ANTIGRAVITY_SECRET_KEY_PATTERN})(\s*[:=]\s*)([^\s,;\"']+)",
        r"\1\2<redacted>",
        redacted,
    )
    if len(redacted) > _ANTIGRAVITY_LOG_DIAGNOSTIC_LINE_CHARS:
        redacted = redacted[:_ANTIGRAVITY_LOG_DIAGNOSTIC_LINE_CHARS].rstrip() + "..."
    return redacted


def _antigravity_marker_text_for_markers(text: str, markers: tuple[str, ...]) -> str | None:
    normalized = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()
    if not normalized:
        return None
    lower = normalized.lower()
    positions = [lower.find(marker) for marker in markers if marker in lower]
    if not positions:
        return None
    pos = min(positions)
    start = max(0, pos - 160)
    end = min(len(normalized), pos + 340)
    excerpt = normalized[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(normalized):
        excerpt += "..."
    return _antigravity_redact_diagnostic(excerpt)


def _antigravity_marker_text(text: str) -> str | None:
    return _antigravity_marker_text_for_markers(text, _ANTIGRAVITY_LOG_DIAGNOSTIC_MARKERS)


def _antigravity_provider_error_marker_text(text: str) -> str | None:
    return _antigravity_marker_text_for_markers(text, _ANTIGRAVITY_PROVIDER_ERROR_MARKERS)


def _antigravity_json_diagnostic_strings(value: object, *, key: str | None = None) -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            lower_key = child_key_text.lower()
            if lower_key in _ANTIGRAVITY_LOG_DIAGNOSTIC_KEYS or "error" in lower_key:
                yield from _antigravity_json_diagnostic_strings(child_value, key=child_key_text)
        return
    if isinstance(value, list):
        for item in value:
            yield from _antigravity_json_diagnostic_strings(item, key=key)
        return
    if isinstance(value, str):
        marker_text = _antigravity_marker_text(value)
        if marker_text:
            yield f"{key}: {marker_text}" if key else marker_text


def _antigravity_log_line_diagnostic(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            event = None
        if event is not None:
            for diagnostic in _antigravity_json_diagnostic_strings(event):
                return diagnostic
            return None
    return _antigravity_marker_text(stripped)


def _antigravity_log_diagnostic(log_file: Path) -> str:
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _ANTIGRAVITY_LOG_DIAGNOSTIC_BYTES))
            text = handle.read().decode(errors="replace")
    except OSError:
        return ""

    diagnostics: list[str] = []
    for line in reversed(text.splitlines()):
        diagnostic = _antigravity_log_line_diagnostic(line)
        if diagnostic and diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
        if len(diagnostics) >= _ANTIGRAVITY_LOG_DIAGNOSTIC_LINES:
            break
    diagnostics.reverse()
    return "\n".join(diagnostics)


def _antigravity_result_error(
    result: subprocess.CompletedProcess[str],
    operation: str,
    log_diagnostic: str = "",
) -> LLMProviderError:
    output_chars, reported_tokens = _antigravity_result_observation(result.stdout or "")

    def _observed(error: LLMProviderError) -> LLMProviderError:
        error.output_chars = output_chars
        error.reported_tokens = reported_tokens
        return error

    stderr = (result.stderr or "").strip()
    log_diagnostic = log_diagnostic.strip()
    if stderr:
        safe_stderr = _antigravity_provider_error_marker_text(stderr) or _antigravity_redact_diagnostic(stderr)
        if log_diagnostic:
            combined = f"{safe_stderr}\nlog diagnostic:\n{log_diagnostic}"
            combined_error = _provider_error("antigravity", operation, combined)
            stderr_error = _provider_error("antigravity", operation, safe_stderr)
            if isinstance(combined_error, LLMFatalError) and not isinstance(stderr_error, LLMFatalError):
                return _observed(combined_error)
        return _observed(_provider_error("antigravity", operation, safe_stderr))
    if log_diagnostic:
        return _observed(_provider_error("antigravity", operation, f"log diagnostic:\n{log_diagnostic}"))
    if (result.stdout or "").strip():
        return _observed(
            LLMTransientError(f"antigravity {operation} error: non-zero exit with stdout but no safe diagnostic")
        )
    return _observed(LLMTransientError(f"antigravity {operation} error: non-zero exit"))


def _antigravity_git_paths(cwd: Path, args: list[str]) -> set[str] | None:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout
    if isinstance(stdout, str):
        stdout = stdout.encode()
    return {path.decode(errors="surrogateescape") for path in stdout.split(b"\0") if path and not path.endswith(b"/")}


def _antigravity_gitlink_paths(cwd: Path) -> set[str] | None:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        capture_output=True,
        text=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout
    if isinstance(stdout, str):
        stdout = stdout.encode()
    gitlinks: set[str] = set()
    for entry in stdout.split(b"\0"):
        if not entry:
            continue
        metadata, _, path = entry.partition(b"\t")
        mode = metadata.split(maxsplit=1)[0] if metadata else b""
        if mode == b"160000" and path and not path.endswith(b"/"):
            gitlinks.add(path.decode(errors="surrogateescape"))
    return gitlinks


def _antigravity_copy_policy(cwd: Path) -> _AntigravityCopyPolicy | None:
    if not any((path / ".git").exists() for path in (cwd, *cwd.parents)):
        return None
    tracked_paths = _antigravity_git_paths(cwd, ["ls-files", "-z"])
    untracked_paths = _antigravity_git_paths(cwd, ["ls-files", "--others", "--exclude-standard", "-z"])
    ignored_paths = _antigravity_git_paths(cwd, ["ls-files", "--ignored", "--others", "--exclude-standard", "-z"])
    gitlink_paths = _antigravity_gitlink_paths(cwd)
    if tracked_paths is None or untracked_paths is None or ignored_paths is None or gitlink_paths is None:
        return None
    gitlink_paths = {
        path for path in gitlink_paths if path and not path.startswith("../") and not Path(path).is_absolute()
    }
    tracked_paths = tracked_paths - gitlink_paths

    generated_source_paths = {
        path
        for path in ignored_paths
        if path
        and not path.startswith("../")
        and not Path(path).is_absolute()
        and _antigravity_is_presync_generated_source(path)
    }
    preserved_paths = frozenset(
        path
        for path in tracked_paths | untracked_paths | generated_source_paths
        if path and not path.startswith("../") and not Path(path).is_absolute()
    )
    ignored_paths = frozenset(
        path for path in ignored_paths if path and not path.startswith("../") and not Path(path).is_absolute()
    )
    preserved_dirs: set[str] = set()
    for path_text in preserved_paths:
        path = Path(path_text)
        for parent in path.parents:
            if parent == Path("."):
                break
            preserved_dirs.add(parent.as_posix())
    ignored_dirs: set[str] = set()
    for path_text in ignored_paths:
        path = Path(path_text)
        for parent in path.parents:
            if parent == Path("."):
                break
            ignored_dirs.add(parent.as_posix())
    return _AntigravityCopyPolicy(
        preserved_paths=preserved_paths,
        preserved_dirs=frozenset(preserved_dirs),
        ignored_paths=ignored_paths,
        ignored_dirs=frozenset(ignored_dirs),
        gitlink_paths=frozenset(gitlink_paths),
    )


def _antigravity_is_presync_generated_source(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    if not normalized.endswith(_ANTIGRAVITY_GENERATED_SOURCE_SUFFIXES):
        return False
    parts = normalized.split("/")
    for root in ("build", "target"):
        try:
            index = parts.index(root)
        except ValueError:
            continue
        if any("generated" in part for part in parts[index + 1 :]):
            return True
    return False


def _antigravity_is_hard_ignored_path(rel_path: Path) -> bool:
    rel = rel_path.as_posix()
    return rel in _ANTIGRAVITY_IGNORED_COPY_PATHS or rel_path.name in _ANTIGRAVITY_HARD_IGNORED_COPY_DIRS


def _antigravity_is_soft_ignored_path(rel_path: Path) -> bool:
    return rel_path.name in _ANTIGRAVITY_SOFT_IGNORED_COPY_DIRS


def _antigravity_preserves_path(rel_path: Path, policy: _AntigravityCopyPolicy | None) -> bool:
    if policy is None:
        return False
    rel = rel_path.as_posix()
    return rel in policy.preserved_paths or rel in policy.preserved_dirs


def _antigravity_ignores_path(rel_path: Path, policy: _AntigravityCopyPolicy | None) -> bool:
    if policy is None:
        return False
    rel = rel_path.as_posix()
    if rel in policy.gitlink_paths:
        return True
    if rel in policy.preserved_paths:
        return False
    if rel in policy.ignored_paths:
        return True
    if rel in policy.ignored_dirs and rel not in policy.preserved_dirs:
        return True
    return False


def _antigravity_ignore_path(rel_path: Path, policy: _AntigravityCopyPolicy | None = None) -> bool:
    if _antigravity_is_hard_ignored_path(rel_path):
        return True
    if _antigravity_ignores_path(rel_path, policy):
        return True
    if _antigravity_is_soft_ignored_path(rel_path) and not _antigravity_preserves_path(rel_path, policy):
        return True
    return False


def _antigravity_snapshot_ignore_path(rel_path: Path, policy: _AntigravityCopyPolicy | None = None) -> bool:
    if _antigravity_is_hard_ignored_path(rel_path):
        return True
    if _antigravity_preserved_ignored_ancestor(rel_path, policy):
        return False
    if _antigravity_is_soft_ignored_path(rel_path) and not _antigravity_preserves_path(rel_path, policy):
        return True
    return False


def _antigravity_copy_ignore(
    root: Path,
    policy: _AntigravityCopyPolicy | None,
) -> Callable[[str, list[str]], set[str]]:
    def _ignore(src: str, names: list[str]) -> set[str]:
        try:
            rel_dir = Path(src).resolve().relative_to(root.resolve())
        except ValueError:
            rel_dir = Path()
        ignored: set[str] = set()
        for name in names:
            rel_path = rel_dir / name
            path = Path(src) / name
            if _antigravity_is_hard_ignored_path(rel_path):
                ignored.add(name)
                continue
            if _antigravity_ignores_path(rel_path, policy):
                ignored.add(name)
                continue
            if _antigravity_is_soft_ignored_path(rel_path):
                if policy is None or not _antigravity_preserves_path(rel_path, policy):
                    ignored.add(name)
                continue
            if policy is not None and rel_path.as_posix() in policy.preserved_dirs:
                continue
            if policy is not None and _antigravity_preserved_ignored_ancestor(rel_path, policy):
                if path.is_dir():
                    if rel_path.as_posix() not in policy.preserved_dirs:
                        ignored.add(name)
                elif rel_path.as_posix() not in policy.preserved_paths:
                    ignored.add(name)
        return ignored

    return _ignore


def _antigravity_preserved_ignored_ancestor(
    rel_path: Path,
    policy: _AntigravityCopyPolicy | None,
) -> str | None:
    if policy is None:
        return None
    parts = rel_path.parts
    for index, part in enumerate(parts[:-1]):
        if part not in _ANTIGRAVITY_SOFT_IGNORED_COPY_DIRS:
            continue
        prefix = Path(*parts[: index + 1]).as_posix()
        if prefix in policy.preserved_dirs:
            return prefix
    return None


def _antigravity_validate_workspace_symlinks(
    root: Path,
    *,
    prune_ignored_paths: bool,
    policy: _AntigravityCopyPolicy | None = None,
) -> None:
    resolved_root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        try:
            rel_dir = directory.resolve().relative_to(resolved_root)
        except ValueError:
            rel_dir = Path()

        kept_dirs: list[str] = []
        for name in dirnames:
            path = directory / name
            rel_path = rel_dir / name
            if prune_ignored_paths and _antigravity_ignore_path(rel_path, policy):
                continue
            if path.is_symlink():
                _antigravity_validate_workspace_symlink(path, resolved_root, rel_path)
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            path = directory / name
            rel_path = rel_dir / name
            if prune_ignored_paths and _antigravity_ignore_path(rel_path, policy):
                continue
            if path.is_symlink():
                _antigravity_validate_workspace_symlink(path, resolved_root, rel_path)


def _antigravity_validate_workspace_symlink(path: Path, resolved_root: Path, rel_path: Path) -> None:
    try:
        target_text = os.readlink(path)
    except OSError as exc:
        raise LLMConfigurationError(f"antigravity workspace cannot inspect symlink {rel_path.as_posix()}") from exc

    if Path(target_text).is_absolute():
        raise LLMConfigurationError(f"antigravity workspace rejects absolute symlink {rel_path.as_posix()}")

    target_path = (path.parent / target_text).resolve(strict=False)
    try:
        target_path.relative_to(resolved_root)
    except ValueError as exc:
        raise LLMConfigurationError(f"antigravity workspace rejects external symlink {rel_path.as_posix()}") from exc


def _antigravity_copy_workspace(cwd: Path, destination: Path) -> _AntigravityCopyPolicy | None:
    policy = _antigravity_copy_policy(cwd)
    _antigravity_validate_workspace_symlinks(cwd, prune_ignored_paths=True, policy=policy)
    shutil.copytree(
        cwd,
        destination,
        symlinks=True,
        ignore=_antigravity_copy_ignore(cwd, policy),
        ignore_dangling_symlinks=True,
    )
    _antigravity_validate_workspace_symlinks(destination, prune_ignored_paths=True, policy=policy)
    return policy


def _antigravity_directory_snapshot(
    root: Path,
    policy: _AntigravityCopyPolicy | None = None,
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        rel_dir = directory.relative_to(root)
        kept_dirs: list[str] = []
        for name in dirnames:
            path = directory / name
            rel_path = rel_dir / name
            if _antigravity_snapshot_ignore_path(rel_path, policy):
                snapshot[rel_path.as_posix()] = "ignored-dir"
                continue
            if path.is_symlink():
                try:
                    snapshot[rel_path.as_posix()] = f"symlink-dir:{os.readlink(path)}"
                except OSError:
                    snapshot[rel_path.as_posix()] = "symlink-dir:<unreadable>"
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            path = directory / name
            rel_path = rel_dir / name
            if _antigravity_snapshot_ignore_path(rel_path, policy):
                continue
            key = rel_path.as_posix()
            if path.is_symlink():
                try:
                    snapshot[key] = f"symlink:{os.readlink(path)}"
                except OSError:
                    snapshot[key] = "symlink:<unreadable>"
                continue
            try:
                snapshot[key] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                snapshot[key] = "<unavailable>"
    return snapshot


def _antigravity_snapshot_changed(before: dict[str, str], after: dict[str, str]) -> bool:
    return any(before.get(path) != after.get(path) for path in before.keys() | after.keys())


def _antigravity_sanitize_readonly_output(output: str, *workspaces: Path) -> str:
    roots: list[str] = []
    for workspace in workspaces:
        for root_path in (workspace, workspace.resolve()):
            for root in (str(root_path).rstrip("/\\"), root_path.as_posix().rstrip("/")):
                if root and root not in roots:
                    roots.append(root)
    uri_roots = list(roots)
    for root in roots:
        encoded_root = quote(root, safe="/:\\")
        if encoded_root not in uri_roots:
            uri_roots.append(encoded_root)

    sanitized = output

    def _replacement(match: re.Match[str]) -> str:
        return unquote(match.group("suffix").lstrip("/\\")).replace("\\", "/")

    suffix_pattern = r"(?P<suffix>[/\\][^\s<>\]\"'`)]+)"
    flags = re.IGNORECASE if os.name == "nt" else 0
    for root in sorted(uri_roots, key=len, reverse=True):
        escaped_root = re.escape(root)
        sanitized = re.sub(rf"file:///?{escaped_root}{suffix_pattern}", _replacement, sanitized, flags=flags)
    for root in sorted(roots, key=len, reverse=True):
        escaped_root = re.escape(root)
        sanitized = re.sub(
            rf"(?<![\w:/\\.-]){escaped_root}{suffix_pattern}",
            _replacement,
            sanitized,
            flags=flags,
        )
    return sanitized


def _antigravity_write_agent_prompt(prompt: str, cwd: Path) -> str:
    if prompt.startswith("ANTIGRAVITY WORKSPACE BOUNDARY:\n"):
        return prompt
    workspace = cwd.resolve().as_posix()
    return (
        "ANTIGRAVITY WORKSPACE BOUNDARY:\n"
        f"- The only project root for this task is: {workspace}\n"
        "- Use that directory for all reads, searches, writes, and git commands.\n"
        "- If a tool starts in a scratch directory, switch to that project root before doing any work.\n"
        "- Do not search for, inspect, or modify any other checkout or repository path, even if it looks similar.\n\n"
        f"{prompt}"
    )


@contextmanager
def _antigravity_prompt_transport(workspace: Path, prompt: str) -> Iterator[tuple[Path, str]]:
    transport: tempfile.TemporaryDirectory[str] | None = None
    try:
        transport = tempfile.TemporaryDirectory(prefix="sikula-antigravity-prompt-")
        prompt_dir = Path(transport.name).resolve()
        resolved_workspace = workspace.resolve()
    except OSError as exc:
        if transport is not None:
            try:
                transport.cleanup()
            except OSError:
                pass
        raise LLMEnvironmentError("antigravity prompt transport could not be created") from exc

    if prompt_dir == resolved_workspace or resolved_workspace in prompt_dir.parents:
        try:
            transport.cleanup()
        except OSError as exc:
            raise LLMEnvironmentError("antigravity prompt transport could not be removed") from exc
        raise LLMEnvironmentError("antigravity prompt transport must be outside the project workspace")

    prompt_path = prompt_dir / "request.md"
    try:
        prompt_path.write_bytes(prompt.encode("utf-8", errors="replace"))
    except OSError as exc:
        try:
            transport.cleanup()
        except OSError:
            pass
        raise LLMEnvironmentError("antigravity prompt transport could not be created") from exc

    request = (
        f"Read the complete Sikula request from the attached workspace path `{prompt_dir.name}/request.md` "
        "and follow it. "
        "Treat that file as task input, not project content. Do not modify, quote, or mention the transport file."
    )
    try:
        yield prompt_dir, request
    finally:
        try:
            transport.cleanup()
        except OSError as exc:
            raise LLMEnvironmentError("antigravity prompt transport could not be removed") from exc


@contextmanager
def _antigravity_log_file() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="sikula-antigravity-log-") as tmp:
        yield Path(tmp) / "agy.log"


def _antigravity_parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _antigravity_require_supported_version() -> None:
    try:
        result = _run_provider_cli(
            ["agy", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise LLMConfigurationError("antigravity CLI not found: install `agy` and authenticate it") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMConfigurationError("antigravity CLI version check timed out") from exc

    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if result.returncode != 0:
        diagnostic = _antigravity_redact_diagnostic(output) or f"exit code {result.returncode}"
        raise LLMConfigurationError(f"antigravity CLI version check failed: {diagnostic}")

    version = _antigravity_parse_version(output)
    if version is None:
        raise LLMConfigurationError("antigravity CLI version check failed: could not parse `agy --version` output")
    if version < _ANTIGRAVITY_MIN_VERSION:
        minimum = ".".join(str(part) for part in _ANTIGRAVITY_MIN_VERSION)
        current = ".".join(str(part) for part in version)
        raise LLMConfigurationError(f"antigravity CLI {current} is unsupported; install agy {minimum} or newer")


def _antigravity_require_no_active_hooks(workspace: Path, timeout: int) -> None:
    """Fail closed when Antigravity could execute provider-configured commands."""
    preflight_timeout = max(1, min(timeout, _ANTIGRAVITY_HOOK_PREFLIGHT_TIMEOUT))
    try:
        with _antigravity_log_file() as log_file:
            result = _run_provider_cli(
                [
                    "agy",
                    "--new-project",
                    "--add-dir",
                    str(workspace),
                    "--log-file",
                    str(log_file),
                    "--print-timeout",
                    f"{preflight_timeout}s",
                    "--output-format",
                    "json",
                    "--print",
                    "/hooks",
                ],
                capture_output=True,
                text=True,
                cwd=workspace,
                timeout=preflight_timeout,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise LLMConfigurationError(
            "antigravity read-only hook preflight failed; verify the provider installation and configuration"
        ) from exc

    payload = _antigravity_result_payload(result.stdout or "")
    command = payload.get("command") if payload is not None else None
    data = command.get("data") if isinstance(command, dict) else None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    valid = (
        result.returncode == 0
        and payload is not None
        and payload.get("status") == "SUCCESS"
        and isinstance(hooks, list)
        and all(isinstance(hook, dict) and isinstance(hook.get("enabled"), bool) for hook in hooks)
    )
    if not valid:
        raise LLMConfigurationError(
            "antigravity read-only hook preflight returned an unverifiable result; "
            "verify the provider installation and configuration"
        )
    if any(hook["enabled"] for hook in hooks):
        raise LLMConfigurationError(
            "antigravity read-only agents cannot run while Antigravity hooks are enabled; disable the hooks and retry"
        )


def _antigravity_write_readonly_agent(workspace: Path) -> str:
    agents_dir = workspace / ".agents" / "agents"
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        agent_dir = Path(tempfile.mkdtemp(prefix="sikula-readonly-", dir=agents_dir))
        agent_name = agent_dir.name
        tool_lines = "\n".join(f"  - {tool}" for tool in _ANTIGRAVITY_READONLY_TOOLS)
        definition = (
            "---\n"
            f"name: {agent_name}\n"
            "description: Sikula read-only repository inspection agent.\n"
            "tools:\n"
            f"{tool_lines}\n"
            "mainAgent: true\n"
            "subagent: false\n"
            "inheritMcp: false\n"
            "commandExecutionPolicy: off\n"
            "mcpServers: []\n"
            "skills: []\n"
            "plugins: []\n"
            "---\n\n"
            "# Read-Only Repository Inspection\n\n"
            "Inspect only the attached workspace with the listed read-only tools. "
            "Never create, modify, delete, move, rename, or format files. "
            "Do not invoke subagents, skills, plugins, MCP servers, shell commands, or network tools.\n"
        )
        (agent_dir / "agent.md").write_bytes(definition.encode("ascii"))
    except OSError as exc:
        raise LLMEnvironmentError("antigravity read-only agent configuration could not be created") from exc
    return agent_name


class AntigravityClient(LLMClient):
    """Calls Antigravity via the structured `agy --output-format json --print` CLI.

    Prompts use a temporary task file because Antigravity print mode requires its prompt
    as an argument. Read-only calls combine disabled slash expansion with a generated
    tool-limited agent, an active-hook preflight, and a disposable workspace snapshot.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._version_checked = False

    def _ensure_supported_version(self) -> None:
        if self._version_checked:
            return
        _antigravity_require_supported_version()
        self._version_checked = True

    def prepare_agent_prompt(self, prompt: str, cwd: Path) -> str:
        return _antigravity_write_agent_prompt(prompt, cwd)

    def _cmd(
        self,
        *,
        cwd: Path,
        timeout: int,
        log_file: Path,
        auto_approve_tools: bool,
        prompt_workspace: Path,
        print_prompt: str,
        agent: str | None = None,
        disable_slash_commands: bool = False,
    ) -> list[str]:
        cmd = ["agy", "--new-project"]
        cmd.extend(["--add-dir", str(cwd)])
        cmd.extend(["--add-dir", str(prompt_workspace)])
        if agent is not None:
            cmd.extend(["--agent", agent])
        if disable_slash_commands:
            cmd.append("--disable-slash-commands")
        if auto_approve_tools:
            cmd.append("--dangerously-skip-permissions")
        cmd.extend(
            [
                "--sandbox",
                "--model",
                self._config.model,
                "--log-file",
                str(log_file),
                "--print-timeout",
                f"{timeout}s",
                "--output-format",
                "json",
                "--print",
                print_prompt,
            ]
        )
        return cmd

    def generate(self, system: str, user: str) -> str:
        self._ensure_supported_version()
        prompt = f"{system}\n\n{user}"
        log.info(
            "Calling LLM via Antigravity (%s, ~%d tokens) — waiting for response...",
            self._config.model,
            len(prompt) // 4,
        )

        with tempfile.TemporaryDirectory(prefix="sikula-antigravity-generate-") as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            _antigravity_require_no_active_hooks(workspace, self._config.agent_timeout)
            agent_name = _antigravity_write_readonly_agent(workspace)

            def _call():
                with _antigravity_prompt_transport(workspace, prompt) as (prompt_workspace, print_prompt):
                    with _antigravity_log_file() as log_file:
                        result = _run_provider_cli(
                            self._cmd(
                                cwd=workspace,
                                timeout=self._config.agent_timeout,
                                log_file=log_file,
                                auto_approve_tools=False,
                                prompt_workspace=prompt_workspace,
                                agent=agent_name,
                                disable_slash_commands=True,
                                print_prompt=print_prompt,
                            ),
                            capture_output=True,
                            text=True,
                            cwd=workspace,
                            timeout=self._config.agent_timeout,
                        )
                        log_diagnostic = _antigravity_log_diagnostic(log_file)
                if result.returncode != 0:
                    raise _antigravity_result_error(result, "CLI", log_diagnostic)
                envelope = _antigravity_result_envelope(
                    result.stdout,
                    "CLI",
                    log_diagnostic=log_diagnostic,
                )
                output = _antigravity_sanitize_readonly_output(
                    envelope.response,
                    workspace,
                    prompt_workspace,
                )
                return _LLMCallValue(
                    output,
                    output_chars=len(output),
                    reported_tokens=envelope.reported_tokens,
                )

            return _call_with_retry(
                "generate",
                _call,
                self._config,
                "generate",
                input_chars=len(prompt),
                before_attempt=lambda: _antigravity_require_no_active_hooks(
                    workspace,
                    self._config.agent_timeout,
                ),
            )

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self._ensure_supported_version()
        log.info("Running Antigravity read-only agent (%s) — waiting for completion...", self._config.model)

        with tempfile.TemporaryDirectory(prefix="sikula-antigravity-readonly-") as tmp:
            workspace = Path(tmp) / "workspace"
            copy_policy = _antigravity_copy_workspace(cwd, workspace)
            _antigravity_require_no_active_hooks(workspace, self._config.agent_timeout)
            agent_name = _antigravity_write_readonly_agent(workspace)
            before = _antigravity_directory_snapshot(workspace, copy_policy)

            def _call():
                result: subprocess.CompletedProcess[str] | None = None
                try:
                    with _antigravity_prompt_transport(workspace, prompt) as (prompt_workspace, print_prompt):
                        with _antigravity_log_file() as log_file:
                            result = _run_provider_cli(
                                self._cmd(
                                    cwd=workspace,
                                    timeout=self._config.agent_timeout,
                                    log_file=log_file,
                                    auto_approve_tools=False,
                                    prompt_workspace=prompt_workspace,
                                    print_prompt=print_prompt,
                                    agent=agent_name,
                                    disable_slash_commands=True,
                                ),
                                capture_output=True,
                                text=True,
                                cwd=workspace,
                                timeout=self._config.agent_timeout,
                            )
                            log_diagnostic = _antigravity_log_diagnostic(log_file)
                except Exception as exc:
                    after = _antigravity_directory_snapshot(workspace, copy_policy)
                    if _antigravity_snapshot_changed(before, after):
                        raise LLMReadOnlyViolation(
                            "antigravity read-only boundary violation: disposable workspace changed"
                        ) from exc
                    raise

                after = _antigravity_directory_snapshot(workspace, copy_policy)
                if _antigravity_snapshot_changed(before, after):
                    output_chars, reported_tokens = _antigravity_result_observation(result.stdout or "")
                    raise LLMReadOnlyViolation(
                        "antigravity read-only boundary violation: disposable workspace changed",
                        output_chars=output_chars,
                        reported_tokens=reported_tokens,
                    )
                if result.returncode != 0:
                    raise _antigravity_result_error(result, "agent", log_diagnostic)
                envelope = _antigravity_result_envelope(
                    result.stdout,
                    "agent",
                    log_diagnostic=log_diagnostic,
                )
                output = _antigravity_sanitize_readonly_output(
                    envelope.response,
                    workspace,
                    prompt_workspace,
                )
                return _LLMCallValue(
                    output,
                    output_chars=len(output),
                    reported_tokens=envelope.reported_tokens,
                )

            return _call_with_retry(
                "read-only agent",
                _call,
                self._config,
                "run_readonly_agent",
                input_chars=len(prompt),
                before_attempt=lambda: _antigravity_require_no_active_hooks(
                    workspace,
                    self._config.agent_timeout,
                ),
            )

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self._ensure_supported_version()
        workspace = cwd.resolve()
        policy = _antigravity_copy_policy(workspace)
        if policy is not None and policy.gitlink_paths:
            paths = ", ".join(sorted(policy.gitlink_paths))
            raise LLMConfigurationError(f"antigravity write agent does not support git submodules: {paths}")
        _antigravity_validate_workspace_symlinks(workspace, prune_ignored_paths=True, policy=policy)
        prompt = self.prepare_agent_prompt(prompt, workspace)
        before = _git_snapshot(workspace)
        log.info("Running Antigravity agent (%s) — waiting for completion...", self._config.model)

        def _call():
            with _antigravity_prompt_transport(workspace, prompt) as (prompt_workspace, print_prompt):
                with _antigravity_log_file() as log_file:
                    result = _run_agent_subprocess_streaming(
                        self._cmd(
                            cwd=workspace,
                            timeout=self._config.agent_timeout,
                            log_file=log_file,
                            auto_approve_tools=True,
                            prompt_workspace=prompt_workspace,
                            print_prompt=print_prompt,
                        ),
                        cwd=workspace,
                        env=None,
                        timeout=self._config.agent_timeout,
                        provider="antigravity",
                    )
                    log_diagnostic = _antigravity_log_diagnostic(log_file)
            if result.returncode != 0:
                raise _antigravity_result_error(result, "agent", log_diagnostic)
            after = _git_snapshot(workspace)
            changed = sorted(p for p in (before.keys() | after.keys()) if before.get(p) != after.get(p))
            envelope = _antigravity_result_envelope(
                result.stdout,
                "agent",
                allow_empty_response=True,
                log_diagnostic=log_diagnostic,
            )
            output = _antigravity_sanitize_readonly_output(
                envelope.response,
                prompt_workspace,
            )
            return _LLMCallValue(
                (changed, output),
                output_chars=len(output),
                reported_tokens=envelope.reported_tokens,
            )

        total = len(_RETRY_DELAYS) + 1
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            _antigravity_require_no_active_hooks(workspace, self._config.agent_timeout)
            try:
                return _call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt + 1,
                    max_attempts=total,
                    input_chars=len(prompt),
                    fn=_call,
                )
            except subprocess.TimeoutExpired as exc:
                last_exc = LLMTimeoutError(f"antigravity agent timed out after {exc.timeout}s")
                if delay is None:
                    break
                if _git_snapshot(workspace) != before:
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
                if _git_snapshot(workspace) != before:
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
    if config.provider == "antigravity":
        return AntigravityClient(config)
    raise ValueError(f"Unknown LLM provider: {config.provider!r}. Add it to llm_client.py.")
