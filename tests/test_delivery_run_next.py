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
    _project_relative_path,
)
from core.delivery_run_next import _blocked_run_next_reason
from core.state import JsonStateStore, TaskState
from sikula_cli.delivery import (
    DeliveryChildRunResult,
    DeliveryChildLinkFailed,
    DeliveryRunNextContext,
    _child_delivery_result_finalized,
    _classify_delivery_child_run,
    _coerce_child_run_result,
    _dependency_commit_errors,
    _delivery_child_run_args,
    _delivery_child_resume_run_args,
    _git_commit_is_ancestor,
    _invoke_delivery_child_run_args,
    _invoke_delivery_child_run,
    _system_exit_code,
    cmd_delivery_run_next,
)


def test_delivery_run_next_register_parser_sets_agent_overrides() -> None:
    import sikula_cli.delivery as delivery_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    delivery_cli.register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            "run-next",
            ".sikula/delivery/demo/plan.yaml",
            "--agent-model",
            "analyst=gpt-5.5",
            "--agent-provider",
            "implementer=antigravity",
            "--agent-timeout",
            "implementer=2400",
        ]
    )

    assert args.command == "delivery"
    assert args.delivery_command == "run-next"
    assert args.plan_file == ".sikula/delivery/demo/plan.yaml"
    assert args.agent_model == ["analyst=gpt-5.5"]
    assert args.agent_provider == ["implementer=antigravity"]
    assert args.agent_timeout == ["implementer=2400"]


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _git_commit(root: Path, name: str = "tracked.txt", body: str = "tracked\n") -> str:
    path = root / name
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit",
            "-m",
            f"add {name}",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


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


def _resume_child_state(
    *,
    task_id: str = "resume-child",
    unit_id: str = "01-foundation",
    plan_id: str = "delivery-run-next-demo",
    plan_path: str | None = ".sikula/delivery/demo/plan.yaml",
) -> TaskState:
    return TaskState(
        task_id=task_id,
        task_description="resume child",
        delivery_plan_id=plan_id,
        delivery_unit_id=unit_id,
        delivery_plan_path=plan_path,
    )


def _record_resume_worktree(
    state: TaskState,
    root: Path,
    *,
    branch: str = "sikula/01-foundation-child",
) -> None:
    worktree_path = root / ".sikula" / "worktrees" / state.task_id / "project"
    worktree_path.mkdir(parents=True, exist_ok=True)
    state.worktree_path = str(worktree_path)
    state.worktree_branch = branch


def _write_transitive_plan(root: Path) -> Path:
    unit_1 = _write_unit(root, "01-foundation.md", "# Unit 01\n\nPrivate task body.\n")
    unit_2 = _write_unit(root, "02-noop.md", "# Unit 02\n\nNo-op follow-up.\n")
    unit_3 = _write_unit(root, "03-feature.md", "# Unit 03\n\nFeature body.\n")
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
                "id": "02-noop",
                "title": "No-op bridge",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_2,
                "depends_on": ["01-foundation"],
            },
            {
                "id": "03-feature",
                "title": "Add feature",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_3,
                "depends_on": ["02-noop"],
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


def _run_next_args(
    plan_path: Path,
    *,
    dry_run: bool = False,
    reset_failed: bool = False,
    json_output: bool = False,
    agent_model: list[str] | None = None,
    agent_provider: list[str] | None = None,
    agent_timeout: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file=str(plan_path),
        dry_run=dry_run,
        reset_failed=reset_failed,
        json=json_output,
        agent_model=agent_model,
        agent_provider=agent_provider,
        agent_timeout=agent_timeout,
    )


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


def test_cmd_delivery_run_next_dry_run_rejects_invalid_agent_override_before_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, agent_model=["bogus=x"]),
            _run_next_cfg(tmp_path),
        )

    assert exc.value.code == 1
    assert "Unknown agent 'bogus'" in capsys.readouterr().out
    assert not delivery_progress_path(tmp_path, "delivery-run-next-demo").exists()
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_rejects_invalid_agent_timeout_before_progress(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    child_called = False

    def runner(args: argparse.Namespace, cfg: dict) -> DeliveryChildRunResult:
        nonlocal child_called
        child_called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="child")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, agent_timeout=["implementer=abc"]),
            _run_next_cfg(tmp_path),
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert child_called is False
    assert "Invalid --agent-timeout value 'abc' for agent 'implementer': expected int" in capsys.readouterr().out
    assert not delivery_progress_path(tmp_path, "delivery-run-next-demo").exists()
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


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


def test_preview_delivery_run_next_preserves_component_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["components"] = [{"id": "api", "path": "packages/api", "stream": "app"}]
    plan["units"][0]["component"] = "api"
    plan["units"][0]["scope_paths"] = ["packages/api/src"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

    result = preview_delivery_run_next(plan_path)
    payload = result.to_dict()

    assert result.ready is True
    assert result.selected_unit is not None
    assert result.selected_unit.component == "api"
    assert result.selected_unit.scope_paths == ["packages/api/src"]
    assert payload["selected_unit"]["component"] == "api"
    assert payload["selected_unit"]["scope_paths"] == ["packages/api/src"]


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
    ("reset_failed", "expected_message"),
    [
        (False, "resume or reconciliation"),
        (True, "resume, retry, or reconciliation"),
    ],
)
def test_preview_delivery_run_next_selects_running_unit_with_child_task(
    tmp_path: Path, reset_failed: bool, expected_message: str
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"},
        ],
    )

    result = preview_delivery_run_next(plan_path, reset_failed=reset_failed)

    assert result.valid is True
    assert result.ready is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "01-foundation"
    assert result.selected_unit.status == "running"
    assert result.selected_unit.child_task_id == "resume-child"
    assert result.errors == []
    assert expected_message in result.message


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


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("running", "delivery.running"),
        ("failed", "delivery.failed_reset_unavailable"),
        ("waiting", "delivery.waiting"),
        ("canceled", "delivery.canceled"),
        ("done", "delivery.complete"),
        ("pending", "delivery.failed_reset_unavailable"),
    ],
)
def test_preview_delivery_run_next_blocks_non_runnable_statuses_with_reset_failed(
    tmp_path: Path, status: str, code: str
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "done"},
            {"unit_id": "02-feature", "status": status},
        ],
    )

    result = preview_delivery_run_next(plan_path, reset_failed=True)

    assert result.ready is False
    assert result.selected_unit is None
    assert [issue.code for issue in result.errors] == [code]


def test_preview_delivery_run_next_selects_failed_unit_with_reset_failed(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-abc"},
        ],
    )

    result = preview_delivery_run_next(plan_path, reset_failed=True)

    assert result.valid is True
    assert result.ready is True
    assert result.dry_run is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "01-foundation"
    assert result.progress_exists is True
    assert "Dry run selected failed delivery unit 01-foundation" in result.message


