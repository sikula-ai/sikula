from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_progress import (
    DeliveryProgress,
    DeliveryProgressEvent,
    DeliveryProgressLockError,
    DeliveryUnitProgress,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    delivery_events_path,
    delivery_lock_path,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_progress_event,
    make_delivery_unit_progress,
    render_delivery_status,
    select_next_delivery_unit,
    upsert_delivery_unit_progress,
    write_delivery_progress,
)
from sikula import main
from sikula_cli.delivery import cmd_delivery_status


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_unit(root: Path, name: str) -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\nUnit body should stay private.\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path, data: dict | None = None) -> Path:
    unit_1 = _write_unit(root, "01-foundation.md")
    unit_2 = _write_unit(root, "02-feature.md")
    plan = data or {
        "schema_version": 1,
        "plan_id": "delivery-status-demo",
        "title": "Delivery status demo",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/status-demo",
        "streams": [{"id": "app", "label": "App"}],
        "units": [
            {
                "id": "01-foundation",
                "title": "Add foundation",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_1,
                "depends_on": [],
            },
            {
                "id": "02-feature",
                "title": "Add feature",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_2,
                "depends_on": ["01-foundation"],
            },
        ],
    }
    path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    return path


def _write_progress(root: Path, plan_id: str, data: dict) -> Path:
    path = delivery_progress_path(root, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _error_codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def _warning_codes(result) -> set[str]:
    return {issue.code for issue in result.warnings}


def test_delivery_status_defaults_to_pending_when_progress_is_missing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "pending"
    assert result.progress_exists is False
    assert result.units[0].status == "pending"
    assert result.units[0].eligible is True
    assert result.units[1].status == "pending"
    assert result.units[1].blocked_by == ["01-foundation"]
    assert result.next_action == "prepare or run an eligible delivery unit with the existing task workflow"
    rendered = render_delivery_status(result)
    assert "Progress:" in rendered
    assert "not created yet" in rendered
    assert "01-foundation: pending (eligible)" in rendered


def test_delivery_status_reads_progress_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {
                    "unit_id": "01-foundation",
                    "status": "done",
                    "child_task_id": "task-1",
                    "branch": "sikula/unit-1",
                    "commit": "abc123",
                    "completed_at": "2026-06-30T12:00:00Z",
                },
                {
                    "unit_id": "02-feature",
                    "status": "running",
                    "child_task_id": "task-2",
                    "started_at": "2026-06-30T12:05:00Z",
                },
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "running"
    assert result.progress_exists is True
    assert result.units[0].status == "done"
    assert result.units[0].child_task_id == "task-1"
    assert result.units[0].branch == "sikula/unit-1"
    assert result.units[0].commit == "abc123"
    assert result.units[1].status == "running"
    assert result.next_action == "wait for the running delivery unit"
    rendered = render_delivery_status(result)
    assert "task=task-1" in rendered
    assert "branch=sikula/unit-1" in rendered
    assert "commit=abc123" in rendered


def test_delivery_status_reports_failed_when_another_unit_is_running(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "failed", "failure_code": "child_failed"},
                {"unit_id": "02-feature", "status": "running", "child_task_id": "task-2"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "failed"
    assert result.next_action == "inspect the failed delivery unit"


def test_delivery_status_reports_waiting_and_failed_states(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "waiting", "waiting_reason": "needs_human"},
                {"unit_id": "02-feature", "status": "failed", "failure_code": "child_failed"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "failed"
    assert result.units[0].waiting_reason == "needs_human"
    assert result.units[1].failure_code == "child_failed"
    assert result.next_action == "inspect the failed delivery unit"


def test_delivery_status_reports_waiting_when_unit_needs_human_input(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "waiting", "waiting_reason": "needs_human"}],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "waiting"
    assert result.next_action == "answer the blocking delivery question or setup requirement"


def test_delivery_status_reports_canceled_progress(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "canceled"}],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "canceled"
    assert result.next_action == "inspect canceled delivery progress"


def test_delivery_status_reports_done_when_all_units_are_done(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "done"},
                {"unit_id": "02-feature", "status": "done"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "done"
    assert result.next_action == "review final delivery branch"


def test_delivery_status_reports_invalid_plan_without_progress_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "../bad",
            "title": "Bad plan id",
            "final_branch": "sikula/delivery/bad",
            "units": [],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert result.status == "invalid"
    assert result.progress_path is None
    assert "plan_id.invalid" in _error_codes(result)


def test_delivery_status_reports_invalid_progress_json(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    progress_path = delivery_progress_path(tmp_path, "delivery-status-demo")
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text("{not-json", encoding="utf-8")

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert result.status == "invalid"
    assert "progress.parse_failed" in _error_codes(result)


def test_delivery_status_reports_non_utf8_progress_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    progress_path = delivery_progress_path(tmp_path, "delivery-status-demo")
    progress_path.parent.mkdir(parents=True)
    progress_path.write_bytes(b"\xff\xfe\x00")

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert result.status == "invalid"
    assert "progress.read_failed" in _error_codes(result)


def test_delivery_status_reports_non_object_progress_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    progress_path = delivery_progress_path(tmp_path, "delivery-status-demo")
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text("[1, 2, 3]", encoding="utf-8")

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.invalid_type" in _error_codes(result)


def test_delivery_status_reports_missing_progress_schema_version(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, "delivery-status-demo", {"plan_id": "delivery-status-demo", "units": []})

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.schema_version_missing" in _error_codes(result)


def test_delivery_status_reports_progress_schema_and_plan_id_errors(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, "delivery-status-demo", {"schema_version": 2, "plan_id": "other", "units": []})

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert result.status == "invalid"
    assert "progress.schema_version_unsupported" in _error_codes(result)


def test_delivery_status_reports_progress_plan_id_mismatch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, "delivery-status-demo", {"schema_version": 1, "plan_id": "other", "units": []})

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.plan_id_mismatch" in _error_codes(result)


def test_delivery_status_reports_invalid_progress_units(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                "bad unit",
                {"unit_id": "01-foundation", "status": "done"},
                {"unit_id": "01-foundation", "status": "done"},
                {"unit_id": "02-feature", "status": "unknown"},
                {"unit_id": "", "status": "pending", "branch": 123},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.unit_invalid_type" in _error_codes(result)
    assert "progress.unit_duplicate" in _error_codes(result)
    assert "progress.status_unknown" in _error_codes(result)
    assert "units[4].unit_id.missing" in _error_codes(result)
    assert "units[4].branch.invalid_type" in _error_codes(result)


def test_delivery_status_reports_invalid_progress_units_container(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {"schema_version": 1, "plan_id": "delivery-status-demo", "units": {"unit_id": "01-foundation"}},
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.units_invalid_type" in _error_codes(result)


def test_delivery_status_warns_about_unknown_progress_unit(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "old-unit", "status": "done"}],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert "progress.unit_unknown" in _warning_codes(result)
    assert "Warnings:" in render_delivery_status(result)


def test_delivery_progress_to_dict_uses_allowlisted_fields() -> None:
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="plan",
        units=[
            DeliveryUnitProgress(
                unit_id="unit-1",
                status="done",
                child_task_id="task-1",
                branch="sikula/unit-1",
                commit="abc123",
                waiting_reason="needs_human",
                failure_code="child_failed",
                started_at="2026-06-30T12:00:00Z",
                completed_at="2026-06-30T12:10:00Z",
                updated_at="2026-06-30T12:11:00Z",
            )
        ],
    )

    assert progress.to_dict() == {
        "schema_version": 1,
        "plan_id": "plan",
        "units": [
            {
                "unit_id": "unit-1",
                "status": "done",
                "child_task_id": "task-1",
                "branch": "sikula/unit-1",
                "commit": "abc123",
                "waiting_reason": "needs_human",
                "failure_code": "child_failed",
                "started_at": "2026-06-30T12:00:00Z",
                "completed_at": "2026-06-30T12:10:00Z",
                "updated_at": "2026-06-30T12:11:00Z",
            }
        ],
    }


def test_write_delivery_progress_writes_atomic_allowlisted_snapshot(tmp_path: Path) -> None:
    progress_path = delivery_progress_path(tmp_path, "plan")
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="plan",
        units=[
            make_delivery_unit_progress(
                "unit-1",
                "done",
                child_task_id="task-1",
                branch="sikula/unit-1",
                commit="abc123",
                timestamp="2026-07-04T12:00:00+00:00",
            )
        ],
    )

    write_delivery_progress(progress_path, progress)

    payload_text = progress_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    assert payload == {
        "schema_version": 1,
        "plan_id": "plan",
        "units": [
            {
                "unit_id": "unit-1",
                "status": "done",
                "child_task_id": "task-1",
                "branch": "sikula/unit-1",
                "commit": "abc123",
                "completed_at": "2026-07-04T12:00:00+00:00",
                "updated_at": "2026-07-04T12:00:00+00:00",
            }
        ],
    }
    assert not list(progress_path.parent.glob("*.tmp"))


def test_append_delivery_progress_event_writes_privacy_safe_jsonl(tmp_path: Path) -> None:
    event_path = delivery_events_path(tmp_path, "plan")
    unit = make_delivery_unit_progress(
        "unit-1",
        "running",
        child_task_id="task-1",
        timestamp="2026-07-04T12:00:00+00:00",
    )
    event = make_delivery_progress_event(
        "plan",
        "unit.running",
        unit=unit,
        timestamp="2026-07-04T12:00:01+00:00",
    )

    append_delivery_progress_event(event_path, event)

    payload_text = event_path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    assert payload == {
        "schema_version": 1,
        "plan_id": "plan",
        "event_type": "unit.running",
        "timestamp": "2026-07-04T12:00:01+00:00",
        "unit_id": "unit-1",
        "status": "running",
        "child_task_id": "task-1",
    }
    assert "raw_prompt" not in payload_text
    assert "raw_llm_output" not in payload_text
    assert "provider_output" not in payload_text


def test_delivery_progress_lock_blocks_concurrent_mutation(tmp_path: Path) -> None:
    lock = acquire_delivery_progress_lock(tmp_path, "plan", owner="test")
    try:
        assert delivery_lock_path(tmp_path, "plan").exists()
        with pytest.raises(DeliveryProgressLockError):
            acquire_delivery_progress_lock(tmp_path, "plan", owner="other")
    finally:
        lock.release()

    assert not delivery_lock_path(tmp_path, "plan").exists()
    with acquire_delivery_progress_lock(tmp_path, "plan", owner="test"):
        assert delivery_lock_path(tmp_path, "plan").exists()
    assert not delivery_lock_path(tmp_path, "plan").exists()


@pytest.mark.parametrize("plan_id", ["../bad", "bad/path", ".hidden", "bad\x00id", ""])
def test_delivery_progress_paths_reject_unsafe_plan_ids(tmp_path: Path, plan_id: str) -> None:
    with pytest.raises(ValueError):
        delivery_progress_path(tmp_path, plan_id)
    with pytest.raises(ValueError):
        delivery_events_path(tmp_path, plan_id)
    with pytest.raises(ValueError):
        delivery_lock_path(tmp_path, plan_id)


def test_delivery_progress_mutation_rejects_unsafe_metadata(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.json"
    with pytest.raises(ValueError):
        write_delivery_progress(progress_path, DeliveryProgress(schema_version=1, plan_id="../bad", units=[]))

    event_path = tmp_path / "events.jsonl"
    with pytest.raises(ValueError):
        append_delivery_progress_event(
            event_path,
            make_delivery_progress_event("plan", "Raw prompt: do not log"),
        )


def test_upsert_delivery_unit_progress_replaces_existing_unit() -> None:
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="plan",
        units=[DeliveryUnitProgress(unit_id="unit-1", status="pending")],
    )
    running = make_delivery_unit_progress(
        "unit-1",
        "running",
        child_task_id="task-1",
        timestamp="2026-07-04T12:00:00+00:00",
    )

    updated = upsert_delivery_unit_progress(progress, running)
    updated = upsert_delivery_unit_progress(
        updated,
        make_delivery_unit_progress("unit-2", "waiting", waiting_reason="needs_human"),
    )

    assert [unit.unit_id for unit in updated.units] == ["unit-1", "unit-2"]
    assert updated.units[0].status == "running"
    assert updated.units[0].child_task_id == "task-1"
    assert updated.units[1].status == "waiting"
    assert updated.units[1].waiting_reason == "needs_human"


def test_terminal_delivery_progress_preserves_started_at_from_running_unit() -> None:
    started_at = "2026-07-04T12:00:00+00:00"
    completed_at = "2026-07-04T12:03:00+00:00"
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="plan",
        units=[
            make_delivery_unit_progress(
                "unit-1",
                "running",
                child_task_id="task-1",
                timestamp=started_at,
            )
        ],
    )

    updated = upsert_delivery_unit_progress(
        progress,
        make_delivery_unit_progress(
            "unit-1",
            "done",
            child_task_id="task-1",
            commit="abc123",
            timestamp=completed_at,
        ),
    )

    assert updated.units[0].status == "done"
    assert updated.units[0].started_at == started_at
    assert updated.units[0].completed_at == completed_at
    assert updated.units[0].updated_at == completed_at


