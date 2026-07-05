"""Delivery-plan CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys


def register_parser(subparsers) -> argparse.ArgumentParser:
    delivery_p = subparsers.add_parser("delivery", help="Inspect and run delivery plans")
    delivery_sub = delivery_p.add_subparsers(dest="delivery_command")

    delivery_check_p = delivery_sub.add_parser("check", help="Check a delivery plan file")
    delivery_check_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_check_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    delivery_status_p = delivery_sub.add_parser("status", help="Show delivery plan progress")
    delivery_status_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_status_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    delivery_run_next_p = delivery_sub.add_parser("run-next", help="Run the next eligible delivery unit")
    delivery_run_next_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_run_next_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview the next eligible unit without running agents or writing delivery progress",
    )
    delivery_run_next_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    return delivery_p


def cmd_delivery_check(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_plan import check_delivery_plan_file, render_delivery_plan_check

    result = check_delivery_plan_file(args.plan_file)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_delivery_plan_check(result), end="")
    if not result.valid:
        sys.exit(1)


def cmd_delivery_status(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_progress import get_delivery_status, render_delivery_status

    result = get_delivery_status(args.plan_file)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_delivery_status(result), end="")
    if not result.valid:
        sys.exit(1)


@dataclass(frozen=True)
class DeliveryChildRunResult:
    exit_code: int
    child_task_id: str | None = None
    interrupted: bool = False
    exception: BaseException | None = None


@dataclass(frozen=True)
class DeliveryRunNextContext:
    run_task: Callable[[argparse.Namespace, dict], DeliveryChildRunResult | int]
    resolve_state_dir: Callable[[dict], Path]


def cmd_delivery_run_next(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext | None = None,
) -> None:
    from core.delivery_run_next import (
        preview_delivery_run_next,
        render_delivery_run_next_execution,
        render_delivery_run_next_preview,
    )

    project_root_raw = cfg.get("project", {}).get("root_path") if isinstance(cfg, dict) else None
    project_root = Path(project_root_raw).resolve() if project_root_raw else None
    if getattr(args, "dry_run", False):
        result = preview_delivery_run_next(args.plan_file, project_root=project_root)
        _print_delivery_result(result, json_output=args.json, render=render_delivery_run_next_preview)
        if not result.ready:
            sys.exit(1)
        return

    if context is None:
        print("delivery run-next execution requires the main Sikula command context.")
        sys.exit(2)

    result = _run_next_delivery_unit(args, cfg, context, project_root=project_root)
    _print_delivery_result(result, json_output=args.json, render=render_delivery_run_next_execution)
    if not result.succeeded:
        sys.exit(1)


def _run_next_delivery_unit(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    *,
    project_root: Path | None,
):
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_progress import (
        DeliveryProgressLockError,
        acquire_delivery_progress_lock,
        append_delivery_progress_event,
        delivery_events_path,
        delivery_progress_path,
        get_delivery_status,
        make_delivery_progress_event,
        make_delivery_unit_progress,
        read_delivery_progress,
        select_next_delivery_unit,
        upsert_delivery_unit_progress,
        write_delivery_progress,
    )
    from core.delivery_run_next import (
        DeliveryRunNextExecutionResult,
        _blocked_run_next_reason,
        preview_delivery_run_next,
    )
    from core.state import JsonStateStore

    preflight = preview_delivery_run_next(args.plan_file, project_root=project_root)
    if not preflight.ready:
        return _execution_result_from_preview(preflight, ran=False)

    status = get_delivery_status(args.plan_file, project_root=project_root)
    if status.plan is None or status.project_root is None:
        return _execution_result_from_preview(preflight, ran=False)

    root = Path(status.project_root).resolve()
    plan_id = status.plan.plan_id
    progress_path = delivery_progress_path(root, plan_id)
    events_path = delivery_events_path(root, plan_id)

    try:
        lock = acquire_delivery_progress_lock(root, plan_id, owner="delivery.run-next")
    except DeliveryProgressLockError:
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=preflight.selected_unit,
            child_task_id=None,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[
                DeliveryPlanIssue(
                    "error",
                    "delivery.locked",
                    "Delivery progress is locked by another run-next process.",
                )
            ],
            warnings=status.warnings,
            message="Delivery run-next is blocked by an existing progress lock.",
        )

    with lock:
        status = get_delivery_status(args.plan_file, project_root=project_root)
        errors = list(status.errors)
        selected_unit = select_next_delivery_unit(status)
        if status.plan is None or status.project_root is None:
            return _execution_result_from_status(
                status,
                ran=False,
                selected_unit=None,
                progress_path=None,
                events_path=None,
                errors=errors,
                message="Delivery plan is not ready to run.",
            )
        if selected_unit is None:
            code, message = _blocked_run_next_reason(status.status)
            errors.append(DeliveryPlanIssue("error", code, message))
            return _execution_result_from_status(
                status,
                ran=False,
                selected_unit=None,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=errors,
                message=message,
            )

        dependency_errors = _dependency_commit_errors(status, selected_unit, root)
        if dependency_errors:
            errors.extend(dependency_errors)
            return _execution_result_from_status(
                status,
                ran=False,
                selected_unit=selected_unit,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=errors,
                message="Delivery unit dependencies are not applied to the current checkout.",
            )

        progress = _progress_for_update(status, progress_path, read_delivery_progress=read_delivery_progress)
        progress_before_start = progress
        progress_existed_before_start = progress_path.exists()
        running_unit = make_delivery_unit_progress(selected_unit.id, "running")
        progress = upsert_delivery_unit_progress(progress, running_unit)
        write_delivery_progress(progress_path, progress)
        append_delivery_progress_event(
            events_path,
            make_delivery_progress_event(plan_id, "unit.running", unit=running_unit),
        )

        child_result = _invoke_delivery_child_run(
            args,
            cfg,
            context,
            root=root,
            task_path=selected_unit.task_path,
        )
        state_dir = context.resolve_state_dir(cfg)
        store = JsonStateStore(state_dir)
        child_task_id = child_result.child_task_id
        if child_task_id is None:
            _restore_delivery_progress(progress_path, progress_before_start, existed=progress_existed_before_start)
            start_failed_unit = make_delivery_unit_progress(selected_unit.id, "pending")
            append_delivery_progress_event(
                events_path,
                make_delivery_progress_event(plan_id, "unit.start_failed", unit=start_failed_unit),
            )
            if child_result.interrupted:
                raise KeyboardInterrupt
            if child_result.exception is not None:
                raise child_result.exception
            updated_status = get_delivery_status(args.plan_file, project_root=project_root)
            updated_unit = _status_unit_by_id(updated_status, selected_unit.id) or selected_unit
            errors = list(updated_status.errors)
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.child_start_failed",
                    "Delivery unit did not start; fix the child run setup or preflight error and retry.",
                )
            )
            return DeliveryRunNextExecutionResult(
                plan_path=status.plan_path,
                project_root=status.project_root,
                valid=False,
                ran=True,
                succeeded=False,
                status=updated_status.status,
                progress_exists=updated_status.progress_exists,
                selected_unit=updated_unit,
                child_task_id=None,
                unit_status=None,
                run_exit_code=child_result.exit_code,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=errors,
                warnings=updated_status.warnings,
                message=f"Delivery unit {selected_unit.id} did not start; fix setup and retry.",
            )
        child_state = store.load(child_task_id) if child_task_id else None
        child_done = bool(child_state and child_state.done)
        unit_status = (
            "done" if child_result.exit_code == 0 and child_done and not child_result.interrupted else "failed"
        )
        failure_code = (
            None
            if unit_status == "done"
            else _delivery_failure_code(
                child_result.exit_code,
                child_state,
                interrupted=child_result.interrupted,
                exception=child_result.exception is not None,
            )
        )
        terminal_unit = make_delivery_unit_progress(
            selected_unit.id,
            unit_status,
            child_task_id=child_task_id,
            branch=getattr(child_state, "worktree_branch", None) if child_state else None,
            commit=getattr(child_state, "result_commit", None) if child_state else None,
            failure_code=failure_code,
        )
        progress = upsert_delivery_unit_progress(progress, terminal_unit)
        write_delivery_progress(progress_path, progress)
        append_delivery_progress_event(
            events_path,
            make_delivery_progress_event(plan_id, f"unit.{unit_status}", unit=terminal_unit),
        )
        if child_result.interrupted:
            raise KeyboardInterrupt
        if child_result.exception is not None:
            raise child_result.exception
        updated_status = get_delivery_status(args.plan_file, project_root=project_root)
        updated_unit = _status_unit_by_id(updated_status, selected_unit.id) or selected_unit

        message = (
            f"Delivery unit {selected_unit.id} completed."
            if unit_status == "done"
            else f"Delivery unit {selected_unit.id} failed; inspect child task state."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=updated_status.valid,
            ran=True,
            succeeded=unit_status == "done" and updated_status.valid,
            status=updated_status.status,
            progress_exists=updated_status.progress_exists,
            selected_unit=updated_unit,
            child_task_id=child_task_id,
            unit_status=unit_status,
            run_exit_code=child_result.exit_code,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=updated_status.errors,
            warnings=updated_status.warnings,
            message=message,
        )


def _execution_result_from_preview(preview, *, ran: bool):
    from core.delivery_run_next import DeliveryRunNextExecutionResult

    return DeliveryRunNextExecutionResult(
        plan_path=preview.plan_path,
        project_root=preview.project_root,
        valid=preview.valid,
        ran=ran,
        succeeded=False,
        status=preview.status,
        progress_exists=preview.progress_exists,
        selected_unit=preview.selected_unit,
        child_task_id=None,
        unit_status=None,
        run_exit_code=None,
        progress_path=None,
        events_path=None,
        errors=preview.errors,
        warnings=preview.warnings,
        message=preview.message,
    )


def _execution_result_from_status(
    status,
    *,
    ran: bool,
    selected_unit,
    progress_path: str | None,
    events_path: str | None,
    errors: list,
    message: str,
):
    from core.delivery_run_next import DeliveryRunNextExecutionResult

    return DeliveryRunNextExecutionResult(
        plan_path=status.plan_path,
        project_root=status.project_root,
        valid=not errors,
        ran=ran,
        succeeded=False,
        status=status.status,
        progress_exists=status.progress_exists,
        selected_unit=selected_unit,
        child_task_id=None,
        unit_status=None,
        run_exit_code=None,
        progress_path=progress_path,
        events_path=events_path,
        errors=errors,
        warnings=status.warnings,
        message=message,
    )


def _progress_from_status(status):
    from core.delivery_progress import DeliveryProgress, DeliveryUnitProgress

    units: list[DeliveryUnitProgress] = []
    for unit in status.units:
        if not _status_unit_has_progress(unit):
            continue
        units.append(
            DeliveryUnitProgress(
                unit_id=unit.id,
                status=unit.status,
                child_task_id=unit.child_task_id,
                branch=unit.branch,
                commit=unit.commit,
                waiting_reason=unit.waiting_reason,
                failure_code=unit.failure_code,
                started_at=unit.started_at,
                completed_at=unit.completed_at,
                updated_at=unit.updated_at,
            )
        )
    return DeliveryProgress(schema_version=1, plan_id=status.plan.plan_id, units=units)


def _progress_for_update(status, progress_path: Path, *, read_delivery_progress: Callable):
    if status.progress_exists:
        progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
        if progress is not None and not errors:
            return progress
    return _progress_from_status(status)


def _dependency_commit_errors(status, selected_unit, root: Path):
    from core.delivery_plan import DeliveryPlanIssue

    units_by_id = {unit.id: unit for unit in status.units}
    errors: list[DeliveryPlanIssue] = []
    for dependency in selected_unit.depends_on:
        dependency_unit = units_by_id.get(dependency)
        if dependency_unit is None or dependency_unit.status != "done":
            continue
        if not dependency_unit.commit:
            continue
        if not _git_commit_is_ancestor(root, dependency_unit.commit):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.dependency_commit_unapplied",
                    f"Dependency unit {dependency} result commit is not applied to the current checkout.",
                )
            )
    return errors


def _git_commit_is_ancestor(root: Path, commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _restore_delivery_progress(progress_path: Path, progress, *, existed: bool) -> None:
    if existed:
        from core.delivery_progress import write_delivery_progress

        write_delivery_progress(progress_path, progress)
        return
    try:
        progress_path.unlink()
    except FileNotFoundError:
        pass


def _status_unit_has_progress(unit) -> bool:
    return unit.status != "pending" or any(
        (
            unit.child_task_id,
            unit.branch,
            unit.commit,
            unit.waiting_reason,
            unit.failure_code,
            unit.started_at,
            unit.completed_at,
            unit.updated_at,
        )
    )


def _invoke_delivery_child_run(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    *,
    root: Path,
    task_path: str,
) -> DeliveryChildRunResult:
    run_args = _delivery_child_run_args(root=root, task_path=task_path)
    run_cfg = copy.deepcopy(cfg)
    try:
        if getattr(args, "json", False):
            with contextlib.redirect_stdout(sys.stderr):
                raw_result = context.run_task(run_args, run_cfg)
        else:
            raw_result = context.run_task(run_args, run_cfg)
    except SystemExit as exc:
        return DeliveryChildRunResult(
            exit_code=_system_exit_code(exc),
            child_task_id=getattr(run_args, "created_task_id", None),
        )
    except KeyboardInterrupt:
        return DeliveryChildRunResult(
            exit_code=130,
            child_task_id=getattr(run_args, "created_task_id", None),
            interrupted=True,
        )
    except Exception as exc:
        return DeliveryChildRunResult(
            exit_code=1,
            child_task_id=getattr(run_args, "created_task_id", None),
            exception=exc,
        )
    return _coerce_child_run_result(raw_result)


def _delivery_child_run_args(*, root: Path, task_path: str) -> argparse.Namespace:
    absolute_task_path = (root / task_path).resolve()
    return argparse.Namespace(
        task_file=str(absolute_task_path),
        task_file_pos=None,
        task_id=None,
        no_isolate=False,
        reset_failed=False,
        build=None,
        presync=None,
        presync_clean=None,
        planner=None,
        review=None,
        security_review=None,
        test_writing=None,
        tests=None,
        build_per_step=None,
        checks=None,
        require_contract_ready=False,
        min_contract_score=None,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )


def _coerce_child_run_result(result: DeliveryChildRunResult | int | None) -> DeliveryChildRunResult:
    if isinstance(result, DeliveryChildRunResult):
        return result
    if isinstance(result, int):
        return DeliveryChildRunResult(exit_code=result)
    return DeliveryChildRunResult(exit_code=0)


def _system_exit_code(exc: SystemExit) -> int:
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


def _delivery_failure_code(
    exit_code: int,
    child_state,
    *,
    interrupted: bool = False,
    exception: bool = False,
) -> str:
    if interrupted:
        return "child_run_interrupted"
    if exception:
        return "child_run_exception"
    if child_state is None:
        return "child_task_missing"
    if exit_code != 0:
        return "child_run_failed"
    return "child_task_incomplete"


def _status_unit_by_id(status, unit_id: str):
    return next((unit for unit in status.units if unit.id == unit_id), None)


def _print_delivery_result(result, *, json_output: bool, render: Callable) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render(result), end="")
