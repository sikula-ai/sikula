"""Reversible quarantine for agent-created delivery files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import secrets
import shutil
import stat
import subprocess
from typing import Callable, Sequence

from core.delivery_quarantine import (
    DELIVERY_QUARANTINE_SCHEMA_VERSION,
    MAX_DELIVERY_QUARANTINE_PATH_CHARS,
    DeliveryQuarantineError,
    DeliveryQuarantineResult,
    delivery_quarantine_task_namespace,
    valid_delivery_quarantine_id,
)
from core.validation_artifacts import (
    DeliveryScopeSnapshotError,
    delivery_scope_git_binding,
    delivery_scope_git_env,
)


_PRIVATE_ROOTS = frozenset({".git", ".sikula", ".claude", ".codex", ".gemini", ".opencode"})
_QUARANTINE_COMPONENTS = ("sikula", "quarantine")


def delivery_quarantine_supported() -> bool:
    """Return whether descriptor-relative, no-follow rename is available."""

    return bool(
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def quarantine_agent_created_file(
    project_root: Path,
    task_id: str,
    path: str,
    provenance: dict[str, object],
    *,
    session_id: str,
    active_write_paths: Sequence[str],
    exact_file_paths: Sequence[str],
    git_root: Path | None = None,
    before_move: Callable[[dict[str, object]], None],
    after_move: Callable[[DeliveryQuarantineResult], None],
) -> DeliveryQuarantineResult:
    """Move one verified agent-created file into persistent private quarantine."""

    if not delivery_quarantine_supported():
        raise DeliveryQuarantineError(
            "delivery_quarantine.platform_unsupported",
            "Reversible file quarantine is unavailable on this platform.",
        )
    root = _validated_project_root(project_root)
    repository_root = _validated_project_root(git_root or root)
    try:
        project_prefix = root.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.worktree_invalid",
            "The delivery project is outside its audited Git worktree.",
        ) from exc
    normalized_path = _canonical_project_path(path)
    git_path = normalized_path if project_prefix == "." else f"{project_prefix}/{normalized_path}"
    _validate_provenance(normalized_path, provenance, session_id)
    if _is_private_path(normalized_path):
        raise DeliveryQuarantineError(
            "delivery_quarantine.private_path_denied",
            "Sikula runtime, provider, and Git metadata cannot be quarantined.",
        )
    if not _path_allowed(normalized_path, active_write_paths, exact_file_paths):
        raise DeliveryQuarantineError(
            "delivery_quarantine.scope_denied",
            "The requested file is outside the active Fixer write scope.",
        )

    try:
        binding = delivery_scope_git_binding(repository_root)
    except DeliveryScopeSnapshotError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.git_state_unavailable",
            "Sikula could not verify Git state for the quarantine request.",
        ) from exc
    _require_ordinary_untracked(repository_root, Path(binding.git_dir), git_path)
    candidate = _inspect_candidate(root, normalized_path)
    _require_provenance_match(candidate, provenance)

    quarantine_id = secrets.token_hex(16)
    intent = {
        "schema_version": DELIVERY_QUARANTINE_SCHEMA_VERSION,
        "status": "moving",
        "path": normalized_path,
        "quarantine_id": quarantine_id,
        "digest": candidate["digest"],
        "mode": candidate["mode"],
        "size": candidate["size"],
    }
    try:
        before_move(intent)
    except Exception as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.checkpoint_failed",
            "Sikula could not persist quarantine intent before moving the file.",
            quarantine_id=quarantine_id,
            move_not_started=True,
        ) from exc

    try:
        if delivery_scope_git_binding(repository_root) != binding:
            raise DeliveryQuarantineError(
                "delivery_quarantine.git_state_changed",
                "Git state changed while Sikula prepared the quarantine move.",
                quarantine_id=quarantine_id,
            )
        _require_ordinary_untracked(repository_root, Path(binding.git_dir), git_path)
        current = _inspect_candidate(root, normalized_path)
        if current != candidate:
            raise DeliveryQuarantineError(
                "delivery_quarantine.path_changed",
                "The requested file changed before Sikula could quarantine it.",
                quarantine_id=quarantine_id,
            )
    except DeliveryQuarantineError as exc:
        raise DeliveryQuarantineError(
            exc.code,
            str(exc),
            quarantine_id=quarantine_id,
            move_not_started=True,
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.move_failed",
            "Sikula could not revalidate the file before moving it into private quarantine.",
            quarantine_id=quarantine_id,
            move_not_started=True,
        ) from exc

    try:
        _move_to_quarantine(
            root,
            Path(binding.common_dir),
            task_id,
            normalized_path,
            quarantine_id,
            candidate,
        )
    except DeliveryQuarantineError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.move_failed",
            "Sikula could not move the requested file into private quarantine.",
            quarantine_id=quarantine_id,
        ) from exc

    result = DeliveryQuarantineResult(
        path=normalized_path,
        quarantine_id=quarantine_id,
        digest=str(candidate["digest"]),
        mode=int(candidate["mode"]),
        size=int(candidate["size"]),
    )
    try:
        after_move(result)
    except Exception as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.completion_failed",
            "The file is retained in quarantine, but completion state could not be persisted.",
            quarantine_id=quarantine_id,
        ) from exc
    return result


def task_quarantine_summary(git_root: Path, task_id: str) -> tuple[int, int]:
    """Return the regular-file count and total bytes retained for one task."""

    path = _task_quarantine_path(git_root, task_id)
    try:
        if not _quarantine_path_exists(path):
            return 0, 0
        _validate_existing_quarantine_path(path)
        count = 0
        size = 0
        for candidate in path.rglob("*"):
            value = candidate.stat(follow_symlinks=False)
            if stat.S_ISREG(value.st_mode):
                count += 1
                size += value.st_size
        return count, size
    except OSError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.cleanup_unavailable",
            "Sikula could not inspect the task quarantine.",
        ) from exc


def task_quarantine_entry_retained(git_root: Path, task_id: str, quarantine_id: str) -> bool:
    """Return whether one recorded move reached its private quarantine entry."""

    if not valid_delivery_quarantine_id(quarantine_id):
        raise DeliveryQuarantineError(
            "delivery_quarantine.cleanup_invalid",
            "The recorded quarantine identifier is invalid.",
        )
    path = _task_quarantine_path(git_root, task_id)
    try:
        if not _quarantine_path_exists(path):
            return False
        _validate_existing_quarantine_path(path)
        operation = path / quarantine_id
        try:
            operation_value = operation.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(operation_value.st_mode)
            or stat.S_IMODE(operation_value.st_mode) & 0o077
            or operation_value.st_uid != os.geteuid()
        ):
            raise DeliveryQuarantineError(
                "delivery_quarantine.cleanup_invalid",
                "The recorded quarantine operation is not a private directory.",
            )
        try:
            entry_value = (operation / "entry").stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(entry_value.st_mode):
            raise DeliveryQuarantineError(
                "delivery_quarantine.cleanup_invalid",
                "The recorded quarantine entry is not a regular file.",
            )
        return True
    except DeliveryQuarantineError:
        raise
    except OSError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.cleanup_unavailable",
            "Sikula could not inspect the recorded quarantine operation.",
        ) from exc


def remove_task_quarantine(git_root: Path, task_id: str) -> int:
    """Remove one exact task quarantine after an explicit cleanup request."""

    path = _task_quarantine_path(git_root, task_id)
    count, _size = task_quarantine_summary(git_root, task_id)
    if not _quarantine_path_exists(path):
        return 0
    try:
        _validate_existing_quarantine_path(path)
        shutil.rmtree(path)
    except DeliveryQuarantineError:
        raise
    except OSError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.cleanup_failed",
            "Sikula could not remove the task quarantine.",
        ) from exc
    return count


def _validated_project_root(project_root: Path) -> Path:
    try:
        return project_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.worktree_invalid",
            "The isolated delivery worktree is unavailable.",
        ) from exc


def _canonical_project_path(path: object) -> str:
    if not isinstance(path, str):
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_invalid",
            "Quarantine paths must be bounded project-relative strings.",
        )
    value = path.strip()
    if not value or len(value) > MAX_DELIVERY_QUARANTINE_PATH_CHARS or len(value.splitlines()) != 1:
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_invalid",
            "Quarantine paths must be bounded project-relative strings.",
        )
    if PureWindowsPath(value).is_absolute() or value.startswith(("/", "~")):
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_invalid",
            "Quarantine paths must be project-relative.",
        )
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_invalid",
            "Quarantine paths cannot traverse parent directories.",
        )
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != value:
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_invalid",
            "Quarantine paths must use canonical project-relative spelling.",
        )
    return normalized


def _validate_provenance(path: str, provenance: object, session_id: str) -> None:
    if not isinstance(provenance, dict) or provenance.get("path") != path or provenance.get("session_id") != session_id:
        raise DeliveryQuarantineError(
            "delivery_quarantine.provenance_missing",
            "The requested file was not created by an audited agent attempt in this run.",
        )
    identity = provenance.get("identity")
    if (
        not isinstance(provenance.get("digest"), str)
        or not isinstance(provenance.get("mode"), int)
        or isinstance(provenance.get("mode"), bool)
        or not isinstance(identity, list)
        or len(identity) != 2
        or not all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in identity)
    ):
        raise DeliveryQuarantineError(
            "delivery_quarantine.provenance_invalid",
            "The quarantine provenance record is malformed.",
        )


def _require_provenance_match(candidate: dict[str, object], provenance: dict[str, object]) -> None:
    if (
        candidate["digest"] != provenance.get("digest")
        or candidate["mode"] != provenance.get("mode")
        or list(candidate["identity"]) != provenance.get("identity")
    ):
        raise DeliveryQuarantineError(
            "delivery_quarantine.provenance_changed",
            "The requested file no longer matches its audited agent-created state.",
        )


def _path_allowed(path: str, roots: Sequence[str], exact_file_paths: Sequence[str]) -> bool:
    exact = {_canonical_scope_root(root) for root in exact_file_paths}
    for raw_root in roots:
        root = _canonical_scope_root(raw_root)
        if root == ".":
            return True
        if path == root:
            return True
        if root not in exact and path.startswith(f"{root}/"):
            return True
    return False


def _canonical_scope_root(path: object) -> str:
    if not isinstance(path, str) or not path.strip() or any(char in path for char in "*?["):
        raise DeliveryQuarantineError(
            "delivery_quarantine.scope_invalid",
            "The active Fixer scope cannot authorize quarantine safely.",
        )
    value = path.strip().rstrip("/") or "."
    if value == ".":
        return value
    return _canonical_project_path(value)


def _is_private_path(path: str) -> bool:
    return bool(set(PurePosixPath(path).parts) & _PRIVATE_ROOTS)


def _require_ordinary_untracked(root: Path, git_dir: Path, path: str) -> None:
    env = delivery_scope_git_env(root=root, git_dir=git_dir)
    indexed = subprocess.run(
        ["git", "ls-files", "--cached", "--error-unmatch", "--", path],
        cwd=root,
        capture_output=True,
        check=False,
        env=env,
    )
    if indexed.returncode == 0:
        raise DeliveryQuarantineError(
            "delivery_quarantine.indexed_path",
            "Tracked or staged files cannot be quarantined.",
        )
    if indexed.returncode not in {0, 1}:
        raise DeliveryQuarantineError(
            "delivery_quarantine.git_state_unavailable",
            "Sikula could not inspect the current Git index.",
        )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", path],
        cwd=root,
        capture_output=True,
        check=False,
        env=env,
    )
    values = {os.fsdecode(value) for value in untracked.stdout.split(b"\0") if value}
    if untracked.returncode != 0 or values != {path}:
        raise DeliveryQuarantineError(
            "delivery_quarantine.not_ordinary_untracked",
            "Only current ordinary-untracked files can be quarantined.",
        )


def _inspect_candidate(root: Path, path: str) -> dict[str, object]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors: list[int] = []
    file_descriptor = -1
    try:
        descriptor = os.open(root, directory_flags)
        descriptors.append(descriptor)
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=descriptor)
        opened = os.fstat(file_descriptor)
        named = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DeliveryQuarantineError(
                "delivery_quarantine.regular_file_required",
                "Quarantine requires one ordinary single-link regular file.",
            )
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            digest.update(chunk)
        return {
            "identity": (opened.st_dev, opened.st_ino),
            "digest": "sha256:" + digest.hexdigest(),
            "mode": stat.S_IMODE(opened.st_mode),
            "size": opened.st_size,
        }
    except DeliveryQuarantineError:
        raise
    except OSError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.path_unavailable",
            "The requested file could not be opened without following links.",
        ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _move_to_quarantine(
    root: Path,
    common_dir: Path,
    task_id: str,
    path: str,
    quarantine_id: str,
    candidate: dict[str, object],
) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_descriptors: list[int] = []
    destination_descriptors: list[int] = []
    moved = False
    try:
        source = os.open(root, directory_flags)
        source_descriptors.append(source)
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            source = os.open(part, directory_flags, dir_fd=source)
            source_descriptors.append(source)

        destination = os.open(common_dir.resolve(strict=True), directory_flags)
        destination_descriptors.append(destination)
        for component in (*_QUARANTINE_COMPONENTS, delivery_quarantine_task_namespace(task_id)):
            destination = _open_or_create_private_directory(destination, component, directory_flags)
            destination_descriptors.append(destination)
        destination = _create_private_directory(destination, quarantine_id, directory_flags)
        destination_descriptors.append(destination)
        if os.fstat(source).st_dev != os.fstat(destination).st_dev:
            raise DeliveryQuarantineError(
                "delivery_quarantine.cross_device",
                "The private quarantine is not on the delivery worktree filesystem.",
                quarantine_id=quarantine_id,
            )

        try:
            os.stat("entry", dir_fd=destination, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DeliveryQuarantineError(
                "delivery_quarantine.storage_invalid",
                "The private quarantine operation already contains an entry.",
                quarantine_id=quarantine_id,
            )
        os.rename(parts[-1], "entry", src_dir_fd=source, dst_dir_fd=destination)
        moved = True
        quarantined = _inspect_quarantine_entry(destination)
        if quarantined != candidate:
            _restore_quarantine_entry(source, parts[-1], destination)
            moved = False
            raise DeliveryQuarantineError(
                "delivery_quarantine.path_changed",
                "The requested file changed while Sikula moved it into quarantine.",
                quarantine_id=quarantine_id,
            )
    except DeliveryQuarantineError as exc:
        if not moved:
            raise DeliveryQuarantineError(
                exc.code,
                str(exc),
                quarantine_id=quarantine_id,
                move_not_started=True,
            ) from exc
        raise
    except OSError as exc:
        if moved:
            _restore_quarantine_entry(source_descriptors[-1], PurePosixPath(path).name, destination_descriptors[-1])
            moved = False
        raise DeliveryQuarantineError(
            "delivery_quarantine.move_failed",
            "Sikula could not move the requested file into private quarantine.",
            quarantine_id=quarantine_id,
            move_not_started=not moved,
        ) from exc
    finally:
        for descriptor in reversed(destination_descriptors):
            os.close(descriptor)
        for descriptor in reversed(source_descriptors):
            os.close(descriptor)


def _open_or_create_private_directory(parent: int, name: str, flags: int) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, flags, dir_fd=parent)
    value = os.fstat(descriptor)
    if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) & 0o077 or value.st_uid != os.geteuid():
        os.close(descriptor)
        raise DeliveryQuarantineError(
            "delivery_quarantine.storage_invalid",
            "The private quarantine directory is invalid.",
        )
    return descriptor


def _create_private_directory(parent: int, name: str, flags: int) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.storage_invalid",
            "The private quarantine operation already exists.",
        ) from exc
    return _open_or_create_private_directory(parent, name, flags)


def _inspect_quarantine_entry(parent: int) -> dict[str, object]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open("entry", flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        named = os.stat("entry", dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise DeliveryQuarantineError(
                "delivery_quarantine.path_changed",
                "The quarantined entry is no longer the requested regular file.",
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return {
            "identity": (opened.st_dev, opened.st_ino),
            "digest": "sha256:" + digest.hexdigest(),
            "mode": stat.S_IMODE(opened.st_mode),
            "size": opened.st_size,
        }
    finally:
        os.close(descriptor)


def _restore_quarantine_entry(source: int, name: str, destination: int) -> None:
    try:
        os.stat(name, dir_fd=source, follow_symlinks=False)
    except FileNotFoundError:
        os.rename("entry", name, src_dir_fd=destination, dst_dir_fd=source)
        return
    raise DeliveryQuarantineError(
        "delivery_quarantine.restore_blocked",
        "A replacement appeared at the original path; the requested file remains in quarantine.",
    )


def _task_quarantine_path(git_root: Path, task_id: str) -> Path:
    try:
        common_dir = Path(delivery_scope_git_binding(git_root.resolve(strict=True)).common_dir)
    except (DeliveryScopeSnapshotError, OSError, RuntimeError) as exc:
        raise DeliveryQuarantineError(
            "delivery_quarantine.cleanup_unavailable",
            "Sikula could not resolve the repository quarantine.",
        ) from exc
    return common_dir.joinpath(*_QUARANTINE_COMPONENTS, delivery_quarantine_task_namespace(task_id))


def _validate_existing_quarantine_path(path: Path) -> None:
    for candidate in (path.parent.parent, path.parent, path):
        value = candidate.stat(follow_symlinks=False)
        if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) & 0o077 or value.st_uid != os.geteuid():
            raise DeliveryQuarantineError(
                "delivery_quarantine.cleanup_invalid",
                "The task quarantine is not a private directory.",
            )


def _quarantine_path_exists(path: Path) -> bool:
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


class DeliveryQuarantineTool:
    """Move verified files out of a child worktree without deleting their contents."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    @staticmethod
    def supported() -> bool:
        return delivery_quarantine_supported()

    def quarantine(
        self,
        task_id: str,
        path: str,
        provenance: dict[str, object],
        *,
        session_id: str,
        active_write_paths: Sequence[str],
        exact_file_paths: Sequence[str],
        git_root: Path | None = None,
        before_move: Callable[[dict[str, object]], None],
        after_move: Callable[[DeliveryQuarantineResult], None],
    ) -> DeliveryQuarantineResult:
        return quarantine_agent_created_file(
            self._root,
            task_id,
            path,
            provenance,
            session_id=session_id,
            active_write_paths=active_write_paths,
            exact_file_paths=exact_file_paths,
            git_root=git_root,
            before_move=before_move,
            after_move=after_move,
        )
