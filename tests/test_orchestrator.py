"""Tests for core/orchestrator.py — pipeline orchestration logic."""

from __future__ import annotations

import subprocess
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

import core.orchestrator as orchestrator_module
from agents.base_agent import AgentResult
from tests.conftest import StubLLMClient
from core.orchestrator import Orchestrator, OrchestratorConfig, _build_tool, _fmt_elapsed
from core.state import JsonStateStore, TaskState
from core.test_execution_gate_audit import detect_new_test_execution_gates
from tools.base_tool import Sandbox, ToolResult
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class StubAgent:
    name: str = ""
    calls: list = field(default_factory=list)
    side_effect: Callable[[TaskState], None] | None = None
    result_data: dict | None = None  # when set, overrides auto-detected files_written
    result_success: bool = True
    result_message: str | None = None
    raise_exception: Exception | None = None

    def run(self, state: TaskState) -> AgentResult:
        if self.raise_exception:
            raise self.raise_exception
        self.calls.append(state)
        files_before = list(state.files_changed)
        if self.side_effect:
            self.side_effect(state)
        if state.failed:
            return AgentResult(success=False, message=f"{self.name} failed")
        data = (
            self.result_data
            if self.result_data is not None
            else {"files_written": [f for f in state.files_changed if f not in files_before]}
        )
        return AgentResult(success=self.result_success, message=self.result_message or f"{self.name} ok", data=data)


class StubBuildTool:
    def __init__(self) -> None:
        self.sync_success = True
        self.compile_success = True
        self.test_success = True
        self.check_success = True
        self.presync_success = True
        self.compile_results: list[bool] = []
        self.check_results: dict[str, list[bool]] = {}
        self.build_config_files: set[str] = set()
        self.sync_calls = 0
        self.compile_calls = 0
        self.test_calls = 0
        self.presync_calls = 0
        self.check_calls: list[str] = []
        self.check_configs: list[tuple[str, dict]] = []
        self.compile_side_effect: Callable[[], None] | None = None
        self.test_side_effect: Callable[[], None] | None = None
        self.check_side_effects: dict[str, Callable[[], None]] = {}
        self.sync_side_effect: Callable[[], None] | None = None
        self.sync_metadata: dict = {}
        self.sync_adoptable_files: set[str] = set()

    def sync(self) -> ToolResult:
        self.sync_calls += 1
        if self.sync_side_effect:
            self.sync_side_effect()
        return ToolResult(
            success=self.sync_success,
            output="",
            error="" if self.sync_success else "sync failed",
            metadata=dict(self.sync_metadata),
        )

    def generate_sources(self) -> ToolResult:
        self.presync_calls += 1
        return ToolResult(
            success=self.presync_success, output="", error="" if self.presync_success else "presync failed"
        )

    def compile_check(self) -> ToolResult:
        self.compile_calls += 1
        if self.compile_side_effect:
            self.compile_side_effect()
        if self.compile_results:
            success = self.compile_results.pop(0)
        else:
            success = self.compile_success
        return ToolResult(success=success, output="", error="" if success else "compile failed")

    def run_tests(self) -> ToolResult:
        self.test_calls += 1
        if self.test_side_effect:
            self.test_side_effect()
        return ToolResult(success=self.test_success, output="", error="" if self.test_success else "tests failed")

    def run_check(self, name: str, config: dict) -> ToolResult:
        self.check_calls.append(name)
        self.check_configs.append((name, config))
        if name in self.check_side_effects:
            self.check_side_effects[name]()
        if name in self.check_results and self.check_results[name]:
            success = self.check_results[name].pop(0)
        else:
            success = self.check_success
        error_msg = "" if success else f"{name} failed"
        return ToolResult(success=success, output=error_msg, error=error_msg)

    def is_build_config_file(self, path: str) -> bool:
        return path in self.build_config_files

    def is_sync_adoptable_file(self, path: str) -> bool:
        return path in self.sync_adoptable_files


class RetryReportingAgent:
    def __init__(self) -> None:
        self.llm = StubLLMClient()

    def run(self, state: TaskState) -> AgentResult:
        self.llm._retry_observer(
            {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "operation": "run_agent",
                "attempt": 1,
                "max_attempts": 4,
                "delay_s": 30,
                "error": "temporary provider failure",
                "error_type": "RuntimeError",
            }
        )
        state.record("test_writer", "test_write", "files changed: []")
        return AgentResult(success=True, message="ok", data={"files_written": []})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestPathClassification:
    def test_test_path_marker_requires_explicit_marker_or_separator(self):
        assert orchestrator_module._path_looks_like_test_artifact(
            "feature/countries/src/integrationTest/kotlin/CountriesModule.kt"
        )
        assert orchestrator_module._path_looks_like_test_artifact(
            "feature/countries/src/integration-test/kotlin/CountriesModule.kt"
        )
        assert not orchestrator_module._path_looks_like_test_artifact("feature/latest/src/main/App.ts")
        assert not orchestrator_module._path_looks_like_test_artifact("feature/contest/src/main/App.ts")

    def test_test_audit_candidates_include_source_files_for_inline_tests(self):
        assert orchestrator_module._path_looks_like_test_audit_candidate("src/lib.rs")
        assert orchestrator_module._path_looks_like_test_audit_candidate("src/main/kotlin/InlineTests.kt")
        assert orchestrator_module._path_looks_like_test_audit_candidate("tests/clientMain.test.ts")
        assert not orchestrator_module._path_looks_like_test_audit_candidate("assets/logo.png")


def _make_orchestrator(
    tmp_path: Path,
    **config_kwargs,
) -> tuple[Orchestrator, dict[str, StubAgent], StubBuildTool]:
    project_config = config_kwargs.pop("project_config", {"project": {"build_tool": "python"}})
    config = OrchestratorConfig(
        project_root=tmp_path,
        allowed_write_paths=["."],
        allowed_read_paths=["."],
        project_config=project_config,
        **config_kwargs,
    )
    store = JsonStateStore(tmp_path / "state")
    orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=store)

    stubs: dict[str, StubAgent] = {
        name: StubAgent(name=name)
        for name in ["analyst", "planner", "implementer", "reviewer", "security_reviewer", "test_writer", "fixer"]
    }

    # Default reviewer/security_reviewer stubs approve so that tests not focused on
    # review behaviour don't fail due to the "no decision" guard added in Bug 2 fix.
    # Tests that care about rejection override these side_effects explicitly.
    def _default_reviewer(state: TaskState) -> None:
        state.review_approved = True
        state.review_issues = []

    def _default_security_reviewer(state: TaskState) -> None:
        state.security_approved = True
        state.review_issues = []

    stubs["reviewer"].side_effect = _default_reviewer
    stubs["security_reviewer"].side_effect = _default_security_reviewer

    orch._agents = stubs  # type: ignore[assignment]

    build = StubBuildTool()
    orch._tools["build"] = build

    return orch, stubs, build


def _save_state(orch: Orchestrator, **kwargs) -> TaskState:
    state = TaskState(task_id="t1", task_description="test task", **kwargs)
    orch._store.save(state)
    return state


# ---------------------------------------------------------------------------
# Tests — loop gates and idempotency
# ---------------------------------------------------------------------------


