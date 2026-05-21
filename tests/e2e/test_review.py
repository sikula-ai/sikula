"""E2E tests for the sikula review command using FakeLLMClient."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.llm_client import LLMClient
from core.state import JsonStateStore
from sikula import cmd_review


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
        "run_security_review": True,
        "planner": {"max_steps": 4},
    }


def _args(**kwargs) -> argparse.Namespace:
    defaults: dict = {
        "branch": None,
        "base_branch": "main",
        "description": None,
        "description_file": None,
        "fix": False,
        "security_review": None,
        "agent_model": None,
        "agent_provider": None,
        "agent_timeout": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _invoke(project: Path, branch: str, fake, base_branch: str = "main") -> int:
    """Call cmd_review (report-only) with a patched LLM. Returns the SystemExit code."""
    with patch("core.llm_client.create_llm_client", return_value=fake):
        with pytest.raises(SystemExit) as exc_info:
            cmd_review(
                _args(
                    branch=branch,
                    base_branch=base_branch,
                    description="Review calculator branch changes",
                ),
                _cfg(project),
            )
    return exc_info.value.code


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReportOnlyReview:
    def test_approved_review_exits_0(self, git_review_project, fake_llm):
        project, branch = git_review_project
        fake = fake_llm(readonly_response="Callers verified: none\nAPPROVED")
        assert _invoke(project, branch, fake) == 0

    def test_rejected_review_exits_1(self, git_review_project, fake_llm):
        project, branch = git_review_project
        fake = fake_llm(
            readonly_response=(
                "## Issues\n\n"
                "### Missing error handling\n"
                "File: src/calculator.py\n"
                "Problem: divide by zero not handled\n"
                "Fix: raise ValueError when b is zero"
            )
        )
        assert _invoke(project, branch, fake) == 1

    def test_approved_review_records_done_state(self, git_review_project, fake_llm):
        project, branch = git_review_project
        fake = fake_llm(readonly_response="APPROVED")

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit):
                cmd_review(
                    _args(
                        branch=branch,
                        base_branch="main",
                        description="Review calculator branch changes",
                    ),
                    _cfg(project),
                )

        from core.state import JsonStateStore

        store = JsonStateStore(project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.review_approved is True
        assert state.security_approved is True
        assert state.done is True

    def test_rejected_review_records_failed_state(self, git_review_project, fake_llm):
        project, branch = git_review_project
        fake = fake_llm(
            readonly_response=(
                "## Issues\n\n### Bad code\nFile: src/calculator.py\nProblem: something wrong\nFix: fix it"
            )
        )

        with patch("core.llm_client.create_llm_client", return_value=fake):
            with pytest.raises(SystemExit):
                cmd_review(
                    _args(
                        branch=branch,
                        base_branch="main",
                        description="Review calculator branch changes",
                    ),
                    _cfg(project),
                )

        from core.state import JsonStateStore

        store = JsonStateStore(project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.review_approved is False
        assert state.failed is True

    def test_security_warnings_are_non_blocking(self, git_review_project, fake_llm):
        """A security review with only warnings (no ## Security Issues) should still pass."""
        project, branch = git_review_project

        # Reviewer approves; security reviewer returns warnings only
        call_count = 0
        responses = [
            "APPROVED",
            "## Warnings\n\n### Minor concern\nFile: src/calculator.py\nConcern: abc\nSuggestion: xyz",
        ]

        from core.llm_client import LLMClient

        class SequencedFake(LLMClient):
            def generate(self, system, user):
                return "SINGLE_PASS"

            def run_readonly_agent(self, prompt, cwd):
                nonlocal call_count
                resp = responses[min(call_count, len(responses) - 1)]
                call_count += 1
                return resp

            def run_agent(self, prompt, cwd):
                return [], ""

        with (
            patch("core.llm_client.create_llm_client", return_value=SequencedFake()),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_review(
                    _args(
                        branch=branch,
                        base_branch="main",
                        description="Review calculator branch changes",
                    ),
                    _cfg(project),
                )

        assert exc_info.value.code == 0


class TestReviewFix:
    def test_fix_mode_refreshes_diff_after_implementer_fix(self, git_review_project):
        project, branch = git_review_project

        class ReviewFixFake(LLMClient):
            def __init__(self) -> None:
                self.reviewer_prompts: list[str] = []
                self.readonly_calls = 0

            def generate(self, system, user):
                return "SINGLE_PASS"

            def run_readonly_agent(self, prompt, cwd):
                self.readonly_calls += 1
                if "Your job: verify that a task implementation is complete" in prompt:
                    self.reviewer_prompts.append(prompt)
                    if len(self.reviewer_prompts) == 1:
                        return (
                            "## Issues\n\n"
                            "### Missing divide implementation\n"
                            "File: src/calculator.py\n"
                            "Problem: divide is missing\n"
                            "Fix: add divide"
                        )
                    assert "def divide(a, b):" in prompt
                    return "Correctness: divide is present in the refreshed diff\nAPPROVED"
                return "APPROVED"

            def run_agent(self, prompt, cwd):
                calculator = cwd / "src" / "calculator.py"
                calculator.write_text(
                    "def add(a, b):\n"
                    "    return a + b\n\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n\n"
                    "def divide(a, b):\n"
                    "    return a / b\n"
                )
                return ["src/calculator.py"], "added divide"

        fake = ReviewFixFake()
        cfg = _cfg(project)
        cfg["run_build"] = False
        cfg["run_security_review"] = False

        with (
            patch("core.llm_client.create_llm_client", return_value=fake),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_review(
                    _args(
                        branch=branch,
                        base_branch="main",
                        description="Review calculator branch changes and ensure divide exists.",
                        fix=True,
                    ),
                    cfg,
                )

        assert exc_info.value.code == 0
        assert len(fake.reviewer_prompts) == 2
        assert "def divide(a, b):" not in fake.reviewer_prompts[0]
        assert "def divide(a, b):" in fake.reviewer_prompts[1]

        store = JsonStateStore(project / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done is True
        assert state.review_mode == "review_fix"
        assert state.review_base_branch == "main"
        assert "def divide(a, b):" in state.review_diff

        committed = subprocess.run(
            ["git", "show", f"{branch}:src/calculator.py"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "def divide(a, b):" in committed.stdout
