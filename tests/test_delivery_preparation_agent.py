from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.base_agent import AGENT_SECURITY_PREFIX, READONLY_AGENT_PREFIX
from agents.delivery_preparation_agent import (
    DeliveryPreparationAgent,
    DeliveryPreparationAgentError,
)
from core.delivery_authoring import DeliveryAuthoringDraft, DeliveryAuthoringParseError


class CapturingLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.system_prompts: list[str] = []
        self.prompts: list[str] = []
        self.readonly_agent_calls: list[str] = []
        self.agent_calls: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.system_prompts.append(system)
        self.prompts.append(user)
        return self.output

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.readonly_agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self.agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")


class FailingLLM:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.system_prompts: list[str] = []
        self.prompts: list[str] = []
        self.readonly_agent_calls: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.system_prompts.append(system)
        self.prompts.append(user)
        raise self.error

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.readonly_agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")


def _unit_markdown(title: str = "Foundation") -> str:
    return f"""# {title}

## Goal

Prepare the delivery unit.

## Current behavior

The project does not yet have this delivery slice.

## Desired behavior

The delivery slice is described as product behavior with observable outcomes.

## Acceptance criteria

- The delivered behavior has a deterministic success path.

## Security and privacy

- Do not expose raw prompts, provider output, or source excerpts.

## Tests

- Cover generated delivery plan authoring output.

## Reviewer focus

- Check the behavior boundary and privacy-safe diagnostics.

## Out of scope

- Do not write delivery plan files in this unit.

## Validation

- `python3 -m pytest tests/test_delivery_preparation_agent.py`
"""


def _authoring_output(*, planning_mode: str | None = "fixed_window", warnings: list[str] | None = None) -> str:
    data = {
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "units": [
            {
                "id": "foundation",
                "title": "Prepare foundation",
                "depends_on": [],
                "task_markdown": _unit_markdown("Foundation"),
                "scope_paths": ["agents"],
                "estimated_size": "small",
                "risk_tags": ["automation_behavior"],
                "budget": {"max_planner_steps": 3},
            }
        ],
    }
    if planning_mode is not None:
        data["planning_mode"] = planning_mode
    if warnings is not None:
        data["warnings"] = warnings
    return json.dumps(data)


def _author_delivery_plan(
    agent: DeliveryPreparationAgent,
    *,
    tmp_path: Path,
    task_path: str | Path = ".sikula/tasks/team-invites.md",
    project_context: dict | None = None,
    audit_records: list[dict] | None = None,
) -> DeliveryAuthoringDraft:
    return agent.author_delivery_plan(
        task_description="Add team invite authoring without exposing raw task text.",
        task_path=task_path,
        plan_id="team-invites",
        project_root=tmp_path,
        output_dir=".sikula/delivery/team-invites",
        project_context=project_context,
        audit_recorder=None if audit_records is None else audit_records.append,
    )


