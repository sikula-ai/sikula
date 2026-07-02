"""Task-related CLI command helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

from sikula_cli.config import _resolve_task_asset_dir


def register_attach_parser(task_subparsers) -> argparse.ArgumentParser:
    task_attach_p = task_subparsers.add_parser("attach", help="Attach a local file as a task asset")
    task_attach_p.add_argument("task_file", metavar="TASK_FILE", help="Path to task .txt/.md file")
    task_attach_p.add_argument(
        "asset_file",
        metavar="ASSET_FILE",
        help="Local file to copy into tasks.task_asset_dir",
    )
    task_attach_kind = task_attach_p.add_mutually_exclusive_group(required=True)
    task_attach_kind.add_argument(
        "--reference",
        action="store_true",
        default=False,
        help="Attach the file as a reference-only asset",
    )
    task_attach_kind.add_argument(
        "--delivery",
        action="store_true",
        default=False,
        help="Attach the file as a delivery asset that should become part of the branch output",
    )
    task_attach_p.add_argument("--note", help="Reference-asset note to include in the Markdown snippet")
    task_attach_p.add_argument("--purpose", help="Delivery-asset purpose; required with --delivery")
    task_attach_p.add_argument("--target", help="Optional project-relative delivery target path")
    task_attach_p.add_argument("--source", help="Delivery-asset source/license/provenance; required with --delivery")
    task_attach_p.add_argument(
        "--write",
        action="store_true",
        default=False,
        help="Append the generated asset snippet to the task file; otherwise only print it",
    )
    return task_attach_p


def _default_resolve_task_path(task_file: str, project_root: Path) -> Path | None:
    path = Path(task_file)
    if path.is_absolute():
        return path if path.exists() else None
    cwd_path = Path.cwd() / path
    return cwd_path if cwd_path.exists() else None


@dataclass(frozen=True)
class TaskContext:
    resolve_task_path: Callable[[str, Path], Path | None] = _default_resolve_task_path
    resolve_task_asset_dir: Callable[[dict], Path] = _resolve_task_asset_dir


def _task_context(context: TaskContext | None = None) -> TaskContext:
    return context or TaskContext()


def cmd_task_attach(args: argparse.Namespace, cfg: dict, context: TaskContext | None = None) -> None:
    from core.task_attach import attach_task_asset

    context = _task_context(context)
    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = context.resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}", file=sys.stderr)
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    kind = "delivery" if args.delivery else "reference"
    task_asset_dir = context.resolve_task_asset_dir(cfg)
    try:
        result = attach_task_asset(
            task_file=task_path,
            source_file=Path(args.asset_file),
            project_root=project_root,
            task_asset_dir=task_asset_dir,
            kind=kind,
            note=args.note or "",
            purpose=args.purpose or "",
            target=args.target or "",
            source_license=args.source or "",
            write=bool(args.write),
        )
    except (OSError, ValueError) as exc:
        print(f"Failed to attach task asset: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Attached task asset: {result.project_path}")
    print(f"Source file: {result.source_path}")
    print(f"SHA-256: {result.sha256}")
    print(f"Size: {result.size_bytes} bytes")
    print(f"Task file updated: {'yes' if result.wrote_task_file else 'no'}")
    if result.reused_existing:
        print("Existing identical asset reused: yes")
    print("Markdown snippet:")
    print(result.snippet)
