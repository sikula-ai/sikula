"""Private node results, bound to accepted control state rather than audit replay.

These describe the reviewed candidate only. Loading them does not establish their
applicability to later code, or replace the mandatory root review.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_review import DeliveryIntegrationAssessment, DeliveryObligationAssessment
from core.delivery_verification_scope import DeliveryVerificationCompletedUnit, DeliveryVerificationScope

if TYPE_CHECKING:
    from core.delivery_composition import CompositionResult
    from core.delivery_progress import DeliveryStatusResult
    from core.delivery_verification import DeliveryVerificationSnapshot


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


@dataclass(frozen=True)
class DeliveryVerificationEvidence:
    """Bounded typed results for one scope and one accepted verification attempt."""

    plan_id: str
    node_id: str
    verification: DeliveryVerificationRecord
    completed_units: tuple[DeliveryVerificationCompletedUnit, ...]
    constraint_ids: tuple[str, ...]
    obligation_results: tuple[DeliveryObligationAssessment, ...]

    def covers(self, scope: DeliveryVerificationScope) -> bool:
        return (
            self.plan_id == scope.plan_id
            and self.node_id == scope.node_id
            and self.verification.security_required == scope.security_required
            and {unit.unit_id for unit in self.completed_units} == set(scope.unit_ids)
            and set(self.constraint_ids) == set(scope.constraint_ids)
            and {result.id for result in self.obligation_results} == set(scope.obligation_ids)
        )


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def checkpoint_evidence_path(directory: Path, record: DeliveryVerificationRecord) -> Path:
    if not _SHA256.fullmatch(record.gate_id) or type(record.attempt) is not int or record.attempt < 1:
        raise ValueError("Invalid checkpoint evidence identity.")
    return directory / f"checkpoint-evidence-{record.gate_id[7:]}-{record.attempt}.json"


def _record_binding(record: DeliveryVerificationRecord) -> dict[str, Any]:
    # The control record is the sole acceptance pointer. The artifact cannot
    # reference its own digest, and contains neither raw review nor source text.
    return replace(record, checkpoint_evidence_fingerprint=None, root_evidence_fingerprint=None).to_dict()


def _parse_evidence(
    payload: dict[str, Any], record: DeliveryVerificationRecord, *, plan_id: str, node_id: str
) -> DeliveryVerificationEvidence:
    parsed_record = parse_delivery_verification_record(payload.get("verification"))
    if (
        set(payload)
        != {
            "schema_version",
            "plan_id",
            "node_id",
            "verification",
            "completed_units",
            "constraint_ids",
            "obligation_results",
        }
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["plan_id"] != plan_id
        or payload["node_id"] != node_id
        or payload["verification"] != _record_binding(record)
        or parsed_record != replace(record, checkpoint_evidence_fingerprint=None, root_evidence_fingerprint=None)
        or not record.passed
        or record.semantic_status != "approved"
        or record.security_status != ("approved" if record.security_required else "not_run")
        or record.stop_code is not None
        or (node_id != "root" and (record.review_rule_fingerprints is None or record.plan_content_fingerprint is None))
        or (node_id == "root" and record.checkpoint_evidence_fingerprint is not None)
        or (node_id != "root" and record.root_evidence_fingerprint is not None)
    ):
        raise ValueError("Checkpoint evidence does not match accepted verification.")
    units = payload["completed_units"]
    if not isinstance(units, list) or not units:
        raise ValueError("Checkpoint evidence requires completed inputs.")
    seen: set[str] = set()
    for unit in units:
        if (
            not isinstance(unit, dict)
            or set(unit) != {"unit_id", "commit", "handoff_fingerprint"}
            or not isinstance(unit["unit_id"], str)
            or not unit["unit_id"]
            or unit["unit_id"] in seen
            or (
                unit["commit"] is not None
                and (not isinstance(unit["commit"], str) or not _GIT_ID.fullmatch(unit["commit"]))
            )
            or (
                unit["handoff_fingerprint"] is not None
                and (
                    not isinstance(unit["handoff_fingerprint"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", unit["handoff_fingerprint"])
                )
            )
        ):
            raise ValueError("Checkpoint execution evidence is invalid.")
        seen.add(unit["unit_id"])
    if _fingerprint(units) != record.completed_scope_fingerprint:
        raise ValueError("Checkpoint execution evidence changed.")
    constraints = payload["constraint_ids"]
    if (
        not isinstance(constraints, list)
        or any(not isinstance(key, str) or not key for key in constraints)
        or len(set(constraints)) != len(constraints)
    ):
        raise ValueError("Checkpoint constraint coverage is invalid.")
    results = payload["obligation_results"]
    if (
        not isinstance(results, list)
        or len(results) != record.obligation_count
        or record.obligation_satisfied_count != record.obligation_count
        or record.obligation_gap_count != 0
    ):
        raise ValueError("Checkpoint obligation coverage is incomplete.")
    seen = set()
    for result in results:
        if (
            not isinstance(result, dict)
            or set(result) != {"id", "outcome"}
            or not isinstance(result["id"], str)
            or not result["id"]
            or result["id"] in seen
            or result["outcome"] != "satisfied"
        ):
            raise ValueError("Checkpoint obligation result is invalid.")
        seen.add(result["id"])
    return DeliveryVerificationEvidence(
        plan_id,
        node_id,
        record,
        tuple(DeliveryVerificationCompletedUnit(**unit) for unit in units),
        tuple(constraints),
        tuple(DeliveryObligationAssessment(**result) for result in results),
    )


def store_checkpoint_evidence(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
) -> str:
    if snapshot.scope.node_id == "root":
        raise ValueError("Checkpoint evidence requires a checkpoint scope.")
    return _store_evidence(root, directory, snapshot, record, assessment)


def root_evidence_path(directory: Path, fingerprint: str) -> Path:
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise ValueError("Invalid root evidence identity.")
    return directory / f"root-evidence-{fingerprint[7:]}.json"


def store_root_evidence(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
    *,
    composition: CompositionResult | None = None,
    status: DeliveryStatusResult | None = None,
) -> str:
    if snapshot.scope.node_id != "root":
        raise ValueError("Root evidence requires the root scope.")
    if composition is not None and composition.child_refs and not composition.fallback:
        if status is None:
            raise ValueError("Root closure requires accepted child control records.")
        return _store_root_closure(root, directory, snapshot, record, assessment, composition, status)
    return _store_evidence(root, directory, snapshot, record, assessment)


def _store_evidence(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
) -> str:
    """Write an unaccepted artifact; only a matching persisted pass can activate it."""
    scope = snapshot.scope
    if not assessment.approved or assessment.findings:
        raise ValueError("Checkpoint evidence requires an approved assessment.")
    if any(getattr(record, key) != value for key, value in vars(snapshot.identity).items()):
        raise ValueError("Checkpoint evidence requires the captured identity.")
    payload = {
        "schema_version": 1,
        "plan_id": scope.plan_id,
        "node_id": scope.node_id,
        "verification": _record_binding(record),
        "completed_units": [unit.to_dict() for unit in snapshot.completed_units],
        "constraint_ids": list(scope.constraint_ids),
        "obligation_results": [result.to_dict() for result in assessment.obligation_results],
    }
    evidence = _parse_evidence(payload, record, plan_id=scope.plan_id, node_id=scope.node_id)
    if not evidence.covers(scope):
        raise ValueError("Checkpoint evidence must cover the captured scope exactly.")
    fingerprint = _fingerprint(payload)
    # Root attempt numbering may restart after authority or candidate changes.
    # Content addressing retains every distinct immutable result without collisions.
    path = (
        root_evidence_path(directory, fingerprint)
        if scope.node_id == "root"
        else checkpoint_evidence_path(directory, record)
    )
    existing = read_repair_state(root, path)
    if existing is not None:
        if existing != payload:
            raise ValueError("Checkpoint evidence for this attempt already exists.")
    else:
        write_repair_state(root, path, payload)
    return fingerprint


def load_checkpoint_evidence(
    root: Path, directory: Path, record: DeliveryVerificationRecord, *, plan_id: str, node_id: str
) -> DeliveryVerificationEvidence:
    """Load explicit results, never infer them from counts or replay review audit."""
    if node_id == "root" or not record.passed or record.checkpoint_evidence_fingerprint is None:
        raise ValueError("Accepted checkpoint evidence is unavailable.")
    try:
        payload = read_repair_state(root, checkpoint_evidence_path(directory, record))
        intact = payload is not None and _fingerprint(payload) == record.checkpoint_evidence_fingerprint
    except (RecursionError, TypeError):
        raise ValueError("Accepted checkpoint evidence is invalid.") from None
    if not intact:
        raise ValueError("Accepted checkpoint evidence is unavailable.")
    return _parse_evidence(payload, record, plan_id=plan_id, node_id=node_id)


def load_root_evidence(
    root: Path,
    directory: Path,
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    unit_order: tuple[str, ...] | None = None,
) -> DeliveryVerificationEvidence:
    """Load the accepted root's typed results, not its aggregate success counts."""
    if not record.passed or record.root_evidence_fingerprint is None:
        raise ValueError("Accepted root evidence is unavailable.")
    try:
        payload = read_repair_state(root, root_evidence_path(directory, record.root_evidence_fingerprint))
        intact = payload is not None and _fingerprint(payload) == record.root_evidence_fingerprint
    except (RecursionError, TypeError):
        raise ValueError("Accepted root evidence is invalid.") from None
    if not intact:
        raise ValueError("Accepted root evidence is unavailable.")
    if payload.get("child_evidence") is not None:
        try:
            return _parse_root_closure(root, directory, payload, record, plan_id=plan_id, unit_order=unit_order)
        except (KeyError, TypeError, AttributeError, RecursionError):
            raise ValueError("Accepted root closure is invalid.") from None
    return _parse_evidence(payload, record, plan_id=plan_id, node_id="root")


