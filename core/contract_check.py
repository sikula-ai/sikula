"""Implementation contract readiness checks for Markdown/plain-text task files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import mimetypes
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from core.state import TaskState
from core.validation_coverage import (
    configured_validation_commands,
    extract_validation_commands,
    validation_command_coverage,
)

SCHEMA_VERSION = 1
DEFAULT_CONTRACT_REPORT_DIR = ".sikula/contract-reports"
_GENERATED_ANSWER_ENTRY_END_MARKER = "<!-- /sikula:generated-answer -->"
_GENERATED_ANSWER_ENTRY_RE = re.compile(r"^\s*<!--\s*sikula:generated-answer:\s*([^>]+?)\s*-->\s*$")
_GENERATED_ANSWERS_ARTIFACT_SUFFIX = ".generated-answers.json"
_GENERATED_OPEN_QUESTIONS_MARKER = "<!-- sikula:generated-open-questions -->"
_IMPLEMENTATION_CONTEXT_ENTRY_IDS = [
    "project_context.details",
    "project_context.validation_commands",
    "asset_manifest.references",
]

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
        "project context",
        "product context",
        "repo context",
        "architecture",
        "implementation notes",
        "existing behavior",
        "existing behaviour",
        "files",
        "references",
    },
    "asset_manifest": {
        "asset manifest",
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
_ASSET_EXTENSIONS = {
    ".avif",
    ".csv",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".otf",
    ".pdf",
    ".png",
    ".svg",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
    ".yaml",
    ".yml",
    ".zip",
}
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_PLAIN_ASSET_PATH_RE = re.compile(
    r"(?<![\w/.-])"
    r"((?:\.{1,2}/|\.sikula/|[A-Za-z0-9_.-]+/)"
    r"[A-Za-z0-9_./@%+=:, -]*?"
    r"\.(?:avif|csv|gif|ico|jpe?g|json|md|otf|pdf|png|svg|ttf|txt|webp|woff2?|xml|ya?ml|zip))"
    r"(?![\w/.-])",
    re.IGNORECASE,
)
_ASSET_REFERENCE_HINT_RE = re.compile(
    r"\b(reference assets?|reference only|do not copy|screenshot|mockup|design reference|layout reference|spec excerpt)\b",
    re.IGNORECASE,
)
_ASSET_DELIVERY_HINT_RE = re.compile(
    r"\b(delivery asset|use as|use this file|copy|include|ship|production asset|target:|source/license:|"
    r"provided by|icon|font|fixture)\b",
    re.IGNORECASE,
)
_ASSET_STRONG_DELIVERY_HINT_RE = re.compile(
    r"\b(delivery assets?|target:|copy to|destination:|production asset|source/license:)\b",
    re.IGNORECASE,
)
_ASSET_TARGET_HINT_RE = re.compile(r"\b(target|target:|copy to|into|destination|path)\b", re.IGNORECASE)
_ASSET_PROVENANCE_HINT_RE = re.compile(
    r"\b(source/license|source:|license:|licence:|provenance|provided by|owned by)\b",
    re.IGNORECASE,
)
_ASSET_TARGET_DETAIL_RE = re.compile(r"\b(?:target|destination|copy to)\s*:\s*(.+)", re.IGNORECASE)
_ASSET_PROVENANCE_DETAIL_RE = re.compile(
    r"\b(?:source/license|source|license|licence|provenance)\s*:\s*(.+)",
    re.IGNORECASE,
)


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
    asset_references: list[dict[str, Any]] = field(default_factory=list)

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
            "asset_references": [dict(reference) for reference in self.asset_references],
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
class ContractImproveResult:
    output_path: Path
    check_result: ContractCheckResult
    answered_question_ids: list[str]
    open_question_ids: list[str]
    source_sha256: str
    answers_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "source_sha256": self.source_sha256,
            "answers_path": str(self.answers_path),
            "answered_question_ids": list(self.answered_question_ids),
            "open_question_ids": list(self.open_question_ids),
            "check": self.check_result.to_dict(),
        }


@dataclass(frozen=True)
class ContractPrepareWriteResult:
    output_path: Path
    generated_answers_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "output_path": str(self.output_path),
            "generated_answers": str(self.generated_answers_path),
        }


@dataclass(frozen=True)
class ContractTextImproveResult:
    markdown: str
    resume_markdown: str
    check_result: ContractCheckResult
    answered_question_ids: list[str]
    open_question_ids: list[str]
    source_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "resume_markdown": self.resume_markdown,
            "source_sha256": self.source_sha256,
            "answered_question_ids": list(self.answered_question_ids),
            "open_question_ids": list(self.open_question_ids),
            "check": self.check_result.to_dict(),
        }


@dataclass(frozen=True)
class PrepareProductContext:
    audience: str | None = None
    product_area: str | None = None
    known_constraints: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.audience:
            data["audience"] = self.audience
        if self.product_area:
            data["product_area"] = self.product_area
        if self.known_constraints:
            data["known_constraints"] = self.known_constraints
        return data


@dataclass(frozen=True)
class PrepareDeliveryEnvironment:
    local_sikula_config_present: bool | None = None
    source: str = "client_reported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_sikula_config_present": self.local_sikula_config_present,
            "source": self.source,
        }


@dataclass(frozen=True)
class PrepareProjectContext:
    validation_commands: list[str] = field(default_factory=list)
    stack: str | None = None
    package_manager: str | None = None
    known_constraints: str | None = None
    delivery_environment: PrepareDeliveryEnvironment | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "validation_commands": list(self.validation_commands),
        }
        if self.stack:
            data["stack"] = self.stack
        if self.package_manager:
            data["package_manager"] = self.package_manager
        if self.known_constraints:
            data["known_constraints"] = self.known_constraints
        if self.delivery_environment:
            data["delivery_environment"] = self.delivery_environment.to_dict()
        return data


@dataclass(frozen=True)
class ContractPrepareResult:
    """Prepared implementation-contract workflow state for chat/API adapters."""

    stage: str
    needs_user_input: bool
    ready_to_save: bool
    ready_to_run: bool
    required_next_step: str
    questions_for_user: list[ClarifyingQuestion]
    answers_template: dict[str, dict[str, Any]]
    prepared_contract_markdown: str
    check_result: ContractCheckResult
    recheck_result: ContractCheckResult | None
    unresolved_gaps: list[ContractGap]
    status_applies_to_sha256: str
    ready_to_run_blockers: list[str] = field(default_factory=list)
    answered_question_ids: list[str] = field(default_factory=list)
    open_question_ids: list[str] = field(default_factory=list)
    revised_answer_question_ids: list[str] = field(default_factory=list)
    user_questions: list[dict[str, Any]] = field(default_factory=list)
    resume_arguments: dict[str, Any] = field(default_factory=dict)
    authoritative_output_markdown: str = ""
    suggested_next_steps: list[str] = field(default_factory=list)
    required_user_action: str = ""
    primary_user_action: str = ""
    assistant_response_markdown: str = ""
    safe_task_path: str | None = None
    anti_loop_guidance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "needs_user_input": self.needs_user_input,
            "ready_to_save": self.ready_to_save,
            "ready_to_run": self.ready_to_run,
            "required_next_step": self.required_next_step,
            "questions_for_user": [question.to_dict() for question in self.questions_for_user],
            "answers_template": {key: dict(value) for key, value in self.answers_template.items()},
            "prepared_contract_markdown": self.prepared_contract_markdown,
            "check": self.check_result.to_dict(),
            "recheck": self.recheck_result.to_dict() if self.recheck_result else None,
            "unresolved_gaps": [gap.to_dict() for gap in self.unresolved_gaps],
            "status_applies_to_sha256": self.status_applies_to_sha256,
            "ready_to_run_blockers": list(self.ready_to_run_blockers),
            "answered_question_ids": list(self.answered_question_ids),
            "open_question_ids": list(self.open_question_ids),
            "revised_answer_question_ids": list(self.revised_answer_question_ids),
            "user_questions": [dict(question) for question in self.user_questions],
            "resume_arguments": dict(self.resume_arguments),
            "authoritative_output_markdown": self.authoritative_output_markdown,
            "suggested_next_steps": list(self.suggested_next_steps),
            "required_user_action": self.required_user_action,
            "primary_user_action": self.primary_user_action,
            "assistant_response_markdown": self.assistant_response_markdown,
            "safe_task_path": self.safe_task_path,
            "anti_loop_guidance": dict(self.anti_loop_guidance),
        }


@dataclass(frozen=True)
class TaskDescriptionPrepareResult:
    """Prepared product task-description workflow state for chat/API adapters."""

    stage: str
    needs_user_input: bool
    required_next_step: str
    questions_for_user: list[ClarifyingQuestion]
    answers_template: dict[str, dict[str, Any]]
    prepared_task_markdown: str
    answered_question_ids: list[str] = field(default_factory=list)
    open_question_ids: list[str] = field(default_factory=list)
    revised_answer_question_ids: list[str] = field(default_factory=list)
    user_questions: list[dict[str, Any]] = field(default_factory=list)
    resume_arguments: dict[str, Any] = field(default_factory=dict)
    authoritative_output_markdown: str = ""
    suggested_next_steps: list[str] = field(default_factory=list)
    required_user_action: str = ""
    primary_user_action: str = ""
    assistant_response_markdown: str = ""
    assumptions: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    anti_loop_guidance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "needs_user_input": self.needs_user_input,
            "required_next_step": self.required_next_step,
            "questions_for_user": [question.to_dict() for question in self.questions_for_user],
            "answers_template": {key: dict(value) for key, value in self.answers_template.items()},
            "prepared_task_markdown": self.prepared_task_markdown,
            "answered_question_ids": list(self.answered_question_ids),
            "open_question_ids": list(self.open_question_ids),
            "revised_answer_question_ids": list(self.revised_answer_question_ids),
            "user_questions": [dict(question) for question in self.user_questions],
            "resume_arguments": dict(self.resume_arguments),
            "authoritative_output_markdown": self.authoritative_output_markdown,
            "suggested_next_steps": list(self.suggested_next_steps),
            "required_user_action": self.required_user_action,
            "primary_user_action": self.primary_user_action,
            "assistant_response_markdown": self.assistant_response_markdown,
            "assumptions": list(self.assumptions),
            "non_goals": list(self.non_goals),
            "anti_loop_guidance": dict(self.anti_loop_guidance),
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
    configured_validation_commands: list[str] | None = None,
) -> ContractCheckResult:
    evaluation_text = _strip_generated_markers(_strip_generated_open_questions_section(text))
    parsed = _parse_markdown_task(evaluation_text)
    sections_detected = _sections_detected(parsed)
    validation = _validation_details(evaluation_text, project_config, configured_validation_commands)
    asset_references = _detect_asset_references(evaluation_text, source_path=source_path, project_config=project_config)
    security_sensitive = bool(_SECURITY_RISK_RE.search(evaluation_text))

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
        "repo_context_sufficiency": _score_repo_context(parsed, security_sensitive),
    }
    scores["task_size"] = _score_task_size(parsed, scores)
    weighted_score = _weighted_score(scores)

    gaps = _build_gaps(parsed, sections_detected, scores, validation, security_sensitive, asset_references)
    if any(gap.severity == "blocking" for gap in gaps):
        weighted_score = min(weighted_score, 69)
    elif gaps:
        weighted_score = min(weighted_score, 84)
    status = _status_for_score(weighted_score)
    questions = _build_questions(gaps, security_sensitive, evaluation_text)
    suggested_sections = _suggested_sections(gaps)
    strong_signals = _strong_signals(scores, sections_detected, validation, asset_references)

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
        asset_references=asset_references,
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

    if result.asset_references:
        lines.append("Asset references:")
        for reference in result.asset_references:
            path = reference.get("project_path") or reference.get("path")
            status = reference.get("status") or "unknown"
            kind = reference.get("kind") or "ambiguous"
            git_status = reference.get("git_status")
            details = [f"kind={kind}", f"status={status}"]
            if git_status:
                details.append(f"git={git_status}")
            lines.append(f"- `{path}` ({', '.join(details)})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def contract_report_paths(
    task_path: Path,
    *,
    project_root: Path | None = None,
    report_dir: Path | str | None = None,
) -> ContractReportPaths:
    task_path = task_path.resolve()
    contract_dir = _contract_report_dir(task_path, project_root=project_root, report_dir=report_dir)
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
    report_dir: Path | str | None = None,
) -> ContractReportWriteResult:
    task_path = task_path.resolve()
    paths = contract_report_paths(task_path, project_root=project_root, report_dir=report_dir)
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


def improve_contract_from_answers(
    task_path: Path,
    *,
    answers_path: Path,
    output_path: Path | None = None,
    write: bool = False,
    project_config: dict | None = None,
) -> ContractImproveResult:
    task_path = task_path.resolve()
    answers_path = answers_path.resolve()
    if write and output_path is not None:
        raise ValueError("Use either output_path or write=True, not both")
    if write:
        if not _is_markdown_path(task_path):
            raise ValueError("Refusing to overwrite a non-Markdown task; use --output TASK.v2.md")
        final_output_path = task_path
    elif output_path is not None:
        final_output_path = output_path.resolve()
    else:
        raise ValueError("Provide output_path or write=True")
    if not _is_markdown_path(final_output_path):
        raise ValueError("Improved contracts must be written as Markdown (.md or .markdown)")
    if not write and final_output_path == task_path:
        raise ValueError("Refusing to overwrite the original task without write=True")
    if final_output_path.exists() and final_output_path != task_path:
        raise FileExistsError(f"Refusing to overwrite existing output file: {final_output_path}")

    task_text = task_path.read_text(encoding="utf-8").strip()
    source_result = check_contract_file(task_path, project_config=project_config)
    answers_data = load_contract_answers_for_task(answers_path, source_result)
    questions = _answers_questions(answers_data)
    answers = _answers_mapping(answers_data)
    _reject_unknown_filled_answers(answers, questions)
    contract_dir = answers_path.parent
    artifact_base = _artifact_base_dir(project_root=None, contract_dir=contract_dir)
    generated_answer_entries = _load_generated_answer_entries(
        task_path,
        source_sha256=str(source_result.source["sha256"]),
        contract_dir=contract_dir,
        artifact_base=artifact_base,
    )

    improved = improve_contract_text(
        task_text,
        contract_name=task_path,
        questions=questions,
        answers=answers,
        source_result=source_result,
        output_name=final_output_path,
        project_config=project_config,
        generated_answer_entries=generated_answer_entries,
    )

    generated_answers_path = _contract_generated_answers_path(final_output_path, contract_dir, artifact_base)
    _ensure_contract_path(generated_answers_path, contract_dir)

    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    final_output_path.write_text(improved.markdown, encoding="utf-8")
    _write_generated_answer_entries(
        final_output_path,
        markdown=improved.markdown,
        resume_markdown=improved.resume_markdown,
        generated_answers_path=generated_answers_path,
        artifact_base=artifact_base,
    )
    return ContractImproveResult(
        output_path=final_output_path,
        check_result=improved.check_result,
        answered_question_ids=improved.answered_question_ids,
        open_question_ids=improved.open_question_ids,
        source_sha256=improved.source_sha256,
        answers_path=answers_path,
    )


def write_prepared_contract(
    result: ContractPrepareResult,
    *,
    output_path: Path,
    project_root: Path | None = None,
    report_dir: Path | str | None = None,
) -> ContractPrepareWriteResult:
    output_path = output_path.resolve()
    contract_dir = _contract_report_dir(output_path, project_root=project_root, report_dir=report_dir)
    artifact_base = _artifact_base_dir(project_root=project_root, contract_dir=contract_dir)
    generated_answers_path = _contract_generated_answers_path(output_path, contract_dir, artifact_base)
    _ensure_contract_path(generated_answers_path, contract_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.prepared_contract_markdown, encoding="utf-8")
    resume_markdown = str(result.resume_arguments.get("contract_markdown") or result.prepared_contract_markdown)
    _write_generated_answer_entries(
        output_path,
        markdown=result.prepared_contract_markdown,
        resume_markdown=resume_markdown,
        generated_answers_path=generated_answers_path,
        artifact_base=artifact_base,
    )
    return ContractPrepareWriteResult(output_path=output_path, generated_answers_path=generated_answers_path)


def load_generated_answer_entries_for_contract(
    task_path: Path,
    *,
    source_text: str,
    project_root: Path | None = None,
    report_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    task_path = task_path.resolve()
    contract_dir = _contract_report_dir(task_path, project_root=project_root, report_dir=report_dir)
    artifact_base = _artifact_base_dir(project_root=project_root, contract_dir=contract_dir)
    source_sha256 = "sha256:" + sha256(source_text.strip().encode("utf-8")).hexdigest()
    return _load_generated_answer_entries(
        task_path,
        source_sha256=source_sha256,
        contract_dir=contract_dir,
        artifact_base=artifact_base,
    )


def improve_contract_text(
    contract_markdown: str,
    *,
    contract_name: str | Path | None = None,
    questions: list[ClarifyingQuestion | dict[str, Any]],
    answers: dict[str, str | dict[str, Any]],
    source_result: ContractCheckResult | None = None,
    output_name: str | Path | None = None,
    project_config: dict | None = None,
    configured_validation_commands: list[str] | None = None,
    generated_answer_entries: list[dict[str, Any]] | None = None,
) -> ContractTextImproveResult:
    """Apply contract answers without reading or writing workflow files."""

    source_path = _virtual_contract_path(contract_name, default_suffix=".md")
    source_format = "text" if source_path.suffix.lower() == ".txt" else "markdown"
    source_evaluation_markdown = _strip_generated_markers(contract_markdown)
    if source_result is None:
        source_result = check_contract(
            source_evaluation_markdown,
            source_path=contract_name,
            source_format=source_format,
            project_config=project_config,
            configured_validation_commands=configured_validation_commands,
        )
    question_dicts = _question_dicts(questions)
    normalized_answers = _normalize_contract_answers(answers)
    _reject_unknown_filled_answers(normalized_answers, question_dicts)

    rendered, answered_ids, open_ids = _render_improved_contract(
        contract_markdown,
        source_path,
        question_dicts,
        normalized_answers,
        asset_references=source_result.asset_references,
        generated_answer_entries=generated_answer_entries,
    )
    check_result = check_contract(
        _strip_generated_markers(rendered),
        source_path=output_name if output_name is not None else contract_name,
        source_format="markdown",
        project_config=project_config,
        configured_validation_commands=configured_validation_commands,
    )
    rendered, open_ids = _reconcile_rendered_open_questions(
        rendered,
        check_result.clarifying_questions,
        normalized_answers,
    )
    resume_markdown = rendered
    rendered = _strip_generated_markers(resume_markdown)
    check_result = check_contract(
        rendered,
        source_path=output_name if output_name is not None else contract_name,
        source_format="markdown",
        project_config=project_config,
        configured_validation_commands=configured_validation_commands,
    )
    return ContractTextImproveResult(
        markdown=rendered,
        resume_markdown=resume_markdown,
        check_result=check_result,
        answered_question_ids=answered_ids,
        open_question_ids=open_ids,
        source_sha256=str(source_result.source["sha256"]),
    )


def prepare_task_description(
    brief: str,
    task_name: str | None = None,
    product_context: PrepareProductContext | dict[str, Any] | None = None,
    answers: dict[str, str | dict[str, Any]] | None = None,
) -> TaskDescriptionPrepareResult:
    """Prepare a product task description without scoring Sikula delivery readiness."""

    normalized_product_context = _normalize_prepare_product_context(product_context)
    source_markdown = _strip_generated_markers(_strip_generated_open_questions_section(brief)).strip()
    source_markdown = source_markdown or brief.strip()
    initial_markdown = _render_task_description_base(source_markdown, task_name, normalized_product_context)
    initial_questions = _build_task_description_questions(initial_markdown)
    active_answers = _answers_for_questions(answers or {}, initial_questions)
    prepared_markdown, resume_markdown, answered_ids = _render_prepared_task_description(
        initial_markdown,
        questions=initial_questions,
        answers=active_answers,
    )
    final_questions = _build_task_description_questions(prepared_markdown)
    resume_markdown, open_question_ids = _reconcile_rendered_open_questions(
        resume_markdown,
        final_questions,
        active_answers,
    )
    prepared_markdown = _strip_generated_markers(resume_markdown)
    final_question_ids = [question.id for question in final_questions]
    revised_answer_question_ids = [question.id for question in final_questions if question.id in set(answered_ids)]
    open_question_ids = list(dict.fromkeys([*open_question_ids, *final_question_ids]))
    needs_user_input = bool(final_questions)
    required_next_step = "answer_questions" if needs_user_input else "prepare_implementation_contract"
    required_user_action = _task_description_required_user_action(required_next_step)
    suggested_next_steps = _task_description_suggested_next_steps(required_next_step)

    return TaskDescriptionPrepareResult(
        stage="needs_user_input" if needs_user_input else "ready",
        needs_user_input=needs_user_input,
        required_next_step=required_next_step,
        questions_for_user=final_questions,
        answers_template=_prepare_answers_template_for_questions(final_questions, revised_answer_question_ids),
        prepared_task_markdown=prepared_markdown,
        answered_question_ids=answered_ids,
        open_question_ids=open_question_ids,
        revised_answer_question_ids=revised_answer_question_ids,
        user_questions=_prepare_user_questions(final_questions, revised_answer_question_ids),
        resume_arguments={
            "brief": resume_markdown,
            "task_name": task_name,
            "product_context": _prepare_product_context_for_resume(normalized_product_context),
        },
        authoritative_output_markdown=prepared_markdown,
        suggested_next_steps=suggested_next_steps,
        required_user_action=required_user_action,
        primary_user_action=required_user_action,
        assistant_response_markdown=_task_description_assistant_response_markdown(
            needs_user_input=needs_user_input,
            revised_answer_question_ids=revised_answer_question_ids,
            suggested_next_steps=suggested_next_steps,
            required_user_action=required_user_action,
        ),
        assumptions=[],
        non_goals=[],
        anti_loop_guidance=_prepare_anti_loop_guidance(),
    )


def prepare_implementation_contract(
    task_description_markdown: str,
    contract_name: str | None = None,
    answers: dict[str, str | dict[str, Any]] | None = None,
    project_context: PrepareProjectContext | dict[str, Any] | None = None,
    project_config: dict | None = None,
    generated_answer_entries: list[dict[str, Any]] | None = None,
) -> ContractPrepareResult:
    """Prepare an implementation contract through an in-memory check/improve loop."""

    source_format = "text" if contract_name and Path(contract_name).suffix.lower() == ".txt" else "markdown"
    project_config = project_config or _prepare_project_config_from_contract_name(contract_name)
    normalized_project_context = _normalize_prepare_project_context(project_context)
    project_context_blockers = _prepare_project_context_blockers(normalized_project_context)
    validation_commands = _validation_commands_from_prepare_context(normalized_project_context)
    safe_task_path = _safe_task_path_hint(contract_name, task_description_markdown)
    source_contract_markdown = _strip_revisable_generated_entries(
        task_description_markdown,
        answers=answers,
        generated_answer_entries=generated_answer_entries or [],
    )
    evaluation_contract_markdown = _strip_generated_markers(source_contract_markdown)
    check_result = check_contract(
        evaluation_contract_markdown,
        source_path=contract_name,
        source_format=source_format,
        project_config=project_config,
        configured_validation_commands=validation_commands,
    )

    if answers:
        # Chat/MCP prepare clients may resend an accumulated answer map across rounds.
        # Only answers for the currently active questions should be applied here;
        # earlier answers are already represented in the returned Markdown.
        active_questions = _prepare_active_questions_for_answers(
            source_contract_markdown,
            normalized_project_context,
            answers=answers,
            check_result=check_result,
            safe_task_path=safe_task_path,
            project_config=project_config,
            configured_validation_commands=validation_commands,
            generated_answer_entries=generated_answer_entries,
        )
        active_answers = _answers_for_questions(answers, active_questions)
        improved = improve_contract_text(
            source_contract_markdown,
            contract_name=contract_name,
            questions=active_questions,
            answers=active_answers,
            source_result=check_result,
            output_name=safe_task_path,
            project_config=project_config,
            configured_validation_commands=validation_commands,
            generated_answer_entries=generated_answer_entries,
        )
        prepared_contract_markdown, resume_contract_markdown = _enrich_implementation_contract_markdown(
            improved.resume_markdown,
            normalized_project_context,
            generated_answer_entries=generated_answer_entries,
            asset_references=improved.check_result.asset_references,
        )
        enriched_check_result = check_contract(
            prepared_contract_markdown,
            source_path=safe_task_path,
            source_format="markdown",
            project_config=project_config,
            configured_validation_commands=validation_commands,
        )
        return _build_prepare_result(
            contract_name=contract_name,
            project_context=normalized_project_context,
            project_context_blockers=project_context_blockers,
            safe_task_path=safe_task_path,
            check_result=check_result,
            recheck_result=enriched_check_result,
            prepared_contract_markdown=prepared_contract_markdown,
            resume_contract_markdown=resume_contract_markdown,
            answered_question_ids=improved.answered_question_ids,
            open_question_ids=improved.open_question_ids,
        )

    prepared_contract_markdown, resume_contract_markdown = _enrich_implementation_contract_markdown(
        source_contract_markdown,
        normalized_project_context,
        generated_answer_entries=generated_answer_entries,
        asset_references=check_result.asset_references,
    )
    recheck_result = None
    if prepared_contract_markdown != evaluation_contract_markdown.strip() + "\n":
        recheck_result = check_contract(
            prepared_contract_markdown,
            source_path=safe_task_path,
            source_format="markdown",
            project_config=project_config,
            configured_validation_commands=validation_commands,
        )

    return _build_prepare_result(
        contract_name=contract_name,
        project_context=normalized_project_context,
        project_context_blockers=project_context_blockers,
        safe_task_path=safe_task_path,
        check_result=check_result,
        recheck_result=recheck_result,
        prepared_contract_markdown=prepared_contract_markdown,
        resume_contract_markdown=resume_contract_markdown,
    )


def _prepare_active_questions_for_answers(
    contract_markdown: str,
    project_context: PrepareProjectContext | None,
    *,
    answers: dict[str, str | dict[str, Any]],
    check_result: ContractCheckResult,
    safe_task_path: str,
    project_config: dict | None,
    configured_validation_commands: list[str] | None,
    generated_answer_entries: list[dict[str, Any]] | None,
) -> list[ClarifyingQuestion]:
    questions = list(check_result.clarifying_questions)
    questions = _merge_clarifying_questions(
        questions,
        _generated_answer_questions_for_answers(answers, generated_answer_entries or []),
    )
    if project_context is None:
        return questions

    enriched_markdown, _resume_markdown = _enrich_implementation_contract_markdown(
        contract_markdown,
        project_context,
        generated_answer_entries=generated_answer_entries,
        asset_references=check_result.asset_references,
    )
    if enriched_markdown == _strip_generated_markers(contract_markdown).strip() + "\n":
        return questions

    enriched_check_result = check_contract(
        enriched_markdown,
        source_path=safe_task_path,
        source_format="markdown",
        project_config=project_config,
        configured_validation_commands=configured_validation_commands,
    )
    return _merge_clarifying_questions(questions, enriched_check_result.clarifying_questions)


def _strip_revisable_generated_entries(
    contract_markdown: str,
    *,
    answers: dict[str, str | dict[str, Any]] | None,
    generated_answer_entries: list[dict[str, Any]],
) -> str:
    if not generated_answer_entries:
        return contract_markdown
    question_ids = [*_IMPLEMENTATION_CONTEXT_ENTRY_IDS]
    if answers:
        question_ids.extend(_answered_generated_entry_ids(answers, generated_answer_entries))
    return _strip_tracked_generated_answer_entries(contract_markdown, question_ids, generated_answer_entries)


def _answered_generated_entry_ids(
    answers: dict[str, str | dict[str, Any]],
    generated_answer_entries: list[dict[str, Any]],
) -> list[str]:
    normalized_answers = _normalize_contract_answers(answers)
    entry_ids = {
        str(entry.get("question_id") or "").strip() for entry in generated_answer_entries if isinstance(entry, dict)
    }
    return [question_id for question_id in normalized_answers if question_id in entry_ids]


def _generated_answer_questions_for_answers(
    answers: dict[str, str | dict[str, Any]],
    generated_answer_entries: list[dict[str, Any]],
) -> list[ClarifyingQuestion]:
    normalized_answers = _normalize_contract_answers(answers)
    questions: list[ClarifyingQuestion] = []
    seen: set[str] = set()
    for entry in generated_answer_entries:
        if not isinstance(entry, dict):
            continue
        question_id = str(entry.get("question_id") or "").strip()
        if not question_id or question_id in seen:
            continue
        if not _answer_text(normalized_answers.get(question_id)):
            continue
        seen.add(question_id)
        questions.append(
            ClarifyingQuestion(
                question_id,
                question_id,
                "This answer updates a previously prepared generated contract section.",
                False,
            )
        )
    return questions


def _merge_clarifying_questions(*question_groups: list[ClarifyingQuestion]) -> list[ClarifyingQuestion]:
    merged: list[ClarifyingQuestion] = []
    seen: set[str] = set()
    for questions in question_groups:
        for question in questions:
            if question.id in seen:
                continue
            seen.add(question.id)
            merged.append(question)
    return merged


def _build_prepare_result(
    *,
    contract_name: str | None,
    project_context: PrepareProjectContext | None,
    project_context_blockers: list[str],
    safe_task_path: str,
    check_result: ContractCheckResult,
    recheck_result: ContractCheckResult | None,
    prepared_contract_markdown: str,
    resume_contract_markdown: str | None = None,
    answered_question_ids: list[str] | None = None,
    open_question_ids: list[str] | None = None,
) -> ContractPrepareResult:
    active_check = recheck_result or check_result
    answered_ids = list(answered_question_ids or [])
    questions_for_user = active_check.clarifying_questions
    active_question_ids = [question.id for question in questions_for_user]
    open_ids = list(dict.fromkeys([*(open_question_ids or []), *active_question_ids]))
    revised_answer_question_ids = [question.id for question in questions_for_user if question.id in set(answered_ids)]
    has_contract_questions = bool(questions_for_user)
    needs_user_input = has_contract_questions or bool(project_context_blockers)
    ready_to_run = active_check.ready_for_autonomous_delivery and not project_context_blockers
    ready_to_save = (
        not has_contract_questions and active_check.status in {"ready", "warn"} and not project_context_blockers
    )
    stage = (
        "ready"
        if ready_to_run
        else "needs_project_context"
        if project_context_blockers
        else "needs_user_input"
        if has_contract_questions
        else "review"
    )
    required_next_step = _prepare_required_next_step(
        has_contract_questions=has_contract_questions,
        project_context_blockers=project_context_blockers,
        ready_to_save=ready_to_save,
        ready_to_run=ready_to_run,
    )
    required_user_action = _required_user_action(required_next_step)
    user_questions = _prepare_user_questions(questions_for_user, revised_answer_question_ids)
    answers_template = _prepare_answers_template(active_check, revised_answer_question_ids)
    resume_markdown = resume_contract_markdown or prepared_contract_markdown
    resume_arguments = _prepare_resume_arguments(
        contract_markdown=resume_markdown,
        contract_name=contract_name,
        project_context=project_context,
        status_applies_to_sha256=str(active_check.source["sha256"]),
    )
    suggested_next_steps = _prepare_suggested_next_steps(
        required_next_step=required_next_step,
        safe_task_path=safe_task_path,
    )
    assistant_response_markdown = _prepare_assistant_response_markdown(
        active_check=active_check,
        project_context_blockers=project_context_blockers,
        ready_to_run=ready_to_run,
        required_user_action=required_user_action,
        suggested_next_steps=suggested_next_steps,
        revised_answer_question_ids=revised_answer_question_ids,
    )
    return ContractPrepareResult(
        stage=stage,
        needs_user_input=needs_user_input,
        ready_to_save=ready_to_save,
        ready_to_run=ready_to_run,
        required_next_step=required_next_step,
        questions_for_user=questions_for_user,
        answers_template=answers_template,
        prepared_contract_markdown=prepared_contract_markdown,
        check_result=check_result,
        recheck_result=recheck_result,
        unresolved_gaps=active_check.gaps,
        status_applies_to_sha256=str(active_check.source["sha256"]),
        ready_to_run_blockers=_ready_to_run_blockers(active_check, project_context_blockers),
        answered_question_ids=answered_ids,
        open_question_ids=open_ids,
        revised_answer_question_ids=revised_answer_question_ids,
        user_questions=user_questions,
        resume_arguments=resume_arguments,
        authoritative_output_markdown=prepared_contract_markdown,
        suggested_next_steps=suggested_next_steps,
        required_user_action=required_user_action,
        primary_user_action=required_user_action,
        assistant_response_markdown=assistant_response_markdown,
        safe_task_path=safe_task_path,
        anti_loop_guidance=_prepare_anti_loop_guidance(),
    )


def _virtual_contract_path(contract_name: str | Path | None, *, default_suffix: str) -> Path:
    if contract_name:
        path = Path(str(contract_name))
        if path.suffix:
            return path
        return path.with_suffix(default_suffix)
    return Path(f"contract{default_suffix}")


def _question_dicts(questions: list[ClarifyingQuestion | dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        data = question.to_dict() if isinstance(question, ClarifyingQuestion) else dict(question)
        question_id = data.get("id")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("Contract question is missing a stable id")
        if question_id in seen:
            raise ValueError(f"Contract questions contain duplicate id: {question_id}")
        seen.add(question_id)
        normalized.append(data)
    return normalized


def _normalize_contract_answers(answers: dict[str, str | dict[str, Any]]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for question_id, answer in answers.items():
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("Contract answers must be keyed by stable question id")
        if isinstance(answer, dict):
            normalized[question_id] = {
                "answer": answer.get("answer", ""),
                "notes": answer.get("notes", ""),
            }
        else:
            normalized[question_id] = {"answer": answer, "notes": ""}
    return normalized


def _answers_for_questions(
    answers: dict[str, str | dict[str, Any]],
    questions: list[ClarifyingQuestion],
) -> dict[str, dict[str, Any]]:
    normalized_answers = _normalize_contract_answers(answers)
    active_ids = {question.id for question in questions}
    return {question_id: answer for question_id, answer in normalized_answers.items() if question_id in active_ids}


def _prepare_answers_template(
    result: ContractCheckResult,
    revised_answer_question_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    return _prepare_answers_template_for_questions(result.clarifying_questions, revised_answer_question_ids)


def _prepare_answers_template_for_questions(
    questions: list[ClarifyingQuestion],
    revised_answer_question_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    revised_ids = set(revised_answer_question_ids or [])
    template: dict[str, dict[str, Any]] = {}
    for question in questions:
        entry = question.to_dict()
        entry["answer"] = ""
        entry["notes"] = ""
        entry["requires_revised_answer"] = question.id in revised_ids
        template[question.id] = entry
    return template


def _prepare_user_questions(
    questions: list[ClarifyingQuestion],
    revised_answer_question_ids: list[str],
) -> list[dict[str, Any]]:
    revised_ids = set(revised_answer_question_ids)
    user_questions: list[dict[str, Any]] = []
    for question in questions:
        data = question.to_dict()
        data["requires_revised_answer"] = question.id in revised_ids
        if question.id in revised_ids:
            data["reason"] = "The previous answer did not resolve this contract gap; provide a more specific answer."
        user_questions.append(data)
    return user_questions


def _prepare_required_next_step(
    *,
    has_contract_questions: bool,
    project_context_blockers: list[str],
    ready_to_save: bool,
    ready_to_run: bool,
) -> str:
    if project_context_blockers:
        return "provide_project_context"
    if has_contract_questions:
        return "answer_questions"
    if ready_to_run:
        return "save_and_run_contract"
    if ready_to_save:
        return "save_contract"
    return "revise_contract"


def _required_user_action(required_next_step: str) -> str:
    return {
        "answer_questions": "answer_contract_questions",
        "provide_project_context": "provide_project_context",
        "save_and_run_contract": "save_contract_and_run_sikula",
        "save_contract": "save_contract",
        "revise_contract": "revise_contract",
    }.get(required_next_step, "review_contract")


def _ready_to_run_blockers(result: ContractCheckResult, project_context_blockers: list[str]) -> list[str]:
    if result.ready_for_autonomous_delivery and not project_context_blockers:
        return []
    blockers = list(project_context_blockers)
    blockers.extend(gap.message for gap in result.gaps if gap.severity == "blocking")
    if result.status != "ready":
        blockers.append(f"Readiness status is {result.status}, not ready.")
    if not blockers and result.gaps:
        blockers.extend(gap.message for gap in result.gaps)
    return list(dict.fromkeys(blockers))


def _safe_task_path_hint(contract_name: str | None, contract_markdown: str) -> str:
    title = _contract_path_source_name(contract_name, contract_markdown)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:80].strip("-")
    return f".sikula/contracts/{slug or 'task'}.contract.md"


def _contract_path_source_name(contract_name: str | None, contract_markdown: str) -> str:
    if contract_name:
        name = _strip_contract_path_suffixes(Path(str(contract_name)).stem)
        if name:
            return name
    parsed = _parse_markdown_task(contract_markdown)
    if parsed.title:
        return parsed.title
    for line in contract_markdown.splitlines():
        stripped = line.strip().strip("#").strip()
        if stripped:
            return stripped
    return "task"


def _strip_contract_path_suffixes(stem: str) -> str:
    for suffix in (".refined", ".contract", ".v2", ".v3"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _normalize_prepare_product_context(
    product_context: PrepareProductContext | dict[str, Any] | None,
) -> PrepareProductContext | None:
    if product_context is None:
        return None
    if isinstance(product_context, PrepareProductContext):
        return product_context
    if not isinstance(product_context, dict):
        raise TypeError("product_context must be a PrepareProductContext or dict")
    return PrepareProductContext(
        audience=_prepare_optional_string(product_context.get("audience")),
        product_area=_prepare_optional_string(product_context.get("product_area")),
        known_constraints=_prepare_optional_string(product_context.get("known_constraints")),
    )


def _prepare_product_context_for_resume(product_context: PrepareProductContext | None) -> dict[str, Any]:
    if product_context is None:
        return {}
    return product_context.to_dict()


def _render_task_description_base(
    brief_markdown: str,
    task_name: str | None,
    product_context: PrepareProductContext | None,
) -> str:
    brief_markdown = brief_markdown.strip()
    parsed = _parse_markdown_task(brief_markdown)
    title = _task_description_title(task_name, parsed, brief_markdown)
    if _task_description_has_known_sections(parsed):
        lines = brief_markdown.splitlines()
    else:
        body = _task_description_body_without_title(brief_markdown, parsed)
        lines = [f"# {title}", ""]
        if body:
            lines.extend(["## Goal", "", *body.splitlines()])
    _append_product_context(lines, product_context)
    return "\n".join(lines).rstrip() + "\n"


def _task_description_title(task_name: str | None, parsed: _ParsedTask, brief_markdown: str) -> str:
    if task_name:
        return Path(str(task_name)).stem.replace("-", " ").strip() or "Product task"
    if parsed.title:
        return parsed.title
    for line in brief_markdown.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return "Product task"


def _task_description_has_known_sections(parsed: _ParsedTask) -> bool:
    for section in parsed.sections:
        if section.normalized_heading == "preamble":
            continue
        if section.heading == parsed.title:
            continue
        if _known_section_heading(section.heading):
            return True
    return False


def _task_description_body_without_title(brief_markdown: str, parsed: _ParsedTask) -> str:
    lines = brief_markdown.splitlines()
    if lines and _HEADING_RE.match(lines[0]) and parsed.title:
        return "\n".join(lines[1:]).strip()
    return brief_markdown


def _append_product_context(lines: list[str], product_context: PrepareProductContext | None) -> None:
    if product_context is None or not product_context.to_dict():
        return
    parsed = _parse_markdown_task("\n".join(lines))
    if _section_content(parsed, "repo_context").strip():
        return

    entries: list[str] = []
    if product_context.audience:
        entries.append(f"- Audience: {product_context.audience}")
    if product_context.product_area:
        entries.append(f"- Product area: {product_context.product_area}")
    if product_context.known_constraints:
        entries.append(f"- Known constraints: {product_context.known_constraints}")
    if not entries:
        return
    lines.extend(["", "## Product context", "", *entries])


def _build_task_description_questions(markdown: str) -> list[ClarifyingQuestion]:
    parsed = _parse_markdown_task(_strip_generated_open_questions_section(_strip_generated_markers(markdown)))
    sections_detected = _sections_detected(parsed)
    questions: list[ClarifyingQuestion] = []

    def add(question: ClarifyingQuestion) -> None:
        if all(existing.id != question.id for existing in questions):
            questions.append(question)

    if parsed.word_count < 5:
        add(
            ClarifyingQuestion(
                "product.goal",
                "What user-facing outcome should this task deliver?",
                "The task description needs a clear product goal before it can become an implementation contract.",
                True,
            )
        )
    if _score_scope(parsed, sections_detected) < 50:
        add(
            ClarifyingQuestion(
                "scope.boundaries",
                "What exactly is in scope, and what related behaviour should remain unchanged?",
                "Clear product boundaries prevent unrelated feature drift during delivery.",
                True,
            )
        )
    if _score_acceptance(parsed, sections_detected) < 50:
        add(
            ClarifyingQuestion(
                "acceptance.criteria",
                "What observable behaviours should prove this product task is complete?",
                "Observable acceptance criteria keep implementation, review, and tests aligned on product intent.",
                True,
            )
        )
    if not sections_detected["out_of_scope"]:
        add(
            ClarifyingQuestion(
                "scope.out_of_scope",
                "Which adjacent product changes are explicitly out of scope?",
                "Out-of-scope boundaries help preserve existing behaviour and avoid accidental product expansion.",
                False,
            )
        )
    return questions


def _render_prepared_task_description(
    base_markdown: str,
    *,
    questions: list[ClarifyingQuestion],
    answers: dict[str, dict[str, Any]],
) -> tuple[str, str, list[str]]:
    answered_question_ids = [question.id for question in questions if _answer_text(answers.get(question.id, {}))]
    task_text = _strip_generated_answer_entries(base_markdown, answered_question_ids)
    task_text = _strip_generated_open_questions_section(task_text)
    lines = task_text.strip().splitlines()

    section_entries: dict[str, list[tuple[ClarifyingQuestion, str]]] = {}
    for question in questions:
        answer_text = _answer_text(answers.get(question.id, {}))
        if not answer_text:
            continue
        section = _task_description_section_for_question(question.id)
        section_entries.setdefault(section, []).append((question, answer_text))

    for section in _ordered_task_description_sections(section_entries):
        entries = section_entries[section]
        rendered_entries = _task_description_answer_entry_lines(entries)
        _insert_or_append_section_entries(lines, section, rendered_entries)

    open_questions = [question.to_dict() for question in questions if question.id not in set(answered_question_ids)]
    _append_open_questions(lines, open_questions, answers)
    resume_markdown = "\n".join(lines).rstrip() + "\n"
    return _strip_generated_markers(resume_markdown), resume_markdown, answered_question_ids


def _task_description_section_for_question(question_id: str) -> str:
    if question_id == "product.goal":
        return "Goal"
    if question_id == "scope.boundaries":
        return "Scope"
    if question_id == "acceptance.criteria":
        return "Acceptance criteria"
    if question_id == "scope.out_of_scope":
        return "Out of scope"
    if question_id == "context.product":
        return "Product context"
    return "Clarifications"


def _ordered_task_description_sections(section_entries: dict[str, list[tuple[ClarifyingQuestion, str]]]) -> list[str]:
    preferred = ["Goal", "Scope", "Acceptance criteria", "Out of scope", "Product context", "Clarifications"]
    return [section for section in preferred if section in section_entries] + sorted(
        section for section in section_entries if section not in preferred
    )


def _task_description_answer_entry_lines(entries: list[tuple[ClarifyingQuestion, str]]) -> list[str]:
    lines: list[str] = []
    for question, answer_text in entries:
        lines.append(_generated_answer_entry_marker(question.id))
        for answer_line in _answer_lines(answer_text):
            lines.append(f"- {_clean_answer_bullet(answer_line)}")
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)
    return lines


def _insert_or_append_section_entries(lines: list[str], section: str, entries: list[str]) -> None:
    normalized_section = _normalize_heading(section)
    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        heading_match = _HEADING_RE.match(line)
        if not heading_match:
            continue
        if _normalize_heading(heading_match.group(2)) == normalized_section:
            section_start = index
            section_end = index + 1
            while section_end < len(lines) and not _HEADING_RE.match(lines[section_end]):
                section_end += 1
            break

    if section_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {section}", "", *entries])
        return

    insertion = list(entries)
    if section_end > section_start + 1 and lines[section_end - 1].strip():
        insertion.insert(0, "")
    if section_end < len(lines) and insertion and insertion[-1].strip():
        insertion.append("")
    lines[section_end:section_end] = insertion


def _enrich_implementation_contract_markdown(
    contract_markdown: str,
    project_context: PrepareProjectContext | None,
    *,
    generated_answer_entries: list[dict[str, Any]] | None = None,
    asset_references: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    source = contract_markdown.strip()
    source = _strip_generated_answer_entries(source, _IMPLEMENTATION_CONTEXT_ENTRY_IDS)
    source = _strip_tracked_generated_answer_entries(
        source,
        _IMPLEMENTATION_CONTEXT_ENTRY_IDS,
        generated_answer_entries or [],
    )
    lines = source.splitlines()

    if project_context is not None:
        current_markdown = "\n".join(lines)
        project_context_entries = _implementation_project_context_entry_lines(project_context, current_markdown)
        if project_context_entries:
            _insert_or_append_section_entries(lines, "Project context", project_context_entries)

        current_markdown = "\n".join(lines)
        validation_entries = _implementation_validation_entry_lines(project_context, current_markdown)
        if validation_entries:
            _insert_or_append_section_entries(lines, "Validation", validation_entries)

    current_markdown = "\n".join(lines)
    asset_manifest_entries = _implementation_asset_manifest_entry_lines(asset_references or [], current_markdown)
    if asset_manifest_entries:
        _insert_or_append_section_entries(lines, "Asset manifest", asset_manifest_entries)

    resume_markdown = "\n".join(lines).rstrip() + "\n"
    return _strip_generated_markers(resume_markdown), resume_markdown


def _implementation_project_context_entry_lines(
    project_context: PrepareProjectContext,
    current_markdown: str,
) -> list[str]:
    existing_context = _section_content(_parse_markdown_task(current_markdown), "repo_context")
    entries: list[str] = []
    for label, value in [
        ("Stack", project_context.stack),
        ("Package manager", project_context.package_manager),
        ("Known constraints", project_context.known_constraints),
    ]:
        if not value:
            continue
        line = f"- {label}: {_single_line(value)}"
        if line not in existing_context:
            entries.append(line)
    if not entries:
        return []
    return [
        _generated_answer_entry_marker("project_context.details"),
        *entries,
        _GENERATED_ANSWER_ENTRY_END_MARKER,
    ]


def _implementation_validation_entry_lines(
    project_context: PrepareProjectContext,
    current_markdown: str,
) -> list[str]:
    existing_commands = set(extract_validation_commands(current_markdown))
    commands = [
        command for command in project_context.validation_commands if command and command not in existing_commands
    ]
    if not commands:
        return []
    return [
        _generated_answer_entry_marker("project_context.validation_commands"),
        *[f"- `{_clean_validation_command(command)}`" for command in commands],
        _GENERATED_ANSWER_ENTRY_END_MARKER,
    ]


def _implementation_asset_manifest_entry_lines(
    asset_references: list[dict[str, Any]],
    current_markdown: str,
) -> list[str]:
    existing_manifest = _section_content(_parse_markdown_task(current_markdown), "asset_manifest")
    entries: list[str] = []
    for reference in asset_references:
        if not _asset_reference_ready_for_manifest(reference):
            continue
        project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
        if not project_path or project_path in existing_manifest:
            continue
        entries.extend(_asset_manifest_reference_lines(reference))

    if not entries:
        return []
    return [
        _generated_answer_entry_marker("asset_manifest.references"),
        *entries,
        _GENERATED_ANSWER_ENTRY_END_MARKER,
    ]


def _asset_reference_ready_for_manifest(reference: dict[str, Any]) -> bool:
    return reference.get("status") == "available" and reference.get("kind") in {"reference", "delivery"}


def _asset_manifest_reference_lines(reference: dict[str, Any]) -> list[str]:
    project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
    kind = str(reference.get("kind") or "reference").strip()
    lines = [f"- Path: `{project_path}`"]
    if kind == "delivery":
        lines.append("  - Usage: delivery asset; use this file only for the requested implementation.")
    else:
        lines.append("  - Usage: reference only; do not copy this asset into production files.")

    sha256_value = str(reference.get("sha256") or "").strip()
    if sha256_value:
        lines.append(f"  - SHA-256: `{sha256_value}`")

    lines.append(_asset_manifest_purpose_line(kind))

    mime_type = str(reference.get("mime_type") or "").strip()
    if mime_type:
        lines.append(f"  - MIME type: `{mime_type}`")
    size_bytes = reference.get("size_bytes")
    if isinstance(size_bytes, int):
        lines.append(f"  - Size: {size_bytes} bytes")
    git_status = str(reference.get("git_status") or "").strip()
    if git_status:
        lines.append(f"  - Git status: `{git_status}`")
    requested_target = str(reference.get("requested_target") or "").strip()
    if requested_target:
        lines.append(f"  - Requested target: `{requested_target}`")
    elif kind == "delivery":
        lines.append("  - Target resolution: analyst should choose the project-conventional location.")
    source_license = str(reference.get("source_license") or "").strip()
    if kind == "delivery" and source_license:
        lines.append(f"  - Source/license: {source_license}")
    return lines


def _asset_manifest_purpose_line(kind: str) -> str:
    if kind == "delivery":
        return "  - Purpose: delivery asset referenced by the implementation contract."
    return "  - Purpose: reference context for the implementation contract."


def _clean_validation_command(command: str) -> str:
    return _single_line(command).strip("`")


def _normalize_prepare_project_context(
    project_context: PrepareProjectContext | dict[str, Any] | None,
) -> PrepareProjectContext | None:
    if project_context is None:
        return None
    if isinstance(project_context, PrepareProjectContext):
        return project_context
    if not isinstance(project_context, dict):
        raise TypeError("project_context must be a PrepareProjectContext or dict")

    delivery_environment = _normalize_prepare_delivery_environment(project_context)
    validation_commands = _normalize_prepare_validation_commands(project_context.get("validation_commands"))
    return PrepareProjectContext(
        validation_commands=validation_commands,
        stack=_prepare_optional_string(project_context.get("stack")),
        package_manager=_prepare_optional_string(project_context.get("package_manager")),
        known_constraints=_prepare_optional_string(project_context.get("known_constraints")),
        delivery_environment=delivery_environment,
    )


def _prepare_project_config_from_contract_name(contract_name: str | None) -> dict[str, Any] | None:
    if not contract_name:
        return None
    path = Path(str(contract_name))
    if not path.is_absolute() and ".sikula" not in path.parts:
        return None
    project_root = _asset_project_root(contract_name, None)
    return {"project": {"root_path": str(project_root)}}


def _normalize_prepare_delivery_environment(project_context: dict[str, Any]) -> PrepareDeliveryEnvironment | None:
    value = project_context.get("delivery_environment")
    if isinstance(value, PrepareDeliveryEnvironment):
        return value
    if isinstance(value, dict):
        present = value.get("local_sikula_config_present")
        return PrepareDeliveryEnvironment(
            local_sikula_config_present=bool(present) if present is not None else None,
            source=str(value.get("source") or "client_reported"),
        )
    if "sikula_configured" in project_context:
        return PrepareDeliveryEnvironment(
            local_sikula_config_present=bool(project_context.get("sikula_configured")),
            source="client_reported",
        )
    return None


def _normalize_prepare_validation_commands(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(command).strip() for command in value if str(command).strip()]


def _prepare_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _prepare_project_context_blockers(project_context: PrepareProjectContext | None) -> list[str]:
    if project_context is None:
        return ["missing_project_context"]
    if not project_context.validation_commands:
        return ["missing_validation_commands"]
    return []


def _prepare_resume_arguments(
    *,
    contract_markdown: str,
    contract_name: str | None,
    project_context: PrepareProjectContext | None,
    status_applies_to_sha256: str,
) -> dict[str, Any]:
    return {
        "contract_markdown": contract_markdown,
        "contract_name": contract_name,
        "project_context": _prepare_project_context_for_resume(project_context),
        "status_applies_to_sha256": status_applies_to_sha256,
    }


def _prepare_project_context_for_resume(project_context: PrepareProjectContext | None) -> dict[str, Any]:
    if project_context is None:
        return {}
    return project_context.to_dict()


def _prepare_suggested_next_steps(
    *,
    required_next_step: str,
    safe_task_path: str,
) -> list[str]:
    if required_next_step == "answer_questions":
        return ["Answer the listed contract questions, then call prepare_implementation_contract again."]
    if required_next_step == "provide_project_context":
        return [
            "Provide project context with effective validation_commands, then call "
            "prepare_implementation_contract again."
        ]
    if required_next_step == "revise_contract":
        return ["Revise the contract manually, then run the contract check again."]
    if required_next_step == "save_contract":
        return [f"Save the prepared contract to `{safe_task_path}` before running delivery."]
    if required_next_step == "save_and_run_contract":
        return [
            f"Save the prepared contract to `{safe_task_path}`.",
            f"Run `sikula run {safe_task_path}` from a locally configured Sikula project.",
        ]
    return []


def _task_description_required_user_action(required_next_step: str) -> str:
    return {
        "answer_questions": "answer_task_description_questions",
        "prepare_implementation_contract": "prepare_implementation_contract",
    }.get(required_next_step, "review_task_description")


def _task_description_suggested_next_steps(required_next_step: str) -> list[str]:
    if required_next_step == "answer_questions":
        return ["Answer the listed product task questions, then call prepare_task_description again."]
    if required_next_step == "prepare_implementation_contract":
        return ["Use the returned task description as input to prepare_implementation_contract with project context."]
    return ["Review the prepared task description."]


def _task_description_assistant_response_markdown(
    *,
    needs_user_input: bool,
    revised_answer_question_ids: list[str],
    suggested_next_steps: list[str],
    required_user_action: str,
) -> str:
    changed = "Prepared product task description is ready for implementation-contract preparation."
    if revised_answer_question_ids:
        changed = "Some previous product-task answers still need more detail."
    elif needs_user_input:
        changed = "Product task description needs user input before implementation-contract preparation."
    next_step = suggested_next_steps[0] if suggested_next_steps else "Review the prepared task description."
    return "\n".join(
        [
            f"Changed: {changed}",
            f"Next step: {next_step}",
            f"Required action: {required_user_action}",
            "Note: This result does not evaluate Sikula delivery readiness.",
        ]
    )


def _prepare_assistant_response_markdown(
    *,
    active_check: ContractCheckResult,
    project_context_blockers: list[str],
    ready_to_run: bool,
    required_user_action: str,
    suggested_next_steps: list[str],
    revised_answer_question_ids: list[str],
) -> str:
    changed = "Prepared contract is ready for the next step."
    if revised_answer_question_ids:
        changed = "Some previous answers still need more detail."
    elif project_context_blockers:
        changed = "Project context is required before autonomous delivery."
    elif active_check.clarifying_questions:
        changed = "Contract needs user input before autonomous delivery."
    next_step = suggested_next_steps[0] if suggested_next_steps else "Review the contract readiness result."
    note = "Do not start `sikula run` until `ready_to_run` is true."
    if ready_to_run:
        note = "Readiness applies to the returned Markdown hash."
    return "\n".join(
        [
            f"Status: {active_check.status.upper()} ({active_check.readiness_score}/100)",
            f"Changed: {changed}",
            f"Next step: {next_step}",
            f"Required action: {required_user_action}",
            f"Note: {note}",
        ]
    )


def _prepare_anti_loop_guidance() -> dict[str, Any]:
    return {
        "max_prepare_attempts_without_new_user_input": 1,
        "on_repeated_question_ids": "Ask the user for revised answers; do not keep improving automatically.",
    }


def _validation_commands_from_prepare_context(project_context: PrepareProjectContext | None) -> list[str] | None:
    if project_context is None:
        return None
    return list(project_context.validation_commands) or None


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


def _is_markdown_path(path: Path) -> bool:
    return path.suffix.lower() in {".md", ".markdown"}


def _load_contract_answers(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid contract answers YAML: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Contract answers file must contain a mapping: {path}")
    if data.get("generated_by") != "sikula.contract_check":
        raise ValueError(f"Contract answers file was not generated by sikula contract check: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported contract answers schema version: {data.get('schema_version')!r}")
    return data


def load_contract_answers_for_task(path: Path, result: ContractCheckResult) -> dict[str, Any]:
    """Load answers YAML and verify that it belongs to the checked task revision."""

    data = _load_contract_answers(path)
    _answers_questions(data)
    _answers_mapping(data)
    _validate_answers_for_task(data, result)
    return data


def _answers_questions(data: dict[str, Any]) -> list[dict[str, Any]]:
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Contract answers file is missing the questions list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str) or not question["id"]:
            raise ValueError("Contract answers file contains an invalid question entry")
        if question["id"] in seen:
            raise ValueError(f"Contract answers file contains duplicate question id: {question['id']}")
        seen.add(question["id"])
        normalized.append(question)
    return normalized


def _answers_mapping(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Contract answers file is missing the answers mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for question_id, answer in answers.items():
        if not isinstance(question_id, str) or not isinstance(answer, dict):
            raise ValueError("Contract answers file contains an invalid answer entry")
        normalized[question_id] = answer
    return normalized


def _validate_answers_for_task(data: dict[str, Any], result: ContractCheckResult) -> None:
    task = data.get("task")
    if not isinstance(task, dict):
        raise ValueError("Contract answers file is missing task metadata")
    expected_sha = str(result.source["sha256"])
    actual_sha = task.get("sha256")
    if actual_sha != expected_sha:
        raise ValueError(
            "Contract answers were generated for a different task revision "
            f"({actual_sha or 'missing hash'} != {expected_sha}); rerun `sikula contract check --write-report`"
        )


def _reject_unknown_filled_answers(
    answers: dict[str, dict[str, Any]],
    questions: list[dict[str, Any]],
) -> None:
    known_ids = {question["id"] for question in questions}
    for question_id, answer in answers.items():
        if question_id in known_ids:
            continue
        if _answer_text(answer) or _answer_notes(answer):
            raise ValueError(f"Contract answers contain a filled answer for an unknown question id: {question_id}")


def _answer_text(answer: dict[str, Any] | None) -> str:
    if not isinstance(answer, dict):
        return ""
    value = answer.get("answer", "")
    return str(value).strip() if value is not None else ""


def _answer_notes(answer: dict[str, Any] | None) -> str:
    if not isinstance(answer, dict):
        return ""
    value = answer.get("notes", "")
    return str(value).strip() if value is not None else ""


def _render_improved_contract(
    task_text: str,
    task_path: Path,
    questions: list[dict[str, Any]],
    answers: dict[str, dict[str, Any]],
    *,
    asset_references: list[dict[str, Any]] | None = None,
    generated_answer_entries: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], list[str]]:
    answered_question_ids = [question["id"] for question in questions if _answer_text(answers.get(question["id"], {}))]
    task_text = _strip_generated_answer_entries(task_text, answered_question_ids)
    task_text = _strip_tracked_generated_answer_entries(
        task_text,
        answered_question_ids,
        generated_answer_entries or [],
    )
    if _answer_text(answers.get("assets.local_files", {})):
        task_text = _strip_unresolved_asset_reference_lines(task_text, asset_references or [])
    task_text = _strip_generated_open_questions_section(task_text)
    lines = _improved_contract_base_lines(task_text, task_path)
    section_entries: dict[str, list[tuple[dict[str, Any], str, str]]] = {}
    answered_notes: list[tuple[str, str]] = []
    answered_ids: list[str] = []
    open_ids: list[str] = []

    for question in questions:
        question_id = question["id"]
        answer = answers.get(question_id, {})
        answer_text = _answer_text(answer)
        notes = _answer_notes(answer)
        if answer_text:
            answered_ids.append(question_id)
            if question_id.startswith("assets."):
                if notes:
                    answered_notes.append((_contract_note_label_for_question(question_id, "Assets"), notes))
                continue
            section = _contract_section_for_question(question_id)
            section_entries.setdefault(section, []).append((question, answer_text, notes))
            if notes:
                answered_notes.append((_contract_note_label_for_question(question_id, section), notes))
        else:
            open_ids.append(question_id)

    for section in _ordered_improved_sections(section_entries):
        entries = section_entries[section]
        if not entries:
            continue
        lines.extend(["", f"## {section}", ""])
        for question, answer_text, _notes in entries:
            _append_answer_entry(lines, question["id"], section, answer_text)

    asset_answer_entries = _asset_answer_entry_lines(asset_references or [], answers)
    if asset_answer_entries:
        _insert_or_append_section_entries(lines, "Assets", asset_answer_entries)

    _append_open_questions(lines, [question for question in questions if question["id"] in open_ids], answers)

    if answered_notes:
        lines.extend(["", "## Notes", ""])
        for label, notes in answered_notes:
            lines.append(f"- {label}: {_single_line(notes)}")

    return "\n".join(lines).rstrip() + "\n", answered_ids, open_ids


def _asset_answer_entry_lines(asset_references: list[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []

    local_files_answer = _answer_text(answers.get("assets.local_files", {}))
    if local_files_answer:
        replacement_kind = _asset_replacement_kind(asset_references)
        lines.append(_generated_answer_entry_marker("assets.local_files"))
        for answer_line in _answer_lines(local_files_answer):
            cleaned = _clean_answer_bullet(answer_line)
            if cleaned:
                lines.append(_asset_local_file_answer_line(cleaned, replacement_kind))
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    intent_answer = _answer_text(answers.get("assets.intent", {}))
    if intent_answer:
        question_id = "assets.intent"
        lines.append(_generated_answer_entry_marker(question_id))
        for answer_line in _answer_lines(intent_answer):
            lines.append(f"- {_clean_answer_bullet(answer_line)}")
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    provenance = _answer_text(answers.get("assets.provenance", {}))
    if provenance:
        delivery_refs = [
            reference
            for reference in asset_references
            if reference.get("kind") == "delivery" and not reference.get("source_license")
        ]
        lines.append(_generated_answer_entry_marker("assets.provenance"))
        if delivery_refs:
            provenance_text = _single_line(provenance)
            for reference in delivery_refs:
                project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
                if not project_path:
                    continue
                lines.append(f"- `{project_path}`")
                lines.append(f"  - Source/license: {provenance_text}")
        else:
            for answer_line in _answer_lines(provenance):
                lines.append(f"- {_clean_answer_bullet(answer_line)}")
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    return lines


def _strip_unresolved_asset_reference_lines(task_text: str, asset_references: list[dict[str, Any]]) -> str:
    source_lines = task_text.splitlines()
    unresolved_line_numbers = [
        int(reference["line"])
        for reference in asset_references
        if reference.get("status") in {"missing", "outside_project", "not_file"}
        and isinstance(reference.get("line"), int)
        and 1 <= int(reference["line"]) <= len(source_lines)
    ]
    if not unresolved_line_numbers:
        return task_text

    ranges = [_asset_reference_line_range(source_lines, line_number - 1) for line_number in unresolved_line_numbers]
    ranges = _merge_line_ranges(ranges)
    stripped = _remove_line_ranges(source_lines, ranges)
    return "\n".join(stripped).strip()


def _asset_reference_line_range(source_lines: list[str], line_index: int) -> tuple[int, int]:
    line = source_lines[line_index]
    bullet_match = _BULLET_RE.match(line)
    if not bullet_match:
        return line_index, line_index + 1

    base_indent = _line_indent(line)
    end_index = line_index + 1
    while end_index < len(source_lines):
        candidate = source_lines[end_index]
        if _HEADING_RE.match(candidate):
            break
        if candidate.strip() and _BULLET_RE.match(candidate) and _line_indent(candidate) <= base_indent:
            break
        end_index += 1
    return line_index, end_index


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _remove_line_ranges(source_lines: list[str], ranges: list[tuple[int, int]]) -> list[str]:
    def removed(line_index: int) -> bool:
        return any(start <= line_index < end for start, end in ranges)

    return [line for index, line in enumerate(source_lines) if not removed(index)]


def _asset_replacement_kind(asset_references: list[dict[str, Any]]) -> str:
    unresolved_kinds = [
        str(reference.get("kind") or "").strip()
        for reference in asset_references
        if reference.get("status") in {"missing", "outside_project", "not_file"}
    ]
    if unresolved_kinds and all(kind == "delivery" for kind in unresolved_kinds):
        return "delivery"
    if unresolved_kinds and all(kind == "reference" for kind in unresolved_kinds):
        return "reference"
    return "asset"


def _asset_local_file_answer_line(answer_line: str, replacement_kind: str) -> str:
    if _ASSET_REFERENCE_HINT_RE.search(answer_line) or _ASSET_DELIVERY_HINT_RE.search(answer_line):
        return f"- {answer_line}"

    label = {
        "delivery": "Delivery asset",
        "reference": "Reference asset",
    }.get(replacement_kind, "Asset")
    value = answer_line
    if _normalize_asset_path_candidate(answer_line):
        value = f"`{answer_line}`"
    return f"- {label}: {value}"


def _reconcile_rendered_open_questions(
    rendered: str,
    active_questions: list[ClarifyingQuestion],
    answers: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    question_dicts = _question_dicts(active_questions)
    rendered = _strip_generated_answer_entries(rendered, [question["id"] for question in question_dicts])
    lines = _strip_generated_open_questions_section(rendered).splitlines()
    _append_open_questions(lines, question_dicts, answers)
    return "\n".join(lines).rstrip() + "\n", [question["id"] for question in question_dicts]


def _append_open_questions(
    lines: list[str],
    questions: list[dict[str, Any]],
    answers: dict[str, dict[str, Any]],
) -> None:
    if not questions:
        return

    lines.extend(["", "## Open questions", "", _GENERATED_OPEN_QUESTIONS_MARKER, ""])
    for question in questions:
        lines.append(f"- {question.get('question', '')}")
        why = str(question.get("why_it_matters", "")).strip()
        if why:
            lines.append(f"  - Why it matters: {why}")
        notes = _answer_notes(answers.get(question["id"], {}))
        if notes:
            lines.append(f"  - Notes: {_single_line(notes)}")
        lines.append(f"  - Blocks delivery: {'yes' if question.get('blocks_delivery') else 'no'}")


def _strip_generated_open_questions_section(task_text: str) -> str:
    source_lines = task_text.splitlines()
    lines: list[str] = []
    index = 0

    while index < len(source_lines):
        line = source_lines[index]
        heading_match = _HEADING_RE.match(line)
        if heading_match and _normalize_heading(heading_match.group(2)) == "open questions":
            section_end = index + 1
            while section_end < len(source_lines) and not _HEADING_RE.match(source_lines[section_end]):
                section_end += 1
            section_body = source_lines[index + 1 : section_end]
            if _is_generated_open_questions_section(section_body):
                index = section_end
                continue
            lines.extend(source_lines[index:section_end])
            index = section_end
            continue

        lines.append(line)
        index += 1

    return "\n".join(lines).strip()


def _strip_generated_answer_entries(task_text: str, question_ids: list[str]) -> str:
    target_ids = set(question_ids)
    if not target_ids:
        return task_text.strip()

    source_lines = task_text.splitlines()
    lines: list[str] = []
    index = 0
    target_sections = {_normalize_heading(_contract_section_for_question(question_id)) for question_id in target_ids}

    while index < len(source_lines):
        line = source_lines[index]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            section_end = index + 1
            while section_end < len(source_lines) and not _HEADING_RE.match(source_lines[section_end]):
                section_end += 1
            section_body = source_lines[index + 1 : section_end]
            normalized_heading = _normalize_heading(heading_match.group(2))
            if normalized_heading in target_sections:
                stripped_body, removed_marked_entry = _strip_marked_answer_entries(section_body, target_ids)
                if removed_marked_entry:
                    stripped_body = _trim_blank_lines(stripped_body)
                    if stripped_body:
                        lines.append(line)
                        if stripped_body[0].strip():
                            lines.append("")
                        lines.extend(stripped_body)
                        if stripped_body[-1].strip():
                            lines.append("")
                    index = section_end
                    continue
            lines.extend(source_lines[index:section_end])
            index = section_end
            continue

        lines.append(line)
        index += 1

    return "\n".join(lines).strip()


def _strip_tracked_generated_answer_entries(
    task_text: str,
    question_ids: list[str],
    entries: list[dict[str, Any]],
) -> str:
    source_lines = task_text.splitlines()
    ranges = _validated_generated_answer_ranges(source_lines, question_ids, entries)
    if not ranges:
        return task_text.strip()

    return _remove_generated_answer_ranges(source_lines, ranges)


def _validated_generated_answer_ranges(
    source_lines: list[str],
    question_ids: list[str],
    entries: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    target_ids = set(question_ids)
    ranges: list[tuple[int, int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        question_id = str(entry.get("question_id") or "").strip()
        if question_id not in target_ids:
            continue
        try:
            start = int(entry.get("start_line", 0)) - 1
            end = int(entry.get("end_line", 0))
        except (TypeError, ValueError):
            continue
        if start < 0 or end <= start or end > len(source_lines):
            continue
        body_lines = source_lines[start:end]
        if str(entry.get("body_sha256") or "") != _lines_sha256(body_lines):
            continue
        ranges.append((start, end))
    return sorted(set(ranges))


def _remove_generated_answer_ranges(source_lines: list[str], ranges: list[tuple[int, int]]) -> str:
    def should_remove(line_index: int) -> bool:
        return any(start <= line_index < end for start, end in ranges)

    lines: list[str] = []
    index = 0
    while index < len(source_lines):
        if should_remove(index):
            index += 1
            continue

        line = source_lines[index]
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            section_end = index + 1
            while section_end < len(source_lines) and not _HEADING_RE.match(source_lines[section_end]):
                section_end += 1
            if any(start < section_end and end > index for start, end in ranges):
                stripped_body = _trim_blank_lines(
                    [
                        source_lines[body_index]
                        for body_index in range(index + 1, section_end)
                        if not should_remove(body_index)
                    ]
                )
                if stripped_body:
                    lines.append(line)
                    if stripped_body[0].strip():
                        lines.append("")
                    lines.extend(stripped_body)
                    if stripped_body[-1].strip():
                        lines.append("")
                index = section_end
                continue

        lines.append(line)
        index += 1

    return "\n".join(lines).strip()


def _strip_marked_answer_entries(section_body: list[str], target_ids: set[str]) -> tuple[list[str], bool]:
    lines: list[str] = []
    index = 0
    removed = False

    while index < len(section_body):
        line = section_body[index]
        marker_match = _GENERATED_ANSWER_ENTRY_RE.match(line)
        if not marker_match:
            lines.append(line)
            index += 1
            continue

        marker_ids = {value.strip() for value in marker_match.group(1).split(",") if value.strip()}
        remove_entry = bool(marker_ids & target_ids)
        entry_end = index + 1
        while entry_end < len(section_body) and section_body[entry_end].strip() != _GENERATED_ANSWER_ENTRY_END_MARKER:
            entry_end += 1
        if entry_end < len(section_body):
            entry_end += 1
        if remove_entry:
            removed = True
        else:
            lines.extend(section_body[index:entry_end])
        index = entry_end

    return lines, removed


def _strip_generated_markers(task_text: str) -> str:
    lines: list[str] = []
    source_lines = task_text.splitlines()
    index = 0
    while index < len(source_lines):
        line = source_lines[index]
        stripped = line.strip()
        if _GENERATED_ANSWER_ENTRY_RE.match(line):
            index += 1
            continue
        if stripped == _GENERATED_ANSWER_ENTRY_END_MARKER:
            index += 1
            continue
        if stripped == _GENERATED_OPEN_QUESTIONS_MARKER:
            if index + 1 < len(source_lines) and not source_lines[index + 1].strip():
                index += 2
                continue
            index += 1
            continue
        lines.append(line)
        index += 1
    return "\n".join(lines).rstrip() + "\n"


def _trim_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _is_generated_open_questions_section(section_body: list[str]) -> bool:
    stripped = [line.strip() for line in section_body if line.strip()]
    if _GENERATED_OPEN_QUESTIONS_MARKER in stripped:
        return True
    if not stripped:
        return False

    saw_question = False
    current_has_blocks_delivery = False
    for line in section_body:
        if not line.strip():
            continue
        if re.match(r"^[-*+]\s+", line):
            if saw_question and not current_has_blocks_delivery:
                return False
            saw_question = True
            current_has_blocks_delivery = False
            continue
        if re.match(r"^\s{2,}[-*+]\s+(Why it matters|Notes|Blocks delivery):", line):
            if "Blocks delivery:" in line:
                current_has_blocks_delivery = True
            continue
        return False

    return saw_question and current_has_blocks_delivery


def _improved_contract_base_lines(task_text: str, task_path: Path) -> list[str]:
    task_text = task_text.strip()
    if _is_markdown_path(task_path) and task_text.startswith("#"):
        return task_text.splitlines()
    return [
        "# Improved implementation contract",
        "",
        "## Original request",
        "",
        *task_text.splitlines(),
    ]


def _contract_section_for_question(question_id: str) -> str:
    if question_id == "product.goal":
        return "Goal"
    if question_id == "context.product":
        return "Product context"
    if question_id == "project_context.details":
        return "Project context"
    if question_id == "project_context.validation_commands":
        return "Validation"
    if question_id == "asset_manifest.references":
        return "Asset manifest"
    if question_id in {"scope.boundaries"}:
        return "Scope"
    if question_id in {"acceptance.criteria", "acceptance.negative_cases"}:
        return "Acceptance criteria"
    if question_id == "scope.out_of_scope":
        return "Out of scope"
    if question_id.startswith(("permissions.", "token.", "privacy.", "security.")):
        return "Security and privacy"
    if question_id == "validation.commands":
        return "Validation"
    if question_id == "reviewer.focus":
        return "Reviewer focus"
    if question_id == "context.domain_rules":
        return "Context"
    return "Clarifications"


def _contract_note_label_for_question(question_id: str, section: str) -> str:
    labels = {
        "scope.boundaries": "Scope",
        "acceptance.criteria": "Acceptance criteria",
        "acceptance.negative_cases": "Negative cases",
        "scope.out_of_scope": "Out of scope",
        "permissions.authorization_model": "Authorization",
        "token.lifecycle": "Token lifecycle",
        "privacy.data_handling": "Data handling",
        "security.privacy_impact": "Security and privacy",
        "validation.commands": "Validation",
        "reviewer.focus": "Reviewer focus",
        "context.domain_rules": "Context",
        "assets.local_files": "Assets",
        "assets.intent": "Asset intent",
        "assets.provenance": "Asset provenance",
    }
    return labels.get(question_id, section)


def _ordered_improved_sections(section_entries: dict[str, list[tuple[dict[str, Any], str, str]]]) -> list[str]:
    preferred = [
        "Scope",
        "Acceptance criteria",
        "Out of scope",
        "Security and privacy",
        "Validation",
        "Reviewer focus",
        "Context",
        "Clarifications",
    ]
    return [section for section in preferred if section in section_entries] + sorted(
        section for section in section_entries if section not in preferred
    )


def _append_answer_entry(
    lines: list[str],
    question_id: str,
    section: str,
    answer_text: str,
) -> None:
    lines.append(_generated_answer_entry_marker(question_id))
    if section == "Validation":
        for command in _answer_lines(answer_text):
            lines.append(f"- `{_clean_answer_bullet(command).strip('`')}`")
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)
        return

    for answer_line in _answer_lines(answer_text):
        lines.append(f"- {_clean_answer_bullet(answer_line)}")
    lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)


def _generated_answer_entry_marker(question_id: str) -> str:
    return f"<!-- sikula:generated-answer: {question_id} -->"


def _answer_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _clean_answer_bullet(value: str) -> str:
    return re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", value).strip()


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _contract_report_dir(
    task_path: Path,
    *,
    project_root: Path | None,
    report_dir: Path | str | None = None,
) -> Path:
    if report_dir is not None:
        path = Path(report_dir)
        if path.is_absolute():
            return path.resolve()
        base = project_root.resolve() if project_root is not None else Path.cwd().resolve()
        return (base / path).resolve()

    if project_root is not None:
        return project_root.resolve() / DEFAULT_CONTRACT_REPORT_DIR

    for parent in task_path.parents:
        if parent.name == ".sikula":
            return parent / "contract-reports"
        if parent.name == "tasks" and parent.parent.name == ".sikula":
            return parent.parent / "contract-reports"
    return Path.cwd().resolve() / DEFAULT_CONTRACT_REPORT_DIR


def _artifact_base_dir(*, project_root: Path | None, contract_dir: Path) -> Path:
    if project_root is not None:
        return project_root.resolve()
    if contract_dir.name == "contract-reports" and contract_dir.parent.name == ".sikula":
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


def _load_generated_answer_entries(
    task_path: Path,
    *,
    source_sha256: str,
    contract_dir: Path,
    artifact_base: Path,
) -> list[dict[str, Any]]:
    try:
        path = _contract_generated_answers_path(task_path, contract_dir, artifact_base)
    except FileExistsError:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("generated_by") != "sikula.contract_prepare":
        return []
    if not _task_path_matches(data.get("task"), task_path, artifact_base):
        return []
    task = data.get("task")
    if not isinstance(task, dict) or task.get("sha256") != source_sha256:
        return []
    entries = data.get("generated_answers")
    return entries if isinstance(entries, list) else []


def _write_generated_answer_entries(
    output_path: Path,
    *,
    markdown: str,
    resume_markdown: str,
    generated_answers_path: Path,
    artifact_base: Path,
) -> None:
    data = {
        "schema_version": 1,
        "generated_by": "sikula.contract_prepare",
        "created_at": _utc_timestamp(),
        "task": {
            "path": _artifact_path(output_path, artifact_base),
            "sha256": "sha256:" + sha256(markdown.strip().encode("utf-8")).hexdigest(),
        },
        "generated_answers": _generated_answer_entries_from_resume(
            resume_markdown,
            markdown,
        ),
    }
    generated_answers_path.parent.mkdir(parents=True, exist_ok=True)
    generated_answers_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _contract_generated_answers_path(task_path: Path, contract_dir: Path, artifact_base: Path) -> Path:
    stem = _select_generated_answers_stem(task_path, contract_dir, artifact_base)
    return contract_dir / f"{stem}{_GENERATED_ANSWERS_ARTIFACT_SUFFIX}"


def _select_generated_answers_stem(task_path: Path, contract_dir: Path, artifact_base: Path) -> str:
    base = _safe_report_stem(task_path.stem)
    candidate = contract_dir / f"{base}{_GENERATED_ANSWERS_ARTIFACT_SUFFIX}"
    if _generated_answers_available_for_task(candidate, task_path, artifact_base):
        return base

    hashed = f"{base}-{sha256(str(task_path).encode('utf-8')).hexdigest()[:8]}"
    hashed_candidate = contract_dir / f"{hashed}{_GENERATED_ANSWERS_ARTIFACT_SUFFIX}"
    if not _generated_answers_available_for_task(hashed_candidate, task_path, artifact_base):
        raise FileExistsError(
            f"Contract generated-answer metadata already exists for a different task: {hashed_candidate}"
        )
    return hashed


def _generated_answers_available_for_task(path: Path, task_path: Path, artifact_base: Path) -> bool:
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("generated_by") == "sikula.contract_prepare" and _task_path_matches(
        data.get("task"), task_path, artifact_base
    )


def _generated_answer_entries_from_resume(resume_markdown: str, clean_markdown: str) -> list[dict[str, Any]]:
    clean_lines = clean_markdown.splitlines()
    resume_lines = resume_markdown.splitlines()
    clean_index = 0
    resume_index = 0
    entries: list[dict[str, Any]] = []

    while resume_index < len(resume_lines):
        line = resume_lines[resume_index]
        marker_match = _GENERATED_ANSWER_ENTRY_RE.match(line)
        if marker_match:
            question_ids = [value.strip() for value in marker_match.group(1).split(",") if value.strip()]
            body_start = clean_index
            body_lines: list[str] = []
            resume_index += 1
            while (
                resume_index < len(resume_lines)
                and resume_lines[resume_index].strip() != _GENERATED_ANSWER_ENTRY_END_MARKER
            ):
                body_lines.append(resume_lines[resume_index])
                clean_index += 1
                resume_index += 1
            if resume_index < len(resume_lines):
                resume_index += 1
            body_end = body_start + len(body_lines)
            if not body_lines or clean_lines[body_start:body_end] != body_lines:
                continue
            for question_id in question_ids:
                entries.append(
                    {
                        "question_id": question_id,
                        "section": _contract_section_for_question(question_id),
                        "start_line": body_start + 1,
                        "end_line": body_end,
                        "line_count": len(body_lines),
                        "body_sha256": _lines_sha256(body_lines),
                    }
                )
            continue

        stripped = line.strip()
        if stripped == _GENERATED_OPEN_QUESTIONS_MARKER:
            resume_index += 1
            if resume_index < len(resume_lines) and not resume_lines[resume_index].strip():
                resume_index += 1
            continue

        if stripped == _GENERATED_ANSWER_ENTRY_END_MARKER:
            resume_index += 1
            continue

        clean_index += 1
        resume_index += 1
    return entries


def _lines_sha256(lines: list[str]) -> str:
    return "sha256:" + sha256("\n".join(lines).encode("utf-8")).hexdigest()


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


def _large_task_has_clear_delivery_boundary(scores: dict[str, int]) -> bool:
    return (
        scores["scope_clarity"] >= 75
        and scores["acceptance_criteria"] >= 75
        and scores["out_of_scope"] >= 80
        and scores["testability"] >= 65
        and scores["validation"] >= 65
    )


def _score_task_size(parsed: _ParsedTask, scores: dict[str, int]) -> int:
    if parsed.word_count < 8:
        return 15
    if parsed.word_count < 35:
        return 55
    if parsed.word_count <= 650:
        return 90
    if parsed.word_count <= 1000:
        return 65
    if _large_task_has_clear_delivery_boundary(scores):
        return 85
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


def _validation_details(
    text: str,
    project_config: dict | None,
    explicit_validation_commands: list[str] | None,
) -> dict[str, Any]:
    task_commands = extract_validation_commands(text)
    configured_commands: list[dict[str, str]] = []
    coverage_gaps: list[str] = []
    covered_commands: list[dict[str, Any]] = []

    if explicit_validation_commands:
        configured_commands.extend(
            {
                "phase": "project_context",
                "name": f"validation-{index}",
                "command": command,
            }
            for index, command in enumerate(explicit_validation_commands, start=1)
            if command
        )
    if project_config:
        state = TaskState(task_id="contract_check", task_description=text)
        configured_commands.extend(configured_validation_commands(project_config, state))
    if configured_commands:
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


def _detect_asset_references(
    text: str,
    *,
    source_path: Path | str | None,
    project_config: dict | None,
) -> list[dict[str, Any]]:
    project_root = _asset_project_root(source_path, project_config)
    references_by_path: dict[str, dict[str, Any]] = {}
    reference_order: list[str] = []
    current_heading = ""
    lines = text.splitlines()

    for line_index, line in enumerate(lines):
        line_number = line_index + 1
        markdown_heading = _HEADING_RE.match(line)
        text_heading = _TEXT_HEADING_RE.match(line)
        if markdown_heading:
            current_heading = markdown_heading.group(2).strip()
        elif text_heading:
            current_heading = text_heading.group(1).strip()

        for raw_path in _asset_path_candidates(line):
            normalized_path = _normalize_asset_path_candidate(raw_path)
            if not normalized_path:
                continue
            context = _asset_reference_context(current_heading, lines, line_index)
            if not _asset_reference_input_context(normalized_path, context, project_config):
                continue
            reference = _asset_reference_metadata(
                normalized_path,
                project_root=project_root,
                line_number=line_number,
                context=context,
            )
            if reference is None:
                continue
            existing = references_by_path.get(normalized_path)
            if existing is None:
                references_by_path[normalized_path] = reference
                reference_order.append(normalized_path)
            else:
                _merge_asset_reference(existing, reference)
    return [references_by_path[path] for path in reference_order]


def _asset_reference_context(current_heading: str, lines: list[str], line_index: int) -> str:
    local_lines = _asset_reference_context_lines(lines, line_index)
    return "\n".join([current_heading, *local_lines])


def _asset_reference_context_lines(lines: list[str], line_index: int) -> list[str]:
    line = lines[line_index]
    bullet_match = _BULLET_RE.match(line)
    if not bullet_match:
        return [line]

    base_indent = _line_indent(line)
    context_lines = [line]
    next_index = line_index + 1
    while next_index < len(lines):
        candidate = lines[next_index]
        if _HEADING_RE.match(candidate):
            break
        if (
            candidate.strip()
            and _BULLET_RE.match(candidate)
            and _line_indent(candidate) <= base_indent
            and _asset_path_candidates(candidate)
        ):
            break
        context_lines.append(candidate)
        next_index += 1
    return context_lines


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _asset_path_candidates(line: str) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for pattern in (_MARKDOWN_LINK_RE, _CODE_SPAN_RE, _PLAIN_ASSET_PATH_RE):
        matches.extend((match.start(1), match.end(1), match.group(1)) for match in pattern.finditer(line))
    matches.sort(key=lambda item: item[0])

    candidates: list[str] = []
    previous_asset_start: int | None = None
    previous_asset_end: int | None = None
    for start_index, end_index, value in matches:
        if _asset_candidate_is_target_path(line, start_index):
            continue
        if _asset_candidate_is_destination_path(
            line,
            start_index,
            previous_asset_start=previous_asset_start,
            previous_asset_end=previous_asset_end,
        ):
            continue
        candidates.append(value)
        previous_asset_start = start_index
        previous_asset_end = end_index
    return candidates


def _asset_candidate_is_target_path(line: str, start_index: int) -> bool:
    prefix = line[:start_index].lower().rstrip("`'\" ")
    return bool(re.search(r"\b(target|destination|copy to)\s*:\s*$", prefix))


def _asset_candidate_is_destination_path(
    line: str,
    start_index: int,
    *,
    previous_asset_start: int | None,
    previous_asset_end: int | None,
) -> bool:
    if previous_asset_start is None or previous_asset_end is None:
        return False
    transfer_prefix = line[:previous_asset_start].casefold()
    if not re.search(r"\b(copy|move|place|install|save|write|add|include)\b", transfer_prefix):
        return False
    between_paths = line[previous_asset_end:start_index].casefold().strip(" `\"'")
    return bool(re.fullmatch(r"(?:to|into|under|at|as)\s*", between_paths))


def _merge_asset_reference(existing: dict[str, Any], update: dict[str, Any]) -> None:
    if existing.get("kind") == "ambiguous" and update.get("kind") in {"delivery", "reference"}:
        existing["kind"] = update["kind"]
    for key in (
        "target_specified",
        "requested_target",
        "provenance_specified",
        "source_license",
        "mime_type",
        "sha256",
        "size_bytes",
        "git_status",
    ):
        if update.get(key) and not existing.get(key):
            existing[key] = update[key]


def _normalize_asset_path_candidate(raw_path: str) -> str | None:
    candidate = unquote(raw_path.strip().strip("\"'<>"))
    candidate = candidate.rstrip(".,;:")
    if not candidate or candidate.startswith("#"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() not in {"file"}:
        return None
    if parsed.scheme.lower() == "file":
        candidate = parsed.path
    if not _asset_candidate_has_supported_extension(candidate):
        return None
    if not _asset_candidate_has_directory(candidate):
        return None
    return candidate


def _asset_candidate_has_supported_extension(path: str) -> bool:
    return Path(path).suffix.lower() in _ASSET_EXTENSIONS


def _asset_candidate_has_directory(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_absolute() or "/" in path or "\\" in path


def _asset_reference_input_context(path_text: str, context: str, project_config: dict | None) -> bool:
    heading = context.splitlines()[0] if context else ""
    normalized_heading = _normalize_heading(heading)
    if "asset" in normalized_heading or "attachment" in normalized_heading:
        return True
    if _asset_path_in_task_asset_dir(path_text, project_config):
        return True
    return bool(
        _ASSET_REFERENCE_HINT_RE.search(context)
        or _ASSET_STRONG_DELIVERY_HINT_RE.search(context)
        or _ASSET_PROVENANCE_HINT_RE.search(context)
    )


def _asset_path_in_task_asset_dir(path_text: str, project_config: dict | None) -> bool:
    normalized_path = Path(path_text).as_posix().lstrip("./")
    configured_dirs = [".sikula/task-assets"]
    tasks = project_config.get("tasks") if isinstance(project_config, dict) else None
    task_asset_dir = tasks.get("task_asset_dir") if isinstance(tasks, dict) else None
    if isinstance(task_asset_dir, str) and task_asset_dir.strip():
        configured_dirs.append(task_asset_dir.strip())
    for configured_dir in configured_dirs:
        normalized_dir = Path(configured_dir).as_posix().strip("/").lstrip("./")
        if normalized_dir and (normalized_path == normalized_dir or normalized_path.startswith(normalized_dir + "/")):
            return True
    return False


def _asset_project_root(source_path: Path | str | None, project_config: dict | None) -> Path:
    if project_config:
        project = project_config.get("project") if isinstance(project_config.get("project"), dict) else {}
        root = project.get("root_path") if isinstance(project, dict) else None
        if isinstance(root, str) and root.strip():
            return Path(root).resolve()
    if source_path is not None:
        try:
            path = Path(str(source_path)).resolve()
            for parent in path.parents:
                if parent.name == "tasks" and parent.parent.name == ".sikula":
                    return parent.parent.parent.resolve()
                if parent.name == "contracts" and parent.parent.name == ".sikula":
                    return parent.parent.parent.resolve()
                if parent.name == ".sikula":
                    return parent.parent.resolve()
            if path.exists():
                return path.parent.resolve()
        except OSError:
            pass
    return Path.cwd().resolve()


def _asset_reference_metadata(
    path_text: str,
    *,
    project_root: Path,
    line_number: int,
    context: str,
) -> dict[str, Any] | None:
    requested_path = Path(path_text)
    candidate_path = requested_path if requested_path.is_absolute() else project_root / requested_path
    resolved_path = candidate_path.resolve(strict=False)
    reference: dict[str, Any] = {
        "path": path_text,
        "line": line_number,
        "kind": _asset_reference_kind(context),
    }
    if _ASSET_TARGET_HINT_RE.search(context):
        reference["target_specified"] = True
        requested_target = _asset_reference_detail(context, _ASSET_TARGET_DETAIL_RE)
        if requested_target:
            reference["requested_target"] = requested_target
    if _ASSET_PROVENANCE_HINT_RE.search(context):
        reference["provenance_specified"] = True
        source_license = _asset_reference_detail(context, _ASSET_PROVENANCE_DETAIL_RE)
        if source_license:
            reference["source_license"] = source_license

    try:
        project_path = resolved_path.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        reference["status"] = "outside_project"
        return reference

    reference["project_path"] = project_path
    if not resolved_path.exists():
        reference["status"] = "missing"
        return reference
    if not resolved_path.is_file():
        reference["status"] = "not_file"
        return reference

    reference["status"] = "available"
    reference["sha256"] = _file_sha256(resolved_path)
    reference["size_bytes"] = resolved_path.stat().st_size
    mime_type, _encoding = mimetypes.guess_type(resolved_path.name)
    if mime_type:
        reference["mime_type"] = mime_type
    reference["git_status"] = _asset_git_status(project_root, project_path)
    return reference


def _asset_reference_kind(context: str) -> str:
    if _ASSET_REFERENCE_HINT_RE.search(context) and not _ASSET_STRONG_DELIVERY_HINT_RE.search(context):
        return "reference"
    if _ASSET_DELIVERY_HINT_RE.search(context):
        return "delivery"
    if _ASSET_REFERENCE_HINT_RE.search(context):
        return "reference"
    return "ambiguous"


def _asset_reference_detail(context: str, pattern: re.Pattern[str]) -> str | None:
    for line in context.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        value = _clean_answer_bullet(match.group(1)).strip("` ")
        return _single_line(value) or None
    return None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _asset_git_status(project_root: Path, project_path: str) -> str:
    if not (project_root / ".git").exists():
        return "unknown"
    status = _git_command_stdout(project_root, "status", "--porcelain", "--ignored", "--", project_path)
    status_line = status.stdout.strip().splitlines()[0] if status.returncode == 0 and status.stdout.strip() else ""
    if status_line.startswith("!!"):
        return "ignored"
    if status_line.startswith("??"):
        return "untracked"
    if status_line:
        return "dirty"
    if _git_command(project_root, "ls-files", "--error-unmatch", "--", project_path).returncode == 0:
        return "tracked"
    if _git_command(project_root, "check-ignore", "-q", "--", project_path).returncode == 0:
        return "ignored"
    return "untracked"


def _git_command_stdout(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


def _git_command(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=["git", *args], returncode=1)


def _build_gaps(
    parsed: _ParsedTask,
    sections_detected: dict[str, bool],
    scores: dict[str, int],
    validation: dict[str, Any],
    security_sensitive: bool,
    asset_references: list[dict[str, Any]],
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
    elif parsed.word_count > 1000 and not _large_task_has_clear_delivery_boundary(scores):
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
    gaps.extend(_asset_reference_gaps(asset_references))
    return gaps


def _asset_reference_gaps(asset_references: list[dict[str, Any]]) -> list[ContractGap]:
    gaps: list[ContractGap] = []
    if not asset_references:
        return gaps
    if any(reference.get("status") == "missing" for reference in asset_references):
        gaps.append(
            ContractGap(
                "gap.assets.missing",
                "blocking",
                "assets",
                "Referenced local asset files are missing.",
            )
        )
    if any(reference.get("status") == "outside_project" for reference in asset_references):
        gaps.append(
            ContractGap(
                "gap.assets.outside_project",
                "blocking",
                "assets",
                "Referenced asset paths must resolve inside the project boundary.",
            )
        )
    if any(reference.get("status") == "not_file" for reference in asset_references):
        gaps.append(
            ContractGap(
                "gap.assets.not_file",
                "blocking",
                "assets",
                "Referenced asset paths must point to files, not directories.",
            )
        )
    if any(reference.get("git_status") in {"dirty", "ignored", "untracked"} for reference in asset_references):
        gaps.append(
            ContractGap(
                "gap.assets.worktree_availability",
                "warning",
                "assets",
                "Referenced assets are not cleanly tracked by git and may be unavailable in isolated Sikula runs.",
            )
        )
    if any(reference.get("kind") == "ambiguous" for reference in asset_references):
        gaps.append(
            ContractGap(
                "gap.assets.intent",
                "warning",
                "assets",
                "Referenced asset purpose is ambiguous; mark assets as reference-only or delivery assets.",
            )
        )
    delivery_assets = [reference for reference in asset_references if reference.get("kind") == "delivery"]
    if any(not reference.get("provenance_specified") for reference in delivery_assets):
        gaps.append(
            ContractGap(
                "gap.assets.provenance",
                "blocking",
                "assets",
                "Delivery assets need explicit source, license, or provenance before autonomous delivery.",
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
    if {"gap.assets.missing", "gap.assets.outside_project", "gap.assets.not_file"} & gap_ids:
        add(
            ClarifyingQuestion(
                "assets.local_files",
                "Which local project files should Sikula use for the referenced assets?",
                "Sikula needs local, project-bound asset files so isolated runs and review see the same inputs.",
                True,
            )
        )
    if "gap.assets.intent" in gap_ids:
        add(
            ClarifyingQuestion(
                "assets.intent",
                "Which referenced assets are reference-only, and which should be used as delivery assets?",
                "Reference assets guide the implementation; delivery assets may be copied into production code.",
                False,
            )
        )
    if "gap.assets.provenance" in gap_ids:
        add(
            ClarifyingQuestion(
                "assets.provenance",
                "What source, license, or provenance applies to each delivery asset?",
                "The pipeline should not infer whether a production asset is legally safe to ship.",
                True,
            )
        )
    return questions


def _suggested_sections(gaps: list[ContractGap]) -> list[str]:
    labels_by_gap_id = {
        "gap.scope.boundaries": "Scope: describe exact in-scope behaviour and unchanged adjacent behaviour",
        "gap.acceptance.criteria": "Acceptance criteria: list observable behaviours that must be true",
        "gap.acceptance.negative_cases": "Acceptance criteria: add negative, edge-case, or rejection behaviour",
        "gap.scope.out_of_scope": "Out of scope: name adjacent changes that should not be made",
        "gap.security_privacy.impact": "Security and privacy: state authorization, token, data, or privacy constraints",
        "gap.security_privacy.section": "Security and privacy: note relevant sensitive-flow constraints",
        "gap.tests.testability": "Tests: describe behaviours that should be covered by tests",
        "gap.validation.coverage": "Validation: align task-described commands with the configured Sikula pipeline",
        "gap.validation.commands": "Validation: list required validation commands or rely on configured Sikula validation",
        "gap.review.reviewer_focus": "Reviewer focus: call out risky areas for human review",
        "gap.task_size.too_large": "Scope: split the task or narrow the autonomous delivery boundary",
        "gap.context.repo_context": "Context: name affected files, APIs, domain rules, or project conventions",
        "gap.assets.missing": "Assets: provide local project files for referenced assets",
        "gap.assets.outside_project": "Assets: move referenced files inside the project boundary",
        "gap.assets.not_file": "Assets: reference concrete files rather than directories",
        "gap.assets.worktree_availability": "Assets: track referenced files or document no-isolate availability",
        "gap.assets.intent": "Assets: label each asset as reference-only or delivery",
        "gap.assets.provenance": "Assets: record source, license, or provenance for delivery assets",
    }
    labels_by_category = {
        "scope": "Scope",
        "acceptance_criteria": "Acceptance criteria",
        "out_of_scope": "Out of scope",
        "security_privacy": "Security and privacy",
        "tests": "Tests",
        "validation": "Validation",
        "reviewer_focus": "Reviewer focus",
        "repo_context": "Context",
        "assets": "Assets",
    }
    sections: list[str] = []
    for gap in gaps:
        label = labels_by_gap_id.get(gap.id) or labels_by_category.get(gap.category)
        if label and label not in sections:
            sections.append(label)
    return sections


def _strong_signals(
    scores: dict[str, int],
    sections_detected: dict[str, bool],
    validation: dict[str, Any],
    asset_references: list[dict[str, Any]],
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
    if asset_references and all(reference.get("status") == "available" for reference in asset_references):
        signals.append("Referenced local assets are available and hashed.")
    return signals
