"""Tests for core/progress.py — active-operation heartbeat helper."""

from __future__ import annotations

import logging

from core.progress import ActiveOperationHeartbeat, _fmt_elapsed
from core.state import TaskState


class RecordingStore:
    def __init__(self) -> None:
        self.saved = []
        self.active_updates = []

    def save(self, state: TaskState) -> None:
        self.saved.append(dict(state.active_operation or {}))

    def update_active_operation(self, task_id: str, active_operation: dict | None) -> None:
        self.active_updates.append((task_id, active_operation))


class TwoTickStop:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self, interval_s: int) -> bool:
        self.calls += 1
        return self.calls > 1


def test_run_writes_heartbeat_and_logs_label(monkeypatch, caplog):
    store = RecordingStore()
    state = TaskState(task_id="t1", task_description="task")
    heartbeat = ActiveOperationHeartbeat(
        store,
        state,
        phase="agent",
        agent="reviewer",
        message="Running reviewer",
        interval_s=60,
    )
    heartbeat._stop = TwoTickStop()  # type: ignore[assignment]
    heartbeat._started_monotonic = 10
    state.start_active_operation("agent", agent="reviewer", message="Running reviewer")
    monkeypatch.setattr("core.progress.time.monotonic", lambda: 75)

    with caplog.at_level(logging.INFO, logger="core.progress"):
        heartbeat._run()

    assert store.active_updates[0][0] == "t1"
    assert store.active_updates[0][1]["heartbeat_count"] == 1
    assert store.active_updates[0][1]["message"] == "Running reviewer"
    assert "Still running: reviewer (1m 05s)" in caplog.text


def test_run_uses_default_message_and_phase_label(monkeypatch, caplog):
    store = RecordingStore()
    state = TaskState(task_id="t1", task_description="task")
    heartbeat = ActiveOperationHeartbeat(store, state, phase="build", interval_s=60)
    heartbeat._stop = TwoTickStop()  # type: ignore[assignment]
    heartbeat._started_monotonic = 0
    state.start_active_operation("build")
    monkeypatch.setattr("core.progress.time.monotonic", lambda: 5)

    with caplog.at_level(logging.INFO, logger="core.progress"):
        heartbeat._run()

    assert store.active_updates[0][1]["message"] == "build running"
    assert "Still running: build (5s)" in caplog.text


def test_label_prefers_agent_then_scope_then_phase():
    store = RecordingStore()
    state = TaskState(task_id="t1", task_description="task")

    assert ActiveOperationHeartbeat(store, state, phase="agent", agent="reviewer", interval_s=1)._label() == "reviewer"
    assert (
        ActiveOperationHeartbeat(store, state, phase="build", scope="final_full_task", interval_s=1)._label()
        == "final_full_task build"
    )
    assert ActiveOperationHeartbeat(store, state, phase="test", interval_s=1)._label() == "test"


def test_fmt_elapsed_formats_seconds_minutes_and_hours():
    assert _fmt_elapsed(59) == "59s"
    assert _fmt_elapsed(65) == "1m 05s"
    assert _fmt_elapsed(3661) == "1h 01m"
