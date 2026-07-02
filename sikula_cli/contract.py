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
