"""Implementation contract readiness checks for Markdown/plain-text task files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import yaml

from core.state import TaskState
from core.validation_coverage import (
    configured_validation_commands,
    extract_validation_commands,
    validation_command_coverage,
)

SCHEMA_VERSION = 1

_STATUS_THRESHOLDS = (
    (85, "ready"),
    (70, "warn"),
    (40, "weak"),
    (0, "not_ready"),
)

_SECTION_ALIASES = {
    "intent": {
        "intent",
        "goal",
        "goals",
        "objective",
        "objectives",
        "summary",
        "overview",
        "problem",
        "context",
        "desired behavior",
        "desired behaviour",
        "expected behavior",
        "expected behaviour",
    },
    "scope": {
        "scope",
        "in scope",
        "required changes",
        "requirements",
        "desired behavior",
        "desired behaviour",
        "expected behavior",
        "expected behaviour",
    },
    "acceptance_criteria": {
        "acceptance",
        "acceptance criteria",
        "acceptance criterias",
        "criteria",
        "desired behavior",
        "desired behaviour",
        "expected behavior",
        "expected behaviour",
        "requirements",
    },
    "out_of_scope": {
        "out of scope",
        "non goals",
        "non-goals",
        "not in scope",
        "excluded",
        "exclusions",
    },
    "security_privacy": {
        "security",
        "privacy",
        "security and privacy",
        "security privacy",
        "security notes",
        "privacy notes",
        "authorization",
        "permissions",
    },
    "tests": {
        "tests",
        "testing",
        "test plan",
        "test coverage",
        "coverage",
        "verification",
    },
    "validation": {
        "validation",
        "verification",
        "checks",
        "check",
        "before merge",
        "how to validate",
    },
    "reviewer_focus": {
        "reviewer focus",
        "review focus",
        "review notes",
        "risky areas",
        "risks",
        "review checklist",
    },
    "repo_context": {
        "context",
        "repo context",
        "architecture",
        "implementation notes",
        "existing behavior",
        "existing behaviour",
        "files",
        "references",
    },
}

_SECURITY_RISK_RE = re.compile(
    r"\b("
    r"auth|authorization|permission|role|admin|owner|member|login|session|token|secret|password|"
    r"invite|email|billing|payment|pii|personal data|privacy|account|tenant|workspace"
    r")\b",
    re.IGNORECASE,
)
_PERMISSION_RE = re.compile(r"\b(role|roles|permission|permissions|admin|owner|member|authorize|authorization)\b", re.I)
_TOKEN_RE = re.compile(r"\b(token|tokens|invite|invites|invitation|session)\b", re.I)
_PRIVACY_RE = re.compile(r"\b(email|privacy|pii|personal data|account|enumeration|leak|logged|logging)\b", re.I)
_NEGATIVE_CASE_RE = re.compile(
    r"\b("
    r"not|cannot|can't|must not|should not|reject|denied|unauthorized|forbidden|invalid|expired|"
    r"reused|duplicate|error|failure|fails|empty|missing|malformed|out of scope"
    r")\b",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(
    r"(`[^`]+`|\b(src|app|api|endpoint|route|screen|view|model|service|repository|database)\b)", re.I
)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_TEXT_HEADING_RE = re.compile(r"^\s{0,3}([A-Za-z][A-Za-z0-9 /&_-]{1,60}):\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


@dataclass(frozen=True)
class ContractGap:
    id: str
    severity: str
    category: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
        }


@dataclass(frozen=True)
class ClarifyingQuestion:
    id: str
    question: str
    why_it_matters: str
    blocks_delivery: bool
    answer_type: str = "text"
    suggested_choices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "question": self.question,
            "why_it_matters": self.why_it_matters,
            "blocks_delivery": self.blocks_delivery,
            "answer_type": self.answer_type,
        }
        if self.suggested_choices:
            data["suggested_choices"] = list(self.suggested_choices)
        return data


@dataclass(frozen=True)
class ContractCheckResult:
    source: dict[str, str | None]
    readiness_score: int
    status: str
    ready_for_autonomous_delivery: bool
    sections_detected: dict[str, bool]
    scores: dict[str, int]
    gaps: list[ContractGap]
    clarifying_questions: list[ClarifyingQuestion]
    suggested_sections: list[str]
    validation: dict[str, Any]
    strong_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": dict(self.source),
            "readiness_score": self.readiness_score,
            "status": self.status,
            "ready_for_autonomous_delivery": self.ready_for_autonomous_delivery,
            "sections_detected": dict(self.sections_detected),
            "scores": dict(self.scores),
            "gaps": [gap.to_dict() for gap in self.gaps],
            "clarifying_questions": [question.to_dict() for question in self.clarifying_questions],
            "suggested_sections": list(self.suggested_sections),
            "validation": dict(self.validation),
        }


@dataclass(frozen=True)
class ContractReportPaths:
    report_path: Path
    answers_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "check_report": str(self.report_path),
            "answers_template": str(self.answers_path),
        }


@dataclass(frozen=True)
class ContractReportWriteResult:
    report_path: Path
    answers_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "check_report": str(self.report_path),
            "answers_template": str(self.answers_path),
        }


@dataclass(frozen=True)
class _Section:
    heading: str
    normalized_heading: str
    content: str


@dataclass(frozen=True)
class _ParsedTask:
    title: str
    body: str
    sections: list[_Section]
    bullets: list[str]
    word_count: int


def check_contract_file(path: Path, *, project_config: dict | None = None) -> ContractCheckResult:
    text = path.read_text(encoding="utf-8").strip()
    source_format = "text" if path.suffix.lower() == ".txt" else "markdown"
    return check_contract(text, source_path=path, source_format=source_format, project_config=project_config)


def check_contract(
    text: str,
    *,
    source_path: Path | str | None = None,
    source_format: str = "markdown",
    project_config: dict | None = None,
) -> ContractCheckResult:
    parsed = _parse_markdown_task(text)
    sections_detected = _sections_detected(parsed)
    validation = _validation_details(text, project_config)
    security_sensitive = bool(_SECURITY_RISK_RE.search(text))

    scores = {
        "intent_clarity": _score_intent(parsed),
        "scope_clarity": _score_scope(parsed, sections_detected),
        "acceptance_criteria": _score_acceptance(parsed, sections_detected),
        "negative_cases": _score_negative_cases(parsed, security_sensitive),
        "out_of_scope": _score_out_of_scope(parsed, sections_detected),
        "security_privacy": _score_security_privacy(parsed, sections_detected, security_sensitive),
        "testability": _score_testability(parsed, sections_detected, validation),
        "validation": _score_validation(validation),
        "reviewer_focus": _score_reviewer_focus(parsed, sections_detected, security_sensitive),
        "task_size": _score_task_size(parsed),
        "repo_context_sufficiency": _score_repo_context(parsed, security_sensitive),
    }
    weighted_score = _weighted_score(scores)

    gaps = _build_gaps(parsed, sections_detected, scores, validation, security_sensitive)
    if any(gap.severity == "blocking" for gap in gaps):
        weighted_score = min(weighted_score, 69)
    elif gaps:
        weighted_score = min(weighted_score, 84)
    status = _status_for_score(weighted_score)
    questions = _build_questions(gaps, security_sensitive, text)
    suggested_sections = _suggested_sections(gaps)
    strong_signals = _strong_signals(scores, sections_detected, validation)

    return ContractCheckResult(
        source={
            "path": str(source_path) if source_path is not None else None,
            "format": source_format,
            "sha256": "sha256:" + sha256(text.encode("utf-8")).hexdigest(),
        },
        readiness_score=weighted_score,
        status=status,
        ready_for_autonomous_delivery=status == "ready" and not any(gap.severity == "blocking" for gap in gaps),
        sections_detected=sections_detected,
        scores=scores,
        gaps=gaps,
        clarifying_questions=questions,
        suggested_sections=suggested_sections,
        validation=validation,
        strong_signals=strong_signals,
    )


def render_contract_check(result: ContractCheckResult) -> str:
    lines = [
        f"Implementation Contract Readiness: {result.readiness_score}/100 - {result.status.upper()}",
        "",
    ]
    if result.strong_signals:
        lines.append("Strong:")
        lines.extend(f"- {signal}" for signal in result.strong_signals)
        lines.append("")

    blocking = [gap for gap in result.gaps if gap.severity == "blocking"]
    warnings = [gap for gap in result.gaps if gap.severity != "blocking"]
    if blocking:
        lines.append("Blocking gaps:")
        lines.extend(f"- {gap.message}" for gap in blocking)
        lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {gap.message}" for gap in warnings)
        lines.append("")
    if not blocking and not warnings:
        lines.append("No blocking gaps or warnings found.")
        lines.append("")

    if result.clarifying_questions:
        lines.append("Follow-up questions:")
        for index, question in enumerate(result.clarifying_questions, start=1):
            lines.append(f"{index}. [{question.id}] {question.question}")
        lines.append("")

    if result.suggested_sections:
        lines.append("Suggested sections:")
        lines.extend(f"- {section}" for section in result.suggested_sections)
        lines.append("")

    task_commands = result.validation.get("task_commands") or []
    if task_commands:
        lines.append("Validation commands found:")
        lines.extend(f"- {command}" for command in task_commands)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def contract_report_paths(
    task_path: Path,
    *,
    project_root: Path | None = None,
) -> ContractReportPaths:
    task_path = task_path.resolve()
    contract_dir = _contract_report_dir(task_path, project_root=project_root)
    artifact_base = _artifact_base_dir(project_root=project_root, contract_dir=contract_dir)
    stem = _select_report_stem(task_path, contract_dir, artifact_base)
    return ContractReportPaths(
        report_path=contract_dir / f"{stem}.check.json",
        answers_path=contract_dir / f"{stem}.answers.yaml",
    )


def write_contract_report(
    result: ContractCheckResult,
    *,
    task_path: Path,
    project_root: Path | None = None,
) -> ContractReportWriteResult:
    task_path = task_path.resolve()
    paths = contract_report_paths(task_path, project_root=project_root)
    paths.report_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_contract_path(paths.report_path, paths.report_path.parent)
    _ensure_contract_path(paths.answers_path, paths.answers_path.parent)
    artifact_base = _artifact_base_dir(project_root=project_root, contract_dir=paths.report_path.parent)

    report_data = _contract_report_data(result, task_path=task_path, artifact_base=artifact_base)
    _assert_generated_or_same_task_json(paths.report_path, task_path, artifact_base)
    paths.report_path.write_text(json.dumps(report_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    answers_data = _contract_answers_template(
        result,
        task_path=task_path,
        report_path=paths.report_path,
        artifact_base=artifact_base,
    )
    answers_data = _merge_existing_answers(paths.answers_path, answers_data)
    _assert_generated_or_same_task_yaml(paths.answers_path, task_path, artifact_base)
    paths.answers_path.write_text(yaml.safe_dump(answers_data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    return ContractReportWriteResult(report_path=paths.report_path, answers_path=paths.answers_path)


def _contract_report_data(result: ContractCheckResult, *, task_path: Path, artifact_base: Path) -> dict[str, Any]:
    data = result.to_dict()
    data["source"]["path"] = _artifact_path(task_path, artifact_base)
    data["generated_by"] = "sikula.contract_check"
    data["checked_at"] = _utc_timestamp()
    return data


def _contract_answers_template(
    result: ContractCheckResult,
    *,
    task_path: Path,
    report_path: Path,
    artifact_base: Path,
) -> dict[str, Any]:
    questions = [question.to_dict() for question in result.clarifying_questions]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "sikula.contract_check",
        "task": {
            "path": _artifact_path(task_path, artifact_base),
            "sha256": str(result.source["sha256"]),
        },
        "check_report": _artifact_path(report_path, artifact_base),
        "questions": questions,
        "answers": {
            question["id"]: {
                "answer": "",
                "notes": "",
            }
            for question in questions
        },
    }


def _merge_existing_answers(path: Path, next_data: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return next_data
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return next_data
    if not isinstance(existing, dict):
        return next_data

    previous_answers = _existing_previous_answers(existing)
    if previous_answers:
        next_data["previous_answers"] = previous_answers

    existing_answers = existing.get("answers")
    next_answers = next_data.get("answers")
    if not isinstance(existing_answers, dict) or not isinstance(next_answers, dict):
        return next_data

    existing_sha = _task_sha(existing)
    next_sha = _task_sha(next_data)
    if existing_sha != next_sha:
        archived = _archived_existing_answers(existing)
        if archived:
            next_data.setdefault("previous_answers", []).append(archived)
        return next_data

    for question_id, answer_template in list(next_answers.items()):
        existing_answer = existing_answers.get(question_id)
        if not isinstance(existing_answer, dict):
            continue
        preserved = {
            "answer": existing_answer.get("answer", ""),
            "notes": existing_answer.get("notes", ""),
        }
        if preserved["answer"] or preserved["notes"]:
            next_answers[question_id] = preserved
        else:
            next_answers[question_id] = answer_template
    return next_data


def _task_sha(data: dict[str, Any]) -> str | None:
    task = data.get("task")
    if not isinstance(task, dict):
        return None
    value = task.get("sha256")
    return value if isinstance(value, str) and value else None


def _existing_previous_answers(data: dict[str, Any]) -> list[dict[str, Any]]:
    previous = data.get("previous_answers")
    if not isinstance(previous, list):
        return []
    return [entry for entry in previous if isinstance(entry, dict)]


def _archived_existing_answers(data: dict[str, Any]) -> dict[str, Any] | None:
    filled = _filled_answers(data.get("answers"))
    if not filled:
        return None
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    return {
        "archived_at": _utc_timestamp(),
        "task": {
            "path": task.get("path"),
            "sha256": task.get("sha256"),
        },
        "questions": data.get("questions") if isinstance(data.get("questions"), list) else [],
        "answers": filled,
    }


def _filled_answers(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    filled: dict[str, dict[str, Any]] = {}
    for question_id, answer in value.items():
        if not isinstance(question_id, str) or not isinstance(answer, dict):
            continue
        answer_text = answer.get("answer", "")
        notes = answer.get("notes", "")
        if answer_text or notes:
            filled[question_id] = {
                "answer": answer_text,
                "notes": notes,
            }
    return filled


def _contract_report_dir(task_path: Path, *, project_root: Path | None) -> Path:
    if project_root is not None:
        return project_root.resolve() / ".sikula" / "contracts"

    for parent in task_path.parents:
        if parent.name == ".sikula":
            return parent / "contracts"
        if parent.name == "tasks" and parent.parent.name == ".sikula":
            return parent.parent / "contracts"
    return Path.cwd().resolve() / ".sikula" / "contracts"


def _artifact_base_dir(*, project_root: Path | None, contract_dir: Path) -> Path:
    if project_root is not None:
        return project_root.resolve()
    if contract_dir.name == "contracts" and contract_dir.parent.name == ".sikula":
        return contract_dir.parent.parent.resolve()
    return Path.cwd().resolve()


def _select_report_stem(task_path: Path, contract_dir: Path, artifact_base: Path) -> str:
    base = _safe_report_stem(task_path.stem)
    candidate = ContractReportPaths(
        report_path=contract_dir / f"{base}.check.json",
        answers_path=contract_dir / f"{base}.answers.yaml",
    )
    if _paths_available_for_task(candidate, task_path, artifact_base):
        return base

    hashed = f"{base}-{sha256(str(task_path).encode('utf-8')).hexdigest()[:8]}"
    hashed_candidate = ContractReportPaths(
        report_path=contract_dir / f"{hashed}.check.json",
        answers_path=contract_dir / f"{hashed}.answers.yaml",
    )
    if not _paths_available_for_task(hashed_candidate, task_path, artifact_base):
        raise FileExistsError(
            f"Contract report paths already exist for a different task: {hashed_candidate.report_path}"
        )
    return hashed


def _paths_available_for_task(paths: ContractReportPaths, task_path: Path, artifact_base: Path) -> bool:
    return _json_report_available_for_task(
        paths.report_path, task_path, artifact_base
    ) and _yaml_answers_available_for_task(paths.answers_path, task_path, artifact_base)


def _json_report_available_for_task(path: Path, task_path: Path, artifact_base: Path) -> bool:
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("generated_by") == "sikula.contract_check" and _source_path_matches(
        data.get("source"), task_path, artifact_base
    )


def _yaml_answers_available_for_task(path: Path, task_path: Path, artifact_base: Path) -> bool:
    if not path.exists():
        return True
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("generated_by") == "sikula.contract_check" and _task_path_matches(
        data.get("task"), task_path, artifact_base
    )


def _assert_generated_or_same_task_json(path: Path, task_path: Path, artifact_base: Path) -> None:
    if _json_report_available_for_task(path, task_path, artifact_base):
        return
    raise FileExistsError(f"Refusing to overwrite non-contract report file: {path}")


def _assert_generated_or_same_task_yaml(path: Path, task_path: Path, artifact_base: Path) -> None:
    if _yaml_answers_available_for_task(path, task_path, artifact_base):
        return
    raise FileExistsError(f"Refusing to overwrite non-contract answers file: {path}")


def _source_path_matches(source: Any, task_path: Path, artifact_base: Path) -> bool:
    if not isinstance(source, dict):
        return False
    return _same_path(source.get("path"), task_path, artifact_base)


def _task_path_matches(task: Any, task_path: Path, artifact_base: Path) -> bool:
    if not isinstance(task, dict):
        return False
    return _same_path(task.get("path"), task_path, artifact_base)


def _same_path(value: Any, task_path: Path, artifact_base: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        if not path.is_absolute():
            path = artifact_base / path
        return path.resolve() == task_path.resolve()
    except OSError:
        return False


def _safe_report_stem(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "task"


def _ensure_contract_path(path: Path, contract_dir: Path) -> None:
    resolved_dir = contract_dir.resolve()
    resolved_path = path.resolve(strict=False)
    if not _is_relative_to(resolved_path, resolved_dir):
        raise ValueError(f"Contract report path resolves outside the contract directory: {path}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _artifact_path(path: Path, artifact_base: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(artifact_base.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_markdown_task(text: str) -> _ParsedTask:
    sections: list[_Section] = []
    current_heading = "preamble"
    current_normalized = "preamble"
    current_lines: list[str] = []
    title = ""
    bullets: list[str] = []

    def flush() -> None:
        content = "\n".join(current_lines).strip()
        sections.append(_Section(current_heading, current_normalized, content))

    for line in text.splitlines():
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            if current_lines or current_heading != "preamble":
                flush()
            raw_heading = heading_match.group(2).strip()
            if not title and heading_match.group(1) == "#":
                title = raw_heading
            current_heading = raw_heading
            current_normalized = _normalize_heading(raw_heading)
            current_lines = []
            continue
        text_heading_match = _TEXT_HEADING_RE.match(line)
        if text_heading_match:
            raw_heading = text_heading_match.group(1).strip()
            if _known_section_heading(raw_heading):
                if current_lines or current_heading != "preamble":
                    flush()
                current_heading = raw_heading
                current_normalized = _normalize_heading(raw_heading)
                current_lines = []
                continue
        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            bullets.append(bullet_match.group(1).strip())
        current_lines.append(line)

    if current_lines or not sections:
        flush()

    if not title:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                title = stripped.lstrip("#").strip()
                break

    return _ParsedTask(
        title=title,
        body=text,
        sections=sections,
        bullets=bullets,
        word_count=len(re.findall(r"\b[\w'-]+\b", text)),
    )


def _normalize_heading(value: str) -> str:
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _known_section_heading(value: str) -> bool:
    normalized = _normalize_heading(value)
    return any(normalized in aliases for aliases in _SECTION_ALIASES.values())


def _sections_detected(parsed: _ParsedTask) -> dict[str, bool]:
    detected = {name: False for name in _SECTION_ALIASES}
    for name in detected:
        content = _section_content(parsed, name)
        detected[name] = bool(content.strip())
    if not detected["intent"] and parsed.title and len(parsed.body.strip()) >= 20:
        detected["intent"] = True
    return detected


def _section_content(parsed: _ParsedTask, section_name: str) -> str:
    aliases = _SECTION_ALIASES[section_name]
    matches = [section.content for section in parsed.sections if section.normalized_heading in aliases]
    return "\n".join(match for match in matches if match.strip())


def _section_bullets(parsed: _ParsedTask, section_name: str) -> list[str]:
    aliases = _SECTION_ALIASES[section_name]
    bullets: list[str] = []
    for section in parsed.sections:
        if section.normalized_heading not in aliases:
            continue
        for line in section.content.splitlines():
            bullet_match = _BULLET_RE.match(line)
            if bullet_match:
                bullets.append(bullet_match.group(1).strip())
    return bullets


def _score_intent(parsed: _ParsedTask) -> int:
    if not parsed.body.strip():
        return 0
    if parsed.title and parsed.word_count >= 15:
        return 80
    if parsed.title:
        return 55
    if parsed.word_count >= 15:
        return 60
    return 25


def _score_scope(parsed: _ParsedTask, sections_detected: dict[str, bool]) -> int:
    bullets = _section_bullets(parsed, "scope")
    content = _section_content(parsed, "scope")
    if len(bullets) >= 3:
        return 90
    if len(bullets) >= 1:
        return 75
    if sections_detected["scope"] and len(content.split()) >= 15:
        return 70
    if len(parsed.bullets) >= 3:
        return 60
    return 25


def _score_acceptance(parsed: _ParsedTask, sections_detected: dict[str, bool]) -> int:
    bullets = _section_bullets(parsed, "acceptance_criteria")
    content = _section_content(parsed, "acceptance_criteria")
    if len(bullets) >= 4:
        return 90
    if len(bullets) >= 2:
        return 75
    if sections_detected["acceptance_criteria"] and len(content.split()) >= 15:
        return 65
    if len(parsed.bullets) >= 4:
        return 55
    return 20


def _score_negative_cases(parsed: _ParsedTask, security_sensitive: bool) -> int:
    relevant_text = "\n".join(
        [
            _section_content(parsed, "acceptance_criteria"),
            _section_content(parsed, "tests"),
            _section_content(parsed, "security_privacy"),
        ]
    )
    matches = _NEGATIVE_CASE_RE.findall(relevant_text)
    if len(matches) >= 3:
        return 90
    if len(matches) >= 1:
        return 70
    if security_sensitive:
        return 25
    return 72


def _score_out_of_scope(parsed: _ParsedTask, sections_detected: dict[str, bool]) -> int:
    if not sections_detected["out_of_scope"]:
        return 35
    if _section_bullets(parsed, "out_of_scope") or len(_section_content(parsed, "out_of_scope").split()) >= 8:
        return 85
    return 55


def _score_security_privacy(
    parsed: _ParsedTask,
    sections_detected: dict[str, bool],
    security_sensitive: bool,
) -> int:
    content = _section_content(parsed, "security_privacy")
    if sections_detected["security_privacy"] and len(content.split()) >= 8:
        return 88
    if sections_detected["security_privacy"]:
        return 70
    if security_sensitive:
        return 15
    return 90


def _score_testability(parsed: _ParsedTask, sections_detected: dict[str, bool], validation: dict[str, Any]) -> int:
    if sections_detected["tests"] and (
        _section_bullets(parsed, "tests") or len(_section_content(parsed, "tests")) > 20
    ):
        return 88
    if sections_detected["acceptance_criteria"] and (
        validation.get("task_commands") or validation.get("configured_commands")
    ):
        return 88
    if sections_detected["acceptance_criteria"]:
        return 65
    return 30


def _score_validation(validation: dict[str, Any]) -> int:
    task_commands = validation.get("task_commands") or []
    coverage_gaps = validation.get("coverage_gaps") or []
    configured_commands = validation.get("configured_commands") or []
    if task_commands and not coverage_gaps:
        return 90
    if task_commands and coverage_gaps:
        return 45
    if configured_commands:
        return 88
    return 25


def _score_reviewer_focus(
    parsed: _ParsedTask,
    sections_detected: dict[str, bool],
    security_sensitive: bool,
) -> int:
    if sections_detected["reviewer_focus"]:
        return 88
    if security_sensitive or parsed.word_count >= 180:
        return 35
    return 78


def _score_task_size(parsed: _ParsedTask) -> int:
    if parsed.word_count < 8:
        return 15
    if parsed.word_count < 35:
        return 55
    if parsed.word_count <= 650:
        return 90
    if parsed.word_count <= 1000:
        return 65
    return 35


def _score_repo_context(parsed: _ParsedTask, security_sensitive: bool) -> int:
    context_text = "\n".join(
        [
            _section_content(parsed, "repo_context"),
            _section_content(parsed, "scope"),
            _section_content(parsed, "acceptance_criteria"),
        ]
    )
    if _CONTEXT_RE.search(context_text):
        return 85
    if parsed.word_count >= 80:
        return 70
    if security_sensitive:
        return 35
    return 60


def _weighted_score(scores: dict[str, int]) -> int:
    weights = {
        "intent_clarity": 10,
        "scope_clarity": 12,
        "acceptance_criteria": 18,
        "negative_cases": 8,
        "out_of_scope": 8,
        "security_privacy": 10,
        "testability": 8,
        "validation": 10,
        "reviewer_focus": 4,
        "task_size": 5,
        "repo_context_sufficiency": 7,
    }
    total = sum(weights.values())
    weighted = sum(scores[name] * weight for name, weight in weights.items()) / total
    return max(0, min(100, round(weighted)))


def _status_for_score(score: int) -> str:
    for threshold, status in _STATUS_THRESHOLDS:
        if score >= threshold:
            return status
    return "not_ready"


def _validation_details(text: str, project_config: dict | None) -> dict[str, Any]:
    task_commands = extract_validation_commands(text)
    configured_commands: list[dict[str, str]] = []
    coverage_gaps: list[str] = []
    covered_commands: list[dict[str, Any]] = []

    if project_config:
        state = TaskState(task_id="contract_check", task_description=text)
        configured_commands = configured_validation_commands(project_config, state)
        for command in task_commands:
            covered, match_kind, configured = validation_command_coverage(command, configured_commands)
            if covered:
                covered_commands.append(
                    {
                        "command": command,
                        "match": match_kind or "covered",
                        "covered_by": configured["command"] if configured else None,
                    }
                )
            else:
                coverage_gaps.append(command)

    return {
        "task_commands": task_commands,
        "configured_commands": configured_commands,
        "covered_commands": covered_commands,
        "coverage_gaps": coverage_gaps,
    }


def _build_gaps(
    parsed: _ParsedTask,
    sections_detected: dict[str, bool],
    scores: dict[str, int],
    validation: dict[str, Any],
    security_sensitive: bool,
) -> list[ContractGap]:
    gaps: list[ContractGap] = []
    if scores["scope_clarity"] < 50:
        gaps.append(
            ContractGap(
                "gap.scope.boundaries",
                "blocking",
                "scope",
                "Scope boundaries are not clear enough for autonomous delivery.",
            )
        )
    if scores["acceptance_criteria"] < 50:
        gaps.append(
            ContractGap(
                "gap.acceptance.criteria",
                "blocking",
                "acceptance_criteria",
                "No explicit acceptance criteria or expected behaviour are defined.",
            )
        )
    if scores["negative_cases"] < 50:
        gaps.append(
            ContractGap(
                "gap.acceptance.negative_cases",
                "warning",
                "acceptance_criteria",
                "Negative, edge-case, or rejection behaviour is underspecified.",
            )
        )
    if not sections_detected["out_of_scope"]:
        gaps.append(
            ContractGap(
                "gap.scope.out_of_scope",
                "warning",
                "out_of_scope",
                "No explicit out-of-scope boundaries are listed.",
            )
        )
    if security_sensitive and scores["security_privacy"] < 50:
        gaps.append(
            ContractGap(
                "gap.security_privacy.impact",
                "blocking",
                "security_privacy",
                "Security or privacy impact is underspecified for a sensitive task.",
            )
        )
    elif not sections_detected["security_privacy"] and security_sensitive:
        gaps.append(
            ContractGap(
                "gap.security_privacy.section",
                "warning",
                "security_privacy",
                "Security/privacy notes are missing for a task with sensitive keywords.",
            )
        )
    if scores["testability"] < 50:
        gaps.append(
            ContractGap(
                "gap.tests.testability",
                "warning",
                "tests",
                "The task does not make the expected behaviour easy to turn into tests.",
            )
        )
    if validation.get("coverage_gaps"):
        gaps.append(
            ContractGap(
                "gap.validation.coverage",
                "blocking",
                "validation",
                "Task-described validation commands are not covered by the configured Sikula pipeline.",
            )
        )
    elif not validation.get("task_commands") and not validation.get("configured_commands"):
        gaps.append(
            ContractGap(
                "gap.validation.commands",
                "blocking",
                "validation",
                "No explicit validation commands are listed in the task.",
            )
        )
    if scores["reviewer_focus"] < 50:
        gaps.append(
            ContractGap(
                "gap.review.reviewer_focus",
                "warning",
                "reviewer_focus",
                "Reviewer focus is not called out for the riskiest areas.",
            )
        )
    if parsed.word_count < 8:
        gaps.append(
            ContractGap(
                "gap.task_size.too_small",
                "blocking",
                "task_size",
                "The task is too short to serve as an implementation contract.",
            )
        )
    elif parsed.word_count > 1000:
        gaps.append(
            ContractGap(
                "gap.task_size.too_large",
                "warning",
                "task_size",
                "The task may be too large for a single autonomous delivery run.",
            )
        )
    if scores["repo_context_sufficiency"] < 50:
        gaps.append(
            ContractGap(
                "gap.context.repo_context",
                "warning",
                "repo_context",
                "Repository context, affected surface, or domain rules are underspecified.",
            )
        )
    return gaps


def _build_questions(gaps: list[ContractGap], security_sensitive: bool, text: str) -> list[ClarifyingQuestion]:
    questions: list[ClarifyingQuestion] = []

    def add(question: ClarifyingQuestion) -> None:
        if all(existing.id != question.id for existing in questions):
            questions.append(question)

    gap_ids = {gap.id for gap in gaps}
    if "gap.scope.boundaries" in gap_ids:
        add(
            ClarifyingQuestion(
                "scope.boundaries",
                "What exactly is in scope, and what related behaviour should remain unchanged?",
                "The implementer needs clear boundaries to avoid unrelated refactors or product drift.",
                True,
            )
        )
    if "gap.acceptance.criteria" in gap_ids:
        add(
            ClarifyingQuestion(
                "acceptance.criteria",
                "What observable behaviours must be true when this task is complete?",
                "Acceptance criteria are the contract used by implementer, reviewer, and test writer.",
                True,
            )
        )
    if "gap.acceptance.negative_cases" in gap_ids:
        add(
            ClarifyingQuestion(
                "acceptance.negative_cases",
                "Which invalid, unauthorized, empty, duplicate, or failure cases should be handled?",
                "Negative cases prevent the pipeline from testing only the happy path.",
                security_sensitive,
            )
        )
    if "gap.scope.out_of_scope" in gap_ids:
        add(
            ClarifyingQuestion(
                "scope.out_of_scope",
                "What adjacent changes are explicitly out of scope?",
                "Out-of-scope boundaries reduce accidental changes outside the requested delivery.",
                False,
            )
        )
    if any(gap.category == "security_privacy" for gap in gaps):
        if _PERMISSION_RE.search(text):
            add(
                ClarifyingQuestion(
                    "permissions.authorization_model",
                    "Which roles or actors are allowed to perform the new or changed behaviour?",
                    "Authorization rules are product decisions that the agent should not guess.",
                    True,
                    "choice_or_text",
                    ["owner_only", "owner_admin", "all_members", "custom"],
                )
            )
        if _TOKEN_RE.search(text):
            add(
                ClarifyingQuestion(
                    "token.lifecycle",
                    "What lifecycle rules apply to tokens or invitations, including expiry and reuse?",
                    "Token lifecycle gaps can create stale access or account takeover risks.",
                    True,
                )
            )
        if _PRIVACY_RE.search(text):
            add(
                ClarifyingQuestion(
                    "privacy.data_handling",
                    "What data must not be logged, leaked, or revealed through errors?",
                    "Privacy-sensitive flows need explicit handling of logs and error messages.",
                    True,
                )
            )
        if not questions or not any(
            question.id.startswith(("permissions.", "token.", "privacy.")) for question in questions
        ):
            add(
                ClarifyingQuestion(
                    "security.privacy_impact",
                    "What security or privacy constraints must this task preserve?",
                    "The pipeline should not infer sensitive product policy from implementation context alone.",
                    True,
                )
            )
    if any(gap.category == "validation" for gap in gaps):
        add(
            ClarifyingQuestion(
                "validation.commands",
                "Which build, test, lint, or typecheck commands should validate this task?",
                "Validation commands connect the human contract to Sikula's configured pipeline.",
                "gap.validation.coverage" in gap_ids,
            )
        )
    if "gap.review.reviewer_focus" in gap_ids:
        add(
            ClarifyingQuestion(
                "reviewer.focus",
                "What should a human reviewer inspect most carefully?",
                "Reviewer focus helps the review phase prioritize the riskiest behavioural contracts.",
                False,
            )
        )
    if "gap.context.repo_context" in gap_ids:
        add(
            ClarifyingQuestion(
                "context.domain_rules",
                "Which existing files, APIs, domain rules, or project conventions should guide the change?",
                "Repo context prevents the analyst and implementer from guessing hidden conventions.",
                False,
            )
        )
    return questions


def _suggested_sections(gaps: list[ContractGap]) -> list[str]:
    labels = {
        "scope": "Scope",
        "acceptance_criteria": "Acceptance criteria",
        "out_of_scope": "Out of scope",
        "security_privacy": "Security and privacy",
        "tests": "Tests",
        "validation": "Validation",
        "reviewer_focus": "Reviewer focus",
        "repo_context": "Context",
    }
    sections: list[str] = []
    for gap in gaps:
        label = labels.get(gap.category)
        if label and label not in sections:
            sections.append(label)
    return sections


def _strong_signals(
    scores: dict[str, int], sections_detected: dict[str, bool], validation: dict[str, Any]
) -> list[str]:
    signals: list[str] = []
    if scores["intent_clarity"] >= 75:
        signals.append("Product intent is understandable.")
    if scores["scope_clarity"] >= 80:
        signals.append("Scope is described with concrete requested behaviour.")
    if scores["acceptance_criteria"] >= 80:
        signals.append("Acceptance criteria are explicit.")
    if sections_detected["out_of_scope"] and scores["out_of_scope"] >= 80:
        signals.append("Out-of-scope boundaries are present.")
    if sections_detected["security_privacy"] and scores["security_privacy"] >= 80:
        signals.append("Security/privacy considerations are described.")
    if validation.get("task_commands") and not validation.get("coverage_gaps"):
        signals.append("Validation commands are explicit and covered by the configured pipeline.")
    elif validation.get("configured_commands"):
        signals.append("Configured Sikula validation pipeline is available.")
    return signals
