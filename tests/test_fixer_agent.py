"""Tests for agents/fixer_agent.py — FixerAgent."""

from __future__ import annotations

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.fixer_agent import (
    FixerAgent,
    _errors_section,
    _guidelines_files,
    _has_valid_production_test_failure_triage,
    _tech_stack,
    _test_constraint,
    _test_failure_production_writes,
    _write_paths_for_state,
)
from tests.conftest import StubLLMClient
from core.state import TaskState


def _make_state(**kwargs) -> TaskState:
    defaults = {
        "task_id": "t1",
        "task_description": "Add login screen",
        "implementation_prompt": "Create LoginActivity",
    }
    defaults.update(kwargs)
    return TaskState(**defaults)


def _make_agent(llm: StubLLMClient, file_tool=None, project_config: dict | None = None) -> FixerAgent:
    tools = {}
    if file_tool is not None:
        tools["file"] = file_tool
    return FixerAgent(llm=llm, tools=tools, project_config=project_config or {})


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------


class TestFixerAgentGuards:
    def test_no_errors_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success
        assert "no errors" in result.message.lower()

    def test_no_file_tool_returns_failure(self, stub_llm: StubLLMClient):
        state = _make_state()
        state.errors = ["compile error"]
        result = _make_agent(stub_llm).run(state)
        assert not result.success


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestFixerAgentSuccess:
    def test_fixer_output_appended_on_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "Fixed the compile error in Login.kt."
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["fixer_output"] == "Fixed the compile error in Login.kt."

    def test_fixer_output_appended_on_no_changes(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        stub_llm.agent_output = "I could not find a way to fix the error."
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["fixer_output"] == "I could not find a way to fix the error."

    def test_fixer_output_accumulated_across_calls(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "first fix"
        state = _make_state()
        state.errors = ["compile error"]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        state.errors = ["another error"]
        stub_llm.agent_output = "second fix"
        agent.run(state)
        assert state.fix_cycle_records[0]["fixer_output"] == "first fix"
        assert state.fix_cycle_records[1]["fixer_output"] == "second fix"

    def test_changed_files_added_to_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "src/Login.kt" in state.files_changed

    def test_errors_cleared_after_fix(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        state.test_errors = ["test failed"]
        state.check_errors = ["lint error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.errors == []
        assert state.test_errors == []
        assert state.check_errors == []

    def test_returns_success_with_data(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success
        assert "files_written" in result.data

    def test_no_duplicate_files_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state(files_changed=["src/Login.kt"])
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.files_changed.count("src/Login.kt") == 1

    def test_fix_recorded_in_history(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "fix" for e in state.history)

    def test_outside_active_write_path_records_warning(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt", "README.md"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["compile error"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        warning = next(e for e in state.history if e["action"] == "write_path_warning")
        assert warning["agent"] == "fixer"
        assert "README.md" in warning["result"]

    def test_no_changes_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        state.errors = ["compile error"]
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_no_changes_recorded_in_history(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "fix_failed" for e in state.history)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestFixerAgentErrors:
    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        state.errors = ["compile error"]
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success
        assert "agent timed out" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "fix_failed" for e in state.history)

    def test_llm_error_still_creates_cycle_record(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        state.errors = ["compile error"]
        state.build_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert len(state.fix_cycle_records) == 1
        rec = state.fix_cycle_records[0]
        assert rec["fixer_output"] is None
        assert rec["build_iteration"] == 2


# ---------------------------------------------------------------------------
# Fix cycle record
# ---------------------------------------------------------------------------


class TestFixerAgentFixCycleRecord:
    def test_record_captures_errors_before(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["build error A"]
        state.test_errors = ["test failure B"]
        state.check_errors = ["lint error C"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        rec = state.fix_cycle_records[0]
        assert rec["errors_before"]["build"] == ["build error A"]
        assert rec["errors_before"]["test"] == ["test failure B"]
        assert rec["errors_before"]["check"] == ["lint error C"]

    def test_record_errors_before_is_snapshot_not_reference(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["build error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        # fixer clears state.errors; snapshot in record must still have original value
        assert state.errors == []
        assert state.fix_cycle_records[0]["errors_before"]["build"] == ["build error"]

    def test_record_stores_fixer_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = state.fix_cycle_records[0]["fixer_prompt"]
        assert state.task_description in prompt
        assert "compile error" in prompt

    def test_record_stores_files_written(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt", "src/Auth.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["files_written"] == ["src/Login.kt", "src/Auth.kt"]

    def test_record_stores_build_iteration(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        state.build_iterations = 3
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["build_iteration"] == 3

    def test_no_changes_still_creates_record(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert len(state.fix_cycle_records) == 1
        assert state.fix_cycle_records[0]["files_written"] == []

    def test_record_stores_step_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["step"] == 0

    def test_record_stores_step_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        state.current_step = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.fix_cycle_records[0]["step"] == 2


# ---------------------------------------------------------------------------
# Write path routing
# ---------------------------------------------------------------------------


class TestFixerAgentWritePaths:
    def test_test_only_errors_use_production_and_test_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "src/, tests/" in prompt
        assert "fix production code instead of weakening the test" in prompt
        assert "TEST FAILURE TRIAGE:" in prompt
        assert "classification: production_defect | stale_test | malformed_test | unclear" in prompt
        assert "chosen_fix: production_code | test_code" in prompt

    def test_build_errors_use_production_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "src/" in stub_llm.agent_calls[0]

    def test_mixed_errors_use_production_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["compile error"]
        state.test_errors = ["assertion failed"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "NEVER create or modify unit tests" in prompt

    def test_check_errors_use_production_and_test_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.check_errors = ["detekt: unused import"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "src/, tests/" in prompt
        assert "MAY modify production or test files" in prompt

    def test_no_network_write_agent_prepended(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert stub_llm.agent_calls[0].startswith(AGENT_SECURITY_PREFIX)


class TestFixerAgentTestFailureProductionGate:
    def test_test_failure_production_fix_requires_explicit_triage(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "Fixed production code without the required triage."
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert state.test_errors == ["assertion failed"]
        assert "src/Login.kt" in state.files_changed
        assert any(e["action"] == "fix_failed" for e in state.history)

    def test_test_failure_production_fix_with_valid_triage_succeeds(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        assert state.failed is False
        assert state.test_errors == []
        assert "src/Login.kt" in state.files_changed

    def test_test_failure_test_only_fix_does_not_require_production_triage(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\nclassification: stale_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        assert state.test_errors == []


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestTestConstraint:
    def test_test_only_errors_allow_test_modification(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        assert "MAY create and modify test files" in _test_constraint(state)
        assert "fix production code instead of weakening the test" in _test_constraint(state)
        assert "TEST FAILURE TRIAGE:" in _test_constraint(state)

    def test_build_errors_block_test_modification(self):
        state = _make_state()
        state.errors = ["compile error"]
        assert "NEVER create or modify unit tests" in _test_constraint(state)

    def test_check_errors_allow_test_modification(self):
        state = _make_state()
        state.check_errors = ["lint error"]
        assert "MAY modify production or test files" in _test_constraint(state)

    def test_check_errors_with_test_errors_allow_test_modification(self):
        state = _make_state()
        state.check_errors = ["lint error"]
        state.test_errors = ["assertion failed"]
        assert "MAY modify production or test files" in _test_constraint(state)

    def test_mixed_errors_block_test_modification(self):
        state = _make_state()
        state.errors = ["compile error"]
        state.test_errors = ["assertion failed"]
        assert "NEVER create or modify unit tests" in _test_constraint(state)


class TestWritePathsForState:
    def test_build_errors_use_only_production_paths(self):
        state = _make_state()
        state.errors = ["compile error"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox) == ["src/"]

    def test_test_failures_use_production_and_test_paths(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox) == ["src/", "tests/"]

    def test_check_errors_use_production_and_test_paths(self):
        state = _make_state()
        state.check_errors = ["fmt failed"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox) == ["src/", "tests/"]

    def test_test_failure_write_paths_are_deduplicated(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        sandbox = {"allowed_write_paths": ["."], "allowed_test_write_paths": ["."]}
        assert _write_paths_for_state(state, sandbox) == ["."]


class TestTestFailureProductionWrites:
    def test_returns_files_outside_test_paths(self):
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert _test_failure_production_writes(["src/Login.kt", "tests/LoginTest.kt"], sandbox) == ["src/Login.kt"]

    def test_no_test_paths_treats_all_changed_files_as_production(self):
        assert _test_failure_production_writes(["src/Login.kt"], {}) == ["src/Login.kt"]


class TestProductionTestFailureTriage:
    def test_valid_triage_allows_production_fix(self):
        output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: structured contract\n"
            "chosen_fix: production_code\n"
        )
        assert _has_valid_production_test_failure_triage(output)

    def test_template_placeholder_is_not_valid_triage(self):
        output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect | stale_test | malformed_test | unclear\n"
            "chosen_fix: production_code | test_code\n"
        )
        assert not _has_valid_production_test_failure_triage(output)

    def test_test_code_triage_does_not_allow_production_fix(self):
        output = "TEST FAILURE TRIAGE:\nclassification: stale_test\ncontract_affected: none\nchosen_fix: test_code\n"
        assert not _has_valid_production_test_failure_triage(output)


class TestErrorsSection:
    def test_build_errors_included(self):
        state = _make_state()
        state.errors = ["error 1", "error 2"]
        section = _errors_section(state)
        assert "BUILD ERRORS" in section
        assert "error 1" in section

    def test_test_errors_included(self):
        state = _make_state()
        state.test_errors = ["test failed"]
        section = _errors_section(state)
        assert "TEST FAILURES" in section
        assert "test failed" in section

    def test_check_errors_included(self):
        state = _make_state()
        state.check_errors = ["lint warning"]
        section = _errors_section(state)
        assert "CHECK ERRORS" in section
        assert "lint warning" in section

    def test_only_last_three_errors_included(self):
        state = _make_state()
        state.errors = ["e1", "e2", "e3", "e4", "e5"]
        section = _errors_section(state)
        assert "e3" in section
        assert "e4" in section
        assert "e5" in section
        assert "e1" not in section
        assert "e2" not in section


class TestFixerTechStack:
    def test_language_and_ui(self):
        result = _tech_stack({"project": {"language": "Kotlin", "ui": "Compose"}})
        assert result == "Kotlin / Compose"

    def test_platform_language_ui(self):
        result = _tech_stack({"project": {"platform": "Android", "language": "Kotlin", "ui": "Compose"}})
        assert result == "Android / Kotlin / Compose"

    def test_empty_returns_software(self):
        assert _tech_stack({}) == "software"


class TestFixerGuidelinesFiles:
    def test_defaults_to_readme(self):
        assert "README.md" in _guidelines_files({})

    def test_uses_configured_files(self):
        config = {"guidelines": {"context_files": ["docs/arch.md"]}}
        assert "docs/arch.md" in _guidelines_files(config)
