from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_plan import DeliveryBudgetExceeded
from core.delivery_progress import (
    DeliveryProgress,
    DeliveryProgressEvent,
    DeliveryProgressLockError,
    DeliveryStatusResult,
    DeliveryStatusUnit,
    DeliveryUnitProgress,
    acquire_delivery_progress_lock,
    append_delivery_progress_event,
    append_delivery_progress_events,
    delivery_events_path,
    delivery_lock_path,
    delivery_progress_path,
    get_delivery_status,
    mark_delivery_assembly,
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


def test_delivery_status_redacts_unsafe_metadata_from_public_projection(tmp_path: Path) -> None:
    _git_init(tmp_path)
    private_path = "/Users/example/private/task.md"
    plan_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "title": f"Read {private_path}",
            "final_branch": "sikula/delivery/status-demo",
            "units": [
                {
                    "id": "01-foundation",
                    "title": f"Implement from {private_path}",
                    "task_path": _write_unit(tmp_path, "01-foundation.md"),
                    "depends_on": [],
                }
            ],
        },
    )

    result = get_delivery_status(plan_path)
    payload = result.to_dict()
    rendered = render_delivery_status(result)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.title == f"Read {private_path}"
    assert payload["plan"]["title"] == "<redacted>"
    assert payload["units"][0]["title"] == "<redacted>"
    assert "Title: <redacted>" in rendered
    assert "01-foundation: pending (eligible) — <redacted>" in rendered
    assert private_path not in json.dumps(payload)
    assert private_path not in rendered


def test_delivery_status_projects_unsafe_identity_references_consistently(tmp_path: Path) -> None:
    _git_init(tmp_path)
    repository_id = "/Users/example/private/repository"
    first_unit_id = "/Users/example/private/foundation"
    second_unit_id = r"C:\Users\example\private\feature"
    plan_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "title": "Delivery status demo",
            "final_branch": "sikula/delivery/status-demo",
            "repositories": [{"id": repository_id, "root": "."}],
            "units": [
                {
                    "id": first_unit_id,
                    "repo_id": repository_id,
                    "task_path": _write_unit(tmp_path, "01-foundation.md"),
                    "depends_on": [],
                },
                {
                    "id": second_unit_id,
                    "repo_id": repository_id,
                    "task_path": _write_unit(tmp_path, "02-feature.md"),
                    "depends_on": [first_unit_id],
                },
            ],
        },
    )
    write_delivery_progress(
        delivery_progress_path(tmp_path, "delivery-status-demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="delivery-status-demo",
            units=[],
            assembly_base_commit="base-commit",
            assembly_status="failed",
            assembly_unit_id=first_unit_id,
            assembly_error_code="delivery.assembly_conflict",
        ),
    )

    result = get_delivery_status(plan_path)
    payload = result.to_dict()
    rendered = render_delivery_status(result)

    assert result.valid is True
    projected_repo = payload["plan"]["repositories"][0]["id"]
    projected_first = payload["units"][0]["id"]
    assert projected_repo == payload["units"][0]["repo_id"]
    assert projected_repo == payload["units"][1]["repo_id"]
    assert projected_first == payload["units"][1]["depends_on"][0]
    assert projected_first == payload["assembly_unit_id"]
    assert projected_first in payload["next_action"]
    serialized = json.dumps(payload)
    assert repository_id not in serialized
    assert first_unit_id not in serialized
    assert second_unit_id not in serialized
    assert repository_id not in rendered
    assert first_unit_id not in rendered
    assert second_unit_id not in rendered


