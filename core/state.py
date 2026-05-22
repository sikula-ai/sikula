"""State model and persistence abstraction.

JsonStateStore is the default implementation. To migrate to a database,
subclass StateStore and swap it in sikula.py — nothing else needs to change.
"""

from __future__ import annotations

import json
import platform
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_text(text: str | None, limit: int = 1000) -> str | None:
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[-limit:]


def runtime_metadata_snapshot() -> dict:
    try:
        sikula_version = version("sikula")
    except PackageNotFoundError:
        sikula_version = "unknown"
    return {
        "sikula_version": sikula_version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _terminal_result(state: "TaskState") -> str:
    if state.done:
        return "done"
    if state.failed:
        return "failed"
    return "incomplete"


def _final_summary(state: "TaskState") -> dict:
    created_at = _parse_iso(state.created_at)
    finished_at = _parse_iso(state.finished_at)
    summary: dict = {
        "result": _terminal_result(state),
        "task_id": state.task_id,
        "branch": state.worktree_branch,
        "commit": state.result_commit,
        "build_attempts": state.build_iterations,
        "build_status": state.build_status,
        "test_status": state.test_status,
        "check_status": state.check_status,
        "files_changed_count": len(state.files_changed),
        "test_files_written_count": len(state.test_files_written),
        "validation_records_count": len(state.validation_cycle_records),
        "validation_failures_count": sum(
            1 for entry in state.validation_cycle_records if entry.get("status") == "failed"
        ),
        "fix_attempts": len(state.fix_cycle_records),
        "review_records_count": len(state.review_cycle_records),
        "reviewer_runs": sum(1 for entry in state.review_cycle_records if entry.get("reviewer") == "reviewer"),
        "security_reviewer_runs": sum(
            1 for entry in state.review_cycle_records if entry.get("reviewer") == "security_reviewer"
        ),
        "test_writer_runs": len(state.test_write_records),
        "llm_retries": sum(1 for entry in state.history if entry.get("action") == "llm_retry"),
        "history_events_count": len(state.history),
        "created_at": state.created_at,
        "finished_at": state.finished_at,
    }
    if created_at and finished_at:
        summary["wall_elapsed_s"] = round((finished_at - created_at).total_seconds(), 1)
    return summary


SCHEMA_VERSION = 1


@dataclass
class TaskState:
    task_id: str
    task_description: str
    schema_version: int = SCHEMA_VERSION
    config_snapshot: dict = field(default_factory=dict)
    analyst_prompt: Optional[str] = None
    planner_prompt: Optional[str] = None
    implementation_prompt: Optional[str] = None
    presync_done: bool = False
    plan: list[str] = field(default_factory=list)
    plan_decided: bool = False
    current_step: int = 0
    step_implemented: bool = False
    files_changed: list[str] = field(default_factory=list)
    build_synced: bool = False
    build_iterations: int = 0  # counts only build/fix cycles; guarded by max_iterations
    build_status: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    test_errors: list[str] = field(default_factory=list)
    check_errors: list[str] = field(default_factory=list)
    review_issues: list[str] = field(default_factory=list)
    review_iterations: int = 0
    review_approved: bool = False
    security_approved: bool = False
    security_review_iterations: int = 0
    analyst_warnings: list[str] = field(default_factory=list)
    review_diff: Optional[str] = None
    review_mode: Optional[str] = None
    review_base_branch: Optional[str] = None
    test_files_written: list[str] = field(default_factory=list)
    tests_up_to_date: bool = False
    fixer_changed_code: bool = (
        False  # set when fixer writes files; cleared after build validates; guards resume skip condition
    )
    # Structured observability records — one entry per agent invocation; never read for pipeline decisions
    implement_cycle_records: list[dict] = field(default_factory=list)
    review_cycle_records: list[dict] = field(default_factory=list)
    test_write_records: list[dict] = field(default_factory=list)
    fix_cycle_records: list[dict] = field(default_factory=list)
    validation_cycle_records: list[dict] = field(default_factory=list)
    task_file: Optional[str] = None
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    worktree_base: Optional[str] = None
    history: list[dict] = field(default_factory=list)
    runtime_metadata: dict = field(default_factory=dict)
    final_summary: dict = field(default_factory=dict)
    done: bool = False
    failed: bool = False
    finished_at: Optional[str] = None
    result_commit: Optional[str] = None
    test_status: Optional[str] = None
    check_status: Optional[str] = None
    pid: Optional[int] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def record(
        self,
        agent: str,
        action: str,
        result: str,
        elapsed_s: float | None = None,
        error: str | None = None,
    ) -> None:
        entry: dict = {
            "agent": agent,
            "action": action,
            "result": result,
            "timestamp": _now(),
        }
        if elapsed_s is not None:
            entry["elapsed_s"] = round(elapsed_s, 1)
        if error:
            entry["error"] = error
        self.history.append(entry)

    def record_validation(
        self,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
        error: str | None = None,
        check_name: str | None = None,
    ) -> None:
        entry: dict = {
            "phase": phase,
            "status": status,
            "build_iteration": self.build_iterations,
            "step": self.current_step,
            "timestamp": _now(),
        }
        if check_name:
            entry["check_name"] = check_name
        if elapsed_s is not None:
            entry["elapsed_s"] = round(elapsed_s, 1)
        error_excerpt = _short_text(error)
        if error_excerpt:
            entry["error_excerpt"] = error_excerpt
        self.validation_cycle_records.append(entry)


# ---------------------------------------------------------------------------
# Abstract store
# ---------------------------------------------------------------------------


class StateStore:
    def load(self, task_id: str) -> Optional[TaskState]:
        raise NotImplementedError

    def save(self, state: TaskState) -> None:
        raise NotImplementedError

    def list_tasks(self) -> list[str]:
        raise NotImplementedError

    def delete(self, task_id: str) -> None:
        raise NotImplementedError

    def create(self, task_description: str) -> TaskState:
        task_id = uuid.uuid4().hex
        state = TaskState(task_id=task_id, task_description=task_description)
        state.runtime_metadata = runtime_metadata_snapshot()
        self.save(state)
        return state


# ---------------------------------------------------------------------------
# JSON implementation
# ---------------------------------------------------------------------------


class JsonStateStore(StateStore):
    """Stores each task as a <task_id>.json file. Concurrent access to different tasks is safe; running the same task_id twice concurrently is not."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def load(self, task_id: str) -> Optional[TaskState]:
        p = self._path(task_id)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        # --- schema migrations (run in version order before TaskState is constructed) ---
        # Migrate field renamed in refactor: gradle_synced → build_synced
        if "gradle_synced" in data and "build_synced" not in data:
            data["build_synced"] = data.pop("gradle_synced")
        # --- end migrations ---
        # Drop unknown fields (forward-compat with state files from older versions)
        known = {f.name for f in TaskState.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in known}
        return TaskState(**data)

    def save(self, state: TaskState) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if (state.done or state.failed) and not state.finished_at:
            state.finished_at = _now()
        if state.done or state.failed:
            state.final_summary = _final_summary(state)
        state.updated_at = _now()
        self._path(state.task_id).write_text(json.dumps(asdict(state), indent=2))

    def list_tasks(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [p.stem for p in sorted(self._dir.glob("*.json"))]

    def delete(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)
