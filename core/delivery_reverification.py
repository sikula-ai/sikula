"""Bounded candidate review control, separate from checkpoint admission."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from core.delivery_checkpoint_evidence import load_checkpoint_evidence
from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_scope import DeliveryVerificationScope


if TYPE_CHECKING:
    from core.delivery_verification import DeliveryVerificationSnapshot
    from core.delivery_verification_review import DeliveryIntegrationAssessment


_ROLES = {"semantic", "security"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def new_reverification() -> dict[str, Any]:
    return {"children": {}, "reassessments": {}, "fallbacks": {}}


def parse_reverification(value: Any, parent: dict[str, Any]) -> dict[str, Any]:
    """Reject recursive records and unbounded work before loading control state."""
    if not isinstance(value, dict) or set(value) != {"children", "reassessments", "fallbacks"}:
        raise ValueError("Invalid candidate re-verification control.")
    if not parent.get("composition_attempted") and not parent.get("security_composition_attempted"):
        raise ValueError("Candidate review requires a reserved composition exchange.")
    from core.delivery_composition import MAX_COMPOSITION_CHILDREN

    children = value["children"]
    if not isinstance(children, dict) or len(children) > MAX_COMPOSITION_CHILDREN:
        raise ValueError("Invalid candidate re-verification fan-in.")
    for key, child in children.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key)
            or not isinstance(child, dict)
            or set(child) != {"origin", "verification"}
            or not isinstance(child["origin"], str)
            or not _DIGEST.fullmatch(child["origin"])
            or not isinstance(child["verification"], dict)
            or "reverification" in child["verification"]
        ):
            raise ValueError("Invalid candidate re-verification child.")
        record = parse_delivery_verification_record(child["verification"])
        if any(
            getattr(record, field) != parent.get(field)
            for field in (
                "candidate_commit",
                "candidate_tree",
                "source_fingerprint",
                "plan_fingerprint",
                "config_fingerprint",
            )
        ):
            raise ValueError("Candidate re-verification target changed.")
        if record.security_required != parent.get("security_required", False):
            raise ValueError("Candidate security policy changed.")
        if (
            record.attempt != 1
            or record.root_evidence_fingerprint
            or record.composition_attempted
            or record.security_composition_attempted
        ):
            raise ValueError("Invalid candidate re-verification attempt.")
    rounds = value["reassessments"]
    if not isinstance(rounds, dict) or set(rounds) - _ROLES:
        raise ValueError("Invalid candidate re-verification reassessments.")
    for role, item in rounds.items():
        if (
            not isinstance(item, dict)
            or set(item) != {"initial", "accepted"}
            or type(item["accepted"]) is not bool
            or not isinstance(item["initial"], str)
            or not _DIGEST.fullmatch(item["initial"])
        ):
            raise ValueError("Invalid candidate re-verification reservation.")
        field = (
            "composition_evidence_fingerprint" if role == "semantic" else "security_composition_evidence_fingerprint"
        )
        if not parent.get(field):
            raise ValueError("Candidate reassessment requires accepted initial evidence.")
    roles = value["fallbacks"]
    if (
        not isinstance(roles, dict)
        or set(roles) - _ROLES
        or any(
            digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest))
            for digest in roles.values()
        )
    ):
        raise ValueError("Invalid candidate re-verification fallback budget.")
    if not parent.get("security_required", False) and ("security" in rounds or "security" in roles):
        raise ValueError("Unexpected security review control.")
    return deepcopy(value)


def candidate_snapshot(
    status: DeliveryStatusResult, cfg: dict[str, Any], parent: DeliveryVerificationRecord, node_id: str
) -> DeliveryVerificationSnapshot:
    """A distinct attempt identity prevents collision with historical admission."""
    from core.delivery_composition import fingerprint
    from core.delivery_verification import build_delivery_verification_snapshot

    snapshot = build_delivery_verification_snapshot(
        replace(status, verification_node=node_id), cfg, candidate_commit=parent.candidate_commit
    )
    policy = fingerprint({"checkpoint_policy": snapshot.identity.policy_fingerprint, "final_gate": parent.gate_id})
    identity = replace(snapshot.identity, policy_fingerprint=policy)
    identity = replace(
        identity, gate_id=fingerprint({key: val for key, val in vars(identity).items() if key != "gate_id"})
    )
    return replace(snapshot, identity=identity)


def composition_origin(
    status: DeliveryStatusResult,
    parent: DeliveryVerificationRecord | None,
    node_id: str,
    *,
    digest: str | None = None,
) -> DeliveryVerificationRecord:
    """Resolve only accepted references; an orphan can never become authority."""
    historical = status.checkpoint_verifications.get(node_id)
    if historical is None:
        raise ValueError("Original checkpoint admission is unavailable.")
    entry = (parent.reverification or {}).get("children", {}).get(node_id) if parent else None
    if entry is not None:
        if entry["origin"] != historical.checkpoint_evidence_fingerprint:
            raise ValueError("Candidate re-verification admission changed.")
        fresh = parse_delivery_verification_record(entry["verification"])
        if fresh.passed and (digest is None or digest == fresh.checkpoint_evidence_fingerprint):
            return fresh
    if digest is not None and digest != historical.checkpoint_evidence_fingerprint:
        raise ValueError("Composition candidate evidence is not accepted.")
    return historical


def validate_reverification(
    status: DeliveryStatusResult, record: DeliveryVerificationRecord, cfg: dict[str, Any] | None = None
) -> None:
    if record.reverification is None:
        return
    parse_reverification(record.reverification, record.to_dict())
    root = Path(status.project_root)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    from core.delivery_composition import load_composition

    for role, round_state in record.reverification["reassessments"].items():
        field = (
            "composition_evidence_fingerprint" if role == "semantic" else "security_composition_evidence_fingerprint"
        )
        initial = load_composition(status, replace(record, **{field: round_state["initial"]}), review_kind=role)
        if not initial.fallback or not initial.assessment.approved:
            raise ValueError("Candidate review did not originate in ordinary uncertainty.")
    for role, digest in record.reverification["fallbacks"].items():
        if digest:
            load_full_result(status, record, role)
    for node_id, entry in record.reverification["children"].items():
        historical = status.checkpoint_verifications.get(node_id)
        if historical is None or historical.checkpoint_evidence_fingerprint != entry["origin"]:
            raise ValueError("Candidate re-verification admission changed.")
        child = parse_delivery_verification_record(entry["verification"])
        if cfg is not None:
            snapshot = candidate_snapshot(status, cfg, record, node_id)
            if any(getattr(child, key) != val for key, val in vars(snapshot.identity).items()):
                raise ValueError("Candidate re-verification authority changed.")
        if child.passed:
            evidence = load_checkpoint_evidence(root, directory, child, plan_id=status.plan.plan_id, node_id=node_id)
            if not evidence.covers(DeliveryVerificationScope.from_plan(status.plan, node_id)):
                raise ValueError("Candidate re-verification coverage changed.")
    if record.passed and semantic_gap_ids(record) and not semantic_fallback_covers_gaps(status, record):
        raise ValueError("Final semantic approval predates a rejected candidate child.")


def review_result_path(directory: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid candidate review result identity.")
    return directory / f"candidate-review-{digest[7:]}.json"


def _full_result_payload(status: DeliveryStatusResult, parent: DeliveryVerificationRecord, role: str) -> dict[str, Any]:
    from core.delivery_composition import fingerprint
    from core.delivery_repair_storage import read_repair_state

    digest = parent.reverification["fallbacks"][role]
    root = Path(status.project_root)
    payload = read_repair_state(
        root, review_result_path(delivery_progress_path(root, status.plan.plan_id).parent, digest)
    )
    if (
        not payload
        or set(payload) != {"gate_id", "role", "assessment", "semantic_gaps"}
        or fingerprint(payload) != digest
        or payload["gate_id"] != parent.gate_id
        or payload["role"] != role
        or not isinstance(payload["semantic_gaps"], list)
        or any(not isinstance(key, str) for key in payload["semantic_gaps"])
        or payload["semantic_gaps"] != sorted(set(payload["semantic_gaps"]))
        or not set(payload["semantic_gaps"]).issubset(semantic_gap_ids(parent) if role == "semantic" else ())
    ):
        raise ValueError("Accepted fallback evidence is unavailable.")
    return payload


def semantic_gap_ids(record: DeliveryVerificationRecord) -> list[str]:
    """Accepted child rejections supersede earlier composed semantic approval."""
    return sorted(
        key
        for key, entry in (record.reverification or {}).get("children", {}).items()
        if entry["verification"]["semantic_status"] == "rejected"
    )


def semantic_fallback_covers_gaps(status: DeliveryStatusResult, record: DeliveryVerificationRecord) -> bool:
    if not (record.reverification or {}).get("fallbacks", {}).get("semantic"):
        return False
    payload = _full_result_payload(status, record, "semantic")
    return set(semantic_gap_ids(record)).issubset(payload["semantic_gaps"])


def reverification_budget_exhausted(status: DeliveryStatusResult, record: DeliveryVerificationRecord) -> bool:
    from core.delivery_verification_model import delivery_verification_is_boundary_stop

    if record.reverification is None or delivery_verification_is_boundary_stop(record):
        return False
    fallbacks = record.reverification["fallbacks"]
    return any(digest is None for digest in fallbacks.values()) or (
        "semantic" in fallbacks and not semantic_fallback_covers_gaps(status, record)
    )


def load_full_result(
    status: DeliveryStatusResult, parent: DeliveryVerificationRecord, role: str
) -> DeliveryIntegrationAssessment:
    from core.delivery_verification_review import parse_delivery_integration_review
    import json

    payload = _full_result_payload(status, parent, role)
    scope = DeliveryVerificationScope.from_plan(status.plan)
    return parse_delivery_integration_review(
        json.dumps(payload["assessment"]),
        known_unit_ids=set(scope.unit_ids),
        known_obligation_ids=set(scope.obligation_ids) if role == "semantic" else set(),
    )


def preflight_review_packets(status: DeliveryStatusResult, cfg: dict[str, Any], cwd: Path, source: str) -> None:
    """Size every potential child and full fallback before reserving provider work."""
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
    from core.delivery_verification import delivery_verification_prompt_is_bounded
    from core.delivery_verification_validation import preview_delivery_validation_summary

    reviewer = DeliveryIntegrationReviewAgent(None, cfg)
    for node_id in ("root", *(node.id for node in status.plan.checkpoints)):
        scope = DeliveryVerificationScope.from_plan(status.plan, node_id)
        validation = preview_delivery_validation_summary(cfg, project_root=cwd, include_final_checks=node_id == "root")
        for role in ("semantic", "security") if scope.security_required else ("semantic",):
            prompt = reviewer._prompt(
                cwd=cwd,
                review_kind=role,
                source_task=source,
                plan_context=scope.plan_context(),
                validation_summary=validation,
                candidate_commit="0" * 64,
                candidate_tree="0" * 64,
                known_obligation_ids=set(scope.obligation_ids) if role == "semantic" else set(),
            )
            if not delivery_verification_prompt_is_bounded(prompt + " " * 4096):
                raise ValueError("Candidate re-verification packet exceeds the prompt limit.")
