from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Callable

from agents.delivery_integration_review_agent import (
    DeliveryIntegrationReviewAgent,
    DeliveryIntegrationReviewAgentError,
    DeliveryIntegrationReviewResult,
    DeliveryIntegrationReviewAttempt,
)
from core.delivery_finalize import assemble_delivery_candidate
from core.llm_client import LLMConfigurationError, LLMReadOnlyViolation
from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import (
    DeliveryStatusResult,
    DeliveryProgressEvent,
    DeliveryProgressLockError,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    mark_delivery_verification,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_public_metadata import (
    REDACTED_DELIVERY_PUBLIC_METADATA,
    sanitize_delivery_public_metadata,
)
from core.delivery_reverification import SemanticRecheckRequired as _SemanticRecheckRequired
from core.delivery_verification import (
    DeliveryVerificationIdentity,
    DeliveryVerificationSnapshot,
    build_delivery_verification_snapshot,
    build_delivery_verification_identity,
    check_delivery_verification_readiness,
    delivery_verification_allowed_read_paths,
    delivery_verification_source_task_is_private,
)
from core.delivery_checkpoints import (
    reconcile_checkpoint_assembly,
    verification_node_status,
    verification_scope_complete,
)
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_checkpoint_evidence import store_checkpoint_evidence, store_root_evidence
from core.delivery_checkpoint_applicability import validate_root_evidence
from core.delivery_verification_model import (
    DeliveryVerificationRecord,
    delivery_verification_covers_obligations,
    delivery_verification_is_boundary_stop,
    delivery_verification_recovery_action,
)
from core.delivery_verification_validation import (
    DeliveryVerificationValidationResult,
    reusable_delivery_validation,
    run_delivery_verification_validation,
)
from core.state import StateStore
from core.version import sikula_version
from core.validation_artifacts import (
    DeliveryScopeSnapshotError,
    detect_validation_artifacts,
    snapshot_delivery_scope_files,
)
from core.worktree import (
    DetachedWorktreeError,
    copy_worktree_environment_file,
    current_worktree_changes,
    delivery_verification_git_env,
    detached_delivery_verification_worktree,
)
from tools.build_factory import build_tool_class
from tools.base_tool import Sandbox
from tools.build_factory import create_build_tool


DELIVERY_VERIFY_RESULT_SCHEMA_VERSION = 2
DELIVERY_VERIFY_PRIVACY_MODE = "public_metadata"


@dataclass(frozen=True)
class DeliveryVerifyResult:
    plan_path: str
    project_root: str | None
    valid: bool
    ready: bool
    succeeded: bool
    status: str
    gate_id: str | None = None
    candidate_commit: str | None = None
    candidate_tree: str | None = None
    attempt: int | None = None
    semantic_status: str = "not_run"
    security_required: bool = False
    security_status: str = "not_run"
    validation_reused: bool = False
    validation_executed: bool = False
    finding_count: int = 0
    obligation_count: int = 0
    obligation_satisfied_count: int = 0
    obligation_gap_count: int = 0
    stop_code: str | None = None
    next_action: str | None = None
    errors: tuple[DeliveryPlanIssue, ...] = ()
    warnings: tuple[DeliveryPlanIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        root = Path(self.project_root).resolve() if self.project_root else None
        plan_path = Path(self.plan_path)
        try:
            public_plan_path = plan_path.resolve().relative_to(root).as_posix() if root else plan_path.name
        except (OSError, RuntimeError, ValueError):
            public_plan_path = plan_path.name
        public_plan_path = _bounded_public_path(public_plan_path)
        data: dict[str, Any] = {
            "schema_version": DELIVERY_VERIFY_RESULT_SCHEMA_VERSION,
            "sikula_version": sikula_version(),
            "command": "delivery.verify",
            "privacy_mode": DELIVERY_VERIFY_PRIVACY_MODE,
            "plan_path": public_plan_path,
            "project_root": "." if root else None,
            "valid": self.valid,
            "ready": self.ready,
            "succeeded": self.succeeded,
            "status": self.status,
            "semantic_status": self.semantic_status,
            "security_required": self.security_required,
            "security_status": self.security_status,
            "validation_reused": self.validation_reused,
            "validation_executed": self.validation_executed,
            "finding_count": self.finding_count,
            "obligation_count": self.obligation_count,
            "obligation_satisfied_count": self.obligation_satisfied_count,
            "obligation_gap_count": self.obligation_gap_count,
            "errors": [_public_issue(issue) for issue in self.errors],
            "warnings": [_public_issue(issue) for issue in self.warnings],
        }
        for key in ("gate_id", "candidate_commit", "candidate_tree", "attempt", "stop_code", "next_action"):
            value = getattr(self, key)
            if value is not None:
                data[key] = sanitize_delivery_public_metadata(str(value)) if isinstance(value, str) else value
        return data


def verify_delivery_plan(
    path: str | Path,
    project_config: dict[str, Any],
    *,
    state_store: StateStore,
    semantic_reviewer: DeliveryIntegrationReviewAgent | None,
    security_reviewer: DeliveryIntegrationReviewAgent | None,
    project_root: Path | None = None,
    node_id: str = "root",
) -> DeliveryVerifyResult:
    status = verification_node_status(
        reconcile_checkpoint_assembly(get_delivery_status(path, project_root=project_root), persist=True), node_id
    )
    readiness = check_delivery_verification_readiness(status, project_config)
    blocked = _preflight_result(status, readiness, project_config)
    if blocked is not None:
        return blocked
    assert status.plan is not None and status.project_root is not None
    if not readiness.required:
        return DeliveryVerifyResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=True,
            ready=True,
            succeeded=True,
            status="not_required",
            next_action="finalize_delivery",
            warnings=tuple(status.warnings),
        )

    root = Path(status.project_root).resolve()
    plan_id = status.plan.plan_id
    progress_path = delivery_progress_path(root, plan_id)
    events_path = delivery_events_path(root, plan_id)
    evidence_path = progress_path.parent / (
        "verification.jsonl" if node_id == "root" else f"checkpoint-{node_id}-verification.jsonl"
    )
    evidence_reference = evidence_path.relative_to(root).as_posix()

    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.capture"):
            status = verification_node_status(get_delivery_status(path, project_root=root), node_id)
            readiness = check_delivery_verification_readiness(status, project_config)
            blocked = _preflight_result(status, readiness, project_config)
            if blocked is not None:
                return blocked
            if node_id != "root":
                from core.delivery_checkpoints import checkpoint_preflight_issue

                issue = checkpoint_preflight_issue(status, project_config, state_store)
                if issue is not None:
                    return _blocked_result(status, issue.code, [issue])
            progress, progress_errors = read_delivery_progress(progress_path, plan_id=plan_id)
            if progress is None or progress_errors:
                return _blocked_result(status, "delivery_verification.progress_invalid", progress_errors)
            source = status.plan.source_task
            if source is None or delivery_verification_source_task_is_private(
                root,
                root / source.path,
                source.path,
                project_config,
            ):
                return _blocked_result(status, "delivery_verification.source_private")
            if status.plan.checkpoints and progress.checkpoint_ids is None:
                progress = replace(progress, checkpoint_ids=tuple(item.id for item in status.plan.checkpoints))
            existing = progress.verification if node_id == "root" else progress.checkpoint_verifications.get(node_id)
            if existing is not None and not _ensure_verification_progress_event(
                events_path, plan_id, existing, node_id=node_id
            ):
                return _blocked_result(status, "delivery_verification.event_unavailable")
            if existing and existing.passed and progress.assembly_status == "ready" and progress.assembled_commit:
                try:
                    existing_identity = build_delivery_verification_identity(
                        status,
                        project_config,
                        candidate_commit=progress.assembled_commit,
                    )
                except (OSError, RuntimeError, ValueError):
                    existing_identity = None
                if (
                    existing_identity is not None
                    and (
                        node_id == "root"
                        or (
                            existing.review_rule_fingerprints is not None
                            and existing.plan_content_fingerprint is not None
                        )
                    )
                    and _record_matches_identity(
                        existing,
                        existing_identity,
                        obligation_count=_status_obligation_count(status),
                    )
                    and _assembly_ref_matches(root, status, existing.candidate_commit)
                ):
                    return _result_from_record(status, existing, succeeded=True, next_action="finalize_delivery")
            progress, candidate_commit, assembly_error = assemble_delivery_candidate(
                root=root,
                status=status,
                progress=progress,
                progress_path=progress_path,
                events_path=events_path,
                git_env=delivery_verification_git_env(),
            )
            if candidate_commit is None or assembly_error is not None:
                return _blocked_result(
                    status,
                    "delivery_verification.assembly_failed",
                    [assembly_error] if assembly_error else [],
                )
            status = verification_node_status(get_delivery_status(path, project_root=root), node_id)
            blocked = _preflight_result(
                status, check_delivery_verification_readiness(status, project_config), project_config
            )
            if blocked is not None:
                return blocked
            if node_id != "root":
                from core.delivery_checkpoints import checkpoint_authority_evidence_issue

                issue = checkpoint_authority_evidence_issue(status, project_config, state_store)
                if issue is not None:
                    return _blocked_result(status, issue.code, [issue])
            snapshot = build_delivery_verification_snapshot(
                status,
                project_config,
                candidate_commit=candidate_commit,
            )
            identity = snapshot.identity
            try:
                source_task = _read_bound_source_task(root, snapshot, project_config)
            except _PrivateSourceTaskError:
                return _blocked_result(status, "delivery_verification.source_private")
            except (OSError, UnicodeError, ValueError):
                return _blocked_result(status, "delivery_verification.source_changed")
            existing = progress.verification if node_id == "root" else progress.checkpoint_verifications.get(node_id)
            if (
                existing
                and existing.passed
                and (
                    node_id == "root"
                    or (existing.review_rule_fingerprints is not None and existing.plan_content_fingerprint is not None)
                )
                and _record_matches_identity(
                    existing,
                    identity,
                    obligation_count=len(snapshot.scope.obligation_ids),
                )
            ):
                return _result_from_record(status, existing, succeeded=True, next_action="finalize_delivery")
            # Checkpoint evidence is immutable. Returning to an earlier gate
            # identity must not reuse its artifact after an intervening policy
            # or candidate change. Root attempts retain their per-gate numbering.
            attempt = (
                existing.attempt + 1 if existing and (node_id != "root" or existing.gate_id == identity.gate_id) else 1
            )
            rule_fingerprints = None
            plan_content_fingerprint = None
            if node_id != "root":
                from core.delivery_checkpoints import checkpoint_plan_fingerprint, checkpoint_review_rule_fingerprints

                try:
                    plan_content_fingerprint = checkpoint_plan_fingerprint(status, candidate_commit)
                except (OSError, UnicodeError, ValueError):
                    return _blocked_result(status, "delivery_checkpoint.evidence_unavailable")

                roles = ("reviewer", "security_reviewer") if snapshot.scope.security_required else ("reviewer",)
                paths = [
                    Path(project_config[role]["extra_rules"]).as_posix()
                    for role in roles
                    if project_config.get(role, {}).get("extra_rules")
                ]
                try:
                    rule_fingerprints = checkpoint_review_rule_fingerprints(root, candidate_commit, paths)
                except (OSError, UnicodeError, ValueError):
                    return _blocked_result(status, "delivery_verification.review_rules_unavailable")
            running = _record_for_identity(
                identity,
                status="running",
                attempt=attempt,
                security_required=snapshot.scope.security_required,
                obligation_count=len(snapshot.scope.obligation_ids),
                evidence_path=evidence_reference,
                started_at=_now(),
                review_rule_fingerprints=rule_fingerprints,
                plan_content_fingerprint=plan_content_fingerprint,
                reverification=(existing.reverification if existing and existing.gate_id == identity.gate_id else None),
                composition_attempted=bool(
                    existing and existing.gate_id == identity.gate_id and existing.composition_attempted
                ),
                composition_evidence_fingerprint=(
                    existing.composition_evidence_fingerprint
                    if existing and existing.gate_id == identity.gate_id
                    else None
                ),
                security_composition_attempted=bool(
                    existing and existing.gate_id == identity.gate_id and existing.security_composition_attempted
                ),
                security_composition_evidence_fingerprint=(
                    existing.security_composition_evidence_fingerprint
                    if existing and existing.gate_id == identity.gate_id
                    else None
                ),
            )
            progress = mark_delivery_verification(progress, running, node_id=node_id)
            write_delivery_progress(progress_path, progress)
            running_event = DeliveryProgressEvent(
                verification_node=node_id if node_id != "root" else None,
                plan_id=plan_id,
                event_type="verification.running",
                timestamp=running.started_at or _now(),
                commit=identity.candidate_commit,
            )
            if not _safe_append_progress_event(events_path, running_event):
                blocked_record = replace(
                    running,
                    status="blocked",
                    stop_code="delivery_verification.event_unavailable",
                    completed_at=_now(),
                )
                progress = mark_delivery_verification(progress, blocked_record, node_id=node_id)
                write_delivery_progress(progress_path, progress)
                _safe_append_progress_event(
                    events_path,
                    DeliveryProgressEvent(
                        verification_node=node_id if node_id != "root" else None,
                        plan_id=plan_id,
                        event_type="verification.blocked",
                        timestamp=blocked_record.completed_at or _now(),
                        commit=identity.candidate_commit,
                    ),
                )
                _safe_append_audit(
                    evidence_path,
                    {"event": "event_unavailable", "record": blocked_record.to_dict()},
                    project_root=root,
                )
                return _result_from_record(
                    status,
                    blocked_record,
                    succeeded=False,
                    next_action="resolve_delivery_verification_blocker",
                )
            if not _safe_append_audit(
                evidence_path,
                {"event": "running", "record": running.to_dict()},
                project_root=root,
            ):
                blocked_record = replace(
                    running,
                    status="blocked",
                    stop_code="delivery_verification.audit_unavailable",
                    completed_at=_now(),
                )
                progress = mark_delivery_verification(progress, blocked_record, node_id=node_id)
                write_delivery_progress(progress_path, progress)
                _safe_append_progress_event(
                    events_path,
                    DeliveryProgressEvent(
                        verification_node=node_id if node_id != "root" else None,
                        plan_id=plan_id,
                        event_type="verification.blocked",
                        timestamp=blocked_record.completed_at or _now(),
                        commit=identity.candidate_commit,
                    ),
                )
                return _result_from_record(
                    status,
                    blocked_record,
                    succeeded=False,
                    next_action="resolve_delivery_verification_blocker",
                )
    except DeliveryProgressLockError:
        return _blocked_result(status, "delivery.locked")

    terminal = running
    if semantic_reviewer is None:
        terminal = replace(
            running,
            status="blocked",
            stop_code="delivery_verification.reviewer_unavailable",
            completed_at=_now(),
        )
        _safe_append_audit(
            evidence_path,
            {"event": "blocked", "record": terminal.to_dict()},
            project_root=root,
        )
        _persist_terminal_if_current(
            path, root, plan_id, project_config, identity, terminal, events_path, node_id=node_id
        )
        return _result_from_record(
            get_delivery_status(path, project_root=root),
            terminal,
            succeeded=False,
            next_action="resolve_delivery_verification_blocker",
        )
    deferred_validation_evidence: dict[str, Any] | None = None
    deferred_review_evidence: dict[str, Any] | None = None
    try:
        terminal = _execute_gate(
            root=root,
            status=status,
            project_config=project_config,
            state_store=state_store,
            snapshot=snapshot,
            running=running,
            source_task=source_task,
            evidence_path=evidence_path,
            semantic_reviewer=semantic_reviewer,
            security_reviewer=security_reviewer,
        )
    except _GateValidationAuditUnavailable as exc:
        terminal = exc.terminal
        deferred_validation_evidence = exc.validation_evidence
    except _GateReviewAuditUnavailable as exc:
        terminal = exc.terminal
        deferred_review_evidence = exc.review_evidence
    except (KeyboardInterrupt, SystemExit):
        terminal = replace(
            running,
            status="interrupted",
            stop_code="delivery_verification.interrupted",
            completed_at=_now(),
        )
        _safe_append_audit(
            evidence_path,
            {"event": "interrupted", "record": terminal.to_dict()},
            project_root=root,
        )
        _persist_terminal_if_current(
            path, root, plan_id, project_config, identity, terminal, events_path, node_id=node_id
        )
        raise
    except BaseException as exc:
        terminal = replace(
            running,
            status="blocked",
            stop_code="delivery_verification.internal_failure",
            completed_at=_now(),
        )
        _safe_append_audit(
            evidence_path,
            {"event": "internal_failure", "error_type": type(exc).__name__, "record": terminal.to_dict()},
            project_root=root,
        )

    terminal_audit: dict[str, Any] = {"event": terminal.status, "record": terminal.to_dict()}
    if deferred_validation_evidence is not None:
        terminal_audit["validation_evidence"] = deferred_validation_evidence
    if deferred_review_evidence is not None:
        terminal_audit["review_evidence"] = deferred_review_evidence
    if not _safe_append_audit(evidence_path, terminal_audit, project_root=root):
        terminal = replace(
            terminal,
            status="blocked",
            stop_code=terminal.stop_code
            if delivery_verification_recovery_action(terminal.stop_code)
            in {"resolve_readonly_boundary", "resolve_workspace_boundary"}
            else "delivery_verification.audit_unavailable",
            completed_at=_now(),
        )

    current = _persist_terminal_if_current(
        path,
        root,
        plan_id,
        project_config,
        identity,
        terminal,
        events_path,
        node_id=node_id,
    )
    if not current:
        stale = replace(
            terminal,
            status="stale",
            stop_code="delivery_verification.candidate_changed",
            completed_at=_now(),
        )
        _persist_stale_attempt(root, plan_id, identity, stale, events_path, node_id=node_id)
        _safe_append_audit(
            evidence_path,
            {"event": "stale", "record": stale.to_dict()},
            project_root=root,
        )
        return _result_from_record(
            get_delivery_status(path, project_root=root),
            stale,
            succeeded=False,
            next_action="rerun_delivery_verification",
        )
    terminal = current
    return _result_from_record(
        get_delivery_status(path, project_root=root),
        terminal,
        succeeded=terminal.passed,
        next_action=(
            "finalize_delivery"
            if terminal.passed
            else "run_delivery"
            if terminal.repair_input_fingerprint
            else delivery_verification_recovery_action(terminal.stop_code)
        ),
    )


def render_delivery_verify(result: DeliveryVerifyResult) -> str:
    data = result.to_dict()
    lines = [f"Delivery verify: {data['plan_path']}", f"Status: {data['status']}"]
    if data.get("gate_id"):
        lines.append(f"Gate: {data['gate_id']}")
    if data.get("candidate_commit"):
        lines.append(f"Candidate commit: {data['candidate_commit']}")
    if data.get("candidate_tree"):
        lines.append(f"Candidate tree: {data['candidate_tree']}")
    if data.get("attempt"):
        lines.append(f"Attempt: {data['attempt']}")
    lines.extend(
        [
            f"Validation reused: {'yes' if data['validation_reused'] else 'no'}",
            f"Validation executed: {'yes' if data['validation_executed'] else 'no'}",
            f"Semantic review: {data['semantic_status']}",
            f"Security review: {data['security_status']}",
        ]
    )
    if data["obligation_count"]:
        lines.append(
            "Obligations: "
            f"{data['obligation_satisfied_count']}/{data['obligation_count']} satisfied, "
            f"{data['obligation_gap_count']} gap(s)"
        )
    if data.get("stop_code"):
        lines.append(f"Stop code: {data['stop_code']}")
    if data["errors"]:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"- {issue['code']}: {issue['message']}" for issue in data["errors"])
    if data["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {issue['code']}: {issue['message']}" for issue in data["warnings"])
    if data.get("next_action"):
        lines.append("")
        lines.append(f"Next action: {data['next_action']}")
    return "\n".join(lines) + "\n"


