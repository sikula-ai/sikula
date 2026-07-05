from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from core.delivery_plan import DeliveryPlan, DeliveryPlanIssue, check_delivery_plan_file

SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION = 1
DELIVERY_UNIT_STATUSES = {"pending", "running", "done", "failed", "canceled", "waiting"}
_DELIVERY_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_DELIVERY_PROGRESS_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TERMINAL_DELIVERY_UNIT_STATUSES = {"done", "failed", "canceled"}


@dataclass(frozen=True)
class DeliveryUnitProgress:
    unit_id: str
    status: str
    child_task_id: str | None = None
    branch: str | None = None
    commit: str | None = None
    waiting_reason: str | None = None
    failure_code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "unit_id": self.unit_id,
            "status": self.status,
        }
        for key in (
            "child_task_id",
            "branch",
            "commit",
            "waiting_reason",
            "failure_code",
            "started_at",
            "completed_at",
            "updated_at",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


@dataclass(frozen=True)
class DeliveryProgress:
    schema_version: int
    plan_id: str
    units: list[DeliveryUnitProgress] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "units": [unit.to_dict() for unit in self.units],
        }


@dataclass(frozen=True)
class DeliveryProgressEvent:
    plan_id: str
    event_type: str
    timestamp: str
    unit_id: str | None = None
    status: str | None = None
    child_task_id: str | None = None
    branch: str | None = None
    commit: str | None = None
    waiting_reason: str | None = None
    failure_code: str | None = None
    schema_version: int = SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
        }
        for key in (
            "unit_id",
            "status",
            "child_task_id",
            "branch",
            "commit",
            "waiting_reason",
            "failure_code",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


class DeliveryProgressLockError(RuntimeError):
    """Raised when a delivery progress mutation lock cannot be acquired."""


@dataclass
class DeliveryProgressLock:
    path: Path
    owner: str = "sikula"
    _acquired: bool = False

    def acquire(self) -> DeliveryProgressLock:
        if self._acquired:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema_version": SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION,
            "owner": self.owner,
            "pid": os.getpid(),
            "created_at": _utc_now(),
        }
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise DeliveryProgressLockError(f"Delivery progress lock already exists: {self.path}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(metadata, sort_keys=True) + "\n")
            self._acquired = True
        except BaseException:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            raise
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False

    def __enter__(self) -> DeliveryProgressLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass(frozen=True)
class DeliveryStatusUnit:
    id: str
    status: str
    title: str | None
    task_path: str
    depends_on: list[str]
    blocked_by: list[str] = field(default_factory=list)
    stream: str | None = None
    platform: str | None = None
    phase: str | None = None
    kind: str | None = None
    repo_id: str | None = None
    child_task_id: str | None = None
    branch: str | None = None
    commit: str | None = None
    waiting_reason: str | None = None
    failure_code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None

    @property
    def eligible(self) -> bool:
        return self.status == "pending" and not self.blocked_by

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "eligible": self.eligible,
            "task_path": self.task_path,
            "depends_on": list(self.depends_on),
            "blocked_by": list(self.blocked_by),
        }
        for key in (
            "title",
            "stream",
            "platform",
            "phase",
            "kind",
            "repo_id",
            "child_task_id",
            "branch",
            "commit",
            "waiting_reason",
            "failure_code",
            "started_at",
            "completed_at",
            "updated_at",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


@dataclass(frozen=True)
class DeliveryStatusResult:
    plan_path: str
    project_root: str | None
    progress_path: str | None
    progress_exists: bool
    status: str
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    plan: DeliveryPlan | None = None
    units: list[DeliveryStatusUnit] = field(default_factory=list)
    next_action: str | None = None

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "plan_path": self.plan_path,
            "project_root": self.project_root,
            "progress_path": self.progress_path,
            "progress_exists": self.progress_exists,
            "valid": self.valid,
            "status": self.status,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "units": [unit.to_dict() for unit in self.units],
        }
        if self.next_action:
            data["next_action"] = self.next_action
        if self.plan:
            data["plan"] = {
                "schema_version": self.plan.schema_version,
                "plan_id": self.plan.plan_id,
                "title": self.plan.title,
                "final_branch": self.plan.final_branch,
                "planning_mode": self.plan.planning_mode,
                "repositories": [repo.to_dict() for repo in self.plan.repositories],
                "streams": list(self.plan.stream_ids),
            }
        return data


