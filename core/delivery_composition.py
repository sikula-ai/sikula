"""Bounded node composition. Historical receipts are inputs, never approval."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Timer
from typing import Any, TYPE_CHECKING

from core.delivery_evidence_resolution import EvidenceResolver, evidence_operation
from core.delivery_checkpoint_evidence import load_checkpoint_evidence
from core.delivery_delta import inspect_delta_metadata
from core.delivery_composition_scope import (
    DeliveryCompositionAttempt,
    DeliveryCompositionScope,
    capture_composition_attempt,
)
from core.delivery_progress import DeliveryStatusResult, delivery_progress_path
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification import delivery_verification_source_task_is_private
from core.delivery_verification_review import (
    DeliveryIntegrationAssessment,
    DeliveryIntegrationFinding,
    DeliveryIntegrationReviewParseError,
    DeliveryObligationAssessment,
    _bounded_metadata,
    _unique_object,
    delivery_integration_review_control_example,
    parse_delivery_integration_review,
)
from core.delivery_verification_model import DeliveryVerificationRecord
from core.worktree import delivery_verification_git_env

COMPOSITION_POLICY = "flat-final-composition-v3"
MAX_COMPOSITION_CHILDREN = 8
MAX_COMPOSITION_DIRECT_UNITS = 32
MAX_COMPOSITION_DELTA_BYTES = 64 * 1024
MAX_COMPOSITION_PACKET_BYTES = 128 * 1024


class CompositionDeltaLimit(ValueError):
    """A bounded Git read exceeded its byte allowance, not an evidence failure."""


@dataclass(frozen=True)
class CompositionRefresh:
    """Required child reviews, with a sizing-only preview of the eventual packet.

    The preview is never review authority: execution rebuilds the packet from
    accepted exact-candidate child evidence after all requested reviews finish.
    """

    nodes: dict[str, dict[str, str]]
    preview_context: dict[str, Any]


if TYPE_CHECKING:
    from core.delivery_verification import DeliveryVerificationSnapshot


def fingerprint(value: Any) -> str:
    return "sha256:" + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git_bounded(root: Path, args: list[str], limit: int) -> str:
    """Stop reading at the budget; never buffer a repository-sized diff."""
    with subprocess.Popen(
        ["git", *args],
        cwd=root,
        env=delivery_verification_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        timer = Timer(20, process.kill)
        timer.start()
        try:
            content = process.stdout.read(limit + 1)
            if len(content) > limit:
                raise CompositionDeltaLimit("Composition delta exceeds the packet budget.")
            if process.wait(timeout=20):
                raise ValueError("Composition delta is unavailable.")
            return content.decode("utf-8")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            timer.join()


@evidence_operation
def build_composition_context(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    project_config: dict[str, Any],
    *,
    review_kind: str = "semantic",
    record: DeliveryVerificationRecord | None = None,
    resolver: EvidenceResolver | None = None,
) -> dict[str, Any] | CompositionRefresh | None:
    """Flat delivery adapter; common construction uses only captured node inputs."""
    composition_scope = snapshot.composition_scope
    if composition_scope is None:
        return None
    attempt = capture_composition_attempt(status, record, composition_scope)
    return build_node_composition_context(
        Path(status.project_root), snapshot, attempt, project_config, review_kind=review_kind, resolver=resolver
    )


@evidence_operation
def build_node_composition_context(
    root: Path,
    snapshot: DeliveryVerificationSnapshot,
    attempt: DeliveryCompositionAttempt,
    project_config: dict[str, Any],
    *,
    review_kind: str = "semantic",
    resolver: EvidenceResolver | None = None,
) -> dict[str, Any] | CompositionRefresh | None:
    """Build both roles' packets from exact authority and immediate children only."""
    resolver = resolver or EvidenceResolver()
    composition_scope = attempt.scope
    attempt.assert_candidate(snapshot.identity)
    if composition_scope.parent != snapshot.scope:
        raise ValueError("Composition snapshot scope changed.")
    if not 0 < len(composition_scope.children) <= MAX_COMPOSITION_CHILDREN:
        return None
    directory = delivery_progress_path(root, snapshot.scope.plan_id).parent
    context = composition_scope.parent.plan_context()
    final_context = composition_scope.retained_authority()
    if review_kind == "security" and final_context is None:
        return None
    children = []
    covered_units: set[str] = set()
    inherited: set[str] = set()
    remaining_delta = MAX_COMPOSITION_DELTA_BYTES
    refresh = {}
    for scope in composition_scope.children:
        if covered_units.intersection(scope.unit_ids) or inherited.intersection(scope.obligation_ids):
            return None
        origin = attempt.origin(scope.node_id, resolver=resolver)
        evidence = load_checkpoint_evidence(
            root, directory, origin, plan_id=scope.plan_id, node_id=scope.node_id, resolver=resolver
        )
        if not evidence.covers(scope):
            raise ValueError("Composition checkpoint evidence changed.")
        if review_kind == "security" and (not origin.security_required or origin.security_status != "approved"):
            return None
        # A later repair outside the checkpoint makes that obligation direct.
        obligations = [
            item
            for item in context["obligations"]
            if item["id"] in scope.obligation_ids and set(item["unit_ids"]) <= set(scope.unit_ids)
        ]
        if not obligations:
            return None
        if final_context is not None and {item["id"] for item in obligations} != set(scope.obligation_ids):
            return None
        delta = ""
        if origin.candidate_tree != snapshot.identity.candidate_tree:
            try:
                refs = [origin.candidate_commit, snapshot.identity.candidate_commit]
                diff_args = [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--ignore-submodules=none",
                    "--relative",
                ]
                path_count = inspect_delta_metadata(
                    root,
                    diff_args,
                    refs,
                    is_private=lambda path: delivery_verification_source_task_is_private(
                        root, root / path, path, project_config, allow_missing=True
                    ),
                )
                reason = "delta_paths" if path_count > 32 else None
                if reason is None:
                    try:
                        delta = _git_bounded(root, [*diff_args, "--unified=3", *refs, "--", "."], remaining_delta)
                    except CompositionDeltaLimit:
                        reason = "delta_bytes"
                if reason:
                    if final_context is None:
                        return None
                    refresh[scope.node_id] = {"origin": origin.checkpoint_evidence_fingerprint, "reason": reason}
                if "Binary files " in delta or "GIT binary patch" in delta:
                    return None
                remaining_delta -= len(delta.encode())
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
        children.append(
            {
                "id": scope.node_id,
                "origin_commit": origin.candidate_commit,
                "origin_tree": origin.candidate_tree,
                "evidence_fingerprint": origin.checkpoint_evidence_fingerprint,
                "historical_outcome": "satisfied",
                "unit_ids": list(scope.unit_ids),
                "obligations": obligations,
                "exact_tree": origin.candidate_tree == snapshot.identity.candidate_tree,
                "delta": delta,
            }
        )
        if final_context is not None:
            interface = next(
                item for item in final_context["final_authority"]["children"] if item["id"] == scope.node_id
            )
            children[-1].pop("unit_ids")
            children[-1].pop("obligations")
            children[-1].update(interface)
            children[-1]["security_outcome"] = origin.security_status
        if scope.node_id in refresh:
            # Preview only. The distinct return type prevents use as approval.
            children[-1].update(
                origin_commit=snapshot.identity.candidate_commit,
                origin_tree=snapshot.identity.candidate_tree,
                exact_tree=True,
                delta="",
            )
        covered_units.update(scope.unit_ids)
        inherited.update(item["id"] for item in obligations)
    if final_context is not None:
        context = final_context
        context["final_authority"] = {"policy": final_context["final_authority"]["policy"]}
    context["units"] = [item for item in context["units"] if item["id"] not in covered_units]
    context["obligations"] = [item for item in context["obligations"] if item["id"] not in inherited]
    if len(context["units"]) > MAX_COMPOSITION_DIRECT_UNITS:
        return None
    context["checkpoint_composition"] = {"policy": COMPOSITION_POLICY, "children": children}
    if not composition_scope.include_final_checks:
        # The node identity is bounded; its full descendant coverage stays private.
        context["verification_node"] = {"id": composition_scope.parent.node_id}
    if final_context is not None:
        context["checkpoint_composition"].update(compact=True, review_kind=review_kind)
    if len(json.dumps(context).encode()) > MAX_COMPOSITION_PACKET_BYTES:
        return None
    return CompositionRefresh(refresh, context) if refresh else context


