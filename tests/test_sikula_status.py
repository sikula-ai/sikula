"""Tests for sikula.py — status command and CLI flags."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
import sikula_cli.status as status_cli

cmd_status = status_cli.cmd_status
_pid_running = status_cli._pid_running

_SIKULA_PY = str(Path(__file__).parent.parent / "sikula.py")


def test_status_cli_module_imports() -> None:
    import sikula_cli.status as status_cli

    assert callable(status_cli.register_parser)
    assert callable(status_cli.cmd_status)
    assert callable(status_cli.cmd_show)


class TestStatusCliModule:
    def test_default_current_branch_delivery_helpers(self):
        from core.state import TaskState
        import sikula_cli.status as status_cli

        state = TaskState(
            task_id="delivery",
            task_description="current branch task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status="failed",
            worktree_base="/tmp/worktree",
        )

        assert status_cli._status_label(state) == "delivery failed"
        assert status_cli._status_next_action(state, "delivery failed") == "sikula run --task-id delivery"

        state.review_delivery_status = "pending"
        state.worktree_base = None
        assert status_cli._status_label(state) == "CLEANED"

        state.review_delivery_status = "delivered"
        assert status_cli._status_label(state) == "DONE"

    def test_default_contract_gate_helpers(self):
        from core.state import TaskState
        import sikula_cli.status as status_cli

        state = TaskState(task_id="blocked", task_description="blocked task", failed=True)
        state.contract_gate_blocked = True
        assert status_cli._status_next_action(state, "FAILED") == "sikula show blocked"

        state.implementation_contract = {"source": {"path": ".sikula/tasks/task.md"}}
        assert (
            status_cli._status_next_action(state, "FAILED")
            == "sikula contract check .sikula/tasks/task.md --write-report"
        )

    def test_default_delivery_child_without_worktree_helper(self, tmp_path: Path):
        from core.state import TaskState
        import sikula_cli.status as status_cli

        state = TaskState(
            task_id="delivery-child",
            task_description="delivery child",
            failed=True,
            delivery_plan_id="plan-123",
            delivery_unit_id="unit-456",
        )
        assert status_cli._status_next_action(state, "FAILED") == "sikula show delivery-child"

        state.worktree_path = str(tmp_path / "missing-worktree")
        assert status_cli._status_next_action(state, "FAILED") == "sikula show delivery-child"

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        state.worktree_path = str(worktree)
        assert status_cli._status_next_action(state, "FAILED") == "sikula run --task-id delivery-child --reset-failed"

    def test_active_operation_fallback_helpers(self):
        from datetime import datetime, timedelta, timezone
        import sikula_cli.status as status_cli

        assert status_cli._active_operation_label({"phase": "build", "scope": "final_full_task"}) == "final build"
        assert status_cli._active_operation_is_fresh({"last_heartbeat_at": "not-a-date"}) is False
        assert status_cli._active_operation_elapsed(None) is None
        assert status_cli._active_operation_elapsed({"started_at": "not-a-date"}) is None

        started = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        assert status_cli._active_operation_elapsed({"started_at": started}) == "2m"

        updated = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = type("State", (), {"updated_at": updated})()
        assert status_cli._status_updated(state) == "2h ago"

    def test_module_cmd_status_empty_and_no_match(self, tmp_path: Path, capsys):
        import sikula_cli.status as status_cli
        from core.state import JsonStateStore, TaskState

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        status_cli.cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))
        assert capsys.readouterr().out.strip() == "[]"

        store = JsonStateStore(tmp_path)
        store.save(TaskState(task_id="done", task_description="done task", done=True))
        status_cli.cmd_status(cfg, argparse.Namespace(json=False, verbose=False, status_filter=["failed"]))
        assert capsys.readouterr().out.strip() == "No matching tasks."


class TestVersionFlag:
    def test_version_flag_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, _SIKULA_PY, "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "sikula" in result.stdout.lower() or "sikula" in result.stderr.lower()

    def test_version_flag_includes_version_number(self):
        result = subprocess.run(
            [sys.executable, _SIKULA_PY, "--version"],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        assert any(c.isdigit() for c in output) or "dev" in output.lower()


class TestPidRunning:
    def test_current_process_is_running(self):
        import os

        assert _pid_running(os.getpid()) is True

    def test_nonexistent_pid_returns_false(self):
        assert _pid_running(999999999) is False


class TestCmdStatusInterrupted:
    def test_in_progress_task_with_dead_pid_shows_interrupted(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True
        s.pid = 999999999  # nonexistent PID
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        assert "INTERRUPTED" in out

    def test_interrupted_task_does_not_show_stale_active_operation(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True
        s.pid = 999999999
        s.start_active_operation("agent", agent="reviewer", message="Running reviewer agent")
        s.active_operation["last_heartbeat_at"] = "2024-01-01T00:00:00+00:00"
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=True, status_filter=[]))

        out = capsys.readouterr().out
        assert "INTERRUPTED" in out
        assert "active: Running reviewer agent" not in out

    def test_fresh_active_operation_wins_when_pid_is_not_visible(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True
        s.pid = 999999999
        s.start_active_operation(
            "agent",
            agent="reviewer",
            message="Running reviewer agent",
            heartbeat_interval_seconds=15,
        )
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=True, status_filter=[]))

        out = capsys.readouterr().out
        assert "reviewer" in out
        assert "INTERRUPTED" not in out
        assert "active: Running reviewer agent" in out
        assert "next: wait" in out

    def test_interrupted_report_only_review_suggests_rerun_review(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="Review branch changes")
        s.review_mode = "review_report"
        s.plan_decided = True
        s.pid = 999999999
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "INTERRUPTED"
        assert rows[0]["next_action"] == "re-run sikula review"

    def test_live_report_only_review_without_heartbeat_waits(self, tmp_path: Path, capsys):
        import os
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="Review branch changes")
        s.review_mode = "review_report"
        s.plan_decided = True
        s.pid = os.getpid()
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["next_action"] == "wait"

    def test_stale_report_only_review_suggests_rerun_review(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="Review branch changes")
        s.review_mode = "review_report"
        s.plan_decided = True
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["next_action"] == "re-run sikula review"

    def test_in_progress_task_with_live_pid_shows_phase_status(self, tmp_path: Path, capsys):
        import os
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True
        s.pid = os.getpid()  # current process — alive
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        assert "implementing" in out
        assert "INTERRUPTED" not in out

    def test_in_progress_task_without_pid_shows_phase_status(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True  # no pid set — old task before this feature
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        assert "implementing" in out
        assert "INTERRUPTED" not in out

    def test_cleaned_isolated_task_shows_cleaned(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan_decided = True
        s.worktree_branch = "sikula/my-task-t1"
        s.pid = 999999999
        s.record("sikula", "cleanup", "worktree removed")
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        assert "CLEANED" in out
        assert "INTERRUPTED" not in out


class TestCmdStatusOrdering:
    def test_tasks_sorted_chronologically(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s1 = TaskState(task_id="t1", task_description="first task")
        s1.created_at = "2024-01-01T00:00:00+00:00"
        s2 = TaskState(task_id="t2", task_description="second task")
        s2.created_at = "2024-01-03T00:00:00+00:00"
        s3 = TaskState(task_id="t3", task_description="third task")
        s3.created_at = "2024-01-02T00:00:00+00:00"
        for s in [s2, s3, s1]:
            store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "task" in ln]
        assert lines[0].startswith("t1")
        assert lines[1].startswith("t3")
        assert lines[2].startswith("t2")


class TestCmdStatusOutput:
    def test_status_includes_step_and_updated_columns(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.plan = ["first", "second", "third"]
        s.current_step = 1
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg)

        out = capsys.readouterr().out
        assert "STEP" in out
        assert "UPDATED" in out
        assert "2/3" in out

    def test_verbose_status_includes_next_action(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.pid = 999999999
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=True, status_filter=[]))

        out = capsys.readouterr().out
        assert "next: sikula run --task-id t1" in out

    def test_verbose_status_includes_active_operation(self, tmp_path: Path, capsys):
        import os
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.pid = os.getpid()
        s.start_active_operation("agent", agent="reviewer", message="Running reviewer agent")
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=True, status_filter=[]))

        out = capsys.readouterr().out
        assert "reviewer" in out
        assert "active: Running reviewer agent" in out

    def test_status_json_includes_derived_fields(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.failed = True
        s.plan = ["first", "second"]
        s.current_step = 0
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows == [
            {
                "id": "t1",
                "status": "FAILED",
                "step": "1/2",
                "build": None,
                "updated": s.updated_at,
                "updated_human": rows[0]["updated_human"],
                "task": "my task",
                "next_action": "sikula run --task-id t1 --reset-failed",
            }
        ]

    def test_status_failed_report_only_review_suggests_rerun_review(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="Review branch changes")
        s.failed = True
        s.review_mode = "review_report"
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "FAILED"
        assert rows[0]["next_action"] == "re-run sikula review"

    def test_status_contract_gate_failed_suggests_contract_check_not_reset(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.failed = True
        s.contract_gate_blocked = True
        s.implementation_contract = {"source": {"path": ".sikula/tasks/my-task.md"}}
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=True, status_filter=[]))

        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "next: sikula contract check .sikula/tasks/my-task.md --write-report" in out
        assert "--reset-failed" not in out

    def test_status_json_contract_gate_failed_next_action(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.failed = True
        s.contract_gate_blocked = True
        s.implementation_contract = {"source": {"path": ".sikula/tasks/my-task.md"}}
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "FAILED"
        assert rows[0]["next_action"] == "sikula contract check .sikula/tasks/my-task.md --write-report"

    def test_status_json_delivery_child_without_worktree_next_action(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(
            task_id="t1",
            task_description="orphan delivery child",
            delivery_plan_id="my-plan-123",
            delivery_unit_id="unit-456",
            delivery_plan_path=".sikula/delivery/plan.yaml",
        )
        s.failed = True
        s.worktree_path = str(tmp_path / "missing-worktree")
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "FAILED"
        assert rows[0]["next_action"] == "sikula show t1"

    @pytest.mark.parametrize(
        ("delivery_status", "expected_status"),
        [
            (None, "delivery pending"),
            ("pending", "delivery pending"),
            ("committed", "delivery pending"),
            ("failed", "delivery failed"),
        ],
    )
    def test_status_json_current_branch_delivery_needs_retry_action(
        self,
        tmp_path: Path,
        capsys,
        delivery_status: str | None,
        expected_status: str,
    ):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(
            task_id="t1",
            task_description="Review branch changes",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
            worktree_path=str(tmp_path / "worktree"),
            worktree_base=str(tmp_path / "worktree"),
            worktree_branch="feature/current",
        )
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == expected_status
        assert rows[0]["next_action"] == "sikula run --task-id t1"

    def test_status_json_cleaned_current_branch_delivery_is_audit_only(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(
            task_id="t1",
            task_description="Review branch changes",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status="failed",
            worktree_branch="feature/current",
        )
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "CLEANED"
        assert rows[0]["next_action"] == "sikula show t1"

    @pytest.mark.parametrize("delivery_status", ["delivered", "no_changes"])
    def test_status_json_current_branch_terminal_delivery_is_done(
        self,
        tmp_path: Path,
        capsys,
        delivery_status: str,
    ):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(
            task_id="t1",
            task_description="Review branch changes",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
            worktree_branch="feature/current",
        )
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "DONE"
        assert rows[0]["next_action"] == "review branch"

    def test_status_json_includes_active_operation_when_present(self, tmp_path: Path, capsys):
        import os
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        s.pid = os.getpid()
        s.start_active_operation("agent", agent="test_writer", message="Running test writer")
        store.save(s)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=True, verbose=False, status_filter=[]))

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "test_writer"
        assert rows[0]["active_operation"]["agent"] == "test_writer"
        assert rows[0]["active_operation"]["message"] == "Running test writer"
        assert rows[0]["active_elapsed"]

    def test_status_filters_are_or_filters(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        done = TaskState(task_id="done", task_description="done task")
        done.done = True
        failed = TaskState(task_id="failed", task_description="failed task")
        failed.failed = True
        active = TaskState(task_id="active", task_description="active task")
        active.plan_decided = True
        for state in [done, failed, active]:
            store.save(state)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=False, status_filter=["active", "failed"]))

        out = capsys.readouterr().out
        assert "active task" in out
        assert "failed task" in out
        assert "done task" not in out

    def test_failed_filter_includes_current_branch_delivery_failed(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        delivery_failed = TaskState(
            task_id="delivery",
            task_description="current branch task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status="failed",
            worktree_path=str(tmp_path / "worktree"),
            worktree_base=str(tmp_path / "worktree"),
        )
        done = TaskState(task_id="done", task_description="done task", done=True)
        for state in [delivery_failed, done]:
            store.save(state)

        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_status(cfg, argparse.Namespace(json=False, verbose=False, status_filter=["failed"]))

        out = capsys.readouterr().out
        assert "current branch task" in out
        assert "delivery failed" in out
        assert "done task" not in out
