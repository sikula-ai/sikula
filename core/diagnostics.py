"""Helpers for preserving useful diagnostics from long command output."""

from __future__ import annotations

import re

DEFAULT_DIAGNOSTIC_LIMIT = 4000
DEFAULT_DIAGNOSTIC_SUMMARY_LINES = 8
DEFAULT_DIAGNOSTIC_SUMMARY_LINE_LIMIT = 260

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
_PRIMARY_DIAGNOSTIC_RE = re.compile(
    r"(^|\s)(e|error(?:\[[^\]]+\])?|fatal error|syntaxerror|typeerror|referenceerror|assertionerror|runtimeexception):\s"
    r"|\b(traceback|panic|panicked|assertionerror|runtimeexception|exception)\b"
    r"|\b(unresolved reference|cannot find|not found|not assignable|undefined|missing|expected|actual)\b"
    r"|(^|\s)failed\s+[\w./\\:-]+"
    r"|[A-Za-z_][\w.$]*(Test|Tests|Spec)\s*>\s*.+\s+FAILED\b"
    r"|[\w./\\-]+\(\d+,\d+\):\s+error\b"
    r"|(?:^|\s)(?:file://)?(?:[/\\][^\s:]+)+:\d+(?::\d+)?:",
    re.IGNORECASE,
)
_NOISY_DIAGNOSTIC_RE = re.compile(
    r"^\s*> Task .+ FAILED\s*$"
    r"|^\s*BUILD FAILED\b"
    r"|^\s*FAILURE: Build failed\b"
    r"|^\s*\* What went wrong:"
    r"|^\s*Execution failed for task ",
    re.IGNORECASE,
)
_CARGO_TEST_RERUN_RE = re.compile(r"^error: test failed, to rerun pass `.*`", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"(?P<prefix>file://)?(?P<path>(?:[/\\][^\s:]+)+)(?P<location>:\d+(?::\d+)?)?")
_ASSERTION_VALUES_RE = re.compile(r"(\b[\w.]*Assertion(?:Failed)?Error:\s+).+", re.IGNORECASE)
_ASSERTION_COMPARISON_VALUES_RE = re.compile(
    r"^([+-]?\s*(?:expected|received|actual|left|right)\b[^:]{0,60}:\s+).+",
    re.IGNORECASE,
)
_STACK_FRAME_DETAIL_RE = re.compile(
    r"^(?:"
    r"at\s+[\w.$/\\<>-]+(?:\([^)]*:\d+(?::\d+)?\)|:\d+(?::\d+)?)"
    r"|File \"[^\"]+\", line \d+, in .+"
    r")$"
)
_EXPLICIT_FAILURE_DETAIL_RE = re.compile(
    r"^(?:"
    r"(?:caused by|suppressed):\s+"
    r"|(?:expected|received|actual|left|right|reason|note|help):\s+"
    r"|(?:e|error|fail|failed):\s+"
    r"|(?:e|error|fail|failed)\s+(?:[\w.]+(?:error|exception)\b|expected\b|actual\b|left:|right:)"
    r"|[\w.]+(?:Error|Exception)(?:\b|:|\s+at\b)"
    r")",
    re.IGNORECASE,
)
_SOURCE_CONTEXT_RE = re.compile(
    r"^(?:"
    r">\s*"
    r"|(?:\d+\s*)?\|\s*"
    r"|[\^~]{2,}"
    r"|(?:assert|return|raise|throw|let|const|var|val|fun|func|def|class|if|else|elif|for|while|switch|case|"
    r"import|from|public|private|protected|internal|static|final|override)\b"
    r"|[@#]"
    r"|//|/\*|\*"
    r"|[{}()[\].,;]"
    r")"
)


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


def diagnostic_summary_lines(
    text: str | None,
    *,
    max_lines: int = DEFAULT_DIAGNOSTIC_SUMMARY_LINES,
    line_limit: int = DEFAULT_DIAGNOSTIC_SUMMARY_LINE_LIMIT,
) -> list[str]:
    """Return short high-signal diagnostic lines from command output.

    This is intentionally platform-neutral. It prefers compiler/test/check lines that
    carry concrete file locations, failed test names, exception classes, or assertion
    failures without echoing source-code frames, and falls back to generic build-tool
    failure lines when nothing better is available.
    """
    if not text or max_lines <= 0 or line_limit <= 0:
        return []

    raw_lines = text.splitlines()
    scored: list[tuple[int, int]] = []
    for index, line in enumerate(raw_lines):
        score = _diagnostic_score(line)
        if score:
            scored.append((index, score))

    if not scored:
        return []

    threshold = 100 if any(score >= 100 for _, score in scored) else 50
    if not any(score >= threshold for _, score in scored):
        threshold = 10

    selected_indexes: list[int] = []
    for index, score in scored:
        if score < threshold:
            continue
        _append_unique_index(selected_indexes, index)
        for related_index in _related_context_indexes(raw_lines, index):
            _append_unique_index(selected_indexes, related_index)

    lines: list[str] = []
    seen: set[str] = set()
    for index in selected_indexes:
        line = _compact_diagnostic_line(raw_lines[index], limit=line_limit)
        key = diagnostic_identity_key(line)
        if not line or key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def diagnostic_identity_key(line: str) -> str:
    """Return a stable key for deduplicating visually different forms of the same diagnostic."""

    compacted = " ".join(_ANSI_RE.sub("", str(line)).split())
    if ".../" in compacted:
        return compacted.split(".../", 1)[1]
    return compacted


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


