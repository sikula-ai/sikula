"""Fixtures and helpers for E2E tests."""

from __future__ import annotations

import argparse
import subprocess
from collections import deque
from pathlib import Path

import pytest

from core.llm_client import LLMClient


class FakeLLMClient(LLMClient):
    """Deterministic LLM client for E2E tests. No API keys or network required.

    - generate()           → generate_response (default: "SINGLE_PASS")
    - run_readonly_agent() → readonly_response  (default: "APPROVED")
    - run_agent()          → pops from agent_responses queue, writes files, returns paths
    """

    def __init__(
        self,
        agent_responses: list[dict[str, str]] | None = None,
        readonly_response: str = "APPROVED",
        generate_response: str = "SINGLE_PASS",
    ) -> None:
        self._agent_queue: deque[dict[str, str]] = deque(agent_responses or [])
        self._readonly_response = readonly_response
        self._generate_response = generate_response

    def generate(self, system: str, user: str) -> str:
        return self._generate_response

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        return self._readonly_response

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        if not self._agent_queue:
            return [], ""
        files = self._agent_queue.popleft()
        written: list[str] = []
        for rel_path, content in files.items():
            dest = cwd / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            written.append(rel_path)
        return sorted(written), ""


class SequencedFakeLLMClient(LLMClient):
    """FakeLLMClient with per-call response queues for all three LLM methods.

    Pops from each queue in order; falls back to the default value once the queue is empty.
    Useful for scenarios where the same method is called by different agents (e.g.
    run_readonly_agent is used by analyst, reviewer, and security_reviewer in sequence).
    """

    def __init__(
        self,
        readonly_responses: list[str] | None = None,
        agent_responses: list[dict[str, str]] | None = None,
        generate_responses: list[str] | None = None,
        default_readonly: str = "APPROVED",
        default_generate: str = "SINGLE_PASS",
    ) -> None:
        self._readonly_queue: deque[str] = deque(readonly_responses or [])
        self._agent_queue: deque[dict[str, str]] = deque(agent_responses or [])
        self._generate_queue: deque[str] = deque(generate_responses or [])
        self._default_readonly = default_readonly
        self._default_generate = default_generate

    def generate(self, system: str, user: str) -> str:
        return self._generate_queue.popleft() if self._generate_queue else self._default_generate

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        return self._readonly_queue.popleft() if self._readonly_queue else self._default_readonly

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        if not self._agent_queue:
            return [], ""
        files = self._agent_queue.popleft()
        written: list[str] = []
        for rel_path, content in files.items():
            dest = cwd / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            written.append(rel_path)
        return sorted(written), ""


@pytest.fixture()
def seq_fake_llm():
    """Return a SequencedFakeLLMClient factory.

    Usage: client = seq_fake_llm(
        readonly_responses=["analyst prompt", "ISSUES...", "APPROVED", "APPROVED"],
        agent_responses=[{"src/foo.py": "..."}],
    )
    """

    def _make(
        readonly_responses: list[str] | None = None,
        agent_responses: list[dict[str, str]] | None = None,
        generate_responses: list[str] | None = None,
        default_readonly: str = "APPROVED",
        default_generate: str = "SINGLE_PASS",
    ) -> SequencedFakeLLMClient:
        return SequencedFakeLLMClient(
            readonly_responses, agent_responses, generate_responses, default_readonly, default_generate
        )

    return _make


@pytest.fixture()
def fake_llm():
    """Return a FakeLLMClient factory.

    Usage: client = fake_llm(agent_responses=[{"src/foo.py": "..."}])
    """

    def _make(
        agent_responses: list[dict[str, str]] | None = None,
        readonly_response: str = "APPROVED",
        generate_response: str = "SINGLE_PASS",
    ) -> FakeLLMClient:
        return FakeLLMClient(agent_responses, readonly_response, generate_response)

    return _make


@pytest.fixture()
def git_project(tmp_path: Path) -> Path:
    """Minimal Python project inside a freshly initialised git repo."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests_proj").mkdir()
    (tmp_path / "tests_proj" / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')

    # Use -c to set defaultBranch without requiring git ≥ 2.28 --initial-branch flag
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


@pytest.fixture()
def git_review_project(git_project: Path) -> tuple[Path, str]:
    """git_project with a committed feature branch ready for review.

    Returns (project_root, feature_branch_name).
    """
    branch = "feature/add-subtract"
    subprocess.run(["git", "checkout", "-b", branch], cwd=git_project, check=True, capture_output=True)
    (git_project / "src" / "calculator.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add subtract"], cwd=git_project, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_project, check=True, capture_output=True)
    return git_project, branch


def e2e_cfg(project_root: Path) -> dict:
    """Minimal Sikula config dict for a Python project (build disabled for speed)."""
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
        "tasks": {
            "state_dir": str(project_root / ".sikula" / "state"),
        },
        "build": {
            "compile_command": "python3 -m compileall -q .",
            "test_command": "python3 -m pytest",
            "timeout": 120,
        },
        "guidelines": {
            "context_files": [],
            "max_file_chars": 3000,
        },
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


def run_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace for cmd_run with sensible E2E defaults."""
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


def review_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace for cmd_review with sensible E2E defaults."""
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
