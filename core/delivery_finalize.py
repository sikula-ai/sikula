from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
from typing import Any

from core.delivery_assembly import delivery_assembly_branch_is_symbolic
from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import (
    DeliveryProgressEvent,
    DeliveryProgressLockError,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    mark_delivery_assembly,
    make_delivery_progress_event,
    mark_delivery_finalized,
    read_delivery_progress,
    write_delivery_progress,
)
from core.worktree import branch_checked_out

_UNSET: Any = object()


@dataclass(frozen=True)
class DeliveryFinalizeResult:
    plan_path: str
    project_root: str | None
    valid: bool
    ready: bool
    dry_run: bool
    finalized: bool
    status: str | None
    progress_exists: bool
    final_branch: str | None
    final_commit: str | None
    progress_path: str | None
    events_path: str | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_path": self.plan_path,
            "project_root": self.project_root,
            "valid": self.valid,
            "ready": self.ready,
            "dry_run": self.dry_run,
            "finalized": self.finalized,
            "status": self.status,
            "progress_exists": self.progress_exists,
            "final_branch": self.final_branch,
            "final_commit": self.final_commit,
            "progress_path": self.progress_path,
            "events_path": self.events_path,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "message": self.message,
        }


@dataclass(frozen=True)
class _FinalizePreflight:
    result: DeliveryFinalizeResult
    plan_id: str | None


def preview_delivery_finalize(path: str | Path, *, project_root: Path | None = None) -> DeliveryFinalizeResult:
    preflight = _preflight_delivery_finalize(path, project_root=project_root, dry_run=True)
    return preflight.result


def finalize_delivery_plan(path: str | Path, *, project_root: Path | None = None) -> DeliveryFinalizeResult:
    preflight = _preflight_delivery_finalize(path, project_root=project_root, dry_run=False)
    if not preflight.result.ready or preflight.plan_id is None or preflight.result.project_root is None:
        return preflight.result

    root = Path(preflight.result.project_root).resolve()
    progress_path = delivery_progress_path(root, preflight.plan_id)
    events_path = delivery_events_path(root, preflight.plan_id)
    try:
        lock = acquire_delivery_progress_lock(root, preflight.plan_id, owner="delivery.finalize")
    except DeliveryProgressLockError:
        errors = [
            *preflight.result.errors,
            DeliveryPlanIssue("error", "delivery.locked", "Delivery progress is locked by another process."),
        ]
        return _replace_result(
            preflight.result,
            ready=False,
            finalized=False,
            errors=errors,
            message="Delivery finalize is blocked by an existing progress lock.",
        )

    with lock:
        preflight = _preflight_delivery_finalize(path, project_root=project_root, dry_run=False)
        result = preflight.result
        if not result.ready or preflight.plan_id is None or result.project_root is None:
            return result
        root = Path(result.project_root).resolve()
        branch = result.final_branch
        if branch is None:
            return _replace_result(
                result,
                ready=False,
                finalized=False,
                errors=[
                    *result.errors,
                    DeliveryPlanIssue(
                        "error",
                        "delivery.final_commit_missing",
                        "Delivery finalize could not determine a final branch commit.",
                    ),
                ],
                message="Delivery final branch is not ready.",
            )

        progress, progress_errors = read_delivery_progress(progress_path, plan_id=preflight.plan_id)
        if progress is None or progress_errors:
            return _replace_result(
                result,
                ready=False,
                finalized=False,
                errors=[*result.errors, *progress_errors],
                message="Delivery progress could not be updated.",
            )

        progress, commit, assembly_error = _assemble_for_finalize(
            root=root,
            status=get_delivery_status(path, project_root=project_root),
            progress=progress,
            progress_path=progress_path,
            events_path=events_path,
        )
        if assembly_error is not None or commit is None:
            return _replace_result(
                result,
                ready=False,
                finalized=False,
                final_commit=None,
                errors=[*result.errors, assembly_error] if assembly_error else list(result.errors),
                message=(
                    assembly_error.message
                    if assembly_error
                    else "Delivery assembly did not produce a final branch commit."
                ),
            )

        progress = mark_delivery_finalized(progress, final_branch=branch, final_commit=commit)
        write_delivery_progress(progress_path, progress)
        append_delivery_progress_event(
            events_path,
            make_delivery_progress_event(
                preflight.plan_id,
                "plan.finalized",
                unit=None,
                branch=branch,
                commit=commit,
                timestamp=progress.finalized_at,
            ),
        )
        finalized_status = get_delivery_status(path, project_root=project_root)
        return DeliveryFinalizeResult(
            plan_path=result.plan_path,
            project_root=result.project_root,
            valid=finalized_status.valid,
            ready=True,
            dry_run=False,
            finalized=True,
            status=finalized_status.status,
            progress_exists=finalized_status.progress_exists,
            final_branch=branch,
            final_commit=commit,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=finalized_status.errors,
            warnings=finalized_status.warnings,
            message=f"Delivery final branch {branch} points to {commit}.",
        )


