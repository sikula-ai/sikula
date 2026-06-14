"""Tests for sikula.py — status command and CLI flags."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

_sikula = importlib.import_module("sikula")
cmd_status = _sikula.cmd_status
_pid_running = _sikula._pid_running

_SIKULA_PY = str(Path(__file__).parent.parent / "sikula.py")


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
