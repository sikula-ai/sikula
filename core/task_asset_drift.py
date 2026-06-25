"""Warning-only task asset drift detection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.task_assets import current_asset_reference_metadata


def detect_declared_asset_hash_drift(asset_records: Iterable[dict[str, Any]], *, phase: str) -> list[dict[str, Any]]:
    """Compare prepared-contract declared hashes with current preflight metadata."""

    findings: list[dict[str, Any]] = []
    for record in asset_records:
        if not isinstance(record, dict):
            continue
        expected_sha256 = _normalized_sha256(record.get("declared_sha256"))
        if not expected_sha256:
            continue
        finding = _drift_record(
            record,
            current_record=record,
            expected_sha256=expected_sha256,
            phase=phase,
            expected_source="asset_manifest",
        )
        if finding:
            findings.append(finding)
    return findings


def detect_snapshot_asset_drift(
    asset_records: Iterable[dict[str, Any]],
    *,
    project_root: Path,
    phase: str,
) -> list[dict[str, Any]]:
    """Compare current filesystem metadata with a saved task-state asset snapshot."""

    findings: list[dict[str, Any]] = []
    for record in asset_records:
        if not isinstance(record, dict):
            continue
        expected_sha256 = _normalized_sha256(record.get("sha256"))
        if not expected_sha256:
            continue
        current_record = current_asset_reference_metadata(record, project_root=project_root)
        finding = _drift_record(
            record,
            current_record=current_record,
            expected_sha256=expected_sha256,
            phase=phase,
            expected_source="task_state_snapshot",
        )
        if finding:
            findings.append(finding)
    return findings


def _drift_record(
    original_record: dict[str, Any],
    *,
    current_record: dict[str, Any] | None,
    expected_sha256: str,
    phase: str,
    expected_source: str,
) -> dict[str, Any] | None:
    current_record = current_record or {}
    current_status = str(current_record.get("status") or "unavailable").strip() or "unavailable"
    current_sha256 = _normalized_sha256(current_record.get("sha256"))

    if current_status != "available":
        status = current_status
    elif not current_sha256:
        status = "unavailable"
    elif current_sha256 and current_sha256 != expected_sha256:
        status = "changed"
    else:
        return None

    return {
        "path": _clean_string(original_record.get("path")),
        "project_path": _clean_string(original_record.get("project_path") or current_record.get("project_path")),
        "kind": _clean_string(original_record.get("kind")),
        "phase": _clean_string(phase),
        "status": status,
        "expected_source": expected_source,
        "expected_sha256": expected_sha256,
        "current_sha256": current_sha256,
        "current_status": current_status,
        "git_status": _clean_string(current_record.get("git_status")),
        "size_bytes": current_record.get("size_bytes"),
        "mime_type": _clean_string(current_record.get("mime_type")),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalized_sha256(value: Any) -> str:
    text = _clean_string(value).lower()
    return text if text.startswith("sha256:") else ""


def _clean_string(value: Any) -> str:
    return str(value or "").strip()
