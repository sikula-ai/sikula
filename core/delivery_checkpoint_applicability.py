"""Candidate-specific checkpoint evidence, separate from historical admission.

Exact-tree reuse needs no semantic inference. Changed trees require fresh typed
final results, from full review or grounded composition; path overlap or ancestry
never establishes independence. Historical admission remains separate.
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
    from core.delivery_reverification import validate_reverification

    validate_reverification(status, record)
    root = Path(status.project_root)
    evidence = load_root_evidence(
        root,
        delivery_progress_path(root, status.plan.plan_id).parent,
        record,
        plan_id=status.plan.plan_id,
        unit_order=tuple(unit.id for unit in status.units if unit.status == "done"),
    )
    scope = DeliveryVerificationScope.from_plan(status.plan, "root")
    if not evidence.covers(scope):
        raise ValueError("Root evidence coverage changed.")
    if record.composition_evidence_fingerprint:
        from core.delivery_composition import load_composition

        composition = load_composition(status, record)
        actual = {item.id: item.outcome for item in evidence.obligation_results}
        outcomes_match = (
            (
                all(actual.get(item.id) == item.outcome for item in composition.assessment.obligation_results)
                and len(composition.assessment.obligation_results) + composition.inherited_count == len(actual)
            )
            if composition.child_refs
            else composition.assessment.obligation_results == list(evidence.obligation_results)
        )
        if not composition.fallback and (not composition.assessment.approved or not outcomes_match):
            raise ValueError("Composition outcomes changed.")
    if record.security_composition_evidence_fingerprint:
        from core.delivery_composition import load_composition

        security = load_composition(status, record, review_kind="security")
        if not security.fallback and not security.assessment.approved:
            raise ValueError("Security composition changed.")


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
    current_children = set()
    if root_matches and root_record.reverification:
        try:
            from core.delivery_reverification import validate_reverification

            validate_reverification(status, root_record, cfg)
            current_children = {
                key
                for key, item in root_record.reverification["children"].items()
                if item["verification"]["status"] == "passed"
            }
        except (OSError, RuntimeError, ValueError):
            root_unavailable = True
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
        elif root_current or checkpoint.id in current_children:
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
                "candidate_evidence": (root_record.reverification or {})
                .get("children", {})
                .get(checkpoint.id, {})
                .get("verification", {})
                .get("checkpoint_evidence_fingerprint")
                if root_matches
                else None,
            }
            binding = "sha256:" + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        result[checkpoint.id] = CheckpointApplicability(
            checkpoint.id, decision, identity.candidate_commit, identity.candidate_tree, binding
        )
    return result