def test_delivery_status_preserves_monorepo_component_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    unit_1 = _write_unit(tmp_path, "01-foundation.md")
    plan_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "title": "Delivery status demo",
            "final_branch": "sikula/delivery/status-demo",
            "streams": [{"id": "app", "label": "App"}],
            "components": [
                {
                    "id": "android",
                    "label": "Android app",
                    "path": "apps/android",
                    "stream": "app",
                }
            ],
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "stream": "app",
                    "component": "android",
                    "scope_paths": ["apps/android/app", "apps/android/gradle.properties"],
                    "estimated_size": "small",
                    "risk_tags": ["validation"],
                    "budget": {"max_planner_steps": 2, "max_changed_files": 4},
                    "task_path": unit_1,
                    "depends_on": [],
                }
            ],
        },
    )

    result = get_delivery_status(plan_path)
    payload = result.to_dict()

    assert result.valid is True
    assert result.units[0].component == "android"
    assert result.units[0].scope_paths == ["apps/android/app", "apps/android/gradle.properties"]
    assert result.units[0].estimated_size == "small"
    assert result.units[0].risk_tags == ["validation"]
    assert result.units[0].budget is not None
    assert result.units[0].budget.to_dict() == {"max_planner_steps": 2, "max_changed_files": 4}
    assert payload["plan"]["components"] == [
        {
            "id": "android",
            "path": "apps/android",
            "label": "Android app",
            "stream": "app",
        }
    ]
    assert payload["units"][0]["component"] == "android"
    assert payload["units"][0]["scope_paths"] == ["apps/android/app", "apps/android/gradle.properties"]
    assert payload["units"][0]["estimated_size"] == "small"
    assert payload["units"][0]["risk_tags"] == ["validation"]
    assert payload["units"][0]["budget"] == {"max_planner_steps": 2, "max_changed_files": 4}
    rendered = render_delivery_status(result)
    assert "size=small" in rendered
    assert "risk=validation" in rendered


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
    assert result.next_action == "run delivery run-next to resume or reconcile the running unit"
    rendered = render_delivery_status(result)
    assert "task=task-1" in rendered
    assert "branch=sikula/unit-1" in rendered
    assert "commit=abc123" in rendered
    assert "(run-next: resume or reconcile linked child)" in rendered


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
    assert result.next_action == "run delivery run-next to resume or reconcile the running unit"