def delivery_progress_path(project_root: Path, plan_id: str) -> Path:
    return _delivery_state_dir(project_root, plan_id) / "progress.json"


def delivery_events_path(project_root: Path, plan_id: str) -> Path:
    return _delivery_state_dir(project_root, plan_id) / "events.jsonl"


def delivery_lock_path(project_root: Path, plan_id: str) -> Path:
    return _delivery_state_dir(project_root, plan_id) / "lock"


def acquire_delivery_progress_lock(
    project_root: Path,
    plan_id: str,
    *,
    owner: str = "sikula",
) -> DeliveryProgressLock:
    return DeliveryProgressLock(delivery_lock_path(project_root, plan_id), owner=owner).acquire()


def write_delivery_progress(path: Path, progress: DeliveryProgress) -> None:
    _validate_progress(progress)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(progress.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def append_delivery_progress_event(path: Path, event: DeliveryProgressEvent) -> None:
    _validate_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def read_delivery_progress(path: Path, *, plan_id: str) -> tuple[DeliveryProgress | None, list[DeliveryPlanIssue]]:
    errors: list[DeliveryPlanIssue] = []
    progress = _load_delivery_progress(path, plan_id=plan_id, errors=errors)
    return progress, errors


def upsert_delivery_unit_progress(
    progress: DeliveryProgress,
    unit: DeliveryUnitProgress,
) -> DeliveryProgress:
    _validate_progress(progress)
    _validate_unit_progress(unit)
    units: list[DeliveryUnitProgress] = []
    replaced = False
    for existing in progress.units:
        if existing.unit_id == unit.unit_id:
            if unit.status in _TERMINAL_DELIVERY_UNIT_STATUSES and not unit.started_at and existing.started_at:
                unit = replace(unit, started_at=existing.started_at)
            units.append(unit)
            replaced = True
        else:
            units.append(existing)
    if not replaced:
        units.append(unit)
    return DeliveryProgress(
        schema_version=progress.schema_version,
        plan_id=progress.plan_id,
        units=units,
    )


def make_delivery_unit_progress(
    unit_id: str,
    status: str,
    *,
    child_task_id: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    waiting_reason: str | None = None,
    failure_code: str | None = None,
    started_at: str | None = None,
    timestamp: str | None = None,
) -> DeliveryUnitProgress:
    timestamp = timestamp or _utc_now()
    started_at = timestamp if status == "running" else started_at
    completed_at = timestamp if status in _TERMINAL_DELIVERY_UNIT_STATUSES else None
    return DeliveryUnitProgress(
        unit_id=unit_id,
        status=status,
        child_task_id=child_task_id,
        branch=branch,
        commit=commit,
        waiting_reason=waiting_reason if status == "waiting" else None,
        failure_code=failure_code if status == "failed" else None,
        started_at=started_at,
        completed_at=completed_at,
        updated_at=timestamp,
    )


def make_delivery_progress_event(
    plan_id: str,
    event_type: str,
    *,
    unit: DeliveryUnitProgress | None = None,
    timestamp: str | None = None,
) -> DeliveryProgressEvent:
    timestamp = timestamp or _utc_now()
    return DeliveryProgressEvent(
        plan_id=plan_id,
        event_type=event_type,
        timestamp=timestamp,
        unit_id=unit.unit_id if unit else None,
        status=unit.status if unit else None,
        child_task_id=unit.child_task_id if unit else None,
        branch=unit.branch if unit else None,
        commit=unit.commit if unit else None,
        waiting_reason=unit.waiting_reason if unit else None,
        failure_code=unit.failure_code if unit else None,
    )


def select_next_delivery_unit(status: DeliveryStatusResult) -> DeliveryStatusUnit | None:
    if not status.valid or status.status in {"failed", "running", "waiting", "canceled", "done"}:
        return None
    return next((unit for unit in status.units if unit.eligible), None)


def get_delivery_status(path: str | Path, *, project_root: Path | None = None) -> DeliveryStatusResult:
    check_result = check_delivery_plan_file(path, project_root=project_root)
    errors = list(check_result.errors)
    warnings = list(check_result.warnings)
    plan = check_result.plan
    progress_path: Path | None = None
    progress_exists = False

    if plan is None or check_result.project_root is None or errors:
        return DeliveryStatusResult(
            plan_path=check_result.plan_path,
            project_root=check_result.project_root,
            progress_path=None,
            progress_exists=False,
            status="invalid",
            errors=errors,
            warnings=warnings,
            plan=plan,
            units=[],
            next_action="fix delivery plan validation errors",
        )

    root = Path(check_result.project_root)
    progress_path = delivery_progress_path(root, plan.plan_id)
    progress_exists = progress_path.exists()
    progress: DeliveryProgress | None = None
    if progress_exists:
        progress = _load_delivery_progress(progress_path, plan_id=plan.plan_id, errors=errors)

    units = _build_status_units(plan, progress, warnings)
    status = "invalid" if errors else _overall_status(units)
    return DeliveryStatusResult(
        plan_path=check_result.plan_path,
        project_root=check_result.project_root,
        progress_path=str(progress_path),
        progress_exists=progress_exists,
        status=status,
        errors=errors,
        warnings=warnings,
        plan=plan,
        units=units,
        next_action=_next_action(status, units),
    )


def _delivery_state_dir(project_root: Path, plan_id: str) -> Path:
    _validate_plan_id_for_path(plan_id)
    return project_root / ".sikula" / "state" / "delivery" / plan_id


def _validate_plan_id_for_path(plan_id: str) -> None:
    if not isinstance(plan_id, str) or not _DELIVERY_PROGRESS_PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("delivery plan id must be a safe path segment")


def _validate_progress(progress: DeliveryProgress) -> None:
    _validate_plan_id_for_path(progress.plan_id)
    if progress.schema_version != SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION:
        raise ValueError(
            "unsupported delivery progress schema_version "
            f"{progress.schema_version}; expected {SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION}"
        )
    seen: set[str] = set()
    for unit in progress.units:
        _validate_unit_progress(unit)
        if unit.unit_id in seen:
            raise ValueError(f"duplicate delivery progress unit id: {unit.unit_id}")
        seen.add(unit.unit_id)


def _validate_unit_progress(unit: DeliveryUnitProgress) -> None:
    if not unit.unit_id.strip():
        raise ValueError("delivery progress unit_id must be non-empty")
    if unit.status not in DELIVERY_UNIT_STATUSES:
        raise ValueError(f"unknown delivery progress status: {unit.status}")


def _validate_event(event: DeliveryProgressEvent) -> None:
    _validate_plan_id_for_path(event.plan_id)
    if event.schema_version != SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION:
        raise ValueError(
            "unsupported delivery progress event schema_version "
            f"{event.schema_version}; expected {SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION}"
        )
    if not _DELIVERY_EVENT_TYPE_RE.fullmatch(event.event_type):
        raise ValueError("delivery progress event_type must be a stable code")
    if event.status and event.status not in DELIVERY_UNIT_STATUSES:
        raise ValueError(f"unknown delivery progress event status: {event.status}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def render_delivery_status(result: DeliveryStatusResult) -> str:
    lines = [
        f"Delivery plan status: {result.plan_path}",
        f"Status: {result.status}",
    ]
    if result.project_root:
        lines.append(f"Project root: {result.project_root}")
    if result.progress_path:
        progress_note = "present" if result.progress_exists else "not created yet"
        lines.append(f"Progress: {result.progress_path} ({progress_note})")
    if result.plan:
        lines.extend(
            [
                f"Plan ID: {result.plan.plan_id}",
                f"Title: {result.plan.title}",
                f"Final branch: {result.plan.final_branch}",
            ]
        )

    if result.units:
        lines.append("")
        lines.append("Units:")
        for unit in result.units:
            detail = unit.status
            if unit.blocked_by:
                detail += f" (blocked by: {', '.join(unit.blocked_by)})"
            elif unit.eligible:
                detail += " (eligible)"
            if unit.child_task_id:
                detail += f" task={unit.child_task_id}"
            if unit.branch:
                detail += f" branch={unit.branch}"
            if unit.commit:
                detail += f" commit={unit.commit}"
            title = f" — {unit.title}" if unit.title else ""
            lines.append(f"- {unit.id}: {detail}{title}")

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
    if result.next_action:
        lines.append("")
        lines.append(f"Next action: {result.next_action}")
    return "\n".join(lines) + "\n"


def _load_delivery_progress(path: Path, *, plan_id: str, errors: list[DeliveryPlanIssue]) -> DeliveryProgress | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(DeliveryPlanIssue("error", "progress.read_failed", f"Failed to read progress file: {exc}"))
        return None
    except json.JSONDecodeError as exc:
        errors.append(DeliveryPlanIssue("error", "progress.parse_failed", f"Failed to parse progress JSON: {exc}"))
        return None
    if not isinstance(data, dict):
        errors.append(DeliveryPlanIssue("error", "progress.invalid_type", "Progress file must be a JSON object."))
        return None

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "progress.schema_version_missing",
                "Progress schema_version must be an integer.",
                "schema_version",
            )
        )
        return None
    if schema_version != SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "progress.schema_version_unsupported",
                (
                    f"Unsupported delivery progress schema_version {schema_version}; "
                    f"expected {SUPPORTED_DELIVERY_PROGRESS_SCHEMA_VERSION}."
                ),
                "schema_version",
            )
        )
        return None

    progress_plan_id = data.get("plan_id")
    if progress_plan_id != plan_id:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "progress.plan_id_mismatch",
                f"Progress plan_id must match delivery plan id: {plan_id}",
                "plan_id",
            )
        )
        return None

    raw_units = data.get("units", [])
    if not isinstance(raw_units, list):
        errors.append(
            DeliveryPlanIssue("error", "progress.units_invalid_type", "Progress units must be a list.", "units")
        )
        return None

    units: list[DeliveryUnitProgress] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw_units):
        unit = _parse_progress_unit(item, idx=idx, errors=errors)
        if unit is None:
            continue
        if unit.unit_id in seen:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "progress.unit_duplicate",
                    f"Duplicate progress unit id: {unit.unit_id}",
                    f"units[{idx}].unit_id",
                )
            )
            continue
        seen.add(unit.unit_id)
        units.append(unit)
    if errors:
        return None
    return DeliveryProgress(schema_version=schema_version, plan_id=plan_id, units=units)


