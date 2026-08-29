from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import DeliveryStatusUnit
from core.delivery_run import (
    DELIVERY_RUN_BLOCKED,
    DELIVERY_RUN_COMPLETED,
    DELIVERY_RUN_ELAPSED_LIMIT_REACHED,
    DELIVERY_RUN_FINALIZE_FAILED,
    DELIVERY_RUN_NO_PROGRESS,
    DELIVERY_RUN_PREVIEW,
    DELIVERY_RUN_SNAPSHOT_EXHAUSTED,
    DELIVERY_RUN_UNIT_FAILED,
    DELIVERY_RUN_UNIT_LIMIT_REACHED,
    DeliveryRunResult,
    _public_path,
    render_delivery_run,
)
from core.state import JsonStateStore
from sikula_cli.delivery import (
    DeliveryRunNextContext,
    _bounded_delivery_run_snapshot_issue,
    _delivery_run_is_current_finalization,
    _finalize_delivery_run,
    _preview_delivery_run,
    _run_delivery_plan,
    cmd_delivery_run,
)


def _args(
    *,
    max_units: int | None = None,
    max_elapsed_minutes: int | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    reset_failed: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file="plan.yaml",
        max_units=max_units,
        max_elapsed_minutes=max_elapsed_minutes,
        reset_failed=reset_failed,
        dry_run=dry_run,
        json=json_output,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )


def _status(unit_statuses: list[str], *, final_commit: str | None = None) -> SimpleNamespace:
    units = [
        DeliveryStatusUnit(
            id=f"unit-{index}",
            status=unit_status,
            title=f"Unit {index}",
            task_path=f"units/unit-{index}.md",
            depends_on=[],
        )
        for index, unit_status in enumerate(unit_statuses, start=1)
    ]
    if any(unit.status == "failed" for unit in units):
        overall = "failed"
    elif any(unit.status == "running" for unit in units):
        overall = "running"
    elif units and all(unit.status == "done" for unit in units):
        overall = "done"
    else:
        overall = "pending"
    return SimpleNamespace(
        plan_path="/project/plan.yaml",
        project_root="/project",
        progress_path="/project/.sikula/state/delivery/demo/progress.json",
        progress_exists=True,
        valid=True,
        status=overall,
        errors=[],
        warnings=[],
        plan=SimpleNamespace(final_branch="sikula/delivery/demo"),
        units=units,
        assembled_commit=final_commit,
        assembly_status="ready" if final_commit else None,
        final_branch="sikula/delivery/demo" if final_commit else None,
        final_commit=final_commit,
        finalized_at="2026-07-27T12:00:00+00:00" if final_commit else None,
    )


def _context() -> DeliveryRunNextContext:
    return DeliveryRunNextContext(
        run_task=lambda args, cfg: 0,
        resolve_state_dir=lambda cfg: Path("/project/.sikula/state"),
        state_store=JsonStateStore(Path("/project/.sikula/state")),
    )


def _unit_result(status: SimpleNamespace, *, succeeded: bool = True, ran: bool = True) -> SimpleNamespace:
    selected = next((unit for unit in status.units if unit.status == "pending"), status.units[-1])
    return SimpleNamespace(
        valid=succeeded,
        ran=ran,
        succeeded=succeeded,
        selected_unit=selected,
        child_task_id=f"child-{selected.id}",
        errors=[] if succeeded else [DeliveryPlanIssue("error", "delivery.failed", "Unit failed.")],
        warnings=[],
        message=f"Delivery unit {selected.id} {'completed' if succeeded else 'failed'}.",
    )


def _run_result(
    *,
    dry_run: bool = False,
    ready: bool = True,
    started: bool = False,
    succeeded: bool = True,
    completed: bool = False,
    finalized: bool = False,
    errors: list[DeliveryPlanIssue] | None = None,
) -> DeliveryRunResult:
    return DeliveryRunResult(
        plan_path="/project/plan.yaml",
        project_root="/project",
        valid=not errors,
        ready=ready,
        dry_run=dry_run,
        started=started,
        succeeded=succeeded,
        completed=completed,
        finalized=finalized,
        status="done" if completed else "pending",
        max_units=2,
        max_elapsed_minutes=None,
        units_attempted=0,
        units_succeeded=0,
        last_unit=None,
        child_task_id=None,
        stop_code=DELIVERY_RUN_COMPLETED if completed else DELIVERY_RUN_PREVIEW,
        progress_path="/project/.sikula/state/delivery/demo/progress.json",
        final_branch="sikula/delivery/demo" if completed else None,
        final_commit="a" * 40 if completed else None,
        errors=list(errors or []),
        warnings=[],
        message="Delivery run result.",
    )


