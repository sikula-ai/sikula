"""Shared delivery-child prompt and disposition contracts for agents."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.delivery_constraint_context import delivery_constraint_prompt_context
from core.delivery_write_scope import (
    DeliveryWriteScope,
    DeliveryWriteScopeError,
    apply_delivery_write_scope_to_config,
    resolve_delivery_write_scope,
)
from core.state import TaskState
from core.structured_output import (
    DELIVERY_DISPOSITION_ALREADY_SATISFIED,
    DELIVERY_DISPOSITION_APPROVED,
    DELIVERY_REVIEW_DISPOSITIONS,
    DeliveryDisposition,
    DeliveryDispositionParseError,
    parse_delivery_disposition,
)

DeliveryAgentRole = Literal[
    "analyst",
    "implementer",
    "fixer",
    "reviewer",
    "security_reviewer",
]

_ANALYSIS_DISPOSITION_CONTRACT = """\

---
DELIVERY STOP OUTPUT CONTRACT:
If and only if codebase inspection proves that this unit cannot proceed without a
required change owned by an external repository or unavailable dependency, output
exactly one flat JSON object and no implementation prompt:
{"sikula_disposition_schema_version":1,"disposition":"external_dependency_gap","summary":"<one bounded single-line summary>"}

Do not emit this object for uncertainty, missing implementation detail that can be
resolved inside the current repository, or ordinary warnings. Never infer another
disposition value and never wrap the object in another JSON structure.\
"""

_IMPLEMENTATION_DISPOSITION_CONTRACT = """\
DELIVERY IMPLEMENTATION OUTCOME CONTRACT:
If and only if repository inspection proves that every requirement in the active task
or current step is already present and no file needs to change, make your final
response exactly one flat JSON object:
{"sikula_disposition_schema_version":1,"disposition":"already_satisfied","summary":"<bounded single-line project-relative evidence>"}

Use `already_satisfied` only for a clean no-change result. Do not use it after writing
files, merely because a previous unit or commit claims the work is complete, or when
any requirement remains uncertain. Sikula will still run its configured review,
security, test-writing, and validation gates.

If implementation cannot safely continue because a required change is owned by an
external repository or unavailable dependency, stop writing immediately and make your
final response exactly one flat JSON object:
{"sikula_disposition_schema_version":1,"disposition":"external_dependency_gap","summary":"<one bounded single-line summary>"}

This disposition remains required if you already made partial in-scope changes. Do not
emit it for ordinary implementation failures, uncertainty, or a
change that can be made within the current task and write scope. Never emit review-only
dispositions and never wrap the object in another JSON structure.
"""

_REVIEW_DISPOSITION_CONTRACT = """\
---
DELIVERY REVIEW DISPOSITION CONTRACT:
This contract replaces the generic APPROVED output instructions for a delivery child.
Every delivery-child review must finish with exactly one flat JSON object as its final
non-empty line. Do not write APPROVED, LGTM, or any other verdict after the object.

For approval with no blocking issues, omit `## Issues` and use:
{"sikula_disposition_schema_version":1,"disposition":"approved","summary":"No blocking correctness issues found."}

For review issues, keep the `## Issues` details and use one of these action values:
{"sikula_disposition_schema_version":1,"disposition":"fix_in_scope","summary":"<one bounded single-line summary>"}

- `approved`: no blocking issue exists and no fix is required.
- `fix_in_scope`: every reported correction fits the current effective unit scope.
- `requires_scope_amendment`: a required correction belongs to this repository but is
  outside the current effective unit scope.
- `external_dependency_gap`: a required correction is owned by an external repository
  or unavailable dependency.

When findings span categories, choose the stop-dominant disposition in this order:
`external_dependency_gap`, then `requires_scope_amendment`, then `fix_in_scope`.
Never use `approved` with `## Issues`. Do not infer additional values or wrap the
object in another JSON structure. Free-form wording is not a disposition.
"""

_SECURITY_REVIEW_DISPOSITION_CONTRACT = """\
---
DELIVERY SECURITY DISPOSITION CONTRACT:
This contract replaces the generic APPROVED output instructions for a delivery child.
Every delivery-child security review must finish with exactly one flat JSON object as
its final non-empty line. Do not write APPROVED, LGTM, or any other verdict after it.

For all-clear or warning-only output without blocking issues, use:
{"sikula_disposition_schema_version":1,"disposition":"approved","summary":"No blocking security issues found."}

For blocking output, keep the `## Security Issues` details and use one of these action values:
{"sikula_disposition_schema_version":1,"disposition":"fix_in_scope","summary":"<one bounded single-line summary>"}

- `approved`: no blocking security issue exists and no remediation is required.
- `fix_in_scope`: every remediation fits the current effective unit scope.
- `requires_scope_amendment`: a required remediation belongs to this repository but is
  outside the current effective unit scope.
- `external_dependency_gap`: a required remediation is owned by an external repository
  or unavailable dependency.

When findings span categories, choose the stop-dominant disposition in this order:
`external_dependency_gap`, then `requires_scope_amendment`, then `fix_in_scope`.
Never use `approved` with `## Security Issues`. Do not infer additional values or wrap
the object in another JSON structure. Free-form wording is not a disposition.
"""

_REVIEW_SCOPE_CONTEXT = """\
---
AUTHORITATIVE ACTIVE DELIVERY WRITE SCOPE:
The following validated project-relative production roots are the complete active scope
for this child invocation:
{scope_entries}

