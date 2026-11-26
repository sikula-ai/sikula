"""Platform-neutral contracts for reversible agent-created file quarantine."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.state import TaskState


DELIVERY_QUARANTINE_SCHEMA_VERSION = 1
DELIVERY_QUARANTINE_OPERATION = "quarantine_untracked"
MAX_DELIVERY_QUARANTINE_PATH_CHARS = 500

_QUARANTINE_ID_RE = re.compile(r"[0-9a-f]{32}")


def quarantine_run_eligible(state: TaskState) -> bool:
    """Select isolated runs; filesystem and attempt authority are checked separately."""
    return bool(
        state.worktree_path
        and state.worktree_base
        and (
            not (state.delivery_plan_id or state.delivery_unit_id)
            or (
                state.delivery_plan_id
                and state.delivery_unit_id
                and state.delivery_write_scope_schema_version is not None
            )
        )
    )


class DeliveryQuarantineError(RuntimeError):
    """Raised when a requested quarantine move cannot be completed safely."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        quarantine_id: str | None = None,
        move_not_started: bool = False,
        retry_safe: bool = False,
    ) -> None:
        self.code = code
        self.quarantine_id = quarantine_id
        self.move_not_started = move_not_started
        # Only the filesystem tool can attest fresh source and Git checks.
        # Knowing that a move did not complete is not sufficient on its own.
        self.retry_safe = retry_safe
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryQuarantineResult:
    path: str
    quarantine_id: str
    digest: str
    mode: int
    size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "quarantine_id": self.quarantine_id,
            "digest": self.digest,
            "mode": self.mode,
            "size": self.size,
        }


def delivery_quarantine_task_namespace(task_id: str) -> str:
    """Return an opaque, filesystem-safe namespace for one task's quarantine."""

    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def valid_delivery_quarantine_id(value: object) -> bool:
    return isinstance(value, str) and _QUARANTINE_ID_RE.fullmatch(value) is not None


def delivery_quarantine_has_incomplete_move(records: object) -> bool:
    if not isinstance(records, list):
        return True
    for record in records:
        if not isinstance(record, dict) or record.get("status") not in {
            "moving",
            "quarantined",
            "cleaned",
            "aborted",
        }:
            return True
        if record["status"] == "moving":
            return True
    return False


def quarantine_recovery_preserves_budgets(records: list[dict]) -> bool:
    """Keep consumed repair budgets until an interrupted Implementer move is recovered."""
    return any(
        isinstance(record, dict)
        and record.get("requested_by") == "implementer"
        and (record.get("status") == "moving" or record.get("recovery_pending") is True)
        for record in records
    )


def quarantine_prunable_paths(state: TaskState, records: list[dict]) -> set[str]:
    """Keep original review changes even when an agent-created replacement is removed."""
    return {
        record["path"]
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and (
            state.review_mode not in {"review_report", "review_fix"} or record.get("review_preexisting_change") is False
        )
    }
