"""Delivery-plan CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import contextlib
import copy
from dataclasses import dataclass, field, replace
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from typing import Any

from core.delivery_authoring import DeliveryAuthoringDraft, DeliveryAuthoringParseError
from core.delivery_plan import delivery_final_branch_for_plan_id, is_valid_delivery_branch_name
from core.delivery_prepare_writer import DeliveryPrepareWriteResult, write_delivery_prepare_artifacts
from sikula_cli.agent_overrides import DELIVERY_PREPARATION_AGENT_NAMES, parse_agent_llm_overrides

_DELIVERY_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DELIVERY_PREPARE_CONTEXT_MISSING_MESSAGE = "Delivery prepare authoring requires the main Sikula command context."
_DELIVERY_PREPARE_AUTHORING_FAILED_MESSAGE = (
    "Delivery authoring assistant failed; see local audit artifacts for details."
)
_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE = (
    "Delivery authoring assistant returned an invalid draft; see local audit artifacts for details."
)
_DELIVERY_PREPARE_AUTHORING_FAILED_HINT = (
    "Delivery prepare authoring failed; inspect the local audit artifact and retry."
)
_DELIVERY_PREPARE_WRITE_FAILED_MESSAGE = (
    "Delivery prepare failed while writing artifacts; no source artifacts were finalized."
)
_DELIVERY_PREPARE_VALIDATION_FAILED_MESSAGE = (
    "Generated delivery plan artifacts failed validation; no source artifacts were finalized."
)
_DELIVERY_PREPARE_UNIT_READINESS_FAILED_MESSAGE = (
    "Generated unit task contracts have blocking readiness gaps; no source artifacts were finalized."
)
_DELIVERY_PREPARE_UNIT_READINESS_FAILURE = "unit_readiness_blocked"
_DELIVERY_PREPARE_PLAN_VALIDATION_FAILURE = "plan_validation_failed"
_DELIVERY_PREPARE_FORBIDDEN_OUTPUT_ROOTS = (
    (".git",),
    (".sikula", "state"),
    (".sikula", "worktrees"),
    (".sikula", "contract-reports"),
)


def register_parser(subparsers) -> argparse.ArgumentParser:
    delivery_p = subparsers.add_parser("delivery", help="Inspect and run delivery plans")
    delivery_sub = delivery_p.add_subparsers(dest="delivery_command")

    delivery_check_p = delivery_sub.add_parser("check", help="Check a delivery plan file")
    delivery_check_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_check_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    delivery_prepare_p = delivery_sub.add_parser("prepare", help="Prepare delivery plan artifacts from a task file")
    delivery_prepare_p.add_argument("task_file", metavar="TASK_FILE", help="Path to source task .txt/.md file")
    delivery_prepare_p.add_argument(
        "--output",
        metavar="DIR",
        help="Delivery plan output directory; defaults to .sikula/delivery/<task-stem>/",
    )
    delivery_prepare_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Allow replacing existing delivery plan artifacts in the output directory",
    )
    delivery_prepare_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")
    delivery_prepare_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for delivery_preparer, e.g. --agent-model delivery_preparer=gpt-5.5",
    )
    delivery_prepare_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for delivery_preparer, e.g. --agent-provider delivery_preparer=claude",
    )
    delivery_prepare_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for delivery_preparer, e.g. --agent-timeout delivery_preparer=1200",
    )

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
    delivery_run_next_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one child run agent, e.g. --agent-model analyst=gpt-5.5",
    )
    delivery_run_next_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one child run agent, e.g. --agent-provider implementer=claude",
    )
    delivery_run_next_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for one child run agent, e.g. --agent-timeout implementer=2400",
    )

    delivery_finalize_p = delivery_sub.add_parser("finalize", help="Create or update a delivery plan final branch")
    delivery_finalize_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_finalize_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview final branch updates without writing delivery progress or Git refs",
    )
    delivery_finalize_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

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
class DeliveryPrepareIssue:
    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True)
class DeliveryPrepareArtifact:
    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True)
class DeliveryPrepareAuthoringSummary:
    drafted: bool = False
    unit_count: int = 0
    planning_mode: str | None = None
    audit_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "drafted": self.drafted,
            "unit_count": self.unit_count,
            "planning_mode": self.planning_mode,
            "audit_path": self.audit_path,
        }


def _delivery_prepare_plan_validation_not_run() -> dict[str, Any]:
    return {"status": "not_run", "valid": None, "errors": [], "warnings": []}


def _delivery_prepare_unit_readiness_not_run() -> dict[str, Any]:
    return {"status": "not_run", "units": []}


@dataclass(frozen=True)
class DeliveryPrepareResult:
    status: str
    ready: bool
    prepared: bool
    force: bool
    overwrite_allowed: bool
    selected_plan_id: str | None
    unit_ids: list[str]
    paths: dict[str, str | None]
    existing_artifacts: list[DeliveryPrepareArtifact]
    errors: list[DeliveryPrepareIssue]
    unit_task_paths: dict[str, str] = field(default_factory=dict)
    written_artifacts: list[DeliveryPrepareArtifact] = field(default_factory=list)
    plan_validation: dict[str, Any] = field(default_factory=_delivery_prepare_plan_validation_not_run)
    unit_readiness: dict[str, Any] = field(default_factory=_delivery_prepare_unit_readiness_not_run)
    authoring: DeliveryPrepareAuthoringSummary = field(default_factory=DeliveryPrepareAuthoringSummary)
    authoring_draft: DeliveryAuthoringDraft | None = field(default=None, repr=False, compare=False)
    warnings: list[DeliveryPrepareIssue] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "prepared": self.prepared,
            "force": self.force,
            "overwrite_allowed": self.overwrite_allowed,
            "selected_plan_id": self.selected_plan_id,
            "unit_ids": list(self.unit_ids),
            "paths": dict(self.paths),
            "unit_task_paths": dict(self.unit_task_paths),
            "written_artifacts": [artifact.to_dict() for artifact in self.written_artifacts],
            "existing_artifacts": [artifact.to_dict() for artifact in self.existing_artifacts],
            "plan_validation": copy.deepcopy(self.plan_validation),
            "unit_readiness": copy.deepcopy(self.unit_readiness),
            "authoring": self.authoring.to_dict(),
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "message": self.message,
        }


@dataclass(frozen=True)
class DeliveryPrepareContext:
    run_authoring_assistant: Callable[..., DeliveryAuthoringDraft]


def cmd_delivery_prepare(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryPrepareContext | None = None,
) -> None:
    override_errors = _validate_delivery_prepare_agent_overrides(args)
    result = _prepare_delivery_preflight(args, cfg, extra_errors=override_errors)
    if result.ready:
        if context is None:
            result = _delivery_prepare_blocked_result(
                result,
                code="delivery_prepare.authoring_context_missing",
                issue_message=_DELIVERY_PREPARE_CONTEXT_MISSING_MESSAGE,
                result_message=_DELIVERY_PREPARE_CONTEXT_MISSING_MESSAGE,
            )
        else:
            result = _run_delivery_prepare_authoring(args, cfg, result, context)
    _print_delivery_result(result, json_output=args.json, render=render_delivery_prepare)
    if not result.ready:
        sys.exit(1)


def render_delivery_prepare(result: DeliveryPrepareResult) -> str:
    task_path = result.paths.get("task_file") or "<unknown>"
    lines = [
        f"Delivery prepare: {task_path}",
        f"Status: {result.status}",
    ]
    if result.selected_plan_id:
        lines.append(f"Selected plan: {result.selected_plan_id}")
    if result.paths.get("output_dir"):
        lines.append(f"Output: {result.paths['output_dir']}")
    if result.paths.get("plan_file"):
        lines.append(f"Plan file: {result.paths['plan_file']}")
    if result.paths.get("units_dir"):
        lines.append(f"Units dir: {result.paths['units_dir']}")
    lines.append(f"Overwrite allowed: {'yes' if result.overwrite_allowed else 'no'}")
    lines.append(f"Draft units: {len(result.unit_ids)}")
    if result.authoring.audit_path:
        lines.append(f"Authoring audit: {result.authoring.audit_path}")
    if result.existing_artifacts:
        lines.append("Existing artifacts:")
        for artifact in result.existing_artifacts:
            lines.append(f"- {artifact.kind}: {artifact.path}")
    if result.written_artifacts:
        lines.append("Written artifacts:")
        for artifact in result.written_artifacts:
            lines.append(f"- {artifact.kind}: {artifact.path}")
    if result.unit_task_paths:
        lines.append("Unit task paths:")
        for unit_id, task_path in result.unit_task_paths.items():
            lines.append(f"- {unit_id}: {task_path}")
    plan_validation_status = result.plan_validation.get("status", "not_run")
    if plan_validation_status != "not_run":
        lines.append("Plan validation:")
        lines.append(f"- status: {plan_validation_status}")
        if result.plan_validation.get("valid") is not None:
            lines.append(f"- valid: {'yes' if result.plan_validation.get('valid') else 'no'}")
        lines.append(f"- errors: {len(result.plan_validation.get('errors') or [])}")
        lines.append(f"- warnings: {len(result.plan_validation.get('warnings') or [])}")
    unit_readiness_status = result.unit_readiness.get("status", "not_run")
    if unit_readiness_status != "not_run":
        lines.append("Unit readiness:")
        lines.append(f"- status: {unit_readiness_status}")
        for unit in result.unit_readiness.get("units") or []:
            if not isinstance(unit, dict):
                continue
            unit_id = unit.get("unit_id") or "<unknown>"
            unit_status = unit.get("status") or "<unknown>"
            readiness_score = unit.get("readiness_score")
            blocking_count = unit.get("blocking_gap_count")
            warning_count = unit.get("warning_gap_count")
            lines.append(
                f"- {unit_id}: {unit_status}, score {readiness_score}, "
                f"blocking {blocking_count}, warnings {warning_count}"
            )
    lines.append(result.message)
    if _delivery_prepare_authoring_failed(result):
        lines.append(_DELIVERY_PREPARE_AUTHORING_FAILED_HINT)
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for issue in result.errors:
            lines.append(_format_prepare_issue(issue))
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in result.warnings:
            lines.append(_format_prepare_issue(issue))
    return "\n".join(lines) + "\n"


def _run_delivery_prepare_authoring(
    args: argparse.Namespace,
    cfg: dict,
    preflight: DeliveryPrepareResult,
    context: DeliveryPrepareContext,
) -> DeliveryPrepareResult:
    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = _resolve_from_cwd(args.task_file)
    output_dir = preflight.paths.get("output_dir")
    selected_plan_id = preflight.selected_plan_id
    if selected_plan_id is None:
        return _delivery_prepare_blocked_result(
            preflight,
            code="delivery_prepare.authoring_invalid",
            issue_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
            result_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
        )
    if output_dir is None:
        return _delivery_prepare_blocked_result(
            preflight,
            code="delivery_prepare.output_outside_project",
            issue_message="Output path must be inside the project root.",
            result_message=_DELIVERY_PREPARE_WRITE_FAILED_MESSAGE,
        )
    output_path = project_root / output_dir

    try:
        draft = context.run_authoring_assistant(
            args=args,
            cfg=cfg,
            task_path=task_path,
            output_dir=output_path,
            selected_plan_id=selected_plan_id,
            project_root=project_root,
        )
    except DeliveryAuthoringParseError as exc:
        return _delivery_prepare_blocked_result(
            preflight,
            code="delivery_prepare.authoring_invalid",
            issue_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
            result_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
            audit_path=_delivery_prepare_exception_audit_path(exc, project_root),
        )
    except Exception as exc:
        return _delivery_prepare_blocked_result(
            preflight,
            code="delivery_prepare.authoring_failed",
            issue_message=_DELIVERY_PREPARE_AUTHORING_FAILED_MESSAGE,
            result_message=_DELIVERY_PREPARE_AUTHORING_FAILED_MESSAGE,
            audit_path=_delivery_prepare_exception_audit_path(exc, project_root),
        )
    if not isinstance(draft, DeliveryAuthoringDraft):
        return _delivery_prepare_blocked_result(
            preflight,
            code="delivery_prepare.authoring_invalid",
            issue_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
            result_message=_DELIVERY_PREPARE_AUTHORING_INVALID_MESSAGE,
        )

    audit_path = _delivery_prepare_authoring_audit_path(draft, project_root)
    authoring_result = replace(
        preflight,
        unit_ids=[unit.id for unit in draft.units],
        authoring=DeliveryPrepareAuthoringSummary(
            drafted=True,
            unit_count=len(draft.units),
            planning_mode=draft.planning_mode,
            audit_path=audit_path,
        ),
        authoring_draft=draft,
        warnings=[
            *preflight.warnings,
            *_delivery_prepare_authoring_warnings(draft.warnings, audit_path=audit_path),
        ],
    )
    write_result = write_delivery_prepare_artifacts(
        draft,
        output_dir=output_dir,
        project_root=project_root,
        project_config=cfg,
        force=bool(getattr(args, "force", False)),
    )
    return _delivery_prepare_result_from_write_result(authoring_result, write_result)


def _delivery_prepare_result_from_write_result(
    result: DeliveryPrepareResult,
    write_result: DeliveryPrepareWriteResult,
) -> DeliveryPrepareResult:
    paths = dict(result.paths)
    if write_result.paths.plan_file:
        paths["plan_file"] = write_result.paths.plan_file
    if write_result.paths.units_dir:
        paths["units_dir"] = write_result.paths.units_dir

    if write_result.prepared:
        return replace(
            result,
            status="ready",
            ready=True,
            prepared=True,
            paths=paths,
            unit_task_paths=dict(write_result.unit_task_paths),
            written_artifacts=_delivery_prepare_artifacts_from_writer(write_result),
            plan_validation=write_result.plan_validation.to_dict(),
            unit_readiness=write_result.unit_readiness.to_dict(),
            errors=[*result.errors, *_delivery_prepare_issues_from_writer(write_result.errors)],
            warnings=[*result.warnings, *_delivery_prepare_issues_from_writer(write_result.warnings)],
            message="Delivery plan artifacts written.",
        )

    code, message = _delivery_prepare_writer_failure(write_result.failure_reason)
    writer_errors = _delivery_prepare_issues_from_writer(write_result.errors)
    return replace(
        result,
        status="blocked",
        ready=False,
        prepared=False,
        paths=paths,
        unit_task_paths=dict(write_result.unit_task_paths),
        written_artifacts=_delivery_prepare_artifacts_from_writer(write_result),
        plan_validation=write_result.plan_validation.to_dict(),
        unit_readiness=write_result.unit_readiness.to_dict(),
        errors=[
            *result.errors,
            DeliveryPrepareIssue("error", code, message),
            *(issue for issue in writer_errors if issue.code != code),
        ],
        warnings=[*result.warnings, *_delivery_prepare_issues_from_writer(write_result.warnings)],
        message=message,
    )


def _delivery_prepare_artifacts_from_writer(
    write_result: DeliveryPrepareWriteResult,
) -> list[DeliveryPrepareArtifact]:
    return [DeliveryPrepareArtifact(artifact.kind, artifact.path) for artifact in write_result.written_artifacts]


def _delivery_prepare_authoring_warnings(
    warnings: list[str],
    *,
    audit_path: str | None,
) -> list[DeliveryPrepareIssue]:
    if not warnings:
        return []
    return [
        DeliveryPrepareIssue(
            "warning",
            "delivery_prepare.authoring_warnings_present",
            "Delivery authoring assistant reported warnings; inspect the local audit artifact for details.",
            audit_path,
        )
    ]


def _delivery_prepare_issues_from_writer(issues: list[Any]) -> list[DeliveryPrepareIssue]:
    result: list[DeliveryPrepareIssue] = []
    for issue in issues:
        path = issue.path if isinstance(issue.path, str) or issue.path is None else str(issue.path)
        result.append(
            DeliveryPrepareIssue(
                str(issue.severity),
                str(issue.code),
                str(issue.message),
                path,
            )
        )
    return result


def _delivery_prepare_writer_failure(failure_reason: str | None) -> tuple[str, str]:
    if failure_reason == _DELIVERY_PREPARE_UNIT_READINESS_FAILURE:
        return "delivery_prepare.unit_readiness_blocked", _DELIVERY_PREPARE_UNIT_READINESS_FAILED_MESSAGE
    if failure_reason == _DELIVERY_PREPARE_PLAN_VALIDATION_FAILURE:
        return "delivery_prepare.plan_validation_failed", _DELIVERY_PREPARE_VALIDATION_FAILED_MESSAGE
    return "delivery_prepare.write_failed", _DELIVERY_PREPARE_WRITE_FAILED_MESSAGE


def _delivery_prepare_blocked_result(
    result: DeliveryPrepareResult,
    *,
    code: str,
    issue_message: str,
    result_message: str,
    audit_path: str | None = None,
) -> DeliveryPrepareResult:
    return replace(
        result,
        status="blocked",
        ready=False,
        prepared=False,
        authoring=replace(
            result.authoring,
            audit_path=audit_path if audit_path is not None else result.authoring.audit_path,
        ),
        errors=[
            *result.errors,
            DeliveryPrepareIssue(
                "error",
                code,
                issue_message,
            ),
        ],
        message=result_message,
    )


def _delivery_prepare_authoring_audit_path(draft: DeliveryAuthoringDraft, project_root: Path) -> str | None:
    raw_path = getattr(draft, "audit_path", None)
    return _delivery_prepare_safe_audit_path(raw_path, project_root)


def _delivery_prepare_exception_audit_path(exc: BaseException, project_root: Path) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        audit_path = _delivery_prepare_safe_audit_path(getattr(current, "audit_path", None), project_root)
        if audit_path is not None:
            return audit_path
        current = current.__cause__ or current.__context__
    return None


def _delivery_prepare_safe_audit_path(raw_path: Any, project_root: Path) -> str | None:
    if not raw_path:
        return None
    try:
        audit_path = Path(raw_path)
        resolved = audit_path.resolve() if audit_path.is_absolute() else (project_root / audit_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not _path_is_within(resolved, project_root):
        return None
    return _project_relative_path(resolved, project_root)


def _delivery_prepare_authoring_failed(result: DeliveryPrepareResult) -> bool:
    return any(
        issue.code in {"delivery_prepare.authoring_failed", "delivery_prepare.authoring_invalid"}
        for issue in result.errors
    )


def _validate_delivery_prepare_agent_overrides(args: argparse.Namespace) -> list[DeliveryPrepareIssue]:
    try:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            parse_agent_llm_overrides(
                getattr(args, "agent_model", None),
                getattr(args, "agent_provider", None),
                getattr(args, "agent_timeout", None),
                valid_agents=DELIVERY_PREPARATION_AGENT_NAMES,
            )
    except SystemExit as exc:
        if _system_exit_code(exc) == 0:
            return []
        message = output.getvalue().strip() or "Invalid delivery_preparer override."
        return [
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.agent_override_invalid",
                message,
            )
        ]
    return []


def _prepare_delivery_preflight(
    args: argparse.Namespace,
    cfg: dict,
    *,
    extra_errors: list[DeliveryPrepareIssue] | None = None,
) -> DeliveryPrepareResult:
    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    force = bool(getattr(args, "force", False))
    errors: list[DeliveryPrepareIssue] = list(extra_errors or [])
    warnings: list[DeliveryPrepareIssue] = []

    task_path = _resolve_from_cwd(args.task_file)
    task_rel = _project_relative_path(task_path, project_root) if _path_is_within(task_path, project_root) else None
    _validate_delivery_prepare_task_path(task_path, task_rel, project_root, errors)

    output_arg = getattr(args, "output", None)
    if output_arg:
        _validate_delivery_prepare_output_arg(output_arg, errors)
    output_path = _resolve_delivery_prepare_output_path(args, task_path, project_root)
    output_rel = (
        _project_relative_path(output_path, project_root) if _path_is_within(output_path, project_root) else None
    )
    output_error_count = len(errors)
    _validate_delivery_prepare_output_components(output_path, project_root, errors)
    output_has_symlink = any(issue.code == "delivery_prepare.output_symlink" for issue in errors[output_error_count:])
    if output_rel is None:
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.output_outside_project",
                "Output path must be inside the project root.",
            )
        )
    elif _is_forbidden_delivery_prepare_output_root(output_rel):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.output_runtime_artifact",
                "Output path must not be inside Sikula runtime, debug, or VCS metadata directories.",
                output_rel,
            )
        )

    plan_id = output_path.name
    if not _DELIVERY_PLAN_ID_RE.fullmatch(plan_id):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.plan_id_invalid",
                "plan_id may contain only letters, numbers, dots, underscores, and hyphens.",
                output_rel,
            )
        )
    elif not is_valid_delivery_branch_name(delivery_final_branch_for_plan_id(plan_id)):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.final_branch_invalid",
                "Selected plan_id would create an invalid final branch name.",
                output_rel,
            )
        )

    plan_file = output_path / "plan.yaml"
    units_dir = output_path / "units"
    plan_rel = _project_relative_path(plan_file, project_root) if output_rel is not None else None
    units_rel = _project_relative_path(units_dir, project_root) if output_rel is not None else None

    if output_rel is not None and not output_has_symlink and output_path.exists() and not output_path.is_dir():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.output_not_directory",
                "Output path already exists and is not a directory.",
                output_rel,
            )
        )

    existing_artifacts = (
        _find_delivery_prepare_existing_artifacts(plan_file, units_dir, project_root)
        if output_rel is not None and not output_has_symlink
        else []
    )
    if force and output_rel is not None and not output_has_symlink:
        _validate_delivery_prepare_non_replaceable_artifacts(plan_file, units_dir, project_root, errors)
    if existing_artifacts and not force:
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.existing_artifacts",
                "Existing delivery plan artifacts require --force to replace.",
                output_rel,
            )
        )

    ready = not errors
    status = "ready" if ready else "blocked"
    return DeliveryPrepareResult(
        status=status,
        ready=ready,
        prepared=False,
        force=force,
        overwrite_allowed=force,
        selected_plan_id=plan_id if _DELIVERY_PLAN_ID_RE.fullmatch(plan_id) else None,
        unit_ids=[],
        paths={
            "task_file": task_rel,
            "output_dir": output_rel,
            "plan_file": plan_rel,
            "units_dir": units_rel,
        },
        unit_task_paths={},
        written_artifacts=[],
        existing_artifacts=existing_artifacts,
        plan_validation=_delivery_prepare_plan_validation_not_run(),
        unit_readiness=_delivery_prepare_unit_readiness_not_run(),
        errors=errors,
        warnings=warnings,
        message=(
            "Delivery prepare preflight passed."
            if ready
            else "Delivery prepare is blocked; fix the reported errors and retry."
        ),
    )


def _resolve_delivery_prepare_output_path(args: argparse.Namespace, task_path: Path, project_root: Path) -> Path:
    output = getattr(args, "output", None)
    if output:
        return project_root / Path(output)
    return project_root / ".sikula" / "delivery" / _kebab_case_slug(task_path.stem)


def _validate_delivery_prepare_task_path(
    task_path: Path,
    task_rel: str | None,
    project_root: Path,
    errors: list[DeliveryPrepareIssue],
) -> None:
    if not _path_is_within(task_path, project_root):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.task_outside_project",
                "Task path must be inside the project root.",
            )
        )
        return
    if not task_path.exists():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.task_missing",
                "Task file does not exist.",
                task_rel,
            )
        )
        return
    if not task_path.is_file():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.task_not_file",
                "Task path is not a file.",
                task_rel,
            )
        )
        return
    try:
        task_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.task_not_utf8",
                "Task file is not readable as UTF-8 text.",
                task_rel,
            )
        )
    except OSError:
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.task_unreadable",
                "Task file could not be read.",
                task_rel,
            )
        )


def _validate_delivery_prepare_output_arg(output: str | Path, errors: list[DeliveryPrepareIssue]) -> None:
    if _is_absolute_delivery_prepare_path(output):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.output_absolute",
                "Output path must be project-relative.",
            )
        )
    if _has_delivery_prepare_parent_traversal(output):
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.output_traversal",
                "Output path must not contain parent-directory traversal.",
            )
        )


def _validate_delivery_prepare_output_components(
    output_path: Path,
    project_root: Path,
    errors: list[DeliveryPrepareIssue],
) -> None:
    for component in _existing_delivery_prepare_path_components(output_path, project_root):
        component_rel = _project_relative_path(component, project_root)
        if component.is_symlink():
            errors.append(
                DeliveryPrepareIssue(
                    "error",
                    "delivery_prepare.output_symlink",
                    "Output directory must not contain symlink components.",
                    component_rel,
                )
            )
            return
        if component != output_path and not component.is_dir():
            errors.append(
                DeliveryPrepareIssue(
                    "error",
                    "delivery_prepare.output_not_directory",
                    "Output path component already exists and is not a directory.",
                    component_rel,
                )
            )
            return


def _existing_delivery_prepare_path_components(path: Path, project_root: Path) -> list[Path]:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return []
    components: list[Path] = []
    current = project_root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        components.append(current)
    return components


def _find_delivery_prepare_existing_artifacts(
    plan_file: Path,
    units_dir: Path,
    project_root: Path,
) -> list[DeliveryPrepareArtifact]:
    artifacts: list[DeliveryPrepareArtifact] = []
    if plan_file.exists() or plan_file.is_symlink():
        artifacts.append(DeliveryPrepareArtifact("plan", _project_relative_path(plan_file, project_root)))
    if units_dir.is_symlink():
        artifacts.append(DeliveryPrepareArtifact("unit_task", _project_relative_path(units_dir, project_root)))
        return artifacts
    if units_dir.exists() and not units_dir.is_dir():
        artifacts.append(DeliveryPrepareArtifact("unit_task", _project_relative_path(units_dir, project_root)))
        return artifacts
    if units_dir.is_dir():
        resolved_units_dir = units_dir.resolve()
        for unit_path in _iter_delivery_prepare_unit_artifacts(units_dir):
            if not unit_path.is_symlink() and not _path_is_within(unit_path, resolved_units_dir):
                continue
            artifacts.append(DeliveryPrepareArtifact("unit_task", _project_relative_path(unit_path, project_root)))
    return artifacts


def _validate_delivery_prepare_non_replaceable_artifacts(
    plan_file: Path,
    units_dir: Path,
    project_root: Path,
    errors: list[DeliveryPrepareIssue],
) -> None:
    if plan_file.is_symlink():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.symlink_artifact",
                "Delivery prepare artifacts must not be symlinks.",
                _project_relative_path(plan_file, project_root),
            )
        )
        return
    if plan_file.exists() and not plan_file.is_file():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.target_not_file",
                "Delivery prepare target already exists and is not a regular file.",
                _project_relative_path(plan_file, project_root),
            )
        )
        return
    if units_dir.is_symlink():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.units_dir_symlink",
                "Delivery prepare units directory must not be a symlink.",
                _project_relative_path(units_dir, project_root),
            )
        )
        return
    if units_dir.exists() and not units_dir.is_dir():
        errors.append(
            DeliveryPrepareIssue(
                "error",
                "delivery_prepare.units_dir_not_directory",
                "Delivery prepare units path already exists and is not a directory.",
                _project_relative_path(units_dir, project_root),
            )
        )
        return
    if units_dir.is_dir():
        for unit_path in _iter_delivery_prepare_unit_artifacts(units_dir):
            if unit_path.is_symlink():
                errors.append(
                    DeliveryPrepareIssue(
                        "error",
                        "delivery_prepare.symlink_artifact",
                        "Delivery prepare artifacts must not be symlinks.",
                        _project_relative_path(unit_path, project_root),
                    )
                )
                return


def _iter_delivery_prepare_unit_artifacts(units_dir: Path) -> list[Path]:
    artifacts: list[Path] = []
    pending = [units_dir]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                artifacts.append(child)
            elif child.is_file():
                artifacts.append(child)
            elif child.is_dir():
                pending.append(child)
    return sorted(artifacts)


def _resolve_from_cwd(path: str | Path) -> Path:
    raw_path = Path(path)
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (Path.cwd() / raw_path).resolve()


def _is_absolute_delivery_prepare_path(path: str | Path) -> bool:
    raw_path = str(path)
    windows_path = PureWindowsPath(raw_path)
    return (
        Path(raw_path).is_absolute()
        or PurePosixPath(raw_path).is_absolute()
        or bool(windows_path.drive or windows_path.root)
    )


def _has_delivery_prepare_parent_traversal(path: str | Path) -> bool:
    raw_path = str(path)
    return ".." in PurePosixPath(raw_path).parts or ".." in PureWindowsPath(raw_path).parts


def _is_forbidden_delivery_prepare_output_root(path: str | Path) -> bool:
    raw_path = str(path)
    return _has_forbidden_delivery_prepare_parts(PurePosixPath(raw_path).parts) or (
        _has_forbidden_delivery_prepare_parts(PureWindowsPath(raw_path).parts)
    )


def _has_forbidden_delivery_prepare_parts(parts: tuple[str, ...]) -> bool:
    normalized = tuple(part.casefold() for part in parts if part not in {"", "."})
    return any(normalized[: len(root)] == root for root in _DELIVERY_PREPARE_FORBIDDEN_OUTPUT_ROOTS)


def _project_relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _kebab_case_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "delivery-plan"


def _format_prepare_issue(issue: DeliveryPrepareIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"


def cmd_delivery_finalize(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_finalize import finalize_delivery_plan, preview_delivery_finalize, render_delivery_finalize

    project_root_raw = cfg.get("project", {}).get("root_path") if isinstance(cfg, dict) else None
    project_root = Path(project_root_raw).resolve() if project_root_raw else None
    if getattr(args, "dry_run", False):
        result = preview_delivery_finalize(args.plan_file, project_root=project_root)
        _print_delivery_result(result, json_output=args.json, render=render_delivery_finalize)
        if not result.ready:
            sys.exit(1)
        return

    result = finalize_delivery_plan(args.plan_file, project_root=project_root)
    _print_delivery_result(result, json_output=args.json, render=render_delivery_finalize)
    if not result.finalized:
        sys.exit(1)


@dataclass(frozen=True)
class DeliveryChildRunResult:
    exit_code: int
    child_task_id: str | None = None
    interrupted: bool = False
    exception: BaseException | None = None
    child_link_failed: bool = False


class DeliveryChildLinkFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryRunNextContext:
    run_task: Callable[[argparse.Namespace, dict], DeliveryChildRunResult | int]
    resolve_state_dir: Callable[[dict], Path]


@dataclass(frozen=True)
class DeliveryChildRunClassification:
    unit_status: str
    failure_code: str | None


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

    _validate_delivery_run_next_agent_overrides(args)
    project_root_raw = cfg.get("project", {}).get("root_path") if isinstance(cfg, dict) else None
    project_root = Path(project_root_raw).resolve() if project_root_raw else None
    if getattr(args, "dry_run", False):
        result = preview_delivery_run_next(args.plan_file, project_root=project_root)
        result = _apply_delivery_preview_execution_guards(result, args.plan_file, project_root=project_root)
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


def _validate_delivery_run_next_agent_overrides(args: argparse.Namespace) -> None:
    parse_agent_llm_overrides(
        getattr(args, "agent_model", None),
        getattr(args, "agent_provider", None),
        getattr(args, "agent_timeout", None),
    )


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

        def link_child_task(child_task_id: str) -> None:
            nonlocal progress
            try:
                linked_unit = make_delivery_unit_progress(
                    selected_unit.id,
                    "running",
                    child_task_id=child_task_id,
                    timestamp=running_unit.started_at,
                )
                progress = upsert_delivery_unit_progress(progress, linked_unit)
                write_delivery_progress(progress_path, progress)
                append_delivery_progress_event(
                    events_path,
                    make_delivery_progress_event(plan_id, "unit.child_linked", unit=linked_unit),
                )
            except Exception as exc:
                raise DeliveryChildLinkFailed() from exc

        try:
            delivery_plan_path = Path(status.plan_path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            delivery_plan_path = None

        child_result = _invoke_delivery_child_run(
            args,
            cfg,
            context,
            root=root,
            task_path=selected_unit.task_path,
            delivery_plan_id=plan_id,
            delivery_unit_id=selected_unit.id,
            delivery_plan_path=delivery_plan_path,
            delivery_child_created_callback=link_child_task,
        )
        state_dir = context.resolve_state_dir(cfg)
        store = JsonStateStore(state_dir)
        child_task_id = child_result.child_task_id
        if child_result.child_link_failed:
            updated_status = get_delivery_status(args.plan_file, project_root=project_root)
            errors = list(updated_status.errors)
            safe_plan_path = (
                _project_relative_path(Path(status.plan_path), root)
                if _path_is_within(Path(status.plan_path), root)
                else Path(status.plan_path).name
            )
            safe_progress_path = (
                _project_relative_path(progress_path, root) if _path_is_within(progress_path, root) else None
            )
            safe_events_path = _project_relative_path(events_path, root) if _path_is_within(events_path, root) else None
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.child_link_failed",
                    "Delivery child task was created, but parent progress could not record the child task id. Child agents were not started; inspect the child task state before retrying.",
                )
            )
            return DeliveryRunNextExecutionResult(
                plan_path=safe_plan_path,
                project_root=_project_relative_path(root, root),
                valid=False,
                ran=True,
                succeeded=False,
                status=updated_status.status,
                progress_exists=updated_status.progress_exists,
                selected_unit=_status_unit_by_id(updated_status, selected_unit.id) or selected_unit,
                child_task_id=child_task_id,
                unit_status=None,
                run_exit_code=child_result.exit_code,
                progress_path=safe_progress_path,
                events_path=safe_events_path,
                errors=errors,
                warnings=updated_status.warnings,
                message="Delivery child task was created, but parent progress could not record the child task id. Child agents were not started; inspect the child task state before retrying.",
            )
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
        classification = _classify_delivery_child_run(child_result, child_state)
        unit_status = classification.unit_status
        failure_code = classification.failure_code
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
    return DeliveryProgress(
        schema_version=1,
        plan_id=status.plan.plan_id,
        units=units,
        final_branch=status.final_branch,
        final_commit=status.final_commit,
        finalized_at=status.finalized_at,
    )


def _progress_for_update(status, progress_path: Path, *, read_delivery_progress: Callable):
    if status.progress_exists:
        progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
        if progress is not None and not errors:
            return progress
    return _progress_from_status(status)


def _apply_delivery_preview_execution_guards(preview, plan_file: str | Path, *, project_root: Path | None):
    if not preview.ready or preview.selected_unit is None or preview.project_root is None:
        return preview

    from core.delivery_progress import get_delivery_status

    status = get_delivery_status(plan_file, project_root=project_root)
    selected_unit = _status_unit_by_id(status, preview.selected_unit.id) or preview.selected_unit
    if not status.valid or status.project_root is None:
        return replace(
            preview,
            valid=False,
            ready=False,
            selected_unit=selected_unit,
            errors=list(status.errors) or list(preview.errors),
            warnings=list(status.warnings) or list(preview.warnings),
            message="Delivery plan is not ready to run.",
        )

    dependency_errors = _dependency_commit_errors(status, selected_unit, Path(status.project_root).resolve())
    if not dependency_errors:
        return preview
    return replace(
        preview,
        valid=False,
        ready=False,
        selected_unit=selected_unit,
        errors=[*preview.errors, *dependency_errors],
        message="Delivery unit dependencies are not applied to the current checkout.",
    )


def _dependency_commit_errors(status, selected_unit, root: Path):
    from core.delivery_plan import DeliveryPlanIssue

    units_by_id = {unit.id: unit for unit in status.units}
    errors: list[DeliveryPlanIssue] = []
    pending = list(selected_unit.depends_on)
    visited: set[str] = set()
    while pending:
        dependency = pending.pop(0)
        if dependency in visited:
            continue
        visited.add(dependency)
        dependency_unit = units_by_id.get(dependency)
        if dependency_unit is None:
            continue
        pending.extend(dependency_unit.depends_on)
        if dependency_unit.status != "done":
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


def _child_delivery_result_finalized(child_state) -> bool:
    if getattr(child_state, "result_commit", None):
        return True
    return not (getattr(child_state, "worktree_path", None) or getattr(child_state, "worktree_base", None))


def _classify_delivery_child_run(
    child_result: DeliveryChildRunResult,
    child_state,
) -> DeliveryChildRunClassification:
    if child_result.interrupted:
        return DeliveryChildRunClassification("failed", "child_run_interrupted")
    if child_result.exception is not None:
        return DeliveryChildRunClassification("failed", "child_run_exception")
    if child_state is None:
        return DeliveryChildRunClassification("failed", "child_task_missing")
    if child_result.exit_code != 0:
        return DeliveryChildRunClassification("failed", "child_run_failed")
    if not getattr(child_state, "done", False):
        return DeliveryChildRunClassification("failed", "child_task_incomplete")
    if not _child_delivery_result_finalized(child_state):
        return DeliveryChildRunClassification("failed", "child_run_unfinalized")
    return DeliveryChildRunClassification("done", None)


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
    delivery_plan_id: str | None = None,
    delivery_unit_id: str | None = None,
    delivery_plan_path: str | None = None,
    delivery_child_created_callback: Callable[[str], None] | None = None,
) -> DeliveryChildRunResult:
    run_args = _delivery_child_run_args(
        root=root,
        task_path=task_path,
        agent_model=getattr(args, "agent_model", None),
        agent_provider=getattr(args, "agent_provider", None),
        agent_timeout=getattr(args, "agent_timeout", None),
        delivery_plan_id=delivery_plan_id,
        delivery_unit_id=delivery_unit_id,
        delivery_plan_path=delivery_plan_path,
        delivery_child_created_callback=delivery_child_created_callback,
    )
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
        if isinstance(exc, DeliveryChildLinkFailed):
            child_task_id = getattr(run_args, "created_task_id", None)
            return DeliveryChildRunResult(
                exit_code=1,
                child_task_id=child_task_id,
                exception=exc,
                child_link_failed=True,
            )
        return DeliveryChildRunResult(
            exit_code=1,
            child_task_id=getattr(run_args, "created_task_id", None),
            exception=exc,
        )
    return _coerce_child_run_result(raw_result)


def _delivery_child_run_args(
    *,
    root: Path,
    task_path: str,
    agent_model: list[str] | None = None,
    agent_provider: list[str] | None = None,
    agent_timeout: list[str] | None = None,
    delivery_plan_id: str | None = None,
    delivery_unit_id: str | None = None,
    delivery_plan_path: str | None = None,
    delivery_child_created_callback: Callable[[str], None] | None = None,
) -> argparse.Namespace:
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
        agent_model=agent_model,
        agent_provider=agent_provider,
        agent_timeout=agent_timeout,
        delivery_plan_id=delivery_plan_id,
        delivery_unit_id=delivery_unit_id,
        delivery_plan_path=delivery_plan_path,
        delivery_child_created_callback=delivery_child_created_callback,
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


def _status_unit_by_id(status, unit_id: str):
    return next((unit for unit in status.units if unit.id == unit_id), None)


def _print_delivery_result(result, *, json_output: bool, render: Callable) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render(result), end="")
