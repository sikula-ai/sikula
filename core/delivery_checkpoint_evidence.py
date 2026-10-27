"""Private node results, bound to accepted control state rather than audit replay.

These describe the reviewed candidate only. Loading them does not establish their
applicability to later code, or replace the mandatory root review.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from heapq import merge
from itertools import zip_longest
import json
from pathlib import Path
import re
from typing import Any, Iterator, TYPE_CHECKING

from core.delivery_evidence_resolution import EvidenceResolver, evidence_operation
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_review import DeliveryIntegrationAssessment, DeliveryObligationAssessment
from core.delivery_verification_scope import DeliveryVerificationCompletedUnit, DeliveryVerificationScope

if TYPE_CHECKING:
    from core.delivery_composition import CompositionResult
    from core.delivery_composition_scope import DeliveryCompositionAttempt
    from core.delivery_progress import DeliveryStatusResult
    from core.delivery_verification import DeliveryVerificationSnapshot


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


@dataclass(frozen=True)
class _EvidenceClosure:
    """Direct typed results and child coverage, without retained child control records."""

    direct_completed_units: tuple[DeliveryVerificationCompletedUnit, ...]
    direct_constraint_ids: tuple[str, ...]
    direct_obligation_results: tuple[DeliveryObligationAssessment, ...]
    children: tuple[_EvidenceClosure, ...] = ()
    _sorted_units: tuple[DeliveryVerificationCompletedUnit, ...] = field(init=False, repr=False)
    _sorted_results: tuple[DeliveryObligationAssessment, ...] = field(init=False, repr=False)
    unit_count: int = field(init=False)
    obligation_count: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_sorted_units", tuple(sorted(self.direct_completed_units, key=lambda item: item.unit_id))
        )
        object.__setattr__(
            self, "_sorted_results", tuple(sorted(self.direct_obligation_results, key=lambda item: item.id))
        )
        object.__setattr__(
            self, "unit_count", len(self.direct_completed_units) + sum(child.unit_count for child in self.children)
        )
        object.__setattr__(
            self,
            "obligation_count",
            len(self.direct_obligation_results) + sum(child.obligation_count for child in self.children),
        )

    def iter_completed_units(self) -> Iterator[DeliveryVerificationCompletedUnit]:
        yield from merge(
            self._sorted_units,
            *(child.iter_completed_units() for child in self.children),
            key=lambda item: item.unit_id,
        )

    def iter_obligation_results(self) -> Iterator[DeliveryObligationAssessment]:
        yield from merge(
            self._sorted_results,
            *(child.iter_obligation_results() for child in self.children),
            key=lambda item: item.id,
        )

    def iter_constraint_ids(self) -> Iterator[str]:
        # Shared constraints legitimately occur in several child scopes.
        previous = None
        for key in merge(self.direct_constraint_ids, *(child.iter_constraint_ids() for child in self.children)):
            if key != previous:
                yield key
                previous = key


@dataclass(frozen=True)
class DeliveryVerificationEvidence:
    """Accepted binding plus exact compact coverage for this requested node."""

    plan_id: str
    node_id: str
    verification: DeliveryVerificationRecord
    closure: _EvidenceClosure

    def covers(self, scope: DeliveryVerificationScope) -> bool:
        return (
            self.plan_id == scope.plan_id
            and self.node_id == scope.node_id
            and self.verification.security_required == scope.security_required
            and all(
                actual == expected
                for actual, expected in zip_longest(
                    (unit.unit_id for unit in self.closure.iter_completed_units()), sorted(scope.unit_ids)
                )
            )
            and all(
                actual == expected
                for actual, expected in zip_longest(self.closure.iter_constraint_ids(), sorted(scope.constraint_ids))
            )
            and all(
                actual == expected
                for actual, expected in zip_longest(
                    (result.id for result in self.closure.iter_obligation_results()), sorted(scope.obligation_ids)
                )
            )
        )

    def matches_results(self, results: list[DeliveryObligationAssessment], *, inherited_count: int | None) -> bool:
        if inherited_count is None:
            return not self.closure.children and tuple(results) == self.closure.direct_obligation_results
        if len(results) + inherited_count != self.closure.obligation_count:
            return False
        expected = iter(sorted(results, key=lambda item: item.id))
        wanted = next(expected, None)
        for actual in self.closure.iter_obligation_results():
            if wanted is not None and actual.id == wanted.id:
                if actual.outcome != wanted.outcome:
                    return False
                wanted = next(expected, None)
        return wanted is None


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


def _parse_direct_evidence(
    payload: dict[str, Any],
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    node_id: str,
    closure: bool = False,
) -> DeliveryVerificationEvidence:
    parsed_record = parse_delivery_verification_record(payload.get("verification"))
    if (
        set(payload)
        != (
            {
                "schema_version",
                "plan_id",
                "node_id",
                "verification",
                "completed_units",
                "constraint_ids",
                "obligation_results",
            }
            | ({"child_evidence"} if closure else set())
        )
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
    if not isinstance(units, list) or (not units and not closure):
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
    if not closure and _fingerprint(units) != record.completed_scope_fingerprint:
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
        or (not closure and len(results) != record.obligation_count)
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
        _EvidenceClosure(
            tuple(DeliveryVerificationCompletedUnit(**unit) for unit in units),
            tuple(sorted(constraints)),
            tuple(DeliveryObligationAssessment(**result) for result in results),
        ),
    )


def _charge_direct_evidence(evidence: _EvidenceClosure, resolver: EvidenceResolver) -> None:
    # Charge only local allocations. Walking children here would re-account every
    # descendant at each ancestor and defeat the compact representation.
    resolver.materialize({**vars(evidence), "children": tuple(None for _ in evidence.children)})


def _parse_evidence(
    payload: dict[str, Any],
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    node_id: str,
    resolver: EvidenceResolver | None = None,
) -> DeliveryVerificationEvidence:
    evidence = _parse_direct_evidence(payload, record, plan_id=plan_id, node_id=node_id)
    if resolver is not None:
        resolver.expand(
            evidence.closure.unit_count
            + evidence.closure.obligation_count
            + len(evidence.closure.direct_constraint_ids)
        )
        _charge_direct_evidence(evidence.closure, resolver)
    return evidence


def _validate_closure_coverage(
    evidence: DeliveryVerificationEvidence,
    unit_order: tuple[str, ...] | None,
    resolver: EvidenceResolver,
) -> None:
    """Check exact identities and the existing ordered execution hash incrementally."""
    if not evidence.closure.unit_count:
        raise ValueError("Checkpoint evidence requires completed inputs.")
    if evidence.closure.obligation_count != evidence.verification.obligation_count:
        raise ValueError("Checkpoint obligation coverage is incomplete.")
    previous = None
    for result in evidence.closure.iter_obligation_results():
        resolver.expand(1)
        if result.id == previous:
            raise ValueError("Checkpoint obligation result is invalid.")
        previous = result.id
    for _ in evidence.closure.iter_constraint_ids():
        resolver.expand(1)

    units = evidence.closure.iter_completed_units()
    # Flat compositions can bind declaration order, whereas nested nodes
    # use canonical ID order. Only that explicit noncanonical root needs an index.
    if unit_order is not None and any(left >= right for left, right in zip(unit_order, unit_order[1:])):
        by_id = {}
        for unit in units:
            resolver.expand(1)
            if unit.unit_id in by_id:
                raise ValueError("Root closure execution inputs changed.")
            by_id[unit.unit_id] = unit
        resolver.materialize(by_id)
        if len(unit_order) != len(by_id) or set(unit_order) != set(by_id):
            raise ValueError("Root closure execution inputs changed.")
        units = (by_id[key] for key in unit_order)
    digest = sha256(b"[")
    first = True
    previous = None
    expected = iter(unit_order) if unit_order is not None else None
    for unit in units:
        resolver.expand(1)
        if expected is not None:
            if unit.unit_id != next(expected, None):
                raise ValueError("Root closure execution inputs changed.")
        elif unit.unit_id == previous:
            raise ValueError("Root closure execution inputs changed.")
        if not first:
            digest.update(b",")
        digest.update(
            json.dumps(unit.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        )
        previous, first = unit.unit_id, False
    if expected is not None and next(expected, None) is not None:
        raise ValueError("Root closure execution inputs changed.")
    digest.update(b"]")
    if "sha256:" + digest.hexdigest() != evidence.verification.completed_scope_fingerprint:
        raise ValueError("Checkpoint execution evidence changed.")


def store_checkpoint_evidence(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
    *,
    composition: CompositionResult | None = None,
    status: DeliveryStatusResult | None = None,
) -> str:
    if snapshot.scope.node_id == "root":
        raise ValueError("Checkpoint evidence requires a checkpoint scope.")
    if composition is not None and composition.child_refs and not composition.fallback:
        if status is None:
            raise ValueError("Checkpoint closure requires accepted children.")
        from core.delivery_composition_scope import capture_composition_attempt

        attempt = capture_composition_attempt(status, record, snapshot.composition_scope)
        payload = build_node_closure(root, directory, snapshot, record, assessment, composition, attempt)
        path = checkpoint_evidence_path(directory, record)
        existing = read_repair_state(root, path)
        if existing is not None and existing != payload:
            raise ValueError("Checkpoint closure for this attempt already exists.")
        if existing is None:
            write_repair_state(root, path, payload)
        return _fingerprint(payload)
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


@evidence_operation
def load_checkpoint_evidence(
    root: Path,
    directory: Path,
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    node_id: str,
    _ancestors: frozenset[str] = frozenset(),
    resolver: EvidenceResolver | None = None,
) -> DeliveryVerificationEvidence:
    """Load explicit results, never infer them from counts or replay review audit."""
    resolver = resolver or EvidenceResolver()
    with resolver.visit(str(root), plan_id, node_id, record.gate_id, str(record.attempt)):
        from core.delivery_candidate_control import expand_record

        record = expand_record(root, directory, record, resolver=resolver)

        if node_id == "root" or not record.passed or record.checkpoint_evidence_fingerprint is None:
            raise ValueError("Accepted checkpoint evidence is unavailable.")
        try:
            payload = resolver.read(root, checkpoint_evidence_path(directory, record), reader=read_repair_state)
            intact = payload is not None and _fingerprint(payload) == record.checkpoint_evidence_fingerprint
        except (RecursionError, TypeError):
            raise ValueError("Accepted checkpoint evidence is invalid.") from None
        if not intact:
            raise ValueError("Accepted checkpoint evidence is unavailable.")
        if payload.get("child_evidence") is not None:
            if node_id in _ancestors or len(_ancestors) >= 256:
                raise ValueError("Invalid recursive checkpoint evidence.")
            try:
                return _parse_node_closure(
                    root,
                    directory,
                    payload,
                    record,
                    plan_id=plan_id,
                    node_id=node_id,
                    unit_order=None,
                    _ancestors=_ancestors | {node_id},
                    resolver=resolver,
                )
            except (KeyError, TypeError, AttributeError, RecursionError):
                raise ValueError("Accepted checkpoint closure is invalid.") from None
        return _parse_evidence(payload, record, plan_id=plan_id, node_id=node_id, resolver=resolver)


@evidence_operation
def load_root_evidence(
    root: Path,
    directory: Path,
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    unit_order: tuple[str, ...] | None = None,
    resolver: EvidenceResolver | None = None,
) -> DeliveryVerificationEvidence:
    """Load the accepted root's typed results, not its aggregate success counts."""
    resolver = resolver or EvidenceResolver()
    with resolver.visit(str(root), plan_id, "root", record.gate_id, str(record.attempt)):
        if not record.passed or record.root_evidence_fingerprint is None:
            raise ValueError("Accepted root evidence is unavailable.")
        try:
            payload = resolver.read(
                root, root_evidence_path(directory, record.root_evidence_fingerprint), reader=read_repair_state
            )
            intact = payload is not None and _fingerprint(payload) == record.root_evidence_fingerprint
        except (RecursionError, TypeError):
            raise ValueError("Accepted root evidence is invalid.") from None
        if not intact:
            raise ValueError("Accepted root evidence is unavailable.")
        if payload.get("child_evidence") is not None:
            try:
                return _parse_node_closure(
                    root,
                    directory,
                    payload,
                    record,
                    plan_id=plan_id,
                    node_id="root",
                    unit_order=unit_order,
                    resolver=resolver,
                )
            except (KeyError, TypeError, AttributeError, RecursionError):
                raise ValueError("Accepted root closure is invalid.") from None
        return _parse_evidence(payload, record, plan_id=plan_id, node_id="root", resolver=resolver)


