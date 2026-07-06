from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import (
    DeliveryProgressLockError,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_progress_event,
    mark_delivery_finalized,
    read_delivery_progress,
    write_delivery_progress,
)

_NULL_COMMIT = "0" * 40


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
        commit = result.final_commit
        if branch is None or commit is None:
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

        update_error = _update_final_branch(root, branch, commit)
        if update_error:
            return _replace_result(
                result,
                ready=False,
                finalized=False,
                errors=[*result.errors, update_error],
                message="Delivery final branch could not be updated.",
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
        errors.extend(_finalize_git_errors(root, branch, status.units))
        if not errors:
            commit = _final_commit_candidate(root)
            if commit is None:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "delivery.final_commit_missing",
                        "Delivery finalize could not determine a final branch commit.",
                    )
                )
            else:
                message = (
                    f"Dry run would update final branch {branch} to {commit}."
                    if dry_run
                    else f"Delivery final branch {branch} is ready to update to {commit}."
                )

    ready = status.valid and not errors and branch is not None and commit is not None
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


def _finalize_git_errors(root: Path, branch: str, units) -> list[DeliveryPlanIssue]:
    errors: list[DeliveryPlanIssue] = []
    if not _valid_branch_name(root, branch):
        return [
            DeliveryPlanIssue(
                "error",
                "delivery.final_branch_invalid",
                "Delivery final_branch is not a valid local branch name.",
            )
        ]

    head = _resolve_commit(root, "HEAD")
    if head is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery.head_missing",
                "Delivery finalize requires a current Git HEAD commit.",
            )
        )
        return errors

    candidate = _final_commit_candidate(root)
    if candidate is None:
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
        if not _git_commit_is_ancestor(root, commit, head):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.unit_commit_unapplied",
                    f"Unit {unit.id} result commit is not applied to the current checkout.",
                )
            )
        if not _git_commit_is_ancestor(root, commit, candidate):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.final_commit_missing_unit",
                    f"Unit {unit.id} result commit is not included in the final branch commit candidate.",
                )
            )

    branch_error = _final_branch_move_error(root, branch, _branch_commit(root, branch), candidate)
    if branch_error:
        errors.append(branch_error)
    return errors


def _final_commit_candidate(root: Path) -> str | None:
    return _resolve_commit(root, "HEAD")


def _update_final_branch(root: Path, branch: str, commit: str) -> DeliveryPlanIssue | None:
    ref = f"refs/heads/{branch}"
    existing = _branch_commit(root, branch)
    if existing == commit:
        return None
    branch_error = _final_branch_move_error(root, branch, existing, commit)
    if branch_error:
        return branch_error
    args = ["git", "update-ref", ref, commit, existing or _NULL_COMMIT]
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    if result.returncode == 0:
        return None
    return DeliveryPlanIssue(
        "error",
        "delivery.final_branch_update_failed",
        "Git refused to update the delivery final branch.",
    )


def _final_branch_move_error(root: Path, branch: str, existing: str | None, target: str) -> DeliveryPlanIssue | None:
    if existing is None or existing == target:
        return None
    if not _git_commit_is_ancestor(root, existing, target):
        return DeliveryPlanIssue(
            "error",
            "delivery.final_branch_diverged",
            "Delivery final branch already exists and cannot be fast-forwarded.",
        )
    if _branch_checked_out(root, branch):
        return DeliveryPlanIssue(
            "error",
            "delivery.final_branch_checked_out",
            "Delivery final branch is checked out; switch away before finalizing.",
        )
    return None


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


def _branch_checked_out(root: Path, branch: str) -> bool:
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        return True
    branch_ref = f"branch refs/heads/{branch}"
    return any(line.strip() == branch_ref for line in result.stdout.splitlines())


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
        final_commit=result.final_commit,
        progress_path=result.progress_path,
        events_path=result.events_path,
        errors=errors,
        warnings=result.warnings,
        message=message,
    )


def _format_issue(issue: DeliveryPlanIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"