def test_delivery_run_parser_accepts_bounds_and_agent_overrides() -> None:
    import sikula_cli.delivery as delivery_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    delivery_cli.register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            "run",
            ".sikula/delivery/demo/plan.yaml",
            "--max-units",
            "3",
            "--max-elapsed-minutes",
            "45",
            "--reset-failed",
            "--agent-provider",
            "implementer=claude",
        ]
    )

    assert args.delivery_command == "run"
    assert args.max_units == 3
    assert args.max_elapsed_minutes == 45
    assert args.reset_failed is True
    assert args.agent_provider == ["implementer=claude"]


@pytest.mark.parametrize(
    ("flag", "value"), [("--max-units", "0"), ("--max-units", "-1"), ("--max-elapsed-minutes", "false")]
)
def test_delivery_run_parser_rejects_invalid_bounds(flag: str, value: str) -> None:
    import sikula_cli.delivery as delivery_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    delivery_cli.register_parser(subparsers)

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["delivery", "run", "plan.yaml", flag, value])

    assert exc_info.value.code == 2


def test_cmd_delivery_run_requires_execution_context(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_run(_args(), {"project": {"root_path": "/project"}})

    assert exc_info.value.code == 2
    assert "requires the main Sikula command context" in capsys.readouterr().out


def test_cmd_delivery_run_dry_run_exits_nonzero_when_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    issue = DeliveryPlanIssue("error", "delivery.blocked", "Blocked.")
    result = _run_result(dry_run=True, ready=False, succeeded=False, errors=[issue])

    with (
        patch("sikula_cli.delivery._preview_delivery_run", return_value=result),
        pytest.raises(SystemExit) as exc_info,
    ):
        cmd_delivery_run(
            _args(dry_run=True, json_output=True),
            {"project": {"root_path": "/project"}},
        )

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_preview_delivery_run_rejects_invalid_status(tmp_path: Path) -> None:
    args = _args(dry_run=True)
    args.plan_file = str(tmp_path / "missing-plan.yaml")

    result = _preview_delivery_run(args, {}, project_root=tmp_path)

    assert result.ready is False
    assert result.stop_code == DELIVERY_RUN_BLOCKED
    assert result.errors


@pytest.mark.parametrize(
    ("preview_ready", "preview_commit", "expected_code", "expected_completed"),
    [
        (True, "a" * 40, DELIVERY_RUN_COMPLETED, True),
        (True, None, DELIVERY_RUN_PREVIEW, False),
        (False, None, DELIVERY_RUN_BLOCKED, False),
    ],
)
def test_preview_delivery_run_handles_completed_plan(
    monkeypatch: pytest.MonkeyPatch,
    preview_ready: bool,
    preview_commit: str | None,
    expected_code: str,
    expected_completed: bool,
) -> None:
    status = _status(["done"], final_commit="a" * 40)
    if not expected_completed:
        status.finalized_at = None
        status.final_commit = None
    final_preview = SimpleNamespace(
        ready=preview_ready,
        final_branch="sikula/delivery/demo",
        final_commit=preview_commit,
        errors=[] if preview_ready else [DeliveryPlanIssue("error", "delivery.finalize_blocked", "Blocked.")],
        warnings=[],
        message="Finalization preview.",
    )

    def preview_finalize(*args, **kwargs):
        if expected_completed:
            pytest.fail("current finalization must bypass mutating finalize preview")
        return final_preview

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr("core.delivery_finalize.preview_delivery_finalize", preview_finalize)
    if expected_completed:
        monkeypatch.setattr(
            "core.delivery_finalize.delivery_assembly_branch_is_symbolic",
            lambda *args, **kwargs: False,
        )
        monkeypatch.setattr("core.delivery_finalize._branch_commit", lambda *args, **kwargs: "a" * 40)
        monkeypatch.setattr("core.delivery_finalize._resolve_commit", lambda *args, **kwargs: "a" * 40)

    result = _preview_delivery_run(_args(dry_run=True), {}, project_root=Path("/project"))

    assert result.ready is preview_ready
    assert result.completed is expected_completed
    assert result.stop_code == expected_code


def test_preview_delivery_run_keeps_finalize_blocker_when_recorded_branch_diverged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _status(["done"], final_commit="a" * 40)
    issue = DeliveryPlanIssue(
        "error",
        "delivery.assembly_branch_checked_out",
        "Delivery assembly branch is checked out.",
    )
    final_preview = SimpleNamespace(
        ready=False,
        final_branch=status.final_branch,
        final_commit=None,
        errors=[issue],
        warnings=[],
        message=issue.message,
    )

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr("core.delivery_finalize.preview_delivery_finalize", lambda *args, **kwargs: final_preview)
    monkeypatch.setattr(
        "core.delivery_finalize.delivery_assembly_branch_is_symbolic",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr("core.delivery_finalize._branch_commit", lambda *args, **kwargs: "b" * 40)
    monkeypatch.setattr("core.delivery_finalize._resolve_commit", lambda *args, **kwargs: "a" * 40)

    result = _preview_delivery_run(_args(dry_run=True), {}, project_root=Path("/project"))

    assert result.ready is False
    assert result.completed is False
    assert result.stop_code == DELIVERY_RUN_BLOCKED
    assert result.errors == [issue]


def test_current_finalization_requires_recorded_branch_to_match_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _status(["done"], final_commit="a" * 40)
    status.plan.final_branch = "sikula/delivery/new-target"
    monkeypatch.setattr("core.delivery_finalize._branch_commit", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr("core.delivery_finalize._resolve_commit", lambda *args, **kwargs: "a" * 40)

    assert _delivery_run_is_current_finalization(status) is False


def test_current_finalization_rejects_symbolic_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["done"], final_commit="a" * 40)
    monkeypatch.setattr(
        "core.delivery_finalize.delivery_assembly_branch_is_symbolic",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "core.delivery_finalize._branch_commit",
        lambda *args, **kwargs: pytest.fail("symbolic branch must not be resolved"),
    )

    assert _delivery_run_is_current_finalization(status) is False


@pytest.mark.parametrize(("ready", "reset_failed"), [(True, False), (False, False), (True, True)])
def test_preview_delivery_run_projects_next_unit(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    reset_failed: bool,
) -> None:
    status = _status(["pending"])
    seen: dict[str, bool] = {}
    preview = SimpleNamespace(
        ready=ready,
        selected_unit=status.units[0],
        errors=[] if ready else [DeliveryPlanIssue("error", "delivery.blocked", "Blocked.")],
        warnings=[],
        message="Next unit preview.",
    )
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)

    def preview_run_next(*args, **kwargs):
        seen["preview"] = kwargs["reset_failed"]
        return preview

    def apply_guards(value, *args, **kwargs):
        seen["guards"] = kwargs["reset_failed"]
        return value

    monkeypatch.setattr("core.delivery_run_next.preview_delivery_run_next", preview_run_next)
    monkeypatch.setattr(
        "sikula_cli.delivery._apply_delivery_preview_execution_guards",
        apply_guards,
    )

    result = _preview_delivery_run(
        _args(dry_run=True, reset_failed=reset_failed),
        {},
        project_root=Path("/project"),
    )

    assert result.ready is ready
    assert result.last_unit == status.units[0]
    assert result.stop_code == (DELIVERY_RUN_PREVIEW if ready else DELIVERY_RUN_BLOCKED)
    assert seen == {"preview": reset_failed, "guards": reset_failed}


def test_delivery_run_stops_successfully_at_unit_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"done": 0}

    def current_status(*args, **kwargs):
        return _status(["done"] * state["done"] + ["pending"] * (2 - state["done"]))

    def run_next(*args, **kwargs):
        before = current_status()
        result = _unit_result(before)
        state["done"] += 1
        return result

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", current_status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)

    args = _args(max_units=1)
    args.plan_file = "/project/plan.yaml"
    result = _run_delivery_plan(args, {}, _context(), project_root=Path("/project"))

    assert result.succeeded is True
    assert result.completed is False
    assert result.units_attempted == 1
    assert result.units_succeeded == 1
    assert result.stop_code == DELIVERY_RUN_UNIT_LIMIT_REACHED
    assert state["done"] == 1