def _parse_progress_unit(
    item: Any,
    *,
    idx: int,
    errors: list[DeliveryPlanIssue],
) -> DeliveryUnitProgress | None:
    path = f"units[{idx}]"
    if not isinstance(item, dict):
        errors.append(
            DeliveryPlanIssue("error", "progress.unit_invalid_type", "Progress unit must be an object.", path)
        )
        return None
    unit_id = _required_string(item, "unit_id", f"{path}.unit_id", errors)
    status = _required_string(item, "status", f"{path}.status", errors)
    if status and status not in DELIVERY_UNIT_STATUSES:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "progress.status_unknown",
                f"Unknown progress status: {status}",
                f"{path}.status",
            )
        )
    optional = {
        key: _optional_string(item, key, f"{path}.{key}", errors)
        for key in (
            "child_task_id",
            "branch",
            "commit",
            "waiting_reason",
            "failure_code",
            "started_at",
            "completed_at",
            "updated_at",
        )
    }
    if unit_id is None or status is None:
        return None
    return DeliveryUnitProgress(unit_id=unit_id, status=status, **optional)


def _required_string(data: dict[str, Any], key: str, path: str, errors: list[DeliveryPlanIssue]) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(DeliveryPlanIssue("error", f"{path}.missing", f"{key} must be a non-empty string.", path))
        return None
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, path: str, errors: list[DeliveryPlanIssue]) -> str | None:
    if key not in data or data.get(key) is None:
        return None
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(DeliveryPlanIssue("error", f"{path}.invalid_type", f"{key} must be a non-empty string.", path))
        return None
    return value.strip()