def test_delivery_status_reports_waiting_and_failed_states(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    private_waiting_reason = "/Users/example/private/waiting"
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {
                    "unit_id": "01-foundation",
                    "status": "waiting",
                    "waiting_reason": private_waiting_reason,
                },
                {"unit_id": "02-feature", "status": "failed", "failure_code": "child_failed"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is True
    assert result.status == "failed"
    assert result.units[0].waiting_reason == private_waiting_reason
    assert result.to_dict()["units"][0]["waiting_reason"] == "<redacted>"
    assert result.units[1].failure_code == "child_failed"
    assert result.next_action == "inspect the failed delivery unit; no linked child task is available for retry"
    rendered = render_delivery_status(result)
    assert "(retry unavailable: missing child task id)" in rendered
    assert private_waiting_reason not in rendered


def test_delivery_status_unit_redacts_unvalidated_progress_strings() -> None:
    private_path = "/Users/example/private/progress"
    unit = DeliveryStatusUnit(
        id="unit",
        status="waiting",
        title="Unit",
        task_path="unit.md",
        depends_on=[],
        child_task_id=private_path,
        branch=private_path,
        commit=private_path,
        waiting_reason=private_path,
        failure_code=private_path,
        started_at=private_path,
        completed_at=private_path,
        updated_at=private_path,
    )

    payload = unit.to_dict()

    assert payload["child_task_id"].startswith("<redacted:")
    for key in (
        "branch",
        "commit",
        "waiting_reason",
        "failure_code",
        "started_at",
        "completed_at",
        "updated_at",
    ):
        assert payload[key] == "<redacted>"
    assert private_path not in json.dumps(payload)


def test_delivery_status_reports_running_without_child_task_id(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "running"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.status == "running"
    assert result.next_action == "inspect parent delivery progress; running unit has no linked child task"
    rendered = render_delivery_status(result)
    assert "(run-next blocked: missing child task id)" in rendered


def test_delivery_status_reports_multiple_running_units_as_manual_reconciliation(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "running", "child_task_id": "task-1"},
                {"unit_id": "02-feature", "status": "running", "child_task_id": "task-2"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.status == "running"
    assert result.next_action == "inspect parent delivery progress; multiple running units need manual reconciliation"


def test_delivery_status_reports_failed_with_child_task_id(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-1"},
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.status == "failed"
    assert result.next_action == "retry a failed delivery unit with delivery run-next --reset-failed"
    rendered = render_delivery_status(result)
    assert "(run-next: retry with --reset-failed)" in rendered


@pytest.mark.parametrize(
    ("status", "child_task_id", "expected_available", "expected_action", "expected_blocked"),
    [
        ("running", "task-1", True, "resume_or_reconcile", None),
        ("running", None, False, None, "missing_child_task_id"),
        ("failed", "task-1", True, "retry_failed", None),
        ("failed", None, False, None, "missing_child_task_id"),
        ("done", "task-1", False, None, None),
        ("pending", None, False, None, None),
        ("canceled", None, False, None, None),
        ("waiting", None, False, None, None),
    ],
)
def test_delivery_status_unit_run_next_metadata(
    status: str,
    child_task_id: str | None,
    expected_available: bool,
    expected_action: str | None,
    expected_blocked: str | None,
) -> None:
    from core.delivery_progress import DeliveryStatusUnit

    unit = DeliveryStatusUnit(
        id="unit-1",
        title="Unit 1",
        status=status,
        task_path="path",
        depends_on=[],
        child_task_id=child_task_id,
    )

    assert unit.run_next_available is expected_available
    assert unit.run_next_action == expected_action
    assert unit.run_next_blocked_reason == expected_blocked

    data = unit.to_dict()
    assert data["run_next_available"] is expected_available
    if expected_action:
        assert data["run_next_action"] == expected_action
    else:
        assert "run_next_action" not in data

    if expected_blocked:
        assert data["run_next_blocked_reason"] == expected_blocked
    else:
        assert "run_next_blocked_reason" not in data


def test_delivery_status_unit_projects_amendment_metadata() -> None:
    from core.delivery_progress import DeliveryStatusUnit

    unit = DeliveryStatusUnit(
        id="original",
        title="Original unit",
        status="superseded",
        task_path="units/original.md",
        depends_on=[],
        superseded_by=["replacement-a", "replacement-b"],
        budget_exceeded=DeliveryBudgetExceeded(name="max_planner_steps", limit=2, actual=5),
    )

    data = unit.to_dict()

    assert data["superseded_by"] == ["replacement-a", "replacement-b"]
    assert data["budget_exceeded"] == {"name": "max_planner_steps", "limit": 2, "actual": 5}


def test_delivery_status_unit_projects_budget_stop_as_split_only() -> None:
    unit = DeliveryStatusUnit(
        id="unit-1",
        title="Unit 1",
        status="failed",
        task_path="units/unit-1.md",
        depends_on=[],
        child_task_id="task-1",
        failure_code="unit_budget_exceeded",
        budget_exceeded=DeliveryBudgetExceeded(name="max_planner_steps", limit=1, actual=3),
    )

    assert unit.run_next_available is False
    assert unit.run_next_action is None
    assert unit.run_next_blocked_reason == "unit_budget_exceeded"
    assert unit.to_dict()["run_next_blocked_reason"] == "unit_budget_exceeded"

    result = DeliveryStatusResult(
        status="failed",
        plan_path="plan.yaml",
        progress_path=None,
        progress_exists=True,
        project_root=None,
        plan=None,
        final_branch=None,
        final_commit=None,
        finalized_at=None,
        next_action="split the budget-exceeded unit",
        units=[unit],
        errors=[],
        warnings=[],
    )
    assert "split required before implementation" in render_delivery_status(result)


def test_append_delivery_progress_events_ignores_empty_batch(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    append_delivery_progress_events(path, [])

    assert not path.exists()


def test_delivery_status_projects_paths_relative_to_project_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "running"},
            ],
        },
    )

    result = get_delivery_status(plan_path, project_root=tmp_path)

    rendered = render_delivery_status(result)
    assert "Project root: ." in rendered
    assert "Delivery plan status: .sikula/delivery/demo/plan.yaml" in rendered
    assert "Progress: .sikula/state/delivery/delivery-status-demo/progress.json" in rendered

    data = result.to_dict()
    assert data["project_root"] == "."
    assert data["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert data["progress_path"] == ".sikula/state/delivery/delivery-status-demo/progress.json"


def test_delivery_status_result_to_dict_relativizes_plan_paths() -> None:
    from core.delivery_progress import DeliveryStatusResult
    from core.delivery_plan import DeliveryPlan, DeliveryRepository, DeliveryComponent

    plan = DeliveryPlan(
        schema_version=1,
        plan_id="plan",
        title="title",
        final_branch="branch",
        repositories=[DeliveryRepository(id="main", root="/opt/project/main_repo")],
        stream_ids={"app"},
        components=[DeliveryComponent(id="api", label="API", path="/opt/project/apps/api", stream="app")],
        units=[],
    )

    result = DeliveryStatusResult(
        plan_path="/opt/project/plan.yaml",
        project_root="/opt/project",
        progress_path="/opt/project/progress.json",
        progress_exists=True,
        status="pending",
        errors=[],
        warnings=[],
        units=[],
        plan=plan,
    )

    data = result.to_dict()
    assert data["project_root"] == "."
    assert data["plan_path"] == "plan.yaml"
    assert data["progress_path"] == "progress.json"
    assert data["plan"]["repositories"][0]["root"] == "main_repo"
    assert data["plan"]["components"][0]["path"] == "apps/api"


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
    assert result.next_action == "finalize delivery branch"


def test_delivery_status_reports_finalized_branch_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "final_branch": "sikula/delivery/status-demo",
            "final_commit": "abc123",
            "finalized_at": "2026-07-04T12:00:00+00:00",
            "units": [
                {"unit_id": "01-foundation", "status": "done"},
                {"unit_id": "02-feature", "status": "done"},
            ],
        },
    )

    result = get_delivery_status(plan_path)
    payload = result.to_dict()

    assert result.valid is True
    assert result.status == "done"
    assert result.final_branch == "sikula/delivery/status-demo"
    assert result.final_commit == "abc123"
    assert result.finalized_at == "2026-07-04T12:00:00+00:00"
    assert result.next_action == "review finalized delivery branch"
    assert payload["final_branch"] == "sikula/delivery/status-demo"
    assert payload["final_commit"] == "abc123"
    assert payload["finalized_at"] == "2026-07-04T12:00:00+00:00"
    rendered = render_delivery_status(result)
    assert "Finalized: sikula/delivery/status-demo @ abc123" in rendered
    assert "Next action: review finalized delivery branch" in rendered


def test_delivery_status_sanitizes_all_top_level_progress_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    private_value = "/Users/example/private/progress-value"
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "assembly_base_commit": private_value,
            "assembled_commit": private_value,
            "assembly_status": "failed",
            "assembly_unit_id": private_value,
            "assembly_error_code": private_value,
            "assembly_updated_at": private_value,
            "final_branch": private_value,
            "final_commit": private_value,
            "finalized_at": private_value,
            "units": [],
        },
    )

    result = get_delivery_status(plan_path)
    payload = result.to_dict()
    rendered = render_delivery_status(result)

    assert result.valid is True
    for key in (
        "assembly_base_commit",
        "assembled_commit",
        "assembly_error_code",
        "assembly_updated_at",
        "final_branch",
        "final_commit",
        "finalized_at",
    ):
        assert getattr(result, key) == private_value
        assert payload[key] == "<redacted>"
    assert result.assembly_unit_id == private_value
    assert payload["assembly_unit_id"].startswith("<redacted:")
    assert private_value not in json.dumps(payload)
    assert private_value not in rendered


