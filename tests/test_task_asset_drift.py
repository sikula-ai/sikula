from __future__ import annotations

from pathlib import Path

from core import task_asset_drift
from core.task_asset_drift import detect_snapshot_asset_drift


def test_snapshot_asset_drift_records_unavailable_when_metadata_collection_fails(
    monkeypatch,
    tmp_path: Path,
):
    def raise_metadata_error(*_args, **_kwargs):
        raise OSError("asset disappeared during drift check")

    monkeypatch.setattr(task_asset_drift, "current_asset_reference_metadata", raise_metadata_error)

    findings = detect_snapshot_asset_drift(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "sha256": "sha256:" + "a" * 64,
            }
        ],
        project_root=tmp_path,
        phase="resume",
    )

    assert findings == [
        {
            "path": ".sikula/task-assets/icon.svg",
            "project_path": ".sikula/task-assets/icon.svg",
            "kind": "delivery",
            "phase": "resume",
            "status": "unavailable",
            "expected_source": "task_state_snapshot",
            "expected_sha256": "sha256:" + "a" * 64,
            "current_sha256": "",
            "current_status": "unavailable",
            "git_status": "",
            "size_bytes": None,
            "mime_type": "",
            "observed_at": findings[0]["observed_at"],
        }
    ]
