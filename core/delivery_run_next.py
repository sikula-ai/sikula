from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.delivery_plan import DeliveryPlanIssue, _find_git_root as _find_delivery_git_root
from core.delivery_progress import DeliveryStatusUnit, get_delivery_status, select_next_delivery_unit


@dataclass(frozen=True)
class DeliveryRunNextPreview:
    plan_path: str
    project_root: str | None
    valid: bool
    ready: bool
    dry_run: bool
    status: str | None
    progress_exists: bool
    selected_unit: DeliveryStatusUnit | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    message: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "plan_path": self.plan_path,
            "project_root": self.project_root,
            "valid": self.valid,
            "ready": self.ready,
            "dry_run": self.dry_run,
            "status": self.status,
            "progress_exists": self.progress_exists,
            "selected_unit": self.selected_unit.to_dict() if self.selected_unit else None,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "message": self.message,
        }
        return data


@dataclass(frozen=True)
class DeliveryRunNextExecutionResult:
    plan_path: str
    project_root: str | None
    valid: bool
    ran: bool
    succeeded: bool
    status: str | None
    progress_exists: bool
    selected_unit: DeliveryStatusUnit | None
    child_task_id: str | None
    unit_status: str | None
    run_exit_code: int | None
    progress_path: str | None
    events_path: str | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    message: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "plan_path": self.plan_path,
            "project_root": self.project_root,
            "valid": self.valid,
            "ran": self.ran,
            "succeeded": self.succeeded,
            "status": self.status,
            "progress_exists": self.progress_exists,
            "selected_unit": self.selected_unit.to_dict() if self.selected_unit else None,
            "child_task_id": self.child_task_id,
            "unit_status": self.unit_status,
            "run_exit_code": self.run_exit_code,
            "progress_path": self.progress_path,
            "events_path": self.events_path,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "message": self.message,
        }
        return data


def preview_delivery_run_next(path: str | Path, *, project_root: Path | None = None) -> DeliveryRunNextPreview:
    git_root_error: DeliveryPlanIssue | None = None
    if project_root is not None and _find_delivery_git_root(project_root.resolve()) is None:
        git_root_error = DeliveryPlanIssue(
            "error",
            "project.git_root_missing",
            "Delivery run-next requires the configured project root to live inside one Git repository.",
        )
    status = get_delivery_status(path, project_root=project_root)
    errors = list(status.errors)
    warnings = list(status.warnings)
    selected_unit: DeliveryStatusUnit | None = None
    message = "Delivery plan is not ready to run."
    if git_root_error and not any(issue.code == git_root_error.code for issue in errors):
        errors.insert(0, git_root_error)

    if status.valid and not errors:
        selected_unit = select_next_delivery_unit(status)
        if selected_unit:
            message = (
                f"Dry run selected delivery unit {selected_unit.id}; "
                "no unit was run and delivery progress was not changed."
            )
        else:
            code, message = _blocked_run_next_reason(status.status)
            errors.append(DeliveryPlanIssue("error", code, message))

    return DeliveryRunNextPreview(
        plan_path=status.plan_path,
        project_root=status.project_root,
        valid=not errors,
        ready=selected_unit is not None and not errors,
        dry_run=True,
        status=status.status if status.valid else None,
        progress_exists=status.progress_exists,
        selected_unit=selected_unit if not errors else None,
        errors=errors,
        warnings=warnings,
        message=message,
    )


def render_delivery_run_next_preview(result: DeliveryRunNextPreview) -> str:
    lines = [
        f"Delivery run-next dry run: {result.plan_path}",
        f"Status: {'ready' if result.ready else 'blocked'}",
    ]
    if result.project_root:
        lines.append(f"Project root: {result.project_root}")
    if result.status:
        lines.append(f"Plan status: {result.status}")
    lines.append(f"Progress exists: {'yes' if result.progress_exists else 'no'}")
    if result.selected_unit:
        title = f" - {result.selected_unit.title}" if result.selected_unit.title else ""
        lines.append(f"Selected unit: {result.selected_unit.id}{title}")
        lines.append(f"Task path: {result.selected_unit.task_path}")
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


def render_delivery_run_next_execution(result: DeliveryRunNextExecutionResult) -> str:
    lines = [
        f"Delivery run-next: {result.plan_path}",
        f"Status: {'done' if result.succeeded else 'failed' if result.ran else 'blocked'}",
    ]
    if result.project_root:
        lines.append(f"Project root: {result.project_root}")
    if result.status:
        lines.append(f"Plan status: {result.status}")
    lines.append(f"Progress exists: {'yes' if result.progress_exists else 'no'}")
    if result.selected_unit:
        title = f" - {result.selected_unit.title}" if result.selected_unit.title else ""
        lines.append(f"Selected unit: {result.selected_unit.id}{title}")
        lines.append(f"Task path: {result.selected_unit.task_path}")
    if result.child_task_id:
        lines.append(f"Child task: {result.child_task_id}")
    if result.unit_status:
        lines.append(f"Unit status: {result.unit_status}")
    if result.run_exit_code is not None:
        lines.append(f"Run exit code: {result.run_exit_code}")
    if result.progress_path:
        lines.append(f"Progress: {result.progress_path}")
    if result.events_path:
        lines.append(f"Events: {result.events_path}")
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


def _blocked_run_next_reason(status: str) -> tuple[str, str]:
    if status == "failed":
        return "delivery.failed", "Delivery plan has failed unit(s); inspect status before running next."
    if status == "running":
        return "delivery.running", "Delivery plan already has a running unit."
    if status == "waiting":
        return "delivery.waiting", "Delivery plan is waiting for human input."
    if status == "canceled":
        return "delivery.canceled", "Delivery plan has canceled unit(s)."
    if status == "done":
        return "delivery.complete", "Delivery plan is already complete."
    return "delivery.no_eligible_unit", "Delivery plan has no eligible pending unit."


def _format_issue(issue: DeliveryPlanIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"
