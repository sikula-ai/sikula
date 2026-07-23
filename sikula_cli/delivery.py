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
from typing import TYPE_CHECKING, Any

from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
)
from core.delivery_handoff import SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION
from core.delivery_plan import (
    DeliveryBudgetExceeded,
    DeliveryPlanIssue,
    delivery_final_branch_for_plan_id,
    is_valid_delivery_branch_name,
)
from core.delivery_prepare_writer import DeliveryPrepareWriteResult, write_delivery_prepare_artifacts
from core.delivery_unit_metadata import (
    DELIVERY_UNIT_BUDGET_EXCEEDED_CODE,
    delivery_unit_budget_snapshot,
    delivery_unit_planner_step_limit,
)
from sikula_cli.agent_overrides import (
    DELIVERY_PREPARATION_AGENT_NAMES,
    RUNTIME_AGENT_NAMES,
    parse_agent_llm_overrides,
)
from sikula_cli.config import _resolve_contract_report_dir, _resolve_state_dir

if TYPE_CHECKING:
    from core.delivery_run_next import DeliveryBudgetSplitPreparationResult, DeliveryRunNextExecutionResult

_DELIVERY_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DELIVERY_CHILD_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
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
    delivery_run_next_mode = delivery_run_next_p.add_mutually_exclusive_group()
    delivery_run_next_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview the next eligible unit without running agents or writing delivery progress",
    )
    delivery_run_next_mode.add_argument(
        "--prepare-budget-split",
        action="store_true",
        default=False,
        help="Prepare, but do not apply, a split proposal after a verified planner budget stop",
    )
    delivery_run_next_p.add_argument(
        "--reset-failed",
        action="store_true",
        default=False,
        help="Select a failed delivery unit with a linked child task instead of later pending work",
    )
    delivery_run_next_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")
    delivery_run_next_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one child run or delivery_preparer agent",
    )
    delivery_run_next_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one child run or delivery_preparer agent",
    )
    delivery_run_next_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for one child run or delivery_preparer agent",
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

    delivery_amend_p = delivery_sub.add_parser("amend", help="Safely amend an existing delivery plan")
    delivery_amend_sub = delivery_amend_p.add_subparsers(dest="delivery_amend_command")

    delivery_amend_prepare_p = delivery_amend_sub.add_parser(
        "prepare", help="Author and store a split proposal without changing the delivery plan"
    )
    delivery_amend_prepare_p.add_argument("plan_file", metavar="PLAN_FILE")
    delivery_amend_prepare_p.add_argument("--split-unit", required=True, metavar="UNIT_ID")
    delivery_amend_prepare_p.add_argument("--json", action="store_true", default=False)
    delivery_amend_prepare_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for delivery_preparer",
    )
    delivery_amend_prepare_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for delivery_preparer",
    )
    delivery_amend_prepare_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for delivery_preparer",
    )

    delivery_amend_apply_p = delivery_amend_sub.add_parser(
        "apply", help="Preview or apply an exact stored split proposal"
    )
    delivery_amend_apply_p.add_argument("plan_file", metavar="PLAN_FILE")
    delivery_amend_apply_p.add_argument("--proposal", required=True, metavar="PROPOSAL_ID")
    delivery_amend_apply_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run all deterministic checks without writing delivery artifacts or events",
    )
    delivery_amend_apply_p.add_argument("--json", action="store_true", default=False)

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
class DeliveryAmendPrepareResult:
    plan_path: str
    project_root: str | None
    status: str
    prepared: bool
    plan_id: str | None
    target_unit_id: str
    proposal_id: str | None = None
    replacement_ids: list[str] = field(default_factory=list)
    proposal_path: str | None = None
    audit_path: str | None = None
    errors: list[DeliveryPlanIssue] = field(default_factory=list)
    warnings: list[DeliveryPlanIssue] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        root = Path(self.project_root).resolve() if self.project_root else None

        def safe_path(value: str | None) -> str | None:
            if value is None or root is None:
                return value
            try:
                raw_path = Path(value)
                resolved = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
                return resolved.relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError):
                return None

        return {
            "plan_path": safe_path(self.plan_path),
            "project_root": "." if root else None,
            "status": self.status,
            "prepared": self.prepared,
            "plan_id": self.plan_id,
            "target_unit_id": self.target_unit_id,
            "proposal_id": self.proposal_id,
            "replacement_ids": list(self.replacement_ids),
            "proposal_path": safe_path(self.proposal_path),
            "audit_path": safe_path(self.audit_path),
            "errors": [_safe_amend_issue_dict(issue, root, sensitive_paths=(self.plan_path,)) for issue in self.errors],
            "warnings": [
                _safe_amend_issue_dict(issue, root, sensitive_paths=(self.plan_path,)) for issue in self.warnings
            ],
            "message": self.message,
        }


@dataclass(frozen=True)
class DeliveryAmendPrepareContext:
    run_authoring_assistant: Callable[..., DeliveryAmendmentAuthoringDraft]


def cmd_delivery_amend_prepare(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryAmendPrepareContext | None = None,
) -> None:
    result = _prepare_delivery_amendment(args, cfg, context)
    _print_delivery_result(result, json_output=args.json, render=render_delivery_amend_prepare)
    if not result.prepared:
        sys.exit(1)


def _prepare_delivery_amendment(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryAmendPrepareContext | None = None,
    *,
    authoritative_amend_reason: str | None = None,
    authoritative_budget_exceeded: DeliveryBudgetExceeded | None = None,
) -> DeliveryAmendPrepareResult:
    from core.delivery_amendment import (
        DeliveryAmendmentError,
        capture_delivery_amendment_source_snapshot,
        create_delivery_amendment_proposal,
        inspect_delivery_amendment_target,
    )

    root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    plan_path = _resolve_from_cwd(args.plan_file)
    target_id = str(args.split_unit)
    errors = _validate_delivery_prepare_agent_overrides(args)
    target = None
    if not errors:
        try:
            target = inspect_delivery_amendment_target(
                plan_path,
                target_id,
                project_root=root,
                project_config=cfg,
            )
        except DeliveryAmendmentError as exc:
            errors.append(DeliveryPrepareIssue(exc.issue.severity, exc.issue.code, exc.issue.message, exc.issue.path))

    result = DeliveryAmendPrepareResult(
        plan_path=str(plan_path),
        project_root=str(root),
        status="blocked",
        prepared=False,
        plan_id=target.plan.plan_id if target else None,
        target_unit_id=target_id,
        errors=[DeliveryPlanIssue(issue.severity, issue.code, issue.message, issue.path) for issue in errors],
        message="Delivery amendment proposal preparation is blocked.",
    )
    if target is not None and not result.errors:
        if context is None:
            result = replace(
                result,
                errors=[
                    DeliveryPlanIssue(
                        "error",
                        "delivery_amend.authoring_context_missing",
                        "Delivery amendment authoring requires the main Sikula command context.",
                    )
                ],
            )
        else:
            draft = None
            try:
                source_snapshot = capture_delivery_amendment_source_snapshot(target)
                authoring_kwargs: dict[str, Any] = {
                    "args": args,
                    "cfg": cfg,
                    "target": target,
                    "source_snapshot": source_snapshot,
                }
                if authoritative_amend_reason is not None or authoritative_budget_exceeded is not None:
                    authoring_kwargs.update(
                        amend_reason=authoritative_amend_reason,
                        budget_exceeded=authoritative_budget_exceeded,
                    )
                draft = context.run_authoring_assistant(
                    **authoring_kwargs,
                )
                draft = _bind_authoritative_amendment_metadata(
                    draft,
                    amend_reason=authoritative_amend_reason,
                    budget_exceeded=authoritative_budget_exceeded,
                )
                proposal, proposal_path = create_delivery_amendment_proposal(
                    plan_path,
                    target_id,
                    draft,
                    project_root=root,
                    proposal_root=_resolve_contract_report_dir(cfg),
                    project_config=cfg,
                    expected_source_snapshot=source_snapshot,
                )
                warnings = []
                if draft.warnings:
                    warnings.append(
                        DeliveryPlanIssue(
                            "warning",
                            "delivery_amend.authoring_warnings_present",
                            "Amendment authoring assistant reported warnings; inspect the local audit artifact.",
                        )
                    )
                result = replace(
                    result,
                    status="ready",
                    prepared=True,
                    proposal_id=proposal.proposal_id,
                    replacement_ids=proposal.replacement_ids,
                    proposal_path=str(proposal_path),
                    audit_path=getattr(draft, "audit_path", None),
                    warnings=warnings,
                    message="Delivery amendment proposal stored; the delivery plan was not changed.",
                )
            except DeliveryAuthoringParseError as exc:
                result = replace(
                    result,
                    audit_path=getattr(exc, "audit_path", None),
                    errors=[DeliveryPlanIssue("error", "delivery_amend.authoring_invalid", exc.message)],
                    message="Delivery amendment authoring returned an invalid proposal.",
                )
            except DeliveryAmendmentError as exc:
                result = replace(
                    result,
                    audit_path=getattr(draft, "audit_path", None),
                    errors=[exc.issue],
                    message="Delivery amendment proposal was rejected.",
                )
            except Exception as exc:
                result = replace(
                    result,
                    audit_path=getattr(exc, "audit_path", None) or getattr(draft, "audit_path", None),
                    errors=[
                        DeliveryPlanIssue(
                            "error",
                            "delivery_amend.authoring_failed",
                            "Delivery amendment authoring assistant failed; inspect the local audit artifact.",
                        )
                    ],
                    message="Delivery amendment proposal preparation failed.",
                )
    return result