def test_render_delivery_run_next_preview_is_safe_and_actionable(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    output = render_delivery_run_next_preview(preview_delivery_run_next(plan_path))

    assert "Status: ready" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Dry run: yes" in output
    assert "Private task body" not in output


def test_render_delivery_run_next_preview_renders_child_task_id(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    output = render_delivery_run_next_preview(preview_delivery_run_next(plan_path, reset_failed=True))

    assert "Status: ready" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Child task: task-xyz" in output
    assert "Dry run: yes" in output
    assert "Dry run selected failed delivery unit 01-foundation" in output


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

    assert "Delivery run-next: plan.yaml" in output
    assert "Status: failed" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Child task: task123" in output
    assert "Unit status: failed" in output
    assert "Run exit code: 1" in output
    assert "Errors:" in output
    assert "- delivery.failed [units[0]]: Unit failed." in output
    assert "Warnings:" in output
    assert "- progress.unit_unknown: Unknown progress unit." in output


def test_delivery_run_next_preview_projects_paths_relative_to_project_root() -> None:
    result = DeliveryRunNextPreview(
        plan_path="/opt/project/plan.yaml",
        project_root="/opt/project",
        valid=True,
        ready=True,
        dry_run=True,
        status="pending",
        progress_exists=True,
        selected_unit=None,
        errors=[],
        warnings=[],
        message="Dry run message.",
    )

    output = render_delivery_run_next_preview(result)
    assert "Delivery run-next dry run: plan.yaml" in output
    assert "Project root: ." in output

    data = result.to_dict()
    assert data["project_root"] == "."
    assert data["plan_path"] == "plan.yaml"


def test_delivery_run_next_preview_null_paths() -> None:
    result = DeliveryRunNextPreview(
        plan_path="/opt/project/plan.yaml",
        project_root=None,
        valid=True,
        ready=True,
        dry_run=True,
        status="pending",
        progress_exists=True,
        selected_unit=None,
        errors=[],
        warnings=[],
        message="Dry run message.",
    )

    output = render_delivery_run_next_preview(result)
    assert "Delivery run-next dry run: /opt/project/plan.yaml" in output
    assert "Project root" not in output

    data = result.to_dict()
    assert data["project_root"] is None
    assert data["plan_path"] == "/opt/project/plan.yaml"


def test_delivery_run_next_execution_projects_paths_relative_to_project_root() -> None:
    result = DeliveryRunNextExecutionResult(
        plan_path="/opt/project/plan.yaml",
        project_root="/opt/project",
        valid=True,
        ran=True,
        succeeded=True,
        status="done",
        progress_exists=True,
        selected_unit=None,
        child_task_id=None,
        unit_status=None,
        run_exit_code=0,
        progress_path="/opt/project/progress.json",
        events_path="/opt/project/events.jsonl",
        errors=[],
        warnings=[],
        message="Done.",
    )

    output = render_delivery_run_next_execution(result)
    assert "Delivery run-next: plan.yaml" in output
    assert "Project root: ." in output
    assert "Progress: progress.json" in output
    assert "Events: events.jsonl" in output

    data = result.to_dict()
    assert data["project_root"] == "."
    assert data["plan_path"] == "plan.yaml"
    assert data["progress_path"] == "progress.json"
    assert data["events_path"] == "events.jsonl"


def test_delivery_run_next_execution_null_paths() -> None:
    result = DeliveryRunNextExecutionResult(
        plan_path="/opt/project/plan.yaml",
        project_root=None,
        valid=True,
        ran=True,
        succeeded=True,
        status="done",
        progress_exists=False,
        selected_unit=None,
        child_task_id=None,
        unit_status=None,
        run_exit_code=0,
        progress_path=None,
        events_path=None,
        errors=[],
        warnings=[],
        message="Done.",
    )

    output = render_delivery_run_next_execution(result)
    assert "Delivery run-next: /opt/project/plan.yaml" in output
    assert "Project root" not in output
    assert "Progress:" not in output
    assert "Events:" not in output

    data = result.to_dict()
    assert data["project_root"] is None
    assert data["plan_path"] == "/opt/project/plan.yaml"
    assert data["progress_path"] is None
    assert data["events_path"] is None


@pytest.mark.parametrize(
    ("status", "reset_failed", "expected_code", "expected_message"),
    [
        (
            "failed",
            False,
            "delivery.failed",
            "Delivery plan has failed unit(s); rerun with --reset-failed to select a failed unit with a linked child task.",
        ),
        (
            "failed",
            True,
            "delivery.failed_reset_unavailable",
            "No failed delivery unit with a linked child task is available for --reset-failed.",
        ),
        ("running", False, "delivery.running", "Delivery plan already has a running unit."),
        ("running", True, "delivery.running", "Delivery plan already has a running unit."),
        ("waiting", False, "delivery.waiting", "Delivery plan is waiting for human input."),
        ("waiting", True, "delivery.waiting", "Delivery plan is waiting for human input."),
        ("canceled", False, "delivery.canceled", "Delivery plan has canceled unit(s)."),
        ("canceled", True, "delivery.canceled", "Delivery plan has canceled unit(s)."),
        ("done", False, "delivery.complete", "Delivery plan is already complete."),
        ("done", True, "delivery.complete", "Delivery plan is already complete."),
        ("pending", False, "delivery.no_eligible_unit", "Delivery plan has no eligible pending unit."),
        (
            "pending",
            True,
            "delivery.failed_reset_unavailable",
            "No failed delivery unit with a linked child task is available for --reset-failed.",
        ),
    ],
)
def test_blocked_run_next_reason_maps_statuses_and_flags(
    status: str, reset_failed: bool, expected_code: str, expected_message: str
) -> None:
    assert _blocked_run_next_reason(status, reset_failed=reset_failed) == (expected_code, expected_message)


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
        assert run_args.agent_model == ["analyst=gpt-5.5"]
        assert run_args.agent_provider == ["implementer=antigravity", "fixer=antigravity"]
        assert run_args.agent_timeout == ["implementer=2400"]
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        store.save(state)
        run_args.created_task_id = state.task_id
        run_args.delivery_child_created_callback(state.task_id)
        state.done = True
        state.worktree_branch = "sikula/01-foundation-child"
        state.result_commit = "abc1234"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    cmd_delivery_run_next(
        _run_next_args(
            plan_path,
            json_output=True,
            agent_model=["analyst=gpt-5.5"],
            agent_provider=["implementer=antigravity", "fixer=antigravity"],
            agent_timeout=["implementer=2400"],
        ),
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
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.running", "unit.child_linked", "unit.done"]
    assert parsed_events[1]["unit_id"] == "01-foundation"
    assert parsed_events[1]["status"] == "running"
    assert parsed_events[1]["child_task_id"] == payload["child_task_id"]
    assert parsed_events[2]["unit_id"] == "01-foundation"
    assert parsed_events[2]["child_task_id"] == payload["child_task_id"]


def test_cmd_delivery_run_next_reports_child_link_failure_if_parent_progress_link_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    created_child_ids: list[str] = []

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        store.save(state)
        run_args.created_task_id = state.task_id
        created_child_ids.append(state.task_id)
        run_args.delivery_child_created_callback(state.task_id)
        state.done = True
        state.worktree_branch = "sikula/01-foundation-child"
        state.result_commit = "abc1234"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    import core.delivery_progress as delivery_progress

    original_write_delivery_progress = delivery_progress.write_delivery_progress
    write_calls = {"count": 0}

    def fail_after_first_write(path: Path, progress) -> None:
        write_calls["count"] += 1
        if write_calls["count"] == 2:
            raise RuntimeError("parent progress write blocked")
        return original_write_delivery_progress(path, progress)

    monkeypatch.setattr(delivery_progress, "write_delivery_progress", fail_after_first_write)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["valid"] is False
    assert payload["succeeded"] is False
    assert payload["child_task_id"] is not None
    assert payload["child_task_id"] in created_child_ids
    assert payload["unit_status"] is None
    assert payload["status"] == "pending"
    assert payload["progress_exists"] is False
    assert payload["selected_unit"]["status"] == "pending"
    assert payload["selected_unit"]["eligible"] is True
    assert payload["errors"][0]["code"] == "delivery.child_link_failed"
    assert payload["errors"][0]["message"] == (
        "Delivery child task was created, but parent progress could not record the child task id. "
        "Child agents were not started; inspect the child task state before retrying."
    )
    assert "parent progress write blocked" not in payload["message"]
    assert "parent progress write blocked" not in payload["errors"][0]["message"]

    assert not delivery_progress_path(tmp_path, "delivery-run-next-demo").exists()
    status = get_delivery_status(plan_path)
    assert status.status == "pending"
    assert status.units[0].eligible is True
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.running", "unit.child_link_failed"]
    assert json.loads(events[1])["child_task_id"] == payload["child_task_id"]


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


def test_cmd_delivery_run_next_does_not_mark_unfinalized_child_run_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    unfinalized_worktree = tmp_path / ".sikula" / "worktrees" / "child"

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.done = True
        state.worktree_path = str(unfinalized_worktree)
        state.worktree_base = str(unfinalized_worktree)
        state.worktree_branch = "sikula/01-foundation-child"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

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
    assert payload["run_exit_code"] == 0
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["selected_unit"]["failure_code"] == "child_run_unfinalized"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_unfinalized"
    assert progress["units"][0]["branch"] == "sikula/01-foundation-child"
    assert "commit" not in progress["units"][0]


def test_cmd_delivery_run_next_allows_noop_dependency_without_result_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "done"}])
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_file == str((tmp_path / ".sikula/delivery/demo/units/02-feature.md").resolve())
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
    assert payload["selected_unit"]["id"] == "02-feature"
    assert payload["selected_unit"]["status"] == "done"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0] == {"unit_id": "01-foundation", "status": "done"}
    assert progress["units"][1]["unit_id"] == "02-feature"
    assert progress["units"][1]["status"] == "done"


