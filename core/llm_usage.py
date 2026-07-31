"""Provider-neutral LLM invocation usage records and aggregates."""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+() -]*$")
_MAX_LABEL_CHARS = 200
_OUTCOMES = {"success", "retryable_error", "fatal_error", "timeout", "error"}
_REPORTED_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "total_tokens",
)


def _safe_label(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if (
        len(text) <= _MAX_LABEL_CHARS
        and text == text.strip()
        and _LABEL_RE.fullmatch(text)
        and "://" not in text
        and not PurePosixPath(text).is_absolute()
        and not PureWindowsPath(text).is_absolute()
    ):
        return text
    return "unknown"


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _non_negative_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        if parsed >= 0 and math.isfinite(parsed):
            return round(parsed, 3)
    return None


def sanitize_llm_usage_record(event: dict[str, object], *, agent: str) -> dict[str, Any] | None:
    """Return one bounded content-free invocation record."""
    attempt = _non_negative_int(event.get("attempt"))
    max_attempts = _non_negative_int(event.get("max_attempts"))
    elapsed_s = _non_negative_float(event.get("elapsed_s"))
    outcome = event.get("outcome")
    if (
        attempt is None
        or attempt < 1
        or max_attempts is None
        or max_attempts < attempt
        or elapsed_s is None
        or outcome not in _OUTCOMES
    ):
        return None

    record: dict[str, Any] = {
        "agent": _safe_label(agent),
        "provider": _safe_label(event.get("provider")),
        "model": _safe_label(event.get("model")),
        "operation": _safe_label(event.get("operation")),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "outcome": outcome,
        "elapsed_s": elapsed_s,
    }
    for key in ("input_chars", "output_chars"):
        value = _non_negative_int(event.get(key))
        if value is not None:
            record[key] = value

    error_type = event.get("error_type")
    if error_type:
        record["error_type"] = _safe_label(error_type)

    raw_tokens = event.get("reported_tokens")
    if isinstance(raw_tokens, dict):
        reported_tokens = {}
        for key in _REPORTED_TOKEN_KEYS:
            value = _non_negative_int(raw_tokens.get(key))
            if value is not None:
                reported_tokens[key] = value
        if reported_tokens:
            record["reported_tokens"] = reported_tokens
    return record


def empty_llm_usage_summary() -> dict[str, Any]:
    return {
        "attempts": 0,
        "successful_attempts": 0,
        "failed_attempts": 0,
        "timeout_attempts": 0,
        "elapsed_s": 0.0,
        "input_chars": 0,
        "output_chars": 0,
        "output_chars_known_attempts": 0,
        "reported_token_attempts": 0,
        "reported_tokens": {},
    }


def aggregate_llm_usage(records: object) -> dict[str, Any]:
    """Aggregate trusted records into a numeric public projection."""
    summary = empty_llm_usage_summary()
    if not isinstance(records, list):
        return summary
    for record in records:
        if not isinstance(record, dict) or record.get("outcome") not in _OUTCOMES:
            continue
        summary["attempts"] += 1
        if record["outcome"] == "success":
            summary["successful_attempts"] += 1
        else:
            summary["failed_attempts"] += 1
        if record["outcome"] == "timeout":
            summary["timeout_attempts"] += 1
        elapsed_s = _non_negative_float(record.get("elapsed_s"))
        if elapsed_s is not None:
            summary["elapsed_s"] += elapsed_s
        input_chars = _non_negative_int(record.get("input_chars"))
        if input_chars is not None:
            summary["input_chars"] += input_chars
        output_chars = _non_negative_int(record.get("output_chars"))
        if output_chars is not None:
            summary["output_chars"] += output_chars
            summary["output_chars_known_attempts"] += 1
        reported_tokens = record.get("reported_tokens")
        if isinstance(reported_tokens, dict):
            included = False
            for key in _REPORTED_TOKEN_KEYS:
                value = _non_negative_int(reported_tokens.get(key))
                if value is None:
                    continue
                summary["reported_tokens"][key] = summary["reported_tokens"].get(key, 0) + value
                included = True
            if included:
                summary["reported_token_attempts"] += 1
    summary["elapsed_s"] = round(summary["elapsed_s"], 3)
    return summary


def aggregate_llm_usage_by_agent(records: object) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict]] = {}
    if not isinstance(records, list):
        return {}
    for record in records:
        if not isinstance(record, dict):
            continue
        agent = _safe_label(record.get("agent"))
        grouped.setdefault(agent, []).append(record)
    return {agent: aggregate_llm_usage(grouped[agent]) for agent in sorted(grouped)}


def merge_llm_usage_summaries(summaries: list[dict]) -> dict[str, Any]:
    """Merge numeric task/unit summaries without reconstructing raw records."""
    merged = empty_llm_usage_summary()
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        for key in (
            "attempts",
            "successful_attempts",
            "failed_attempts",
            "timeout_attempts",
            "input_chars",
            "output_chars",
            "output_chars_known_attempts",
            "reported_token_attempts",
        ):
            value = _non_negative_int(summary.get(key))
            if value is not None:
                merged[key] += value
        elapsed_s = _non_negative_float(summary.get("elapsed_s"))
        if elapsed_s is not None:
            merged["elapsed_s"] += elapsed_s
        reported_tokens = summary.get("reported_tokens")
        if isinstance(reported_tokens, dict):
            for key in _REPORTED_TOKEN_KEYS:
                value = _non_negative_int(reported_tokens.get(key))
                if value is not None:
                    merged["reported_tokens"][key] = merged["reported_tokens"].get(key, 0) + value
    merged["elapsed_s"] = round(merged["elapsed_s"], 3)
    return merged
