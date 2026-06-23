"""Tests for agents/security_reviewer_agent.py — SecurityReviewerAgent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.security_reviewer_agent import SecurityReviewerAgent, _MAX_DIFF_CHARS
from tests.conftest import StubLLMClient
from core.state import TaskState
from tools.base_tool import ToolResult


def _make_state(**kwargs) -> TaskState:
    defaults = {
        "task_id": "t1",
        "task_description": "Add payment flow",
        "implementation_prompt": "Implement PaymentViewModel calling /payments endpoint",
        "files_changed": ["feature/payment/PaymentViewModel.kt"],
    }
    defaults.update(kwargs)
    return TaskState(**defaults)


def _make_agent(
    llm: StubLLMClient,
    file_tool=None,
    git_tool=None,
    project_config: dict | None = None,
) -> SecurityReviewerAgent:
    tools: dict = {}
    if file_tool is not None:
        tools["file"] = file_tool
    if git_tool is not None:
        tools["git"] = git_tool
    return SecurityReviewerAgent(llm=llm, tools=tools, project_config=project_config or {})


class TestSecurityReviewerGuards:
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
        result = _make_agent(stub_llm).run(state)
        assert not result.success
        assert "FileTool" in result.message


class TestSecurityReviewerApproval:
    def test_approved_sets_security_approved(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success
        assert state.security_approved is True

    @pytest.mark.parametrize(
        "decorated",
        [
            "**APPROVED**",
            "## APPROVED",
            "> APPROVED",
            "[APPROVED]",
            "1. APPROVED",
        ],
    )
    def test_approved_detected_regardless_of_decoration(self, decorated: str, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = f"{decorated}"
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success
        assert state.security_approved is True

    def test_approved_clears_review_issues(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_issues = ["old issue"]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_issues == []

    def test_approved_record_added(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "review" and e["result"] == "approved" for e in state.history)


class TestSecurityReviewerBlockingIssues:
    def _blocking_output(self) -> str:
        return (
            "## Security Issues\n\n"
            "### Hardcoded API key\n"
            "File: feature/payment/PaymentViewModel.kt\n"
            "Problem: API key in source\n"
            "Fix: Use env var\n"
        )

    def test_blocking_issue_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._blocking_output()
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_blocking_issue_sets_security_approved_false(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._blocking_output()
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_approved is False

    def test_blocking_issue_resets_review_approved(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._blocking_output()
        state = _make_state()
        state.review_approved = True
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_approved is False

    def test_blocking_issue_populates_review_issues(self, stub_llm: StubLLMClient, file_tool):
        output = self._blocking_output()
        stub_llm.readonly_result = output
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert output in state.review_issues

    def test_blocking_issue_data_in_result(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._blocking_output()
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "issues" in result.data


class TestSecurityReviewerWarnings:
    def _warning_output(self) -> str:
        return (
            "## Warnings\n\n"
            "### Missing input validation\n"
            "File: feature/payment/PaymentViewModel.kt\n"
            "Concern: amount not validated\n"
            "Suggestion: add bounds check\n"
        )

    def test_warnings_only_returns_success(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._warning_output()
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success

    def test_warnings_stored_in_state(self, stub_llm: StubLLMClient, file_tool):
        output = self._warning_output()
        stub_llm.readonly_result = output
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[-1]["has_warnings"] is True

    def test_warnings_set_security_approved(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = self._warning_output()
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_approved is True

    def test_both_sections_is_blocking(self, stub_llm: StubLLMClient, file_tool):
        output = self._warning_output() + "\n## Security Issues\n\n### Critical\nFile: x\nProblem: y\nFix: z\n"
        stub_llm.readonly_result = output
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success
        assert state.security_approved is False


class TestSecurityReviewerUnexpectedOutput:
    def test_no_signal_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "The code looks fine to me, nothing to report."
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_no_signal_sets_security_approved_false(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "The code looks fine to me, nothing to report."
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_approved is False

    def test_no_signal_resets_review_approved(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "The code looks fine to me, nothing to report."
        state = _make_state()
        state.review_approved = True
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.review_approved is False

    def test_no_signal_populates_review_issues(self, stub_llm: StubLLMClient, file_tool):
        output = "The code looks fine to me, nothing to report."
        stub_llm.readonly_result = output
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert output in state.review_issues

    def test_approved_mid_output_does_not_approve(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED\n\n## Security Issues\n\n### Critical\nFile: x\nProblem: y\nFix: z"
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success
        assert state.security_approved is False

    def test_warnings_only_is_not_unexpected(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Warnings\n\n### Minor concern\nFile: x\nConcern: y\nSuggestion: z"
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert result.success


class TestSecurityReviewerState:
    def test_output_appended_to_security_review_cycle_records(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert len(state.security_review_cycle_records) == 1
        assert "reviewer" not in state.security_review_cycle_records[0]

    def test_approved_logged(self, stub_llm: StubLLMClient, file_tool, caplog):
        import logging

        stub_llm.readonly_result = "No issues found.\n\nAPPROVED"
        state = _make_state()
        with caplog.at_level(logging.INFO, logger="agents.security_reviewer_agent"):
            _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any("Security review approved" in r.message for r in caplog.records)

    def test_llm_error_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_error = RuntimeError("connection error")
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success

    def test_llm_error_recorded(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_error = RuntimeError("connection error")
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert any(e["action"] == "review_failed" for e in state.history)

    def test_empty_output_returns_failure(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = ""
        state = _make_state()
        result = _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert not result.success


class TestSecurityReviewerPrompt:
    def test_tech_stack_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        config = {"project": {"platform": "Android", "language": "Kotlin"}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "Android / Kotlin" in stub_llm.readonly_calls[0]

    def test_guidelines_content_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.readonly_result = "APPROVED"
        (tmp_project / "src" / "security.md").write_text("# Security rules\n")
        config = {"guidelines": {"context_files": ["src/security.md"]}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "src/security.md" in prompt
        assert "# Security rules" in prompt

    def test_asset_manifest_security_review_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Asset manifest" in prompt
        assert "Production asset additions" in prompt
        assert "reference-only assets were not copied into production files" in prompt
        assert "licensing risk" in prompt

    def test_step_security_scope_precedes_full_task_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 0
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert prompt.index("CURRENT STEP SECURITY SCOPE") < prompt.index("Task description:")
        assert "Do NOT report missing future planned steps as security issues." in prompt
        assert "  - Step B" in prompt

    def test_final_full_task_security_scope_replaces_step_scope(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.active_scope = "final_full_task"
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "FINAL FULL-TASK SECURITY SCOPE" in prompt
        assert "Step context: This security review covers step" not in prompt
        assert "Do not restrict findings to the last planned step." in prompt
        assert state.security_review_cycle_records[0]["scope"] == "final_full_task"


class TestSecurityReviewerHistory:
    def test_previous_security_reviews_included_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.security_review_cycle_records = [
            {
                "reviewer_output": "## Security Issues\n\n### Hardcoded key\nFile: x",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous security reviews of this task" in prompt
        assert "Hardcoded key" in prompt
        assert "[Security Review 1]" in prompt

    def test_security_prefix_stripped_in_history(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.security_review_cycle_records = [
            {
                "reviewer_output": "Some security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Some security finding" in prompt

    def test_reviewer_entries_excluded_from_security_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.review_cycle_records = [
            {
                "reviewer_output": "## Issues\n\n### Missing null check",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous security reviews of this task" not in prompt
        assert "Missing null check" not in prompt

    def test_no_history_section_when_no_security_entries(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Your previous security reviews of this task" not in prompt

    def test_multiple_security_reviews_numbered(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.security_review_cycle_records = [
            {
                "reviewer_output": "First security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "reviewer_output": "Second security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        state.review_cycle_records = [
            {
                "reviewer_output": "Reviewer issue (should be excluded)",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            }
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "[Security Review 1]" in prompt
        assert "[Security Review 2]" in prompt
        assert "First security finding" in prompt
        assert "Second security finding" in prompt
        assert "should be excluded" not in prompt

    def test_plan_history_includes_only_current_step_security_reviews(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.security_review_cycle_records = [
            {
                "step": 0,
                "reviewer_output": "Old step security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "step": 1,
                "reviewer_output": "Current step security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Current step security finding" in prompt
        assert "Old step security finding" not in prompt

    def test_final_scope_history_includes_only_final_security_reviews(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.plan = ["Step A", "Step B"]
        state.current_step = 1
        state.active_scope = "final_full_task"
        state.security_review_cycle_records = [
            {
                "step": 1,
                "scope": "step",
                "reviewer_output": "Last step security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
            {
                "step": 1,
                "scope": "final_full_task",
                "reviewer_output": "Final security finding",
                "reviewer_prompt": None,
                "approved": False,
                "has_warnings": False,
                "timestamp": "",
            },
        ]
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Final security finding" in prompt
        assert "Last step security finding" not in prompt


class TestSecurityReviewerDiff:
    def test_diff_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, git_tool, tmp_project: Path):
        (tmp_project / "src" / "main.py").write_text("# changed\n")
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool).run(state)
        assert "main.py" in stub_llm.readonly_calls[0]

    def test_diff_unavailable_fallback_message(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "diff not available" in stub_llm.readonly_calls[0]

    def test_long_diff_truncated(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        big_diff = "+" + "x" * (_MAX_DIFF_CHARS + 1000)
        git_tool.diff_head = MagicMock(return_value=ToolResult(success=True, output=big_diff))
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool).run(state)
        assert "truncated" in stub_llm.readonly_calls[0]

    def test_review_diff_state_used_when_set(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_diff="+ added line from PR diff")
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "added line from PR diff" in stub_llm.readonly_calls[0]

    def test_review_diff_state_truncated(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        big_diff = "+" + "x" * (_MAX_DIFF_CHARS + 500)
        state = _make_state(review_diff=big_diff)
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "truncated" in stub_llm.readonly_calls[0]

    def test_review_diff_state_does_not_call_git_tool(self, stub_llm: StubLLMClient, file_tool, git_tool):
        stub_llm.readonly_result = "APPROVED"
        git_tool.diff_head = MagicMock(return_value=ToolResult(success=True, output="should not appear"))
        state = _make_state(review_diff="+ added line from PR diff")
        _make_agent(stub_llm, file_tool=file_tool, git_tool=git_tool).run(state)
        git_tool.diff_head.assert_not_called()

    def test_review_diff_empty_string_uses_fallback_message(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state(review_diff="")
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "diff not available" in stub_llm.readonly_calls[0]


# ---------------------------------------------------------------------------
# Review cycle record
# ---------------------------------------------------------------------------


class TestSecurityReviewerCycleRecord:
    def test_record_has_no_reviewer_field(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "reviewer" not in state.security_review_cycle_records[0]

    def test_record_approved_true_on_approval(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["approved"] is True

    def test_record_approved_true_on_warnings_only(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Warnings\n\n### Minor concern\nFile: x\nConcern: y\nSuggestion: z"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["approved"] is True

    def test_record_approved_false_on_blocking(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Security Issues\n\n### Hardcoded key\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["approved"] is False

    def test_record_has_warnings_true(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Warnings\n\n### Minor concern\nFile: x\nConcern: y\nSuggestion: z"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["has_warnings"] is True

    def test_record_has_warnings_false_on_approval(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["has_warnings"] is False

    def test_record_stores_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.task_description in state.security_review_cycle_records[0]["reviewer_prompt"]

    def test_second_security_review_sees_first_in_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "## Security Issues\n\n### Hardcoded key\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        stub_llm.readonly_result = "APPROVED"
        agent.run(state)
        second_prompt = state.security_review_cycle_records[1]["reviewer_prompt"]
        assert "Hardcoded key" in second_prompt
        assert "Your previous security reviews of this task" in second_prompt

    def test_record_stores_step_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["step"] == 0

    def test_record_stores_step_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.current_step = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["step"] == 2

    def test_record_stores_build_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["build_iteration"] == 0

    def test_record_stores_build_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.build_iterations = 3
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["build_iteration"] == 3

    def test_record_stores_security_review_iteration_default(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["security_review_iteration"] == 0

    def test_record_stores_security_review_iteration_from_state(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        state.security_review_iterations = 2
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert state.security_review_cycle_records[0]["security_review_iteration"] == 2

    def test_security_review_iteration_reflects_orchestrator_increment_between_rounds(
        self, stub_llm: StubLLMClient, file_tool
    ):
        # Simulates orchestrator: security reviewer runs (security_review_iterations=0),
        # blocking issues found, orchestrator increments to 1, security reviewer runs again.
        # First record must show 0, second must show 1.
        stub_llm.readonly_result = "## Security Issues\n\n### Hardcoded key\nFile: x\nProblem: p\nFix: f"
        state = _make_state()
        agent = _make_agent(stub_llm, file_tool=file_tool)
        agent.run(state)
        state.security_review_iterations = 1  # orchestrator increments before next security review
        stub_llm.readonly_result = "APPROVED"
        agent.run(state)
        assert state.security_review_cycle_records[0]["security_review_iteration"] == 0
        assert state.security_review_cycle_records[1]["security_review_iteration"] == 1


class TestSecurityReviewerAgentExtraRules:
    def test_extra_rules_included_in_prompt(self, stub_llm: StubLLMClient, file_tool, tmp_project: Path):
        stub_llm.readonly_result = "APPROVED"
        (tmp_project / "security_rules.md").write_text("Always check GDPR compliance.")
        config = {"security_reviewer": {"extra_rules": "security_rules.md"}}
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Always check GDPR compliance." in prompt
        assert "Project-specific rules" in prompt

    def test_extra_rules_absent_when_not_configured(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Project-specific rules" not in stub_llm.readonly_calls[0]


# ---------------------------------------------------------------------------
# security.context injection
# ---------------------------------------------------------------------------


class TestSecurityContext:
    def test_context_injected_into_prompt(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        cfg = {"security": {"context": "Mobile app. Handles auth tokens. No PII."}}
        _make_agent(stub_llm, file_tool=file_tool, project_config=cfg).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "Project security context:" in prompt
        assert "Mobile app. Handles auth tokens. No PII." in prompt

    def test_context_absent_when_not_configured(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert "Project security context:" not in stub_llm.readonly_calls[0]

    def test_context_absent_when_empty_string(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        cfg = {"security": {"context": ""}}
        _make_agent(stub_llm, file_tool=file_tool, project_config=cfg).run(state)
        assert "Project security context:" not in stub_llm.readonly_calls[0]

    def test_context_absent_when_whitespace_only(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        cfg = {"security": {"context": "   \n  "}}
        _make_agent(stub_llm, file_tool=file_tool, project_config=cfg).run(state)
        assert "Project security context:" not in stub_llm.readonly_calls[0]


class TestSecurityReviewerAgentSecurityPrefix:
    def test_security_prefix_prepended(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        assert stub_llm.readonly_calls[0].startswith(AGENT_SECURITY_PREFIX)


class TestSecurityReviewerApprovalContractPrompt:
    def test_prompt_requires_final_approved_line(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.readonly_result = "APPROVED"
        state = _make_state()
        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = stub_llm.readonly_calls[0]
        assert "For an all-clear approval, the final non-empty line must be exactly APPROVED" in prompt
        assert "will trigger another fix/review loop" in prompt
        assert "Do not include APPROVED when reporting security issues or warnings" in prompt
        assert "Warnings are non-blocking: warning-only output is accepted and recorded" in prompt
