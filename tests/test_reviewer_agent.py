"""Tests for agents/reviewer_agent.py — ReviewerAgent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.base_agent import AGENT_SECURITY_PREFIX, READONLY_AGENT_PREFIX
from agents.reviewer_agent import (
    ReviewerAgent,
    _MAX_DIFF_CHARS,
)
from core.delivery_constraint_context import delivery_constraint_context_fingerprint
from core.state import TaskState
from core.validation_coverage import (
    configured_validation_commands,
    extract_validation_commands,
    validation_coverage_gaps,
    validation_command_coverage,
    validation_commands_equivalent,
)
from tools.base_tool import ToolResult
from tests.conftest import StubLLMClient


def _make_state(**kwargs) -> TaskState:
    defaults = {
        "task_id": "t1",
        "task_description": "Add login screen",
        "implementation_prompt": "Create LoginActivity with email/password fields",
        "files_changed": ["app/src/LoginActivity.kt"],
    }
    defaults.update(kwargs)
    return TaskState(**defaults)


def _make_agent(
    llm: StubLLMClient,
    file_tool=None,
    git_tool=None,
    project_config: dict | None = None,
) -> ReviewerAgent:
    tools: dict = {}
    if file_tool is not None:
        tools["file"] = file_tool
        if project_config is None:
            project_config = {
                "project": {"root_path": str(file_tool._root)},
                "sandbox": {"allowed_write_paths": ["."]},
            }
    if git_tool is not None:
        tools["git"] = git_tool
    return ReviewerAgent(llm=llm, tools=tools, project_config=project_config or {})


def _add_delivery_constraint_context(state: TaskState) -> None:
    state.delivery_plan_id = "demo-plan"
    state.delivery_unit_id = "feature-unit"
    state.delivery_plan_path = ".sikula/delivery/demo-plan/plan.yaml"
    state.delivery_constraint_context_schema_version = 1
    state.delivery_source_task = {
        "path": ".sikula/tasks/demo.md",
        "sha256": f"sha256:{'a' * 64}",
    }
    state.delivery_inherited_constraints = [
        {
            "id": "external-read-only",
            "kind": "authoritative_read_only_dependency",
            "summary": "Treat the external repository as read-only evidence.",
            "unit_ids": ["feature-unit"],
            "disposition": "preserved",
        }
    ]
    state.delivery_constraint_context_fingerprint = delivery_constraint_context_fingerprint(
        schema_version=state.delivery_constraint_context_schema_version,
        plan_id=state.delivery_plan_id,
        unit_id=state.delivery_unit_id,
        plan_path=state.delivery_plan_path,
        source_task=state.delivery_source_task,
        constraints=state.delivery_inherited_constraints,
    )


def _add_delivery_write_scope(state: TaskState) -> None:
    state.delivery_write_scope_schema_version = 2
    state.delivery_write_scope_mode = "unit_explicit"
    state.delivery_declared_write_paths = ["src"]
    state.delivery_declared_write_exact_file_paths = []
    state.delivery_effective_write_paths = ["src"]
    state.delivery_effective_write_exact_file_paths = []
    state.delivery_runtime_write_scope_binding = {
        "schema_version": 1,
        "status": "bound",
        "roots": [{"path": "src", "resolved_path": "src", "exact_file": False}],
    }


def _delivery_approval_output() -> str:
    return json.dumps(
        {
            "sikula_disposition_schema_version": 1,
            "disposition": "approved",
            "summary": "No blocking correctness issues found.",
        }
    )


class TestReviewerAgentGuards:
    def test_no_implementation_prompt_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(implementation_prompt=None)
        agent = _make_agent(stub_llm, file_tool=file_tool)
        result = agent.run(state)
        assert not result.success
        assert "implementation prompt" in result.message.lower()

    def test_no_files_changed_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state(files_changed=[])
        agent = _make_agent(stub_llm, file_tool=file_tool)
        result = agent.run(state)
        assert not result.success
        assert "changed files" in result.message.lower()

    def test_no_file_tool_returns_failure(self, stub_llm: StubLLMClient):
        state = _make_state()
        agent = _make_agent(stub_llm)
        result = agent.run(state)
        assert not result.success
        assert "FileTool" in result.message

    def test_malformed_modern_constraint_context_fails_before_provider_call(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        state.delivery_constraint_context_fingerprint = None

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert not result.success
        assert "fingerprint_invalid" in result.message
        assert stub_llm.readonly_calls == []
        assert state.review_cycle_records == []
        assert state.review_issues == []
        assert state.history[-1]["action"] == "delivery_constraint_context_rejected"

    def test_invalid_active_write_scope_fails_before_provider_call(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        _add_delivery_write_scope(state)
        project_config = {
            "project": {"root_path": str(file_tool._root)},
            "sandbox": {"allowed_write_paths": ["../outside"]},
        }

        result = _make_agent(
            stub_llm,
            file_tool=file_tool,
            project_config=project_config,
        ).run(state)

        assert not result.success
        assert "runtime_intersection_invalid" in result.message
        assert stub_llm.readonly_calls == []
        assert state.review_cycle_records == []
        assert state.history[-1]["action"] == "delivery_write_scope_context_rejected"


class TestReviewerAgentApproval:
    def test_approved_output_sets_flag(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "Callers verified: none\nAPPROVED"
        state = _make_state()
        state.review_issues = ["leftover issue from previous pass"]
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        result = agent.run(state)
        assert result.success
        assert state.review_approved is True
        assert state.review_issues == []

    def test_approved_case_insensitive(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "approved"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        assert state.review_approved is True

    @pytest.mark.parametrize(
        "decorated",
        [
            "**APPROVED**",
            "## APPROVED",
            "> APPROVED",
            "[APPROVED]",
            "1. APPROVED",
            "__APPROVED__",
        ],
    )
    def test_approved_detected_regardless_of_decoration(
        self, decorated: str, stub_llm: StubLLMClient, file_tool, git_tool
    ):
        stub_llm.readonly_result = f"{decorated}"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        assert state.review_approved is True

    def test_approved_mid_output_does_not_approve(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = (
            "Callers verified: none\nAPPROVED\n\n## Issues\n\n### Missing null check\nFile: x\nFix: y"
        )
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        result = agent.run(state)
        assert not result.success
        assert state.review_approved is False

    def test_issues_output_sets_review_issues(self, stub_llm: StubLLMClient, file_tool, git_tool):
        issues = "## Issues\n\n### Missing null check\nFile: app/Login.kt\nProblem: x\nFix: y"
        stub_llm.readonly_result = issues
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        result = agent.run(state)
        assert not result.success
        assert state.review_approved is False
        assert issues in state.review_issues

    def test_output_appended_to_review_history(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        assert "APPROVED" in state.review_cycle_records[0]["reviewer_output"]

    def test_approved_logged(self, stub_llm: StubLLMClient, file_tool, git_tool, caplog):
        import logging

        stub_llm.readonly_result = "Callers verified.\n\nAPPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        with caplog.at_level(logging.INFO, logger="agents.reviewer_agent"):
            agent.run(state)
        assert any("Review approved" in r.message for r in caplog.records)

    def test_issues_logged(self, stub_llm: StubLLMClient, file_tool, git_tool, caplog):
        import logging

        issues = "## Issues\n\n### Missing null check\nFile: x\nFix: y"
        stub_llm.readonly_result = issues
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        with caplog.at_level(logging.INFO, logger="agents.reviewer_agent"):
            agent.run(state)
        assert any("Review issues" in r.message for r in caplog.records)

    def test_record_added_on_approval(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        assert any(e["action"] == "review" and e["result"] == "approved" for e in state.history)


class TestReviewerAgentErrors:
    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_error = RuntimeError("LLM timeout")
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        result = agent.run(state)
        assert not result.success
        assert "LLM timeout" in result.message

    def test_llm_error_recorded_in_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_error = RuntimeError("LLM timeout")
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert any(e["action"] == "review_failed" for e in state.history)
        assert state.review_cycle_records[-1]["reviewer_output"] is None
        assert state.review_cycle_records[-1]["error"] == "LLM timeout"

    def test_empty_output_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = " \n "
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        result = agent.run(state)
        assert not result.success
        assert "empty" in result.message
        assert result.data["disposition_parse_error"] == "delivery_disposition.output_empty"
        assert state.review_cycle_records[-1]["disposition_parse_error"] == "delivery_disposition.output_empty"

    def test_empty_delivery_output_is_included_in_protocol_retry_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_results = ["", TestReviewerDeliveryDispositions._issue_output("fix_in_scope")]
        agent = _make_agent(stub_llm, file_tool=file_tool)

        first = agent.run(state)
        agent.run(state)

        assert first.data["disposition_parse_error"] == "delivery_disposition.output_empty"
        assert "delivery_disposition.output_empty" in stub_llm.readonly_calls[1]


class TestReviewerDeliveryDispositions:
    @staticmethod
    def _issue_output(disposition: str) -> str:
        return (
            "## Issues\n\n### Boundary finding\nFile: app/src/LoginActivity.kt\n"
            "Problem: The required correction crosses a delivery boundary.\n"
            "Fix: Apply the disposition-specific recovery.\n\n"
            + json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": disposition,
                    "summary": "The review found one bounded delivery issue.",
                }
            )
        )

    def test_fix_in_scope_authorizes_existing_bounded_fix_signal(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = self._issue_output("fix_in_scope")

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert result.data["issues"] == stub_llm.readonly_result
        assert result.data["disposition"]["disposition"] == "fix_in_scope"
        assert state.delivery_stop_disposition is None
        assert state.review_cycle_records[-1]["disposition"]["recommended_action"] == "bounded_fix"

    def test_explicit_approval_disposition_approves_delivery_child(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = json.dumps(
            {
                "sikula_disposition_schema_version": 1,
                "disposition": "approved",
                "summary": "No blocking correctness issues found.",
            }
        )

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is True
        assert state.review_approved is True
        assert state.review_issues == []
        assert state.review_cycle_records[-1]["disposition"]["recommended_action"] == "continue"

    def test_legacy_approval_signal_does_not_approve_delivery_child(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = "APPROVED"

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert result.data["disposition_parse_error"] == "delivery_disposition.decision_missing"
        assert state.review_approved is False

    @pytest.mark.parametrize(
        ("disposition", "action"),
        [
            ("requires_scope_amendment", "delivery_amend_prepare"),
            ("external_dependency_gap", "external_dependency_follow_up"),
        ],
    )
    def test_terminal_disposition_does_not_authorize_fix_signal(
        self,
        disposition: str,
        action: str,
        stub_llm: StubLLMClient,
        file_tool,
    ):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = self._issue_output(disposition)

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert "issues" not in result.data
        assert state.delivery_stop_disposition is not None
        assert state.delivery_stop_disposition["disposition"] == disposition
        assert state.delivery_stop_disposition["recommended_action"] == action

    def test_free_form_delivery_issue_cannot_authorize_fix(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = (
            "## Issues\n\n### Scope\nFile: app/src/LoginActivity.kt\nProblem: outside scope\n"
            "Fix: requires_scope_amendment"
        )

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert "issues" not in result.data
        assert result.data["disposition_parse_error"] == "delivery_disposition.missing"
        assert state.delivery_stop_disposition is None

    def test_mixed_approval_and_issue_disposition_fails_closed(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = self._issue_output("fix_in_scope") + "\nAPPROVED"

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert "issues" not in result.data
        assert result.data["disposition_parse_error"] == "delivery_disposition.conflicting_decision"
        assert state.review_approved is False

    def test_disposition_without_issue_section_fails_closed(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        state.review_cycle_records.append({"reviewer_output": None})
        stub_llm.readonly_result = self._issue_output("fix_in_scope").replace("## Issues", "Review note")

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert result.data["disposition_parse_error"] == "delivery_disposition.issue_section_missing"
        assert state.delivery_stop_disposition is None

    def test_output_without_issue_or_decision_fails_closed(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = "The review output contains neither a decision nor an issue section."

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success is False
        assert result.data["disposition_parse_error"] == "delivery_disposition.decision_missing"
        assert state.review_approved is False

    def test_disposition_error_is_included_in_next_review_prompt(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        malformed = (
            json.dumps(
                {
                    "sikula_disposition_schema_version": 1,
                    "disposition": "fix_in_scope",
                    "summary": "No blocking correctness issues found.",
                }
            )
            + "\nAPPROVED"
        )
        approved = json.dumps(
            {
                "sikula_disposition_schema_version": 1,
                "disposition": "approved",
                "summary": "No blocking correctness issues found.",
            }
        )
        stub_llm.readonly_results = [malformed, approved]
        agent = _make_agent(stub_llm, file_tool=file_tool)

        first = agent.run(state)
        second = agent.run(state)

        assert first.data["disposition_parse_error"] == "delivery_disposition.position_invalid"
        assert second.success is True
        assert "Sikula protocol correction required" in stub_llm.readonly_calls[1]
        assert "delivery_disposition.position_invalid" in stub_llm.readonly_calls[1]

    def test_delivery_prompt_declares_closed_disposition_contract(self, stub_llm: StubLLMClient, file_tool):
        state = _make_state()
        _add_delivery_constraint_context(state)
        stub_llm.readonly_result = "APPROVED"

        _make_agent(stub_llm, file_tool=file_tool).run(state)

        prompt = stub_llm.readonly_calls[0]
        assert "AUTHORITATIVE ACTIVE DELIVERY WRITE SCOPE" in prompt
        assert '{"kind":"path_prefix","path":"."}' in prompt
        assert "DELIVERY REVIEW DISPOSITION CONTRACT" in prompt
        assert '"disposition":"approved"' in prompt
        assert "replaces the generic APPROVED output instructions" in prompt


class TestReviewerAgentDiff:
    def test_diff_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, git_tool, tmp_project: Path):
        (tmp_project / "src" / "main.py").write_text("# changed\n")
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "main.py" in prompt

    def test_diff_unavailable_fallback_message(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        # No git_tool — diff will be unavailable
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "diff not available" in prompt

    def test_long_diff_truncated(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        big_diff = "+" + "x" * (_MAX_DIFF_CHARS + 1000)
        git_tool.diff_head = MagicMock(return_value=ToolResult(success=True, output=big_diff))
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "truncated" in prompt

    def test_review_diff_state_used_when_set(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_diff="+ added line from PR diff")
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert "added line from PR diff" in stub_llm.readonly_calls[0]

    def test_review_diff_state_truncated(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        big_diff = "+" + "x" * (_MAX_DIFF_CHARS + 500)
        state = _make_state(review_diff=big_diff)
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert "truncated" in stub_llm.readonly_calls[0]

    def test_review_diff_state_does_not_call_git_tool(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        git_tool.diff_head = MagicMock(return_value=ToolResult(success=True, output="should not appear"))
        state = _make_state(review_diff="+ added line from PR diff")
        agent = _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool)
        agent.run(state)
        git_tool.diff_head.assert_not_called()

    def test_review_diff_empty_string_uses_fallback_message(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_diff="")
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert "diff not available" in stub_llm.readonly_calls[0]


class TestReviewerAgentPrompt:
    def test_delivery_prompt_uses_current_narrowed_exact_file_scope(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = _delivery_approval_output()
        state = _make_state()
        _add_delivery_constraint_context(state)
        _add_delivery_write_scope(state)
        project_config = {
            "project": {"root_path": str(file_tool._root)},
            "sandbox": {"allowed_write_paths": ["src/main.py"]},
        }

        result = _make_agent(
            stub_llm,
            file_tool=file_tool,
            project_config=project_config,
        ).run(state)

        assert result.success
        prompt = stub_llm.readonly_calls[0]
        assert '{"kind":"exact_file","path":"src/main.py"}' in prompt
        assert '{"kind":"path_prefix","path":"src"}' not in prompt

    def test_authoritative_constraint_context_is_between_task_and_implementation_prompt(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.readonly_result = _delivery_approval_output()
        state = _make_state(implementation_prompt="Update the local feature implementation.")
        _add_delivery_constraint_context(state)

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success
        prompt = stub_llm.readonly_calls[0]
        context_index = prompt.index("Authoritative inherited delivery constraint context:")
        assert prompt.index("Task description:") < context_index < prompt.index("Implementation prompt:")
        assert "Treat the external repository as read-only evidence." in prompt
        assert '"fingerprint":"{value}"'.format(value=state.delivery_constraint_context_fingerprint) in prompt
        assert "may restrict the task scope but can" in prompt
        assert "never expand it" in prompt
        assert "Report any violation as a" in prompt
        assert "DELIVERY REVIEW DISPOSITION CONTRACT" in prompt
        assert state.review_cycle_records[0]["reviewer_prompt"] == prompt

    def test_legacy_reviewer_prompt_omits_constraint_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()

        result = _make_agent(stub_llm, file_tool=file_tool).run(state)

        assert result.success
        assert "Authoritative inherited delivery constraint context:" not in stub_llm.readonly_calls[0]
        assert "AUTHORITATIVE ACTIVE DELIVERY WRITE SCOPE" not in stub_llm.readonly_calls[0]
        assert "DELIVERY REVIEW DISPOSITION CONTRACT" not in stub_llm.readonly_calls[0]

    def test_re_review_keeps_authoritative_constraint_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_results = [
            "## Issues\n\n### Boundary violation\nFile: src/local.py\nProblem: p\nFix: f",
            _delivery_approval_output(),
        ]
        state = _make_state()
        _add_delivery_constraint_context(state)
        agent = _make_agent(stub_llm, file_tool=file_tool)

        first = agent.run(state)
        second = agent.run(state)

        assert not first.success
        assert second.success
        assert len(stub_llm.readonly_calls) == 2
        assert all(
            "Authoritative inherited delivery constraint context:" in prompt for prompt in stub_llm.readonly_calls
        )

    def test_tech_stack_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        config = {"project": {"platform": "Android", "language": "Kotlin"}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "Android / Kotlin" in stub_llm.readonly_calls[0]

    def test_guidelines_content_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.readonly_result = "APPROVED"
        (tmp_project / "src" / "arch.md").write_text("# Architecture rules\n")
        config = {"guidelines": {"context_files": ["src/arch.md"]}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "src/arch.md" in prompt
        assert "# Architecture rules" in prompt

    def test_structured_input_contract_checks_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Structured input contracts" in prompt
        assert "expected result type mismatches" in prompt
        assert "validates" in prompt
        assert "API that does not know the expected result type" in prompt

    def test_asset_declaration_consistency_checks_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Asset declaration consistency" in prompt
        assert "structured asset declarations" in prompt
        assert "`### Reference assets` / `### Delivery assets`" in prompt
        assert "Reference-only assets must not be copied into" in prompt
        assert "Delivery assets must be used only within the requested task scope" in prompt
        assert "unexpected production asset additions" in prompt

    def test_external_boundary_contract_checks_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "External boundary contract consistency" in prompt
        assert "API clients" in prompt
        assert "object vs. list/array" in prompt
        assert "encoded vs. raw route" in prompt
        assert "even if tests pass" in prompt

    def test_entry_point_async_boundary_checks_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Entry-point and async boundary consistency" in prompt
        assert "user interaction handlers, API or" in prompt
        assert "entry points call the same operation" in prompt
        assert "starts async or deferred work without awaiting" in prompt
        assert "Report unhandled errors or inconsistent" in prompt

    def test_pipeline_prompt_allows_contract_test_weakening_as_production_evidence(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "contract-bearing test was deleted, relaxed" in prompt
        assert "report the production-code issue" in prompt

    def test_lockfile_guidance_avoids_prebuild_validation_record_block(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(files_changed=["Cargo.toml", "Cargo.lock"])
        config = {"project": {"build_tool": "cargo"}}

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "Build-tool-specific policy" in prompt
        assert "Cargo lockfiles" in prompt
        assert "not hand-written by agents" in prompt
        assert "signs of manual fabrication" in prompt
        assert "Do not ask the implementer to run Cargo tooling" in prompt
        assert "do not block solely" in prompt
        assert "sync/build validation record does" in prompt
        assert "not exist yet" in prompt

    def test_lockfile_guidance_is_omitted_for_non_cargo_projects(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(files_changed=["app/src/LoginActivity.kt"])
        config = {"project": {"build_tool": "gradle-android"}}

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "Cargo lockfiles" not in prompt
        assert "Cargo.lock" not in prompt

    def test_validation_pipeline_context_covers_task_commands(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            task_description=(
                "Add config validation.\n\n"
                "Acceptance criteria:\n"
                "- Run `cargo fmt --all -- --check`.\n"
                "- Run `cargo test --workspace --all-features`.\n"
            )
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {
                "test_command": "cargo test --workspace --all-features",
                "checks": [
                    {
                        "name": "fmt",
                        "command": "cargo fmt --all -- --check",
                        "fix_command": "cargo fmt --all",
                    }
                ],
            },
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "Configured validation pipeline" in prompt
        assert "`cargo fmt --all -- --check` -> covered by check/fmt (exact)" in prompt
        assert "`cargo test --workspace --all-features` -> covered by test/tests (exact)" in prompt
        assert "do not ask the implementer to run it manually" in prompt

    def test_validation_pipeline_context_marks_near_match_uncovered(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            task_description=(
                "Add config validation.\n\nAcceptance criteria:\n- Run `cargo test --workspace --all-features`.\n"
            )
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {"test_command": "cargo test"},
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert (
            "`cargo test --workspace --all-features` -> not covered by configured pipeline "
            "(nearest test/tests: `cargo test`; same command family)"
        ) in prompt
        assert "`cargo test --workspace --all-features` -> covered" not in prompt

    def test_validation_pipeline_context_marks_uncovered_task_command(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            task_description="Add export support. Acceptance: run `cargo run -p codegen_tool -- fixtures/`."
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {
                "test_command": "cargo test",
                "checks": [{"name": "fmt", "command": "cargo fmt --check"}],
            },
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "`cargo run -p codegen_tool -- fixtures/` -> not covered by configured pipeline" in prompt
        assert "report a Validation Coverage Gap" in prompt

    def test_validation_pipeline_context_respects_disabled_checks(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(task_description="Format the project. Run `ruff format --check .`.")
        config = {
            "run_checks": False,
            "project": {"build_tool": "python"},
            "build": {"checks": [{"name": "ruff-format", "command": "ruff format --check ."}]},
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "checks=off" in prompt
        assert "check/ruff-format" not in prompt
        assert "`ruff format --check .` -> not covered by configured pipeline" in prompt

    def test_report_only_review_does_not_claim_pipeline_will_run(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            review_mode="review_report",
            task_description="Review branch. Run `cargo test --workspace` before merge.",
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {"test_command": "cargo test --workspace"},
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "build=off, tests=off, checks=off" in prompt
        assert "Validation commands found in review description" in prompt
        assert "`cargo test --workspace` -> not covered by configured pipeline" in prompt
        assert "In review mode, do not report a Validation Coverage Gap" in prompt

    def test_review_fix_validation_commands_are_informational(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            review_mode="review_fix",
            task_description="Review branch. Run `cargo test --workspace --all-features` before merge.",
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {"test_command": "cargo test"},
        }

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]

        assert "Review mode: validation command coverage is informational" in prompt
        assert "Validation commands found in review description" in prompt
        assert (
            "`cargo test --workspace --all-features` -> not covered by configured pipeline "
            "(nearest test/tests: `cargo test`; same command family)"
        ) in prompt
        assert "In review mode, do not report a Validation Coverage Gap" in prompt

    def test_validation_command_extraction_is_limited_to_command_like_text(self):
        text = (
            "Use `ConfigDocument` and `parse_config`.\n"
            "Document `npm test` in README.\n"
            "make sure validation still passes.\n"
            "go through parser edge cases.\n"
            "- `cargo` features should remain optional.\n"
            "- `npm` package metadata should remain private.\n"
            "- `cargo run -p codegen_tool -- fixtures/` should keep passing for committed fixtures.\n"
            "```yaml\ncommand: npm test\n```\n"
            "```markdown\nnpm test\n```\n"
            "Run: `cargo test --workspace`\n"
            "```bash\n$ ruff check .\n# comment\n```\n"
        )

        assert extract_validation_commands(text) == [
            "cargo run -p codegen_tool -- fixtures/",
            "cargo test --workspace",
            "ruff check .",
        ]

    def test_validation_command_extraction_handles_shell_edge_cases(self):
        text = (
            "`cargo test`,\n"
            "`python should`,\n"
            "$ ruff check .\n"
            "Verification:\n"
            "pytest\n"
            "python parser should reject invalid input\n"
            "```bash\n"
            "python 'unterminated\n"
            "swift evolve\n"
            "```\n"
        )

        assert extract_validation_commands(text) == [
            "cargo test",
            "python should",
            "ruff check .",
            "pytest",
            "python 'unterminated",
        ]

    def test_validation_command_extraction_ignores_prose_starting_with_tool_names(self):
        text = (
            "python parser should reject invalid quoted strings.\n"
            "cargo features should remain optional.\n"
            "npm package should stay private.\n"
            "Run and report results for:\n"
            "cargo test --workspace\n"
            "cargo clippy should not be described as prose here.\n"
        )

        assert extract_validation_commands(text) == [
            "cargo test --workspace",
        ]

    def test_validation_command_extraction_supports_validation_block_headings(self):
        text = (
            "## Verification\n"
            "\n"
            "cargo test --workspace\n"
            "\n"
            "ruff check .\n"
            "Implementation notes:\n"
            "pytest remains configured.\n"
        )

        assert extract_validation_commands(text) == [
            "cargo test --workspace",
            "ruff check .",
        ]

    def test_validation_command_extraction_preserves_validation_block_across_blank_separators(self):
        text = "## Verification\n\n\ncargo test --workspace\n"

        assert extract_validation_commands(text) == ["cargo test --workspace"]

    def test_validation_command_extraction_closes_validation_block_on_non_command_content(self):
        text = "## Verification\n\nNotes:\ncargo test --workspace\n"

        assert extract_validation_commands(text) == []

    def test_validation_command_extraction_supports_prompted_bare_commands(self):
        text = "$ pytest tests/unit\n"

        assert extract_validation_commands(text) == [
            "pytest tests/unit",
        ]

    def test_validation_command_extraction_rejects_bare_tool_names(self):
        text = "Run `cargo` and `npm` prose checks, but `pytest` before merge.\n"

        assert extract_validation_commands(text) == ["pytest"]

    def test_validation_command_matching_keeps_scripts_and_targets_distinct(self):
        assert validation_commands_equivalent("`cargo test`", "cargo test") == (True, "exact")
        assert not validation_commands_equivalent("", "cargo test")[0]
        assert not validation_commands_equivalent("npm run lint", "npm run test")[0]
        assert not validation_commands_equivalent("npm run lint", "npm test")[0]
        assert not validation_commands_equivalent("pnpm run lint", "pnpm run test")[0]
        assert not validation_commands_equivalent("python scripts/check.py", "python scripts/test.py")[0]
        assert not validation_commands_equivalent(
            "xcodebuild test -scheme App",
            "xcodebuild test -scheme AppTests",
        )[0]
        assert not validation_commands_equivalent(
            "xcodebuild test -scheme=App",
            "xcodebuild test -scheme Other",
        )[0]
        assert not validation_commands_equivalent("./gradlew testDebugUnitTest", "./gradlew lintDebug")[0]

    def test_validation_command_matching_covers_platform_command_signatures(self):
        same_family_commands = [
            ("python -m ruff check .", "ruff check src"),
            ("python -m pytest tests", "pytest tests/unit"),
            ("cargo doc", "cargo doc --no-deps"),
            ("swiftlint lint", "swiftlint lint --strict"),
            ("swift test", "swift test --parallel"),
            ("bun run test", "bun run test --coverage"),
            ("yarn run test", "yarn run test --watch"),
            ("yarn test", "yarn test --watch"),
            ("npx eslint .", "npx eslint src"),
            ("go test ./...", "go test ./pkg"),
            ("dotnet test", "dotnet test --no-build"),
        ]

        for left, right in same_family_commands:
            assert validation_commands_equivalent(left, right) == (True, "same command family")

        distinct_commands = [
            ("python -m mypy src", "python -m pyright src"),
            ("cargo run --bin app", "cargo run --bin app -- --help"),
            ("make test", "make test-ci"),
        ]

        for left, right in distinct_commands:
            assert not validation_commands_equivalent(left, right)[0]

    def test_validation_command_matching_treats_package_script_shortcuts_as_exact_aliases(self):
        aliases = [
            ("npm test", "npm run test"),
            ("npm test -- --watch", "npm run test -- --watch"),
            ("pnpm test", "pnpm run test"),
            ("pnpm typecheck", "pnpm run typecheck"),
            ("pnpm format:check", "pnpm run format:check"),
            ("yarn test", "yarn run test"),
            ("yarn test --watch", "yarn run test --watch"),
            ("yarn typecheck", "yarn run typecheck"),
        ]

        for left, right in aliases:
            assert validation_commands_equivalent(left, right) == (True, "exact")

    def test_validation_command_matching_treats_node_script_flags_strictly(self):
        assert validation_commands_equivalent("pnpm typecheck --watch", "pnpm run typecheck") == (
            True,
            "same command family",
        )
        assert validation_commands_equivalent("yarn lint --fix", "yarn run lint") == (
            True,
            "same command family",
        )

    def test_validation_command_matching_treats_python_module_forms_as_exact_aliases(self):
        aliases = [
            ("python -m pytest tests/unit", "pytest tests/unit"),
            ("python3 -m pytest", "pytest"),
            ("python -m ruff check .", "ruff check ."),
            ("python3 -m ruff format --check .", "ruff format --check ."),
        ]

        for left, right in aliases:
            assert validation_commands_equivalent(left, right) == (True, "exact")

    def test_node_configured_validation_commands_include_build_test_and_checks(self):
        state = _make_state()
        config = {
            "project": {"build_tool": "node"},
            "build": {
                "package_manager": "pnpm",
                "compile_command": "pnpm typecheck",
                "test_command": "pnpm test",
                "checks": [{"name": "lint", "command": "pnpm lint", "fix_command": "pnpm format"}],
            },
        }

        commands = configured_validation_commands(config, state)

        assert commands == [
            {"phase": "build", "name": "compile", "command": "pnpm typecheck"},
            {"phase": "test", "name": "tests", "command": "pnpm test"},
            {"phase": "check", "name": "lint", "command": "pnpm lint"},
            {"phase": "check_autofix", "name": "lint autofix", "command": "pnpm format"},
        ]

    def test_node_configured_validation_commands_use_detected_defaults(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "package.json").write_text('{"scripts": {"typecheck": "tsc --noEmit", "test": "vitest run"}}')
        state = _make_state()
        config = {
            "project": {"build_tool": "node", "root_path": str(tmp_path)},
            "build": {},
        }

        commands = configured_validation_commands(config, state)

        assert commands[:2] == [
            {"phase": "build", "name": "compile", "command": "pnpm typecheck"},
            {"phase": "test", "name": "tests", "command": "pnpm test"},
        ]

    @pytest.mark.parametrize(
        ("package_manager", "compile_command", "test_command"),
        [
            ("npm", "npm run build", "npm test"),
            ("bun", "bun run build", "bun run test"),
            ("pnpm", "pnpm build", "pnpm test"),
        ],
    )
    def test_node_configured_validation_commands_fall_back_without_root_path(
        self, package_manager: str, compile_command: str, test_command: str
    ):
        state = _make_state()
        config = {
            "project": {"build_tool": "node"},
            "build": {"package_manager": package_manager},
        }

        commands = configured_validation_commands(config, state)

        assert commands[:2] == [
            {"phase": "build", "name": "compile", "command": compile_command},
            {"phase": "test", "name": "tests", "command": test_command},
        ]

    def test_validation_command_coverage_requires_exact_command(self):
        configured = [{"phase": "test", "name": "tests", "command": "cargo test"}]

        covered, match_kind, nearest = validation_command_coverage(
            "cargo test --workspace --all-features",
            configured,
        )

        assert not covered
        assert match_kind == "same command family"
        assert nearest == configured[0]

    def test_validation_command_coverage_uses_first_near_match(self):
        configured = [
            {"phase": "test", "name": "tests", "command": "cargo test"},
            {"phase": "test", "name": "all-tests", "command": "cargo test --all-features"},
        ]

        covered, match_kind, nearest = validation_command_coverage(
            "cargo test --workspace",
            configured,
        )

        assert not covered
        assert match_kind == "same command family"
        assert nearest == configured[0]

    def test_validation_command_coverage_ignores_autofix_commands(self):
        configured = [
            {"phase": "check_autofix", "name": "format autofix", "command": "ruff format ."},
        ]

        covered, match_kind, nearest = validation_command_coverage("ruff format .", configured)

        assert not covered
        assert match_kind == ""
        assert nearest is None

    def test_validation_command_coverage_keeps_package_script_flags_strict(self):
        configured = [
            {"phase": "check", "name": "npm-tests", "command": "npm run test"},
            {"phase": "check", "name": "yarn-tests", "command": "yarn run test"},
        ]

        npm_covered, npm_match_kind, npm_nearest = validation_command_coverage(
            "npm test -- --watch",
            configured,
        )
        yarn_covered, yarn_match_kind, yarn_nearest = validation_command_coverage(
            "yarn test --watch",
            configured,
        )

        assert not npm_covered
        assert npm_match_kind == "same command family"
        assert npm_nearest == configured[0]
        assert not yarn_covered
        assert yarn_match_kind == "same command family"
        assert yarn_nearest == configured[1]

    def test_validation_command_coverage_treats_build_wrappers_as_exact_aliases(self):
        configured = [
            {"phase": "test", "name": "gradle-tests", "command": "gradle testDebugUnitTest"},
            {"phase": "test", "name": "maven-tests", "command": "mvn test"},
        ]

        gradle_covered, gradle_match_kind, _ = validation_command_coverage(
            "./gradlew testDebugUnitTest",
            configured,
        )
        maven_covered, maven_match_kind, _ = validation_command_coverage(
            "./mvnw test",
            configured,
        )

        assert gradle_covered
        assert gradle_match_kind == "exact"
        assert maven_covered
        assert maven_match_kind == "exact"

    def test_validation_command_coverage_keeps_wrapper_flags_strict(self):
        configured = [
            {"phase": "test", "name": "gradle-tests", "command": "gradle testDebugUnitTest"},
            {"phase": "test", "name": "maven-tests", "command": "mvn test"},
        ]

        covered, match_kind, nearest = validation_command_coverage(
            "./gradlew testDebugUnitTest --stacktrace",
            configured,
        )
        maven_covered, maven_match_kind, maven_nearest = validation_command_coverage(
            "./mvnw test -DskipITs",
            configured,
        )

        assert not covered
        assert match_kind == "same command family"
        assert nearest == configured[0]
        assert not maven_covered
        assert maven_match_kind == "same command family"
        assert maven_nearest == configured[1]

    def test_validation_command_coverage_keeps_wrapper_paths_strict(self):
        configured = [{"phase": "test", "name": "tests", "command": "gradle testDebugUnitTest"}]

        covered, match_kind, nearest = validation_command_coverage(
            "/opt/gradle/bin/gradlew testDebugUnitTest",
            configured,
        )

        assert not covered
        assert match_kind == "same command family"
        assert nearest == configured[0]

    def test_configured_validation_defaults_use_expected_commands(self):
        state = _make_state()

        android_commands = configured_validation_commands({"project": {"build_tool": "gradle-android"}}, state)
        jvm_commands = configured_validation_commands({"project": {"build_tool": "gradle-jvm"}}, state)
        xcode_commands = configured_validation_commands({"project": {"build_tool": "xcodebuild"}}, state)

        assert {"phase": "build", "name": "compile", "command": "./gradlew compileDebugKotlin"} in android_commands
        assert {"phase": "test", "name": "tests", "command": "./gradlew testDebugUnitTest"} in android_commands
        assert {"phase": "build", "name": "compile", "command": "./gradlew classes"} in jvm_commands
        assert {"phase": "test", "name": "tests", "command": "./gradlew test"} in jvm_commands
        assert {"phase": "build", "name": "compile", "command": "xcodebuild build -scheme Countries"} in xcode_commands
        assert {"phase": "test", "name": "tests", "command": "xcodebuild test -scheme Countries"} in xcode_commands

    def test_configured_validation_commands_ignore_invalid_checks(self):
        state = _make_state()
        config = {
            "project": {"build_tool": "python"},
            "build": {
                "checks": [
                    "ruff check .",
                    {"name": "missing-command"},
                    {"name": "format", "fix_command": "ruff format ."},
                ]
            },
        }

        commands = configured_validation_commands(config, state)

        assert {"phase": "check", "name": "missing-command", "command": ""} not in commands
        assert {"phase": "check_autofix", "name": "format autofix", "command": "ruff format ."} in commands

    def test_validation_coverage_gaps_reports_only_uncovered_commands(self):
        state = _make_state(
            task_description=("Run `cargo test --workspace` and `cargo run -p codegen_tool -- fixtures/` before merge.")
        )
        config = {
            "project": {"build_tool": "cargo"},
            "build": {"test_command": "cargo test --workspace"},
        }

        assert validation_coverage_gaps(config, state) == ["cargo run -p codegen_tool -- fixtures/"]


class TestReviewerAgentHistory:
    def test_previous_reviews_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_cycle_records = [
            {
                "reviewer_output": "## Issues\n\n### Missing null check\nFile: x\nProblem: p\nFix: f",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous reviews of this task" in prompt
        assert "Missing null check" in prompt
        assert "[Review 1]" in prompt

    def test_multiple_reviews_numbered(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_cycle_records = [
            {
                "reviewer_output": "First issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "reviewer_output": "Second issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "[Review 1]" in prompt
        assert "[Review 2]" in prompt
        assert "First issue" in prompt
        assert "Second issue" in prompt

    def test_security_reviews_excluded_from_reviewer_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.security_review_cycle_records = [
            {
                "reviewer_output": "## Security Issues\n\n### Hardcoded key",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous reviews of this task" not in prompt
        assert "Hardcoded key" not in prompt

    def test_no_history_section_when_review_history_empty(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous reviews of this task" not in prompt

    def test_test_files_written_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.test_files_written = ["tests/LoginTest.kt", "tests/AuthTest.kt"]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Files written by the test writer agent" in prompt
        assert "tests/LoginTest.kt" in prompt
        assert "tests/AuthTest.kt" in prompt

    def test_prompt_tells_reviewer_not_to_block_on_test_files(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Test files are not reviewer-owned output" in prompt
        assert "Do not review test files for correctness" in prompt
        assert "Do not block approval because a test" in prompt
        assert "report the production-code issue, not a" in prompt

    def test_review_diff_without_review_mode_keeps_pipeline_test_policy(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_diff="+ changed test")
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Test files are not reviewer-owned output" in prompt
        assert "Test files are branch-owned output" not in prompt

    @pytest.mark.parametrize("review_mode", ["review_report", "review_fix"])
    def test_review_mode_tells_reviewer_to_review_changed_tests(
        self, review_mode: str, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_mode=review_mode, review_diff="+ changed test")
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Test files are branch-owned output in `sikula review` mode" in prompt
        assert "Review changed test files" in prompt
        assert "stale fixtures" in prompt
        assert "negative tests changed to easier/different" in prompt
        assert "Test files are not reviewer-owned output" not in prompt
        assert "Do not review test files for correctness" not in prompt

    def test_review_mode_test_writer_files_are_still_reviewed(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_mode="review_fix", review_diff="+ changed test")
        state.test_files_written = ["tests/LoginTest.kt"]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Files written by the test writer agent" in prompt
        assert "tests/LoginTest.kt" in prompt
        assert "Still review their correctness" in prompt
        assert "relevance in review mode" in prompt

    def test_no_test_files_written_section_when_empty(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert "Files written by the test writer agent" not in stub_llm.readonly_calls[0]

    def test_test_failure_fixer_records_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.fix_cycle_records = [
            {
                "errors_before": {"build": [], "test": ["assertion failed"], "check": []},
                "files_written": ["tests/LoginTest.kt"],
                "fixer_output": "TEST FAILURE TRIAGE:\nclassification: stale_test",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Recent test-related fixer records" in prompt
        assert "tests/LoginTest.kt" in prompt
        assert "classification: stale_test" in prompt

    def test_test_origin_validation_fixer_records_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.fix_cycle_records = [
            {
                "errors_before": {"build": ["tests/LoginTest.kt:12: compile error"], "test": [], "check": []},
                "files_written": ["tests/LoginTest.kt"],
                "fixer_output": "TEST FAILURE TRIAGE:\nclassification: malformed_test",
                "triage_scope": "test_origin_validation",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Recent test-related fixer records" in prompt
        assert "Test-origin validation fix" in prompt
        assert "classification: malformed_test" in prompt

    def test_confirmed_production_test_fixer_triage_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.fix_cycle_records = [
            {
                "errors_before": {"build": [], "test": ["assertion failed"], "check": []},
                "files_written": ["src/Login.kt"],
                "fixer_output": "Fixed the confirmed production defect.",
                "triage_scope": "test_failure",
                "triage_pass": "production_confirmed",
                "confirmed_test_failure_triage": (
                    "TEST FAILURE TRIAGE:\n"
                    "classification: production_defect\n"
                    "contract_affected: login validation\n"
                    "chosen_fix: production_code\n"
                ),
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Triage pass: production_confirmed" in prompt
        assert "Confirmed production triage" in prompt
        assert "classification: production_defect" in prompt
        assert "src/Login.kt" in prompt

    def test_build_only_fixer_records_not_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.fix_cycle_records = [
            {
                "errors_before": {"build": ["compile error"], "test": [], "check": []},
                "files_written": ["src/Login.kt"],
                "fixer_output": "fixed compile error",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        assert "Recent test-related fixer records" not in stub_llm.readonly_calls[0]

    def test_mixed_history_includes_only_reviewer_entries(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_cycle_records = [
            {
                "reviewer_output": "Reviewer issue A",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "reviewer_output": "Reviewer issue C",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        state.security_review_cycle_records = [
            {
                "reviewer_output": "Security issue B",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Reviewer issue A" in prompt
        assert "Reviewer issue C" in prompt
        assert "Security issue B" not in prompt
        assert "[Review 1]" in prompt
        assert "[Review 2]" in prompt
        assert "[Review 3]" not in prompt


class TestReviewerAgentPlanContext:
    def test_step_context_appended_when_plan_set(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "step 2 of 2" in prompt.lower()
        assert "Step B" in prompt
        assert "CURRENT STEP REVIEW SCOPE" in prompt

    def test_step_scope_precedes_full_task_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 0
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert prompt.index("CURRENT STEP REVIEW SCOPE") < prompt.index("Task description:")
        assert "Do NOT report work that belongs only to future planned steps." in prompt
        assert "  - Step B" in prompt

    def test_plan_history_includes_only_current_step_reviews(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.review_cycle_records = [
            {
                "step": 0,
                "reviewer_output": "Old step issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "step": 1,
                "reviewer_output": "Current step issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Current step issue" in prompt
        assert "Old step issue" not in prompt

    def test_final_full_task_scope_replaces_step_scope(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.active_scope = "final_full_task"
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "FINAL FULL-TASK REVIEW SCOPE" in prompt
        assert "Step context: This review covers step" not in prompt
        assert "Do not restrict findings to the last planned step." in prompt
        assert state.review_cycle_records[0]["scope"] == "final_full_task"

    def test_final_full_task_history_includes_only_final_reviews(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.active_scope = "final_full_task"
        state.review_cycle_records = [
            {
                "step": 1,
                "scope": "step",
                "reviewer_output": "Last step issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "step": 1,
                "scope": "final_full_task",
                "reviewer_output": "Final review issue",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Final review issue" in prompt
        assert "Last step issue" not in prompt

    def test_no_step_context_without_plan(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Step context" not in prompt


# ---------------------------------------------------------------------------
# Review cycle record
# ---------------------------------------------------------------------------


class TestReviewerAgentCycleRecord:
    @pytest.mark.parametrize("provider_error", [False, True])
    def test_record_has_complete_correlation_fields(
        self,
        stub_llm: StubLLMClient,
        file_tool,
        provider_error: bool,
    ):
        state = _make_state()
        state.current_step = 2
        state.build_iterations = 3
        state.review_iterations = 4
        state.security_review_iterations = 5
        if provider_error:
            stub_llm.readonly_error = RuntimeError("review unavailable")
        else:
            stub_llm.readonly_result = "APPROVED"

        _make_agent(stub_llm, file_tool=file_tool).run(state)

        record = state.review_cycle_records[0]
        assert record["step"] == 2
        assert record["build_iteration"] == 3
        assert record["review_iteration"] == 4
        assert record["security_review_iteration"] == 5
        assert record["scope"] == "task"
        assert record["files_written"] == []

    def test_record_has_no_reviewer_field(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "reviewer" not in state.review_cycle_records[0]

    def test_record_approved_true_on_approval(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["approved"] is True

    def test_record_approved_false_on_issues(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Issues\n\n### Missing null check\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["approved"] is False

    def test_record_stores_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.task_description in state.review_cycle_records[0]["reviewer_prompt"]

    def test_second_review_sees_first_output_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        # Simulate two review rounds: first returns issues, second approves.
        # The second reviewer must see the first output in its prompt.
        stub_llm.readonly_result = "## Issues\n\n### Missing null check\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        stub_llm.readonly_result = "APPROVED"
        agent.run(state)
        second_prompt = state.review_cycle_records[1]["reviewer_prompt"]
        assert "Missing null check" in second_prompt
        assert "Your previous reviews of this task" in second_prompt

    def test_record_stores_step_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["step"] == 0

    def test_record_stores_step_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.current_step = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["step"] == 2

    def test_record_stores_build_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["build_iteration"] == 0

    def test_record_stores_build_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.build_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["build_iteration"] == 2

    def test_record_stores_review_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["review_iteration"] == 0

    def test_record_stores_review_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_cycle_records[0]["review_iteration"] == 2

    def test_review_iteration_reflects_orchestrator_increment_between_rounds(self, stub_llm: StubLLMClient, file_tool):
        # Simulates orchestrator: reviewer runs (review_iterations=0), issues found,
        # orchestrator increments review_iterations to 1, reviewer runs again.
        # First record must show 0, second must show 1.
        stub_llm.readonly_result = "## Issues\n\n### Missing null check\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        state.review_iterations = 1  # orchestrator increments before next review
        stub_llm.readonly_result = "APPROVED"
        agent.run(state)
        assert state.review_cycle_records[0]["review_iteration"] == 0
        assert state.review_cycle_records[1]["review_iteration"] == 1


class TestReviewerAgentExtraRules:
    def test_extra_rules_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.readonly_result = "APPROVED"
        (tmp_project / "reviewer_rules.md").write_text("Always verify null safety.")
        config = {"reviewer": {"extra_rules": "reviewer_rules.md"}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Always verify null safety." in prompt
        assert "Project-specific rules" in prompt

    def test_extra_rules_absent_when_not_configured(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Project-specific rules" not in stub_llm.readonly_calls[0]


class TestReviewerAgentSecurityPrefix:
    def test_security_prefix_prepended(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert stub_llm.readonly_calls[0].startswith(AGENT_SECURITY_PREFIX)
        assert READONLY_AGENT_PREFIX in stub_llm.readonly_calls[0]
        assert READONLY_AGENT_PREFIX in state.review_cycle_records[0]["reviewer_prompt"]


class TestReviewerAgentApprovalContractPrompt:
    def test_prompt_requires_final_approved_line(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "The final non-empty line must be exactly APPROVED" in prompt
        assert "will trigger another fix/review loop" in prompt
        assert "Do not include APPROVED when reporting issues" in prompt


class TestReviewerAgentDesignCompliance:
    def test_design_compliance_criterion_always_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Design compliance" in stub_llm.readonly_calls[0]

    def test_skip_instruction_present_when_no_design_files(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(implementation_prompt="Create LoginActivity with email/password fields")
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Skip this check if no design files" in prompt
        assert "Files referenced in the task" not in prompt

    def test_design_files_section_passed_to_reviewer_when_present(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(
            implementation_prompt=(
                "Create LoginActivity\n\n"
                "---\n\nFiles referenced in the task:\n\n"
                "Design/login.png — shows login screen with email/password fields"
            )
        )
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Files referenced in the task" in prompt
        assert "login.png" in prompt
