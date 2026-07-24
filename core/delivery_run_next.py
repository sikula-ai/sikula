from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.delivery_plan import DeliveryBudgetExceeded, DeliveryPlanIssue, _find_git_root as _find_delivery_git_root
from core.delivery_progress import DeliveryStatusUnit, get_delivery_status, select_next_delivery_unit
from core.delivery_public_metadata import (
    project_delivery_public_identity,
    sanitize_delivery_public_metadata,
)
from core.delivery_unit_metadata import DELIVERY_UNIT_BUDGET_EXCEEDED_CODE


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
        plan_path = self.plan_path
        project_root = self.project_root
        if project_root and project_root != ".":
            plan_path = _project_relative_path(plan_path, Path(project_root))
            project_root = "."

        data: dict[str, Any] = {
            "plan_path": plan_path,
            "project_root": project_root,
            "valid": self.valid,
            "ready": self.ready,
            "dry_run": self.dry_run,
            "status": self.status,
            "progress_exists": self.progress_exists,
            "selected_unit": self.selected_unit.to_dict() if self.selected_unit else None,
            "errors": [
                _sanitize_issue(issue, self.project_root, self.plan_path, None, None).to_dict() for issue in self.errors
            ],
            "warnings": [
                _sanitize_issue(issue, self.project_root, self.plan_path, None, None).to_dict()
                for issue in self.warnings
            ],
            "message": sanitize_delivery_public_metadata(self.message),
        }
        return data


@dataclass(frozen=True)
class DeliveryBudgetSplitPreparationResult:
    prepared: bool
    target_unit_id: str | None
    proposal_id: str | None
    replacement_ids: list[str]
    proposal_path: str | None
    audit_path: str | None
    budget_exceeded: DeliveryBudgetExceeded | None
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    message: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "prepared": self.prepared,
            "target_unit_id": project_delivery_public_identity(self.target_unit_id),
            "proposal_id": self.proposal_id,
            "replacement_ids": [project_delivery_public_identity(value) for value in self.replacement_ids],
            "proposal_path": sanitize_delivery_public_metadata(self.proposal_path),
            "audit_path": sanitize_delivery_public_metadata(self.audit_path),
            "budget_exceeded": self.budget_exceeded.to_dict() if self.budget_exceeded else None,
            "errors": [_project_budget_split_issue(issue) for issue in self.errors],
            "warnings": [_project_budget_split_issue(issue) for issue in self.warnings],
            "message": sanitize_delivery_public_metadata(self.message),
        }
        if self.prepared:
            data["next_action"] = "delivery_amend_apply"
        return data