def _execute_gate(
    *,
    root: Path,
    status,
    project_config: dict[str, Any],
    state_store: StateStore,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    source_task: str,
    evidence_path: Path,
    semantic_reviewer: DeliveryIntegrationReviewAgent,
    security_reviewer: DeliveryIntegrationReviewAgent | None,
    candidate_review: bool = False,
) -> DeliveryVerificationRecord:
    scope = snapshot.scope
    identity = snapshot.identity
    validation: DeliveryVerificationValidationResult | None = None
    active_review: str | None = None
    semantic_status = "not_run"
    security_status = "not_run"
    obligation_satisfied_count = 0
    obligation_gap_count = 0
    try:
        with detached_delivery_verification_worktree(root, identity.candidate_commit) as worktree:
            copied_environment_files = _copy_environment_files(root, worktree, project_config)
            if not _candidate_links_are_safe(worktree, project_config):
                return replace(
                    running,
                    status="blocked",
                    stop_code="delivery_verification.workspace_boundary_invalid",
                    completed_at=_now(),
                )
            if not _candidate_config_matches_capture(root, worktree, project_config):
                return replace(
                    running,
                    status="blocked",
                    stop_code="delivery_verification.config_changed",
                    completed_at=_now(),
                )
            reusable = reusable_delivery_validation(
                status,
                project_config,
                state_store,
                candidate_tree=identity.candidate_tree,
                scope=scope,
            )
            validation_before = _review_snapshot(worktree, project_config, exclude_ephemeral_paths=True)
            validation = run_delivery_verification_validation(
                worktree,
                project_config,
                reusable=reusable,
                **({"include_final_checks": False} if scope.node_id != "root" else {}),
            )
            validation_evidence = {"event": "validation", "result": _validation_audit(validation)}
            if not _safe_append_audit(evidence_path, validation_evidence, project_root=root):
                terminal = _review_blocked(
                    running,
                    validation,
                    "delivery_verification.audit_unavailable",
                )
                raise _GateValidationAuditUnavailable(terminal, validation_evidence)
            validation_workspace_unchanged = _review_workspace_unchanged(
                worktree,
                project_config,
                validation_before,
                exclude_ephemeral_paths=True,
            )
            _remove_copied_environment_files(worktree, copied_environment_files)
            if not validation_workspace_unchanged:
                return replace(
                    running,
                    status="failed",
                    validation_reused=validation.reused,
                    validation_executed=validation.executed,
                    stop_code="delivery_verification.validation_workspace_mutated",
                    completed_at=_now(),
                )
            if not validation.passed:
                return replace(
                    running,
                    status="failed",
                    validation_reused=validation.reused,
                    validation_executed=validation.executed,
                    stop_code=validation.stop_code or "delivery_verification.validation_failed",
                    completed_at=_now(),
                )

            if snapshot.composition_scope and snapshot.composition_scope.retained_authority_json:
                from core.delivery_reverification import CandidateReviewBoundsError, preflight_candidate_tree

                try:
                    preflight_candidate_tree(status, project_config, snapshot, worktree, source_task, record=running)
                except CandidateReviewBoundsError:
                    return _review_blocked(running, validation, "delivery_verification.hierarchy_required")
                except (OSError, ValueError):
                    return _review_blocked(running, validation, "delivery_verification.evidence_unavailable")
            try:
                semantic_reviewer.prepare_workspace(worktree)
            except LLMReadOnlyViolation:
                return _review_blocked(
                    running, validation, "delivery_verification.readonly_mutation", semantic_status="blocked"
                )
            except (LLMConfigurationError, OSError):
                return _review_blocked(
                    running,
                    validation,
                    "delivery_verification.reviewer_workspace_unavailable",
                    semantic_status="blocked",
                )
            if (
                snapshot.composition_scope
                and snapshot.composition_scope.retained_authority_json
                and scope.security_required
            ):
                if security_reviewer is None:
                    return _review_blocked(
                        running, validation, "delivery_verification.security_unavailable", security_status="blocked"
                    )
                try:
                    security_reviewer.prepare_workspace(worktree)
                except LLMReadOnlyViolation:
                    return _review_blocked(
                        running, validation, "delivery_verification.readonly_mutation", security_status="blocked"
                    )
                except (LLMConfigurationError, OSError):
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.security_workspace_unavailable",
                        security_status="blocked",
                    )
            plan_context = scope.plan_context()
            known_unit_ids = set(scope.unit_ids)
            known_obligation_ids = set(scope.obligation_ids)
            semantic_before = _review_snapshot(worktree, project_config)
            active_review = "semantic"
            semantic, running = _composition_review(
                state_store=state_store,
                semantic_reviewer=semantic_reviewer,
                security_reviewer=security_reviewer,
                status=status,
                snapshot=snapshot,
                running=running,
                reviewer=semantic_reviewer,
                worktree=worktree,
                root=root,
                source_task=source_task,
                validation=validation,
                evidence_path=evidence_path,
                project_config=project_config,
            )
            if semantic is None:
                semantic, running = _bounded_full_review(
                    semantic_reviewer,
                    guard_attempts=candidate_review,
                    status=status,
                    snapshot=snapshot,
                    running=running,
                    project_config=project_config,
                    worktree=worktree,
                    kind="semantic",
                    source_task=source_task,
                    plan_context=plan_context,
                    validation=validation,
                    identity=identity,
                    known_unit_ids=known_unit_ids,
                    known_obligation_ids=known_obligation_ids,
                    evidence_path=evidence_path,
                    audit_root=root,
                )
            semantic_status = "approved" if semantic.assessment.approved else "rejected"
            obligation_satisfied_count = sum(
                result.outcome == "satisfied" for result in semantic.assessment.obligation_results
            )
            obligation_gap_count = len(semantic.assessment.obligation_results) - obligation_satisfied_count
            if (
                semantic.composition is not None
                and semantic.composition.child_refs
                and not semantic.composition.fallback
            ):
                if semantic.assessment.approved or semantic.assessment.disposition == "repair_required":
                    obligation_satisfied_count += semantic.composition.inherited_count
                else:
                    obligation_gap_count += semantic.composition.inherited_count
            if not _review_workspace_unchanged(worktree, project_config, semantic_before) or not _candidate_is_clean(
                worktree, identity.candidate_commit
            ):
                return _review_blocked(
                    running,
                    validation,
                    "delivery_verification.readonly_mutation",
                    semantic_status="blocked",
                )
            if not semantic.assessment.approved:
                repair_fingerprint = None
                if semantic.assessment.disposition == "repair_required" and scope.obligation_ids:
                    from core.delivery_repair_input import store_repair_input

                    try:
                        repair_fingerprint = store_repair_input(
                            root,
                            evidence_path.parent,
                            identity,
                            running.attempt,
                            semantic.assessment,
                            project_config,
                            composition_fingerprint=running.composition_evidence_fingerprint
                            if semantic.composition is not None
                            and semantic.composition.child_refs
                            and not semantic.composition.fallback
                            else None,
                        )
                    except (OSError, ValueError, TypeError):
                        return _review_blocked(
                            running,
                            validation,
                            "delivery_verification.repair_input_unavailable",
                            semantic_status="rejected",
                            obligation_satisfied_count=obligation_satisfied_count,
                            obligation_gap_count=obligation_gap_count,
                        )
                return replace(
                    running,
                    status="failed",
                    semantic_status="rejected",
                    security_status="not_run",
                    validation_reused=validation.reused,
                    validation_executed=validation.executed,
                    finding_count=len(semantic.assessment.findings),
                    obligation_satisfied_count=obligation_satisfied_count,
                    obligation_gap_count=obligation_gap_count,
                    stop_code=f"delivery_verification.{semantic.assessment.disposition}",
                    repair_input_fingerprint=repair_fingerprint,
                    completed_at=_now(),
                )

            finding_count = 0
            if running.security_required:
                if security_reviewer is None:
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.security_unavailable",
                        semantic_status=semantic_status,
                        security_status="blocked",
                        obligation_satisfied_count=obligation_satisfied_count,
                        obligation_gap_count=obligation_gap_count,
                    )
                try:
                    security_reviewer.prepare_workspace(worktree)
                except LLMReadOnlyViolation:
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.readonly_mutation",
                        semantic_status=semantic_status,
                        security_status="blocked",
                    )
                except (LLMConfigurationError, OSError):
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.security_workspace_unavailable",
                        semantic_status=semantic_status,
                        security_status="blocked",
                        obligation_satisfied_count=obligation_satisfied_count,
                        obligation_gap_count=obligation_gap_count,
                    )
                security_before = _review_snapshot(worktree, project_config)
                active_review = "security"
                security, running = _composition_review(
                    state_store=state_store,
                    semantic_reviewer=semantic_reviewer,
                    security_reviewer=security_reviewer,
                    status=status,
                    snapshot=snapshot,
                    running=running,
                    reviewer=security_reviewer,
                    worktree=worktree,
                    root=root,
                    source_task=source_task,
                    validation=validation,
                    evidence_path=evidence_path,
                    project_config=project_config,
                    review_kind="security",
                )
                if security is None:
                    security, running = _bounded_full_review(
                        security_reviewer,
                        guard_attempts=candidate_review,
                        status=status,
                        snapshot=snapshot,
                        running=running,
                        project_config=project_config,
                        worktree=worktree,
                        kind="security",
                        source_task=source_task,
                        plan_context=plan_context,
                        validation=validation,
                        identity=identity,
                        known_unit_ids=known_unit_ids,
                        known_obligation_ids=set(),
                        evidence_path=evidence_path,
                        audit_root=root,
                    )
                security_status = "approved" if security.assessment.approved else "rejected"
                if not _review_workspace_unchanged(
                    worktree, project_config, security_before
                ) or not _candidate_is_clean(worktree, identity.candidate_commit):
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.readonly_mutation",
                        semantic_status=semantic_status,
                        security_status="blocked",
                        obligation_satisfied_count=obligation_satisfied_count,
                        obligation_gap_count=obligation_gap_count,
                    )
                if not security.assessment.approved:
                    return replace(
                        running,
                        status="failed",
                        semantic_status="approved",
                        security_status="rejected",
                        validation_reused=validation.reused,
                        validation_executed=validation.executed,
                        finding_count=len(security.assessment.findings),
                        obligation_satisfied_count=obligation_satisfied_count,
                        obligation_gap_count=obligation_gap_count,
                        stop_code=f"delivery_verification.{security.assessment.disposition}",
                        completed_at=_now(),
                    )
                finding_count = len(security.assessment.findings)
            if snapshot.candidate_route and running.completed_at is None:
                # Reserve the completion identity before immutable evidence publication.
                # If publication is interrupted, accepted exchanges can reconstruct the
                # same artifact without inventing a new timestamp or provider attempt.
                running = _update_candidate_record(status, snapshot, running, project_config, {"completed_at": _now()})
            passed = replace(
                running,
                status="passed",
                semantic_status="approved",
                security_status=security_status,
                validation_reused=validation.reused,
                validation_executed=validation.executed,
                finding_count=finding_count,
                obligation_satisfied_count=obligation_satisfied_count,
                obligation_gap_count=obligation_gap_count,
                completed_at=running.completed_at or _now(),
            )
            if scope.node_id != "root":
                try:
                    fingerprint = store_checkpoint_evidence(
                        root,
                        evidence_path.parent,
                        snapshot,
                        passed,
                        semantic.assessment,
                        composition=semantic.composition,
                        status=status,
                    )
                except (OSError, ValueError):
                    return replace(
                        passed,
                        status="blocked",
                        stop_code="delivery_checkpoint.evidence_unavailable",
                    )
                passed = replace(passed, checkpoint_evidence_fingerprint=fingerprint)
            elif status.plan.checkpoints:
                try:
                    fingerprint = store_root_evidence(
                        root,
                        evidence_path.parent,
                        snapshot,
                        passed,
                        semantic.assessment,
                        **(
                            {"composition": semantic.composition, "status": status}
                            if semantic.composition is not None
                            and semantic.composition.child_refs
                            and not semantic.composition.fallback
                            else {}
                        ),
                    )
                except (OSError, ValueError):
                    return replace(passed, status="blocked", stop_code="delivery_verification.evidence_unavailable")
                passed = replace(passed, root_evidence_fingerprint=fingerprint)
            return passed
    except _CandidateRepairRequired as exc:
        from core.delivery_candidate_repair import CANDIDATE_REPAIR_REQUIRED, store_candidate_repair_handoff

        running = exc.record
        try:
            digest = store_candidate_repair_handoff(status, running, exc.child_id, project_config)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return _review_blocked(running, validation, "delivery_verification.repair_input_unavailable")
        return replace(
            running,
            status="failed",
            semantic_status="rejected",
            security_status="not_run",
            stop_code=CANDIDATE_REPAIR_REQUIRED,
            repair_input_fingerprint=digest,
            finding_count=exc.child.finding_count,
            obligation_satisfied_count=0,
            obligation_gap_count=exc.child.obligation_gap_count,
            validation_reused=validation.reused,
            validation_executed=validation.executed,
            completed_at=_now(),
        )
    except _SemanticRecheckRequired as exc:
        # The child result is already durable. Re-enter semantic review with the
        # same reservations; it must use the remaining full fallback, not the
        # earlier composed approval. A spent fallback stops instead of refilling.
        return _execute_gate(
            root=root,
            status=status,
            project_config=project_config,
            state_store=state_store,
            snapshot=snapshot,
            running=exc.record,
            source_task=source_task,
            evidence_path=evidence_path,
            semantic_reviewer=semantic_reviewer,
            security_reviewer=security_reviewer,
            candidate_review=candidate_review,
        )
    except _CandidateReviewStopped as exc:
        return replace(
            running,
            status="blocked",
            stop_code=exc.record.stop_code,
            security_status=exc.record.security_status,
            completed_at=_now(),
        )
    except DeliveryIntegrationReviewAgentError as exc:
        return _review_blocked(
            running,
            validation,
            exc.code,
            semantic_status="blocked" if active_review == "semantic" else semantic_status,
            security_status="blocked" if active_review == "security" else security_status,
            obligation_satisfied_count=obligation_satisfied_count,
            obligation_gap_count=obligation_gap_count,
        )
    except _ReviewAuditUnavailable as exc:
        phase_status = exc.phase_status
        terminal = _review_blocked(
            running,
            validation,
            exc.boundary_stop_code or "delivery_verification.audit_unavailable",
            semantic_status=phase_status if active_review == "semantic" else semantic_status,
            security_status=phase_status if active_review == "security" else security_status,
            obligation_satisfied_count=(
                exc.obligation_satisfied_count if active_review == "semantic" else obligation_satisfied_count
            ),
            obligation_gap_count=(exc.obligation_gap_count if active_review == "semantic" else obligation_gap_count),
        )
        terminal = replace(terminal, finding_count=exc.finding_count)
        raise _GateReviewAuditUnavailable(terminal, exc.review_evidence) from None
    except DeliveryScopeSnapshotError:
        return _review_blocked(
            running,
            validation,
            "delivery_verification.workspace_audit_unavailable",
            semantic_status=semantic_status,
            security_status=security_status,
            obligation_satisfied_count=obligation_satisfied_count,
            obligation_gap_count=obligation_gap_count,
        )
    except DetachedWorktreeError:
        return _review_blocked(
            running,
            validation,
            "delivery_verification.worktree_unavailable",
            semantic_status=semantic_status,
            security_status=security_status,
            obligation_satisfied_count=obligation_satisfied_count,
            obligation_gap_count=obligation_gap_count,
        )