def test_unit_progress_update_clears_stale_finalization_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    unit_1 = _write_unit(tmp_path, "01-foundation.md")
    unit_2 = _write_unit(tmp_path, "02-feature.md")
    unit_3 = _write_unit(tmp_path, "03-followup.md")
    plan_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "title": "Delivery status demo",
            "final_branch": "sikula/delivery/status-demo",
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "task_path": unit_1,
                    "depends_on": [],
                },
                {
                    "id": "02-feature",
                    "title": "Add feature",
                    "task_path": unit_2,
                    "depends_on": ["01-foundation"],
                },
                {
                    "id": "03-followup",
                    "title": "Add follow-up",
                    "task_path": unit_3,
                    "depends_on": ["02-feature"],
                },
            ],
        },
    )
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="delivery-status-demo",
        units=[
            DeliveryUnitProgress(unit_id="01-foundation", status="done", commit="commit-1"),
            DeliveryUnitProgress(unit_id="02-feature", status="done", commit="commit-2"),
        ],
        assembly_base_commit="base-commit",
        assembled_commit="commit-2",
        assembly_status="ready",
        assembly_updated_at="2026-07-04T11:59:00+00:00",
        final_branch="sikula/delivery/status-demo",
        final_commit="commit-2",
        finalized_at="2026-07-04T12:00:00+00:00",
    )

    updated = upsert_delivery_unit_progress(
        progress,
        DeliveryUnitProgress(unit_id="03-followup", status="done", commit="commit-3"),
    )
    write_delivery_progress(delivery_progress_path(tmp_path, "delivery-status-demo"), updated)
    result = get_delivery_status(plan_path)

    assert updated.final_branch is None
    assert updated.final_commit is None
    assert updated.finalized_at is None
    assert updated.assembly_base_commit == "base-commit"
    assert updated.assembled_commit == "commit-2"
    assert updated.assembly_status == "ready"
    assert result.status == "done"
    assert result.assembly_base_commit == "base-commit"
    assert result.assembled_commit == "commit-2"
    assert result.assembly_status == "ready"
    assert result.final_branch is None
    assert result.final_commit is None
    assert result.finalized_at is None
    assert result.next_action == "finalize delivery branch"