def _project_budget_split_issue(issue: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in ("severity", "code", "message", "path"):
        value = issue.get(key)
        if isinstance(value, str):
            projected[key] = sanitize_delivery_public_metadata(value)
    return projected


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
    budget_split_preparation: DeliveryBudgetSplitPreparationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        plan_path = self.plan_path
        progress_path = self.progress_path
        events_path = self.events_path
        project_root = self.project_root
        if project_root and project_root != ".":
            root_path = Path(project_root)
            plan_path = _project_relative_path(plan_path, root_path)
            if progress_path:
                progress_path = _project_relative_path(progress_path, root_path)
            if events_path:
                events_path = _project_relative_path(events_path, root_path)
            project_root = "."

        data: dict[str, Any] = {
            "plan_path": plan_path,
            "project_root": project_root,
            "valid": self.valid,
            "ran": self.ran,
            "succeeded": self.succeeded,
            "status": self.status,
            "progress_exists": self.progress_exists,
            "selected_unit": self.selected_unit.to_dict() if self.selected_unit else None,
            "child_task_id": project_delivery_public_identity(self.child_task_id),
            "unit_status": self.unit_status,
            "run_exit_code": self.run_exit_code,
            "progress_path": progress_path,
            "events_path": events_path,
            "errors": [
                _sanitize_issue(
                    issue, self.project_root, self.plan_path, self.progress_path, self.events_path
                ).to_dict()
                for issue in self.errors
            ],
            "warnings": [
                _sanitize_issue(
                    issue, self.project_root, self.plan_path, self.progress_path, self.events_path
                ).to_dict()
                for issue in self.warnings
            ],
            "message": sanitize_delivery_public_metadata(self.message),
        }
        if self.budget_split_preparation is not None:
            data["budget_split_preparation"] = self.budget_split_preparation.to_dict()
        return data


def _project_relative_path(path_str: str, project_root: Path | None) -> str:
    if not project_root:
        return path_str
    try:
        return Path(path_str).resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return Path(path_str).name


def _sanitize_issue(
    issue: DeliveryPlanIssue,
    project_root: str | None,
    plan_path: str,
    progress_path: str | None,
    events_path: str | None,
) -> DeliveryPlanIssue:
    if not project_root or project_root == ".":
        return issue

    msg = issue.message
    path = issue.path
    root_path = Path(project_root)

    plan_rel = _project_relative_path(plan_path, root_path)
    msg = msg.replace(plan_path, plan_rel)
    if path:
        path = path.replace(plan_path, plan_rel)

    if progress_path:
        prog_rel = _project_relative_path(progress_path, root_path)
        msg = msg.replace(progress_path, prog_rel)
        if path:
            path = path.replace(progress_path, prog_rel)

    if events_path:
        events_rel = _project_relative_path(events_path, root_path)
        msg = msg.replace(events_path, events_rel)
        if path:
            path = path.replace(events_path, events_rel)

    root_str = str(root_path.resolve())
    if not root_str.endswith("/"):
        root_str += "/"
    msg = msg.replace(root_str, "")
    if path:
        path = path.replace(root_str, "")

    root_str_no_slash = str(root_path.resolve())
    msg = msg.replace(root_str_no_slash, ".")
    if path:
        path = path.replace(root_str_no_slash, ".")

    if msg == issue.message and path == issue.path:
        return issue
    return DeliveryPlanIssue(issue.severity, issue.code, msg, path)


def preview_delivery_run_next(
    path: str | Path, *, project_root: Path | None = None, reset_failed: bool = False
) -> DeliveryRunNextPreview:
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
        running_recovery_unit = _select_running_recovery_unit(status)
        if running_recovery_unit:
            selected_unit = running_recovery_unit
            action_text = "resume, retry, or reconciliation" if reset_failed else "resume or reconciliation"
            message = (
                f"Dry run selected running delivery unit {selected_unit.id}; run-next will inspect the linked "
                f"child task for {action_text} without selecting pending work."
            )
        else:
            selected_unit = select_next_delivery_unit(status, reset_failed=reset_failed)
            if selected_unit:
                if reset_failed:
                    message = (
                        f"Dry run selected failed delivery unit {selected_unit.id}; "
                        "no unit was run and delivery progress was not changed."
                    )
                else:
                    message = (
                        f"Dry run selected delivery unit {selected_unit.id}; "
                        "no unit was run and delivery progress was not changed."
                    )
        if selected_unit is None:
            code, message = _blocked_run_next_reason(
                status.status,
                reset_failed=reset_failed,
                units=status.units,
            )
            errors.append(DeliveryPlanIssue("error", code, message))

    effective_root = (
        project_root if project_root is not None else (Path(status.project_root) if status.project_root else None)
    )

    return DeliveryRunNextPreview(
        plan_path=status.plan_path,
        project_root=str(effective_root.resolve()) if effective_root else status.project_root,
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


def _select_running_recovery_unit(status) -> DeliveryStatusUnit | None:
    running_units = [unit for unit in status.units if unit.status == "running"]
    if len(running_units) == 1 and running_units[0].child_task_id:
        return running_units[0]
    return None


def render_delivery_run_next_preview(result: DeliveryRunNextPreview) -> str:
    plan_path = result.plan_path
    project_root = result.project_root
    if project_root and project_root != ".":
        plan_path = _project_relative_path(plan_path, Path(project_root))
        project_root = "."

    lines = [
        f"Delivery run-next dry run: {plan_path}",
        f"Status: {'ready' if result.ready else 'blocked'}",
    ]
    if project_root:
        lines.append(f"Project root: {project_root}")
    if result.status:
        lines.append(f"Plan status: {result.status}")
    lines.append(f"Progress exists: {'yes' if result.progress_exists else 'no'}")
    if result.selected_unit:
        selected_unit = result.selected_unit.to_dict()
        safe_title = selected_unit.get("title")
        title = f" - {safe_title}" if safe_title else ""
        lines.append(f"Selected unit: {selected_unit['id']}{title}")
        lines.append(f"Task path: {selected_unit['task_path']}")
        if selected_unit.get("child_task_id"):
            lines.append(f"Child task: {selected_unit['child_task_id']}")
    lines.append(f"Dry run: {'yes' if result.dry_run else 'no'}")
    lines.append(sanitize_delivery_public_metadata(result.message) or "")
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for issue in result.errors:
            lines.append(_format_issue(_sanitize_issue(issue, result.project_root, result.plan_path, None, None)))
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in result.warnings:
            lines.append(_format_issue(_sanitize_issue(issue, result.project_root, result.plan_path, None, None)))
    return "\n".join(lines) + "\n"


def render_delivery_run_next_execution(result: DeliveryRunNextExecutionResult) -> str:
    plan_path = result.plan_path
    progress_path = result.progress_path
    events_path = result.events_path
    project_root = result.project_root
    if project_root and project_root != ".":
        root_path = Path(project_root)
        plan_path = _project_relative_path(plan_path, root_path)
        if progress_path:
            progress_path = _project_relative_path(progress_path, root_path)
        if events_path:
            events_path = _project_relative_path(events_path, root_path)
        project_root = "."

    lines = [
        f"Delivery run-next: {plan_path}",
        f"Status: {'done' if result.succeeded else 'failed' if result.ran else 'blocked'}",
    ]
    if project_root:
        lines.append(f"Project root: {project_root}")
    if result.status:
        lines.append(f"Plan status: {result.status}")
    lines.append(f"Progress exists: {'yes' if result.progress_exists else 'no'}")
    if result.selected_unit:
        selected_unit = result.selected_unit.to_dict()
        safe_title = selected_unit.get("title")
        title = f" - {safe_title}" if safe_title else ""
        lines.append(f"Selected unit: {selected_unit['id']}{title}")
        lines.append(f"Task path: {selected_unit['task_path']}")
    if result.child_task_id:
        lines.append(f"Child task: {project_delivery_public_identity(result.child_task_id)}")
    if result.unit_status:
        lines.append(f"Unit status: {result.unit_status}")
    if result.run_exit_code is not None:
        lines.append(f"Run exit code: {result.run_exit_code}")
    if progress_path:
        lines.append(f"Progress: {progress_path}")
    if events_path:
        lines.append(f"Events: {events_path}")
    lines.append(sanitize_delivery_public_metadata(result.message) or "")
    if result.budget_split_preparation is not None:
        preparation = result.budget_split_preparation
        preparation_data = preparation.to_dict()
        lines.extend(
            [
                "",
                "Budget split preparation:",
                f"Status: {'prepared' if preparation.prepared else 'blocked'}",
            ]
        )
        if preparation_data["target_unit_id"]:
            lines.append(f"Target unit: {preparation_data['target_unit_id']}")
        if preparation_data["proposal_id"]:
            lines.append(f"Proposal: {preparation_data['proposal_id']}")
        if preparation_data["replacement_ids"]:
            lines.append("Replacements: " + ", ".join(preparation_data["replacement_ids"]))
        if preparation_data["proposal_path"]:
            lines.append(f"Proposal artifact: {preparation_data['proposal_path']}")
        if preparation_data["audit_path"]:
            lines.append(f"Authoring audit: {preparation_data['audit_path']}")
        lines.append(preparation_data["message"] or "")
        if preparation_data["errors"]:
            lines.append("Preparation errors:")
            lines.extend(f"- {_format_projected_issue(issue)}" for issue in preparation_data["errors"])
        if preparation_data["warnings"]:
            lines.append("Preparation warnings:")
            lines.extend(f"- {_format_projected_issue(issue)}" for issue in preparation_data["warnings"])
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for issue in result.errors:
            lines.append(
                _format_issue(
                    _sanitize_issue(
                        issue, result.project_root, result.plan_path, result.progress_path, result.events_path
                    )
                )
            )
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in result.warnings:
            lines.append(
                _format_issue(
                    _sanitize_issue(
                        issue, result.project_root, result.plan_path, result.progress_path, result.events_path
                    )
                )
            )
    return "\n".join(lines) + "\n"


def _format_projected_issue(issue: dict[str, Any]) -> str:
    code = issue.get("code") or "delivery.budget_split_preparation_failed"
    path = f" [{issue['path']}]" if issue.get("path") else ""
    message = issue.get("message") or "Budget split preparation failed."
    return f"{code}{path}: {message}"


def _blocked_run_next_reason(
    status: str,
    reset_failed: bool = False,
    units: list[DeliveryStatusUnit] | None = None,
) -> tuple[str, str]:
    if any(unit.status == "failed" and unit.failure_code == DELIVERY_UNIT_BUDGET_EXCEEDED_CODE for unit in units or []):
        return (
            "delivery.unit_budget_exceeded",
            "A delivery unit exceeded its planner-step budget; split it with delivery amend prepare before continuing.",
        )
    if status == "failed":
        if reset_failed:
            return (
                "delivery.failed_reset_unavailable",
                "No failed delivery unit with a linked child task is available for --reset-failed.",
            )
        return (
            "delivery.failed",
            "Delivery plan has failed unit(s); rerun with --reset-failed to select a failed unit with a linked child task.",
        )
    if status == "running":
        return "delivery.running", "Delivery plan already has a running unit."
    if status == "waiting":
        return "delivery.waiting", "Delivery plan is waiting for human input."
    if status == "canceled":
        return "delivery.canceled", "Delivery plan has canceled unit(s)."
    if status == "done":
        return "delivery.complete", "Delivery plan is already complete."
    if reset_failed:
        return (
            "delivery.failed_reset_unavailable",
            "No failed delivery unit with a linked child task is available for --reset-failed.",
        )
    return "delivery.no_eligible_unit", "Delivery plan has no eligible pending unit."


def _format_issue(issue: DeliveryPlanIssue) -> str:
    return issue.to_public_text()
