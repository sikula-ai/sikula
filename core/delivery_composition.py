"""Bounded final-gate composition. Historical receipts are inputs, never approval."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Timer
from typing import Any, TYPE_CHECKING

from core.delivery_checkpoint_evidence import load_checkpoint_evidence
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
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_model import DeliveryVerificationRecord
from core.worktree import delivery_verification_git_env

COMPOSITION_POLICY = "flat-final-composition-v1"
MAX_COMPOSITION_CHILDREN = 8
MAX_COMPOSITION_DIRECT_UNITS = 32
MAX_COMPOSITION_DELTA_BYTES = 64 * 1024
MAX_COMPOSITION_PACKET_BYTES = 128 * 1024

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
                raise ValueError("Composition delta exceeds the packet budget.")
            if process.wait(timeout=20):
                raise ValueError("Composition delta is unavailable.")
            return content.decode("utf-8")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            timer.join()


def build_composition_context(
    status: DeliveryStatusResult, snapshot: DeliveryVerificationSnapshot, project_config: dict[str, Any]
) -> dict[str, Any] | None:
    """Caller validates admission/policy first. Unsupported shapes use full review."""
    if snapshot.scope.node_id != "root" or not status.plan.checkpoints:
        return None
    if len(status.plan.checkpoints) > MAX_COMPOSITION_CHILDREN:
        return None
    root = Path(status.project_root)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    context = snapshot.scope.plan_context()
    children = []
    covered_units: set[str] = set()
    inherited: set[str] = set()
    remaining_delta = MAX_COMPOSITION_DELTA_BYTES
    for checkpoint in status.plan.checkpoints:
        scope = DeliveryVerificationScope.from_plan(status.plan, checkpoint.id)
        if covered_units.intersection(scope.unit_ids) or inherited.intersection(scope.obligation_ids):
            return None
        origin = status.checkpoint_verifications[checkpoint.id]
        evidence = load_checkpoint_evidence(root, directory, origin, plan_id=scope.plan_id, node_id=checkpoint.id)
        if not evidence.covers(scope):
            raise ValueError("Composition checkpoint evidence changed.")
        # A later repair outside the checkpoint makes that obligation direct.
        obligations = [
            item
            for item in context["obligations"]
            if item["id"] in scope.obligation_ids and set(item["unit_ids"]) <= set(scope.unit_ids)
        ]
        if not obligations:
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
                entries = _git_bounded(
                    root,
                    [*diff_args, "--raw", "--no-abbrev", "-z", *refs, "--", "."],
                    8192,
                ).split("\0")
                if entries[-1] or (len(entries) - 1) % 2:
                    return None
                paths = entries[1:-1:2]
                for metadata in entries[:-1:2]:
                    fields = metadata.split()
                    if len(fields) != 5 or not fields[0].startswith(":"):
                        return None
                    # Gitlinks describe dependency pointers, not complete content deltas.
                    if "160000" in (fields[0][1:], fields[1]):
                        return None
                if len(paths) > 32 or any(
                    delivery_verification_source_task_is_private(
                        root, root / path, path, project_config, allow_missing=True
                    )
                    for path in paths
                ):
                    return None
                delta = _git_bounded(
                    root,
                    [*diff_args, "--unified=3", *refs, "--", "."],
                    remaining_delta,
                )
                if "Binary files " in delta or "GIT binary patch" in delta:
                    return None
                remaining_delta -= len(delta.encode())
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
        children.append(
            {
                "id": checkpoint.id,
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
        covered_units.update(scope.unit_ids)
        inherited.update(item["id"] for item in obligations)
    context["units"] = [item for item in context["units"] if item["id"] not in covered_units]
    context["obligations"] = [item for item in context["obligations"] if item["id"] not in inherited]
    if len(context["units"]) > MAX_COMPOSITION_DIRECT_UNITS:
        return None
    context["checkpoint_composition"] = {"policy": COMPOSITION_POLICY, "children": children}
    if len(json.dumps(context).encode()) > MAX_COMPOSITION_PACKET_BYTES:
        return None
    return context


def composition_example(context: dict[str, Any]) -> str:
    payload = json.loads(delivery_integration_review_control_example({item["id"] for item in context["obligations"]}))
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


def parse_composition(output: str, context: dict[str, Any], known_unit_ids: set[str]) -> CompositionResult:
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
            known_obligation_ids={item["id"] for item in context["obligations"]},
            known_finding_obligation_ids={item["id"] for item in context["obligations"]}
            | {item["id"] for child in children.values() for item in child["obligations"]},
        )
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.composition_invalid",
            "Return all direct outcomes and every checkpoint decision exactly once.",
        ) from None
    fallback = any(item["outcome"] != "applicable" for item in checkpoints)
    inherited = [item["id"] for child in children.values() for item in child["obligations"]]
    hard_stop = assessment.disposition in {
        "external_dependency_gap",
        "human_review_required",
        "scope_amendment_required",
    }
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
    digest = fingerprint(payload)
    write_repair_state(root, composition_evidence_path(directory, digest), payload)
    return digest


def load_composition(status: DeliveryStatusResult, record: DeliveryVerificationRecord) -> CompositionResult:
    root = Path(status.project_root)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    payload = read_repair_state(root, composition_evidence_path(directory, record.composition_evidence_fingerprint))
    if (
        payload is None
        or set(payload) != {"schema_version", "gate_id", "packet_fingerprint", "control", "origins"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or fingerprint(payload) != record.composition_evidence_fingerprint
        or payload.get("gate_id") != record.gate_id
    ):
        raise ValueError("Composition evidence unavailable.")
    composition_evidence_path(directory, payload["packet_fingerprint"])
    origins = {}
    for child in status.plan.checkpoints:
        origin = status.checkpoint_verifications.get(child.id)
        if origin is None:
            raise ValueError("Composition origin unavailable.")
        evidence = load_checkpoint_evidence(root, directory, origin, plan_id=status.plan.plan_id, node_id=child.id)
        if not evidence.covers(DeliveryVerificationScope.from_plan(status.plan, child.id)):
            raise ValueError("Composition origin coverage changed.")
        origins[child.id] = origin.checkpoint_evidence_fingerprint
    if payload["origins"] != origins:
        raise ValueError("Composition origins changed.")
    scope = DeliveryVerificationScope.from_plan(status.plan)
    authority = scope.plan_context()
    children = []
    inherited = set()
    covered = set()
    if not 0 < len(status.plan.checkpoints) <= MAX_COMPOSITION_CHILDREN:
        raise ValueError("Unsupported composition coverage.")
    for child in status.plan.checkpoints:
        if covered.intersection(child.unit_ids):
            raise ValueError("Overlapping composition coverage.")
        covered.update(child.unit_ids)
        obligations = [
            item
            for item in authority["obligations"]
            if item["id"] in child.obligation_ids and set(item["unit_ids"]) <= set(child.unit_ids)
        ]
        ids = {item["id"] for item in obligations}
        if not ids or inherited.intersection(ids):
            raise ValueError("Invalid composition ownership.")
        inherited.update(ids)
        children.append(
            {
                "id": child.id,
                "obligations": obligations,
                "exact_tree": status.checkpoint_verifications[child.id].candidate_tree == record.candidate_tree,
            }
        )
    authority["obligations"] = [item for item in authority["obligations"] if item["id"] not in inherited]
    authority["checkpoint_composition"] = {"children": children}
    return parse_composition(json.dumps(payload["control"]), authority, set(scope.unit_ids))
