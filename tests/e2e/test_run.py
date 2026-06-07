"""E2E tests for the sikula run command using FakeLLMClient."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from core.llm_client import LLMClient
from sikula import cmd_run


# ---------------------------------------------------------------------------
# Helpers (defined inline — no cross-package imports needed)
# ---------------------------------------------------------------------------


def _cfg(project_root: Path) -> dict:
    return {
        "project": {
            "name": "test-project",
            "build_tool": "python",
            "root_path": str(project_root),
            "language": "Python",
        },
        "sandbox": {
            "allowed_write_paths": ["src/"],
            "allowed_test_write_paths": ["tests_proj/"],
            "allowed_read_paths": ["."],
            "max_iterations": 3,
            "max_review_iterations": 2,
            "max_security_review_iterations": 2,
        },
        "tasks": {"state_dir": str(project_root / ".sikula" / "state")},
        "build": {"compile_command": "python3 -m compileall -q .", "test_command": "python3 -m pytest", "timeout": 120},
        "guidelines": {"context_files": [], "max_file_chars": 3000},
        "run_planner": True,
        "run_presync": False,
        "run_build": False,
        "run_build_per_step": False,
        "run_review": True,
        "run_security_review": True,
        "run_test_writing": False,
        "run_tests": False,
        "run_checks": False,
        "planner": {"max_steps": 4},
    }


def _args(**kwargs) -> argparse.Namespace:
    defaults: dict = {
        "task_file": None,
        "task_file_pos": None,
        "task_id": None,
        "no_isolate": True,
        "reset_failed": False,
        "build": None,
        "presync": None,
        "presync_clean": None,
        "planner": None,
        "review": None,
        "security_review": None,
        "test_writing": None,
        "tests": None,
        "build_per_step": None,
        "checks": None,
        "agent_model": None,
        "agent_provider": None,
        "agent_timeout": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _invoke(project: Path, task_file: Path, fake, **kwargs) -> int:
    """Call cmd_run with a patched LLM. Returns the SystemExit code."""
    with patch("core.llm_client.create_llm_client", return_value=fake):
        with pytest.raises(SystemExit) as exc_info:
            cmd_run(_args(task_file=str(task_file), **kwargs), _cfg(project))
    return exc_info.value.code


def _task(project: Path, text: str) -> Path:
    d = project / ".sikula" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "task.md"
    f.write_text(text)
    return f


@pytest.fixture(autouse=True)
def _run_from_project_root(git_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(git_project)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSinglePassRun:
    def test_happy_path_exits_0(self, git_project: Path, fake_llm):
        fake = fake_llm(
            agent_responses=[
                {"src/calculator.py": "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"}
            ]
        )
        assert _invoke(git_project, _task(git_project, "Add a subtract function."), fake) == 0

    def test_state_is_done(self, git_project: Path, fake_llm):
        fake = fake_llm(
            agent_responses=[{"src/calculator.py": "def add(a, b): return a + b\ndef multiply(a, b): return a * b\n"}]
        )
        _invoke(git_project, _task(git_project, "Add multiply."), fake)

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.failed is False

    def test_files_changed_recorded(self, git_project: Path, fake_llm):
        fake = fake_llm(agent_responses=[{"src/calculator.py": "def add(a, b): return a + b\n"}])
        _invoke(git_project, _task(git_project, "Tweak calculator."), fake)

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert "src/calculator.py" in state.files_changed

    def test_history_records_key_phases(self, git_project: Path, fake_llm):
        fake = fake_llm(agent_responses=[{"src/calculator.py": "def add(a, b): return a + b\n"}])
        _invoke(git_project, _task(git_project, "Tweak calculator."), fake)

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        actions = [h["action"] for h in state.history]
        assert "analyze" in actions
        assert "plan" in actions
        assert "implement" in actions
        assert "review" in actions

    def test_file_written_to_project_dir(self, git_project: Path, fake_llm):
        new_content = "def add(a, b): return a + b\ndef divide(a, b): return a / b\n"
        fake = fake_llm(agent_responses=[{"src/calculator.py": new_content}])
        _invoke(git_project, _task(git_project, "Add divide."), fake)
        assert (git_project / "src" / "calculator.py").read_text() == new_content

    def test_no_changes_fails(self, git_project: Path, fake_llm):
        """Single-pass implementer that writes nothing → task fails."""
        fake = fake_llm(agent_responses=[])  # run_agent returns ([], "")
        assert _invoke(git_project, _task(git_project, "Do nothing."), fake) == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        assert store.load(store.list_tasks()[0]).failed is True

    def test_invalid_analyst_output_fails_before_implementation(self, git_project: Path, seq_fake_llm):
        fake = seq_fake_llm(readonly_responses=[_BAD_ANALYST_META_PROMPT, _BAD_ANALYST_META_PROMPT])

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add subtract."))), _cfg(git_project))

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.implementation_prompt is None
        assert state.files_changed == []
        assert len(state.analyst_retry_records) == 2
        assert any(entry["action"] == "analyze_failed" for entry in state.history)

    def test_build_fixer_no_changes_fails_immediately(self, git_project: Path, fake_llm):
        fake = fake_llm(agent_responses=[{"src/calculator.py": "def add(a, b):\n    return a +\n"}])
        cfg = _cfg(git_project)
        cfg["run_build"] = True
        cfg["run_review"] = False
        cfg["run_security_review"] = False
        cfg["run_tests"] = False
        cfg["run_checks"] = False
        cfg["sandbox"]["max_iterations"] = 3

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Break the build."))), cfg)

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.done is False
        assert state.build_iterations == 1
        assert len(state.fix_cycle_records) == 1
        assert state.fix_cycle_records[0]["files_written"] == []
        assert any(
            entry["action"] == "abort"
            and "fixer failed" in entry["result"]
            and "Agent made no file changes" in entry["result"]
            for entry in state.history
        )


class TestMultiStepRun:
    def test_two_step_plan_completes(self, git_project: Path, fake_llm):
        fake = fake_llm(
            generate_response="1. Add subtract function\n2. Add multiply function",
            agent_responses=[
                {"src/calculator.py": "def add(a, b): return a + b\ndef subtract(a, b): return a - b\n"},
                {
                    "src/calculator.py": "def add(a, b): return a + b\ndef subtract(a, b): return a - b\ndef multiply(a, b): return a * b\n"
                },
            ],
        )
        assert _invoke(git_project, _task(git_project, "Add subtract and multiply."), fake) == 0

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert len(state.plan) == 2

    def test_final_full_task_review_can_reject_after_step_reviews_approved(self, git_project: Path):
        class FinalReviewRejectsFake(LLMClient):
            def __init__(self) -> None:
                self.agent_calls = 0
                self.fixed = False
                self.final_rejected = False
                self.reviewer_prompts: list[str] = []

            def generate(self, system, user):
                return "1. Add subtract function\n2. Add multiply function"

            def run_readonly_agent(self, prompt, cwd):
                if "Your job: analyze a feature or bug task" in prompt:
                    return "Add subtract(a, b) and multiply(a, b) to src/calculator.py."
                if "Your job: verify that a task implementation is complete" in prompt:
                    self.reviewer_prompts.append(prompt)
                    if "\nFINAL FULL-TASK REVIEW SCOPE:\n" in prompt and not self.fixed:
                        self.final_rejected = True
                        return (
                            "## Issues\n\n"
                            "### Incorrect multiplication\n"
                            "File: src/calculator.py\n"
                            "Problem: multiply(a, b) returns a + b, so the completed task is wrong.\n"
                            "Fix: return a * b from multiply(a, b)."
                        )
                    return "APPROVED"
                if "Your job is to identify security vulnerabilities" in prompt:
                    return "Security checks: no external inputs or sensitive operations introduced.\nAPPROVED"
                return "APPROVED"

            def run_agent(self, prompt, cwd):
                calculator = cwd / "src" / "calculator.py"
                if "REVIEW ISSUES TO FIX:" in prompt:
                    self.fixed = True
                    calculator.write_text(
                        "def add(a, b): return a + b\n"
                        "def subtract(a, b): return a - b\n"
                        "def multiply(a, b): return a * b\n"
                    )
                    return ["src/calculator.py"], "fixed multiply implementation"

                self.agent_calls += 1
                if self.agent_calls == 1:
                    calculator.write_text("def add(a, b): return a + b\ndef subtract(a, b): return a - b\n")
                    return ["src/calculator.py"], "added subtract"

                calculator.write_text(
                    "def add(a, b): return a + b\ndef subtract(a, b): return a - b\ndef multiply(a, b): return a + b\n"
                )
                return ["src/calculator.py"], "added multiply with incorrect implementation"

        fake = FinalReviewRejectsFake()
        assert _invoke(git_project, _task(git_project, "Add subtract and multiply."), fake) == 0

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.final_full_task_review_done is True
        assert fake.final_rejected is True
        assert [r["scope"] for r in state.review_cycle_records] == [
            "step",
            "step",
            "final_full_task",
            "final_full_task",
        ]
        assert state.review_cycle_records[2]["approved"] is False
        assert state.review_cycle_records[3]["approved"] is True
        assert state.implement_cycle_records[-1]["scope"] == "final_full_task"
        assert "def multiply(a, b): return a * b" in (git_project / "src" / "calculator.py").read_text()

    def test_final_build_fixer_review_runs_in_full_task_scope(self, git_project: Path):
        class FinalBuildFixFake(LLMClient):
            def __init__(self) -> None:
                self.agent_calls = 0
                self.reviewer_prompts: list[str] = []
                self.security_prompts: list[str] = []

            def generate(self, system, user):
                return "1. Add subtract function\n2. Add multiply function"

            def run_readonly_agent(self, prompt, cwd):
                if "Your job: analyze a feature or bug task" in prompt:
                    return "Add subtract(a, b) and multiply(a, b) to src/calculator.py."
                if "Your job: verify that a task implementation is complete" in prompt:
                    self.reviewer_prompts.append(prompt)
                    return "APPROVED"
                if "Your job is to identify security vulnerabilities" in prompt:
                    self.security_prompts.append(prompt)
                    return "Security checks: no external inputs or sensitive operations introduced.\nAPPROVED"
                return "APPROVED"

            def run_agent(self, prompt, cwd):
                self.agent_calls += 1
                calculator = cwd / "src" / "calculator.py"
                if "BUILD ERRORS:" in prompt:
                    calculator.write_text(
                        "def add(a, b):\n"
                        "    return a + b\n\n"
                        "def subtract(a, b):\n"
                        "    return a - b\n\n"
                        "def multiply(a, b):\n"
                        "    return a * b\n"
                    )
                    return ["src/calculator.py"], "fixed syntax after final build"
                if self.agent_calls == 1:
                    calculator.write_text("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")
                    return ["src/calculator.py"], "added subtract"
                calculator.write_text(
                    "def add(a, b):\n"
                    "    return a + b\n\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n\n"
                    "def multiply(a, b)\n"
                    "    return a * b\n"
                )
                return ["src/calculator.py"], "added multiply with syntax error"

        fake = FinalBuildFixFake()
        cfg = _cfg(git_project)
        cfg["run_build"] = True
        cfg["run_tests"] = False
        cfg["run_checks"] = False

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add subtract and multiply."))), cfg)

        assert exc_info.value.code == 0
        assert len(fake.reviewer_prompts) == 4
        assert "\nCURRENT STEP REVIEW SCOPE:\n" in fake.reviewer_prompts[0]
        assert "\nCURRENT STEP REVIEW SCOPE:\n" in fake.reviewer_prompts[1]
        assert "\nFINAL FULL-TASK REVIEW SCOPE:\n" in fake.reviewer_prompts[2]
        assert "\nFINAL FULL-TASK REVIEW SCOPE:\n" in fake.reviewer_prompts[3]
        assert len(fake.security_prompts) == 4
        assert "\nFINAL FULL-TASK SECURITY SCOPE:\n" in fake.security_prompts[2]
        assert "\nFINAL FULL-TASK SECURITY SCOPE:\n" in fake.security_prompts[3]

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.plan_completed is True
        assert state.final_full_task_review_done is True
        assert [r["scope"] for r in state.review_cycle_records] == [
            "step",
            "step",
            "final_full_task",
            "final_full_task",
        ]
        assert any(
            r["phase"] == "build" and r["status"] == "failed" and r.get("scope") == "final_full_task"
            for r in state.validation_cycle_records
        )
        assert "def multiply(a, b):" in (git_project / "src" / "calculator.py").read_text()

    def test_step_with_no_changes_is_skipped_not_aborted(self, git_project: Path, fake_llm):
        """A multi-step step that writes no files is skipped (not a fatal abort)."""
        fake = fake_llm(
            generate_response="1. Verify existing code\n2. Add subtract function",
            agent_responses=[
                {},  # step 1: no changes → step_skipped
                {"src/calculator.py": "def add(a, b): return a + b\ndef subtract(a, b): return a - b\n"},
            ],
        )
        assert _invoke(git_project, _task(git_project, "Add subtract."), fake) == 0

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert any(h["action"] == "step_skipped" for h in state.history)


_REVIEW_ISSUES = (
    "## Issues\n\n### Missing docstring\nFile: src/calculator.py\nProblem: no docstring\nFix: add docstring"
)

_SECURITY_BLOCKING = (
    "## Security Issues\n\n### Unvalidated input\n"
    "File: src/calculator.py\nProblem: no input validation\nFix: validate inputs"
)

_CALC_V1 = "def add(a, b): return a + b\ndef subtract(a, b): return a - b\n"
_CALC_V2 = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\ndef subtract(a, b):\n    """Subtract b from a."""\n    return a - b\n'
_ANALYST_PROMPT = (
    "1. Context: calculator module\n"
    "2. Required changes: update src/calculator.py for the requested calculator behavior\n"
    "3. Architecture constraints: keep the existing simple function style\n"
    "4. Hard rules: minimal changes only\n"
    "5. Cleanup: no dead production code expected\n"
    "6. Acceptance criteria: requested calculator behavior is implemented and validation passes"
)
_BAD_ANALYST_META_PROMPT = (
    "The implementation prompt above is the final output. The task is complete — no further tracking is needed "
    "(this analyser run produced a single artifact, the prompt itself, and is not part of an ongoing multi-step "
    "implementation)."
)


class TestReviewRejectionCycle:
    def test_review_rejection_triggers_fix_and_reapproval(self, git_project: Path, seq_fake_llm):
        """Reviewer rejects once → implementer fix runs → reviewer approves → done."""
        # run_readonly_agent call order: analyst, reviewer(initial), reviewer(after fix), security
        fake = seq_fake_llm(
            readonly_responses=[
                "Add subtract and improve docstrings.",  # analyst → implementation_prompt
                _REVIEW_ISSUES,  # reviewer — initial rejection
                "APPROVED",  # reviewer — after fix
                "APPROVED",  # security reviewer
            ],
            agent_responses=[
                {"src/calculator.py": _CALC_V1},  # implementer initial
                {"src/calculator.py": _CALC_V2},  # implementer review fix
            ],
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add subtract with docstrings."))), _cfg(git_project))

        assert exc_info.value.code == 0

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.review_approved is True
        actions = [h["action"] for h in state.history]
        assert actions.count("review") >= 2  # reviewer ran at least twice
        assert actions.count("implement") >= 2  # initial + fix

    def test_max_review_iterations_fails_task(self, git_project: Path, seq_fake_llm):
        """Reviewer always rejects → max_review_iterations reached → task fails."""
        # max_review_iterations=2 → 2 fix attempts → 3 reviewer runs before abort
        fake = seq_fake_llm(
            readonly_responses=[
                _ANALYST_PROMPT,  # analyst
                _REVIEW_ISSUES,  # reviewer run 1
                _REVIEW_ISSUES,  # reviewer run 2 (after fix 1)
                _REVIEW_ISSUES,  # reviewer run 3 (after fix 2) → triggers abort
            ],
            agent_responses=[
                {"src/calculator.py": _CALC_V1},  # initial
                {"src/calculator.py": _CALC_V1},  # fix 1
                {"src/calculator.py": _CALC_V1},  # fix 2
            ],
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Improve calculator."))), _cfg(git_project))

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.done is False
        actions = [h["action"] for h in state.history]
        assert "abort" in actions


class TestSecurityReviewBlocking:
    def test_security_blocking_triggers_fix_and_reapproval(self, git_project: Path, seq_fake_llm):
        """Security review blocks → implementer fix → code re-review → security approves → done."""
        # run_readonly_agent call order:
        # analyst, code reviewer, security(blocking), code reviewer(re-run), security(approved)
        fake = seq_fake_llm(
            readonly_responses=[
                _ANALYST_PROMPT,  # analyst
                "APPROVED",  # code reviewer initial
                _SECURITY_BLOCKING,  # security reviewer — blocking
                "APPROVED",  # code reviewer after security fix
                "APPROVED",  # security reviewer after fix
            ],
            agent_responses=[
                {"src/calculator.py": _CALC_V1},  # implementer initial
                {"src/calculator.py": _CALC_V2},  # implementer security fix
            ],
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add secure subtract."))), _cfg(git_project))

        assert exc_info.value.code == 0

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.security_approved is True
        assert state.review_approved is True
        # Code review ran at least twice (initial + after security fix)
        assert len(state.review_cycle_records) >= 2
        assert len(state.security_review_cycle_records) >= 2


class TestSecurityReviewTimeout:
    def test_max_security_review_iterations_fails_task(self, git_project: Path, seq_fake_llm):
        """Security reviewer always blocks → max_security_review_iterations reached → task fails."""
        # max_security_review_iterations=2 → 2 fix attempts before abort (same semantics as review loop):
        #   block 1 (iterations=0, 0<2 → fix 1), block 2 (iterations=1, 1<2 → fix 2),
        #   block 3 (iterations=2, 2>=2 → abort)
        # call order: analyst, review, security(block1), review, security(block2), review, security(block3 → abort)
        fake = seq_fake_llm(
            readonly_responses=[
                _ANALYST_PROMPT,  # analyst
                "APPROVED",  # code reviewer initial
                _SECURITY_BLOCKING,  # security reviewer block 1 → fix 1
                "APPROVED",  # code reviewer after fix 1
                _SECURITY_BLOCKING,  # security reviewer block 2 → fix 2
                "APPROVED",  # code reviewer after fix 2
                _SECURITY_BLOCKING,  # security reviewer block 3 → abort (iterations=2 >= max=2)
            ],
            agent_responses=[
                {"src/calculator.py": _CALC_V1},  # implementer initial
                {"src/calculator.py": _CALC_V1},  # implementer security fix 1
                {"src/calculator.py": _CALC_V1},  # implementer security fix 2
            ],
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add secure feature."))), _cfg(git_project))

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.done is False
        actions = [h["action"] for h in state.history]
        assert "abort" in actions


class TestAgentException:
    def test_implementer_exception_fails_task(self, git_project: Path):
        """An unexpected (non-RuntimeError) exception from an agent sets state.failed=True."""
        from core.llm_client import LLMClient as _LLMClient

        class _CrashingLLM(_LLMClient):
            def generate(self, system, user):
                return "SINGLE_PASS"

            def run_readonly_agent(self, prompt, cwd):
                return _ANALYST_PROMPT

            def run_agent(self, prompt, cwd):
                raise ValueError("Simulated unexpected crash in implementer")

        with patch("core.llm_client.create_llm_client", return_value=_CrashingLLM()):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Do something."))), _cfg(git_project))

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        actions = [h["action"] for h in state.history]
        assert "error" in actions

    @pytest.mark.parametrize(
        ("provider", "stdout", "stderr", "log_text", "expected_message"),
        [
            (
                "opencode",
                "",
                "",
                json.dumps(
                    {
                        "responseHeaders": {
                            "x-codex-credits-balance": "0",
                            "x-codex-credits-has-credits": "False",
                        },
                        "responseBody": {
                            "error": {
                                "type": "usage_limit_reached",
                                "message": "The usage limit has been reached",
                            }
                        },
                    }
                ),
                "usage_limit",
            ),
            (
                "codex",
                json.dumps({"type": "error", "message": "quota exceeded"}),
                "",
                "",
                "quota exceeded",
            ),
            ("claude", "", "not authenticated", "", "not authenticated"),
            ("gemini", "", "invalid model: gemini/nope", "", "invalid model"),
        ],
    )
    def test_streaming_fatal_provider_error_fails_implementer_without_retry(
        self,
        git_project: Path,
        provider: str,
        stdout: str,
        stderr: str,
        log_text: str,
        expected_message: str,
    ):
        """Full run with real provider clients fails on streamed fatal provider errors."""
        from core.llm_client import LLMClient as _LLMClient
        from core.llm_client import ClaudeClient, CodexClient, GeminiClient, OpenCodeClient

        class _PlanningLLM(_LLMClient):
            def generate(self, system, user):
                return "SINGLE_PASS"

            def run_readonly_agent(self, prompt, cwd):
                return _ANALYST_PROMPT

            def run_agent(self, prompt, cwd):
                raise AssertionError("Only the OpenCode implementer should run as a write agent")

        class _HangingFatalProcess:
            def __init__(self, *args, **kwargs) -> None:
                self.stdin = StringIO()
                self.stdout = StringIO(stdout)
                self.stderr = StringIO(stderr)
                self.returncode = None
                self.terminated = False
                if log_text:
                    log_dir = Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "log"
                    log_dir.mkdir(parents=True)
                    cmd = args[0]
                    (log_dir / "2026-06-07T000000.log").write_text(
                        f"INFO args={json.dumps(cmd[1:])} opencode\n{log_text}"
                    )

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        processes = []
        real_popen = subprocess.Popen

        def _popen(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            provider_commands = {
                "opencode": "opencode",
                "codex": "codex",
                "claude": "claude",
                "gemini": "gemini",
            }
            if not (isinstance(cmd, list) and cmd and cmd[0] == provider_commands[provider]):
                return real_popen(*args, **kwargs)
            process = _HangingFatalProcess(*args, **kwargs)
            processes.append(process)
            return process

        def _llm_factory(config):
            if config.provider == "opencode":
                return OpenCodeClient(config)
            if config.provider == "codex":
                return CodexClient(config)
            if config.provider == "claude":
                return ClaudeClient(config)
            if config.provider == "gemini":
                return GeminiClient(config)
            return _PlanningLLM()

        cfg = _cfg(git_project)
        cfg["llm"] = {"provider": "fake", "model": "fake"}
        cfg["agents"] = {
            "implementer": {
                "llm": {
                    "provider": provider,
                    "model": "test-model",
                    "agent_timeout": 30,
                }
            }
        }
        cfg["run_review"] = False
        cfg["run_security_review"] = False
        cfg["run_build"] = False
        cfg["run_tests"] = False
        cfg["run_checks"] = False

        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": str(git_project / ".xdg")}),
            patch("core.llm_client.create_llm_client", side_effect=_llm_factory),
            patch("core.llm_client.subprocess.Popen", side_effect=_popen),
            patch("core.llm_client.time.sleep") as sleep,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_run(_args(task_file=str(_task(git_project, f"Trigger {provider} fatal provider error."))), cfg)

        assert exc_info.value.code == 1
        assert processes[0].terminated is True
        sleep.assert_not_called()

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.done is False
        assert any(
            entry["agent"] == "implementer"
            and entry["action"] == "implement_failed"
            and expected_message in entry["result"]
            for entry in state.history
        )
        assert any(
            entry["agent"] == "orchestrator" and entry["action"] == "abort" and expected_message in entry["result"]
            for entry in state.history
        )


class TestAllStepsSkipped:
    def test_all_steps_skipped_fails_task(self, git_project: Path, seq_fake_llm):
        """Multi-step where every step writes nothing → task fails (consistent with single-pass)."""
        fake = seq_fake_llm(
            readonly_responses=[_ANALYST_PROMPT],  # analyst only; review never runs for skipped steps
            generate_responses=["1. Step one\n2. Step two"],  # planner → 2-step plan
            agent_responses=[],  # both implementer calls write nothing → step_skipped
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Multi-step that does nothing."))), _cfg(git_project))

        assert exc_info.value.code == 1

        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed is True
        assert state.done is False
        assert state.files_changed == []
        actions = [h["action"] for h in state.history]
        assert "abort" in actions


class TestWorktreeIsolation:
    def test_isolated_run_commits_to_branch(self, git_project: Path, fake_llm, capsys):
        """no_isolate=False: task runs in a worktree, commits on success, removes worktree."""
        import subprocess

        fake = fake_llm(
            agent_responses=[{"src/calculator.py": "def add(a, b): return a + b\ndef subtract(a, b): return a - b\n"}]
        )
        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(
                    _args(task_file=str(_task(git_project, "Add subtract function.")), no_isolate=False),
                    _cfg(git_project),
                )
        assert exc_info.value.code == 0

        # worktree directory removed after successful finalize
        worktrees_dir = git_project / ".sikula" / "worktrees"
        if worktrees_dir.exists():
            assert not any(worktrees_dir.iterdir())

        # a sikula/* branch was created in the repo
        result = subprocess.run(["git", "branch"], capture_output=True, text=True, cwd=git_project)
        assert "sikula/" in result.stdout

        # summary output includes branch name
        out = capsys.readouterr().out
        assert "Branch:" in out

    def test_isolated_run_preserves_worktree_on_failure(self, git_project: Path, fake_llm):
        """no_isolate=False: on failure the worktree is kept for inspection/resume."""
        fake = fake_llm(agent_responses=[])  # no changes → failed

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Do nothing.")), no_isolate=False), _cfg(git_project))
        assert exc_info.value.code == 1

        # worktree directory preserved
        worktrees_dir = git_project / ".sikula" / "worktrees"
        assert worktrees_dir.exists()
        assert any(True for _ in worktrees_dir.iterdir())


class TestResumeRun:
    def test_reset_failed_after_invalid_analyst_output_reruns_analysis(self, git_project: Path, seq_fake_llm):
        from core.state import JsonStateStore

        first_fake = seq_fake_llm(readonly_responses=[_BAD_ANALYST_META_PROMPT, _BAD_ANALYST_META_PROMPT])

        with patch("core.llm_client.create_llm_client", return_value=first_fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_file=str(_task(git_project, "Add subtract."))), _cfg(git_project))

        assert exc_info.value.code == 1

        store = JsonStateStore(git_project / ".sikula" / "state")
        task_id = store.list_tasks()[0]
        failed = store.load(task_id)
        assert failed.failed is True
        assert failed.implementation_prompt is None
        assert len(failed.analyst_retry_records) == 2

        second_fake = seq_fake_llm(
            readonly_responses=[_ANALYST_PROMPT],
            agent_responses=[{"src/calculator.py": _CALC_V1}],
        )

        with patch("core.llm_client.create_llm_client", return_value=second_fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_id=task_id, reset_failed=True), _cfg(git_project))

        assert exc_info.value.code == 0
        resumed = store.load(task_id)
        assert resumed.done is True
        assert resumed.failed is False
        assert resumed.implementation_prompt == _ANALYST_PROMPT
        assert resumed.files_changed == ["src/calculator.py"]
        assert len(resumed.analyst_retry_records) == 2

    def test_reset_failed_then_resumes_to_completion(self, git_project: Path, fake_llm):
        """--reset-failed clears the failed flag and the task runs to completion (covers line 565)."""
        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")
        failed = store.create("Add a subtract function.")
        failed.failed = True
        failed.plan_decided = True
        failed.files_changed = ["src/calculator.py"]  # non-empty → skip git-diff in reset
        store.save(failed)

        fake = fake_llm()  # no more writing needed — review/security only

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_id=failed.task_id, reset_failed=True), _cfg(git_project))

        assert exc_info.value.code == 0
        state = store.load(failed.task_id)
        assert state.done is True
        assert state.failed is False

    def test_partially_done_task_can_be_resumed(self, git_project: Path, fake_llm):
        """Resume a task where analyst + planner ran but implementer did not."""
        from core.state import JsonStateStore

        store = JsonStateStore(git_project / ".sikula" / "state")

        partial = store.create("Add a subtract function.")
        partial.implementation_prompt = "Add subtract(a, b) to src/calculator.py."
        partial.plan_decided = True  # planner ran; no plan list → single-pass
        store.save(partial)

        fake = fake_llm(
            agent_responses=[{"src/calculator.py": "def add(a, b): return a + b\ndef subtract(a, b): return a - b\n"}]
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit) as exc_info:
                cmd_run(_args(task_id=partial.task_id), _cfg(git_project))

        assert exc_info.value.code == 0
        state = store.load(partial.task_id)
        assert state.done is True
        assert "src/calculator.py" in state.files_changed