def test_cmd_delivery_run_next_blocks_dependent_unit_when_dependency_commit_is_unapplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "done", "commit": "deadbeef", "branch": "sikula/unit-01"}],
    )
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["selected_unit"]["id"] == "02-feature"
    assert payload["errors"][0]["code"] == "delivery.dependency_commit_unapplied"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["commit"] == "deadbeef"
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_blocks_dependent_unit_when_dependency_commit_is_unapplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "done", "commit": "deadbeef", "branch": "sikula/unit-01"}],
    )

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            _run_next_cfg(tmp_path),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["selected_unit"]["id"] == "02-feature"
    assert payload["errors"][0]["code"] == "delivery.dependency_commit_unapplied"
    assert "01-foundation" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_reports_running_recovery_even_when_dependency_commit_unapplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "done", "commit": "deadbeef", "branch": "sikula/unit-01"},
            {"unit_id": "02-feature", "status": "running", "child_task_id": "resume-child"},
        ],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state(task_id="resume-child", unit_id="02-feature")
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    cmd_delivery_run_next(
        _run_next_args(plan_path, dry_run=True, json_output=True),
        cfg,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["selected_unit"]["id"] == "02-feature"
    assert payload["selected_unit"]["status"] == "running"
    assert payload["selected_unit"]["child_task_id"] == "resume-child"
    assert payload["errors"] == []
    assert "resume or reconciliation" in payload["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_blocks_running_recovery_missing_child_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            _run_next_cfg(tmp_path),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "running"
    assert payload["selected_unit"]["child_task_id"] == "resume-child"
    assert payload["errors"][0]["code"] == "delivery.child_task_missing"
    assert "was not found in the configured state directory" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_blocks_running_recovery_child_metadata_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state(plan_id="other-plan")
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            cfg,
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["errors"][0]["code"] == "delivery.child_task_metadata_mismatch"
    assert "metadata does not match the parent plan" in payload["errors"][0]["message"]
    assert "other-plan" not in json.dumps(payload)
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_blocks_transitive_dependency_commit_when_noop_bridge_is_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_transitive_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {
                "unit_id": "01-foundation",
                "status": "done",
                "commit": "deadbeef",
                "branch": "sikula/unit-01",
            },
            {"unit_id": "02-noop", "status": "done"},
        ],
    )
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["selected_unit"]["id"] == "03-feature"
    assert payload["errors"][0]["code"] == "delivery.dependency_commit_unapplied"
    assert "01-foundation" in payload["errors"][0]["message"]
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["commit"] == "deadbeef"
    assert progress["units"][1] == {"unit_id": "02-noop", "status": "done"}
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_blocks_transitive_dependency_commit_when_noop_bridge_is_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_transitive_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {
                "unit_id": "01-foundation",
                "status": "done",
                "commit": "deadbeef",
                "branch": "sikula/unit-01",
            },
            {"unit_id": "02-noop", "status": "done"},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            _run_next_cfg(tmp_path),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["selected_unit"]["id"] == "03-feature"
    assert payload["errors"][0]["code"] == "delivery.dependency_commit_unapplied"
    assert "01-foundation" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_runs_dependent_unit_when_dependency_commit_is_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    dependency_commit = _git_commit(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "done", "commit": dependency_commit, "branch": "sikula/unit-01"}],
    )
    cfg = _run_next_cfg(tmp_path)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_file == str((tmp_path / ".sikula/delivery/demo/units/02-feature.md").resolve())
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
    assert payload["selected_unit"]["id"] == "02-feature"
    assert payload["selected_unit"]["status"] == "done"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["unit_id"] == "01-foundation"
    assert progress["units"][1]["unit_id"] == "02-feature"
    assert progress["units"][1]["status"] == "done"


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
        status="running",
        progress_exists=False,
        selected_unit=DeliveryStatusUnit(
            id="01-foundation",
            status="pending",
            title="Add foundation",
            task_path=".sikula/delivery/demo/units/01-foundation.md",
            depends_on=[],
        ),
        errors=[DeliveryPlanIssue("error", "delivery.running", "Running unit already exists.")],
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
    assert payload["errors"][0]["code"] == "delivery.running_child_missing"


