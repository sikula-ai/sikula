"""Helpers for preserving useful diagnostics from long command output."""

from __future__ import annotations

import re

DEFAULT_DIAGNOSTIC_LIMIT = 4000

_TRUNCATED = "\n... [truncated] ...\n"
_TRUNCATED_BEFORE_DIAGNOSTICS = "\n... [truncated before diagnostics] ...\n"
_TRUNCATED_AFTER_DIAGNOSTICS = "\n... [truncated after diagnostics] ...\n"
_DIAGNOSTIC_OMITTED = "\n... [diagnostic output omitted] ...\n"

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\(B")
_DIAGNOSTIC_RE = re.compile(
    r"\b(error|errors|failed|failure|failures|panic|panicked|exception|assertion|traceback|expected|actual)\b"
    r"|fail:"
    r"|\*\* (build|test) failed \*\*"
    r"|test case '",
    re.IGNORECASE,
)
_CARGO_TEST_RERUN_RE = re.compile(r"^error: test failed, to rerun pass `.*`", re.IGNORECASE)


def diagnostic_excerpt(text: str | None, limit: int = DEFAULT_DIAGNOSTIC_LIMIT, context_lines: int = 8) -> str:
    """Return a compact excerpt that keeps failure context instead of only the tail.

    Build tools often emit the actual failure in the middle of a long output stream, followed
    by many unrelated "Running ..." or summary lines. A plain tail loses the assertion or
    compiler diagnostic that the fixer needs. This helper keeps blocks around likely failure
    markers and then uses remaining budget for head/tail context.
    """
    if not text or limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    diagnostic = _diagnostic_blocks(text, context_lines=context_lines)
    if diagnostic:
        return _compose_diagnostic_excerpt(text, diagnostic, limit)

    return _head_tail(text, limit)


def cargo_test_failure_excerpt(text: str | None, limit: int = DEFAULT_DIAGNOSTIC_LIMIT) -> str:
    """Return a Cargo-test-specific failure excerpt, falling back to generic extraction.

    Cargo workspace test output can contain many harmless summaries such as
    "0 failed" before or after the actual failure block. The generic extractor treats
    those as diagnostic markers, so select Cargo's structured failure section first.
    """
    if not text or limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    lines = text.splitlines(keepends=True)
    first_failure = _first_cargo_failure_block(lines)
    if first_failure is None:
        return diagnostic_excerpt(text, limit=limit)

    end = _cargo_failure_block_end(lines, first_failure)
    excerpt = "".join(lines[first_failure:end])
    return _head_tail(excerpt, limit) if len(excerpt) > limit else excerpt


def _first_cargo_failure_block(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if _ANSI_RE.sub("", line).strip() == "failures:":
            return index
    return None


def _cargo_failure_block_end(lines: list[str], start: int) -> int:
    end = len(lines)
    for index in range(start + 1, len(lines)):
        normalized = _ANSI_RE.sub("", lines[index]).strip()
        if _CARGO_TEST_RERUN_RE.match(normalized):
            return index + 1
        if normalized.startswith("error: test failed"):
            end = index + 1
    return end


def _diagnostic_blocks(text: str, context_lines: int) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return ""

    marker_lines = [idx for idx, line in enumerate(lines) if _is_diagnostic_line(line)]
    if not marker_lines:
        return ""

    ranges: list[tuple[int, int]] = []
    for idx in marker_lines:
        start = max(0, idx - context_lines)
        end = min(len(lines), idx + context_lines + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    if len(ranges) > 4:
        ranges = ranges[:2] + ranges[-2:]

    parts: list[str] = []
    for start, end in ranges:
        if parts:
            parts.append(_DIAGNOSTIC_OMITTED)
        parts.append("".join(lines[start:end]))
    return "".join(parts)


def _is_diagnostic_line(line: str) -> bool:
    normalized = _ANSI_RE.sub("", line)
    return bool(_DIAGNOSTIC_RE.search(normalized))


def _compose_diagnostic_excerpt(text: str, diagnostic: str, limit: int) -> str:
    head_budget = min(600, max(0, limit // 6))
    tail_budget = min(1000, max(0, limit // 4))
    overhead = len(_TRUNCATED_BEFORE_DIAGNOSTICS) + len(_TRUNCATED_AFTER_DIAGNOSTICS)
    diagnostic_budget = limit - head_budget - tail_budget - overhead

    minimum_diagnostic_budget = min(1200, max(0, limit // 2))
    if diagnostic_budget < minimum_diagnostic_budget:
        head_budget = 0
        overhead = len(_TRUNCATED_AFTER_DIAGNOSTICS)
        diagnostic_budget = limit - tail_budget - overhead
    if diagnostic_budget < minimum_diagnostic_budget:
        tail_budget = 0
        overhead = 0
        diagnostic_budget = limit

    if diagnostic_budget <= 0:
        return _head_tail(diagnostic, limit)

    diagnostic_part = _head_tail(diagnostic, diagnostic_budget) if len(diagnostic) > diagnostic_budget else diagnostic

    parts: list[str] = []
    if head_budget:
        parts.extend([text[:head_budget], _TRUNCATED_BEFORE_DIAGNOSTICS])
    parts.append(diagnostic_part)
    if tail_budget:
        parts.extend([_TRUNCATED_AFTER_DIAGNOSTICS, text[-tail_budget:]])

    result = "".join(parts)
    if len(result) <= limit:
        return result
    return _head_tail(diagnostic, limit)


def _head_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return text[-limit:]

    available = limit - len(_TRUNCATED)
    head_len = available // 2
    tail_len = available - head_len
    return text[:head_len] + _TRUNCATED + text[-tail_len:]