def render_delivery_finalize(result: DeliveryFinalizeResult) -> str:
    lines = [
        f"Delivery finalize{' dry run' if result.dry_run else ''}: {result.plan_path}",
        f"Status: {'ready' if result.ready and result.dry_run else 'done' if result.finalized else 'blocked'}",
    ]
    if result.project_root:
        lines.append(f"Project root: {result.project_root}")
    if result.status:
        lines.append(f"Plan status: {result.status}")
    lines.append(f"Progress exists: {'yes' if result.progress_exists else 'no'}")
    if result.final_branch:
        lines.append(f"Final branch: {result.final_branch}")
    if result.final_commit:
        lines.append(f"Final commit: {result.final_commit}")
    if result.progress_path:
        lines.append(f"Progress: {result.progress_path}")
    if result.events_path:
        lines.append(f"Events: {result.events_path}")
    lines.append(f"Dry run: {'yes' if result.dry_run else 'no'}")
    lines.append(result.message)
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for issue in result.errors:
            lines.append(_format_issue(issue))
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in result.warnings:
            lines.append(_format_issue(issue))
    return "\n".join(lines) + "\n"


def _preflight_delivery_finalize(
    path: str | Path,
    *,
    project_root: Path | None,
    dry_run: bool,
) -> _FinalizePreflight:
    status = get_delivery_status(path, project_root=project_root)
    errors = list(status.errors)
    warnings = list(status.warnings)
    plan_id = status.plan.plan_id if status.plan else None
    progress_path: str | None = None
    events_path: str | None = None
    branch = status.plan.final_branch if status.plan else None
    commit: str | None = None
    assembly_pending = False
    message = "Delivery final branch is not ready."

    root = Path(status.project_root).resolve() if status.project_root else None
    if status.valid and root is not None and plan_id:
        progress_path = str(delivery_progress_path(root, plan_id))
        events_path = str(delivery_events_path(root, plan_id))

    if status.valid and status.status != "done":
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.not_done",
                "Delivery plan must be done before finalizing the final branch.",
            )
        )
    if status.valid and not status.progress_exists:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.progress_missing",
                "Delivery progress is required before finalizing the final branch.",
            )
        )

    if status.valid and root is not None and branch:
        errors.extend(
            _finalize_git_errors(
                root,
                branch,
                status.units,
                assembly_base_commit=status.assembly_base_commit,
                assembled_commit=status.assembled_commit,
            )
        )
        if not errors:
            assembly_issue = _finalize_assembly_preview_issue(root, status)
            if assembly_issue is not None:
                errors.append(assembly_issue)
        if not errors:
            candidate = _final_commit_candidate(
                root,
                branch=branch,
                assembly_base_commit=status.assembly_base_commit,
                assembled_commit=status.assembled_commit,
            )
            if candidate is None:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "delivery.final_commit_missing",
                        "Delivery finalize could not determine a final branch commit.",
                    )
                )
            elif _commit_contains_completed_units(root, candidate, status.units):
                commit = candidate
                message = (
                    f"Dry run would update final branch {branch} to {commit}."
                    if dry_run
                    else f"Delivery final branch {branch} is ready to update to {commit}."
                )
            else:
                assembly_pending = True
                message = (
                    (
                        f"Dry run would assemble completed units into final branch {branch}; "
                        "the resulting commit is not known yet."
                    )
                    if dry_run
                    else f"Delivery final branch {branch} is ready for assembly."
                )

    ready = status.valid and not errors and branch is not None and (commit is not None or assembly_pending)
    return _FinalizePreflight(
        result=DeliveryFinalizeResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=not errors,
            ready=ready,
            dry_run=dry_run,
            finalized=False,
            status=status.status if status.valid else None,
            progress_exists=status.progress_exists,
            final_branch=branch,
            final_commit=commit,
            progress_path=progress_path,
            events_path=events_path,
            errors=errors,
            warnings=warnings,
            message=message,
        ),
        plan_id=plan_id,
    )


