"""Cleanup and delete commands for preserved Sikula task worktrees."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

from core.state import JsonStateStore
from sikula_cli.config import _resolve_state_dir


def _default_find_git_root(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=path,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _default_worktree_dirty(worktree_base: Path) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
    )
    return bool(status.stdout.strip()) if status.returncode == 0 else True


def _default_remove_worktree(worktree_base: Path, git_root: Path, *, force: bool) -> bool:
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree_base))
    result = subprocess.run(cmd, cwd=git_root, check=False)
    return result.returncode == 0


def _default_path_is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class CleanupContext:
    resolve_state_dir: Callable[[dict], Path] = _resolve_state_dir
    path_is_within: Callable[[Path, Path], bool] = _default_path_is_within
    worktree_dirty: Callable[[Path], bool] = _default_worktree_dirty
    find_git_root: Callable[[Path], Path | None] = _default_find_git_root
    remove_worktree: Callable[..., bool] = _default_remove_worktree


def _cleanup_context(context: CleanupContext | None = None) -> CleanupContext:
    return context or CleanupContext()


def cmd_cleanup(args: argparse.Namespace, cfg: dict, context: CleanupContext | None = None) -> None:
    """Remove a task worktree, optionally deleting the persisted state as well."""
    context = _cleanup_context(context)
    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = store.load(args.task_id)
    if not state:
        print(f"Task {args.task_id} not found")
        sys.exit(1)

    action = "delete" if args.delete_state else "cleanup"
    dry_run = not args.force
    removed_worktree = False
    clear_worktree_refs = False

    print(f"Task {state.task_id}: {action.upper()}{' (dry run)' if dry_run else ''}")

    if state.worktree_base or state.worktree_path:
        worktree_base = Path(state.worktree_base or state.worktree_path)
        if worktree_base.exists():
            if not dry_run and context.path_is_within(Path.cwd(), worktree_base):
                print(f"Refusing to remove the current working tree: {worktree_base}")
                print("Run this command from the original project or another directory.")
                sys.exit(1)
            dirty = context.worktree_dirty(worktree_base)
            if dirty and not dry_run and not args.discard:
                print(f"Worktree has uncommitted changes: {worktree_base}")
                print("Refusing to remove it. Re-run with --discard to delete those changes.")
                sys.exit(1)
            if dry_run:
                print(f"Would remove worktree: {worktree_base}")
                if dirty:
                    print("Worktree has uncommitted changes; applying this cleanup requires --discard.")
            else:
                git_root = (
                    context.find_git_root(Path(cfg["project"]["root_path"]).resolve())
                    or Path(cfg["project"]["root_path"]).resolve()
                )
                if not context.remove_worktree(worktree_base, git_root, force=args.discard):
                    print(f"Failed to remove worktree: {worktree_base}")
                    sys.exit(1)
                removed_worktree = True
                clear_worktree_refs = True
                print(f"Removed worktree: {worktree_base}")
        else:
            print(f"Worktree already missing: {worktree_base}")
            clear_worktree_refs = True
    else:
        print("Task has no isolated worktree recorded.")

    if args.delete_state:
        if dry_run:
            print(f"Would delete state: {state_dir / (state.task_id + '.json')}")
        else:
            store.delete(state.task_id)
            print(f"Deleted state: {state.task_id}")
    elif not dry_run:
        store.delete_text_snapshots(state.task_id)
        state.record(
            "sikula",
            "cleanup",
            "worktree removed" if removed_worktree else "worktree already missing or not recorded",
        )
        if clear_worktree_refs:
            state.worktree_path = None
            state.worktree_base = None
        store.save(state)

    if dry_run:
        print("No changes made. Re-run with --force to apply.")
