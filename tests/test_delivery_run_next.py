from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from core.delivery_plan import DeliveryPlanIssue
from core.delivery_progress import (
    DeliveryStatusUnit,
    acquire_delivery_progress_lock,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
)
from core.delivery_run_next import (
    DeliveryRunNextExecutionResult,
    DeliveryRunNextPreview,
    preview_delivery_run_next,
    render_delivery_run_next_execution,
    render_delivery_run_next_preview,
)
from core.delivery_run_next import _blocked_run_next_reason
from core.state import JsonStateStore
from sikula_cli.delivery import (
    DeliveryChildRunResult,
    DeliveryRunNextContext,
    _coerce_child_run_result,
    _delivery_child_run_args,
    _delivery_failure_code,
    _invoke_delivery_child_run,
    _system_exit_code,
    cmd_delivery_run_next,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_unit(root: Path, name: str, body: str) -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path) -> Path:
    unit_1 = _write_unit(root, "01-foundation.md", "# Unit 01\n\nPrivate task body.\n")
    unit_2 = _write_unit(root, "02-feature.md", "# Unit 02\n\nPrivate follow-up body.\n")
    plan = {
        "schema_version": 1,
        "plan_id": "delivery-run-next-demo",
        "title": "Delivery run-next demo",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/run-next-demo",
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


def _write_progress(root: Path, units: list[dict]) -> None:
    path = delivery_progress_path(root, "delivery-run-next-demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "delivery-run-next-demo",
                "units": units,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_next_args(plan_path: Path, *, dry_run: bool = False, json_output: bool = False) -> argparse.Namespace:
    return argparse.Namespace(plan_file=str(plan_path), dry_run=dry_run, json=json_output)


def _run_next_cfg(root: Path) -> dict:
    return {
        "project": {"root_path": str(root), "build_tool": "python"},
        "tasks": {"state_dir": str(root / ".sikula" / "state")},
    }


def _run_next_context(root: Path, runner) -> DeliveryRunNextContext:
    return DeliveryRunNextContext(
        run_task=runner,
        resolve_state_dir=lambda cfg: Path(cfg["tasks"]["state_dir"]),
    )


def _load_delivery_progress(root: Path) -> dict:
    return json.loads(delivery_progress_path(root, "delivery-run-next-demo").read_text(encoding="utf-8"))


def test_preview_delivery_run_next_selects_first_eligible_unit(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    result = preview_delivery_run_next(plan_path)

    assert result.valid is True
    assert result.ready is True
    assert result.dry_run is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "01-foundation"
    assert result.progress_exists is False
    assert "Private task body" not in json.dumps(result.to_dict())


def test_preview_delivery_run_next_respects_completed_dependencies(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "done"}])

    result = preview_delivery_run_next(plan_path)

    assert result.ready is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "02-feature"
    assert result.progress_exists is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("running", "delivery.running"),
        ("failed", "delivery.failed"),
        ("waiting", "delivery.waiting"),
        ("canceled", "delivery.canceled"),
        ("done", "delivery.complete"),
    ],
)
def test_preview_delivery_run_next_blocks_non_runnable_statuses(tmp_path: Path, status: str, code: str) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "done"},
            {"unit_id": "02-feature", "status": status},
        ],
    )

    result = preview_delivery_run_next(plan_path)

    assert result.ready is False
    assert result.selected_unit is None
    assert [issue.code for issue in result.errors] == [code]