def test_current_finalization_fails_closed_when_git_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["done"], final_commit="a" * 40)

    def fail_lookup(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(
        "core.delivery_finalize.delivery_assembly_branch_is_symbolic",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr("core.delivery_finalize._branch_commit", fail_lookup)

    assert _delivery_run_is_current_finalization(status) is False


def test_delivery_run_soft_elapsed_limit_is_checked_between_units(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"done": 0}

    def current_status(*args, **kwargs):
        return _status(["done"] * state["done"] + ["pending"] * (2 - state["done"]))

    def run_next(*args, **kwargs):
        before = current_status()
        result = _unit_result(before)
        state["done"] += 1
        return result

    monotonic_values = iter([0.0, 61.0])
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", current_status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)
    monkeypatch.setattr("sikula_cli.delivery.time.monotonic", lambda: next(monotonic_values))

    result = _run_delivery_plan(
        _args(max_elapsed_minutes=1),
        {},
        _context(),
        project_root=Path("/project"),
    )

    assert result.succeeded is True
    assert result.completed is False
    assert result.units_succeeded == 1
    assert result.stop_code == DELIVERY_RUN_ELAPSED_LIMIT_REACHED
    assert state["done"] == 1


def test_delivery_run_keeps_absolute_plan_path_after_child_changes_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    invocation_root = tmp_path / "invocation"
    target_root = tmp_path / "target"
    invocation_root.mkdir()
    target_root.mkdir()
    monkeypatch.chdir(invocation_root)
    args = _args(max_units=1)
    args.plan_file = "../target/plan.yaml"
    state = {"done": False}
    seen_paths: list[str] = []

    def current_status(plan_file, *args, **kwargs):
        seen_paths.append(plan_file)
        return _status(["done", "pending"] if state["done"] else ["pending", "pending"])

    def run_next(run_args, *args, **kwargs):
        assert run_args.plan_file == str(target_root / "plan.yaml")
        state["done"] = True
        monkeypatch.chdir(target_root)
        return _unit_result(_status(["pending", "pending"]))

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", current_status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)

    result = _run_delivery_plan(args, {}, _context(), project_root=target_root)

    assert result.succeeded is True
    assert result.stop_code == DELIVERY_RUN_UNIT_LIMIT_REACHED
    assert args.plan_file == "../target/plan.yaml"
    assert seen_paths
    assert set(seen_paths) == {str(target_root / "plan.yaml")}


def test_delivery_run_stops_after_failed_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["pending", "pending"])
    calls = 0

    def run_next(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _unit_result(status, succeeded=False)

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)

    result = _run_delivery_plan(_args(), {}, _context(), project_root=Path("/project"))

    assert result.succeeded is False
    assert result.completed is False
    assert result.stop_code == DELIVERY_RUN_UNIT_FAILED
    assert calls == 1


def test_delivery_run_projects_preflight_failure_as_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["pending"])
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(
        "sikula_cli.delivery._run_next_delivery_unit",
        lambda *args, **kwargs: _unit_result(status, succeeded=False, ran=False),
    )

    result = _run_delivery_plan(_args(), {}, _context(), project_root=Path("/project"))

    assert result.started is False
    assert result.units_attempted == 0
    assert result.stop_code == DELIVERY_RUN_BLOCKED