class _CandidateRepairRequired(RuntimeError):
    def __init__(self, record: DeliveryVerificationRecord, child_id: str, child: DeliveryVerificationRecord) -> None:
        self.record = record
        self.child_id = child_id
        self.child = child


def _route_candidate_repair(status, parent, child, child_id, cfg):
    from core.delivery_candidate_repair import CANDIDATE_REPAIR_REQUIRED, eligible_local_finding

    if child.stop_code == CANDIDATE_REPAIR_REQUIRED or eligible_local_finding(
        replace(status, verification_node=child_id), child, cfg
    ):
        raise _CandidateRepairRequired(parent, child_id, child)


class _CandidateReviewStopped(RuntimeError):
    def __init__(self, record: DeliveryVerificationRecord) -> None:
        self.record = record


def _update_candidate_record(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    cfg: dict[str, Any],
    changes: dict[str, Any],
    *,
    boundary: bool = False,
) -> DeliveryVerificationRecord:
    """Publish one node and its immediate acceptance references under one lock."""
    from core.delivery_candidate_control import CandidateBudgetExceeded, record_at, replace_at, reserved_node_count
    from core.delivery_reverification import candidate_snapshot

    root = Path(status.project_root)
    path = delivery_progress_path(root, status.plan.plan_id)
    anchor_id = snapshot.candidate_anchor if snapshot.candidate_route else snapshot.scope.node_id
    with acquire_delivery_progress_lock(root, status.plan.plan_id, owner="delivery.verify.candidate"):
        progress, errors = read_delivery_progress(path, plan_id=status.plan.plan_id)
        anchor = (
            (progress.verification if anchor_id == "root" else progress.checkpoint_verifications.get(anchor_id))
            if progress
            else None
        )
        if errors or anchor is None:
            raise ValueError("Candidate review attempt unavailable.")
        if (
            snapshot.candidate_route
            and not boundary
            and (
                anchor.status != "running"
                or (anchor.gate_id, anchor.attempt)
                != (snapshot.candidate_anchor_gate_id, snapshot.candidate_anchor_attempt)
            )
        ):
            raise ValueError("Initiating candidate review attempt changed.")
        record = record_at(root, path.parent, anchor, snapshot.candidate_route)
        if (record.gate_id, record.attempt) != (running.gate_id, running.attempt) or (
            record.status != "running" and not (boundary and delivery_verification_is_boundary_stop(record))
        ):
            raise ValueError("Candidate review attempt changed.")
        if not boundary:
            current = verification_node_status(get_delivery_status(status.plan_path, project_root=root), anchor_id)
            expected = build_delivery_verification_snapshot(current, cfg, candidate_commit=anchor.candidate_commit)
            if expected.identity.gate_id != anchor.gate_id or not _assembly_ref_matches(
                root, current, anchor.candidate_commit
            ):
                raise ValueError("Candidate review authority changed.")
            parent = anchor
            for node_id in snapshot.candidate_route:
                expected = candidate_snapshot(current, cfg, parent, node_id)
                from core.delivery_candidate_control import expanded_child

                parent = expanded_child(current, parent.reverification["children"][node_id])
            if expected.identity != snapshot.identity or (
                snapshot.composition_scope and not _composition_children_unchanged(status, current, snapshot, cfg)
            ):
                raise ValueError("Candidate review authority changed.")
        updated = replace(record, **changes)
        updated_anchor = replace_at(root, path.parent, anchor, snapshot.candidate_route, updated)
        # The persisted tree is the reservation ledger. Charge the worst case for
        # each node once, regardless of role, result, interruption or fallback.
        if not boundary:
            try:
                reserved_node_count(root, path.parent, updated_anchor)
            except CandidateBudgetExceeded as exc:
                raise DeliveryIntegrationReviewAgentError(
                    "delivery_verification.reverification_budget_exhausted",
                    "Candidate subtree work budget exhausted.",
                    [],
                ) from exc
        if not boundary:
            from core.delivery_reverification import validate_reverification

            validate_reverification(
                replace(current, verification_node=snapshot.scope.node_id),
                updated,
                cfg,
                scope=snapshot.composition_scope,
            )
            if updated.passed and snapshot.candidate_route:
                from core.delivery_checkpoint_evidence import load_checkpoint_evidence

                evidence = load_checkpoint_evidence(
                    root, path.parent, updated, plan_id=status.plan.plan_id, node_id=snapshot.scope.node_id
                )
                if not evidence.covers(snapshot.scope):
                    raise ValueError("Candidate node coverage changed.")
        if anchor_id == "root":
            progress = replace(progress, verification=updated_anchor)
        else:
            progress = replace(
                progress, checkpoint_verifications={**progress.checkpoint_verifications, anchor_id: updated_anchor}
            )
        write_delivery_progress(path, progress)
        return updated


