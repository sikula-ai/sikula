"""Contract-related CLI command helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from sikula_cli.config import _resolve_contract_report_dir


def register_check_parser(contract_subparsers) -> argparse.ArgumentParser:
    contract_check_p = contract_subparsers.add_parser(
        "check",
        help="Check a task file as an implementation contract",
    )
    contract_check_p.add_argument("task_file", metavar="TASK_FILE", help="Path to task .txt/.md file")
    contract_check_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")
    contract_check_p.add_argument(
        "--write-report",
        action="store_true",
        default=False,
        help="Write .sikula/contract-reports check report and answers template artifacts",
    )
    return contract_check_p


def register_prepare_parser(contract_subparsers) -> argparse.ArgumentParser:
    contract_prepare_p = contract_subparsers.add_parser(
        "prepare",
        help="Create a project-aware Markdown implementation contract",
    )
    contract_prepare_p.add_argument("task_file", metavar="TASK_FILE", help="Path to refined task .txt/.md file")
    contract_prepare_p.add_argument(
        "--answers",
        help="Path to .sikula/contract-reports/*.answers.yaml created by Sikula prepare/check tooling",
    )
    contract_prepare_p.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Use a read-only LLM assistant to answer supported contract-preparation questions",
    )
    contract_prepare_p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Prompt for missing contract answers before writing the implementation contract",
    )
    contract_prepare_p.add_argument(
        "--output",
        help="Write the implementation contract to this file; defaults to contracts.<stem>.contract.md",
    )
    contract_prepare_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for task_preparer, e.g. --agent-model task_preparer=gpt-5.5",
    )
    contract_prepare_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for task_preparer, e.g. --agent-provider task_preparer=claude",
    )
    contract_prepare_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for task_preparer, e.g. --agent-timeout task_preparer=1200",
    )
    return contract_prepare_p


def _default_resolve_task_path(task_file: str, project_root: Path) -> Path | None:
    path = Path(task_file)
    if path.is_absolute():
        return path if path.exists() else None
    cwd_path = Path.cwd() / path
    return cwd_path if cwd_path.exists() else None


def _default_project_config(cfg: dict) -> dict | None:
    return cfg or None


@dataclass(frozen=True)
class ContractContext:
    resolve_task_path: Callable[[str, Path], Path | None] = _default_resolve_task_path
    project_config: Callable[[dict], dict | None] = _default_project_config
    resolve_contract_report_dir: Callable[[dict], Path] = _resolve_contract_report_dir


def _contract_context(context: ContractContext | None = None) -> ContractContext:
    return context or ContractContext()


@dataclass(frozen=True)
class ContractPrepareContext:
    resolve_task_path: Callable[[str, Path], Path | None]
    project_config: Callable[[dict], dict | None]
    prepare_project_context_from_config: Callable[[dict], dict | None]
    resolve_output_path: Callable[[str], Path]
    default_contract_path: Callable[[Path, dict], Path]
    resolve_contract_report_dir: Callable[[dict], Path]
    load_prepare_answers: Callable[..., dict[str, dict]]
    collect_prepare_answers_interactive: Callable[..., dict[str, dict]]
    resolve_answers_path: Callable[[str], Path]
    existing_prepare_answers_path: Callable[..., Path | None]
    prepare_default_answers_has_current_filled_values: Callable[..., bool]
    run_contract_prepare_auto: Callable[..., object]
    write_prepare_answers_template: Callable[..., Path]
    prepare_answers_path: Callable[..., Path]
    print_project_context_required: Callable[[object, str], None]
    print_existing_output_next_step_note: Callable[[Path], None]
    print_existing_output_hint: Callable[[Path], None]
    print_open_question_details: Callable[[list[dict]], None]


def _contract_prepare_context(context: ContractPrepareContext | None = None) -> ContractPrepareContext:
    if context is None:
        raise RuntimeError("contract prepare requires a ContractPrepareContext")
    return context


def cmd_contract_check(args: argparse.Namespace, cfg: dict, context: ContractContext | None = None) -> None:
    from core.contract_check import check_contract_file, render_contract_check, write_contract_report

    context = _contract_context(context)
    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = context.resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    result = check_contract_file(
        task_path,
        project_config=context.project_config(cfg),
        document_kind="implementation_contract",
    )
    write_result = None
    if args.write_report:
        report_root = project_root if cfg.get("project", {}).get("root_path") else None
        report_dir = context.resolve_contract_report_dir(cfg) if cfg.get("_config_path") else None
        try:
            write_result = write_contract_report(
                result,
                task_path=task_path,
                project_root=report_root,
                report_dir=report_dir,
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to write contract report: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.json:
        data = result.to_dict()
        if write_result:
            data["written_report"] = write_result.to_dict()
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render_contract_check(result), end="")
        if write_result:
            print("Generated contract report artifacts:")
            print(f"- {write_result.report_path}")
            print(f"- {write_result.answers_path}")


def cmd_contract_prepare(
    args: argparse.Namespace,
    cfg: dict,
    context: ContractPrepareContext | None = None,
) -> None:
    from core.contract_check import (
        load_generated_answer_entries_for_contract,
        prepare_implementation_contract,
        render_contract_check,
        write_prepared_contract,
    )

    if args.auto and args.interactive:
        print("Failed to prepare contract: --auto cannot be combined with --interactive", file=sys.stderr)
        sys.exit(2)
    context = _contract_prepare_context(context)

    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = context.resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    task_text = task_path.read_text(encoding="utf-8")
    project_context = context.prepare_project_context_from_config(cfg)
    prepare_project_config = context.project_config(cfg)
    output_path = (
        context.resolve_output_path(args.output) if args.output else context.default_contract_path(task_path, cfg)
    )
    report_root = project_root if cfg.get("project", {}).get("root_path") else None
    report_dir = context.resolve_contract_report_dir(cfg) if cfg.get("_config_path") else None
    generated_answer_entries = load_generated_answer_entries_for_contract(
        task_path,
        source_text=task_text,
        project_root=report_root,
        report_dir=report_dir,
    )
    if project_context is None or not project_context.get("validation_commands"):
        result = prepare_implementation_contract(
            task_text,
            contract_name=str(task_path),
            project_context=project_context,
            project_config=prepare_project_config,
            generated_answer_entries=generated_answer_entries,
        )
        if result.required_next_step == "provide_project_context":
            context.print_project_context_required(result, args.task_file)
            if output_path.exists():
                context.print_existing_output_next_step_note(output_path)
            sys.exit(1)

    answers: dict[str, dict] = {}
    answers_supplied = bool(args.interactive or args.answers)
    existing_default_answers_path = None
    if args.interactive:
        try:
            first = prepare_implementation_contract(
                task_text,
                contract_name=str(task_path),
                project_context=project_context,
                project_config=prepare_project_config,
                generated_answer_entries=generated_answer_entries,
            )
            answers = context.collect_prepare_answers_interactive(
                generated_by="sikula.contract_prepare",
                label="contract preparation",
                source_path=task_path,
                source_text=task_text,
                project_root=project_root,
                questions=first.user_questions,
                cfg=cfg,
                answers_path=context.resolve_answers_path(args.answers) if args.answers else None,
            )
        except (EOFError, OSError, ValueError) as exc:
            print(f"Failed to collect contract answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.answers:
        try:
            answers = context.load_prepare_answers(
                context.resolve_answers_path(args.answers), source_path=task_path, source_text=task_text
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to load contract answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.auto:
        existing_default_answers_path = context.existing_prepare_answers_path(
            task_path,
            cfg,
            generated_by="sikula.contract_prepare",
        )

    result = prepare_implementation_contract(
        task_text,
        contract_name=str(task_path),
        answers=answers,
        project_context=project_context,
        project_config=prepare_project_config,
        generated_answer_entries=generated_answer_entries,
    )
    if result.required_next_step == "provide_project_context":
        context.print_project_context_required(result, args.task_file)
        if output_path.exists():
            context.print_existing_output_next_step_note(output_path)
        sys.exit(1)

    auto_answer_count = 0
    if args.auto and output_path.exists():
        print(f"Failed to prepare contract: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        context.print_existing_output_hint(output_path)
        sys.exit(1)

    if args.auto and existing_default_answers_path:
        try:
            has_current_filled_default_answers = context.prepare_default_answers_has_current_filled_values(
                answers_path=existing_default_answers_path,
                generated_by="sikula.contract_prepare",
                source_path=task_path,
                source_text=task_text,
                project_root=project_root,
                questions=result.user_questions,
                cfg=cfg,
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to inspect existing contract answers: {exc}", file=sys.stderr)
            sys.exit(1)
        if has_current_filled_default_answers:
            print(
                "Failed to auto-prepare contract: existing contract answers contain filled values; "
                f"rerun with --answers {existing_default_answers_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.auto and result.user_questions:
        try:
            auto_result = context.run_contract_prepare_auto(
                args=args,
                cfg=cfg,
                project_root=project_root,
                source_path=task_path,
                task_text=task_text,
                output_path=output_path,
                project_context=project_context,
                generated_answer_entries=generated_answer_entries,
                answers=answers,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Failed to auto-prepare contract: {exc}", file=sys.stderr)
            sys.exit(1)
        result = auto_result.result
        answers = auto_result.answers
        auto_answer_count = len(auto_result.auto_answers)
        if args.answers and auto_answer_count:
            try:
                context.write_prepare_answers_template(
                    generated_by="sikula.contract_prepare",
                    source_path=task_path,
                    source_text=task_text,
                    project_root=project_root,
                    questions=result.user_questions,
                    cfg=cfg,
                    answers=answers,
                    answers_path=context.resolve_answers_path(args.answers),
                )
            except (OSError, ValueError) as exc:
                print(f"Failed to update contract answers: {exc}", file=sys.stderr)
                sys.exit(1)
        elif existing_default_answers_path and auto_answer_count and not result.needs_user_input:
            try:
                context.write_prepare_answers_template(
                    generated_by="sikula.contract_prepare",
                    source_path=task_path,
                    source_text=task_text,
                    project_root=project_root,
                    questions=result.user_questions,
                    cfg=cfg,
                    answers=answers,
                )
            except (OSError, ValueError) as exc:
                print(f"Failed to update contract answers: {exc}", file=sys.stderr)
                sys.exit(1)

    if result.needs_user_input and not answers_supplied:
        answers_path = context.write_prepare_answers_template(
            generated_by="sikula.contract_prepare",
            source_path=task_path,
            source_text=task_text,
            project_root=project_root,
            questions=result.user_questions,
            cfg=cfg,
            answers=answers if args.auto else None,
        )
        print("Contract preparation needs answers before writing an implementation contract.")
        print(f"Contract preparation answers template written: {answers_path}")
        if args.auto:
            print(f"Auto-applied answers: {auto_answer_count}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        context.print_open_question_details(result.user_questions)
        print("Next step:")
        print(f"- Fill the answers file, then run: sikula contract prepare {args.task_file} --answers {answers_path}")
        print(f"- Or answer in the terminal: sikula contract prepare {args.task_file} --interactive")
        if output_path.exists():
            context.print_existing_output_next_step_note(output_path)
        sys.exit(1)

    if result.required_next_step == "revise_contract":
        print("Contract preparation needs task description revisions before writing an implementation contract.")
        print("")
        print(render_contract_check(result.recheck_result or result.check_result), end="")
        if result.suggested_next_steps:
            print("")
            print("Next step:")
            for step in result.suggested_next_steps:
                print(f"- {step}")
        if output_path.exists():
            context.print_existing_output_next_step_note(output_path)
        sys.exit(1)

    if output_path.exists():
        print(f"Failed to prepare contract: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        context.print_existing_output_hint(output_path)
        sys.exit(1)

    try:
        write_prepared_contract(
            result,
            output_path=output_path,
            project_root=report_root,
            report_dir=report_dir,
        )
    except (OSError, ValueError) as exc:
        print(f"Failed to prepare contract: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Implementation contract written: {output_path}")
    if args.auto:
        print(f"Auto-applied answers: {auto_answer_count}")
    print(f"Applied answers: {len(result.answered_question_ids)}")
    print(f"Open questions: {len(result.open_question_ids)}")
    print("")
    print(render_contract_check(result.recheck_result or result.check_result), end="")
    if result.ready_to_run:
        print("")
        print(f"Next step: sikula run {output_path}")
    elif result.needs_user_input:
        answers_path = (
            context.resolve_answers_path(args.answers)
            if args.answers
            else context.prepare_answers_path(
                task_path,
                cfg,
                generated_by="sikula.contract_prepare",
            )
        )
        print("")
        print("Next step:")
        print(f"- Fill/update the answers file: {answers_path}")
        print(
            "- Then rerun contract prepare with a new --output path, or remove/rename the output written above first."
        )
    else:
        print("")
        print(f"Next step: review the contract check output above before running sikula run {output_path}")