def test_delivery_run_reset_failed_is_consumed_after_one_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"completed": 0}
    reset_values: list[bool] = []

    def current_status(*args, **kwargs):
        if state["completed"] == 0:
            return _status(["failed", "pending"])
        if state["completed"] == 1:
            return _status(["done", "pending"])
        return _status(["done", "done"])

    def run_next(run_args, *args, **kwargs):
        reset_values.append(run_args.reset_failed)
        status = current_status()
        selected = status.units[state["completed"]]
        state["completed"] += 1
        return SimpleNamespace(
            ran=True,
            succeeded=True,
            selected_unit=selected,
            child_task_id=f"child-{selected.id}",
            errors=[],
            warnings=[],
            message="Completed.",
        )

    completed_result = _run_result(completed=True, finalized=True)
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", current_status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)
    monkeypatch.setattr("sikula_cli.delivery._finalize_delivery_run", lambda *args, **kwargs: completed_result)

    result = _run_delivery_plan(
        _args(reset_failed=True),
        {},
        _context(),
        project_root=Path("/project"),
    )

    assert result.completed is True
    assert reset_values == [True, False]


def test_delivery_run_stops_before_unit_added_after_initial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _status(["pending", "pending"])
    amended = _status(["done", "superseded", "pending", "pending"])
    state = {"amended": False}
    executed: list[str] = []

    def current_status(*args, **kwargs):
        return amended if state["amended"] else initial

    def run_next(run_args, *args, **kwargs):
        unit_ids = kwargs["bounded_run_unit_ids"]
        assert unit_ids == frozenset({"unit-1", "unit-2"})
        status = current_status()
        selected = next(unit for unit in status.units if unit.status == "pending")
        issue = _bounded_delivery_run_snapshot_issue(unit_ids, selected)
        if issue is not None:
            return SimpleNamespace(
                ran=False,
                succeeded=False,
                selected_unit=selected,
                child_task_id=None,
                errors=[issue],
                warnings=[],
                message=issue.message,
            )
        executed.append(selected.id)
        state["amended"] = True
        return SimpleNamespace(
            ran=True,
            succeeded=True,
            selected_unit=selected,
            child_task_id=f"child-{selected.id}",
            errors=[],
            warnings=[],
            message="Completed.",
        )

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", current_status)
    monkeypatch.setattr("sikula_cli.delivery._run_next_delivery_unit", run_next)

    result = _run_delivery_plan(_args(), {}, _context(), project_root=Path("/project"))

    assert result.succeeded is True
    assert result.completed is False
    assert result.units_attempted == 1
    assert result.units_succeeded == 1
    assert result.stop_code == DELIVERY_RUN_UNIT_LIMIT_REACHED
    assert executed == ["unit-1"]