def test_cmd_delivery_run_next_resumes_non_terminal_running_child_unit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)

    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = False
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_file is None
        assert run_args.task_id == "resume-child"
        assert run_args.delivery_plan_id is None
        assert run_args.delivery_unit_id is None
        assert run_args.delivery_plan_path is None
        assert run_args.delivery_child_created_callback is None
        state = store.load("resume-child")
        assert state is not None
        state.done = True
        state.worktree_branch = "sikula/01-foundation-child"
        state.result_commit = "abc123"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id="resume-child")

    cmd_delivery_run_next(
        _run_next_args(
            plan_path,
            json_output=True,
            agent_model=["analyst=gpt-5.5"],
            agent_provider=["implementer=antigravity"],
            agent_timeout=["implementer=2400"],
        ),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is True
    assert payload["unit_status"] == "done"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "done"
    assert payload["selected_unit"]["eligible"] is False
    assert payload["message"] == "Delivery unit 01-foundation resumed and completed."
    assert payload["child_task_id"] == "resume-child"
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["progress_path"] == ".sikula/state/delivery/delivery-run-next-demo/progress.json"
    assert payload["events_path"] == ".sikula/state/delivery/delivery-run-next-demo/events.jsonl"
    assert str(tmp_path) not in payload["plan_path"]
    assert str(tmp_path) not in payload["progress_path"]
    assert str(tmp_path) not in payload["events_path"]
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "done"
    assert progress["units"][0]["child_task_id"] == "resume-child"
    assert progress["units"][0]["branch"] == "sikula/01-foundation-child"
    assert progress["units"][0]["commit"] == "abc123"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.resume_intent", "unit.done"]
    assert parsed_events[0]["unit_id"] == "01-foundation"
    assert parsed_events[0]["status"] == "running"
    assert parsed_events[1]["unit_id"] == "01-foundation"
    assert parsed_events[1]["child_task_id"] == "resume-child"


def test_cmd_delivery_run_next_blocks_running_child_without_isolated_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = False
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="resume-child")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(_run_next_args(plan_path, json_output=True), cfg, _run_next_context(tmp_path, runner))

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["unit_status"] == "running"
    assert payload["child_task_id"] == "resume-child"
    assert payload["selected_unit"]["status"] == "running"
    assert payload["errors"][0]["code"] == "delivery.child_worktree_missing"
    assert "has no available isolated worktree path recorded" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_resumes_running_child_and_records_failed_parent_unit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        state = store.load("resume-child")
        assert state is not None
        state.done = True
        store.save(state)
        return DeliveryChildRunResult(exit_code=1, child_task_id=run_args.created_task_id)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(_run_next_args(plan_path, json_output=True), cfg, _run_next_context(tmp_path, runner))

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is False
    assert payload["unit_status"] == "failed"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["selected_unit"]["failure_code"] == "child_run_failed"
    assert payload["message"] == "Delivery unit 01-foundation resumed and failed; inspect child task state."
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_failed"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.resume_intent", "unit.failed"]


def test_cmd_delivery_run_next_resumes_running_child_and_marks_interrupted_parent_unit(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = False
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_file is None
        assert run_args.task_id == "resume-child"
        assert run_args.delivery_plan_id is None
        assert run_args.delivery_unit_id is None
        assert run_args.delivery_plan_path is None
        assert run_args.delivery_child_created_callback is None
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_interrupted"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.resume_intent", "unit.failed"]


def test_cmd_delivery_run_next_resumes_running_child_and_marks_exception_parent_unit(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = False
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_file is None
        assert run_args.task_id == "resume-child"
        assert run_args.delivery_plan_id is None
        assert run_args.delivery_unit_id is None
        assert run_args.delivery_plan_path is None
        assert run_args.delivery_child_created_callback is None
        raise RuntimeError("child task crashed during resume")

    with pytest.raises(RuntimeError, match="child task crashed during resume"):
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_exception"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.resume_intent", "unit.failed"]


def test_cmd_delivery_run_next_blocks_running_unit_without_child_task_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running"}])
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["errors"][0]["code"] == "delivery.running_child_missing"
    assert payload["message"] == (
        "Delivery unit 01-foundation is running but has no child task id; "
        "inspect parent delivery progress before retrying."
    )
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["progress_path"] == ".sikula/state/delivery/delivery-run-next-demo/progress.json"
    assert payload["events_path"] == ".sikula/state/delivery/delivery-run-next-demo/events.jsonl"


def test_cmd_delivery_run_next_blocks_running_unit_with_invalid_child_task_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "bad:child"}])
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["errors"][0]["code"] == "delivery.child_task_missing"


def test_cmd_delivery_run_next_blocks_running_unit_with_missing_child_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["errors"][0]["code"] == "delivery.child_task_missing"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivery_plan_id", "other-plan"),
        ("delivery_unit_id", "02-feature"),
        ("delivery_plan_path", ".sikula/delivery/other/plan.yaml"),
        ("delivery_plan_id", None),
        ("delivery_unit_id", None),
        ("delivery_plan_path", None),
    ],
)
def test_cmd_delivery_run_next_blocks_running_unit_with_mismatched_child_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str | None,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    setattr(state, field, value)
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["errors"][0]["code"] == "delivery.child_task_metadata_mismatch"
    assert payload["message"] == (
        "Delivery unit 01-foundation is linked to child task resume-child, but the child task delivery metadata "
        "does not match the parent plan and unit; inspect child task state before retrying."
    )
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["child_task_id"] == "resume-child"
    assert "other-plan" not in json.dumps(payload)
    assert ".sikula/delivery/other/plan.yaml" not in json.dumps(payload)
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


@pytest.mark.parametrize(
    ("progress_payload", "expected_code", "expected_message"),
    [
        (
            {"unit_id": "01-foundation", "status": "running"},
            "delivery.running_child_missing",
            "Delivery unit 01-foundation is running but has no child task id; "
            "inspect parent delivery progress before retrying.",
        ),
        (
            {"unit_id": "01-foundation", "status": "running", "child_task_id": ""},
            "units[0].child_task_id.invalid_type",
            "Delivery plan is not ready to run.",
        ),
        (
            {"unit_id": "01-foundation", "status": "running", "child_task_id": None},
            "delivery.running_child_missing",
            "Delivery unit 01-foundation is running but has no child task id; "
            "inspect parent delivery progress before retrying.",
        ),
    ],
)
def test_cmd_delivery_run_next_blocks_running_unit_without_nonempty_child_task_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    progress_payload: dict[str, str | None],
    expected_code: str,
    expected_message: str,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [progress_payload])
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["errors"][0]["code"] == expected_code
    assert payload["message"] == expected_message