def _build_status_units(
    plan: DeliveryPlan,
    progress: DeliveryProgress | None,
    warnings: list[DeliveryPlanIssue],
) -> list[DeliveryStatusUnit]:
    progress_by_id = {unit.unit_id: unit for unit in progress.units} if progress else {}
    plan_unit_ids = {unit.id for unit in plan.units}
    for unit_id in progress_by_id:
        if unit_id not in plan_unit_ids:
            warnings.append(
                DeliveryPlanIssue(
                    "warning",
                    "progress.unit_unknown",
                    f"Progress references unknown unit id: {unit_id}",
                    "units",
                )
            )

    done_ids = {unit_id for unit_id, unit in progress_by_id.items() if unit.status == "done"}
    status_units: list[DeliveryStatusUnit] = []
    for plan_unit in plan.units:
        progress_unit = progress_by_id.get(plan_unit.id)
        status = progress_unit.status if progress_unit else "pending"
        blocked_by = [dependency for dependency in plan_unit.depends_on if dependency not in done_ids]
        if status != "pending":
            blocked_by = []
        status_units.append(
            DeliveryStatusUnit(
                id=plan_unit.id,
                status=status,
                title=plan_unit.title,
                task_path=plan_unit.task_path,
                depends_on=list(plan_unit.depends_on),
                blocked_by=blocked_by,
                stream=plan_unit.stream,
                platform=plan_unit.platform,
                phase=plan_unit.phase,
                kind=plan_unit.kind,
                repo_id=plan_unit.repo_id,
                child_task_id=progress_unit.child_task_id if progress_unit else None,
                branch=progress_unit.branch if progress_unit else None,
                commit=progress_unit.commit if progress_unit else None,
                waiting_reason=progress_unit.waiting_reason if progress_unit else None,
                failure_code=progress_unit.failure_code if progress_unit else None,
                started_at=progress_unit.started_at if progress_unit else None,
                completed_at=progress_unit.completed_at if progress_unit else None,
                updated_at=progress_unit.updated_at if progress_unit else None,
            )
        )
    return status_units


def _overall_status(units: list[DeliveryStatusUnit]) -> str:
    if not units:
        return "pending"
    statuses = {unit.status for unit in units}
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if "waiting" in statuses:
        return "waiting"
    if "canceled" in statuses:
        return "canceled"
    if statuses == {"done"}:
        return "done"
    return "pending"


def _next_action(status: str, units: list[DeliveryStatusUnit]) -> str:
    if status == "invalid":
        return "fix delivery plan status errors"
    if status == "running":
        return "wait for the running delivery unit"
    if status == "failed":
        return "inspect the failed delivery unit"
    if status == "waiting":
        return "answer the blocking delivery question or setup requirement"
    if status == "canceled":
        return "inspect canceled delivery progress"
    if status == "done":
        return "review final delivery branch"
    if any(unit.eligible for unit in units):
        return "prepare or run an eligible delivery unit with the existing task workflow"
    return "complete prerequisite delivery units"


def _format_issue(issue: DeliveryPlanIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"
