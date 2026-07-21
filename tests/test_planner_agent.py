"""Tests for agents/planner_agent.py — PlannerAgent and _parse_plan."""

from __future__ import annotations

from pathlib import Path

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.planner_agent import PlannerAgent, _parse_plan
from core.state import TaskState
from tests.conftest import StubLLMClient


def _make_agent(llm: StubLLMClient, project_config: dict | None = None, tools: dict | None = None) -> PlannerAgent:
    return PlannerAgent(llm=llm, tools=tools or {}, project_config=project_config or {})


class SequentialGenerateLLM(StubLLMClient):
    def __init__(self, results: list[str]) -> None:
        super().__init__()
        self.results = results

    def generate(self, system: str, user: str) -> str:
        self.generate_calls.append((system, user))
        if not self.results:
            raise AssertionError("No generate result left")
        return self.results.pop(0)


class TestParsePlan:
    def test_parses_numbered_list(self):
        output = "1. Create ViewModel\n2. Update Repository\n3. Wire UI"
        assert _parse_plan(output) == ["Create ViewModel", "Update Repository", "Wire UI"]

    def test_ignores_non_step_lines(self):
        output = "Here is the plan:\n1. Step one\nsome notes\n2. Step two"
        assert _parse_plan(output) == ["Step one", "Step two"]

    def test_empty_output_returns_empty(self):
        assert _parse_plan("") == []

    def test_single_item_list(self):
        assert _parse_plan("1. Only step") == ["Only step"]

    def test_strips_step_text(self):
        assert _parse_plan("1.   Padded step   ") == ["Padded step"]