def test_terminal_delivery_progress_accepts_explicit_started_at() -> None:
    unit = make_delivery_unit_progress(
        "unit-1",
        "failed",
        child_task_id="task-1",
        failure_code="tests_failed",
        started_at="2026-07-04T12:00:00+00:00",
        timestamp="2026-07-04T12:04:00+00:00",
    )

    assert unit.started_at == "2026-07-04T12:00:00+00:00"
    assert unit.completed_at == "2026-07-04T12:04:00+00:00"
    assert unit.failure_code == "tests_failed"


def test_delivery_progress_validation_rejects_duplicate_and_invalid_units(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        write_delivery_progress(
            tmp_path / "progress.json",
            DeliveryProgress(
                schema_version=1,
                plan_id="plan",
                units=[
                    DeliveryUnitProgress(unit_id="unit-1", status="pending"),
                    DeliveryUnitProgress(unit_id="unit-1", status="done"),
                ],
            ),
        )

    with pytest.raises(ValueError, match="unit_id"):
        upsert_delivery_unit_progress(
            DeliveryProgress(schema_version=1, plan_id="plan", units=[]),
            DeliveryUnitProgress(unit_id="", status="pending"),
        )

    with pytest.raises(ValueError, match="unknown"):
        upsert_delivery_unit_progress(
            DeliveryProgress(schema_version=1, plan_id="plan", units=[]),
            DeliveryUnitProgress(unit_id="unit-1", status="mystery"),
        )


def test_delivery_progress_validation_rejects_unsupported_schemas(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported delivery progress schema_version"):
        write_delivery_progress(
            tmp_path / "progress.json",
            DeliveryProgress(schema_version=2, plan_id="plan", units=[]),
        )

    with pytest.raises(ValueError, match="unsupported delivery progress event schema_version"):
        append_delivery_progress_event(
            tmp_path / "events.jsonl",
            DeliveryProgressEvent(
                plan_id="plan",
                event_type="unit.done",
                timestamp="2026-07-04T12:00:00+00:00",
                schema_version=2,
            ),
        )


def test_delivery_progress_validation_rejects_invalid_event_status(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        append_delivery_progress_event(
            tmp_path / "events.jsonl",
            make_delivery_progress_event(
                "plan",
                "unit.done",
                unit=DeliveryUnitProgress(unit_id="unit-1", status="mystery"),
            ),
        )


def test_select_next_delivery_unit_respects_dependencies_and_terminal_status(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    pending = get_delivery_status(plan_path)
    assert select_next_delivery_unit(pending).id == "01-foundation"

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "done"}],
        },
    )
    after_first = get_delivery_status(plan_path)
    assert select_next_delivery_unit(after_first).id == "02-feature"

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "running"}],
        },
    )
    running = get_delivery_status(plan_path)
    assert select_next_delivery_unit(running) is None