@pytest.mark.parametrize(
    (
        "child_done",
        "child_failed",
        "result_commit",
        "worktree_path",
        "worktree_base",
        "expected_unit_status",
        "expected_failure_code",
        "expected_message",
        "expected_exit",
        "expected_succeeded",
    ),
    [
        (
            True,
            False,
            None,
            None,
            None,
            "done",
            None,
            "Delivery unit 01-foundation reconciled terminal child task as done.",
            None,
            True,
        ),
        (
            True,
            False,
            "abc123",
            None,
            None,
            "done",
            None,
            "Delivery unit 01-foundation reconciled terminal child task as done.",
            None,
            True,
        ),
        (
            False,
            True,
            "abc123",
            None,
            None,
            "failed",
            "child_run_failed",
            "Delivery unit 01-foundation reconciled terminal child task as failed; inspect child task state.",
            1,
            False,
        ),
        (
            True,
            False,
            None,
            "sikula/worktrees/01-foundation-child",
            None,
            "failed",
            "child_run_unfinalized",
            "Delivery unit 01-foundation reconciled terminal child task as failed; inspect child task state.",
            1,
            False,
        ),
        (
            True,
            False,
            "abc123",
            "sikula/worktrees/01-foundation-child",
            None,
            "done",
            None,
            "Delivery unit 01-foundation reconciled terminal child task as done.",
            None,
            True,
        ),
        (
            True,
            False,
            None,
            None,
            "sikula/worktrees-base/01-foundation-child",
            "failed",
            "child_run_unfinalized",
            "Delivery unit 01-foundation reconciled terminal child task as failed; inspect child task state.",
            1,
            False,
        ),
    ],
)
def test_cmd_delivery_run_next_reconciles_running_unit_with_terminal_child_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    child_done: bool,
    child_failed: bool,
    result_commit: str | None,
    worktree_path: str | None,
    worktree_base: str | None,
    expected_unit_status: str,
    expected_failure_code: str | None,
    expected_message: str,
    expected_exit: int | None,
    expected_succeeded: bool,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = child_done
    state.failed = child_failed
    state.result_commit = result_commit
    state.worktree_branch = "sikula/01-foundation-child"
    state.worktree_path = worktree_path
    state.worktree_base = worktree_base
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    if expected_exit is None:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )
        payload = json.loads(capsys.readouterr().out)
    else:
        with pytest.raises(SystemExit) as exc:
            cmd_delivery_run_next(
                _run_next_args(plan_path, json_output=True),
                cfg,
                _run_next_context(tmp_path, runner),
            )

        assert exc.value.code == expected_exit
        payload = json.loads(capsys.readouterr().out)

    assert called is False
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["progress_path"] == ".sikula/state/delivery/delivery-run-next-demo/progress.json"
    assert payload["events_path"] == ".sikula/state/delivery/delivery-run-next-demo/events.jsonl"
    assert payload["ran"] is True
    assert payload["valid"] is True
    assert payload["succeeded"] is expected_succeeded
    assert payload["unit_status"] == expected_unit_status
    assert payload["child_task_id"] == "resume-child"
    assert payload["run_exit_code"] is None
    assert payload["message"] == expected_message
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == expected_unit_status
    assert payload["selected_unit"]["branch"] == "sikula/01-foundation-child"
    assert str(tmp_path) not in payload["plan_path"]
    assert str(tmp_path) not in payload["progress_path"]
    assert str(tmp_path) not in payload["events_path"]
    if expected_failure_code is None:
        assert "failure_code" not in payload["selected_unit"]
    else:
        assert payload["selected_unit"]["failure_code"] == expected_failure_code
    if result_commit is None:
        assert payload["selected_unit"].get("commit") is None
    else:
        assert payload["selected_unit"]["commit"] == result_commit
    assert payload["errors"] == []
    assert payload["warnings"] == []
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == expected_unit_status
    assert progress["units"][0]["unit_id"] == "01-foundation"
    assert progress["units"][0]["child_task_id"] == "resume-child"
    assert progress["units"][0]["branch"] == "sikula/01-foundation-child"
    if result_commit is None:
        assert "commit" not in progress["units"][0]
    else:
        assert progress["units"][0]["commit"] == result_commit
    if expected_failure_code is None:
        assert "failure_code" not in progress["units"][0]
    else:
        assert progress["units"][0]["failure_code"] == expected_failure_code
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == [
        "unit.reconcile_intent",
        f"unit.{expected_unit_status}",
    ]
    assert parsed_events[0]["unit_id"] == "01-foundation"
    assert parsed_events[0]["status"] == "running"
    assert parsed_events[1]["unit_id"] == "01-foundation"
    assert parsed_events[1]["status"] == expected_unit_status
    assert parsed_events[1]["child_task_id"] == "resume-child"


def test_cmd_delivery_run_next_reset_failed_retries_failed_running_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.failed = True
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    child_called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal child_called
        child_called = True
        assert run_args.task_file is None
        assert run_args.task_id == "resume-child"
        assert run_args.created_task_id == "resume-child"
        assert run_args.reset_failed is True
        state = store.load("resume-child")
        assert state is not None
        state.failed = False
        state.done = True
        state.worktree_branch = "sikula/01-foundation-child"
        state.result_commit = "abc123"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id="resume-child")

    cmd_delivery_run_next(
        _run_next_args(plan_path, reset_failed=True, json_output=True),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    assert child_called is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is True
    assert payload["unit_status"] == "done"
    assert payload["child_task_id"] == "resume-child"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "done"
    assert payload["selected_unit"]["branch"] == "sikula/01-foundation-child"
    assert payload["message"] == "Delivery unit 01-foundation retried and completed."

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "done"
    assert progress["units"][0]["child_task_id"] == "resume-child"
    assert progress["units"][0]["commit"] == "abc123"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.retry_intent", "unit.done"]
    assert parsed_events[0]["unit_id"] == "01-foundation"
    assert parsed_events[0]["status"] == "running"
    assert parsed_events[0]["child_task_id"] == "resume-child"
    assert parsed_events[1]["unit_id"] == "01-foundation"
    assert parsed_events[1]["status"] == "done"
    assert parsed_events[1]["child_task_id"] == "resume-child"


def test_cmd_delivery_run_next_reset_failed_blocks_failed_running_child_without_isolated_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}],
    )
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.failed = True
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="resume-child")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["unit_status"] == "running"
    assert payload["child_task_id"] == "resume-child"
    assert payload["selected_unit"]["status"] == "running"
    assert payload["errors"][0]["code"] == "delivery.child_worktree_missing"
    assert "has no available isolated worktree path recorded" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_marks_resumed_running_child_failed_when_child_state_missing_after_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"}])
    cfg = _run_next_cfg(tmp_path)
    store = JsonStateStore(Path(cfg["tasks"]["state_dir"]))
    state = _resume_child_state()
    state.done = False
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        run_store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        run_store.delete("resume-child")
        return DeliveryChildRunResult(exit_code=0, child_task_id=run_args.created_task_id)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(_run_next_args(plan_path, json_output=True), cfg, _run_next_context(tmp_path, runner))

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is False
    assert payload["selected_unit"]["failure_code"] == "child_task_missing"
    assert payload["unit_status"] == "failed"
    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_task_missing"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    assert [json.loads(event)["event_type"] for event in events] == ["unit.resume_intent", "unit.failed"]


def test_cmd_delivery_run_next_blocks_multiple_running_units_before_pending_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "running", "child_task_id": "resume-child"},
            {"unit_id": "02-feature", "status": "running", "child_task_id": "resume-child-2"},
        ],
    )
    cfg = _run_next_cfg(tmp_path)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert called is False
    assert payload["selected_unit"] is None
    assert payload["errors"][0]["code"] == "delivery.running_unit_ambiguous"
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["progress_path"] == ".sikula/state/delivery/delivery-run-next-demo/progress.json"
    assert payload["events_path"] == ".sikula/state/delivery/delivery-run-next-demo/events.jsonl"


@pytest.mark.parametrize("reset_failed", [False, True])
def test_delivery_child_resume_run_args_uses_task_id_resume_shape(reset_failed: bool) -> None:
    args = _delivery_child_resume_run_args(
        child_task_id="resume-child",
        created_task_id="resume-child",
        agent_model=["analyst=gpt-5.5"],
        agent_provider=["implementer=antigravity"],
        agent_timeout=["implementer=2400"],
        reset_failed=reset_failed,
    )

    assert args.task_file is None
    assert args.task_id == "resume-child"
    assert args.task_file_pos is None
    assert args.no_isolate is False
    assert args.reset_failed is reset_failed
    assert args.delivery_plan_id is None
    assert args.delivery_unit_id is None
    assert args.delivery_plan_path is None
    assert args.delivery_child_created_callback is None
    assert args.created_task_id == "resume-child"
    assert args.agent_model == ["analyst=gpt-5.5"]
    assert args.agent_provider == ["implementer=antigravity"]
    assert args.agent_timeout == ["implementer=2400"]


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

    def raises_child_link_failed(run_args: argparse.Namespace, run_cfg: dict) -> int:
        run_args.created_task_id = "link-failed-child"
        raise DeliveryChildLinkFailed()

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
    link_failed_result = _invoke_delivery_child_run(
        args,
        cfg,
        _run_next_context(tmp_path, raises_child_link_failed),
        root=root,
        task_path=task_path,
    )
    assert link_failed_result.exit_code == 1
    assert link_failed_result.child_link_failed is True
    assert link_failed_result.child_task_id == "link-failed-child"
    assert _coerce_child_run_result(7).exit_code == 7
    assert _coerce_child_run_result(None).exit_code == 0
    assert _system_exit_code(SystemExit(3)) == 3
    assert _delivery_child_run_args(root=root, task_path=task_path).task_file == str((root / task_path).resolve())


