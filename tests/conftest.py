"""Shared fixtures for all Sikula tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.llm_client import LLMClient
from core.state import TaskState
from tools.base_tool import Sandbox
from tools.file_tool import FileTool
from tools.git_tool import GitTool


# ---------------------------------------------------------------------------
# Stub LLM client
# ---------------------------------------------------------------------------


class StubLLMClient(LLMClient):
    """Configurable stub — set .generate_result, .readonly_result, .agent_result before use.

    Raises the stored exception if an attribute ending in _error is set.
    """

    def __init__(self) -> None:
        self.generate_result: str = ""
        self.readonly_result: str = ""
        self.readonly_results: list[str] = []
        self.agent_result: list[str] = []
        self.agent_output: str = ""
        self.generate_error: Exception | None = None
        self.readonly_error: Exception | None = None
        self.agent_error: Exception | None = None
        self.generate_calls: list[tuple[str, str]] = []
        self.readonly_calls: list[str] = []
        self.agent_calls: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.generate_calls.append((system, user))
        if self.generate_error:
            raise self.generate_error
        return self.generate_result

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.readonly_calls.append(prompt)
        if self.readonly_error:
            raise self.readonly_error
        if self.readonly_results:
            return self.readonly_results.pop(0)
        return self.readonly_result

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self.agent_calls.append(prompt)
        if self.agent_error:
            raise self.agent_error
        return self.agent_result, self.agent_output


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_llm() -> StubLLMClient:
    return StubLLMClient()


@pytest.fixture
def task_state() -> TaskState:
    return TaskState(task_id="test-task-1", task_description="Add a login screen")


@pytest.fixture
def minimal_project_config() -> dict[str, Any]:
    return {
        "project": {"name": "test-project"},
        "guidelines": {"context_files": [], "max_file_chars": 5000},
        "planner": {"max_steps": 6},
    }


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Minimal git repo suitable for tool tests."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def sandbox(tmp_project: Path) -> Sandbox:
    return Sandbox(
        project_root=tmp_project,
        allowed_write_paths=["src/"],
        allowed_read_paths=["."],
    )


@pytest.fixture
def file_tool(sandbox: Sandbox, tmp_project: Path) -> FileTool:
    return FileTool(sandbox=sandbox, project_root=tmp_project)


@pytest.fixture
def git_tool(sandbox: Sandbox, tmp_project: Path) -> GitTool:
    return GitTool(sandbox=sandbox, project_root=tmp_project)
