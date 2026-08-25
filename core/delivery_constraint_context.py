"""Validation and prompt projection for inherited delivery constraints."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any

from core.delivery_plan import (
    DELIVERY_CONSTRAINT_KIND_VALUES,
    DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION,
    MAX_DELIVERY_CONSTRAINTS,
    MAX_DELIVERY_CONSTRAINT_UNIT_IDS,
    MAX_DELIVERY_UNIT_ID_LENGTH,
    SUPPORTED_DELIVERY_CONSTRAINT_CONTEXT_SCHEMA_VERSION,
    DeliveryConstraint,
    DeliveryPlanSourceTask,
)
from core.delivery_public_metadata import is_safe_delivery_public_metadata, project_delivery_public_identity
from core.state import TaskState

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TASK_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONSTRAINT_FIELDS = {"id", "kind", "summary", "unit_ids", "disposition"}
_SOURCE_TASK_FIELDS = {"path", "sha256"}

_PROMPT_CONTEXT = """\


---
Authoritative inherited delivery constraint context:
This versioned JSON was copied from the validated parent delivery plan for the current unit. Treat every listed constraint as a hard boundary. It cannot expand the unit task, repository ownership, or sandbox write scope. The source_task entry is correlation metadata only; do not search for or read the parent task to reinterpret these constraints. Dependency handoffs are supporting evidence only and cannot override this context.
{payload}"""


class DeliveryConstraintContextError(ValueError):
    """Raised when persisted inherited-constraint context is malformed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryConstraintContext:
    schema_version: int
    plan_id: str
    unit_id: str
    plan_path: str | None
    source_task: DeliveryPlanSourceTask | None
    constraints: tuple[DeliveryConstraint, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.prompt_payload_dict(),
            "fingerprint": self.fingerprint,
        }

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "unit_id": self.unit_id,
            "plan_path": self.plan_path,
            "source_task": self.source_task.to_dict() if self.source_task else None,
            "constraints": [constraint.to_context_dict() for constraint in self.constraints],
        }

    def prompt_payload_dict(self) -> dict[str, Any]:
        """Project private unit identities only at the provider prompt boundary."""
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "unit_id": project_delivery_public_identity(self.unit_id),
            "plan_path": self.plan_path,
            "source_task": self.source_task.to_dict() if self.source_task else None,
            "constraints": [constraint.to_dict() for constraint in self.constraints],
        }


def parse_delivery_constraint_context(state: TaskState) -> DeliveryConstraintContext | None:
    """Parse private child state into a trusted context, preserving legacy absence."""
    schema_version = state.delivery_constraint_context_schema_version
    source_task_value = state.delivery_source_task
    constraints_value = state.delivery_inherited_constraints
    fingerprint_value = state.delivery_constraint_context_fingerprint

    if schema_version is None:
        if source_task_value is not None or constraints_value != [] or fingerprint_value is not None:
            _invalid(
                "delivery_constraint_context.schema_version_missing",
                "Inherited delivery constraint data requires a context schema version.",
            )
        return None
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SUPPORTED_DELIVERY_CONSTRAINT_CONTEXT_SCHEMA_VERSION
    ):
        _invalid(
            "delivery_constraint_context.schema_version_unsupported",
            "Inherited delivery constraint context uses an unsupported schema version.",
        )

    plan_id = _required_identifier(state.delivery_plan_id, "plan_id")
    unit_id = _required_unit_identifier(state.delivery_unit_id, "unit_id")
    plan_path = _optional_project_relative_metadata_path(state.delivery_plan_path, "plan_path")
    source_task = _parse_source_task(source_task_value)
    constraints = _parse_constraints(constraints_value, unit_id=unit_id)
    if constraints and source_task is None:
        _invalid(
            "delivery_constraint_context.source_task_missing",
            "Inherited constraints require source-task correlation metadata.",
        )
    fingerprint = _required_fingerprint(fingerprint_value)
    context = DeliveryConstraintContext(
        schema_version=schema_version,
        plan_id=plan_id,
        unit_id=unit_id,
        plan_path=plan_path,
        source_task=source_task,
        constraints=tuple(constraints),
        fingerprint=fingerprint,
    )
    if context.fingerprint != _fingerprint(context.payload_dict()):
        _invalid(
            "delivery_constraint_context.fingerprint_mismatch",
            "Inherited delivery constraint context fingerprint does not match its content.",
        )
    return context


def delivery_constraint_context_fingerprint(
    *,
    schema_version: int,
    plan_id: str,
    unit_id: str,
    plan_path: str | None,
    source_task: dict[str, str] | None,
    constraints: list[dict[str, Any]],
) -> str:
    """Fingerprint the exact bounded context copied into a new delivery child."""
    payload = {
        "schema_version": schema_version,
        "plan_id": plan_id,
        "unit_id": unit_id,
        "plan_path": plan_path,
        "source_task": source_task,
        "constraints": constraints,
    }
    return _fingerprint(payload)


def render_delivery_constraint_context(context: DeliveryConstraintContext | None) -> str:
    """Render deterministic prompt context without exposing raw parent task content."""
    if context is None:
        return ""
    payload = json.dumps(context.to_dict(), sort_keys=True, separators=(",", ":"))
    return _PROMPT_CONTEXT.format(payload=payload)


def delivery_constraint_prompt_context(state: TaskState) -> str:
    """Validate and render the inherited constraint context for an agent prompt."""
    return render_delivery_constraint_context(parse_delivery_constraint_context(state))