def _diagnostic_score(line: str) -> int:
    normalized = _ANSI_RE.sub("", line).rstrip()
    if not normalized.strip():
        return 0
    stripped = normalized.strip()
    if _NOISY_DIAGNOSTIC_RE.search(stripped):
        return 10
    if _ASSERTION_COMPARISON_VALUES_RE.search(stripped):
        return 50
    if _looks_like_source_context_line(normalized) and not _looks_like_structured_diagnostic_line(stripped):
        return 0
    if _PRIMARY_DIAGNOSTIC_RE.search(stripped):
        return 100
    if _looks_like_source_context_line(normalized):
        return 0
    if _DIAGNOSTIC_RE.search(stripped):
        return 50
    return 0


def _append_unique_index(indexes: list[int], index: int) -> None:
    if index not in indexes:
        indexes.append(index)


def _related_context_indexes(lines: list[str], index: int) -> list[int]:
    indexes: list[int] = []
    blank_lines_seen = 0
    for related_index in range(index + 1, min(len(lines), index + 4)):
        line = _ANSI_RE.sub("", lines[related_index]).rstrip()
        stripped = line.strip()
        if not stripped:
            blank_lines_seen += 1
            if blank_lines_seen > 1:
                break
            continue
        if _diagnostic_score(line) >= 100 or _looks_like_failure_detail(line):
            indexes.append(related_index)
            continue
        if _looks_like_source_context_line(line):
            continue
        break
    return indexes


def _looks_like_failure_detail(line: str) -> bool:
    stripped = line.strip()
    return bool(_STACK_FRAME_DETAIL_RE.search(stripped) or _EXPLICIT_FAILURE_DETAIL_RE.search(stripped))


def _looks_like_structured_diagnostic_line(line: str) -> bool:
    return bool(
        _ASSERTION_COMPARISON_VALUES_RE.search(line)
        or _STACK_FRAME_DETAIL_RE.search(line)
        or _EXPLICIT_FAILURE_DETAIL_RE.search(line)
        or re.search(r"(?:^|\s)(?:file://)?(?:[/\\][^\s:]+)+:\d+(?::\d+)?:", line)
        or re.search(r"(^|\s)(?:e|error(?:\[[^\]]+\])?|fatal error):\s", line, re.IGNORECASE)
    )


def _looks_like_source_context_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _looks_like_failure_detail(line):
        return False
    if (
        stripped.startswith(">")
        or re.match(r"^[EW]\s+", stripped)
        or re.match(r"^(?:\d+\s*)?\|", stripped)
        or re.match(r"^[\^~]{2,}", stripped)
    ):
        return True
    return line.startswith((" ", "\t")) or bool(_SOURCE_CONTEXT_RE.match(stripped))


def _compact_diagnostic_line(line: str, *, limit: int) -> str:
    compacted = " ".join(_ANSI_RE.sub("", line).split())
    if not compacted:
        return ""
    compacted = _ASSERTION_VALUES_RE.sub(r"\1assertion failed", compacted)
    compacted = _ASSERTION_COMPARISON_VALUES_RE.sub(r"\1<redacted>", compacted)
    compacted = _shorten_paths(compacted)
    return _middle_truncate(compacted, limit)


def _shorten_paths(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group("path")
        location = match.group("location") or ""
        normalized = path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if len(parts) <= 3 and len(path) <= 80:
            return match.group(0)
        suffix = "/".join(parts[-3:]) if len(parts) >= 3 else normalized
        return f".../{suffix}{location}"

    return _ABSOLUTE_PATH_RE.sub(replace, line)


def _middle_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "..."
    if limit <= len(marker):
        return text[:limit]
    available = limit - len(marker)
    head_len = max(1, available // 3)
    tail_len = available - head_len
    return text[:head_len].rstrip() + marker + text[-tail_len:].lstrip()


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
