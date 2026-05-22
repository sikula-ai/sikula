"""Tests for core/state.py — TaskState, JsonStateStore."""

from __future__ import annotations

import json
from pathlib import Path


import core.state as state_module
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
        assert keys.index("validation_cycle_records") > last

    def test_runtime_metadata_set_when_state_is_created(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = store.create("metadata task")

        assert state.runtime_metadata["python_version"]
        assert state.runtime_metadata["sikula_version"]
        assert state.runtime_metadata["system"]

    def test_runtime_metadata_uses_unknown_when_package_version_is_unavailable(self, monkeypatch):
        def missing_version(_: str) -> str:
            raise state_module.PackageNotFoundError

        monkeypatch.setattr(state_module, "version", missing_version)

        metadata = state_module.runtime_metadata_snapshot()

        assert metadata["sikula_version"] == "unknown"

    def test_validation_record_captures_iteration_step_and_error_excerpt(self):
        state = TaskState(task_id="v1", task_description="validation task")
        state.build_iterations = 2
        state.current_step = 1
        state.record_validation("build", "failed", elapsed_s=1.26, error="x" * 1200)

        record = state.validation_cycle_records[0]
        assert record["phase"] == "build"
        assert record["status"] == "failed"
        assert record["build_iteration"] == 2
        assert record["step"] == 1
        assert record["elapsed_s"] == 1.3
        assert len(record["error_excerpt"]) == 1000

    def test_load_old_state_without_new_observability_fields(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldobs1", task_description="old observability task")
        store.save(state)
        path = tmp_path / "oldobs1.json"
        data = json.loads(path.read_text())
        data.pop("validation_cycle_records", None)
        data.pop("runtime_metadata", None)
        data.pop("final_summary", None)
        path.write_text(json.dumps(data))

        loaded = store.load("oldobs1")

        assert loaded is not None
        assert loaded.validation_cycle_records == []
        assert loaded.runtime_metadata == {}
        assert loaded.final_summary == {}

    def test_final_summary_handles_failed_state_with_invalid_timestamps(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="summary_failed",
            task_description="failed summary task",
            failed=True,
            created_at=None,  # type: ignore[arg-type]
            finished_at="not-a-timestamp",
        )

        store.save(state)
        loaded = store.load("summary_failed")

        assert loaded is not None
        assert loaded.final_summary["result"] == "failed"
        assert "wall_elapsed_s" not in loaded.final_summary

    def test_terminal_result_reports_incomplete_for_non_terminal_state(self):
        state = TaskState(task_id="summary_incomplete", task_description="incomplete")

        assert state_module._terminal_result(state) == "incomplete"

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

    def test_final_summary_written_for_terminal_state(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="summary1",
            task_description="summary task",
            done=True,
            files_changed=["src/app.py", "tests/test_app.py"],
            test_files_written=["tests/test_app.py"],
            result_commit="abc123",
            build_iterations=2,
            build_status="success",
            test_status="success",
            check_status="skipped",
        )
        state.record("test_writer", "llm_retry", "temporary failure")
        state.record_validation("build", "success", elapsed_s=2.0)
        store.save(state)

        loaded = store.load("summary1")

        assert loaded is not None
        assert loaded.final_summary["result"] == "done"
        assert loaded.final_summary["commit"] == "abc123"
        assert loaded.final_summary["build_attempts"] == 2
        assert loaded.final_summary["files_changed_count"] == 2
        assert loaded.final_summary["test_files_written_count"] == 1
        assert loaded.final_summary["validation_records_count"] == 1
        assert loaded.final_summary["validation_failures_count"] == 0
        assert loaded.final_summary["reviewer_runs"] == 0
        assert loaded.final_summary["security_reviewer_runs"] == 0
        assert loaded.final_summary["llm_retries"] == 1