def test_delivery_child_run_args_cover_task_id_resume_invocation(tmp_path: Path) -> None:
    args = argparse.Namespace(json=False)

    def plain_runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.task_id == "resume-child"
        assert run_args.task_file is None
        return DeliveryChildRunResult(exit_code=4, child_task_id=run_args.created_task_id)

    resume_args = _delivery_child_resume_run_args(child_task_id="resume-child", created_task_id="resume-child")

    result = _invoke_delivery_child_run_args(
        args,
        cfg={},
        context=_run_next_context(tmp_path, plain_runner),
        run_args=resume_args,
    )

    assert result.exit_code == 4
    assert result.child_task_id == "resume-child"


def test_invoke_delivery_child_run_args_handles_result_shapes(tmp_path: Path) -> None:
    args = argparse.Namespace(json=False)
    cfg = {}

    resume_args = _delivery_child_resume_run_args(child_task_id="resume-child", created_task_id="resume-child")

    def raises_none(run_args: argparse.Namespace, run_cfg: dict) -> None:
        raise SystemExit(None)

    def raises_text(run_args: argparse.Namespace, run_cfg: dict) -> None:
        raise SystemExit("bad")

    def raises_interrupt(run_args: argparse.Namespace, run_cfg: dict) -> None:
        raise KeyboardInterrupt

    def raises_exception(run_args: argparse.Namespace, run_cfg: dict) -> None:
        raise RuntimeError("boom")

    def raises_child_link_failed(run_args: argparse.Namespace, run_cfg: dict) -> None:
        run_args.created_task_id = "resume-child"
        raise DeliveryChildLinkFailed()

    def returns_child_run_result(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        return DeliveryChildRunResult(exit_code=3, child_task_id=run_args.created_task_id)

    assert (
        _invoke_delivery_child_run_args(
            args, cfg, _run_next_context(tmp_path, raises_none), run_args=resume_args
        ).exit_code
        == 0
    )
    assert (
        _invoke_delivery_child_run_args(
            args, cfg, _run_next_context(tmp_path, raises_text), run_args=resume_args
        ).exit_code
        == 1
    )
    assert (
        _invoke_delivery_child_run_args(
            args, cfg, _run_next_context(tmp_path, raises_interrupt), run_args=resume_args
        ).interrupted
        is True
    )
    exception_result = _invoke_delivery_child_run_args(
        args, cfg, _run_next_context(tmp_path, raises_exception), run_args=resume_args
    )
    assert exception_result.exit_code == 1
    assert isinstance(exception_result.exception, RuntimeError)
    link_failed_result = _invoke_delivery_child_run_args(
        args, cfg, _run_next_context(tmp_path, raises_child_link_failed), run_args=resume_args
    )
    assert link_failed_result.exit_code == 1
    assert link_failed_result.child_link_failed is True
    assert link_failed_result.child_task_id == "resume-child"
    returned_result = _invoke_delivery_child_run_args(
        args, cfg, _run_next_context(tmp_path, returns_child_run_result), run_args=resume_args
    )
    assert returned_result.exit_code == 3
    assert returned_result.child_task_id == "resume-child"


def test_delivery_child_run_args_keeps_child_created_callback() -> None:
    def callback(_task_id: str) -> None:
        pass

    args = _delivery_child_run_args(
        root=Path("/fake/root"),
        task_path="tasks/unit.md",
        delivery_child_created_callback=callback,
    )
    assert args.delivery_child_created_callback is callback


def test_child_delivery_result_finalized_distinguishes_commits_noops_and_preserved_worktrees(tmp_path: Path) -> None:
    assert _child_delivery_result_finalized(argparse.Namespace(result_commit="abc123", worktree_path="wt")) is True
    assert _child_delivery_result_finalized(argparse.Namespace(result_commit=None, worktree_path=None)) is True
    assert (
        _child_delivery_result_finalized(
            argparse.Namespace(result_commit=None, worktree_path=str(tmp_path / "wt"), worktree_base=None)
        )
        is False
    )
    assert (
        _child_delivery_result_finalized(
            argparse.Namespace(
                result_commit=None,
                worktree_path=None,
                worktree_base="sikula/worktrees-base/01-foundation-child",
            )
        )
        is False
    )
    assert (
        _child_delivery_result_finalized(
            argparse.Namespace(
                result_commit="abc123",
                worktree_path="wt",
                worktree_base="sikula/worktrees-base/01-foundation-child",
            )
        )
        is True
    )


@pytest.mark.parametrize(
    ("child_result", "child_state", "unit_status", "failure_code"),
    [
        (
            DeliveryChildRunResult(exit_code=130, interrupted=True),
            None,
            "failed",
            "child_run_interrupted",
        ),
        (
            DeliveryChildRunResult(exit_code=1, exception=RuntimeError("boom")),
            None,
            "failed",
            "child_run_exception",
        ),
        (
            DeliveryChildRunResult(exit_code=0),
            None,
            "failed",
            "child_task_missing",
        ),
        (
            DeliveryChildRunResult(exit_code=1),
            argparse.Namespace(done=True, result_commit="abc123", worktree_path=None, worktree_base=None),
            "failed",
            "child_run_failed",
        ),
        (
            DeliveryChildRunResult(exit_code=1),
            argparse.Namespace(done=True, result_commit=None, worktree_path="wt", worktree_base=None),
            "failed",
            "child_run_failed",
        ),
        (
            DeliveryChildRunResult(exit_code=0),
            argparse.Namespace(done=False, result_commit=None, worktree_path=None, worktree_base=None),
            "failed",
            "child_task_incomplete",
        ),
        (
            DeliveryChildRunResult(exit_code=0),
            argparse.Namespace(done=True, result_commit=None, worktree_path="wt", worktree_base=None),
            "failed",
            "child_run_unfinalized",
        ),
        (
            DeliveryChildRunResult(exit_code=0),
            argparse.Namespace(done=True, result_commit="abc123", worktree_path="wt", worktree_base=None),
            "done",
            None,
        ),
        (
            DeliveryChildRunResult(exit_code=0),
            argparse.Namespace(done=True, result_commit=None, worktree_path=None, worktree_base=None),
            "done",
            None,
        ),
    ],
)
def test_classify_delivery_child_run_state_matrix(
    child_result: DeliveryChildRunResult,
    child_state: argparse.Namespace | None,
    unit_status: str,
    failure_code: str | None,
) -> None:
    classification = _classify_delivery_child_run(child_result, child_state)

    assert classification.unit_status == unit_status
    assert classification.failure_code == failure_code


def test_dependency_commit_errors_ignore_noop_unfinished_or_missing_dependency_units(tmp_path: Path) -> None:
    selected = DeliveryStatusUnit(
        id="02-feature",
        status="pending",
        title="Feature",
        task_path=".sikula/delivery/demo/units/02-feature.md",
        depends_on=["missing", "01-foundation", "noop"],
    )
    status = argparse.Namespace(
        units=[
            DeliveryStatusUnit(
                id="01-foundation",
                status="pending",
                title="Foundation",
                task_path=".sikula/delivery/demo/units/01-foundation.md",
                depends_on=[],
            ),
            DeliveryStatusUnit(
                id="noop",
                status="done",
                title="No-op",
                task_path=".sikula/delivery/demo/units/noop.md",
                depends_on=[],
            ),
        ]
    )

    assert _dependency_commit_errors(status, selected, tmp_path) == []


def test_git_commit_is_ancestor_handles_git_execution_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr("sikula_cli.delivery.subprocess.run", fail_run)

    assert _git_commit_is_ancestor(tmp_path, "abc123") is False


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


@pytest.mark.parametrize(
    ("plan_id", "unit_id", "plan_path"),
    [
        ("p123", "u456", "relative/path/plan.yaml"),
        (None, None, None),
        ("p123", None, None),
        (None, "u456", None),
        (None, None, "relative/path/plan.yaml"),
    ],
)
def test_delivery_child_run_args_metadata(plan_id: str | None, unit_id: str | None, plan_path: str | None) -> None:
    args = _delivery_child_run_args(
        root=Path("/fake/root"),
        task_path="tasks/unit.md",
        delivery_plan_id=plan_id,
        delivery_unit_id=unit_id,
        delivery_plan_path=plan_path,
    )
    assert args.delivery_plan_id == plan_id
    assert args.delivery_unit_id == unit_id
    assert args.delivery_plan_path == plan_path


def test_invoke_delivery_child_run_forwards_metadata(tmp_path: Path) -> None:
    captured_args = None

    def callback(_task_id: str) -> None:
        pass

    def dummy_runner(args: argparse.Namespace, cfg: dict) -> DeliveryChildRunResult:
        nonlocal captured_args
        captured_args = args
        return DeliveryChildRunResult(exit_code=0)

    args = argparse.Namespace(
        agent_model=["analyst=gpt-5"],
        agent_provider=None,
        agent_timeout=None,
    )

    _invoke_delivery_child_run(
        args,
        cfg={},
        context=_run_next_context(tmp_path, dummy_runner),
        root=tmp_path,
        task_path="task.md",
        delivery_plan_id="my-plan-id",
        delivery_unit_id="my-unit-id",
        delivery_plan_path="my-plan-path",
        delivery_child_created_callback=callback,
    )

    assert captured_args is not None
    assert captured_args.delivery_plan_id == "my-plan-id"
    assert captured_args.delivery_unit_id == "my-unit-id"
    assert captured_args.delivery_plan_path == "my-plan-path"
    assert captured_args.delivery_child_created_callback is callback


def test_cmd_delivery_run_next_computes_and_forwards_metadata(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    captured_args = None

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal captured_args
        captured_args = run_args
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.done = True
        state.worktree_branch = "branch"
        state.result_commit = "commit"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    cmd_delivery_run_next(
        _run_next_args(plan_path),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    assert captured_args is not None
    assert captured_args.delivery_plan_id == "delivery-run-next-demo"
    assert captured_args.delivery_unit_id == "01-foundation"
    expected_path = plan_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    assert captured_args.delivery_plan_path == expected_path


def test_cmd_delivery_run_next_omits_unsafe_metadata_plan_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)

    captured_args = None

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal captured_args
        captured_args = run_args
        store = JsonStateStore(Path(run_cfg["tasks"]["state_dir"]))
        state = store.create("child task")
        state.done = True
        state.worktree_branch = "branch"
        state.result_commit = "commit"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    from dataclasses import replace

    orig_get_delivery_status = get_delivery_status

    def mock_get_status(*args, **kwargs):
        status = orig_get_delivery_status(*args, **kwargs)
        return replace(status, plan_path="/outside/project/root/plan.yaml")

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", mock_get_status)

    cmd_delivery_run_next(
        _run_next_args(plan_path),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    assert captured_args is not None
    assert captured_args.delivery_plan_id == "delivery-run-next-demo"
    assert captured_args.delivery_unit_id == "01-foundation"
    assert captured_args.delivery_plan_path is None


def test_cmd_delivery_run_next_blocks_missing_child_task_state_for_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, lambda *args: None),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["succeeded"] is False
    assert payload["valid"] is False
    assert payload["status"] == "failed"
    assert payload["unit_status"] == "failed"
    assert payload["child_task_id"] == "task-xyz"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "delivery.child_task_missing"
    assert "was not found in the configured state directory" in payload["errors"][0]["message"]


def test_cmd_delivery_run_next_blocks_child_task_metadata_mismatch_for_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "other-plan"
    store.save(state)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, lambda *args: None),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["succeeded"] is False
    assert payload["valid"] is False
    assert payload["status"] == "failed"
    assert payload["unit_status"] == "failed"
    assert payload["child_task_id"] == "task-xyz"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "delivery.child_task_metadata_mismatch"
    assert "metadata does not match the parent plan" in payload["errors"][0]["message"]


def test_cmd_delivery_run_next_blocks_failed_child_retry_without_isolated_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    state.worktree_branch = "sikula/pre-worktree-child"
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="task-xyz")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["unit_status"] == "failed"
    assert payload["child_task_id"] == "task-xyz"
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["errors"][0]["code"] == "delivery.child_worktree_missing"
    assert "has no available isolated worktree path recorded" in payload["errors"][0]["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_blocks_failed_child_retry_with_stale_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    state.worktree_path = str(tmp_path / ".sikula" / "worktrees" / "missing-child")
    state.worktree_branch = "sikula/stale-child"
    state.failed = True
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="task-xyz")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["errors"][0]["code"] == "delivery.child_worktree_missing"
    assert store.load("task-xyz").failed is True
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_blocks_failed_child_retry_with_stale_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    state.worktree_path = str(tmp_path / ".sikula" / "worktrees" / "missing-child")
    state.worktree_branch = "sikula/stale-child"
    state.failed = True
    store.save(state)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, reset_failed=True, json_output=True),
            cfg,
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["selected_unit"]["child_task_id"] == "task-xyz"
    assert payload["errors"][0]["code"] == "delivery.child_worktree_missing"
    assert store.load("task-xyz").failed is True
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_dry_run_allows_completed_failed_child_retry_without_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    state.done = True
    state.result_commit = "abc1234"
    store.save(state)

    cmd_delivery_run_next(
        _run_next_args(plan_path, dry_run=True, reset_failed=True, json_output=True),
        cfg,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"
    assert payload["selected_unit"]["child_task_id"] == "task-xyz"
    assert payload["errors"] == []
    assert "selected failed delivery unit" in payload["message"]
    assert not delivery_events_path(tmp_path, "delivery-run-next-demo").exists()


def test_cmd_delivery_run_next_reconciles_completed_failed_child_retry_without_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    state.done = True
    state.result_commit = "abc1234"
    state.worktree_branch = "sikula/01-foundation-manual"
    store.save(state)
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="task-xyz")

    cmd_delivery_run_next(
        _run_next_args(plan_path, reset_failed=True, json_output=True),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is True
    assert payload["unit_status"] == "done"
    assert payload["selected_unit"]["status"] == "done"
    assert payload["selected_unit"]["commit"] == "abc1234"
    assert payload["message"] == "Delivery unit 01-foundation reconciled terminal child task as done."
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.reconcile_intent", "unit.done"]


def test_cmd_delivery_run_next_runs_failed_child_retry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    child_called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal child_called
        child_called = True
        assert run_args.task_id == "task-xyz"
        assert run_args.created_task_id == "task-xyz"
        assert run_args.reset_failed is True
        progress = _load_delivery_progress(tmp_path)
        assert progress["units"][0]["status"] == "running"
        assert progress["units"][0]["child_task_id"] == "task-xyz"
        state.done = True
        state.worktree_branch = "sikula/01-foundation-retry"
        state.result_commit = "abc1234"
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id="task-xyz")

    cmd_delivery_run_next(
        _run_next_args(plan_path, reset_failed=True, json_output=True),
        cfg,
        _run_next_context(tmp_path, runner),
    )

    assert child_called is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is True
    assert payload["valid"] is True
    assert payload["status"] == "pending"
    assert payload["unit_status"] == "done"
    assert payload["child_task_id"] == "task-xyz"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "done"
    assert payload["selected_unit"]["branch"] == "sikula/01-foundation-retry"

    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.retry_intent", "unit.done"]
    assert parsed_events[0]["unit_id"] == "01-foundation"
    assert parsed_events[0]["child_task_id"] == "task-xyz"
    assert parsed_events[1]["unit_id"] == "01-foundation"
    assert parsed_events[1]["child_task_id"] == "task-xyz"


