"""Reviewed source attribution and exact, bounded checkpoint authority packets.

The tracked receipt records preparation's independent attribution decision; it is
not a signature or a substitute for the immutable plan/source/contract checks.
Absent or changed receipts select the conservative full-authority review mode.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.delivery_obligations import delivery_authority_fragments
from core.delivery_source_accounting import SourceAccountingError
from core.markdown_document import parse_markdown_document

if TYPE_CHECKING:
    from core.delivery_plan import DeliveryPlan


AUTHORITY_PACKET_POLICY = "scoped-source-authority-v1"


def authority_digest(value: Any) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    )


def preparation_authority_fingerprint(
    source_fingerprint: str,
    checkpoints: list[Any],
    units: list[Any],
    constraints: list[Any],
    obligations: list[Any],
    accounting: list[Any],
) -> str:
    return authority_digest(
        {
            "policy": AUTHORITY_PACKET_POLICY,
            "source": source_fingerprint,
            "checkpoints": [asdict(item) for item in checkpoints],
            "units": [asdict(item) for item in units],
            "constraints": [asdict(item) for item in constraints],
            "obligations": [asdict(item) for item in obligations],
            "accounting": [asdict(item) for item in accounting],
        }
    )


def authority_contract_fingerprint(content: bytes) -> str:
    # Markdown authority uses universal newlines, independently of Git checkout policy.
    normalized = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + sha256(normalized.encode("utf-8")).hexdigest()


def validate_checkpoint_attribution(
    accounting: list[Any], checkpoints: list[Any], obligations: list[Any], constraints: list[Any]
) -> None:
    """Check declared coverage; semantic relevance is independently reviewed."""
    nodes = {node.id: node for node in checkpoints}
    outcomes = {item.id: item for item in obligations}
    rules = {item.id: item for item in constraints}
    for record in accounting:
        if record.checkpoint_ids is None:
            continue
        if not set(record.checkpoint_ids) <= nodes.keys():
            raise SourceAccountingError("checkpoint_invalid", "Source attribution references an unknown checkpoint.")
        owners = {owner for key in record.obligation_ids for owner in outcomes[key].unit_ids} | {
            owner for key in record.constraint_ids for owner in rules[key].unit_ids
        }
        required = {node.id for node in checkpoints if owners.intersection(node.unit_ids)}
        if not required <= set(record.checkpoint_ids):
            raise SourceAccountingError(
                "checkpoint_incomplete", "Checkpoint attribution omits authority governing a covered contributor."
            )


def _enclosing_fragments(source: str, selected: set[str]) -> set[str]:
    fragments = delivery_authority_fragments(source)
    headings = parse_markdown_document(source).headings_by_line()
    stack: list[tuple[int, str]] = []
    result = set(selected)
    for fragment in fragments:
        heading = headings.get(fragment.start_line - 1)
        if heading is not None:
            if heading.level == 0:
                # Unranked text headings cannot close Markdown ancestors. Keep
                # their context until the containing Markdown scope closes,
                # including across subsequent text headings and subsections.
                level = stack[-1][0] if stack else 0
            else:
                level = heading.level
                while stack and stack[-1][0] >= level:
                    stack.pop()
            stack.append((level, fragment.id))
        if fragment.id in selected:
            result.update(key for _, key in stack)
    return result


def checkpoint_authority_context(plan: DeliveryPlan, node_id: str, source: str) -> dict[str, Any]:
    """Construct the proposed packet, without authorizing its use."""
    from core.delivery_verification_scope import DeliveryVerificationScope

    uncaptured = replace(plan, verified_checkpoint_authority={})
    context = DeliveryVerificationScope.from_plan(uncaptured, node_id).plan_context()
    # The whole-plan title can summarize unrelated future work. Exact enclosing
    # source fragments, rather than this author-supplied label, carry context.
    context.pop("title")
    covered = set(context["verification_node"]["unit_ids"])
    selected = {
        record.source_fragment_id
        for record in plan.source_accounting or ()
        if record.checkpoint_ids is None or node_id in record.checkpoint_ids
    }
    # Changing ownership during amendment/repair can make a previous exclusion
    # unsafe. Include every deterministically required fragment before comparing
    # the receipt; never reuse an old attribution for the newly widened scope.
    governed_outcomes = {item.id for item in plan.obligations if covered.intersection(item.unit_ids)}
    governed_constraints = {item.id for item in plan.constraints if covered.intersection(item.unit_ids)}
    selected.update(
        record.source_fragment_id
        for record in plan.source_accounting or ()
        if governed_outcomes.intersection(record.obligation_ids)
        or governed_constraints.intersection(record.constraint_ids)
    )
    selected = _enclosing_fragments(source, selected)
    records = [record for record in plan.source_accounting or () if record.source_fragment_id in selected]
    # Explicit contextual attribution can bring a constraint into this node even
    # when its functional outcome belongs to a later integration boundary.
    constraint_ids = {key for record in records for key in record.constraint_ids}
    constraints = [
        item for item in plan.constraints if item.id in constraint_ids or covered.intersection(item.unit_ids)
    ]
    context["constraints"] = [
        {**item.to_context_dict(), "unit_ids": [key for key in item.unit_ids if key in covered]} for item in constraints
    ]
    due = {item["id"] for item in context["obligations"]}
    context["source_accounting"] = [
        {
            "source_fragment_id": record.source_fragment_id,
            "disposition": record.disposition,
            "obligation_ids": [key for key in record.obligation_ids if key in due],
            "constraint_ids": list(record.constraint_ids),
            "responsibility": "local_and_final" if set(record.obligation_ids) <= due else "contribution_and_final",
        }
        for record in records
    ]
    components = {unit.component for unit in plan.units if unit.id in covered and unit.component}
    context["components"] = [item.to_dict() for item in plan.components if item.id in components]
    fragments = delivery_authority_fragments(source)
    if not selected <= {fragment.id for fragment in fragments} or not plan.source_task:
        raise ValueError("Checkpoint authority fragments are unavailable")
    context["authority_packet"] = {
        "policy": AUTHORITY_PACKET_POLICY,
        "source_fingerprint": plan.source_task.sha256,
        "fragments": [
            {"id": fragment.id, "sha256": fragment.sha256} for fragment in fragments if fragment.id in selected
        ],
        "remaining_authority": "final_gate",
    }
    context["scope_rule"] = (
        "Independently assess all supplied exact authority, including context-only fragments, prohibitions, "
        "literals and shared rules. Assess only this group's contribution to cross-group requirements; "
        "the final gate retains the complete source and all outcomes, including every excluded fragment."
    )
    return context


def checkpoint_authority_receipt(context: dict[str, Any], contracts: dict[str, str]) -> str:
    return authority_digest({"context": context, "contracts": contracts})


def restore_checkpoint_authority(plan: DeliveryPlan, source: str, root: Path) -> dict[str, str]:
    """Restore only unchanged independently reviewed node packets; never repair a receipt."""
    result: dict[str, str] = {}
    if not plan.checkpoint_authority:
        return result
    from core.delivery_amendment import _read_assembly_contract

    for node in plan.checkpoints:
        if node.id not in plan.checkpoint_authority:
            continue
        try:
            context = checkpoint_authority_context(plan, node.id, source)
            contracts = {
                unit.id: authority_contract_fingerprint(
                    _read_assembly_contract(root, unit.task_path, private_artifact_roots=())
                )
                for unit in plan.units
                if unit.id in node.unit_ids
            }
            if checkpoint_authority_receipt(context, contracts) == plan.checkpoint_authority[node.id]:
                result[node.id] = json.dumps(context, ensure_ascii=True)
        except (OSError, RuntimeError, ValueError):
            continue  # Full authority must pass ordinary readiness and evidence checks.
    return result


def verification_authority_fragments(source: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the complete inline packet identically for sizing and both reviewers."""
    fragments = delivery_authority_fragments(source)
    packet = context.get("authority_packet")
    if packet is None:
        return [fragment.to_prompt_dict() for fragment in fragments]
    if (
        packet["policy"] != AUTHORITY_PACKET_POLICY
        or packet["source_fingerprint"] != "sha256:" + sha256(source.encode("utf-8")).hexdigest()
    ):
        raise ValueError("Checkpoint authority source changed")
    expected = {item["id"]: item["sha256"] for item in packet["fragments"]}
    selected = [fragment for fragment in fragments if fragment.id in expected]
    if len(selected) != len(expected) or any(fragment.sha256 != expected[fragment.id] for fragment in selected):
        raise ValueError("Checkpoint authority fragments changed")
    return [{**fragment.to_prompt_dict(), "sha256": fragment.sha256} for fragment in selected]
