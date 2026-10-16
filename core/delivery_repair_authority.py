"""Exact, independently reviewable authority changes for one supported leaf repair.

The coordinator owns the append. These proposals cannot authorize themselves:
only matching durable independent approvals may install their receipts.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import yaml

from agents.delivery_repair_authority_agent import (
    authority_renewal_rules,
    build_authority_renewal_prompt,
    parse_authority_renewal,
)
from core.delivery_amendment import _read_assembly_contract
from core.delivery_authority import (
    authority_contract_fingerprint,
    authority_digest,
    checkpoint_authority_context,
    checkpoint_authority_receipt,
    verification_authority_fragments,
)
from core.delivery_checkpoint_model import checkpoint_children
from core.delivery_final_authority import final_authority_context
from core.delivery_plan import DeliveryPlan, check_delivery_plan_data
from core.delivery_verification import delivery_verification_source_task_is_private


def renewal_nodes(plan: DeliveryPlan, node_id: str) -> list[str]:
    """Renew only an already independently attributed leaf and its composing chain."""
    if node_id == "root" or checkpoint_children(plan.checkpoints, node_id):
        return []
    nodes = {node.id: node for node in plan.checkpoints}
    if node_id not in plan.verified_checkpoint_authority:
        return []
    result = [node_id]
    parent = nodes[node_id].parent_id
    while parent != "root":
        if parent not in plan.verified_composition_authority or parent not in plan.verified_checkpoint_authority:
            return []
        result.append(parent)
        parent = nodes[parent].parent_id
    if plan.verified_final_gate_authority is None:
        return []
    return [*result, "root"]


def build_renewal(
    state: dict[str, Any],
    proposed: bytes,
    root: Path,
    candidate: Path,
    cfg: dict[str, Any],
    plan_path: str,
) -> tuple[list[dict[str, Any]], bytes]:
    """Reconstruct packets/receipts from captured inputs, including on partial publication resume."""
    old_data = yaml.safe_load(state["source_plan"])
    new_data = yaml.safe_load(proposed)
    for relative_path in [old_data["source_task"]["path"], *(unit["task_path"] for unit in new_data["units"])]:
        if any(
            delivery_verification_source_task_is_private(
                base, base / relative_path, relative_path, cfg, allow_missing=True
            )
            for base in (root, candidate)
        ):
            raise ValueError("Repair authority cannot inline private artifacts")
    relative = Path(plan_path).relative_to(root)
    plans = []
    for data in (old_data, new_data):
        result = check_delivery_plan_data(
            data,
            project_root=candidate,
            plan_path=str(candidate / relative),
            virtual_task_paths={unit["task_path"] for unit in data["units"]},
        )
        if not result.valid or result.plan is None:
            raise ValueError("Repair authority plan unavailable")
        plans.append(
            replace(
                result.plan,
                verified_checkpoint_authority={},
                verified_composition_authority={},
                verified_final_gate_authority=None,
            )
        )
    old, new = plans
    source = (candidate / old.source_task.path).read_text(encoding="utf-8")
    contracts = {}
    text = {}
    for unit in old.units:
        content = state["contracts"].get(unit.task_path)
        if content is None:
            content = _read_assembly_contract(candidate, unit.task_path, private_artifact_roots=()).decode("utf-8")
        text[unit.id] = content
        contracts[unit.id] = authority_contract_fingerprint(content.encode("utf-8"))
    text[state["unit_id"]] = state["task_markdown"]
    updated = {**contracts, state["unit_id"]: authority_contract_fingerprint(state["task_markdown"].encode("utf-8"))}
    identity = authority_digest(
        {
            "verification": state["verification"],
            "policy": state["config_fingerprint"],
            "candidate_recovery": state.get("candidate_recovery"),
            "proposal": new_data,
            "contracts": updated,
            "authoring_attempt": state["attempts"],
        }
    )
    by_id = {node.id: node for node in new.checkpoints}
    previous = None
    packets = []
    for node_id in state["authority_renewal"]["nodes"]:
        composing = bool(checkpoint_children(new.checkpoints, node_id))
        ids = set(by_id[node_id].unit_ids) if node_id != "root" else set(updated)
        old_hashes = {key: value for key, value in contracts.items() if key in ids}
        new_hashes = {key: value for key, value in updated.items() if key in ids}
        receipts = {}
        if node_id != "root":
            before_raw = checkpoint_authority_context(old, node_id, source)
            after_raw = checkpoint_authority_context(new, node_id, source)
            if checkpoint_authority_receipt(before_raw, old_hashes) != old.checkpoint_authority.get(node_id):
                raise ValueError("Prior checkpoint attribution no longer matches")
            # This slice preserves the accepted source partition. New delegation
            # requires a broader authority operation, not approval over omitted text.
            if before_raw["authority_packet"] != after_raw["authority_packet"]:
                raise ValueError("Repair changes the accepted source partition")
            receipts["checkpoint_authority"] = checkpoint_authority_receipt(after_raw, new_hashes)
            old = replace(
                old,
                verified_checkpoint_authority={**old.verified_checkpoint_authority, node_id: json.dumps(before_raw)},
            )
            new = replace(
                new, verified_checkpoint_authority={**new.verified_checkpoint_authority, node_id: json.dumps(after_raw)}
            )
        if composing:
            before = final_authority_context(old, source, node_id)
            after = final_authority_context(new, source, node_id)
            key = "final_gate_authority" if node_id == "root" else "composition_authority"
            old_receipt = old.final_gate_authority if node_id == "root" else old.composition_authority.get(node_id)
            if checkpoint_authority_receipt(before, old_hashes) != old_receipt:
                raise ValueError("Prior composition attribution no longer matches")
            if before["authority_packet"] != after["authority_packet"]:
                raise ValueError("Repair changes retained source responsibility")
            receipts[key] = checkpoint_authority_receipt(after, new_hashes)
        else:
            before, after = before_raw, after_raw
        direct = {unit["id"] for unit in after["units"]}
        packet = {
            "schema_version": 1,
            "policy": "repair-authority-renewal-v1",
            "review_rules_fingerprint": authority_digest(authority_renewal_rules(cfg, candidate)),
            "node_id": node_id,
            "repair_identity": identity,
            "before": before,
            "after": after,
            "source_fragments": verification_authority_fragments(source, after),
            "contracts": {key: text[key] for key in sorted(direct)},
            "renewed_child": previous,
            "proposed_receipts": receipts,
        }
        packet["packet_fingerprint"] = authority_digest(packet)
        build_authority_renewal_prompt(packet, cfg, candidate, correction=True)
        packets.append(packet)
        previous = {"node_id": node_id, "packet_fingerprint": packet["packet_fingerprint"]}
        for key, receipt in receipts.items():
            if key == "final_gate_authority":
                new_data[key] = receipt
            else:
                new_data.setdefault(key, {})[node_id] = receipt
    return packets, yaml.safe_dump(new_data, sort_keys=False, allow_unicode=True).encode("utf-8")


def apply_accepted_renewal(
    state: dict[str, Any],
    proposed: bytes,
    root: Path,
    candidate: Path,
    cfg: dict[str, Any],
    plan_path: str,
) -> bytes:
    renewal = state.get("authority_renewal")
    if renewal is None or renewal["status"] != "accepted":
        return proposed
    packets, content = build_renewal(state, proposed, root, candidate, cfg, plan_path)
    if set(renewal["reviews"]) != {packet["node_id"] for packet in packets}:
        raise ValueError("Incomplete repair authority evidence")
    for packet in packets:
        record = renewal["reviews"][packet["node_id"]]
        if record["fingerprint"] != packet["packet_fingerprint"]:
            raise ValueError("Repair authority reservation does not match its assessment")
        assessment = parse_authority_renewal(json.dumps(record["assessment"]), packet["packet_fingerprint"])
        if assessment["disposition"] != "approved":
            raise ValueError("Repair authority was not independently accepted")
    return content


def validate_renewal_state(state: dict[str, Any]) -> None:
    renewal = state.get("authority_renewal")
    if renewal is None:
        return
    if (
        not isinstance(renewal, dict)
        or set(renewal) != {"nodes", "status", "reviews"}
        or renewal["status"] not in {"pending", "accepted"}
        or not isinstance(renewal["nodes"], list)
        or not renewal["nodes"]
        or len(renewal["nodes"]) > 32
        or any(not isinstance(key, str) for key in renewal["nodes"])
        or len(set(renewal["nodes"])) != len(renewal["nodes"])
        or not isinstance(renewal["reviews"], dict)
        or not set(renewal["reviews"]) <= set(renewal["nodes"])
    ):
        raise ValueError("Invalid repair authority control")
    data = yaml.safe_load(state["source_plan"])
    nodes = {node["id"]: node for node in data["checkpoints"]}
    chain = [state["node_id"]]
    while chain[-1] != "root":
        parent = nodes[chain[-1]].get("parent_id", "root")
        if parent in chain:
            raise ValueError("Cyclic repair authority chain")
        chain.append(parent)
    if chain != renewal["nodes"]:
        raise ValueError("Changed repair authority chain")
    for record in renewal["reviews"].values():
        if (
            not isinstance(record, dict)
            or set(record) not in ({"attempts", "fingerprint"}, {"attempts", "fingerprint", "assessment"})
            or ("assessment" in record and not isinstance(record["assessment"], dict))
            or type(record.get("attempts")) is not int
            or not 1 <= record["attempts"] <= 2
            or not isinstance(record.get("fingerprint"), str)
        ):
            raise ValueError("Invalid repair authority reservation")