def _finalize_assembly_preview_issue(root: Path, status: Any) -> DeliveryPlanIssue | None:
    from core.delivery_assembly import (
        ordered_delivery_assembly_units,
        preview_delivery_assembly,
        recorded_delivery_assembly_conflict_issue,
    )

    if status.plan is None:
        return None
    completed_commits = {unit.id: unit.commit for unit in status.units if unit.status == "done"}
    preview = preview_delivery_assembly(
        root,
        branch=status.plan.final_branch,
        base_commit=status.assembly_base_commit or "HEAD",
        expected_commit=status.assembled_commit,
        units=ordered_delivery_assembly_units(status.plan, completed_commits),
    )
    if preview.error is not None and preview.error.code == "delivery.assembly_git_unsupported":
        return preview.error
    failed_unit = next((unit for unit in status.units if unit.id == status.assembly_unit_id), None)
    return recorded_delivery_assembly_conflict_issue(
        root,
        branch=status.plan.final_branch,
        assembly_status=status.assembly_status,
        assembly_error_code=status.assembly_error_code,
        assembled_commit=status.assembled_commit,
        failed_unit_id=status.assembly_unit_id,
        failed_unit_commit=failed_unit.commit if failed_unit else None,
    )


def _finalize_git_errors(
    root: Path,
    branch: str,
    units,
    *,
    assembly_base_commit: str | None,
    assembled_commit: str | None,
) -> list[DeliveryPlanIssue]:
    errors: list[DeliveryPlanIssue] = []
    if not _valid_branch_name(root, branch):
        return [
            DeliveryPlanIssue(
                "error",
                "delivery.final_branch_invalid",
                "Delivery final_branch is not a valid local branch name.",
            )
        ]
    if delivery_assembly_branch_is_symbolic(root, branch):
        return [
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_branch_symbolic",
                "Delivery final_branch must be a direct ref, not a symbolic ref.",
            )
        ]

    base = _resolve_commit(root, assembly_base_commit or "HEAD")
    if base is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_base_missing",
                "Delivery finalize requires a resolvable assembly base commit.",
            )
        )
        return errors

    for unit in units:
        if unit.status != "done" or not unit.commit:
            continue
        commit = _resolve_commit(root, unit.commit)
        if commit is None:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.unit_commit_missing",
                    f"Unit {unit.id} result commit cannot be resolved.",
                )
            )
            continue
    existing = _branch_commit(root, branch)
    expected = _resolve_commit(root, assembled_commit) if assembled_commit else None
    if assembled_commit and expected is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_expected_commit_missing",
                "Recorded delivery assembly commit is missing.",
            )
        )
    elif existing is None and expected is not None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_branch_missing",
                "Delivery assembly branch is missing after assembly progress was recorded.",
            )
        )
    elif existing is not None and expected is not None and not _git_commit_is_ancestor(root, expected, existing):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch diverged from recorded assembly progress.",
            )
        )
    elif existing is not None and expected is None:
        branch_contains_base = _git_commit_is_ancestor(root, base, existing)
        if branch_contains_base and existing != base:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.assembly_branch_diverged",
                    "Delivery assembly branch is ahead of the base without recorded assembly progress.",
                )
            )
        elif not _git_commit_is_ancestor(root, existing, base):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.assembly_branch_diverged",
                    "Delivery assembly branch does not share the assembly base history.",
                )
            )
    if existing is not None and branch_checked_out(root, branch):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.assembly_branch_checked_out",
                "Delivery assembly branch is checked out; switch that worktree away before retrying.",
            )
        )
    return errors


def _final_commit_candidate(
    root: Path,
    *,
    branch: str,
    assembly_base_commit: str | None,
    assembled_commit: str | None,
) -> str | None:
    branch_commit = _resolve_commit(root, f"refs/heads/{branch}")
    expected_commit = _resolve_commit(root, assembled_commit) if assembled_commit else None
    base_commit = _resolve_commit(root, assembly_base_commit or "HEAD")
    if (
        branch_commit is not None
        and expected_commit is None
        and base_commit is not None
        and _git_commit_is_ancestor(root, branch_commit, base_commit)
    ):
        return base_commit
    return branch_commit or expected_commit or base_commit


def _commit_contains_completed_units(root: Path, candidate: str, units) -> bool:
    for unit in units:
        if unit.status != "done" or not unit.commit:
            continue
        commit = _resolve_commit(root, unit.commit)
        if commit is None or not _git_commit_is_ancestor(root, commit, candidate):
            return False
    return True