class TestPlannerAgentRun:
    def test_no_implementation_prompt_returns_failure(self):
        state = TaskState(task_id="t1", task_description="task")
        agent = _make_agent(StubLLMClient())
        result = agent.run(state)
        assert not result.success
        assert "implementation prompt" in result.message.lower()

    def test_single_pass_decision(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert result.success
        assert "Single-pass" in result.message
        assert state.plan == []
        assert state.plan_decided is True

    def test_single_pass_case_insensitive(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "single_pass"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert result.success
        assert state.plan == []
        assert state.plan_decided is True

    def test_multi_step_plan(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "1. Add ViewModel\n2. Update UI\n3. Wire DI"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert result.success
        assert state.plan == ["Add ViewModel", "Update UI", "Wire DI"]
        assert state.current_step == 0
        assert state.plan_decided is True
        assert result.data["steps"] == state.plan

    def test_fewer_than_two_steps_falls_back_to_single_pass(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "1. Only one step"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert result.success
        assert state.plan == []
        assert state.plan_decided is True

    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient):
        stub_llm.generate_error = RuntimeError("model unavailable")
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert not result.success
        assert "model unavailable" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient):
        stub_llm.generate_error = RuntimeError("model unavailable")
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        agent.run(state)
        assert any(e["action"] == "plan_failed" for e in state.history)

    def test_empty_output_returns_failure(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = ""
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert not result.success

    def test_max_steps_passed_to_prompt(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3}}
        agent = _make_agent(stub_llm, project_config=config)
        agent.run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert "3" in system_prompt

    def test_output_format_contract_in_prompt(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 4}}
        _make_agent(stub_llm, project_config=config).run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert "Output format contract:" in system_prompt
        assert "numbered list with 2–4 items" in system_prompt
        assert "Each numbered item must be exactly one physical line" in system_prompt
        assert "Do not emit numbered sub-items" in system_prompt
        assert "complete compile-safe implementation step" in system_prompt

    def test_over_max_steps_retries_and_accepts_valid_plan(self):
        llm = SequentialGenerateLLM(
            [
                "1. A\n2. B\n3. C\n4. D",
                "1. Add ViewModel and routes\n2. Wire UI and styles\n3. Add browser history behavior",
            ]
        )
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3}}

        result = _make_agent(llm, project_config=config).run(state)

        assert result.success
        assert state.plan == [
            "Add ViewModel and routes",
            "Wire UI and styles",
            "Add browser history behavior",
        ]
        assert state.plan_decided is True
        assert len(llm.generate_calls) == 2
        assert "previous planner output was rejected" in llm.generate_calls[1][1]
        assert "parsed as 4" in llm.generate_calls[1][1]
        assert len(state.planner_retry_records) == 1
        retry = state.planner_retry_records[0]
        assert retry["reason"] == "planner output parsed as 4 steps, exceeding planner.max_steps=3"
        assert retry["parsed_step_count"] == 4
        assert retry["max_steps"] == 3
        assert retry["will_retry"] is True
        assert retry["output"] == "1. A\n2. B\n3. C\n4. D"
        assert "2–3 complete" in retry["retry_prompt"]
        assert any(record["action"] == "plan_retry" for record in state.history)

    def test_over_max_steps_retry_can_choose_single_pass(self):
        llm = SequentialGenerateLLM(["1. A\n2. B\n3. C\n4. D", "SINGLE_PASS"])
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3}}

        result = _make_agent(llm, project_config=config).run(state)

        assert result.success
        assert state.plan == []
        assert state.plan_decided is True
        assert len(llm.generate_calls) == 2
        assert len(state.planner_retry_records) == 1

    def test_delivery_unit_preserves_oversized_plan_without_retry(self):
        llm = SequentialGenerateLLM(["1. A\n2. B\n3. C\n4. D"])
        state = TaskState(
            task_id="t1",
            task_description="task",
            implementation_prompt="do x",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 1},
        )
        config = {"planner": {"max_steps": 3}}

        result = _make_agent(llm, project_config=config).run(state)

        assert result.success
        assert state.plan == ["A", "B", "C", "D"]
        assert state.plan_decided is True
        assert len(llm.generate_calls) == 1
        assert state.planner_retry_records == []
        assert state.planner_output == "1. A\n2. B\n3. C\n4. D"
        assert any(record["action"] == "plan" for record in state.history)

    def test_delivery_unit_accepts_plan_within_explicit_two_step_budget(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "1. A\n2. B"
        state = TaskState(
            task_id="t1",
            task_description="task",
            implementation_prompt="do x",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 2},
        )

        result = _make_agent(stub_llm).run(state)

        assert result.success
        assert state.plan == ["A", "B"]
        assert state.planner_retry_records == []

    def test_single_pass_marker_does_not_bypass_over_limit_numbered_plan(self):
        llm = SequentialGenerateLLM(["SINGLE_PASS\n1. A\n2. B\n3. C\n4. D", "SINGLE_PASS"])
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3}}

        result = _make_agent(llm, project_config=config).run(state)

        assert result.success
        assert state.plan == []
        assert state.plan_decided is True
        assert len(llm.generate_calls) == 2
        assert state.planner_retry_records[0]["parsed_step_count"] == 4

    def test_over_max_steps_retry_failure_does_not_decide_plan(self):
        llm = SequentialGenerateLLM(["1. A\n2. B\n3. C\n4. D", "1. E\n2. F\n3. G\n4. H"])
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3}}

        result = _make_agent(llm, project_config=config).run(state)

        assert not result.success
        assert "planner retry exhausted" in result.message
        assert state.plan == []
        assert state.plan_decided is False
        assert state.current_step == 0
        assert len(state.planner_retry_records) == 2
        assert state.planner_retry_records[0]["will_retry"] is True
        assert state.planner_retry_records[1]["will_retry"] is False
        assert "retry_prompt" not in state.planner_retry_records[1]
        assert any(record["action"] == "plan_rejected" for record in state.history)
        assert any(record["action"] == "plan_failed" for record in state.history)

    def test_invalid_max_steps_fails_before_llm_call(self, stub_llm: StubLLMClient):
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 1}}

        result = _make_agent(stub_llm, project_config=config).run(state)

        assert not result.success
        assert "planner.max_steps" in result.message
        assert stub_llm.generate_calls == []
        assert state.plan_decided is False

    def test_non_integer_max_steps_fails_before_llm_call(self, stub_llm: StubLLMClient):
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        config = {"planner": {"max_steps": 3.5}}

        result = _make_agent(stub_llm, project_config=config).run(state)

        assert not result.success
        assert "planner.max_steps" in result.message
        assert stub_llm.generate_calls == []

    def test_compile_dependencies_rule_in_prompt(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        _make_agent(stub_llm).run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert "every immediate compile dependency" in system_prompt
        assert "resource or localization keys/IDs" in system_prompt
        assert "dependency-injection" in system_prompt
        assert "service registrations" in system_prompt
        assert "interface/trait/protocol/abstract methods" in system_prompt
        assert "If step N references it, step N must also create or update it" in system_prompt
        assert "choose SINGLE_PASS" in system_prompt

    def test_record_added_on_success(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        agent = _make_agent(stub_llm)
        agent.run(state)
        assert any(e["action"] == "plan" for e in state.history)

    def test_planner_prompt_saved_to_state(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        _make_agent(stub_llm).run(state)
        assert state.planner_prompt is not None
        assert "do x" in state.planner_prompt
        assert state.planner_output == "SINGLE_PASS"


class TestPlannerAgentExtraRules:
    def test_extra_rules_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.generate_result = "SINGLE_PASS"
        (tmp_project / "planner_rules.md").write_text("Always split UI and business logic.")
        config = {"planner": {"extra_rules": "planner_rules.md"}}
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        _make_agent(stub_llm, project_config=config, tools={"file": file_tool}).run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert "Always split UI and business logic." in system_prompt
        assert "Project-specific rules" in system_prompt

    def test_extra_rules_absent_when_not_configured(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        _make_agent(stub_llm).run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert "Project-specific rules" not in system_prompt


class TestPlannerAgentSecurityPrefix:
    def test_security_prefix_prepended_to_system_prompt(self, stub_llm: StubLLMClient):
        stub_llm.generate_result = "SINGLE_PASS"
        state = TaskState(task_id="t1", task_description="task", implementation_prompt="do x")
        _make_agent(stub_llm).run(state)
        system_prompt = stub_llm.generate_calls[0][0]
        assert system_prompt.startswith(AGENT_SECURITY_PREFIX)
        assert state.planner_prompt.startswith(AGENT_SECURITY_PREFIX)