def _save_reverification(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    cfg: dict[str, Any],
    control: dict[str, Any],
    *,
    boundary: bool = False,
) -> DeliveryVerificationRecord:
    return _update_candidate_record(status, snapshot, running, cfg, {"reverification": control}, boundary=boundary)


def _persist_candidate_boundary(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    terminal: DeliveryVerificationRecord,
) -> None:
    """Latch the initiating gate's boundary without reading descendant sidecars."""
    if not snapshot.candidate_route or not delivery_verification_is_boundary_stop(terminal):
        raise ValueError("Candidate boundary evidence is unavailable.")
    root = Path(status.project_root)
    path = delivery_progress_path(root, status.plan.plan_id)
    anchor_id = snapshot.candidate_anchor
    with acquire_delivery_progress_lock(root, status.plan.plan_id, owner="delivery.verify.boundary"):
        progress, errors = read_delivery_progress(path, plan_id=status.plan.plan_id)
        anchor = (
            (progress.verification if anchor_id == "root" else progress.checkpoint_verifications.get(anchor_id))
            if progress
            else None
        )
        if errors or anchor is None or anchor.gate_id != snapshot.candidate_anchor_gate_id:
            raise ValueError("Initiating candidate review evidence is unavailable.")
        if delivery_verification_is_boundary_stop(replace(anchor, reverification=None)):
            return
        anchor = replace(
            anchor,
            status="blocked",
            stop_code=terminal.stop_code,
            security_status=terminal.security_status,
            completed_at=terminal.completed_at or _now(),
        )
        progress = (
            replace(progress, verification=anchor)
            if anchor_id == "root"
            else replace(progress, checkpoint_verifications={**progress.checkpoint_verifications, anchor_id: anchor})
        )
        write_delivery_progress(path, progress)


def _current_candidate_record(
    status: DeliveryStatusResult, snapshot: DeliveryVerificationSnapshot
) -> DeliveryVerificationRecord:
    from core.delivery_candidate_control import record_at

    root = Path(status.project_root)
    path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(path, plan_id=status.plan.plan_id)
    anchor_id = snapshot.candidate_anchor if snapshot.candidate_route else snapshot.scope.node_id
    if errors or progress is None:
        raise ValueError("Candidate control is unavailable.")
    anchor = progress.verification if anchor_id == "root" else progress.checkpoint_verifications.get(anchor_id)
    if anchor is None:
        raise ValueError("Candidate control is unavailable.")
    return record_at(root, path.parent, anchor, snapshot.candidate_route)


def _composition_children_unchanged(
    status: DeliveryStatusResult,
    current: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    cfg: dict[str, Any],
) -> bool:
    from core.delivery_checkpoints import checkpoint_pass_is_usable

    if snapshot.composition_scope is None:
        return False
    children = {child.node_id for child in snapshot.composition_scope.children}
    return all(
        current.checkpoint_verifications.get(key) == status.checkpoint_verifications.get(key) for key in children
    ) and all(
        checkpoint_pass_is_usable(current, child, cfg) for child in current.plan.checkpoints if child.id in children
    )


def _composition_review(
    *,
    state_store: StateStore,
    semantic_reviewer: DeliveryIntegrationReviewAgent,
    security_reviewer: DeliveryIntegrationReviewAgent | None,
    **kwargs: Any,
) -> tuple[DeliveryIntegrationReviewResult | None, DeliveryVerificationRecord]:
    """Adapt the common node cycle to captured delivery persistence and review."""
    from core.delivery_composition import load_composition
    from core.delivery_reverification import (
        run_node_composition_cycle,
        validate_reverification,
        semantic_fallback_covers_gaps,
    )

    role = kwargs.get("review_kind", "semantic")
    scope = kwargs["snapshot"].composition_scope
    try:
        return run_node_composition_cycle(
            scope,
            kwargs["running"],
            role,
            exchange=lambda running, reassessment: _composition_exchange(
                **{**kwargs, "running": running, "reassessment": reassessment}
            ),
            validate=lambda running: validate_reverification(
                kwargs["status"], running, kwargs["project_config"], scope=scope
            ),
            load=lambda running: load_composition(kwargs["status"], running, review_kind=role, scope=scope),
            review_child=lambda running, node_id: _reverify_candidate_child(
                status=kwargs["status"],
                snapshot=kwargs["snapshot"],
                running=running,
                cfg=kwargs["project_config"],
                node_id=node_id,
                source_task=kwargs["source_task"],
                evidence_path=kwargs["evidence_path"],
                state_store=state_store,
                semantic_reviewer=semantic_reviewer,
                security_reviewer=security_reviewer,
            ),
            semantic_fallback_covers=lambda running: semantic_fallback_covers_gaps(kwargs["status"], running),
            refresh=lambda running: _reserve_composition_refresh(
                kwargs["status"],
                kwargs["snapshot"],
                running,
                kwargs["project_config"],
                role,
                kwargs["evidence_path"],
                packet_fits=lambda context: _composition_packet_fits(
                    context,
                    kwargs["snapshot"],
                    kwargs["reviewer"],
                    kwargs["worktree"],
                    kwargs["source_task"],
                    kwargs["validation"],
                    kwargs["project_config"],
                    role,
                ),
            ),
        )
    except (OSError, ValueError, KeyError, DeliveryProgressLockError):
        # Validation and accepted-result loading also read private evidence
        # between exchanges; storage loss must stop the entire cycle safely.
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.evidence_unavailable", "Composition evidence is unavailable.", []
        ) from None


def _reserve_composition_refresh(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    cfg: dict[str, Any],
    role: str,
    evidence_path: Path,
    *,
    packet_fits: Callable[[dict[str, Any]], bool],
) -> tuple[DeliveryVerificationRecord, list[str]]:
    """Reserve deterministic prerequisites without inventing a reviewer result."""
    from copy import deepcopy
    from core.delivery_composition import CompositionRefresh, build_composition_context
    from core.delivery_reverification import new_reverification

    control = deepcopy(running.reverification) if running.reverification is not None else new_reverification()
    request = control.get("refreshes", {}).get(role)
    if request is None:
        prefix = "" if role == "semantic" else "security_"
        if getattr(running, prefix + "composition_attempted") or role in control["fallbacks"]:
            return running, []
        context = build_composition_context(status, snapshot, cfg, review_kind=role, record=running)
        if not isinstance(context, CompositionRefresh):
            return running, []
        if not packet_fits(context.preview_context):
            return running, []
        request = {"nodes": context.nodes, "review_reserved": False}
        control.setdefault("refreshes", {})[role] = request
        running = _persist_composition_control(
            status, snapshot, running, cfg, attempted=True, review_kind=role, reverification=control
        )
    if request["review_reserved"] or role in control["fallbacks"]:
        return running, []
    if not _safe_append_audit(
        evidence_path,
        {
            "event": "composition_refresh_required",
            "gate_id": running.gate_id,
            "attempt": running.attempt,
            "review_kind": role,
            "nodes": request["nodes"],
        },
        project_root=Path(status.project_root),
    ):
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.audit_unavailable", "Composition refresh audit is unavailable.", []
        )
    return running, list(request["nodes"])