def composition_example(context: dict[str, Any]) -> str:
    security = context["checkpoint_composition"].get("review_kind") == "security"
    payload = json.loads(
        delivery_integration_review_control_example(
            set() if security else {item["id"] for item in context["obligations"]}
        )
    )
    payload["checkpoint_results"] = [
        {
            "id": child["id"],
            "outcome": "applicable",
            "rationale": "Bounded evidence-based reason",
            "evidence": [] if child["exact_tree"] else ["delta:" + child["id"]],
        }
        for child in context["checkpoint_composition"]["children"]
    ]
    return json.dumps(payload, separators=(",", ":"))


@dataclass(frozen=True)
class CompositionResult:
    assessment: DeliveryIntegrationAssessment
    checkpoints: list[dict[str, Any]]
    fallback: bool
    control: dict[str, Any]
    child_refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inherited_count(self) -> int:
        return sum(child["obligation_count"] for child in self.child_refs)

    @property
    def has_parent_gap(self) -> bool:
        return bool(self.child_refs) and _retained_repair_gap(self.assessment)


def _retained_repair_gap(assessment: DeliveryIntegrationAssessment) -> bool:
    """The parsed compact assessment covers all retained direct obligations."""
    gaps = {item.id for item in assessment.obligation_results if item.outcome != "satisfied"}
    return (
        assessment.disposition == "repair_required"
        and bool(gaps)
        and bool(assessment.findings)
        and all(item.obligation_ids and set(item.obligation_ids) <= gaps for item in assessment.findings)
    )


