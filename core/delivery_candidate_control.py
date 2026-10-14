"""Bounded candidate-node control, linked by accepted content-addressed references."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record

# Reserve the worst case (two roles, initial/reassessment/fallback, two format
# attempts) for every node before entering it. Unused capacity is never refunded.
MAX_CANDIDATE_NODES = 32
MAX_NODE_PROVIDER_CALLS = 12


class CandidateBudgetExceeded(ValueError):
    """The persisted subtree has spent its finite node reservations."""


def expand_record(root: Path, directory: Path, record: DeliveryVerificationRecord) -> DeliveryVerificationRecord:
    from core.delivery_composition import fingerprint

    control = record.reverification
    if control is None or "reference" not in control:
        return record
    digest = control["reference"]
    payload = read_repair_state(root, directory / f"candidate-node-{digest[7:]}.json")
    if (
        not payload
        or set(payload) != {"gate_id", "control"}
        or fingerprint(payload) != digest
        or payload["gate_id"] != record.gate_id
        or not isinstance(payload["control"], dict)
        or "reference" in payload["control"]
    ):
        raise ValueError("Candidate node control is unavailable.")
    return parse_delivery_verification_record({**record.to_dict(), "reverification": payload["control"]})


def compact_record(root: Path, directory: Path, record: DeliveryVerificationRecord) -> DeliveryVerificationRecord:
    from core.delivery_composition import fingerprint

    if record.reverification is None or "reference" in record.reverification:
        return record
    payload = {"gate_id": record.gate_id, "control": record.reverification}
    digest = fingerprint(payload)
    write_repair_state(root, directory / f"candidate-node-{digest[7:]}.json", payload)
    return replace(record, reverification={"reference": digest})


def expanded_child(status: DeliveryStatusResult, entry: dict[str, Any]) -> DeliveryVerificationRecord:
    root = Path(status.project_root)
    return expand_record(
        root,
        delivery_progress_path(root, status.plan.plan_id).parent,
        parse_delivery_verification_record(entry["verification"]),
    )


def record_at(
    root: Path, directory: Path, anchor: DeliveryVerificationRecord, route: tuple[str, ...]
) -> DeliveryVerificationRecord:
    record = anchor
    for key in route:
        record = expand_record(root, directory, record)
        record = parse_delivery_verification_record(record.reverification["children"][key]["verification"])
    return expand_record(root, directory, record)


def replace_at(
    root: Path,
    directory: Path,
    anchor: DeliveryVerificationRecord,
    route: tuple[str, ...],
    updated: DeliveryVerificationRecord,
) -> DeliveryVerificationRecord:
    if not route:
        return updated
    record = expand_record(root, directory, anchor)
    control = deepcopy(record.reverification)
    entry = control["children"][route[0]]
    child = parse_delivery_verification_record(entry["verification"])
    child = replace_at(root, directory, child, route[1:], updated)
    entry["verification"] = compact_record(root, directory, child).to_dict()
    from core.delivery_reverification import semantic_gap_ids

    if child.semantic_status == "rejected" or semantic_gap_ids(child):
        # A later local fallback may approve this child, but ancestors must still
        # reassess the implications of a confirmed descendant semantic gap.
        entry["semantic_gap"] = True
    from core.delivery_verification_model import delivery_verification_is_boundary_stop

    # Copy only the boundary classification, not descendant control/history. This
    # makes a terminal stop visible without needing readable sidecar evidence.
    if delivery_verification_is_boundary_stop(child):
        record = replace(record, status="blocked", stop_code=child.stop_code, security_status=child.security_status)
    return replace(record, reverification=control)


def reserved_node_count(root: Path, directory: Path, record: DeliveryVerificationRecord) -> int:
    pending = [record]
    seen = set()
    while pending:
        item = expand_record(root, directory, pending.pop())
        if item.gate_id in seen:
            raise ValueError("Repeated candidate node identity.")
        seen.add(item.gate_id)
        if len(seen) > MAX_CANDIDATE_NODES:
            raise CandidateBudgetExceeded("Candidate subtree budget exhausted.")
        pending.extend(
            parse_delivery_verification_record(entry["verification"])
            for entry in (item.reverification or {}).get("children", {}).values()
        )
    return len(seen)