@evidence_operation
def _parse_node_closure(
    root: Path,
    directory: Path,
    payload: dict[str, Any],
    record: DeliveryVerificationRecord,
    *,
    plan_id: str,
    node_id: str,
    unit_order: tuple[str, ...] | None,
    _ancestors: frozenset[str] = frozenset(),
    resolver: EvidenceResolver | None = None,
) -> DeliveryVerificationEvidence:
    """Resolve bounded immediate children for deterministic coverage, never into prompts."""
    from core.delivery_composition import MAX_COMPOSITION_CHILDREN, composition_evidence_path, fingerprint

    resolver = resolver or EvidenceResolver()

    direct = _parse_direct_evidence(payload, record, plan_id=plan_id, node_id=node_id, closure=True)
    _charge_direct_evidence(direct.closure, resolver)
    children = payload["child_evidence"]
    if not isinstance(children, list) or not 0 < len(children) <= MAX_COMPOSITION_CHILDREN:
        raise ValueError("Invalid root closure fan-in.")
    composed = resolver.read(
        root, composition_evidence_path(directory, record.composition_evidence_fingerprint), reader=read_repair_state
    )
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
    resolved_children = []
    for child in children:
        if not isinstance(child, dict) or set(child) != {"node_id", "verification"} or child["node_id"] in origins:
            raise ValueError("Invalid child closure reference.")
        origin = parse_delivery_verification_record(child["verification"])
        evidence = load_checkpoint_evidence(
            root, directory, origin, plan_id=plan_id, node_id=child["node_id"], _ancestors=_ancestors, resolver=resolver
        )
        origins[child["node_id"]] = origin.checkpoint_evidence_fingerprint
        resolved_children.append(evidence.closure)
    if origins != composed["origins"] or set(origins) != set(ids):
        raise ValueError("Root closure origins changed.")
    evidence = replace(direct, closure=replace(direct.closure, children=tuple(resolved_children)))
    resolver.materialize(tuple(None for _ in evidence.closure.children))
    _validate_closure_coverage(evidence, unit_order, resolver)
    return evidence