def parse_composition(output: str, context: dict[str, Any], known_unit_ids: set[str]) -> CompositionResult:
    compact = context["checkpoint_composition"].get("compact", False)
    security = context["checkpoint_composition"].get("review_kind") == "security"
    try:
        payload = json.loads(
            next(line for line in reversed(output.splitlines()) if line.strip()), object_pairs_hook=_unique_object
        )
        checkpoints = payload.pop("checkpoint_results")
        children = {child["id"]: child for child in context["checkpoint_composition"]["children"]}
        if not isinstance(checkpoints, list) or len(checkpoints) != len(children):
            raise ValueError
        seen = set()
        for result in checkpoints:
            if not isinstance(result, dict) or set(result) != {"id", "outcome", "rationale", "evidence"}:
                raise ValueError
            key = result["id"]
            if not isinstance(key, str) or key not in children or key in seen:
                raise ValueError
            seen.add(key)
            if result["outcome"] not in ("applicable", "verification_required"):
                raise ValueError
            _bounded_metadata(result["rationale"], "checkpoint rationale")
            if result["evidence"] not in ([], ["delta:" + key]):
                raise ValueError
            if result["outcome"] == "applicable" and not children[key]["exact_tree"] and not result["evidence"]:
                raise ValueError
        assessment = parse_delivery_integration_review(
            json.dumps(payload),
            known_unit_ids=known_unit_ids,
            known_obligation_ids=set() if security else {item["id"] for item in context["obligations"]},
            known_finding_obligation_ids={item["id"] for item in context["obligations"]}
            | {item["id"] for child in children.values() for item in child.get("obligations", [])},
        )
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.composition_invalid",
            "Return all direct outcomes and every checkpoint decision exactly once.",
        ) from None
    fallback = any(item["outcome"] != "applicable" for item in checkpoints)
    inherited = [item["id"] for child in children.values() for item in child.get("obligations", [])]
    hard_stop = assessment.disposition in {
        "external_dependency_gap",
        "human_review_required",
        "scope_amendment_required",
    }
    if compact:
        refs = [
            {key: child[key] for key in ("id", "evidence_fingerprint", "coverage_fingerprint", "obligation_count")}
            for child in children.values()
        ]
        # A complete assessment of retained parent obligations can authorize
        # local integration repair without rediscovering the gap in full review.
        parent_repair = not security and _retained_repair_gap(assessment)
        needs_full = fallback if security else (fallback or not (assessment.approved or parent_repair))
        return CompositionResult(
            assessment,
            checkpoints,
            needs_full and not hard_stop and (not security or assessment.approved),
            {**payload, "checkpoint_results": checkpoints},
            refs,
        )
    # A provisional child is never reported as satisfied, even on a hard stop.
    outcomes = {item["id"]: item["outcome"] for item in checkpoints}
    extra = [
        DeliveryObligationAssessment(item["id"], "satisfied" if outcomes[child["id"]] == "applicable" else "uncertain")
        for child in children.values()
        for item in child["obligations"]
    ]
    findings = list(assessment.findings)
    if fallback:
        for result in extra:
            if result.outcome == "uncertain":
                findings.append(
                    DeliveryIntegrationFinding(
                        "checkpoint.verification_required", "Checkpoint requires current verification.", [], [result.id]
                    )
                )
    # Parent repair findings require the complete assessment; do not author from partial evidence.
    fallback = (fallback or not assessment.approved) and not hard_stop
    expanded = replace(assessment, obligation_results=[*assessment.obligation_results, *extra], findings=findings)
    assert len(inherited) == len(set(inherited))
    return CompositionResult(expanded, checkpoints, fallback, {**payload, "checkpoint_results": checkpoints})


def composition_evidence_path(directory: Path, digest: str) -> Path:
    if (
        not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(c not in "0123456789abcdef" for c in digest[7:])
    ):
        raise ValueError("Invalid composition evidence identity.")
    return directory / f"composition-evidence-{digest[7:]}.json"


