"""Status and task-state display commands for the Sikula CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from core.state import JsonStateStore
from sikula_cli.config import _resolve_state_dir


def register_parser(subparsers) -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    status_p = subparsers.add_parser("status", help="List all tasks")
    status_p.add_argument("--json", action="store_true", default=False, help="Print task status rows as JSON")
    status_p.add_argument("--verbose", action="store_true", default=False, help="Include next suggested action")
    status_p.add_argument(
        "--active",
        dest="status_filter",
        action="append_const",
        const="active",
        default=[],
        help="Show only active or interrupted tasks",
    )
    status_p.add_argument(
        "--done",
        dest="status_filter",
        action="append_const",
        const="done",
        help="Show only completed tasks",
    )
    status_p.add_argument(
        "--failed",
        dest="status_filter",
        action="append_const",
        const="failed",
        help="Show only failed tasks",
    )
    status_p.add_argument(
        "--cleaned",
        dest="status_filter",
        action="append_const",
        const="cleaned",
        help="Show only cleaned audit-only tasks",
    )

    show_p = subparsers.add_parser("show", help="Show full task state as JSON")
    show_p.add_argument("task_id")
    return status_p, show_p


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _default_current_branch_delivery_terminal(state: object) -> bool:
    return state.review_delivery_status in {"delivered", "no_changes"}


def _default_current_branch_delivery_pending(state: object) -> bool:
    return (
        state.review_mode == "review_fix"
        and state.review_delivery_mode == "current_branch"
        and not _default_current_branch_delivery_terminal(state)
    )


def _default_current_branch_delivery_needs_finalization(state: object) -> bool:
    return bool(
        state.done
        and not state.failed
        and _default_current_branch_delivery_pending(state)
        and (state.worktree_base or state.worktree_path)
    )


def _default_current_branch_delivery_cleaned(state: object) -> bool:
    return bool(
        state.done
        and not state.failed
        and _default_current_branch_delivery_pending(state)
        and not state.worktree_base
        and not state.worktree_path
    )


def _default_contract_gate_task_path(state) -> str | None:
    snapshot = getattr(state, "implementation_contract", None)
    if not isinstance(snapshot, dict):
        return None
    source = snapshot.get("source")
    if not isinstance(source, dict):
        return None
    path = source.get("path")
    return path if isinstance(path, str) and path.strip() else None


def _default_contract_gate_blocked_without_worktree(state) -> bool:
    return bool(
        getattr(state, "contract_gate_blocked", False)
        and not getattr(state, "worktree_path", None)
        and not getattr(state, "worktree_branch", None)
    )


def _default_contract_gate_next_action(state) -> str:
    path = _default_contract_gate_task_path(state)
    if path:
        return f"sikula contract check {path} --write-report"
    return f"sikula show {state.task_id}"


def _default_delivery_child_without_worktree(state) -> bool:
    if not (getattr(state, "delivery_plan_id", None) and getattr(state, "delivery_unit_id", None)):
        return False
    worktree_path = getattr(state, "worktree_path", None)
    if not worktree_path:
        return True
    try:
        return not Path(worktree_path).exists()
    except OSError:
        return True


@dataclass(frozen=True)
class StatusContext:
    resolve_state_dir: Callable[[dict], Path] = _resolve_state_dir
    current_branch_delivery_needs_finalization: Callable[[object], bool] = (
        _default_current_branch_delivery_needs_finalization
    )
    current_branch_delivery_cleaned: Callable[[object], bool] = _default_current_branch_delivery_cleaned
    contract_gate_blocked_without_worktree: Callable[[object], bool] = _default_contract_gate_blocked_without_worktree
    contract_gate_next_action: Callable[[object], str] = _default_contract_gate_next_action
    delivery_child_without_worktree: Callable[[object], bool] = _default_delivery_child_without_worktree
    pid_running: Callable[[int], bool] = _pid_running


def _status_context(context: StatusContext | None = None) -> StatusContext:
    return context or StatusContext()


def _status_label(state, context: StatusContext | None = None) -> str:
    context = _status_context(context)
    if context.current_branch_delivery_needs_finalization(state):
        return "delivery failed" if state.review_delivery_status == "failed" else "delivery pending"
    if context.current_branch_delivery_cleaned(state):
        return "CLEANED"
    if state.done:
        return "DONE"
    if state.failed:
        return "FAILED"
    if state.worktree_branch and not state.worktree_path:
        return "CLEANED"
    if state.active_operation and _active_operation_is_fresh(state.active_operation):
        return _active_operation_label(state.active_operation)
    if state.pid and not context.pid_running(state.pid):
        return "INTERRUPTED"
    if state.active_operation:
        return _active_operation_label(state.active_operation)
    final_scope = state.active_scope == "final_full_task"
    if state.build_status == "failed":
        return "final build failed" if final_scope else "build failed"
    if state.build_iterations and state.build_status != "success":
        return "final building" if final_scope else "building"
    if state.tests_up_to_date:
        return "final validation" if final_scope else "testing"
    if state.security_approved:
        return "final test writing" if final_scope else "writing tests"
    if state.review_approved:
        return "final security review" if final_scope else "security review"
    if state.files_changed:
        return "final review" if final_scope else "reviewing"
    if state.plan_decided:
        return "implementing"
    if state.presync_done:
        return "analyzing"
    return "starting"


def _active_operation_label(active_operation: dict) -> str:
    agent = active_operation.get("agent")
    if agent:
        return str(agent)
    phase = str(active_operation.get("phase", "running"))
    if active_operation.get("scope") == "final_full_task":
        return f"final {phase}"
    return phase


def _active_operation_is_fresh(active_operation: dict) -> bool:
    last_heartbeat_at = active_operation.get("last_heartbeat_at")
    try:
        last_heartbeat = datetime.fromisoformat(last_heartbeat_at)
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        age_s = max(0, int((datetime.now(timezone.utc) - last_heartbeat.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return False
    interval_s = int(active_operation.get("heartbeat_interval_seconds") or 60)
    return age_s <= max(120, interval_s * 2 + 10)


def _active_operation_elapsed(active_operation: dict | None) -> str | None:
    if not active_operation:
        return None
    started_at = active_operation.get("started_at")
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    return f"{elapsed // 3600}h {elapsed % 3600 // 60}m"


def _status_step(state) -> str:
    if not state.plan:
        return "-"
    total = len(state.plan)
    current = max(1, min(state.current_step + 1, total))
    return f"{current}/{total}"


def _status_updated(state) -> str:
    try:
        updated = datetime.fromisoformat(state.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return "-"
    if elapsed < 60:
        return f"{elapsed}s ago"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"


def _status_next_action(state, status: str, context: StatusContext | None = None) -> str:
    context = _status_context(context)
    if context.current_branch_delivery_needs_finalization(state):
        return f"sikula run --task-id {state.task_id}"
    if status == "DONE":
        return "review branch" if state.worktree_branch else "review changes"
    if status == "FAILED":
        if state.review_mode == "review_report":
            return "re-run sikula review"
        if context.contract_gate_blocked_without_worktree(state):
            return context.contract_gate_next_action(state)
        if context.delivery_child_without_worktree(state):
            return f"sikula show {state.task_id}"
        return f"sikula run --task-id {state.task_id} --reset-failed"
    if state.review_mode == "review_report":
        if state.active_operation and _active_operation_is_fresh(state.active_operation):
            return "wait"
        if state.pid and context.pid_running(state.pid):
            return "wait"
        return "re-run sikula review"
    if status == "CLEANED":
        return f"sikula show {state.task_id}"
    if status == "INTERRUPTED":
        return f"sikula run --task-id {state.task_id}"
    if state.active_operation and _active_operation_is_fresh(state.active_operation):
        return "wait"
    return "wait" if state.pid and context.pid_running(state.pid) else f"sikula run --task-id {state.task_id}"


def _status_row(state, context: StatusContext | None = None) -> dict:
    context = _status_context(context)
    status = _status_label(state, context)
    task_label = state.task_file
    if not task_label:
        task_label = state.task_description.splitlines()[0][:60] if state.task_description else "(no description)"
    row = {
        "id": state.task_id,
        "status": status,
        "step": _status_step(state),
        "build": state.build_iterations if state.build_iterations else None,
        "updated": state.updated_at,
        "updated_human": _status_updated(state),
        "task": task_label,
        "next_action": _status_next_action(state, status, context),
    }
    if state.active_operation and (status != "INTERRUPTED" or _active_operation_is_fresh(state.active_operation)):
        row["active_operation"] = state.active_operation
        row["active_elapsed"] = _active_operation_elapsed(state.active_operation)
    return row


def _status_matches(row: dict, filters: set[str]) -> bool:
    if not filters:
        return True
    status = row["status"].lower().replace(" ", "_")
    if status in filters:
        return True
    if "failed" in filters and status == "delivery_failed":
        return True
    if "active" in filters and row["status"] not in {"DONE", "FAILED", "CLEANED"}:
        return True
    return False


def cmd_status(cfg: dict, args: argparse.Namespace | None = None, context: StatusContext | None = None) -> None:
    context = _status_context(context)
    args = args or argparse.Namespace(json=False, verbose=False, status_filter=[])
    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    tasks = store.list_tasks()
    if not tasks:
        if args.json:
            print("[]")
        else:
            print("No tasks yet.")
        return
    states = [s for tid in tasks if (s := store.load(tid)) is not None]
    states.sort(key=lambda s: s.created_at)
    filters = {f.lower().replace("-", "_") for f in args.status_filter}
    rows = [row for s in states if _status_matches((row := _status_row(s, context)), filters)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No matching tasks.")
        return
    if args.verbose:
        print(f"{'ID':<32}  {'STATUS':<16}  {'STEP':>5}  {'BUILD':>5}  {'UPDATED':>8}  TASK")
        for row in rows:
            build_col = str(row["build"]) if row["build"] is not None else "-"
            print(
                f"{row['id']:<32}  {row['status']:<16}  {row['step']:>5}  "
                f"{build_col:>5}  {row['updated_human']:>8}  {row['task']}"
            )
            if row.get("active_operation"):
                active = row["active_operation"]
                elapsed = row.get("active_elapsed") or "-"
                message = active.get("message") or row["status"]
                print(f"{'':<32}  active: {message} ({elapsed})")
            print(f"{'':<32}  next: {row['next_action']}")
        return
    print(f"{'ID':<32}  {'STATUS':<16}  {'STEP':>5}  {'BUILD':>5}  {'UPDATED':>8}  TASK")
    for row in rows:
        build_col = str(row["build"]) if row["build"] is not None else "-"
        print(
            f"{row['id']:<32}  {row['status']:<16}  {row['step']:>5}  "
            f"{build_col:>5}  {row['updated_human']:>8}  {row['task']}"
        )


def cmd_show(task_id: str, cfg: dict, context: StatusContext | None = None) -> None:
    context = _status_context(context)
    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = store.load(task_id)
    if not state:
        print(f"Task {task_id} not found")
        sys.exit(1)
    print(json.dumps(state.__dict__, indent=2))
