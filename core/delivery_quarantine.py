"""Platform-neutral contracts for reversible delivery-file quarantine."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


DELIVERY_QUARANTINE_SCHEMA_VERSION = 1
DELIVERY_QUARANTINE_OPERATION = "quarantine_untracked"
MAX_DELIVERY_QUARANTINE_PATH_CHARS = 500

_QUARANTINE_ID_RE = re.compile(r"[0-9a-f]{32}")


class DeliveryQuarantineError(RuntimeError):
    """Raised when a requested quarantine move cannot be completed safely."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        quarantine_id: str | None = None,
        move_not_started: bool = False,
    ) -> None:
        self.code = code
        self.quarantine_id = quarantine_id
        self.move_not_started = move_not_started
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