def store_composition(root: Path, directory: Path, gate_id: str, context: dict, result: CompositionResult) -> str:
    payload = {
        "schema_version": 1,
        "gate_id": gate_id,
        "packet_fingerprint": fingerprint(context),
        "control": result.control,
        "origins": {
            child["id"]: child["evidence_fingerprint"] for child in context["checkpoint_composition"]["children"]
        },
    }
    if context["checkpoint_composition"].get("compact"):
        payload.update(compact=True, review_kind=context["checkpoint_composition"]["review_kind"])
    digest = fingerprint(payload)
    write_repair_state(root, composition_evidence_path(directory, digest), payload)
    return digest


@evidence_operation
def load_composition(
    status: DeliveryStatusResult,
    record: DeliveryVerificationRecord,
    *,
    review_kind: str = "semantic",
    scope: DeliveryCompositionScope | None = None,
    resolver: EvidenceResolver | None = None,
) -> CompositionResult:
    return load_node_composition(
        Path(status.project_root),
        capture_composition_attempt(status, record, scope),
        record,
        review_kind=review_kind,
        resolver=resolver,
    )


@evidence_operation
def load_node_composition(
    root: Path,
    attempt: DeliveryCompositionAttempt,
    record: DeliveryVerificationRecord,
    *,
    review_kind: str = "semantic",
    resolver: EvidenceResolver | None = None,
) -> CompositionResult:
    """Validate accepted results against the same explicit coverage as construction."""
    resolver = resolver or EvidenceResolver()
    attempt.assert_record(record)
    scope = attempt.scope.parent
    directory = delivery_progress_path(root, scope.plan_id).parent
    digest = (
        record.composition_evidence_fingerprint
        if review_kind == "semantic"
        else record.security_composition_evidence_fingerprint
    )
    payload = resolver.read(root, composition_evidence_path(directory, digest), reader=read_repair_state)
    if (
        payload is None
        or set(payload)
        != (
            {"schema_version", "gate_id", "packet_fingerprint", "control", "origins"}
            | ({"compact", "review_kind"} if payload.get("compact") is True else set())
        )
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or fingerprint(payload) != digest
        or payload.get("gate_id") != record.gate_id
        or payload.get("review_kind", "semantic") != review_kind
    ):
        raise ValueError("Composition evidence unavailable.")
    if not isinstance(payload["origins"], dict):
        raise ValueError("Composition origins must be an object.")
    composition_evidence_path(directory, payload["packet_fingerprint"])
    origins = {}
    origin_records = {}
    for child in attempt.scope.children:
        origin = attempt.origin(child.node_id, digest=payload["origins"].get(child.node_id), resolver=resolver)
        if origin is None:
            raise ValueError("Composition origin unavailable.")
        evidence = load_checkpoint_evidence(
            root, directory, origin, plan_id=scope.plan_id, node_id=child.node_id, resolver=resolver
        )
        if not evidence.covers(child):
            raise ValueError("Composition origin coverage changed.")
        origins[child.node_id] = origin.checkpoint_evidence_fingerprint
        origin_records[child.node_id] = origin
    if payload["origins"] != origins:
        raise ValueError("Composition origins changed.")
    full_context = scope.plan_context()
    authority = full_context
    compact = payload.get("compact") is True
    if compact:
        if attempt.scope.retained_authority_json is None:
            raise ValueError("Accepted final authority changed.")
        authority = attempt.scope.retained_authority()
    children = []
    inherited = set()
    covered = set()
    if not 0 < len(attempt.scope.children) <= MAX_COMPOSITION_CHILDREN:
        raise ValueError("Unsupported composition coverage.")
    for child in attempt.scope.children:
        if covered.intersection(child.unit_ids):
            raise ValueError("Overlapping composition coverage.")
        covered.update(child.unit_ids)
        obligations = [
            item
            for item in full_context["obligations"]
            if item["id"] in child.obligation_ids and set(item["unit_ids"]) <= set(child.unit_ids)
        ]
        ids = {item["id"] for item in obligations}
        if not ids or inherited.intersection(ids):
            raise ValueError("Invalid composition ownership.")
        inherited.update(ids)
        children.append(
            {
                "id": child.node_id,
                "obligations": obligations,
                "exact_tree": origin_records[child.node_id].candidate_tree == record.candidate_tree,
            }
        )
        if compact:
            origin = origin_records[child.node_id]
            if review_kind == "security" and (not origin.security_required or origin.security_status != "approved"):
                raise ValueError("Security composition origin changed.")
            interface = next(item for item in authority["final_authority"]["children"] if item["id"] == child.node_id)
            children[-1].pop("obligations")
            children[-1].update(interface, evidence_fingerprint=origin.checkpoint_evidence_fingerprint)
    authority["obligations"] = [item for item in authority["obligations"] if item["id"] not in inherited]
    authority["checkpoint_composition"] = {"children": children}
    if compact:
        authority["checkpoint_composition"].update(compact=True, review_kind=review_kind)
    return parse_composition(json.dumps(payload["control"]), authority, set(scope.unit_ids))
