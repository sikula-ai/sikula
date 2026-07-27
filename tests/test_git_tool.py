"""Tests for tools/git_tool.py — GitTool wraps git subprocess calls."""

from __future__ import annotations

import subprocess
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

    def test_diff_head_can_be_scoped_to_selected_paths(self, git_tool: GitTool, tmp_project: Path):
        other = tmp_project / "src" / "other.py"
        other.write_text("# other\n")
        subprocess.run(["git", "add", "."], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add other"], cwd=tmp_project, check=True, capture_output=True)
        (tmp_project / "src" / "main.py").write_text("# modified main\n")
        other.write_text("# modified other\n")

        result = git_tool.diff_head(["src/other.py"])

        assert result.success
        assert "other.py" in result.output
        assert "main.py" not in result.output

    def test_scoped_paths_are_relative_to_subproject_root(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "app"
        source = project / "src"
        source.mkdir(parents=True)
        selected = source / "selected[1].py"
        other = source / "other.py"
        selected.write_text("# selected\n")
        other.write_text("# other\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        selected.write_text("# changed selected\n")
        other.write_text("# changed other\n")
        sandbox = Sandbox(project_root=project, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = GitTool(sandbox=sandbox, project_root=project)

        result = tool.diff_head(["src/selected[1].py"])

        assert result.success
        assert "src/selected[1].py" in result.output
        assert "src/other.py" not in result.output


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

    def test_scoped_paths_follow_end_of_options_marker(self, tmp_path: Path):
        tool = self._make_tool(tmp_path)
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("tools.git_tool.subprocess.run", return_value=mock_result) as run:
            tool.diff_head(["-unsafe", "src/main.py"])

        assert run.call_args.args[0] == [
            "git",
            "--literal-pathspecs",
            "diff",
            "--relative",
            "HEAD",
            "--",
            "-unsafe",
            "src/main.py",
        ]

    @pytest.mark.parametrize("paths", [["../outside.py"], "src/main.py", [True]])
    def test_invalid_scoped_paths_fail_without_running_git(self, tmp_path: Path, paths):
        tool = self._make_tool(tmp_path)
        with patch("tools.git_tool.subprocess.run") as run:
            result = tool.diff_head(paths)

        assert not result.success
        run.assert_not_called()

    def test_absolute_scoped_paths_fail_without_running_git(self, tmp_path: Path):
        tool = self._make_tool(tmp_path)
        outside = str(tmp_path.parent / "outside.py")
        with patch("tools.git_tool.subprocess.run") as run:
            result = tool.diff_head([outside])

        assert not result.success
        run.assert_not_called()