def test_mark_delivery_assembly_records_and_clears_recoverable_failure() -> None:
    progress = DeliveryProgress(schema_version=1, plan_id="plan", units=[])

    failed = mark_delivery_assembly(
        progress,
        base_commit="base",
        assembled_commit="partial",
        status="failed",
        unit_id="unit-2",
        error_code="delivery.assembly_conflict",
        timestamp="2026-07-04T12:00:00+00:00",
    )
    ready = mark_delivery_assembly(
        failed,
        base_commit="base",
        assembled_commit="resolved",
        status="ready",
        timestamp="2026-07-04T12:05:00+00:00",
    )

    assert failed.to_dict()["assembly_error_code"] == "delivery.assembly_conflict"
    assert failed.assembly_unit_id == "unit-2"
    assert ready.assembly_status == "ready"
    assert ready.assembled_commit == "resolved"
    assert ready.assembly_unit_id is None
    assert ready.assembly_error_code is None


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

    result = get_delivery_status(plan_path, project_root=tmp_path)

    assert result.valid is False
    assert result.status == "invalid"
    assert result.progress_path is None
    assert "plan_id.invalid" in _error_codes(result)

    data = result.to_dict()
    assert data["project_root"] == "."
    assert data["progress_path"] is None

    rendered = render_delivery_status(result)
    assert "Project root: ." in rendered
    assert "Progress:" not in rendered


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


def test_delivery_status_rejects_inconsistent_assembly_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [],
            "assembly_base_commit": "base",
            "assembly_status": "failed",
            "assembly_updated_at": "2026-07-04T12:00:00+00:00",
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.assembly_invalid" in _error_codes(result)


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