def _reverify_candidate_child(
    *,
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    cfg: dict[str, Any],
    node_id: str,
    source_task: str,
    evidence_path: Path,
    state_store: StateStore,
    semantic_reviewer: DeliveryIntegrationReviewAgent,
    security_reviewer: DeliveryIntegrationReviewAgent | None,
) -> tuple[DeliveryVerificationRecord, bool]:
    from copy import deepcopy
    from core.delivery_reverification import candidate_snapshot
    from core.delivery_checkpoints import checkpoint_plan_fingerprint, checkpoint_review_rule_fingerprints

    if snapshot.composition_scope is None:
        raise ValueError("Candidate review requires a captured composition scope.")
    snapshot.composition_scope.child(node_id)
    from core.delivery_candidate_control import MAX_CANDIDATE_NODES, MAX_NODE_PROVIDER_CALLS, expanded_child

    control = deepcopy(running.reverification)
    existing = control["children"].get(node_id)
    root = Path(status.project_root)
    child_snapshot = candidate_snapshot(status, cfg, running, node_id)
    child_snapshot = replace(
        child_snapshot,
        candidate_route=(*snapshot.candidate_route, node_id),
        candidate_anchor=snapshot.candidate_anchor if snapshot.candidate_route else snapshot.scope.node_id,
        candidate_anchor_gate_id=snapshot.candidate_anchor_gate_id if snapshot.candidate_route else running.gate_id,
        candidate_anchor_attempt=snapshot.candidate_anchor_attempt if snapshot.candidate_route else running.attempt,
    )
    if existing:
        child = expanded_child(status, existing)
        if (
            delivery_verification_is_boundary_stop(child)
            or child.stop_code == "delivery_verification.reverification_budget_exhausted"
        ):
            raise _CandidateReviewStopped(child)
        if child.status != "running" or child_snapshot.composition_scope is None:
            _route_candidate_repair(status, running, child, node_id, cfg)
            return running, child.passed
    else:
        roles = ("reviewer", "security_reviewer") if child_snapshot.scope.security_required else ("reviewer",)
        rules = checkpoint_review_rule_fingerprints(
            root,
            running.candidate_commit,
            [Path(cfg[role]["extra_rules"]).as_posix() for role in roles if cfg.get(role, {}).get("extra_rules")],
        )
        child = _record_for_identity(
            child_snapshot.identity,
            status="running",
            attempt=1,
            security_required=child_snapshot.scope.security_required,
            obligation_count=len(child_snapshot.scope.obligation_ids),
            evidence_path=evidence_path.relative_to(root).as_posix(),
            started_at=_now(),
            review_rule_fingerprints=rules,
            plan_content_fingerprint=checkpoint_plan_fingerprint(status, running.candidate_commit),
        )
        control["children"][node_id] = {
            "origin": status.checkpoint_verifications[node_id].checkpoint_evidence_fingerprint,
            "verification": child.to_dict(),
        }
        running = _save_reverification(status, snapshot, running, cfg, control)
    if not _safe_append_audit(
        evidence_path,
        {
            "event": "candidate_review_reserved",
            "max_node_provider_calls": MAX_NODE_PROVIDER_CALLS,
            "max_subtree_provider_calls": MAX_CANDIDATE_NODES * MAX_NODE_PROVIDER_CALLS,
            "node_id": node_id,
            "parent_gate": running.gate_id,
            "record": child.to_dict(),
        },
        project_root=root,
    ):
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.audit_unavailable", "Candidate review audit unavailable.", []
        )
    deferred = {}
    try:
        child = _execute_gate(
            root=root,
            status=replace(status, verification_node=node_id),
            project_config=cfg,
            state_store=state_store,
            snapshot=child_snapshot,
            candidate_review=True,
            running=child,
            source_task=source_task,
            evidence_path=evidence_path,
            semantic_reviewer=semantic_reviewer,
            security_reviewer=security_reviewer,
        )
    except _GateReviewAuditUnavailable as exc:
        child = exc.terminal
        deferred = {"review_evidence": exc.review_evidence}
    except _GateValidationAuditUnavailable as exc:
        child = exc.terminal
        deferred = {"validation_evidence": exc.validation_evidence}
    boundary = delivery_verification_is_boundary_stop(child)
    if boundary:
        _persist_candidate_boundary(status, child_snapshot, child)
    if (
        not _safe_append_audit(
            evidence_path,
            {
                "event": "candidate_review_completed",
                "node_id": node_id,
                "parent_gate": running.gate_id,
                "record": child.to_dict(),
                **deferred,
            },
            project_root=root,
        )
        and not boundary
    ):
        child = replace(child, status="blocked", stop_code="delivery_verification.audit_unavailable")
    # Inner exchanges publish their own reservations. Refresh them before accepting
    # the terminal result; an outer stack frame must never overwrite newer control.
    try:
        latest = _current_candidate_record(status, child_snapshot)
        child = replace(
            child,
            reverification=latest.reverification,
            composition_attempted=latest.composition_attempted,
            composition_evidence_fingerprint=latest.composition_evidence_fingerprint,
            security_composition_attempted=latest.security_composition_attempted,
            security_composition_evidence_fingerprint=latest.security_composition_evidence_fingerprint,
        )
        _update_candidate_record(status, child_snapshot, latest, cfg, vars(child), boundary=boundary)
        parent = _current_candidate_record(status, snapshot)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        if boundary:
            # The stop is already durable. Missing detail must not turn it into
            # a retryable evidence/internal failure while unwinding ancestors.
            raise _CandidateReviewStopped(child) from None
        raise
    if not boundary and (parent.gate_id, parent.attempt) != (running.gate_id, running.attempt):
        raise ValueError("Initiating candidate review attempt changed.")
    running = parent
    if not boundary:
        _route_candidate_repair(status, running, child, node_id, cfg)
    if boundary or (not child.passed and child.stop_code != "delivery_verification.repair_required"):
        raise _CandidateReviewStopped(child)
    return running, child.passed


def _bounded_full_review(
    reviewer: DeliveryIntegrationReviewAgent,
    *,
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    project_config: dict[str, Any],
    **kwargs: Any,
) -> tuple[DeliveryIntegrationReviewResult, DeliveryVerificationRecord]:
    from copy import deepcopy
    from core.delivery_composition import fingerprint
    from core.delivery_reverification import (
        load_full_result,
        review_result_path,
        semantic_gap_ids,
        semantic_fallback_covers_gaps,
    )
    from core.delivery_repair_storage import write_repair_state
    from core.delivery_review_route import review_route

    role = kwargs["kind"]
    fallbacks = (running.reverification or {}).get("fallbacks", {})
    if role in fallbacks:
        if fallbacks[role] is None or (role == "semantic" and not semantic_fallback_covers_gaps(status, running)):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.reverification_budget_exhausted",
                "The reserved fallback has no accepted result covering current child assessments.",
                [],
            )
        return DeliveryIntegrationReviewResult(
            load_full_result(status, running, role, scope=snapshot.scope), []
        ), running
    if not review_route(
        snapshot.scope,
        project_config,
        kwargs["worktree"],
        kwargs["source_task"],
        role,
        validation=kwargs["validation"].to_review_dict(
            project_config,
            project_root=kwargs["worktree"],
            include_final_checks=snapshot.scope.node_id == "root",
        ),
        reviewer=reviewer,
    ).full_available:
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.hierarchy_required",
            "This unresolved assessment requires full review, but its rendered packet exceeds supported bounds.",
            [],
        )

    if running.reverification is None and snapshot.candidate_route and snapshot.composition_scope is not None:
        from core.delivery_reverification import new_reverification

        running = _update_candidate_record(
            status,
            snapshot,
            running,
            project_config,
            {"composition_attempted": True, "reverification": new_reverification()},
        )
    if running.reverification is None:
        return _run_review(reviewer, **kwargs), running
    kwargs["guard_attempts"] = True
    control = deepcopy(running.reverification)
    control["fallbacks"][role] = None
    running = _save_reverification(status, snapshot, running, project_config, control)
    before = _review_snapshot(kwargs["worktree"], project_config)
    result = _run_review(reviewer, **kwargs)
    if not _review_workspace_unchanged(kwargs["worktree"], project_config, before) or not _candidate_is_clean(
        kwargs["worktree"], running.candidate_commit
    ):
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.readonly_mutation", "Fallback reviewer changed its workspace.", []
        )
    if role == "security" and not result.assessment.approved:
        # Rejections belong to the captured attempt even if assembly advanced.
        # Let the caller persist the terminal boundary, without ordinary result
        # storage/freshness checks that could replace it with candidate_changed.
        return result, running
    # The existing caller also checks the workspace; do not accept a cached result
    # until the full exchange has passed the same physical boundary.
    payload = {
        "gate_id": running.gate_id,
        "role": role,
        "assessment": result.assessment.to_dict(),
        "semantic_gaps": semantic_gap_ids(running) if role == "semantic" else [],
    }
    digest = fingerprint(payload)
    write_repair_state(kwargs["audit_root"], review_result_path(kwargs["evidence_path"].parent, digest), payload)
    control["fallbacks"][role] = digest
    running = _save_reverification(status, snapshot, running, project_config, control)
    return result, running


def _composition_packet_fits(
    context: dict[str, Any],
    snapshot: DeliveryVerificationSnapshot,
    reviewer: DeliveryIntegrationReviewAgent,
    worktree: Path,
    source_task: str,
    validation: DeliveryVerificationValidationResult,
    cfg: dict[str, Any],
    role: str,
) -> bool:
    from core.delivery_review_route import review_route

    return review_route(
        snapshot.scope,
        cfg,
        worktree,
        source_task,
        role,
        context=context,
        validation=validation.to_review_dict(
            cfg, project_root=worktree, include_final_checks=snapshot.scope.node_id == "root"
        ),
        reviewer=reviewer,
    ).composition_available