def _bind_authoritative_amendment_metadata(
    draft: DeliveryAmendmentAuthoringDraft,
    *,
    amend_reason: str | None,
    budget_exceeded: DeliveryBudgetExceeded | None,
) -> DeliveryAmendmentAuthoringDraft:
    if amend_reason is None and budget_exceeded is None:
        return draft
    expected_budget = budget_exceeded.to_dict() if budget_exceeded else None
    reason_mismatch = draft.amend_reason is not None and draft.amend_reason != amend_reason
    budget_mismatch = draft.budget_exceeded is not None and draft.budget_exceeded != expected_budget
    if reason_mismatch or budget_mismatch:
        from core.delivery_amendment import DeliveryAmendmentError

        raise DeliveryAmendmentError(
            "delivery_amend.authoring_recovery_metadata_mismatch",
            "Amendment authoring returned recovery metadata that does not match the verified budget stop.",
        )
    bound = replace(
        draft,
        amend_reason=amend_reason,
        budget_exceeded=expected_budget,
    )
    audit_path = getattr(draft, "audit_path", None)
    if audit_path is not None:
        setattr(bound, "audit_path", audit_path)
    return bound


def render_delivery_amend_prepare(result: DeliveryAmendPrepareResult) -> str:
    projection = result.to_dict()
    lines = [
        f"Delivery amend prepare: {projection['plan_path']}",
        f"Status: {result.status}",
        f"Target unit: {result.target_unit_id}",
    ]
    if result.plan_id:
        lines.append(f"Plan ID: {result.plan_id}")
    if result.proposal_id:
        lines.append(f"Proposal: {result.proposal_id}")
    if result.replacement_ids:
        lines.append("Replacements: " + ", ".join(result.replacement_ids))
    if projection["proposal_path"]:
        lines.append(f"Proposal artifact: {projection['proposal_path']}")
    if projection["audit_path"]:
        lines.append(f"Authoring audit: {projection['audit_path']}")
    lines.append(result.message)
    if result.errors:
        lines.extend(["", "Errors:", *[_format_amend_issue_data(issue) for issue in projection["errors"]]])
    if result.warnings:
        lines.extend(["", "Warnings:", *[_format_amend_issue_data(issue) for issue in projection["warnings"]]])
    return "\n".join(lines) + "\n"


