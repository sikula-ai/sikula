from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from agents.base_agent import AGENT_SECURITY_PREFIX, load_extra_rules as _load_extra_rules, read_only_agent_prompt
from core.delivery_verification_review import (
    DeliveryIntegrationAssessment,
    DeliveryIntegrationReviewParseError,
    parse_delivery_integration_review,
)
from core.delivery_verification import (
    delivery_verification_allowed_read_paths,
    delivery_verification_provider_read_scope_supported,
    delivery_verification_prompt_is_bounded,
)
from core.llm_client import LLMClient
from tools.base_tool import Sandbox
from tools.file_tool import FileTool


@dataclass(frozen=True)
class DeliveryIntegrationReviewAttempt:
    attempt: int
    prompt: str
    output: str | None
    parse_error: str | None = None


@dataclass(frozen=True)
class DeliveryIntegrationReviewResult:
    assessment: DeliveryIntegrationAssessment
    attempts: list[DeliveryIntegrationReviewAttempt]


class DeliveryIntegrationReviewAgentError(RuntimeError):
    def __init__(self, code: str, message: str, attempts: list[DeliveryIntegrationReviewAttempt]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.attempts = attempts


class DeliveryIntegrationReviewAgent:
    """Read-only whole-candidate reviewer for one bounded integration node."""

    name = "delivery_integration_reviewer"

    def __init__(
        self,
        llm: LLMClient,
        project_config: dict[str, Any] | None = None,
        *,
        usage_records: list[dict[str, object]] | None = None,
    ) -> None:
        self.llm = llm
        self.project_config = project_config or {}
        self.usage_records = usage_records if usage_records is not None else []

    def review(
        self,
        *,
        cwd: Path,
        review_kind: str,
        source_task: str,
        plan_context: dict[str, Any],
        validation_summary: dict[str, Any],
        candidate_commit: str,
        candidate_tree: str,
        known_unit_ids: set[str],
    ) -> DeliveryIntegrationReviewResult:
        if review_kind not in {"semantic", "security"}:
            raise ValueError("delivery integration review kind is invalid")
        prompt = self._prompt(
            cwd=cwd,
            review_kind=review_kind,
            source_task=source_task,
            plan_context=plan_context,
            validation_summary=validation_summary,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
        )
        attempts: list[DeliveryIntegrationReviewAttempt] = []
        format_error: str | None = None
        for attempt in (1, 2):
            effective_prompt = prompt
            if format_error:
                effective_prompt += (
                    "\n\nYour previous response was rejected as malformed: "
                    f"{format_error}. Return the complete assessment again and finish with the exact JSON object."
                )
            if not delivery_verification_prompt_is_bounded(effective_prompt):
                raise DeliveryIntegrationReviewAgentError(
                    "delivery_verification.hierarchy_required",
                    "The integration review packet exceeds the bounded final-gate limit.",
                    attempts,
                )
            try:
                output = self.llm.run_readonly_agent(effective_prompt, cwd)
            except Exception as exc:
                attempts.append(
                    DeliveryIntegrationReviewAttempt(
                        attempt=attempt,
                        prompt=effective_prompt,
                        output=None,
                        parse_error=type(exc).__name__,
                    )
                )
                raise DeliveryIntegrationReviewAgentError(
                    "delivery_verification.review_provider_failed",
                    "Integration reviewer provider failed.",
                    attempts,
                ) from None
            try:
                assessment = parse_delivery_integration_review(output, known_unit_ids=known_unit_ids)
            except DeliveryIntegrationReviewParseError as exc:
                attempts.append(
                    DeliveryIntegrationReviewAttempt(
                        attempt=attempt,
                        prompt=effective_prompt,
                        output=output,
                        parse_error=exc.code,
                    )
                )
                format_error = exc.code
                if attempt == 2:
                    raise DeliveryIntegrationReviewAgentError(exc.code, exc.message, attempts) from None
                continue
            attempts.append(
                DeliveryIntegrationReviewAttempt(
                    attempt=attempt,
                    prompt=effective_prompt,
                    output=output,
                )
            )
            return DeliveryIntegrationReviewResult(assessment=assessment, attempts=attempts)
        raise AssertionError("bounded integration review loop did not terminate")

    def prepare_workspace(self, cwd: Path) -> None:
        prepare = getattr(self.llm, "prepare_readonly_agent_workspace", None)
        if callable(prepare):
            prepare(cwd)

    def consume_usage_records(self) -> list[dict[str, object]]:
        records = list(self.usage_records)
        self.usage_records.clear()
        return records

    def _prompt(
        self,
        *,
        cwd: Path,
        review_kind: str,
        source_task: str,
        plan_context: dict[str, Any],
        validation_summary: dict[str, Any],
        candidate_commit: str,
        candidate_tree: str,
    ) -> str:
        focus = (
            "Review whether the complete assembled candidate satisfies the authoritative source task and whether "
            "the completed units form one coherent implementation."
            if review_kind == "semantic"
            else "Review cross-unit security and privacy behavior in the complete assembled candidate."
        )
        security_context = ""
        if review_kind == "security":
            configured = str(self.project_config.get("security", {}).get("context") or "").strip()
            if configured:
                security_context = f"\n\nProject security context:\n{configured}"
        agent_name = "reviewer" if review_kind == "semantic" else "security_reviewer"
        try:
            allowed_read_paths = delivery_verification_allowed_read_paths(self.project_config)
        except ValueError:
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.config_invalid",
                "Integration reviewer read-path configuration is invalid.",
                [],
            ) from None
        if not delivery_verification_provider_read_scope_supported(allowed_read_paths):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.read_scope_unsupported",
                "Integration reviewer providers cannot enforce narrowed read paths.",
                [],
            )
        file_tool = FileTool(
            Sandbox(cwd, allowed_write_paths=[], allowed_read_paths=allowed_read_paths),
            cwd,
        )
        configured_rules = self.project_config.get(agent_name, {}).get("extra_rules")
        if configured_rules and not file_tool.read(configured_rules).success:
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.review_rules_unavailable",
                "Configured integration review rules are unavailable in the candidate workspace.",
                [],
            )
        extra_rules = _load_extra_rules(self.project_config, agent_name, file_tool)
        prompt = f"""{AGENT_SECURITY_PREFIX}{focus}

Inspect the candidate workspace using read-only tools. Do not modify files or project state.
You may only inspect project files under these configured paths: {json.dumps(allowed_read_paths)}.
The candidate commit is {candidate_commit} and its tree is {candidate_tree}.

Authoritative source task:
<source-task>
{source_task}
</source-task>

Delivery plan context:
{json.dumps(plan_context, indent=2, sort_keys=True)}

Deterministic validation summary:
{json.dumps(validation_summary, indent=2, sort_keys=True)}{security_context}{extra_rules}

Disposition rules:
- approved: no blocking issue remains; findings must be empty.
- repair_required: accepted authority can be satisfied by a new in-repository repair unit.
- scope_amendment_required: accepted delivery scope or decomposition must change first.
- external_dependency_gap: an authoritative external dependency must change first.
- human_review_required: evidence or authority is ambiguous and cannot be approved safely.

You may write bounded review prose before the control object. The final non-empty line must be exactly:
{{"schema_version":1,"disposition":"approved","summary":"One bounded single-line summary","findings":[]}}

For a non-approved disposition, include one or more findings with exactly code, summary, and unit_ids.
Do not output absolute paths, source excerpts, secrets, credentials, or private local metadata.
"""
        return read_only_agent_prompt(prompt)