def test_author_delivery_plan_calls_generate_and_records_success(tmp_path: Path) -> None:
    (tmp_path / "guidelines.md").write_text("# Project Guidelines\nKeep prompts platform-neutral.\n")
    llm = CapturingLLM(_authoring_output(warnings=["Review before writing artifacts."]))
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={
            "project": {"platform": "backend", "language": "Python"},
            "guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000},
        },
    )
    audit_records: list[dict] = []

    draft = _author_delivery_plan(
        agent,
        tmp_path=tmp_path,
        project_context={"validation_commands": ["python3 -m pytest tests/"], "delivery": {"mode": "slice"}},
        audit_records=audit_records,
    )

    assert draft.plan_id == "team-invites"
    assert draft.planning_mode == "fixed_window"
    assert draft.warnings == ["Review before writing artifacts."]
    assert [unit.id for unit in draft.units] == ["foundation"]
    assert draft.units[0].scope_paths == ["agents"]
    assert draft.units[0].estimated_size == "small"
    assert draft.units[0].risk_tags == ["automation_behavior"]
    assert draft.units[0].budget is not None
    assert draft.units[0].budget.to_dict() == {"max_planner_steps": 3}
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []
    assert llm.agent_calls == []
    prompt = llm.prompts[0]
    assert prompt.startswith(AGENT_SECURITY_PREFIX)
    assert READONLY_AGENT_PREFIX in prompt
    assert "Project stack: backend / Python" in prompt
    assert "Selected delivery plan id: team-invites" in prompt
    assert "Source task file: .sikula/tasks/team-invites.md" in prompt
    assert "Delivery output directory: .sikula/delivery/team-invites" in prompt
    assert "- guidelines.md" in prompt
    assert "# Project Guidelines" in prompt
    assert '"python3 -m pytest tests/"' in prompt
    assert "Do not include writer-facing path fields" in prompt
    assert "Do not write, edit, delete, move, rename, format, or create files." in prompt
    assert "Prefer small units with one primary production surface" in prompt
    assert "Security and privacy" in prompt
    assert "Security/privacy notes" not in prompt
    assert "Security and privacy, Reviewer focus" in prompt
    assert "Security and privacy, Tests" not in prompt
    assert "where applicable" not in prompt
    assert "must include all of these exact contract-ready section headings" in prompt
    assert "Validation sections must include explicit commands" in prompt
    assert "UI/API/CLI behavior" in prompt
    assert "data model or persistence changes" in prompt
    assert "automation or prompt-driven behavior" in prompt
    assert "External provider, tool, or integration boundary changes" in prompt
    assert '"estimated_size": "small"' in prompt
    assert '"risk_tags": ["cli_surface"]' in prompt
    assert '"budget": {"max_planner_steps": 3, "max_changed_files": 8}' in prompt
    assert "Add team invite authoring without exposing raw task text." in prompt
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["round_index"] == 1
    assert record["prompt"] == prompt
    assert record["raw_output"] == llm.output
    assert record["parsed"] == {
        "status": "parsed",
        "plan_id": "team-invites",
        "unit_ids": ["foundation"],
        "unit_count": 1,
        "planning_mode": "fixed_window",
        "warnings": ["Review before writing artifacts."],
    }
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_author_delivery_plan_handles_absent_context_and_audit_recorder(tmp_path: Path) -> None:
    llm = CapturingLLM(_authoring_output(planning_mode=None))
    agent = DeliveryPreparationAgent(llm=llm)

    draft = _author_delivery_plan(agent, tmp_path=tmp_path)

    assert draft.planning_mode is None
    assert draft.warnings == []
    prompt = llm.prompts[0]
    assert "Project stack: software" in prompt
    assert "- README.md" in prompt
    assert "No configured guidelines content found." in prompt
    assert "Project context:\n```json\n{}" in prompt
    assert "Configured validation commands:\n```json\n[]" in prompt


def test_author_delivery_plan_filters_guidelines_paths_and_truncates_content(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("abcdef")
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={
            "guidelines": {
                "context_files": [None, "", "/absolute.md", "../outside.md", "missing.md", "good.md"],
                "max_file_chars": 3,
            }
        },
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "=== good.md ===\nabc\n... [truncated; inspect good.md for full content]" in prompt
    assert "=== /absolute.md ===" not in prompt
    assert "=== ../outside.md ===" not in prompt
    assert "=== missing.md ===" not in prompt


def test_author_delivery_plan_honors_zero_guidelines_char_limit(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("SECRET_GUIDELINE_CONTEXT")
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={"guidelines": {"context_files": ["good.md"], "max_file_chars": 0}},
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "=== good.md ===\n" in prompt
    assert "SECRET_GUIDELINE_CONTEXT" not in prompt
    assert "[truncated; inspect good.md for full content]" not in prompt


def test_author_delivery_plan_uses_default_guidelines_limit_when_config_value_is_invalid(tmp_path: Path) -> None:
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={"guidelines": {"context_files": ["missing.md"], "max_file_chars": "many"}},
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "- missing.md" in prompt
    assert "No configured guidelines content found." in prompt


def test_author_delivery_plan_redacts_outside_project_task_path_in_prompt(tmp_path: Path) -> None:
    outside_task = tmp_path.parent / "secret-task.md"
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(llm=llm)

    _author_delivery_plan(agent, tmp_path=tmp_path, task_path=outside_task)

    prompt = llm.prompts[0]
    assert "Source task file: <outside-project>" in prompt
    assert str(outside_task) not in prompt


def test_author_delivery_plan_records_parse_failure_and_raises_safe_error(tmp_path: Path) -> None:
    llm = CapturingLLM("Assistant draft:\nSECRET_PROVIDER_OUTPUT")
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert exc_info.value.code == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert "Add team invite authoring" not in str(exc_info.value)
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["raw_output"] == llm.output
    assert record["parsed"]["status"] == "failed"
    assert record["parsed"]["error_type"] == "DeliveryAuthoringParseError"
    assert record["parsed"]["error_code"] == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in record["parsed"]["error"]


def test_author_delivery_plan_wraps_provider_failure_with_safe_exception(tmp_path: Path) -> None:
    llm = FailingLLM(RuntimeError("provider timeout with SECRET_PROVIDER_OUTPUT"))
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryPreparationAgentError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert str(exc_info.value) == "Delivery authoring assistant failed."
    assert exc_info.value.__cause__ is None
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["raw_output"] is None
    assert record["parsed"] == {
        "status": "failed",
        "error_type": "RuntimeError",
        "error_code": "delivery_prepare.authoring_failed",
        "error": "provider timeout with SECRET_PROVIDER_OUTPUT",
    }