def cmd_delivery_amend_apply(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_amendment import apply_delivery_amendment, preview_delivery_amendment

    root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    kwargs = {
        "proposal_root": _resolve_contract_report_dir(cfg),
        "project_root": root,
        "project_config": cfg,
    }
    if args.dry_run:
        result = preview_delivery_amendment(args.plan_file, args.proposal, **kwargs)
    else:
        result = apply_delivery_amendment(args.plan_file, args.proposal, **kwargs)
    _print_delivery_result(result, json_output=args.json, render=render_delivery_amend_apply)
    if not result.ready or (not args.dry_run and not result.applied):
        sys.exit(1)


def render_delivery_amend_apply(result) -> str:
    projection = result.to_dict()
    lines = [
        f"Delivery amend apply{' dry run' if result.dry_run else ''}: {projection['plan_path']}",
        f"Status: {'ready' if result.dry_run and result.ready else 'applied' if result.applied else 'blocked'}",
        f"Proposal: {result.proposal_id}",
    ]
    if result.target_unit_id:
        lines.append(f"Target unit: {result.target_unit_id}")
    if result.replacement_ids:
        lines.append("Replacements: " + ", ".join(result.replacement_ids))
    if result.rewired_unit_ids:
        lines.append("Rewired downstream units: " + ", ".join(result.rewired_unit_ids))
    lines.append(f"Dry run: {'yes' if result.dry_run else 'no'}")
    lines.append(result.message)
    if result.errors:
        lines.extend(["", "Errors:", *[_format_amend_issue_data(issue) for issue in projection["errors"]]])
    if result.warnings:
        lines.extend(["", "Warnings:", *[_format_amend_issue_data(issue) for issue in projection["warnings"]]])
    return "\n".join(lines) + "\n"


def _safe_amend_issue_dict(
    issue: DeliveryPlanIssue,
    root: Path | None,
    *,
    sensitive_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    message = issue.message
    for sensitive_path in sensitive_paths:
        try:
            candidate = Path(sensitive_path)
            if not candidate.is_absolute():
                continue
            if root is None:
                raise ValueError
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            message = message.replace(str(candidate), "<redacted>")
    if root is not None:
        root_text = str(root)
        message = message.replace(root_text + "/", "").replace(root_text, ".")
    path = issue.path
    if path:
        try:
            candidate = Path(path)
            if candidate.is_absolute():
                if root is None:
                    raise ValueError
                path = candidate.resolve(strict=False).relative_to(root).as_posix()
            elif root is not None:
                root_text = str(root)
                path = path.replace(root_text + "/", "").replace(root_text, ".")
        except (OSError, RuntimeError, ValueError):
            path = "<redacted>"
    return DeliveryPlanIssue(issue.severity, issue.code, message, path).to_dict()


def _format_amend_issue_data(issue: dict[str, Any]) -> str:
    location = f" [{issue['path']}]" if issue.get("path") else ""
    return f"- {issue['code']}{location}: {issue['message']}"


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
    run_amendment_authoring: Callable[..., DeliveryAmendmentAuthoringDraft] | None = None


@dataclass(frozen=True)
class DeliveryChildRunClassification:
    unit_status: str
    failure_code: str | None
    budget_exceeded: DeliveryBudgetExceeded | None = None


def cmd_delivery_run_next(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext | None = None,
) -> None:
    from core.delivery_run_next import (
        DeliveryRunNextPreview,
        preview_delivery_run_next,
        render_delivery_run_next_execution,
        render_delivery_run_next_preview,
    )

    _validate_delivery_run_next_agent_overrides(args)
    project_root_raw = cfg.get("project", {}).get("root_path") if isinstance(cfg, dict) else None
    project_root = Path(project_root_raw).resolve() if project_root_raw else None
    if getattr(args, "dry_run", False) and getattr(args, "prepare_budget_split", False):
        issue = DeliveryPlanIssue(
            "error",
            "delivery.budget_split_dry_run_conflict",
            "--prepare-budget-split cannot be used with --dry-run because split preparation invokes an agent and writes local audit artifacts.",
        )
        result = DeliveryRunNextPreview(
            plan_path=str(args.plan_file),
            project_root=str(project_root) if project_root else None,
            valid=False,
            ready=False,
            dry_run=True,
            status=None,
            progress_exists=False,
            selected_unit=None,
            errors=[issue],
            warnings=[],
            message=issue.message,
        )
        _print_delivery_result(result, json_output=args.json, render=render_delivery_run_next_preview)
        sys.exit(1)
    if getattr(args, "dry_run", False):
        result = preview_delivery_run_next(
            args.plan_file,
            project_root=project_root,
            reset_failed=bool(getattr(args, "reset_failed", False)),
        )
        result = _apply_delivery_preview_execution_guards(
            result,
            args.plan_file,
            cfg=cfg,
            project_root=project_root,
            reset_failed=bool(getattr(args, "reset_failed", False)),
        )
        _print_delivery_result(result, json_output=args.json, render=render_delivery_run_next_preview)
        if not result.ready:
            sys.exit(1)
        return

    if context is None:
        print("delivery run-next execution requires the main Sikula command context.")
        sys.exit(2)

    result = _run_next_delivery_unit(args, cfg, context, project_root=project_root)
    if getattr(args, "prepare_budget_split", False) and not any(
        issue.code == "delivery.locked" for issue in result.errors
    ):
        result = _coordinate_budget_split_preparation(
            args,
            cfg,
            context,
            result,
            project_root=project_root,
        )
    _print_delivery_result(result, json_output=args.json, render=render_delivery_run_next_execution)
    if not result.succeeded:
        sys.exit(1)


def _validate_delivery_run_next_agent_overrides(args: argparse.Namespace) -> None:
    valid_agents = set(RUNTIME_AGENT_NAMES)
    if getattr(args, "prepare_budget_split", False):
        valid_agents.update(DELIVERY_PREPARATION_AGENT_NAMES)
    parse_agent_llm_overrides(
        getattr(args, "agent_model", None),
        getattr(args, "agent_provider", None),
        getattr(args, "agent_timeout", None),
        valid_agents=valid_agents,
    )


def _filter_agent_override_entries(entries: list[str] | None, allowed_agents: set[str]) -> list[str] | None:
    if entries is None:
        return None
    filtered = []
    for entry in entries:
        raw_agent, _, _ = entry.partition("=")
        if raw_agent.strip().replace("-", "_") in allowed_agents:
            filtered.append(entry)
    return filtered or None


def _coordinate_budget_split_preparation(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    result: "DeliveryRunNextExecutionResult",
    *,
    project_root: Path | None,
) -> "DeliveryRunNextExecutionResult":
    from core.delivery_progress import get_delivery_status
    from core.delivery_run_next import DeliveryBudgetSplitPreparationResult
    from core.state import JsonStateStore

    status = get_delivery_status(args.plan_file, project_root=project_root)
    candidates = [
        unit
        for unit in status.units
        if unit.status == "failed"
        and unit.failure_code == DELIVERY_UNIT_BUDGET_EXCEEDED_CODE
        and unit.budget_exceeded is not None
    ]
    if not candidates:
        return result
    if len(candidates) > 1:
        issue = DeliveryPlanIssue(
            "error",
            "delivery.budget_split_candidate_ambiguous",
            "Automatic split preparation requires exactly one failed planner-budget-stopped delivery unit.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=None,
            budget_exceeded=None,
            project_root=project_root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    candidate = candidates[0]
    budget_exceeded = candidate.budget_exceeded
    child_task_id = candidate.child_task_id
    if not child_task_id or not _is_valid_delivery_child_task_id(child_task_id):
        issue = DeliveryPlanIssue(
            "error",
            "delivery.child_task_missing",
            f"Delivery unit {candidate.id} has no valid linked child task for budget split preparation.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=project_root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    store = JsonStateStore(context.resolve_state_dir(cfg))
    try:
        child_state = store.load(child_task_id)
    except (AttributeError, OSError, TypeError, ValueError):
        issue = DeliveryPlanIssue(
            "error",
            "delivery.child_task_state_invalid",
            f"Delivery unit {candidate.id} is linked to child task {child_task_id}, but that task state is invalid.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=project_root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)
    if child_state is None:
        issue = DeliveryPlanIssue(
            "error",
            "delivery.child_task_missing",
            f"Delivery unit {candidate.id} is linked to child task {child_task_id}, but that task state was not found in the configured state directory.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=project_root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    root = Path(status.project_root).resolve() if status.project_root else project_root
    expected_plan_path = _delivery_plan_metadata_path(status, root) if root else None
    if (
        status.plan is None
        or root is None
        or not _delivery_child_metadata_matches(
            child_state,
            plan_id=status.plan.plan_id if status.plan else "",
            unit_id=candidate.id,
            plan_path=expected_plan_path,
        )
    ):
        issue = DeliveryPlanIssue(
            "error",
            "delivery.child_task_metadata_mismatch",
            f"Delivery unit {candidate.id} and its linked child task do not identify the same delivery plan and unit.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    child_budget_stop = getattr(child_state, "delivery_budget_stop", None)
    child_budget_exceeded = _delivery_child_budget_exceeded(child_state)
    child_budget_snapshot = getattr(child_state, "delivery_unit_budget", None)
    child_budget_limit = (
        child_budget_snapshot.get("max_planner_steps") if isinstance(child_budget_snapshot, dict) else None
    )
    child_budget_limit_valid = (
        isinstance(child_budget_limit, int) and not isinstance(child_budget_limit, bool) and child_budget_limit > 0
    )
    expected_budget_limit = delivery_unit_planner_step_limit(candidate.budget)
    if (
        not getattr(child_state, "failed", False)
        or not isinstance(child_budget_stop, dict)
        or child_budget_stop.get("phase") != "planner"
        or child_budget_exceeded != budget_exceeded
        or not child_budget_limit_valid
        or child_budget_limit != expected_budget_limit
        or budget_exceeded.limit != expected_budget_limit
        or budget_exceeded.actual <= budget_exceeded.limit
    ):
        issue = DeliveryPlanIssue(
            "error",
            "delivery.budget_split_stop_mismatch",
            f"Delivery unit {candidate.id} parent and child planner budget-stop metadata do not match.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    if context.run_amendment_authoring is None:
        issue = DeliveryPlanIssue(
            "error",
            "delivery_amend.authoring_context_missing",
            "Automatic budget split preparation requires the main Sikula amendment authoring context.",
        )
        preparation = _blocked_budget_split_preparation(
            issue,
            target_unit_id=candidate.id,
            budget_exceeded=budget_exceeded,
            project_root=root,
            plan_path=status.plan_path,
        )
        return replace(result, budget_split_preparation=preparation)

    prepare_args = copy.copy(args)
    prepare_args.split_unit = candidate.id
    prepare_args.agent_model = _filter_agent_override_entries(
        getattr(args, "agent_model", None), DELIVERY_PREPARATION_AGENT_NAMES
    )
    prepare_args.agent_provider = _filter_agent_override_entries(
        getattr(args, "agent_provider", None), DELIVERY_PREPARATION_AGENT_NAMES
    )
    prepare_args.agent_timeout = _filter_agent_override_entries(
        getattr(args, "agent_timeout", None), DELIVERY_PREPARATION_AGENT_NAMES
    )
    prepare_result = _prepare_delivery_amendment(
        prepare_args,
        cfg,
        DeliveryAmendPrepareContext(context.run_amendment_authoring),
        authoritative_amend_reason=DELIVERY_UNIT_BUDGET_EXCEEDED_CODE,
        authoritative_budget_exceeded=budget_exceeded,
    )
    projection = prepare_result.to_dict()
    preparation = DeliveryBudgetSplitPreparationResult(
        prepared=prepare_result.prepared,
        target_unit_id=candidate.id,
        proposal_id=prepare_result.proposal_id,
        replacement_ids=list(prepare_result.replacement_ids),
        proposal_path=projection["proposal_path"],
        audit_path=projection["audit_path"],
        budget_exceeded=budget_exceeded,
        errors=list(projection["errors"]),
        warnings=list(projection["warnings"]),
        message=(
            "Budget split proposal prepared; the tracked plan was not changed. Review it and use delivery amend apply."
            if prepare_result.prepared
            else prepare_result.message
        ),
    )
    return replace(result, budget_split_preparation=preparation)


def _blocked_budget_split_preparation(
    issue: DeliveryPlanIssue,
    *,
    target_unit_id: str | None,
    budget_exceeded: DeliveryBudgetExceeded | None,
    project_root: Path | None,
    plan_path: str,
) -> "DeliveryBudgetSplitPreparationResult":
    from core.delivery_run_next import DeliveryBudgetSplitPreparationResult

    safe_issue = _safe_amend_issue_dict(issue, project_root, sensitive_paths=(plan_path,))
    return DeliveryBudgetSplitPreparationResult(
        prepared=False,
        target_unit_id=target_unit_id,
        proposal_id=None,
        replacement_ids=[],
        proposal_path=None,
        audit_path=None,
        budget_exceeded=budget_exceeded,
        errors=[safe_issue],
        warnings=[],
        message=issue.message,
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

    reset_failed = bool(getattr(args, "reset_failed", False))
    preflight = preview_delivery_run_next(args.plan_file, project_root=project_root, reset_failed=reset_failed)
    status = get_delivery_status(args.plan_file, project_root=project_root)

    if not preflight.valid:
        budget_split_recovery = bool(getattr(args, "prepare_budget_split", False)) and any(
            issue.code == "delivery.unit_budget_exceeded" for issue in preflight.errors
        )
        fatal_errors = [
            issue
            for issue in preflight.errors
            if issue.code not in ("delivery.running", "delivery.failed", "delivery.failed_reset_unavailable")
            and not (budget_split_recovery and issue.code == "delivery.unit_budget_exceeded")
        ]
        if (not _running_delivery_units(status) and not budget_split_recovery) or fatal_errors:
            return _execution_result_from_preview(preflight, ran=False)

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
        running_units = _running_delivery_units(status)
        if len(running_units) == 1:
            return _handle_running_delivery_unit(
                args=args,
                cfg=cfg,
                context=context,
                status=status,
                root=root,
                plan_id=plan_id,
                progress_path=progress_path,
                events_path=events_path,
                running_unit=running_units[0],
            )
        if len(running_units) > 1:
            code = "delivery.running_unit_ambiguous"
            message = "Delivery progress has multiple running units; inspect parent progress before retrying."
            blocked_errors = [*errors, DeliveryPlanIssue("error", code, message)]
            return DeliveryRunNextExecutionResult(
                plan_path=status.plan_path,
                project_root=str(root.resolve()),
                valid=False,
                ran=False,
                succeeded=False,
                status=status.status,
                progress_exists=status.progress_exists,
                selected_unit=None,
                child_task_id=None,
                unit_status=None,
                run_exit_code=None,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=blocked_errors,
                warnings=status.warnings,
                message=message,
            )

        selected_unit = select_next_delivery_unit(status, reset_failed=reset_failed)
        if selected_unit is None:
            code, message = _blocked_run_next_reason(
                status.status,
                reset_failed=reset_failed,
                units=status.units,
            )
            errors.append(DeliveryPlanIssue("error", code, message))
            if reset_failed:
                return DeliveryRunNextExecutionResult(
                    plan_path=status.plan_path,
                    project_root=str(root.resolve()),
                    valid=False,
                    ran=False,
                    succeeded=False,
                    status=status.status,
                    progress_exists=status.progress_exists,
                    selected_unit=None,
                    child_task_id=None,
                    unit_status=None,
                    run_exit_code=None,
                    progress_path=str(progress_path),
                    events_path=str(events_path),
                    errors=errors,
                    warnings=status.warnings,
                    message=message,
                )
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
            if reset_failed:
                return DeliveryRunNextExecutionResult(
                    plan_path=status.plan_path,
                    project_root=str(root.resolve()),
                    valid=False,
                    ran=False,
                    succeeded=False,
                    status=status.status,
                    progress_exists=status.progress_exists,
                    selected_unit=selected_unit,
                    child_task_id=None,
                    unit_status=None,
                    run_exit_code=None,
                    progress_path=str(progress_path),
                    events_path=str(events_path),
                    errors=errors,
                    warnings=status.warnings,
                    message="Delivery unit dependencies are not applied to the current checkout.",
                )
            return _execution_result_from_status(
                status,
                ran=False,
                selected_unit=selected_unit,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=errors,
                message="Delivery unit dependencies are not applied to the current checkout.",
            )

        dependency_handoffs, handoff_errors = _load_dependency_handoffs(status, selected_unit, root)
        if handoff_errors:
            errors.extend(handoff_errors)
            return _execution_result_from_status(
                status,
                ran=False,
                selected_unit=selected_unit,
                progress_path=str(progress_path),
                events_path=str(events_path),
                errors=errors,
                message="Delivery unit dependency handoff evidence is unavailable or invalid.",
            )

        if reset_failed and selected_unit.status == "failed":
            return _handle_failed_delivery_unit_retry(
                args=args,
                cfg=cfg,
                context=context,
                status=status,
                root=root,
                plan_id=plan_id,
                progress_path=progress_path,
                events_path=events_path,
                failed_unit=selected_unit,
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

        delivery_plan_path = _delivery_plan_metadata_path(status, root)

        child_result = _invoke_delivery_child_run(
            args,
            cfg,
            context,
            root=root,
            task_path=selected_unit.task_path,
            delivery_plan_id=plan_id,
            delivery_unit_id=selected_unit.id,
            delivery_plan_path=delivery_plan_path,
            delivery_unit_budget=delivery_unit_budget_snapshot(selected_unit.budget),
            delivery_handoff_schema_version=SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION,
            delivery_dependency_handoffs=dependency_handoffs,
            delivery_child_created_callback=link_child_task,
        )
        state_dir = context.resolve_state_dir(cfg)
        store = JsonStateStore(state_dir)
        child_task_id = child_result.child_task_id
        if child_result.child_link_failed:
            _restore_delivery_progress(progress_path, progress_before_start, existed=progress_existed_before_start)
            link_failed_unit = make_delivery_unit_progress(
                selected_unit.id,
                "pending",
                child_task_id=child_task_id,
            )
            append_delivery_progress_event(
                events_path,
                make_delivery_progress_event(plan_id, "unit.child_link_failed", unit=link_failed_unit),
            )
            updated_status = get_delivery_status(args.plan_file, project_root=project_root)
            errors = list(updated_status.errors)
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.child_link_failed",
                    "Delivery child task was created, but parent progress could not record the child task id. Child agents were not started; inspect the child task state before retrying.",
                )
            )
            return DeliveryRunNextExecutionResult(
                plan_path=status.plan_path,
                project_root=str(root.resolve()),
                valid=False,
                ran=True,
                succeeded=False,
                status=updated_status.status,
                progress_exists=updated_status.progress_exists,
                selected_unit=_status_unit_by_id(updated_status, selected_unit.id) or selected_unit,
                child_task_id=child_task_id,
                unit_status=None,
                run_exit_code=child_result.exit_code,
                progress_path=str(progress_path),
                events_path=str(events_path),
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
        progress, unit_status, handoff_issue = _record_delivery_child_terminal_result(
            progress=progress,
            selected_unit=selected_unit,
            child_task_id=child_task_id,
            child_state=child_state,
            child_result=child_result,
            plan_id=plan_id,
            project_root=root,
            progress_path=progress_path,
            events_path=events_path,
        )
        if child_result.interrupted:
            raise KeyboardInterrupt
        if child_result.exception is not None:
            raise child_result.exception
        updated_status = get_delivery_status(args.plan_file, project_root=project_root)
        updated_unit = _status_unit_by_id(updated_status, selected_unit.id) or selected_unit

        if handoff_issue is not None:
            message = handoff_issue.message
        elif unit_status == "done":
            message = f"Delivery unit {selected_unit.id} completed."
        elif updated_unit.failure_code == DELIVERY_UNIT_BUDGET_EXCEEDED_CODE:
            message = (
                f"Delivery unit {selected_unit.id} exceeded its planner-step budget before implementation; "
                "split the failed unit with delivery amend prepare."
            )
        else:
            message = f"Delivery unit {selected_unit.id} failed; inspect child task state."
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=status.project_root,
            valid=updated_status.valid and handoff_issue is None,
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
            errors=[*updated_status.errors, *([handoff_issue] if handoff_issue else [])],
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
                budget_exceeded=unit.budget_exceeded,
                handoff_schema_version=unit.handoff_schema_version,
                handoff_fingerprint=unit.handoff_fingerprint,
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


def _apply_delivery_preview_execution_guards(
    preview,
    plan_file: str | Path,
    *,
    cfg: dict,
    project_root: Path | None,
    reset_failed: bool,
):
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
    root = Path(status.project_root).resolve()
    child_state_issue = _delivery_preview_child_state_issue(
        cfg=cfg,
        status=status,
        root=root,
        selected_unit=selected_unit,
        reset_failed=reset_failed,
    )
    if child_state_issue is not None:
        return replace(
            preview,
            valid=False,
            ready=False,
            selected_unit=selected_unit,
            errors=[*preview.errors, child_state_issue],
            message=child_state_issue.message,
        )
    if selected_unit.status == "running":
        return replace(preview, selected_unit=selected_unit)

    dependency_errors = _dependency_commit_errors(status, selected_unit, root)
    if dependency_errors:
        return replace(
            preview,
            valid=False,
            ready=False,
            selected_unit=selected_unit,
            errors=[*preview.errors, *dependency_errors],
            message="Delivery unit dependencies are not applied to the current checkout.",
        )
    _, handoff_errors = _load_dependency_handoffs(status, selected_unit, root)
    if handoff_errors:
        return replace(
            preview,
            valid=False,
            ready=False,
            selected_unit=selected_unit,
            errors=[*preview.errors, *handoff_errors],
            message="Delivery unit dependency handoff evidence is unavailable or invalid.",
        )
    return preview


def _delivery_preview_child_state_issue(
    *,
    cfg: dict,
    status,
    root: Path,
    selected_unit,
    reset_failed: bool,
):
    from core.delivery_plan import DeliveryPlanIssue
    from core.state import JsonStateStore

    if selected_unit.status not in {"running", "failed"}:
        return None
    if selected_unit.status == "failed" and not reset_failed:
        return None

    unit_id = selected_unit.id
    child_task_id = selected_unit.child_task_id
    if not child_task_id:
        if selected_unit.status == "running":
            message = (
                f"Delivery unit {unit_id} is running but has no child task id; "
                "inspect parent delivery progress before retrying."
            )
            return DeliveryPlanIssue("error", "delivery.running_child_missing", message)
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryPlanIssue("error", "delivery.child_task_missing", message)

    if not _is_valid_delivery_child_task_id(child_task_id):
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryPlanIssue("error", "delivery.child_task_missing", message)

    store = JsonStateStore(_resolve_state_dir(cfg))
    child_state = store.load(child_task_id)
    if child_state is None:
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryPlanIssue("error", "delivery.child_task_missing", message)

    expected_plan_path = _delivery_plan_metadata_path(status, root)
    if not _delivery_child_metadata_matches(
        child_state,
        plan_id=status.plan.plan_id,
        unit_id=unit_id,
        plan_path=expected_plan_path,
    ):
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but the child task delivery metadata "
            "does not match the parent plan and unit; inspect child task state before retrying."
        )
        return DeliveryPlanIssue("error", "delivery.child_task_metadata_mismatch", message)

    if selected_unit.status == "running":
        if getattr(child_state, "failed", False) and reset_failed:
            if not _delivery_child_has_resume_worktree(child_state):
                return _delivery_child_worktree_missing_issue(selected_unit, child_task_id)
            return None
        if getattr(child_state, "done", False) or getattr(child_state, "failed", False):
            return None
        if not _delivery_child_has_resume_worktree(child_state):
            return _delivery_child_worktree_missing_issue(selected_unit, child_task_id)
        return None

    if getattr(child_state, "done", False):
        return None
    if not _delivery_child_has_resume_worktree(child_state):
        return _delivery_child_worktree_missing_issue(selected_unit, child_task_id)
    return None


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


def _load_dependency_handoffs(
    status: Any,
    selected_unit: Any,
    root: Path,
) -> tuple[list[dict[str, Any]], list[DeliveryPlanIssue]]:
    from core.delivery_handoff import (
        DeliveryHandoffError,
        delivery_unit_handoff_path,
        delivery_unit_handoff_matches_unit,
        read_delivery_unit_handoff,
    )

    units_by_id = {unit.id: unit for unit in status.units}
    dependency_ids: set[str] = set()
    pending = list(selected_unit.depends_on)
    while pending:
        dependency_id = pending.pop(0)
        if dependency_id in dependency_ids:
            continue
        dependency_ids.add(dependency_id)
        dependency = units_by_id.get(dependency_id)
        if dependency is not None:
            pending.extend(dependency.depends_on)

    handoffs: list[dict[str, Any]] = []
    errors: list[DeliveryPlanIssue] = []
    for dependency in status.units:
        if dependency.id not in dependency_ids or dependency.status != "done":
            continue
        if dependency.handoff_schema_version is None and dependency.handoff_fingerprint is None:
            continue
        if dependency.handoff_schema_version != SUPPORTED_DELIVERY_HANDOFF_SCHEMA_VERSION:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.dependency_handoff_schema_unsupported",
                    f"Dependency unit {dependency.id} uses an unsupported delivery handoff schema.",
                )
            )
            continue

        try:
            path = delivery_unit_handoff_path(root, status.plan.plan_id, dependency.id)
            handoff = read_delivery_unit_handoff(path, project_root=root)
        except FileNotFoundError:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.dependency_handoff_missing",
                    f"Dependency unit {dependency.id} references a delivery handoff that is missing.",
                )
            )
            continue
        except DeliveryHandoffError:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.dependency_handoff_invalid",
                    f"Dependency unit {dependency.id} references an invalid delivery handoff.",
                )
            )
            continue

        if (
            handoff.schema_version != dependency.handoff_schema_version
            or handoff.fingerprint != dependency.handoff_fingerprint
            or handoff.plan_id != status.plan.plan_id
            or handoff.unit_id != dependency.id
            or handoff.child_task_id != dependency.child_task_id
            or handoff.result_branch != dependency.branch
            or handoff.result_commit != dependency.commit
            or not delivery_unit_handoff_matches_unit(handoff, dependency)
        ):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery.dependency_handoff_mismatch",
                    f"Dependency unit {dependency.id} handoff does not match parent progress evidence.",
                )
            )
            continue
        handoffs.append(handoff.to_dict())
    return handoffs, errors


