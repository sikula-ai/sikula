"""Audit generated tests for synthetic runtime/framework harnesses."""

from __future__ import annotations

import re
from collections.abc import Iterable


_DECL_PREFIX = (
    r"^\s*(?:export\s+)?"
    r"(?:(?:abstract|data|final|internal|open|private|protected|public|sealed|static)\s+)*"
)


def _type_decl_pattern(name_suffix: str) -> re.Pattern[str]:
    return re.compile(
        _DECL_PREFIX + rf"(?:class|interface|object|struct|type)\s+(?:Fake|Mock|Stub)\w*(?:{name_suffix})\b",
        re.IGNORECASE,
    )


def _value_decl_pattern(name_suffix: str) -> re.Pattern[str]:
    return re.compile(
        _DECL_PREFIX
        + r"(?:(?:async\s+)?(?:function|fun)|const|let|var)\s+"
        + rf"(?:fake|mock|stub|installFake)\w*(?:{name_suffix})\b",
        re.IGNORECASE,
    )


_SUBSYSTEM_PATTERNS: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "render_tree",
        "render tree / UI structure fake",
        (
            _type_decl_pattern("Document|Element|Node|Dom|HTML|Html"),
            _type_decl_pattern("View|Widget|Composable|RenderTree"),
        ),
    ),
    (
        "event_dispatch",
        "event dispatch fake",
        (_type_decl_pattern("Event|MouseEvent|EventTarget|Listener"),),
    ),
    (
        "navigation_history",
        "navigation/history/router fake",
        (
            _type_decl_pattern("History|Location|Router|Navigator|NavController"),
            _value_decl_pattern("History|Location|Router|Navigator|NavController"),
        ),
    ),
    (
        "network_server",
        "network/server fake",
        (
            _type_decl_pattern("Fetch|API|Api|Server|HTTP|Http|Request|Response|Client"),
            _value_decl_pattern("Fetch|API|Api|Server|HTTP|Http|Request|Response|Client"),
        ),
    ),
    (
        "scheduler_lifecycle",
        "scheduler/lifecycle fake",
        (
            _type_decl_pattern("Scheduler|Timer|Clock|Looper|RunLoop|Lifecycle"),
            _value_decl_pattern("Scheduler|Timer|Clock|Looper|RunLoop|Lifecycle"),
        ),
    ),
    (
        "dependency_container",
        "dependency container/provider fake",
        (
            _type_decl_pattern("Container|Provider|Module|Registry|ServiceLocator|Injector"),
            _value_decl_pattern("Container|Provider|Module|Registry|ServiceLocator|Injector"),
        ),
    ),
    (
        "filesystem_command",
        "filesystem/command-runner fake",
        (
            _type_decl_pattern("FileSystem|CommandRunner|Process|Subprocess|Shell|FS"),
            _value_decl_pattern("FileSystem|CommandRunner|Process|Subprocess|Shell|FS"),
        ),
    ),
    (
        "platform_runtime",
        "platform/device runtime fake",
        (
            _type_decl_pattern("Activity|Application|Device|Emulator|Simulator|Runtime"),
            _value_decl_pattern("Activity|Application|Device|Emulator|Simulator|Runtime"),
        ),
    ),
)

_MIN_SUBSYSTEMS_FOR_FINDING = 3
_MAX_EVIDENCE_PER_SUBSYSTEM = 2


def detect_new_synthetic_test_harnesses(
    *,
    path: str,
    before: str | None,
    after: str | None,
) -> list[dict]:
    """Return soft-audit findings for newly added broad synthetic test harnesses.

    The detector compares the current whole test file against the task baseline. Existing
    project test helpers remain accepted seams; Sikula only records a finding when its own
    generated/modified test code newly crosses the broad-harness threshold.
    """

    if not after:
        return []

    baseline_evidence = _collect_subsystem_evidence(before)
    current_evidence = _collect_subsystem_evidence(after)

    if len(current_evidence) < _MIN_SUBSYSTEMS_FOR_FINDING:
        return []

    baseline_subsystems = set(baseline_evidence)
    current_subsystems = set(current_evidence)
    if len(baseline_subsystems) >= _MIN_SUBSYSTEMS_FOR_FINDING and current_subsystems <= baseline_subsystems:
        return []
    if current_subsystems <= baseline_subsystems:
        return []

    categories = sorted(current_evidence)
    evidence = [current_evidence[category] for category in categories]
    return [
        {
            "path": path,
            "category": "synthetic_runtime_harness",
            "reason": "generated or modified test code now combines multiple fake runtime subsystems",
            "subsystems": categories,
            "baseline_subsystems": sorted(baseline_subsystems),
            "subsystem_count": len(categories),
            "evidence": evidence,
            "recommendation": (
                "Replace with narrower existing-seam coverage, use project-standard runtime test "
                "infrastructure, or report a structured TESTABILITY GAP for behaviour outside the "
                "configured test surface."
            ),
        }
    ]


