"""Generated-output ownership is shared across roles and build platforms."""

from __future__ import annotations

import pytest

from agents.build_guidance import pipeline_sync_guidance
from core.state import TaskState
from core.validation_coverage import INTERNAL_PIPELINE_CONFIG_KEY
from tests.test_analyst_agent import VALID_ANALYST_PROMPT, _make_agent as analyst
from tests.test_planner_agent import _make_agent as planner
from tests.test_security_reviewer_agent import _make_agent as security


@pytest.mark.parametrize("platform", ["node", "cargo", "gradle-android", "gradle-jvm", "python"])
def test_platform_neutral_guidance_and_effective_build_flag(platform):
    guidance = pipeline_sync_guidance({"project": {"build_tool": platform}})
    assert "before enabled reviews" in guidance
    assert "Do not manually synthesize or edit" in guidance
    assert all(name not in guidance for name in ("Gradle", "Cargo", "npm", "gradle.lockfile"))
    disabled = pipeline_sync_guidance({"run_build": True, INTERNAL_PIPELINE_CONFIG_KEY: {"run_build": False}})
    assert "Build validation is disabled" in disabled
    assert "before enabled reviews" not in disabled


@pytest.mark.parametrize("role", ["analyst", "planner", "security"])
def test_planning_and_security_prompts_include_sync_ownership(stub_llm, file_tool, role):
    state = TaskState(task_id="sync", task_description="Change dependencies", implementation_prompt="Implement")
    if role == "analyst":
        state.implementation_prompt = None
        stub_llm.readonly_result = VALID_ANALYST_PROMPT
        result = analyst(stub_llm, file_tool).run(state)
        prompt = state.analyst_prompt
    elif role == "planner":
        stub_llm.generate_result = "SINGLE_PASS"
        result = planner(stub_llm).run(state)
        prompt = state.planner_prompt
    else:
        stub_llm.readonly_result = "APPROVED"
        state.files_changed = ["src/main.py"]
        result = security(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
    assert result.success
    assert "Pipeline-owned build outputs" in prompt
    assert "Do not ask implementation or fix agents to run build tooling" in prompt