def _composition_exchange(
    *,
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    reviewer: DeliveryIntegrationReviewAgent,
    worktree: Path,
    root: Path,
    source_task: str,
    validation: DeliveryVerificationValidationResult,
    evidence_path: Path,
    project_config: dict[str, Any],
    review_kind: str = "semantic",
    reassessment: bool = False,
) -> tuple[DeliveryIntegrationReviewResult | None, DeliveryVerificationRecord]:
    from core.delivery_composition import (
        CompositionRefresh,
        build_composition_context,
        load_composition,
        store_composition,
    )

    if snapshot.composition_scope is None:
        return None, running
    try:
        evidence_field = (
            "composition_evidence_fingerprint"
            if review_kind == "semantic"
            else "security_composition_evidence_fingerprint"
        )
        attempted_field = "composition_attempted" if review_kind == "semantic" else "security_composition_attempted"
        round_state = (running.reverification or {}).get("reassessments", {}).get(review_kind)
        refresh = (running.reverification or {}).get("refreshes", {}).get(review_kind)
        if reassessment and round_state and not round_state["accepted"]:
            return None, running
        if getattr(running, evidence_field) and (not reassessment or round_state):
            accepted = load_composition(status, running, review_kind=review_kind, scope=snapshot.composition_scope)
            if accepted.fallback:
                return None, running
            return DeliveryIntegrationReviewResult(accepted.assessment, [], accepted), running
        if getattr(running, attempted_field) and not reassessment and (refresh is None or refresh["review_reserved"]):
            return None, running
        context = build_composition_context(status, snapshot, project_config, review_kind=review_kind, record=running)
        if context is None or isinstance(context, CompositionRefresh):
            return None, running
        if not _composition_packet_fits(
            context, snapshot, reviewer, worktree, source_task, validation, project_config, review_kind
        ):
            return None, running
        from copy import deepcopy
        from core.delivery_reverification import new_reverification

        control = (
            deepcopy(running.reverification)
            if running.reverification is not None
            else new_reverification()
            if context["checkpoint_composition"].get("compact")
            else None
        )
        if reassessment:
            control["reassessments"][review_kind] = {"initial": getattr(running, evidence_field), "accepted": False}
        if refresh is not None:
            control["refreshes"][review_kind]["review_reserved"] = True
        running = _persist_composition_control(
            status,
            snapshot,
            running,
            project_config,
            attempted=True,
            review_kind=review_kind,
            fingerprint=getattr(running, evidence_field),
            reverification=control,
        )
        if not _safe_append_audit(
            evidence_path,
            {
                "event": "composition_reserved",
                "review_kind": review_kind,
                "gate_id": running.gate_id,
                "attempt": running.attempt,
                "max_provider_calls": 2,
            },
            project_root=root,
        ):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.audit_unavailable", "Composition audit is unavailable.", []
            )
        before = _review_snapshot(worktree, project_config)
        try:
            result = _run_review(
                reviewer,
                worktree=worktree,
                kind=review_kind,
                source_task=source_task,
                plan_context=context,
                validation=validation,
                identity=snapshot.identity,
                known_unit_ids=set(snapshot.scope.unit_ids),
                known_obligation_ids=set(snapshot.scope.obligation_ids) if review_kind == "semantic" else set(),
                evidence_path=evidence_path,
                audit_root=root,
            )
        except DeliveryIntegrationReviewAgentError as exc:
            if not _review_workspace_unchanged(worktree, project_config, before) or not _candidate_is_clean(
                worktree, snapshot.identity.candidate_commit
            ):
                raise DeliveryIntegrationReviewAgentError(
                    "delivery_verification.readonly_mutation", "Composition reviewer changed its workspace.", []
                ) from exc
            if exc.code == "delivery_verification.composition_invalid":
                return None, running
            raise
        except _ReviewAuditUnavailable as exc:
            if not _review_workspace_unchanged(worktree, project_config, before) or not _candidate_is_clean(
                worktree, snapshot.identity.candidate_commit
            ):
                exc.boundary_stop_code = "delivery_verification.readonly_mutation"
            raise
        if not _review_workspace_unchanged(worktree, project_config, before) or not _candidate_is_clean(
            worktree, snapshot.identity.candidate_commit
        ):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.readonly_mutation", "Composition reviewer changed its workspace.", []
            )
        if result.composition is None:
            raise ValueError("Composition assessment unavailable.")
        if (
            not result.assessment.approved
            and not result.composition.fallback
            and not (review_kind == "semantic" and result.assessment.disposition == "repair_required")
        ):
            # Preserve the stop, but do not cache an unavailable external input forever.
            # A later explicit retry uses full review within the existing recovery policy.
            return result, running
        digest = store_composition(root, evidence_path.parent, running.gate_id, context, result.composition)
        if reassessment:
            control["reassessments"][review_kind]["accepted"] = True
        running = _persist_composition_control(
            status,
            snapshot,
            running,
            project_config,
            attempted=True,
            fingerprint=digest,
            review_kind=review_kind,
            reverification=control,
        )
        return (None if result.composition.fallback else result), running
    except (OSError, ValueError, KeyError, DeliveryProgressLockError):
        raise DeliveryIntegrationReviewAgentError(
            "delivery_verification.evidence_unavailable", "Composition evidence is unavailable.", []
        ) from None


def _persist_composition_control(
    status: DeliveryStatusResult,
    snapshot: DeliveryVerificationSnapshot,
    running: DeliveryVerificationRecord,
    cfg: dict[str, Any],
    *,
    attempted: bool,
    fingerprint: str | None = None,
    review_kind: str = "semantic",
    reverification: dict[str, Any] | None = None,
) -> DeliveryVerificationRecord:
    prefix = "" if review_kind == "semantic" else "security_"
    changes = {prefix + "composition_attempted": attempted, prefix + "composition_evidence_fingerprint": fingerprint}
    if reverification is not None:
        changes["reverification"] = reverification
    return _update_candidate_record(status, snapshot, running, cfg, changes)


def _run_review(
    reviewer: DeliveryIntegrationReviewAgent,
    *,
    worktree: Path,
    kind: str,
    source_task: str,
    plan_context: dict[str, Any],
    validation: DeliveryVerificationValidationResult,
    identity: DeliveryVerificationIdentity,
    known_unit_ids: set[str],
    known_obligation_ids: set[str],
    evidence_path: Path,
    audit_root: Path,
    guard_attempts: bool = False,
) -> DeliveryIntegrationReviewResult:
    composing = "checkpoint_composition" in plan_context and (
        kind == "semantic" or plan_context["checkpoint_composition"].get("compact")
    )
    guarded = composing or guard_attempts
    review_before = _review_snapshot(worktree, reviewer.project_config) if guarded else None

    def audit_review_attempt(prompt: str, attempts: list[DeliveryIntegrationReviewAttempt]) -> None:
        if not _review_workspace_unchanged(worktree, reviewer.project_config, review_before) or not _candidate_is_clean(
            worktree, identity.candidate_commit
        ):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.readonly_mutation", "Reviewer changed its workspace.", attempts
            )
        if not _safe_append_audit(
            evidence_path,
            {
                "event": "composition_call" if composing else "review_call",
                "gate_id": identity.gate_id,
                "node_id": plan_context.get("verification_node", {}).get("id", "root"),
                "prompt": prompt,
                "prior_attempts": _attempt_audit(attempts),
            },
            project_root=audit_root,
        ):
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.audit_unavailable", "Review prompt audit is unavailable.", attempts
            )

    try:
        result = reviewer.review(
            cwd=worktree,
            review_kind=kind,
            source_task=source_task,
            plan_context=plan_context,
            validation_summary=validation.to_review_dict(
                reviewer.project_config,
                project_root=worktree,
                include_final_checks=not bool(plan_context.get("verification_node")),
            ),
            candidate_commit=identity.candidate_commit,
            candidate_tree=identity.candidate_tree,
            known_unit_ids=known_unit_ids,
            known_obligation_ids=known_obligation_ids,
            **({"before_attempt": audit_review_attempt} if guarded else {}),
        )
    except (KeyboardInterrupt, SystemExit):
        boundary = guarded and (
            not _review_workspace_unchanged(worktree, reviewer.project_config, review_before)
            or not _candidate_is_clean(worktree, identity.candidate_commit)
        )
        _safe_append_audit(
            evidence_path,
            {
                "event": "review_interrupted",
                "kind": kind,
                "gate_id": identity.gate_id,
                "node_id": plan_context.get("verification_node", {}).get("id", "root"),
                "readonly_violation": boundary,
            },
            project_root=audit_root,
        )
        if boundary:
            raise DeliveryIntegrationReviewAgentError(
                "delivery_verification.readonly_mutation", "Interrupted reviewer changed its workspace.", []
            ) from None
        raise
    except DeliveryIntegrationReviewAgentError as exc:
        if guarded and (
            not _review_workspace_unchanged(worktree, reviewer.project_config, review_before)
            or not _candidate_is_clean(worktree, identity.candidate_commit)
        ):
            exc = DeliveryIntegrationReviewAgentError(
                "delivery_verification.readonly_mutation", "Reviewer changed its workspace.", exc.attempts
            )
        review_evidence = {
            "event": "review_failed",
            "gate_id": identity.gate_id,
            "node_id": plan_context.get("verification_node", {}).get("id", "root"),
            "kind": kind,
            "code": exc.code,
            "attempts": _attempt_audit(exc.attempts),
            "usage": reviewer.consume_usage_records(),
        }
        if not _safe_append_audit(evidence_path, review_evidence, project_root=audit_root):
            raise _ReviewAuditUnavailable(
                review_evidence,
                phase_status="blocked",
                finding_count=0,
                obligation_satisfied_count=0,
                obligation_gap_count=0,
                boundary_stop_code=exc.code if exc.code == "delivery_verification.readonly_mutation" else None,
            ) from exc
        raise exc
    review_evidence = {
        "event": f"{kind}_review",
        "gate_id": identity.gate_id,
        "node_id": plan_context.get("verification_node", {}).get("id", "root"),
        "assessment": result.assessment.to_dict(),
        "attempts": _attempt_audit(result.attempts),
        "usage": reviewer.consume_usage_records(),
    }
    if result.composition is not None:
        review_evidence["composition"] = {
            "checkpoint_results": result.composition.checkpoints,
            "full_review_required": result.composition.fallback,
        }
    if not _safe_append_audit(evidence_path, review_evidence, project_root=audit_root):
        # The caller's normal post-review check is skipped by this exception.
        # Capture a physical violation before the detached workspace is removed.
        boundary = guarded and (
            not _review_workspace_unchanged(worktree, reviewer.project_config, review_before)
            or not _candidate_is_clean(worktree, identity.candidate_commit)
        )
        if boundary:
            review_evidence["readonly_violation"] = True
        obligation_satisfied_count = sum(
            obligation.outcome == "satisfied" for obligation in result.assessment.obligation_results
        )
        raise _ReviewAuditUnavailable(
            review_evidence,
            phase_status="blocked" if boundary else "approved" if result.assessment.approved else "rejected",
            finding_count=len(result.assessment.findings),
            obligation_satisfied_count=obligation_satisfied_count,
            obligation_gap_count=len(result.assessment.obligation_results) - obligation_satisfied_count,
            boundary_stop_code="delivery_verification.readonly_mutation" if boundary else None,
        )
    return result


def _persist_terminal_if_current(
    path: str | Path,
    root: Path,
    plan_id: str,
    project_config: dict[str, Any],
    identity: DeliveryVerificationIdentity,
    terminal: DeliveryVerificationRecord,
    events_path: Path,
    *,
    node_id: str = "root",
) -> DeliveryVerificationRecord | None:
    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.complete"):
            progress_path = delivery_progress_path(root, plan_id)
            progress, errors = read_delivery_progress(progress_path, plan_id=plan_id)
            record = (
                (progress.verification if node_id == "root" else progress.checkpoint_verifications.get(node_id))
                if progress
                else None
            )
            if progress is None or errors or record is None:
                return None
            if (
                record.gate_id != identity.gate_id
                or record.attempt != terminal.attempt
                or (record.status != "running" and not delivery_verification_is_boundary_stop(record))
            ):
                return None
            # Reservations and accepted composition survive interruption and outer error paths.
            terminal = replace(
                terminal,
                reverification=record.reverification,
                composition_attempted=record.composition_attempted,
                composition_evidence_fingerprint=record.composition_evidence_fingerprint,
                security_composition_attempted=record.security_composition_attempted,
                security_composition_evidence_fingerprint=record.security_composition_evidence_fingerprint,
            )
            if terminal.reverification:
                from core.delivery_verification_model import parse_delivery_verification_record

                for entry in terminal.reverification["children"].values():
                    child = parse_delivery_verification_record(entry["verification"])
                    if delivery_verification_is_boundary_stop(child):
                        terminal = replace(
                            terminal, status="blocked", stop_code=child.stop_code, security_status=child.security_status
                        )
                        break
            # A boundary violation belongs to the attempt even when authority
            # changes while its provider is running. Never replace it with stale.
            retain_boundary = delivery_verification_is_boundary_stop(terminal)
            if not retain_boundary:
                try:
                    status = verification_node_status(get_delivery_status(path, project_root=root), node_id)
                    current_identity = build_delivery_verification_identity(
                        status,
                        project_config,
                        candidate_commit=identity.candidate_commit,
                    )
                except (OSError, RuntimeError, ValueError):
                    return None
                if current_identity != identity or not _assembly_ref_matches(root, status, identity.candidate_commit):
                    return None
            if node_id != "root" and terminal.passed:
                try:
                    from core.delivery_checkpoint_applicability import validate_node_evidence

                    validate_node_evidence(status, terminal, node_id=node_id)
                except (OSError, ValueError):
                    terminal = replace(
                        terminal,
                        status="blocked",
                        stop_code="delivery_checkpoint.evidence_unavailable",
                        checkpoint_evidence_fingerprint=None,
                    )
                    if terminal.evidence_path:
                        _safe_append_audit(
                            root / terminal.evidence_path,
                            {"event": "evidence_unavailable", "record": terminal.to_dict()},
                            project_root=root,
                        )
            if node_id == "root" and terminal.passed and status.plan.checkpoints:
                try:
                    validate_root_evidence(status, terminal)
                except (OSError, ValueError):
                    terminal = replace(
                        terminal,
                        status="blocked",
                        stop_code="delivery_verification.evidence_unavailable",
                        root_evidence_fingerprint=None,
                    )
                    if terminal.evidence_path:
                        _safe_append_audit(
                            root / terminal.evidence_path,
                            {"event": "evidence_unavailable", "record": terminal.to_dict()},
                            project_root=root,
                        )
            if delivery_verification_is_boundary_stop(record):
                # A descendant already published this boundary under the same
                # captured attempt. Finish its audit without readmitting an old
                # candidate or requiring assembly to remain current.
                terminal = replace(
                    terminal, status=record.status, stop_code=record.stop_code, security_status=record.security_status
                )
                progress = (
                    replace(progress, verification=terminal)
                    if node_id == "root"
                    else replace(
                        progress, checkpoint_verifications={**progress.checkpoint_verifications, node_id: terminal}
                    )
                )
            else:
                progress = mark_delivery_verification(progress, terminal, node_id=node_id)
            write_delivery_progress(progress_path, progress)
            append_delivery_progress_event(
                events_path,
                DeliveryProgressEvent(
                    verification_node=node_id if node_id != "root" else None,
                    plan_id=plan_id,
                    event_type=f"verification.{terminal.status}",
                    timestamp=terminal.completed_at or _now(),
                    commit=identity.candidate_commit,
                ),
            )
            return terminal
    except DeliveryProgressLockError:
        return None


