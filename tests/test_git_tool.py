"""Tests for tools/git_tool.py — GitTool wraps git subprocess calls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.git_tool import GitTool


@pytest.fixture
def git_tool(tmp_project: Path) -> GitTool:
    sandbox = Sandbox(project_root=tmp_project, allowed_write_paths=["."], allowed_read_paths=["."])
    return GitTool(sandbox=sandbox, project_root=tmp_project)


class TestGitToolIntegration:
    """Integration tests that run real git commands in the tmp_project fixture."""

    def test_diff_head_clean(self, git_tool: GitTool):
        result = git_tool.diff_head()
        assert result.success
        assert result.output.strip() == ""

    def test_diff_head_with_modification(self, git_tool: GitTool, tmp_project: Path):
        (tmp_project / "src" / "main.py").write_text("# modified\n")
        result = git_tool.diff_head()
        assert result.success
        assert "main.py" in result.output


class TestGitToolSubprocessMock:
    """Unit tests using subprocess mocks — test error handling without real git."""

    def _make_tool(self, tmp_path: Path) -> GitTool:
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        return GitTool(sandbox=sandbox, project_root=tmp_path)

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = self._make_tool(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "fatal: not a git repository"
        with patch("tools.git_tool.subprocess.run", return_value=mock_result):
            result = tool.diff_head()
        assert not result.success
        assert "fatal" in result.error

    def test_subprocess_exception_returns_failure(self, tmp_path: Path):
        tool = self._make_tool(tmp_path)
        with patch("tools.git_tool.subprocess.run", side_effect=OSError("git not found")):
            result = tool.diff_head()
        assert not result.success
        assert "git not found" in result.error
