from __future__ import annotations

from pathlib import Path

import pytest

from core.worktree import WorktreeEnvironmentCopyError, copy_worktree_environment_file


def test_copy_worktree_environment_file_copies_missing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.properties"
    source.write_text("sdk.dir=/opt/android-sdk\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    destination = worktree / "local.properties"

    assert copy_worktree_environment_file(source, destination, worktree) is True
    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_copy_worktree_environment_file_rejects_dangling_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.properties"
    source.write_text("sdk.dir=/opt/android-sdk\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    external = tmp_path / "external" / "local.properties"
    try:
        (worktree / "local.properties").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")

    with pytest.raises(WorktreeEnvironmentCopyError, match="contains a symlink"):
        copy_worktree_environment_file(source, worktree / "local.properties", worktree)

    assert not external.exists()


def test_copy_worktree_environment_file_rejects_symlinked_parent(tmp_path: Path) -> None:
    source = tmp_path / "source.env"
    source.write_text("TOKEN=local\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    try:
        (worktree / "config").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")

    with pytest.raises(WorktreeEnvironmentCopyError, match="contains a symlink"):
        copy_worktree_environment_file(source, worktree / "config" / ".env", worktree)

    assert not (external / ".env").exists()
