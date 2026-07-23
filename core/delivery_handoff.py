"""Versioned, privacy-safe delivery unit handoff artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Any

from core.delivery_progress import delivery_progress_path

SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_VALIDATION_STATUSES = {"success", "failed", "skipped"}
_MAX_METADATA_TEXT = 1000
_HANDOFF_FILENAME_PREFIX_LENGTH = 48


class DeliveryHandoffError(ValueError):
    """Raised when a delivery handoff is malformed or conflicts with durable evidence."""


@dataclass(frozen=True)
class DeliveryUnitHandoff:
    schema_version: int
    plan_id: str
    unit_id: str
    child_task_id: str
    result_branch: str | None
    result_commit: str | None
    files_changed: list[str]
    completed_at: str
    unit_title: str | None
    component: str | None
    depends_on: list[str]
    scope_paths: list[str]
    build_status: str | None
    test_status: str | None
    check_status: str | None
    validation_records_count: int
    validation_failures_count: int
    review_records_count: int
    security_review_records_count: int
    test_files_written: list[str]
    test_writer_runs: int
    testability_gaps_count: int
    fingerprint: str

    def payload_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "unit_id": self.unit_id,
            "child_task_id": self.child_task_id,
            "result_branch": self.result_branch,
            "result_commit": self.result_commit,
            "files_changed": list(self.files_changed),
            "completed_at": self.completed_at,
            "unit": {
                "title": self.unit_title,
                "component": self.component,
                "depends_on": list(self.depends_on),
                "scope_paths": list(self.scope_paths),
            },
            "validation": {
                "build_status": self.build_status,
                "test_status": self.test_status,
                "check_status": self.check_status,
                "records_count": self.validation_records_count,
                "failures_count": self.validation_failures_count,
                "review_records_count": self.review_records_count,
                "security_review_records_count": self.security_review_records_count,
            },
            "tests": {
                "files_written": list(self.test_files_written),
                "test_writer_runs": self.test_writer_runs,
                "testability_gaps_count": self.testability_gaps_count,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload_dict(), "fingerprint": self.fingerprint}


def delivery_unit_handoff_path(project_root: Path, plan_id: str, unit_id: str) -> Path:
    _required_identifier(plan_id, "plan_id")
    normalized_unit_id = _required_unit_identifier(unit_id, "unit_id")
    root = project_root.resolve()
    path = delivery_progress_path(root, plan_id).parent / "handoffs" / _handoff_filename(normalized_unit_id)
    try:
        path.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise DeliveryHandoffError("delivery handoff path escapes the project root") from exc
    return path


def build_delivery_unit_handoff(
    *,
    plan_id: str,
    selected_unit: Any,
    child_task_id: str,
    child_state: Any,
) -> DeliveryUnitHandoff:
    schema_version = getattr(child_state, "delivery_handoff_schema_version", None)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION
    ):
        raise DeliveryHandoffError("child task does not opt in to the supported delivery handoff schema")

    unit_id = _required_unit_identifier(getattr(selected_unit, "id", None), "unit_id")
    validation_records = _required_list(getattr(child_state, "validation_cycle_records", None), "validation records")
    test_files = _safe_paths(_required_list(getattr(child_state, "test_files_written", None), "test files"))
    completed_at = _required_timestamp(
        getattr(child_state, "finished_at", None) or getattr(child_state, "updated_at", None),
        "completed_at",
    )

    handoff = DeliveryUnitHandoff(
        schema_version=schema_version,
        plan_id=_required_identifier(plan_id, "plan_id"),
        unit_id=unit_id,
        child_task_id=_required_identifier(child_task_id, "child_task_id"),
        result_branch=_optional_text(getattr(child_state, "worktree_branch", None), "result_branch"),
        result_commit=_optional_text(getattr(child_state, "result_commit", None), "result_commit"),
        files_changed=_safe_paths(_required_list(getattr(child_state, "files_changed", None), "files changed")),
        completed_at=completed_at,
        unit_title=_project_optional_label(getattr(selected_unit, "title", None), "unit.title"),
        component=_project_optional_label(getattr(selected_unit, "component", None), "unit.component"),
        depends_on=[
            _required_unit_identifier(value, "unit.depends_on")
            for value in _required_list(selected_unit.depends_on, "depends_on")
        ],
        scope_paths=_safe_paths(_required_list(getattr(selected_unit, "scope_paths", []), "scope_paths")),
        build_status=_optional_status(getattr(child_state, "build_status", None), "build_status"),
        test_status=_optional_status(getattr(child_state, "test_status", None), "test_status"),
        check_status=_optional_status(getattr(child_state, "check_status", None), "check_status"),
        validation_records_count=len(validation_records),
        validation_failures_count=sum(
            1 for record in validation_records if isinstance(record, dict) and record.get("status") == "failed"
        ),
        review_records_count=len(_required_list(getattr(child_state, "review_cycle_records", None), "review records")),
        security_review_records_count=len(
            _required_list(getattr(child_state, "security_review_cycle_records", None), "security review records")
        ),
        test_files_written=test_files,
        test_writer_runs=len(_required_list(getattr(child_state, "test_write_records", None), "test write records")),
        testability_gaps_count=len(_required_list(getattr(child_state, "testability_gaps", None), "testability gaps")),
        fingerprint="",
    )
    return _with_fingerprint(handoff)


def delivery_unit_handoff_matches_unit(handoff: DeliveryUnitHandoff, unit: Any) -> bool:
    try:
        return (
            handoff.unit_id == _required_unit_identifier(getattr(unit, "id", None), "unit_id")
            and handoff.unit_title == _project_optional_label(getattr(unit, "title", None), "unit.title")
            and handoff.component == _project_optional_label(getattr(unit, "component", None), "unit.component")
            and handoff.depends_on
            == [
                _required_unit_identifier(value, "unit.depends_on")
                for value in _required_list(getattr(unit, "depends_on", None), "unit.depends_on")
            ]
            and handoff.scope_paths
            == _safe_paths(_required_list(getattr(unit, "scope_paths", None), "unit.scope_paths"))
        )
    except DeliveryHandoffError:
        return False


def parse_delivery_unit_handoff(data: Any) -> DeliveryUnitHandoff:
    if not isinstance(data, dict):
        raise DeliveryHandoffError("delivery handoff must be an object")
    if set(data) != {
        "schema_version",
        "plan_id",
        "unit_id",
        "child_task_id",
        "result_branch",
        "result_commit",
        "files_changed",
        "completed_at",
        "unit",
        "validation",
        "tests",
        "fingerprint",
    }:
        raise DeliveryHandoffError("delivery handoff contains unexpected or missing fields")

    schema_version = _required_nonnegative_int(data.get("schema_version"), "schema_version")
    if schema_version != SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION:
        raise DeliveryHandoffError(
            f"unsupported delivery handoff schema_version {schema_version}; "
            f"expected {SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION}"
        )

    unit = _required_object(data.get("unit"), "unit")
    if set(unit) != {"title", "component", "depends_on", "scope_paths"}:
        raise DeliveryHandoffError("delivery handoff unit metadata is malformed")
    validation = _required_object(data.get("validation"), "validation")
    if set(validation) != {
        "build_status",
        "test_status",
        "check_status",
        "records_count",
        "failures_count",
        "review_records_count",
        "security_review_records_count",
    }:
        raise DeliveryHandoffError("delivery handoff validation metadata is malformed")
    tests = _required_object(data.get("tests"), "tests")
    if set(tests) != {"files_written", "test_writer_runs", "testability_gaps_count"}:
        raise DeliveryHandoffError("delivery handoff test metadata is malformed")

    handoff = DeliveryUnitHandoff(
        schema_version=schema_version,
        plan_id=_required_identifier(data.get("plan_id"), "plan_id"),
        unit_id=_required_unit_identifier(data.get("unit_id"), "unit_id"),
        child_task_id=_required_identifier(data.get("child_task_id"), "child_task_id"),
        result_branch=_optional_text(data.get("result_branch"), "result_branch"),
        result_commit=_optional_text(data.get("result_commit"), "result_commit"),
        files_changed=_safe_paths(_required_list(data.get("files_changed"), "files_changed")),
        completed_at=_required_timestamp(data.get("completed_at"), "completed_at"),
        unit_title=_optional_label(unit.get("title"), "unit.title"),
        component=_optional_label(unit.get("component"), "unit.component"),
        depends_on=[
            _required_unit_identifier(value, "unit.depends_on")
            for value in _required_list(unit.get("depends_on"), "unit.depends_on")
        ],
        scope_paths=_safe_paths(_required_list(unit.get("scope_paths"), "unit.scope_paths")),
        build_status=_optional_status(validation.get("build_status"), "validation.build_status"),
        test_status=_optional_status(validation.get("test_status"), "validation.test_status"),
        check_status=_optional_status(validation.get("check_status"), "validation.check_status"),
        validation_records_count=_required_nonnegative_int(validation.get("records_count"), "validation.records_count"),
        validation_failures_count=_required_nonnegative_int(
            validation.get("failures_count"), "validation.failures_count"
        ),
        review_records_count=_required_nonnegative_int(
            validation.get("review_records_count"), "validation.review_records_count"
        ),
        security_review_records_count=_required_nonnegative_int(
            validation.get("security_review_records_count"), "validation.security_review_records_count"
        ),
        test_files_written=_safe_paths(_required_list(tests.get("files_written"), "tests.files_written")),
        test_writer_runs=_required_nonnegative_int(tests.get("test_writer_runs"), "tests.test_writer_runs"),
        testability_gaps_count=_required_nonnegative_int(
            tests.get("testability_gaps_count"), "tests.testability_gaps_count"
        ),
        fingerprint=_required_fingerprint(data.get("fingerprint")),
    )
    expected = _fingerprint(handoff.payload_dict())
    if handoff.fingerprint != expected:
        raise DeliveryHandoffError("delivery handoff fingerprint does not match its content")
    return handoff


def read_delivery_unit_handoff(path: Path, *, project_root: Path | None = None) -> DeliveryUnitHandoff:
    if path.is_symlink():
        raise DeliveryHandoffError("delivery handoff path must not be a symlink")
    if project_root is not None:
        try:
            path.resolve().relative_to(project_root.resolve())
        except (OSError, ValueError) as exc:
            raise DeliveryHandoffError("delivery handoff path escapes the project root") from exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryHandoffError("delivery handoff could not be read as JSON") from exc
    return parse_delivery_unit_handoff(data)


def write_delivery_unit_handoff(path: Path, handoff: DeliveryUnitHandoff) -> None:
    validated = parse_delivery_unit_handoff(handoff.to_dict())
    if path.is_symlink():
        raise DeliveryHandoffError("delivery handoff path must not be a symlink")
    if path.exists():
        existing = read_delivery_unit_handoff(path)
        if existing != validated:
            raise DeliveryHandoffError("existing delivery handoff conflicts with completed child evidence")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _with_fingerprint(handoff: DeliveryUnitHandoff) -> DeliveryUnitHandoff:
    return replace(handoff, fingerprint=_fingerprint(handoff.payload_dict()))


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeliveryHandoffError(f"{field_name} must be an object")
    return value


def _required_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeliveryHandoffError(f"{field_name} must be a list")
    return value


def _required_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise DeliveryHandoffError(f"{field_name} must be a safe non-empty identifier")
    return value


def _required_unit_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DeliveryHandoffError(f"{field_name} must be a bounded non-empty string")
    identifier = value.strip()
    if not identifier or len(identifier) > _MAX_METADATA_TEXT:
        raise DeliveryHandoffError(f"{field_name} must be a bounded non-empty string")
    if any(ord(char) < 32 for char in identifier):
        raise DeliveryHandoffError(f"{field_name} contains unsupported control characters")
    return identifier


def _handoff_filename(unit_id: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", unit_id).strip(".-_")
    prefix = prefix[:_HANDOFF_FILENAME_PREFIX_LENGTH] or "unit"
    digest = hashlib.sha256(unit_id.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}.json"


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_METADATA_TEXT:
        raise DeliveryHandoffError(f"{field_name} must be a bounded non-empty string or null")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise DeliveryHandoffError(f"{field_name} contains unsupported control characters")
    return value.strip()


def _optional_label(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeliveryHandoffError(f"{field_name} must be a string or null")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _MAX_METADATA_TEXT:
        raise DeliveryHandoffError(f"{field_name} must be a bounded non-empty string or null")
    return normalized


def _project_optional_label(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeliveryHandoffError(f"{field_name} must be a string or null")
    normalized = " ".join(value.split())
    if not normalized:
        raise DeliveryHandoffError(f"{field_name} must be a non-empty string or null")
    if len(normalized) <= _MAX_METADATA_TEXT:
        return normalized
    digest_suffix = f" [sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}]"
    return normalized[: _MAX_METADATA_TEXT - len(digest_suffix)].rstrip() + digest_suffix


def _required_timestamp(value: Any, field_name: str) -> str:
    text = _optional_text(value, field_name)
    if text is None:
        raise DeliveryHandoffError(f"{field_name} must be present")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryHandoffError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return text


def _optional_status(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _VALIDATION_STATUSES:
        raise DeliveryHandoffError(f"{field_name} contains an unsupported validation status")
    return value


def _required_nonnegative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DeliveryHandoffError(f"{field_name} must be a non-negative integer")
    return value


def _required_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise DeliveryHandoffError("fingerprint must be a lowercase SHA-256 digest")
    return value


def _safe_paths(values: list[Any]) -> list[str]:
    paths: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise DeliveryHandoffError("handoff paths must be non-empty strings")
        raw_path = value.strip()
        posix_path = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in posix_path.parts
            or ".." in windows_path.parts
        ):
            raise DeliveryHandoffError("handoff paths must stay project-relative")
        paths.append(PurePosixPath(raw_path.replace("\\", "/")).as_posix())
    return list(dict.fromkeys(paths))


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
