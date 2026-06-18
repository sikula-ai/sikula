"""Transport-facing implementation contract prepare response helpers."""

from __future__ import annotations

from typing import Any

from core.contract_check import prepare_implementation_contract

PREPARE_IMPLEMENTATION_CONTRACT_RESPONSE_SCHEMA_VERSION = 1


def prepare_implementation_contract_response(
    contract_markdown: str,
    *,
    contract_name: str | None = None,
    answers: dict[str, str | dict[str, Any]] | None = None,
    project_context: dict | None = None,
) -> dict[str, Any]:
    """Return a stable response shape for chat/MCP prepare workflows."""

    result = prepare_implementation_contract(
        contract_markdown,
        contract_name=contract_name,
        answers=answers,
        project_context=project_context,
    )
    return _prepare_response_from_core_result(result.to_dict())


def _prepare_response_from_core_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PREPARE_IMPLEMENTATION_CONTRACT_RESPONSE_SCHEMA_VERSION,
        "workflow": "prepare_implementation_contract",
        "stage": result["stage"],
        "needs_user_input": result["needs_user_input"],
        "required_next_step": result["required_next_step"],
        "answers_template": result["answers_template"],
        "resume_arguments": result["resume_arguments"],
        "authoritative_output_markdown": result["authoritative_output_markdown"],
        "unresolved_gaps": result["unresolved_gaps"],
        "suggested_next_steps": result["suggested_next_steps"],
        "user_questions": result["user_questions"],
        "ready_to_save": result["ready_to_save"],
        "ready_to_run": result["ready_to_run"],
        "primary_user_action": result["primary_user_action"],
        "required_user_action": result["required_user_action"],
        "assistant_response_markdown": result["assistant_response_markdown"],
        "status_applies_to_sha256": result["status_applies_to_sha256"],
        "safe_task_path": result["safe_task_path"],
        "ready_to_run_blockers": result["ready_to_run_blockers"],
        "answered_question_ids": result["answered_question_ids"],
        "open_question_ids": result["open_question_ids"],
        "revised_answer_question_ids": result["revised_answer_question_ids"],
        "anti_loop_guidance": result["anti_loop_guidance"],
        "check": result["check"],
        "recheck": result["recheck"],
    }