def _safe_append_progress_event(path: Path, event: DeliveryProgressEvent) -> bool:
    try:
        append_delivery_progress_event(path, event)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


def _ensure_verification_progress_event(
    events_path: Path,
    plan_id: str,
    record: DeliveryVerificationRecord,
    *,
    node_id: str = "root",
) -> bool:
    timestamp = record.started_at if record.status == "running" else record.completed_at
    if record.status == "pending" or timestamp is None:
        return True
    event_type = f"verification.{record.status}"
    try:
        if events_path.exists():
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if (
                        isinstance(event, dict)
                        and event.get("plan_id") == plan_id
                        and event.get("verification_node") == (node_id if node_id != "root" else None)
                        and event.get("event_type") == event_type
                        and event.get("timestamp") == timestamp
                        and event.get("commit") == record.candidate_commit
                    ):
                        return True
        append_delivery_progress_event(
            events_path,
            DeliveryProgressEvent(
                verification_node=node_id if node_id != "root" else None,
                plan_id=plan_id,
                event_type=event_type,
                timestamp=timestamp,
                commit=record.candidate_commit,
            ),
        )
        return True
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False


def _preflight_result(status, readiness, project_config: dict[str, Any] | None = None) -> DeliveryVerifyResult | None:
    if not status.valid or not readiness.ready:
        return DeliveryVerifyResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=False,
            ready=False,
            succeeded=False,
            status="blocked",
            obligation_count=_status_obligation_count(status),
            stop_code="delivery_verification.not_ready",
            next_action="resolve_delivery_verification_readiness",
            errors=tuple(dict.fromkeys([*status.errors, *readiness.errors])),
            warnings=tuple(readiness.warnings),
        )
    if readiness.required and not verification_scope_complete(status):
        issue = DeliveryPlanIssue(
            "error",
            "delivery_verification.plan_not_done",
            "All active delivery units must complete before final verification.",
        )
        return DeliveryVerifyResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=True,
            ready=False,
            succeeded=False,
            status="blocked",
            obligation_count=_status_obligation_count(status),
            stop_code=issue.code,
            next_action="run_delivery_units",
            errors=(issue,),
            warnings=tuple(readiness.warnings),
        )
    if readiness.required and status.plan and status.plan.checkpoints:
        from core.delivery_checkpoints import checkpoint_barrier_issue, checkpoint_pass_is_usable
        from core.delivery_checkpoint_model import checkpoint_children
        from core.delivery_handoff import load_delivery_dependency_handoffs

        if status.verification and delivery_verification_is_boundary_stop(status.verification):
            boundary = status.verification
            if status.verification_node != "root" and boundary.security_status == "rejected":
                # A child security verdict is terminal, not parent semantic
                # repair. Do not require repair inputs or descendant sidecars.
                return _blocked_result(status, "delivery_verification.security_rejected")
            if boundary.reverification:
                from core.delivery_verification_model import parse_delivery_verification_record

                for entry in boundary.reverification["children"].values():
                    child = parse_delivery_verification_record(entry["verification"])
                    if delivery_verification_is_boundary_stop(child):
                        boundary = child
                        break
            return _blocked_result(status, boundary.stop_code or "delivery_verification.security_rejected")
        issue = checkpoint_barrier_issue(status, project_config)
        if issue is not None and issue.code == "delivery_checkpoint.handoff_stale":
            return _blocked_result(status, issue.code, [issue])
        if any(
            not checkpoint_pass_is_usable(status, checkpoint, project_config)
            for checkpoint in checkpoint_children(status.plan.checkpoints, status.verification_node)
        ):
            return _blocked_result(status, "delivery_checkpoint.required")
        scope = DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
        _, issues = load_delivery_dependency_handoffs(status, list(scope.unit_ids), Path(status.project_root))
        if issues:
            return _blocked_result(status, issues[0].code, issues)
    if status.verification and status.verification.reverification:
        record = status.verification
        try:
            from core.delivery_reverification import validate_reverification, reverification_budget_exhausted
            from core.delivery_composition import load_composition

            if (
                project_config
                and build_delivery_verification_identity(
                    status, project_config, candidate_commit=status.assembled_commit
                ).gate_id
                == record.gate_id
            ):
                validate_reverification(status, record, project_config)
                for role, digest in (
                    ("semantic", record.composition_evidence_fingerprint),
                    ("security", record.security_composition_evidence_fingerprint),
                ):
                    if digest:
                        load_composition(status, record, review_kind=role)
                if reverification_budget_exhausted(status, record):
                    return _blocked_result(status, "delivery_verification.reverification_budget_exhausted")
        except (OSError, ValueError, KeyError, TypeError):
            return _blocked_result(status, "delivery_verification.evidence_unavailable")
    from core.delivery_candidate_repair import (
        CANDIDATE_REPAIR_REQUIRED,
        resolve_candidate_repair,
        parent_repair_authority,
    )

    if status.verification and status.verification.stop_code in {
        CANDIDATE_REPAIR_REQUIRED,
        "delivery_verification.repair_required",
    }:
        try:
            current = build_delivery_verification_identity(
                status, project_config or {}, candidate_commit=status.assembled_commit
            )
            if current.gate_id == status.verification.gate_id and (
                status.verification.stop_code == CANDIDATE_REPAIR_REQUIRED
                or parent_repair_authority(status, status.verification) is not None
            ):
                resolve_candidate_repair(status, project_config or {})
                return _result_from_record(status, status.verification, succeeded=False, next_action="run_delivery")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return _blocked_result(status, "delivery_verification.evidence_unavailable")
    if readiness.required and not status.progress_exists:
        return _blocked_result(status, "delivery_verification.progress_missing")
    if (
        status.verification_node == "root"
        and status.plan
        and status.plan.checkpoints
        and status.verification
        and status.verification.passed
        and status.assembled_commit == status.verification.candidate_commit
    ):
        try:
            identity = build_delivery_verification_identity(
                status, project_config or {}, candidate_commit=status.assembled_commit
            )
            if _record_matches_identity(
                status.verification, identity, obligation_count=_status_obligation_count(status)
            ):
                validate_root_evidence(status, status.verification)
        except (OSError, RuntimeError, ValueError):
            return _blocked_result(status, "delivery_verification.evidence_unavailable")
    return None


def _persist_stale_attempt(
    root: Path,
    plan_id: str,
    identity: DeliveryVerificationIdentity,
    stale: DeliveryVerificationRecord,
    events_path: Path,
    *,
    node_id: str = "root",
) -> None:
    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.stale"):
            progress_path = delivery_progress_path(root, plan_id)
            progress, errors = read_delivery_progress(progress_path, plan_id=plan_id)
            record = (
                (progress.verification if node_id == "root" else progress.checkpoint_verifications.get(node_id))
                if progress
                else None
            )
            if (
                progress is None
                or errors
                or record is None
                or record.gate_id != identity.gate_id
                or record.attempt != stale.attempt
                or record.status != "running"
                or progress.assembly_status != "ready"
                or progress.assembled_commit != identity.candidate_commit
            ):
                return
            progress = mark_delivery_verification(progress, stale, node_id=node_id)
            write_delivery_progress(progress_path, progress)
            append_delivery_progress_event(
                events_path,
                DeliveryProgressEvent(
                    verification_node=node_id if node_id != "root" else None,
                    plan_id=plan_id,
                    event_type="verification.stale",
                    timestamp=stale.completed_at or _now(),
                    commit=identity.candidate_commit,
                ),
            )
    except (DeliveryProgressLockError, OSError, RuntimeError, ValueError):
        return


def _blocked_result(status, code: str, errors: list[DeliveryPlanIssue] | None = None) -> DeliveryVerifyResult:
    issues = list(errors or [])
    if not issues:
        issues.append(DeliveryPlanIssue("error", code, "Delivery verification is blocked."))
    return DeliveryVerifyResult(
        plan_path=status.plan_path,
        project_root=status.project_root,
        valid=status.valid,
        ready=False,
        succeeded=False,
        status="blocked",
        obligation_count=_status_obligation_count(status),
        stop_code=code,
        next_action="resolve_delivery_verification_blocker",
        errors=tuple(issues),
        warnings=tuple(status.warnings),
    )


def _status_obligation_count(status) -> int:
    plan = getattr(status, "plan", None)
    return (
        len(DeliveryVerificationScope.from_plan(plan, getattr(status, "verification_node", "root")).obligation_ids)
        if plan is not None
        else 0
    )


def _result_from_record(
    status, record: DeliveryVerificationRecord, *, succeeded: bool, next_action: str
) -> DeliveryVerifyResult:
    return DeliveryVerifyResult(
        plan_path=status.plan_path,
        project_root=status.project_root,
        valid=status.valid,
        ready=record.passed,
        succeeded=succeeded,
        status=record.status,
        gate_id=record.gate_id,
        candidate_commit=record.candidate_commit,
        candidate_tree=record.candidate_tree,
        attempt=record.attempt,
        semantic_status=record.semantic_status,
        security_required=record.security_required,
        security_status=record.security_status,
        validation_reused=record.validation_reused,
        validation_executed=record.validation_executed,
        finding_count=record.finding_count,
        obligation_count=record.obligation_count,
        obligation_satisfied_count=record.obligation_satisfied_count,
        obligation_gap_count=record.obligation_gap_count,
        stop_code=record.stop_code,
        next_action=next_action,
        warnings=tuple(status.warnings),
    )


def _record_for_identity(identity: DeliveryVerificationIdentity, **values: Any) -> DeliveryVerificationRecord:
    return DeliveryVerificationRecord(
        schema_version=1,
        gate_id=identity.gate_id,
        candidate_commit=identity.candidate_commit,
        candidate_tree=identity.candidate_tree,
        source_fingerprint=identity.source_fingerprint,
        plan_fingerprint=identity.plan_fingerprint,
        completed_scope_fingerprint=identity.completed_scope_fingerprint,
        config_fingerprint=identity.config_fingerprint,
        policy_fingerprint=identity.policy_fingerprint,
        **values,
    )


