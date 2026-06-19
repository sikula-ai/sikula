"""LLM-assisted product task-description refinement helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.contract_auto_prepare import load_auto_json_object
from core.contract_check import TaskDescriptionPrepareResult, prepare_task_description


@dataclass(frozen=True)
class TaskAutoRefineRequest:
    brief: str
    task_name: str | None
    product_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class TaskAutoRefineDraft:
    task_markdown: str
    input_language: str | None = None
    normalized_to_english: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskAutoRefineResult:
    result: TaskDescriptionPrepareResult
    normalized_task_markdown: str
    input_language: str | None = None
    normalized_to_english: bool = False
    warnings: list[str] = field(default_factory=list)


TaskAutoRefineProvider = Callable[[TaskAutoRefineRequest], TaskAutoRefineDraft]


def parse_task_auto_refine_output(output: str) -> TaskAutoRefineDraft:
    """Parse read-only LLM output for product task-description normalization."""

    payload = load_auto_json_object(output)
    raw_markdown = payload.get("task_markdown")
    if not isinstance(raw_markdown, str) or not raw_markdown.strip():
        raise ValueError("auto task refine output must contain non-empty task_markdown")
    task_markdown = raw_markdown.strip() + "\n"
    if "sikula:generated-" in task_markdown:
        raise ValueError("auto task refine output must not contain Sikula generated markers")

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


def auto_refine_task_description(
    brief: str,
    *,
    task_name: str | None = None,
    product_context: dict[str, Any] | None = None,
    answers: dict[str, dict[str, Any]] | None = None,
    normalize_provider: TaskAutoRefineProvider,
) -> TaskAutoRefineResult:
    """Normalize a raw task description, then run deterministic task preparation."""

    draft = normalize_provider(
        TaskAutoRefineRequest(
            brief=brief,
            task_name=task_name,
            product_context=product_context,
        )
    )
    result = prepare_task_description(
        draft.task_markdown,
        task_name=task_name,
        product_context=product_context,
        answers=answers,
    )
    return TaskAutoRefineResult(
        result=result,
        normalized_task_markdown=draft.task_markdown,
        input_language=draft.input_language,
        normalized_to_english=draft.normalized_to_english,
        warnings=draft.warnings,
    )


def _normalize_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