@pytest.mark.parametrize(
    "budget_exceeded",
    [
        {"name": "max_planner_steps", "limit": 1},
        {"name": "bad value", "limit": 1, "actual": 3},
    ],
)
def test_delivery_status_rejects_invalid_budget_exceeded_metadata(tmp_path: Path, budget_exceeded: dict) -> None:
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
                    "status": "failed",
                    "failure_code": "unit_budget_exceeded",
                    "budget_exceeded": budget_exceeded,
                }
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert "progress.budget_exceeded_invalid" in _error_codes(result)


@pytest.mark.parametrize(
    "handoff_metadata",
    [
        {"handoff_schema_version": True, "handoff_fingerprint": "a" * 64},
        {"handoff_schema_version": 1},
        {"handoff_fingerprint": "a" * 64},
    ],
)
def test_delivery_status_rejects_invalid_handoff_reference(tmp_path: Path, handoff_metadata: dict) -> None:
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
                    **handoff_metadata,
                }
            ],
        },
    )

    result = get_delivery_status(plan_path)

    assert result.valid is False
    assert any("handoff" in code for code in _error_codes(result))


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


def test_running_delivery_progress_accepts_explicit_started_at() -> None:
    unit = make_delivery_unit_progress(
        "unit-1",
        "running",
        child_task_id="task-1",
        started_at="2026-07-04T12:00:00+00:00",
        timestamp="2026-07-04T12:04:00+00:00",
    )

    assert unit.started_at == "2026-07-04T12:00:00+00:00"
    assert unit.updated_at == "2026-07-04T12:04:00+00:00"
    assert unit.completed_at is None


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

    invalid_budget = DeliveryBudgetExceeded(name="bad value", limit=1, actual=3)
    with pytest.raises(ValueError, match="budget_exceeded"):
        write_delivery_progress(
            tmp_path / "progress.json",
            DeliveryProgress(
                schema_version=1,
                plan_id="plan",
                units=[
                    DeliveryUnitProgress(
                        unit_id="unit-1",
                        status="failed",
                        budget_exceeded=invalid_budget,
                    )
                ],
            ),
        )

    with pytest.raises(ValueError, match="budget_exceeded"):
        append_delivery_progress_event(
            tmp_path / "events.jsonl",
            DeliveryProgressEvent(
                plan_id="plan",
                event_type="unit.failed",
                timestamp="2026-07-21T12:00:00+00:00",
                budget_exceeded=invalid_budget,
            ),
        )


def test_delivery_progress_accepts_amendment_budget_metadata_domain(tmp_path: Path) -> None:
    budget = DeliveryBudgetExceeded(name="MaxFiles", limit=0, actual=2)
    progress_path = tmp_path / "progress.json"
    events_path = tmp_path / "events.jsonl"

    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="plan",
            units=[
                DeliveryUnitProgress(
                    unit_id="unit-1",
                    status="failed",
                    budget_exceeded=budget,
                )
            ],
        ),
    )
    append_delivery_progress_event(
        events_path,
        DeliveryProgressEvent(
            plan_id="plan",
            event_type="plan.amended",
            timestamp="2026-07-21T12:00:00+00:00",
            budget_exceeded=budget,
        ),
    )

    assert json.loads(progress_path.read_text())["units"][0]["budget_exceeded"] == {
        "name": "MaxFiles",
        "limit": 0,
        "actual": 2,
    }
    assert json.loads(events_path.read_text())["budget_exceeded"] == {
        "name": "MaxFiles",
        "limit": 0,
        "actual": 2,
    }


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


