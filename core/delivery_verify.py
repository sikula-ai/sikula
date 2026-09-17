from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from agents.delivery_integration_review_agent import (
    DeliveryIntegrationReviewAgent,
    DeliveryIntegrationReviewAgentError,
    DeliveryIntegrationReviewResult,
)
from core.delivery_finalize import assemble_delivery_candidate
from core.llm_client import LLMConfigurationError
from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import (
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
from core.delivery_verification import (
    DeliveryVerificationIdentity,
    build_delivery_verification_identity,
    check_delivery_verification_readiness,
    delivery_verification_allowed_read_paths,
    delivery_verification_plan_context,
    delivery_verification_source_task_is_private,
)
from core.delivery_verification_model import (
    DeliveryVerificationRecord,
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


DELIVERY_VERIFY_RESULT_SCHEMA_VERSION = 1
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
) -> DeliveryVerifyResult:
    status = get_delivery_status(path, project_root=project_root)
    readiness = check_delivery_verification_readiness(status, project_config)
    blocked = _preflight_result(status, readiness)
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
    evidence_path = progress_path.parent / "verification.jsonl"
    evidence_reference = evidence_path.relative_to(root).as_posix()

    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.capture"):
            status = get_delivery_status(path, project_root=root)
            readiness = check_delivery_verification_readiness(status, project_config)
            blocked = _preflight_result(status, readiness)
            if blocked is not None:
                return blocked
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
            existing = progress.verification
            if existing is not None and not _ensure_verification_progress_event(events_path, plan_id, existing):
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
                    and _record_matches_identity(existing, existing_identity)
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
            status = get_delivery_status(path, project_root=root)
            identity = build_delivery_verification_identity(
                status,
                project_config,
                candidate_commit=candidate_commit,
            )
            try:
                source_task = _read_bound_source_task(root, status, identity, project_config)
            except _PrivateSourceTaskError:
                return _blocked_result(status, "delivery_verification.source_private")
            except (OSError, UnicodeError, ValueError):
                return _blocked_result(status, "delivery_verification.source_changed")
            existing = progress.verification
            if existing and existing.passed and _record_matches_identity(existing, identity):
                return _result_from_record(status, existing, succeeded=True, next_action="finalize_delivery")
            attempt = existing.attempt + 1 if existing and existing.gate_id == identity.gate_id else 1
            running = _record_for_identity(
                identity,
                status="running",
                attempt=attempt,
                security_required=readiness.security_required,
                evidence_path=evidence_reference,
                started_at=_now(),
            )
            progress = mark_delivery_verification(progress, running)
            write_delivery_progress(progress_path, progress)
            running_event = DeliveryProgressEvent(
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
                progress = mark_delivery_verification(progress, blocked_record)
                write_delivery_progress(progress_path, progress)
                _safe_append_progress_event(
                    events_path,
                    DeliveryProgressEvent(
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
                progress = mark_delivery_verification(progress, blocked_record)
                write_delivery_progress(progress_path, progress)
                _safe_append_progress_event(
                    events_path,
                    DeliveryProgressEvent(
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
        _persist_terminal_if_current(path, root, plan_id, project_config, identity, terminal, events_path)
        return _result_from_record(
            get_delivery_status(path, project_root=root),
            terminal,
            succeeded=False,
            next_action="resolve_delivery_verification_blocker",
        )
    deferred_review_evidence: dict[str, Any] | None = None
    try:
        terminal = _execute_gate(
            root=root,
            status=status,
            project_config=project_config,
            state_store=state_store,
            identity=identity,
            running=running,
            source_task=source_task,
            evidence_path=evidence_path,
            semantic_reviewer=semantic_reviewer,
            security_reviewer=security_reviewer,
        )
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
        _persist_terminal_if_current(path, root, plan_id, project_config, identity, terminal, events_path)
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
    if deferred_review_evidence is not None:
        terminal_audit["review_evidence"] = deferred_review_evidence
    if not _safe_append_audit(evidence_path, terminal_audit, project_root=root):
        terminal = replace(
            terminal,
            status="blocked",
            stop_code="delivery_verification.audit_unavailable",
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
    )
    if not current:
        stale = replace(
            terminal,
            status="stale",
            stop_code="delivery_verification.candidate_changed",
            completed_at=_now(),
        )
        _persist_stale_attempt(root, plan_id, identity, stale, events_path)
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
    return _result_from_record(
        get_delivery_status(path, project_root=root),
        terminal,
        succeeded=terminal.passed,
        next_action=(
            "finalize_delivery" if terminal.passed else delivery_verification_recovery_action(terminal.stop_code)
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
    identity: DeliveryVerificationIdentity,
    running: DeliveryVerificationRecord,
    source_task: str,
    evidence_path: Path,
    semantic_reviewer: DeliveryIntegrationReviewAgent,
    security_reviewer: DeliveryIntegrationReviewAgent | None,
) -> DeliveryVerificationRecord:
    validation: DeliveryVerificationValidationResult | None = None
    active_review: str | None = None
    semantic_status = "not_run"
    security_status = "not_run"
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
            )
            validation_before = _review_snapshot(worktree, project_config, exclude_ephemeral_paths=True)
            validation = run_delivery_verification_validation(worktree, project_config, reusable=reusable)
            _append_audit(
                evidence_path,
                {"event": "validation", "result": _validation_audit(validation)},
                project_root=root,
            )
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

            try:
                semantic_reviewer.prepare_workspace(worktree)
            except LLMConfigurationError:
                return _review_blocked(
                    running,
                    validation,
                    "delivery_verification.reviewer_workspace_unavailable",
                    semantic_status="blocked",
                )
            plan_context = delivery_verification_plan_context(status)
            known_unit_ids = {unit.id for unit in status.plan.units if not unit.superseded}
            semantic_before = _review_snapshot(worktree, project_config)
            active_review = "semantic"
            semantic = _run_review(
                semantic_reviewer,
                worktree=worktree,
                kind="semantic",
                source_task=source_task,
                plan_context=plan_context,
                validation=validation,
                identity=identity,
                known_unit_ids=known_unit_ids,
                evidence_path=evidence_path,
                audit_root=root,
            )
            semantic_status = "approved" if semantic.assessment.approved else "rejected"
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
                return replace(
                    running,
                    status="failed",
                    semantic_status="rejected",
                    security_status="not_run",
                    validation_reused=validation.reused,
                    validation_executed=validation.executed,
                    finding_count=len(semantic.assessment.findings),
                    stop_code=f"delivery_verification.{semantic.assessment.disposition}",
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
                    )
                try:
                    security_reviewer.prepare_workspace(worktree)
                except LLMConfigurationError:
                    return _review_blocked(
                        running,
                        validation,
                        "delivery_verification.security_workspace_unavailable",
                        semantic_status=semantic_status,
                        security_status="blocked",
                    )
                security_before = _review_snapshot(worktree, project_config)
                active_review = "security"
                security = _run_review(
                    security_reviewer,
                    worktree=worktree,
                    kind="security",
                    source_task=source_task,
                    plan_context=plan_context,
                    validation=validation,
                    identity=identity,
                    known_unit_ids=known_unit_ids,
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
                        stop_code=f"delivery_verification.{security.assessment.disposition}",
                        completed_at=_now(),
                    )
                finding_count = len(security.assessment.findings)
            return replace(
                running,
                status="passed",
                semantic_status="approved",
                security_status=security_status,
                validation_reused=validation.reused,
                validation_executed=validation.executed,
                finding_count=finding_count,
                completed_at=_now(),
            )
    except DeliveryIntegrationReviewAgentError as exc:
        return _review_blocked(
            running,
            validation,
            exc.code,
            semantic_status="blocked" if active_review == "semantic" else semantic_status,
            security_status="blocked" if active_review == "security" else security_status,
        )
    except _ReviewAuditUnavailable as exc:
        phase_status = exc.phase_status
        terminal = _review_blocked(
            running,
            validation,
            "delivery_verification.audit_unavailable",
            semantic_status=phase_status if active_review == "semantic" else semantic_status,
            security_status=phase_status if active_review == "security" else security_status,
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
        )
    except DetachedWorktreeError:
        return _review_blocked(
            running,
            validation,
            "delivery_verification.worktree_unavailable",
            semantic_status=semantic_status,
            security_status=security_status,
        )


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
    evidence_path: Path,
    audit_root: Path,
) -> DeliveryIntegrationReviewResult:
    try:
        result = reviewer.review(
            cwd=worktree,
            review_kind=kind,
            source_task=source_task,
            plan_context=plan_context,
            validation_summary=validation.to_review_dict(reviewer.project_config, project_root=worktree),
            candidate_commit=identity.candidate_commit,
            candidate_tree=identity.candidate_tree,
            known_unit_ids=known_unit_ids,
        )
    except DeliveryIntegrationReviewAgentError as exc:
        review_evidence = {
            "event": "review_failed",
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
            ) from exc
        raise
    review_evidence = {
        "event": f"{kind}_review",
        "assessment": result.assessment.to_dict(),
        "attempts": _attempt_audit(result.attempts),
        "usage": reviewer.consume_usage_records(),
    }
    if not _safe_append_audit(evidence_path, review_evidence, project_root=audit_root):
        raise _ReviewAuditUnavailable(
            review_evidence,
            phase_status="approved" if result.assessment.approved else "rejected",
            finding_count=len(result.assessment.findings),
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
) -> bool:
    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.complete"):
            status = get_delivery_status(path, project_root=root)
            progress_path = delivery_progress_path(root, plan_id)
            progress, errors = read_delivery_progress(progress_path, plan_id=plan_id)
            if progress is None or errors or progress.verification is None:
                return False
            if (
                progress.verification.gate_id != identity.gate_id
                or progress.verification.attempt != terminal.attempt
                or progress.verification.status != "running"
            ):
                return False
            try:
                current_identity = build_delivery_verification_identity(
                    status,
                    project_config,
                    candidate_commit=identity.candidate_commit,
                )
            except (OSError, RuntimeError, ValueError):
                return False
            if current_identity != identity or not _assembly_ref_matches(root, status, identity.candidate_commit):
                return False
            progress = mark_delivery_verification(progress, terminal)
            write_delivery_progress(progress_path, progress)
            append_delivery_progress_event(
                events_path,
                DeliveryProgressEvent(
                    plan_id=plan_id,
                    event_type=f"verification.{terminal.status}",
                    timestamp=terminal.completed_at or _now(),
                    commit=identity.candidate_commit,
                ),
            )
            return True
    except DeliveryProgressLockError:
        return False


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
                        and event.get("event_type") == event_type
                        and event.get("timestamp") == timestamp
                        and event.get("commit") == record.candidate_commit
                    ):
                        return True
        append_delivery_progress_event(
            events_path,
            DeliveryProgressEvent(
                plan_id=plan_id,
                event_type=event_type,
                timestamp=timestamp,
                commit=record.candidate_commit,
            ),
        )
        return True
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False


def _preflight_result(status, readiness) -> DeliveryVerifyResult | None:
    if not status.valid or not readiness.ready:
        return DeliveryVerifyResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=False,
            ready=False,
            succeeded=False,
            status="blocked",
            stop_code="delivery_verification.not_ready",
            next_action="resolve_delivery_verification_readiness",
            errors=tuple(readiness.errors),
            warnings=tuple(readiness.warnings),
        )
    if readiness.required and status.status != "done":
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
            stop_code=issue.code,
            next_action="run_delivery_units",
            errors=(issue,),
            warnings=tuple(readiness.warnings),
        )
    if readiness.required and not status.progress_exists:
        return _blocked_result(status, "delivery_verification.progress_missing")
    return None


def _persist_stale_attempt(
    root: Path,
    plan_id: str,
    identity: DeliveryVerificationIdentity,
    stale: DeliveryVerificationRecord,
    events_path: Path,
) -> None:
    try:
        with acquire_delivery_progress_lock(root, plan_id, owner="delivery.verify.stale"):
            progress_path = delivery_progress_path(root, plan_id)
            progress, errors = read_delivery_progress(progress_path, plan_id=plan_id)
            if (
                progress is None
                or errors
                or progress.verification is None
                or progress.verification.gate_id != identity.gate_id
                or progress.verification.attempt != stale.attempt
                or progress.verification.status != "running"
                or progress.assembly_status != "ready"
                or progress.assembled_commit != identity.candidate_commit
            ):
                return
            progress = mark_delivery_verification(progress, stale)
            write_delivery_progress(progress_path, progress)
            append_delivery_progress_event(
                events_path,
                DeliveryProgressEvent(
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
        stop_code=code,
        next_action="resolve_delivery_verification_blocker",
        errors=tuple(issues),
        warnings=tuple(status.warnings),
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


def _record_matches_identity(record: DeliveryVerificationRecord, identity: DeliveryVerificationIdentity) -> bool:
    return all(
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
    status,
    identity: DeliveryVerificationIdentity,
    project_config: dict[str, Any],
) -> str:
    source = status.plan.source_task
    if source is None:
        raise ValueError("delivery source task is unavailable")
    source_path = root / source.path
    if delivery_verification_source_task_is_private(root, source_path, source.path, project_config):
        raise _PrivateSourceTaskError("delivery source task references private data")
    source_text = source_path.read_text(encoding="utf-8")
    source_fingerprint = "sha256:" + sha256(source_text.encode("utf-8")).hexdigest()
    if source_fingerprint != identity.source_fingerprint:
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
    ) -> None:
        super().__init__("Delivery verification review evidence could not be persisted.")
        self.review_evidence = review_evidence
        self.phase_status = phase_status
        self.finding_count = finding_count


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
) -> DeliveryVerificationRecord:
    return replace(
        running,
        status="blocked",
        semantic_status=semantic_status,
        security_status=security_status,
        validation_reused=validation.reused if validation is not None else False,
        validation_executed=validation.executed if validation is not None else False,
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
