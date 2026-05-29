"""Detect and clean repository changes produced by validation commands."""

from __future__ import annotations

import subprocess
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSnapshot:
    status: str
    exists: bool
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class ValidationArtifact:
    path: str
    before_status: str
    after_status: str

    def to_record(self) -> dict[str, str]:
        return {
            "path": self.path,
            "before_status": self.before_status,
            "after_status": self.after_status,
        }


def _git_lines(cwd: Path, args: list[str]) -> list[str] | None:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _dirty_paths(cwd: Path) -> tuple[set[str], set[str]] | None:
    modified = _git_lines(cwd, ["diff", "--name-only", "--relative", "HEAD", "--", "."])
    staged = _git_lines(cwd, ["diff", "--cached", "--name-only", "--relative", "HEAD", "--", "."])
    untracked = _git_lines(cwd, ["ls-files", "--others", "--exclude-standard", "--", "."])
    if modified is None or staged is None or untracked is None:
        return None
    tracked_paths = set(modified) | set(staged)
    untracked_paths = set(untracked)
    return tracked_paths | untracked_paths, untracked_paths


def _safe_project_path(cwd: Path, relative_path: str) -> Path | None:
    root = cwd.resolve()
    if Path(relative_path).is_absolute():
        return None
    normalized = _normalize_relative_path(relative_path)
    if not normalized:
        return None
    path_parts = Path(normalized).parts
    if any(part == ".." for part in path_parts):
        return None
    path = root.joinpath(*path_parts)
    parent = path.parent.resolve(strict=False)
    if parent == root or root in parent.parents:
        return path
    return None


def _normalize_relative_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _path_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_artifact_path(path: Path) -> str | None:
    if not _path_entry_exists(path):
        return None
    if path.is_dir() and not path.is_symlink():
        return "artifact path is a directory"
    path.unlink()
    return None


def _is_ignored_root(path: str, ignored_roots: tuple[str, ...]) -> bool:
    normalized = _normalize_relative_path(path)
    return any(normalized == root or normalized.startswith(f"{root}/") for root in ignored_roots)


def snapshot_validation_dirty_files(
    cwd: Path,
    *,
    ignored_roots: Iterable[str] | None = None,
) -> dict[str, FileSnapshot]:
    """Return non-ignored dirty files and their current bytes.

    Ignored files are intentionally excluded through git's exclude-standard rules.
    This keeps normal build caches, coverage folders, and platform output directories
    out of the artifact guard when projects ignore them correctly.
    """

    dirty = _dirty_paths(cwd)
    if dirty is None:
        return {}
    paths, untracked_paths = dirty
    normalized_ignored_roots = tuple(
        root for root in (_normalize_relative_path(path) for path in (ignored_roots or ())) if root and root != "."
    )
    snapshot: dict[str, FileSnapshot] = {}
    for path in sorted(paths):
        if _is_ignored_root(path, normalized_ignored_roots):
            continue
        project_path = _safe_project_path(cwd, path)
        if project_path is None:
            continue
        status = "untracked" if path in untracked_paths else "tracked"
        try:
            content = project_path.read_bytes()
            exists = True
        except FileNotFoundError:
            content = None
            exists = project_path.is_symlink()
        except (IsADirectoryError, PermissionError):
            content = None
            exists = True
        snapshot[path] = FileSnapshot(
            status=status,
            exists=exists,
            content=content,
            mode=_file_mode(project_path) if exists else None,
        )
    return snapshot


def detect_validation_artifacts(
    before: dict[str, FileSnapshot],
    after: dict[str, FileSnapshot],
) -> list[ValidationArtifact]:
    artifacts: list[ValidationArtifact] = []
    for path in sorted(set(before) | set(after)):
        before_snapshot = before.get(path)
        after_snapshot = after.get(path)
        if before_snapshot == after_snapshot:
            continue
        artifacts.append(
            ValidationArtifact(
                path=path,
                before_status=before_snapshot.status if before_snapshot else "clean",
                after_status=after_snapshot.status if after_snapshot else "clean",
            )
        )
    return artifacts


def _restore_head_file(cwd: Path, relative_path: str) -> str | None:
    result = subprocess.run(
        ["git", "checkout", "HEAD", "--", relative_path],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "git checkout failed").strip()


def _restore_file_mode(path: Path, mode: int | None) -> None:
    if mode is not None:
        path.chmod(mode)


def restore_validation_artifacts(
    cwd: Path,
    before: dict[str, FileSnapshot],
    artifacts: list[ValidationArtifact],
) -> list[str]:
    """Restore changed paths to their pre-validation state.

    Returns a list of cleanup errors. The caller decides whether those errors should
    fail validation or be handed to the fixer.
    """

    errors: list[str] = []
    for artifact in artifacts:
        project_path = _safe_project_path(cwd, artifact.path)
        if project_path is None:
            errors.append(f"{artifact.path}: path resolves outside project root")
            continue
        before_snapshot = before.get(artifact.path)
        try:
            if before_snapshot is None:
                if artifact.after_status == "tracked":
                    restore_error = _restore_head_file(cwd, artifact.path)
                    if restore_error:
                        errors.append(f"{artifact.path}: {restore_error}")
                    continue
                remove_error = _remove_artifact_path(project_path)
                if remove_error:
                    errors.append(f"{artifact.path}: {remove_error}")
                continue

            if not before_snapshot.exists:
                remove_error = _remove_artifact_path(project_path)
                if remove_error:
                    errors.append(f"{artifact.path}: {remove_error}")
                continue

            if before_snapshot.content is None:
                if artifact.before_status == "tracked":
                    restore_error = _restore_head_file(cwd, artifact.path)
                    if restore_error:
                        errors.append(f"{artifact.path}: {restore_error}")
                    continue
                remove_error = _remove_artifact_path(project_path)
                if remove_error:
                    errors.append(f"{artifact.path}: {remove_error}")
                continue

            project_path.parent.mkdir(parents=True, exist_ok=True)
            project_path.write_bytes(before_snapshot.content)
            _restore_file_mode(project_path, before_snapshot.mode)
        except OSError as exc:
            errors.append(f"{artifact.path}: {exc}")
    return errors
