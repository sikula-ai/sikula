"""Bounded candidate review control, separate from checkpoint admission."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING, Callable

from core.delivery_evidence_resolution import EvidenceResolver, evidence_operation
from core.delivery_checkpoint_evidence import load_checkpoint_evidence
from core.delivery_composition_scope import DeliveryCompositionScope, delivery_composition_scope
from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_scope import DeliveryVerificationScope


if TYPE_CHECKING:
    from core.delivery_verification import DeliveryVerificationSnapshot
    from core.delivery_verification_review import DeliveryIntegrationAssessment
    from core.delivery_composition import CompositionResult
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewResult


_ROLES = {"semantic", "security"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CandidateReviewBoundsError(ValueError):
    """Candidate hierarchy or rendered packets exceed supported review bounds."""


def new_reverification() -> dict[str, Any]:
    return {"children": {}, "reassessments": {}, "fallbacks": {}}


def parse_reverification(value: Any, parent: dict[str, Any]) -> dict[str, Any]:
    """Accept bounded local records; descendants are content-addressed references."""
    if isinstance(value, dict) and set(value) == {"reference"}:
        if not isinstance(value["reference"], str) or not _DIGEST.fullmatch(value["reference"]):
            raise ValueError("Invalid candidate node reference.")
        if not parent.get("composition_attempted") and not parent.get("security_composition_attempted"):
            raise ValueError("Candidate node reference requires a reserved exchange.")
        return dict(value)
    if not isinstance(value, dict) or set(value) not in (
        {"children", "reassessments", "fallbacks"},
        {"children", "reassessments", "fallbacks", "refreshes"},
    ):
        raise ValueError("Invalid candidate re-verification control.")
    if not parent.get("composition_attempted") and not parent.get("security_composition_attempted"):
        raise ValueError("Candidate review requires a reserved composition exchange.")
    from core.delivery_composition import MAX_COMPOSITION_CHILDREN

    refreshes = value.get("refreshes", {})
    if not isinstance(refreshes, dict) or set(refreshes) - _ROLES:
        raise ValueError("Invalid candidate refresh roles.")
    for role, request in refreshes.items():
        prefix = "" if role == "semantic" else "security_"
        if (
            not isinstance(request, dict)
            or set(request) != {"nodes", "review_reserved"}
            or type(request["review_reserved"]) is not bool
            or not isinstance(request["nodes"], dict)
            or not 0 < len(request["nodes"]) <= MAX_COMPOSITION_CHILDREN
            or not parent.get(prefix + "composition_attempted")
            or (parent.get(prefix + "composition_evidence_fingerprint") and not request["review_reserved"])
        ):
            raise ValueError("Invalid candidate refresh reservation.")
        for node, item in request["nodes"].items():
            if (
                not isinstance(node, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", node)
                or not isinstance(item, dict)
                or set(item) != {"origin", "reason"}
                or not isinstance(item["origin"], str)
                or not _DIGEST.fullmatch(item["origin"])
                or item["reason"] not in ("delta_bytes", "delta_paths")
            ):
                raise ValueError("Invalid candidate refresh prerequisite.")
    children = value["children"]
    if not isinstance(children, dict) or len(children) > MAX_COMPOSITION_CHILDREN:
        raise ValueError("Invalid candidate re-verification fan-in.")
    for key, child in children.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key)
            or not isinstance(child, dict)
            or set(child) not in ({"origin", "verification"}, {"origin", "verification", "semantic_gap"})
            or ("semantic_gap" in child and child["semantic_gap"] is not True)
            or not isinstance(child["origin"], str)
            or not _DIGEST.fullmatch(child["origin"])
            or not isinstance(child["verification"], dict)
            or (
                "reverification" in child["verification"]
                and (
                    not isinstance(child["verification"]["reverification"], dict)
                    or set(child["verification"]["reverification"]) != {"reference"}
                )
            )
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
        if record.attempt != 1 or record.root_evidence_fingerprint:
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
    if not parent.get("security_required", False) and (
        "security" in rounds or "security" in roles or "security" in refreshes
    ):
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
    return accepted_composition_origin(historical, parent, node_id, digest=digest)


def accepted_composition_origin(
    historical: DeliveryVerificationRecord,
    parent: DeliveryVerificationRecord | None,
    node_id: str,
    *,
    digest: str | None = None,
) -> DeliveryVerificationRecord:
    """Select within the parent's captured attempt without traversing a plan."""
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