`exact_file` authorizes only that file. `path_prefix` authorizes that path and descendants.
Classify a required in-repository correction outside every listed root as
`requires_scope_amendment`; do not classify it as `fix_in_scope`.\
"""

_DISPOSITION_CONTRACTS: dict[DeliveryAgentRole, str] = {
    "analyst": _ANALYSIS_DISPOSITION_CONTRACT,
    "implementer": _IMPLEMENTATION_DISPOSITION_CONTRACT,
    "fixer": "",
    "reviewer": _REVIEW_DISPOSITION_CONTRACT,
    "security_reviewer": _SECURITY_REVIEW_DISPOSITION_CONTRACT,
}


@dataclass(frozen=True)
class DeliveryAgentPromptContext:
    inherited_constraints: str
    disposition_contract: str
    effective_write_scope: str
    is_delivery_child: bool


@dataclass(frozen=True)
class DeliveryReviewDispositionResult:
    disposition: DeliveryDisposition | None
    error_code: str | None


def is_delivery_implementation_already_satisfied(state: TaskState) -> bool:
    """Return whether state carries a valid delivery-child no-change outcome."""
    return bool(
        isinstance(state.delivery_plan_id, str)
        and state.delivery_plan_id
        and isinstance(state.delivery_unit_id, str)
        and state.delivery_unit_id
        and state.delivery_no_change_outcome == DELIVERY_DISPOSITION_ALREADY_SATISFIED
    )


def delivery_agent_prompt_context(
    state: TaskState,
    *,
    role: DeliveryAgentRole,
    project_config: dict | None = None,
) -> DeliveryAgentPromptContext:
    """Validate inherited constraints and select the role's delivery contract."""
    inherited_constraints = delivery_constraint_prompt_context(state)
    is_delivery_child = bool(state.delivery_plan_id and state.delivery_unit_id)
    effective_write_scope = ""
    if is_delivery_child and role in {"reviewer", "security_reviewer"}:
        effective_write_scope = _delivery_review_scope_context(state, project_config)
    return DeliveryAgentPromptContext(
        inherited_constraints=inherited_constraints,
        disposition_contract=_DISPOSITION_CONTRACTS[role] if is_delivery_child else "",
        effective_write_scope=effective_write_scope,
        is_delivery_child=is_delivery_child,
    )


def _delivery_review_scope_context(state: TaskState, project_config: dict | None) -> str:
    if not isinstance(project_config, dict):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.review_context_invalid",
            "The delivery review write-scope context is unavailable.",
        )
    runtime_config = copy.deepcopy(project_config)
    runtime_scope = apply_delivery_write_scope_to_config(runtime_config, state)
    if runtime_scope is None:
        runtime_scope = _legacy_review_scope(runtime_config)
    exact_files = set(runtime_scope.effective_exact_file_paths)
    entries = [
        json.dumps(
            {
                "path": path,
                "kind": "exact_file" if path in exact_files else "path_prefix",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        for path in runtime_scope.effective_paths
    ]
    scope_entries = (
        "\n".join(f"- {entry}" for entry in entries) if entries else "- none (no production writes authorized)"
    )
    return _REVIEW_SCOPE_CONTEXT.format(scope_entries=scope_entries)


def _legacy_review_scope(project_config: dict) -> DeliveryWriteScope:
    project = project_config.get("project")
    sandbox = project_config.get("sandbox")
    if (
        not isinstance(project, dict)
        or not isinstance(sandbox, dict)
        or not isinstance(project.get("root_path"), (str, Path))
        or not isinstance(sandbox.get("allowed_write_paths"), list)
    ):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.review_context_invalid",
            "The delivery review write-scope context is unavailable.",
        )
    return resolve_delivery_write_scope(
        project_root=Path(project["root_path"]),
        configured_write_paths=sandbox["allowed_write_paths"],
        unit_scope_paths=None,
    )


def classify_delivery_review_disposition(
    output: str,
    *,
    is_delivery_child: bool,
    has_blocking_section: bool,
    approved_signal: bool,
    has_warnings: bool = False,
) -> DeliveryReviewDispositionResult:
    """Apply the shared fail-closed review disposition decision table."""
    if not is_delivery_child:
        return DeliveryReviewDispositionResult(disposition=None, error_code=None)

    disposition = None
    error_code = None
    try:
        disposition = parse_delivery_disposition(
            output,
            allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS,
        )
    except DeliveryDispositionParseError as exc:
        error_code = exc.code

    approved_disposition = disposition is not None and disposition.disposition == DELIVERY_DISPOSITION_APPROVED
    action_disposition = disposition is not None and not approved_disposition

    if approved_signal and (has_blocking_section or has_warnings or action_disposition):
        disposition = None
        error_code = "delivery_disposition.conflicting_decision"
    elif approved_disposition and has_blocking_section:
        disposition = None
        error_code = "delivery_disposition.conflicting_decision"
    elif has_blocking_section and disposition is None and error_code is None:
        error_code = "delivery_disposition.missing"
    elif not has_blocking_section and action_disposition:
        disposition = None
        error_code = "delivery_disposition.issue_section_missing"
    elif not has_blocking_section and not approved_disposition and error_code is None:
        error_code = "delivery_disposition.decision_missing"

    return DeliveryReviewDispositionResult(disposition=disposition, error_code=error_code)
