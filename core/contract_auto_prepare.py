"""LLM-assisted implementation-contract preparation helpers.

The helpers in this module are intentionally side-effect free. They let a caller
ask a read-only LLM agent for answers to currently active contract-preparation
questions, then apply those answers through the deterministic contract prepare
core. The LLM never writes Markdown directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable

from core.contract_check import ContractPrepareResult, prepare_implementation_contract


@dataclass(frozen=True)
class ContractAutoPrepareRequest:
    contract_markdown: str
    contract_name: str | None
    project_context: dict[str, Any] | None
    user_questions: list[dict[str, Any]]
    round_index: int


@dataclass(frozen=True)
class ContractAutoAnswerBatch:
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    unanswered: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ContractAutoPrepareResult:
    result: ContractPrepareResult
    answers: dict[str, dict[str, str]]
    auto_answers: dict[str, dict[str, str]]
    rounds: int
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


ContractAutoAnswerProvider = Callable[[ContractAutoPrepareRequest], ContractAutoAnswerBatch]
AutoPreparationAuditRecorder = Callable[[dict[str, Any]], None]


def parse_contract_auto_answer_output(output: str, active_question_ids: set[str]) -> ContractAutoAnswerBatch:
    """Parse read-only LLM answer JSON for active contract questions."""

    payload = load_auto_json_object(output)
    raw_answers = payload.get("answers", {})
    if raw_answers is None:
        raw_answers = {}
    if not isinstance(raw_answers, dict):
        raise ValueError("auto contract answer output must contain an answers object")

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

    return ContractAutoAnswerBatch(answers=answers, unanswered=unanswered, warnings=warnings)


def auto_prepare_implementation_contract(
    task_description_markdown: str,
    *,
    contract_name: str | None = None,
    project_context: dict[str, Any] | None = None,
    generated_answer_entries: list[dict[str, Any]] | None = None,
    initial_answers: dict[str, dict[str, Any]] | None = None,
    answer_provider: ContractAutoAnswerProvider,
    audit_recorder: AutoPreparationAuditRecorder | None = None,
    max_rounds: int = 2,
) -> ContractAutoPrepareResult:
    """Run bounded auto-answer rounds, then return the deterministic prepare result."""

    answers = _normalize_answers(initial_answers or {})
    auto_answers: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    audit_records: list[dict[str, Any]] = []
    result = prepare_implementation_contract(
        task_description_markdown,
        contract_name=contract_name,
        answers=answers,
        project_context=project_context,
        generated_answer_entries=generated_answer_entries,
    )
    if result.required_next_step == "provide_project_context" or not result.user_questions:
        return ContractAutoPrepareResult(
            result=result,
            answers=answers,
            auto_answers={},
            rounds=0,
            warnings=warnings,
            audit_records=audit_records,
        )

    rounds = 0
    for round_index in range(max(0, max_rounds)):
        if not result.user_questions or result.required_next_step == "provide_project_context":
            break
        active_ids = {
            str(question.get("id") or "").strip()
            for question in result.user_questions
            if isinstance(question, dict) and str(question.get("id") or "").strip()
        }
        if not active_ids:
            break

        request = ContractAutoPrepareRequest(
            contract_markdown=result.prepared_contract_markdown,
            contract_name=contract_name,
            project_context=project_context,
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
            if question_id in active_ids and _answer_changed(answers.get(question_id), answer)
        }
        if not new_answers:
            break

        rounds += 1
        answers.update(new_answers)
        auto_answers.update(new_answers)
        result = prepare_implementation_contract(
            task_description_markdown,
            contract_name=contract_name,
            answers=answers,
            project_context=project_context,
            generated_answer_entries=generated_answer_entries,
        )

    return ContractAutoPrepareResult(
        result=result,
        answers=answers,
        auto_answers=auto_answers,
        rounds=rounds,
        warnings=warnings,
        audit_records=audit_records,
    )


def load_auto_json_object(output: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        raise ValueError("auto LLM output is empty")
    if text.startswith("```"):
        text = _strip_fenced_json(text)

    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise ValueError("auto LLM output did not contain a JSON object")
    try:
        payload, end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError("auto LLM output is not valid JSON") from exc
    trailing = text[start + end :].strip()
    if "{" in trailing:
        raise ValueError("auto LLM output contains multiple JSON objects")
    if not isinstance(payload, dict):
        raise ValueError("auto LLM output must be a JSON object")
    return payload


def _strip_fenced_json(text: str) -> str:
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _normalize_answer_entry(raw_answer: Any) -> dict[str, str] | None:
    if isinstance(raw_answer, str):
        return {"answer": raw_answer.strip(), "notes": ""}
    if not isinstance(raw_answer, dict):
        return None
    answer = str(raw_answer.get("answer") or "").strip()
    notes = str(raw_answer.get("notes") or "").strip()
    return {"answer": answer, "notes": notes}


def _normalize_answers(raw_answers: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    answers: dict[str, dict[str, str]] = {}
    for question_id, raw_answer in raw_answers.items():
        if not isinstance(question_id, str) or not isinstance(raw_answer, dict):
            continue
        normalized = _normalize_answer_entry(raw_answer)
        if normalized is None:
            continue
        answers[question_id] = normalized
    return answers


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


def _answer_changed(existing: dict[str, Any] | None, new_answer: dict[str, str]) -> bool:
    if existing is None:
        return True
    existing_answer = str(existing.get("answer") or "").strip()
    existing_notes = str(existing.get("notes") or "").strip()
    return existing_answer != new_answer["answer"] or existing_notes != new_answer["notes"]
