"""Audit generated/modified tests for newly introduced execution gates."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping


_SKIP_GATE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "skip",
        "skipped JavaScript/TypeScript test",
        re.compile(r"\b(?:describe|context|suite|test|it)(?:\.\w+)*\.skip(?:\.\w+)*\s*\("),
    ),
    (
        "skip",
        "Playwright fixme-skipped test",
        re.compile(r"\b(?:describe|context|suite|test|it)(?:\.\w+)*\.fixme(?:\.\w+)*\s*\("),
    ),
    (
        "skip",
        "JavaScript/TypeScript todo test",
        re.compile(r"\b(?:describe|context|suite|test|it)(?:\.\w+)*\.todo(?:\.\w+)*\s*\("),
    ),
    (
        "skip",
        "disabled JavaScript/TypeScript test alias",
        re.compile(r"\b(?:xdescribe|xcontext|xsuite|xtest|xit)\s*\("),
    ),
    (
        "skip",
        "pytest skipped test",
        re.compile(r"(?:^|\s)(?:@pytest\.mark\.skip(?:if)?\b|pytest\.skip\s*\()"),
    ),
    (
        "skip",
        "unittest skipped test",
        re.compile(r"(?:^|\s)(?:@unittest\.skip(?:If|Unless)?\b|self\.skipTest\s*\()"),
    ),
    (
        "skip",
        "JUnit disabled test",
        re.compile(r"(?:^|\s)@(?:(?:org\.junit\.jupiter\.api\.)?Disabled|(?:org\.junit\.)?Ignore)\b"),
    ),
    (
        "assumption",
        "JUnit assumption-gated test",
        re.compile(r"\b(?:Assumptions?\.)?(?:assumeTrue|assumeFalse|assumeNoException|assumingThat)\s*\("),
    ),
    (
        "skip",
        "Rust ignored test",
        re.compile(r"#\s*\[\s*ignore\b"),
    ),
    (
        "skip",
        "Go skipped test",
        re.compile(r"\bt\.Skip(?:f|Now)?\s*\("),
    ),
    (
        "skip",
        "XCTest skipped test",
        re.compile(r"\b(?:(?:try[!?]?|throw)\s+)?XCTSkip(?:If|Unless)?\s*\("),
    ),
    (
        "skip",
        "PHPUnit skipped test",
        re.compile(r"\bmarkTestSkipped\s*\("),
    ),
)
_PLAYWRIGHT_CONFIGURE_OPEN_RE = re.compile(r"\btest\.describe\.configure\s*\(\s*\{")
_PLAYWRIGHT_SKIP_MODE_RE = re.compile(r"\bmode\s*:\s*[\"']skip[\"']")

_ENVIRONMENT_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btypeof\s+(?:globalThis\.)?(?:document|window|navigator)\b"),
    re.compile(r"(?:globalThis\.)?(?:document|window|navigator)\s*[!=]==?\s*undefined\b"),
    re.compile(r"(?:globalThis\.)?(?:document|window|navigator)\s*[!=]==?\s*null\b"),
    re.compile(r"[\"'](?:document|window|navigator)[\"']\s+in\s+globalThis\b"),
    re.compile(r"\b(?:process|import\.meta)\.env(?:\.[A-Za-z_]\w*|\s*\[[^\]]+\])"),
    re.compile(r"\bos\.environ(?:\b|\s*\[|\.get\s*\()"),
    re.compile(r"\bos\.getenv\s*\("),
    re.compile(r"\bSystem\.getenv\s*\("),
    re.compile(r"\bENV\s*\["),
    re.compile(r"\b(?:getenv\s*\(|\$_(?:ENV|SERVER)\s*\[)"),
    re.compile(r"\bProcessInfo\.processInfo\.environment\b"),
    re.compile(r"\bos\.Getenv\s*\("),
)
_ENVIRONMENT_EXPRESSION_OPERATOR_RE = re.compile(r"(?:&&|\|\||\?)")
_CONTROL_GATE_START_RE = re.compile(r"^\s*if\b")
_MAX_GATE_HEADER_LINES = 20
_MAX_GATED_BODY_LINES = 50

_TEST_REGISTRATION_PATTERN = re.compile(
    r"\b(?:describe|context|suite|test|it)"
    r"(?:\.(?:describe|each|concurrent|serial|parallel|skip|fixme|todo|only))*\s*\(|"
    r"(?:^|\s)(?:describe|context|it)\s+[\"'][^\"']+[\"']\s+do\b|"
    r"(?:^|\s)def\s+test_\w*\s*\(|"
    r"(?:^|\s)(?:func|fun)\s+[Tt]est\w*\s*\(|"
    r"(?:^|\s)(?:public\s+)?function\s+test\w*\s*\(|"
    r"(?:^|\s)@Test\b|"
    r"#\s*\[\s*test\b"
)


def detect_new_test_execution_gates(
    *,
    path: str,
    before: str | None,
    after: str | None,
    before_counts: Mapping[str, int] | None = None,
) -> list[dict]:
    """Return newly added skip/disable/assumption/environment gates in a test file.

    The detector intentionally looks only at inserted/replaced lines. Existing project
    skips remain untouched; Sikula only cares when its own agent introduces a new gate.
    """

    baseline_counts = _coerce_gate_signature_counts(before_counts)
    if before_counts is None:
        baseline_counts = _gate_signature_counts(before)
    seen_after: Counter[str] = Counter()
    findings: list[dict] = []

    for finding in _all_test_execution_gates(path=path, text=after):
        signature = str(finding.get("signature", ""))
        seen_after[signature] += 1
        occurrence = seen_after[signature]
        baseline_count = baseline_counts[signature]
        if occurrence <= baseline_count:
            continue
        finding["baseline_count"] = baseline_count
        finding["occurrence"] = occurrence
        findings.append(finding)
    return findings


def test_execution_gate_signature_counts(text: str | None) -> dict[str, int]:
    """Return sanitized execution-gate baseline counts for durable task state."""

    return dict(_gate_signature_counts(text))


def active_findings_for_current_files(root, records: Iterable[dict]) -> list[dict]:
    """Return findings that still appear in the current working tree.

    This keeps pending audit errors resume-safe and avoids retrying against a stale
    finding that a later test writer/fixer pass already removed.
    """

    active: list[dict] = []
    cache: dict[str, list[dict]] = {}
    for record in records:
        if record.get("status") == "resolved":
            continue
        for finding in record.get("findings", []):
            path = str(finding.get("path", ""))
            if not path:
                continue
            try:
                text = root.joinpath(*path.split("/")).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if path not in cache:
                cache[path] = _all_test_execution_gates(path=path, text=text)
            active_finding = _active_finding_from_current_gates(finding, cache[path], text)
            if active_finding:
                active.append(active_finding)
    return active


def _all_test_execution_gates(*, path: str, text: str | None) -> list[dict]:
    if not text:
        return []

    lines = text.splitlines()
    findings: list[dict] = []
    for index, line in enumerate(lines):
        classification = _classify_direct_gate(line)
        if classification:
            category, reason = classification
            findings.append(_finding(path, index, category, reason, line))
            continue

        if _is_playwright_skip_mode_configuration(lines, index):
            findings.append(_finding(path, index, "skip", "Playwright skip-mode configuration", line))
            continue

        environment_gate_signature_text = _environment_gate_signature_text(lines, index)
        if environment_gate_signature_text:
            findings.append(
                _finding(
                    path,
                    index,
                    "environment",
                    "environment-gated test registration",
                    environment_gate_signature_text,
                )
            )
    return findings


def _gate_signature_counts(text: str | None) -> Counter[str]:
    return Counter(str(finding.get("signature", "")) for finding in _all_test_execution_gates(path="", text=text))


def _coerce_gate_signature_counts(counts: Mapping[str, int] | None) -> Counter[str]:
    coerced: Counter[str] = Counter()
    for signature, count in (counts or {}).items():
        if not signature:
            continue
        try:
            parsed = int(count)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            coerced[str(signature)] = parsed
    return coerced


def _active_finding_from_current_gates(finding: dict, current_gates: list[dict], text: str) -> dict | None:
    signature = str(finding.get("signature", ""))
    occurrence = _positive_int(finding.get("occurrence"))
    baseline_count = _nonnegative_int(finding.get("baseline_count"))
    if signature and occurrence is not None:
        matching = [gate for gate in current_gates if gate.get("signature") == signature]
        if len(matching) >= occurrence and len(matching) > baseline_count:
            active = dict(finding)
            active["line"] = matching[occurrence - 1].get("line", active.get("line"))
            active.pop("excerpt", None)
            return active
        return None

    # Backward compatibility for state files written before signatures were recorded.
    excerpt = str(finding.get("excerpt", "")).strip()
    line = _positive_int(finding.get("line"))
    lines = text.splitlines()
    if excerpt and line is not None and line <= len(lines) and excerpt in lines[line - 1]:
        active = dict(finding)
        active.pop("excerpt", None)
        return active
    return None


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _classify_direct_gate(line: str) -> tuple[str, str] | None:
    stripped = _strip_line_comment(line).strip()
    if not stripped:
        return None
    for category, reason, pattern in _SKIP_GATE_PATTERNS:
        if pattern.search(stripped):
            return category, reason
    return None


def _is_playwright_skip_mode_configuration(lines: list[str], index: int) -> bool:
    stripped = _strip_line_comment(lines[index]).strip()
    if not _PLAYWRIGHT_SKIP_MODE_RE.search(stripped):
        return False
    if _PLAYWRIGHT_CONFIGURE_OPEN_RE.search(stripped):
        return True

    for line in reversed(lines[max(0, index - 20) : index]):
        previous = _strip_line_comment(line).strip()
        if "}" in previous:
            break
        if _PLAYWRIGHT_CONFIGURE_OPEN_RE.search(previous):
            return True
    return False


def _environment_gate_signature_text(lines: list[str], index: int) -> str | None:
    stripped = _strip_line_comment(lines[index]).strip()
    if not stripped:
        return None

    if _is_environment_expression_gate(stripped):
        return stripped

    header = _control_gate_header(lines, index)
    if header is None:
        return None
    header_lines, header_end_index = header
    header_text = " ".join(header_lines)
    if not _has_environment_signal(header_text):
        return None
    if _TEST_REGISTRATION_PATTERN.search(header_text):
        return header_text
    if any(_TEST_REGISTRATION_PATTERN.search(line) for line in _gated_body_lines(lines, index, header_end_index)):
        return header_text
    return None


def _has_environment_signal(text: str) -> bool:
    return any(pattern.search(text) for pattern in _ENVIRONMENT_SIGNAL_PATTERNS)


def _is_environment_expression_gate(stripped: str) -> bool:
    return (
        _has_environment_signal(stripped)
        and bool(_ENVIRONMENT_EXPRESSION_OPERATOR_RE.search(stripped))
        and bool(_TEST_REGISTRATION_PATTERN.search(stripped))
    )


def _control_gate_header(lines: list[str], index: int) -> tuple[list[str], int] | None:
    first = _strip_line_comment(lines[index]).strip()
    if not _CONTROL_GATE_START_RE.search(first):
        return None

    header: list[str] = []
    paren_depth = 0
    saw_paren = False
    for offset, line in enumerate(lines[index : min(len(lines), index + _MAX_GATE_HEADER_LINES)]):
        stripped = _strip_line_comment(line).strip()
        if not stripped:
            continue
        header.append(stripped)
        paren_depth += _paren_delta(stripped)
        saw_paren = saw_paren or "(" in stripped or ")" in stripped
        if "{" in stripped or stripped.endswith(":"):
            return header, index + offset
        if saw_paren and paren_depth <= 0:
            return header, index + offset
        if offset == 0 and _has_environment_signal(stripped):
            return header, index
        if offset == 0 and not saw_paren:
            return header, index
    return (header, index + len(header) - 1) if header else None


def _gated_body_lines(lines: list[str], index: int, header_end_index: int | None = None) -> list[str]:
    header_end = header_end_index if header_end_index is not None else index
    header_lines = [_strip_line_comment(line).strip() for line in lines[index : header_end + 1]]
    if any("{" in line for line in header_lines):
        return _brace_body_lines(lines, index, header_end)

    indented = _indented_body_lines(lines, index, header_end)
    if indented:
        return indented

    return _end_delimited_body_lines(lines, header_end)


def _brace_body_lines(lines: list[str], index: int, header_end_index: int) -> list[str]:
    depth = sum(_brace_delta(_strip_line_comment(line)) for line in lines[index : header_end_index + 1])
    if depth <= 0:
        return []

    body: list[str] = []
    for line in lines[header_end_index + 1 : min(len(lines), header_end_index + 1 + _MAX_GATED_BODY_LINES)]:
        stripped = _strip_line_comment(line).strip()
        if stripped:
            body.append(stripped)
        depth += _brace_delta(line)
        if depth <= 0:
            break
    return body


def _indented_body_lines(lines: list[str], index: int, header_end_index: int) -> list[str]:
    gate_indent = _indent_width(lines[index])
    body: list[str] = []
    for line in lines[header_end_index + 1 : min(len(lines), header_end_index + 1 + _MAX_GATED_BODY_LINES)]:
        stripped = _strip_line_comment(line).strip()
        if not stripped:
            continue
        if _indent_width(line) <= gate_indent:
            break
        body.append(stripped)
    return body


def _end_delimited_body_lines(lines: list[str], index: int) -> list[str]:
    body: list[str] = []
    for line in lines[index + 1 : min(len(lines), index + 50)]:
        stripped = _strip_line_comment(line).strip()
        if stripped == "end":
            return body
        if stripped:
            body.append(stripped)
    return []


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _paren_delta(line: str) -> int:
    return line.count("(") - line.count(")")


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _strip_line_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#") and not stripped.startswith("#["):
        return ""
    return line.split("//", 1)[0]


def _finding(path: str, index: int, category: str, reason: str, line: str) -> dict:
    return {
        "path": path,
        "line": index + 1,
        "category": category,
        "reason": reason,
        "signature": _gate_signature(category, reason, line),
    }


def _gate_signature(category: str, reason: str, line: str) -> str:
    normalized = re.sub(r"\s+", " ", _strip_line_comment(line).strip())
    digest = hashlib.sha256(f"{category}\0{reason}\0{normalized}".encode("utf-8")).hexdigest()
    return f"{category}:{digest[:16]}"
