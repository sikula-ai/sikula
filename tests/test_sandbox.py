"""Tests for tools/base_tool.py — Sandbox path enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.base_tool import Sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "other").mkdir()
    return Sandbox(
        project_root=tmp_path,
        allowed_write_paths=["src/"],
        allowed_read_paths=["."],
    )


class TestSandboxRead:
    def test_allows_project_root(self, sandbox: Sandbox, tmp_path: Path):
        sandbox.check_read(tmp_path / "README.md")  # must not raise

    def test_allows_subdir(self, sandbox: Sandbox, tmp_path: Path):
        sandbox.check_read(tmp_path / "src" / "main.py")

    def test_allows_relative_path(self, sandbox: Sandbox, tmp_path: Path):
        sandbox.check_read(Path("src/main.py"))

    def test_denies_path_outside_root(self, sandbox: Sandbox, tmp_path: Path):
        with pytest.raises(PermissionError, match="Sandbox read denied"):
            sandbox.check_read(tmp_path.parent / "outside.py")


class TestSandboxWrite:
    def test_allows_path_in_write_dir(self, sandbox: Sandbox, tmp_path: Path):
        sandbox.check_write(tmp_path / "src" / "new_file.py")

    def test_allows_nested_subdir_write(self, sandbox: Sandbox, tmp_path: Path):
        sandbox.check_write(tmp_path / "src" / "sub" / "nested.py")

    def test_allows_relative_write(self, sandbox: Sandbox):
        sandbox.check_write(Path("src/new_file.py"))

    def test_denies_write_outside_allowed(self, sandbox: Sandbox, tmp_path: Path):
        with pytest.raises(PermissionError, match="Sandbox write denied"):
            sandbox.check_write(tmp_path / "other" / "file.py")

    def test_denies_write_to_root(self, sandbox: Sandbox, tmp_path: Path):
        with pytest.raises(PermissionError, match="Sandbox write denied"):
            sandbox.check_write(tmp_path / "root_file.py")

    def test_denies_path_outside_project(self, sandbox: Sandbox, tmp_path: Path):
        with pytest.raises(PermissionError):
            sandbox.check_write(tmp_path.parent / "evil.py")
