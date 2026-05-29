"""Tests for validation artifact snapshot/restore helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.validation_artifacts import (
    FileSnapshot,
    ValidationArtifact,
    restore_validation_artifacts,
    snapshot_validation_dirty_files,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "src").mkdir()
    (path / "src" / "main.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _artifact(path: str, before: str = "tracked", after: str = "tracked") -> ValidationArtifact:
    return ValidationArtifact(path=path, before_status=before, after_status=after)


def _make_symlink(path: Path, target: str = "missing-target") -> None:
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def test_snapshot_returns_empty_when_git_command_fails(tmp_path: Path, monkeypatch):
    from core import validation_artifacts

    def fail_git(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="fatal: not a repository")

    monkeypatch.setattr(validation_artifacts.subprocess, "run", fail_git)

    assert snapshot_validation_dirty_files(tmp_path) == {}


def test_snapshot_skips_ignored_roots_and_records_deleted_files(tmp_path: Path):
    _init_repo(tmp_path)
    source = tmp_path / "src" / "main.py"
    source.unlink()
    ignored = tmp_path / "reports" / "runtime.txt"
    ignored.parent.mkdir()
    ignored.write_text("generated\n")

    snapshot = snapshot_validation_dirty_files(tmp_path, ignored_roots=["reports"])

    assert set(snapshot) == {"src/main.py"}
    assert snapshot["src/main.py"] == FileSnapshot(
        status="tracked",
        exists=False,
        content=None,
        mode=None,
    )


def test_snapshot_records_directory_like_dirty_path(tmp_path: Path, monkeypatch):
    from core import validation_artifacts

    dirty_dir = tmp_path / "generated"
    dirty_dir.mkdir()
    monkeypatch.setattr(validation_artifacts, "_dirty_paths", lambda cwd: ({"generated"}, {"generated"}))

    snapshot = snapshot_validation_dirty_files(tmp_path)

    entry = snapshot["generated"]
    assert entry.status == "untracked"
    assert entry.exists
    assert entry.content is None
    assert entry.mode is not None


def test_snapshot_records_broken_symlink_dirty_path(tmp_path: Path):
    _init_repo(tmp_path)
    link = tmp_path / "generated-link"
    _make_symlink(link)

    snapshot = snapshot_validation_dirty_files(tmp_path)

    assert snapshot["generated-link"] == FileSnapshot(
        status="untracked",
        exists=True,
        content=None,
        mode=None,
        symlink_target="missing-target",
    )


def test_restore_rejects_paths_outside_project_root(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("../outside.txt", before="clean", after="untracked")],
    )

    assert errors == ["../outside.txt: path resolves outside project root"]


def test_restore_deletes_new_untracked_file(tmp_path: Path):
    artifact = tmp_path / "generated.txt"
    artifact.write_text("generated\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated.txt", before="clean", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()


def test_restore_deletes_new_broken_symlink_artifact(tmp_path: Path):
    artifact = tmp_path / "generated-link"
    _make_symlink(artifact)

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated-link", before="clean", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()
    assert not artifact.is_symlink()


def test_restore_reports_directory_for_new_untracked_artifact(tmp_path: Path):
    artifact = tmp_path / "generated"
    artifact.mkdir()

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated", before="clean", after="untracked")],
    )

    assert errors == ["generated: artifact path is a directory"]


def test_restore_clean_tracked_file_from_head(tmp_path: Path):
    _init_repo(tmp_path)
    source = tmp_path / "src" / "main.py"
    source.write_text("# generated during validation\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("src/main.py", before="clean", after="tracked")],
    )

    assert errors == []
    assert source.read_text() == "# placeholder\n"


def test_restore_records_head_restore_error(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("src/main.py", before="clean", after="tracked")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")


def test_restore_deletes_file_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("# generated during validation\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.exists()


def test_restore_deletes_broken_symlink_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    _make_symlink(source)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.exists()
    assert not source.is_symlink()


def test_restore_reports_directory_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.mkdir(parents=True)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == ["src/main.py: artifact path is a directory"]


def test_restore_recreates_dirty_symlink_replaced_by_file_without_touching_target(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-target.txt"
    outside.write_text("outside original\n")
    source = tmp_path / "generated-link"
    _make_symlink(source, str(outside))
    source.unlink()
    source.write_text("regular file artifact\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={
            "generated-link": FileSnapshot(
                status="untracked",
                exists=True,
                content=None,
                mode=None,
                symlink_target=str(outside),
            )
        },
        artifacts=[_artifact("generated-link", before="untracked", after="untracked")],
    )

    assert errors == []
    assert source.is_symlink()
    assert str(source.readlink()) == str(outside)
    assert outside.read_text() == "outside original\n"


def test_restore_regular_file_replaced_by_symlink_does_not_write_through_target(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-target.txt"
    outside.write_text("outside original\n")
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    _make_symlink(source, str(outside))

    errors = restore_validation_artifacts(
        tmp_path,
        before={
            "src/main.py": FileSnapshot(
                status="tracked",
                exists=True,
                content=b"# task change\n",
                mode=0o644,
            )
        },
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.is_symlink()
    assert source.read_text() == "# task change\n"
    assert outside.read_text() == "outside original\n"


def test_restore_deletes_existing_untracked_file_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated.txt"
    artifact.write_text("generated\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated.txt": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated.txt", before="untracked", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()


def test_restore_deletes_existing_untracked_broken_symlink_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated-link"
    _make_symlink(artifact)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated-link": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated-link", before="untracked", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()
    assert not artifact.is_symlink()


def test_restore_reports_existing_untracked_directory_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated"
    artifact.mkdir()

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated", before="untracked", after="untracked")],
    )

    assert errors == ["generated: artifact path is a directory"]


def test_restore_records_error_when_existing_tracked_snapshot_cannot_restore_from_head(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")


def test_restore_records_os_error_when_content_path_is_directory(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.mkdir(parents=True)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=True, content=b"# task change\n", mode=0o644)},
        artifacts=[_artifact("src/main.py")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")