def _running_delivery_units(status) -> list:
    return [unit for unit in status.units if unit.status == "running"]


def _is_valid_delivery_child_task_id(task_id: str) -> bool:
    return bool(_DELIVERY_CHILD_TASK_ID_RE.fullmatch(task_id))


def _handle_failed_delivery_unit_retry(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    *,
    status,
    root: Path,
    plan_id: str,
    progress_path: Path,
    events_path: Path,
    failed_unit,
) -> "DeliveryRunNextExecutionResult":
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_run_next import DeliveryRunNextExecutionResult
    from core.state import JsonStateStore

    unit_id = failed_unit.id
    child_task_id = failed_unit.child_task_id
    if not child_task_id or not _is_valid_delivery_child_task_id(child_task_id):
        code = "delivery.child_task_missing"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=failed_unit,
            child_task_id=child_task_id,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    child_state = store.load(child_task_id)
    if child_state is None:
        code = "delivery.child_task_missing"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=failed_unit,
            child_task_id=child_task_id,
            unit_status="failed",
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    expected_plan_path = _delivery_plan_metadata_path(status, root)
    if not _delivery_child_metadata_matches(
        child_state,
        plan_id=plan_id,
        unit_id=unit_id,
        plan_path=expected_plan_path,
    ):
        code = "delivery.child_task_metadata_mismatch"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but the child task delivery metadata "
            "does not match the parent plan and unit; inspect child task state before retrying."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=failed_unit,
            child_task_id=child_task_id,
            unit_status="failed",
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    return _run_delivery_child_retry(
        args=args,
        cfg=cfg,
        context=context,
        status=status,
        root=root,
        plan_id=plan_id,
        progress_path=progress_path,
        events_path=events_path,
        selected_unit=failed_unit,
    )


