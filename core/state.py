"""State model and persistence abstraction.

JsonStateStore is the default implementation. To migrate to a database,
subclass StateStore and swap it in sikula.py — nothing else needs to change.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    task_file: Optional[str] = None
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    worktree_base: Optional[str] = None
    history: list[dict] = field(default_factory=list)
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
        state.updated_at = _now()
        self._path(state.task_id).write_text(json.dumps(asdict(state), indent=2))

    def list_tasks(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [p.stem for p in sorted(self._dir.glob("*.json"))]

    def delete(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)
