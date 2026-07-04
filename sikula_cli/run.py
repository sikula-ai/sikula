"""Run command parser helpers for the Sikula CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import sys
import time


def register_parser(
    subparsers,
    *,
    contract_score_threshold: Callable[[str], int],
) -> argparse.ArgumentParser:
    run_p = subparsers.add_parser("run", help="Run a task")
    run_p.add_argument(
        "task_file_pos",
        nargs="?",
        default=None,
        metavar="TASK_FILE",
        help="Path to task .txt/.md file (positional shorthand for --task-file)",
    )
    run_p.add_argument("--task-file", help="Path to task .txt/.md file (absolute or relative to CWD)")
    run_p.add_argument("--task-id", help="Resume existing task by ID")
    run_p.add_argument(
        "--reset-failed",
        action="store_true",
        default=False,
        help="Reset a failed task before resuming; requires --task-id",
    )
    run_p.add_argument(
        "--no-isolate",
        action="store_true",
        default=False,
        help="Run in the project directory directly without creating a git worktree branch",
    )

    # Phase toggle flags: default None means use config; True/False forces a value.
    boolean_action = argparse.BooleanOptionalAction
    run_p.add_argument("--build", action=boolean_action, default=None, help="Override run_build")
    run_p.add_argument("--presync", action=boolean_action, default=None, help="Override run_presync")
    run_p.add_argument(
        "--presync-clean",
        action=boolean_action,
        default=None,
        help="Override build.presync_clean (run clean before presync task)",
    )
    run_p.add_argument("--planner", action=boolean_action, default=None, help="Override run_planner")
    run_p.add_argument("--review", action=boolean_action, default=None, help="Override run_review")
    run_p.add_argument(
        "--security-review",
        action=boolean_action,
        default=None,
        help="Override run_security_review",
    )
    run_p.add_argument("--test-writing", action=boolean_action, default=None, help="Override run_test_writing")
    run_p.add_argument("--tests", action=boolean_action, default=None, help="Override run_tests")
    run_p.add_argument(
        "--build-per-step",
        action=boolean_action,
        default=None,
        help="Override run_build_per_step",
    )
    run_p.add_argument("--checks", action=boolean_action, default=None, help="Override run_checks")
    run_p.add_argument(
        "--require-contract-ready",
        action="store_true",
        default=False,
        help="Abort fresh task-file runs before agents unless the implementation contract is ready",
    )
    run_p.add_argument(
        "--min-contract-score",
        type=contract_score_threshold,
        default=None,
        metavar="0-100",
        help="Abort fresh task-file runs before agents unless the implementation contract score is at least this value",
    )

    # Per-agent LLM overrides are repeatable and layer on top of agents.<name>.llm.
    run_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one agent, e.g. --agent-model analyst=gpt-5.5",
    )
    run_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one agent, e.g. --agent-provider implementer=claude",
    )
    run_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override agent_timeout for one agent, e.g. --agent-timeout implementer=2400",
    )
    return run_p


@dataclass(frozen=True)
class RunContext:
    supported_build_tools: set[str]
    parse_agent_llm_overrides: Callable[..., dict]
    resolve_state_dir: Callable[[dict], Path]
    sikula_worktree_base_for_path: Callable[[Path], Path | None]
    reset_failed_state: Callable[..., None]
    resolve_task_path: Callable[[str, Path], Path | None]
    find_git_root: Callable[[Path], Path | None]
    require_committed_config_for_isolated_run: Callable[[dict, Path], None]
    run_config_snapshot: Callable[..., dict]
    contract_preflight_config: Callable[[dict, dict], dict]
    build_contract_preflight_snapshot_and_assets: Callable[..., tuple[dict, list[dict]]]
    record_contract_asset_drift: Callable[..., None]
    contract_preflight_record_result: Callable[[dict], str]
    print_contract_preflight_summary: Callable[[dict], None]
    contract_readiness_gate_failures: Callable[..., list[str]]
    print_contract_readiness_gate_failure: Callable[[dict, list[str], str], None]
    branch_stem: Callable[[str], str]
    ensure_gitignore: Callable[[Path], None]
    create_worktree: Callable[[Path, Path, str], tuple[bool, str]]
    build_tool_class: Callable[[dict], object]
    record_snapshot_asset_drift: Callable[..., None]
    build_orchestrator: Callable[..., object]
    current_branch_delivery_needs_finalization: Callable[[object], bool]
    current_branch_delivery_cleaned: Callable[[object], bool]
    path_is_within: Callable[[Path, Path], bool]
    record_asset_target_audit: Callable[..., None]
    current_branch_delivery_pending: Callable[[object], bool]
    deliver_current_branch_review_fix: Callable[..., tuple[bool, bool, str | None]]
    default_worktree_commit_message: Callable[[object], str]
    finalize_worktree: Callable[..., tuple[bool, bool, str | None]]
    current_branch_delivery_terminal: Callable[[object], bool]
    task_warning_count: Callable[[object], int]
    contract_gate_blocked_without_worktree: Callable[[object], bool]
    contract_gate_next_action: Callable[[object], str]
    fmt_time: Callable[[float], str]
    print_task_audit_report: Callable[[object], int]
    logger: logging.Logger


def _run_context(context: RunContext | None = None) -> RunContext:
    if context is None:
        raise RuntimeError("run command requires a RunContext")
    return context


def _project_scoped_task_worktree_base(cwd: Path, project_root: Path, context: RunContext) -> Path | None:
    worktree_base = context.sikula_worktree_base_for_path(cwd)
    if not worktree_base:
        return None
    try:
        rel = cwd.resolve().relative_to(worktree_base)
    except ValueError:
        return None
    original_path = (worktree_base.parent.parent.parent / rel).resolve()
    try:
        original_path.relative_to(project_root.resolve())
    except ValueError:
        return None
    else:
        return worktree_base


def cmd_run(args: argparse.Namespace, cfg: dict, context: RunContext | None = None) -> None:
    from core.state import JsonStateStore

    context = _run_context(context)

    build_tool = cfg.get("project", {}).get("build_tool")
    if build_tool not in context.supported_build_tools:
        supported = ", ".join(sorted(context.supported_build_tools))
        val = repr(build_tool) if build_tool else "not set"
        print(f"Unsupported build_tool: {val}. Set project.build_tool in .sikula/config.yaml to one of: {supported}")
        sys.exit(1)

    overrides: dict = {
        "run_build": args.build,
        "run_presync": args.presync,
        "run_planner": args.planner,
        "run_review": args.review,
        "run_security_review": args.security_review,
        "run_test_writing": args.test_writing,
        "run_tests": args.tests,
        "run_build_per_step": args.build_per_step,
        "run_checks": args.checks,
        "agent_llms": context.parse_agent_llm_overrides(args.agent_model, args.agent_provider, args.agent_timeout),
    }
    if args.presync_clean is not None:
        overrides["presync_clean"] = args.presync_clean

    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    isolate = not args.no_isolate
    original_project_root = Path(cfg["project"]["root_path"]).resolve()
    current_task_worktree_base = _project_scoped_task_worktree_base(Path.cwd(), original_project_root, context)
    worktree_base: Path | None = None  # git root of the worktree (for git ops)
    leave_current_worktree_before_finalize = False
    already_terminal = False
    current_branch_delivery_retry = False
    delivery_failed = False

    if args.reset_failed:
        if not args.task_id:
            print("--reset-failed requires --task-id")
            sys.exit(1)
        context.reset_failed_state(args.task_id, cfg, store)

    t_start = time.time()

    if not args.task_file and getattr(args, "task_file_pos", None):
        args.task_file = args.task_file_pos

    if args.task_file:
        if current_task_worktree_base:
            print("Refusing to start a new task from inside a Sikula task worktree.")
            print("Run this command from the original project, or use 'sikula run --task-id <task-id>' to resume.")
            sys.exit(1)
        task_path = context.resolve_task_path(args.task_file, original_project_root)
        if task_path is None:
            print(f"Task file not found: {args.task_file}")
            sys.exit(1)

        git_root = context.find_git_root(original_project_root)
        if git_root is None:
            print(f"Error: project root is not inside a git repository: {original_project_root}")
            print("  Run 'git init && git add -A && git commit -m init' to initialize a repository.")
            sys.exit(1)
        if isolate:
            context.require_committed_config_for_isolated_run(cfg, git_root)

        description = task_path.read_text().strip()
        state = store.create(description)
        state.task_file = Path(args.task_file).name
        state.config_snapshot = context.run_config_snapshot(cfg, overrides)
        preflight_cfg = context.contract_preflight_config(cfg, overrides)
        state.implementation_contract, implementation_asset_records = (
            context.build_contract_preflight_snapshot_and_assets(task_path, preflight_cfg, original_project_root)
        )
        state.record_implementation_assets(implementation_asset_records)
        context.record_contract_asset_drift(state, implementation_asset_records, store, phase="run_start")
        state.record(
            "orchestrator",
            "contract_check",
            context.contract_preflight_record_result(state.implementation_contract),
        )
        store.save(state)
        context.print_contract_preflight_summary(state.implementation_contract)
        gate_failures = context.contract_readiness_gate_failures(
            state.implementation_contract,
            require_ready=bool(getattr(args, "require_contract_ready", False)),
            min_score=getattr(args, "min_contract_score", None),
        )
        if gate_failures:
            state.failed = True
            state.contract_gate_blocked = True
            state.record("orchestrator", "contract_gate_failed", "; ".join(gate_failures))
            store.save(state)
            context.print_contract_readiness_gate_failure(state.implementation_contract, gate_failures, state.task_id)
            sys.exit(1)

        if isolate:
            branch = f"sikula/{context.branch_stem(args.task_file)}-{state.task_id}"
            worktree_base = git_root / ".sikula" / "worktrees" / state.task_id
            # Effective project root within the worktree mirrors the relative path from git root.
            rel = original_project_root.relative_to(git_root)
            worktree_project_root = worktree_base / rel
            context.ensure_gitignore(git_root)
            ok, err = context.create_worktree(git_root, worktree_base, branch)
            if not ok:
                print(f"Failed to create git worktree: {err}")
                sys.exit(1)
            # Copy gitignored environment files that the build needs but are not tracked.
            for name in context.build_tool_class(cfg).env_files():
                src = original_project_root / name
                dst = worktree_project_root / name
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
                    context.logger.info("Copied %s to worktree", name)
            state.worktree_path = str(worktree_project_root)
            state.worktree_base = str(worktree_base)
            state.worktree_branch = branch
            context.record_snapshot_asset_drift(state, worktree_project_root, store, phase="worktree_start")
            store.save(state)
            context.logger.info("Worktree created: %s (branch: %s)", worktree_base, branch)
            cfg["project"]["root_path"] = str(worktree_project_root)

        orch = context.build_orchestrator(cfg, overrides, state_store=store)
        state = orch.run(task_id=state.task_id, label=Path(args.task_file).name)

    elif args.task_id:
        state = store.load(args.task_id)
        if not state:
            print(f"Task {args.task_id} not found")
            sys.exit(1)
        current_branch_delivery_retry = context.current_branch_delivery_needs_finalization(state)
        already_terminal = (state.done or state.failed) and not current_branch_delivery_retry
        is_review_fix_resume = state.review_mode == "review_fix"
        if state.review_mode == "review_report" and not already_terminal:
            print(f"Task {args.task_id} is a report-only review task and cannot be resumed.")
            print("Re-run 'sikula review' to start a fresh review.")
            sys.exit(1)
        if is_review_fix_resume:
            overrides["run_planner"] = False
            overrides["run_review"] = True
            if args.security_review is None and state.config_snapshot:
                saved_security_review = state.config_snapshot.get("run_security_review")
                if saved_security_review is not None:
                    overrides["run_security_review"] = saved_security_review

        if current_branch_delivery_retry:
            if not state.worktree_base:
                print(f"Task {args.task_id} has no worktree path recorded.")
                print("It was likely cleaned up already, so current-branch delivery cannot be retried safely.")
                sys.exit(1)
            worktree_base = Path(state.worktree_base)
            if not worktree_base.exists():
                print(f"Worktree no longer exists: {worktree_base}")
                print("Restore the worktree manually, or inspect the task state before deleting it.")
                sys.exit(1)
            if context.path_is_within(Path.cwd(), worktree_base):
                leave_current_worktree_before_finalize = True
        elif context.current_branch_delivery_cleaned(state):
            print(f"Task {args.task_id} has no current-branch delivery worktree recorded.")
            print("It was likely cleaned up already, so delivery cannot be retried safely.")
            print(f"Use 'sikula show {args.task_id}' for audit, or start a new review-fix task.")
            sys.exit(1)
        elif already_terminal:
            pass
        elif state.worktree_path:
            wt = Path(state.worktree_path)
            if wt.exists():
                worktree_base = Path(state.worktree_base) if state.worktree_base else wt
                if context.path_is_within(Path.cwd(), worktree_base):
                    leave_current_worktree_before_finalize = True
                cfg["project"]["root_path"] = str(wt)
                context.record_snapshot_asset_drift(state, wt, store, phase="resume")
            else:
                print(f"Worktree no longer exists: {wt}")
                print("Delete the task state and re-run with --task-file, or restore the worktree manually.")
                sys.exit(1)
        elif state.worktree_branch and not state.done and not state.failed:
            print(f"Task {args.task_id} has no worktree path recorded.")
            print("It was likely cleaned up already, so it cannot be resumed safely.")
            print(f"Use 'sikula show {args.task_id}' for audit, or start a new task with --task-file.")
            sys.exit(1)
        elif not already_terminal:
            project_root = Path(cfg.get("project", {}).get("root_path") or original_project_root)
            context.record_snapshot_asset_drift(state, project_root, store, phase="resume")

        if not current_branch_delivery_retry:
            orch = context.build_orchestrator(cfg, overrides, state_store=store)
            state = orch.run(task_id=args.task_id)

    else:
        raise AssertionError("unreachable — task_file/task_id check is in main()")

    total_s = time.time() - t_start
    if state.done and not already_terminal and not current_branch_delivery_retry:
        project_root = Path(cfg.get("project", {}).get("root_path") or original_project_root)
        context.record_asset_target_audit(state, project_root, store, phase="completion")

    if worktree_base and state.done:
        if leave_current_worktree_before_finalize:
            os.chdir(original_project_root)
        git_root = context.find_git_root(original_project_root) or original_project_root
        commit_msg = None
        if state.review_mode == "review_fix" and state.worktree_branch:
            commit_msg = f"sikula: review fixes for {state.worktree_branch}\n\nTask ID: {state.task_id}"
        if context.current_branch_delivery_pending(state):
            success, committed, _ = context.deliver_current_branch_review_fix(
                worktree_base,
                git_root,
                state,
                store,
                commit_msg=commit_msg or context.default_worktree_commit_message(state),
            )
            delivery_failed = not success
        else:
            success, committed, _ = context.finalize_worktree(worktree_base, git_root, state, commit_msg=commit_msg)
            store.save(state)
            if success:
                state.worktree_path = None
                state.worktree_base = None
                store.save(state)
                if committed:
                    context.logger.info("Changes committed to branch %s", state.worktree_branch)
                context.logger.info("Worktree removed: %s", worktree_base)
            else:
                context.logger.warning("Could not finalize worktree — inspect manually: %s", worktree_base)
        if context.current_branch_delivery_terminal(state):
            if committed:
                context.logger.info("Current-branch review fixes delivered to %s", state.worktree_branch)
            else:
                context.logger.info("No fixes needed — worktree removed")
        else:
            delivery_failed = delivery_failed or context.current_branch_delivery_pending(state)
    elif worktree_base and not state.done:
        context.logger.info("Worktree preserved for inspection/resume: %s", worktree_base)

    longest_label, longest_s = "-", 0.0
    for h in state.history:
        dur = h.get("elapsed_s", 0.0)
        if dur > longest_s:
            longest_s = dur
            longest_label = f"{h['agent']}/{h['action']}"

    max_iter = cfg.get("sandbox", {}).get("max_iterations", 10)
    warning_count = context.task_warning_count(state)
    if state.done and not delivery_failed:
        status = f"✓ DONE with warnings ({warning_count})" if warning_count else "✓ DONE"
    elif state.failed or delivery_failed:
        status = "✗ FAILED"
    else:
        status = "⚠ INCOMPLETE"
    print(f"\nTask {state.task_id}: {status}")
    if already_terminal:
        if state.done:
            print("This task is already complete; no work was run.")
        else:
            print("This task has failed; no work was run.")
            if context.contract_gate_blocked_without_worktree(state):
                print("The contract readiness gate blocked delivery before a worktree was created.")
                print(f"Suggested next step: {context.contract_gate_next_action(state)}")
            elif state.review_mode == "review_report":
                print("Report-only review tasks cannot be retried with sikula run.")
                print("Re-run 'sikula review' to start a fresh review.")
            else:
                print(f"Use --reset-failed to retry: sikula run --task-id {state.task_id} --reset-failed")
        print()
        print("Previous run:")
    else:
        print(f"Total time:      {context.fmt_time(total_s)}")
    if longest_s > 0:
        print(f"Longest phase:   {longest_label} ({context.fmt_time(longest_s)})")
    print(f"Build attempts:  {state.build_iterations} total (max {max_iter}/loop)")
    print(f"Total phases:    {len(state.history)}")
    if state.worktree_branch:
        print(f"Branch:          {state.worktree_branch}")
    if state.files_changed:
        print("Files changed:")
        for f in state.files_changed:
            print(f"  {f}")
    context.print_task_audit_report(state)
    if state.errors:
        print(f"Errors:          {len(state.errors)} remaining (see: sikula show {state.task_id})")

    sys.exit(0 if state.done and not delivery_failed else 1)