def test_delivery_status_json_is_privacy_safe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "provider_output": "SECRET_PROVIDER_OUTPUT",
            "units": [
                {
                    "unit_id": "01-foundation",
                    "status": "done",
                    "raw_prompt": "SECRET_PROMPT",
                    "raw_llm_output": "SECRET_LLM_OUTPUT",
                }
            ],
        },
    )

    cmd_delivery_status(argparse.Namespace(plan_file=str(plan_path), json=True), {})

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert payload["valid"] is True
    assert "SECRET_PROVIDER_OUTPUT" not in payload_text
    assert "SECRET_PROMPT" not in payload_text
    assert "SECRET_LLM_OUTPUT" not in payload_text
    assert "Unit body should stay private" not in payload_text


def test_delivery_status_cli_exits_nonzero_for_invalid_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, {"schema_version": 1})

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_status(argparse.Namespace(plan_file=str(plan_path), json=False), {})

    assert exc.value.code == 1
    assert "Status: invalid" in capsys.readouterr().out


def test_main_dispatches_delivery_status_without_loading_project_config(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("schema_version: 1\n", encoding="utf-8")

    with patch("sys.argv", ["sikula", "delivery", "status", str(plan_path)]):
        with patch("sikula._load_runtime_config", return_value={}) as load_config:
            with patch("sikula.cmd_delivery_status") as delivery_status:
                main()

    load_config.assert_not_called()
    delivery_status.assert_called_once()


def test_main_dispatches_delivery_run_next_through_runtime_config(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("schema_version: 1\n", encoding="utf-8")
    cfg = {"project": {"root_path": str(tmp_path), "build_tool": "python"}}

    with patch("sys.argv", ["sikula", "delivery", "run-next", str(plan_path), "--dry-run"]):
        with patch("sikula._load_runtime_config", return_value=cfg) as load_config:
            with patch("sikula.cmd_delivery_run_next") as delivery_run_next:
                main()

    load_config.assert_called_once()
    delivery_run_next.assert_called_once()