def test_render_delivery_run_next_preview_is_safe_and_actionable(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    output = render_delivery_run_next_preview(preview_delivery_run_next(plan_path))

    assert "Status: ready" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Dry run: yes" in output
    assert "Private task body" not in output


def test_render_delivery_run_next_preview_includes_errors_and_warnings() -> None:
    result = DeliveryRunNextPreview(
        plan_path="/tmp/plan.yaml",
        project_root="/tmp/project",
        valid=False,
        ready=False,
        dry_run=True,
        status="pending",
        progress_exists=True,
        selected_unit=None,
        errors=[DeliveryPlanIssue("error", "delivery.no_eligible_unit", "No unit is eligible.", "units")],
        warnings=[DeliveryPlanIssue("warning", "progress.unit_unknown", "Progress references an unknown unit.")],
        message="Delivery plan is not ready to run.",
    )

    output = render_delivery_run_next_preview(result)

    assert "Errors:" in output
    assert "- delivery.no_eligible_unit [units]: No unit is eligible." in output
    assert "Warnings:" in output
    assert "- progress.unit_unknown: Progress references an unknown unit." in output


def test_render_delivery_run_next_execution_is_safe_and_actionable() -> None:
    selected_unit = DeliveryStatusUnit(
        id="01-foundation",
        status="done",
        title="Add foundation",
        task_path=".sikula/delivery/demo/units/01-foundation.md",
        depends_on=[],
    )
    result = DeliveryRunNextExecutionResult(
        plan_path="/tmp/plan.yaml",
        project_root="/tmp/project",
        valid=False,
        ran=True,
        succeeded=False,
        status="pending",
        progress_exists=True,
        selected_unit=selected_unit,
        child_task_id="task123",
        unit_status="failed",
        run_exit_code=1,
        progress_path="/tmp/project/.sikula/state/delivery/demo/progress.json",
        events_path="/tmp/project/.sikula/state/delivery/demo/events.jsonl",
        errors=[DeliveryPlanIssue("error", "delivery.failed", "Unit failed.", "units[0]")],
        warnings=[DeliveryPlanIssue("warning", "progress.unit_unknown", "Unknown progress unit.")],
        message="Delivery unit failed.",
    )

    output = render_delivery_run_next_execution(result)

    assert "Delivery run-next: /tmp/plan.yaml" in output
    assert "Status: failed" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Child task: task123" in output
    assert "Unit status: failed" in output
    assert "Run exit code: 1" in output
    assert "Errors:" in output
    assert "- delivery.failed [units[0]]: Unit failed." in output
    assert "Warnings:" in output
    assert "- progress.unit_unknown: Unknown progress unit." in output


def test_blocked_run_next_reason_falls_back_for_no_eligible_unit() -> None:
    assert _blocked_run_next_reason("pending") == (
        "delivery.no_eligible_unit",
        "Delivery plan has no eligible pending unit.",
    )


def test_cmd_delivery_run_next_runs_selected_unit_and_records_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        print("child run stdout")
        assert run_args.task_file == str((tmp_path / ".sikula/delivery/demo/units/01-foundation.md").resolve())
        assert run_args.no_isolate is False
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.done = True
        state.worktree_branch = "sikula/01-foundation-child"
        state.result_commit = "abc1234"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    cmd_delivery_run_next(
        _run_next_args(plan_path, json_output=True),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    captured = capsys.readouterr()
    assert "child run stdout" not in captured.out
    assert "child run stdout" in captured.err
    payload = json.loads(captured.out)
    assert payload["ran"] is True
    assert payload["succeeded"] is True
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "done"
    assert payload["selected_unit"]["eligible"] is False
    assert payload["selected_unit"]["branch"] == "sikula/01-foundation-child"
    assert payload["selected_unit"]["commit"] == "abc1234"
    assert payload["child_task_id"]
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"] == [
        {
            "branch": "sikula/01-foundation-child",
            "child_task_id": payload["child_task_id"],
            "commit": "abc1234",
            "completed_at": progress["units"][0]["completed_at"],
            "started_at": progress["units"][0]["started_at"],
            "status": "done",
            "unit_id": "01-foundation",
            "updated_at": progress["units"][0]["updated_at"],
        }
    ]
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.done"]


def test_cmd_delivery_run_next_preserves_unknown_progress_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {
                "unit_id": "old-renamed-unit",
                "status": "done",
                "child_task_id": "old-task",
                "branch": "sikula/old-unit",
                "commit": "oldcommit",
            }
        ],
    )

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.done = True
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    cmd_delivery_run_next(
        _run_next_args(plan_path, json_output=True),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["succeeded"] is True
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0] == {
        "branch": "sikula/old-unit",
        "child_task_id": "old-task",
        "commit": "oldcommit",
        "status": "done",
        "unit_id": "old-renamed-unit",
    }
    assert progress["units"][1]["unit_id"] == "01-foundation"
    assert progress["units"][1]["status"] == "done"


def test_cmd_delivery_run_next_requires_context_for_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(_run_next_args(plan_path), _run_next_cfg(tmp_path))

    assert exc.value.code == 2
    assert "requires the main Sikula command context" in capsys.readouterr().out


def test_cmd_delivery_run_next_records_failed_child_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.failed = True
        store.save(state)
        return DeliveryChildRunResult(exit_code=1, child_task_id=state.task_id)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is False
    assert payload["unit_status"] == "failed"
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["selected_unit"]["failure_code"] == "child_run_failed"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_failed"


def test_cmd_delivery_run_next_keeps_unit_retryable_when_child_run_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        raise SystemExit(2)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is False
    assert payload["status"] == "pending"
    assert payload["progress_exists"] is False
    assert payload["unit_status"] is None
    assert payload["run_exit_code"] == 2
    assert payload["selected_unit"]["status"] == "pending"
    assert payload["selected_unit"]["eligible"] is True
    assert payload["errors"][0]["code"] == "delivery.child_start_failed"
    assert not delivery_progress_path(tmp_path, "delivery-run-next-demo").exists()
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.start_failed"]
    status = get_delivery_status(plan_path)
    assert status.status == "pending"
    assert status.units[0].eligible is True


def test_cmd_delivery_run_next_marks_interrupted_child_run_terminal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        run_args.created_task_id = "interrupted-child"
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"] == [
        {
            "child_task_id": "interrupted-child",
            "completed_at": progress["units"][0]["completed_at"],
            "failure_code": "child_run_interrupted",
            "started_at": progress["units"][0]["started_at"],
            "status": "failed",
            "unit_id": "01-foundation",
            "updated_at": progress["units"][0]["updated_at"],
        }
    ]
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.failed"]
    status = get_delivery_status(plan_path)
    assert status.status == "failed"
    assert status.units[0].status == "failed"


