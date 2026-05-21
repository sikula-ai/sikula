"""Tests for tools/file_tool.py — FileTool read/write with sandbox enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.base_tool import Sandbox
from tools.file_tool import FileTool


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n")
    return tmp_path


@pytest.fixture
def file_tool(project: Path) -> FileTool:
    sandbox = Sandbox(
        project_root=project,
        allowed_write_paths=["src/"],
        allowed_read_paths=["."],
    )
    return FileTool(sandbox=sandbox, project_root=project)


class TestFileTool:
    def test_read_returns_content(self, file_tool: FileTool):
        result = file_tool.read("src/main.py")
        assert result.success
        assert "print('hello')" in result.output

    def test_read_denied_outside_sandbox(self, file_tool: FileTool, project: Path):
        result = file_tool.read(str(project.parent / "outside.py"))
        assert not result.success
        assert "Sandbox read denied" in result.error

    def test_read_missing_file_fails_gracefully(self, file_tool: FileTool):
        result = file_tool.read("src/missing.py")
        assert not result.success
        assert result.error

    def test_write_creates_file(self, file_tool: FileTool, project: Path):
        result = file_tool.write("src/new_module.py", "x = 1\n")
        assert result.success
        assert (project / "src" / "new_module.py").read_text() == "x = 1\n"

    def test_write_creates_parent_dirs(self, file_tool: FileTool, project: Path):
        result = file_tool.write("src/sub/deep/file.py", "pass\n")
        assert result.success
        assert (project / "src" / "sub" / "deep" / "file.py").exists()

    def test_write_denied_outside_allowed(self, file_tool: FileTool):
        result = file_tool.write("root_level.py", "x = 1\n")
        assert not result.success
        assert "Sandbox write denied" in result.error

    def test_root_attribute_is_project_path(self, file_tool: FileTool, project: Path):
        assert file_tool._root == project