def test_delivery_run_detects_success_without_durable_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["pending"])
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(
        "sikula_cli.delivery._run_next_delivery_unit",
        lambda *args, **kwargs: _unit_result(status),
    )

    result = _run_delivery_plan(_args(), {}, _context(), project_root=Path("/project"))

    assert result.succeeded is False
    assert result.stop_code == DELIVERY_RUN_NO_PROGRESS
    assert [issue.code for issue in result.errors] == [DELIVERY_RUN_NO_PROGRESS]


def test_delivery_run_stops_when_status_becomes_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = _status(["pending"])
    invalid = _status(["pending"])
    invalid.valid = False
    invalid.errors = [DeliveryPlanIssue("error", "delivery.invalid", "Invalid.")]
    statuses = iter([valid, invalid])
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: next(statuses))

    result = _run_delivery_plan(_args(), {}, _context(), project_root=Path("/project"))

    assert result.succeeded is False
    assert result.stop_code == DELIVERY_RUN_BLOCKED
    assert [issue.code for issue in result.errors] == ["delivery.invalid"]


def test_finalize_delivery_run_routes_current_finalization_through_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _status(["done"], final_commit="a" * 40)
    seen: list[str] = []
    monkeypatch.setattr(
        "core.delivery_finalize.delivery_assembly_branch_is_symbolic",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr("core.delivery_finalize._branch_commit", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr("core.delivery_finalize._resolve_commit", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(
        "core.delivery_finalize.preview_delivery_finalize",
        lambda *args, **kwargs: pytest.fail("current finalization must bypass mutating finalize preview"),
    )

    def finalize(*args, **kwargs):
        seen.append("finalize")
        return SimpleNamespace(
            finalized=True,
            final_branch=status.final_branch,
            final_commit=status.final_commit,
            errors=[],
            warnings=[],
            message="Delivery finalization is current.",
        )

    monkeypatch.setattr("core.delivery_finalize.finalize_delivery_plan", finalize)
    monkeypatch.setattr(
        "core.delivery_progress.get_delivery_status",
        lambda *args, **kwargs: status,
    )

    result = _finalize_delivery_run(
        _args(),
        status=status,
        project_root=Path("/project"),
        max_units=1,
        max_elapsed_minutes=None,
        units_attempted=0,
        units_succeeded=0,
        last_unit=None,
        child_task_id=None,
    )

    assert result.succeeded is True
    assert result.finalized is True
    assert result.stop_code == DELIVERY_RUN_COMPLETED
    assert seen == ["finalize"]


def test_finalize_delivery_run_stops_on_blocked_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _status(["done"])
    issue = DeliveryPlanIssue("error", "delivery.finalize_blocked", "Blocked.")
    preview = SimpleNamespace(
        ready=False,
        final_branch="sikula/delivery/demo",
        final_commit=None,
        errors=[issue],
        warnings=[],
    )
    monkeypatch.setattr("core.delivery_finalize.preview_delivery_finalize", lambda *args, **kwargs: preview)

    result = _finalize_delivery_run(
        _args(),
        status=status,
        project_root=Path("/project"),
        max_units=1,
        max_elapsed_minutes=None,
        units_attempted=1,
        units_succeeded=1,
        last_unit=status.units[0],
        child_task_id="child-1",
    )

    assert result.succeeded is False
    assert result.stop_code == DELIVERY_RUN_FINALIZE_FAILED
    assert result.errors == [issue]


@pytest.mark.parametrize("finalized", [True, False])
def test_finalize_delivery_run_projects_mutating_result(
    monkeypatch: pytest.MonkeyPatch,
    finalized: bool,
) -> None:
    status = _status(["done"])
    preview = SimpleNamespace(
        ready=True,
        final_branch="sikula/delivery/demo",
        final_commit=None,
        errors=[],
        warnings=[],
    )
    final_result = SimpleNamespace(
        finalized=finalized,
        final_branch="sikula/delivery/demo",
        final_commit="b" * 40 if finalized else None,
        errors=[] if finalized else [DeliveryPlanIssue("error", "delivery.finalize_failed", "Failed.")],
        warnings=[],
        message="Finalization failed.",
    )
    updated_status = _status(["done"], final_commit="b" * 40) if finalized else status
    monkeypatch.setattr("core.delivery_finalize.preview_delivery_finalize", lambda *args, **kwargs: preview)
    monkeypatch.setattr("core.delivery_finalize.finalize_delivery_plan", lambda *args, **kwargs: final_result)
    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: updated_status)

    result = _finalize_delivery_run(
        _args(),
        status=status,
        project_root=Path("/project"),
        max_units=1,
        max_elapsed_minutes=30,
        units_attempted=1,
        units_succeeded=1,
        last_unit=status.units[0],
        child_task_id="child-1",
    )

    assert result.succeeded is finalized
    assert result.completed is finalized
    assert result.stop_code == (DELIVERY_RUN_COMPLETED if finalized else DELIVERY_RUN_FINALIZE_FAILED)


def test_cmd_delivery_run_json_is_one_compact_document(capsys: pytest.CaptureFixture[str]) -> None:
    result = DeliveryRunResult(
        plan_path="/project/plan.yaml",
        project_root="/project",
        valid=True,
        ready=True,
        dry_run=False,
        started=True,
        succeeded=True,
        completed=False,
        finalized=False,
        status="pending",
        max_units=1,
        max_elapsed_minutes=None,
        units_attempted=1,
        units_succeeded=1,
        last_unit=DeliveryStatusUnit(
            id="unit-1",
            status="done",
            title="Unit 1",
            task_path="units/unit-1.md",
            depends_on=[],
        ),
        child_task_id="child-1",
        stop_code=DELIVERY_RUN_UNIT_LIMIT_REACHED,
        progress_path="/project/.sikula/state/delivery/demo/progress.json",
        final_branch=None,
        final_commit=None,
        errors=[],
        warnings=[],
        message="Stopped safely.",
    )

    with patch("sikula_cli.delivery._run_delivery_plan", return_value=result):
        cmd_delivery_run(_args(json_output=True), {"project": {"root_path": "/project"}}, _context())

    payload = json.loads(capsys.readouterr().out)
    assert payload["succeeded"] is True
    assert payload["completed"] is False
    assert payload["units_succeeded"] == 1
    assert payload["progress_path"] == ".sikula/state/delivery/demo/progress.json"


def test_render_delivery_run_uses_public_projection() -> None:
    private_path = "/Users/alice/private/task.md"
    result = DeliveryRunResult(
        plan_path="/project/plan.yaml",
        project_root="/project",
        valid=False,
        ready=False,
        dry_run=False,
        started=False,
        succeeded=False,
        completed=False,
        finalized=False,
        status="failed",
        max_units=2,
        max_elapsed_minutes=30,
        units_attempted=0,
        units_succeeded=0,
        last_unit=None,
        child_task_id=None,
        stop_code=DELIVERY_RUN_UNIT_FAILED,
        progress_path="/project/progress.json",
        final_branch=None,
        final_commit=None,
        errors=[DeliveryPlanIssue("error", "delivery.failed", f"Inspect {private_path}.")],
        warnings=[],
        message=f"Blocked by {private_path}.",
    )

    output = render_delivery_run(result)

    assert private_path not in output
    assert "Status: blocked" in output
    assert "Stop code: delivery.run.unit_failed" in output


def test_delivery_run_issue_projection_does_not_treat_sibling_path_as_project_path() -> None:
    sibling_path = "/project-secret/plan.yaml"
    result = _run_result(
        ready=False,
        succeeded=False,
        errors=[
            DeliveryPlanIssue(
                "error",
                DELIVERY_RUN_SNAPSHOT_EXHAUSTED,
                f"Inspect {sibling_path}.",
                path=sibling_path,
            ),
            DeliveryPlanIssue("error", "delivery.relative", "Inspect the plan.", path="plan.yaml"),
        ],
    )

    payload = result.to_dict()
    projected = payload["errors"][0]

    assert projected["message"] == "<redacted>"
    assert projected["path"] == "<redacted>"
    assert payload["errors"][1]["path"] == "plan.yaml"
    assert sibling_path not in json.dumps(payload)
    assert ".-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("dry_run", "ready", "started", "succeeded", "completed", "expected"),
    [
        (True, True, False, False, False, "Status: ready"),
        (False, False, True, False, False, "Status: failed"),
        (False, True, True, True, False, "Status: stopped"),
        (False, True, True, True, True, "Status: done"),
    ],
)
def test_render_delivery_run_statuses(
    dry_run: bool,
    ready: bool,
    started: bool,
    succeeded: bool,
    completed: bool,
    expected: str,
) -> None:
    result = _run_result(
        dry_run=dry_run,
        ready=ready,
        started=started,
        succeeded=succeeded,
        completed=completed,
        finalized=completed,
    )

    assert expected in render_delivery_run(result)


def test_render_delivery_run_includes_optional_public_fields(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    plan_path = str(project_root / "plan.yaml")
    unit = DeliveryStatusUnit(
        id="unit-1",
        status="done",
        title="",
        task_path="units/unit-1.md",
        depends_on=[],
    )
    warning = DeliveryPlanIssue("warning", "delivery.warning", "Warning.", path=plan_path)
    result = DeliveryRunResult(
        plan_path=plan_path,
        project_root=str(project_root),
        valid=True,
        ready=True,
        dry_run=False,
        started=True,
        succeeded=True,
        completed=True,
        finalized=True,
        status="done",
        max_units=1,
        max_elapsed_minutes=30,
        units_attempted=1,
        units_succeeded=1,
        last_unit=unit,
        child_task_id="child-1",
        stop_code=DELIVERY_RUN_COMPLETED,
        progress_path=str(project_root / "progress.json"),
        final_branch="sikula/delivery/demo",
        final_commit="a" * 40,
        errors=[],
        warnings=[warning],
        message="Completed.",
    )

    output = render_delivery_run(result)

    assert "Last unit: unit-1\n" in output
    assert "Child task: child-1" in output
    assert "Elapsed limit: 30 minute(s)" in output
    assert "Final branch: sikula/delivery/demo" in output
    assert f"Final commit: {'a' * 40}" in output
    assert "Warnings:" in output
    assert "[./plan.yaml]" in output


def test_public_path_projects_or_sanitizes_paths() -> None:
    assert _public_path(None, Path("/project")) is None
    assert _public_path("/project/plan.yaml", Path("/project")) == "plan.yaml"
    assert _public_path("/outside/private/plan.yaml", Path("/project")) == "plan.yaml"
    assert _public_path("safe/path", None) == "safe/path"


def test_render_delivery_run_omits_absent_optional_fields_without_project_root() -> None:
    issue = DeliveryPlanIssue("error", "delivery.blocked", "Blocked.", path="plan.yaml")
    result = DeliveryRunResult(
        plan_path="plan.yaml",
        project_root=None,
        valid=False,
        ready=False,
        dry_run=True,
        started=False,
        succeeded=False,
        completed=False,
        finalized=False,
        status=None,
        max_units=1,
        max_elapsed_minutes=None,
        units_attempted=0,
        units_succeeded=0,
        last_unit=None,
        child_task_id=None,
        stop_code=DELIVERY_RUN_BLOCKED,
        progress_path=None,
        final_branch=None,
        final_commit=None,
        errors=[issue],
        warnings=[],
        message="Blocked.",
    )

    output = render_delivery_run(result)

    assert "Project root:" not in output
    assert "Plan status:" not in output
    assert "Progress:" not in output
    assert "[plan.yaml]" in output