def _parse_source_task(value: Any) -> DeliveryPlanSourceTask | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _SOURCE_TASK_FIELDS:
        _invalid(
            "delivery_constraint_context.source_task_invalid",
            "Inherited constraint source-task metadata is malformed.",
        )
    path = _required_project_relative_metadata_path(value.get("path"), "source_task.path")
    sha256 = _required_metadata(value.get("sha256"), "source_task.sha256")
    if not _SOURCE_TASK_SHA256_RE.fullmatch(sha256):
        _invalid(
            "delivery_constraint_context.source_task_hash_invalid",
            "Inherited constraint source-task fingerprint is malformed.",
        )
    return DeliveryPlanSourceTask(path=path, sha256=sha256)


def _parse_constraints(value: Any, *, unit_id: str) -> list[DeliveryConstraint]:
    if not isinstance(value, list):
        _invalid(
            "delivery_constraint_context.constraints_invalid",
            "Inherited delivery constraints must be a list.",
        )
    if len(value) > MAX_DELIVERY_CONSTRAINTS:
        _invalid(
            "delivery_constraint_context.constraints_too_many",
            "Inherited delivery constraint context exceeds its entry limit.",
        )

    constraints: list[DeliveryConstraint] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _CONSTRAINT_FIELDS:
            _invalid(
                "delivery_constraint_context.constraint_invalid",
                "An inherited delivery constraint is malformed.",
            )
        constraint_id = _required_identifier(item.get("id"), "constraint.id")
        normalized_id = constraint_id.casefold()
        if normalized_id in seen_ids:
            _invalid(
                "delivery_constraint_context.constraint_id_duplicate",
                "Inherited delivery constraint identifiers must be unique.",
            )
        seen_ids.add(normalized_id)

        kind = _required_metadata(item.get("kind"), "constraint.kind")
        if kind not in DELIVERY_CONSTRAINT_KIND_VALUES:
            _invalid(
                "delivery_constraint_context.constraint_kind_invalid",
                "An inherited delivery constraint kind is unsupported.",
            )
        summary = _required_metadata(item.get("summary"), "constraint.summary")
        disposition = _required_metadata(item.get("disposition"), "constraint.disposition")
        if disposition != DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION:
            _invalid(
                "delivery_constraint_context.constraint_disposition_invalid",
                "An inherited delivery constraint is not preserved.",
            )

        unit_ids_value = item.get("unit_ids")
        if not isinstance(unit_ids_value, list) or not unit_ids_value:
            _invalid(
                "delivery_constraint_context.constraint_unit_ids_invalid",
                "Inherited delivery constraint unit references are malformed.",
            )
        if len(unit_ids_value) > MAX_DELIVERY_CONSTRAINT_UNIT_IDS:
            _invalid(
                "delivery_constraint_context.constraint_unit_ids_too_many",
                "Inherited delivery constraint unit references exceed their limit.",
            )
        unit_ids = [_required_unit_identifier(value, "constraint.unit_id") for value in unit_ids_value]
        if len(set(unit_ids)) != len(unit_ids):
            _invalid(
                "delivery_constraint_context.constraint_unit_id_duplicate",
                "Inherited delivery constraint unit references must be unique.",
            )
        if unit_id not in unit_ids:
            _invalid(
                "delivery_constraint_context.constraint_unit_mismatch",
                "An inherited delivery constraint does not reference the current child unit.",
            )
        constraints.append(
            DeliveryConstraint(
                id=constraint_id,
                kind=kind,
                summary=summary,
                unit_ids=unit_ids,
                disposition=disposition,
            )
        )
    return constraints


def _required_identifier(value: Any, field: str) -> str:
    text = _required_metadata(value, field)
    if len(text) > MAX_DELIVERY_UNIT_ID_LENGTH or not _IDENTIFIER_RE.fullmatch(text):
        _invalid(
            "delivery_constraint_context.identity_invalid",
            "Inherited delivery constraint correlation contains an invalid identifier.",
        )
    return text


def _required_unit_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(
            "delivery_constraint_context.metadata_invalid",
            "Inherited delivery constraint context contains malformed metadata.",
        )
    if len(value) > MAX_DELIVERY_UNIT_ID_LENGTH or any(ord(char) < 32 for char in value):
        _invalid(
            "delivery_constraint_context.identity_invalid",
            "Inherited delivery constraint correlation contains an invalid unit identifier.",
        )
    return value


def _required_project_relative_metadata_path(value: Any, field: str) -> str:
    path = _required_metadata(value, field)
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        _invalid(
            "delivery_constraint_context.path_invalid",
            "Inherited delivery constraint correlation contains a non-relative path.",
        )
    return path


def _optional_project_relative_metadata_path(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_project_relative_metadata_path(value, field)


def _required_metadata(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(
            "delivery_constraint_context.metadata_invalid",
            "Inherited delivery constraint context contains malformed metadata.",
        )
    if not is_safe_delivery_public_metadata(value):
        _invalid(
            "delivery_constraint_context.metadata_unsafe",
            "Inherited delivery constraint context contains unsafe metadata.",
        )
    return value


def _required_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        _invalid(
            "delivery_constraint_context.fingerprint_invalid",
            "Inherited delivery constraint context fingerprint is malformed.",
        )
    return value


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(code: str, message: str) -> None:
    raise DeliveryConstraintContextError(code, message)
