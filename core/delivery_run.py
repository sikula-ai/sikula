"""Typed public result for bounded delivery-plan execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import DeliveryStatusUnit
from core.delivery_public_metadata import (
    project_delivery_public_identity,
    sanitize_delivery_public_metadata,
)

DELIVERY_RUN_BLOCKED = "delivery.run.blocked"
DELIVERY_RUN_COMPLETED = "delivery.run.completed"
DELIVERY_RUN_ELAPSED_LIMIT_REACHED = "delivery.run.elapsed_limit_reached"
DELIVERY_RUN_FINALIZE_FAILED = "delivery.run.finalize_failed"
DELIVERY_RUN_NO_PROGRESS = "delivery.run.no_progress"
DELIVERY_RUN_PREVIEW = "delivery.run.preview"
DELIVERY_RUN_SNAPSHOT_EXHAUSTED = "delivery.run.snapshot_exhausted"
DELIVERY_RUN_UNIT_FAILED = "delivery.run.unit_failed"
DELIVERY_RUN_UNIT_LIMIT_REACHED = "delivery.run.unit_limit_reached"


@dataclass(frozen=True)
class DeliveryRunResult:
    plan_path: str
    project_root: str | None
    valid: bool
    ready: bool
    dry_run: bool
    started: bool
    succeeded: bool
    completed: bool
    finalized: bool
    status: str | None
    max_units: int
    max_elapsed_minutes: int | None
    units_attempted: int
    units_succeeded: int
    last_unit: DeliveryStatusUnit | None
    child_task_id: str | None
    stop_code: str
    progress_path: str | None
    final_branch: str | None
    final_commit: str | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    message: str

    def to_dict(self) -> dict[str, Any]:
        root = Path(self.project_root).resolve() if self.project_root and self.project_root != "." else None
        return {
            "plan_path": _public_path(self.plan_path, root),
            "project_root": "." if root else self.project_root,
            "valid": self.valid,
            "ready": self.ready,
            "dry_run": self.dry_run,
            "started": self.started,
            "succeeded": self.succeeded,
            "completed": self.completed,
            "finalized": self.finalized,
            "status": self.status,
            "max_units": self.max_units,
            "max_elapsed_minutes": self.max_elapsed_minutes,
            "units_attempted": self.units_attempted,
            "units_succeeded": self.units_succeeded,
            "last_unit": self.last_unit.to_dict() if self.last_unit else None,
            "child_task_id": project_delivery_public_identity(self.child_task_id),
            "stop_code": self.stop_code,
            "progress_path": _public_path(self.progress_path, root),
            "final_branch": sanitize_delivery_public_metadata(self.final_branch),
            "final_commit": sanitize_delivery_public_metadata(self.final_commit),
            "errors": [_public_issue(issue, root).to_dict() for issue in self.errors],
            "warnings": [_public_issue(issue, root).to_dict() for issue in self.warnings],
            "message": sanitize_delivery_public_metadata(self.message),
        }


def render_delivery_run(result: DeliveryRunResult) -> str:
    data = result.to_dict()
    if result.dry_run:
        display_status = "ready" if result.ready else "blocked"
    elif result.completed:
        display_status = "done"
    elif result.succeeded:
        display_status = "stopped"
    else:
        display_status = "failed" if result.started else "blocked"

    lines = [
        f"Delivery run{' dry run' if result.dry_run else ''}: {data['plan_path']}",
        f"Status: {display_status}",
    ]
    if data["project_root"]:
        lines.append(f"Project root: {data['project_root']}")
    if data["status"]:
        lines.append(f"Plan status: {data['status']}")
    lines.extend(
        [
            f"Unit limit: {data['max_units']}",
            (
                f"Elapsed limit: {data['max_elapsed_minutes']} minute(s)"
                if data["max_elapsed_minutes"] is not None
                else "Elapsed limit: none"
            ),
            f"Units attempted: {data['units_attempted']}",
            f"Units succeeded: {data['units_succeeded']}",
        ]
    )
    if data["last_unit"]:
        title = f" - {data['last_unit']['title']}" if data["last_unit"].get("title") else ""
        lines.append(f"Last unit: {data['last_unit']['id']}{title}")
    if data["child_task_id"]:
        lines.append(f"Child task: {data['child_task_id']}")
    lines.append(f"Stop code: {data['stop_code']}")
    if data["progress_path"]:
        lines.append(f"Progress: {data['progress_path']}")
    if data["final_branch"]:
        lines.append(f"Final branch: {data['final_branch']}")
    if data["final_commit"]:
        lines.append(f"Final commit: {data['final_commit']}")
    lines.append(data["message"] or "")
    if data["errors"]:
        lines.append("")
        lines.append("Errors:")
        lines.extend(_format_public_issue(issue) for issue in data["errors"])
    if data["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(_format_public_issue(issue) for issue in data["warnings"])
    return "\n".join(lines) + "\n"


def _public_path(value: str | None, root: Path | None) -> str | None:
    if value is None:
        return None
    if root is None:
        return sanitize_delivery_public_metadata(value)
    try:
        return Path(value).resolve().relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return sanitize_delivery_public_metadata(Path(value).name)


def _public_issue(issue: DeliveryPlanIssue, root: Path | None) -> DeliveryPlanIssue:
    message = issue.message
    path = issue.path
    if root is not None:
        message = _replace_project_root(message, root)
        path = _public_issue_path(path, root)
    return DeliveryPlanIssue(
        issue.severity,
        issue.code,
        sanitize_delivery_public_metadata(message) or "",
        sanitize_delivery_public_metadata(path),
    )


def _replace_project_root(value: str, root: Path) -> str:
    root_text = str(root)
    pattern = re.compile(rf"(?<![\w./\\-]){re.escape(root_text)}(?=$|[/\\])")
    return pattern.sub(".", value)


def _public_issue_path(value: str | None, root: Path) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        return sanitize_delivery_public_metadata(value)
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return sanitize_delivery_public_metadata(value)
    return f"./{relative.as_posix()}" if relative != Path(".") else "."


def _format_public_issue(issue: dict[str, Any]) -> str:
    location = f" [{issue['path']}]" if issue.get("path") else ""
    return f"- {issue.get('code', 'delivery.run.error')}{location}: {issue.get('message', '')}"