def active_findings_for_current_files(root, records: Iterable[dict]) -> list[dict]:
    """Return synthetic-harness findings that still appear in the current tree."""

    active: list[dict] = []
    for record in records:
        if record.get("status") == "resolved":
            continue
        for finding in record.get("findings", []):
            path = str(finding.get("path", ""))
            excerpts = _finding_excerpts(finding)
            if not path:
                continue
            try:
                text = root.joinpath(*path.split("/")).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _finding_is_active_in_text(finding, text, excerpts):
                active.append(finding)
    return active


def prompt_context_for_records(records: Iterable[dict], *, limit: int = 5) -> str:
    """Format unresolved synthetic harness findings for agent prompts."""

    findings: list[dict] = []
    for record in records:
        if record.get("status") == "resolved":
            continue
        for finding in record.get("findings", []):
            if isinstance(finding, dict):
                findings.append(finding)
    if not findings:
        return ""

    lines = [
        "SYNTHETIC TEST HARNESS AUDIT CONTEXT (non-blocking):",
        "Sikula previously detected generated or modified tests that combine multiple fake",
        "runtime/framework subsystems. This is an audit warning, not a hard failure.",
        "Sikula may restore those generated test files and retry the agent pass so the broad",
        "harness does not remain in branch output. Small narrow test doubles are still",
        "acceptable. When touching these tests, prefer existing project-standard seams,",
        "narrower public-contract coverage, or a structured TESTABILITY GAP for behaviour",
        "outside the configured test surface.",
        "",
        "Findings:",
    ]
    for finding in findings[:limit]:
        path = finding.get("path", "<unknown>")
        subsystems = ", ".join(finding.get("subsystems") or [])
        recommendation = finding.get("recommendation", "narrow the generated test surface")
        lines.append(f"- {path}: {subsystems or 'multiple runtime subsystems'}")
        lines.append(f"  recommendation: {recommendation}")
        evidence = _prompt_evidence(finding)
        if evidence:
            lines.append(f"  evidence: {evidence}")

    remaining = len(findings) - limit
    if remaining > 0:
        lines.append(f"- ... {remaining} more synthetic harness finding(s) in task state")
    return "\n".join(lines)


def _collect_subsystem_evidence(text: str | None) -> dict[str, dict]:
    evidence_by_subsystem: dict[str, dict] = {}
    if not text:
        return evidence_by_subsystem

    for index, line in enumerate(text.splitlines()):
        stripped = _strip_line_comment(line).strip()
        if not stripped:
            continue
        for category, reason, patterns in _SUBSYSTEM_PATTERNS:
            if not any(pattern.search(stripped) for pattern in patterns):
                continue
            evidence = evidence_by_subsystem.setdefault(
                category,
                {
                    "category": category,
                    "reason": reason,
                    "lines": [],
                },
            )
            if len(evidence["lines"]) < _MAX_EVIDENCE_PER_SUBSYSTEM:
                evidence["lines"].append({"line": index + 1, "excerpt": stripped})
    return evidence_by_subsystem


def _finding_is_active_in_text(finding: dict, text: str, excerpts: list[str]) -> bool:
    current_subsystems = set(_collect_subsystem_evidence(text))
    finding_subsystems = set(finding.get("subsystems") or [])
    if finding_subsystems:
        baseline_subsystems = set(finding.get("baseline_subsystems") or [])
        required_subsystems = finding_subsystems - baseline_subsystems or finding_subsystems
        return len(current_subsystems) >= _MIN_SUBSYSTEMS_FOR_FINDING and required_subsystems <= current_subsystems
    return any(excerpt in text for excerpt in excerpts)


def _strip_line_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#") and not stripped.startswith("#["):
        return ""
    return line.split("//", 1)[0]


def _finding_excerpts(finding: dict) -> list[str]:
    excerpts: list[str] = []
    for subsystem in finding.get("evidence", []):
        for line in subsystem.get("lines", []):
            excerpt = str(line.get("excerpt", "")).strip()
            if excerpt:
                excerpts.append(excerpt)
    return excerpts


def _prompt_evidence(finding: dict) -> str:
    samples: list[str] = []
    for subsystem in finding.get("evidence", []):
        category = str(subsystem.get("category", "")).strip()
        for line in subsystem.get("lines", [])[:1]:
            line_no = line.get("line", "?")
            samples.append(f"{category} line {line_no}")
            break
        if len(samples) >= 3:
            break
    return "; ".join(samples)