def _run_delivery_child_retry(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    *,
    status,
    root: Path,
    plan_id: str,
    progress_path: Path,
    events_path: Path,
    selected_unit,
) -> "DeliveryRunNextExecutionResult":
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_progress import (
        append_delivery_progress_event,
        get_delivery_status,
        make_delivery_progress_event,
        make_delivery_unit_progress,
        read_delivery_progress,
        upsert_delivery_unit_progress,
        write_delivery_progress,
    )
    from core.delivery_run_next import DeliveryRunNextExecutionResult
    from core.state import JsonStateStore

    unit_id = selected_unit.id
    child_task_id = selected_unit.child_task_id
    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    child_state = store.load(child_task_id)
    if child_state is None:
        code = "delivery.child_task_missing"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=selected_unit,
            child_task_id=child_task_id,
            unit_status=selected_unit.status,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )
    progress = _progress_for_update(status, progress_path, read_delivery_progress=read_delivery_progress)
    if child_state.done:
        reconcile_unit = make_delivery_unit_progress(
            unit_id,
            selected_unit.status,
            child_task_id=child_task_id,
            timestamp=selected_unit.started_at,
        )
        append_delivery_progress_event(
            events_path,
            make_delivery_progress_event(plan_id, "unit.reconcile_intent", unit=reconcile_unit),
        )
        synthetic_child_result = DeliveryChildRunResult(exit_code=0)
        progress, unit_status, handoff_issue = _record_delivery_child_terminal_result(
            progress=progress,
            selected_unit=selected_unit,
            child_task_id=child_task_id,
            child_state=child_state,
            child_result=synthetic_child_result,
            plan_id=plan_id,
            project_root=root,
            progress_path=progress_path,
            events_path=events_path,
        )
        updated_status = get_delivery_status(args.plan_file, project_root=root)
        updated_unit = _status_unit_by_id(updated_status, unit_id) or selected_unit
        message = (
            handoff_issue.message
            if handoff_issue is not None
            else (
                f"Delivery unit {unit_id} reconciled terminal child task as done."
                if unit_status == "done"
                else f"Delivery unit {unit_id} reconciled terminal child task as failed; inspect child task state."
            )
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=updated_status.valid and handoff_issue is None,
            ran=True,
            succeeded=unit_status == "done" and updated_status.valid,
            status=updated_status.status,
            progress_exists=updated_status.progress_exists,
            selected_unit=updated_unit,
            child_task_id=child_task_id,
            unit_status=unit_status,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*updated_status.errors, *([handoff_issue] if handoff_issue else [])],
            warnings=updated_status.warnings,
            message=message,
        )
    if not _delivery_child_has_resume_worktree(child_state):
        return _delivery_child_worktree_missing_result(
            status=status,
            root=root,
            progress_path=progress_path,
            events_path=events_path,
            selected_unit=selected_unit,
            child_task_id=child_task_id,
        )

    retry_unit = make_delivery_unit_progress(
        unit_id,
        "running",
        child_task_id=child_task_id,
        started_at=selected_unit.started_at,
    )
    append_delivery_progress_event(
        events_path,
        make_delivery_progress_event(plan_id, "unit.retry_intent", unit=retry_unit),
    )
    progress = upsert_delivery_unit_progress(progress, retry_unit)
    write_delivery_progress(progress_path, progress)

    run_args = _delivery_child_resume_run_args(
        child_task_id=child_task_id,
        created_task_id=child_task_id,
        agent_model=getattr(args, "agent_model", None),
        agent_provider=getattr(args, "agent_provider", None),
        agent_timeout=getattr(args, "agent_timeout", None),
        reset_failed=True,
    )
    child_result = _invoke_delivery_child_run_args(args, cfg, context, run_args)

    child_state = store.load(child_task_id)
    progress, unit_status, handoff_issue = _record_delivery_child_terminal_result(
        progress=progress,
        selected_unit=selected_unit,
        child_task_id=child_task_id,
        child_state=child_state,
        child_result=child_result,
        plan_id=plan_id,
        project_root=root,
        progress_path=progress_path,
        events_path=events_path,
    )

    if child_result.interrupted:
        raise KeyboardInterrupt
    if child_result.exception is not None:
        raise child_result.exception

    updated_status = get_delivery_status(args.plan_file, project_root=root)
    updated_unit = _status_unit_by_id(updated_status, unit_id) or selected_unit
    message = (
        handoff_issue.message
        if handoff_issue is not None
        else (
            f"Delivery unit {unit_id} retried and completed."
            if unit_status == "done"
            else f"Delivery unit {unit_id} retried and failed; inspect child task state."
        )
    )

    return DeliveryRunNextExecutionResult(
        plan_path=status.plan_path,
        project_root=str(root.resolve()),
        valid=updated_status.valid and handoff_issue is None,
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
        errors=[*updated_status.errors, *([handoff_issue] if handoff_issue else [])],
        warnings=updated_status.warnings,
        message=message,
    )