@evidence_operation
def validate_reverification(
    status: DeliveryStatusResult,
    record: DeliveryVerificationRecord,
    cfg: dict[str, Any] | None = None,
    *,
    scope: DeliveryCompositionScope | None = None,
    _seen: set[str] | None = None,
    resolver: EvidenceResolver | None = None,
) -> None:
    from core.delivery_candidate_control import MAX_CANDIDATE_NODES, expanded_child

    resolver = resolver or EvidenceResolver()
    _seen = set() if _seen is None else _seen
    if record.gate_id in _seen or len(_seen) >= MAX_CANDIDATE_NODES:
        raise ValueError("Repeated or oversized candidate review tree.")
    _seen.add(record.gate_id)
    if record.reverification is None:
        return
    if "reference" in record.reverification:
        from core.delivery_candidate_control import expand_record

        root = Path(status.project_root)
        record = expand_record(
            root, delivery_progress_path(root, status.plan.plan_id).parent, record, resolver=resolver
        )
    parse_reverification(record.reverification, record.to_dict())
    composition_scope = scope or delivery_composition_scope(
        status.plan, DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
    )
    if composition_scope is None:
        raise ValueError("Candidate review requires captured composition authority.")
    root = Path(status.project_root)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    from core.delivery_composition import load_composition

    for request in record.reverification.get("refreshes", {}).values():
        if not composition_scope.retained_authority_json:
            raise ValueError("Candidate refresh requires independent parent authority.")
        for node_id, item in request["nodes"].items():
            child_scope = composition_scope.child(node_id)
            historical = status.checkpoint_verifications.get(node_id)
            if historical is None or historical.checkpoint_evidence_fingerprint != item["origin"]:
                raise ValueError("Candidate refresh admission changed.")
            evidence = load_checkpoint_evidence(
                root, directory, historical, plan_id=status.plan.plan_id, node_id=node_id, resolver=resolver
            )
            if not evidence.covers(child_scope):
                raise ValueError("Candidate refresh coverage changed.")
            if request["review_reserved"]:
                entry = record.reverification["children"].get(node_id)
                if entry is None or not expanded_child(status, entry, resolver=resolver).passed:
                    raise ValueError("Candidate refresh prerequisites are incomplete.")
    for role, digest in (
        ("semantic", record.composition_evidence_fingerprint),
        ("security", record.security_composition_evidence_fingerprint),
    ):
        if digest:
            load_composition(status, record, review_kind=role, scope=composition_scope, resolver=resolver)
    for role, round_state in record.reverification["reassessments"].items():
        field = (
            "composition_evidence_fingerprint" if role == "semantic" else "security_composition_evidence_fingerprint"
        )
        initial = load_composition(
            status,
            replace(record, **{field: round_state["initial"]}),
            review_kind=role,
            scope=composition_scope,
            resolver=resolver,
        )
        if not initial.fallback or not (initial.assessment.approved or (role == "semantic" and initial.has_parent_gap)):
            raise ValueError("Candidate review did not originate in ordinary uncertainty.")
    for role, digest in record.reverification["fallbacks"].items():
        if digest:
            load_full_result(status, record, role, scope=composition_scope.parent, resolver=resolver)
    for node_id, entry in record.reverification["children"].items():
        child_scope = composition_scope.child(node_id)
        historical = status.checkpoint_verifications.get(node_id)
        if historical is None or historical.checkpoint_evidence_fingerprint != entry["origin"]:
            raise ValueError("Candidate re-verification admission changed.")
        child = expanded_child(status, entry, resolver=resolver)
        if cfg is not None:
            snapshot = candidate_snapshot(status, cfg, record, node_id)
            if snapshot.scope != child_scope or any(
                getattr(child, key) != val for key, val in vars(snapshot.identity).items()
            ):
                raise ValueError("Candidate re-verification authority changed.")
        child_status = replace(status, verification_node=node_id)
        validate_reverification(child_status, child, cfg, _seen=_seen, resolver=resolver)
        if child.passed:
            evidence = load_checkpoint_evidence(
                root, directory, child, plan_id=status.plan.plan_id, node_id=node_id, resolver=resolver
            )
            if not evidence.covers(child_scope):
                raise ValueError("Candidate re-verification coverage changed.")
    if (
        record.passed
        and semantic_gap_ids(record)
        and not semantic_fallback_covers_gaps(status, record, resolver=resolver)
    ):
        raise ValueError("Final semantic approval predates a rejected candidate child.")


