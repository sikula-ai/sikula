"""Independently reviewed final integration responsibility, never inferred omission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.delivery_authority import (
    AUTHORITY_PACKET_POLICY,
    _enclosing_fragments,
    authority_contract_fingerprint,
    authority_digest,
    checkpoint_authority_receipt,
)
from core.delivery_obligations import delivery_authority_fragments
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_checkpoint_model import checkpoint_children

if TYPE_CHECKING:
    from core.delivery_plan import DeliveryPlan


FINAL_AUTHORITY_POLICY = "final-authority-closure-v1"


def final_authority_context(plan: DeliveryPlan, source: str, node_id: str = "root") -> dict[str, Any]:
    """Build a proposed packet; only an independently issued receipt authorizes it."""
    if not plan.source_task or not plan.source_accounting or not plan.checkpoints:
        raise ValueError("Final delegation requires source-backed checkpoint authority.")
    covered: set[str] = set()
    inherited: set[str] = set()
    immediate = checkpoint_children(plan.checkpoints, node_id)
    if not immediate:
        raise ValueError("Composition authority requires immediate children.")
    scope = DeliveryVerificationScope.from_plan(plan, node_id)
    nodes = {node.id: node for node in immediate}
    outcomes = {item.id: item for item in plan.obligations}
    rules = {item.id: item for item in plan.constraints}
    children = []
    for node in immediate:
        if (
            not node.integration_context
            or covered.intersection(node.unit_ids)
            or inherited.intersection(node.obligation_ids)
        ):
            raise ValueError("Final delegation requires disjoint reviewed child interfaces.")
        if any(not set(outcomes[key].unit_ids) <= set(node.unit_ids) for key in node.obligation_ids):
            raise ValueError("Later contributors require fresh final attribution.")
        covered.update(node.unit_ids)
        inherited.update(node.obligation_ids)
        children.append(
            {
                "id": node.id,
                "integration_context": node.integration_context,
                "coverage_fingerprint": authority_digest(
                    {"units": list(node.unit_ids), "obligations": list(node.obligation_ids)}
                ),
                "obligation_count": len(node.obligation_ids),
            }
        )
    selected = set()
    hierarchical = any(node.parent_id != "root" for node in plan.checkpoints)
    for record in plan.source_accounting:
        if record.source_fragment_id not in scope.source_fragment_ids:
            continue
        if hierarchical:
            # Independent preparation review approves this delegation at every level.
            # Structural ownership alone never grants permission to use the packet.
            owning_children = [
                child
                for child in immediate
                if child.id in (record.checkpoint_ids or ())
                and set(record.obligation_ids) <= set(child.obligation_ids)
                and all(set(rules[key].unit_ids) <= set(child.unit_ids) for key in record.constraint_ids)
            ]
            if (node_id == "root" and record.final_gate) or len(owning_children) != 1:
                selected.add(record.source_fragment_id)
            continue
        if record.final_gate:
            selected.add(record.source_fragment_id)
            continue
        if not record.checkpoint_ids or len(record.checkpoint_ids) != 1 or record.checkpoint_ids[0] not in nodes:
            raise ValueError("Final authority has no single owning child.")
        child = nodes[record.checkpoint_ids[0]]
        if not set(record.obligation_ids) <= set(child.obligation_ids) or any(
            not set(rules[key].unit_ids) <= set(child.unit_ids) for key in record.constraint_ids
        ):
            raise ValueError("Shared outcomes and constraints cannot be delegated as child-local authority.")
    selected = _enclosing_fragments(source, selected)
    context = scope.plan_context()
    context.pop("title", None)
    if node_id != "root":
        context["verification_node"] = {"id": node_id}
    context["units"] = [item for item in context["units"] if item["id"] not in covered]
    # Dependency edges to child internals are represented by a bounded node reference.
    for unit in context["units"]:
        unit["depends_on"] = [key for key in unit["depends_on"] if key not in covered]
        unit["checkpoint_dependencies"] = [
            node.id
            for node in immediate
            if set(node.unit_ids).intersection(next(u.depends_on for u in plan.units if u.id == unit["id"]))
        ]
    context["obligations"] = [item for item in context["obligations"] if item["id"] not in inherited]
    direct = {item["id"] for item in context["obligations"]}
    relevant_records = [item for item in plan.source_accounting if item.source_fragment_id in selected]
    constraint_ids = {key for item in relevant_records for key in item.constraint_ids}
    direct_units = {item["id"] for item in context["units"]}
    constraint_ids.update(item.id for item in plan.constraints if set(item.unit_ids).intersection(direct_units))
    context["constraints"] = [
        {
            **item.to_context_dict(),
            "unit_ids": [key for key in item.unit_ids if key in direct_units],
            "checkpoint_ids": [node.id for node in immediate if set(node.unit_ids).intersection(item.unit_ids)],
        }
        for item in plan.constraints
        if item.id in constraint_ids
    ]
    # Shared obligations likewise name their child contributors without listing every unit.
    for item in context["obligations"]:
        owners = set(item["unit_ids"])
        item["unit_ids"] = [key for key in item["unit_ids"] if key in direct_units]
        item["checkpoint_ids"] = [node.id for node in immediate if owners.intersection(node.unit_ids)]
    context["source_accounting"] = [
        {
            "source_fragment_id": item.source_fragment_id,
            "disposition": item.disposition,
            "obligation_ids": [key for key in item.obligation_ids if key in direct],
            "constraint_ids": [key for key in item.constraint_ids if key in constraint_ids],
        }
        for item in relevant_records
    ]
    components = {unit.component for unit in plan.units if unit.id in direct_units and unit.component}
    context["components"] = [item.to_dict() for item in plan.components if item.id in components]
    context["authority_packet"] = {
        "policy": AUTHORITY_PACKET_POLICY,
        "source_fingerprint": plan.source_task.sha256,
        "fragments": [
            {"id": item.id, "sha256": item.sha256}
            for item in delivery_authority_fragments(source)
            if item.id in selected
        ],
        "remaining_authority": "accepted_child_closure",
    }
    context["final_authority"] = {"policy": FINAL_AUTHORITY_POLICY, "children": children}
    return context


def restore_final_authority(plan: DeliveryPlan, source: str, root: Path) -> str | None:
    if not plan.final_gate_authority or set(plan.verified_checkpoint_authority) != {
        node.id for node in plan.checkpoints
    }:
        return None
    from core.delivery_amendment import _read_assembly_contract

    try:
        context = final_authority_context(plan, source)
        contracts = {
            unit.id: authority_contract_fingerprint(
                _read_assembly_contract(root, unit.task_path, private_artifact_roots=())
            )
            for unit in plan.units
        }
        if checkpoint_authority_receipt(context, contracts) == plan.final_gate_authority:
            return json.dumps(context, ensure_ascii=True)
    except (OSError, RuntimeError, ValueError, KeyError):
        pass
    return None


def restore_composition_authority(plan: DeliveryPlan, source: str, root: Path) -> dict[str, str]:
    from core.delivery_amendment import _read_assembly_contract

    result = {}
    for node_id, receipt in plan.composition_authority.items():
        if node_id not in plan.verified_checkpoint_authority or any(
            child.id not in plan.verified_checkpoint_authority
            for child in checkpoint_children(plan.checkpoints, node_id)
        ):
            continue
        try:
            scope = DeliveryVerificationScope.from_plan(plan, node_id)
            context = final_authority_context(plan, source, node_id)
            contracts = {
                unit.id: authority_contract_fingerprint(
                    _read_assembly_contract(root, unit.task_path, private_artifact_roots=())
                )
                for unit in plan.units
                if unit.id in scope.unit_ids
            }
            if checkpoint_authority_receipt(context, contracts) == receipt:
                result[node_id] = json.dumps(context, ensure_ascii=True)
        except (OSError, RuntimeError, ValueError, KeyError):
            continue
    return result
