"""Tests for agents/implementer_agent.py — ImplementerAgent."""

from __future__ import annotations

from agents.base_agent import AGENT_SECURITY_PREFIX, paths_outside_allowed
from agents.implementer_agent import ImplementerAgent, _guidelines_files, _tech_stack
from tests.conftest import StubLLMClient
from core.state import TaskState


def _make_state(**kwargs) -> TaskState:
    defaults = {
        "task_id": "t1",
        "task_description": "Add login screen",
        "implementation_prompt": "Create LoginActivity with email/password fields",
    }
    defaults.update(kwargs)
    return TaskState(**defaults)


def _make_agent(llm: StubLLMClient, file_tool=None, project_config: dict | None = None) -> ImplementerAgent:
    tools = {}
    if file_tool is not None:
        tools["file"] = file_tool
    return ImplementerAgent(llm=llm, tools=tools, project_config=project_config or {})


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------


class TestImplementerAgentGuards:
    def test_no_implementation_prompt_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt=None)
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_no_file_tool_returns_failure(self, stub_llm: StubLLMClient):
        state = _make_state()
        result = _make_agent(stub_llm).run(state)
        assert not result.success


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestImplementerAgentSuccess:
    def test_agent_output_appended_on_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "Created LoginActivity with email/password fields."
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert (
            state.implement_cycle_records[0]["implementer_output"]
            == "Created LoginActivity with email/password fields."
        )

    def test_agent_output_appended_on_no_changes(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        stub_llm.agent_output = "I found nothing to change."
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["implementer_output"] == "I found nothing to change."

    def test_agent_output_accumulated_across_calls(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/A.kt"]
        stub_llm.agent_output = "first"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        stub_llm.agent_output = "second"
        agent.run(state)
        assert state.implement_cycle_records[0]["implementer_output"] == "first"
        assert state.implement_cycle_records[1]["implementer_output"] == "second"

    def test_changed_files_added_to_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt", "src/di/Module.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "src/Login.kt" in state.files_changed
        assert "src/di/Module.kt" in state.files_changed

    def test_returns_success_with_files_in_data(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success
        assert "files_written" in result.data

    def test_no_duplicate_files_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state(files_changed=["src/Login.kt"])
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.files_changed.count("src/Login.kt") == 1

    def test_no_changes_returns_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success

    def test_no_changes_skipped_message(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = []
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "no file changes" in result.message.lower()

    def test_implement_recorded_in_history(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "implement" for e in state.history)

    def test_outside_allowed_write_path_records_warning(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt", "README.md"]
        config = {"sandbox": {"allowed_write_paths": ["src/"]}}
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        warning = next(e for e in state.history if e["action"] == "write_path_warning")
        assert warning["agent"] == "implementer"
        assert "README.md" in warning["result"]

    def test_allowed_write_path_does_not_record_warning(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"]}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not any(e["action"] == "write_path_warning" for e in state.history)

    def test_root_allowed_write_path_does_not_record_warning(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["README.md"]
        config = {"sandbox": {"allowed_write_paths": ["."]}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not any(e["action"] == "write_path_warning" for e in state.history)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestImplementerAgentErrors:
    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success
        assert "agent timed out" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "implement_failed" for e in state.history)

    def test_llm_error_still_creates_step_record(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_error = RuntimeError("agent timed out")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert len(state.implement_cycle_records) == 1
        assert state.implement_cycle_records[0]["implementer_output"] is None


# ---------------------------------------------------------------------------
# Step record
# ---------------------------------------------------------------------------


class TestImplementerAgentStepRecord:
    def test_record_stores_implementer_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state(implementation_prompt="Create LoginActivity")
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Create LoginActivity" in state.implement_cycle_records[0]["implementer_prompt"]

    def test_record_stores_implementer_output(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "Created LoginActivity"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["implementer_output"] == "Created LoginActivity"

    def test_record_stores_files_written(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt", "src/di/Module.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["files_written"] == ["src/Login.kt", "src/di/Module.kt"]

    def test_record_step_zero_when_no_plan(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["step"] == 0
        assert state.implement_cycle_records[0]["step_description"] is None

    def test_record_captures_step_description_from_plan(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.plan = ["Add login screen", "Add logout button"]
        state.current_step = 1
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        rec = state.implement_cycle_records[0]
        assert rec["step"] == 1
        assert rec["step_description"] == "Add logout button"

    def test_record_stores_build_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["build_iteration"] == 0

    def test_record_stores_build_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.build_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["build_iteration"] == 2

    def test_record_stores_review_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["review_iteration"] == 0

    def test_record_stores_review_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.review_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["review_iteration"] == 2

    def test_record_stores_security_review_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.security_review_iterations = 1
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["security_review_iteration"] == 1

    def test_security_fix_pass_has_review_iteration_zero(self, stub_llm: StubLLMClient, file_tool):
        # Simulates orchestrator behaviour after moving review_iterations reset
        # to before the implementer call: security fix always has review_iteration=0.
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.security_review_iterations = 1
        state.review_iterations = 0  # orchestrator resets before calling implementer
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.implement_cycle_records[0]["review_iteration"] == 0
        assert state.implement_cycle_records[0]["security_review_iteration"] == 1


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestImplementerAgentPrompt:
    def test_implementation_prompt_in_agent_call(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt="Create LoginActivity")
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Create LoginActivity" in stub_llm.agent_calls[0]

    def test_step_context_included_when_plan_set(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "step 2 of 2" in prompt.lower()
        assert "Step B" in prompt

    def test_final_full_task_scope_omits_current_step(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.active_scope = "final_full_task"
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "FINAL FULL-TASK PHASE" in prompt
        assert "CURRENT STEP" not in prompt
        assert "Do NOT restrict changes to" in prompt
        assert state.implement_cycle_records[0]["scope"] == "final_full_task"
        assert state.implement_cycle_records[0]["step_description"] is None

    def test_no_step_context_without_plan(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "CURRENT STEP" not in stub_llm.agent_calls[0]

    def test_review_issues_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        state.review_issues = ["missing null check", "wrong return type"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "missing null check" in prompt
        assert "wrong return type" in prompt

    def test_no_review_section_without_issues(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "REVIEW ISSUES" not in stub_llm.agent_calls[0]

    def test_allowed_write_paths_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        config = {"sandbox": {"allowed_write_paths": ["app/src/main/kotlin"]}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "app/src/main/kotlin" in stub_llm.agent_calls[0]

    def test_no_network_write_agent_prepended(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert stub_llm.agent_calls[0].startswith(AGENT_SECURITY_PREFIX)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestTechStack:
    def test_language_only(self):
        assert _tech_stack({"project": {"language": "Kotlin"}}) == "Kotlin"

    def test_language_and_ui(self):
        result = _tech_stack({"project": {"language": "Kotlin", "ui": "Jetpack Compose"}})
        assert result == "Kotlin / Jetpack Compose"

    def test_platform_language_ui(self):
        result = _tech_stack({"project": {"platform": "Android", "language": "Kotlin", "ui": "Jetpack Compose"}})
        assert result == "Android / Kotlin / Jetpack Compose"

    def test_platform_only(self):
        assert _tech_stack({"project": {"platform": "iOS"}}) == "iOS"

    def test_platform_without_ui(self):
        assert _tech_stack({"project": {"platform": "Android", "language": "Kotlin"}}) == "Android / Kotlin"

    def test_empty_returns_software(self):
        assert _tech_stack({}) == "software"

    def test_missing_project_key(self):
        assert _tech_stack({"project": {}}) == "software"


class TestWritePathAudit:
    def test_detects_paths_outside_allowed_roots(self):
        result = paths_outside_allowed(["src/Login.kt", "README.md"], ["src/"])
        assert result == ["README.md"]

    def test_allows_root_path(self):
        assert paths_outside_allowed(["README.md"], ["."]) == []

    def test_treats_parent_and_absolute_paths_as_outside(self):
        result = paths_outside_allowed(["../outside.txt", "/tmp/outside.txt"], ["src/"])
        assert result == ["../outside.txt", "/tmp/outside.txt"]

    def test_empty_allowed_paths_do_not_warn(self):
        assert paths_outside_allowed(["README.md"], []) == []


class TestGuidelinesFiles:
    def test_defaults_to_readme(self):
        lines = _guidelines_files({})
        assert "README.md" in lines

    def test_uses_configured_context_files(self):
        config = {"guidelines": {"context_files": ["docs/guidelines.md", "docs/arch.md"]}}
        lines = _guidelines_files(config)
        assert "docs/guidelines.md" in lines
        assert "docs/arch.md" in lines

    def test_empty_context_files(self):
        config = {"guidelines": {"context_files": []}}
        assert _guidelines_files(config) == ""
