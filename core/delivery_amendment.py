"""Safe delivery-plan amendment proposals and deterministic application."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Any

import yaml

from core.contract_check import check_contract
from core.delivery_assembly import (
    DeliveryAssemblyArtifact,
    assemble_delivery_artifacts,
    delivery_artifact_compatibility_issue,
    delivery_artifact_content_id,
    delivery_branch_commit,
    delivery_commit_is_ancestor,
    find_delivery_artifact_commit,
    preview_delivery_artifacts,
    preview_delivery_assembly,
    rollback_delivery_artifacts,
)
from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    parse_delivery_amendment_authoring_output,
)
from core.delivery_asset_assignment import (
    DeliveryAssetAssignmentError,
    DeliveryAssetAssignmentUnit,
    render_delivery_asset_assignments,
)
from core.delivery_plan import (
    DeliveryBudgetExceeded,
    DeliveryPlan,
    DeliveryPlanIssue,
    DeliveryPlanUnit,
    check_delivery_plan_data,
    check_delivery_plan_file,
)
from core.delivery_public_metadata import (
    is_safe_delivery_public_metadata,
    project_delivery_public_identity,
    sanitize_delivery_public_metadata,
)
from core.delivery_progress import (
    DeliveryProgress,
    DeliveryProgressLockError,
    DeliveryProgressEvent,
    DeliveryStatusResult,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    append_delivery_progress_events,
    delivery_terminal_stop_recommended_action,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    mark_delivery_assembly,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_unit_metadata import DELIVERY_UNIT_BUDGET_FIELDS
from core.state import (
    DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
    DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
    DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
    StateStore,
)
from core.worktree import resolve_git_commit

SUPPORTED_DELIVERY_AMENDMENT_PROPOSAL_SCHEMA_VERSION = 1
SUPPORTED_DELIVERY_AMENDMENT_FAILURE_EVIDENCE_SCHEMA_VERSION = 2
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_FORBIDDEN_PLAN_ROOTS = (
    (".git",),
    (".sikula", "state"),
    (".sikula", "worktrees"),
    (".sikula", "contract-reports"),
)
_DELIVERY_AMENDMENT_EVIDENCE_STOP_CODES = frozenset(
    {
        DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
        DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
        DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
    }
)
_MAX_DELIVERY_AMENDMENT_EVIDENCE_PATHS = 100
_MAX_DELIVERY_AMENDMENT_EVIDENCE_DISPOSITIONS = 16


class DeliveryAmendmentError(RuntimeError):
    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.issue = DeliveryPlanIssue("error", code, message, path)


@dataclass(frozen=True)
class DeliveryAmendmentReviewEvidence:
    records_count: int
    issues_count: int
    issue_summaries: tuple[str, ...]
    dispositions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_count": self.records_count,
            "issues_count": self.issues_count,
            "issue_summaries": list(self.issue_summaries),
            "dispositions": [dict(value) for value in self.dispositions],
        }


@dataclass(frozen=True)
class DeliveryAmendmentDependencyIdentity:
    plan_id: str
    unit_id: str
    child_task_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "plan_id": self.plan_id,
            "unit_id": self.unit_id,
            "child_task_id": self.child_task_id,
        }

    def to_prompt_dict(self) -> dict[str, str | None]:
        return {
            "plan_id": project_delivery_public_identity(self.plan_id),
            "unit_id": project_delivery_public_identity(self.unit_id),
            "child_task_id": project_delivery_public_identity(self.child_task_id),
        }


@dataclass(frozen=True)
class DeliveryAmendmentFailureEvidence:
    plan_id: str
    unit_id: str
    child_task_id: str
    failure_code: str
    recommended_action: str
    inherited_constraints: tuple[dict[str, Any], ...]
    declared_write_paths: tuple[str, ...]
    effective_write_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    changed_count: int
    omitted_changed_paths_count: int
    violation_paths: tuple[str, ...]
    violation_count: int
    outside_project_paths: tuple[str, ...]
    outside_project_count: int
    omitted_outside_project_paths_count: int
    reviewer: DeliveryAmendmentReviewEvidence
    security_reviewer: DeliveryAmendmentReviewEvidence
    stop_disposition: dict[str, Any] | None
    dependency_handoffs: tuple[DeliveryAmendmentDependencyIdentity, ...]
    fingerprint: str
    schema_version: int = SUPPORTED_DELIVERY_AMENDMENT_FAILURE_EVIDENCE_SCHEMA_VERSION

    @property
    def requires_external_follow_up(self) -> bool:
        return self.failure_code == DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "unit_id": self.unit_id,
            "child_task_id": self.child_task_id,
            "failure_code": self.failure_code,
            "recommended_action": self.recommended_action,
            "inherited_constraints": [dict(value) for value in self.inherited_constraints],
            "write_scope": {
                "declared_paths": list(self.declared_write_paths),
                "effective_paths": list(self.effective_write_paths),
            },
            "changed_files": {
                "count": self.changed_count,
                "paths": list(self.changed_paths),
                "omitted_paths_count": self.omitted_changed_paths_count,
            },
            "scope_violations": {
                "count": self.violation_count,
                "paths": list(self.violation_paths),
                "outside_project": {
                    "count": self.outside_project_count,
                    "paths": list(self.outside_project_paths),
                    "omitted_paths_count": self.omitted_outside_project_paths_count,
                },
            },
            "reviews": {
                "reviewer": self.reviewer.to_dict(),
                "security_reviewer": self.security_reviewer.to_dict(),
            },
            "stop_disposition": dict(self.stop_disposition) if self.stop_disposition else None,
            "dependency_handoffs": [value.to_dict() for value in self.dependency_handoffs],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "fingerprint": self.fingerprint}

    def to_prompt_dict(self) -> dict[str, Any]:
        """Project private identities only at the external-provider boundary."""
        payload = self.payload_dict()
        payload["plan_id"] = project_delivery_public_identity(self.plan_id)
        payload["unit_id"] = project_delivery_public_identity(self.unit_id)
        payload["child_task_id"] = project_delivery_public_identity(self.child_task_id)
        payload["inherited_constraints"] = [
            {
                **constraint,
                "id": project_delivery_public_identity(constraint["id"]),
                "unit_ids": [project_delivery_public_identity(value) for value in constraint["unit_ids"]],
            }
            for constraint in self.inherited_constraints
        ]
        payload["dependency_handoffs"] = [value.to_prompt_dict() for value in self.dependency_handoffs]
        return {**payload, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class DeliveryAmendmentProposalUnit:
    id: str
    title: str
    task_path: str
    depends_on: list[str]
    task_markdown: str
    stream: str | None = None
    component: str | None = None
    phase: str | None = None
    kind: str | None = None
    platform: str | None = None
    repo_id: str | None = None
    scope_paths: list[str] = field(default_factory=list)
    estimated_size: str | None = None
    risk_tags: list[str] = field(default_factory=list)
    budget: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "task_path": self.task_path,
            "depends_on": list(self.depends_on),
            "task_markdown": self.task_markdown,
        }
        for key in ("stream", "component", "phase", "kind", "platform", "repo_id", "estimated_size"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.scope_paths:
            data["scope_paths"] = list(self.scope_paths)
        if self.risk_tags:
            data["risk_tags"] = list(self.risk_tags)
        if self.budget:
            data["budget"] = dict(self.budget)
        return data

    def plan_entry(self, *, target_unit_id: str) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("task_markdown")
        data["supersedes"] = target_unit_id
        return data


@dataclass(frozen=True)
class DeliveryAmendmentProposal:
    proposal_id: str
    plan_id: str
    target_unit_id: str
    source_plan_path: str
    source_plan_fingerprint: str
    source_plan_blob_id: str
    target_task_fingerprint: str
    retained_contract_fingerprints: dict[str, str]
    progress_fingerprint: str
    source_assembly_base_commit: str
    source_assembled_commit: str
    created_at: str
    replacement_units: list[DeliveryAmendmentProposalUnit]
    amended_plan_fingerprint: str | None = None
    rewired_unit_ids: list[str] = field(default_factory=list)
    amend_reason: str | None = None
    budget_exceeded: DeliveryBudgetExceeded | None = None

    @property
    def replacement_ids(self) -> list[str]:
        return [unit.id for unit in self.replacement_units]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SUPPORTED_DELIVERY_AMENDMENT_PROPOSAL_SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "plan_id": self.plan_id,
            "target_unit_id": self.target_unit_id,
            "source_plan_path": self.source_plan_path,
            "source_plan_fingerprint": self.source_plan_fingerprint,
            "source_plan_blob_id": self.source_plan_blob_id,
            "target_task_fingerprint": self.target_task_fingerprint,
            "retained_contract_fingerprints": dict(self.retained_contract_fingerprints),
            "progress_fingerprint": self.progress_fingerprint,
            "created_at": self.created_at,
            "replacement_units": [unit.to_dict() for unit in self.replacement_units],
        }
        data["source_assembly_base_commit"] = self.source_assembly_base_commit
        data["source_assembled_commit"] = self.source_assembled_commit
        if self.amended_plan_fingerprint:
            data["amended_plan_fingerprint"] = self.amended_plan_fingerprint
        if self.rewired_unit_ids:
            data["rewired_unit_ids"] = list(self.rewired_unit_ids)
        if self.amend_reason:
            data["amend_reason"] = self.amend_reason
        if self.budget_exceeded:
            data["budget_exceeded"] = self.budget_exceeded.to_dict()
        return data


@dataclass(frozen=True)
class DeliveryAmendmentTarget:
    plan_path: str
    project_root: str
    private_artifact_roots: tuple[Path, ...]
    plan: DeliveryPlan
    status: DeliveryStatusResult
    target: DeliveryPlanUnit
    downstream_unit_ids: list[str]
    assembly_base_commit: str
    assembled_commit: str
    recorded_assembled_commit: str | None


@dataclass(frozen=True)
class DeliveryAmendmentSourceSnapshot:
    source_plan_fingerprint: str
    source_plan_blob_id: str
    target_task_fingerprint: str
    retained_contract_fingerprints: dict[str, str]
    progress_fingerprint: str
    assembly_base_commit: str
    assembled_commit: str


@dataclass(frozen=True)
class DeliveryAmendmentApplyResult:
    plan_path: str
    project_root: str | None
    proposal_id: str
    target_unit_id: str | None
    replacement_ids: list[str]
    rewired_unit_ids: list[str]
    dry_run: bool
    ready: bool
    applied: bool
    proposal_path: str | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    message: str

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        sensitive_paths = (self.plan_path,)
        safe_errors = [
            _sanitize_issue(issue, self.project_root, sensitive_paths=sensitive_paths) for issue in self.errors
        ]
        safe_warnings = [
            _sanitize_issue(issue, self.project_root, sensitive_paths=sensitive_paths) for issue in self.warnings
        ]
        return {
            "plan_path": _safe_relative(self.plan_path, self.project_root),
            "project_root": "." if self.project_root else None,
            "proposal_id": self.proposal_id,
            "target_unit_id": project_delivery_public_identity(self.target_unit_id),
            "replacement_ids": [project_delivery_public_identity(value) for value in self.replacement_ids],
            "rewired_unit_ids": [project_delivery_public_identity(value) for value in self.rewired_unit_ids],
            "dry_run": self.dry_run,
            "ready": self.ready,
            "applied": self.applied,
            "proposal_path": _safe_relative(self.proposal_path, self.project_root),
            "errors": [issue.to_dict() for issue in safe_errors],
            "warnings": [issue.to_dict() for issue in safe_warnings],
            "message": sanitize_delivery_public_metadata(self.message),
        }


@dataclass(frozen=True)
class _AmendmentPreflight:
    result: DeliveryAmendmentApplyResult
    proposal: DeliveryAmendmentProposal | None = None
    target: DeliveryAmendmentTarget | None = None
    plan_data: dict[str, Any] | None = None
    artifacts_applied: bool = False
    amendment_commit: str | None = None


@dataclass(frozen=True)
class _AppliedAssemblyRecovery:
    amendment_commit: str | None
    current_commit: str | None
    expected_commit: str | None

    @property
    def mutation_required(self) -> bool:
        return self.amendment_commit is None


@dataclass(frozen=True)
class _Backup:
    path: Path
    existed: bool
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class _PublishedArtifact:
    path: Path
    fingerprint: str


def inspect_delivery_amendment_target(
    path: str | Path,
    target_unit_id: str,
    *,
    project_root: Path | None = None,
    project_config: dict | None = None,
) -> DeliveryAmendmentTarget:
    status = get_delivery_status(path, project_root=project_root)
    if not status.valid or status.plan is None or status.project_root is None:
        issue = (
            status.errors[0]
            if status.errors
            else DeliveryPlanIssue("error", "delivery_amend.plan_invalid", "Delivery plan is invalid.")
        )
        raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
    root = Path(status.project_root)
    private_artifact_roots = _configured_private_artifact_roots(root, project_config)
    _validate_amendment_plan_destination(
        Path(status.plan_path),
        root,
        private_artifact_roots=private_artifact_roots,
    )
    plan_by_id = {unit.id: unit for unit in status.plan.units}
    target = plan_by_id.get(target_unit_id)
    if target is None:
        raise DeliveryAmendmentError(
            "delivery_amend.target_unknown",
            f"Delivery plan has no unit with id {target_unit_id}.",
        )
    _validate_amendment_target_task_source(
        target.task_path,
        root,
        private_artifact_roots=private_artifact_roots,
    )
    _validate_amendment_source_contracts(
        status.plan,
        root,
        private_artifact_roots=private_artifact_roots,
    )
    status_by_id = {unit.id: unit for unit in status.units}
    target_status = status_by_id[target_unit_id].status
    if target.superseded or target_status == "superseded":
        raise DeliveryAmendmentError(
            "delivery_amend.target_superseded",
            "A superseded unit cannot be split again; select an active replacement unit.",
        )
    if target_status == "done":
        raise DeliveryAmendmentError(
            "delivery_amend.target_done",
            "Completed delivery units are immutable and cannot be split.",
        )
    if target_status == "running":
        raise DeliveryAmendmentError(
            "delivery_amend.target_running",
            "Running delivery unit must be resumed or reconciled before it can be split.",
        )
    if target_status not in {"pending", "failed"}:
        raise DeliveryAmendmentError(
            "delivery_amend.target_state_unsafe",
            f"Delivery unit in state {target_status} cannot be split.",
        )
    if status.final_commit or status.finalized_at:
        raise DeliveryAmendmentError(
            "delivery_amend.plan_finalized",
            "A finalized delivery plan cannot be amended.",
        )

    downstream = [unit for unit in status.plan.units if target_unit_id in unit.depends_on and not unit.superseded]
    for unit in downstream:
        unit_status = status_by_id[unit.id].status
        if unit_status != "pending":
            raise DeliveryAmendmentError(
                "delivery_amend.downstream_state_unsafe",
                f"Downstream unit {unit.id} is {unit_status}; only pending downstream units may be rewired.",
            )

    assembly_base_commit, assembled_commit = _resolve_amendment_assembly(
        root,
        status,
    )
    context = DeliveryAmendmentTarget(
        plan_path=status.plan_path,
        project_root=status.project_root,
        private_artifact_roots=private_artifact_roots,
        plan=status.plan,
        status=status,
        target=target,
        downstream_unit_ids=[unit.id for unit in downstream],
        assembly_base_commit=assembly_base_commit,
        assembled_commit=assembled_commit,
        recorded_assembled_commit=status.assembled_commit,
    )
    commit_errors = _completed_dependency_commit_errors(context)
    if commit_errors:
        issue = commit_errors[0]
        raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
    return context


def capture_delivery_amendment_failure_evidence(
    context: DeliveryAmendmentTarget,
    *,
    state_store: StateStore,
) -> DeliveryAmendmentFailureEvidence | None:
    """Build a bounded, correlated projection of an eligible failed delivery child."""
    from core.delivery_constraint_context import (
        DeliveryConstraintContextError,
        parse_delivery_constraint_context,
    )
    from core.delivery_handoff import (
        DeliveryHandoffError,
        delivery_unit_handoff_matches_unit,
        parse_delivery_unit_handoff,
    )
    from core.delivery_plan import delivery_unit_constraint_context
    from core.delivery_write_scope import (
        DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED,
        DeliveryWriteScopeError,
        delivery_write_scope_matches_unit_declaration,
        resolve_delivery_write_scope,
        validate_delivery_runtime_write_scope_binding,
        validate_delivery_write_scope_snapshot,
    )

    status_unit = next((unit for unit in context.status.units if unit.id == context.target.id), None)
    if (
        status_unit is None
        or status_unit.status != "failed"
        or status_unit.failure_code not in _DELIVERY_AMENDMENT_EVIDENCE_STOP_CODES
    ):
        return None
    child_task_id = status_unit.child_task_id
    if not isinstance(child_task_id, str) or not _ID_RE.fullmatch(child_task_id):
        _invalid_failure_evidence("The failed delivery unit has no valid linked child task.")

    try:
        child_state = state_store.load(child_task_id)
    except (OSError, TypeError, ValueError):
        _invalid_failure_evidence("The linked delivery child state is unavailable or malformed.")
    if child_state is None:
        _invalid_failure_evidence("The linked delivery child state was not found.")

    root = Path(context.project_root).resolve()
    expected_plan_path = _project_relative_plan_path(Path(context.plan_path), root)
    if (
        getattr(child_state, "task_id", None) != child_task_id
        or getattr(child_state, "delivery_plan_id", None) != context.plan.plan_id
        or getattr(child_state, "delivery_unit_id", None) != context.target.id
        or getattr(child_state, "delivery_plan_path", None) != expected_plan_path
        or getattr(child_state, "failed", None) is not True
        or getattr(child_state, "done", None) is not False
        or getattr(child_state, "delivery_stop_code", None) != status_unit.failure_code
    ):
        _mismatched_failure_evidence()

    try:
        constraint_context = parse_delivery_constraint_context(child_state)
    except DeliveryConstraintContextError:
        _invalid_failure_evidence("The linked child inherited-constraint context is invalid.")
    if constraint_context is None:
        _invalid_failure_evidence("The linked child has no inherited-constraint context.")
    expected_schema, expected_source_task, expected_constraints = delivery_unit_constraint_context(
        context.plan,
        context.target.id,
    )
    if (
        constraint_context.schema_version != expected_schema
        or constraint_context.plan_id != context.plan.plan_id
        or constraint_context.unit_id != context.target.id
        or constraint_context.plan_path != expected_plan_path
        or (constraint_context.source_task.to_dict() if constraint_context.source_task else None)
        != expected_source_task
        or [value.to_context_dict() for value in constraint_context.constraints] != expected_constraints
    ):
        _mismatched_failure_evidence()

    runtime_scope_root = _delivery_amendment_child_scope_root(child_state, fallback=root)
    runtime_binding_value = getattr(child_state, "delivery_runtime_write_scope_binding", None)
    scope_violation_evidence = status_unit.failure_code == DELIVERY_STOP_UNIT_SCOPE_VIOLATION
    try:
        write_scope = validate_delivery_write_scope_snapshot(
            project_root=runtime_scope_root,
            schema_version=getattr(child_state, "delivery_write_scope_schema_version", None),
            mode=getattr(child_state, "delivery_write_scope_mode", None),
            declared_paths=getattr(child_state, "delivery_declared_write_paths", None),
            declared_exact_file_paths=getattr(child_state, "delivery_declared_write_exact_file_paths", None),
            effective_paths=getattr(child_state, "delivery_effective_write_paths", None),
            effective_exact_file_paths=getattr(child_state, "delivery_effective_write_exact_file_paths", None),
            validate_current_paths=not scope_violation_evidence,
        )
    except DeliveryWriteScopeError:
        _invalid_failure_evidence("The linked child write-scope snapshot is invalid.")
    if write_scope is None:
        _mismatched_failure_evidence()
    if scope_violation_evidence:
        if not delivery_write_scope_matches_unit_declaration(write_scope, context.target.scope_paths):
            _mismatched_failure_evidence()
    else:
        try:
            expected_write_scope = resolve_delivery_write_scope(
                project_root=runtime_scope_root,
                configured_write_paths=write_scope.effective_paths,
                unit_scope_paths=context.target.scope_paths,
            )
        except DeliveryWriteScopeError:
            _mismatched_failure_evidence()
        if expected_write_scope != write_scope:
            _mismatched_failure_evidence()
    try:
        runtime_binding = validate_delivery_runtime_write_scope_binding(
            project_root=runtime_scope_root,
            binding=runtime_binding_value,
            upper_bound_paths=write_scope.effective_paths,
            upper_bound_exact_file_paths=write_scope.effective_exact_file_paths,
            validate_current_paths=not scope_violation_evidence,
        )
    except DeliveryWriteScopeError:
        _invalid_failure_evidence("The linked child runtime write-scope snapshot is invalid.")
    evidence_effective_paths = (
        runtime_binding.effective_paths
        if runtime_binding is not None and runtime_binding.status != DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED
        else write_scope.effective_paths
    )

    stop_disposition = _delivery_amendment_stop_disposition(child_state, status_unit.failure_code)
    reviewer = _delivery_amendment_review_evidence(
        getattr(child_state, "review_cycle_records", None),
        reviewer="reviewer",
        authoritative=stop_disposition,
    )
    security_reviewer = _delivery_amendment_review_evidence(
        getattr(child_state, "security_review_cycle_records", None),
        reviewer="security_reviewer",
        authoritative=stop_disposition,
    )

    (
        changed_paths,
        changed_count,
        omitted_count,
        violation_paths,
        violation_count,
        outside_project_paths,
        outside_project_count,
        omitted_outside_project_count,
    ) = _delivery_amendment_changed_file_evidence(child_state, root=runtime_scope_root)

    status_by_id = {unit.id: unit for unit in context.status.units}
    dependency_ids = _delivery_amendment_dependency_ids(context)
    identities: list[DeliveryAmendmentDependencyIdentity] = []
    seen_handoffs: set[str] = set()
    handoff_values = getattr(child_state, "delivery_dependency_handoffs", None)
    if not isinstance(handoff_values, list):
        _invalid_failure_evidence("The linked child dependency-handoff evidence is malformed.")
    for value in handoff_values:
        try:
            handoff = parse_delivery_unit_handoff(value)
        except DeliveryHandoffError:
            _invalid_failure_evidence("The linked child dependency-handoff evidence is invalid.")
        dependency = status_by_id.get(handoff.unit_id)
        if (
            handoff.plan_id != context.plan.plan_id
            or handoff.unit_id not in dependency_ids
            or dependency is None
            or dependency.status != "done"
            or handoff.child_task_id != dependency.child_task_id
            or handoff.schema_version != dependency.handoff_schema_version
            or handoff.fingerprint != dependency.handoff_fingerprint
            or not delivery_unit_handoff_matches_unit(handoff, dependency)
            or handoff.unit_id in seen_handoffs
        ):
            _mismatched_failure_evidence()
        seen_handoffs.add(handoff.unit_id)
        identities.append(
            DeliveryAmendmentDependencyIdentity(
                plan_id=handoff.plan_id,
                unit_id=handoff.unit_id,
                child_task_id=handoff.child_task_id,
            )
        )

    recommended_action = delivery_terminal_stop_recommended_action(status_unit.failure_code)
    if recommended_action is None:
        _invalid_failure_evidence("The linked child terminal stop has no recovery action.")
    preliminary = DeliveryAmendmentFailureEvidence(
        plan_id=context.plan.plan_id,
        unit_id=context.target.id,
        child_task_id=child_task_id,
        failure_code=status_unit.failure_code,
        recommended_action=recommended_action,
        inherited_constraints=tuple(expected_constraints),
        declared_write_paths=write_scope.declared_paths,
        effective_write_paths=evidence_effective_paths,
        changed_paths=changed_paths,
        changed_count=changed_count,
        omitted_changed_paths_count=omitted_count,
        violation_paths=violation_paths,
        violation_count=violation_count,
        outside_project_paths=outside_project_paths,
        outside_project_count=outside_project_count,
        omitted_outside_project_paths_count=omitted_outside_project_count,
        reviewer=reviewer,
        security_reviewer=security_reviewer,
        stop_disposition=stop_disposition,
        dependency_handoffs=tuple(sorted(identities, key=lambda value: value.unit_id)),
        fingerprint="",
    )
    fingerprint = hashlib.sha256(
        json.dumps(preliminary.payload_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return replace(preliminary, fingerprint=fingerprint)


def _invalid_failure_evidence(message: str) -> None:
    raise DeliveryAmendmentError("delivery_amend.failure_evidence_invalid", message)


def _delivery_amendment_child_scope_root(child_state: Any, *, fallback: Path) -> Path:
    value = getattr(child_state, "worktree_path", None)
    if value is None:
        return fallback
    if not isinstance(value, str) or not value:
        _invalid_failure_evidence("The linked child authoritative worktree is invalid.")
    try:
        root = Path(value).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("child project root is not a directory")
        base_value = getattr(child_state, "worktree_base", None)
        if base_value is not None:
            if not isinstance(base_value, str) or not base_value:
                raise ValueError("child worktree base is invalid")
            base = Path(base_value).resolve(strict=True)
            root.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        _invalid_failure_evidence("The linked child authoritative worktree is invalid.")
    return root


def _mismatched_failure_evidence() -> None:
    raise DeliveryAmendmentError(
        "delivery_amend.failure_evidence_mismatch",
        "The linked child failure evidence does not match the selected delivery plan and unit.",
    )


def _delivery_amendment_stop_disposition(child_state: Any, failure_code: str) -> dict[str, Any] | None:
    try:
        disposition_code = child_state.delivery_stop_code_from_disposition()
    except (AttributeError, TypeError, ValueError):
        _invalid_failure_evidence("The linked child stop disposition is invalid.")
    value = getattr(child_state, "delivery_stop_disposition", None)
    if (
        failure_code
        in {
            DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
            DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
        }
        and disposition_code != failure_code
    ):
        _mismatched_failure_evidence()
    if value is None or failure_code == DELIVERY_STOP_UNIT_SCOPE_VIOLATION:
        return None
    return {
        "schema_version": value["schema_version"],
        "disposition": value["disposition"],
        "summary": value["summary"],
        "recommended_action": value["recommended_action"],
        "source": value["source"],
    }


def _delivery_amendment_review_evidence(
    records: Any,
    *,
    reviewer: str,
    authoritative: dict[str, Any] | None,
) -> DeliveryAmendmentReviewEvidence:
    from core.structured_output import (
        DELIVERY_DISPOSITION_APPROVED,
        DELIVERY_DISPOSITION_SCHEMA_VERSION,
        DELIVERY_REVIEW_DISPOSITIONS,
        delivery_disposition_recommended_action,
    )

    if not isinstance(records, list):
        _invalid_failure_evidence("The linked child review evidence is malformed.")
    dispositions: list[dict[str, Any]] = []
    summaries: list[str] = []
    issues_count = 0
    records_count = 0
    for record in records:
        if not isinstance(record, dict):
            _invalid_failure_evidence("The linked child review evidence is malformed.")
        record_reviewer = record.get("reviewer")
        if record_reviewer not in {None, reviewer}:
            continue
        records_count += 1
        if record.get("approved") is False:
            issues_count += 1
        value = record.get("disposition")
        if value is None:
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "disposition", "summary", "recommended_action"}
            or value.get("schema_version") != DELIVERY_DISPOSITION_SCHEMA_VERSION
            or value.get("disposition") not in DELIVERY_REVIEW_DISPOSITIONS
            or value.get("recommended_action") != delivery_disposition_recommended_action(value.get("disposition"))
            or not isinstance(value.get("summary"), str)
            or not is_safe_delivery_public_metadata(value["summary"])
        ):
            _invalid_failure_evidence("The linked child review disposition is invalid.")
        if value["disposition"] == DELIVERY_DISPOSITION_APPROVED:
            if record.get("approved") is not True:
                _invalid_failure_evidence("The linked child review approval is inconsistent.")
            continue
        projected = {
            "schema_version": value["schema_version"],
            "disposition": value["disposition"],
            "summary": value["summary"],
            "recommended_action": value["recommended_action"],
        }
        if projected not in dispositions and len(dispositions) < _MAX_DELIVERY_AMENDMENT_EVIDENCE_DISPOSITIONS:
            dispositions.append(projected)
            summaries.append(value["summary"])
    if authoritative and authoritative.get("source") == reviewer:
        projected = {
            "schema_version": DELIVERY_DISPOSITION_SCHEMA_VERSION,
            "disposition": authoritative["disposition"],
            "summary": authoritative["summary"],
            "recommended_action": authoritative["recommended_action"],
        }
        if projected not in dispositions and len(dispositions) < _MAX_DELIVERY_AMENDMENT_EVIDENCE_DISPOSITIONS:
            dispositions.append(projected)
            summaries.append(authoritative["summary"])
    return DeliveryAmendmentReviewEvidence(
        records_count=records_count,
        issues_count=issues_count,
        issue_summaries=tuple(summaries),
        dispositions=tuple(dispositions),
    )


def _delivery_amendment_changed_file_evidence(
    child_state: Any,
    *,
    root: Path,
) -> tuple[tuple[str, ...], int, int, tuple[str, ...], int, tuple[str, ...], int, int]:
    raw_changed = getattr(child_state, "files_changed", None)
    if not isinstance(raw_changed, list):
        _invalid_failure_evidence("The linked child changed-file evidence is malformed.")
    changed_values: list[Any] = list(raw_changed)
    changed_count = len(raw_changed)
    violation_values: Any = []
    violation_count = 0
    outside_project_values: Any = []
    outside_project_count = 0
    records = getattr(child_state, "validation_cycle_records", None)
    if not isinstance(records, list):
        _invalid_failure_evidence("The linked child validation evidence is malformed.")
    for record in records:
        if not isinstance(record, dict) or record.get("phase") != "delivery_scope_audit":
            continue
        status = record.get("status")
        metadata = record.get("metadata")
        if status not in {"passed", "failed"} or not isinstance(metadata, dict):
            continue
        code = metadata.get("code")
        if code not in {"delivery_scope_audit_passed", "unit_scope_violation"}:
            continue
        audit_changed_values = metadata.get("changed_paths")
        if audit_changed_values is None and code == "delivery_scope_audit_passed":
            # Compatibility with successful audit records created before paths were retained.
            continue
        if audit_changed_values is None:
            audit_changed_values = raw_changed
        audit_changed_count = _nonnegative_evidence_count(metadata.get("changed_count"), "changed-file")
        if not isinstance(audit_changed_values, list) or audit_changed_count < len(audit_changed_values):
            _invalid_failure_evidence("The linked child scope-audit counts are inconsistent.")
        changed_values.extend(audit_changed_values)
        changed_count = max(changed_count, audit_changed_count)
        if status == "failed" and code == "unit_scope_violation":
            audit_violation_values = metadata.get("violation_paths", [])
            audit_violation_count = _nonnegative_evidence_count(
                metadata.get("violation_count"),
                "scope-violation",
            )
            audit_outside_project_values = metadata.get("outside_project_paths", [])
            audit_outside_project_count = _nonnegative_evidence_count(
                metadata.get("outside_project_count", 0),
                "outside-project",
            )
            if (
                not isinstance(audit_violation_values, list)
                or not isinstance(audit_outside_project_values, list)
                or audit_outside_project_count < len(audit_outside_project_values)
                or audit_violation_count < len(audit_violation_values) + audit_outside_project_count
            ):
                _invalid_failure_evidence("The linked child scope-audit counts are inconsistent.")
            violation_values = audit_violation_values
            violation_count = audit_violation_count
            outside_project_values = audit_outside_project_values
            outside_project_count = audit_outside_project_count
    unique_changed_values: list[Any] = []
    seen_changed_values: set[str] = set()
    for value in changed_values:
        if isinstance(value, str):
            if value in seen_changed_values:
                continue
            seen_changed_values.add(value)
        unique_changed_values.append(value)
    changed_count = max(changed_count, len(unique_changed_values))
    changed_paths, unsafe_changed_count = _sanitize_delivery_amendment_paths(unique_changed_values, root=root)
    violation_paths, _ = _sanitize_delivery_amendment_paths(violation_values, root=root)
    outside_project_paths, unsafe_outside_project_count = _sanitize_delivery_amendment_worktree_paths(
        outside_project_values
    )
    omitted_count = max(changed_count - len(changed_paths), unsafe_changed_count)
    omitted_outside_project_count = max(
        outside_project_count - len(outside_project_paths),
        unsafe_outside_project_count,
    )
    return (
        changed_paths,
        changed_count,
        omitted_count,
        violation_paths,
        violation_count,
        outside_project_paths,
        outside_project_count,
        omitted_outside_project_count,
    )


def _nonnegative_evidence_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid_failure_evidence(f"The linked child {label} count is invalid.")
    return value


def _sanitize_delivery_amendment_paths(values: Any, *, root: Path) -> tuple[tuple[str, ...], int]:
    if not isinstance(values, list):
        _invalid_failure_evidence("The linked child changed paths are malformed.")
    result: list[str] = []
    omitted = 0
    seen: set[str] = set()
    for value in values:
        safe = _safe_delivery_amendment_path(value, root=root)
        if safe is None or safe in seen:
            omitted += 1
            continue
        seen.add(safe)
        if len(result) >= _MAX_DELIVERY_AMENDMENT_EVIDENCE_PATHS:
            omitted += 1
            continue
        result.append(safe)
    return tuple(sorted(result)), omitted


def _sanitize_delivery_amendment_worktree_paths(values: Any) -> tuple[tuple[str, ...], int]:
    if not isinstance(values, list):
        _invalid_failure_evidence("The linked child outside-project paths are malformed.")
    result: list[str] = []
    omitted = 0
    seen: set[str] = set()
    for value in values:
        safe = _safe_delivery_amendment_worktree_path(value)
        if safe is None or safe in seen:
            omitted += 1
            continue
        seen.add(safe)
        if len(result) >= _MAX_DELIVERY_AMENDMENT_EVIDENCE_PATHS:
            omitted += 1
            continue
        result.append(safe)
    return tuple(sorted(result)), omitted


def _safe_delivery_amendment_worktree_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or ".." in windows.parts
        or not is_safe_delivery_public_metadata(value)
    ):
        return None
    normalized = windows.as_posix() if os.name == "nt" else posix.as_posix()
    return normalized if normalized not in {"", "."} else None


def _safe_delivery_amendment_path(value: Any, *, root: Path) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or ".." in windows.parts
        or not is_safe_delivery_public_metadata(value)
    ):
        return None
    normalized = windows.as_posix() if os.name == "nt" else posix.as_posix()
    try:
        relative = (root / normalized).resolve(strict=False).relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return relative or "."


def _delivery_amendment_dependency_ids(context: DeliveryAmendmentTarget) -> set[str]:
    units = {unit.id: unit for unit in context.plan.units}
    result: set[str] = set()
    pending = list(context.target.depends_on)
    while pending:
        unit_id = pending.pop(0)
        if unit_id in result:
            continue
        result.add(unit_id)
        unit = units.get(unit_id)
        if unit is not None:
            pending.extend(unit.depends_on)
    return result


def create_delivery_amendment_proposal(
    path: str | Path,
    target_unit_id: str,
    draft: DeliveryAmendmentAuthoringDraft,
    *,
    project_root: Path | None = None,
    proposal_root: Path,
    project_config: dict | None = None,
    expected_source_snapshot: DeliveryAmendmentSourceSnapshot | None = None,
    created_at: str | None = None,
) -> tuple[DeliveryAmendmentProposal, Path]:
    context = inspect_delivery_amendment_target(
        path,
        target_unit_id,
        project_root=project_root,
        project_config=project_config,
    )
    source_snapshot = capture_delivery_amendment_source_snapshot(context)
    if expected_source_snapshot is not None and source_snapshot != expected_source_snapshot:
        raise DeliveryAmendmentError(
            "delivery_amend.authoring_inputs_stale",
            "Delivery plan, target task, or progress changed while the amendment was being authored; retry prepare.",
        )
    if draft.plan_id != context.plan.plan_id or draft.target_unit_id != target_unit_id:
        raise DeliveryAmendmentError(
            "delivery_amend.authoring_mismatch",
            "Authored amendment does not match the selected plan and target unit.",
        )
    if len(draft.replacement_units) < 2:
        raise DeliveryAmendmentError(
            "delivery_amend.replacements_too_few",
            "A split proposal must contain at least two replacement units.",
        )

    _validate_amendment_constraint_verification(context, draft)

    root = Path(context.project_root)
    plan_path = Path(context.plan_path)
    target_task_path, target_task_description = read_delivery_amendment_target_task(
        context,
        expected_fingerprint=source_snapshot.target_task_fingerprint,
    )
    assignment_units = [
        DeliveryAssetAssignmentUnit(unit.id, unit.task_markdown, unit.asset_paths) for unit in draft.replacement_units
    ]
    try:
        rendered_tasks = render_delivery_asset_assignments(
            target_task_description,
            assignment_units,
            source_task_path=target_task_path,
            project_root=root,
            project_config=project_config,
            allow_source_asset_manifest=True,
        )
    except DeliveryAssetAssignmentError as exc:
        raise DeliveryAmendmentError(
            f"delivery_amend.{exc.code}",
            exc.message,
        ) from None
    except (OSError, RuntimeError, ValueError, KeyError):
        raise DeliveryAmendmentError(
            "delivery_amend.asset_assignment_check_failed",
            "Delivery amendment asset assignments could not be validated.",
        ) from None
    rendered_drafts = [replace(unit, task_markdown=rendered_tasks[unit.id]) for unit in draft.replacement_units]
    replacement_units = _normalize_replacements(context, rendered_drafts, root=root, plan_path=plan_path)
    readiness_errors = _replacement_contract_readiness_errors(replacement_units, project_config=project_config)
    if readiness_errors:
        issue = readiness_errors[0]
        raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
    timestamp = created_at or _utc_now()
    budget_exceeded = _budget_exceeded_from_mapping(draft.budget_exceeded)
    amend_reason = _optional_stable_code(draft.amend_reason, "amend_reason")
    source_plan_path = _project_relative_plan_path(plan_path, root)
    preliminary = DeliveryAmendmentProposal(
        proposal_id="pending",
        plan_id=context.plan.plan_id,
        target_unit_id=target_unit_id,
        source_plan_path=source_plan_path,
        source_plan_fingerprint=source_snapshot.source_plan_fingerprint,
        source_plan_blob_id=source_snapshot.source_plan_blob_id,
        target_task_fingerprint=source_snapshot.target_task_fingerprint,
        retained_contract_fingerprints=source_snapshot.retained_contract_fingerprints,
        progress_fingerprint=source_snapshot.progress_fingerprint,
        source_assembly_base_commit=source_snapshot.assembly_base_commit,
        source_assembled_commit=source_snapshot.assembled_commit,
        created_at=timestamp,
        replacement_units=replacement_units,
        amend_reason=amend_reason,
        budget_exceeded=budget_exceeded,
    )
    amended_plan, rewired_ids = _amended_plan_data(plan_path, context, preliminary)
    amended_plan_content = yaml.safe_dump(amended_plan, sort_keys=False, allow_unicode=True).encode("utf-8")
    proposal_payload = {
        "plan_id": context.plan.plan_id,
        "target_unit_id": target_unit_id,
        "source_plan_path": source_plan_path,
        "source_plan_fingerprint": source_snapshot.source_plan_fingerprint,
        "source_plan_blob_id": source_snapshot.source_plan_blob_id,
        "target_task_fingerprint": source_snapshot.target_task_fingerprint,
        "retained_contract_fingerprints": source_snapshot.retained_contract_fingerprints,
        "progress_fingerprint": source_snapshot.progress_fingerprint,
        "source_assembly_base_commit": source_snapshot.assembly_base_commit,
        "source_assembled_commit": source_snapshot.assembled_commit,
        "created_at": timestamp,
        "replacement_units": [unit.to_dict() for unit in replacement_units],
        "amended_plan_fingerprint": hashlib.sha256(amended_plan_content).hexdigest(),
    }
    if rewired_ids:
        proposal_payload["rewired_unit_ids"] = rewired_ids
    if amend_reason:
        proposal_payload["amend_reason"] = amend_reason
    if budget_exceeded:
        proposal_payload["budget_exceeded"] = budget_exceeded.to_dict()
    proposal_id = _proposal_content_id(proposal_payload)
    proposal = DeliveryAmendmentProposal(
        proposal_id=proposal_id,
        plan_id=context.plan.plan_id,
        target_unit_id=target_unit_id,
        source_plan_path=source_plan_path,
        source_plan_fingerprint=proposal_payload["source_plan_fingerprint"],
        source_plan_blob_id=proposal_payload["source_plan_blob_id"],
        target_task_fingerprint=proposal_payload["target_task_fingerprint"],
        retained_contract_fingerprints=proposal_payload["retained_contract_fingerprints"],
        progress_fingerprint=proposal_payload["progress_fingerprint"],
        source_assembly_base_commit=proposal_payload["source_assembly_base_commit"],
        source_assembled_commit=proposal_payload["source_assembled_commit"],
        created_at=timestamp,
        replacement_units=replacement_units,
        amended_plan_fingerprint=proposal_payload["amended_plan_fingerprint"],
        rewired_unit_ids=rewired_ids,
        amend_reason=amend_reason,
        budget_exceeded=budget_exceeded,
    )
    proposal_path = delivery_amendment_proposal_path(proposal_root, proposal.plan_id, proposal.proposal_id)
    validation = check_delivery_plan_data(
        amended_plan,
        project_root=root,
        plan_path=str(plan_path),
        virtual_task_paths={unit.task_path for unit in proposal.replacement_units},
    )
    if not validation.valid:
        issue = validation.errors[0]
        raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
    _assert_replacement_task_paths_available(
        proposal.replacement_units,
        root=root,
        private_artifact_roots=context.private_artifact_roots,
    )
    artifact_issue = delivery_artifact_compatibility_issue(
        root,
        parent_commit=context.assembled_commit,
        artifacts=_proposal_assembly_artifacts(
            proposal,
            amended_plan_content,
            project_root=root,
            plan=validation.plan,
            private_artifact_roots=context.private_artifact_roots,
            enforce_parent_state=True,
        ),
    )
    if artifact_issue is not None:
        raise DeliveryAmendmentError(artifact_issue.code, artifact_issue.message, artifact_issue.path)
    _assert_proposal_source_fingerprints_current(context, proposal)
    _write_new_proposal(proposal_path, proposal)
    try:
        _assert_replacement_task_paths_available(
            proposal.replacement_units,
            root=root,
            private_artifact_roots=context.private_artifact_roots,
        )
        _assert_proposal_source_fingerprints_current(context, proposal)
    except BaseException:
        _remove_published_proposal(proposal_path)
        raise
    return proposal, proposal_path


def _validate_amendment_constraint_verification(
    context: DeliveryAmendmentTarget,
    draft: DeliveryAmendmentAuthoringDraft,
) -> None:
    replacement_ids = [unit.id for unit in draft.replacement_units]
    applicable = [constraint for constraint in context.plan.constraints if context.target.id in constraint.unit_ids]
    if not applicable:
        return
    verification = draft.constraint_verification
    if verification is None:
        raise DeliveryAmendmentError(
            "delivery_amend.constraint_verification_required",
            "Applicable constraints must be independently verified before an amendment can be published.",
        )
    expected = [(constraint.id, constraint.kind, constraint.summary, replacement_ids) for constraint in applicable]
    actual = [
        (constraint.id, constraint.kind, constraint.summary, constraint.unit_ids)
        for constraint in verification.constraints
    ]
    if actual != expected:
        raise DeliveryAmendmentError(
            "delivery_amend.constraint_verification_invalid",
            "Amendment constraint verification does not match the target's applicable constraints.",
        )
    if not verification.constraints_complete:
        raise DeliveryAmendmentError(
            "delivery_amend.constraint_verification_incomplete",
            "Independent verification found an omitted or incompletely assigned amendment constraint.",
        )
    for constraint in verification.constraints:
        if constraint.disposition == "conflict":
            raise DeliveryAmendmentError(
                "delivery_amend.constraint_conflict",
                "A replacement unit conflicts with an inherited delivery constraint.",
            )
        if constraint.disposition != "preserved":
            raise DeliveryAmendmentError(
                "delivery_amend.constraint_review_required",
                "Amendment constraint verification requires operator review.",
            )


def delivery_amendment_proposal_path(proposal_root: Path, plan_id: str, proposal_id: str) -> Path:
    if not _ID_RE.fullmatch(plan_id) or not _ID_RE.fullmatch(proposal_id):
        raise DeliveryAmendmentError("delivery_amend.proposal_id_invalid", "Proposal id is not a safe identifier.")
    root = proposal_root.resolve()
    parent = root
    for component in ("delivery-amendments", plan_id):
        parent /= component
        if parent.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_path_symlink",
                "Proposal path must not traverse symlinks below the configured proposal root.",
            )
        try:
            parent.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_path_unsafe",
                "Proposal path escapes the configured proposal root.",
            ) from None
    return parent / f"{proposal_id}.json"


def preview_delivery_amendment(
    path: str | Path,
    proposal_id: str,
    *,
    proposal_root: Path,
    project_root: Path | None = None,
    project_config: dict | None = None,
) -> DeliveryAmendmentApplyResult:
    return _preflight_amendment(
        path,
        proposal_id,
        proposal_root=proposal_root,
        project_root=project_root,
        project_config=project_config,
        dry_run=True,
    ).result


def apply_delivery_amendment(
    path: str | Path,
    proposal_id: str,
    *,
    proposal_root: Path,
    project_root: Path | None = None,
    project_config: dict | None = None,
) -> DeliveryAmendmentApplyResult:
    initial = _preflight_amendment(
        path,
        proposal_id,
        proposal_root=proposal_root,
        project_root=project_root,
        project_config=project_config,
        dry_run=False,
    )
    if initial.proposal is None or initial.result.project_root is None:
        return initial.result
    if any(issue.code == "delivery_amend.proposal_plan_mismatch" for issue in initial.result.errors):
        return initial.result
    root = Path(initial.result.project_root)
    try:
        lock = acquire_delivery_progress_lock(root, initial.proposal.plan_id, owner="delivery.amend")
    except DeliveryProgressLockError:
        return _blocked_result(
            initial.result,
            DeliveryPlanIssue("error", "delivery.locked", "Delivery progress is locked by another process."),
        )
    except OSError:
        return _blocked_result(
            initial.result,
            DeliveryPlanIssue(
                "error",
                "delivery_amend.lock_failed",
                "Delivery amendment could not create or open its progress lock.",
            ),
        )

    with lock:
        refreshed = _preflight_amendment(
            path,
            proposal_id,
            proposal_root=proposal_root,
            project_root=project_root,
            project_config=project_config,
            dry_run=False,
        )
        if refreshed.proposal is None:
            return refreshed.result
        proposal = refreshed.proposal
        events_path = delivery_events_path(root, proposal.plan_id)
        timestamp = _utc_now()
        if refreshed.artifacts_applied and refreshed.target is not None:
            try:
                _reconcile_applied_amendment(
                    root,
                    refreshed.target,
                    proposal,
                    amendment_commit=refreshed.amendment_commit,
                    events_path=events_path,
                    rewired_ids=refreshed.result.rewired_unit_ids,
                    timestamp=timestamp,
                )
            except DeliveryAmendmentError as exc:
                return _blocked_result(refreshed.result, exc.issue)
            return replace(
                refreshed.result,
                applied=True,
                message="Delivery plan amendment is applied and its assembly audit is current.",
            )
        if refreshed.target is None or not refreshed.result.ready or refreshed.plan_data is None:
            try:
                append_delivery_progress_events(
                    events_path,
                    [
                        _amendment_event(proposal, "plan.amend_started", timestamp=timestamp),
                        _amendment_event(
                            proposal,
                            "plan.amend_failed",
                            timestamp=timestamp,
                            failure_code=(refreshed.result.errors[0].code if refreshed.result.errors else None),
                        ),
                    ],
                )
            except Exception:
                return _blocked_result(
                    refreshed.result,
                    DeliveryPlanIssue(
                        "error",
                        "delivery_amend.event_write_failed",
                        "Delivery amendment failed and its audit event could not be recorded.",
                    ),
                )
            return refreshed.result
        try:
            append_delivery_progress_event(
                events_path,
                _amendment_event(proposal, "plan.amend_started", timestamp=timestamp),
            )
        except Exception:
            return _blocked_result(
                refreshed.result,
                DeliveryPlanIssue(
                    "error",
                    "delivery_amend.event_write_failed",
                    "Delivery amendment could not record its audit start event; no plan artifacts were changed.",
                ),
            )
        try:
            _write_amended_artifacts(
                Path(refreshed.target.plan_path),
                root,
                refreshed.plan_data,
                proposal,
                source_context=refreshed.target,
                events_path=events_path,
                rewired_ids=refreshed.result.rewired_unit_ids,
                timestamp=timestamp,
            )
        except BaseException as exc:
            interrupted = not isinstance(exc, Exception)
            if isinstance(exc, DeliveryAmendmentError):
                failure_issue = exc.issue
            elif interrupted:
                failure_issue = DeliveryPlanIssue(
                    "error",
                    "delivery_amend.interrupted",
                    "Delivery amendment was interrupted after artifact rollback.",
                )
            else:
                failure_issue = DeliveryPlanIssue(
                    "error",
                    "delivery_amend.write_failed",
                    "Delivery amendment failed while writing artifacts; previous tracked artifacts were restored.",
                )
            event_failure_issue = None
            try:
                append_delivery_progress_event(
                    events_path,
                    _amendment_event(
                        proposal,
                        "plan.amend_failed",
                        timestamp=_utc_now(),
                        failure_code=failure_issue.code,
                    ),
                )
            except Exception:
                event_failure_issue = DeliveryPlanIssue(
                    "error",
                    "delivery_amend.event_write_failed",
                    "Delivery amendment failed and its terminal audit event could not be recorded.",
                )
            if interrupted:
                if event_failure_issue is not None and hasattr(exc, "add_note"):
                    exc.add_note(event_failure_issue.message)
                raise
            return _replace_errors(
                refreshed.result,
                [
                    *refreshed.result.errors,
                    failure_issue,
                    *([event_failure_issue] if event_failure_issue is not None else []),
                ],
            )

        return DeliveryAmendmentApplyResult(
            plan_path=refreshed.result.plan_path,
            project_root=refreshed.result.project_root,
            proposal_id=proposal.proposal_id,
            target_unit_id=proposal.target_unit_id,
            replacement_ids=proposal.replacement_ids,
            rewired_unit_ids=refreshed.result.rewired_unit_ids,
            dry_run=False,
            ready=True,
            applied=True,
            proposal_path=refreshed.result.proposal_path,
            errors=[],
            warnings=refreshed.result.warnings,
            message="Delivery plan amendment applied.",
        )


def _preflight_amendment(
    path: str | Path,
    proposal_id: str,
    *,
    proposal_root: Path,
    project_root: Path | None,
    project_config: dict | None,
    dry_run: bool,
) -> _AmendmentPreflight:
    plan_check = check_delivery_plan_file(path, project_root=project_root)
    plan_path = plan_check.plan_path
    root_text = plan_check.project_root
    base = DeliveryAmendmentApplyResult(
        plan_path=plan_path,
        project_root=root_text,
        proposal_id=proposal_id,
        target_unit_id=None,
        replacement_ids=[],
        rewired_unit_ids=[],
        dry_run=dry_run,
        ready=False,
        applied=False,
        proposal_path=None,
        errors=[],
        warnings=list(plan_check.warnings),
        message="Delivery plan amendment is blocked.",
    )
    if not plan_check.valid or plan_check.plan is None or root_text is None:
        return _AmendmentPreflight(
            result=_replace_errors(
                base,
                plan_check.errors
                or [DeliveryPlanIssue("error", "delivery_amend.plan_invalid", "Delivery plan is invalid.")],
            )
        )
    try:
        _validate_amendment_plan_destination(
            Path(plan_path),
            Path(root_text),
            private_artifact_roots=_configured_private_artifact_roots(Path(root_text), project_config),
        )
    except DeliveryAmendmentError as exc:
        return _AmendmentPreflight(result=_blocked_result(base, exc.issue))
    proposal: DeliveryAmendmentProposal | None = None
    target: DeliveryAmendmentTarget | None = None
    plan_data: dict[str, Any] | None = None
    try:
        proposal_path = delivery_amendment_proposal_path(proposal_root, plan_check.plan.plan_id, proposal_id)
        proposal = load_delivery_amendment_proposal(
            proposal_path,
            expected_proposal_id=proposal_id,
            project_root=Path(root_text),
        )
        base = DeliveryAmendmentApplyResult(
            **{
                **base.__dict__,
                "proposal_path": str(proposal_path),
                "target_unit_id": proposal.target_unit_id,
                "replacement_ids": proposal.replacement_ids,
            }
        )
        if proposal.plan_id != plan_check.plan.plan_id:
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_plan_mismatch", "Proposal belongs to a different delivery plan."
            )
        if proposal.source_plan_path != _project_relative_plan_path(Path(plan_path), Path(root_text)):
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_plan_mismatch",
                "Proposal was prepared for a different delivery plan path.",
            )
        applied = _preflight_applied_amendment(
            plan_check.plan,
            Path(plan_path),
            Path(root_text),
            proposal,
            project_config=project_config,
            dry_run=dry_run,
            base=base,
        )
        if applied is not None:
            return applied
        target = inspect_delivery_amendment_target(
            plan_path,
            proposal.target_unit_id,
            project_root=Path(root_text),
            project_config=project_config,
        )
        if _plan_fingerprint(Path(plan_path)) != proposal.source_plan_fingerprint:
            raise DeliveryAmendmentError(
                "delivery_amend.plan_stale", "Delivery plan changed after the proposal was prepared."
            )
        if _target_task_fingerprint(target) != proposal.target_task_fingerprint:
            raise DeliveryAmendmentError(
                "delivery_amend.target_task_stale",
                "Target unit task changed after the proposal was prepared.",
            )
        _assert_retained_contract_fingerprints_current(target, proposal)
        if _progress_fingerprint(target) != proposal.progress_fingerprint:
            raise DeliveryAmendmentError(
                "delivery_amend.progress_stale", "Delivery progress changed after the proposal was prepared."
            )
        if target.assembly_base_commit != proposal.source_assembly_base_commit:
            raise DeliveryAmendmentError(
                "delivery_amend.assembly_stale", "Delivery assembly base changed after the proposal was prepared."
            )
        if target.assembled_commit != proposal.source_assembled_commit:
            raise DeliveryAmendmentError(
                "delivery_amend.assembly_stale", "Delivery assembly changed after the proposal was prepared."
            )
        mutation_expected_commit = target.recorded_assembled_commit
        if mutation_expected_commit is None:
            try:
                current_branch_commit = delivery_branch_commit(Path(root_text), target.plan.final_branch)
            except OSError:
                current_branch_commit = None
            if current_branch_commit == target.assembled_commit:
                mutation_expected_commit = current_branch_commit
        mutation_preview = preview_delivery_assembly(
            Path(root_text),
            branch=target.plan.final_branch,
            base_commit=target.assembly_base_commit,
            expected_commit=mutation_expected_commit,
            units=[],
        )
        if not mutation_preview.success:
            issue = mutation_preview.error or DeliveryPlanIssue(
                "error",
                "delivery.assembly_failed",
                "Delivery assembly is not ready for amendment.",
            )
            raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
        _validate_loaded_replacement_contracts(
            proposal.plan_id,
            proposal.target_unit_id,
            proposal.replacement_units,
            proposal.amend_reason,
            proposal.budget_exceeded,
            inherited_from=target.target,
            project_root=Path(root_text),
        )
        _validate_proposal_against_target(proposal, target)
        readiness_errors = _replacement_contract_readiness_errors(
            proposal.replacement_units,
            project_config=project_config,
        )
        if readiness_errors:
            return _AmendmentPreflight(
                result=_replace_errors(base, readiness_errors),
                proposal=proposal,
                target=target,
            )
        plan_data, rewired_ids = _amended_plan_data(Path(plan_path), target, proposal)
        plan_content = yaml.safe_dump(plan_data, sort_keys=False, allow_unicode=True).encode("utf-8")
        if (
            hashlib.sha256(plan_content).hexdigest() != proposal.amended_plan_fingerprint
            or rewired_ids != proposal.rewired_unit_ids
        ):
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_output_mismatch",
                "Recomputed amendment output no longer matches the proposal; prepare a new proposal.",
            )
        validation = check_delivery_plan_data(
            plan_data,
            project_root=Path(root_text),
            plan_path=plan_path,
            virtual_task_paths={unit.task_path for unit in proposal.replacement_units},
        )
        if not validation.valid:
            return _AmendmentPreflight(
                result=_replace_errors(base, validation.errors),
                proposal=proposal,
                target=target,
                plan_data=plan_data,
            )
        artifact_preview = preview_delivery_artifacts(
            Path(root_text),
            branch=target.plan.final_branch,
            parent_commit=target.assembled_commit,
            artifacts=_proposal_assembly_artifacts(
                proposal,
                plan_content,
                project_root=Path(root_text),
                plan=validation.plan,
                private_artifact_roots=target.private_artifact_roots,
                enforce_parent_state=True,
            ),
        )
        if not artifact_preview.success:
            issue = artifact_preview.error or DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_git_failed",
                "Git could not validate delivery amendment artifacts.",
            )
            raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
        result = DeliveryAmendmentApplyResult(
            plan_path=plan_path,
            project_root=root_text,
            proposal_id=proposal.proposal_id,
            target_unit_id=proposal.target_unit_id,
            replacement_ids=proposal.replacement_ids,
            rewired_unit_ids=rewired_ids,
            dry_run=dry_run,
            ready=True,
            applied=False,
            proposal_path=str(proposal_path),
            errors=[],
            warnings=validation.warnings,
            message=(
                "Delivery plan amendment preview passed; no files were changed."
                if dry_run
                else "Delivery plan amendment is ready to apply."
            ),
        )
        return _AmendmentPreflight(result=result, proposal=proposal, target=target, plan_data=plan_data)
    except DeliveryAmendmentError as exc:
        return _AmendmentPreflight(
            result=_blocked_result(base, exc.issue),
            proposal=proposal,
            target=target,
            plan_data=plan_data,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, yaml.YAMLError):
        return _AmendmentPreflight(
            result=_blocked_result(
                base,
                DeliveryPlanIssue(
                    "error", "delivery_amend.proposal_invalid", "Proposal artifact is missing or malformed."
                ),
            )
        )


def load_delivery_amendment_proposal(
    path: Path,
    *,
    expected_proposal_id: str,
    project_root: Path,
) -> DeliveryAmendmentProposal:
    if path.is_symlink() or not path.is_file():
        raise DeliveryAmendmentError("delivery_amend.proposal_unknown", "Proposal id was not found.")
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object_pairs_without_duplicates)
    if not isinstance(data, dict):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Proposal artifact must be a JSON object.")
    allowed = {
        "schema_version",
        "proposal_id",
        "plan_id",
        "target_unit_id",
        "source_plan_path",
        "source_plan_fingerprint",
        "source_plan_blob_id",
        "target_task_fingerprint",
        "retained_contract_fingerprints",
        "progress_fingerprint",
        "source_assembly_base_commit",
        "source_assembled_commit",
        "created_at",
        "replacement_units",
        "amended_plan_fingerprint",
        "rewired_unit_ids",
        "amend_reason",
        "budget_exceeded",
    }
    if set(data) - allowed:
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Proposal contains unsupported fields.")
    if data.get("schema_version") != SUPPORTED_DELIVERY_AMENDMENT_PROPOSAL_SCHEMA_VERSION:
        raise DeliveryAmendmentError("delivery_amend.proposal_version", "Proposal schema_version is unsupported.")
    proposal_id = _required_id(data, "proposal_id")
    if proposal_id != expected_proposal_id:
        raise DeliveryAmendmentError("delivery_amend.proposal_id_mismatch", "Proposal id does not match its artifact.")
    plan_id = _required_id(data, "plan_id")
    target_unit_id = _required_string(data, "target_unit_id")
    source_plan_path = _required_string(data, "source_plan_path")
    source_fingerprint = _required_hash(data, "source_plan_fingerprint")
    source_plan_blob_id = _required_object_id(data, "source_plan_blob_id")
    target_task_fingerprint = _required_hash(data, "target_task_fingerprint")
    retained_contract_fingerprints = _required_fingerprint_map(data, "retained_contract_fingerprints")
    progress_fingerprint = _required_hash(data, "progress_fingerprint")
    source_assembly_base_commit = _required_object_id(data, "source_assembly_base_commit")
    source_assembled_commit = _required_object_id(data, "source_assembled_commit")
    created_at = _required_string(data, "created_at")
    amended_plan_fingerprint = _required_hash(data, "amended_plan_fingerprint")
    rewired_unit_ids = _optional_id_list(data.get("rewired_unit_ids"), "rewired_unit_ids")
    amend_reason = _optional_stable_code(data.get("amend_reason"), "amend_reason")
    budget_exceeded = _budget_exceeded_from_mapping(data.get("budget_exceeded"))
    raw_units = data.get("replacement_units")
    if not isinstance(raw_units, list) or len(raw_units) < 2:
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Proposal must contain replacement units.")
    units = [_proposal_unit_from_dict(item) for item in raw_units]
    if len({unit.id for unit in units}) != len(units):
        raise DeliveryAmendmentError("delivery_amend.replacement_duplicate", "Replacement unit ids must be unique.")
    proposal = DeliveryAmendmentProposal(
        proposal_id=proposal_id,
        plan_id=plan_id,
        target_unit_id=target_unit_id,
        source_plan_path=source_plan_path,
        source_plan_fingerprint=source_fingerprint,
        source_plan_blob_id=source_plan_blob_id,
        target_task_fingerprint=target_task_fingerprint,
        retained_contract_fingerprints=retained_contract_fingerprints,
        progress_fingerprint=progress_fingerprint,
        source_assembly_base_commit=source_assembly_base_commit,
        source_assembled_commit=source_assembled_commit,
        created_at=created_at,
        replacement_units=units,
        amended_plan_fingerprint=amended_plan_fingerprint,
        rewired_unit_ids=rewired_unit_ids,
        amend_reason=amend_reason,
        budget_exceeded=budget_exceeded,
    )
    content_payload = proposal.to_dict()
    content_payload.pop("schema_version")
    content_payload.pop("proposal_id")
    if _proposal_content_id(content_payload) != proposal_id:
        raise DeliveryAmendmentError(
            "delivery_amend.proposal_fingerprint_mismatch",
            "Proposal content does not match its proposal id.",
        )
    return proposal


def _preflight_applied_amendment(
    plan: DeliveryPlan,
    plan_path: Path,
    root: Path,
    proposal: DeliveryAmendmentProposal,
    *,
    project_config: dict | None,
    dry_run: bool,
    base: DeliveryAmendmentApplyResult,
) -> _AmendmentPreflight | None:
    status = get_delivery_status(plan_path, project_root=root)
    if not status.valid or status.plan is None:
        issue = (
            status.errors[0]
            if status.errors
            else DeliveryPlanIssue(
                "error", "delivery_amend.plan_invalid", "Applied delivery amendment plan is invalid."
            )
        )
        return _blocked_applied_preflight(base, proposal, issue.code, issue.message, issue.path)
    if _plan_fingerprint(plan_path) != proposal.amended_plan_fingerprint:
        return None
    target = next((unit for unit in plan.units if unit.id == proposal.target_unit_id), None)
    if target is None or target.superseded_by != proposal.replacement_ids:
        return None
    private_artifact_roots = _configured_private_artifact_roots(root, project_config)
    source_context = DeliveryAmendmentTarget(
        plan_path=str(plan_path),
        project_root=str(root),
        private_artifact_roots=private_artifact_roots,
        plan=plan,
        status=status,
        target=target,
        downstream_unit_ids=list(proposal.rewired_unit_ids),
        assembly_base_commit=proposal.source_assembly_base_commit,
        assembled_commit=proposal.source_assembled_commit,
        recorded_assembled_commit=status.assembled_commit,
    )
    if _target_task_fingerprint(source_context) != proposal.target_task_fingerprint:
        return _blocked_applied_preflight(
            base,
            proposal,
            "delivery_amend.target_task_stale",
            "Target unit task changed after the proposal was prepared.",
        )
    try:
        _assert_retained_contract_fingerprints_current(source_context, proposal)
    except DeliveryAmendmentError as exc:
        return _blocked_applied_preflight(base, proposal, exc.issue.code, exc.issue.message, exc.issue.path)
    for unit in proposal.replacement_units:
        path = root / unit.task_path
        try:
            if path.is_symlink() or path.read_bytes() != unit.task_markdown.encode("utf-8"):
                return _blocked_applied_preflight(
                    base,
                    proposal,
                    "delivery_amend.replacement_task_stale",
                    "An applied amendment replacement task is missing or changed.",
                )
        except OSError:
            return _blocked_applied_preflight(
                base,
                proposal,
                "delivery_amend.replacement_task_stale",
                "An applied amendment replacement task is missing or changed.",
            )
    readiness_errors = _replacement_contract_readiness_errors(
        proposal.replacement_units,
        project_config=project_config,
    )
    if readiness_errors:
        return _AmendmentPreflight(
            result=_replace_errors(base, readiness_errors),
            proposal=proposal,
            artifacts_applied=True,
        )
    try:
        artifacts = _proposal_assembly_artifacts(
            proposal,
            plan_path.read_bytes(),
            project_root=root,
            plan=status.plan,
            private_artifact_roots=private_artifact_roots,
        )
    except DeliveryAmendmentError as exc:
        return _blocked_applied_preflight(base, proposal, exc.issue.code, exc.issue.message, exc.issue.path)
    if status.assembly_status == "failed":
        try:
            _raise_unresolved_assembly_failure(status.assembly_error_code)
        except DeliveryAmendmentError as exc:
            return _blocked_applied_preflight(base, proposal, exc.issue.code, exc.issue.message)
    try:
        recovery = _inspect_applied_assembly_recovery(root, plan, proposal, artifacts, status)
    except DeliveryAmendmentError as exc:
        return _blocked_applied_preflight(base, proposal, exc.issue.code, exc.issue.message, exc.issue.path)
    if recovery.mutation_required:
        progress_path = delivery_progress_path(root, proposal.plan_id)
        if not progress_path.exists() and proposal.progress_fingerprint != hashlib.sha256(b"null").hexdigest():
            return _blocked_applied_preflight(
                base,
                proposal,
                "delivery_amend.progress_invalid",
                "Delivery progress is missing during amendment assembly recovery.",
            )
        if _progress_fingerprint(source_context) != proposal.progress_fingerprint:
            return _blocked_applied_preflight(
                base,
                proposal,
                "delivery_amend.progress_stale",
                "Delivery progress changed before amendment assembly recovery.",
            )
    assembly = preview_delivery_assembly(
        root,
        branch=plan.final_branch,
        base_commit=proposal.source_assembly_base_commit,
        expected_commit=recovery.expected_commit,
        units=[],
        allow_checked_out=not recovery.mutation_required,
    )
    if not assembly.success or assembly.assembled_commit is None:
        issue = assembly.error or DeliveryPlanIssue(
            "error", "delivery.assembly_failed", "Applied delivery amendment assembly is invalid."
        )
        return _blocked_applied_preflight(base, proposal, issue.code, issue.message, issue.path)
    assembly_base = assembly.base_commit
    assembled_commit = assembly.assembled_commit
    amendment_commit = recovery.amendment_commit
    branch_commit = recovery.current_commit
    effective_branch_commit = branch_commit or assembled_commit
    if assembled_commit != effective_branch_commit:
        return _blocked_applied_preflight(
            base,
            proposal,
            "delivery_amend.assembly_artifact_missing",
            "The applied amendment artifact commit is missing from the delivery assembly.",
        )
    context = replace(
        source_context,
        assembly_base_commit=assembly_base,
        assembled_commit=effective_branch_commit,
    )
    plan_data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan_data, dict):
        return None
    result = replace(
        base,
        rewired_unit_ids=list(proposal.rewired_unit_ids),
        ready=True,
        applied=False,
        errors=[],
        message=(
            "Delivery plan amendment is already applied; dry-run found no pending changes."
            if dry_run and amendment_commit is not None
            else "Delivery plan amendment artifacts are ready for assembly integration."
            if dry_run
            else "Delivery plan amendment is already applied and ready for audit reconciliation."
        ),
    )
    return _AmendmentPreflight(
        result=result,
        proposal=proposal,
        target=context,
        plan_data=plan_data,
        artifacts_applied=True,
        amendment_commit=amendment_commit,
    )


def _inspect_applied_assembly_recovery(
    root: Path,
    plan: DeliveryPlan,
    proposal: DeliveryAmendmentProposal,
    artifacts: list[DeliveryAssemblyArtifact],
    status: DeliveryStatusResult,
) -> _AppliedAssemblyRecovery:
    source_commit = proposal.source_assembled_commit
    try:
        current_commit = delivery_branch_commit(root, plan.final_branch)
    except OSError:
        raise DeliveryAmendmentError(
            "delivery.assembly_branch_diverged",
            "Delivery assembly branch could not be verified during amendment recovery.",
        ) from None
    amendment_commit, lookup_commit = find_delivery_artifact_commit(
        root,
        branch=plan.final_branch,
        parent_commit=source_commit,
        proposal_id=proposal.proposal_id,
        artifacts=artifacts,
    )
    if lookup_commit != current_commit:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_artifact_missing",
            "Delivery assembly branch changed while amendment recovery was inspected.",
        )
    if amendment_commit is not None:
        return _AppliedAssemblyRecovery(
            amendment_commit=amendment_commit,
            current_commit=current_commit,
            expected_commit=status.assembled_commit or source_commit,
        )
    if current_commit is None:
        return _AppliedAssemblyRecovery(None, None, status.assembled_commit)
    try:
        branch_is_source_or_behind = current_commit == source_commit or delivery_commit_is_ancestor(
            root, current_commit, source_commit
        )
        source_is_behind_branch = delivery_commit_is_ancestor(root, source_commit, current_commit)
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.git_check_failed",
            "Delivery assembly could not be verified with Git.",
        ) from None
    if branch_is_source_or_behind:
        return _AppliedAssemblyRecovery(
            amendment_commit=None,
            current_commit=current_commit,
            expected_commit=status.assembled_commit or current_commit,
        )
    if source_is_behind_branch:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_artifact_missing",
            "The delivery assembly advanced without the exact applied amendment artifacts.",
        )
    raise DeliveryAmendmentError(
        "delivery.assembly_branch_diverged",
        "Delivery assembly branch diverged from the amendment source assembly.",
    )


def _blocked_applied_preflight(
    base: DeliveryAmendmentApplyResult,
    proposal: DeliveryAmendmentProposal,
    code: str,
    message: str,
    path: str | None = None,
) -> _AmendmentPreflight:
    return _AmendmentPreflight(
        result=_blocked_result(base, DeliveryPlanIssue("error", code, message, path)),
        proposal=proposal,
        artifacts_applied=True,
    )


def _normalize_replacements(
    context: DeliveryAmendmentTarget,
    drafts: list[DeliveryAuthoringUnitDraft],
    *,
    root: Path,
    plan_path: Path,
) -> list[DeliveryAmendmentProposalUnit]:
    existing_ids = {unit.id.casefold() for unit in context.plan.units}
    progress_ids = _progress_unit_ids(context)
    existing_paths = {unit.task_path.casefold() for unit in context.plan.units}
    replacement_ids = {unit.id for unit in drafts}
    replacement_ids_casefold: set[str] = set()
    result: list[DeliveryAmendmentProposalUnit] = []
    for draft in drafts:
        if not isinstance(draft.id, str) or not _ID_RE.fullmatch(draft.id):
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_id_invalid",
                "Replacement unit id must be a path-safe stable identifier.",
            )
        normalized_id = draft.id.casefold()
        if normalized_id in replacement_ids_casefold:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_duplicate",
                "Replacement unit ids must be unique across case-insensitive filesystems.",
            )
        if normalized_id in existing_ids:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_id_conflict", f"Replacement unit id already exists: {draft.id}"
            )
        if normalized_id in progress_ids:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_progress_conflict",
                f"Replacement unit id already exists in parent progress: {draft.id}",
            )
        internal_dependencies = [dependency for dependency in draft.depends_on if dependency in replacement_ids]
        if internal_dependencies != draft.depends_on:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_dependency_external",
                "Replacement authoring dependencies may reference replacement units only.",
            )
        depends_on = list(draft.depends_on) if draft.depends_on else list(context.target.depends_on)
        task_path = _replacement_task_path(plan_path, root, draft.id)
        if task_path.casefold() in existing_paths:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict", f"Replacement task path already exists: {task_path}"
            )
        replacement_ids_casefold.add(normalized_id)
        existing_paths.add(task_path.casefold())
        task_file = root / task_path
        if task_file.exists() or task_file.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict",
                f"Replacement task path already exists: {task_path}",
            )
        _validate_new_task_target(
            task_file,
            root,
            private_artifact_roots=context.private_artifact_roots,
        )
        result.append(
            DeliveryAmendmentProposalUnit(
                id=draft.id,
                title=draft.title,
                task_path=task_path,
                depends_on=depends_on,
                task_markdown=draft.task_markdown.rstrip() + "\n",
                stream=draft.stream or context.target.stream,
                component=draft.component or context.target.component,
                phase=draft.phase or context.target.phase,
                kind=draft.kind or context.target.kind,
                platform=draft.platform or context.target.platform,
                repo_id=context.target.repo_id,
                scope_paths=list(draft.scope_paths),
                estimated_size=draft.estimated_size,
                risk_tags=list(draft.risk_tags),
                budget=draft.budget.to_dict() if draft.budget else None,
            )
        )
    return result


def _validate_proposal_against_target(
    proposal: DeliveryAmendmentProposal,
    context: DeliveryAmendmentTarget,
) -> None:
    existing_ids = {unit.id.casefold() for unit in context.plan.units}
    progress_ids = _progress_unit_ids(context)
    existing_paths = {unit.task_path.casefold() for unit in context.plan.units}
    replacement_ids = set(proposal.replacement_ids)
    replacement_ids_casefold: set[str] = set()
    replacement_paths_casefold: set[str] = set()
    root = Path(context.project_root)
    plan_path = Path(context.plan_path)
    for unit in proposal.replacement_units:
        normalized_id = unit.id.casefold()
        if normalized_id in replacement_ids_casefold:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_duplicate",
                "Replacement unit ids must be unique across case-insensitive filesystems.",
            )
        if normalized_id in existing_ids:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_id_conflict", f"Replacement unit id already exists: {unit.id}"
            )
        if normalized_id in progress_ids:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_progress_conflict",
                f"Replacement unit id already exists in parent progress: {unit.id}",
            )
        internal = [dependency for dependency in unit.depends_on if dependency in replacement_ids]
        external = [dependency for dependency in unit.depends_on if dependency not in replacement_ids]
        if internal and external:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_root_dependencies_invalid",
                "Only replacement root units may inherit target dependencies.",
            )
        if not internal and external != context.target.depends_on:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_root_dependencies_invalid",
                "Replacement root dependencies must exactly match the target dependencies.",
            )
        expected_path = _replacement_task_path(plan_path, root, unit.id)
        normalized_path = unit.task_path.casefold()
        if (
            unit.task_path != expected_path
            or normalized_path in existing_paths
            or normalized_path in replacement_paths_casefold
        ):
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_unsafe",
                "Replacement task path does not match its deterministic location.",
            )
        replacement_ids_casefold.add(normalized_id)
        replacement_paths_casefold.add(normalized_path)
        task_path = root / unit.task_path
        _validate_new_task_target(
            task_path,
            root,
            private_artifact_roots=context.private_artifact_roots,
        )
        if task_path.exists() or task_path.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict",
                f"Replacement task path already exists: {unit.task_path}",
            )


def _amended_plan_data(
    plan_path: Path,
    context: DeliveryAmendmentTarget,
    proposal: DeliveryAmendmentProposal,
) -> tuple[dict[str, Any], list[str]]:
    data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        raise DeliveryAmendmentError("delivery_amend.plan_invalid", "Delivery plan YAML is malformed.")
    amended = copy.deepcopy(data)
    units = amended["units"]
    by_id = {item.get("id"): item for item in units if isinstance(item, dict)}
    target_entry = by_id.get(proposal.target_unit_id)
    if not isinstance(target_entry, dict):
        raise DeliveryAmendmentError("delivery_amend.target_unknown", "Target unit is missing from the delivery plan.")

    replacement_ids = proposal.replacement_ids
    leaves = _replacement_leaves(proposal.replacement_units)
    target_entry["superseded_by"] = replacement_ids
    if proposal.amend_reason:
        target_entry["amend_reason"] = proposal.amend_reason
    if proposal.budget_exceeded:
        target_entry["budget_exceeded"] = proposal.budget_exceeded.to_dict()

    target_index = units.index(target_entry)
    units[target_index + 1 : target_index + 1] = [
        unit.plan_entry(target_unit_id=proposal.target_unit_id) for unit in proposal.replacement_units
    ]
    constraints = amended.get("constraints")
    if constraints is not None:
        if not isinstance(constraints, list):
            raise DeliveryAmendmentError("delivery_amend.plan_invalid", "Delivery plan constraints are malformed.")
        for constraint in constraints:
            if not isinstance(constraint, dict) or not isinstance(constraint.get("unit_ids"), list):
                raise DeliveryAmendmentError(
                    "delivery_amend.plan_invalid",
                    "Delivery plan constraints are malformed.",
                )
            if proposal.target_unit_id not in constraint["unit_ids"]:
                continue
            reassigned_unit_ids: list[str] = []
            for unit_id in constraint["unit_ids"]:
                inherited_ids = replacement_ids if unit_id == proposal.target_unit_id else [unit_id]
                for inherited_id in inherited_ids:
                    if inherited_id not in reassigned_unit_ids:
                        reassigned_unit_ids.append(inherited_id)
            constraint["unit_ids"] = reassigned_unit_ids
    rewired: list[str] = []
    for item in units:
        if (
            not isinstance(item, dict)
            or item.get("id") in replacement_ids
            or item is target_entry
            or item.get("superseded_by")
        ):
            continue
        depends_on = item.get("depends_on")
        if not isinstance(depends_on, list) or proposal.target_unit_id not in depends_on:
            continue
        replacement_dependencies: list[str] = []
        for dependency in depends_on:
            if dependency == proposal.target_unit_id:
                replacement_dependencies.extend(leaves)
            elif dependency not in replacement_dependencies:
                replacement_dependencies.append(dependency)
        item["depends_on"] = list(dict.fromkeys(replacement_dependencies))
        rewired.append(str(item.get("id")))

    _assert_completed_units_unchanged(data, amended, context)
    return amended, rewired


def _write_amended_artifacts(
    plan_path: Path,
    root: Path,
    plan_data: dict[str, Any],
    proposal: DeliveryAmendmentProposal,
    *,
    source_context: DeliveryAmendmentTarget,
    events_path: Path,
    rewired_ids: list[str],
    timestamp: str,
) -> None:
    plan_content = yaml.safe_dump(plan_data, sort_keys=False, allow_unicode=True).encode("utf-8")
    task_targets: list[tuple[Path, bytes]] = []
    for unit in proposal.replacement_units:
        target = root / unit.task_path
        _validate_new_task_target(target, root)
        if target.exists() or target.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict", f"Replacement task path already exists: {unit.task_path}"
            )
        task_targets.append((target, unit.task_markdown.encode("utf-8")))
    plan_backup = _backup(plan_path)
    event_log_existed = events_path.exists()
    event_log_size = events_path.stat().st_size if event_log_existed else 0
    created_dirs: list[Path] = []
    published_tasks: list[_PublishedArtifact] = []
    plan_published = False
    progress_path = delivery_progress_path(root, proposal.plan_id)
    progress_backup = _backup(progress_path)
    assembly_commit: str | None = None
    previous_branch_commit: str | None = None
    try:
        source_mode = _default_source_file_mode()
        for path, content in task_targets:
            created_dirs.extend(_ensure_parent(path.parent, root))
            published_tasks.append(_atomic_write_new(path, content, mode=source_mode, root=root))
        _assert_proposal_source_fingerprints_current(source_context, proposal)
        _atomic_replace_if_unchanged(
            plan_path,
            plan_content,
            expected_fingerprint=proposal.source_plan_fingerprint,
        )
        plan_published = True
        if plan_backup.mode is not None:
            os.chmod(plan_path, plan_backup.mode)
        validation = check_delivery_plan_file(plan_path, project_root=root)
        if not validation.valid:
            raise DeliveryAmendmentError(
                "delivery_amend.final_validation_failed", "Written delivery plan failed final validation."
            )
        if _plan_fingerprint(plan_path) != hashlib.sha256(plan_content).hexdigest():
            raise DeliveryAmendmentError(
                "delivery_amend.plan_stale",
                "Delivery plan changed while amendment artifacts were being written.",
            )
        _assert_proposal_non_plan_fingerprints_current(source_context, proposal)
        _assert_published_artifacts_current(published_tasks, root)
        assembly = assemble_delivery_artifacts(
            root,
            plan_id=proposal.plan_id,
            proposal_id=proposal.proposal_id,
            branch=source_context.plan.final_branch,
            parent_commit=source_context.assembled_commit,
            artifacts=_proposal_assembly_artifacts(
                proposal,
                plan_content,
                project_root=root,
                plan=validation.plan,
                private_artifact_roots=source_context.private_artifact_roots,
                enforce_parent_state=True,
            ),
            created_at=proposal.created_at,
        )
        if not assembly.success or assembly.assembled_commit is None:
            issue = assembly.error or DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_git_failed",
                "Git could not integrate delivery amendment artifacts.",
            )
            raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
        assembly_commit = assembly.assembled_commit
        previous_branch_commit = assembly.previous_commit
        try:
            current_branch_commit = delivery_branch_commit(root, source_context.plan.final_branch)
        except OSError:
            current_branch_commit = None
        if current_branch_commit != assembly_commit:
            raise DeliveryAmendmentError(
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch changed while amendment artifacts were integrated.",
            )
        if _plan_fingerprint(plan_path) != hashlib.sha256(plan_content).hexdigest():
            raise DeliveryAmendmentError(
                "delivery_amend.plan_stale",
                "Delivery plan changed while amendment artifacts were being integrated.",
            )
        _assert_proposal_non_plan_fingerprints_current(source_context, proposal)
        _assert_published_artifacts_current(published_tasks, root)
        _write_amendment_assembly_progress(
            progress_path,
            source_context,
            assembled_commit=assembly_commit,
            timestamp=timestamp,
        )
        _append_success_events(
            events_path,
            proposal,
            rewired_ids,
            branch=source_context.plan.final_branch,
            commit=assembly_commit,
            timestamp=timestamp,
        )
    except BaseException:
        ref_rollback_failed = assembly_commit is not None and not rollback_delivery_artifacts(
            root,
            branch=source_context.plan.final_branch,
            assembled_commit=assembly_commit,
            previous_commit=previous_branch_commit,
        )
        progress_rollback_failed = False
        try:
            _restore_backup(progress_backup)
        except Exception:
            progress_rollback_failed = True
        rollback_failed = _rollback_amended_artifacts(
            plan_backup,
            project_root=root,
            plan_published=plan_published,
            amended_plan_fingerprint=hashlib.sha256(plan_content).hexdigest(),
            published_tasks=published_tasks,
            created_dirs=created_dirs,
        )
        try:
            _restore_appended_events(events_path, existed=event_log_existed, size=event_log_size)
        except Exception:
            rollback_failed = True
        if ref_rollback_failed or rollback_failed or progress_rollback_failed:
            raise DeliveryAmendmentError(
                "delivery_amend.rollback_failed",
                "Delivery amendment failed and its assembly ref, artifacts, or events could not be fully restored; "
                "inspect the plan state.",
            ) from None
        raise


def _append_success_events(
    path: Path,
    proposal: DeliveryAmendmentProposal,
    rewired_ids: list[str],
    *,
    branch: str,
    commit: str,
    timestamp: str,
) -> None:
    append_delivery_progress_events(
        path,
        _success_events(proposal, rewired_ids, branch=branch, commit=commit, timestamp=timestamp),
    )


def _success_events(
    proposal: DeliveryAmendmentProposal,
    rewired_ids: list[str],
    *,
    branch: str,
    commit: str,
    timestamp: str,
) -> list[DeliveryProgressEvent]:
    events = [
        _amendment_event(proposal, "unit.split_recommended", timestamp=timestamp),
        _amendment_event(proposal, "unit.superseded", timestamp=timestamp),
    ]
    events.extend(
        [
            DeliveryProgressEvent(
                plan_id=proposal.plan_id,
                event_type="unit.replacement_added",
                timestamp=timestamp,
                unit_id=unit_id,
                proposal_id=proposal.proposal_id,
                amend_reason=proposal.amend_reason,
                budget_exceeded=proposal.budget_exceeded,
            )
            for unit_id in proposal.replacement_ids
        ]
    )
    events.append(
        DeliveryProgressEvent(
            plan_id=proposal.plan_id,
            event_type="plan.amended",
            timestamp=timestamp,
            unit_id=proposal.target_unit_id,
            proposal_id=proposal.proposal_id,
            replacement_ids=proposal.replacement_ids,
            rewired_unit_ids=rewired_ids,
            amend_reason=proposal.amend_reason,
            budget_exceeded=proposal.budget_exceeded,
            branch=branch,
            commit=commit,
        )
    )
    return events


def _reconcile_applied_amendment(
    root: Path,
    context: DeliveryAmendmentTarget,
    proposal: DeliveryAmendmentProposal,
    *,
    amendment_commit: str | None,
    events_path: Path,
    rewired_ids: list[str],
    timestamp: str,
) -> None:
    assembly_head = context.assembled_commit
    progress_path = delivery_progress_path(root, proposal.plan_id)
    try:
        progress_backup = _backup(progress_path)
        event_log_backup = _backup(events_path)
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.progress_invalid",
            "Delivery amendment recovery state could not be backed up.",
        ) from None
    recovery_commit: str | None = None
    previous_branch_commit: str | None = None
    try:
        if amendment_commit is None:
            plan_path = Path(context.plan_path)
            assembly = assemble_delivery_artifacts(
                root,
                plan_id=proposal.plan_id,
                proposal_id=proposal.proposal_id,
                branch=context.plan.final_branch,
                parent_commit=proposal.source_assembled_commit,
                artifacts=_proposal_assembly_artifacts(
                    proposal,
                    plan_path.read_bytes(),
                    project_root=root,
                    plan=context.plan,
                    private_artifact_roots=context.private_artifact_roots,
                    enforce_parent_state=True,
                ),
                created_at=proposal.created_at,
            )
            if not assembly.success or assembly.assembled_commit is None:
                issue = assembly.error or DeliveryPlanIssue(
                    "error",
                    "delivery.assembly_artifact_git_failed",
                    "Git could not integrate recovered delivery amendment artifacts.",
                )
                raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
            amendment_commit = assembly.assembled_commit
            recovery_commit = assembly.assembled_commit
            previous_branch_commit = assembly.previous_commit
            try:
                current_branch_commit = delivery_branch_commit(root, context.plan.final_branch)
            except OSError:
                current_branch_commit = None
            if current_branch_commit != amendment_commit:
                raise DeliveryAmendmentError(
                    "delivery.assembly_branch_diverged",
                    "Delivery assembly branch changed while recovered amendment artifacts were integrated.",
                )
            assembly_head = amendment_commit

        if progress_path.exists():
            progress, errors = read_delivery_progress(progress_path, plan_id=proposal.plan_id)
        else:
            progress, errors = None, []
        if errors or (progress is None and not _missing_progress_recovery_is_current(events_path, proposal)):
            raise DeliveryAmendmentError(
                "delivery_amend.progress_invalid",
                "Delivery progress is missing or invalid during amendment recovery.",
            )
        if progress is None:
            progress = DeliveryProgress(schema_version=1, plan_id=proposal.plan_id)
        if not (
            progress.assembly_base_commit == context.assembly_base_commit
            and progress.assembled_commit == assembly_head
            and progress.assembly_status == "ready"
        ):
            _write_amendment_assembly_progress(
                progress_path,
                context,
                assembled_commit=assembly_head,
                timestamp=timestamp,
            )
        expected = _success_events(
            proposal,
            rewired_ids,
            branch=context.plan.final_branch,
            commit=amendment_commit,
            timestamp=timestamp,
        )
        existing_keys, repair = _amendment_event_keys(events_path, proposal.proposal_id)
        if repair is not None:
            _repair_amendment_event_suffix(events_path, *repair)
        missing = [event for event in expected if (event.event_type, event.unit_id) not in existing_keys]
        try:
            append_delivery_progress_events(events_path, missing)
        except OSError:
            raise DeliveryAmendmentError(
                "delivery_amend.event_write_failed",
                "Delivery amendment is applied but its audit events could not be reconciled.",
            ) from None
    except BaseException as exc:
        rollback_failed = recovery_commit is not None and not rollback_delivery_artifacts(
            root,
            branch=context.plan.final_branch,
            assembled_commit=recovery_commit,
            previous_commit=previous_branch_commit,
        )
        try:
            _restore_backup(progress_backup)
            _restore_backup(event_log_backup)
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise DeliveryAmendmentError(
                "delivery_amend.rollback_failed",
                "Delivery amendment recovery failed and its assembly ref, progress, or events could not be restored; "
                "inspect the plan state.",
            ) from None
        if isinstance(exc, DeliveryAmendmentError):
            raise
        if isinstance(exc, Exception):
            raise DeliveryAmendmentError(
                "delivery_amend.write_failed",
                "Delivery amendment recovery failed while reading or writing local artifacts; previous state was "
                "restored.",
            ) from None
        raise


def _amendment_event_keys(
    path: Path,
    proposal_id: str,
) -> tuple[set[tuple[str, str | None]], tuple[int | None, bool] | None]:
    if not path.exists():
        return set(), None
    keys: set[tuple[str, str | None]] = set()
    try:
        content = path.read_bytes()
        lines = content.splitlines(keepends=True)
        offset = 0
        for index, line in enumerate(lines):
            complete = line.endswith(b"\n")
            payload = line[:-1].removesuffix(b"\r") if complete else line
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if index == len(lines) - 1 and not complete:
                    return keys, (offset, False)
                raise
            if not isinstance(data, dict) or data.get("proposal_id") != proposal_id:
                offset += len(line)
                continue
            event_type = data.get("event_type")
            unit_id = data.get("unit_id")
            if isinstance(event_type, str) and (unit_id is None or isinstance(unit_id, str)):
                keys.add((event_type, unit_id))
            offset += len(line)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DeliveryAmendmentError(
            "delivery_amend.events_invalid",
            "Delivery amendment event history is unavailable or malformed.",
        ) from None
    return keys, ((None, True) if content and not content.endswith(b"\n") else None)


def _repair_amendment_event_suffix(path: Path, truncate_to: int | None, append_newline: bool) -> None:
    try:
        with path.open("r+b") as handle:
            if truncate_to is not None:
                handle.truncate(truncate_to)
            if append_newline:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.event_write_failed",
            "Delivery amendment is applied but its truncated audit event could not be repaired.",
        ) from None


def _missing_progress_recovery_is_current(
    path: Path,
    proposal: DeliveryAmendmentProposal,
) -> bool:
    if proposal.progress_fingerprint != hashlib.sha256(b"null").hexdigest() or not path.is_file():
        return False
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return False
        event = json.loads(lines[-1])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(event, dict)
        and event.get("plan_id") == proposal.plan_id
        and event.get("event_type") == "plan.amend_started"
        and event.get("proposal_id") == proposal.proposal_id
    )


def _write_amendment_assembly_progress(
    path: Path,
    context: DeliveryAmendmentTarget,
    *,
    assembled_commit: str,
    timestamp: str,
) -> None:
    if path.exists():
        progress, errors = read_delivery_progress(path, plan_id=context.plan.plan_id)
        if progress is None or errors:
            raise DeliveryAmendmentError(
                "delivery_amend.progress_invalid",
                "Delivery progress is invalid.",
            )
        if progress.assembly_status == "failed":
            _raise_unresolved_assembly_failure(progress.assembly_error_code)
    else:
        progress = DeliveryProgress(schema_version=1, plan_id=context.plan.plan_id)
    updated = mark_delivery_assembly(
        progress,
        base_commit=context.assembly_base_commit,
        assembled_commit=assembled_commit,
        status="ready",
        timestamp=timestamp,
    )
    write_delivery_progress(path, updated)


def _proposal_assembly_artifacts(
    proposal: DeliveryAmendmentProposal,
    plan_content: bytes,
    *,
    project_root: Path,
    plan: DeliveryPlan | None,
    private_artifact_roots: tuple[Path, ...],
    enforce_parent_state: bool = False,
) -> list[DeliveryAssemblyArtifact]:
    if plan is None:
        raise DeliveryAmendmentError(
            "delivery_amend.plan_invalid",
            "Amended delivery plan is unavailable for assembly publication.",
        )
    replacement_content = {
        _canonical_assembly_task_destination(
            project_root,
            unit.task_path,
            private_artifact_roots=private_artifact_roots,
        ): unit.task_markdown.encode("utf-8")
        for unit in proposal.replacement_units
    }
    artifacts_by_path = {
        proposal.source_plan_path: DeliveryAssemblyArtifact(
            proposal.source_plan_path,
            plan_content,
            expected_object_id=proposal.source_plan_blob_id if enforce_parent_state else None,
        )
    }
    for unit in plan.units:
        task_path = _canonical_assembly_task_destination(
            project_root,
            unit.task_path,
            private_artifact_roots=private_artifact_roots,
        )
        content = replacement_content.get(task_path)
        replacement = content is not None
        if content is None:
            task_path = _canonical_assembly_task_path(
                project_root,
                unit.task_path,
                private_artifact_roots=private_artifact_roots,
            )
            content = _read_assembly_contract(
                project_root,
                unit.task_path,
                private_artifact_roots=private_artifact_roots,
            )
        artifact = DeliveryAssemblyArtifact(
            task_path,
            content,
            expected_content=(content if enforce_parent_state and not replacement else None),
            must_not_exist=enforce_parent_state and replacement,
        )
        existing = artifacts_by_path.get(task_path)
        if existing is not None:
            if existing.content != artifact.content:
                raise DeliveryAmendmentError(
                    "delivery_amend.contract_path_ambiguous",
                    "Delivery plan references one contract path with conflicting content.",
                    task_path,
                )
            continue
        artifacts_by_path[task_path] = artifact
    return list(artifacts_by_path.values())


def _validate_amendment_source_contracts(
    plan: DeliveryPlan,
    root: Path,
    *,
    private_artifact_roots: tuple[Path, ...],
) -> None:
    for unit in plan.units:
        path = root / unit.task_path
        try:
            _validate_amendment_target_task_source(
                unit.task_path,
                root,
                private_artifact_roots=private_artifact_roots,
            )
            _validate_new_task_target(path, root, private_artifact_roots=private_artifact_roots)
            if not path.is_file():
                raise OSError("contract is not a regular file")
        except (DeliveryAmendmentError, OSError, RuntimeError, ValueError):
            raise DeliveryAmendmentError(
                "delivery_amend.source_contract_unsafe",
                "Every contract referenced by the delivery plan must be a stable project-local file.",
                unit.task_path,
            ) from None


def _canonical_assembly_task_destination(
    root: Path,
    task_path: str,
    *,
    private_artifact_roots: tuple[Path, ...],
) -> str:
    _validate_amendment_target_task_source(
        task_path,
        root,
        private_artifact_roots=private_artifact_roots,
    )
    target = root / task_path
    _validate_new_task_target(target, root, private_artifact_roots=private_artifact_roots)
    try:
        return target.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.source_contract_unsafe",
            "Every contract referenced by the amended delivery plan must be a stable project-local file.",
            task_path,
        ) from None


def _canonical_assembly_task_path(
    root: Path,
    task_path: str,
    *,
    private_artifact_roots: tuple[Path, ...],
) -> str:
    _validate_amendment_target_task_source(
        task_path,
        root,
        private_artifact_roots=private_artifact_roots,
    )
    try:
        return (root / task_path).resolve(strict=True).relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.source_contract_unsafe",
            "Every contract referenced by the amended delivery plan must be a stable project-local file.",
            task_path,
        ) from None


def _read_assembly_contract(
    root: Path,
    task_path: str,
    *,
    private_artifact_roots: tuple[Path, ...],
) -> bytes:
    _validate_amendment_target_task_source(
        task_path,
        root,
        private_artifact_roots=private_artifact_roots,
    )
    path = root / task_path
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("contract is not a regular file")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
        content = resolved.read_bytes()
        if path.resolve(strict=True) != resolved:
            raise OSError("contract changed while it was read")
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.source_contract_unsafe",
            "Every contract referenced by the amended delivery plan must be a stable project-local file.",
            task_path,
        ) from None
    return content


def _amendment_event(
    proposal: DeliveryAmendmentProposal,
    event_type: str,
    *,
    timestamp: str,
    failure_code: str | None = None,
) -> DeliveryProgressEvent:
    return DeliveryProgressEvent(
        plan_id=proposal.plan_id,
        event_type=event_type,
        timestamp=timestamp,
        unit_id=proposal.target_unit_id,
        proposal_id=proposal.proposal_id,
        replacement_ids=proposal.replacement_ids,
        amend_reason=proposal.amend_reason,
        budget_exceeded=proposal.budget_exceeded,
        failure_code=failure_code,
    )


def _write_new_proposal(path: Path, proposal: DeliveryAmendmentProposal) -> None:
    if path.exists() or path.is_symlink():
        raise DeliveryAmendmentError("delivery_amend.proposal_exists", "Proposal artifact already exists.")
    payload = json.dumps(proposal.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise DeliveryAmendmentError(
                "delivery_amend.proposal_exists",
                "Proposal artifact already exists.",
            ) from None
        _fsync_directory(path.parent)
    except DeliveryAmendmentError:
        raise
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.proposal_write_failed",
            "Delivery amendment proposal could not be stored atomically.",
        ) from None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _remove_published_proposal(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.proposal_cleanup_failed",
            "A stale delivery amendment proposal could not be removed.",
        ) from None
    _fsync_directory(path.parent)


def _proposal_unit_from_dict(value: Any) -> DeliveryAmendmentProposalUnit:
    if not isinstance(value, dict):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Replacement unit must be an object.")
    allowed = {
        "id",
        "title",
        "task_path",
        "depends_on",
        "task_markdown",
        "stream",
        "component",
        "phase",
        "kind",
        "platform",
        "repo_id",
        "scope_paths",
        "estimated_size",
        "risk_tags",
        "budget",
    }
    if set(value) - allowed:
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Replacement unit has unsupported fields.")
    depends_on = _string_list(value, "depends_on")
    scope_paths = _string_list(value, "scope_paths", optional=True)
    risk_tags = _string_list(value, "risk_tags", optional=True)
    budget = value.get("budget")
    if budget is not None and (
        not isinstance(budget, dict)
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in budget.values())
    ):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Replacement budget is invalid.")
    normalized_budget = None
    if budget:
        normalized_budget = {
            field_name: budget[field_name] for field_name in DELIVERY_UNIT_BUDGET_FIELDS if field_name in budget
        }
        normalized_budget.update(
            (field_name, value) for field_name, value in budget.items() if field_name not in normalized_budget
        )
    return DeliveryAmendmentProposalUnit(
        id=_required_id(value, "id"),
        title=_required_string(value, "title"),
        task_path=_required_string(value, "task_path"),
        depends_on=depends_on,
        task_markdown=_required_text(value, "task_markdown"),
        stream=_optional_string(value, "stream"),
        component=_optional_string(value, "component"),
        phase=_optional_string(value, "phase"),
        kind=_optional_string(value, "kind"),
        platform=_optional_string(value, "platform"),
        repo_id=_optional_string(value, "repo_id"),
        scope_paths=scope_paths,
        estimated_size=_optional_string(value, "estimated_size"),
        risk_tags=risk_tags,
        budget=normalized_budget,
    )


def _validate_loaded_replacement_contracts(
    plan_id: str,
    target_unit_id: str,
    units: list[DeliveryAmendmentProposalUnit],
    amend_reason: str | None,
    budget_exceeded: DeliveryBudgetExceeded | None,
    *,
    inherited_from: DeliveryPlanUnit,
    project_root: Path,
) -> None:
    replacement_ids = {unit.id for unit in units}
    authored_units = []
    for unit in units:
        internal_dependencies = [dependency for dependency in unit.depends_on if dependency in replacement_ids]
        authored_units.append(
            {
                **unit.to_dict(),
                "depends_on": internal_dependencies,
            }
        )
        authored_units[-1].pop("task_path")
        authored_units[-1].pop("repo_id", None)
        for key in ("stream", "component", "phase", "kind", "platform"):
            if authored_units[-1].get(key) == getattr(inherited_from, key):
                authored_units[-1].pop(key, None)
    payload = {
        "plan_id": plan_id,
        "target_unit_id": target_unit_id,
        "replacement_units": authored_units,
        "warnings": [],
    }
    if amend_reason:
        payload["amend_reason"] = amend_reason
    if budget_exceeded:
        payload["budget_exceeded"] = budget_exceeded.to_dict()
    try:
        parse_delivery_amendment_authoring_output(
            json.dumps(payload),
            expected_plan_id=plan_id,
            expected_target_unit_id=target_unit_id,
            project_root=project_root,
            allow_asset_manifest=True,
        )
    except DeliveryAuthoringParseError as exc:
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", exc.message) from None


def _replacement_leaves(units: list[DeliveryAmendmentProposalUnit]) -> list[str]:
    internal_ids = {unit.id for unit in units}
    depended_on = {dependency for unit in units for dependency in unit.depends_on if dependency in internal_ids}
    leaves = [unit.id for unit in units if unit.id not in depended_on]
    if not leaves:
        raise DeliveryAmendmentError(
            "delivery_amend.replacement_leaves_missing", "Replacement graph has no leaf units."
        )
    return leaves


def _assert_completed_units_unchanged(
    original: dict[str, Any],
    amended: dict[str, Any],
    context: DeliveryAmendmentTarget,
) -> None:
    done_ids = {unit.id for unit in context.status.units if unit.status == "done"}
    before = {item.get("id"): item for item in original.get("units", []) if isinstance(item, dict)}
    after = {item.get("id"): item for item in amended.get("units", []) if isinstance(item, dict)}
    for unit_id in done_ids:
        if unit_id not in after or before.get(unit_id) != after.get(unit_id):
            raise DeliveryAmendmentError(
                "delivery_amend.completed_unit_changed", f"Completed unit {unit_id} would be modified."
            )


def _completed_dependency_commit_errors(context: DeliveryAmendmentTarget) -> list[DeliveryPlanIssue]:
    by_id = {unit.id: unit for unit in context.plan.units}
    closure: set[str] = set()
    stack = list(context.target.depends_on)
    while stack:
        unit_id = stack.pop()
        if unit_id in closure:
            continue
        closure.add(unit_id)
        unit = by_id.get(unit_id)
        if unit:
            stack.extend(unit.depends_on)
    status_by_id = {unit.id: unit for unit in context.status.units}
    errors: list[DeliveryPlanIssue] = []
    for unit_id in sorted(closure):
        unit = status_by_id.get(unit_id)
        if unit is None or unit.status != "done" or not unit.commit:
            continue
        try:
            applied = delivery_commit_is_ancestor(Path(context.project_root), unit.commit, context.assembled_commit)
        except OSError:
            raise DeliveryAmendmentError(
                "delivery_amend.git_check_failed",
                "Completed dependency commits could not be verified with Git.",
            ) from None
        if not applied:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.unit_commit_unapplied",
                    f"Completed dependency {unit_id} result commit is not applied to the delivery assembly.",
                )
            )
    return errors


def _resolve_amendment_assembly(
    root: Path,
    status: DeliveryStatusResult,
) -> tuple[str, str]:
    if status.plan is None:
        raise DeliveryAmendmentError("delivery_amend.plan_invalid", "Delivery plan is invalid.")
    if status.assembly_status == "failed":
        _raise_unresolved_assembly_failure(status.assembly_error_code)
    base_commit = status.assembly_base_commit
    expected_commit = status.assembled_commit
    if base_commit is None:
        try:
            base_commit, _ = resolve_git_commit(root, "HEAD")
            branch_commit = delivery_branch_commit(root, status.plan.final_branch)
            if (
                base_commit is not None
                and branch_commit is not None
                and delivery_commit_is_ancestor(root, base_commit, branch_commit)
            ):
                expected_commit = branch_commit
        except OSError:
            raise DeliveryAmendmentError(
                "delivery_amend.git_check_failed",
                "Delivery assembly could not be verified with Git.",
            ) from None
    if base_commit is None:
        raise DeliveryAmendmentError(
            "delivery.assembly_base_missing",
            "Delivery assembly base commit is missing.",
        )
    try:
        preview = preview_delivery_assembly(
            root,
            branch=status.plan.final_branch,
            base_commit=base_commit,
            expected_commit=expected_commit,
            units=[],
            allow_checked_out=True,
        )
    except OSError:
        raise DeliveryAmendmentError(
            "delivery_amend.git_check_failed",
            "Delivery assembly could not be verified with Git.",
        ) from None
    if not preview.success or preview.assembled_commit is None:
        issue = preview.error or DeliveryPlanIssue(
            "error",
            "delivery.assembly_failed",
            "Delivery assembly could not be validated for amendment.",
        )
        raise DeliveryAmendmentError(issue.code, issue.message, issue.path)
    return preview.base_commit, preview.assembled_commit


def _raise_unresolved_assembly_failure(error_code: str | None) -> None:
    raise DeliveryAmendmentError(
        error_code or "delivery.assembly_failed",
        "Delivery assembly has an unresolved recorded failure; reconcile it before amending the plan.",
    )


def _plan_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project_relative_plan_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.plan_path_unsafe",
            "Delivery plan path must resolve inside the project root.",
        ) from None


def capture_delivery_amendment_source_snapshot(
    context: DeliveryAmendmentTarget,
) -> DeliveryAmendmentSourceSnapshot:
    root = Path(context.project_root)
    plan_path = Path(context.plan_path)
    assembly_base_commit, assembled_commit = _resolve_amendment_assembly(
        root,
        context.status,
    )
    source_plan_blob_id = delivery_artifact_content_id(
        root,
        parent_commit=assembled_commit,
        path=_project_relative_plan_path(plan_path, root),
        content=plan_path.read_bytes(),
    )
    if source_plan_blob_id is None:
        raise DeliveryAmendmentError(
            "delivery_amend.git_check_failed",
            "Delivery plan content could not be canonicalized with the delivery assembly's Git attributes.",
        )
    return DeliveryAmendmentSourceSnapshot(
        source_plan_fingerprint=_plan_fingerprint(plan_path),
        source_plan_blob_id=source_plan_blob_id,
        target_task_fingerprint=_target_task_fingerprint(context),
        retained_contract_fingerprints=_retained_contract_fingerprints(context),
        progress_fingerprint=_progress_fingerprint(context),
        assembly_base_commit=assembly_base_commit,
        assembled_commit=assembled_commit,
    )


def read_delivery_amendment_target_task(
    context: DeliveryAmendmentTarget,
    *,
    expected_fingerprint: str | None = None,
) -> tuple[Path, str]:
    root = Path(context.project_root).resolve()
    path = root / context.target.task_path
    _validate_amendment_target_task_source(
        context.target.task_path,
        root,
        private_artifact_roots=context.private_artifact_roots,
    )
    try:
        resolved = path.resolve(strict=True)
        _validate_amendment_target_task_source(
            resolved.relative_to(root).as_posix(),
            root,
            private_artifact_roots=context.private_artifact_roots,
        )
        content = resolved.read_bytes()
        resolved_after = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_unsafe",
            "Target unit task changed while it was being read.",
        ) from None
    if resolved_after != resolved:
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_unsafe",
            "Target unit task changed while it was being read.",
        )
    _validate_amendment_target_task_source(
        resolved_after.relative_to(root).as_posix(),
        root,
        private_artifact_roots=context.private_artifact_roots,
    )
    fingerprint = hashlib.sha256(content).hexdigest()
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.authoring_inputs_stale",
            "Target unit task changed before amendment authoring started; retry prepare.",
        )
    try:
        return resolved, content.decode("utf-8")
    except UnicodeDecodeError:
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_unsafe",
            "Target unit task must be valid UTF-8 text.",
        ) from None


def _assert_proposal_source_fingerprints_current(
    context: DeliveryAmendmentTarget,
    proposal: DeliveryAmendmentProposal,
) -> None:
    current = capture_delivery_amendment_source_snapshot(context)
    if current.source_plan_fingerprint != proposal.source_plan_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.plan_stale",
            "Delivery plan changed after the proposal was prepared.",
        )
    if current.target_task_fingerprint != proposal.target_task_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_stale",
            "Target unit task changed after the proposal was prepared.",
        )
    if current.retained_contract_fingerprints != proposal.retained_contract_fingerprints:
        raise DeliveryAmendmentError(
            "delivery_amend.retained_contract_stale",
            "A retained unit contract changed after the proposal was prepared.",
        )
    if current.progress_fingerprint != proposal.progress_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.progress_stale",
            "Delivery progress changed after the proposal was prepared.",
        )
    if current.assembly_base_commit != proposal.source_assembly_base_commit:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_stale",
            "Delivery assembly base changed after the proposal was prepared.",
        )
    if current.assembled_commit != proposal.source_assembled_commit:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_stale",
            "Delivery assembly changed after the proposal was prepared.",
        )


def _assert_proposal_non_plan_fingerprints_current(
    context: DeliveryAmendmentTarget,
    proposal: DeliveryAmendmentProposal,
) -> None:
    if _target_task_fingerprint(context) != proposal.target_task_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_stale",
            "Target unit task changed after the proposal was prepared.",
        )
    _assert_retained_contract_fingerprints_current(context, proposal)
    if _progress_fingerprint(context) != proposal.progress_fingerprint:
        raise DeliveryAmendmentError(
            "delivery_amend.progress_stale",
            "Delivery progress changed after the proposal was prepared.",
        )
    if context.assembly_base_commit != proposal.source_assembly_base_commit:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_stale",
            "Delivery assembly base changed after the proposal was prepared.",
        )
    if context.assembled_commit != proposal.source_assembled_commit:
        raise DeliveryAmendmentError(
            "delivery_amend.assembly_stale",
            "Delivery assembly changed after the proposal was prepared.",
        )


def _target_task_fingerprint(context: DeliveryAmendmentTarget) -> str:
    _, task_text = read_delivery_amendment_target_task(context)
    return hashlib.sha256(task_text.encode("utf-8")).hexdigest()


def _retained_contract_fingerprints(
    context: DeliveryAmendmentTarget,
    *,
    excluded_unit_ids: set[str] | None = None,
) -> dict[str, str]:
    root = Path(context.project_root)
    excluded = set(excluded_unit_ids or ())
    excluded.add(context.target.id)
    fingerprints: dict[str, str] = {}
    for unit in context.plan.units:
        if unit.id in excluded:
            continue
        task_path = _canonical_assembly_task_path(
            root,
            unit.task_path,
            private_artifact_roots=context.private_artifact_roots,
        )
        content = _read_assembly_contract(
            root,
            unit.task_path,
            private_artifact_roots=context.private_artifact_roots,
        )
        fingerprints[task_path] = hashlib.sha256(content).hexdigest()
    return dict(sorted(fingerprints.items()))


def _assert_retained_contract_fingerprints_current(
    context: DeliveryAmendmentTarget,
    proposal: DeliveryAmendmentProposal,
) -> None:
    current = _retained_contract_fingerprints(context, excluded_unit_ids=set(proposal.replacement_ids))
    if current != proposal.retained_contract_fingerprints:
        raise DeliveryAmendmentError(
            "delivery_amend.retained_contract_stale",
            "A retained unit contract changed after the proposal was prepared.",
        )


def _progress_fingerprint(context: DeliveryAmendmentTarget) -> str:
    path = delivery_progress_path(Path(context.project_root), context.plan.plan_id)
    if not path.exists():
        return hashlib.sha256(b"null").hexdigest()
    progress, errors = read_delivery_progress(path, plan_id=context.plan.plan_id)
    if progress is None or errors:
        raise DeliveryAmendmentError("delivery_amend.progress_invalid", "Delivery progress is invalid.")
    return hashlib.sha256(_canonical_json(progress.to_dict())).hexdigest()


def _progress_unit_ids(context: DeliveryAmendmentTarget) -> set[str]:
    path = delivery_progress_path(Path(context.project_root), context.plan.plan_id)
    if not path.exists():
        return set()
    progress, errors = read_delivery_progress(path, plan_id=context.plan.plan_id)
    if progress is None or errors:
        raise DeliveryAmendmentError("delivery_amend.progress_invalid", "Delivery progress is invalid.")
    return {unit.unit_id.casefold() for unit in progress.units}


def _replacement_contract_readiness_errors(
    replacement_units: list[DeliveryAmendmentProposalUnit],
    *,
    project_config: dict | None,
) -> list[DeliveryPlanIssue]:
    errors: list[DeliveryPlanIssue] = []
    for unit in replacement_units:
        try:
            result = check_contract(
                unit.task_markdown,
                source_path=unit.task_path,
                source_format="markdown",
                project_config=project_config,
                document_kind="implementation_contract",
            )
        except (OSError, RuntimeError, ValueError, KeyError):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_amend.replacement_readiness_check_failed",
                    f"Replacement unit {unit.id} task contract readiness could not be checked.",
                    unit.task_path,
                )
            )
            continue
        blocking_gap_ids = [gap.id for gap in result.gaps if gap.severity == "blocking"]
        if blocking_gap_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_amend.replacement_contract_not_ready",
                    f"Replacement unit {unit.id} task contract has blocking readiness gaps: "
                    + ", ".join(blocking_gap_ids),
                    unit.task_path,
                )
            )
    return errors


def _replacement_task_path(plan_path: Path, root: Path, unit_id: str) -> str:
    target = plan_path.parent / "units" / f"{unit_id}.md"
    try:
        return target.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.replacement_task_unsafe", "Replacement task path escapes the project root."
        ) from None


def _assert_replacement_task_paths_available(
    replacement_units: list[DeliveryAmendmentProposalUnit],
    *,
    root: Path,
    private_artifact_roots: tuple[Path, ...] = (),
) -> None:
    for unit in replacement_units:
        task_file = root / unit.task_path
        if task_file.exists() or task_file.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict",
                f"Replacement task path already exists: {unit.task_path}",
            )
        _validate_new_task_target(
            task_file,
            root,
            private_artifact_roots=private_artifact_roots,
        )


def _configured_private_artifact_roots(root: Path, project_config: dict | None) -> tuple[Path, ...]:
    tasks = project_config.get("tasks", {}) if isinstance(project_config, dict) else {}
    if not isinstance(tasks, dict):
        tasks = {}
    roots: list[Path] = []
    for key, default in (
        ("state_dir", ".sikula/state"),
        ("contract_report_dir", ".sikula/contract-reports"),
    ):
        raw = tasks.get(key, default)
        if not isinstance(raw, str):
            raw = default
        path = Path(raw)
        resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _path_is_in_private_artifact_root(path: Path, private_artifact_roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    for private_root in private_artifact_roots:
        try:
            resolved.relative_to(private_root)
            return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _validate_amendment_plan_destination(
    plan_path: Path,
    root: Path,
    *,
    private_artifact_roots: tuple[Path, ...] = (),
) -> None:
    try:
        relative_parts = plan_path.resolve().relative_to(root.resolve()).parts
    except (OSError, RuntimeError, ValueError):
        return
    normalized = tuple(part.casefold() for part in relative_parts if part not in {"", "."})
    if _path_is_in_private_artifact_root(plan_path, private_artifact_roots) or any(
        normalized[index : index + len(forbidden)] == forbidden
        for forbidden in _FORBIDDEN_PLAN_ROOTS
        for index in range(len(normalized) - len(forbidden) + 1)
    ):
        raise DeliveryAmendmentError(
            "delivery_amend.plan_destination_forbidden",
            "Delivery plan amendment destination must not be inside runtime, report, worktree, or VCS metadata.",
        )


def _validate_amendment_target_task_source(
    task_path: str,
    root: Path,
    *,
    private_artifact_roots: tuple[Path, ...] = (),
) -> None:
    try:
        relative_parts = (root / task_path).resolve(strict=False).relative_to(root.resolve()).parts
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_unsafe",
            "Target unit task must resolve inside the project root.",
        ) from None
    normalized = tuple(part.casefold() for part in relative_parts if part not in {"", "."})
    if _path_is_in_private_artifact_root(root / task_path, private_artifact_roots) or any(
        normalized[index : index + len(forbidden)] == forbidden
        for forbidden in _FORBIDDEN_PLAN_ROOTS
        for index in range(len(normalized) - len(forbidden) + 1)
    ):
        raise DeliveryAmendmentError(
            "delivery_amend.target_task_forbidden",
            "Target unit task must not read from runtime, report, worktree, or VCS metadata.",
        )


def _validate_new_task_target(
    path: Path,
    root: Path,
    *,
    private_artifact_roots: tuple[Path, ...] = (),
) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        raise DeliveryAmendmentError(
            "delivery_amend.replacement_task_unsafe", "Replacement task path escapes the project root."
        ) from None
    if _path_is_in_private_artifact_root(path, private_artifact_roots):
        raise DeliveryAmendmentError(
            "delivery_amend.replacement_task_forbidden",
            "Replacement task path must not be inside configured private artifact storage.",
        )
    current = path
    while current != root:
        if current.is_symlink():
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_symlink", "Replacement task path must not traverse symlinks."
            )
        current = current.parent


def _backup(path: Path) -> _Backup:
    if not path.exists():
        return _Backup(path, False, None, None)
    stat = path.stat()
    return _Backup(path, True, path.read_bytes(), stat.st_mode)


def _restore_backup(backup: _Backup) -> None:
    if backup.existed:
        if backup.content is None:
            raise OSError("artifact backup is missing")
        backup.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(backup.path, backup.content)
        if backup.mode is not None:
            os.chmod(backup.path, backup.mode)
        return
    if backup.path.exists() or backup.path.is_symlink():
        backup.path.unlink()


def _ensure_parent(path: Path, root: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
    except BaseException:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return created


def _atomic_write(path: Path, content: bytes) -> None:
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _atomic_write_new(path: Path, content: bytes, *, mode: int, root: Path) -> _PublishedArtifact:
    tmp: Path | None = None
    try:
        _validate_new_task_target(path, root)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        _validate_new_task_target(path, root)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_conflict",
                f"Replacement task path already exists: {path.name}",
            ) from None
        _fsync_directory(path.parent)
        return _PublishedArtifact(path=path, fingerprint=hashlib.sha256(content).hexdigest())
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _assert_published_artifacts_current(artifacts: list[_PublishedArtifact], root: Path) -> None:
    for artifact in artifacts:
        try:
            _validate_new_task_target(artifact.path, root)
            if artifact.path.is_symlink():
                raise OSError("replacement task became a symlink")
            content = artifact.path.read_bytes()
        except (OSError, RuntimeError, ValueError):
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_stale",
                "A published replacement task changed before the amendment completed.",
            ) from None
        if hashlib.sha256(content).hexdigest() != artifact.fingerprint:
            raise DeliveryAmendmentError(
                "delivery_amend.replacement_task_stale",
                "A published replacement task changed before the amendment completed.",
            )


def _atomic_replace_if_unchanged(path: Path, content: bytes, *, expected_fingerprint: str) -> None:
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, path.stat().st_mode)
        if _plan_fingerprint(path) != expected_fingerprint:
            raise DeliveryAmendmentError(
                "delivery_amend.plan_stale",
                "Delivery plan changed after the proposal was prepared.",
            )
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _default_source_file_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _rollback_amended_artifacts(
    plan_backup: _Backup,
    *,
    project_root: Path,
    plan_published: bool,
    amended_plan_fingerprint: str,
    published_tasks: list[_PublishedArtifact],
    created_dirs: list[Path],
) -> bool:
    rollback_failed = False
    plan_restored = not plan_published
    if plan_published:
        try:
            if plan_backup.content is None:
                raise OSError("delivery plan backup is missing")
            _atomic_replace_if_unchanged(
                plan_backup.path,
                plan_backup.content,
                expected_fingerprint=amended_plan_fingerprint,
            )
            if plan_backup.mode is not None:
                os.chmod(plan_backup.path, plan_backup.mode)
            plan_restored = True
        except Exception:
            rollback_failed = True
    if not plan_restored:
        return True
    if _plan_may_reference_published_tasks(plan_backup.path, project_root, published_tasks):
        return True
    for artifact in reversed(published_tasks):
        try:
            if not artifact.path.exists() and not artifact.path.is_symlink():
                continue
            if artifact.path.is_symlink():
                raise OSError("replacement task became a symlink")
            content = artifact.path.read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact.fingerprint:
                raise OSError("replacement task changed before rollback")
            artifact.path.unlink()
        except Exception:
            rollback_failed = True
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass
    return rollback_failed


def _plan_may_reference_published_tasks(
    plan_path: Path,
    project_root: Path,
    published_tasks: list[_PublishedArtifact],
) -> bool:
    if not published_tasks:
        return False
    try:
        data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        units = data.get("units") if isinstance(data, dict) else None
        if not isinstance(units, list):
            return True
        task_paths = {
            str(item.get("task_path"))
            for item in units
            if isinstance(item, dict) and isinstance(item.get("task_path"), str)
        }
        referenced_paths = {(project_root / task_path).resolve(strict=False) for task_path in task_paths}
        return any(artifact.path.resolve(strict=False) in referenced_paths for artifact in published_tasks)
    except (OSError, RuntimeError, ValueError, yaml.YAMLError):
        return True


def _restore_appended_events(path: Path, *, existed: bool, size: int) -> None:
    if not existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if path.stat().st_size < size:
        raise OSError("delivery event log is shorter than its rollback boundary")
    with path.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def _budget_exceeded_from_mapping(value: Any) -> DeliveryBudgetExceeded | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"name", "limit", "actual"}:
        raise DeliveryAmendmentError("delivery_amend.budget_invalid", "budget_exceeded metadata is invalid.")
    name = value.get("name")
    limit = value.get("limit")
    actual = value.get("actual")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not _ID_RE.fullmatch(name.strip())
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 0
        or isinstance(actual, bool)
        or not isinstance(actual, int)
        or actual < 0
    ):
        raise DeliveryAmendmentError("delivery_amend.budget_invalid", "budget_exceeded metadata is invalid.")
    return DeliveryBudgetExceeded(name=name.strip(), limit=limit, actual=actual)


def _optional_stable_code(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or not _ID_RE.fullmatch(value.strip()):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} must be a stable code.")
    return value.strip()


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} must be a string.")
    return value.strip()


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} must be text.")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    if key not in data:
        return None
    return _required_string(data, key)


def _required_id(data: dict[str, Any], key: str) -> str:
    value = _required_string(data, key)
    if not _ID_RE.fullmatch(value):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} is invalid.")
    return value


def _required_hash(data: dict[str, Any], key: str) -> str:
    value = _required_string(data, key)
    if not _HASH_RE.fullmatch(value):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} is invalid.")
    return value


def _required_fingerprint_map(data: dict[str, Any], key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict) or any(
        not isinstance(path, str)
        or not path.strip()
        or path != path.strip()
        or not isinstance(fingerprint, str)
        or not _HASH_RE.fullmatch(fingerprint)
        for path, fingerprint in value.items()
    ):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} is invalid.")
    return dict(value)


def _required_object_id(data: dict[str, Any], key: str) -> str:
    value = _required_string(data, key)
    if not _GIT_OBJECT_ID_RE.fullmatch(value):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} is invalid.")
    return value


def _optional_id_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    data = {key: value}
    result = _string_list(data, key)
    if any(not _ID_RE.fullmatch(item) for item in result):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} is invalid.")
    return result


def _string_list(data: dict[str, Any], key: str, *, optional: bool = False) -> list[str]:
    if optional and key not in data:
        return []
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} must be a string list.")
    result = [item.strip() for item in value]
    if len(result) != len(set(result)):
        raise DeliveryAmendmentError("delivery_amend.proposal_invalid", f"Proposal {key} contains duplicates.")
    return result


def _object_pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeliveryAmendmentError("delivery_amend.proposal_invalid", "Proposal contains duplicate JSON keys.")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _proposal_content_id(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()[:20]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _replace_errors(
    result: DeliveryAmendmentApplyResult,
    errors: list[DeliveryPlanIssue],
) -> DeliveryAmendmentApplyResult:
    return DeliveryAmendmentApplyResult(
        **{
            **result.__dict__,
            "errors": list(errors),
            "ready": False,
            "applied": False,
            "message": "Delivery plan amendment is blocked.",
        }
    )


def _blocked_result(
    result: DeliveryAmendmentApplyResult,
    issue: DeliveryPlanIssue,
) -> DeliveryAmendmentApplyResult:
    return _replace_errors(result, [*result.errors, issue])


def _safe_relative(path: str | None, root: str | None) -> str | None:
    if not path or not root:
        return path
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _sanitize_issue(
    issue: DeliveryPlanIssue,
    root: str | None,
    *,
    sensitive_paths: tuple[str, ...] = (),
) -> DeliveryPlanIssue:
    message = issue.message
    root_path = Path(root).resolve() if root else None
    for sensitive_path in sensitive_paths:
        try:
            candidate = Path(sensitive_path)
            if not candidate.is_absolute():
                continue
            if root_path is None:
                raise ValueError
            candidate.resolve(strict=False).relative_to(root_path)
        except (OSError, RuntimeError, ValueError):
            message = message.replace(str(candidate), "<redacted>")
    if root_path is not None:
        root_text = str(root_path)
        message = message.replace(root_text + os.sep, "").replace(root_text, ".")
    path = issue.path
    if path:
        try:
            candidate = Path(path)
            if candidate.is_absolute():
                if root_path is None:
                    raise ValueError
                path = candidate.resolve(strict=False).relative_to(root_path).as_posix()
            elif root_path is not None:
                root_text = str(root_path)
                path = path.replace(root_text + os.sep, "").replace(root_text, ".")
        except (OSError, RuntimeError, ValueError):
            path = "<redacted>"
    if message == issue.message and path == issue.path:
        return issue
    return DeliveryPlanIssue(issue.severity, issue.code, message, path)
