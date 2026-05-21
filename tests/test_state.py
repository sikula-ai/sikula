"""Tests for core/state.py — TaskState, JsonStateStore."""

from __future__ import annotations

import json
from pathlib import Path


from core.state import JsonStateStore, TaskState


class TestTaskStateRecord:
    def test_appends_to_history(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record("reviewer", "review", "approved")
        assert len(state.history) == 1
        entry = state.history[0]
        assert entry["agent"] == "reviewer"
        assert entry["action"] == "review"
        assert entry["result"] == "approved"
        assert "timestamp" in entry

    def test_elapsed_included_when_provided(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record("analyst", "analyze", "done", elapsed_s=12.349)
        assert state.history[0]["elapsed_s"] == 12.3  # source rounds to 1 decimal place

    def test_elapsed_omitted_when_none(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record("analyst", "analyze", "done")
        assert "elapsed_s" not in state.history[0]

    def test_error_included_when_provided(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record("fixer", "fix_failed", "error", error="timeout")
        assert state.history[0]["error"] == "timeout"

    def test_multiple_records_accumulate(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record("analyst", "analyze", "done")
        state.record("implementer", "implement", "done")
        assert len(state.history) == 2


class TestJsonStateStore:
    def test_create_returns_saved_state(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = store.create("add feature")
        assert state.task_description == "add feature"
        assert len(state.task_id) == 32  # uuid4().hex
        assert (tmp_path / f"{state.task_id}.json").exists()

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="abc123", task_description="test task")
        state.build_iterations = 3
        state.review_approved = True
        store.save(state)

        loaded = store.load("abc123")
        assert loaded is not None
        assert loaded.task_id == "abc123"
        assert loaded.build_iterations == 3
        assert loaded.review_approved is True

    def test_observability_records_saved_in_pipeline_order(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="order1", task_description="record order")
        store.save(state)

        data = json.loads((tmp_path / "order1.json").read_text())
        keys = list(data)
        record_keys = [
            "implement_cycle_records",
            "review_cycle_records",
            "test_write_records",
            "fix_cycle_records",
        ]
        first = keys.index(record_keys[0])
        last = keys.index(record_keys[-1])

        assert keys[first : last + 1] == record_keys

    def test_load_returns_none_for_missing_id(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        assert store.load("nonexistent") is None

    def test_save_updates_updated_at(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="t1", task_description="task")
        original_updated = state.updated_at
        store.save(state)
        assert state.updated_at >= original_updated

    def test_list_tasks_returns_sorted(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        for tid in ["ccc", "aaa", "bbb"]:
            store.save(TaskState(task_id=tid, task_description="x"))
        assert store.list_tasks() == ["aaa", "bbb", "ccc"]

    def test_list_tasks_empty(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        assert store.list_tasks() == []

    def test_list_tasks_returns_empty_when_dir_missing(self, tmp_path: Path):
        store = JsonStateStore(tmp_path / "nonexistent")
        assert store.list_tasks() == []

    def test_load_migrates_gradle_synced(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="m1", task_description="migration test")
        store.save(state)
        path = tmp_path / "m1.json"
        data = json.loads(path.read_text())
        data.pop("build_synced", None)
        data["gradle_synced"] = True
        path.write_text(json.dumps(data))

        loaded = store.load("m1")
        assert loaded is not None
        assert loaded.build_synced is True

    def test_load_drops_unknown_fields(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="u1", task_description="unknown fields test")
        store.save(state)
        path = tmp_path / "u1.json"
        data = json.loads(path.read_text())
        data["future_unknown_field"] = "should be dropped"
        path.write_text(json.dumps(data))

        loaded = store.load("u1")
        assert loaded is not None
        assert not hasattr(loaded, "future_unknown_field")

    def test_state_dir_created_on_first_save(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        store = JsonStateStore(nested)
        assert not nested.exists()
        store.save(store.create("t1"))
        assert nested.exists()

    def test_create_unique_ids(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        ids = {store.create("task").task_id for _ in range(10)}
        assert len(ids) == 10

    def test_load_old_state_without_review_diff_defaults_to_none(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        store.save(TaskState(task_id="old1", task_description="old task"))
        path = tmp_path / "old1.json"
        data = json.loads(path.read_text())
        data.pop("review_diff", None)
        path.write_text(json.dumps(data))
        loaded = store.load("old1")
        assert loaded is not None
        assert loaded.review_diff is None

    def test_save_and_load_review_diff(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="r1", task_description="review task")
        state.review_diff = "diff content\nwith\nmultiple lines"
        store.save(state)
        loaded = store.load("r1")
        assert loaded.review_diff == "diff content\nwith\nmultiple lines"

    def test_review_diff_preserved_across_save_load_cycles(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="r2", task_description="review task")
        state.review_diff = "original diff"
        store.save(state)
        loaded = store.load("r2")
        loaded.review_approved = True
        store.save(loaded)
        reloaded = store.load("r2")
        assert reloaded.review_diff == "original diff"

    def test_finished_at_set_for_terminal_state(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="done1", task_description="done task", done=True)
        store.save(state)

        loaded = store.load("done1")
        assert loaded is not None
        assert loaded.finished_at is not None

    def test_finished_at_not_overwritten_for_terminal_state(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="done2",
            task_description="done task",
            done=True,
            finished_at="2026-01-01T00:00:00Z",
        )
        store.save(state)

        loaded = store.load("done2")
        assert loaded is not None
        loaded.history.append({"agent": "test", "action": "touch", "result": "ok"})
        store.save(loaded)
        reloaded = store.load("done2")
        assert reloaded is not None
        assert reloaded.finished_at == "2026-01-01T00:00:00Z"

    def test_final_audit_fields_round_trip(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="audit1",
            task_description="audit task",
            done=True,
            result_commit="abc123",
            test_status="success",
            check_status="skipped",
        )
        store.save(state)

        loaded = store.load("audit1")
        assert loaded is not None
        assert loaded.result_commit == "abc123"
        assert loaded.test_status == "success"
        assert loaded.check_status == "skipped"
        assert loaded.finished_at is not None