def _handle_running_delivery_unit(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    *,
    status,
    root: Path,
    plan_id: str,
    progress_path: Path,
    events_path: Path,
    running_unit,
) -> "DeliveryRunNextExecutionResult":
    from core.delivery_progress import (
        append_delivery_progress_event,
        get_delivery_status,
        make_delivery_progress_event,
        make_delivery_unit_progress,
        read_delivery_progress,
    )
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_run_next import DeliveryRunNextExecutionResult
    from core.state import JsonStateStore

    unit_id = running_unit.id
    child_task_id = running_unit.child_task_id
    if not child_task_id:
        code = "delivery.running_child_missing"
        message = (
            f"Delivery unit {unit_id} is running but has no child task id; "
            "inspect parent delivery progress before retrying."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=running_unit,
            child_task_id=None,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )
    if not _is_valid_delivery_child_task_id(child_task_id):
        code = "delivery.child_task_missing"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=running_unit,
            child_task_id=child_task_id,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    state_dir = context.resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    child_state = store.load(child_task_id)
    if child_state is None:
        code = "delivery.child_task_missing"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but that task state was not found in "
            "the configured state directory."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=running_unit,
            child_task_id=child_task_id,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    expected_plan_path = _delivery_plan_metadata_path(status, root)
    if not _delivery_child_metadata_matches(
        child_state,
        plan_id=plan_id,
        unit_id=unit_id,
        plan_path=expected_plan_path,
    ):
        code = "delivery.child_task_metadata_mismatch"
        message = (
            f"Delivery unit {unit_id} is linked to child task {child_task_id}, but the child task delivery metadata "
            "does not match the parent plan and unit; inspect child task state before retrying."
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=False,
            ran=False,
            succeeded=False,
            status=status.status,
            progress_exists=status.progress_exists,
            selected_unit=running_unit,
            child_task_id=child_task_id,
            unit_status=None,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*status.errors, DeliveryPlanIssue("error", code, message)],
            warnings=status.warnings,
            message=message,
        )

    if child_state.failed and bool(getattr(args, "reset_failed", False)):
        return _run_delivery_child_retry(
            args=args,
            cfg=cfg,
            context=context,
            status=status,
            root=root,
            plan_id=plan_id,
            progress_path=progress_path,
            events_path=events_path,
            selected_unit=running_unit,
        )

    if child_state.done or child_state.failed:
        reconcile_unit = make_delivery_unit_progress(
            unit_id, "running", child_task_id=child_task_id, timestamp=running_unit.started_at
        )
        append_delivery_progress_event(
            events_path,
            make_delivery_progress_event(plan_id, "unit.reconcile_intent", unit=reconcile_unit),
        )
        progress = _progress_for_update(status, progress_path, read_delivery_progress=read_delivery_progress)
        synthetic_child_result = DeliveryChildRunResult(exit_code=1 if child_state.failed else 0)
        progress, unit_status, handoff_issue = _record_delivery_child_terminal_result(
            progress=progress,
            selected_unit=running_unit,
            child_task_id=child_task_id,
            child_state=child_state,
            child_result=synthetic_child_result,
            plan_id=plan_id,
            project_root=root,
            progress_path=progress_path,
            events_path=events_path,
        )
        updated_status = get_delivery_status(args.plan_file, project_root=root)
        updated_unit = _status_unit_by_id(updated_status, unit_id) or running_unit
        message = (
            handoff_issue.message
            if handoff_issue is not None
            else (
                f"Delivery unit {unit_id} reconciled terminal child task as done."
                if unit_status == "done"
                else f"Delivery unit {unit_id} reconciled terminal child task as failed; inspect child task state."
            )
        )
        return DeliveryRunNextExecutionResult(
            plan_path=status.plan_path,
            project_root=str(root.resolve()),
            valid=updated_status.valid and handoff_issue is None,
            ran=True,
            succeeded=unit_status == "done" and updated_status.valid,
            status=updated_status.status,
            progress_exists=updated_status.progress_exists,
            selected_unit=updated_unit,
            child_task_id=child_task_id,
            unit_status=unit_status,
            run_exit_code=None,
            progress_path=str(progress_path),
            events_path=str(events_path),
            errors=[*updated_status.errors, *([handoff_issue] if handoff_issue else [])],
            warnings=updated_status.warnings,
            message=message,
        )

    if not _delivery_child_has_resume_worktree(child_state):
        return _delivery_child_worktree_missing_result(
            status=status,
            root=root,
            progress_path=progress_path,
            events_path=events_path,
            selected_unit=running_unit,
            child_task_id=child_task_id,
        )

    resume_unit = make_delivery_unit_progress(
        unit_id, "running", child_task_id=child_task_id, timestamp=running_unit.started_at
    )
    append_delivery_progress_event(
        events_path,
        make_delivery_progress_event(plan_id, "unit.resume_intent", unit=resume_unit),
    )
    progress = _progress_for_update(status, progress_path, read_delivery_progress=read_delivery_progress)
    run_args = _delivery_child_resume_run_args(
        child_task_id=child_task_id,
        created_task_id=child_task_id,
        agent_model=getattr(args, "agent_model", None),
        agent_provider=getattr(args, "agent_provider", None),
        agent_timeout=getattr(args, "agent_timeout", None),
    )
    child_result = _invoke_delivery_child_run_args(args, cfg, context, run_args)
    child_state = store.load(child_task_id)
    progress, unit_status, handoff_issue = _record_delivery_child_terminal_result(
        progress=progress,
        selected_unit=running_unit,
        child_task_id=child_task_id,
        child_state=child_state,
        child_result=child_result,
        plan_id=plan_id,
        project_root=root,
        progress_path=progress_path,
        events_path=events_path,
    )
    if child_result.interrupted:
        raise KeyboardInterrupt
    if child_result.exception is not None:
        raise child_result.exception

    updated_status = get_delivery_status(args.plan_file, project_root=root)
    updated_unit = _status_unit_by_id(updated_status, unit_id) or running_unit
    message = (
        handoff_issue.message
        if handoff_issue is not None
        else (
            f"Delivery unit {unit_id} resumed and completed."
            if unit_status == "done"
            else f"Delivery unit {unit_id} resumed and failed; inspect child task state."
        )
    )
    return DeliveryRunNextExecutionResult(
        plan_path=status.plan_path,
        project_root=str(root.resolve()),
        valid=updated_status.valid and handoff_issue is None,
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
        errors=[*updated_status.errors, *([handoff_issue] if handoff_issue else [])],
        warnings=updated_status.warnings,
        message=message,
    )


