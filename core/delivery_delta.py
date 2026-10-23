"""Complete, bounded-memory inspection of immutable composition delta metadata."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import subprocess
from threading import Timer
import time
from typing import BinaryIO, Callable, Iterator

from core.worktree import delivery_verification_git_env

METADATA_CHUNK_BYTES = 4096
MAX_METADATA_RECORD_BYTES = 16 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_PATHS = 100_000
METADATA_TIMEOUT_SECONDS = 20
_RAW_ENTRY = re.compile(
    rb":(000000|100644|100755|120000|160000) (000000|100644|100755|120000|160000) ([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) ([AMDT])\Z"
)


@dataclass
class _ScanBudget:
    remaining: int = field(default_factory=lambda: MAX_METADATA_BYTES)
    deadline: float = field(default_factory=lambda: time.monotonic() + METADATA_TIMEOUT_SECONDS)

    def seconds_left(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Composition metadata inspection timed out.")
        return remaining


def _nul_records(stream: BinaryIO, budget: _ScanBudget) -> Iterator[bytes]:
    """Bound both read chunks and individual records, including unterminated tails."""
    pending = b""
    while True:
        budget.seconds_left()
        chunk = stream.read(min(METADATA_CHUNK_BYTES, budget.remaining + 1))
        budget.seconds_left()
        budget.remaining -= len(chunk)
        if budget.remaining < 0:
            raise ValueError("Composition metadata work budget exceeded.")
        if not chunk:
            if pending:
                raise ValueError("Incomplete composition metadata.")
            return
        pending += chunk
        while (end := pending.find(b"\0")) >= 0:
            if end > MAX_METADATA_RECORD_BYTES:
                raise ValueError("Composition metadata record exceeds its budget.")
            record, pending = pending[:end], pending[end + 1 :]
            yield record
        if len(pending) > MAX_METADATA_RECORD_BYTES:
            raise ValueError("Composition metadata record exceeds its budget.")


@contextmanager
def _git_records(root: Path, args: list[str], budget: _ScanBudget) -> Iterator[Iterator[bytes]]:
    """Require successful EOF; close and reap Git on every exit, including interruption."""
    timeout = budget.seconds_left()
    with subprocess.Popen(
        ["git", *args],
        cwd=root,
        env=delivery_verification_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        timer = Timer(timeout, process.kill)
        timer.start()
        records = _nul_records(process.stdout, budget)
        try:
            yield records
            # Callers must consume the complete stream before accepting its result.
            if next(records, None) is not None:
                raise ValueError("Incomplete composition metadata inspection.")
            if process.wait(timeout=budget.seconds_left()):
                raise ValueError("Composition metadata is unavailable.")
            budget.seconds_left()
        finally:
            records.close()
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            timer.join()


def inspect_delta_metadata(
    root: Path, diff_args: list[str], refs: list[str], *, is_private: Callable[[str], bool]
) -> int:
    """Return only a count after matching complete raw/numstat path sequences.

    Length-delimited path digests compare the two immutable Git views without
    retaining paths. Neither counts nor digests grant semantic approval.
    """
    budget = _ScanBudget()
    raw_digest, stats_digest = sha256(), sha256()
    raw_count = stats_count = 0
    with _git_records(root, [*diff_args, "--raw", "--no-abbrev", "-z", *refs, "--", "."], budget) as records:
        for metadata in records:
            fields = _RAW_ENTRY.fullmatch(metadata)
            if fields is None or len(fields[3]) != len(fields[4]):
                raise ValueError("Invalid composition metadata.")
            if b"160000" in (fields[1], fields[2]):
                raise ValueError("Composition delta contains unsupported dependency pointers.")
            path = next(records, None)
            if not path:
                raise ValueError("Composition metadata path is unavailable.")
            relative = path.decode("utf-8")
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts or is_private(relative):
                raise ValueError("Composition metadata path is unavailable.")
            raw_count += 1
            if raw_count > MAX_METADATA_PATHS:
                raise ValueError("Composition metadata path budget exceeded.")
            raw_digest.update(len(path).to_bytes(8, "big"))
            raw_digest.update(path)
    with _git_records(root, [*diff_args, "--numstat", "-z", *refs, "--", "."], budget) as records:
        for entry in records:
            counts = entry.split(b"\t", 2)
            if len(counts) != 3 or not counts[2] or not all(n.isdigit() for n in counts[:2]):
                raise ValueError("Composition delta has unsupported or invalid content metadata.")
            stats_count += 1
            if stats_count > raw_count:
                raise ValueError("Composition metadata paths changed.")
            path = counts[2]
            stats_digest.update(len(path).to_bytes(8, "big"))
            stats_digest.update(path)
    if raw_count != stats_count or raw_digest.digest() != stats_digest.digest():
        raise ValueError("Composition metadata paths changed.")
    return raw_count