def test_cmd_delivery_run_next_reports_failed_child_retry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        state.failed = True
        store.save(state)
        return DeliveryChildRunResult(exit_code=1, child_task_id="task-xyz")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["succeeded"] is False
    assert payload["valid"] is True
    assert payload["status"] == "failed"
    assert payload["unit_status"] == "failed"
    assert payload["child_task_id"] == "task-xyz"
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["status"] == "failed"

    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.retry_intent", "unit.failed"]
    assert parsed_events[1]["failure_code"] == "child_run_failed"


def test_cmd_delivery_run_next_retries_failed_child_and_marks_exception_parent_unit(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        raise RuntimeError("child task crashed during retry")

    with pytest.raises(RuntimeError, match="child task crashed during retry"):
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_exception"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.retry_intent", "unit.failed"]


def test_cmd_delivery_run_next_retries_failed_child_and_handles_interrupt(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    state_dir = Path(cfg["tasks"]["state_dir"])
    store = JsonStateStore(state_dir)
    state = store.create("task-xyz")
    state.task_id = "task-xyz"
    state.delivery_plan_id = "delivery-run-next-demo"
    state.delivery_unit_id = "01-foundation"
    state.delivery_plan_path = ".sikula/delivery/demo/plan.yaml"
    _record_resume_worktree(state, tmp_path)
    store.save(state)

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    progress = _load_delivery_progress(tmp_path)
    assert progress["units"][0]["status"] == "failed"
    assert progress["units"][0]["failure_code"] == "child_run_interrupted"
    events = delivery_events_path(tmp_path, "delivery-run-next-demo").read_text(encoding="utf-8").splitlines()
    parsed_events = [json.loads(event) for event in events]
    assert [event["event_type"] for event in parsed_events] == ["unit.retry_intent", "unit.failed"]


def test_project_relative_path(tmp_path: Path) -> None:
    assert _project_relative_path("/foo/bar", None) == "/foo/bar"
    assert _project_relative_path("/foo/bar", tmp_path) == "bar"

    sub = tmp_path / "sub"
    sub.mkdir()
    file_path = sub / "file.txt"
    assert _project_relative_path(str(file_path), tmp_path) == "sub/file.txt"


def test_sanitize_issue() -> None:
    from core.delivery_run_next import _sanitize_issue

    issue = DeliveryPlanIssue(
        "error",
        "code",
        "Issue in /opt/project/plan.yaml, /opt/project/progress.json, and /opt/project/events.jsonl",
        "/opt/project/events.jsonl",
    )

    # Without project root, returns unchanged
    assert (
        _sanitize_issue(
            issue, None, "/opt/project/plan.yaml", "/opt/project/progress.json", "/opt/project/events.jsonl"
        )
        is issue
    )
    assert (
        _sanitize_issue(issue, ".", "/opt/project/plan.yaml", "/opt/project/progress.json", "/opt/project/events.jsonl")
        is issue
    )

    # Sanitizes absolute paths inside messages and path
    sanitized = _sanitize_issue(
        issue, "/opt/project", "/opt/project/plan.yaml", "/opt/project/progress.json", "/opt/project/events.jsonl"
    )
    assert sanitized.message == "Issue in plan.yaml, progress.json, and events.jsonl"
    assert sanitized.path == "events.jsonl"

    # Sanitizes project root strings not caught by exact matches
    issue2 = DeliveryPlanIssue(
        "error", "code", "Error loading /opt/project/some/other/file.txt", "/opt/project/some/other/file.txt"
    )
    sanitized2 = _sanitize_issue(
        issue2, "/opt/project", "/opt/project/plan.yaml", "/opt/project/progress.json", "/opt/project/events.jsonl"
    )
    assert sanitized2.message == "Error loading some/other/file.txt"
    assert sanitized2.path == "some/other/file.txt"


def test_cmd_delivery_run_next_blocks_failed_plan_without_reset_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed", "child_task_id": "task-xyz"},
        ],
    )

    child_called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal child_called
        child_called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="new-task")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert child_called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["status"] == "failed"
    assert payload["selected_unit"] is None
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "delivery.failed"
    assert "rerun with --reset-failed" in payload["errors"][0]["message"]


