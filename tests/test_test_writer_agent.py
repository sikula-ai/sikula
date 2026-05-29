"""Tests for agents/test_writer_agent.py — TestWriterAgent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.test_writer_agent import TestWriterAgent, _MAX_DIFF_CHARS, _parse_testability_gaps, _step_scope
from tests.conftest import StubLLMClient
from core.state import TaskState
from tools.base_tool import ToolResult


def _make_state(**kwargs) -> TaskState:
    defaults = {
        "task_id": "t1",
        "task_description": "Add login screen",
        "implementation_prompt": "Create LoginActivity",
        "files_changed": ["src/LoginActivity.kt"],
    }
    defaults.update(kwargs)
    return TaskState(**defaults)


def _make_agent(
    llm: StubLLMClient,
    file_tool=None,
    git_tool=None,
    project_config: dict | None = None,
) -> TestWriterAgent:
    tools = {}
    if file_tool is not None:
        tools["file"] = file_tool
    if git_tool is not None:
        tools["git"] = git_tool
    return TestWriterAgent(llm=llm, tools=tools, project_config=project_config or {})


def _config_with_test_paths(test_paths: list[str] | None = None) -> dict:
    return {"sandbox": {"allowed_test_write_paths": test_paths or ["tests/"]}}


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------


class TestTestWriterAgentGuards:
    def test_no_implementation_prompt_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt=None)
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_no_files_changed_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(files_changed=[])
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_no_file_tool_returns_failure(self, stub_llm: StubLLMClient):
        state = _make_state()
        result = _make_agent(stub_llm, project_config=_config_with_test_paths()).run(state)
        assert not result.success

    def test_no_test_write_paths_skips_and_sets_flag(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config={"sandbox": {}}).run(state)
        assert result.success
        assert state.tests_up_to_date is True

    def test_empty_test_write_paths_skips(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths([])).run(state)
        assert result.success
        assert state.tests_up_to_date is True


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestTestWriterAgentSuccess:
    def test_tests_up_to_date_set_on_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.tests_up_to_date is True

    def test_changed_test_files_added_to_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "tests/LoginTest.kt" in state.files_changed

    def test_returns_success_with_data(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert result.success
        assert "files_written" in result.data

    def test_changed_files_added_to_test_files_written(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "tests/LoginTest.kt" in state.test_files_written

    def test_test_files_written_not_duplicated_on_second_run(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths())
        agent.run(state)
        agent.run(state)
        assert state.test_files_written.count("tests/LoginTest.kt") == 1

    def test_no_changes_leaves_test_files_written_empty(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_files_written == []

    def test_no_changes_returns_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert result.success

    def test_no_changes_sets_tests_up_to_date(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.tests_up_to_date is True

    def test_test_write_recorded_in_history(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert any(e["action"] == "test_write" for e in state.history)

    def test_outside_test_write_path_records_warning(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt", "src/Login.kt"]
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert result.success
        warning = next(e for e in state.history if e["action"] == "write_path_warning")
        assert warning["agent"] == "test_writer"
        assert "src/Login.kt" in warning["result"]

    def test_testability_gap_records_warning_by_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        stub_llm.agent_output = (
            "TESTABILITY GAP:\n"
            "target: share sheet opens\n"
            "reason: no UI test harness\n"
            "recommended_action: add UI test helper\n"
            "risk: medium\n"
        )
        state = _make_state(active_scope="final_full_task", build_iterations=4)
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert result.success
        assert state.failed is False
        assert state.tests_up_to_date is True
        assert len(state.testability_gaps) == 1
        gap = state.testability_gaps[0]
        assert gap["source"] == "test_writer"
        assert gap["scope"] == "final_full_task"
        assert gap["build_iteration"] == 4
        assert gap["target"] == "share sheet opens"
        assert gap["reason"] == "no UI test harness"
        assert gap["recommended_action"] == "add UI test helper"
        assert gap["risk"] == "medium"
        assert any(e["action"] == "testability_gap" for e in state.history)

    def test_testability_gap_can_fail_by_policy(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        stub_llm.agent_output = "TESTABILITY GAP:\ntarget: login UI\nreason: no UI harness\nrisk: high\n"
        state = _make_state()
        config = {**_config_with_test_paths(), "test_writer": {"testability_gap_policy": "fail"}}
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert state.tests_up_to_date is True
        assert len(state.testability_gaps) == 1

    def test_testability_gap_with_written_tests_can_fail_by_policy(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = "TESTABILITY GAP:\ntarget: login UI\nreason: no UI harness\nrisk: high\n"
        state = _make_state()
        config = {**_config_with_test_paths(), "test_writer": {"testability_gap_policy": "fail"}}
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert state.tests_up_to_date is True
        assert "tests/LoginTest.kt" in state.test_files_written
        assert result.data["files_written"] == ["tests/LoginTest.kt"]
        assert len(state.testability_gaps) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestTestWriterAgentErrors:
    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert not result.success
        assert "agent timed out" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert any(e["action"] == "test_write_failed" for e in state.history)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestTestWriterAgentPrompt:
    def test_files_changed_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(files_changed=["src/Login.kt", "src/Auth.kt"])
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "src/Login.kt" in prompt
        assert "src/Auth.kt" in prompt

    def test_coverage_target_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        config = {"sandbox": {"allowed_test_write_paths": ["tests/"]}, "test_writer": {"coverage_target": 85}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "85%" in prompt
        assert "Within the configured test surface" in prompt

    def test_default_coverage_target_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "90%" in stub_llm.agent_calls[0]

    def test_default_test_surface_policy_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "Test surface policy: existing_infrastructure" in prompt
        assert "Use only existing project test infrastructure" in prompt
        assert "Missing out-of-surface harnesses are not by themselves a TESTABILITY GAP" in prompt
        assert state.test_write_records[0]["test_surface_policy"] == "existing_infrastructure"

    def test_complete_test_surface_policy_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        config = {
            **_config_with_test_paths(),
            "test_writer": {"test_surface_policy": "complete"},
        }
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "Test surface policy: complete" in prompt
        assert "cover the complete changed behavior" in prompt
        assert "report a TESTABILITY GAP" in prompt
        assert state.test_write_records[0]["test_surface_policy"] == "complete"

    def test_unknown_test_surface_policy_falls_back_to_existing_infrastructure(
        self, stub_llm: StubLLMClient, file_tool
    ):
        config = {
            **_config_with_test_paths(),
            "test_writer": {"test_surface_policy": "unknown"},
        }
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "Test surface policy: existing_infrastructure" in prompt
        assert state.test_write_records[0]["test_surface_policy"] == "existing_infrastructure"

    def test_diff_unavailable_fallback_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "diff not available" in stub_llm.agent_calls[0]

    def test_task_description_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(task_description="Add login screen. Add tests for empty credentials.")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "ORIGINAL TASK DESCRIPTION:" in prompt
        assert "Add tests for empty credentials" in prompt

    def test_ui_internal_testing_guard_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt="Add SwiftUI navigation")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "test through stable seams" in prompt
        assert "Do NOT write brittle tests" in prompt
        assert "opaque view trees" in prompt

    def test_source_inspection_fallback_guard_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt="Add navigation contract")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "Source-file inspection tests are a last-resort" in prompt
        assert "do not depend on the test runner's current working directory" in prompt
        assert "TESTABILITY GAP:" in prompt
        assert "build configuration" in prompt

    def test_entry_point_failure_path_coverage_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt="Add detail navigation")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "Map changed behaviour through its production entry points" in prompt
        assert "If multiple entry points reach the same changed operation" in prompt
        assert "cover each entry point separately" in prompt
        assert "promises, futures" in prompt
        assert "observable failure/error path through the entry point" in prompt
        assert "configured" in prompt
        assert "test surface can do so" in prompt
        assert "report a TESTABILITY GAP" in prompt

    def test_structured_input_contract_matrix_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt="Update expression validator")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "positive/negative contract matrix" in prompt
        assert "expected result type" in prompt
        assert "wrong expected result type rejection" in prompt
        assert "Preserve the contract dimension" in prompt
        assert "parse/load validation" in prompt

    def test_single_pass_prompt_has_no_current_step(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "CURRENT STEP:" not in stub_llm.agent_calls[0]

    def test_multi_step_prompt_contains_current_step_scope(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(
            task_description="Add login flow. Add tests for locked accounts.",
            plan=["Create UI", "Add locked-account validation"],
            current_step=1,
        )
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "CURRENT STEP:" in prompt
        assert "Step 2/2: Add locked-account validation" in prompt
        assert "use CURRENT STEP as the primary scope signal" in prompt
        assert "Do not add" in prompt
        assert "tests for future steps" in prompt

    def test_final_full_task_prompt_replaces_current_step_scope(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(
            task_description="Add login flow. Add tests for locked accounts.",
            plan=["Create UI", "Add locked-account validation"],
            current_step=1,
            active_scope="final_full_task",
        )
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "FINAL FULL-TASK TEST SCOPE" in prompt
        assert "CURRENT STEP:" not in prompt
        assert "Do not restrict tests to the last planned step." in prompt
        assert state.test_write_records[0]["scope"] == "final_full_task"

    def test_step_scope_handles_out_of_range_current_step(self):
        state = _make_state(plan=["Create UI"], current_step=3)
        assert "unknown" in _step_scope(state)


# ---------------------------------------------------------------------------
# Diff handling
# ---------------------------------------------------------------------------


class TestTestWriterAgentDiff:
    def test_diff_included_when_git_tool_present(self, stub_llm: StubLLMClient, file_tool, git_tool, tmp_project: Path):
        (tmp_project / "src" / "main.py").write_text("# changed\n")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool, project_config=_config_with_test_paths()).run(
            state
        )
        assert "main.py" in stub_llm.agent_calls[0]

    def test_long_diff_truncated(self, stub_llm: StubLLMClient, file_tool, git_tool):
        big_diff = "+" + "x" * (_MAX_DIFF_CHARS + 1000)
        git_tool.diff_head = MagicMock(return_value=ToolResult(success=True, output=big_diff))
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool, project_config=_config_with_test_paths()).run(
            state
        )
        assert "truncated" in stub_llm.agent_calls[0]


class TestTestabilityGapParsing:
    def test_extracts_structured_gap(self):
        gaps = _parse_testability_gaps(
            "TESTABILITY GAP:\n"
            "target: native share sheet\n"
            "reason: no UI test harness\n"
            "recommended_action: add UI harness\n"
            "risk: medium\n"
        )
        assert gaps == [
            {
                "message": (
                    "TESTABILITY GAP:\n"
                    "target: native share sheet\n"
                    "reason: no UI test harness\n"
                    "recommended_action: add UI harness\n"
                    "risk: medium"
                ),
                "target": "native share sheet",
                "reason": "no UI test harness",
                "recommended_action": "add UI harness",
                "risk": "medium",
            }
        ]

    def test_extracts_multiple_gaps(self):
        gaps = _parse_testability_gaps(
            "TESTABILITY GAP:\ntarget: native share\nreason: no UI harness\n\n"
            "TESTABILITY GAP:\ntarget: deep link\nreason: no navigation test helper\n"
        )
        assert [gap["target"] for gap in gaps] == ["native share", "deep link"]

    def test_no_marker_returns_empty(self):
        assert _parse_testability_gaps("No test changes needed.") == []


# ---------------------------------------------------------------------------
# Write record
# ---------------------------------------------------------------------------


class TestTestWriterWriteRecord:
    def test_record_created_on_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert len(state.test_write_records) == 1

    def test_record_stores_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        state = _make_state(implementation_prompt="Create LoginActivity")
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "Create LoginActivity" in state.test_write_records[0]["test_writer_prompt"]

    def test_record_stores_output(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = "Added LoginActivityTest with 5 tests."
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["test_writer_output"] == "Added LoginActivityTest with 5 tests."

    def test_record_stores_files_written(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt", "tests/AuthTest.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["files_written"] == ["tests/LoginTest.kt", "tests/AuthTest.kt"]

    def test_record_created_on_no_changes(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert len(state.test_write_records) == 1
        assert state.test_write_records[0]["files_written"] == []

    def test_record_created_on_llm_error_with_none_output(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("timeout")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert len(state.test_write_records) == 1
        rec = state.test_write_records[0]
        assert rec["test_writer_output"] is None
        assert rec["files_written"] == []

    def test_record_has_timestamp(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "timestamp" in state.test_write_records[0]

    def test_records_accumulate_across_calls(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = "first run"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths())
        agent.run(state)
        stub_llm.agent_output = "second run"
        agent.run(state)
        assert len(state.test_write_records) == 2
        assert state.test_write_records[0]["test_writer_output"] == "first run"
        assert state.test_write_records[1]["test_writer_output"] == "second run"

    def test_record_stores_step_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["step"] == 0

    def test_record_stores_step_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        state.current_step = 2
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["step"] == 2

    def test_record_stores_build_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["build_iteration"] == 0

    def test_record_stores_build_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        state.build_iterations = 3
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert state.test_write_records[0]["build_iteration"] == 3


class TestTestWriterAgentExtraRules:
    def test_extra_rules_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.agent_result = []
        (tmp_project / "test_writer_rules.md").write_text("Always use data-driven tests.")
        config = {**_config_with_test_paths(), "test_writer": {"extra_rules": "test_writer_rules.md"}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "Always use data-driven tests." in stub_llm.agent_calls[0]
        assert "Project-specific rules" in stub_llm.agent_calls[0]

    def test_extra_rules_absent_when_not_configured(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert "Project-specific rules" not in stub_llm.agent_calls[0]


class TestTestWriterNoNetworkConstraint:
    def test_no_network_write_agent_prepended(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=_config_with_test_paths()).run(state)
        assert stub_llm.agent_calls[0].startswith(AGENT_SECURITY_PREFIX)
