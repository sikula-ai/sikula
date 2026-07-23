from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.delivery_handoff import (
    DeliveryHandoffError,
    SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION,
    build_delivery_unit_handoff,
    delivery_unit_handoff_path,
    delivery_unit_handoff_matches_unit,
    parse_delivery_unit_handoff,
    read_delivery_unit_handoff,
    write_delivery_unit_handoff,
)
from core.state import TaskState


def _selected_unit() -> SimpleNamespace:
    return SimpleNamespace(
        id="foundation",
        title="Add delivery foundation",
        component="delivery",
        depends_on=[],
        scope_paths=["core/", "tests/"],
    )


def _child_state() -> TaskState:
    state = TaskState(
        task_id="child-123",
        task_description="Private child task body",
        delivery_handoff_schema_version=SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION,
    )
    state.worktree_branch = "sikula/foundation-child"
    state.result_commit = "a" * 40
    state.build_status = "success"
    state.test_status = "success"
    state.check_status = "success"
    state.validation_cycle_records = [{"status": "success"}, {"status": "failed"}]
    state.files_changed = ["core/delivery.py", "tests/test_delivery.py"]
    state.review_cycle_records = [{"status": "approved"}]
    state.security_review_cycle_records = [{"status": "approved"}]
    state.test_files_written = ["tests/test_delivery.py"]
    state.test_write_records = [{"scope": "task"}]
    state.testability_gaps = [{"message": "Private free-form gap details"}]
    return state


def test_delivery_handoff_roundtrip_projects_allowlisted_metadata(tmp_path: Path) -> None:
    child_state = _child_state()
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id=child_state.task_id,
        child_state=child_state,
    )
    path = delivery_unit_handoff_path(tmp_path, "demo-plan", "foundation")

    write_delivery_unit_handoff(path, handoff)
    write_delivery_unit_handoff(path, handoff)
    loaded = read_delivery_unit_handoff(path)

    assert loaded == handoff
    assert loaded.validation_records_count == 2
    assert loaded.validation_failures_count == 1
    assert loaded.testability_gaps_count == 1
    assert loaded.files_changed == ["core/delivery.py", "tests/test_delivery.py"]
    assert loaded.test_files_written == ["tests/test_delivery.py"]
    serialized = path.read_text(encoding="utf-8")
    assert "Private child task body" not in serialized
    assert "Private free-form gap details" not in serialized


def test_delivery_handoff_paths_are_bounded_and_case_collision_safe(tmp_path: Path) -> None:
    lower_path = delivery_unit_handoff_path(tmp_path, "demo-plan", "api")
    upper_path = delivery_unit_handoff_path(tmp_path, "demo-plan", "API")
    spaced_path = delivery_unit_handoff_path(tmp_path, "demo-plan", "unit one")
    long_path = delivery_unit_handoff_path(tmp_path, "demo-plan", "x" * 1000)

    assert lower_path.name != upper_path.name
    assert spaced_path.name.startswith("unit-one-")
    assert len(long_path.name) < 128
    assert all(path.parent == lower_path.parent for path in (upper_path, spaced_path, long_path))


def test_build_delivery_handoff_accepts_legacy_unit_id_with_spaces() -> None:
    selected_unit = _selected_unit()
    selected_unit.id = "unit one"

    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=selected_unit,
        child_task_id="child-123",
        child_state=_child_state(),
    )

    assert handoff.unit_id == "unit one"


def test_build_delivery_handoff_projects_long_labels_with_digest() -> None:
    selected_unit = _selected_unit()
    selected_unit.title = "Title " + "x" * 1100
    selected_unit.component = "component-" + "y" * 1100

    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=selected_unit,
        child_task_id="child-123",
        child_state=_child_state(),
    )

    assert handoff.unit_title is not None
    assert handoff.component is not None
    assert len(handoff.unit_title) == 1000
    assert len(handoff.component) == 1000
    assert " [sha256:" in handoff.unit_title
    assert " [sha256:" in handoff.component
    assert parse_delivery_unit_handoff(handoff.to_dict()) == handoff


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("title", "Changed title"),
        ("component", "changed-component"),
        ("depends_on", ["changed-dependency"]),
        ("scope_paths", ["changed/scope"]),
    ],
)
def test_delivery_handoff_matches_current_unit_metadata(field_name: str, changed_value: Any) -> None:
    selected_unit = _selected_unit()
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=selected_unit,
        child_task_id="child-123",
        child_state=_child_state(),
    )

    assert delivery_unit_handoff_matches_unit(handoff, selected_unit) is True

    setattr(selected_unit, field_name, changed_value)

    assert delivery_unit_handoff_matches_unit(handoff, selected_unit) is False


@pytest.mark.parametrize("invalid_title", [42, " \n "])
def test_delivery_handoff_rejects_invalid_current_unit_metadata(invalid_title: Any) -> None:
    selected_unit = _selected_unit()
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=selected_unit,
        child_task_id="child-123",
        child_state=_child_state(),
    )
    selected_unit.title = invalid_title

    assert delivery_unit_handoff_matches_unit(handoff, selected_unit) is False


def test_parse_delivery_handoff_rejects_tampered_payload() -> None:
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    payload = handoff.to_dict()
    payload["result_commit"] = "b" * 40

    with pytest.raises(DeliveryHandoffError, match="fingerprint"):
        parse_delivery_unit_handoff(payload)


def test_build_delivery_handoff_rejects_boolean_schema_version() -> None:
    child_state = _child_state()
    child_state.delivery_handoff_schema_version = True

    with pytest.raises(DeliveryHandoffError, match="does not opt in"):
        build_delivery_unit_handoff(
            plan_id="demo-plan",
            selected_unit=_selected_unit(),
            child_task_id=child_state.task_id,
            child_state=child_state,
        )