def _child_delivery_result_finalized(child_state) -> bool:
    if getattr(child_state, "result_commit", None):
        return True
    return not (getattr(child_state, "worktree_path", None) or getattr(child_state, "worktree_base", None))


def _delivery_plan_metadata_path(status, root: Path) -> str | None:
    try:
        return Path(status.plan_path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _delivery_child_metadata_matches(
    child_state,
    *,
    plan_id: str,
    unit_id: str,
    plan_path: str | None,
) -> bool:
    return (
        getattr(child_state, "delivery_plan_id", None) == plan_id
        and getattr(child_state, "delivery_unit_id", None) == unit_id
        and getattr(child_state, "delivery_plan_path", None) == plan_path
    )


def _delivery_child_has_resume_worktree(child_state) -> bool:
    worktree_path = getattr(child_state, "worktree_path", None)
    if not worktree_path:
        return False
    try:
        return Path(worktree_path).exists()
    except OSError:
        return False


def _delivery_child_worktree_missing_issue(selected_unit, child_task_id: str | None):
    from core.delivery_plan import DeliveryPlanIssue

    code = "delivery.child_worktree_missing"
    message = (
        f"Delivery unit {selected_unit.id} is linked to child task {child_task_id}, but the child task has no "
        "available isolated worktree path recorded; inspect child task state before resuming or retrying."
    )
    return DeliveryPlanIssue("error", code, message)


def _delivery_child_worktree_missing_result(
    *,
    status,
    root: Path,
    progress_path: Path,
    events_path: Path,
    selected_unit,
    child_task_id: str | None,
) -> "DeliveryRunNextExecutionResult":
    from core.delivery_run_next import DeliveryRunNextExecutionResult

    issue = _delivery_child_worktree_missing_issue(selected_unit, child_task_id)
    return DeliveryRunNextExecutionResult(
        plan_path=status.plan_path,
        project_root=str(root.resolve()),
        valid=False,
        ran=False,
        succeeded=False,
        status=status.status,
        progress_exists=status.progress_exists,
        selected_unit=selected_unit,
        child_task_id=child_task_id,
        unit_status=selected_unit.status,
        run_exit_code=None,
        progress_path=str(progress_path),
        events_path=str(events_path),
        errors=[*status.errors, issue],
        warnings=status.warnings,
        message=issue.message,
    )


def _record_delivery_child_terminal_result(
    *,
    progress,
    selected_unit,
    child_task_id: str | None,
    child_state,
    child_result: "DeliveryChildRunResult",
    plan_id: str,
    project_root: Path,
    progress_path: Path,
    events_path: Path,
):
    from core.delivery_handoff import (
        DeliveryHandoffError,
        build_delivery_unit_handoff,
        delivery_unit_handoff_path,
        write_delivery_unit_handoff,
    )
    from core.delivery_plan import DeliveryPlanIssue
    from core.delivery_progress import (
        append_delivery_progress_event,
        make_delivery_progress_event,
        make_delivery_unit_progress,
        upsert_delivery_unit_progress,
        write_delivery_progress,
    )

    classification = _classify_delivery_child_run(child_result, child_state)
    unit_status = classification.unit_status
    handoff = None
    handoff_schema_version = getattr(child_state, "delivery_handoff_schema_version", None) if child_state else None
    if unit_status == "done" and handoff_schema_version is not None:
        try:
            handoff = build_delivery_unit_handoff(
                plan_id=plan_id,
                selected_unit=selected_unit,
                child_task_id=child_task_id,
                child_state=child_state,
            )
            write_delivery_unit_handoff(
                delivery_unit_handoff_path(project_root, plan_id, selected_unit.id),
                handoff,
            )
        except (DeliveryHandoffError, OSError):
            running_unit = make_delivery_unit_progress(
                selected_unit.id,
                "running",
                child_task_id=child_task_id,
                branch=getattr(child_state, "worktree_branch", None),
                commit=getattr(child_state, "result_commit", None),
                timestamp=getattr(selected_unit, "started_at", None),
            )
            progress = upsert_delivery_unit_progress(progress, running_unit)
            write_delivery_progress(progress_path, progress)
            append_delivery_progress_event(
                events_path,
                make_delivery_progress_event(plan_id, "unit.handoff_write_failed", unit=running_unit),
            )
            issue = DeliveryPlanIssue(
                "error",
                "delivery.unit_handoff_write_failed",
                (
                    f"Delivery unit {selected_unit.id} child completed, but its handoff could not be persisted; "
                    "rerun delivery run-next to reconcile it."
                ),
            )
            return progress, "running", issue

    terminal_unit = make_delivery_unit_progress(
        selected_unit.id,
        unit_status,
        child_task_id=child_task_id,
        branch=getattr(child_state, "worktree_branch", None) if child_state else None,
        commit=getattr(child_state, "result_commit", None) if child_state else None,
        failure_code=classification.failure_code,
        budget_exceeded=classification.budget_exceeded,
        handoff_schema_version=handoff.schema_version if handoff else None,
        handoff_fingerprint=handoff.fingerprint if handoff else None,
    )
    progress = upsert_delivery_unit_progress(progress, terminal_unit)
    write_delivery_progress(progress_path, progress)
    append_delivery_progress_event(
        events_path,
        make_delivery_progress_event(plan_id, f"unit.{unit_status}", unit=terminal_unit),
    )
    return progress, unit_status, None


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
    budget_exceeded = _delivery_child_budget_exceeded(child_state)
    if budget_exceeded is not None:
        return DeliveryChildRunClassification(
            "failed",
            DELIVERY_UNIT_BUDGET_EXCEEDED_CODE,
            budget_exceeded,
        )
    if child_result.exit_code != 0:
        return DeliveryChildRunClassification("failed", "child_run_failed")
    if not getattr(child_state, "done", False):
        return DeliveryChildRunClassification("failed", "child_task_incomplete")
    if not _child_delivery_result_finalized(child_state):
        return DeliveryChildRunClassification("failed", "child_run_unfinalized")
    return DeliveryChildRunClassification("done", None)


def _delivery_child_budget_exceeded(child_state) -> DeliveryBudgetExceeded | None:
    value = getattr(child_state, "delivery_budget_stop", None)
    if not isinstance(value, dict) or value.get("code") != DELIVERY_UNIT_BUDGET_EXCEEDED_CODE:
        return None
    name = value.get("name")
    limit = value.get("limit")
    actual = value.get("actual")
    if (
        name != "max_planner_steps"
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or not isinstance(actual, int)
        or isinstance(actual, bool)
        or actual < 0
    ):
        return None
    return DeliveryBudgetExceeded(name=name, limit=limit, actual=actual)


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
    if unit.status == "superseded":
        return False
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
    delivery_unit_budget: dict[str, int] | None = None,
    delivery_handoff_schema_version: int | None = None,
    delivery_dependency_handoffs: list[dict] | None = None,
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
        delivery_unit_budget=delivery_unit_budget,
        delivery_handoff_schema_version=delivery_handoff_schema_version,
        delivery_dependency_handoffs=delivery_dependency_handoffs,
        delivery_child_created_callback=delivery_child_created_callback,
    )
    return _invoke_delivery_child_run_args(args, cfg, context, run_args)


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
    delivery_unit_budget: dict[str, int] | None = None,
    delivery_handoff_schema_version: int | None = None,
    delivery_dependency_handoffs: list[dict] | None = None,
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
        agent_model=_filter_agent_override_entries(agent_model, RUNTIME_AGENT_NAMES),
        agent_provider=_filter_agent_override_entries(agent_provider, RUNTIME_AGENT_NAMES),
        agent_timeout=_filter_agent_override_entries(agent_timeout, RUNTIME_AGENT_NAMES),
        delivery_plan_id=delivery_plan_id,
        delivery_unit_id=delivery_unit_id,
        delivery_plan_path=delivery_plan_path,
        delivery_unit_budget=dict(delivery_unit_budget or {}),
        delivery_handoff_schema_version=delivery_handoff_schema_version,
        delivery_dependency_handoffs=copy.deepcopy(delivery_dependency_handoffs or []),
        delivery_child_created_callback=delivery_child_created_callback,
        created_task_id=None,
    )


def _delivery_child_resume_run_args(
    *,
    child_task_id: str,
    created_task_id: str,
    agent_model: list[str] | None = None,
    agent_provider: list[str] | None = None,
    agent_timeout: list[str] | None = None,
    reset_failed: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        task_file=None,
        task_file_pos=None,
        task_id=child_task_id,
        no_isolate=False,
        reset_failed=reset_failed,
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
        agent_model=_filter_agent_override_entries(agent_model, RUNTIME_AGENT_NAMES),
        agent_provider=_filter_agent_override_entries(agent_provider, RUNTIME_AGENT_NAMES),
        agent_timeout=_filter_agent_override_entries(agent_timeout, RUNTIME_AGENT_NAMES),
        delivery_plan_id=None,
        delivery_unit_id=None,
        delivery_plan_path=None,
        delivery_unit_budget=None,
        delivery_handoff_schema_version=None,
        delivery_dependency_handoffs=None,
        delivery_child_created_callback=None,
        created_task_id=created_task_id,
    )


def _invoke_delivery_child_run_args(
    args: argparse.Namespace,
    cfg: dict,
    context: DeliveryRunNextContext,
    run_args: argparse.Namespace,
) -> DeliveryChildRunResult:
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
