"""Tests for agents/fixer_agent.py — FixerAgent."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.fixer_agent import (
    FixerAgent,
    _changed_text_contents_after,
    _changed_text_contents_before,
    _errors_section,
    _git_dirty_text_snapshot,
    _guidelines_files,
    _has_valid_production_test_failure_triage,
    _is_test_origin_validation_failure,
    _parse_generated_test_retriage,
    _tech_stack,
    _test_constraint,
    _test_failure_production_writes,
    _validation_error_paths,
    _validation_error_targets,
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


class _FakeBuildTool:
    def is_build_config_file(self, path: str) -> bool:
        return path.endswith((".gradle", ".gradle.kts", "pyproject.toml"))


class _FakeMixedSourceBuildTool(_FakeBuildTool):
    def is_test_only_change(self, path: str, before: str | None, after: str | None) -> bool:
        return path == "src/lib.rs" and before == "before" and after == "after"


class _SequentialStubLLMClient(StubLLMClient):
    def __init__(self, responses: list[tuple[list[str], str]]) -> None:
        super().__init__()
        self._responses = responses

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self.agent_calls.append(prompt)
        if self.agent_error:
            raise self.agent_error
        if not self._responses:
            raise AssertionError("No stubbed agent response left")
        return self._responses.pop(0)


_WritingResponse = tuple[dict[str, str | None], list[str], str]


class _WritingSequentialStubLLMClient(StubLLMClient):
    def __init__(self, responses: list[_WritingResponse | Exception]) -> None:
        super().__init__()
        self._responses = responses

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self.agent_calls.append(prompt)
        if not self._responses:
            raise AssertionError("No stubbed agent response left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        writes, changed, output = response
        for relative_path, content in writes.items():
            path = cwd / relative_path
            if content is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return changed, output


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

    def test_test_failure_prompt_includes_generated_test_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["tests/LoginTest.kt failed"]
        state.test_files_written = ["tests/LoginTest.kt", "tests/AuthTest.kt"]

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = state.fix_cycle_records[0]["fixer_prompt"]

        assert "TEST FILES GENERATED OR UPDATED BY SIKULA EARLIER IN THIS TASK:" in prompt
        assert "tests/LoginTest.kt" in prompt
        assert "tests/AuthTest.kt" in prompt
        assert "generated test output from the current task" in prompt
        assert "malformed or brittle generated harness" in prompt

    def test_build_error_prompt_omits_generated_test_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        state.test_files_written = ["tests/LoginTest.kt"]

        _make_agent(stub_llm, file_tool=file_tool).run(state)
        prompt = state.fix_cycle_records[0]["fixer_prompt"]

        assert "TEST FILES GENERATED OR UPDATED BY SIKULA EARLIER IN THIS TASK:" not in prompt

    def test_prompt_tells_agent_not_to_manually_edit_lockfiles(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["Cargo.toml"]
        state = _make_state()
        state.errors = ["compile error"]
        config = {"project": {"build_tool": "cargo"}}

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = state.fix_cycle_records[0]["fixer_prompt"]

        assert "For Cargo/Rust projects" in prompt
        assert "do not manually synthesize or edit Cargo.lock" in prompt
        assert "Cargo.toml changes require lockfile updates" in prompt
        assert "rely on configured" in prompt
        assert "Cargo sync/build tooling" in prompt

    def test_prompt_omits_cargo_lockfile_guidance_for_non_cargo_projects(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        state = _make_state()
        state.errors = ["compile error"]
        config = {"project": {"build_tool": "gradle-android"}}

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = state.fix_cycle_records[0]["fixer_prompt"]

        assert "Cargo/Rust projects" not in prompt
        assert "Cargo.lock" not in prompt

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
    def test_test_only_errors_start_with_test_only_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "You may only write to these directories: tests/" in prompt
        assert "You may only write to these directories: src/" not in prompt
        assert "test-only triage/fix pass" in prompt
        assert "do not weaken the test" in prompt
        assert "malformed generated test or test harness assumption" in prompt
        assert "you may replace or delete that generated test" in prompt
        assert "do not hand-copy production" in prompt
        assert "Do not keep expanding a synthetic runtime/framework harness" in prompt
        assert "navigation/history stacks" in prompt
        assert "combine multiple fake runtime subsystems" in prompt
        assert "fake DOM/window" in prompt
        assert "network/fetch" in prompt
        assert "fixing one fake" in prompt
        assert "subsystem at a time" in prompt
        assert "coverage through existing project-standard seams" in prompt
        assert "Do not fix a generated test failure by adding a new skipped" in prompt
        assert "expected to be skipped" in prompt
        assert "structured TESTABILITY GAP block" in prompt
        assert "covered_by: <existing-surface tests/seams added or preserved, or none>" in prompt
        assert "Treat build files" in prompt
        assert "TEST FAILURE TRIAGE:" in prompt
        assert "classification: production_defect | stale_test | malformed_test | unclear" in prompt
        assert "chosen_fix: production_code | test_code" in prompt

    def test_test_only_prompt_includes_synthetic_harness_audit_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        state.record_synthetic_test_harness_audit(
            "test_writer",
            [
                {
                    "path": "tests/LoginTest.kt",
                    "subsystems": ["event_dispatch", "navigation_history", "network_server"],
                    "recommendation": "Replace with narrower existing-seam coverage.",
                    "evidence": [
                        {
                            "category": "event_dispatch",
                            "lines": [{"line": 10, "excerpt": "class FakeEventTarget {}"}],
                        }
                    ],
                }
            ],
        )

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)

        prompt = stub_llm.agent_calls[0]
        assert "SYNTHETIC TEST HARNESS AUDIT CONTEXT (non-blocking):" in prompt
        assert "harness does not remain in branch output" in prompt
        assert "tests/LoginTest.kt" in prompt
        assert "event_dispatch, navigation_history, network_server" in prompt

    def test_repeated_generated_test_failures_add_harness_replacement_context(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["tests/LoginTest.kt failed in generated fake navigation harness"]
        state.test_files_written = ["tests/LoginTest.kt"]
        state.fix_cycle_records = [
            {
                "triage_pass": "test_only",
                "files_written": ["tests/LoginTest.kt"],
            },
            {
                "triage_pass": "test_only",
                "files_written": ["tests/LoginTest.kt"],
            },
        ]

        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)

        prompt = stub_llm.agent_calls[0]
        assert "REPEATED GENERATED TEST FIX CONTEXT:" in prompt
        assert "already made 2 test-only fixer pass(es)" in prompt
        assert "Do not keep patching small pieces of a brittle synthetic runtime harness" in prompt
        assert "combines fake runtime subsystems" in prompt
        assert "navigation/history/router" in prompt
        assert "network/fetch" in prompt
        assert "existing project-standard seams" in prompt
        assert "GENERATED TEST RE-TRIAGE:" in prompt
        assert "replace_with_narrower_seam_test" in prompt
        assert "remove_malformed_generated_test" in prompt
        assert "report_testability_gap" in prompt
        assert "coverage targets as applying only to runnable behaviour" in prompt
        assert "TESTABILITY GAP" in prompt

    def test_generated_test_retriage_parser_stops_before_next_structured_block(self):
        retriage = _parse_generated_test_retriage(
            "TEST FAILURE TRIAGE:\n"
            "classification: malformed_test\n"
            "chosen_fix: test_code\n\n"
            "GENERATED TEST RE-TRIAGE:\n"
            "strategy: report_testability_gap\n"
            "target: generated browser harness\n"
            "reason: configured tests cannot run browser interactions\n"
            "covered_by: route contract tests\n\n"
            "TESTABILITY GAP:\n"
            "target: browser navigation\n"
        )

        assert retriage == {
            "message": (
                "GENERATED TEST RE-TRIAGE:\n"
                "strategy: report_testability_gap\n"
                "target: generated browser harness\n"
                "reason: configured tests cannot run browser interactions\n"
                "covered_by: route contract tests"
            ),
            "strategy": "report_testability_gap",
            "target": "generated browser harness",
            "reason": "configured tests cannot run browser interactions",
            "covered_by": "route contract tests",
        }

    def test_test_only_fixer_output_records_testability_gap(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: malformed_test\n"
            "contract_affected: none\n"
            "chosen_fix: test_code\n\n"
            "TESTABILITY GAP:\n"
            "target: browser detail navigation\n"
            "reason: configured validation has no DOM runtime\n"
            "covered_by: route parser tests\n"
            "recommended_action: add project-standard browser test helper\n"
            "risk: medium\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state(active_scope="final_full_task", build_iterations=3)
        state.test_errors = ["tests/LoginTest.kt failed because document is not defined"]

        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        assert len(state.testability_gaps) == 1
        gap = state.testability_gaps[0]
        assert gap["source"] == "fixer"
        assert gap["scope"] == "final_full_task"
        assert gap["build_iteration"] == 3
        assert gap["target"] == "browser detail navigation"
        assert gap["reason"] == "configured validation has no DOM runtime"
        assert gap["covered_by"] == "route parser tests"
        assert gap["recommended_action"] == "add project-standard browser test helper"
        assert gap["risk"] == "medium"
        assert any(entry["action"] == "testability_gap" and entry["agent"] == "fixer" for entry in state.history)

    def test_repeated_generated_test_retriage_is_recorded(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/LoginTest.kt"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: malformed_test\n"
            "contract_affected: none\n"
            "chosen_fix: test_code\n\n"
            "GENERATED TEST RE-TRIAGE:\n"
            "strategy: replace_with_narrower_seam_test\n"
            "target: LoginTest generated navigation harness\n"
            "reason: repeated failures show the test surface is wrong\n"
            "covered_by: route state tests\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["tests/LoginTest.kt failed in generated fake navigation harness"]
        state.test_files_written = ["tests/LoginTest.kt"]
        state.fix_cycle_records = [
            {
                "triage_pass": "test_only",
                "files_written": ["tests/LoginTest.kt"],
            },
            {
                "triage_pass": "test_only",
                "files_written": ["tests/LoginTest.kt"],
            },
        ]

        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        retriage = state.fix_cycle_records[-1]["generated_test_retriage"]
        assert retriage["strategy"] == "replace_with_narrower_seam_test"
        assert retriage["target"] == "LoginTest generated navigation harness"
        assert retriage["reason"] == "repeated failures show the test surface is wrong"
        assert retriage["covered_by"] == "route state tests"

    def test_missing_generated_test_retriage_restores_attempt_and_retries(self, file_tool, tmp_project: Path, caplog):
        (tmp_project / "tests").mkdir()
        (tmp_project / "tests" / "LoginTest.py").write_text("# placeholder test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test"], cwd=tmp_project, check=True, capture_output=True)

        test_triage = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        retriage = (
            test_triage + "\nGENERATED TEST RE-TRIAGE:\n"
            "strategy: replace_with_narrower_seam_test\n"
            "target: generated login harness\n"
            "reason: repeated generated test failures point to a malformed harness\n"
            "covered_by: route state tests\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {"tests/LoginTest.py": "assert False\n"},
                    ["tests/LoginTest.py"],
                    test_triage,
                ),
                (
                    {"tests/LoginTest.py": "assert True\n"},
                    ["tests/LoginTest.py"],
                    retriage,
                ),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["tests/LoginTest.py failed in generated fake runtime harness"]
        state.test_files_written = ["tests/LoginTest.py"]
        state.fix_cycle_records = [
            {"triage_pass": "test_only", "files_written": ["tests/LoginTest.py"]},
            {"triage_pass": "test_only", "files_written": ["tests/LoginTest.py"]},
        ]

        with caplog.at_level(logging.INFO, logger="agents.fixer_agent"):
            result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        assert (tmp_project / "tests" / "LoginTest.py").read_text() == "assert True\n"
        assert len(llm.agent_calls) == 2
        assert "GENERATED TEST RE-TRIAGE RECOVERY" in llm.agent_calls[1]
        violation = state.fix_cycle_records[2]["generated_test_retriage_violation"]
        assert violation["generated_test_files"] == ["tests/LoginTest.py"]
        assert violation["restored_files"] == ["tests/LoginTest.py"]
        assert violation["retry"] is True
        retry_record = state.fix_cycle_records[3]
        assert retry_record["triage_pass"] == "test_only_retry"
        assert retry_record["generated_test_retriage"]["strategy"] == "replace_with_narrower_seam_test"
        assert state.files_changed == ["tests/LoginTest.py"]
        assert any(e["action"] == "generated_test_retriage_violation_reverted" for e in state.history)
        assert (
            "Generated test re-triage missing for tests/LoginTest.py - restored tests/LoginTest.py and retrying fixer"
        ) in caplog.text
        assert "Retrying fixer agent with test_only_retry recovery prompt" in caplog.text

    def test_repeated_missing_generated_test_retriage_continues_after_retry_audit(
        self,
        file_tool,
        tmp_project: Path,
        caplog,
    ):
        (tmp_project / "tests").mkdir()
        (tmp_project / "tests" / "LoginTest.py").write_text("# placeholder test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test"], cwd=tmp_project, check=True, capture_output=True)

        test_triage = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {"tests/LoginTest.py": "assert False\n"},
                    ["tests/LoginTest.py"],
                    test_triage,
                ),
                (
                    {"tests/LoginTest.py": "assert 1 == 1\n"},
                    ["tests/LoginTest.py"],
                    test_triage,
                ),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["tests/LoginTest.py failed in generated fake runtime harness"]
        state.test_files_written = ["tests/LoginTest.py"]
        state.fix_cycle_records = [
            {"triage_pass": "test_only", "files_written": ["tests/LoginTest.py"]},
            {"triage_pass": "test_only", "files_written": ["tests/LoginTest.py"]},
        ]

        with caplog.at_level(logging.INFO, logger="agents.fixer_agent"):
            result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        assert state.failed is False
        assert (tmp_project / "tests" / "LoginTest.py").read_text() == "assert 1 == 1\n"
        assert len(llm.agent_calls) == 2
        assert state.fix_cycle_records[2]["generated_test_retriage_violation"]["retry"] is True
        assert state.fix_cycle_records[3]["generated_test_retriage_violation"]["retry"] is False
        assert state.files_changed == ["tests/LoginTest.py"]
        assert any(e["action"] == "generated_test_retriage_violation_continue" for e in state.history)
        assert "Generated test re-triage still missing after retry for tests/LoginTest.py" in caplog.text

    def test_build_errors_use_production_write_paths(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["src/Login.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["compile error"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert "src/" in stub_llm.agent_calls[0]

    def test_build_errors_from_test_files_use_test_triage_scope(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/countryFilters.test.ts"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["tests/countryFilters.test.ts(14,39): error TS2322: Type '\"   \"' is not assignable"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "You may only write to these directories: tests/" in prompt
        assert "You may only write to these directories: src/" not in prompt
        assert "test-origin validation failure" in prompt
        assert "TEST FAILURE TRIAGE:" in prompt
        assert state.fix_cycle_records[0]["triage_scope"] == "test_origin_validation"
        assert state.fix_cycle_records[0]["triage_pass"] == "test_only"

    def test_build_errors_from_mixed_production_and_test_files_use_production_scope(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.agent_result = ["src/Login.kt"]
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = [
            "src/Login.kt:10: error: incompatible type\ntests/LoginTest.kt:5: note: required by generated test"
        ]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "NEVER create or modify unit tests" in prompt

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
        assert "src/Login.kt" not in state.files_changed
        assert len(stub_llm.agent_calls) == 2
        assert "TEST-ONLY SCOPE RECOVERY" in stub_llm.agent_calls[1]
        assert state.fix_cycle_records[0]["test_only_scope_violation"]["violation_files"] == ["src/Login.kt"]
        assert state.fix_cycle_records[0]["test_only_scope_violation"]["retry"] is True
        assert any(e["action"] == "fix_failed" for e in state.history)

    def test_test_failure_first_pass_cannot_write_production_even_with_valid_triage(
        self, stub_llm: StubLLMClient, file_tool
    ):
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
        assert not result.success
        assert state.failed is True
        assert "test_only_retry pass" in result.message
        assert len(stub_llm.agent_calls) == 2
        assert "TEST-ONLY SCOPE RECOVERY" in stub_llm.agent_calls[1]

    def test_test_failure_production_request_must_not_change_tests_in_first_pass(self, file_tool):
        triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        llm = _SequentialStubLLMClient([(["tests/LoginTest.kt"], triage), (["tests/LoginTest.kt"], triage)])
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert "requested a production-code fix" in result.message
        assert len(llm.agent_calls) == 2
        assert state.files_changed == []

    def test_test_only_scope_violation_restores_attempt_and_retries_test_fix(self, file_tool, tmp_project: Path):
        test_triage = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {
                        "src/main.py": "# illegal production edit\n",
                        "tests/LoginTest.py": "assert False\n",
                    },
                    ["src/main.py", "tests/LoginTest.py"],
                    test_triage,
                ),
                (
                    {"tests/LoginTest.py": "assert True\n"},
                    ["tests/LoginTest.py"],
                    test_triage,
                ),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["android.net.Uri.encode is not mocked"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        assert (tmp_project / "src" / "main.py").read_text() == "# placeholder\n"
        assert (tmp_project / "tests" / "LoginTest.py").read_text() == "assert True\n"
        assert state.files_changed == ["tests/LoginTest.py"]
        assert len(llm.agent_calls) == 2
        assert "TEST-ONLY SCOPE RECOVERY" in llm.agent_calls[1]
        violation = state.fix_cycle_records[0]["test_only_scope_violation"]
        assert violation["kind"] == "production_writes"
        assert violation["violation_files"] == ["src/main.py"]
        assert violation["restored_files"] == ["src/main.py", "tests/LoginTest.py"]
        assert violation["retry"] is True
        assert state.fix_cycle_records[1]["triage_pass"] == "test_only_retry"

    def test_test_only_scope_violation_restore_failure_fails_closed(self, file_tool, monkeypatch, tmp_project: Path):
        test_triage = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {
                        "src/main.py": "# illegal production edit\n",
                        "tests/LoginTest.py": "assert False\n",
                    },
                    ["src/main.py", "tests/LoginTest.py"],
                    test_triage,
                )
            ]
        )
        monkeypatch.setattr(
            "agents.fixer_agent.restore_validation_artifacts",
            lambda _cwd, _before, _artifacts: ["src/main.py: permission denied"],
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert not result.success
        assert state.failed is True
        assert "failed to restore" in result.message
        assert state.files_changed == ["src/main.py", "tests/LoginTest.py"]
        violation = state.fix_cycle_records[0]["test_only_scope_violation"]
        assert violation["restore_errors"] == ["src/main.py: permission denied"]
        assert violation["retry"] is False
        assert any(e["action"] == "test_only_scope_violation_restore_failed" for e in state.history)
        assert (tmp_project / "src" / "main.py").read_text() == "# illegal production edit\n"

    def test_test_only_scope_recovery_retry_llm_error_records_recovery_context(self, file_tool):
        test_triage = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {"src/main.py": "# illegal production edit\n"},
                    ["src/main.py"],
                    test_triage,
                ),
                RuntimeError("retry timed out"),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert not result.success
        assert "retry timed out" in result.message
        assert len(state.fix_cycle_records) == 2
        retry_record = state.fix_cycle_records[1]
        assert retry_record["fixer_output"] is None
        assert retry_record["triage_pass"] == "test_only_retry"
        assert "TEST-ONLY SCOPE RECOVERY" in retry_record["scope_recovery"]
        assert "TEST-ONLY SCOPE RECOVERY" in llm.agent_calls[1]

    def test_recovered_production_request_uses_production_confirmed_pass(self, file_tool, tmp_project: Path):
        production_triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                (
                    {"tests/LoginTest.py": "assert False\n"},
                    ["tests/LoginTest.py"],
                    production_triage,
                ),
                ({}, [], production_triage),
                (
                    {"src/main.py": "# production fix\n"},
                    ["src/main.py"],
                    "Fixed the confirmed production defect.",
                ),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert result.success
        assert (tmp_project / "src" / "main.py").read_text() == "# production fix\n"
        assert not (tmp_project / "tests" / "LoginTest.py").exists()
        assert state.files_changed == ["src/main.py"]
        assert [record["triage_pass"] for record in state.fix_cycle_records] == [
            "test_only",
            "test_only_retry",
            "production_confirmed",
        ]
        assert state.fix_cycle_records[0]["test_only_scope_violation"]["kind"] == "production_request_changed_files"
        assert "CONFIRMED TEST FAILURE TRIAGE" in llm.agent_calls[2]

    def test_test_failure_production_fix_with_valid_triage_uses_second_pass(self, file_tool):
        triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        llm = _SequentialStubLLMClient(
            [
                ([], triage),
                (["src/Login.kt"], "Fixed the confirmed production defect."),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        assert state.failed is False
        assert state.test_errors == []
        assert "src/Login.kt" in state.files_changed
        assert len(llm.agent_calls) == 2
        assert "You may only write to these directories: tests/" in llm.agent_calls[0]
        assert "You may only write to these directories: src/, tests/" in llm.agent_calls[1]
        assert "CONFIRMED TEST FAILURE TRIAGE" in llm.agent_calls[1]
        assert len(state.fix_cycle_records) == 2
        assert state.fix_cycle_records[0]["triage_pass"] == "test_only"
        assert state.fix_cycle_records[1]["triage_pass"] == "production_confirmed"
        assert state.fix_cycle_records[1]["confirmed_test_failure_triage"] == triage

    def test_production_confirmed_pass_llm_error_records_confirmed_triage(self, file_tool):
        triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        llm = _WritingSequentialStubLLMClient(
            [
                ({}, [], triage),
                RuntimeError("production retry timed out"),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)

        assert not result.success
        assert "production retry timed out" in result.message
        assert len(state.fix_cycle_records) == 2
        confirmed_record = state.fix_cycle_records[1]
        assert confirmed_record["fixer_output"] is None
        assert confirmed_record["triage_pass"] == "production_confirmed"
        assert confirmed_record["confirmed_test_failure_triage"] == triage
        assert "CONFIRMED TEST FAILURE TRIAGE" in llm.agent_calls[1]

    def test_confirmed_production_pass_must_change_production_files(self, file_tool):
        triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: login validation\n"
            "chosen_fix: production_code\n"
        )
        llm = _SequentialStubLLMClient(
            [
                ([], triage),
                (["tests/LoginTest.kt"], "Changed only the test after production triage."),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.test_errors = ["assertion failed"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert "changed no production files" in result.message
        assert "tests/LoginTest.kt" in state.files_changed

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

    def test_malformed_test_triage_cannot_change_build_config_under_broad_test_path(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.agent_result = ["feature/countries/build.gradle.kts"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\n"
            "classification: malformed_test\n"
            "contract_affected: none\n"
            "chosen_fix: production_code\n"
        )
        config = {
            "sandbox": {
                "allowed_write_paths": ["feature/countries/"],
                "allowed_test_write_paths": ["feature/countries/"],
            }
        }
        state = _make_state()
        state.test_errors = ["test failed because source-inspection paths are relative to cwd"]
        agent = _make_agent(stub_llm, file_tool=file_tool, project_config=config)
        agent.tools["build"] = _FakeBuildTool()
        result = agent.run(state)
        assert not result.success
        assert state.failed is True
        assert "build.gradle.kts" in result.message

    def test_test_origin_build_failure_production_fix_requires_explicit_triage(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.agent_result = ["src/domain/countryFilters.ts"]
        stub_llm.agent_output = "Widened the type to satisfy the generated test."
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["tests/countryFilters.test.ts(14,39): error TS2322: Type '\"   \"' is not assignable"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert state.errors == ["tests/countryFilters.test.ts(14,39): error TS2322: Type '\"   \"' is not assignable"]
        assert "test-origin validation" in result.message.lower()
        assert "src/domain/countryFilters.ts" not in state.files_changed
        assert len(stub_llm.agent_calls) == 2

    def test_test_origin_build_failure_production_fix_with_valid_triage_uses_second_pass(self, file_tool):
        triage = (
            "TEST FAILURE TRIAGE:\n"
            "classification: production_defect\n"
            "contract_affected: filter API accepts blank input\n"
            "chosen_fix: production_code\n"
        )
        llm = _SequentialStubLLMClient(
            [
                ([], triage),
                (["src/domain/countryFilters.ts"], "Fixed the confirmed production defect."),
            ]
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["tests/countryFilters.test.ts(14,39): error TS2322: Type '\"   \"' is not assignable"]
        result = _make_agent(llm, file_tool=file_tool, project_config=config).run(state)
        assert result.success
        assert state.failed is False
        assert state.errors == []
        assert "src/domain/countryFilters.ts" in state.files_changed
        assert state.fix_cycle_records[0]["triage_scope"] == "test_origin_validation"
        assert state.fix_cycle_records[0]["triage_pass"] == "test_only"
        assert state.fix_cycle_records[1]["triage_scope"] == "test_origin_validation"
        assert state.fix_cycle_records[1]["triage_pass"] == "production_confirmed"

    def test_test_origin_check_failure_production_fix_requires_explicit_triage(
        self, stub_llm: StubLLMClient, file_tool
    ):
        stub_llm.agent_result = ["src/Login.kt"]
        stub_llm.agent_output = "Changed production code for a lint failure in a test."
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.check_errors = ["[lint]\ntests/LoginTest.kt:12:5: no-explicit-any"]
        result = _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        assert not result.success
        assert state.failed is True
        assert "test-origin validation" in result.message.lower()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestTestConstraint:
    def test_test_only_errors_allow_test_modification(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        assert "MAY create and modify test files" in _test_constraint(state)
        assert "test-only triage/fix pass" in _test_constraint(state)
        assert "leave files unchanged" in _test_constraint(state)
        assert "do not weaken the test" in _test_constraint(state)
        assert "TEST FAILURE TRIAGE:" in _test_constraint(state)

    def test_build_errors_block_test_modification(self):
        state = _make_state()
        state.errors = ["compile error"]
        assert "NEVER create or modify unit tests" in _test_constraint(state)

    def test_test_origin_validation_allows_test_modification_with_triage(self):
        state = _make_state()
        state.errors = ["tests/LoginTest.kt:12: error: unresolved reference"]
        constraint = _test_constraint(state, test_origin_validation=True)
        assert "test-origin validation failure" in constraint
        assert "MAY create and modify test files" in constraint
        assert "test-only triage/fix pass" in constraint
        assert "TEST FAILURE TRIAGE:" in constraint

    def test_confirmed_production_test_fix_allows_production_modification(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        constraint = _test_constraint(state, production_fix_confirmed=True)
        assert "previous test-only triage" in constraint
        assert "MAY modify production source files" in constraint
        assert "classification: production_defect" in constraint

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

    def test_test_origin_build_errors_start_with_test_paths(self):
        state = _make_state()
        state.errors = ["tests/LoginTest.kt:12: error: unresolved reference"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox, test_origin_validation=True) == ["tests/"]

    def test_test_failures_start_with_test_paths(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox) == ["tests/"]

    def test_confirmed_production_test_fix_uses_production_and_test_paths(self):
        state = _make_state()
        state.test_errors = ["assertion failed"]
        sandbox = {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}
        assert _write_paths_for_state(state, sandbox, production_fix_confirmed=True) == ["src/", "tests/"]

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

    def test_broad_module_test_path_treats_build_config_as_production(self):
        sandbox = {"allowed_test_write_paths": ["feature/countries/"]}
        assert _test_failure_production_writes(
            ["feature/countries/build.gradle.kts"],
            sandbox,
            _FakeBuildTool(),
        ) == ["feature/countries/build.gradle.kts"]

    def test_broad_module_test_path_allows_src_test_files(self):
        sandbox = {"allowed_test_write_paths": ["feature/countries/"]}
        assert (
            _test_failure_production_writes(
                ["feature/countries/src/test/kotlin/CountriesRepositoryTest.kt"],
                sandbox,
                _FakeBuildTool(),
            )
            == []
        )

    def test_specific_test_root_allows_test_fixtures(self):
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert (
            _test_failure_production_writes(
                ["tests/fixtures/pyproject.toml"],
                sandbox,
                _FakeBuildTool(),
            )
            == []
        )

    def test_concatenated_tests_root_allows_test_fixtures(self):
        sandbox = {"allowed_test_write_paths": ["CountryTests/"]}
        assert _test_failure_production_writes(["CountryTests/Fixtures/country.json"], sandbox) == []

    def test_root_test_path_still_treats_non_test_source_as_production(self):
        sandbox = {"allowed_test_write_paths": ["."]}
        assert _test_failure_production_writes(["src/Login.kt"], sandbox) == ["src/Login.kt"]

    def test_test_marker_does_not_match_unrelated_segment_suffixes(self):
        sandbox = {"allowed_test_write_paths": ["."]}
        assert _test_failure_production_writes(
            ["src/latest/Login.kt", "src/main/kotlin/Latest.kt"],
            sandbox,
        ) == ["src/latest/Login.kt", "src/main/kotlin/Latest.kt"]

    def test_platform_test_only_change_allows_mixed_source_file(self):
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert (
            _test_failure_production_writes(
                ["src/lib.rs"],
                sandbox,
                _FakeMixedSourceBuildTool(),
                {"src/lib.rs": "before"},
                {"src/lib.rs": "after"},
            )
            == []
        )

    def test_platform_hook_does_not_override_build_config_files(self):
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert _test_failure_production_writes(
            ["pyproject.toml"],
            sandbox,
            _FakeMixedSourceBuildTool(),
            {"pyproject.toml": "before"},
            {"pyproject.toml": "after"},
        ) == ["pyproject.toml"]

    def test_no_test_paths_treats_all_changed_files_as_production(self):
        assert _test_failure_production_writes(["src/Login.kt"], {}) == ["src/Login.kt"]


class TestFixerIncrementalContentSnapshots:
    def test_before_contents_use_pre_fixer_dirty_worktree_state(self, tmp_project: Path):
        path = tmp_project / "src" / "lib.rs"
        path.write_text("pub fn value() -> i32 { 1 }\n")
        subprocess.run(["git", "add", "."], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add rust source"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )

        path.write_text("pub fn value() -> i32 { 2 }\n")
        dirty_before = _git_dirty_text_snapshot(tmp_project)
        path.write_text("pub fn value() -> i32 { 2 }\n#[cfg(test)]\nmod tests {}\n")

        assert _changed_text_contents_before(tmp_project, ["src/lib.rs"], dirty_before) == {
            "src/lib.rs": "pub fn value() -> i32 { 2 }\n"
        }
        assert _changed_text_contents_after(tmp_project, ["src/lib.rs"]) == {
            "src/lib.rs": "pub fn value() -> i32 { 2 }\n#[cfg(test)]\nmod tests {}\n"
        }


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


class TestTestOriginValidationDetection:
    def test_extracts_platform_neutral_validation_paths(self, tmp_project: Path):
        errors = [
            "tests/countryFilters.test.ts(14,39): error TS2322",
            "e: file:///tmp/project/feature/src/test/kotlin/LoginTest.kt: (8, 5) unresolved reference",
            'File "/tmp/project/tests/test_app.py", line 12, in test_login',
            "/tmp/project/CountriesTests/CountryRowTests.swift:10: error: cannot find Country",
        ]
        assert _validation_error_paths(errors, tmp_project) == [
            "tests/countryFilters.test.ts",
            "/tmp/project/feature/src/test/kotlin/LoginTest.kt",
            "/tmp/project/tests/test_app.py",
            "/tmp/project/CountriesTests/CountryRowTests.swift",
        ]

    def test_extracts_path_like_validation_paths_with_unknown_extensions(self, tmp_project: Path):
        errors = [
            "tests/fixtures/login.snap:1: snapshot mismatch",
            "schemas/public/service.proto:42: field number conflict",
            "service.proto:43: field name conflict",
        ]
        assert _validation_error_paths(errors, tmp_project) == [
            "tests/fixtures/login.snap",
            "schemas/public/service.proto",
            "service.proto",
        ]

    def test_does_not_extract_urls_as_validation_paths(self, tmp_project: Path):
        errors = [
            "See https://ci.example.com/tests/LoginTest.kt for logs",
            "GET https://example.com:443/tests/login.test.ts failed",
        ]
        assert _validation_error_paths(errors, tmp_project) == []

    def test_extracts_platform_neutral_validation_targets(self):
        errors = [
            "ERROR: //pkg/countries:country_filters_test failed to build",
            "> Task :feature:countries:testDebugUnitTest FAILED",
            "ERROR: //pkg/countries:country_filters failed to build",
        ]
        assert _validation_error_targets(errors) == [
            "//pkg/countries:country_filters_test",
            ":feature:countries:testDebugUnitTest",
            "//pkg/countries:country_filters",
        ]

    def test_does_not_extract_namespace_scope_as_gradle_target(self):
        errors = [
            "error[E0425]: cannot find function `foo::test` in this scope",
            "note: candidate is `project::module::tests`",
        ]
        assert _validation_error_targets(errors) == []

    def test_detects_test_origin_build_failure_from_test_path(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["tests/countryFilters.test.ts(14,39): error TS2322"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_detects_test_origin_build_failure_from_platform_test_path(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["feature/countries/src/test/kotlin/LoginTest.kt:8: unresolved reference"]
        sandbox = {"allowed_test_write_paths": ["feature/countries/"]}
        assert _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_detects_test_origin_check_failure_from_test_path(self, tmp_project: Path):
        state = _make_state()
        state.check_errors = ["[lint]\nCountriesTests/CountryRowTests.swift:10: warning: unused value"]
        sandbox = {"allowed_test_write_paths": ["CountriesTests/"]}
        assert _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_absolute_test_path_outside_project_root(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["/tmp/external-project/tests/LoginTest.kt:12: unresolved reference"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_detects_test_origin_build_failure_from_bazel_test_target(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["ERROR: //pkg/countries:country_filters_test failed to build"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_detects_test_origin_build_failure_from_gradle_test_target(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["> Task :feature:countries:testDebugUnitTest FAILED"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_test_target_build_failure_uses_test_triage_scope(self, stub_llm: StubLLMClient, file_tool):
        stub_llm.agent_result = ["tests/countryFilters.test.ts"]
        stub_llm.agent_output = (
            "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"
        )
        config = {"sandbox": {"allowed_write_paths": ["src/"], "allowed_test_write_paths": ["tests/"]}}
        state = _make_state()
        state.errors = ["ERROR: //pkg/countries:country_filters_test failed to build"]
        _make_agent(stub_llm, file_tool=file_tool, project_config=config).run(state)
        prompt = stub_llm.agent_calls[0]
        assert "You may only write to these directories: tests/" in prompt
        assert "You may only write to these directories: src/" not in prompt
        assert "test-origin validation failure" in prompt
        assert state.fix_cycle_records[0]["triage_scope"] == "test_origin_validation"
        assert state.fix_cycle_records[0]["triage_pass"] == "test_only"

    def test_rejects_mixed_test_and_production_validation_paths(self, tmp_project: Path):
        state = _make_state()
        state.errors = [
            "src/domain/countryFilters.ts:12: error TS2322\ntests/countryFilters.test.ts:8: note: from test"
        ]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_mixed_test_and_unknown_extension_production_paths(self, tmp_project: Path):
        state = _make_state()
        state.errors = [
            "tests/fixtures/login.snap:1: snapshot mismatch\nschemas/public/service.proto:42: field conflict"
        ]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_mixed_test_and_bare_unknown_extension_production_paths(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["tests/fixtures/login.snap:1: snapshot mismatch\nservice.proto:42: field conflict"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_mixed_test_and_production_validation_targets(self, tmp_project: Path):
        state = _make_state()
        state.errors = [
            "ERROR: //pkg/countries:country_filters_test failed because //pkg/countries:country_filters failed"
        ]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_production_validation_target(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["> Task :feature:countries:compileDebugKotlin FAILED"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_non_test_targets_that_end_with_test_text(self, tmp_project: Path):
        sandbox = {"allowed_test_write_paths": ["tests/"]}

        state = _make_state()
        state.errors = ["> Task :feature:latest FAILED"]
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

        state = _make_state()
        state.errors = ["ERROR: //events:contest failed to build"]
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_url_shaped_bazel_target(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["See https://ci.example.com/tests:unitTest for logs"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_namespace_scope_named_test(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["error[E0425]: cannot find function `foo::test` in this scope"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_non_test_spec_target(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["ERROR: //api:openapi_spec failed to build"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_validation_errors_without_paths(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["Compilation failed with 1 error"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)

    def test_rejects_existing_test_errors_to_preserve_current_test_failure_path(self, tmp_project: Path):
        state = _make_state()
        state.errors = ["tests/LoginTest.kt:12: error"]
        state.test_errors = ["assertion failed"]
        sandbox = {"allowed_test_write_paths": ["tests/"]}
        assert not _is_test_origin_validation_failure(state, sandbox, tmp_project)


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
