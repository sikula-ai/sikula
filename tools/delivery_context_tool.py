"""Bounded local evidence retrieval for a delivery preparation correction."""

from __future__ import annotations

from hashlib import sha256
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from core.delivery_plan import is_private_delivery_source_task_path
from core.delivery_verification import (
    delivery_verification_allowed_read_paths,
    delivery_verification_source_task_is_private,
)
from tools.base_tool import Sandbox


def read_delivery_context(root: Path, paths: list[str], config: dict[str, Any]) -> dict[str, Any]:
    """Read requested regular project files without shell, network, or write access."""
    root = root.resolve()
    try:
        allowed = delivery_verification_allowed_read_paths(config)
    except ValueError:
        return {"status": "read_scope_invalid"}
    sandbox = Sandbox(root, [], allowed)
    records: list[dict[str, Any]] = []
    remaining = 64_000
    for value in paths[:8]:
        record: dict[str, Any] = {"request_index": len(records), "status": "unavailable"}
        records.append(record)
        path = Path(value)
        if (
            path.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or ".." in path.parts
            or is_private_delivery_source_task_path(value)
        ):
            record["status"] = "denied"
            continue
        try:
            resolved = (root / path).resolve()
            resolved.relative_to(root)
            if resolved != root / path:
                record["status"] = "denied"
                continue
            sandbox.check_read(resolved)
            if delivery_verification_source_task_is_private(root, resolved, value, config):
                record["status"] = "denied"
                continue
            content = _read_regular_file(root, path, min(16_000, remaining) + 1)
            if len(content) > min(16_000, remaining):
                record["status"] = "too_large"
                continue
            text = content.decode("utf-8")
            if "\x00" in text:
                continue
            remaining -= len(content)
            record.update(
                status="read", path=path.as_posix(), sha256="sha256:" + sha256(content).hexdigest(), text=text
            )
        except PermissionError:
            record["status"] = "denied"
        except (OSError, ValueError, RuntimeError, UnicodeError):
            continue
    return {"files": records}


def _read_regular_file(root: Path, path: Path, limit: int) -> bytes:
    # Verify every lexical binding before and after the read. Rejected content
    # never reaches a prompt, including on platforms without O_NOFOLLOW.
    root_metadata = root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode) or getattr(root_metadata, "st_file_attributes", 0) & 0x400:
        raise PermissionError("Context root identity changed.")
    locations = [(root, root_metadata)]
    current = root
    for part in path.parts:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise PermissionError("Linked context paths are not supported.")
        locations.append((current, metadata))
    if len(locations) < 2 or not stat.S_ISREG(locations[-1][1].st_mode) or locations[-1][1].st_nlink != 1:
        raise PermissionError("Context must be a regular unlinked file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(root / path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        expected = locations[-1][1]
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise PermissionError("Context identity changed.")
        if (opened.st_size, opened.st_mtime_ns) != (expected.st_size, expected.st_mtime_ns):
            raise PermissionError("Context changed before inspection.")
        content = stream.read(limit)
        after = os.fstat(stream.fileno())
        # Windows Python 3.12 can give lstat() and fstat() different ctime meanings.
        # Compare ctime only within each API's before/after snapshots.
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise PermissionError("Context changed during inspection.")
    for location, before in locations:
        after = location.lstat()
        if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
            raise PermissionError("Context path changed during inspection.")
        if stat.S_ISREG(before.st_mode) and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise PermissionError("Context changed during inspection.")
    return content