def test_cmd_delivery_run_next_marks_unexpected_child_exception_terminal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        run_args.created_task_id = "crashed-child"
        raise RuntimeError("contract preflight exploded")

    with pytest.raises(RuntimeError, match="contract preflight exploded"):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_exception"
    assert progress["units"][0]["child_task_id"] == "crashed-child"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.failed"]
    status = get_delivery_status(plan_path)
    assert status.status == "failed"
    assert status.units[0].failure_code == "child_run_exception"


def test_cmd_delivery_run_next_rolls_back_unstarted_child_exception(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        raise RuntimeError("preflight exploded")

    with pytest.raises(RuntimeError, match="preflight exploded"):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert not delivery_progress_path(tmp_path, "delivery-run-next-demo").exists()
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.start_failed"]
    status = get_delivery_status(plan_path)
    assert status.status == "pending"
    assert status.units[0].eligible is True


def test_cmd_delivery_run_next_records_missing_child_state_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        return DeliveryChildRunResult(exit_code=0, child_task_id="missing-task")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_status"] == "failed"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["failure_code"] == "child_task_missing"


def test_cmd_delivery_run_next_records_incomplete_child_state_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    with pytest.raises(SystemExit):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_status"] == "failed"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["failure_code"] == "child_task_incomplete"


def test_cmd_delivery_run_next_rechecks_status_after_lock_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running"}])
    cfg = _run_next_cfg(tmp_path)
    called = False

    ready_preview = DeliveryRunNextPreview(
        plan_path=str(plan_path.resolve()),
        project_root=str(tmp_path.resolve()),
        valid=True,
        ready=True,
        dry_run=True,
        status="pending",
        progress_exists=False,
        selected_unit=DeliveryStatusUnit(
            id="01-foundation",
            status="pending",
            title="Add foundation",
            task_path=".sikula/delivery/demo/units/01-foundation.md",
            depends_on=[],
        ),
        errors=[],
        warnings=[],
        message="Ready.",
    )

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    monkeypatch.setattr("core.delivery_run_next.preview_delivery_run_next", lambda *args, **kwargs: ready_preview)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "delivery.running"


def test_cmd_delivery_run_next_rejects_existing_progress_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    called = False
    lock = acquire_delivery_progress_lock(tmp_path, "delivery-run-next-demo")

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    try:
        with pytest.raises(SystemExit) as exc:
            cmd_delivery_run_next(
                _run_next_args(plan_path, json_output=True),
                cfg,
                _run_next_context(tmp_path, runner),
            )
    finally:
        lock.release()

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "delivery.locked"


def test_delivery_child_run_helpers_cover_exit_and_result_shapes(tmp_path: Path) -> None:
    args = argparse.Namespace(json=False)
    cfg = {}
    root = tmp_path
    task_path = "task.md"

    def raises_none(run_args: argparse.Namespace, run_cfg: dict) -> int:
        raise SystemExit(None)

    def raises_text(run_args: argparse.Namespace, run_cfg: dict) -> int:
        raise SystemExit("bad")

    def raises_exception(run_args: argparse.Namespace, run_cfg: dict) -> int:
        raise RuntimeError("boom")

    def exits_after_creating_child(run_args: argparse.Namespace, run_cfg: dict) -> int:
        run_args.created_task_id = "created-child"
        raise SystemExit(0)

    assert (
        _invoke_delivery_child_run(
            args, cfg, _run_next_context(tmp_path, raises_none), root=root, task_path=task_path
        ).exit_code
        == 0
    )
    assert (
        _invoke_delivery_child_run(
            args, cfg, _run_next_context(tmp_path, raises_text), root=root, task_path=task_path
        ).exit_code
        == 1
    )
    exception_result = _invoke_delivery_child_run(
        args, cfg, _run_next_context(tmp_path, raises_exception), root=root, task_path=task_path
    )
    assert exception_result.exit_code == 1
    assert isinstance(exception_result.exception, RuntimeError)
    created_result = _invoke_delivery_child_run(
        args,
        cfg,
        _run_next_context(tmp_path, exits_after_creating_child),
        root=root,
        task_path=task_path,
    )
    assert created_result.exit_code == 0
    assert created_result.child_task_id == "created-child"
    assert _coerce_child_run_result(7).exit_code == 7
    assert _coerce_child_run_result(None).exit_code == 0
    assert _system_exit_code(SystemExit(3)) == 3
    assert _delivery_child_run_args(root=root, task_path=task_path).task_file == str((root / task_path).resolve())
    assert _delivery_failure_code(0, argparse.Namespace()) == "child_task_incomplete"


def test_cmd_delivery_run_next_uses_configured_project_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    _git_init(project)
    _git_init(other)
    plan_path = _write_plan(project)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            {"project": {"root_path": str(other)}},
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["errors"][0]["code"] == "plan.path_outside_project"


def test_cmd_delivery_run_next_rejects_configured_root_without_git(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = _write_plan(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            {"project": {"root_path": str(tmp_path)}},
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["errors"][0]["code"] == "project.git_root_missing"