def test_write_delivery_handoff_rejects_conflicting_existing_evidence(tmp_path: Path) -> None:
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    path = delivery_unit_handoff_path(tmp_path, "demo-plan", "foundation")
    write_delivery_unit_handoff(path, handoff)

    conflicting_state = _child_state()
    conflicting_state.result_commit = "b" * 40
    conflicting = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=conflicting_state,
    )

    with pytest.raises(DeliveryHandoffError, match="conflicts"):
        write_delivery_unit_handoff(path, conflicting)


def test_build_delivery_handoff_rejects_non_project_test_path() -> None:
    child_state = _child_state()
    child_state.test_files_written = ["../private-test.py"]

    with pytest.raises(DeliveryHandoffError, match="project-relative"):
        build_delivery_unit_handoff(
            plan_id="demo-plan",
            selected_unit=_selected_unit(),
            child_task_id=child_state.task_id,
            child_state=child_state,
        )


def _invalid_payload_case(payload: dict[str, Any], case: str) -> Any:
    if case == "not_object":
        return []
    if case == "missing_field":
        payload.pop("tests")
    elif case == "unsupported_schema":
        payload["schema_version"] = 2
    elif case == "unit_not_object":
        payload["unit"] = []
    elif case == "unit_fields":
        payload["unit"]["unexpected"] = True
    elif case == "validation_not_object":
        payload["validation"] = []
    elif case == "validation_fields":
        payload["validation"]["unexpected"] = True
    elif case == "tests_not_object":
        payload["tests"] = []
    elif case == "tests_fields":
        payload["tests"]["unexpected"] = True
    elif case == "identifier":
        payload["plan_id"] = "../bad"
    elif case == "unit_id_too_long":
        payload["unit_id"] = "x" * 1001
    elif case == "unit_id_type":
        payload["unit_id"] = 42
    elif case == "unit_id_control":
        payload["unit_id"] = "unit\x01one"
    elif case == "empty_text":
        payload["result_branch"] = ""
    elif case == "control_text":
        payload["result_branch"] = "branch\nother"
    elif case == "label_type":
        payload["unit"]["title"] = 42
    elif case == "empty_label":
        payload["unit"]["title"] = " \n "
    elif case == "missing_timestamp":
        payload["completed_at"] = None
    elif case == "invalid_timestamp":
        payload["completed_at"] = "not-a-timestamp"
    elif case == "status":
        payload["validation"]["build_status"] = "unknown"
    elif case == "count":
        payload["validation"]["records_count"] = True
    elif case == "fingerprint":
        payload["fingerprint"] = "invalid"
    elif case == "list":
        payload["files_changed"] = "core/file.py"
    elif case == "empty_path":
        payload["files_changed"] = [""]
    elif case == "windows_path":
        payload["files_changed"] = ["C:\\private\\file.py"]
    else:
        raise AssertionError(f"unknown case: {case}")
    return payload


@pytest.mark.parametrize(
    "case",
    [
        "not_object",
        "missing_field",
        "unsupported_schema",
        "unit_not_object",
        "unit_fields",
        "validation_not_object",
        "validation_fields",
        "tests_not_object",
        "tests_fields",
        "identifier",
        "unit_id_too_long",
        "unit_id_type",
        "unit_id_control",
        "empty_text",
        "control_text",
        "label_type",
        "empty_label",
        "missing_timestamp",
        "invalid_timestamp",
        "status",
        "count",
        "fingerprint",
        "list",
        "empty_path",
        "windows_path",
    ],
)
def test_parse_delivery_handoff_rejects_malformed_fields(case: str) -> None:
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    payload = _invalid_payload_case(handoff.to_dict(), case)

    with pytest.raises(DeliveryHandoffError):
        parse_delivery_unit_handoff(payload)


def test_read_delivery_handoff_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "handoff.json"
    path.write_text("{invalid", encoding="utf-8")

    with pytest.raises(DeliveryHandoffError, match="JSON"):
        read_delivery_unit_handoff(path)


def test_read_delivery_handoff_rejects_symlink(tmp_path: Path) -> None:
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    target = tmp_path / "target.json"
    write_delivery_unit_handoff(target, handoff)
    path = tmp_path / "handoff.json"
    path.symlink_to(target)

    with pytest.raises(DeliveryHandoffError, match="symlink"):
        read_delivery_unit_handoff(path, project_root=tmp_path)


def test_read_delivery_handoff_rejects_path_outside_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    path = tmp_path / "outside.json"
    write_delivery_unit_handoff(path, handoff)

    with pytest.raises(DeliveryHandoffError, match="escapes"):
        read_delivery_unit_handoff(path, project_root=root)


def test_write_delivery_handoff_rejects_symlink(tmp_path: Path) -> None:
    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    path = tmp_path / "handoff.json"
    path.symlink_to(target)

    with pytest.raises(DeliveryHandoffError, match="symlink"):
        write_delivery_unit_handoff(path, handoff)


def test_write_delivery_handoff_removes_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.delivery_handoff as delivery_handoff

    handoff = build_delivery_unit_handoff(
        plan_id="demo-plan",
        selected_unit=_selected_unit(),
        child_task_id="child-123",
        child_state=_child_state(),
    )
    path = tmp_path / "handoff.json"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(delivery_handoff.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_delivery_unit_handoff(path, handoff)

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_delivery_handoff_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".sikula").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeliveryHandoffError, match="escapes"):
        delivery_unit_handoff_path(root, "demo-plan", "foundation")
