"""Task-related CLI command helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

from sikula_cli.config import _resolve_task_asset_dir


def register_refine_parser(task_subparsers) -> argparse.ArgumentParser:
    task_refine_p = task_subparsers.add_parser("refine", help="Refine a product task description")
    task_refine_p.add_argument("task_file", metavar="TASK_FILE", help="Path to task .txt/.md file")
    task_refine_p.add_argument("--answers", help="Path to a Sikula answers YAML file")
    task_refine_p.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Use a read-only LLM assistant to normalize the task description before deterministic refinement",
    )
    task_refine_p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Prompt for product task answers before writing the refined task description",
    )
    task_refine_p.add_argument(
        "--output",
        help="Write the refined Markdown task description to this file; defaults to tasks.<stem>.refined.md",
    )
    task_refine_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for task_preparer, e.g. --agent-model task_preparer=gpt-5.5",
    )
    task_refine_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for task_preparer, e.g. --agent-provider task_preparer=claude",
    )
    task_refine_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for task_preparer, e.g. --agent-timeout task_preparer=1200",
    )
    return task_refine_p


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


@dataclass(frozen=True)
class TaskRefineContext:
    resolve_task_path: Callable[[str, Path], Path | None]
    resolve_answers_path: Callable[[str], Path]
    resolve_output_path: Callable[[str], Path]
    default_refined_task_path: Callable[[Path, dict], Path]
    load_prepare_answers: Callable[..., dict[str, dict]]
    collect_prepare_answers_interactive: Callable[..., dict[str, dict]]
    run_task_refine_auto: Callable[..., object]
    write_prepare_answers_template: Callable[..., Path]
    prepare_answers_path: Callable[..., Path]
    print_existing_output_next_step_note: Callable[[Path], None]
    print_existing_output_hint: Callable[[Path], None]
    print_open_question_details: Callable[[list[dict]], None]
    print_task_refinement_scope_note: Callable[[], None]


def _task_context(context: TaskContext | None = None) -> TaskContext:
    return context or TaskContext()


def _task_refine_context(context: TaskRefineContext | None = None) -> TaskRefineContext:
    if context is None:
        raise RuntimeError("task refine requires a TaskRefineContext")
    return context


def cmd_task_refine(
    args: argparse.Namespace,
    cfg: dict,
    context: TaskRefineContext | None = None,
) -> None:
    from core.contract_check import prepare_task_description

    if args.auto and args.interactive:
        print("Failed to refine task: --auto cannot be combined with --interactive", file=sys.stderr)
        sys.exit(2)
    context = _task_refine_context(context)

    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = context.resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    task_text = task_path.read_text(encoding="utf-8")
    answers: dict[str, dict] = {}
    answers_supplied = bool(args.interactive or args.answers)
    if args.interactive:
        try:
            first = prepare_task_description(task_text, task_name=task_path.name)
            answers = context.collect_prepare_answers_interactive(
                generated_by="sikula.task_refine",
                label="task refinement",
                source_path=task_path,
                source_text=task_text,
                project_root=project_root,
                questions=first.user_questions,
                cfg=cfg,
                answers_path=context.resolve_answers_path(args.answers) if args.answers else None,
            )
        except (EOFError, OSError, ValueError) as exc:
            print(f"Failed to collect task refinement answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.answers:
        try:
            answers = context.load_prepare_answers(
                context.resolve_answers_path(args.answers), source_path=task_path, source_text=task_text
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to load task refinement answers: {exc}", file=sys.stderr)
            sys.exit(1)

    output_path = (
        context.resolve_output_path(args.output) if args.output else context.default_refined_task_path(task_path, cfg)
    )
    if args.auto:
        if output_path.exists():
            print(f"Failed to refine task: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
            context.print_existing_output_hint(output_path)
            sys.exit(1)
        try:
            auto_result = context.run_task_refine_auto(
                args=args,
                cfg=cfg,
                project_root=project_root,
                source_path=task_path,
                task_text=task_text,
                task_name=task_path.name,
                output_path=output_path,
                answers=answers,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Failed to auto-refine task: {exc}", file=sys.stderr)
            sys.exit(1)
        result = auto_result.result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.prepared_task_markdown, encoding="utf-8")

        print(f"Refined task description written: {output_path}")
        print("Auto-normalized task description: yes")
        if auto_result.input_language:
            print(f"Input language: {auto_result.input_language}")
        if auto_result.normalized_to_english:
            print("Normalized to English: yes")
        if auto_result.warnings:
            print("Auto-refine warnings:")
            for warning in auto_result.warnings:
                print(f"- {warning}")
        print(f"Auto-applied answers: {len(auto_result.auto_answers)}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        context.print_open_question_details(result.user_questions)
        context.print_task_refinement_scope_note()
        if result.needs_user_input:
            answers_path = context.write_prepare_answers_template(
                generated_by="sikula.task_refine",
                source_path=output_path,
                source_text=result.prepared_task_markdown,
                project_root=project_root,
                questions=result.user_questions,
                cfg=cfg,
            )
            print("Next step:")
            print(f"- Fill the answers file, then run: sikula task refine {output_path} --answers {answers_path}")
            print("- Use a new --output path, or remove/rename the refined task written above first.")
        else:
            print(f"Next step: sikula contract prepare {output_path}")
        return

    result = prepare_task_description(task_text, task_name=task_path.name, answers=answers)
    if result.needs_user_input and not answers_supplied:
        answers_path = context.write_prepare_answers_template(
            generated_by="sikula.task_refine",
            source_path=task_path,
            source_text=task_text,
            project_root=project_root,
            questions=result.user_questions,
            cfg=cfg,
        )
        print("Task refinement needs answers before writing a refined task description.")
        print(f"Task refinement answers template written: {answers_path}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        context.print_open_question_details(result.user_questions)
        context.print_task_refinement_scope_note()
        print("Next step:")
        print(f"- Fill the answers file, then run: sikula task refine {args.task_file} --answers {answers_path}")
        print(f"- Or answer in the terminal: sikula task refine {args.task_file} --interactive")
        if output_path.exists():
            context.print_existing_output_next_step_note(output_path)
        sys.exit(1)

    if output_path.exists():
        print(f"Failed to refine task: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        context.print_existing_output_hint(output_path)
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.prepared_task_markdown, encoding="utf-8")

    print(f"Refined task description written: {output_path}")
    print(f"Applied answers: {len(result.answered_question_ids)}")
    print(f"Open questions: {len(result.open_question_ids)}")
    context.print_open_question_details(result.user_questions)
    context.print_task_refinement_scope_note()
    if result.needs_user_input:
        answers_path = (
            context.resolve_answers_path(args.answers)
            if args.answers
            else context.prepare_answers_path(
                task_path,
                cfg,
                generated_by="sikula.task_refine",
            )
        )
        print("Next step:")
        print(f"- Fill/update the answers file: {answers_path}")
        print("- Then rerun task refine with a new --output path, or remove/rename the output written above first.")
    else:
        print(f"Next step: sikula contract prepare {output_path}")


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
