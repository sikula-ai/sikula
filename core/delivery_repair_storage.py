"""Private, bounded control snapshots for delivery integration recovery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator
from uuid import uuid4


_MAX_STATE_BYTES = 2 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate delivery recovery control field.")
        result[key] = value
    return result


def _link_like(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


@contextmanager
def _parent(root: Path, path: Path, *, create: bool) -> Iterator[int | None]:
    relative = path.absolute().relative_to(root.resolve(strict=True))
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("Invalid delivery recovery state path.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None if os.name == "nt" else os.open(root, flags)
    current = root
    try:
        for part in relative.parts[:-1]:
            current /= part
            if descriptor is None:
                if create:
                    current.mkdir(mode=0o700, exist_ok=True)
                metadata = current.lstat()
                if _link_like(metadata) or not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("Unsafe delivery recovery state directory.")
            else:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_repair_state(root: Path, path: Path) -> dict[str, Any] | None:
    try:
        with _parent(root, path, create=False) as parent:
            target = path if parent is None else path.name
            metadata = os.stat(target, dir_fd=parent, follow_symlinks=False)
            if _link_like(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("Unsafe delivery recovery state file.")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(target, flags, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise OSError("Delivery recovery state file changed.")
                content = handle.read(_MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > _MAX_STATE_BYTES:
        raise ValueError("Delivery recovery state is oversized.")
    value = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("Delivery recovery state must be an object.")
    return value


def write_repair_state(root: Path, path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")
    if len(content) > _MAX_STATE_BYTES:
        raise ValueError("Delivery recovery state is oversized.")
    with _parent(root, path, create=True) as parent:
        target = path if parent is None else path.name
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temp_target = temporary if parent is None else temporary.name
        try:
            try:
                metadata = os.stat(target, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if _link_like(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError("Unsafe delivery recovery state file.")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            fd = os.open(temp_target, flags, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_target, target, src_dir_fd=parent, dst_dir_fd=parent)
            if parent is not None:
                os.fsync(parent)
        finally:
            try:
                os.unlink(temp_target, dir_fd=parent)
            except FileNotFoundError:
                pass