def _parse_root_closure(
    root: Path,
    directory: Path,
    payload: dict[str, Any],
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    unit_order: tuple[str, ...] | None,
) -> DeliveryVerificationEvidence:
    """Resolve bounded immediate children for deterministic coverage, never into prompts."""
    from core.delivery_composition import MAX_COMPOSITION_CHILDREN, composition_evidence_path, fingerprint

    if set(payload) != {
        "schema_version",
        "plan_id",
        "node_id",
        "verification",
        "completed_units",
        "constraint_ids",
        "obligation_results",
        "child_evidence",
    }:
        raise ValueError("Invalid root closure fields.")
    children = payload["child_evidence"]
    if not isinstance(children, list) or not 0 < len(children) <= MAX_COMPOSITION_CHILDREN:
        raise ValueError("Invalid root closure fan-in.")
    composed = read_repair_state(root, composition_evidence_path(directory, record.composition_evidence_fingerprint))
    if (
        not composed
        or fingerprint(composed) != record.composition_evidence_fingerprint
        or composed.get("gate_id") != record.gate_id
        or composed.get("compact") is not True
        or composed.get("review_kind") != "semantic"
    ):
        raise ValueError("Accepted root composition is unavailable.")
    control = composed["control"]
    if (
        control["disposition"] != "approved"
        or control["findings"]
        or control["obligation_results"] != payload["obligation_results"]
    ):
        raise ValueError("Root closure does not match accepted direct results.")
    decisions = control["checkpoint_results"]
    ids = [item["id"] for item in decisions]
    if len(set(ids)) != len(ids) or any(item["outcome"] != "applicable" for item in decisions):
        raise ValueError("Root closure requires every child to be applicable.")
    origins = {}
    units = list(payload["completed_units"])
    constraints = set(payload["constraint_ids"])
    results = list(payload["obligation_results"])
    for child in children:
        if not isinstance(child, dict) or set(child) != {"node_id", "verification"} or child["node_id"] in origins:
            raise ValueError("Invalid child closure reference.")
        origin = parse_delivery_verification_record(child["verification"])
        evidence = load_checkpoint_evidence(root, directory, origin, plan_id=plan_id, node_id=child["node_id"])
        origins[child["node_id"]] = origin.checkpoint_evidence_fingerprint
        units.extend(unit.to_dict() for unit in evidence.completed_units)
        constraints.update(evidence.constraint_ids)
        results.extend(result.to_dict() for result in evidence.obligation_results)
    if origins != composed["origins"] or set(origins) != set(ids):
        raise ValueError("Root closure origins changed.")
    # Restore the captured declaration order, then use the same immutable execution
    # fingerprint check as full evidence. No per-unit order list enters the artifact.
    by_id = {unit["unit_id"]: unit for unit in units}
    if unit_order is None or len(by_id) != len(units) or set(by_id) != set(unit_order):
        raise ValueError("Root closure execution inputs changed.")
    expanded = {key: value for key, value in payload.items() if key != "child_evidence"}
    expanded.update(
        completed_units=[by_id[key] for key in unit_order],
        constraint_ids=sorted(constraints),
        obligation_results=results,
    )
    return _parse_evidence(expanded, record, plan_id=plan_id, node_id="root")


