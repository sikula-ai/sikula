"""Review command helpers for the Sikula CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


def register_parser(subparsers) -> argparse.ArgumentParser:
    review_p = subparsers.add_parser("review", help="Review an existing branch (report-only or --fix)")
    review_target = review_p.add_mutually_exclusive_group(required=True)
    review_target.add_argument("--branch", help="Branch to review (must already exist)")
    review_target.add_argument(
        "--current-branch",
        action="store_true",
        default=False,
        help="Use the currently checked-out branch as the review-fix target; only valid with --fix",
    )
    review_p.add_argument("--base-branch", default="main", help="Base branch to diff against (default: main)")
    review_context = review_p.add_mutually_exclusive_group(required=True)
    review_context.add_argument(
        "--description", default=None, help="Human-readable PR description (context for the reviewer)"
    )
    review_context.add_argument(
        "--description-file",
        default=None,
        metavar="FILE",
        help="Path to a file containing the PR description",
    )
    review_p.add_argument(
        "--fix",
        action="store_true",
        default=False,
        help="Apply fixes: run implementer on review issues and commit them to the branch",
    )
    review_p.add_argument(
        "--security-review",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override run_security_review (default: from project config)",
    )
    review_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one agent (repeatable)",
    )
    review_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one agent (repeatable)",
    )
    review_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for one agent (repeatable)",
    )
    return review_p


@dataclass(frozen=True)
class ReviewContext:
    supported_build_tools: set[str]
    find_git_root: Callable[[Path], Path | None]
    ensure_gitignore: Callable[[Path], None]
    current_branch_name: Callable[[Path], tuple[str | None, str | None]]
    current_worktree_changes: Callable[[Path], tuple[list[str], list[str], list[str], str | None]]
    print_current_branch_clean_error: Callable[[list[str], list[str], list[str], str | None], None]
    resolve_git_commit: Callable[[Path, str], tuple[str | None, str | None]]
    worktree_error_message: Callable[[str | None, str], str]
    build_tool_class: Callable[[dict], object]
    resolve_state_dir: Callable[[dict], Path]
    heartbeat_interval_seconds: Callable[[dict], int]
    enrich_review_state_prompt: Callable[..., None]
    parse_agent_llm_overrides: Callable[..., dict]
    build_orchestrator: Callable[..., object]
    deliver_current_branch_review_fix: Callable[..., tuple[bool, bool, str | None]]
    finalize_worktree: Callable[..., tuple[bool, bool, str | None]]
    run_report_only_review: Callable[..., float]
    current_branch_delivery_needs_finalization: Callable[[object], bool]
    print_review_summary: Callable[..., None]
    logger: logging.Logger


def _review_context(context: ReviewContext | None = None) -> ReviewContext:
    if context is None:
        raise RuntimeError("review command requires a ReviewContext")
    return context


def cmd_review(args: argparse.Namespace, cfg: dict, context: ReviewContext | None = None) -> None:
    """Checkout an existing branch in a worktree and run code + security review."""
    from core.state import JsonStateStore, TaskState, runtime_metadata_snapshot

    context = _review_context(context)

    if getattr(args, "current_branch", False) and not args.fix:
        print("Error: --current-branch is only valid with sikula review --fix.")
        sys.exit(1)

    build_tool = cfg.get("project", {}).get("build_tool")
    if args.fix and build_tool not in context.supported_build_tools:
        supported = ", ".join(sorted(context.supported_build_tools))
        val = repr(build_tool) if build_tool else "not set"
        print(f"Unsupported build_tool: {val}. Set project.build_tool in .sikula/config.yaml to one of: {supported}")
        sys.exit(1)

    if args.description and args.description_file:
        print("Error: use either --description or --description-file, not both.")
        sys.exit(1)

    if args.description_file:
        desc_path = Path(args.description_file)
        if not desc_path.is_absolute():
            desc_path = Path.cwd() / desc_path
        if not desc_path.exists():
            print(f"Description file not found: {desc_path}")
            sys.exit(1)
        description = desc_path.read_text().strip()
    elif args.description:
        description = args.description.strip()
    else:
        print("Error: sikula review requires --description or --description-file.")
        print("The description is used as the review scope; without it the reviewer has to guess intent.")
        sys.exit(1)

    if not description:
        print("Error: review description is empty.")
        sys.exit(1)

    current_branch_mode = getattr(args, "current_branch", False)
    branch = args.branch
    base_branch = args.base_branch
    target_start_commit: str | None = None
    original_project_root = Path(cfg["project"]["root_path"]).resolve()

    git_root = context.find_git_root(original_project_root)
    if git_root is None:
        print(f"Error: project root is not inside a git repository: {original_project_root}")
        print("  Run 'git init' first.")
        sys.exit(1)

    context.ensure_gitignore(git_root)

    if current_branch_mode:
        branch_name, branch_error = context.current_branch_name(git_root)
        if branch_error == "detached":
            print("Error: --current-branch requires a named current branch; HEAD is detached.")
            sys.exit(1)
        if branch_name is None:
            print("Error: could not determine the current branch for --current-branch.")
            sys.exit(1)
        branch = branch_name

        staged, unstaged, untracked, clean_error = context.current_worktree_changes(git_root)
        if staged or unstaged or untracked or clean_error:
            context.print_current_branch_clean_error(staged, unstaged, untracked, clean_error)
            sys.exit(1)

        resolved_base_commit, base_error = context.resolve_git_commit(git_root, base_branch)
        if resolved_base_commit is None:
            print(f"Error: base branch/ref '{base_branch}' could not be resolved: {base_error}")
            sys.exit(1)

        target_start_commit, head_error = context.resolve_git_commit(git_root, "HEAD")
        if target_start_commit is None:
            print(f"Error: could not resolve HEAD for --current-branch: {head_error}")
            sys.exit(1)

    task_id = uuid.uuid4().hex
    worktree_base = git_root / ".sikula" / "worktrees" / task_id
    worktree_base.parent.mkdir(parents=True, exist_ok=True)

    if args.fix:
        if current_branch_mode:
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree_base), target_start_commit],
                capture_output=True,
                text=True,
                cwd=git_root,
            )
        else:
            # Fix mode writes commits back to the branch by using a real branch checkout.
            r = subprocess.run(
                ["git", "worktree", "add", str(worktree_base), branch],
                capture_output=True,
                text=True,
                cwd=git_root,
            )
        if r.returncode != 0:
            print(context.worktree_error_message(branch, r.stderr.strip()))
            sys.exit(1)
    else:
        # Report-only mode never commits, so detached HEAD works even if the caller
        # is currently on the reviewed branch.
        sha_r = subprocess.run(
            ["git", "rev-parse", branch],
            capture_output=True,
            text=True,
            cwd=git_root,
        )
        if sha_r.returncode != 0:
            print(f"Branch '{branch}' not found: {sha_r.stderr.strip()}")
            sys.exit(1)
        r = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_base), sha_r.stdout.strip()],
            capture_output=True,
            text=True,
            cwd=git_root,
        )
        if r.returncode != 0:
            print(context.worktree_error_message(branch, r.stderr.strip()))
            sys.exit(1)

    context.logger.info("Worktree created: %s (branch: %s)", worktree_base, branch)

    rel = original_project_root.relative_to(git_root)
    worktree_project_root = worktree_base / rel

    if args.fix:
        # Copy gitignored environment files the build needs (e.g. local.properties on Android).
        for name in context.build_tool_class(cfg).env_files():
            src = original_project_root / name
            dst = worktree_project_root / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                context.logger.info("Copied %s to worktree", name)

    # Compute three-dot diff: all commits introduced by the selected target vs base.
    diff_target = "HEAD" if current_branch_mode else branch
    diff_r = subprocess.run(
        ["git", "diff", f"{base_branch}...{diff_target}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if diff_r.returncode != 0:
        print(f"Failed to compute diff between '{base_branch}' and '{diff_target}': {diff_r.stderr.strip()}")
        subprocess.run(["git", "worktree", "remove", str(worktree_base)], cwd=git_root, check=False)
        sys.exit(1)
    review_diff = diff_r.stdout

    files_r = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...{diff_target}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    files_changed = (
        [line.strip() for line in files_r.stdout.splitlines() if line.strip()] if files_r.returncode == 0 else []
    )

    if not files_changed:
        print(f"No files changed between '{base_branch}' and '{branch}'")
        subprocess.run(["git", "worktree", "remove", str(worktree_base)], cwd=git_root, check=False)
        sys.exit(0)

    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = TaskState(
        task_id=task_id,
        task_description=description,
        implementation_prompt=description,
        files_changed=files_changed,
        review_diff=review_diff,
        review_mode="review_fix" if args.fix else "review_report",
        review_base_branch=base_branch,
        review_delivery_mode="current_branch" if current_branch_mode else None,
        review_target_branch=branch if current_branch_mode else None,
        review_target_start_commit=target_start_commit if current_branch_mode else None,
        review_delivery_status="pending" if current_branch_mode else None,
        plan_decided=True,
        worktree_path=str(worktree_project_root),
        worktree_base=str(worktree_base),
        worktree_branch=branch,
        runtime_metadata=runtime_metadata_snapshot(),
    )
    if not args.fix:
        state.pid = os.getpid()
    store.save(state)
    task_label = Path(args.description_file).name if args.description_file else description.splitlines()[0][:60]

    t_start = time.time()
    cli_security_review = getattr(args, "security_review", None)
    run_security_review = cfg.get("run_security_review", True) if cli_security_review is None else cli_security_review
    heartbeat_interval_seconds = context.heartbeat_interval_seconds(cfg)

    base_llm_cfg = cfg.get("llm", {})

    if args.fix:
        # Analyst is skipped in review mode. Enrich the prompt with referenced files
        # before the orchestrator starts so reviewer and fixer have that context.
        context.enrich_review_state_prompt(state, store, description, base_llm_cfg, cfg, worktree_project_root)

        cfg["project"]["root_path"] = str(worktree_project_root)

        overrides = {
            "run_planner": False,
            "run_review": True,
            "run_security_review": run_security_review,
            "agent_llms": context.parse_agent_llm_overrides(
                getattr(args, "agent_model", None),
                getattr(args, "agent_provider", None),
                getattr(args, "agent_timeout", None),
            ),
        }
        orch = context.build_orchestrator(cfg, overrides, state_store=store)
        state = orch.run(task_id=task_id, label=task_label)
        total_s = time.time() - t_start

        if state.done:
            fix_msg = f"sikula: review fixes for {branch}\n\nTask ID: {state.task_id}"
            if current_branch_mode:
                success, committed, _ = context.deliver_current_branch_review_fix(
                    worktree_base,
                    git_root,
                    state,
                    store,
                    commit_msg=fix_msg,
                )
                if success:
                    if committed:
                        context.logger.info("Current-branch review fixes delivered to %s", branch)
                    else:
                        context.logger.info("No fixes needed — worktree removed")
            else:
                success, committed, _ = context.finalize_worktree(worktree_base, git_root, state, commit_msg=fix_msg)
                store.save(state)
                if success:
                    if committed:
                        context.logger.info("Changes committed to branch %s", branch)
                    else:
                        context.logger.info("No fixes needed — worktree removed")
                else:
                    context.logger.warning("Could not finalize worktree — inspect manually: %s", worktree_base)
        else:
            context.logger.info("Worktree preserved for inspection/resume: %s", worktree_base)
    else:
        total_s = context.run_report_only_review(
            args=args,
            cfg=cfg,
            state=state,
            store=store,
            task_id=task_id,
            task_label=task_label,
            description=description,
            branch=branch,
            base_branch=base_branch,
            files_changed=files_changed,
            base_llm_cfg=base_llm_cfg,
            run_security_review=run_security_review,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            worktree_project_root=worktree_project_root,
            git_root=git_root,
            worktree_base=worktree_base,
            t_start=t_start,
        )

    approved = (
        state.review_approved
        and (state.security_approved if run_security_review else True)
        and not context.current_branch_delivery_needs_finalization(state)
    )
    context.print_review_summary(state, branch, base_branch, total_s, run_security_review=run_security_review)
    sys.exit(0 if approved else 1)
