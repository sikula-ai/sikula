"""Warning-only delivery asset target audit helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def audit_delivery_asset_targets(
    asset_records: Iterable[dict[str, Any]],
    *,
    files_changed: Iterable[str],
    project_root: Path,
    phase: str,
) -> list[dict[str, Any]]:
    """Return audit records for delivery asset requested targets.

    This is intentionally conservative: it only checks explicit requested target
    paths and never infers platform-specific replacements or transformations.
    """

    changed_paths: set[str] = set()
    for path in files_changed:
        normalized = _normalize_project_path(path)
        if normalized:
            changed_paths.add(normalized)
    records: list[dict[str, Any]] = []
    for asset in asset_records:
        if not isinstance(asset, dict) or str(asset.get("kind") or "").strip() != "delivery":
            continue
        requested_target = str(asset.get("requested_target") or "").strip()
        if not requested_target:
            records.append(_target_record(asset, phase=phase, status="not_specified"))
            continue

        target_path = _project_relative_target(requested_target, project_root=project_root)
        if target_path is None:
            records.append(
                _target_record(
                    asset,
                    phase=phase,
                    status="outside_project",
                    requested_target=requested_target,
                )
            )
            continue

        target_exists = _target_exists(project_root / target_path)
        if target_path in changed_paths and target_exists:
            records.append(
                _target_record(
                    asset,
                    phase=phase,
                    status="matched",
                    requested_target=target_path,
                    matched_path=target_path,
                )
            )
            continue

        if target_exists:
            records.append(
                _target_record(
                    asset,
                    phase=phase,
                    status="present_unchanged",
                    requested_target=target_path,
                    matched_path=target_path,
                )
            )
            continue

        records.append(
            _target_record(
                asset,
                phase=phase,
                status="missing",
                requested_target=target_path,
            )
        )
    return records


def _target_record(
    asset: dict[str, Any],
    *,
    phase: str,
    status: str,
    requested_target: str = "",
    matched_path: str = "",
) -> dict[str, Any]:
    return {
        "path": str(asset.get("path") or "").strip(),
        "project_path": str(asset.get("project_path") or "").strip(),
        "kind": "delivery",
        "phase": str(phase or "").strip(),
        "status": status,
        "requested_target": requested_target,
        "matched_path": matched_path,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _project_relative_target(value: str, *, project_root: Path) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    target = Path(raw)
    candidate = target if target.is_absolute() else project_root / target
    try:
        relative = candidate.resolve(strict=False).relative_to(project_root.resolve(strict=False))
    except (OSError, ValueError):
        return None
    normalized = _normalize_project_path(relative.as_posix())
    return normalized or None


def _normalize_project_path(value: str | Path) -> str:
    text = Path(str(value)).as_posix().strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _target_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
