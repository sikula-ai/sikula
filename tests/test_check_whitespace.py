"""Tests for tools/check_whitespace.py."""

from __future__ import annotations

from pathlib import Path
import subprocess

from tools.check_whitespace import check_whitespace


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("clean\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def test_clean_tracked_and_untracked_text_passes(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("clean change\n")
    (tmp_path / "notes.md").write_text("new clean file\n")

    assert check_whitespace(tmp_path) == []


def test_detects_tracked_trailing_whitespace(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("bad tracked line  \n")

    errors = check_whitespace(tmp_path)

    assert any("README.md" in error and "trailing whitespace" in error for error in errors)


def test_detects_untracked_trailing_whitespace(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "new.md").write_text("bad untracked line  \n")

    errors = check_whitespace(tmp_path)

    assert errors == ["new.md:1: trailing whitespace."]


def test_detects_untracked_space_before_tab_in_indent(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "script.py").write_text(" \tprint('bad indent')\n")

    errors = check_whitespace(tmp_path)

    assert errors == ["script.py:1: space before tab in indent."]


def test_ignores_untracked_binary_files(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "image.bin").write_bytes(b"\x00bad trailing spaces  \n")

    assert check_whitespace(tmp_path) == []
