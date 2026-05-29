"""Detect and clean repository changes produced by validation commands."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSnapshot:
    status: str
    exists: bool
    content: bytes | None


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
    path = (root / relative_path).resolve(strict=False)
    if path == root or root in path.parents:
        return path
    return None


def _normalize_relative_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


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
        root
        for root in (_normalize_relative_path(path) for path in (ignored_roots or ()))
        if root and root != "."
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
            exists = False
        except (IsADirectoryError, PermissionError):
            content = None
            exists = True
        snapshot[path] = FileSnapshot(status=status, exists=exists, content=content)
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
                if project_path.exists():
                    if project_path.is_dir():
                        errors.append(f"{artifact.path}: artifact path is a directory")
                    else:
                        project_path.unlink()
                continue

            if not before_snapshot.exists:
                if project_path.exists():
                    if project_path.is_dir():
                        errors.append(f"{artifact.path}: artifact path is a directory")
                    else:
                        project_path.unlink()
                continue

            if before_snapshot.content is None:
                if artifact.before_status == "tracked":
                    restore_error = _restore_head_file(cwd, artifact.path)
                    if restore_error:
                        errors.append(f"{artifact.path}: {restore_error}")
                    continue
                if project_path.exists():
                    if project_path.is_dir():
                        errors.append(f"{artifact.path}: artifact path is a directory")
                    else:
                        project_path.unlink()
                continue

            project_path.parent.mkdir(parents=True, exist_ok=True)
            project_path.write_bytes(before_snapshot.content)
        except OSError as exc:
            errors.append(f"{artifact.path}: {exc}")
    return errors