def test_select_next_delivery_unit_reset_failed_selection(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"}],
        },
    )
    failed_with_child = get_delivery_status(plan_path)
    assert failed_with_child.status == "failed"
    assert select_next_delivery_unit(failed_with_child) is None

    selected = select_next_delivery_unit(failed_with_child, reset_failed=True)
    assert selected is not None
    assert selected.id == "01-foundation"

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [{"unit_id": "01-foundation", "status": "failed"}],
        },
    )
    failed_no_child = get_delivery_status(plan_path)
    assert failed_no_child.status == "failed"
    assert select_next_delivery_unit(failed_no_child, reset_failed=True) is None

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "done"},
                {"unit_id": "02-feature", "status": "failed", "child_task_id": "task-abc"},
            ],
        },
    )
    failed_later_unit = get_delivery_status(plan_path)
    assert failed_later_unit.status == "failed"
    selected_later = select_next_delivery_unit(failed_later_unit, reset_failed=True)
    assert selected_later is not None
    assert selected_later.id == "02-feature"


def test_select_next_delivery_unit_reset_failed_returns_none_for_terminal_status(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    for status in ["running", "waiting", "canceled", "done"]:
        _write_progress(
            tmp_path,
            "delivery-status-demo",
            {
                "schema_version": 1,
                "plan_id": "delivery-status-demo",
                "units": [
                    {"unit_id": "01-foundation", "status": status, "child_task_id": "task-xyz"},
                    {"unit_id": "02-feature", "status": status, "child_task_id": "task-abc"},
                ],
            },
        )
        result = get_delivery_status(plan_path)
        assert result.status == status
        assert select_next_delivery_unit(result, reset_failed=True) is None

    invalid_path = _write_plan(
        tmp_path,
        {
            "schema_version": 1,
            "plan_id": "../bad",
            "title": "Bad plan id",
            "final_branch": "sikula/delivery/bad",
            "units": [],
        },
    )
    invalid_result = get_delivery_status(invalid_path)
    assert not invalid_result.valid
    assert select_next_delivery_unit(invalid_result, reset_failed=True) is None


def test_select_next_delivery_unit_reset_failed_returns_none_for_failed_status_with_running_unit(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    _write_progress(
        tmp_path,
        "delivery-status-demo",
        {
            "schema_version": 1,
            "plan_id": "delivery-status-demo",
            "units": [
                {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
                {"unit_id": "02-feature", "status": "running"},
            ],
        },
    )
    result = get_delivery_status(plan_path)
    assert result.status == "failed"
    assert select_next_delivery_unit(result, reset_failed=True) is None


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


def test_project_relative_path(tmp_path: Path) -> None:
    from core.delivery_progress import _project_relative_path

    assert _project_relative_path("/foo/bar", None) == "/foo/bar"
    assert _project_relative_path("/foo/bar", str(tmp_path)) == "bar"

    sub = tmp_path / "sub"
    sub.mkdir()
    file_path = sub / "file.txt"
    assert _project_relative_path(str(file_path), str(tmp_path)) == "sub/file.txt"


def test_sanitize_issue() -> None:
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_progress import _sanitize_issue

    issue = DeliveryPlanIssue(
        "error", "code", "Issue in /opt/project/plan.yaml and /opt/project/progress.json", "/opt/project/plan.yaml"
    )

    # Without project root, returns unchanged
    assert _sanitize_issue(issue, None, "/opt/project/plan.yaml", "/opt/project/progress.json") is issue
    assert _sanitize_issue(issue, ".", "/opt/project/plan.yaml", "/opt/project/progress.json") is issue

    # Sanitizes absolute paths inside messages and path
    sanitized = _sanitize_issue(issue, "/opt/project", "/opt/project/plan.yaml", "/opt/project/progress.json")
    assert sanitized.message == "Issue in plan.yaml and progress.json"
    assert sanitized.path == "plan.yaml"

    # Sanitizes project root strings not caught by exact plan/progress matches
    issue2 = DeliveryPlanIssue(
        "error", "code", "Error loading /opt/project/some/other/file.txt", "/opt/project/some/other/file.txt"
    )
    sanitized2 = _sanitize_issue(issue2, "/opt/project", "/opt/project/plan.yaml", "/opt/project/progress.json")
    assert sanitized2.message == "Error loading some/other/file.txt"
    assert sanitized2.path == "some/other/file.txt"
