"""Tests for agents/analyst_agent.py — AnalystAgent."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from agents.analyst_agent import AnalystAgent
from agents.base_agent import AGENT_SECURITY_PREFIX
from tests.conftest import StubLLMClient
from core.state import TaskState


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
        stub_llm.readonly_result = "1. Context: feature module\n2. Required changes: none"
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
        stub_llm.readonly_result = "some text\n⚠️ Missing field in API\n⚠️ No string key"
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert len(task_state.analyst_warnings) == 2
        assert "Missing field in API" in task_state.analyst_warnings[0]

    def test_record_added_on_success(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "the prompt"
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert any(e["action"] == "analyze" for e in task_state.history)

    def test_result_data_contains_prompt(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "the prompt"
        agent = _make_agent(stub_llm, file_tool)
        result = agent.run(task_state)
        assert result.data.get("implementation_prompt") == "the prompt"

    def test_analyst_prompt_stored_in_state(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "the prompt"
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.analyst_prompt == stub_llm.readonly_calls[0]

    def test_analyst_prompt_stored_even_on_llm_error(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_error = RuntimeError("timeout")
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.analyst_prompt is not None

    def test_analyst_prompt_contains_task_description(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "the prompt"
        agent = _make_agent(stub_llm, file_tool)
        agent.run(task_state)
        assert task_state.task_description in task_state.analyst_prompt

    def test_analyst_prompt_contains_guidelines_content(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "guidelines.md").write_text("# Coding Standards\n")
        config = {"guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = "the prompt"
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        assert "# Coding Standards" in task_state.analyst_prompt


class TestAnalystGatherGuidelines:
    def test_reads_configured_context_files(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "guidelines.md").write_text("# Project Guidelines\n")
        config = {"guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = "prompt"
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        prompt_sent = stub_llm.readonly_calls[0]
        assert "guidelines.md" in prompt_sent
        assert "# Project Guidelines" in prompt_sent

    def test_missing_context_file_skipped_silently(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        config = {"guidelines": {"context_files": ["missing.md"], "max_file_chars": 5000}}
        stub_llm.readonly_result = "prompt"
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        result = agent.run(task_state)
        assert result.success

    def test_context_file_truncated_to_max_chars(
        self, stub_llm: StubLLMClient, task_state: TaskState, file_tool, tmp_project: Path
    ):
        (tmp_project / "big.md").write_text("X" * 10000)
        config = {"guidelines": {"context_files": ["big.md"], "max_file_chars": 100}}
        stub_llm.readonly_result = "prompt"
        agent = _make_agent(stub_llm, file_tool, project_config=config)
        agent.run(task_state)
        prompt_sent = stub_llm.readonly_calls[0]
        assert "X" * 100 in prompt_sent
        assert "X" * 101 not in prompt_sent
        assert "truncated" in prompt_sent
        assert "big.md" in prompt_sent


class TestAnalystAgentSecurityPrefix:
    def test_security_prefix_prepended(self, stub_llm: StubLLMClient, task_state: TaskState, file_tool):
        stub_llm.readonly_result = "the prompt"
        _make_agent(stub_llm, file_tool).run(task_state)
        assert stub_llm.readonly_calls[0].startswith(AGENT_SECURITY_PREFIX)