def _store_root_closure(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
    composition: CompositionResult,
    status: DeliveryStatusResult,
) -> str:
    from core.delivery_composition_scope import capture_composition_attempt

    attempt = capture_composition_attempt(status, record, snapshot.composition_scope)
    payload = build_node_closure(root, directory, snapshot, record, assessment, composition, attempt)
    digest = _fingerprint(payload)
    write_repair_state(root, root_evidence_path(directory, digest), payload)
    return digest


def build_node_closure(
    root: Path,
    directory: Path,
    snapshot: DeliveryVerificationSnapshot,
    record: DeliveryVerificationRecord,
    assessment: DeliveryIntegrationAssessment,
    composition: CompositionResult,
    attempt: DeliveryCompositionAttempt,
) -> dict[str, Any]:
    """Check direct results plus immediate typed children against exact node scope.

    This constructs private evidence, not a public nested execution format. The
    delivery adapter owns publication and acceptance of its final-gate artifact.
    """
    attempt.assert_record(record)
    attempt.assert_candidate(snapshot.identity)
    if attempt.scope.parent != snapshot.scope or {ref["id"] for ref in composition.child_refs} != {
        child.node_id for child in attempt.scope.children
    }:
        raise ValueError("Node closure immediate coverage changed.")
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
    with EvidenceResolver() as resolver:
        for ref in composition.child_refs:
            origin = attempt.origin(ref["id"], digest=ref["evidence_fingerprint"], resolver=resolver)
            if origin.checkpoint_evidence_fingerprint != ref["evidence_fingerprint"]:
                raise ValueError("Root closure child changed.")
            child = load_checkpoint_evidence(
                root, directory, origin, plan_id=snapshot.scope.plan_id, node_id=ref["id"], resolver=resolver
            )
            if not child.covers(attempt.scope.child(ref["id"])):
                raise ValueError("Node closure child coverage changed.")
            if any(expected.get(unit.unit_id) != unit for unit in child.closure.iter_completed_units()):
                raise ValueError("Root closure child execution changed.")
            covered.update(unit.unit_id for unit in child.closure.iter_completed_units())
            inherited_constraints.update(child.closure.iter_constraint_ids())
            children.append((ref["id"], origin))
    # Publication is a separate phase; no read context survives these writes.
    from core.delivery_candidate_control import compact_record

    children = [
        {"node_id": node, "verification": compact_record(root, directory, origin).to_dict()}
        for node, origin in children
    ]
    payload = {
        "schema_version": 1,
        "plan_id": snapshot.scope.plan_id,
        "node_id": snapshot.scope.node_id,
        "verification": _record_binding(record),
        "completed_units": [unit.to_dict() for unit in snapshot.completed_units if unit.unit_id not in covered],
        "constraint_ids": [key for key in snapshot.scope.constraint_ids if key not in inherited_constraints],
        "obligation_results": [item.to_dict() for item in assessment.obligation_results],
        "child_evidence": children,
    }
    evidence = _parse_node_closure(
        root,
        directory,
        payload,
        record,
        plan_id=snapshot.scope.plan_id,
        node_id=snapshot.scope.node_id,
        unit_order=tuple(unit.unit_id for unit in snapshot.completed_units),
    )
    if not evidence.covers(snapshot.scope):
        raise ValueError("Root closure must cover the complete scope.")
    return payload
