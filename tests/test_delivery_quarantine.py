"""Tests for reversible delivery-file quarantine."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest

from core.delivery_quarantine import DeliveryQuarantineError
from tools.delivery_quarantine_tool import (
    delivery_quarantine_supported,
    quarantine_agent_created_file,
    remove_task_quarantine,
    task_quarantine_entry_retained,
    task_quarantine_summary,
)


pytestmark = pytest.mark.skipif(
    not delivery_quarantine_supported(),
    reason="descriptor-safe quarantine is unavailable",
)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def _provenance(path: Path, relative: str, session_id: str) -> dict[str, object]:
    value = path.stat(follow_symlinks=False)
    return {
        "path": relative,
        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(value.st_mode),
        "identity": [value.st_dev, value.st_ino],
        "session_id": session_id,
    }


def test_quarantine_moves_owned_untracked_file_without_unlink(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src" / "Scratch.kt"
    source.parent.mkdir()
    source.write_text("scratch\n", encoding="utf-8")
    session_id = "session-a"
    checkpoints: list[tuple[str, bool]] = []
    real_unlink = os.unlink

    def reject_unlink(*_args, **_kwargs):
        raise AssertionError("pipeline quarantine must not unlink content")

    monkeypatch.setattr(os, "unlink", reject_unlink)
    result = quarantine_agent_created_file(
        tmp_path,
        "task-a",
        "src/Scratch.kt",
        _provenance(source, "src/Scratch.kt", session_id),
        session_id=session_id,
        active_write_paths=["src"],
        exact_file_paths=[],
        before_move=lambda intent: checkpoints.append((str(intent["status"]), source.exists())),
        after_move=lambda _result: checkpoints.append(("complete", source.exists())),
    )
    monkeypatch.setattr(os, "unlink", real_unlink)

    assert result.path == "src/Scratch.kt"
    assert not source.exists()
    assert checkpoints == [("moving", True), ("complete", False)]
    assert task_quarantine_entry_retained(tmp_path, "task-a", result.quarantine_id) is True
    assert task_quarantine_summary(tmp_path, "task-a") == (1, len(b"scratch\n"))
    assert remove_task_quarantine(tmp_path, "task-a") == 1
    assert task_quarantine_entry_retained(tmp_path, "task-a", result.quarantine_id) is False
    assert task_quarantine_summary(tmp_path, "task-a") == (0, 0)


def test_quarantine_rejects_stale_identity(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "Scratch.kt"
    source.write_text("scratch\n", encoding="utf-8")
    provenance = _provenance(source, "Scratch.kt", "session-a")
    replacement = tmp_path / "replacement"
    replacement.write_text("scratch\n", encoding="utf-8")
    source.unlink()
    replacement.rename(source)

    with pytest.raises(DeliveryQuarantineError) as exc_info:
        quarantine_agent_created_file(
            tmp_path,
            "task-a",
            "Scratch.kt",
            provenance,
            session_id="session-a",
            active_write_paths=["."],
            exact_file_paths=[],
            before_move=lambda _intent: None,
            after_move=lambda _result: None,
        )

    assert exc_info.value.code == "delivery_quarantine.provenance_changed"
    assert source.exists()


def test_quarantine_supports_project_nested_below_git_root(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    project = tmp_path / "apps" / "service"
    source = project / "src" / "Scratch.kt"
    source.parent.mkdir(parents=True)
    source.write_text("scratch\n", encoding="utf-8")

    result = quarantine_agent_created_file(
        project,
        "task-a",
        "src/Scratch.kt",
        _provenance(source, "src/Scratch.kt", "session-a"),
        session_id="session-a",
        active_write_paths=["src"],
        exact_file_paths=[],
        git_root=tmp_path,
        before_move=lambda _intent: None,
        after_move=lambda _result: None,
    )

    assert result.path == "src/Scratch.kt"
    assert not source.exists()
    assert task_quarantine_summary(tmp_path, "task-a") == (1, len(b"scratch\n"))


def test_quarantine_rejects_indexed_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "Scratch.kt"
    source.write_text("scratch\n", encoding="utf-8")
    provenance = _provenance(source, "Scratch.kt", "session-a")
    subprocess.run(["git", "add", "Scratch.kt"], cwd=tmp_path, check=True)

    with pytest.raises(DeliveryQuarantineError) as exc_info:
        quarantine_agent_created_file(
            tmp_path,
            "task-a",
            "Scratch.kt",
            provenance,
            session_id="session-a",
            active_write_paths=["."],
            exact_file_paths=[],
            before_move=lambda _intent: None,
            after_move=lambda _result: None,
        )

    assert exc_info.value.code == "delivery_quarantine.indexed_path"
    assert source.exists()


def test_quarantine_ignores_inherited_alternate_git_index(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    alternate_index = tmp_path / ".alternate-index"
    alternate_env = dict(os.environ)
    alternate_env["GIT_INDEX_FILE"] = str(alternate_index)
    subprocess.run(
        ["git", "read-tree", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=alternate_env,
    )
    source = tmp_path / "Scratch.kt"
    source.write_text("scratch\n", encoding="utf-8")
    provenance = _provenance(source, "Scratch.kt", "session-a")
    subprocess.run(["git", "add", "Scratch.kt"], cwd=tmp_path, check=True)
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate_index))

    with pytest.raises(DeliveryQuarantineError) as exc_info:
        quarantine_agent_created_file(
            tmp_path,
            "task-a",
            "Scratch.kt",
            provenance,
            session_id="session-a",
            active_write_paths=["."],
            exact_file_paths=[],
            before_move=lambda _intent: None,
            after_move=lambda _result: None,
        )

    assert exc_info.value.code == "delivery_quarantine.indexed_path"
    assert source.exists()


def test_quarantine_checkpoint_failure_keeps_source(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "Scratch.kt"
    source.write_text("scratch\n", encoding="utf-8")

    def fail_checkpoint(_intent: dict[str, object]) -> None:
        raise OSError("state unavailable")

    with pytest.raises(DeliveryQuarantineError) as exc_info:
        quarantine_agent_created_file(
            tmp_path,
            "task-a",
            "Scratch.kt",
            _provenance(source, "Scratch.kt", "session-a"),
            session_id="session-a",
            active_write_paths=["."],
            exact_file_paths=[],
            before_move=fail_checkpoint,
            after_move=lambda _result: None,
        )

    assert exc_info.value.code == "delivery_quarantine.checkpoint_failed"
    assert source.exists()
