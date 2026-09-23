from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SUPPORTED_DELIVERY_VERIFICATION_SCHEMA_VERSION = 1
DELIVERY_VERIFICATION_STATUSES = frozenset(
    {
        "pending",
        "running",
        "passed",
        "failed",
        "blocked",
        "stale",
        "interrupted",
    }
)
DELIVERY_VERIFICATION_REVIEW_STATUSES = frozenset({"not_run", "approved", "rejected", "blocked"})
_SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_DELIVERY_VERIFICATION_RECOVERY_ACTIONS = {
    "repair_required": "add_delivery_repair_unit",
    "scope_amendment_required": "prepare_delivery_amendment",
    "external_dependency_gap": "resolve_external_dependency",
    "human_review_required": "request_human_review",
    "config_changed": "restart_with_candidate_config",
}


def delivery_verification_recovery_action(stop_code: str | None) -> str:
    if stop_code:
        for suffix, action in _DELIVERY_VERIFICATION_RECOVERY_ACTIONS.items():
            if stop_code.endswith(suffix):
                return action
    return "retry_delivery_verification"


def delivery_verification_covers_obligations(
    record: DeliveryVerificationRecord,
    obligation_count: int,
) -> bool:
    """Return whether persisted evidence covers the current plan obligations."""

    if record.obligation_count != obligation_count:
        return False
    if not record.passed:
        return True
    return record.obligation_satisfied_count == obligation_count and record.obligation_gap_count == 0