def test_cmd_delivery_run_next_blocks_with_reset_failed_when_reset_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed"},
        ],
    )

    child_called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal child_called
        child_called = True
        return DeliveryChildRunResult(exit_code=0, child_task_id="new-task")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert child_called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["status"] == "failed"
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["project_root"] == "."
    assert payload["progress_path"] is None
    assert payload["events_path"] is None
    assert payload["selected_unit"] is None
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "delivery.failed_reset_unavailable"
    assert (
        "No failed delivery unit with a linked child task is available for --reset-failed."
        in payload["errors"][0]["message"]
    )
    assert str(tmp_path) not in json.dumps(payload)


def test_cmd_delivery_run_next_reset_failed_recheck_block_uses_safe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    cfg = _run_next_cfg(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "failed"},
        ],
    )
    ready_preview = DeliveryRunNextPreview(
        plan_path=str(plan_path.resolve()),
        project_root=str(tmp_path.resolve()),
        valid=True,
        ready=True,
        dry_run=True,
        status="pending",
        progress_exists=True,
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
    called = False

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        nonlocal called
        called = True
        return DeliveryChildRunResult(exit_code=0)

    monkeypatch.setattr("core.delivery_run_next.preview_delivery_run_next", lambda *args, **kwargs: ready_preview)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            _run_next_args(plan_path, reset_failed=True, json_output=True),
            cfg,
            _run_next_context(tmp_path, runner),
        )

    assert exc.value.code == 1
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["valid"] is False
    assert payload["status"] == "failed"
    assert payload["plan_path"] == ".sikula/delivery/demo/plan.yaml"
    assert payload["project_root"] == "."
    assert payload["progress_path"] == ".sikula/state/delivery/delivery-run-next-demo/progress.json"
    assert payload["events_path"] == ".sikula/state/delivery/delivery-run-next-demo/events.jsonl"
    assert payload["selected_unit"] is None
    assert payload["errors"][0]["code"] == "delivery.failed_reset_unavailable"
    assert str(tmp_path) not in json.dumps(payload)