def _record_matches_identity(
    record: DeliveryVerificationRecord,
    identity: DeliveryVerificationIdentity,
    *,
    obligation_count: int,
) -> bool:
    return delivery_verification_covers_obligations(record, obligation_count) and all(
        getattr(record, key) == getattr(identity, key)
        for key in (
            "gate_id",
            "candidate_commit",
            "candidate_tree",
            "source_fingerprint",
            "plan_fingerprint",
            "completed_scope_fingerprint",
            "config_fingerprint",
            "policy_fingerprint",
        )
    )


def _read_bound_source_task(
    root: Path,
    snapshot: DeliveryVerificationSnapshot,
    project_config: dict[str, Any],
) -> str:
    source = snapshot.scope.source_task
    if source is None:
        raise ValueError("delivery source task is unavailable")
    source_path = root / source.path
    if delivery_verification_source_task_is_private(root, source_path, source.path, project_config):
        raise _PrivateSourceTaskError("delivery source task references private data")
    source_text = source_path.read_text(encoding="utf-8")
    source_fingerprint = "sha256:" + sha256(source_text.encode("utf-8")).hexdigest()
    if source_fingerprint != snapshot.identity.source_fingerprint:
        raise ValueError("delivery source task changed during verification capture")
    return source_text


class _PrivateSourceTaskError(ValueError):
    pass


class _ReviewAuditUnavailable(RuntimeError):
    def __init__(
        self,
        review_evidence: dict[str, Any],
        *,
        phase_status: str,
        finding_count: int,
        obligation_satisfied_count: int,
        obligation_gap_count: int,
        boundary_stop_code: str | None = None,
    ) -> None:
        super().__init__("Delivery verification review evidence could not be persisted.")
        self.review_evidence = review_evidence
        self.phase_status = phase_status
        self.finding_count = finding_count
        self.obligation_satisfied_count = obligation_satisfied_count
        self.obligation_gap_count = obligation_gap_count
        self.boundary_stop_code = boundary_stop_code


class _GateValidationAuditUnavailable(RuntimeError):
    def __init__(self, terminal: DeliveryVerificationRecord, validation_evidence: dict[str, Any]) -> None:
        super().__init__("Delivery verification validation evidence requires terminal fallback persistence.")
        self.terminal = terminal
        self.validation_evidence = validation_evidence


class _GateReviewAuditUnavailable(RuntimeError):
    def __init__(self, terminal: DeliveryVerificationRecord, review_evidence: dict[str, Any]) -> None:
        super().__init__("Delivery verification review evidence requires terminal fallback persistence.")
        self.terminal = terminal
        self.review_evidence = review_evidence


def _copy_environment_files(root: Path, worktree: Path, project_config: dict[str, Any]) -> list[Path]:
    copied: list[Path] = []
    for relative in build_tool_class(project_config).env_files():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise DetachedWorktreeError("Delivery verification environment-file policy is invalid.")
        if copy_worktree_environment_file(root / rel, worktree / rel, worktree):
            copied.append(rel)
    return copied


def _candidate_config_matches_capture(root: Path, worktree: Path, project_config: dict[str, Any]) -> bool:
    expected = project_config.get("_config_source_fingerprint")
    raw_path = project_config.get("_config_path")
    if not isinstance(expected, str) or not isinstance(raw_path, str):
        return True
    try:
        source_path = Path(raw_path).resolve(strict=False)
        source_git_root = _git_top_level(root)
        candidate_git_root = _git_top_level(worktree)
    except (OSError, RuntimeError):
        return False
    try:
        relative = source_path.relative_to(source_git_root)
    except ValueError:
        return True
    candidate_path = candidate_git_root
    for part in relative.parts:
        candidate_path /= part
        try:
            if candidate_path.is_symlink():
                return False
        except OSError:
            return False
    try:
        source = candidate_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return f"sha256:{sha256(source.encode('utf-8')).hexdigest()}" == expected


def _git_top_level(path: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("delivery verification repository root is unavailable")
    return Path(result.stdout.strip()).resolve(strict=True)


def _remove_copied_environment_files(worktree: Path, relative_paths: list[Path]) -> None:
    root = worktree.resolve(strict=True)
    for relative in relative_paths:
        destination = worktree / relative
        current = worktree
        try:
            for part in relative.parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise ValueError
                current.resolve(strict=True).relative_to(root)
            if destination.is_dir() and not destination.is_symlink():
                raise ValueError
            destination.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DetachedWorktreeError(
                "Delivery verification could not remove copied environment data before review."
            ) from exc


def _candidate_is_clean(worktree: Path, commit: str) -> bool:
    git_env = delivery_verification_git_env()
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=git_env,
    )
    staged, unstaged, untracked, error = current_worktree_changes(worktree, git_env=git_env)
    return (
        head.returncode == 0
        and head.stdout.strip() == commit
        and not error
        and not staged
        and not unstaged
        and not untracked
    )


def _review_snapshot(
    worktree: Path,
    project_config: dict[str, Any],
    *,
    exclude_ephemeral_paths: bool = False,
):
    repository_root = _candidate_repository_root(worktree)
    project_prefix = worktree.relative_to(repository_root)
    allowed_read_paths = delivery_verification_allowed_read_paths(project_config)
    lexical_read_roots = tuple(worktree / Path(path) for path in allowed_read_paths)
    resolved_read_roots = tuple(root.resolve(strict=False) for root in lexical_read_roots)
    tool = create_build_tool(
        Sandbox(worktree, allowed_write_paths=["."], allowed_read_paths=["."]), worktree, project_config
    )
    classifier = getattr(tool, "is_ephemeral_build_path", None)

    def exclude_ephemeral(path: str) -> bool:
        try:
            project_path = Path(path) if project_prefix == Path(".") else Path(path).relative_to(project_prefix)
        except ValueError:
            return False
        return bool(callable(classifier) and classifier(project_path.as_posix()))

    return snapshot_delivery_scope_files(
        repository_root,
        validate_symlink=lambda path, _target: _review_symlink_is_safe(
            repository_root,
            path,
            lexical_read_roots=lexical_read_roots,
            resolved_read_roots=resolved_read_roots,
        ),
        symlink_roots=(".",),
        exclude_ephemeral=exclude_ephemeral if exclude_ephemeral_paths else None,
    )


def _review_workspace_unchanged(
    worktree: Path,
    project_config: dict[str, Any],
    before,
    *,
    exclude_ephemeral_paths: bool = False,
) -> bool:
    after = _review_snapshot(
        worktree,
        project_config,
        exclude_ephemeral_paths=exclude_ephemeral_paths,
    )
    return not detect_validation_artifacts(before, after)


def _candidate_links_are_safe(worktree: Path, project_config: dict[str, Any]) -> bool:
    _review_snapshot(worktree, project_config)
    return True


def _candidate_repository_root(project_root: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise DeliveryScopeSnapshotError("Delivery verification could not resolve the candidate repository root.")
    try:
        repository_root = Path(result.stdout.strip()).resolve(strict=True)
        project_root.resolve(strict=True).relative_to(repository_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryScopeSnapshotError("Delivery verification candidate project root is invalid.") from exc
    return repository_root


def _worktree_symlink_is_internal(worktree: Path, relative_path: str) -> bool:
    try:
        (worktree / Path(relative_path)).resolve(strict=False).relative_to(worktree.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _review_symlink_is_safe(
    repository_root: Path,
    relative_path: str,
    *,
    lexical_read_roots: tuple[Path, ...],
    resolved_read_roots: tuple[Path, ...],
) -> bool:
    symlink_path = repository_root / Path(relative_path)
    if not _worktree_symlink_is_internal(repository_root, relative_path):
        return False
    if not any(symlink_path == root or root in symlink_path.parents for root in lexical_read_roots):
        return True
    try:
        target = symlink_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return any(target == root or root in target.parents for root in resolved_read_roots)


def _assembly_ref_matches(root: Path, status, commit: str) -> bool:
    if status.plan is None:
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{status.plan.final_branch}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    return result.returncode == 0 and result.stdout.strip() == commit


def _review_blocked(
    running: DeliveryVerificationRecord,
    validation: DeliveryVerificationValidationResult | None,
    code: str,
    *,
    semantic_status: str = "not_run",
    security_status: str = "not_run",
    obligation_satisfied_count: int = 0,
    obligation_gap_count: int = 0,
) -> DeliveryVerificationRecord:
    return replace(
        running,
        status="blocked",
        semantic_status=semantic_status,
        security_status=security_status,
        validation_reused=validation.reused if validation is not None else False,
        validation_executed=validation.executed if validation is not None else False,
        obligation_satisfied_count=obligation_satisfied_count,
        obligation_gap_count=obligation_gap_count,
        stop_code=code,
        completed_at=_now(),
    )


def _validation_audit(result: DeliveryVerificationValidationResult) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "reused": result.reused,
        "executed": result.executed,
        "reused_child_task_id": result.reused_child_task_id,
        "stop_code": result.stop_code,
        "records": [record.to_dict() for record in result.records],
    }


def _attempt_audit(attempts) -> list[dict[str, Any]]:
    return [
        {
            "attempt": attempt.attempt,
            "prompt": attempt.prompt,
            "output": attempt.output,
            "parse_error": attempt.parse_error,
        }
        for attempt in attempts
    ]


def _append_audit(path: Path, value: dict[str, Any], *, project_root: Path) -> None:
    root, relative = _audit_location(project_root, path)
    record = {
        "schema_version": 1,
        "recorded_at": _now(),
        "sikula_version": sikula_version(),
        **value,
    }
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        _prepare_audit_parent(root, relative)
        audit_path = root / relative
        fd = os.open(audit_path, flags, 0o600)
        current = os.lstat(audit_path)
    else:
        fd, current = _open_audit_file(root, relative, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_nlink != 1
        ):
            raise OSError("Delivery verification audit path has an unsafe filesystem identity.")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        with handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _safe_append_audit(path: Path, value: dict[str, Any], *, project_root: Path) -> bool:
    try:
        _append_audit(path, value, project_root=project_root)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _audit_location(project_root: Path, path: Path) -> tuple[Path, Path]:
    try:
        root = project_root.resolve(strict=True)
        relative = path.absolute().relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError("Delivery verification audit path is outside the project.") from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise OSError("Delivery verification audit path is invalid.")
    return root, relative


def _prepare_audit_parent(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("Delivery verification audit parent has an unsafe filesystem identity.")
        try:
            current.resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise OSError("Delivery verification audit parent escapes the project.") from exc


def _open_audit_file(root: Path, relative: Path, flags: int) -> tuple[int, os.stat_result]:
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            try:
                opened = os.fstat(child_fd)
                current = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise OSError("Delivery verification audit parent has an unsafe filesystem identity.")
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd

        fd = os.open(relative.name, flags, 0o600, dir_fd=parent_fd)
        try:
            current = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except BaseException:
            os.close(fd)
            raise
        return fd, current
    finally:
        os.close(parent_fd)


def _public_issue(issue: DeliveryPlanIssue) -> dict[str, Any]:
    return {
        "severity": issue.severity,
        "code": sanitize_delivery_public_metadata(issue.code),
        "message": sanitize_delivery_public_metadata(issue.message),
        **({"path": _bounded_public_path(issue.path)} if issue.path else {}),
    }


def _bounded_public_path(value: str) -> str:
    projected = sanitize_delivery_public_metadata(value)
    if projected is None or len(projected) > 500:
        return REDACTED_DELIVERY_PUBLIC_METADATA
    return projected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