def _assemble_for_finalize(
    *,
    root: Path,
    status,
    progress,
    progress_path: Path,
    events_path: Path,
):
    from core.delivery_assembly import assemble_delivery_commits, ordered_delivery_assembly_units

    if status.plan is None:
        issue = DeliveryPlanIssue(
            "error",
            "delivery.assembly_plan_missing",
            "Delivery finalize requires a valid delivery plan.",
        )
        return progress, None, issue

    base_commit = progress.assembly_base_commit or _resolve_commit(root, "HEAD")
    if base_commit is None:
        issue = DeliveryPlanIssue(
            "error",
            "delivery.assembly_base_missing",
            "Delivery finalize requires a resolvable assembly base commit.",
        )
        return progress, None, issue
    if progress.assembly_base_commit is None:
        progress = replace(progress, assembly_base_commit=base_commit)
        write_delivery_progress(progress_path, progress)

    completed_commits = {unit.id: unit.commit for unit in status.units if unit.status == "done"}
    units = ordered_delivery_assembly_units(status.plan, completed_commits)
    previous_commit = progress.assembled_commit
    result = assemble_delivery_commits(
        root,
        plan_id=status.plan.plan_id,
        branch=status.plan.final_branch,
        base_commit=base_commit,
        expected_commit=progress.assembled_commit,
        units=units,
    )
    if result.success and result.assembled_commit is not None:
        progress = mark_delivery_assembly(
            progress,
            base_commit=result.base_commit,
            assembled_commit=result.assembled_commit,
            status="ready",
        )
        write_delivery_progress(progress_path, progress)
        for outcome in result.outcomes:
            if outcome.outcome in {"already_applied", "no_op"} and previous_commit == outcome.assembled_commit:
                continue
            append_delivery_progress_event(
                events_path,
                DeliveryProgressEvent(
                    plan_id=status.plan.plan_id,
                    event_type=f"assembly.{outcome.outcome}",
                    timestamp=progress.assembly_updated_at,
                    unit_id=outcome.unit_id,
                    branch=status.plan.final_branch,
                    commit=outcome.assembled_commit,
                ),
            )
        return progress, result.assembled_commit, None

    issue = result.error or DeliveryPlanIssue(
        "error",
        "delivery.assembly_failed",
        "Delivery branch assembly failed.",
    )
    progress = mark_delivery_assembly(
        progress,
        base_commit=result.base_commit,
        assembled_commit=result.assembled_commit,
        status="failed",
        unit_id=result.failed_unit_id,
        error_code=issue.code,
    )
    write_delivery_progress(progress_path, progress)
    for outcome in result.outcomes:
        if outcome.outcome in {"already_applied", "no_op"} and previous_commit == outcome.assembled_commit:
            continue
        append_delivery_progress_event(
            events_path,
            DeliveryProgressEvent(
                plan_id=status.plan.plan_id,
                event_type=f"assembly.{outcome.outcome}",
                timestamp=progress.assembly_updated_at,
                unit_id=outcome.unit_id,
                branch=status.plan.final_branch,
                commit=outcome.assembled_commit,
            ),
        )
    append_delivery_progress_event(
        events_path,
        DeliveryProgressEvent(
            plan_id=status.plan.plan_id,
            event_type="assembly.failed",
            timestamp=progress.assembly_updated_at,
            unit_id=result.failed_unit_id,
            branch=status.plan.final_branch,
            commit=result.assembled_commit,
            failure_code=issue.code,
        ),
    )
    return progress, result.assembled_commit, issue


def _valid_branch_name(root: Path, branch: str) -> bool:
    branch_result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        cwd=root,
        capture_output=True,
        text=True,
    )
    literal_result = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return branch_result.returncode == 0 and literal_result.returncode == 0


def _branch_commit(root: Path, branch: str) -> str | None:
    return _resolve_commit(root, f"refs/heads/{branch}")


def _resolve_commit(root: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    commit = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return commit or None


def _git_commit_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _replace_result(
    result: DeliveryFinalizeResult,
    *,
    ready: bool,
    finalized: bool,
    errors: list[DeliveryPlanIssue],
    message: str,
    final_commit: str | None | Any = _UNSET,
) -> DeliveryFinalizeResult:
    return DeliveryFinalizeResult(
        plan_path=result.plan_path,
        project_root=result.project_root,
        valid=not errors,
        ready=ready,
        dry_run=result.dry_run,
        finalized=finalized,
        status=result.status,
        progress_exists=result.progress_exists,
        final_branch=result.final_branch,
        final_commit=result.final_commit if final_commit is _UNSET else final_commit,
        progress_path=result.progress_path,
        events_path=result.events_path,
        errors=errors,
        warnings=result.warnings,
        message=message,
    )


def _format_issue(issue: DeliveryPlanIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"
