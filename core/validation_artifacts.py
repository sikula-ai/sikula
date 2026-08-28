"""Detect and clean repository changes produced by validation commands."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


_MAX_DELIVERY_SCOPE_RETAINED_CONTENT_BYTES = 1024 * 1024
_DELIVERY_SCOPE_GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class FileSnapshot:
    status: str
    exists: bool
    content: bytes | None
    mode: int | None
    symlink_target: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class DeliveryScopeGitBinding:
    baseline: str
    git_dir: str
    common_dir: str
    ignore_fingerprint: str
    ref_fingerprint: str


class DeliveryScopeSnapshotError(RuntimeError):
    """Raised when a complete delivery-scope filesystem snapshot cannot be trusted."""


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


def _git_lines(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            cwd=cwd,
            check=False,
            env=env,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    if not isinstance(result.stdout, bytes):
        return None
    return [os.fsdecode(line) for line in result.stdout.splitlines() if line]


def _git_paths_z(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            cwd=cwd,
            check=False,
            env=env,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    if not isinstance(result.stdout, bytes):
        return None
    try:
        return [os.fsdecode(value) for value in result.stdout.split(b"\0") if value]
    except UnicodeDecodeError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not decode a Git path safely.") from exc


def _delivery_scope_git_env(
    *,
    root: Path | None = None,
    git_dir: Path | None = None,
    index_path: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("GIT_CONFIG", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(key, None)
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_KEY_1": "core.untrackedCache",
            "GIT_CONFIG_KEY_2": "core.excludesFile",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_VALUE_2": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    if root is not None:
        env["GIT_WORK_TREE"] = str(root)
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
    if index_path is not None:
        env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _delivery_scope_link_like(entry_stat) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(entry_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(entry_stat.st_mode) or bool(reparse_flag and file_attributes & reparse_flag)


def _normalize_windows_delivery_scope_link_target(target: str) -> str:
    for prefix in ("\\\\?\\UNC\\", "\\??\\UNC\\"):
        if target[: len(prefix)].casefold() == prefix.casefold():
            return "\\\\" + target[len(prefix) :]
    for prefix in ("\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            remainder = target[len(prefix) :]
            if len(remainder) >= 3 and remainder[0].isalpha() and remainder[1] == ":" and remainder[2] in "\\/":
                return remainder
    return target


def _normalize_delivery_scope_link_target(target: str) -> str:
    if os.name != "nt":
        return target
    return _normalize_windows_delivery_scope_link_target(target)


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
    normalized = path.replace("\\", "/") if os.name == "nt" else path
    return normalized.strip("/")


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


def _snapshot_path(status: str, path: Path) -> FileSnapshot:
    if path.is_symlink():
        return FileSnapshot(
            status=status,
            exists=True,
            content=None,
            mode=None,
            symlink_target=str(path.readlink()),
        )
    try:
        content = path.read_bytes()
        exists = True
    except FileNotFoundError:
        content = None
        exists = False
    except (IsADirectoryError, PermissionError):
        content = None
        exists = True
    return FileSnapshot(
        status=status,
        exists=exists,
        content=content,
        mode=_file_mode(path) if exists else None,
    )


def _restore_symlink(path: Path, target: str) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_error = _remove_artifact_path(path)
    if remove_error:
        return remove_error
    path.symlink_to(target)
    return None


def _prepare_regular_file_restore(path: Path) -> str | None:
    if path.is_symlink():
        path.unlink()
        return None
    if path.exists() and path.is_dir():
        return "artifact path is a directory"
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
        snapshot[path] = _snapshot_path(status, project_path)
    return snapshot


def snapshot_delivery_scope_files(
    cwd: Path,
    *,
    git_baseline: str | None = None,
    git_dir: str | Path | None = None,
    git_common_dir: str | Path | None = None,
    git_ignore_fingerprint: str | None = None,
    git_ref_fingerprint: str | None = None,
    ignored_roots: Iterable[str] | None = None,
    include_content: Callable[[str], bool] | None = None,
    validate_symlink: Callable[[str, str], bool] | None = None,
    symlink_roots: Iterable[str] | None = None,
    exclude_ephemeral: Callable[[str], bool] | None = None,
) -> dict[str, FileSnapshot]:
    """Snapshot sparse Git changes and persistent ignored files without following symlinks."""
    try:
        root = cwd.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit root is unavailable.") from exc
    normalized_ignored_roots = tuple(
        value for value in (_normalize_relative_path(path) for path in (ignored_roots or ())) if value and value != "."
    )
    if git_dir is None:
        binding = delivery_scope_git_binding(root)
        bound_git_dir = Path(binding.git_dir)
        bound_common_dir = Path(binding.common_dir)
        baseline = git_baseline or binding.baseline
        expected_ignore_fingerprint = git_ignore_fingerprint or binding.ignore_fingerprint
        expected_ref_fingerprint = git_ref_fingerprint or binding.ref_fingerprint
    else:
        bound_git_dir = _validated_delivery_scope_git_dir(git_dir)
        bound_common_dir = (
            _validated_delivery_scope_git_dir(git_common_dir)
            if git_common_dir is not None
            else _delivery_scope_git_common_dir(root, bound_git_dir)
        )
        _validate_delivery_scope_git_binding(root, bound_git_dir, bound_common_dir)
        baseline = git_baseline or delivery_scope_git_baseline(root, git_dir=bound_git_dir)
        expected_ignore_fingerprint = git_ignore_fingerprint or _delivery_scope_ignore_fingerprint(
            root,
            bound_git_dir,
        )
        expected_ref_fingerprint = git_ref_fingerprint or _delivery_scope_git_ref_fingerprint(
            bound_git_dir,
            bound_common_dir,
        )
    try:
        relative_git_dir = bound_git_dir.relative_to(root).as_posix()
    except ValueError:
        pass
    else:
        if relative_git_dir and relative_git_dir != ".":
            normalized_ignored_roots = tuple(dict.fromkeys((*normalized_ignored_roots, relative_git_dir)))
    _validate_delivery_scope_git_ref_fingerprint(
        bound_git_dir,
        bound_common_dir,
        expected_ref_fingerprint,
    )
    _validate_delivery_scope_ignore_fingerprint(root, bound_git_dir, expected_ignore_fingerprint)
    dirty_paths = _delivery_scope_dirty_paths(root, baseline, bound_git_dir)
    ignored_file_paths, ignored_directory_roots = _delivery_scope_ignored_paths(root, bound_git_dir)
    _validate_delivery_scope_ignore_fingerprint(root, bound_git_dir, expected_ignore_fingerprint)
    _validate_delivery_scope_git_ref_fingerprint(
        bound_git_dir,
        bound_common_dir,
        expected_ref_fingerprint,
    )
    normalized_symlink_roots = tuple(
        dict.fromkeys(
            value for value in (_normalize_relative_path(path) or "." for path in (symlink_roots or ())) if value
        )
    )
    if validate_symlink is not None and symlink_roots is None:
        normalized_symlink_roots = (".",)

    def ephemeral(path: str) -> bool:
        try:
            return bool(exclude_ephemeral and exclude_ephemeral(path))
        except Exception as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not classify an ephemeral path.") from exc

    retained_content_paths: dict[str, bool] = {}

    def retain_content(path: str) -> bool:
        if path not in retained_content_paths:
            try:
                retained_content_paths[path] = bool(include_content and include_content(path))
            except Exception as exc:
                raise DeliveryScopeSnapshotError(
                    "Delivery scope audit could not classify a retained-content path."
                ) from exc
        return retained_content_paths[path]

    dirty_paths = {path for path in dirty_paths if not _is_ignored_root(path, normalized_ignored_roots)}
    ignored_file_paths = {
        path
        for path in ignored_file_paths
        if not _is_ignored_root(path, normalized_ignored_roots) and not ephemeral(path)
    }
    exact_paths = dirty_paths | ignored_file_paths
    ignored_directory_roots = tuple(
        path
        for path in ignored_directory_roots
        if not _is_ignored_root(path, normalized_ignored_roots) and not ephemeral(path)
    )
    snapshot: dict[str, FileSnapshot] = {}

    descriptor_traversal = _delivery_scope_descriptor_traversal_supported()

    def record_entry(
        entry,
        entry_stat,
        normalized: str,
        *,
        directory_fd: int | None,
        link_like: bool,
        validate_entry_link: bool,
    ) -> None:
        if link_like:
            # Windows may report different inode identities for the same reparse
            # point. Bind the observable link target on both sides of the stat.
            symlink_target_before = _normalize_delivery_scope_link_target(
                os.readlink(entry.name, dir_fd=directory_fd) if directory_fd is not None else os.readlink(entry.path)
            )
            after = (
                os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                if directory_fd is not None
                else Path(entry.path).stat(follow_symlinks=False)
            )
            symlink_target_after = _normalize_delivery_scope_link_target(
                os.readlink(entry.name, dir_fd=directory_fd) if directory_fd is not None else os.readlink(entry.path)
            )
            if not _delivery_scope_link_like(after) or symlink_target_after != symlink_target_before:
                raise DeliveryScopeSnapshotError("A project path changed during delivery scope audit.")
            if (
                validate_entry_link
                and validate_symlink is not None
                and not validate_symlink(normalized, symlink_target_after)
            ):
                raise DeliveryScopeSnapshotError(
                    "Delivery scope audit found a symlink that escapes the active write scope."
                )
            snapshot[normalized] = FileSnapshot(
                status="filesystem",
                exists=True,
                content=None,
                mode=stat.S_IMODE(entry_stat.st_mode),
                symlink_target=symlink_target_after,
            )
        elif stat.S_ISREG(entry_stat.st_mode):
            path = entry.name if directory_fd is not None else Path(entry.path)
            fallback_before = None
            if directory_fd is None:
                fallback_before = Path(path).stat(follow_symlinks=False)
                if not stat.S_ISREG(fallback_before.st_mode):
                    raise DeliveryScopeSnapshotError("A project path changed type during delivery scope audit.")
            file_mode = stat.S_IMODE(fallback_before.st_mode if fallback_before is not None else entry_stat.st_mode)
            file_snapshot = _snapshot_delivery_scope_regular_file(
                path,
                mode=file_mode,
                retain_content=retain_content(normalized),
                expected_identity=(entry_stat.st_dev, entry_stat.st_ino) if directory_fd is not None else None,
                dir_fd=directory_fd,
            )
            if fallback_before is not None:
                fallback_after = Path(path).stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(fallback_after.st_mode)
                    or (fallback_after.st_dev, fallback_after.st_ino)
                    != (fallback_before.st_dev, fallback_before.st_ino)
                    or stat.S_IMODE(fallback_after.st_mode) != file_mode
                ):
                    raise DeliveryScopeSnapshotError("A project path changed during delivery scope audit.")
            snapshot[normalized] = file_snapshot
        else:
            snapshot[normalized] = FileSnapshot(
                status="filesystem",
                exists=True,
                content=None,
                mode=stat.S_IMODE(entry_stat.st_mode),
                digest=f"special:{stat.S_IFMT(entry_stat.st_mode)}",
            )

    def path_related(path: str, roots: Iterable[str]) -> bool:
        return any(
            root_path == "."
            or path == root_path
            or path.startswith(f"{root_path}/")
            or root_path.startswith(f"{path}/")
            for root_path in roots
        )

    def should_visit(path: str) -> bool:
        return (
            path_related(path, exact_paths)
            or path_related(path, ignored_directory_roots)
            or path_related(path, normalized_symlink_roots)
        )

    def should_record(path: str, *, symlink: bool, special: bool) -> bool:
        return (
            path in exact_paths
            or any(path == ignored or path.startswith(f"{ignored}/") for ignored in ignored_directory_roots)
            or ((symlink or special) and path_related(path, normalized_symlink_roots))
            or (not symlink and not special and retain_content(path))
        )

    def walk_descriptor(directory_fd: int, prefix: str, *, metadata_only: bool = False) -> None:
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not enumerate the project tree.") from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            normalized = _normalize_relative_path(relative)
            if _is_ignored_root(normalized, normalized_ignored_roots):
                continue
            if not should_visit(normalized):
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
                link_like = _delivery_scope_link_like(entry_stat)
                if link_like:
                    if should_record(normalized, symlink=True, special=False):
                        record_entry(
                            entry,
                            entry_stat,
                            normalized,
                            directory_fd=directory_fd,
                            link_like=True,
                            validate_entry_link=path_related(normalized, normalized_symlink_roots),
                        )
                elif stat.S_ISDIR(entry_stat.st_mode):
                    dirty_related = path_related(normalized, dirty_paths)
                    child_metadata_only = metadata_only and not dirty_related
                    if ephemeral(normalized) and not dirty_related:
                        child_metadata_only = True
                    if child_metadata_only and not path_related(normalized, normalized_symlink_roots):
                        continue
                    child_fd = _open_delivery_scope_directory(
                        entry.name,
                        expected_identity=(entry_stat.st_dev, entry_stat.st_ino),
                        dir_fd=directory_fd,
                    )
                    try:
                        walk_descriptor(child_fd, normalized, metadata_only=child_metadata_only)
                    finally:
                        os.close(child_fd)
                elif should_record(
                    normalized,
                    symlink=False,
                    special=not stat.S_ISREG(entry_stat.st_mode),
                ):
                    if (
                        (metadata_only or ephemeral(normalized))
                        and normalized not in dirty_paths
                        and stat.S_ISREG(entry_stat.st_mode)
                    ):
                        continue
                    record_entry(
                        entry,
                        entry_stat,
                        normalized,
                        directory_fd=directory_fd,
                        link_like=False,
                        validate_entry_link=False,
                    )
            except DeliveryScopeSnapshotError:
                raise
            except OSError as exc:
                raise DeliveryScopeSnapshotError("Delivery scope audit could not inspect a project path.") from exc

    def walk_path(
        directory: Path,
        prefix: str,
        expected_identity: tuple[int, int],
        *,
        metadata_only: bool = False,
    ) -> None:
        try:
            before = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or (before.st_dev, before.st_ino) != expected_identity:
                raise DeliveryScopeSnapshotError("A project directory changed during delivery scope audit.")
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not enumerate the project tree.") from exc
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            normalized = _normalize_relative_path(relative)
            if _is_ignored_root(normalized, normalized_ignored_roots):
                continue
            if not should_visit(normalized):
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
                link_like = _delivery_scope_link_like(entry_stat)
                if link_like:
                    if should_record(normalized, symlink=True, special=False):
                        record_entry(
                            entry,
                            entry_stat,
                            normalized,
                            directory_fd=None,
                            link_like=True,
                            validate_entry_link=path_related(normalized, normalized_symlink_roots),
                        )
                elif stat.S_ISDIR(entry_stat.st_mode):
                    dirty_related = path_related(normalized, dirty_paths)
                    child_metadata_only = metadata_only and not dirty_related
                    if ephemeral(normalized) and not dirty_related:
                        child_metadata_only = True
                    if child_metadata_only and not path_related(normalized, normalized_symlink_roots):
                        continue
                    child_path = Path(entry.path)
                    child_stat = child_path.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(child_stat.st_mode):
                        raise DeliveryScopeSnapshotError("A project directory changed during delivery scope audit.")
                    walk_path(
                        child_path,
                        normalized,
                        (child_stat.st_dev, child_stat.st_ino),
                        metadata_only=child_metadata_only,
                    )
                elif should_record(
                    normalized,
                    symlink=False,
                    special=not stat.S_ISREG(entry_stat.st_mode),
                ):
                    if (
                        (metadata_only or ephemeral(normalized))
                        and normalized not in dirty_paths
                        and stat.S_ISREG(entry_stat.st_mode)
                    ):
                        continue
                    record_entry(
                        entry,
                        entry_stat,
                        normalized,
                        directory_fd=None,
                        link_like=False,
                        validate_entry_link=False,
                    )
            except DeliveryScopeSnapshotError:
                raise
            except OSError as exc:
                raise DeliveryScopeSnapshotError("Delivery scope audit could not inspect a project path.") from exc
        try:
            after = directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not inspect a project directory.") from exc
        if not stat.S_ISDIR(after.st_mode) or (after.st_dev, after.st_ino) != expected_identity:
            raise DeliveryScopeSnapshotError("A project directory changed during delivery scope audit.")

    try:
        root_stat = root.stat(follow_symlinks=False)
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        if descriptor_traversal:
            root_fd = _open_delivery_scope_directory(root, expected_identity=root_identity)
            try:
                walk_descriptor(root_fd, "")
            finally:
                os.close(root_fd)
        else:
            walk_path(root, "", root_identity)
    except DeliveryScopeSnapshotError:
        raise
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not inspect the project tree.") from exc
    for path in sorted(exact_paths - snapshot.keys()):
        snapshot[path] = FileSnapshot(
            status="filesystem",
            exists=False,
            content=None,
            mode=None,
        )
    return snapshot


def delivery_scope_git_binding(root: Path) -> DeliveryScopeGitBinding:
    """Capture the Git authority used for one delivery mutation audit."""
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit root is unavailable.") from exc
    values = _git_lines(
        resolved_root,
        ["rev-parse", "--absolute-git-dir", "--show-toplevel"],
        env=_delivery_scope_git_env(),
    )
    if values is None or len(values) != 2:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not resolve its Git repository binding.")
    git_dir = _validated_delivery_scope_git_dir(values[0])
    try:
        discovered_root = Path(values[1]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not resolve its Git repository binding.") from exc
    if discovered_root != resolved_root:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git worktree does not match its project root.")
    common_dir = _delivery_scope_git_common_dir(resolved_root, git_dir)
    ref_fingerprint = _delivery_scope_git_ref_fingerprint(git_dir, common_dir)
    baseline = delivery_scope_git_baseline(resolved_root, git_dir=git_dir)
    ignore_fingerprint = _delivery_scope_ignore_fingerprint(resolved_root, git_dir)
    if _delivery_scope_git_ref_fingerprint(git_dir, common_dir) != ref_fingerprint:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git references changed while binding the mutation.")
    return DeliveryScopeGitBinding(
        baseline=baseline,
        git_dir=str(git_dir),
        common_dir=str(common_dir),
        ignore_fingerprint=ignore_fingerprint,
        ref_fingerprint=ref_fingerprint,
    )


def _validated_delivery_scope_git_dir(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid Git directory binding.")
    path = Path(value)
    if not path.is_absolute():
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid Git directory binding.")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git directory is unavailable.") from exc
    if not resolved.is_dir():
        raise DeliveryScopeSnapshotError("Delivery scope audit Git directory is unavailable.")
    return resolved


def _delivery_scope_git_common_dir(root: Path, git_dir: Path) -> Path:
    values = _git_lines(
        root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        env=_delivery_scope_git_env(root=root, git_dir=git_dir),
    )
    if values is None or len(values) != 1:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not bind the common Git directory.")
    return _validated_delivery_scope_git_dir(values[0])


def _delivery_scope_symbolic_head(git_dir: Path) -> str | None:
    head_path = git_dir / "HEAD"
    try:
        head_stat = head_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not bind the active Git reference.") from exc
    if not stat.S_ISREG(head_stat.st_mode):
        raise DeliveryScopeSnapshotError("Delivery scope audit requires a regular Git HEAD file.")
    snapshot = _snapshot_delivery_scope_regular_file(
        head_path,
        mode=head_stat.st_mode,
        retain_content=True,
        expected_identity=(head_stat.st_dev, head_stat.st_ino),
    )
    if snapshot.content is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not bind the active Git reference.")
    value = snapshot.content.strip()
    if not value.startswith(b"ref: "):
        if _DELIVERY_SCOPE_GIT_OBJECT_ID.fullmatch(os.fsdecode(value)) is None:
            raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid detached Git HEAD.")
        return None
    head_ref = os.fsdecode(value.removeprefix(b"ref: "))
    if (
        not head_ref.startswith("refs/")
        or "//" in head_ref
        or head_ref.endswith("/")
        or any(part in {"", ".", ".."} for part in head_ref.split("/"))
    ):
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid active Git reference.")
    return head_ref


def _delivery_scope_git_ref_fingerprint(git_dir: Path, common_dir: Path) -> str:
    head_ref = _delivery_scope_symbolic_head(git_dir)
    paths = {
        git_dir / "HEAD",
        git_dir / "logs" / "HEAD",
        common_dir / "packed-refs",
        common_dir / "reftable",
    }
    if head_ref is not None:
        worktree_ref = head_ref.startswith(("refs/bisect/", "refs/rewritten/", "refs/worktree/"))
        ref_root = git_dir if worktree_ref else common_dir
        paths.add(ref_root.joinpath(*head_ref.split("/")))
        paths.add(ref_root.joinpath("logs", *head_ref.split("/")))

    anchors = {git_dir, common_dir}
    for path in tuple(paths):
        if not any(path == anchor or anchor in path.parents for anchor in anchors):
            raise DeliveryScopeSnapshotError("Delivery scope audit Git reference metadata escapes its binding.")
        parent = path.parent
        while parent not in anchors:
            paths.add(parent)
            parent = parent.parent

    digest = hashlib.sha256()
    digest.update(b"delivery-scope-git-refs-v1\0")
    for path in sorted(paths, key=os.fspath):
        _update_delivery_scope_git_ref_digest(
            digest,
            path,
            recursive=path.name == "reftable",
        )
    return digest.hexdigest()


def _delivery_scope_ref_stat_signature(path_stat) -> tuple[int, ...]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_mode,
        path_stat.st_size,
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
        getattr(path_stat, "st_file_attributes", 0),
        getattr(path_stat, "st_reparse_tag", 0),
    )


def _update_delivery_scope_git_ref_digest(
    digest,
    path: Path,
    *,
    recursive: bool,
) -> None:
    digest.update(os.fsencode(path))
    digest.update(b"\0")
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not read Git reference metadata.") from exc

    signature = _delivery_scope_ref_stat_signature(path_stat)
    digest.update(repr(signature).encode("ascii"))
    digest.update(b"\0")
    if stat.S_ISLNK(path_stat.st_mode):
        try:
            digest.update(os.fsencode(os.readlink(path)))
        except OSError as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not read Git reference metadata.") from exc
        digest.update(b"\0")
    elif stat.S_ISREG(path_stat.st_mode):
        snapshot = _snapshot_delivery_scope_regular_file(
            path,
            mode=path_stat.st_mode,
            retain_content=False,
            expected_identity=(path_stat.st_dev, path_stat.st_ino),
        )
        if snapshot.digest is None:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not hash Git reference metadata.")
        digest.update(snapshot.digest.encode("ascii"))
        digest.update(b"\0")
    elif stat.S_ISDIR(path_stat.st_mode) and recursive:
        try:
            children = sorted(path.iterdir(), key=lambda child: os.fsencode(child.name))
        except OSError as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not inspect Git reference metadata.") from exc
        for child in children:
            _update_delivery_scope_git_ref_digest(digest, child, recursive=True)

    try:
        final_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git reference metadata changed during capture.") from exc
    if _delivery_scope_ref_stat_signature(final_stat) != signature:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git reference metadata changed during capture.")


def _validate_delivery_scope_git_ref_fingerprint(
    git_dir: Path,
    common_dir: Path,
    expected: str,
) -> None:
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid Git reference binding.")
    if _delivery_scope_git_ref_fingerprint(git_dir, common_dir) != expected:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git references changed during the mutation.")


def _validate_delivery_scope_git_binding(root: Path, git_dir: Path, common_dir: Path | None = None) -> None:
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit root is unavailable.") from exc
    values = _git_lines(
        resolved_root,
        ["rev-parse", "--absolute-git-dir", "--show-toplevel"],
        env=_delivery_scope_git_env(),
    )
    if values is None or len(values) != 2:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not validate its Git repository binding.")
    current_git_dir = _validated_delivery_scope_git_dir(values[0])
    try:
        current_root = Path(values[1]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not validate its Git repository binding.") from exc
    if current_git_dir != git_dir:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git repository binding changed during the mutation.")
    if current_root != resolved_root:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git worktree changed during the mutation.")
    if common_dir is not None and _delivery_scope_git_common_dir(resolved_root, git_dir) != common_dir:
        raise DeliveryScopeSnapshotError("Delivery scope audit common Git directory changed during the mutation.")


def delivery_scope_git_baseline(root: Path, *, git_dir: Path | None = None) -> str:
    """Return the immutable commit used to classify one delivery mutation."""
    try:
        resolved_root = root.resolve(strict=True)
        if git_dir is None:
            discovered = _git_lines(
                resolved_root,
                ["rev-parse", "--absolute-git-dir"],
                env=_delivery_scope_git_env(),
            )
            if discovered is None or len(discovered) != 1:
                raise DeliveryScopeSnapshotError("Delivery scope audit could not resolve the Git baseline.")
            bound_git_dir = _validated_delivery_scope_git_dir(discovered[0])
        else:
            bound_git_dir = _validated_delivery_scope_git_dir(git_dir)
        values = _git_lines(
            resolved_root,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            env=_delivery_scope_git_env(root=resolved_root, git_dir=bound_git_dir),
        )
    except (OSError, RuntimeError) as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not resolve the Git baseline.") from exc
    if values is None or len(values) != 1 or _DELIVERY_SCOPE_GIT_OBJECT_ID.fullmatch(values[0]) is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not resolve the Git baseline.")
    return values[0]


def _delivery_scope_ignore_fingerprint(root: Path, git_dir: Path) -> str:
    """Hash every ignore input trusted by delivery-scope Git queries."""
    env = _delivery_scope_git_env(root=root, git_dir=git_dir)
    info_exclude = _git_lines(root, ["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], env=env)
    if info_exclude is None or len(info_exclude) != 1:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not bind Git ignore metadata.")
    ignore_paths: set[str] = set()
    pathspec = ["--", ".gitignore", ":(glob)**/.gitignore"]
    for command in (
        ["ls-files", "--cached", "-z", *pathspec],
        ["ls-files", "--others", "--exclude-standard", "-z", *pathspec],
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z", *pathspec],
    ):
        values = _git_paths_z(root, command, env=env)
        if values is None:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not bind Git ignore metadata.")
        ignore_paths.update(_validated_delivery_scope_git_path(value) for value in values)

    digest = hashlib.sha256()
    digest.update(b"delivery-scope-ignore-v1\0")
    _update_delivery_scope_control_file_digest(digest, Path(info_exclude[0]), label=b"info/exclude")
    for relative_path in sorted(ignore_paths):
        digest.update(b"worktree\0")
        digest.update(os.fsencode(relative_path))
        digest.update(b"\0")
        _update_delivery_scope_control_file_digest(
            digest,
            root.joinpath(*relative_path.split("/")),
            label=b".gitignore",
        )
    return digest.hexdigest()


def _update_delivery_scope_control_file_digest(digest, path: Path, *, label: bytes) -> None:
    digest.update(label)
    digest.update(b"\0")
    digest.update(os.fsencode(path))
    digest.update(b"\0")
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not read Git ignore metadata.") from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise DeliveryScopeSnapshotError("Delivery scope audit requires regular Git ignore metadata files.")
    snapshot = _snapshot_delivery_scope_regular_file(
        path,
        mode=path_stat.st_mode,
        retain_content=False,
        expected_identity=(path_stat.st_dev, path_stat.st_ino),
    )
    if snapshot.digest is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not hash Git ignore metadata.")
    digest.update(str(stat.S_IMODE(path_stat.st_mode)).encode("ascii"))
    digest.update(b"\0")
    digest.update(snapshot.digest.encode("ascii"))
    digest.update(b"\0")


def _validate_delivery_scope_ignore_fingerprint(root: Path, git_dir: Path, expected: str) -> None:
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid Git ignore binding.")
    if _delivery_scope_ignore_fingerprint(root, git_dir) != expected:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git ignore metadata changed during the mutation.")


def _delivery_scope_dirty_paths(root: Path, git_baseline: str, git_dir: Path) -> set[str]:
    if _DELIVERY_SCOPE_GIT_OBJECT_ID.fullmatch(git_baseline) is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid Git baseline.")
    env = _delivery_scope_git_env(root=root, git_dir=git_dir)
    try:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", git_baseline, "HEAD"],
            capture_output=True,
            cwd=root,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not query Git history.") from exc
    if ancestor.returncode != 0:
        raise DeliveryScopeSnapshotError("Delivery scope audit Git history no longer descends from its baseline.")
    commands = (
        ["diff", "--name-only", "--relative", "-z", git_baseline, "HEAD", "--", "."],
        [
            "log",
            "--format=",
            "--name-only",
            "--full-history",
            "--diff-merges=separate",
            "--no-renames",
            "--relative",
            "-z",
            f"{git_baseline}..HEAD",
            "--",
            ".",
        ],
        ["diff", "--cached", "--name-only", "--relative", "-z", git_baseline, "--", "."],
    )
    paths: set[str] = set()
    for command in commands:
        values = _git_paths_z(root, command, env=env)
        if values is None:
            raise DeliveryScopeSnapshotError("Delivery scope audit could not query Git changes.")
        paths.update(_validated_delivery_scope_git_path(value) for value in values)
    paths.update(_delivery_scope_worktree_paths(root, git_baseline, git_dir))
    return paths


def _delivery_scope_worktree_paths(root: Path, git_baseline: str, git_dir: Path) -> set[str]:
    """Return worktree changes through an index rebuilt from the immutable baseline."""
    descriptor = -1
    index_path: Path | None = None
    try:
        descriptor, index_name = tempfile.mkstemp(prefix="sikula-delivery-scope-index-")
        os.close(descriptor)
        descriptor = -1
        index_path = Path(index_name)
        index_path.unlink()
        env = _delivery_scope_git_env(root=root, git_dir=git_dir, index_path=index_path)
        for command in (
            ["read-tree", "--reset", git_baseline],
            ["update-index", "-q", "--really-refresh"],
        ):
            result = subprocess.run(
                ["git", *command],
                capture_output=True,
                cwd=root,
                check=False,
                env=env,
            )
            if result.returncode != 0:
                raise DeliveryScopeSnapshotError("Delivery scope audit could not build its trusted Git index.")
        commands = (
            ["diff-files", "--name-only", "--relative", "-z", "--", "."],
            ["ls-files", "--others", "--exclude-standard", "-z", "--", "."],
        )
        paths: set[str] = set()
        for command in commands:
            values = _git_paths_z(root, command, env=env)
            if values is None:
                raise DeliveryScopeSnapshotError("Delivery scope audit could not query trusted Git changes.")
            paths.update(_validated_delivery_scope_git_path(value) for value in values)
        return paths
    except DeliveryScopeSnapshotError:
        raise
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not build its trusted Git index.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if index_path is not None:
            try:
                index_path.unlink(missing_ok=True)
            except OSError:
                pass


def _delivery_scope_ignored_paths(root: Path, git_dir: Path) -> tuple[set[str], tuple[str, ...]]:
    values = _git_paths_z(
        root,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
            "--",
            ".",
        ],
        env=_delivery_scope_git_env(root=root, git_dir=git_dir),
    )
    if values is None:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not query persistent ignored paths.")
    files: set[str] = set()
    directories: list[str] = []
    for raw in values:
        directory = raw.endswith("/")
        path = _validated_delivery_scope_git_path(raw)
        if directory:
            directories.append(path)
        else:
            files.add(path)
    return files, tuple(dict.fromkeys(sorted(directories)))


def _validated_delivery_scope_git_path(value: str) -> str:
    normalized = _normalize_relative_path(value)
    if not normalized or Path(normalized).is_absolute() or ".." in Path(normalized).parts:
        raise DeliveryScopeSnapshotError("Delivery scope audit received an unsafe Git path.")
    return normalized


def _delivery_scope_descriptor_traversal_supported() -> bool:
    return bool(
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and os.readlink in os.supports_dir_fd
        and getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
    )


def _open_delivery_scope_directory(
    path: str | Path,
    *,
    expected_identity: tuple[int, int],
    dir_fd: int | None = None,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            )
            != expected_identity
        ):
            raise DeliveryScopeSnapshotError("A project directory changed during delivery scope audit.")
        return descriptor
    except DeliveryScopeSnapshotError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise DeliveryScopeSnapshotError("Delivery scope audit could not open a project directory safely.") from exc


def _snapshot_delivery_scope_regular_file(
    path: str | Path,
    *,
    mode: int,
    retain_content: bool,
    expected_identity: tuple[int, int] | None = None,
    dir_fd: int | None = None,
) -> FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    chunks: list[bytes] | None = [] if retain_content else None
    retained_size = 0
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or (
                expected_identity is not None and (opened_stat.st_dev, opened_stat.st_ino) != expected_identity
            ):
                raise DeliveryScopeSnapshotError("A project path changed type during delivery scope audit.")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if chunks is not None:
                    retained_size += len(chunk)
                    if retained_size > _MAX_DELIVERY_SCOPE_RETAINED_CONTENT_BYTES or b"\x00" in chunk:
                        chunks = None
                    else:
                        chunks.append(chunk)
        finally:
            os.close(descriptor)
    except DeliveryScopeSnapshotError:
        raise
    except OSError as exc:
        raise DeliveryScopeSnapshotError("Delivery scope audit could not read a project file.") from exc
    return FileSnapshot(
        status="filesystem",
        exists=True,
        content=b"".join(chunks) if chunks is not None else None,
        mode=mode,
        digest="sha256:" + digest.hexdigest(),
    )


def serialize_delivery_scope_snapshot(snapshot: dict[str, FileSnapshot]) -> dict[str, str | None]:
    serialized: dict[str, str | None] = {}
    for path, value in snapshot.items():
        serialized[path] = json.dumps(
            {
                "status": value.status,
                "exists": value.exists,
                "content": base64.b64encode(value.content).decode("ascii") if value.content is not None else None,
                "mode": value.mode,
                "symlink_target": value.symlink_target,
                "digest": value.digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return serialized


def deserialize_delivery_scope_snapshot(snapshot: dict[str, str | None]) -> dict[str, FileSnapshot]:
    result: dict[str, FileSnapshot] = {}
    try:
        for path, raw in snapshot.items():
            if raw is None:
                raise ValueError("missing snapshot value")
            value = json.loads(raw)
            if not isinstance(value, dict) or type(value.get("exists")) is not bool:
                raise ValueError("invalid snapshot value")
            content_value = value.get("content")
            content = None if content_value is None else base64.b64decode(content_value, validate=True)
            status = value.get("status")
            mode = value.get("mode")
            symlink_target = value.get("symlink_target")
            digest = value.get("digest")
            if (
                not isinstance(status, str)
                or (mode is not None and type(mode) is not int)
                or (symlink_target is not None and not isinstance(symlink_target, str))
                or (digest is not None and not isinstance(digest, str))
            ):
                raise ValueError("invalid snapshot value")
            result[path] = FileSnapshot(
                status=status,
                exists=value["exists"],
                content=content,
                mode=mode,
                symlink_target=symlink_target,
                digest=digest,
            )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeliveryScopeSnapshotError("Persisted delivery scope audit snapshot is malformed.") from exc
    return result


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

            if before_snapshot.symlink_target is not None:
                restore_error = _restore_symlink(project_path, before_snapshot.symlink_target)
                if restore_error:
                    errors.append(f"{artifact.path}: {restore_error}")
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
            restore_error = _prepare_regular_file_restore(project_path)
            if restore_error:
                errors.append(f"{artifact.path}: {restore_error}")
                continue
            project_path.write_bytes(before_snapshot.content)
            _restore_file_mode(project_path, before_snapshot.mode)
        except OSError as exc:
            errors.append(f"{artifact.path}: {exc}")
    return errors
