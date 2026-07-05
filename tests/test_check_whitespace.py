"""Tests for tools/check_whitespace.py."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

from tools import check_whitespace as check_whitespace_module
from tools.check_whitespace import _untracked_file_errors
from tools.check_whitespace import _untracked_files
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


def test_reports_when_not_inside_git_repo(tmp_path: Path):
    errors = check_whitespace(tmp_path)

    assert errors
    assert any("not a git repository" in error.lower() for error in errors)


def test_checks_untracked_files_before_first_commit(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "new.md").write_bytes(b"bad before first commit  \r\n")

    assert check_whitespace(tmp_path) == ["new.md:1: trailing whitespace."]


def test_handles_untracked_file_without_line_ending(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "notes.md").write_bytes(b"clean final line")

    assert check_whitespace(tmp_path) == []


def test_ignores_untracked_symlinks(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "readme-link").symlink_to("README.md")

    assert check_whitespace(tmp_path) == []


def test_reports_unreadable_untracked_file(tmp_path: Path):
    path = tmp_path / "locked.md"
    path.write_text("content\n")

    with patch.object(Path, "read_bytes", side_effect=OSError("permission denied")):
        errors = _untracked_file_errors(tmp_path, Path("locked.md"))

    assert errors == ["locked.md: could not read file: permission denied"]


def test_reports_untracked_file_listing_errors(tmp_path: Path, monkeypatch):
    def fail_git(root: Path, args: list[str], *, text: bool = True):
        stdout = "" if text else b""
        stderr = "fatal: index read failed\n" if text else b"fatal: index read failed\n"
        return subprocess.CompletedProcess(args, 128, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(check_whitespace_module, "_git", fail_git)

    paths, errors = _untracked_files(tmp_path)

    assert paths == []
    assert errors == ["fatal: index read failed"]


def test_main_returns_zero_when_clean(monkeypatch):
    monkeypatch.setattr(check_whitespace_module, "check_whitespace", lambda: [])

    assert check_whitespace_module.main() == 0


def test_main_prints_errors_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(check_whitespace_module, "check_whitespace", lambda: ["bad.md:1: trailing whitespace."])

    assert check_whitespace_module.main() == 1
    assert "bad.md:1: trailing whitespace." in capsys.readouterr().err