@dataclass(frozen=True)
class DeliveryVerificationRecord:
    schema_version: int
    gate_id: str
    candidate_commit: str
    candidate_tree: str
    source_fingerprint: str
    plan_fingerprint: str
    completed_scope_fingerprint: str
    config_fingerprint: str
    policy_fingerprint: str
    status: str
    attempt: int
    semantic_status: str = "not_run"
    security_required: bool = False
    security_status: str = "not_run"
    validation_reused: bool = False
    validation_executed: bool = False
    finding_count: int = 0
    obligation_count: int = 0
    obligation_satisfied_count: int = 0
    obligation_gap_count: int = 0
    stop_code: str | None = None
    evidence_path: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    repair_input_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "gate_id": self.gate_id,
            "candidate_commit": self.candidate_commit,
            "candidate_tree": self.candidate_tree,
            "source_fingerprint": self.source_fingerprint,
            "plan_fingerprint": self.plan_fingerprint,
            "completed_scope_fingerprint": self.completed_scope_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "status": self.status,
            "attempt": self.attempt,
            "semantic_status": self.semantic_status,
            "security_required": self.security_required,
            "security_status": self.security_status,
            "validation_reused": self.validation_reused,
            "validation_executed": self.validation_executed,
            "finding_count": self.finding_count,
            "obligation_count": self.obligation_count,
            "obligation_satisfied_count": self.obligation_satisfied_count,
            "obligation_gap_count": self.obligation_gap_count,
        }
        for key in ("stop_code", "evidence_path", "started_at", "completed_at", "repair_input_fingerprint"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def parse_delivery_verification_record(value: Any) -> DeliveryVerificationRecord:
    if not isinstance(value, dict):
        raise ValueError("delivery verification record must be an object")
    allowed = {
        "schema_version",
        "gate_id",
        "candidate_commit",
        "candidate_tree",
        "source_fingerprint",
        "plan_fingerprint",
        "completed_scope_fingerprint",
        "config_fingerprint",
        "policy_fingerprint",
        "status",
        "attempt",
        "semantic_status",
        "security_required",
        "security_status",
        "validation_reused",
        "validation_executed",
        "finding_count",
        "obligation_count",
        "obligation_satisfied_count",
        "obligation_gap_count",
        "stop_code",
        "evidence_path",
        "started_at",
        "completed_at",
        "repair_input_fingerprint",
    }
    if set(value) - allowed:
        raise ValueError("delivery verification record contains unsupported fields")

    required_strings = (
        "gate_id",
        "candidate_commit",
        "candidate_tree",
        "source_fingerprint",
        "plan_fingerprint",
        "completed_scope_fingerprint",
        "config_fingerprint",
        "policy_fingerprint",
        "status",
    )
    for key in required_strings:
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"delivery verification {key} must be a non-empty string")
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SUPPORTED_DELIVERY_VERIFICATION_SCHEMA_VERSION
    ):
        raise ValueError("delivery verification schema_version is unsupported")
    for key in (
        "gate_id",
        "source_fingerprint",
        "plan_fingerprint",
        "completed_scope_fingerprint",
        "config_fingerprint",
        "policy_fingerprint",
    ):
        if not _SHA256_ID_RE.fullmatch(value[key]):
            raise ValueError(f"delivery verification {key} must be a sha256 identity")
    for key in ("candidate_commit", "candidate_tree"):
        if not _GIT_OBJECT_ID_RE.fullmatch(value[key]):
            raise ValueError(f"delivery verification {key} must be a Git object identity")
    if value["status"] not in DELIVERY_VERIFICATION_STATUSES:
        raise ValueError("delivery verification status is unsupported")

    attempt = value.get("attempt")
    finding_count = value.get("finding_count", 0)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("delivery verification attempt must be a positive integer")
    if not isinstance(finding_count, int) or isinstance(finding_count, bool) or finding_count < 0:
        raise ValueError("delivery verification finding_count must be a non-negative integer")
    obligation_counts: dict[str, int] = {}
    for key in ("obligation_count", "obligation_satisfied_count", "obligation_gap_count"):
        count = value.get(key, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"delivery verification {key} must be a non-negative integer")
        obligation_counts[key] = count
    if (
        obligation_counts["obligation_satisfied_count"] + obligation_counts["obligation_gap_count"]
        > obligation_counts["obligation_count"]
    ):
        raise ValueError("delivery verification obligation counts are inconsistent")
    if value["status"] == "passed" and (
        obligation_counts["obligation_satisfied_count"] != obligation_counts["obligation_count"]
        or obligation_counts["obligation_gap_count"] != 0
    ):
        raise ValueError("passed delivery verification requires complete obligation closure")

    semantic_status = value.get("semantic_status", "not_run")
    security_status = value.get("security_status", "not_run")
    if not isinstance(semantic_status, str) or semantic_status not in DELIVERY_VERIFICATION_REVIEW_STATUSES:
        raise ValueError("delivery verification semantic_status is unsupported")
    if not isinstance(security_status, str) or security_status not in DELIVERY_VERIFICATION_REVIEW_STATUSES:
        raise ValueError("delivery verification security_status is unsupported")

    bool_fields = ("security_required", "validation_reused", "validation_executed")
    for key in bool_fields:
        if not isinstance(value.get(key, False), bool):
            raise ValueError(f"delivery verification {key} must be a boolean")

    optional_strings = ("stop_code", "evidence_path", "started_at", "completed_at")
    for key in optional_strings:
        candidate = value.get(key)
        if candidate is not None and (not isinstance(candidate, str) or not candidate):
            raise ValueError(f"delivery verification {key} must be a non-empty string when present")
    stop_code = value.get("stop_code")
    repair_fingerprint = value.get("repair_input_fingerprint")
    if repair_fingerprint is not None and (
        not isinstance(repair_fingerprint, str) or not _SHA256_ID_RE.fullmatch(repair_fingerprint)
    ):
        raise ValueError("delivery verification repair input fingerprint is invalid")
    if stop_code is not None and not _SAFE_CODE_RE.fullmatch(stop_code):
        raise ValueError("delivery verification stop_code is invalid")
    evidence_path = value.get("evidence_path")
    if evidence_path is not None and (
        evidence_path.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", evidence_path)
        or ".." in evidence_path.replace("\\", "/").split("/")
        or len(evidence_path) > 500
    ):
        raise ValueError("delivery verification evidence_path must be bounded and project-relative")

    return DeliveryVerificationRecord(
        schema_version=SUPPORTED_DELIVERY_VERIFICATION_SCHEMA_VERSION,
        gate_id=value["gate_id"],
        candidate_commit=value["candidate_commit"],
        candidate_tree=value["candidate_tree"],
        source_fingerprint=value["source_fingerprint"],
        plan_fingerprint=value["plan_fingerprint"],
        completed_scope_fingerprint=value["completed_scope_fingerprint"],
        config_fingerprint=value["config_fingerprint"],
        policy_fingerprint=value["policy_fingerprint"],
        status=value["status"],
        attempt=attempt,
        semantic_status=semantic_status,
        security_required=value.get("security_required", False),
        security_status=security_status,
        validation_reused=value.get("validation_reused", False),
        validation_executed=value.get("validation_executed", False),
        finding_count=finding_count,
        obligation_count=obligation_counts["obligation_count"],
        obligation_satisfied_count=obligation_counts["obligation_satisfied_count"],
        obligation_gap_count=obligation_counts["obligation_gap_count"],
        stop_code=stop_code,
        evidence_path=evidence_path,
        started_at=value.get("started_at"),
        completed_at=value.get("completed_at"),
        repair_input_fingerprint=repair_fingerprint,
    )