def _store_root_closure(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
    composition: CompositionResult,
    status: DeliveryStatusResult,
) -> str:
    if (
        not assessment.approved
        or assessment.findings
        or any(getattr(record, key) != value for key, value in vars(snapshot.identity).items())
    ):
        raise ValueError("Root closure requires the captured approved candidate.")
    covered = set()
    inherited_constraints = set()
    children = []
    expected = {unit.unit_id: unit for unit in snapshot.completed_units}
    for ref in composition.child_refs:
        from core.delivery_reverification import composition_origin

        origin = composition_origin(status, record, ref["id"], digest=ref["evidence_fingerprint"])
        if origin.checkpoint_evidence_fingerprint != ref["evidence_fingerprint"]:
            raise ValueError("Root closure child changed.")
        child = load_checkpoint_evidence(root, directory, origin, plan_id=snapshot.scope.plan_id, node_id=ref["id"])
        if any(expected.get(unit.unit_id) != unit for unit in child.completed_units):
            raise ValueError("Root closure child execution changed.")
        covered.update(unit.unit_id for unit in child.completed_units)
        inherited_constraints.update(child.constraint_ids)
        children.append({"node_id": ref["id"], "verification": origin.to_dict()})
    payload = {
        "schema_version": 1,
        "plan_id": snapshot.scope.plan_id,
        "node_id": "root",
        "verification": _record_binding(record),
        "completed_units": [unit.to_dict() for unit in snapshot.completed_units if unit.unit_id not in covered],
        "constraint_ids": [key for key in snapshot.scope.constraint_ids if key not in inherited_constraints],
        "obligation_results": [item.to_dict() for item in assessment.obligation_results],
        "child_evidence": children,
    }
    evidence = _parse_root_closure(
        root,
        directory,
        payload,
        record,
        plan_id=snapshot.scope.plan_id,
        unit_order=tuple(unit.unit_id for unit in snapshot.completed_units),
    )
    if not evidence.covers(snapshot.scope):
        raise ValueError("Root closure must cover the complete scope.")
    digest = _fingerprint(payload)
    write_repair_state(root, root_evidence_path(directory, digest), payload)
    return digest
