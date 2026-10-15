"""Private, candidate-bound handoffs from recursive review to local checkpoint repair."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import re
from typing import Any

from core.delivery_checkpoint_model import _ID, checkpoint_children
from core.delivery_checkpoints import verification_node_status
from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_repair_input import (
    _input_path,
    _load_bound_input,
    load_repair_input,
    repair_content_fingerprint,
)
from core.delivery_repair_storage import write_repair_state
from core.delivery_verification import build_delivery_verification_identity
from core.delivery_verification_model import DeliveryVerificationRecord, delivery_verification_is_boundary_stop
from core.delivery_verification_scope import DeliveryVerificationScope


CANDIDATE_REPAIR_REQUIRED = "delivery_verification.candidate_repair_required"
REPAIR_REQUIRED_CODES = frozenset({"delivery_verification.repair_required", CANDIDATE_REPAIR_REQUIRED})


def validate_recovery_binding(binding: Any) -> None:
    if (
        not isinstance(binding, dict)
        or set(binding) != {"node_id", "gate_id", "attempt", "repair_input_fingerprint"}
        or not isinstance(binding["node_id"], str)
        or not _ID.fullmatch(binding["node_id"])
        or type(binding["attempt"]) is not int
        or binding["attempt"] < 1
        or any(
            not isinstance(binding[key], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding[key])
            for key in ("gate_id", "repair_input_fingerprint")
        )
    ):
        raise ValueError("Candidate repair initiating binding is invalid.")


def eligible_local_finding(status: DeliveryStatusResult, record: DeliveryVerificationRecord, cfg: dict) -> bool:
    """Only independently attributed leaf authority can bypass ancestor rediscovery."""
    node_id = status.verification_node
    plan = status.plan
    if (
        node_id == "root"
        or checkpoint_children(plan.checkpoints, node_id)
        or node_id not in plan.verified_checkpoint_authority
        or record.status != "failed"
        or record.stop_code != "delivery_verification.repair_required"
        or record.semantic_status != "rejected"
        or record.security_status not in {"not_run", "approved"}
        or not record.repair_input_fingerprint
        or delivery_verification_is_boundary_stop(record)
    ):
        return False
    scope = DeliveryVerificationScope.from_plan(plan, node_id)
    assessment = load_repair_input(
        Path(status.project_root),
        delivery_progress_path(Path(status.project_root), plan.plan_id).parent,
        record,
        plan,
        cfg,
        node_id=node_id,
        # Review identity remains binding; the preparer policy is frozen by the
        # repair coordinator at its first attempt, not by this older review.
        require_current_policy=False,
    )
    gaps = {item.id for item in assessment.obligation_results if item.outcome != "satisfied"}
    return (
        assessment.disposition == "repair_required"
        and bool(gaps)
        and bool(assessment.findings)
        and all(
            finding.obligation_ids
            and set(finding.obligation_ids) <= gaps
            and set(finding.unit_ids) <= set(scope.unit_ids)
            for finding in assessment.findings
        )
        and all(set(item.unit_ids) <= set(scope.unit_ids) for item in plan.obligations if item.id in gaps)
    )


def store_candidate_repair_handoff(
    status: DeliveryStatusResult, parent: DeliveryVerificationRecord, child_id: str, cfg: dict
) -> str:
    from core.delivery_candidate_control import expanded_child
    from core.delivery_verification import DeliveryVerificationIdentity

    child = expanded_child(status, parent.reverification["children"][child_id])
    payload = {
        "schema_version": 1,
        "kind": "candidate_repair",
        "identity": {key: getattr(parent, key) for key in DeliveryVerificationIdentity.__dataclass_fields__},
        "attempt": parent.attempt,
        "repair_policy_fingerprint": repair_content_fingerprint(cfg),
        "child": {
            "node_id": child_id,
            "gate_id": child.gate_id,
            "attempt": child.attempt,
            "repair_input_fingerprint": child.repair_input_fingerprint,
        },
    }
    root = Path(status.project_root)
    write_repair_state(
        root,
        _input_path(delivery_progress_path(root, status.plan.plan_id).parent, parent.gate_id, parent.attempt),
        payload,
    )
    return repair_content_fingerprint(payload)


def resolve_candidate_repair(
    status: DeliveryStatusResult, cfg: dict, *, expected: dict[str, Any] | None = None
) -> tuple[DeliveryStatusResult, dict[str, Any]]:
    """Validate the initiating gate and each accepted link; never use orphan artifacts."""
    from core.delivery_assembly import delivery_branch_commit
    from core.delivery_progress import read_delivery_progress

    if (
        not status.plan
        or delivery_branch_commit(Path(status.project_root), status.plan.final_branch) != status.assembled_commit
    ):
        raise ValueError("Candidate repair assembly changed.")
    progress, errors = read_delivery_progress(
        delivery_progress_path(Path(status.project_root), status.plan.plan_id), plan_id=status.plan.plan_id
    )
    if (
        progress is None
        or errors
        or any(
            item is not None and delivery_verification_is_boundary_stop(item)
            for item in [progress.verification, *progress.checkpoint_verifications.values()]
        )
    ):
        raise ValueError("Candidate repair cannot cross an unavailable or terminal verification boundary.")
    return _resolve_captured_candidate_repair(status, cfg, expected=expected)


def _resolve_captured_candidate_repair(
    status: DeliveryStatusResult, cfg: dict[str, Any], *, expected: dict[str, Any] | None = None
) -> tuple[DeliveryStatusResult, dict[str, Any]]:
    from core.delivery_candidate_control import MAX_CANDIDATE_NODES, expanded_child
    from core.delivery_reverification import validate_reverification

    anchor = status.verification
    root = Path(status.project_root)
    if (
        not status.valid
        or anchor is None
        or anchor.status != "failed"
        or anchor.stop_code != CANDIDATE_REPAIR_REQUIRED
        or delivery_verification_is_boundary_stop(anchor)
        or any(delivery_verification_is_boundary_stop(item) for item in status.checkpoint_verifications.values())
        or status.assembly_status != "ready"
        or status.assembled_commit != anchor.candidate_commit
        or any(
            getattr(anchor, key) != value
            for key, value in asdict(
                build_delivery_verification_identity(status, cfg, candidate_commit=anchor.candidate_commit)
            ).items()
        )
    ):
        raise ValueError("Candidate repair initiating authority is unavailable.")
    binding = {
        "node_id": status.verification_node,
        "gate_id": anchor.gate_id,
        "attempt": anchor.attempt,
        "repair_input_fingerprint": anchor.repair_input_fingerprint,
    }
    validate_recovery_binding(binding)
    if expected is not None:
        validate_recovery_binding(expected)
    if expected is not None and binding != expected:
        raise ValueError("Candidate repair initiating attempt changed.")
    validate_reverification(status, anchor, cfg)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    record = anchor
    seen = set()
    while record.stop_code == CANDIDATE_REPAIR_REQUIRED:
        if record.gate_id in seen or len(seen) >= MAX_CANDIDATE_NODES:
            raise ValueError("Candidate repair handoff hierarchy is invalid.")
        seen.add(record.gate_id)
        payload = _load_bound_input(root, directory, record)
        if (
            set(payload) != {"schema_version", "kind", "identity", "attempt", "repair_policy_fingerprint", "child"}
            or payload["kind"] != "candidate_repair"
            or not isinstance(payload["child"], dict)
            or set(payload["child"]) != {"node_id", "gate_id", "attempt", "repair_input_fingerprint"}
        ):
            raise ValueError("Candidate repair handoff is unavailable.")
        child_ref = payload["child"]
        validate_recovery_binding(child_ref)
        child_id = child_ref["node_id"]
        if child_id not in {item.id for item in checkpoint_children(status.plan.checkpoints, status.verification_node)}:
            raise ValueError("Candidate repair is outside its parent authority.")
        child = expanded_child(status, record.reverification["children"][child_id])
        if (
            child.status != "failed"
            or child.semantic_status != "rejected"
            or child.security_status not in {"not_run", "approved"}
            or any(getattr(child, key) != child_ref[key] for key in ("gate_id", "attempt", "repair_input_fingerprint"))
        ):
            raise ValueError("Candidate repair finding authority changed.")
        status = replace(status, verification_node=child_id, verification=child, verification_status=child.status)
        record = child
    if not eligible_local_finding(status, record, cfg):
        raise ValueError("Candidate finding is not eligible for local repair.")
    return status, binding


def candidate_repair_status(
    path: str | Path,
    root: Path,
    node_id: str,
    cfg: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> tuple[DeliveryStatusResult, dict[str, Any] | None]:
    """Resolve fresh control before each attempt; caller holds the delivery lock."""
    from core.delivery_progress import get_delivery_status

    status = verification_node_status(
        get_delivery_status(path, project_root=root), binding["node_id"] if binding else node_id
    )
    if binding or (status.verification and status.verification.stop_code == CANDIDATE_REPAIR_REQUIRED):
        return resolve_candidate_repair(status, cfg, expected=binding)
    return status, None


def validate_prepared_candidate_repair(
    status: DeliveryStatusResult, state: dict[str, Any], cfg: dict[str, Any]
) -> None:
    """Reopen required evidence after partial publication from the frozen snapshot.

    The caller has checked exact publication bytes, branch lineage and progress.
    Live authority no longer describes the pre-repair candidate after those writes.
    """
    import yaml
    from core.delivery_plan import check_delivery_plan_data
    from core.delivery_verification_model import parse_delivery_verification_record

    binding = state["candidate_recovery"]
    checked = check_delivery_plan_data(
        yaml.safe_load(state["source_plan"]), project_root=Path(status.project_root), plan_path=status.plan_path
    )
    if not checked.valid:
        raise ValueError("Captured candidate repair authority is unavailable.")
    progress = state["completed_progress"]
    checkpoints = {
        key: parse_delivery_verification_record(value)
        for key, value in progress.get("checkpoint_verifications", {}).items()
    }
    anchor = (
        parse_delivery_verification_record(progress["verification"])
        if binding["node_id"] == "root"
        else checkpoints[binding["node_id"]]
    )
    captured = replace(
        status,
        plan=checked.plan,
        plan_fingerprint=anchor.plan_fingerprint,
        assembled_commit=anchor.candidate_commit,
        assembly_status="ready",
        verification_node=binding["node_id"],
        verification=anchor,
        checkpoint_verifications=checkpoints,
    )
    selected, _ = _resolve_captured_candidate_repair(captured, cfg, expected=binding)
    if selected.verification.to_dict() != state["verification"] or selected.verification_node != state["node_id"]:
        raise ValueError("Captured candidate repair finding changed.")
