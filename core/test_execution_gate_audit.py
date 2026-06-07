"""Audit generated/modified tests for newly introduced execution gates."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable


_SKIP_GATE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "skip",
        "skipped JavaScript/TypeScript test",
        re.compile(r"\b(?:describe|context|suite|test|it)\.skip\s*\("),
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
        re.compile(r"(?:^|\s)@Disabled\b"),
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
        re.compile(r"\b(?:throw\s+)?XCTSkip(?:If|Unless)?\s*\("),
    ),
    (
        "skip",
        "PHPUnit skipped test",
        re.compile(r"\bmarkTestSkipped\s*\("),
    ),
)

_ENVIRONMENT_GATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bif\s*\(.*\btypeof\s+(?:globalThis\.)?(?:document|window|navigator)\b"),
    re.compile(r"\bif\s*\(.*(?:globalThis\.)?(?:document|window|navigator)\s*[!=]==?\s*undefined\b"),
    re.compile(r"\bif\s*\(.*(?:globalThis\.)?(?:document|window|navigator)\s*[!=]==?\s*null\b"),
    re.compile(r"\bif\s*\(.*[\"'](?:document|window|navigator)[\"']\s+in\s+globalThis\b"),
)

_TEST_REGISTRATION_PATTERN = re.compile(
    r"\b(?:describe|context|suite|test|it)\s*\(|(?:^|\s)(?:func|fun)\s+test\w*\s*\(|(?:^|\s)@Test\b|#\s*\[\s*test\b"
)


def detect_new_test_execution_gates(
    *,
    path: str,
    before: str | None,
    after: str | None,
) -> list[dict]:
    """Return newly added skip/disable/assumption/environment gates in a test file.

    The detector intentionally looks only at inserted/replaced lines. Existing project
    skips remain untouched; Sikula only cares when its own agent introduces a new gate.
    """

    if not after:
        return []

    before_lines = before.splitlines() if before else []
    after_lines = after.splitlines()
    new_line_indexes = _changed_after_line_indexes(before_lines, after_lines)

    findings: list[dict] = []
    for index in new_line_indexes:
        line = after_lines[index]
        classification = _classify_direct_gate(line)
        if classification:
            category, reason = classification
            findings.append(_finding(path, index, category, reason, line))
            continue

        if _is_environment_gate_for_test_registration(after_lines, index):
            findings.append(
                _finding(
                    path,
                    index,
                    "environment",
                    "environment-gated test registration",
                    line,
                )
            )
    return findings


def active_findings_for_current_files(root, records: Iterable[dict]) -> list[dict]:
    """Return findings that still appear in the current working tree.

    This keeps pending audit errors resume-safe and avoids retrying against a stale
    finding that a later test writer/fixer pass already removed.
    """

    active: list[dict] = []
    for record in records:
        if record.get("status") == "resolved":
            continue
        for finding in record.get("findings", []):
            path = str(finding.get("path", ""))
            excerpt = str(finding.get("excerpt", "")).strip()
            if not path or not excerpt:
                continue
            try:
                text = root.joinpath(*path.split("/")).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if excerpt in text:
                active.append(finding)
    return active


def _changed_after_line_indexes(before_lines: list[str], after_lines: list[str]) -> list[int]:
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    indexes: list[int] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            indexes.extend(range(j1, j2))
    return indexes


def _classify_direct_gate(line: str) -> tuple[str, str] | None:
    stripped = _strip_line_comment(line).strip()
    if not stripped:
        return None
    for category, reason, pattern in _SKIP_GATE_PATTERNS:
        if pattern.search(stripped):
            return category, reason
    return None


def _is_environment_gate_for_test_registration(lines: list[str], index: int) -> bool:
    stripped = _strip_line_comment(lines[index]).strip()
    if not stripped:
        return False
    if not any(pattern.search(stripped) for pattern in _ENVIRONMENT_GATE_PATTERNS):
        return False

    window = lines[index : min(len(lines), index + 12)]
    return any(_TEST_REGISTRATION_PATTERN.search(_strip_line_comment(line).strip()) for line in window)


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
        "excerpt": line.strip(),
    }
