"""Cleanup and delete commands for preserved Sikula task worktrees."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

from core import worktree as core_worktree
from core.state import JsonStateStore
from sikula_cli.config import _resolve_state_dir


def register_parser(subparsers) -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    cleanup_p = subparsers.add_parser("cleanup", help="Remove a task worktree but keep its state JSON")
    cleanup_p.set_defaults(delete_state=False)
    cleanup_p.add_argument("task_id")
    cleanup_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Apply cleanup. Without this flag, cleanup only prints what would happen.",
    )
    cleanup_p.add_argument(
        "--discard",
        action="store_true",
        default=False,
        help="Allow removing a dirty worktree and discarding uncommitted changes.",
    )

    delete_p = subparsers.add_parser("delete", help="Delete a task worktree and its state JSON")
    delete_p.set_defaults(delete_state=True)
    delete_p.add_argument("task_id")
    delete_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Apply deletion. Without this flag, delete only prints what would happen.",
    )
    delete_p.add_argument(
        "--discard",
        action="store_true",
        default=False,
        help="Allow removing a dirty worktree and discarding uncommitted changes.",
    )
    return cleanup_p, delete_p


def _default_find_git_root(path: Path) -> Path | None:
    return core_worktree.find_git_root(path)


def _default_worktree_dirty(worktree_base: Path) -> bool:
    return core_worktree.worktree_dirty(worktree_base)


def _default_remove_worktree(worktree_base: Path, git_root: Path, *, force: bool) -> bool:
    return core_worktree.remove_worktree(worktree_base, git_root, force=force)


def _default_path_is_within(path: Path, base: Path) -> bool:
    return core_worktree.path_is_within(path, base)


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