class TestOrchestratorLoop:
    def test_run_agent_records_active_operation_while_agent_runs(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=60)

        def assert_active_operation(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "agent"
            assert loaded.active_operation["agent"] == "analyst"

        stubs["analyst"].side_effect = assert_active_operation
        state = _save_state(orch)

        result = orch._run_agent("analyst", state)
        loaded = orch._store.load(state.task_id)

        assert result.success
        assert loaded is not None
        assert loaded.active_operation is None

    def test_run_agent_sets_scoped_llm_session_title(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)
        llm = StubLLMClient()
        stubs["analyst"].llm = llm
        state = TaskState(
            task_id="5b891efb881f4388a8f0fd0578f98573",
            task_description="Confidential Customer Bug SECRET-123",
        )
        orch._store.save(state)

        def assert_session_title(_state: TaskState) -> None:
            title = getattr(llm, "_session_title")
            assert title == "sikula-analyst-5b891efb"
            assert "Confidential" not in title
            assert "SECRET-123" not in title

        stubs["analyst"].side_effect = assert_session_title

        result = orch._run_agent("analyst", state)

        assert result.success
        assert getattr(llm, "_session_title") is None

    def test_run_agent_skips_active_operation_when_heartbeat_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)

        def assert_no_active_operation(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is None

        stubs["analyst"].side_effect = assert_no_active_operation
        state = _save_state(orch)

        result = orch._run_agent("analyst", state)

        assert result.success

    def test_test_writer_audit_prep_records_active_operation_before_agent(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            heartbeat_interval_seconds=60,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def restore_snapshot_effect(state: TaskState, extra_paths=()) -> dict[str, str | None]:
            assert list(extra_paths) == []
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "test_writer audit prep"
            assert loaded.active_operation["message"] == "Preparing test-writer audit baseline"
            return {}

        def test_writer_effect(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "agent"
            assert loaded.active_operation["agent"] == "test_writer"
            state.tests_up_to_date = True

        monkeypatch.setattr(orch, "_test_writer_restore_snapshot", restore_snapshot_effect)
        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)
        loaded = orch._store.load(state.task_id)

        assert changed is False
        assert loaded is not None
        assert loaded.active_operation is None

    def test_run_clears_stale_active_operation_on_resume(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)
        state = _save_state(orch, implementation_prompt="already analyzed")
        state.start_active_operation("agent", agent="analyst", message="stale")
        orch._store.save(state)

        def assert_stale_operation_cleared(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is None
            state.review_approved = True

        stubs["implementer"].side_effect = assert_stale_operation_cleared

        orch.run(task_id=state.task_id)

    def test_agents_receive_effective_pipeline_flags(self, tmp_path: Path):
        config = OrchestratorConfig(
            project_root=tmp_path,
            allowed_write_paths=["."],
            allowed_read_paths=["."],
            run_build=True,
            run_tests=True,
            run_checks=False,
            project_config={
                "project": {"build_tool": "python"},
                "run_checks": True,
                "build": {"checks": [{"name": "ruff", "command": "ruff check ."}]},
            },
        )

        orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=JsonStateStore(tmp_path / "state"))

        effective = orch._agents["reviewer"].project_config["__sikula_effective_pipeline"]
        assert effective == {"run_build": True, "run_tests": True, "run_checks": False}

    def test_uncovered_task_validation_command_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {
                    "test_command": "cargo test",
                    "checks": [{"name": "fmt", "command": "cargo fmt --check"}],
                },
            },
        )

        result = orch.run(
            task_description="Add export support. Acceptance: run `cargo run -p codegen_tool -- fixtures/`."
        )

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["action"] == "abort" and "validation coverage gap" in entry["result"] for entry in result.history
        )
        assert any(
            entry["phase"] == "validation_coverage" and entry["status"] == "failed"
            for entry in result.validation_cycle_records
        )

    def test_near_match_task_validation_command_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )

        result = orch.run(task_description="Update config loader. Run `cargo test --workspace --all-features`.")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["phase"] == "validation_coverage"
            and entry["status"] == "failed"
            and "cargo test --workspace --all-features" in entry.get("error_excerpt", "")
            for entry in result.validation_cycle_records
        )

    def test_node_detected_script_defaults_cover_task_validation_commands(self, tmp_path: Path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "package.json").write_text('{"scripts": {"typecheck": "tsc --noEmit", "test": "vitest run"}}')
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "node", "root_path": str(tmp_path)},
                "build": {},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/index.ts")

        result = orch.run(task_description="## Verification\n\npnpm run typecheck\npnpm test\n")

        assert result.done
        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_validation_heading_with_blank_separator_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )

        result = orch.run(task_description="## Verification\n\ncargo test --workspace --all-features\n")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["phase"] == "validation_coverage"
            and entry["status"] == "failed"
            and "cargo test --workspace --all-features" in entry.get("error_excerpt", "")
            for entry in result.validation_cycle_records
        )

    def test_unknown_review_mode_does_not_skip_validation_coverage_preflight(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        state = _save_state(
            orch,
            review_mode="unknown",
        )
        state.task_description = "Update parser. Run `cargo test --workspace --all-features`."
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_prose_starting_with_tool_name_does_not_fail_validation_coverage(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.rs")

        result = orch.run(
            task_description=(
                "python parser should reject invalid quoted strings.\n"
                "cargo features should remain optional.\n"
                "- `cargo` features should remain optional."
            )
        )

        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_python_module_validation_command_is_covered_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "python"},
                "build": {"test_command": "python3 -m pytest"},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.py")

        result = orch.run(task_description="Update parser. Run `pytest`.")

        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_package_script_shortcut_validation_command_is_covered_before_agents(self, tmp_path: Path):
        cases = [
            (
                tmp_path / "npm",
                {"name": "npm-tests", "command": "npm run test"},
                "Update package. Run `npm test`.",
            ),
            (
                tmp_path / "yarn",
                {"name": "yarn-tests", "command": "yarn run test"},
                "Update package. Run `yarn test`.",
            ),
        ]

        for root, check, task_description in cases:
            root.mkdir()
            orch, stubs, _ = _make_orchestrator(
                root,
                run_build=True,
                run_tests=False,
                run_checks=True,
                project_config={
                    "project": {"build_tool": "python"},
                    "build": {
                        "compile_command": "ruff check .",
                        "checks": [check],
                    },
                },
            )
            stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.py")

            result = orch.run(task_description=task_description)

            assert not result.failed
            assert stubs["analyst"].calls
            assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_wrapper_alias_validation_command_is_covered_before_agents(self, tmp_path: Path):
        cases = [
            (
                tmp_path / "gradle",
                {"project": {"build_tool": "gradle-android"}, "build": {}},
                "Update UI. Run `./gradlew testDebugUnitTest`.",
            ),
            (
                tmp_path / "maven",
                {"project": {"build_tool": "maven"}, "build": {}},
                "Update API. Run `./mvnw test`.",
            ),
        ]

        for root, project_config, task_description in cases:
            root.mkdir()
            orch, stubs, _ = _make_orchestrator(
                root,
                run_build=True,
                run_tests=True,
                run_checks=True,
                project_config=project_config,
            )
            stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/Main")

            result = orch.run(task_description=task_description)

            assert not result.failed
            assert stubs["analyst"].calls
            assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_done_task_skips_all_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, done=True)
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())

    def test_failed_task_skips_all_agents(self, tmp_path: Path, caplog):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, failed=True)
        caplog.set_level("INFO")
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())
        assert "--reset-failed" in caplog.text
        assert "nothing to do" not in caplog.text

    def test_contract_gate_failed_task_does_not_log_reset_hint(self, tmp_path: Path, caplog):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, failed=True, contract_gate_blocked=True)
        caplog.set_level("INFO")
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())
        assert "failed by contract readiness gate" in caplog.text
        assert "--reset-failed" not in caplog.text

    def test_agent_llm_retry_is_recorded_in_history(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = TaskState(task_id="t1", task_description="test")
        orch._agents["test_writer"] = RetryReportingAgent()  # type: ignore[assignment]

        result = orch._run_agent("test_writer", state)

        assert result.success
        retry = state.history[0]
        assert retry["agent"] == "test_writer"
        assert retry["action"] == "llm_retry"
        assert retry["result"] == "temporary provider failure"
        assert retry["provider"] == "codex"
        assert retry["model"] == "gpt-5.3-codex"
        assert retry["operation"] == "run_agent"
        assert retry["attempt"] == 1
        assert retry["max_attempts"] == 4
        assert retry["delay_s"] == 30
        assert retry["error_type"] == "RuntimeError"
        assert state.history[1]["action"] == "test_write"

    def test_analyze_phase_skipped_when_prompt_exists(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(orch, implementation_prompt="existing", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["analyst"].calls) == 0

    def test_analyst_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        stubs["analyst"].side_effect = lambda s: setattr(s, "failed", True)
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 0

    def test_analyst_result_failure_aborts_before_planner(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)

        class FailingAnalyst:
            def __init__(self) -> None:
                self.calls: list[TaskState] = []

            def run(self, state: TaskState) -> AgentResult:
                self.calls.append(state)
                state.record("analyst", "analyze_failed", "invalid implementation prompt")
                return AgentResult(success=False, message="invalid implementation prompt")

        failing_analyst = FailingAnalyst()
        orch._agents["analyst"] = failing_analyst  # type: ignore[assignment]
        _save_state(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(failing_analyst.calls) == 1
        assert len(stubs["planner"].calls) == 0
        assert len(stubs["implementer"].calls) == 0

    def test_planner_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["planner"].calls) == 0

    def test_planner_skipped_when_already_decided(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        _save_state(orch, implementation_prompt="p", plan_decided=True, files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["planner"].calls) == 0

    def test_implementer_no_files_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, implementation_prompt="p")
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 1

    def test_implementer_no_files_adopts_dirty_worktree_on_resume(self, tmp_path: Path):
        # Simulate a worktree where the previous (interrupted) implementer run modified a
        # tracked file but the process was killed before state.files_changed was populated.
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "src").mkdir()
        original = tmp_path / "src" / "main.py"
        original.write_text("# original")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        original.write_text("# modified by previous interrupted implementer run")

        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(
            orch,
            implementation_prompt="p",
            worktree_path=str(tmp_path),
            worktree_base=str(tmp_path),
        )
        result = orch.run(task_id="t1")

        assert not result.failed
        assert "src/main.py" in result.files_changed
        assert any(h["action"] == "adopt_worktree_changes" for h in result.history)

    def test_implementer_skipped_when_files_already_changed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["implementer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — build/fix loop
# ---------------------------------------------------------------------------


class TestOrchestratorBuildLoop:
    def _build_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

    def test_max_build_iterations_sets_failed(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert result.failed
        assert result.build_iterations == 2

    def test_active_build_loop_budget_survives_resume(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=0,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.build_iterations == 2
        assert build.compile_calls == 1

    def test_last_fixer_change_gets_final_validation_chance(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        stubs["fixer"].side_effect = fixer_effect
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert result.build_iterations == 2
        assert build.compile_calls == 2
        assert len(stubs["fixer"].calls) == 1
        assert any(record["action"] == "final_validation_after_fix" for record in result.history)

    def test_final_validation_after_last_fix_does_not_run_fixer_again(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        build.compile_results = [False, False]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        stubs["fixer"].side_effect = fixer_effect
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.build_iterations == 2
        assert build.compile_calls == 2
        assert len(stubs["fixer"].calls) == 1

    def test_resume_after_last_fix_gets_final_validation_chance(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "src/fix.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=0,
            fixer_changed_code=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.build_iterations == 2
        assert build.compile_calls == 1
        assert len(stubs["fixer"].calls) == 0

    def test_sync_skipped_when_already_synced(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True)
        self._build_ready(orch)
        orch.run(task_id="t1")
        assert build.sync_calls == 0

    def test_sync_runs_when_not_synced(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        orch.run(task_id="t1")
        assert build.sync_calls == 1

    def test_tests_skipped_when_disabled(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=False)
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert build.test_calls == 0
        assert result.test_status == "skipped"

    def test_done_set_when_build_passes(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_build=True, run_tests=False, run_checks=False)
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert result.done
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_test_and_check_statuses_set_when_enabled_phases_pass(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, run_checks=True)
        self._build_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert build.test_calls == 1
        assert result.test_status == "success"
        assert result.check_status == "skipped"

    def test_validation_records_capture_successful_build_test_and_skipped_checks(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_build=True, run_tests=True, run_checks=True)
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        phases = [(r["phase"], r["status"]) for r in result.validation_cycle_records]
        assert ("build", "success") in phases
        assert ("test", "success") in phases
        assert ("check", "skipped") in phases
        assert all(r["build_iteration"] == 1 for r in result.validation_cycle_records)

    def test_validation_records_capture_failed_build_error_excerpt(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.compile_success = False
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert build_records[-1]["status"] == "failed"
        assert build_records[-1]["error_excerpt"] == "compile failed"

    def test_validation_records_capture_configured_check_name(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=True,
        )
        orch._config.project_config["build"] = {"checks": [{"name": "ruff", "command": "ruff check ."}]}
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        check_records = [r for r in result.validation_cycle_records if r["phase"] == "check"]
        assert len(check_records) == 1
        assert check_records[0]["status"] == "success"
        assert check_records[0]["build_iteration"] == 1
        assert check_records[0]["step"] == 0
        assert check_records[0]["check_name"] == "ruff"
        assert "timestamp" in check_records[0]


# ---------------------------------------------------------------------------
# Tests — review loop
# ---------------------------------------------------------------------------


class TestOrchestratorReviewLoop:
    def _review_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

    def test_review_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=False, run_build=False)
        self._review_ready(orch)
        result = orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 0
        assert result.done
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_review_and_security_skipped_when_both_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=False,
            run_security_review=False,
            run_build=False,
        )
        self._review_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 0
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_review_skipped_when_already_approved(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=True)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 0

    def test_max_review_iterations_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=2)
        self._review_ready(orch)

        def always_issues(state: TaskState) -> None:
            state.review_issues = ["dead field X"]

        stubs["reviewer"].side_effect = always_issues
        result = orch.run(task_id="t1")
        assert result.failed
        # max_review_iterations=2 fix attempts → 3 reviews (initial + 2 post-fix), 2 implements
        assert len(stubs["reviewer"].calls) == 3
        assert len(stubs["implementer"].calls) == 2

    def test_every_implement_fix_gets_reviewed(self, tmp_path: Path):
        """Every fix attempt must be followed by a review — implementer never runs unreviewd."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)
        self._review_ready(orch)

        def always_issues(state: TaskState) -> None:
            state.review_issues = ["dead field X"]

        stubs["reviewer"].side_effect = always_issues
        orch.run(task_id="t1")
        # max=3 fix attempts → 4 reviews, 3 implements; reviews == implements + 1
        assert len(stubs["reviewer"].calls) == 4
        assert len(stubs["implementer"].calls) == 3
        assert len(stubs["reviewer"].calls) == len(stubs["implementer"].calls) + 1

    def test_review_issues_trigger_implementer_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)
        call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                state.review_issues = ["missing null check"]
            else:
                state.review_approved = True
                state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        self._review_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1

    def test_noop_review_fix_does_not_mark_code_or_tests_stale(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=False,
            max_review_iterations=1,
        )
        call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                state.review_approved = False
                state.review_issues = ["false positive already addressed"]
            else:
                state.review_approved = True
                state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": []}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            tests_up_to_date=True,
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.tests_up_to_date is True
        assert result.build_synced is True
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1

    def test_review_fix_implementer_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = False
            state.review_issues = ["missing null check"]

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "quota exhausted"
        self._review_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 1
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: quota exhausted"
            for entry in result.history
        )

    def test_reviewer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        stubs["reviewer"].side_effect = None
        stubs["reviewer"].result_success = False
        stubs["reviewer"].result_message = "usage limit reached"
        self._review_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "reviewer failed: usage limit reached"
            for entry in result.history
        )


# ---------------------------------------------------------------------------
# Tests — security review loop
# ---------------------------------------------------------------------------


class TestOrchestratorSecurityLoop:
    def _security_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=True)

    def test_security_review_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_security_review=False, run_build=False)
        self._security_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["security_reviewer"].calls) == 0

    def test_max_security_iterations_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path, run_security_review=True, run_review=False, run_build=False, max_security_review_iterations=2
        )
        self._security_ready(orch)

        def always_blocking(state: TaskState) -> None:
            state.review_issues = ["hardcoded secret"]

        stubs["security_reviewer"].side_effect = always_blocking
        result = orch.run(task_id="t1")
        assert result.failed
        # max=2 fix attempts → 3 security reviews, 2 implements
        assert len(stubs["security_reviewer"].calls) == 3
        assert len(stubs["implementer"].calls) == 2

    def test_security_blocking_issue_triggers_re_review(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=2,
            max_review_iterations=3,
        )
        sec_calls = {"n": 0}

        def security_effect(state: TaskState) -> None:
            sec_calls["n"] += 1
            if sec_calls["n"] == 1:
                state.review_issues = ["hardcoded key"]
            else:
                state.security_approved = True
                state.review_issues.clear()

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["security_reviewer"].side_effect = security_effect
        stubs["reviewer"].side_effect = reviewer_effect
        self._security_ready(orch)
        result = orch.run(task_id="t1")
        assert not result.failed
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) >= 1

    def test_noop_security_fix_does_not_mark_code_or_tests_stale(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=1,
            max_review_iterations=1,
        )
        security_calls = {"n": 0}

        def security_effect(state: TaskState) -> None:
            security_calls["n"] += 1
            if security_calls["n"] == 1:
                state.security_approved = False
                state.review_issues = ["false positive already addressed"]
            else:
                state.security_approved = True
                state.review_issues.clear()

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["security_reviewer"].side_effect = security_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": []}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=True,
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.tests_up_to_date is True
        assert result.build_synced is True
        assert len(stubs["security_reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 1

    def test_security_fix_implementer_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=2,
            max_review_iterations=3,
        )

        def security_effect(state: TaskState) -> None:
            state.security_approved = False
            state.review_issues = ["hardcoded key"]

        stubs["security_reviewer"].side_effect = security_effect
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "not authenticated"
        self._security_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: not authenticated"
            for entry in result.history
        )

    def test_security_reviewer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=False,
            run_build=False,
        )
        stubs["security_reviewer"].side_effect = None
        stubs["security_reviewer"].result_success = False
        stubs["security_reviewer"].result_message = "not authenticated"
        self._security_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "security_reviewer failed: not authenticated"
            for entry in result.history
        )

    def test_security_warnings_only_do_not_trigger_implementer_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
        )

        def security_warning_effect(state: TaskState) -> None:
            state.security_review_cycle_records.append(
                {
                    "reviewer_output": "## Warnings\n\n### Missing audit log\nConcern: low-risk observability gap",
                    "approved": True,
                    "has_warnings": True,
                }
            )
            state.security_approved = True

        stubs["security_reviewer"].side_effect = security_warning_effect
        self._security_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert not result.failed
        assert len(stubs["implementer"].calls) == 0
        assert result.security_review_cycle_records[-1]["has_warnings"] is True


# ---------------------------------------------------------------------------
# Tests — fix phase flag resets
# ---------------------------------------------------------------------------


class TestOrchestratorFixPhase:
    def test_fix_resets_build_synced_on_build_config_change(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        build.build_config_files = {"requirements.txt"}

        def fixer_effect(state: TaskState) -> None:
            if "requirements.txt" not in state.files_changed:
                state.files_changed.append("requirements.txt")

        stubs["fixer"].side_effect = fixer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        orch.run(task_id="t1")
        assert build.sync_calls > 0

    def test_fix_new_files_defer_re_review_until_validation_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_success = False

        def fixer_effect(state: TaskState) -> None:
            if "src/new.py" not in state.files_changed:
                state.files_changed.append("src/new.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 0

    def test_fix_new_files_trigger_re_review_after_validation_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/new.py" not in state.files_changed:
                state.files_changed.append("src/new.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 2
        assert len(stubs["reviewer"].calls) == 1

    def test_fix_re_edit_of_existing_file_triggers_re_review(self, tmp_path: Path):
        """Re-editing an already-tracked file must still trigger review after validation passes."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_results = [False, True]

        # Fixer always reports editing src/main.py — which is already in files_changed.
        # Before the fix, new_files would be empty (set-diff misses re-edits), so review
        # would not be marked stale. With the fix, fixer_result.data["files_written"] is used.
        stubs["fixer"].result_data = {"files_written": ["src/main.py"]}

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1

    def test_fix_triggers_security_re_review(self, tmp_path: Path):
        """After fixer writes files, both reviewer and security reviewer run after validation passes."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            max_iterations=2,
            max_review_iterations=2,
            max_security_review_iterations=2,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def security_effect(state: TaskState) -> None:
            state.security_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["security_reviewer"].side_effect = security_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1

    def test_test_writer_changes_after_post_fix_review_trigger_another_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
            max_iterations=3,
        )
        build.compile_results = [False, True, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def test_writer_effect(state: TaskState) -> None:
            if "tests/test_fix.py" not in state.files_changed:
                state.files_changed.append("tests/test_fix.py")
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 3
        assert len(stubs["test_writer"].calls) == 1

    def test_resume_after_fixer_validates_before_re_review(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=2,
        )
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            fixer_changed_code=True,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_resume_active_build_loop_validates_before_re_review_when_fixer_flag_cleared(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=1,
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_resume_active_final_build_loop_keeps_final_scope_before_re_review(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        review_scopes: list[str | None] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope)
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            plan=["Step 1", "Step 2"],
            plan_decided=True,
            plan_completed=True,
            current_step=1,
            step_implemented=True,
            build_synced=True,
            build_iterations=2,
            build_loop_key="final_full_task",
            build_loop_start_iteration=2,
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 1
        assert review_scopes == ["final_full_task"]

    def test_review_fix_build_config_change_resyncs_before_next_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
            max_review_iterations=2,
        )
        build.compile_results = [False, True, True]
        build.build_config_files = {"pyproject.toml"}
        review_calls = {"n": 0}

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs config fix"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            if "pyproject.toml" not in state.files_changed:
                state.files_changed.append("pyproject.toml")

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 3
        assert build.sync_calls == 1

    def test_resume_after_interrupt_between_fixer_and_reviewer_runs_review(self, tmp_path: Path):
        """Simulate interrupt after fixer ran and flags were reset+saved, but reviewer had not yet run.
        On resume, review_approved=False must cause reviewer to run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, max_review_iterations=2)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # State reflects: fixer ran, flags reset and saved, reviewer not yet run
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_fixer_no_files_skips_review(self, tmp_path: Path):
        """If fixer writes no files, review must not be re-triggered."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_success = False

        # Fixer reports no files written
        stubs["fixer"].result_data = {"files_written": []}

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        orch.run(task_id="t1")

        assert len(stubs["reviewer"].calls) == 0

    def test_test_only_fix_preserves_review_and_test_writer_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        def fixer_effect(state: TaskState) -> None:
            if "tests/test_main.py" not in state.files_changed:
                state.files_changed.append("tests/test_main.py")

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 0
        assert any(record["action"] == "test_only_fix" for record in result.history)

    def test_test_only_fix_execution_gate_audit_triggers_followup_fix(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=4,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_existing():\n    assert True\n")
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            state.errors.clear()
            state.test_errors.clear()
            state.check_errors.clear()
            if attempts["count"] == 1:
                test_file.write_text(
                    'if (typeof document === "undefined") {\n'
                    '  test("client main tests require DOM", () => {});\n'
                    "} else {\n"
                    '  test("opens detail", () => {});\n'
                    "}\n"
                )
            else:
                test_file.write_text("def test_existing_seam():\n    assert True\n")
            if "tests/test_main.py" not in state.files_changed:
                state.files_changed.append("tests/test_main.py")

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/test_main.py"]}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert attempts["count"] == 2
        assert build.test_calls == 2
        assert len(result.test_execution_gate_records) == 1
        assert result.test_execution_gate_records[0]["source"] == "fixer"
        assert result.test_execution_gate_records[0]["status"] == "resolved"
        assert not any(str(error).startswith("TEST EXECUTION GATE AUDIT:") for error in result.test_errors)
        assert any(record["action"] == "test_execution_gate_audit" for record in result.history)

    def test_test_writer_execution_gate_audit_records_pending_test_error(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text('test.skip("changed behavior", () => {});\n')
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert len(state.test_execution_gate_records) == 1
        assert state.test_execution_gate_records[0]["source"] == "test_writer"
        assert "excerpt" not in state.test_execution_gate_records[0]["findings"][0]
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")
        assert 'test.skip("changed behavior"' not in state.test_errors[0]
        assert "skipped JavaScript/TypeScript test" in state.test_errors[0]

    def test_test_writer_audit_pending_counts_only_saved_snapshot(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        monkeypatch.setattr(
            orch,
            "_iter_configured_test_files",
            lambda: (_ for _ in ()).throw(AssertionError("unexpected full test-tree scan")),
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        orch._begin_test_writer_audit_pending(
            state,
            {"tests/target.test.ts": 'test.skip("preexisting target gate", () => {});\n'},
        )

        assert set(state.test_writer_audit_gate_counts) == {"tests/target.test.ts"}
        assert len(state.test_writer_audit_gate_counts["tests/target.test.ts"]) == 1

    def test_test_writer_audit_uses_head_baseline_for_newly_reported_existing_file(self, tmp_path: Path):
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("preexisting project gate", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add preexisting skip"], cwd=tmp_path, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def test_writer_effect(state: TaskState) -> None:
            assert "tests/client_main.test.ts" not in state.test_writer_audit_gate_counts
            test_file.write_text(
                test_file.read_text(encoding="utf-8") + "test('new behavior', () => {});\n",
                encoding="utf-8",
            )
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert state.test_execution_gate_records == []
        assert state.test_errors == []

    def test_resume_missing_snapshot_uses_reported_file_gate_counts(self, tmp_path: Path):
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("preexisting project gate", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add preexisting skip"], cwd=tmp_path, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=True,
            test_writer_audit_files_written=["tests/client_main.test.ts"],
        )
        orch._persist_test_writer_audit_restore_baselines(state, ["tests/client_main.test.ts"])
        orch._store.delete_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT)
        test_file.write_text(
            test_file.read_text(encoding="utf-8") + "test('new behavior', () => {});\n",
            encoding="utf-8",
        )
        orch._store.save(state)

        changed = orch._run_test_write_phase(state)

        assert changed
        assert stubs["test_writer"].calls == []
        assert state.test_execution_gate_records == []
        assert state.test_errors == []

    def test_test_writer_execution_gate_audit_includes_inline_source_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "sandbox": {"allowed_test_write_paths": ["src/"]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        source_file.write_text(
            """\
pub fn answer() -> i32 {
    42
}

#[cfg(test)]
mod tests {
    #[test]
    fn existing_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
            encoding="utf-8",
        )

        def test_writer_effect(state: TaskState) -> None:
            source_file.write_text(
                """\
pub fn answer() -> i32 {
    42
}

#[cfg(test)]
mod tests {
    #[test]
    fn existing_contract() {
        assert_eq!(super::answer(), 42);
    }

    #[ignore]
    #[test]
    fn generated_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("src/lib.rs")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.rs"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert len(state.test_execution_gate_records) == 1
        finding = state.test_execution_gate_records[0]["findings"][0]
        assert finding["path"] == "src/lib.rs"
        assert finding["reason"] == "Rust ignored test"
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")

    def test_test_writer_synthetic_harness_audit_includes_inline_source_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "sandbox": {"allowed_test_write_paths": ["src/"]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        baseline = "pub fn answer() -> i32 {\n    42\n}\n"
        source_file.write_text(baseline, encoding="utf-8")
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                source_file.write_text(
                    baseline
                    + """\

#[cfg(test)]
mod tests {
    struct FakeElement;
    struct FakeEventTarget;
    struct FakeHistory;
    struct FakeServer;
}
""",
                    encoding="utf-8",
                )
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                assert source_file.read_text(encoding="utf-8") == baseline
                source_file.write_text(
                    baseline
                    + """\

#[cfg(test)]
mod tests {
    #[test]
    fn generated_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
                    encoding="utf-8",
                )
            state.tests_up_to_date = True
            state.files_changed.append("src/lib.rs")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["src/lib.rs"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.rs"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert len(state.synthetic_test_harness_records) == 1
        record = state.synthetic_test_harness_records[0]
        assert record["findings"][0]["path"] == "src/lib.rs"
        assert record["status"] == "resolved"
        assert "FakeElement" not in source_file.read_text(encoding="utf-8")

    def test_test_writer_execution_gate_audit_fails_when_build_loop_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text('test.skip("changed behavior", () => {});\n')
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert state.failed
        assert state.history[-1]["action"] == "abort"

    def test_no_build_resume_with_active_execution_gate_audit_fails_even_when_tests_up_to_date(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("changed behavior", () => {});\n', encoding="utf-8")
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            tests_up_to_date=True,
        )
        state.record_test_execution_gate_audit(
            "test_writer",
            detect_new_test_execution_gates(
                path="tests/test_main.py",
                before=None,
                after=test_file.read_text(encoding="utf-8"),
            ),
        )
        orch._store.save(state)

        orch._run_single_pass(state)

        assert state.failed
        assert state.done is False
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")
        assert state.history[-1]["action"] == "abort"

    def test_execution_gate_audit_treats_dot_test_write_root_as_project_root(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            project_config={
                "project": {"build_tool": "rust"},
                "sandbox": {"allowed_test_write_paths": ["."]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        source_file.write_text(
            """\
#[cfg(test)]
mod tests {
    #[ignore]
    #[test]
    fn generated_contract_test() {}
}
""",
            encoding="utf-8",
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/lib.rs"])

        findings = orch._audit_test_execution_gates_after_agent(
            state,
            source="test_writer",
            files_written=["src/lib.rs"],
            before_snapshot={"src/lib.rs": None},
        )

        assert len(findings) == 1
        assert findings[0]["path"] == "src/lib.rs"
        assert findings[0]["reason"] == "Rust ignored test"
        assert len(state.test_execution_gate_records) == 1
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")

    def test_test_writer_dot_root_snapshot_omits_unrelated_source_but_keeps_gate_counts(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "node"},
                "sandbox": {"allowed_test_write_paths": ["."]},
            },
        )
        unrelated = tmp_path / "src" / "unrelated.ts"
        test_file = tmp_path / "tests" / "existing.test.ts"
        unrelated.parent.mkdir()
        test_file.parent.mkdir()
        unrelated.write_text("export const unrelated = 'do not snapshot';\n", encoding="utf-8")
        test_file.write_text('test.skip("external service contract", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test User", "commit", "-m", "baseline"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        snapshots: list[dict[str, str | None]] = []

        def test_writer_effect(state: TaskState) -> None:
            snapshot = orch._store.load_text_snapshot("t1", orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT)
            assert snapshot is not None
            snapshots.append(snapshot)
            test_file.write_text(
                "\n".join(
                    [
                        'test.skip("external service contract", () => {});',
                        'test.skip("external service contract", () => {});',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("tests/existing.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/existing.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.ts"])

        assert orch._run_test_write_phase(state)

        assert snapshots == [{"src/main.ts": None}]
        findings = state.test_execution_gate_records[0]["findings"]
        assert len(findings) == 1
        assert findings[0]["path"] == "tests/existing.test.ts"
        assert findings[0]["baseline_count"] == 1
        assert findings[0]["occurrence"] == 2

    def test_resume_recovers_pending_synthetic_harness_and_retries_test_writer(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        pre_agent_test = "test('previous valid seam', () => expect(true).toBe(true));\n"
        test_file.write_text(
            pre_agent_test
            + "\n".join(
                [
                    "test.skip('generated harness', () => {});",
                    "class FakeEventTarget { addEventListener() {}; dispatchEvent() {} }",
                    "class FakeHistory { pushState() {} }",
                    "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                ]
            ),
            encoding="utf-8",
        )
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {"tests/client_main.test.ts": pre_agent_test},
        )
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            saved = orch._store.load("t1")
            assert saved is not None
            assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
            assert "tests/client_main.test.ts" in saved.files_changed
            assert test_file.read_text(encoding="utf-8") == pre_agent_test
            test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/client_main.test.ts"],
            test_files_written=["tests/client_main.test.ts"],
            tests_up_to_date=True,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=True,
            test_writer_audit_gate_counts={"tests/client_main.test.ts": {}},
        )

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 1
        assert state.test_writer_audit_pending is False
        assert state.test_writer_audit_files_written == []
        assert state.test_writer_audit_gate_counts == {}
        assert orch._store.load_text_snapshot("t1", orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None
        assert len(state.test_execution_gate_records) == 1
        assert state.test_execution_gate_records[0]["findings"][0]["path"] == "tests/client_main.test.ts"
        assert len(state.synthetic_test_harness_records) == 1
        assert state.synthetic_test_harness_records[0]["findings"][0]["path"] == "tests/client_main.test.ts"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"

    def test_test_writer_synthetic_harness_recovery_retries_with_narrow_test(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeEventTarget { addEventListener() {}; dispatchEvent() {} }",
                            "class FakeElement extends FakeEventTarget { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert saved.tests_up_to_date is False
                assert saved.test_writer_audit_agent_completed is False
                assert "tests/client_main.test.ts" not in saved.files_changed
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert state.failed is False
        assert state.test_errors == []
        assert len(state.synthetic_test_harness_records) == 1
        assert state.synthetic_test_harness_records[0]["source"] == "test_writer"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"
        assert any(record["action"] == "synthetic_test_harness_audit" for record in state.history)
        assert any(record["action"] == "synthetic_test_harness_recovered" for record in state.history)

    def test_test_writer_repeated_synthetic_harness_is_removed_and_gap_recorded(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text(
                "\n".join(
                    [
                        "class FakeElement { appendChild() {}; querySelector() {} }",
                        "class FakeHistory { pushState() {} }",
                        "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                    ]
                ),
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert not changed
        assert state.failed is False
        assert not test_file.exists()
        assert len(state.synthetic_test_harness_records) == 1
        assert all(record["status"] == "resolved" for record in state.synthetic_test_harness_records)
        assert len(state.testability_gaps) == 1
        assert state.testability_gaps[0]["target"] == "synthetic runtime harness in tests/client_main.test.ts"
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written

    def test_test_writer_synthetic_harness_recovery_preserves_other_written_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        synthetic_file = tmp_path / "tests" / "client_main.test.ts"
        narrow_file = tmp_path / "tests" / "view_model.test.ts"
        synthetic_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                synthetic_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                narrow_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
                state.files_changed.extend(["tests/client_main.test.ts", "tests/view_model.test.ts"])
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert not synthetic_file.exists()
        assert narrow_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/view_model.test.ts" in state.files_changed

    def test_test_writer_synthetic_harness_recovery_drops_clean_retry_reported_file(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                assert not test_file.exists()
                state.record_testability_gap(
                    "test_writer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert not changed
        assert attempts["count"] == 2
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written
        assert len(state.testability_gaps) == 1

    def test_test_writer_synthetic_harness_audit_uses_pre_agent_dirty_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test('existing narrow test', () => {});\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add narrow test"], cwd=repo, check=True, capture_output=True)
        dirty_harness = "\n".join(
            [
                "class FakeElement { appendChild() {}; querySelector() {} }",
                "class FakeHistory { pushState() {} }",
                "class FakeMouseEvent { preventDefault() {} }",
            ]
        )
        test_file.write_text(dirty_harness, encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        assert orch._run_test_write_phase(state)

        assert state.synthetic_test_harness_records == []
        assert "test('uses helper'" in test_file.read_text(encoding="utf-8")

    def test_synthetic_harness_audit_catches_cumulative_test_harness(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text(
            "\n".join(
                [
                    "class FakeElement { appendChild() {}; querySelector() {} }",
                    "class FakeHistory { pushState() {} }",
                ]
            ),
            encoding="utf-8",
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])
        attempts = {"count": 0}

        def fixer_effect(_state: TaskState) -> None:
            attempts["count"] += 1
            test_file.write_text(
                "\n".join(
                    [
                        "class FakeElement { appendChild() {}; querySelector() {} }",
                        "class FakeHistory { pushState() {} }",
                        "class FakeMouseEvent { preventDefault() {} }",
                    ]
                ),
                encoding="utf-8",
            )

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.failed is False
        assert state.test_errors == []
        assert attempts["count"] == 2
        assert len(state.synthetic_test_harness_records) == 1
        finding = state.synthetic_test_harness_records[0]["findings"][0]
        assert set(finding["subsystems"]) == {"event_dispatch", "navigation_history", "render_tree"}
        assert all(record["status"] == "resolved" for record in state.synthetic_test_harness_records)
        assert "FakeMouseEvent" not in test_file.read_text(encoding="utf-8")
        assert len(state.testability_gaps) == 1

        stubs["fixer"].side_effect = lambda _state: test_file.write_text(
            test_file.read_text(encoding="utf-8") + "\ntest('assertion tweak', () => {});\n",
            encoding="utf-8",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert len(state.synthetic_test_harness_records) == 1

    def test_fixer_synthetic_harness_recovery_restores_validation_errors_for_retry(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.test_errors.clear()
                state.test_status = "success"
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert saved.test_errors == ["tests failed"]
                assert saved.test_status == "failed"
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                assert state.test_errors == ["tests failed"]
                assert state.test_status == "failed"
                test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.files_changed.append("tests/client_main.test.ts")

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"

    def test_fixer_synthetic_harness_recovery_aborts_failed_retry(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.append("tests/client_main.test.ts")
            else:
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "provider failure"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert not orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is True
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "fixer failed: provider failure"
            for entry in state.history
        )

    def test_fixer_synthetic_harness_recovery_preserves_other_changed_files(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        src_file = tmp_path / "src" / "main.py"
        test_file = tmp_path / "tests" / "client_main.test.ts"
        src_file.parent.mkdir()
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                src_file.write_text("def value():\n    return 1\n", encoding="utf-8")
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.test_errors.clear()
                state.files_changed.extend(["src/main.py", "tests/client_main.test.ts"])

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert src_file.exists()
        assert not test_file.exists()
        assert "src/main.py" in state.files_changed
        assert "tests/client_main.test.ts" not in state.files_changed
        assert state.review_approved is False
        assert state.security_approved is False
        assert state.tests_up_to_date is False

    def test_fixer_synthetic_harness_recovery_accepts_noop_retry_with_retained_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        src_file = tmp_path / "src" / "main.py"
        test_file = tmp_path / "tests" / "client_main.test.ts"
        src_file.parent.mkdir()
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                stubs["fixer"].result_success = True
                stubs["fixer"].result_message = None
                src_file.write_text("def value():\n    return 1\n", encoding="utf-8")
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.extend(["src/main.py", "tests/client_main.test.ts"])
            else:
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "Agent made no file changes"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is False
        assert src_file.exists()
        assert not test_file.exists()
        assert "src/main.py" in state.files_changed
        assert "tests/client_main.test.ts" not in state.files_changed
        assert state.review_approved is False
        assert state.security_approved is False
        assert state.tests_up_to_date is False
        assert any(record["action"] == "synthetic_test_harness_recovery_noop_retry" for record in state.history)
        assert not any(
            record["action"] == "abort" and "Agent made no file changes" in record["result"] for record in state.history
        )

    def test_fixer_synthetic_harness_recovery_accepts_noop_retry_with_restored_tree_only(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                stubs["fixer"].result_success = True
                stubs["fixer"].result_message = None
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.append("tests/client_main.test.ts")
            else:
                state.record_testability_gap(
                    "fixer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "Agent made no file changes"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is False
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert any(record["action"] == "synthetic_test_harness_recovery_noop_retry" for record in state.history)
        assert not any(
            record["action"] == "abort" and "Agent made no file changes" in record["result"] for record in state.history
        )

    def test_fixer_synthetic_harness_recovery_drops_clean_retry_reported_file(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                assert not test_file.exists()
                state.record_testability_gap(
                    "fixer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_files_written=["tests/client_main.test.ts"],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written
        assert len(state.testability_gaps) == 1

    def test_synthetic_harness_audit_uses_git_head_project_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "\n".join(
                [
                    "class FakeElement { appendChild() {}; querySelector() {} }",
                    "class FakeHistory { pushState() {} }",
                    "class FakeMouseEvent { preventDefault() {} }",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test harness"], cwd=repo, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records == []

    def test_synthetic_harness_audit_uses_pre_agent_dirty_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test('existing narrow test', () => {});\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add narrow test"], cwd=repo, check=True, capture_output=True)
        dirty_harness = "\n".join(
            [
                "class FakeElement { appendChild() {}; querySelector() {} }",
                "class FakeHistory { pushState() {} }",
                "class FakeMouseEvent { preventDefault() {} }",
            ]
        )
        test_file.write_text(dirty_harness, encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records == []
        assert "test('uses helper'" in test_file.read_text(encoding="utf-8")

    def test_synthetic_harness_audit_resolves_when_test_is_narrowed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text("class FakeHistory {}\n", encoding="utf-8")
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        state.record_synthetic_test_harness_audit(
            "test_writer",
            [
                {
                    "path": "tests/client_main.test.ts",
                    "subsystems": ["navigation_history", "event_dispatch", "network_server"],
                    "evidence": [
                        {
                            "category": "navigation_history",
                            "lines": [{"line": 1, "excerpt": "class FakeHistory {}"}],
                        }
                    ],
                }
            ],
        )

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text("def test_view_model_contract():\n    assert True\n", encoding="utf-8")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert "resolved_at" in state.synthetic_test_harness_records[0]

    def test_final_scope_test_only_fix_resets_final_gate_and_security(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append("tests/test_main.py")

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            active_scope="final_full_task",
            final_full_task_review_done=True,
            review_approved=True,
            security_approved=True,
            security_review_iterations=2,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/3")

        assert state.review_approved
        assert state.tests_up_to_date
        assert not state.security_approved
        assert state.security_review_iterations == 0
        assert not state.final_full_task_review_done
        assert any(record["action"] == "test_only_fix" for record in state.history)

    def test_fix_phase_fails_task_when_fixer_result_is_unsuccessful(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=True)
        stubs["fixer"].result_success = False
        stubs["fixer"].result_message = "Agent made no file changes"
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            errors=["compile failed"],
        )

        assert not orch._run_fix_phase(state, "1/3")

        assert state.failed
        assert any(
            record["agent"] == "orchestrator"
            and record["action"] == "abort"
            and "Agent made no file changes" in record["result"]
            for record in state.history
        )

    def test_test_only_fix_with_broad_android_root_requires_test_artifact_path(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        def fixer_effect(state: TaskState) -> None:
            path = "feature/countries/src/test/kotlin/com/example/countries/CountriesModuleTest.kt"
            if path not in state.files_changed:
                state.files_changed.append(path)

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/countries/src/main/kotlin/com/example/countries/CountriesRoutes.kt"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 0
        assert any(record["action"] == "test_only_fix" for record in result.history)

    def test_test_writer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
        )
        stubs["test_writer"].result_success = False
        stubs["test_writer"].result_message = "quota exhausted"
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            security_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert not result.done
        assert len(stubs["test_writer"].calls) == 1
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "test_writer failed: quota exhausted"
            for entry in result.history
        )

    def test_production_fix_under_broad_test_root_stales_semantic_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            path = "feature/countries/src/main/kotlin/com/example/countries/CountriesRoutes.kt"
            if path not in state.files_changed:
                state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/countries/src/main/kotlin/com/example/countries/CountriesScreen.kt"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)

    def test_production_fix_under_directory_ending_test_does_not_count_as_test_only(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            path = "feature/latest/src/main/App.ts"
            if path not in state.files_changed:
                state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/latest/src/main/Existing.ts"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)

    def test_mixed_test_and_production_fix_stales_semantic_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            for path in ["src/fix.py", "tests/test_main.py"]:
                if path not in state.files_changed:
                    state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)


# ---------------------------------------------------------------------------
# Tests — interrupt / resume scenarios
# ---------------------------------------------------------------------------


class TestOrchestratorInterruptResume:
    def test_resume_after_interrupt_between_fixer_and_reviewer_runs_review(self, tmp_path: Path):
        """Interrupt after fixer ran and flags were reset+saved, reviewer had not yet run.
        On resume, review_approved=False must cause reviewer to run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, max_review_iterations=2)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=False)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_resume_after_interrupt_between_security_fix_and_review_runs_review(self, tmp_path: Path):
        """Interrupt after security-fix implementer ran and review_approved was reset+saved,
        but review loop had not yet run. On resume, reviewer must run."""
        orch, stubs, _ = _make_orchestrator(
            tmp_path, run_build=False, run_review=True, run_security_review=True, max_review_iterations=2
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # Simulates state saved after security-fix implementer reset review_approved
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
            security_approved=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_resume_with_tests_up_to_date_false_reruns_test_writer(self, tmp_path: Path):
        """Interrupt after implementer fix reset tests_up_to_date but before test writer ran.
        On resume, test writer must run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, run_test_writing=True)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
            tests_up_to_date=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["test_writer"].calls) >= 1

    def test_resume_pending_test_writer_audit_before_agent_completion_reruns_test_writer(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        partial_test = tmp_path / "tests" / "test_main.py"
        partial_test.parent.mkdir()
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {},
        )
        partial_test.write_text('test.skip("partial generated placeholder", () => {});\n', encoding="utf-8")

        def test_writer_effect(state: TaskState) -> None:
            assert not partial_test.exists()
            partial_test.write_text("def test_generated_behavior():\n    assert True\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/test_main.py"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
            test_writer_audit_gate_counts={"tests/test_main.py": {}},
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["test_writer"].calls) == 1
        assert result.tests_up_to_date is True
        assert result.test_writer_audit_pending is False
        assert result.test_writer_audit_agent_completed is False
        assert not result.test_execution_gate_records
        assert partial_test.read_text(encoding="utf-8") == "def test_generated_behavior():\n    assert True\n"
        assert orch._store.load_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None

    def test_resume_pending_test_writer_audit_restores_unaudited_state_paths_before_rerun(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        unaudited_test = tmp_path / "tests" / "test_main.py"
        unaudited_test.parent.mkdir()
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {},
        )
        unaudited_test.write_text('test.skip("partial generated placeholder", () => {});\n', encoding="utf-8")

        def test_writer_effect(state: TaskState) -> None:
            assert not unaudited_test.exists()
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": []}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            test_files_written=["tests/test_main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
            test_writer_audit_gate_counts={"tests/test_main.py": {}},
        )

        changed = orch._run_test_write_phase(state)

        assert changed is False
        assert len(stubs["test_writer"].calls) == 1
        assert not unaudited_test.exists()
        assert state.tests_up_to_date is True
        assert state.test_writer_audit_pending is False
        assert "tests/test_main.py" not in state.files_changed
        assert "tests/test_main.py" not in state.test_files_written
        assert state.test_execution_gate_records == []
        assert any(record["action"] == "test_writer_interrupted_output_restored" for record in state.history)
        assert orch._store.load_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None

    def test_resume_pending_test_writer_audit_before_agent_completion_fails_without_snapshot(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["test_writer"].calls) == 0
        assert result.history[-1]["action"] == "abort"
        assert "snapshot missing" in result.history[-1]["result"]

    def test_restore_test_writer_snapshot_rejects_symlink_parent(self, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        linked_tests = project / "tests"
        try:
            linked_tests.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation failed: {exc}")
        orch, _, _ = _make_orchestrator(project)
        state = _save_state(orch, implementation_prompt="p")

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": "def test_generated():\n    assert True\n"},
        )

        assert restored == []
        assert errors
        assert "symlink component" in errors[0]
        assert not (outside / "test_main.py").exists()

    def test_restore_test_writer_snapshot_rejects_symlink_target(self, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "test_main.py"
        outside_file.write_text("outside\n", encoding="utf-8")
        tests_dir = project / "tests"
        tests_dir.mkdir()
        linked_test = tests_dir / "test_main.py"
        try:
            linked_test.symlink_to(outside_file)
        except OSError as exc:
            pytest.skip(f"symlink creation failed: {exc}")
        orch, _, _ = _make_orchestrator(project)
        state = _save_state(orch, implementation_prompt="p")

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": "def test_generated():\n    assert True\n"},
        )

        assert restored == []
        assert errors
        assert "symlink component" in errors[0]
        assert outside_file.read_text(encoding="utf-8") == "outside\n"

    def test_review_fix_restore_preserves_branch_diff_test_file(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        branch_text = "def test_existing_branch_change():\n    assert True\n"
        test_file.write_text("class FakeElement:\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(orch, "_path_has_pending_changes", lambda _path: False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            test_files_written=["tests/test_main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
        )

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": branch_text},
        )

        assert restored == ["tests/test_main.py"]
        assert errors == []
        assert test_file.read_text(encoding="utf-8") == branch_text
        assert "tests/test_main.py" in state.files_changed
        assert "tests/test_main.py" not in state.test_files_written

    def test_review_fix_restore_prunes_new_generated_test_file(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        test_file = tmp_path / "tests" / "generated_test.py"
        test_file.parent.mkdir()
        test_file.write_text("class FakeElement:\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(orch, "_path_has_pending_changes", lambda _path: False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/generated_test.py"],
            test_files_written=["tests/generated_test.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
        )

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/generated_test.py"],
            before_snapshot={"tests/generated_test.py": None},
        )

        assert restored == ["tests/generated_test.py"]
        assert errors == []
        assert not test_file.exists()
        assert "tests/generated_test.py" not in state.files_changed
        assert "tests/generated_test.py" not in state.test_files_written

    def test_step_loop_runs_implementer_per_step(self, tmp_path: Path):
        """Step loop: implementer must run once per step."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{len(stubs['implementer'].calls)}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2

    def test_step_loop_implementer_failure_aborts_before_review(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "usage limit reached"
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert saved is not None
        assert saved.failed is True
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: usage limit reached"
            for entry in saved.history
        )

    def test_step_loop_max_review_iterations_persists_failed_state(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            max_review_iterations=1,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = False
            state.review_issues = ["future step is missing"]

        def implementer_effect(state: TaskState) -> None:
            if not state.files_changed:
                state.files_changed.append("src/step0.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert saved.failed is True
        assert saved.done is False
        assert any(h["action"] == "abort" for h in saved.history)

    def test_step_loop_resume_at_review_limit_allows_approval_before_abort(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            max_review_iterations=3,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=1,
            step_implemented=True,
            files_changed=["src/step0.py"],
            review_approved=False,
            review_iterations=3,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.done is True
        assert saved.done is True
        assert saved.failed is False
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 0

    def test_step_loop_resume_reruns_current_step(self, tmp_path: Path):
        """Interrupt during step 1, resume: step 1 implementer must run again."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        # Simulate interrupt during step 0: step_implemented=False (implementer not yet done)
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=0,
            step_implemented=False,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2

    def test_step_loop_no_changes_advances_to_next_step(self, tmp_path: Path):
        """Step with no file changes must not abort — treat as already implemented and advance."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        call_count = [0]

        def implementer_effect(state: TaskState) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                pass  # step 0: no changes — already implemented
            else:
                state.files_changed.append("src/step1.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: already done", "Step 2: add feature B"],
            plan_decided=True,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2
        history_actions = [h["action"] for h in result.history]
        assert "step_skipped" in history_actions

    def test_step_loop_skips_completed_step(self, tmp_path: Path):
        """Interrupt after step 0 completed: on resume, step 0 implementer must not run again."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        # Simulate interrupt after step 0 completed: current_step=1, step_implemented=False
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=1,
            step_implemented=False,
            files_changed=["src/step0.py"],
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 1  # only step 1, not step 0 again

    def test_multi_step_run_gets_final_full_task_review_before_final_build(self, tmp_path: Path, caplog):
        import logging

        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=False,
            run_security_review=False,
            run_test_writing=False,
        )

        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        with caplog.at_level(logging.INFO, logger="core.orchestrator"):
            result = orch.run(task_id="t1")

        assert result.done
        assert result.plan_completed is True
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "final_full_task"]
        assert build.compile_calls == 1
        messages = [record.message for record in caplog.records]
        assert "--- Phase: final full-task gate ---" in messages
        assert "--- Phase: final full-task review (initial) ---" in messages

    def test_deferred_final_build_fixer_reruns_review_in_final_scope(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        build.compile_results = [False, True]
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append("src/final_build_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "final_full_task", "final_full_task"]
        assert build.compile_calls == 2

    def test_build_per_step_fixer_review_stays_step_scoped_before_final_gate(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=6,
        )
        build.compile_results = [False, True, True, True]
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}_build_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "step", "final_full_task"]
        assert build.compile_calls == 4
        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert build_records[-1]["scope"] == "final_full_task"

    def test_build_per_step_budget_does_not_starve_final_build(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=2,
        )

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not result.failed
        assert result.build_iterations == 3
        assert build.compile_calls == 3
        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert [r.get("scope") for r in build_records] == [None, None, "final_full_task"]

    def test_final_gate_completes_when_optional_agents_are_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.plan_completed is True
        assert result.final_full_task_review_done is True
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 0
        assert len(stubs["test_writer"].calls) == 0

    def test_plan_completed_resume_skips_last_step_and_runs_final_gate(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_security_review=False,
            run_test_writing=False,
        )
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
            plan_completed=True,
            current_step=1,
            step_implemented=True,
            files_changed=["src/step0.py", "src/step1.py"],
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["implementer"].calls) == 0
        assert review_scopes == ["final_full_task"]
        assert result.final_full_task_review_done is True


# ---------------------------------------------------------------------------
# Tests — preexisting-changes fast-path (review --fix with no fixer needed)
# ---------------------------------------------------------------------------


_REVIEW_DIFF = "@@ -1 +1 @@\n+x"  # minimal non-empty diff marking a task as a review task


def _git_diff_refresh_subprocess(cmd, **kwargs):
    if cmd[0:2] == ["git", "merge-base"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="base123\n", stderr="")
    if cmd[0:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="@@ fresh diff\n+current\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestOrchestratorPreexistingChangesFastPath:
    def test_review_fix_without_plan_stays_task_scoped(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=False,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or ("step" if state.plan else "task"))
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert review_scopes == ["task"]
        assert result.plan_completed is False
        assert result.final_full_task_review_done is False

    def test_review_fix_fast_path_runs_build_loop_for_active_execution_gate(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("changed behavior", () => {});\n', encoding="utf-8")

        def fixer_effect(state: TaskState) -> None:
            test_file.unlink()
            state.files_changed.append("tests/test_main.py")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/test_main.py"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
            tests_up_to_date=True,
        )
        state.record_test_execution_gate_audit(
            "test_writer",
            detect_new_test_execution_gates(
                path="tests/test_main.py",
                before=None,
                after='test.skip("changed behavior", () => {});\n',
            ),
        )
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["fixer"].calls) == 1
        assert any(entry["action"] == "review_fast_path_blocked" for entry in result.history)
        assert result.test_execution_gate_records[0]["status"] == "resolved"
        assert not test_file.exists()

    def test_review_fix_skips_validation_coverage_preflight_abort(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=False,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.rs"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
        )
        state.task_description = "Review branch.\n\n## Verification\n\ncargo test --workspace --all-features\n"
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.done
        assert stubs["reviewer"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_review_fix_refreshes_review_diff_before_review(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=False,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        def reviewer_effect(state: TaskState) -> None:
            assert state.review_diff == "@@ fresh diff\n+current\n"
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff="@@ stale diff\n+old",
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_diff == "@@ fresh diff\n+current\n"

    def test_review_fix_reviews_test_writer_changes(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=False,
            max_review_iterations=3,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        review_calls = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs branch fix"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append("src/fix.py")

        def test_writer_effect(state: TaskState) -> None:
            if "tests/test_fix.py" not in state.files_changed:
                state.files_changed.append("tests/test_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["test_writer"].side_effect = test_writer_effect

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert review_calls["n"] == 3
        assert "tests/test_fix.py" in result.files_changed

    def test_review_fix_rejects_test_writer_changes_without_rerunning_test_writer(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=False,
            max_review_iterations=3,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        review_calls = {"n": 0}
        test_writer_calls = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs branch fix"]
                return
            if review_calls["n"] == 3:
                state.review_approved = False
                state.review_issues = ["test writer changes are not acceptable"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/fix_{review_calls['n']}.py")

        def test_writer_effect(state: TaskState) -> None:
            test_writer_calls["n"] += 1
            state.files_changed.append(f"tests/test_fix_{test_writer_calls['n']}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["test_writer"].side_effect = test_writer_effect

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.failed
        assert not result.done
        assert review_calls["n"] == 3
        assert test_writer_calls["n"] == 1
        assert "tests/test_fix_1.py" in result.files_changed
        assert "tests/test_fix_2.py" not in result.files_changed

    def test_build_and_test_writer_skipped_when_reviewer_approves_without_fixes(self, tmp_path: Path):
        """review --fix fast-path: files pre-exist, reviewer approves, nothing written → skip build."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_test_writing=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_diff=_REVIEW_DIFF)
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["test_writer"].calls) == 0
        assert build.compile_calls == 0
        assert build.test_calls == 0
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_build_runs_when_review_issues_required_implementer_fix(self, tmp_path: Path):
        """When reviewer finds issues and implementer runs to fix them, build must still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
            max_review_iterations=3,
        )

        review_call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_call_count["n"] += 1
            if review_call_count["n"] >= 2:
                state.review_approved = True
                state.review_issues.clear()
            else:
                state.review_issues = ["needs fix"]

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": ["src/main.py"]}
        _save_state(
            orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True, review_diff=_REVIEW_DIFF
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1

    def test_fast_path_not_triggered_when_review_was_already_approved_before_session(self, tmp_path: Path):
        """If review_approved=True at session start (previous session), reviewer is skipped →
        fast-path must NOT trigger — build should still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            review_diff=_REVIEW_DIFF,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 0  # reviewer skipped (already approved)
        assert build.compile_calls >= 1

    def test_fast_path_not_triggered_when_fixer_changed_code(self, tmp_path: Path):
        """review --fix resume edge case: fixer changed code in a previous session
        (fixer_changed_code=True), reviewer approves this session — build must still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_diff=_REVIEW_DIFF,
            fixer_changed_code=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1

    def test_review_fix_resume_active_build_loop_validates_before_re_review(self, tmp_path: Path, monkeypatch):
        """review --fix resume with an active build loop must validate before refreshing review."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=True,
            run_tests=True,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            assert state.review_diff == "@@ fresh diff\n+current\n"
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=1,
            review_diff="@@ stale diff\n+old",
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_fast_path_not_triggered_in_run_mode_resume(self, tmp_path: Path):
        """Run-mode resume: implementer ran in previous session (files pre-exist), reviewer
        approves in this session — build must still run (code not yet CI-validated)."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # No review_diff — this is a run-mode task, not a review task
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1


# ---------------------------------------------------------------------------
# Tests — build tool factory
# ---------------------------------------------------------------------------


class TestBuildToolFactory:
    def test_python_platform_creates_python_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "python"}, "build": {}})
        assert isinstance(tool, PythonTool)

    def test_default_platform_creates_gradle_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {}, "build": {}})
        assert isinstance(tool, AndroidGradleTool)

    def test_python_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "python"},
            "build": {"compile_command": "mypy .", "test_command": "pytest -v", "timeout": 120},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, PythonTool)
        assert tool._compile_command == "mypy ."
        assert tool._test_command == "pytest -v"
        assert tool._timeout == 120

    def test_cargo_platform_creates_cargo_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "cargo"}, "build": {}})
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo fetch --locked"

    def test_cargo_platform_without_lockfile_uses_unlocked_fetch(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "cargo"}, "build": {}})
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo fetch"

    def test_cargo_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "cargo"},
            "build": {
                "sync_command": "cargo generate-lockfile",
                "compile_command": "cargo check --workspace",
                "test_command": "cargo test --workspace",
                "timeout": 300,
            },
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo generate-lockfile"
        assert tool._compile_command == "cargo check --workspace"
        assert tool._test_command == "cargo test --workspace"
        assert tool._timeout == 300

    def test_node_platform_creates_node_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "node"}, "build": {}})
        assert isinstance(tool, NodeTool)

    def test_node_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "node"},
            "build": {
                "package_manager": "pnpm",
                "sync_command": "pnpm install --offline",
                "compile_command": "pnpm typecheck",
                "test_command": "pnpm test",
                "timeout": 240,
            },
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, NodeTool)
        assert tool._package_manager == "pnpm"
        assert tool._sync_command == "pnpm install --offline"
        assert tool._compile_command == "pnpm typecheck"
        assert tool._test_command == "pnpm test"
        assert tool._compile_timeout == 240

    def test_gradle_jvm_creates_jvm_tool(self, tmp_path: Path):
        from tools.gradle_jvm_tool import JvmGradleTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "gradle-jvm"}, "build": {}})
        assert isinstance(tool, JvmGradleTool)

    def test_gradle_jvm_uses_configured_tasks(self, tmp_path: Path):
        from tools.gradle_jvm_tool import JvmGradleTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "gradle-jvm"},
            "build": {"compile_task": "compileKotlin", "test_task": "test", "compile_timeout": 300},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, JvmGradleTool)
        assert tool._compile_task == "compileKotlin"
        assert tool._compile_timeout == 300

    def test_maven_creates_maven_tool(self, tmp_path: Path):
        from tools.maven_tool import MavenTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "maven"}, "build": {}})
        assert isinstance(tool, MavenTool)

    def test_maven_uses_configured_commands(self, tmp_path: Path):
        from tools.maven_tool import MavenTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "maven"},
            "build": {"compile_command": "mvn compile -q", "test_command": "mvn test -q"},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, MavenTool)
        assert tool._compile_command == "mvn compile -q"
        assert tool._test_command == "mvn test -q"


# ---------------------------------------------------------------------------
# Tests — fix_command in check config
# ---------------------------------------------------------------------------

_CHECK_WITH_FIX = [
    {"name": "ruff-format", "command": "ruff format --check .", "fix_command": "ruff format .", "timeout": 60}
]
_CHECK_WITH_FIX_GRADLE = [
    {
        "name": "ktlint-format",
        "command": "./gradlew ktlintCheck",
        "fix_command": "./gradlew ktlintFormat",
        "timeout": 600,
    }
]
_CHECK_WITHOUT_FIX = [{"name": "ruff-check", "command": "ruff check .", "timeout": 60}]
_CHECK_WITH_FIX_NO_TIMEOUT = [
    {"name": "ruff-format", "command": "ruff format --check .", "fix_command": "ruff format ."}
]


def _make_orch_with_checks(
    tmp_path: Path, checks: list, **config_kwargs
) -> tuple[Orchestrator, dict[str, StubAgent], StubBuildTool]:
    project_config = {"project": {"build_tool": "python"}, "build": {"checks": checks}}
    config = OrchestratorConfig(
        project_root=tmp_path,
        allowed_write_paths=["."],
        allowed_read_paths=["."],
        project_config=project_config,
        run_build=True,
        run_tests=False,
        run_checks=True,
        **config_kwargs,
    )
    store = JsonStateStore(tmp_path / "state")
    orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=store)
    stubs = {
        name: StubAgent(name=name)
        for name in ["analyst", "planner", "implementer", "reviewer", "security_reviewer", "test_writer", "fixer"]
    }

    def _default_reviewer(state: TaskState) -> None:
        state.review_approved = True
        state.review_issues = []

    def _default_security_reviewer(state: TaskState) -> None:
        state.security_approved = True
        state.review_issues = []

    stubs["reviewer"].side_effect = _default_reviewer
    stubs["security_reviewer"].side_effect = _default_security_reviewer

    orch._agents = stubs  # type: ignore[assignment]
    build = StubBuildTool()
    orch._tools["build"] = build
    return orch, stubs, build


class TestOrchestratorFixCommand:
    def _checks_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

    def test_autofix_called_when_check_fails(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert "ruff-format_autofix" in build.check_calls

    def test_check_rerun_after_autofix_success(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert build.check_calls.count("ruff-format") == 2

    def test_task_done_without_fixer_when_autofix_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["fixer"].calls) == 0

    def test_validation_records_include_autofix_attempt(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        check_records = [(r["phase"], r["status"], r.get("check_name")) for r in result.validation_cycle_records]
        assert ("check", "failed", "ruff-format") in check_records
        assert ("check_autofix", "success", "ruff-format") in check_records
        assert ("check", "success", "ruff-format") in check_records

    def test_autofix_cleans_new_untracked_artifacts_before_check_rerun(self, tmp_project: Path):
        orch, _, build = _make_orch_with_checks(tmp_project, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}

        def write_autofix_artifact() -> None:
            artifact = tmp_project / "reports" / "ruff-format.cache"
            artifact.parent.mkdir()
            artifact.write_text("generated by autofix\n")

        build.check_side_effects["ruff-format_autofix"] = write_autofix_artifact
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "reports" / "ruff-format.cache").exists()
        assert result.validation_artifact_records[0]["phase"] == "check_autofix"
        assert result.validation_artifact_records[0]["status"] == "cleaned"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "reports/ruff-format.cache",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_autofix_preserves_existing_source_changes(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        orch, _, build = _make_orch_with_checks(tmp_project, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}

        def format_source() -> None:
            source.write_text("# formatted by autofix\n")

        build.check_side_effects["ruff-format_autofix"] = format_source
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# formatted by autofix\n"
        assert result.validation_artifact_records == []

    def test_autofix_failure_falls_through_to_fixer(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX, max_iterations=2)
        build.check_results = {"ruff-format": [False], "ruff-format_autofix": [False]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["fixer"].calls) >= 1

    def test_validation_records_do_not_duplicate_check_failure_when_autofix_fails(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX, max_iterations=1)
        build.check_results = {"ruff-format": [False], "ruff-format_autofix": [False]}
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        check_failures = [
            r
            for r in result.validation_cycle_records
            if r["phase"] == "check" and r["status"] == "failed" and r.get("check_name") == "ruff-format"
        ]
        autofix_failures = [
            r
            for r in result.validation_cycle_records
            if r["phase"] == "check_autofix" and r["status"] == "failed" and r.get("check_name") == "ruff-format"
        ]
        assert len(check_failures) == 1
        assert len(autofix_failures) == 1

    def test_no_fix_command_calls_fixer_on_failure(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITHOUT_FIX, max_iterations=2)
        build.check_success = False
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["fixer"].calls) >= 1
        assert "ruff-check_autofix" not in build.check_calls

    def test_fix_command_timeout_not_forwarded_when_absent(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX_NO_TIMEOUT)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        autofix_cfg = next(cfg for name, cfg in build.check_configs if name == "ruff-format_autofix")
        assert "timeout" not in autofix_cfg

    def test_fix_command_works_for_gradle_check(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX_GRADLE)
        build.check_results = {"ktlint-format": [False, True], "ktlint-format_autofix": [True]}
        self._checks_ready(orch)
        result = orch.run(task_id="t1")
        assert "ktlint-format_autofix" in build.check_calls
        assert result.done
        assert len(stubs["fixer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — OrchestratorConfig defaults (__post_init__ None guards)
# ---------------------------------------------------------------------------


class TestOrchestratorConfigDefaults:
    def test_none_write_paths_defaults_to_empty_list(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, allowed_write_paths=None)
        assert cfg.allowed_write_paths == []

    def test_none_read_paths_defaults_to_dot(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, allowed_read_paths=None)
        assert cfg.allowed_read_paths == ["."]

    def test_none_project_config_defaults_to_empty_dict(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, project_config=None)
        assert cfg.project_config == {}


# ---------------------------------------------------------------------------
# Tests — run() error paths
# ---------------------------------------------------------------------------


class TestOrchestratorRunErrors:
    def test_task_not_found_raises_value_error(self, tmp_path: Path):
        import pytest

        orch, _, _ = _make_orchestrator(tmp_path)
        with pytest.raises(ValueError, match="Task not found"):
            orch.run(task_id="nonexistent")

    def test_no_args_raises_value_error(self, tmp_path: Path):
        import pytest

        orch, _, _ = _make_orchestrator(tmp_path)
        with pytest.raises(ValueError):
            orch.run()


# ---------------------------------------------------------------------------
# Tests — presync phase
# ---------------------------------------------------------------------------


class TestOrchestratorPresync:
    def test_presync_success_sets_presync_done(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        _save_state(orch)
        orch.run(task_id="t1")
        assert orch._store.load("t1").presync_done is True

    def test_presync_failure_is_non_fatal_analyst_still_runs(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        build.presync_success = False
        _save_state(orch)
        orch.run(task_id="t1")
        assert len(stubs["analyst"].calls) >= 1

    def test_presync_failure_still_sets_presync_done(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        build.presync_success = False
        _save_state(orch)
        orch.run(task_id="t1")
        assert orch._store.load("t1").presync_done is True

    def test_presync_skipped_on_resume(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        _save_state(orch, presync_done=True)
        orch.run(task_id="t1")
        assert build.presync_calls == 0


# ---------------------------------------------------------------------------
# Tests — planner failure abort
# ---------------------------------------------------------------------------


class TestOrchestratorPlannerAbort:
    def test_delivery_child_stops_before_implementation_when_planner_budget_is_exceeded(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)

        def planner_effect(state: TaskState) -> None:
            state.plan = ["Step 1", "Step 2", "Step 3"]
            state.plan_decided = True
            state.record("planner", "plan", "3 steps")

        stubs["planner"].side_effect = planner_effect
        _save_state(
            orch,
            implementation_prompt="p",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 1},
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert result.plan == ["Step 1", "Step 2", "Step 3"]
        assert result.delivery_budget_stop is not None
        assert result.delivery_budget_stop["code"] == "unit_budget_exceeded"
        assert result.delivery_budget_stop["name"] == "max_planner_steps"
        assert result.delivery_budget_stop["limit"] == 1
        assert result.delivery_budget_stop["actual"] == 3
        assert result.delivery_budget_stop["phase"] == "planner"
        assert len(stubs["planner"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert saved is not None
        assert saved.delivery_budget_stop == result.delivery_budget_stop
        assert any(entry["action"] == "delivery_budget_exceeded" for entry in saved.history)

    def test_delivery_child_allows_explicit_two_step_budget(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)

        def planner_effect(state: TaskState) -> None:
            state.plan = ["Step 1", "Step 2"]
            state.plan_decided = True

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{len(stubs['implementer'].calls)}.py")

        stubs["planner"].side_effect = planner_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 2},
        )

        result = orch.run(task_id="t1")

        assert result.done is True
        assert result.delivery_budget_stop is None
        assert len(stubs["planner"].calls) == 1
        assert len(stubs["implementer"].calls) == 2

    def test_planner_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        stubs["planner"].side_effect = lambda s: setattr(s, "failed", True)
        _save_state(orch, implementation_prompt="p")
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 0

    def test_planner_result_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        class FailingPlanner:
            def __init__(self) -> None:
                self.calls: list[TaskState] = []

            def run(self, state: TaskState) -> AgentResult:
                self.calls.append(state)
                state.record("planner", "plan_failed", "temporary provider failure")
                return AgentResult(success=False, message="temporary provider failure")

        failing_planner = FailingPlanner()
        orch._agents["planner"] = failing_planner  # type: ignore[assignment]
        _save_state(orch, implementation_prompt="p")

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.plan_decided is False
        assert result.plan == []
        assert len(failing_planner.calls) == 1
        assert len(stubs["implementer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — _run_agent exception handling
# ---------------------------------------------------------------------------


class TestOrchestratorAgentException:
    def test_agent_exception_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        stubs["analyst"].raise_exception = RuntimeError("unexpected crash")
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert result.failed

    def test_agent_exception_recorded_in_history(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        stubs["analyst"].raise_exception = RuntimeError("crash")
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert any(h.get("action") == "error" for h in result.history)


# ---------------------------------------------------------------------------
# Tests — _run_tests and _sync failure paths
# ---------------------------------------------------------------------------


class TestOrchestratorTestsAndSyncFailure:
    def test_test_failure_appends_to_test_errors(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, max_iterations=1)
        build.test_success = False
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        result = orch.run(task_id="t1")
        assert result.test_errors
        assert result.test_status == "failed"

    def test_test_failure_preserves_middle_failure_diagnostics(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, max_iterations=1)
        failure_output = (
            "Compiling workspace\n"
            + "".join(f"build line {i}\n" for i in range(160))
            + "thread 'test_rejects_wrong_result_type' panicked at assertion failed\n"
            + "left: Ok(ParsedConfig)\n"
            + "right: Err(ValidationError)\n"
            + "failures:\n"
            + "    test_rejects_wrong_result_type\n"
            + "".join(f"Running unrelated test binary {i}\n" for i in range(260))
            + "error: test failed, to rerun pass `-p example_crate --test validation_tests`\n"
        )

        def failing_tests() -> ToolResult:
            build.test_calls += 1
            return ToolResult(success=False, output=failure_output, error=failure_output)

        build.run_tests = failing_tests
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert "test_rejects_wrong_result_type" in result.test_errors[-1]
        test_records = [r for r in result.validation_cycle_records if r["phase"] == "test"]
        assert "test_rejects_wrong_result_type" in test_records[-1]["error_excerpt"]

    def test_sync_failure_appends_to_errors(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.sync_success = False
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        result = orch.run(task_id="t1")
        assert result.errors

    def test_sync_records_tool_metadata_for_audit(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.sync_metadata = {
            "sync_retry": {
                "reason": "cargo_lockfile_needs_update",
                "initial_command": "cargo fetch --locked",
                "retry_command": "cargo fetch",
                "retry_status": "success",
            }
        }
        _save_state(orch, implementation_prompt="p", files_changed=["Cargo.toml"], build_synced=False)

        result = orch.run(task_id="t1")

        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["status"] == "success"
        assert sync_records[-1]["metadata"]["sync_retry"]["reason"] == "cargo_lockfile_needs_update"
        assert sync_records[-1]["metadata"]["sync_retry"]["initial_command"] == "cargo fetch --locked"
        assert sync_records[-1]["metadata"]["sync_retry"]["retry_command"] == "cargo fetch"

    def test_sync_adopts_known_outputs_and_reruns_semantic_gates(self, tmp_project: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
        )
        output = tmp_project / "generated.lock"
        output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        build.sync_adoptable_files = {"generated.lock"}
        build.sync_side_effect = lambda: output.write_text("resolved during sync\n")
        stubs["test_writer"].side_effect = lambda state: setattr(state, "tests_up_to_date", True)
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.files_changed == ["src/main.py", "generated.lock"]
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"] == [
            {"path": "generated.lock", "before_status": "clean", "after_status": "tracked"}
        ]

    def test_sync_cleans_brand_new_builtin_lockfile_without_config(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "Cargo.lock"
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("brand-new lockfile from default sync\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert not output.exists()
        assert "Cargo.lock" not in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["cleaned"] == [
            {"path": "Cargo.lock", "before_status": "clean", "after_status": "untracked"}
        ]

    def test_sync_adopts_brand_new_builtin_lockfile_when_configured(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": ["Cargo.lock"]},
        }
        orch, _, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "Cargo.lock"
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("brand-new lockfile explicitly opted in\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert output.exists()
        assert "Cargo.lock" in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"] == [
            {"path": "Cargo.lock", "before_status": "clean", "after_status": "untracked"}
        ]

    def test_sync_adopts_outputs_from_configured_patterns(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": ["generated/api/"]},
        }
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "generated" / "api" / "schema.json"
        build.sync_side_effect = lambda: (output.parent.mkdir(parents=True, exist_ok=True), output.write_text("{}\n"))
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert "generated/api/schema.json" in result.files_changed
        assert len(stubs["reviewer"].calls) == 1
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "generated/api/schema.json"

    def test_sync_adopts_outputs_from_configured_string_glob(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": "generated/**/*.json"},
        }
        orch, _, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "generated" / "api" / "schema.json"
        build.sync_side_effect = lambda: (output.parent.mkdir(parents=True, exist_ok=True), output.write_text("{}\n"))
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert "generated/api/schema.json" in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "generated/api/schema.json"

    def test_sync_adopts_project_relative_paths_when_git_root_differs(self, tmp_project: Path):
        project = tmp_project / "app"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# app\n")
        output = project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(
            ["git", "add", "app/src/main.py", "app/Cargo.lock"], cwd=tmp_project, check=True, capture_output=True
        )
        subprocess.run(["git", "commit", "-m", "add app"], cwd=tmp_project, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("resolved during sync\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert "Cargo.lock" in result.files_changed
        assert "app/Cargo.lock" not in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "Cargo.lock"

    def test_sync_blocks_adoptable_outputs_outside_project_root(self, tmp_project: Path):
        project = tmp_project / "app"
        project.mkdir()
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock"}
        output = tmp_project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(["git", "add", "Cargo.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add root lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        output.write_text("resolved outside project root\n")

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert output.read_text() == "before sync\n"
        assert "outside project root" in (error or "")
        assert "Cargo.lock" not in state.files_changed
        assert metadata["outside_project"][0]["path"] == "Cargo.lock"
        assert metadata["cleaned"][0]["path"] == "Cargo.lock"
        assert "cleanup_failed" not in metadata
        assert state.validation_artifact_records[0]["status"] == "blocked"

    def test_sync_blocks_outside_project_outputs_after_adopting_inside_outputs(self, tmp_project: Path):
        project = tmp_project / "app"
        project.mkdir()
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock", "generated.lock"}
        inside_output = project / "generated.lock"
        inside_output.write_text("before sync\n")
        output = tmp_project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(
            ["git", "add", "app/generated.lock", "Cargo.lock"], cwd=tmp_project, check=True, capture_output=True
        )
        subprocess.run(["git", "commit", "-m", "add sync outputs"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        inside_output.write_text("resolved inside project root\n")
        output.write_text("resolved outside project root\n")

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "outside project root" in (error or "")
        assert output.read_text() == "before sync\n"
        assert "generated.lock" in state.files_changed
        assert metadata["adopted"][0]["path"] == "generated.lock"
        assert metadata["outside_project"][0]["path"] == "Cargo.lock"
        assert metadata["cleaned"][0]["path"] == "Cargo.lock"

    def test_sync_cleans_unexpected_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        artifact = tmp_project / "tmp-sync.log"
        build.sync_side_effect = lambda: artifact.write_text("cache\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert not artifact.exists()
        assert "tmp-sync.log" not in result.files_changed
        assert result.validation_artifact_records[0]["phase"] == "sync"
        assert result.validation_artifact_records[0]["status"] == "cleaned"
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["cleaned"][0]["path"] == "tmp-sync.log"

    def test_sync_adopts_known_outputs_before_unexpected_cleanup_failure(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
        )
        build.sync_adoptable_files = {"generated.lock"}
        tracked_output = tmp_project / "generated.lock"
        tracked_output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )
        before = orch._validation_artifact_snapshot(state)
        tracked_output.write_text("resolved during sync\n")
        (tmp_project / "tmp-sync.log").write_text("cache\n")
        monkeypatch.setattr(
            orchestrator_module,
            "restore_validation_artifacts",
            lambda *_args: ["tmp-sync.log: permission denied"],
        )

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "permission denied" in (error or "")
        assert "generated.lock" in state.files_changed
        assert not state.review_approved
        assert not state.security_approved
        assert not state.tests_up_to_date
        assert metadata["adopted"] == [{"path": "generated.lock", "before_status": "clean", "after_status": "tracked"}]
        assert metadata["cleanup_failed"] == [
            {"path": "tmp-sync.log", "before_status": "clean", "after_status": "untracked"}
        ]
        assert "adoptable" not in metadata
        assert state.validation_artifact_records[0]["status"] == "cleanup_failed"

    def test_sync_adopts_known_outputs_before_cleanup_pass_limit(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"generated.lock"}
        tracked_output = tmp_project / "generated.lock"
        tracked_output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        tracked_output.write_text("resolved during sync\n")
        (tmp_project / "tmp-sync.log").write_text("cache\n")
        monkeypatch.setattr(orchestrator_module, "_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES", 0)

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "after 0 cleanup passes" in (error or "")
        assert "generated.lock" in state.files_changed
        assert metadata["adopted"][0]["path"] == "generated.lock"
        assert metadata["cleanup_failed"][0]["path"] == "tmp-sync.log"
        assert "adoptable" not in metadata
        assert state.validation_artifact_records[0]["status"] == "cleanup_failed"


class TestValidationArtifacts:
    @staticmethod
    def _non_executable_mode(path: Path) -> int:
        path.chmod(0o644)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & stat.S_IXUSR:
            mode &= ~stat.S_IXUSR
            path.chmod(mode)
            mode = stat.S_IMODE(path.stat().st_mode)
        return mode

    @staticmethod
    def _executable_mode(path: Path, before_mode: int) -> int:
        mode = before_mode | stat.S_IXUSR
        if mode == before_mode:
            pytest.skip("filesystem does not preserve executable bit changes")
        path.chmod(mode)
        changed_mode = stat.S_IMODE(path.stat().st_mode)
        if changed_mode != mode:
            pytest.skip("filesystem does not preserve executable bit changes")
        return changed_mode

    def test_successful_build_cleans_untracked_generated_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_build_artifact() -> None:
            artifact = tmp_project / "generated" / "compile-artifact.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("generated by compile validation\n")

        build.compile_side_effect = write_build_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "generated" / "compile-artifact.txt").exists()
        assert result.validation_artifact_records[0]["phase"] == "build"
        assert result.validation_artifact_records[0]["status"] == "cleaned"

    def test_successful_tests_clean_untracked_generated_artifacts(self, tmp_project: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_runtime_artifact() -> None:
            runtime = tmp_project / "tests" / "client-main.runtime-1.ts"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text('import "file:///Users/example/project/src/client/main.ts";\n')

        build.test_side_effect = write_runtime_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "tests" / "client-main.runtime-1.ts").exists()
        assert len(stubs["fixer"].calls) == 0
        assert result.validation_artifact_records == [
            {
                "phase": "test",
                "status": "cleaned",
                "build_iteration": 1,
                "step": 0,
                "timestamp": result.validation_artifact_records[0]["timestamp"],
                "artifacts": [
                    {
                        "path": "tests/client-main.runtime-1.ts",
                        "before_status": "clean",
                        "after_status": "untracked",
                    }
                ],
            }
        ]
        assert any(
            record["phase"] == "validation_artifact" and record["status"] == "cleaned"
            for record in result.validation_cycle_records
        )

    def test_successful_tests_restore_existing_dirty_file_contents(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_task_file() -> None:
            source.write_text("# task change\n# generated during test\n")

        build.test_side_effect = mutate_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_existing_dirty_file_mode_only_change(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        before_mode = self._non_executable_mode(source)
        after_mode = self._executable_mode(source, before_mode)
        source.chmod(before_mode)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def chmod_task_file() -> None:
            source.chmod(after_mode)

        build.test_side_effect = chmod_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert stat.S_IMODE(source.stat().st_mode) == before_mode
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_existing_dirty_file_content_and_mode(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        before_mode = self._non_executable_mode(source)
        after_mode = self._executable_mode(source, before_mode)
        source.chmod(before_mode)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_task_file() -> None:
            source.write_text("# task change\n# generated during test\n")
            source.chmod(after_mode)

        build.test_side_effect = mutate_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert stat.S_IMODE(source.stat().st_mode) == before_mode
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_clean_tracked_file_from_head(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        assert source.read_text() == "# placeholder\n"
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_clean_tracked_file() -> None:
            source.write_text("# generated during test\n")

        build.test_side_effect = mutate_clean_tracked_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/feature.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# placeholder\n"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "clean",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_preserve_task_deleted_tracked_file(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.unlink()
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def recreate_deleted_file() -> None:
            source.write_text("# generated during test\n")

        build.test_side_effect = recreate_deleted_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not source.exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_checks_clean_untracked_generated_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=True,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            project_config={
                "project": {"build_tool": "python"},
                "build": {"checks": [{"name": "typecheck", "command": "python -m py_compile src/main.py"}]},
            },
        )

        def write_check_artifact() -> None:
            artifact = tmp_project / "reports" / "typecheck.runtime.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("generated by check validation\n")

        build.check_side_effects["typecheck"] = write_check_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "reports" / "typecheck.runtime.txt").exists()
        assert result.validation_artifact_records[0]["phase"] == "check"
        assert result.validation_artifact_records[0]["check_name"] == "typecheck"
        assert result.validation_artifact_records[0]["status"] == "cleaned"

    def test_gitignored_validation_outputs_are_not_artifacts(self, tmp_project: Path):
        (tmp_project / ".gitignore").write_text("coverage/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignore coverage"], cwd=tmp_project, check=True, capture_output=True)

        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_ignored_coverage() -> None:
            coverage = tmp_project / "coverage" / "index.html"
            coverage.parent.mkdir(parents=True, exist_ok=True)
            coverage.write_text("<html></html>\n")

        build.test_side_effect = write_ignored_coverage
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert (tmp_project / "coverage" / "index.html").exists()
        assert result.validation_artifact_records == []

    def test_isolated_subproject_validation_cleans_repo_root_artifacts(self, tmp_path: Path):
        repo = tmp_path
        project = repo / "apps" / "demo"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# placeholder\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_repo_root_artifact() -> None:
            (repo / "coverage.json").write_text("{}\n")

        build.test_side_effect = write_repo_root_artifact
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            worktree_path=str(project),
            worktree_base=str(repo),
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not (repo / "coverage.json").exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "coverage.json",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_no_isolate_subproject_validation_cleans_repo_root_artifacts(self, tmp_path: Path):
        repo = tmp_path
        project = repo / "apps" / "demo"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# placeholder\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_repo_root_artifact() -> None:
            (repo / "coverage.json").write_text("{}\n")

        build.test_side_effect = write_repo_root_artifact
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not (repo / "coverage.json").exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "coverage.json",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_validation_artifact_root_falls_back_to_project_root_outside_git(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        state = TaskState(task_id="t1", task_description="test task")

        assert orch._validation_artifact_root(state) == tmp_path.resolve()

    def test_validation_artifact_cleanup_rescans_after_restoring_gitignore(self, tmp_project: Path):
        (tmp_project / ".gitignore").write_text("coverage/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignore coverage"], cwd=tmp_project, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_hidden_artifact() -> None:
            (tmp_project / ".gitignore").write_text("coverage/\ngenerated/\n")
            artifact = tmp_project / "generated" / "test-runtime.cache"
            artifact.parent.mkdir()
            artifact.write_text("generated while hidden by mutated ignore rules\n")

        build.test_side_effect = write_hidden_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert (tmp_project / ".gitignore").read_text() == "coverage/\n"
        assert not (tmp_project / "generated" / "test-runtime.cache").exists()
        assert [record["artifacts"] for record in result.validation_artifact_records] == [
            [
                {
                    "path": ".gitignore",
                    "before_status": "clean",
                    "after_status": "tracked",
                }
            ],
            [
                {
                    "path": "generated/test-runtime.cache",
                    "before_status": "clean",
                    "after_status": "untracked",
                }
            ],
        ]

    def test_validation_artifact_cleanup_fails_after_repeated_rescan_artifacts(self, tmp_project: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        before = orch._validation_artifact_snapshot(state)
        artifact = tmp_project / "reports" / "stuck.cache"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("keeps reappearing\n")
        monkeypatch.setattr(orchestrator_module, "_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES", 2)
        monkeypatch.setattr(orchestrator_module, "restore_validation_artifacts", lambda *_args: [])

        ok = orch._record_validation_artifacts(
            state,
            phase="check",
            before=before,
            check_name="typecheck",
        )

        assert not ok
        assert state.check_status == "failed"
        assert (
            "check/typecheck command produced additional repository artifact(s) after 2 cleanup passes"
            in (state.check_errors[-1])
        )
        assert [record["status"] for record in state.validation_artifact_records] == ["cleaned", "cleaned"]
        assert [record["check_name"] for record in state.validation_artifact_records] == ["typecheck", "typecheck"]


# ---------------------------------------------------------------------------
# Tests — _build_tool xcodebuild factory
# ---------------------------------------------------------------------------


class TestBuildToolFactoryXcode:
    def test_xcodebuild_creates_xcode_tool(self, tmp_path: Path):
        from tools.xcode_tool import XcodeTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "xcodebuild"}, "build": {}})
        assert isinstance(tool, XcodeTool)

    def test_xcodebuild_uses_configured_scheme(self, tmp_path: Path):
        from tools.xcode_tool import XcodeTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "xcodebuild"},
            "build": {"scheme": "MyApp", "destination": "generic/platform=iOS"},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, XcodeTool)
        assert tool._scheme == "MyApp"


# ---------------------------------------------------------------------------
# Tests — _fmt_elapsed
# ---------------------------------------------------------------------------


class TestFmtElapsed:
    def test_seconds_under_60(self):
        assert _fmt_elapsed(45.7) == "46s"

    def test_zero_seconds(self):
        assert _fmt_elapsed(0.0) == "0s"

    def test_exactly_60_seconds(self):
        assert _fmt_elapsed(60.0) == "1m 00s"

    def test_minutes_format(self):
        assert _fmt_elapsed(125.0) == "2m 05s"
