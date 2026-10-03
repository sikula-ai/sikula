"""Candidate-specific checkpoint evidence, separate from historical admission.

Exact-tree reuse needs no semantic inference. Changed trees require fresh typed
root results in this slice; path overlap or ancestry never establishes independence.
The existing root reviewer supplies that assessment without an additional call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from core.delivery_checkpoint_evidence import load_root_evidence
from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_verification_model import DeliveryVerificationRecord, delivery_verification_is_boundary_stop
from core.delivery_verification_scope import DeliveryVerificationScope


@dataclass(frozen=True)
class CheckpointApplicability:
    node_id: str
    status: str
    candidate_commit: str | None = None
    candidate_tree: str | None = None
    binding: str | None = None


def validate_root_evidence(status: DeliveryStatusResult, record: DeliveryVerificationRecord) -> None:
    """Validate exact typed coverage; the caller separately validates freshness."""
    if not status.plan or not status.project_root:
        raise ValueError("Root evidence requires plan authority.")
    root = Path(status.project_root)
    evidence = load_root_evidence(
        root, delivery_progress_path(root, status.plan.plan_id).parent, record, plan_id=status.plan.plan_id
    )
    scope = DeliveryVerificationScope.from_plan(status.plan, "root")
    if not evidence.covers(scope):
        raise ValueError("Root evidence coverage changed.")


def checkpoint_applicability(
    status: DeliveryStatusResult, cfg: dict[str, Any] | None, usable_handoffs: frozenset[str]
) -> dict[str, CheckpointApplicability]:
    """Derive bounded decisions from accepted evidence, never audit history.

    The caller supplies handoffs freshly validated against this status and policy.
    No decision survives a different candidate or authority. The binding references
    the original receipt and current verification identity, without copying receipts.
    """
    from core.delivery_verification import build_delivery_verification_snapshot

    if not status.plan or not status.plan.checkpoints:
        return {}
    result = {item.id: CheckpointApplicability(item.id, "not_checked") for item in status.plan.checkpoints}
    if cfg is None:
        return result
    records = list(status.checkpoint_verifications.values())
    if status.verification is not None:
        records.append(status.verification)
    if not status.valid or any(delivery_verification_is_boundary_stop(record) for record in records):
        return {key: replace(item, status="unavailable") for key, item in result.items()}
    try:
        if status.assembly_status != "ready" or status.assembled_commit is None:
            raise ValueError("Candidate unavailable.")
        snapshot = build_delivery_verification_snapshot(
            replace(status, verification_node="root"), cfg, candidate_commit=status.assembled_commit
        )
    except (OSError, RuntimeError, ValueError):
        return {key: replace(item, status="unavailable") for key, item in result.items()}

    identity = snapshot.identity
    root_record = status.verification if status.verification_node == "root" else None
    root_matches = bool(
        root_record and all(getattr(root_record, key) == value for key, value in vars(identity).items())
    )
    root_current = root_matches and root_record.passed
    root_rejected = root_matches and root_record.semantic_status == "rejected"
    root_unavailable = False
    if root_current:
        try:
            validate_root_evidence(status, root_record)
        except (OSError, RuntimeError, ValueError):
            root_current = False
            root_unavailable = True

    for checkpoint in status.plan.checkpoints:
        origin = status.checkpoint_verifications.get(checkpoint.id)
        decision = "verification_required"
        if checkpoint.id not in usable_handoffs or origin is None or root_unavailable:
            decision = "unavailable"
        elif root_rejected:
            # A current adverse assessment supersedes exact-tree reuse, while
            # leaving the original admission receipt and recovery path intact.
            decision = "verification_required"
        elif origin.candidate_tree == identity.candidate_tree:
            decision = "exact"
        elif root_current:
            decision = "reverified"
        binding = None
        if decision in {"exact", "reverified"}:
            payload = {
                "plan_id": status.plan.plan_id,
                "node_id": checkpoint.id,
                "origin_gate": origin.gate_id,
                "origin_attempt": origin.attempt,
                "origin_evidence": origin.checkpoint_evidence_fingerprint,
                "target": vars(identity),
                "decision": decision,
                "root_evidence": root_record.root_evidence_fingerprint if decision == "reverified" else None,
            }
            binding = "sha256:" + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        result[checkpoint.id] = CheckpointApplicability(
            checkpoint.id, decision, identity.candidate_commit, identity.candidate_tree, binding
        )
    return result
