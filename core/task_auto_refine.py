"""LLM-assisted product task-description refinement helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.contract_auto_prepare import AutoPreparationAuditRecorder, load_auto_json_object
from core.contract_check import TaskDescriptionPrepareResult, prepare_task_description
from core.task_assets import task_description_has_asset_manifest_section


@dataclass(frozen=True)
class TaskAutoRefineRequest:
    brief: str
    task_name: str | None
    product_context: dict[str, Any] | None = None
    asset_path_candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TaskAutoAnswerRequest:
    original_brief: str
    task_markdown: str
    task_name: str | None
    product_context: dict[str, Any] | None
    user_questions: list[dict[str, Any]]
    round_index: int


@dataclass(frozen=True)
class TaskAutoRefineDraft:
    task_markdown: str
    input_language: str | None = None
    normalized_to_english: bool = False
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TaskAutoRefineResult:
    result: TaskDescriptionPrepareResult
    normalized_task_markdown: str
    input_language: str | None = None
    normalized_to_english: bool = False
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    auto_answers: dict[str, dict[str, str]] = field(default_factory=dict)
    rounds: int = 0
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TaskAutoAnswerBatch:
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    unanswered: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


TaskAutoRefineProvider = Callable[[TaskAutoRefineRequest], TaskAutoRefineDraft]
TaskAutoAnswerProvider = Callable[[TaskAutoAnswerRequest], TaskAutoAnswerBatch]


def parse_task_auto_refine_output(output: str) -> TaskAutoRefineDraft:
    """Parse read-only LLM output for product task-description normalization."""

    payload = load_auto_json_object(output)
    raw_markdown = payload.get("task_markdown")
    if not isinstance(raw_markdown, str) or not raw_markdown.strip():
        raise ValueError("auto task refine output must contain non-empty task_markdown")
    task_markdown = raw_markdown.strip() + "\n"
    if "sikula:generated-" in task_markdown:
        raise ValueError("auto task refine output must not contain Sikula generated markers")
    if task_description_has_asset_manifest_section(task_markdown):
        raise ValueError("auto task refine output must not contain an Asset manifest")

    input_language = payload.get("input_language")
    if input_language is not None:
        input_language = str(input_language).strip() or None
    normalized_to_english = bool(payload.get("normalized_to_english", False))
    warnings = _normalize_warnings(payload.get("warnings"))
    return TaskAutoRefineDraft(
        task_markdown=task_markdown,
        input_language=input_language,
        normalized_to_english=normalized_to_english,
        warnings=warnings,
    )


def parse_task_auto_answer_output(output: str, active_question_ids: set[str]) -> TaskAutoAnswerBatch:
    """Parse read-only LLM answer JSON for active product task-refinement questions."""

    payload = load_auto_json_object(output)
    raw_answers = payload.get("answers", {})
    if raw_answers is None:
        raw_answers = {}
    if not isinstance(raw_answers, dict):
        raise ValueError("auto task answer output must contain an answers object")

    answers: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    for question_id, raw_answer in raw_answers.items():
        if not isinstance(question_id, str) or not question_id.strip():
            warnings.append("ignored answer with invalid question id")
            continue
        question_id = question_id.strip()
        if question_id not in active_question_ids:
            warnings.append(f"ignored answer for inactive question id: {question_id}")
            continue
        normalized = _normalize_answer_entry(raw_answer)
        if normalized is None:
            warnings.append(f"ignored invalid answer for question id: {question_id}")
            continue
        if not normalized["answer"]:
            continue
        answers[question_id] = normalized

    unanswered = _normalize_unanswered(payload.get("unanswered"))
    raw_warnings = payload.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(item).strip() for item in raw_warnings if str(item).strip())
    elif raw_warnings is not None:
        warnings.append("ignored non-list warnings field")

    return TaskAutoAnswerBatch(answers=answers, unanswered=unanswered, warnings=warnings)


def auto_refine_task_description(
    brief: str,
    *,
    task_name: str | None = None,
    product_context: dict[str, Any] | None = None,
    asset_path_candidates: list[dict[str, Any]] | None = None,
    answers: dict[str, Any] | None = None,
    normalize_provider: TaskAutoRefineProvider,
    answer_provider: TaskAutoAnswerProvider | None = None,
    audit_recorder: AutoPreparationAuditRecorder | None = None,
    max_answer_rounds: int = 2,
) -> TaskAutoRefineResult:
    """Normalize a raw task description, then run deterministic task preparation."""

    draft = normalize_provider(
        TaskAutoRefineRequest(
            brief=brief,
            task_name=task_name,
            product_context=product_context,
            asset_path_candidates=list(asset_path_candidates or []),
        )
    )
    if audit_recorder:
        for record in draft.audit_records:
            audit_recorder(record)
    applied_answers = _normalize_answers(answers or {})
    human_answer_ids = {
        question_id for question_id, answer in applied_answers.items() if not _answer_entry_empty(answer)
    }
    result = prepare_task_description(
        draft.task_markdown,
        task_name=task_name,
        product_context=product_context,
        answers=applied_answers,
    )
    auto_answers: dict[str, dict[str, str]] = {}
    warnings = list(draft.warnings)
    audit_records = list(draft.audit_records)
    rounds = 0

    if answer_provider is not None:
        for round_index in range(max(0, max_answer_rounds)):
            if not result.user_questions:
                break
            active_ids = {
                str(question.get("id") or "").strip()
                for question in result.user_questions
                if isinstance(question, dict) and str(question.get("id") or "").strip()
            }
            if not active_ids:
                break

            request = TaskAutoAnswerRequest(
                original_brief=brief,
                task_markdown=result.prepared_task_markdown,
                task_name=task_name,
                product_context=product_context,
                user_questions=list(result.user_questions),
                round_index=round_index + 1,
            )
            batch = answer_provider(request)
            audit_records.extend(batch.audit_records)
            if audit_recorder:
                for record in batch.audit_records:
                    audit_recorder(record)
            warnings.extend(batch.warnings)
            new_answers = {
                question_id: answer
                for question_id, answer in batch.answers.items()
                if question_id in active_ids
                and question_id not in human_answer_ids
                and _answer_changed(applied_answers.get(question_id), answer)
            }
            if not new_answers:
                break

            rounds += 1
            applied_answers.update(new_answers)
            auto_answers.update(new_answers)
            result = prepare_task_description(
                draft.task_markdown,
                task_name=task_name,
                product_context=product_context,
                answers=applied_answers,
            )

    return TaskAutoRefineResult(
        result=result,
        normalized_task_markdown=draft.task_markdown,
        input_language=draft.input_language,
        normalized_to_english=draft.normalized_to_english,
        answers=applied_answers,
        auto_answers=auto_answers,
        rounds=rounds,
        warnings=warnings,
        audit_records=audit_records,
    )


def _normalize_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_answer_entry(raw_answer: Any) -> dict[str, str] | None:
    if isinstance(raw_answer, str):
        return {"answer": raw_answer.strip(), "notes": ""}
    if not isinstance(raw_answer, dict):
        return None
    answer = str(raw_answer.get("answer") or "").strip()
    notes = str(raw_answer.get("notes") or "").strip()
    return {"answer": answer, "notes": notes}


def _normalize_answers(raw_answers: dict[str, Any]) -> dict[str, dict[str, str]]:
    answers: dict[str, dict[str, str]] = {}
    for question_id, raw_answer in raw_answers.items():
        if not isinstance(question_id, str):
            continue
        normalized = _normalize_answer_entry(raw_answer)
        if normalized is None:
            continue
        answers[question_id] = normalized
    return answers


def _answer_entry_empty(existing: dict[str, Any] | None) -> bool:
    if existing is None:
        return True
    existing_answer = str(existing.get("answer") or "").strip()
    existing_notes = str(existing.get("notes") or "").strip()
    return not existing_answer and not existing_notes


def _answer_changed(existing: dict[str, Any] | None, new_answer: dict[str, str]) -> bool:
    if existing is None:
        return True
    existing_answer = str(existing.get("answer") or "").strip()
    existing_notes = str(existing.get("notes") or "").strip()
    return existing_answer != new_answer["answer"] or existing_notes != new_answer["notes"]


def _normalize_unanswered(raw_unanswered: Any) -> list[dict[str, str]]:
    if not isinstance(raw_unanswered, list):
        return []
    unanswered: list[dict[str, str]] = []
    for item in raw_unanswered:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if question_id:
            unanswered.append({"id": question_id, "reason": reason})
    return unanswered
