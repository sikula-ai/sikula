"""Read-only authoring of one integration repair contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from agents.base_agent import AGENT_SECURITY_PREFIX, read_only_agent_prompt
from core.delivery_verification import delivery_verification_prompt_is_bounded
from core.llm_client import LLMClient, LLMReadOnlyViolation


_SYSTEM_REPAIR = """\
Author one small, self-contained delivery repair contract after inspecting the exact candidate
workspace with read-only tools. Do not implement changes, write files, run validation commands,
or modify Git. The supplied source and constraints are authoritative; findings identify gaps,
not new requirements. Resolve ordinary implementation choices using available authorized code.
Never replace an unavailable external dependency with an invented library, shim, or substitute.
If a required dependency, evidence, authority, or security decision is unavailable, stop.

The coordinator fixes identity, dependencies, source obligations, inherited constraints,
write scope, assets, and a one-planner-step budget. You may author only task Markdown.
If the work cannot fit one small unit inside the supplied scope, return scope_amendment_required.
Preserve every applicable constraint and exact source identifier. The child cannot read the
parent source task: put all needed facts, negative cases, and acceptance criteria in its contract.
Keep the full source intent intact; do not solve findings by weakening tests or requirements.
Use the project conventions present in the candidate. Do not invent missing prerequisites.

For a repair, include these Markdown sections: Goal, Current behavior, Desired behavior,
Acceptance criteria, Out of scope, Security and privacy, Reviewer focus, and Verification. Verification commands
must be covered by the supplied effective validation policy. The coordinator copies existing
asset declarations from the supplied contracts; do not write asset sections or declarations.

Return exactly one JSON object with schema_version=1, disposition, and task_markdown.
disposition is repair, scope_amendment_required, external_dependency_gap,
human_review_required, evidence_unavailable, or security_stop.
For any stop, task_markdown must be null. For repair it is the complete Markdown contract.

Authoritative bounded packet:
{packet}
"""
_DISPOSITIONS = frozenset(
    {
        "repair",
        "scope_amendment_required",
        "external_dependency_gap",
        "human_review_required",
        "evidence_unavailable",
        "security_stop",
    }
)


@dataclass(frozen=True)
class DeliveryRepairDraft:
    disposition: str
    task_markdown: str | None


class DeliveryRepairAuthoringError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("Integration repair authoring did not produce an accepted contract.")
        self.code = code


def build_delivery_repair_prompt(packet: dict[str, Any]) -> str:
    """Render and bound the exact prompt used by both readiness and authoring."""
    prompt = read_only_agent_prompt(
        AGENT_SECURITY_PREFIX + _SYSTEM_REPAIR.format(packet=json.dumps(packet, sort_keys=True, ensure_ascii=True))
    )
    if not delivery_verification_prompt_is_bounded(prompt):
        raise DeliveryRepairAuthoringError("delivery_repair.hierarchy_required")
    return prompt


def parse_delivery_repair_draft(output: str) -> DeliveryRepairDraft:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    try:
        if not isinstance(output, str) or len(output) > 128 * 1024:
            raise ValueError("Invalid response")
        value = json.loads(output, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {"schema_version", "disposition", "task_markdown"}:
            raise ValueError("Invalid fields")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("Invalid schema")
        disposition = value["disposition"]
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            raise ValueError("Invalid disposition")
        markdown = value["task_markdown"]
        if disposition == "repair":
            if not isinstance(markdown, str) or not markdown.strip() or len(markdown.encode("utf-8")) > 64 * 1024:
                raise ValueError("Invalid contract")
        elif markdown is not None:
            raise ValueError("Stop cannot contain work")
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise DeliveryRepairAuthoringError("delivery_repair.output_invalid") from None
    return DeliveryRepairDraft(disposition, markdown)


class DeliveryRepairAgent:
    name = "delivery_preparer"

    def __init__(self, llm: LLMClient, *, usage_records: list[dict[str, Any]] | None = None) -> None:
        self.llm = llm
        self.usage_records = usage_records if usage_records is not None else []

    def prepare_workspace(self, cwd: Path) -> None:
        prepare = getattr(self.llm, "prepare_readonly_agent_workspace", None)
        if callable(prepare):
            prepare(cwd)

    def author(
        self,
        *,
        cwd: Path,
        packet: dict[str, Any],
        audit_recorder: Callable[[dict[str, Any]], None],
    ) -> DeliveryRepairDraft:
        prompt = build_delivery_repair_prompt(packet)
        audit_recorder({"event": "authoring_started", "prompt": prompt, "output": None})
        output = None
        error = None
        try:
            output = self.llm.run_readonly_agent(prompt, cwd)
            return parse_delivery_repair_draft(output)
        except DeliveryRepairAuthoringError as exc:
            error = exc.code
            raise
        except (KeyboardInterrupt, SystemExit):
            error = "delivery_repair.interrupted"
            raise
        except LLMReadOnlyViolation:
            error = "delivery_repair.readonly_mutation"
            raise DeliveryRepairAuthoringError(error) from None
        except Exception as exc:
            error = type(exc).__name__
            raise DeliveryRepairAuthoringError("delivery_repair.provider_failed") from None
        finally:
            audit_recorder(
                {
                    "event": "authoring_finished",
                    "prompt": prompt,
                    "output": output,
                    "error": error,
                    "usage": list(self.usage_records),
                }
            )
            self.usage_records.clear()
