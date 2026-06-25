from __future__ import annotations

from pathlib import Path

from core.task_asset_targets import audit_delivery_asset_targets


def test_delivery_asset_target_audit_matches_changed_requested_target(tmp_path: Path):
    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "requested_target": "app/assets/icon.svg",
            }
        ],
        files_changed=["app/assets/icon.svg"],
        project_root=tmp_path,
        phase="completion",
    )

    assert records == [
        {
            "path": ".sikula/task-assets/icon.svg",
            "project_path": ".sikula/task-assets/icon.svg",
            "kind": "delivery",
            "phase": "completion",
            "status": "matched",
            "requested_target": "app/assets/icon.svg",
            "matched_path": "app/assets/icon.svg",
            "observed_at": records[0]["observed_at"],
        }
    ]


def test_delivery_asset_target_audit_records_present_unchanged_target(tmp_path: Path):
    target = tmp_path / "app" / "assets" / "icon.svg"
    target.parent.mkdir(parents=True)
    target.write_text("<svg />", encoding="utf-8")

    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "requested_target": "app/assets/icon.svg",
            }
        ],
        files_changed=[],
        project_root=tmp_path,
        phase="completion",
    )

    assert records[0]["status"] == "present_unchanged"
    assert records[0]["matched_path"] == "app/assets/icon.svg"


def test_delivery_asset_target_audit_records_missing_requested_target(tmp_path: Path):
    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "requested_target": "app/assets/icon.svg",
            }
        ],
        files_changed=[],
        project_root=tmp_path,
        phase="completion",
    )

    assert records[0]["status"] == "missing"
    assert records[0]["requested_target"] == "app/assets/icon.svg"


def test_delivery_asset_target_audit_records_not_specified_without_warning_target(tmp_path: Path):
    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
            }
        ],
        files_changed=["app/assets/icon.svg"],
        project_root=tmp_path,
        phase="completion",
    )

    assert records[0]["status"] == "not_specified"
    assert records[0]["requested_target"] == ""
    assert records[0]["matched_path"] == ""


def test_delivery_asset_target_audit_ignores_reference_assets(tmp_path: Path):
    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/mockup.png",
                "project_path": ".sikula/task-assets/mockup.png",
                "kind": "reference",
                "requested_target": "app/assets/mockup.png",
            }
        ],
        files_changed=["app/assets/mockup.png"],
        project_root=tmp_path,
        phase="completion",
    )

    assert records == []


def test_delivery_asset_target_audit_rejects_outside_project_targets(tmp_path: Path):
    outside = tmp_path.parent / "outside.svg"

    records = audit_delivery_asset_targets(
        [
            {
                "path": ".sikula/task-assets/icon.svg",
                "project_path": ".sikula/task-assets/icon.svg",
                "kind": "delivery",
                "requested_target": str(outside),
            }
        ],
        files_changed=[str(outside)],
        project_root=tmp_path,
        phase="completion",
    )

    assert records[0]["status"] == "outside_project"
    assert records[0]["requested_target"] == str(outside)
