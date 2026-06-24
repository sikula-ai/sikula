"""Tests for agents/analyst_agent.py — AnalystAgent."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from agents.analyst_agent import AnalystAgent
from agents.base_agent import AGENT_SECURITY_PREFIX
from tests.conftest import StubLLMClient
from core.state import TaskState

VALID_ANALYST_PROMPT = (
    "1. Context: feature module\n"
    "2. Required changes: update src/main.py to implement the requested behavior\n"
    "3. Architecture constraints: follow existing project patterns\n"
    "4. Hard rules: minimal changes only\n"
    "5. Cleanup: no dead production code expected\n"
    "6. Acceptance criteria: requested behavior works and configured validation passes"
)

BAD_META_PROMPT = (
    "The implementation prompt above is the final output. The task is complete — no further tracking is needed "
    "(this analyser run produced a single artifact, the prompt itself, and is not part of an ongoing multi-step "
    "implementation)."
)


def _make_agent(
    llm: StubLLMClient,
    file_tool,
    project_config: dict[str, Any] | None = None,
) -> AnalystAgent:
    tools = {"file": file_tool}
    return AnalystAgent(llm=llm, tools=tools, project_config=project_config or {})


class TestAnalystAgentRun:
    def test_missing_file_tool_returns_failure(self, stub_llm: StubLLMClient, task_state: TaskState):
        agent = AnalystAgent(llm=stub_llm, tools={})
        result = agent.run(task_state)
        assert not result.success
        assert "FileTool" in result.message

    def test_stores_implementation_prompt(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        result = agent.run(task_state)
        assert result.success
        assert task_state.implementation_prompt == stub_llm.readonly_result

    def test_empty_output_returns_failure(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = ""
        agent = _make_agent(stub_llm, file_tool)
        result = agent.run(task_state)
        assert not result.success
        assert "empty" in result.message

    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_error = RuntimeError("timeout")
        agent = _make_agent(stub_llm, file_tool)
        result = agent.run(task_state)
        assert not result.success
        assert "timeout" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_error = RuntimeError("timeout")
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert any(e["action"] == "analyze_failed" for e in task_state.history)

    def test_warnings_extracted_and_stored(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = f"{VALID_ANALYST_PROMPT}\n⚠️ Missing field in API\n⚠️ No string key"
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert len(task_state.analyst_warnings) == 2
        assert "Missing field in API" in task_state.analyst_warnings[0]

    def test_record_added_on_success(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert any(e["action"] == "analyze" for e in task_state.history)

    def test_result_data_contains_prompt(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        result = agent.run(task_state)
        assert result.data.get("implementation_prompt") == VALID_ANALYST_PROMPT

    def test_analyst_prompt_stored_in_state(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.analyst_prompt == stub_llm.readonly_calls[0]

    def test_analyst_prompt_stored_even_on_llm_error(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_error = RuntimeError("timeout")
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.analyst_prompt is not None

    def test_analyst_prompt_contains_task_description(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.task_description in task_state.analyst_prompt

    def test_analyst_prompt_contains_guidelines_content(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "guidelines.md").write_text("# Coding Standards\n")
        config = {"guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        assert "# Coding Standards" in task_state.analyst_prompt

    def test_analyst_prompt_requires_structured_input_contracts(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool
    ):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert "Structured input contract" in task_state.analyst_prompt
        assert "expected result types" in task_state.analyst_prompt
        assert "generic and expected-type validation APIs" in task_state.analyst_prompt
        assert "clearly labelled structured" in task_state.analyst_prompt
        assert "platform-neutral" in task_state.analyst_prompt
        assert "materially different rejected input" in task_state.analyst_prompt
        assert "classes" in task_state.analyst_prompt

    def test_analyst_prompt_requires_asset_manifest_obligations(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool
    ):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        prompt = task_state.analyst_prompt
        assert "Asset manifest" in prompt
        assert "Reference-only assets may guide implementation" in prompt
        assert "Delivery assets may be used only within the requested scope" in prompt
        assert "source/license/provenance" in prompt

    def test_meta_output_is_retried_and_not_stored(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_results = [BAD_META_PROMPT, VALID_ANALYST_PROMPT]
        agent = _make_agent(stub_llm, file_tool)

        result = agent.run(task_state)

        assert result.success
        assert len(stub_llm.readonly_calls) == 2
        assert task_state.implementation_prompt == VALID_ANALYST_PROMPT
        assert len(task_state.analyst_retry_records) == 1
        assert task_state.analyst_retry_records[0]["will_retry"] is True
        assert "implementation prompt above" in task_state.analyst_retry_records[0]["reason"]
        assert task_state.analyst_retry_records[0]["output"] == BAD_META_PROMPT
        assert "Retry once" in task_state.analyst_retry_records[0]["retry_prompt"]
        assert any(e["action"] == "analyze_retry" for e in task_state.history)

    def test_meta_output_fails_after_retry(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_results = [BAD_META_PROMPT, BAD_META_PROMPT]
        agent = _make_agent(stub_llm, file_tool)

        result = agent.run(task_state)

        assert not result.success
        assert "invalid implementation prompt" in result.message
        assert task_state.implementation_prompt is None
        assert len(task_state.analyst_retry_records) == 2
        assert task_state.analyst_retry_records[-1]["will_retry"] is False
        assert task_state.analyst_retry_records[-1]["output"] == BAD_META_PROMPT
        assert "retry_prompt" not in task_state.analyst_retry_records[-1]
        assert any(e["action"] == "analyze_failed" for e in task_state.history)

    def test_structured_meta_output_without_actionable_detail_is_rejected(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool
    ):
        prompt = (
            "1. Context: the task is complete\n"
            "2. Required changes: no further action is needed\n"
            "3. Architecture constraints: none\n"
            "4. Hard rules: none\n"
            "5. Cleanup: none\n"
            "6. Acceptance criteria: complete"
        )
        stub_llm.readonly_results = [prompt, prompt]
        agent = _make_agent(stub_llm, file_tool)

        result = agent.run(task_state)

        assert not result.success
        assert task_state.implementation_prompt is None
        assert len(task_state.analyst_retry_records) == 2
        assert "task is complete" in task_state.analyst_retry_records[0]["reason"]

    def test_concise_actionable_prompt_is_allowed(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "Add subtract(a, b) to src/calculator.py."
        agent = _make_agent(stub_llm, file_tool)

        result = agent.run(task_state)

        assert result.success
        assert task_state.implementation_prompt == "Add subtract(a, b) to src/calculator.py."

    def test_actionable_prompt_can_include_required_meta_phrase_copy(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool
    ):
        prompt = (
            "1. Context: onboarding completion screen\n"
            '2. Required changes: update src/onboarding/banner.py to display "Task is complete" '
            "as the exact user-visible completion copy from the task\n"
            "3. Architecture constraints: follow existing string resource conventions\n"
            "4. Hard rules: keep the change scoped to the completion banner\n"
            "5. Cleanup: no dead production code expected\n"
            "6. Acceptance criteria: the completion banner renders the required copy"
        )
        stub_llm.readonly_result = prompt
        agent = _make_agent(stub_llm, file_tool)

        result = agent.run(task_state)

        assert result.success
        assert len(stub_llm.readonly_calls) == 1
        assert task_state.implementation_prompt == prompt
        assert task_state.analyst_retry_records == []


class TestAnalystGatherGuidelines:
    def test_reads_configured_context_files(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "guidelines.md").write_text("# Project Guidelines\n")
        config = {"guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        prompt_sent = stub_llm.readonly_calls[0]
        assert "guidelines.md" in prompt_sent
        assert "# Project Guidelines" in prompt_sent

    def test_missing_context_file_skipped_silently(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        config = {"guidelines": {"context_files": ["missing.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        result = agent.run(task_state)
        assert result.success

    def test_context_file_truncated_to_max_chars(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "big.md").write_text("X" * 10000)
        config = {"guidelines": {"context_files": ["big.md"], "max_file_chars": 100}}
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        prompt_sent = stub_llm.readonly_calls[0]
        assert "X" * 100 in prompt_sent
        assert "X" * 101 not in prompt_sent
        assert "truncated" in prompt_sent
        assert "big.md" in prompt_sent


class TestAnalystAgentSecurityPrefix:
    def test_security_prefix_prepended(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        _make_agent(stub_llm, file_tool).run(task_state)
        assert stub_llm.readonly_calls[0].startswith(AGENT_SECURITY_PREFIX)
