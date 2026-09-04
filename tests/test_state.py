"""Tests for core/state.py — TaskState, JsonStateStore."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import core.state as state_module
from core.state import JsonStateStore, TaskState
from core.structured_output import (
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
    DELIVERY_DISPOSITION_FIX_IN_SCOPE,
    DELIVERY_IMPLEMENTATION_DISPOSITIONS,
    DELIVERY_REVIEW_DISPOSITIONS,
    parse_delivery_disposition,
)


_REVIEW_DELIVERY_FIELD_VALUES = (
    ("review_delivery_mode", "current_branch"),
    ("review_target_branch", "feature/current-branch"),
    ("review_target_start_commit", "1111111111111111111111111111111111111111"),
    ("review_isolated_fix_commit", "2222222222222222222222222222222222222222"),
    ("review_delivery_status", "delivered"),
    ("review_delivery_result", "Delivered by fast-forward"),
)
_REVIEW_DELIVERY_FIELDS = tuple(field_name for field_name, _ in _REVIEW_DELIVERY_FIELD_VALUES)


class _NonReentrantLock:
    def __init__(self) -> None:
        self.locked = False

    def __enter__(self):
        if self.locked:
            raise AssertionError("lock re-entered")
        self.locked = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.locked = False


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

    def test_llm_usage_record_is_content_free_and_bounded(self):
        state = TaskState(task_id="t1", task_description="task")

        state.record_llm_usage(
            "planner",
            {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "operation": "generate",
                "attempt": 1,
                "max_attempts": 4,
                "outcome": "success",
                "elapsed_s": 0.5,
                "input_chars": 100,
                "output_chars": 20,
                "prompt": "PRIVATE_PROMPT",
                "output": "PRIVATE_PROVIDER_OUTPUT",
            },
        )

        assert len(state.llm_usage_records) == 1
        record = state.llm_usage_records[0]
        assert record["agent"] == "planner"
        assert record["input_chars"] == 100
        assert "recorded_at" in record
        assert "prompt" not in record
        assert "output" not in record

    def test_run_invocation_record_preserves_an_independent_config_snapshot(self):
        state = TaskState(task_id="t1", task_description="task")
        config = {"run_build": True, "agents": {"implementer": {"model": "model-a"}}}

        state.record_run_invocation(config)
        config["run_build"] = False
        config["agents"]["implementer"]["model"] = "model-b"

        assert state.run_invocation_records == [
            {
                "started_at": state.run_invocation_records[0]["started_at"],
                "config_snapshot": {
                    "run_build": True,
                    "agents": {"implementer": {"model": "model-a"}},
                },
            }
        ]
        assert state.run_invocation_schema_version is None

    def test_first_run_invocation_record_can_mark_history_complete(self):
        state = TaskState(task_id="t1", task_description="task")

        state.record_run_invocation(
            {"run_build": True},
            complete_history_from_creation=True,
        )

        assert state.run_invocation_schema_version == state_module.RUN_INVOCATION_SCHEMA_VERSION
        assert len(state.run_invocation_records) == 1

    def test_partial_history_cannot_later_be_marked_complete(self):
        state = TaskState(task_id="t1", task_description="task")
        state.record_run_invocation({"run_build": True})

        with pytest.raises(ValueError, match="can only be marked on the first record"):
            state.record_run_invocation(
                {"run_build": False},
                complete_history_from_creation=True,
            )

    def test_delivery_budget_stop_records_structured_audit(self):
        state = TaskState(task_id="t1", task_description="task")

        state.record_delivery_budget_stop(name="max_planner_steps", limit=1, actual=3)

        assert state.delivery_budget_stop is not None
        assert state.delivery_budget_stop["code"] == "unit_budget_exceeded"
        assert state.delivery_budget_stop["name"] == "max_planner_steps"
        assert state.delivery_budget_stop["limit"] == 1
        assert state.delivery_budget_stop["actual"] == 3
        assert state.delivery_budget_stop["phase"] == "planner"
        assert "timestamp" in state.delivery_budget_stop
        assert state.history[-1]["action"] == "delivery_budget_exceeded"


class TestTaskStateReviewDeliveryMetadata:
    @pytest.mark.parametrize("field_name", _REVIEW_DELIVERY_FIELDS)
    def test_review_delivery_fields_default_to_none(self, field_name: str):
        state = TaskState(task_id="t1", task_description="task")

        assert getattr(state, field_name) is None


class TestJsonStateStore:
    def test_create_returns_saved_state(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = store.create("add feature")
        assert state.task_description == "add feature"
        assert len(state.task_id) == 32  # uuid4().hex
        assert state.run_invocation_schema_version is None
        assert state.run_invocation_records == []
        assert (tmp_path / f"{state.task_id}.json").exists()

    def test_create_persists_delivery_unit_budget_snapshot(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)

        state = store.create(
            "delivery child",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 2, "max_changed_files": 4},
        )
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_unit_budget == {"max_planner_steps": 2, "max_changed_files": 4}

    def test_create_persists_copied_delivery_constraint_context(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        source_task = {"path": ".sikula/tasks/source.md", "sha256": f"sha256:{'a' * 64}"}
        constraints = [
            {
                "id": "repository-ownership",
                "kind": "repository_ownership",
                "summary": "Only this repository may be changed.",
                "unit_ids": ["unit-1"],
                "disposition": "preserved",
            }
        ]

        state = store.create(
            "delivery child",
            delivery_constraint_context_schema_version=1,
            delivery_source_task=source_task,
            delivery_inherited_constraints=constraints,
            delivery_constraint_context_fingerprint="b" * 64,
        )
        source_task["path"] = "mutated.md"
        constraints[0]["unit_ids"].append("mutated")
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_constraint_context_schema_version == 1
        assert loaded.delivery_constraint_context_fingerprint == "b" * 64
        assert loaded.delivery_source_task == {
            "path": ".sikula/tasks/source.md",
            "sha256": f"sha256:{'a' * 64}",
        }
        assert loaded.delivery_inherited_constraints == [
            {
                "id": "repository-ownership",
                "kind": "repository_ownership",
                "summary": "Only this repository may be changed.",
                "unit_ids": ["unit-1"],
                "disposition": "preserved",
            }
        ]

    def test_create_persists_copied_delivery_write_scope(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        declared_paths = ["core/state.py"]
        declared_exact_file_paths = ["core/state.py"]
        effective_paths = ["core/state.py"]
        effective_exact_file_paths = ["core/state.py"]

        state = store.create(
            "delivery child",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=declared_paths,
            delivery_declared_write_exact_file_paths=declared_exact_file_paths,
            delivery_effective_write_paths=effective_paths,
            delivery_effective_write_exact_file_paths=effective_exact_file_paths,
        )
        declared_paths.append("mutated.py")
        declared_exact_file_paths.append("mutated.py")
        effective_paths.append("mutated.py")
        effective_exact_file_paths.append("mutated.py")
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_write_scope_schema_version == 2
        assert loaded.delivery_write_scope_mode == "unit_explicit"
        assert loaded.delivery_declared_write_paths == ["core/state.py"]
        assert loaded.delivery_declared_write_exact_file_paths == ["core/state.py"]
        assert loaded.delivery_effective_write_paths == ["core/state.py"]
        assert loaded.delivery_effective_write_exact_file_paths == ["core/state.py"]

    def test_create_persists_delivery_handoff_inputs(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        dependency_handoff = {
            "schema_version": 1,
            "fingerprint": "a" * 64,
            "unit": {"depends_on": ["foundation"]},
        }

        state = store.create(
            "delivery child",
            delivery_handoff_schema_version=1,
            delivery_dependency_handoffs=[dependency_handoff],
        )
        dependency_handoff["schema_version"] = 2
        dependency_handoff["unit"]["depends_on"].append("mutated")
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_handoff_schema_version == 1
        assert loaded.delivery_dependency_handoffs == [
            {
                "schema_version": 1,
                "fingerprint": "a" * 64,
                "unit": {"depends_on": ["foundation"]},
            }
        ]

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="abc123", task_description="test task")
        state.build_iterations = 3
        state.review_approved = True
        state.planner_output = "1. Inspect the delivery unit\n2. Implement it"
        state.generated_test_fix_counts = {"tests/LoginTest.py": 2}
        state.implementation_contract = {"status": "warn", "readiness_score": 72}
        state.test_writer_audit_pending = True
        state.test_writer_audit_agent_completed = True
        state.test_writer_audit_files_written = ["tests/LoginTest.py"]
        state.test_writer_audit_gate_counts = {"tests/LoginTest.py": {"skip:abc123": 1}}
        state.delivery_runtime_write_scope_binding = {
            "schema_version": 1,
            "status": "bound",
            "roots": [{"path": "src", "resolved_path": "src", "exact_file": False}],
        }
        state.delivery_scope_audit_pending = {"schema_version": 1, "agent": "implementer"}
        store.save(state)

        loaded = store.load("abc123")
        assert loaded is not None
        assert loaded.task_id == "abc123"
        assert loaded.build_iterations == 3
        assert loaded.review_approved is True
        assert loaded.planner_output == "1. Inspect the delivery unit\n2. Implement it"
        assert loaded.implementation_contract == {"status": "warn", "readiness_score": 72}
        assert loaded.generated_test_fix_counts == {"tests/LoginTest.py": 2}
        assert loaded.test_writer_audit_pending is True
        assert loaded.test_writer_audit_agent_completed is True
        assert loaded.test_writer_audit_files_written == ["tests/LoginTest.py"]
        assert loaded.test_writer_audit_gate_counts == {"tests/LoginTest.py": {"skip:abc123": 1}}
        assert loaded.delivery_runtime_write_scope_binding == state.delivery_runtime_write_scope_binding
        assert loaded.delivery_scope_audit_pending == {"schema_version": 1, "agent": "implementer"}

    def test_legacy_state_without_step_file_tracking_loads_with_safe_defaults(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="legacy-step-files",
            task_description="legacy planned task",
            plan=["Step 1", "Step 2"],
            plan_decided=True,
            files_changed=["src/step1.py"],
        )
        store.save(state)
        path = tmp_path / "legacy-step-files.json"
        data = json.loads(path.read_text())
        data.pop("step_file_tracking_enabled")
        data.pop("step_files_changed")
        data.pop("llm_usage_records")
        data.pop("run_invocation_schema_version")
        data.pop("run_invocation_records")
        path.write_text(json.dumps(data))

        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.step_file_tracking_enabled is False
        assert loaded.step_files_changed == []
        assert loaded.llm_usage_records == []
        assert loaded.run_invocation_schema_version is None
        assert loaded.run_invocation_records == []

    def test_step_file_tracking_round_trips_for_resume(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="tracked-step-files",
            task_description="planned task",
            plan=["Step 1", "Step 2"],
            plan_decided=True,
            current_step=1,
            step_file_tracking_enabled=True,
            step_files_changed=["src/current.py"],
            files_changed=["src/previous.py", "src/current.py"],
        )

        store.save(state)
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.step_file_tracking_enabled is True
        assert loaded.step_files_changed == ["src/current.py"]

    @pytest.mark.parametrize(("field_name", "value"), _REVIEW_DELIVERY_FIELD_VALUES)
    def test_review_delivery_fields_round_trip(self, tmp_path: Path, field_name: str, value: str):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="delivery1", task_description="review delivery task")
        setattr(state, field_name, value)

        store.save(state)
        loaded = store.load("delivery1")

        assert loaded is not None
        assert getattr(loaded, field_name) == value

    def test_text_snapshots_are_stored_outside_task_json(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="abc123", task_description="test task")
        store.save(state)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/LoginTest.py": "assert True\n"})

        loaded = store.load_text_snapshot("abc123", "test_writer_audit_before")
        data = json.loads((tmp_path / "abc123.json").read_text())

        assert loaded == {"tests/LoginTest.py": "assert True\n"}
        assert "assert True" not in json.dumps(data)

    def test_delete_removes_text_snapshots(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="abc123", task_description="test task")
        store.save(state)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/LoginTest.py": "assert True\n"})

        store.delete("abc123")

        assert store.load("abc123") is None
        assert store.load_text_snapshot("abc123", "test_writer_audit_before") is None

    def test_delete_removes_text_snapshots_without_reentering_lock(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="abc123", task_description="test task")
        store.save(state)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/LoginTest.py": "assert True\n"})
        store._lock = _NonReentrantLock()  # type: ignore[assignment]

        store.delete("abc123")

        assert store.load("abc123") is None
        assert store.load_text_snapshot("abc123", "test_writer_audit_before") is None

    def test_observability_records_saved_in_pipeline_order(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="order1", task_description="record order")
        store.save(state)

        data = json.loads((tmp_path / "order1.json").read_text())
        keys = list(data)
        record_keys = [
            "analyst_cycle_records",
            "planner_retry_records",
            "implement_cycle_records",
            "review_cycle_records",
            "security_review_cycle_records",
            "test_write_records",
            "testability_gaps",
            "test_execution_gate_records",
            "synthetic_test_harness_records",
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

    def test_runtime_metadata_uses_shared_sikula_version(self, monkeypatch):
        monkeypatch.setattr(state_module, "sikula_version", lambda: "1.2.3-dev+branch.abc123")

        metadata = state_module.runtime_metadata_snapshot()

        assert metadata["sikula_version"] == "1.2.3-dev+branch.abc123"

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
        assert "diagnostic_summary" not in record

    def test_validation_record_preserves_gradle_failure_block_in_private_excerpt(self):
        state = TaskState(task_id="v1", task_description="validation task")
        error = (
            "configuration noise\n" * 200
            + "FAILURE: Build failed with an exception.\n"
            + "* What went wrong:\n"
            + "Execution failed for task ':library:presync'.\n"
            + "> Could not create directory 'C:\\workspace\\generated'.\n"
            + "> java.nio.file.FileSystemException: filename too long\n"
            + "* Try:\n"
            + "Run with --stacktrace option to get the stack trace.\n"
            + "trailing noise\n" * 200
        )

        state.record_validation("build", "failed", error=error)

        excerpt = state.validation_cycle_records[0]["error_excerpt"]
        assert "configuration noise" in excerpt
        assert "* What went wrong:" in excerpt
        assert "> Could not create directory 'C:\\workspace\\generated'." in excerpt
        assert "> java.nio.file.FileSystemException: filename too long" in excerpt

    def test_validation_record_keeps_diagnostics_before_short_gradle_footer(self):
        state = TaskState(task_id="v1", task_description="validation task")
        error = (
            "src/main/kotlin/App.kt:42: error: unresolved reference\n"
            "assertion failed before Gradle footer\n"
            "* What went wrong:\n"
            "Execution failed for task ':app:compile'.\n"
            "> Compilation failed; see compiler error output.\n"
            "* Try:\n"
            "Run with --stacktrace.\n"
        )

        state.record_validation("build", "failed", error=error)

        assert state.validation_cycle_records[0]["error_excerpt"] == error

    def test_validation_record_preserves_multiple_gradle_failure_blocks(self):
        state = TaskState(task_id="v1", task_description="validation task")
        error = (
            "FAILURE: Build completed with 2 failures.\n"
            "* What went wrong:\n"
            "Execution failed for task ':app:compile'.\n"
            "> Compilation failed for module app.\n"
            "* Try:\n"
            "Run with --stacktrace.\n"
            "\n"
            "* What went wrong:\n"
            "Execution failed for task ':library:test'.\n"
            "> There were failing tests.\n"
            "* Try:\n"
            "Run with --scan.\n"
        )

        state.record_validation("build", "failed", error=error)

        excerpt = state.validation_cycle_records[0]["error_excerpt"]
        assert "Execution failed for task ':app:compile'." in excerpt
        assert "> Compilation failed for module app." in excerpt
        assert "Execution failed for task ':library:test'." in excerpt
        assert "> There were failing tests." in excerpt

    def test_validation_record_captures_metadata(self):
        state = TaskState(task_id="v1", task_description="validation task")

        state.record_validation(
            "sync",
            "success",
            metadata={
                "sync_retry": {
                    "reason": "cargo_lockfile_needs_update",
                    "initial_command": "cargo fetch --locked",
                    "retry_command": "cargo fetch",
                }
            },
        )

        record = state.validation_cycle_records[0]
        assert record["metadata"]["sync_retry"]["reason"] == "cargo_lockfile_needs_update"
        assert record["metadata"]["sync_retry"]["initial_command"] == "cargo fetch --locked"
        assert record["metadata"]["sync_retry"]["retry_command"] == "cargo fetch"

    def test_validation_record_captures_diagnostic_summary(self):
        state = TaskState(task_id="v1", task_description="validation task")
        error = (
            "> Task :app:test FAILED\n"
            "CountryDetailScreenContractTest > detail content uses capital fallback() FAILED\n"
            "    java.lang.AssertionError at CountryDetailScreenContractTest.kt:53\n"
            "BUILD FAILED in 3s\n"
        )

        state.record_validation("test", "failed", error=error)

        record = state.validation_cycle_records[0]
        assert record["diagnostic_summary"] == [
            "CountryDetailScreenContractTest > detail content uses capital fallback() FAILED",
            "java.lang.AssertionError at CountryDetailScreenContractTest.kt:53",
        ]

    def test_load_old_state_without_new_observability_fields(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldobs1", task_description="old observability task")
        store.save(state)
        path = tmp_path / "oldobs1.json"
        data = json.loads(path.read_text())
        data.pop("validation_cycle_records", None)
        data.pop("runtime_metadata", None)
        data.pop("final_summary", None)
        data.pop("implementation_contract", None)
        data.pop("implementation_asset_records", None)
        data.pop("implementation_asset_target_records", None)
        data.pop("testability_gaps", None)
        data.pop("test_execution_gate_records", None)
        data.pop("build_loop_key", None)
        data.pop("build_loop_start_iteration", None)
        path.write_text(json.dumps(data))

        loaded = store.load("oldobs1")

        assert loaded is not None
        assert loaded.validation_cycle_records == []
        assert loaded.runtime_metadata == {}
        assert loaded.final_summary == {}
        assert loaded.implementation_contract == {}
        assert loaded.implementation_asset_records == []
        assert loaded.implementation_asset_target_records == []
        assert loaded.testability_gaps == []
        assert loaded.test_execution_gate_records == []
        assert loaded.build_loop_key is None
        assert loaded.build_loop_start_iteration == 0

    @pytest.mark.parametrize("field_name", _REVIEW_DELIVERY_FIELDS)
    def test_load_old_state_without_review_delivery_field_defaults_to_none(self, tmp_path: Path, field_name: str):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="olddelivery1", task_description="old review delivery task")
        store.save(state)
        path = tmp_path / "olddelivery1.json"
        data = json.loads(path.read_text())
        data.pop(field_name, None)
        path.write_text(json.dumps(data))

        loaded = store.load("olddelivery1")

        assert loaded is not None
        assert getattr(loaded, field_name) is None

    def test_record_implementation_assets_sanitizes_records(self):
        state = TaskState(task_id="assets1", task_description="asset task")

        state.record_implementation_assets(
            [
                {
                    "path": ".sikula/task-assets/icon.svg",
                    "project_path": ".sikula/task-assets/icon.svg",
                    "kind": "delivery",
                    "status": "available",
                    "line": 12,
                    "size_bytes": 42,
                    "target_specified": True,
                    "requested_target": "app/assets/icon.svg",
                    "provenance_specified": True,
                    "source_license": "provided by product team; MIT.",
                    "sha256": "sha256:abc",
                    "declared_sha256": "sha256:old",
                    "mime_type": "image/svg+xml",
                    "git_status": "tracked",
                    "_raw_paths": [".sikula/task-assets/icon.svg"],
                    "excerpt": "raw source excerpt",
                }
            ]
        )

        assert state.implementation_asset_records == [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "status": "available",
                "line": 12,
                "size_bytes": 42,
                "target_specified": True,
                "requested_target": "app/assets/icon.svg",
                "provenance_specified": True,
                "source_license": "provided by product team; MIT.",
                "sha256": "sha256:abc",
                "declared_sha256": "sha256:old",
                "mime_type": "image/svg+xml",
                "git_status": "tracked",
            }
        ]
        assert state.history[-1]["action"] == "asset_snapshot"

    def test_record_implementation_asset_drift_sanitizes_and_deduplicates_records(self):
        state = TaskState(task_id="assetdrift1", task_description="asset drift task")

        drift_record = {
            "path": ".sikula/task-assets/icon.svg",
            "project_path": ".sikula/task-assets/icon.svg",
            "kind": "delivery",
            "phase": "resume",
            "status": "changed",
            "expected_source": "task_state_snapshot",
            "expected_sha256": "sha256:old",
            "current_sha256": "sha256:new",
            "current_status": "available",
            "git_status": "dirty",
            "mime_type": "image/svg+xml",
            "size_bytes": 42,
            "observed_at": "2026-06-25T10:00:00+00:00",
            "excerpt": "raw source excerpt",
        }
        state.record_implementation_asset_drift([drift_record, dict(drift_record)])

        assert state.implementation_asset_drift_records == [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "phase": "resume",
                "status": "changed",
                "expected_source": "task_state_snapshot",
                "expected_sha256": "sha256:old",
                "current_sha256": "sha256:new",
                "current_status": "available",
                "git_status": "dirty",
                "mime_type": "image/svg+xml",
                "observed_at": "2026-06-25T10:00:00+00:00",
                "size_bytes": 42,
            }
        ]
        assert state.history[-1]["action"] == "asset_drift"

    def test_record_implementation_asset_targets_sanitizes_and_deduplicates_records(self):
        state = TaskState(task_id="assettarget1", task_description="asset target task")

        target_record = {
            "path": ".sikula/task-assets/icon.svg",
            "project_path": ".sikula/task-assets/icon.svg",
            "kind": "delivery",
            "phase": "completion",
            "status": "missing",
            "requested_target": "app/assets/icon.svg",
            "matched_path": "",
            "observed_at": "2026-06-25T10:00:00+00:00",
            "excerpt": "raw source excerpt",
        }
        state.record_implementation_asset_targets([target_record, dict(target_record)])

        assert state.implementation_asset_target_records == [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "phase": "completion",
                "status": "missing",
                "requested_target": "app/assets/icon.svg",
                "observed_at": "2026-06-25T10:00:00+00:00",
            }
        ]
        assert state.history[-1]["action"] == "asset_target_audit"

    def test_record_testability_gap_captures_scope_and_metadata(self):
        state = TaskState(
            task_id="gap1",
            task_description="gap task",
            current_step=2,
            build_iterations=3,
            active_scope="final_full_task",
        )
        state.record_testability_gap(
            "test_writer",
            "TESTABILITY GAP:\ntarget: share sheet",
            target="share sheet",
            reason="no UI harness",
            covered_by="view model state tests",
            recommended_action="add UI tests",
            risk="medium",
        )

        assert len(state.testability_gaps) == 1
        gap = state.testability_gaps[0]
        assert gap["source"] == "test_writer"
        assert gap["step"] == 2
        assert gap["build_iteration"] == 3
        assert gap["scope"] == "final_full_task"
        assert gap["target"] == "share sheet"
        assert gap["reason"] == "no UI harness"
        assert gap["covered_by"] == "view model state tests"
        assert gap["recommended_action"] == "add UI tests"
        assert gap["risk"] == "medium"
        assert state.history[-1]["action"] == "testability_gap"

    def test_record_test_execution_gate_audit_captures_scope_and_metadata(self):
        state = TaskState(
            task_id="gate1",
            task_description="gate task",
            current_step=1,
            build_iterations=2,
            active_scope="final_full_task",
        )

        state.record_test_execution_gate_audit(
            "fixer",
            [
                {
                    "path": "tests/clientMain.test.ts",
                    "line": 31,
                    "category": "environment",
                    "reason": "environment-gated test registration",
                    "excerpt": 'if (typeof document === "undefined") {',
                    "signature": "environment:abc123",
                    "baseline_count": 0,
                    "occurrence": 1,
                }
            ],
        )

        assert len(state.test_execution_gate_records) == 1
        record = state.test_execution_gate_records[0]
        assert record["source"] == "fixer"
        assert record["step"] == 1
        assert record["build_iteration"] == 2
        assert record["scope"] == "final_full_task"
        assert record["status"] == "detected"
        assert record["findings"][0]["path"] == "tests/clientMain.test.ts"
        assert record["findings"][0]["signature"] == "environment:abc123"
        assert "excerpt" not in record["findings"][0]
        assert state.history[-1]["action"] == "test_execution_gate_audit"

    def test_record_synthetic_test_harness_audit_strips_source_excerpts(self):
        state = TaskState(task_id="synthetic1", task_description="synthetic task")

        state.record_synthetic_test_harness_audit(
            "test_writer",
            [
                {
                    "path": "tests/clientMain.test.ts",
                    "subsystems": ["event_dispatch", "navigation_history", "network_server"],
                    "excerpt": "class FakeEventTarget {}",
                    "evidence": [
                        {
                            "category": "event_dispatch",
                            "reason": "event dispatch fake",
                            "lines": [{"line": 10, "excerpt": "class FakeEventTarget {}"}],
                        }
                    ],
                }
            ],
        )

        finding = state.synthetic_test_harness_records[0]["findings"][0]
        assert finding["path"] == "tests/clientMain.test.ts"
        assert "excerpt" not in finding
        assert finding["evidence"][0]["lines"] == [{"line": 10}]
        assert state.history[-1]["action"] == "synthetic_test_harness_audit"

    def test_load_migrates_mixed_review_cycle_records(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldreview1", task_description="old review task")
        store.save(state)
        path = tmp_path / "oldreview1.json"
        data = json.loads(path.read_text())
        data["schema_version"] = 1
        data.pop("security_review_cycle_records", None)
        data["review_cycle_records"] = [
            {
                "reviewer": "reviewer",
                "reviewer_output": "APPROVED",
                "approved": True,
            },
            {
                "reviewer": "security_reviewer",
                "reviewer_output": "## Warnings\n\n### Minor",
                "approved": True,
                "has_warnings": True,
            },
        ]
        path.write_text(json.dumps(data))

        loaded = store.load("oldreview1")

        assert loaded is not None
        assert loaded.schema_version == state_module.SCHEMA_VERSION
        assert len(loaded.review_cycle_records) == 1
        assert loaded.review_cycle_records[0]["reviewer_output"] == "APPROVED"
        assert "reviewer" not in loaded.review_cycle_records[0]
        assert len(loaded.security_review_cycle_records) == 1
        assert loaded.security_review_cycle_records[0]["has_warnings"] is True
        assert "reviewer" not in loaded.security_review_cycle_records[0]

    def test_load_cleans_partially_migrated_security_review_records(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="partialreview1", task_description="partial review task")
        store.save(state)
        path = tmp_path / "partialreview1.json"
        data = json.loads(path.read_text())
        data["schema_version"] = state_module.SCHEMA_VERSION
        data["security_review_cycle_records"] = [
            {
                "reviewer": "security_reviewer",
                "reviewer_output": "APPROVED",
                "approved": True,
            }
        ]
        path.write_text(json.dumps(data))

        loaded = store.load("partialreview1")

        assert loaded is not None
        assert len(loaded.security_review_cycle_records) == 1
        assert "reviewer" not in loaded.security_review_cycle_records[0]

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

    def test_analyst_run_count_uses_dedicated_prompt_and_attempt_metadata(self):
        state = TaskState(
            task_id="analyst_count",
            task_description="count analyst invocations",
            analyst_prompt="initial prompt",
        )

        assert state_module._analyst_runs_count(state) == 1

        state.analyst_retry_records.append({"attempt": 1, "will_retry": True})
        assert state_module._analyst_runs_count(state) == 2

        state.analyst_cycle_records.append({"attempt": 2, "error": "timeout"})
        assert state_module._analyst_runs_count(state) == 2

    @pytest.mark.parametrize(
        ("done", "failed", "delivery_status", "expected_result"),
        [
            (True, False, None, "incomplete"),
            (True, False, "pending", "incomplete"),
            (True, False, "committed", "incomplete"),
            (True, False, "failed", "failed"),
            (True, True, "pending", "failed"),
            (True, False, "delivered", "done"),
            (True, False, "no_changes", "done"),
        ],
    )
    def test_terminal_result_accounts_for_current_branch_delivery_status(
        self,
        done: bool,
        failed: bool,
        delivery_status: str | None,
        expected_result: str,
    ):
        state = TaskState(
            task_id="summary_delivery_status",
            task_description="review delivery task",
            done=done,
            failed=failed,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
        )

        assert state_module._terminal_result(state) == expected_result

    def test_load_returns_none_for_missing_id(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        assert store.load("nonexistent") is None

    @pytest.mark.parametrize(
        "task_id",
        [
            "",
            "../escape",
            "a/b",
            "a b",
            "bad:task",
            ".leadingdot",
            None,
            0,
        ],
    )
    def test_load_returns_none_for_invalid_task_id(self, tmp_path: Path, task_id) -> None:
        store = JsonStateStore(tmp_path)
        assert store.load(task_id) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "task_id",
        ["", "../escape", "a/b", "a b", "bad:task", ".leadingdot"],
    )
    def test_save_rejects_invalid_task_id(self, tmp_path: Path, task_id: str) -> None:
        store = JsonStateStore(tmp_path)
        with pytest.raises(ValueError):
            store.save(TaskState(task_id=task_id, task_description="invalid task"))
        assert store.list_tasks() == []

    def test_state_store_ignores_invalid_task_ids(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        valid_state = TaskState(task_id="valid-1", task_description="valid task")
        store.save(valid_state)
        store.save_text_snapshot("valid-1", "before", {"unit": "abc"})

        store.save_text_snapshot("../escape", "before", {"unit": "abc"})
        store.delete("../escape")
        store.delete_text_snapshot("../escape", "before")
        store.delete_text_snapshots("../escape")
        store.update_active_operation("../escape", {"phase": "agent"})
        assert store.load_text_snapshot("../escape", "before") is None

        assert store.load_text_snapshot("valid-1", "before") == {"unit": "abc"}
        assert store.load("valid-1") is not None

    def test_save_updates_updated_at(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="t1", task_description="task")
        original_updated = state.updated_at
        store.save(state)
        assert state.updated_at >= original_updated

    def test_active_operation_roundtrip_and_update(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="active1", task_description="active task")
        state.start_active_operation("agent", agent="reviewer", message="Running reviewer")
        store.save(state)

        loaded = store.load("active1")

        assert loaded is not None
        assert loaded.active_operation is not None
        assert loaded.active_operation["phase"] == "agent"
        assert loaded.active_operation["agent"] == "reviewer"

        state.heartbeat_active_operation("Still reviewing")
        store.update_active_operation(state.task_id, state.active_operation)
        loaded = store.load("active1")

        assert loaded is not None
        assert loaded.active_operation is not None
        assert loaded.active_operation["heartbeat_count"] == 1
        assert loaded.active_operation["message"] == "Still reviewing"

        store.update_active_operation(state.task_id, None)
        loaded = store.load("active1")

        assert loaded is not None
        assert loaded.active_operation is None

    def test_update_active_operation_ignores_missing_state_file(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)

        store.update_active_operation("missing", {"phase": "agent"})

        assert store.load("missing") is None

    def test_atomic_write_cleans_temp_file_on_replace_failure(self, tmp_path: Path, monkeypatch):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="replacefail", task_description="replace failure")

        def fail_replace(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(state_module.os, "replace", fail_replace)

        try:
            store.save(state)
        except OSError as exc:
            assert str(exc) == "replace failed"
        else:
            raise AssertionError("expected replace failure")

        assert list(tmp_path.glob("*.tmp")) == []
        assert store.load("replacefail") is None

    def test_concurrent_save_and_active_operation_update_preserves_json_and_history(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="concurrent1", task_description="concurrent task")
        state.start_active_operation("agent", agent="reviewer", message="Running reviewer")
        store.save(state)

        def save_retry_records() -> None:
            for i in range(50):
                state.record("reviewer", "llm_retry", f"retry {i}")
                store.save(state)

        def update_heartbeats() -> None:
            for i in range(50):
                state.heartbeat_active_operation(f"heartbeat {i}")
                store.update_active_operation(state.task_id, state.active_operation)

        threads = [threading.Thread(target=save_retry_records), threading.Thread(target=update_heartbeats)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        data = json.loads((tmp_path / "concurrent1.json").read_text())
        loaded = store.load("concurrent1")

        assert loaded is not None
        assert len(loaded.history) == 50
        assert data["history"][-1]["result"] == "retry 49"
        assert data["active_operation"] is not None
        assert data["active_operation"]["message"].startswith("heartbeat")

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

    @pytest.mark.parametrize("delivery_status", [None, "pending", "committed"])
    def test_finished_at_deferred_for_incomplete_current_branch_delivery(
        self, tmp_path: Path, delivery_status: str | None
    ):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="current_branch_incomplete",
            task_description="current branch delivery task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
        )

        store.save(state)
        loaded = store.load("current_branch_incomplete")

        assert loaded is not None
        assert loaded.finished_at is None
        assert loaded.final_summary == {}

    def test_incomplete_current_branch_delivery_clears_stale_audit_metadata(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="current_branch_stale_audit",
            task_description="current branch delivery task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status="pending",
            finished_at="2026-01-01T00:00:00Z",
            final_summary={"result": "done"},
        )

        store.save(state)
        loaded = store.load("current_branch_stale_audit")

        assert loaded is not None
        assert loaded.finished_at is None
        assert loaded.final_summary == {}

    @pytest.mark.parametrize(
        ("delivery_status", "expected_result"),
        [
            ("failed", "failed"),
            ("delivered", "done"),
            ("no_changes", "done"),
        ],
    )
    def test_finished_at_set_for_terminal_current_branch_delivery(
        self,
        tmp_path: Path,
        delivery_status: str,
        expected_result: str,
    ):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id=f"current_branch_{delivery_status}",
            task_description="current branch delivery task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
        )

        store.save(state)
        loaded = store.load(f"current_branch_{delivery_status}")

        assert loaded is not None
        assert loaded.finished_at is not None
        assert loaded.final_summary["result"] == expected_result

    def test_finished_at_set_when_current_branch_delivery_later_completes(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="current_branch_later_delivered",
            task_description="current branch delivery task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status="pending",
        )
        store.save(state)
        loaded = store.load("current_branch_later_delivered")

        assert loaded is not None
        assert loaded.finished_at is None
        assert loaded.final_summary == {}

        loaded.review_delivery_status = "delivered"
        store.save(loaded)
        reloaded = store.load("current_branch_later_delivered")

        assert reloaded is not None
        assert reloaded.finished_at is not None
        assert reloaded.final_summary["result"] == "done"

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
        state.record_implementation_assets(
            [
                {"path": ".sikula/task-assets/reference.png", "kind": "reference", "status": "available"},
                {
                    "path": ".sikula/task-assets/icon.svg",
                    "kind": "delivery",
                    "status": "available",
                    "git_status": "dirty",
                    "source_license": "provided by product team; MIT.",
                },
            ]
        )
        state.record_implementation_asset_drift(
            [
                {
                    "path": ".sikula/task-assets/icon.svg",
                    "project_path": ".sikula/task-assets/icon.svg",
                    "kind": "delivery",
                    "phase": "resume",
                    "status": "changed",
                    "expected_source": "task_state_snapshot",
                    "expected_sha256": "sha256:old",
                    "current_sha256": "sha256:new",
                    "current_status": "available",
                }
            ]
        )
        state.record_implementation_asset_targets(
            [
                {
                    "path": ".sikula/task-assets/icon.svg",
                    "project_path": ".sikula/task-assets/icon.svg",
                    "kind": "delivery",
                    "phase": "completion",
                    "status": "missing",
                    "requested_target": "app/assets/icon.svg",
                }
            ]
        )
        state.record("test_writer", "llm_retry", "temporary failure")
        state.record_llm_usage(
            "test_writer",
            {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "operation": "run_agent",
                "attempt": 1,
                "max_attempts": 4,
                "outcome": "success",
                "elapsed_s": 1.5,
                "input_chars": 100,
                "output_chars": 20,
            },
        )
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
        assert loaded.final_summary["testability_gaps_count"] == 0
        assert loaded.final_summary["test_execution_gate_audits_count"] == 0
        assert loaded.final_summary["implementation_asset_records_count"] == 2
        assert loaded.final_summary["implementation_asset_records_by_kind"] == {"reference": 1, "delivery": 1}
        assert loaded.final_summary["implementation_asset_warnings_count"] == 1
        assert loaded.final_summary["implementation_asset_drift_records_count"] == 1
        assert loaded.final_summary["implementation_asset_target_records_count"] == 1
        assert loaded.final_summary["implementation_asset_target_warnings_count"] == 1
        assert loaded.final_summary["planner_retries_count"] == 0
        assert loaded.final_summary["llm_retries"] == 1
        assert loaded.final_summary["llm_usage"]["attempts"] == 1
        assert loaded.final_summary["llm_usage"]["input_chars"] == 100
        assert loaded.final_summary["llm_usage_by_agent"]["test_writer"]["attempts"] == 1

    @pytest.mark.parametrize("field_name", _REVIEW_DELIVERY_FIELDS)
    def test_final_summary_includes_empty_review_delivery_metadata(self, tmp_path: Path, field_name: str):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_empty_delivery", task_description="summary task", done=True)

        store.save(state)
        loaded = store.load("summary_empty_delivery")

        assert loaded is not None
        assert loaded.final_summary[field_name] is None

    def test_final_summary_includes_review_delivery_metadata(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_delivery", task_description="summary task", done=True)
        for field_name, value in _REVIEW_DELIVERY_FIELD_VALUES:
            setattr(state, field_name, value)

        store.save(state)
        loaded = store.load("summary_delivery")

        assert loaded is not None
        for field_name, value in _REVIEW_DELIVERY_FIELD_VALUES:
            assert loaded.final_summary[field_name] == value

    @pytest.mark.parametrize(
        ("delivery_status", "expected_result"),
        [
            (None, "incomplete"),
            ("pending", "incomplete"),
            ("committed", "incomplete"),
            ("failed", "failed"),
            ("delivered", "done"),
            ("no_changes", "done"),
        ],
    )
    def test_final_summary_result_accounts_for_current_branch_delivery_status(
        self,
        tmp_path: Path,
        delivery_status: str | None,
        expected_result: str,
    ):
        store = JsonStateStore(tmp_path)
        state = TaskState(
            task_id="summary_delivery_result",
            task_description="summary task",
            done=True,
            review_mode="review_fix",
            review_delivery_mode="current_branch",
            review_delivery_status=delivery_status,
        )

        store.save(state)
        loaded = store.load("summary_delivery_result")

        assert loaded is not None
        if expected_result == "incomplete":
            assert loaded.final_summary == {}
            assert loaded.finished_at is None
        else:
            assert loaded.final_summary["result"] == expected_result
            assert loaded.finished_at is not None

    def test_final_summary_counts_planner_retries(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary-planner", task_description="summary")
        state.failed = True
        state.finished_at = state.created_at
        state.record_planner_retry(
            1,
            "too many steps",
            "1. A\n2. B\n3. C",
            max_steps=2,
            parsed_step_count=3,
            will_retry=False,
        )

        store.save(state)
        loaded = store.load("summary-planner")

        assert loaded.final_summary["planner_retries_count"] == 1

    def test_final_summary_counts_testability_gaps(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_gaps", task_description="summary gaps task", done=True)
        state.record_testability_gap("test_writer", "TESTABILITY GAP:\ntarget: native share")

        store.save(state)
        loaded = store.load("summary_gaps")

        assert loaded is not None
        assert loaded.final_summary["testability_gaps_count"] == 1

    def test_final_summary_counts_test_execution_gate_audits(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_gates", task_description="summary gates task", done=True)
        state.record_test_execution_gate_audit(
            "test_writer",
            [{"path": "tests/test_main.py", "line": 5, "category": "skip", "excerpt": "test.skip("}],
        )

        store.save(state)
        loaded = store.load("summary_gates")

        assert loaded is not None
        assert loaded.final_summary["test_execution_gate_audits_count"] == 1

    def test_final_summary_counts_synthetic_test_harness_audits(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_harness", task_description="summary harness task", done=True)
        state.record_synthetic_test_harness_audit(
            "test_writer",
            [
                {
                    "path": "tests/clientMain.test.ts",
                    "subsystems": ["event_dispatch", "navigation_history", "network_server"],
                    "evidence": [],
                }
            ],
        )

        store.save(state)
        loaded = store.load("summary_harness")

        assert loaded is not None
        assert loaded.final_summary["synthetic_test_harness_audits_count"] == 1

    def test_final_summary_counts_review_records_separately(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="summary_reviews", task_description="summary reviews task", done=True)
        state.review_cycle_records.append({"reviewer_output": "APPROVED", "approved": True})
        state.security_review_cycle_records.append({"reviewer_output": "APPROVED", "approved": True})

        store.save(state)
        loaded = store.load("summary_reviews")

        assert loaded is not None
        assert loaded.final_summary["review_records_count"] == 1
        assert loaded.final_summary["security_review_records_count"] == 1
        assert loaded.final_summary["reviewer_runs"] == 1
        assert loaded.final_summary["security_reviewer_runs"] == 1


class TestTaskStateChildDeliveryMetadata:
    def test_new_fields_default_to_none(self):
        state = TaskState(task_id="t1", task_description="task")
        assert state.delivery_plan_id is None
        assert state.delivery_unit_id is None
        assert state.delivery_plan_path is None
        assert state.delivery_constraint_context_schema_version is None
        assert state.delivery_source_task is None
        assert state.delivery_inherited_constraints == []
        assert state.delivery_constraint_context_fingerprint is None
        assert state.delivery_stop_code is None
        assert state.delivery_stop_disposition is None
        assert state.delivery_disposition_parse_error is None

    def test_implementer_disposition_parse_error_roundtrips_as_terminal_evidence(self, tmp_path: Path):
        state = TaskState(task_id="delivery-invalid-output", task_description="delivery child")
        state.record_delivery_disposition_parse_error(
            "implementer",
            "delivery_disposition.keys_invalid",
        )
        store = JsonStateStore(tmp_path)

        store.save(state)
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_disposition_parse_error is not None
        assert loaded.delivery_disposition_parse_error["schema_version"] == 1
        assert loaded.delivery_disposition_parse_error["source"] == "implementer"
        assert loaded.delivery_disposition_parse_error["error_code"] == "delivery_disposition.keys_invalid"
        assert "timestamp" in loaded.delivery_disposition_parse_error
        assert loaded.delivery_stop_code_from_parse_error() == "implementer_disposition_invalid"
        assert loaded.history[-1]["action"] == "delivery_disposition_parse_error"

    @pytest.mark.parametrize(
        ("source", "error_code", "expected_message"),
        [
            ("reviewer", "delivery_disposition.keys_invalid", "source is invalid"),
            ("implementer", "invalid", "code is invalid"),
        ],
    )
    def test_implementer_disposition_parse_error_rejects_invalid_input(
        self,
        source: str,
        error_code: str,
        expected_message: str,
    ) -> None:
        state = TaskState(task_id="delivery-invalid-parse-input", task_description="delivery child")

        with pytest.raises(ValueError, match=expected_message):
            state.record_delivery_disposition_parse_error(source, error_code)

        assert state.delivery_disposition_parse_error is None

    @pytest.mark.parametrize(
        ("field", "value", "expected_message"),
        [
            ("schema_version", 2, "schema is invalid"),
            ("error_code", "invalid", "code is invalid"),
            ("source", "reviewer", "source is invalid"),
            ("timestamp", "not-a-timestamp", "timestamp is invalid"),
        ],
    )
    def test_persisted_implementer_disposition_parse_error_rejects_invalid_fields(
        self,
        field: str,
        value: object,
        expected_message: str,
    ) -> None:
        parse_error = {
            "schema_version": 1,
            "error_code": "delivery_disposition.keys_invalid",
            "source": "implementer",
            "timestamp": "2026-07-20T10:03:00Z",
        }
        parse_error[field] = value
        state = TaskState(
            task_id="delivery-invalid-parse-field",
            task_description="delivery child",
            delivery_disposition_parse_error=parse_error,
        )

        with pytest.raises(ValueError, match=expected_message):
            state.delivery_stop_code_from_parse_error()

    def test_terminal_delivery_disposition_roundtrips(self, tmp_path: Path):
        parsed = parse_delivery_disposition(
            json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
                    "summary": "The required API belongs to another repository.",
                }
            ),
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )
        assert parsed is not None
        state = TaskState(task_id="delivery-stop", task_description="delivery child")
        state.set_delivery_stop_disposition("implementer", parsed)
        store = JsonStateStore(tmp_path)

        store.save(state)
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_stop_disposition is not None
        assert loaded.delivery_stop_disposition["schema_version"] == 1
        assert loaded.delivery_stop_disposition["source"] == "implementer"
        assert loaded.delivery_stop_disposition["disposition"] == "external_dependency_gap"
        assert loaded.delivery_stop_disposition["recommended_action"] == "external_dependency_follow_up"
        assert "timestamp" in loaded.delivery_stop_disposition
        assert loaded.delivery_stop_code_from_disposition() == "external_dependency_gap"
        assert loaded.history[-1]["action"] == "delivery_stop_disposition"

    def test_delivery_no_change_outcome_roundtrips(self, tmp_path: Path):
        state = TaskState(
            task_id="delivery-noop",
            task_description="delivery child",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_no_change_outcome="already_satisfied",
        )
        store = JsonStateStore(tmp_path)

        store.save(state)
        loaded = store.load(state.task_id)

        assert loaded is not None
        assert loaded.delivery_no_change_outcome == "already_satisfied"

    def test_scope_amendment_disposition_requires_review_source(self):
        parsed = parse_delivery_disposition(
            json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": "requires_scope_amendment",
                    "summary": "The bounded unit needs another production surface.",
                }
            ),
            allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS,
        )
        assert parsed is not None
        state = TaskState(task_id="delivery-amend", task_description="delivery child")

        with pytest.raises(ValueError, match="source is invalid"):
            state.set_delivery_stop_disposition("implementer", parsed)

        state.set_delivery_stop_disposition("reviewer", parsed)
        assert state.delivery_stop_code_from_disposition() == "scope_amendment_required"

    @pytest.mark.parametrize(
        "code",
        [
            state_module.DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
            state_module.DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
            state_module.DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
            state_module.DELIVERY_STOP_IMPLEMENTER_DISPOSITION_INVALID,
        ],
    )
    def test_terminal_stop_code_sets_stable_failed_state(self, code: str):
        state = TaskState(task_id="delivery-terminal", task_description="delivery child", done=True)

        state.set_delivery_terminal_stop(code, source="orchestrator")
        state.set_delivery_terminal_stop(code, source="orchestrator")

        assert state.delivery_stop_code == code
        assert state.failed is True
        assert state.done is False
        assert [entry["action"] for entry in state.history].count("delivery_terminal_stop") == 1

    def test_scope_violation_has_priority_over_reported_dependency_stop(self):
        state = TaskState(task_id="delivery-priority", task_description="delivery child")
        state.set_delivery_terminal_stop(
            state_module.DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
            source="implementer",
        )

        state.set_delivery_terminal_stop(
            state_module.DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
            source="orchestrator",
        )
        state.set_delivery_terminal_stop(
            state_module.DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
            source="reviewer",
        )

        assert state.delivery_stop_code == "unit_scope_violation"
        assert [entry["result"] for entry in state.history if entry["action"] == "delivery_terminal_stop"] == [
            "external_dependency_gap",
            "unit_scope_violation",
        ]

    @pytest.mark.parametrize(
        ("code", "source"),
        [
            ("unknown_stop", "orchestrator"),
            (state_module.DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP, "provider-output"),
        ],
    )
    def test_terminal_stop_rejects_unknown_code_or_source(self, code: str, source: str):
        state = TaskState(task_id="delivery-invalid-stop", task_description="delivery child")

        with pytest.raises(ValueError, match="invalid"):
            state.set_delivery_terminal_stop(code, source=source)

        assert state.failed is False
        assert state.delivery_stop_code is None

    def test_invalid_persisted_terminal_disposition_fails_validation(self):
        state = TaskState(
            task_id="delivery-invalid",
            task_description="delivery child",
            delivery_stop_disposition={"disposition": "external_dependency_gap"},
        )

        with pytest.raises(ValueError, match="malformed"):
            state.delivery_stop_code_from_disposition()

    def test_invalid_persisted_disposition_parse_error_fails_validation(self):
        state = TaskState(
            task_id="delivery-invalid-parse-error",
            task_description="delivery child",
            delivery_disposition_parse_error={"error_code": "delivery_disposition.keys_invalid"},
        )

        with pytest.raises(ValueError, match="parse-error state is malformed"):
            state.delivery_stop_code_from_parse_error()

    @pytest.mark.parametrize(
        ("field", "value", "expected_message"),
        [
            ("schema_version", None, "schema is invalid"),
            ("disposition", "fix_in_scope", "value is invalid"),
            ("recommended_action", "bounded_fix", "recovery action is invalid"),
            ("summary", "", "summary is invalid"),
            ("summary", "Read /Users/example/private/task.md", "summary is invalid"),
            ("source", "orchestrator", "source is invalid"),
            ("timestamp", "not-a-timestamp", "timestamp is invalid"),
        ],
    )
    def test_persisted_terminal_disposition_rejects_invalid_fields(
        self,
        field: str,
        value: object,
        expected_message: str,
    ) -> None:
        disposition = {
            "schema_version": 1,
            "disposition": "external_dependency_gap",
            "summary": "The required API belongs to another repository.",
            "recommended_action": "external_dependency_follow_up",
            "source": "implementer",
            "timestamp": "2026-07-20T10:03:00Z",
        }
        disposition[field] = value
        state = TaskState(
            task_id="delivery-invalid-field",
            task_description="delivery child",
            delivery_stop_disposition=disposition,
        )

        with pytest.raises(ValueError, match=expected_message):
            state.delivery_stop_code_from_disposition()

    def test_persisted_scope_amendment_disposition_requires_review_source(self) -> None:
        state = TaskState(
            task_id="delivery-invalid-scope-source",
            task_description="delivery child",
            delivery_stop_disposition={
                "schema_version": 1,
                "disposition": "requires_scope_amendment",
                "summary": "The bounded unit needs another production surface.",
                "recommended_action": "delivery_amend_prepare",
                "source": "implementer",
                "timestamp": "2026-07-20T10:03:00Z",
            },
        )

        with pytest.raises(ValueError, match="scope-amendment disposition source is invalid"):
            state.delivery_stop_code_from_disposition()

    def test_terminal_disposition_setter_rejects_unknown_source_and_unparsed_value(self) -> None:
        parsed = parse_delivery_disposition(
            json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
                    "summary": "The required API belongs to another repository.",
                }
            ),
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )
        assert parsed is not None
        state = TaskState(task_id="delivery-invalid-setter", task_description="delivery child")

        with pytest.raises(ValueError, match="source is invalid"):
            state.set_delivery_stop_disposition("orchestrator", parsed)
        with pytest.raises(TypeError, match="parser-validated"):
            state.set_delivery_stop_disposition("implementer", object())  # type: ignore[arg-type]

        assert state.delivery_stop_disposition is None

    def test_terminal_stop_rejects_invalid_persisted_code(self) -> None:
        state = TaskState(
            task_id="delivery-invalid-persisted-stop",
            task_description="delivery child",
            delivery_stop_code="unknown_stop",
        )

        with pytest.raises(ValueError, match="persisted delivery terminal stop code is invalid"):
            state.set_delivery_terminal_stop(
                state_module.DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
                source="orchestrator",
            )

    def test_non_terminal_disposition_cannot_enter_terminal_state_field(self):
        parsed = parse_delivery_disposition(
            json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": DELIVERY_DISPOSITION_FIX_IN_SCOPE,
                    "summary": "Fix the bounded null check.",
                }
            ),
            allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS,
        )
        assert parsed is not None
        state = TaskState(task_id="delivery-fix", task_description="delivery child")

        with pytest.raises(ValueError, match="not terminal"):
            state.set_delivery_stop_disposition("reviewer", parsed)

        assert state.delivery_stop_disposition is None

    @pytest.mark.parametrize(
        ("plan_id", "unit_id", "plan_path"),
        [
            ("p1", "u1", "path1"),
            (None, "u1", "path1"),
            ("p1", None, "path1"),
            ("p1", "u1", None),
            (None, None, None),
        ],
    )
    def test_create_delivery_metadata_nullability(
        self, tmp_path: Path, plan_id: str | None, unit_id: str | None, plan_path: str | None
    ):
        store = JsonStateStore(tmp_path)
        state = store.create(
            "task description",
            delivery_plan_id=plan_id,
            delivery_unit_id=unit_id,
            delivery_plan_path=plan_path,
        )
        assert state.delivery_plan_id == plan_id
        assert state.delivery_unit_id == unit_id
        assert state.delivery_plan_path == plan_path

        loaded = store.load(state.task_id)
        assert loaded is not None
        assert loaded.delivery_plan_id == plan_id
        assert loaded.delivery_unit_id == unit_id
        assert loaded.delivery_plan_path == plan_path

    @pytest.mark.parametrize("field_name", ["delivery_plan_id", "delivery_unit_id", "delivery_plan_path"])
    def test_load_old_state_without_delivery_metadata_defaults_to_none(self, tmp_path: Path, field_name: str):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldchild1", task_description="old child task")
        store.save(state)
        path = tmp_path / "oldchild1.json"
        data = json.loads(path.read_text())
        data.pop(field_name, None)
        path.write_text(json.dumps(data))

        loaded = store.load("oldchild1")
        assert loaded is not None
        assert getattr(loaded, field_name) is None

    def test_load_old_state_without_delivery_constraint_context_uses_legacy_defaults(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldconstraints1", task_description="old child task")
        store.save(state)
        path = tmp_path / "oldconstraints1.json"
        data = json.loads(path.read_text())
        data.pop("delivery_constraint_context_schema_version", None)
        data.pop("delivery_source_task", None)
        data.pop("delivery_inherited_constraints", None)
        data.pop("delivery_constraint_context_fingerprint", None)
        path.write_text(json.dumps(data))

        loaded = store.load("oldconstraints1")

        assert loaded is not None
        assert loaded.delivery_constraint_context_schema_version is None
        assert loaded.delivery_source_task is None
        assert loaded.delivery_inherited_constraints == []
        assert loaded.delivery_constraint_context_fingerprint is None

    def test_load_old_state_without_delivery_write_scope_uses_legacy_defaults(self, tmp_path: Path):
        store = JsonStateStore(tmp_path)
        state = TaskState(task_id="oldwritescope1", task_description="old child task")
        store.save(state)
        path = tmp_path / "oldwritescope1.json"
        data = json.loads(path.read_text())
        data.pop("delivery_write_scope_schema_version", None)
        data.pop("delivery_write_scope_mode", None)
        data.pop("delivery_declared_write_paths", None)
        data.pop("delivery_declared_write_exact_file_paths", None)
        data.pop("delivery_effective_write_paths", None)
        data.pop("delivery_effective_write_exact_file_paths", None)
        data.pop("delivery_runtime_write_scope_binding", None)
        path.write_text(json.dumps(data))

        loaded = store.load("oldwritescope1")

        assert loaded is not None
        assert loaded.delivery_write_scope_schema_version is None
        assert loaded.delivery_write_scope_mode is None
        assert loaded.delivery_declared_write_paths == []
        assert loaded.delivery_declared_write_exact_file_paths is None
        assert loaded.delivery_effective_write_paths == []
        assert loaded.delivery_effective_write_exact_file_paths is None
        assert loaded.delivery_runtime_write_scope_binding is None