def review_result_path(directory: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid candidate review result identity.")
    return directory / f"candidate-review-{digest[7:]}.json"


@evidence_operation
def _full_result_payload(
    status: DeliveryStatusResult,
    parent: DeliveryVerificationRecord,
    role: str,
    *,
    resolver: EvidenceResolver | None = None,
) -> dict[str, Any]:
    from core.delivery_composition import fingerprint
    from core.delivery_repair_storage import read_repair_state

    digest = parent.reverification["fallbacks"][role]
    root = Path(status.project_root)
    resolver = resolver or EvidenceResolver()
    payload = resolver.read(
        root,
        review_result_path(delivery_progress_path(root, status.plan.plan_id).parent, digest),
        reader=read_repair_state,
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
        if entry["verification"]["semantic_status"] == "rejected" or entry.get("semantic_gap", False)
    )


@evidence_operation
def semantic_fallback_covers_gaps(
    status: DeliveryStatusResult, record: DeliveryVerificationRecord, *, resolver: EvidenceResolver | None = None
) -> bool:
    if not (record.reverification or {}).get("fallbacks", {}).get("semantic"):
        return False
    payload = _full_result_payload(status, record, "semantic", resolver=resolver)
    return set(semantic_gap_ids(record)).issubset(payload["semantic_gaps"])


@evidence_operation
def reverification_budget_exhausted(
    status: DeliveryStatusResult, record: DeliveryVerificationRecord, *, resolver: EvidenceResolver | None = None
) -> bool:
    from core.delivery_verification_model import delivery_verification_is_boundary_stop

    from core.delivery_candidate_control import expanded_child

    with resolver.visit(record.gate_id, str(record.attempt), "budget"):
        if delivery_verification_is_boundary_stop(record):
            return False
        if record.stop_code == "delivery_verification.reverification_budget_exhausted":
            return True
        if record.reverification is None:
            return False
        fallbacks = record.reverification["fallbacks"]
        return (
            any(digest is None for digest in fallbacks.values())
            or (
                "semantic" in fallbacks
                and record.stop_code != "delivery_verification.candidate_repair_required"
                and not semantic_fallback_covers_gaps(status, record, resolver=resolver)
            )
            or any(
                reverification_budget_exhausted(
                    status, expanded_child(status, entry, resolver=resolver), resolver=resolver
                )
                for entry in record.reverification["children"].values()
            )
        )


@evidence_operation
def load_full_result(
    status: DeliveryStatusResult,
    parent: DeliveryVerificationRecord,
    role: str,
    *,
    scope: DeliveryVerificationScope | None = None,
    resolver: EvidenceResolver | None = None,
) -> DeliveryIntegrationAssessment:
    from core.delivery_verification_review import parse_delivery_integration_review
    import json

    payload = _full_result_payload(status, parent, role, resolver=resolver)
    scope = scope or DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
    return parse_delivery_integration_review(
        json.dumps(payload["assessment"]),
        known_unit_ids=set(scope.unit_ids),
        known_obligation_ids=set(scope.obligation_ids) if role == "semantic" else set(),
    )


def preflight_node_review_packets(
    composition_scope: DeliveryCompositionScope, cfg: dict[str, Any], cwd: Path, source: str, *, plan=None
) -> tuple[int, bool]:
    """Size local routes, not mandatory expanded fallback for composing children."""
    from core.delivery_review_route import preview_composition_context, review_route
    from core.delivery_verification_validation import preview_delivery_validation_summary

    parent_bytes = 0
    conditional = False
    for scope in (composition_scope.parent, *composition_scope.children):
        local = (
            composition_scope
            if scope is composition_scope.parent
            else delivery_composition_scope(plan, scope)
            if plan is not None
            else None
        )
        validation = preview_delivery_validation_summary(
            cfg,
            project_root=cwd,
            include_final_checks=composition_scope.include_final_checks and scope is composition_scope.parent,
        )
        for role in ("semantic", "security") if scope.security_required else ("semantic",):
            route = review_route(
                scope,
                cfg,
                cwd,
                source,
                role,
                context=preview_composition_context(local, role),
                validation=validation,
            )
            if not route.ready:
                raise CandidateReviewBoundsError("No bounded candidate review route is available.")
            conditional = conditional or not route.full_available
            if scope is composition_scope.parent:
                parent_bytes = max(parent_bytes, route.packet_bytes)
    return parent_bytes, conditional


@evidence_operation
def preflight_candidate_tree(
    status: DeliveryStatusResult,
    cfg: dict[str, Any],
    snapshot: DeliveryVerificationSnapshot,
    cwd: Path,
    source: str,
    *,
    record: DeliveryVerificationRecord | None = None,
    resolver: EvidenceResolver | None = None,
) -> None:
    """Check both roles and all possible child routes before reserving any calls."""
    from core.delivery_composition import CompositionRefresh, build_composition_context
    from core.delivery_review_route import review_route
    from core.delivery_verification import build_delivery_verification_snapshot

    from core.delivery_candidate_control import expanded_child

    resolver = resolver or EvidenceResolver()
    record = record or status.verification
    if record is not None and record.gate_id != snapshot.identity.gate_id:
        record = None
    pending = [(snapshot, record)]
    seen = set()
    while pending:
        current, record = pending.pop()
        node = current.scope.node_id
        if node in seen or len(seen) >= 257:
            raise CandidateReviewBoundsError("Invalid candidate preflight hierarchy.")
        seen.add(node)
        scope = current.composition_scope
        for role in ("semantic", "security") if current.scope.security_required else ("semantic",):
            context = (
                build_composition_context(status, current, cfg, review_kind=role, record=record, resolver=resolver)
                if scope
                else None
            )
            if isinstance(context, CompositionRefresh):
                context = context.preview_context
            route = review_route(current.scope, cfg, cwd, source, role, context=context)
            if not route.ready:
                raise CandidateReviewBoundsError("No bounded candidate review route is available.")
        if scope and scope.retained_authority_json:
            for child in scope.children:
                child_snapshot = build_delivery_verification_snapshot(
                    replace(status, verification_node=child.node_id),
                    cfg,
                    candidate_commit=snapshot.identity.candidate_commit,
                )
                entry = (record.reverification or {}).get("children", {}).get(child.node_id) if record else None
                child_record = expanded_child(status, entry, resolver=resolver) if entry else None
                if child_record is not None:
                    child_snapshot = candidate_snapshot(status, cfg, record, child.node_id)
                pending.append((child_snapshot, child_record))


class SemanticRecheckRequired(RuntimeError):
    """A child rejection invalidated an earlier semantic parent approval."""

    def __init__(self, record: DeliveryVerificationRecord) -> None:
        self.record = record


def run_node_composition_cycle(
    scope: DeliveryCompositionScope | None,
    running: DeliveryVerificationRecord,
    role: str,
    *,
    exchange: Callable[
        [DeliveryVerificationRecord, bool], tuple[DeliveryIntegrationReviewResult | None, DeliveryVerificationRecord]
    ],
    validate: Callable[[DeliveryVerificationRecord], None],
    load: Callable[[DeliveryVerificationRecord], CompositionResult],
    review_child: Callable[[DeliveryVerificationRecord, str], tuple[DeliveryVerificationRecord, bool]],
    semantic_fallback_covers: Callable[[DeliveryVerificationRecord], bool],
    refresh: Callable[[DeliveryVerificationRecord], tuple[DeliveryVerificationRecord, list[str]]] | None = None,
) -> tuple[DeliveryIntegrationReviewResult | None, DeliveryVerificationRecord]:
    """The finite node cycle; persistence and delivery policy belong to the caller.

    Hooks use the same captured attempt and durably reserve work before provider
    calls. This coordinator neither allocates new budgets nor admits children.
    """
    if role == "semantic" and semantic_gap_ids(running):
        return None, running
    if refresh is not None and scope is not None:
        running, nodes = refresh(running)
        if nodes:
            validate(running)
        for node_id in nodes:
            scope.child(node_id)
        for node_id in nodes:
            running, passed = review_child(running, node_id)
            if semantic_gap_ids(running) and not semantic_fallback_covers(running):
                if role == "security":
                    raise SemanticRecheckRequired(running)
                return None, running
            if not passed:
                return None, running
    result, running = exchange(running, False)
    if result is not None or scope is None or running.reverification is None:
        return result, running
    field = "composition_evidence_fingerprint" if role == "semantic" else "security_composition_evidence_fingerprint"
    if not getattr(running, field):
        return None, running
    validate(running)
    if role in running.reverification["reassessments"]:
        return exchange(running, True)
    accepted = load(running)
    if (
        not accepted.fallback
        or not accepted.child_refs
        or not (accepted.assessment.approved or (role == "semantic" and accepted.has_parent_gap))
    ):
        return None, running
    nodes = [item["id"] for item in accepted.checkpoints if item["outcome"] == "verification_required"]
    # Validate the entire selection before invoking any child, including on resume.
    for node_id in nodes:
        scope.child(node_id)
    for node_id in nodes:
        running, passed = review_child(running, node_id)
        if semantic_gap_ids(running) and not semantic_fallback_covers(running):
            if role == "security":
                raise SemanticRecheckRequired(running)
            return None, running
        if not passed:
            return None, running
    if not nodes:
        return None, running
    return exchange(running, True)
