"""Operation-local, bounded reads of private verification evidence.

Only parsed artifacts and exact-commit Git fingerprints are shared. Callers still
validate the expected digest, accepted record, scope and candidate on every use.
Never retain this context across provider calls, publication, lock reacquisition
or a new status snapshot.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from functools import wraps
from hashlib import sha256
from itertools import chain
from pathlib import Path
import sys
from typing import Any, Callable, Iterator

from core.delivery_repair_storage import read_repair_state, read_repair_state_bytes, repair_state_identity


def evidence_operation(function):
    """Explicit resolver injection for pure read operations; never decorate provider work."""

    @wraps(function)
    def scoped(*args, resolver=None, **kwargs):
        if resolver is not None:
            resolver._check()
            return function(*args, resolver=resolver, **kwargs)
        with EvidenceResolver() as resolver:
            return function(*args, resolver=resolver, **kwargs)

    return scoped


EVIDENCE_BUDGET_STOP = "delivery_verification.evidence_budget_exhausted"


def evidence_failure_code(error: BaseException, default: str) -> str:
    return EVIDENCE_BUDGET_STOP if isinstance(error, EvidenceResolutionLimit) else default


def evidence_failure_message(error: BaseException, default: str) -> str:
    if isinstance(error, EvidenceResolutionLimit):
        return "Evidence resolution exceeded its host-resource allowance; investigate the evidence graph and supported limits."
    return default


class EvidenceResolutionLimit(ValueError):
    """Evidence work exhausted its host-resource allowance, not a review verdict."""


@dataclass(frozen=True)
class EvidenceResolutionLimits:
    artifacts: int = 4096
    reads: int = 16384
    bytes_read: int = 64 * 1024 * 1024
    materialized_bytes: int = 128 * 1024 * 1024
    cache_bytes: int = 8 * 1024 * 1024
    depth: int = 128
    visits: int = 16384
    expanded_items: int = 1_000_000


def _weight(value: Any, limit: int) -> int:
    """Conservative Python object cost; bound the walk without a wide work list."""
    pending = [iter((value,))]
    size = 0
    while pending:
        item = next(pending[-1], pending)
        if item is pending:
            pending.pop()
            continue
        size += sys.getsizeof(item)
        if size > limit or len(pending) > 256:
            raise EvidenceResolutionLimit("Evidence materialization exceeds its resource budget.")
        if isinstance(item, dict):
            pending.append(chain(item.keys(), item.values()))
        elif is_dataclass(item) and not isinstance(item, type):
            size += sys.getsizeof(getattr(item, "__dict__", None))
            pending.append(iter(tuple(getattr(item, field.name) for field in fields(item))))
        elif isinstance(item, (list, tuple)):
            pending.append(iter(item))
    return size


class EvidenceResolver:
    """Share reads within one captured, read-only evidence operation only."""

    def __init__(self, limits: EvidenceResolutionLimits | None = None):
        self.limits = limits or EvidenceResolutionLimits()
        self.bytes_read = self.reads = self.materialized_bytes = self.cache_bytes = 0
        self.visits = self.expanded_items = self._integrity_bytes = 0
        self._identities: dict[tuple[Path, Path], tuple[int, ...]] = {}
        self._digests: dict[tuple[Path, Path], str] = {}
        self._git_keys: set[tuple] = set()
        self._cache: OrderedDict[tuple, tuple[dict[str, Any], int]] = OrderedDict()
        self._active: set[tuple[str, ...]] = set()
        self._failed = False
        self._changed = False

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self._check()
                # Timestamps are only an early-change check. In particular Windows
                # ctime need not change for an in-place edit with restored mtime.
                for (root, path), digest in self._digests.items():
                    identity = repair_state_identity(root, path)
                    if identity != self._identities[root, path]:
                        raise ValueError("Evidence changed during resolution.")
                    self._charge("bytes_read", identity[2])
                    content = read_repair_state_bytes(root, path)
                    if content is None or sha256(content).hexdigest() != digest:
                        raise ValueError("Evidence changed during resolution.")
        finally:
            self._cache.clear()
            self._identities.clear()
            self._digests.clear()
            self._git_keys.clear()
            self.cache_bytes = 0
            self._failed = True

    def _check(self) -> None:
        if self._changed:
            raise ValueError("Evidence changed during resolution.")
        if self._failed:
            raise EvidenceResolutionLimit("Evidence resolution must restart with fresh inputs.")

    def _charge(self, field: str, amount: int) -> None:
        self._check()
        value = getattr(self, field) + amount
        if value > getattr(self.limits, field):
            self._failed = True
            self._cache.clear()
            self.cache_bytes = 0
            raise EvidenceResolutionLimit("Evidence resolution exceeds its resource budget.")
        setattr(self, field, value)

    def materialize(self, value: Any) -> int:
        """Charge transient expanded authority as well as raw JSON copies."""
        self._check()
        try:
            size = _weight(value, self.limits.materialized_bytes - self.materialized_bytes)
        except EvidenceResolutionLimit:
            self._failed = True
            raise
        self._charge("materialized_bytes", size)
        return size

    def expand(self, count: int) -> None:
        self._charge("expanded_items", count)

    @contextmanager
    def visit(self, *identity: str) -> Iterator[None]:
        self._charge("visits", 1)
        key = tuple(identity)
        if key in self._active:
            raise ValueError("Cyclic verification evidence.")
        if len(self._active) >= self.limits.depth:
            self._failed = True
            raise EvidenceResolutionLimit("Evidence traversal exceeds its depth budget.")
        self._active.add(key)
        try:
            yield
        finally:
            self._active.remove(key)

    def read(self, root: Path, path: Path, *, reader: Callable = read_repair_state) -> dict[str, Any] | None:
        self._charge("reads", 1)
        # Keep lexical path components: resolving them would hide symlink/traversal checks.
        key = (root.absolute(), path.absolute())
        identity = repair_state_identity(root, path)
        previous = self._identities.get(key)
        if previous is not None and previous != identity:
            self._changed = True
            self._failed = True
            self._cache.clear()
            self.cache_bytes = 0
            raise ValueError("Evidence changed during resolution.")
        if identity is None:
            return None
        if previous is None:
            if len(self._identities) + len(self._git_keys) >= self.limits.artifacts:
                self._failed = True
                raise EvidenceResolutionLimit("Evidence graph exceeds its artifact budget.")
            self._identities[key] = identity
            self._integrity_bytes += identity[2]
        cached = self._cache.get(key)
        if cached is None:
            self._charge("bytes_read", identity[2])
            # Reserve capacity for the final integrity reads before accepting data.
            if self.bytes_read + self._integrity_bytes > self.limits.bytes_read:
                self._failed = True
                raise EvidenceResolutionLimit("Evidence integrity reads exceed the resource budget.")
            captured = []
            value = reader(root, path, _capture=lambda content: captured.append(sha256(content).hexdigest()))
            if value is None or repair_state_identity(root, path) != identity:
                self._changed = True
                self._failed = True
                raise ValueError("Evidence changed during resolution.")
            if len(captured) != 1 or (key in self._digests and self._digests[key] != captured[0]):
                self._changed = True
                self._failed = True
                raise ValueError("Evidence changed during resolution.")
            self._digests[key] = captured[0]
            try:
                size = _weight(value, self.limits.materialized_bytes - self.materialized_bytes)
            except EvidenceResolutionLimit:
                self._failed = True
                raise
            self._charge("materialized_bytes", size)
            if not self._retain(key, value, size):
                return value
        else:
            value, size = cached
            self._cache.move_to_end(key)
        # Each caller gets its own object; validation cannot mutate a cached authority.
        self._charge("materialized_bytes", size)
        return deepcopy(value)

    def _retain(self, key: tuple, value: dict, size: int) -> bool:
        if size > self.limits.cache_bytes:
            return False
        while self.cache_bytes + size > self.limits.cache_bytes:
            _, (_, evicted) = self._cache.popitem(last=False)
            self.cache_bytes -= evicted
        self._cache[key] = (value, size)
        self.cache_bytes += size
        return True

    def git_read_started(self) -> None:
        """Count actual Git exchanges, including repository-context checks on hits."""
        self._charge("reads", 1)

    def git_read_finished(self, stdout: bytes, stderr: bytes) -> None:
        self._charge("bytes_read", len(stdout) + len(stderr))
        self.materialize((stdout, stderr))
        if self.bytes_read + self._integrity_bytes > self.limits.bytes_read:
            self._failed = True
            raise EvidenceResolutionLimit("Evidence integrity reads exceed the resource budget.")

    def _git_key(self, identity: tuple) -> tuple:
        self._check()
        key = ("git-fingerprints", *identity)
        if key not in self._git_keys:
            if len(self._identities) + len(self._git_keys) >= self.limits.artifacts:
                self._failed = True
                raise EvidenceResolutionLimit("Evidence graph exceeds its artifact budget.")
            self.materialize(key)
            self._git_keys.add(key)
        return key

    def git_blob_fingerprints(
        self,
        identity: tuple,
        batch: list[tuple[str, int]],
        reader: Callable[[list[tuple[str, int]]], dict[str, str]],
    ) -> dict[str, str]:
        """Reuse individual immutable blobs after the caller validates path metadata.

        Only missing blobs reach the bounded reader. Nothing from that exchange
        is retained until the reader has validated its complete response.
        """
        # Charge the logical batch once, not each in-memory blob lookup.
        # Individual keys still consume artifact/materialization allowances;
        # actual Git exchanges are charged separately by git_read_started().
        self._charge("reads", 1)
        hashes: dict[str, str] = {}
        missing: list[tuple[str, int]] = []
        keys: dict[str, tuple] = {}
        for oid, size in batch:
            key = self._git_key(("blob", *identity, oid, size))
            cached = self._cache.get(key)
            if cached is None:
                missing.append((oid, size))
                keys[oid] = key
            else:
                value, _ = cached
                hashes.update(value)
                self._cache.move_to_end(key)
        self.materialize((missing, keys, hashes))
        if missing:
            values = reader(missing)
            for oid, _ in missing:
                value = {oid: values[oid]}
                key = keys[oid]
                self._retain(key, value, self.materialize((key, value)))
            hashes.update(values)
        self.materialize(hashes)
        return hashes

    def git_fingerprints(self, identity: tuple, reader: Callable[[], dict[str, str]]) -> dict[str, str]:
        """Share successful immutable fingerprints, never handoff validation decisions.

        The caller supplies an exact object ID, fresh repository context and all
        interpretation policies. Git output and copies share the sidecar budgets.
        """
        self._charge("reads", 1)
        key = self._git_key(identity)
        cached = self._cache.get(key)
        if cached is None:
            value = reader()
            # Include retained key storage in the shared LRU allowance.
            size = self.materialize((key, value))
            self._retain(key, value, size)
        else:
            value, _ = cached
            self._cache.move_to_end(key)
        self.materialize(value)
        return value.copy()
