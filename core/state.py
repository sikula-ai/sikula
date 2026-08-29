"""State model and persistence abstraction.

JsonStateStore is the default implementation. To migrate to a database,
subclass StateStore and swap it in sikula.py — nothing else needs to change.
"""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.delivery_unit_metadata import DELIVERY_UNIT_BUDGET_EXCEEDED_CODE
from core.delivery_public_metadata import is_safe_delivery_public_metadata
from core.diagnostics import diagnostic_excerpt, diagnostic_summary_lines, validation_error_excerpt
from core.llm_usage import aggregate_llm_usage, aggregate_llm_usage_by_agent, sanitize_llm_usage_record
from core.structured_output import (
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
    DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT,
    DELIVERY_DISPOSITION_SCHEMA_VERSION,
    MAX_DELIVERY_DISPOSITION_SUMMARY_CHARS,
    DeliveryDisposition,
    delivery_disposition_recommended_action,
)
from core.version import sikula_version

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

DELIVERY_STOP_UNIT_SCOPE_VIOLATION = "unit_scope_violation"
DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED = "scope_amendment_required"
DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP = "external_dependency_gap"
DELIVERY_STOP_IMPLEMENTER_DISPOSITION_INVALID = "implementer_disposition_invalid"
DELIVERY_TERMINAL_STOP_CODES = frozenset(
    {
        DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
        DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
        DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
        DELIVERY_STOP_IMPLEMENTER_DISPOSITION_INVALID,
    }
)

DELIVERY_DISPOSITION_PARSE_ERROR_SCHEMA_VERSION = 1
_DELIVERY_DISPOSITION_PARSE_ERROR_KEYS = frozenset(
    {
        "schema_version",
        "error_code",
        "source",
        "timestamp",
    }
)
_DELIVERY_DISPOSITION_PARSE_ERROR_CODE_RE = re.compile(r"^delivery_disposition\.[a-z][a-z0-9_]*$")

_DELIVERY_STOP_DISPOSITION_KEYS = frozenset(
    {
        "schema_version",
        "disposition",
        "summary",
        "recommended_action",
        "source",
        "timestamp",
    }
)
_DELIVERY_STOP_DISPOSITION_CODES = {
    DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT: DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED,
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP: DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP,
}
_DELIVERY_STOP_PRIORITIES = {
    DELIVERY_STOP_EXTERNAL_DEPENDENCY_GAP: 1,
    DELIVERY_STOP_IMPLEMENTER_DISPOSITION_INVALID: 2,
    DELIVERY_STOP_SCOPE_AMENDMENT_REQUIRED: 3,
    DELIVERY_STOP_UNIT_SCOPE_VIOLATION: 4,
}
_DELIVERY_STOP_SOURCES = frozenset(
    {
        "analyst",
        "implementer",
        "reviewer",
        "security_reviewer",
        "orchestrator",
    }
)


def _is_valid_state_task_id(task_id: str) -> bool:
    if not isinstance(task_id, str):
        return False
    return bool(_TASK_ID_RE.fullmatch(task_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_text(text: str | None, limit: int = 1000) -> str | None:
    if not text:
        return None
    return diagnostic_excerpt(text, limit=limit)


def _sanitize_test_execution_gate_finding(finding: dict) -> dict:
    return {key: value for key, value in finding.items() if key != "excerpt"}


def _strip_excerpt_fields(value):
    if isinstance(value, dict):
        return {key: _strip_excerpt_fields(item) for key, item in value.items() if key != "excerpt"}
    if isinstance(value, list):
        return [_strip_excerpt_fields(item) for item in value]
    return value


def _sanitize_synthetic_test_harness_finding(finding: dict) -> dict:
    return _strip_excerpt_fields(finding)


_IMPLEMENTATION_ASSET_STRING_KEYS = (
    "path",
    "kind",
    "status",
    "project_path",
    "sha256",
    "mime_type",
    "git_status",
    "requested_target",
    "source_license",
    "declared_sha256",
)
_IMPLEMENTATION_ASSET_INT_KEYS = ("line", "size_bytes")
_IMPLEMENTATION_ASSET_BOOL_KEYS = ("target_specified", "provenance_specified")
_IMPLEMENTATION_ASSET_WARNING_STATUSES = {"missing", "outside_project", "not_file"}
_IMPLEMENTATION_ASSET_WARNING_GIT_STATUSES = {"dirty", "ignored", "untracked"}
_IMPLEMENTATION_ASSET_DRIFT_STRING_KEYS = (
    "path",
    "project_path",
    "kind",
    "phase",
    "status",
    "expected_source",
    "expected_sha256",
    "current_sha256",
    "current_status",
    "git_status",
    "mime_type",
    "observed_at",
)
_IMPLEMENTATION_ASSET_DRIFT_INT_KEYS = ("size_bytes",)
_IMPLEMENTATION_ASSET_TARGET_STRING_KEYS = (
    "path",
    "project_path",
    "kind",
    "phase",
    "status",
    "requested_target",
    "matched_path",
    "observed_at",
)
_IMPLEMENTATION_ASSET_TARGET_WARNING_STATUSES = {"missing", "outside_project"}


def _sanitize_implementation_asset_record(record: dict) -> dict:
    sanitized: dict = {}
    for key in _IMPLEMENTATION_ASSET_STRING_KEYS:
        value = record.get(key)
        if value is None:
            continue
        text = _short_text(str(value), limit=1000)
        if text:
            sanitized[key] = text
    for key in _IMPLEMENTATION_ASSET_INT_KEYS:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            sanitized[key] = value
    for key in _IMPLEMENTATION_ASSET_BOOL_KEYS:
        value = record.get(key)
        if isinstance(value, bool):
            sanitized[key] = value
    return sanitized


def _sanitize_implementation_asset_drift_record(record: dict) -> dict:
    sanitized: dict = {}
    for key in _IMPLEMENTATION_ASSET_DRIFT_STRING_KEYS:
        value = record.get(key)
        if value is None:
            continue
        text = _short_text(str(value), limit=1000)
        if text:
            sanitized[key] = text
    for key in _IMPLEMENTATION_ASSET_DRIFT_INT_KEYS:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            sanitized[key] = value
    return sanitized


def _sanitize_implementation_asset_target_record(record: dict) -> dict:
    sanitized: dict = {}
    for key in _IMPLEMENTATION_ASSET_TARGET_STRING_KEYS:
        value = record.get(key)
        if value is None:
            continue
        text = _short_text(str(value), limit=1000)
        if text:
            sanitized[key] = text
    return sanitized


def _implementation_asset_drift_key(record: dict) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        str(record.get("path") or "").strip(),
        str(record.get("project_path") or "").strip(),
        str(record.get("phase") or "").strip(),
        str(record.get("status") or "").strip(),
        str(record.get("expected_source") or "").strip(),
        str(record.get("expected_sha256") or "").strip(),
        str(record.get("current_sha256") or "").strip(),
        str(record.get("current_status") or "").strip(),
    )


def _implementation_asset_target_key(record: dict) -> tuple[str, str, str, str, str, str]:
    return (
        str(record.get("path") or "").strip(),
        str(record.get("project_path") or "").strip(),
        str(record.get("phase") or "").strip(),
        str(record.get("status") or "").strip(),
        str(record.get("requested_target") or "").strip(),
        str(record.get("matched_path") or "").strip(),
    )


def _implementation_asset_kind_counts(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = str(record.get("kind") or "unknown").strip() or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _implementation_asset_has_warning(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    status = str(record.get("status") or "").strip()
    git_status = str(record.get("git_status") or "").strip()
    kind = str(record.get("kind") or "").strip()
    if status in _IMPLEMENTATION_ASSET_WARNING_STATUSES:
        return True
    if git_status in _IMPLEMENTATION_ASSET_WARNING_GIT_STATUSES:
        return True
    if kind == "ambiguous":
        return True
    return kind == "delivery" and not str(record.get("source_license") or "").strip()


def _implementation_asset_warning_count(records: list[dict]) -> int:
    return sum(1 for record in records if _implementation_asset_has_warning(record))


def _implementation_asset_target_warning_count(records: list[dict]) -> int:
    return sum(
        1
        for record in records
        if isinstance(record, dict)
        and str(record.get("status") or "").strip() in _IMPLEMENTATION_ASSET_TARGET_WARNING_STATUSES
    )


def runtime_metadata_snapshot() -> dict:
    return {
        "sikula_version": sikula_version(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _terminal_result(state: "TaskState") -> str:
    if (
        state.review_mode == "review_fix"
        and state.review_delivery_mode == "current_branch"
        and state.review_delivery_status not in {"delivered", "no_changes"}
    ):
        if state.failed or state.review_delivery_status == "failed":
            return "failed"
        return "incomplete"
    if state.done:
        return "done"
    if state.failed:
        return "failed"
    return "incomplete"


def _is_terminal_for_audit(state: "TaskState") -> bool:
    return _terminal_result(state) != "incomplete"


def _analyst_runs_count(state: "TaskState") -> int:
    count = 1 if state.analyst_prompt is not None else 0
    for record in state.analyst_retry_records:
        attempt = record.get("attempt")
        if type(attempt) is int and attempt > 0:
            count = max(count, attempt + (1 if record.get("will_retry") is True else 0))
    for record in state.analyst_cycle_records:
        attempt = record.get("attempt")
        if type(attempt) is int and attempt > 0:
            count = max(count, attempt)
    return count


def _final_summary(state: "TaskState") -> dict:
    created_at = _parse_iso(state.created_at)
    finished_at = _parse_iso(state.finished_at)
    summary: dict = {
        "result": _terminal_result(state),
        "task_id": state.task_id,
        "branch": state.worktree_branch,
        "commit": state.result_commit,
        "build_attempts": state.build_iterations,
        "build_status": state.build_status,
        "test_status": state.test_status,
        "check_status": state.check_status,
        "files_changed_count": len(state.files_changed),
        "test_files_written_count": len(state.test_files_written),
        "validation_records_count": len(state.validation_cycle_records),
        "validation_failures_count": sum(
            1 for entry in state.validation_cycle_records if entry.get("status") == "failed"
        ),
        "validation_artifacts_count": len(state.validation_artifact_records),
        "fix_attempts": len(state.fix_cycle_records),
        "plan_completed": state.plan_completed,
        "final_full_task_review_done": state.final_full_task_review_done,
        "review_records_count": len(state.review_cycle_records),
        "security_review_records_count": len(state.security_review_cycle_records),
        "reviewer_runs": len(state.review_cycle_records),
        "security_reviewer_runs": len(state.security_review_cycle_records),
        "review_delivery_mode": state.review_delivery_mode,
        "review_target_branch": state.review_target_branch,
        "review_target_start_commit": state.review_target_start_commit,
        "review_isolated_fix_commit": state.review_isolated_fix_commit,
        "review_delivery_status": state.review_delivery_status,
        "review_delivery_result": state.review_delivery_result,
        "test_writer_runs": len(state.test_write_records),
        "testability_gaps_count": len(state.testability_gaps),
        "test_execution_gate_audits_count": len(state.test_execution_gate_records),
        "synthetic_test_harness_audits_count": len(state.synthetic_test_harness_records),
        "implementation_asset_records_count": len(state.implementation_asset_records),
        "implementation_asset_records_by_kind": _implementation_asset_kind_counts(state.implementation_asset_records),
        "implementation_asset_warnings_count": _implementation_asset_warning_count(state.implementation_asset_records),
        "implementation_asset_drift_records_count": len(state.implementation_asset_drift_records),
        "implementation_asset_target_records_count": len(state.implementation_asset_target_records),
        "implementation_asset_target_warnings_count": _implementation_asset_target_warning_count(
            state.implementation_asset_target_records
        ),
        "analyst_runs_count": _analyst_runs_count(state),
        "analyst_retries_count": len(state.analyst_retry_records),
        "planner_retries_count": len(state.planner_retry_records),
        "delivery_unit_budget": dict(state.delivery_unit_budget),
        "delivery_budget_stop": dict(state.delivery_budget_stop) if state.delivery_budget_stop else None,
        "delivery_stop_code": state.delivery_stop_code,
        "delivery_disposition_parse_error": (
            dict(state.delivery_disposition_parse_error) if state.delivery_disposition_parse_error else None
        ),
        "delivery_handoff_schema_version": state.delivery_handoff_schema_version,
        "delivery_dependency_handoffs_count": len(state.delivery_dependency_handoffs),
        "llm_retries": sum(1 for entry in state.history if entry.get("action") == "llm_retry"),
        "llm_usage": aggregate_llm_usage(state.llm_usage_records),
        "llm_usage_by_agent": aggregate_llm_usage_by_agent(state.llm_usage_records),
        "history_events_count": len(state.history),
        "created_at": state.created_at,
        "finished_at": state.finished_at,
    }
    if created_at and finished_at:
        summary["wall_elapsed_s"] = round((finished_at - created_at).total_seconds(), 1)
    return summary


SCHEMA_VERSION = 2
RUN_INVOCATION_SCHEMA_VERSION = 1


def _without_reviewer_field(record: dict) -> dict:
    normalized = dict(record)
    normalized.pop("reviewer", None)
    return normalized


def _migrate_review_cycle_records(data: dict) -> None:
    review_records = data.get("review_cycle_records")
    existing_security_records = data.get("security_review_cycle_records")
    security_records = existing_security_records if isinstance(existing_security_records, list) else []

    normalized_reviews = []
    migrated_security_reviews = []
    if not isinstance(review_records, list):
        review_records = []

    for record in review_records:
        if not isinstance(record, dict):
            normalized_reviews.append(record)
            continue

        reviewer = record.get("reviewer")
        normalized_record = _without_reviewer_field(record)
        if reviewer == "security_reviewer":
            migrated_security_reviews.append(normalized_record)
        else:
            normalized_reviews.append(normalized_record)

    normalized_security_records = [
        _without_reviewer_field(record) if isinstance(record, dict) else record for record in security_records
    ]

    data["review_cycle_records"] = normalized_reviews
    data["security_review_cycle_records"] = normalized_security_records + migrated_security_reviews


@dataclass
class TaskState:
    task_id: str
    task_description: str
    schema_version: int = SCHEMA_VERSION
    config_snapshot: dict = field(default_factory=dict)
    run_invocation_schema_version: Optional[int] = None
    run_invocation_records: list[dict] = field(default_factory=list)
    implementation_contract: dict = field(default_factory=dict)
    implementation_asset_records: list[dict] = field(default_factory=list)
    implementation_asset_drift_records: list[dict] = field(default_factory=list)
    implementation_asset_target_records: list[dict] = field(default_factory=list)
    contract_gate_blocked: bool = False
    analyst_prompt: Optional[str] = None
    planner_prompt: Optional[str] = None
    planner_output: Optional[str] = None
    implementation_prompt: Optional[str] = None
    presync_done: bool = False
    plan: list[str] = field(default_factory=list)
    plan_decided: bool = False
    plan_completed: bool = False
    current_step: int = 0
    step_implemented: bool = False
    step_file_tracking_enabled: bool = False
    step_files_changed: list[str] = field(default_factory=list)
    active_scope: Optional[str] = None
    final_full_task_review_done: bool = False
    files_changed: list[str] = field(default_factory=list)
    build_synced: bool = False
    build_iterations: int = 0  # total build/fix attempts across all build loops
    build_loop_key: Optional[str] = None
    build_loop_start_iteration: int = 0
    build_status: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    test_errors: list[str] = field(default_factory=list)
    check_errors: list[str] = field(default_factory=list)
    review_issues: list[str] = field(default_factory=list)
    review_iterations: int = 0
    review_approved: bool = False
    security_approved: bool = False
    security_review_iterations: int = 0
    analyst_warnings: list[str] = field(default_factory=list)
    analyst_retry_records: list[dict] = field(default_factory=list)
    review_diff: Optional[str] = None
    review_mode: Optional[str] = None
    review_base_branch: Optional[str] = None
    review_delivery_mode: Optional[str] = None
    review_target_branch: Optional[str] = None
    review_target_start_commit: Optional[str] = None
    review_isolated_fix_commit: Optional[str] = None
    review_delivery_status: Optional[str] = None
    review_delivery_result: Optional[str] = None
    test_files_written: list[str] = field(default_factory=list)
    tests_up_to_date: bool = False
    generated_test_fix_counts: dict[str, int] = field(default_factory=dict)
    test_writer_audit_pending: bool = False
    test_writer_audit_agent_completed: bool = False
    test_writer_audit_files_written: list[str] = field(default_factory=list)
    test_writer_audit_gate_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    fixer_changed_code: bool = (
        False  # set when fixer writes files; cleared after build validates; guards resume skip condition
    )
    # Structured observability records; never read for pipeline decisions.
    llm_usage_records: list[dict] = field(default_factory=list)
    analyst_cycle_records: list[dict] = field(default_factory=list)
    planner_retry_records: list[dict] = field(default_factory=list)
    implement_cycle_records: list[dict] = field(default_factory=list)
    review_cycle_records: list[dict] = field(default_factory=list)
    security_review_cycle_records: list[dict] = field(default_factory=list)
    test_write_records: list[dict] = field(default_factory=list)
    testability_gaps: list[dict] = field(default_factory=list)
    test_execution_gate_records: list[dict] = field(default_factory=list)
    synthetic_test_harness_records: list[dict] = field(default_factory=list)
    fix_cycle_records: list[dict] = field(default_factory=list)
    validation_cycle_records: list[dict] = field(default_factory=list)
    validation_artifact_records: list[dict] = field(default_factory=list)
    task_file: Optional[str] = None
    delivery_plan_id: Optional[str] = None
    delivery_unit_id: Optional[str] = None
    delivery_plan_path: Optional[str] = None
    delivery_unit_budget: dict[str, int] = field(default_factory=dict)
    delivery_budget_stop: Optional[dict] = None
    delivery_stop_code: Optional[str] = None
    delivery_stop_disposition: Optional[dict] = None
    delivery_disposition_parse_error: Optional[dict] = None
    delivery_constraint_context_schema_version: Optional[int] = None
    delivery_source_task: Optional[dict[str, str]] = None
    delivery_inherited_constraints: list[dict] = field(default_factory=list)
    delivery_constraint_context_fingerprint: Optional[str] = None
    delivery_write_scope_schema_version: Optional[int] = None
    delivery_write_scope_mode: Optional[str] = None
    delivery_declared_write_paths: list[str] = field(default_factory=list)
    delivery_declared_write_exact_file_paths: Optional[list[str]] = None
    delivery_effective_write_paths: list[str] = field(default_factory=list)
    delivery_effective_write_exact_file_paths: Optional[list[str]] = None
    delivery_runtime_write_scope_binding: Optional[dict] = None
    delivery_scope_audit_pending: Optional[dict] = None
    delivery_handoff_schema_version: Optional[int] = None
    delivery_dependency_handoffs: list[dict] = field(default_factory=list)
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    worktree_base: Optional[str] = None
    history: list[dict] = field(default_factory=list)
    runtime_metadata: dict = field(default_factory=dict)
    final_summary: dict = field(default_factory=dict)
    active_operation: Optional[dict] = None
    done: bool = False
    failed: bool = False
    finished_at: Optional[str] = None
    result_commit: Optional[str] = None
    test_status: Optional[str] = None
    check_status: Optional[str] = None
    pid: Optional[int] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def record(
        self,
        agent: str,
        action: str,
        result: str,
        elapsed_s: float | None = None,
        error: str | None = None,
    ) -> None:
        entry: dict = {
            "agent": agent,
            "action": action,
            "result": result,
            "timestamp": _now(),
        }
        if elapsed_s is not None:
            entry["elapsed_s"] = round(elapsed_s, 1)
        if error:
            entry["error"] = error
        self.history.append(entry)

    def record_llm_usage(self, agent: str, event: dict[str, object]) -> None:
        record = sanitize_llm_usage_record(event, agent=agent)
        if record is None:
            return
        record["recorded_at"] = _now()
        self.llm_usage_records.append(record)

    def record_run_invocation(
        self,
        config_snapshot: dict,
        *,
        complete_history_from_creation: bool = False,
    ) -> None:
        if complete_history_from_creation and (
            self.run_invocation_schema_version is not None or self.run_invocation_records
        ):
            raise ValueError("complete run-invocation history can only be marked on the first record")
        self.run_invocation_records.append(
            {
                "started_at": _now(),
                "config_snapshot": copy.deepcopy(config_snapshot),
            }
        )
        if complete_history_from_creation:
            self.run_invocation_schema_version = RUN_INVOCATION_SCHEMA_VERSION

    def record_implementation_assets(self, records: list[dict]) -> None:
        sanitized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            sanitized_record = _sanitize_implementation_asset_record(record)
            if sanitized_record:
                sanitized.append(sanitized_record)
        self.implementation_asset_records = sanitized
        if sanitized:
            self.record("orchestrator", "asset_snapshot", f"{len(sanitized)} implementation asset reference(s)")

    def record_implementation_asset_drift(self, records: list[dict]) -> None:
        sanitized = []
        existing_keys = {_implementation_asset_drift_key(record) for record in self.implementation_asset_drift_records}
        for record in records:
            if not isinstance(record, dict):
                continue
            sanitized_record = _sanitize_implementation_asset_drift_record(record)
            if not sanitized_record:
                continue
            key = _implementation_asset_drift_key(sanitized_record)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            sanitized.append(sanitized_record)
        if not sanitized:
            return
        self.implementation_asset_drift_records.extend(sanitized)
        self.record("orchestrator", "asset_drift", f"{len(sanitized)} implementation asset drift warning(s)")

    def record_implementation_asset_targets(self, records: list[dict]) -> None:
        sanitized = []
        existing_keys = {
            _implementation_asset_target_key(record) for record in self.implementation_asset_target_records
        }
        for record in records:
            if not isinstance(record, dict):
                continue
            sanitized_record = _sanitize_implementation_asset_target_record(record)
            if not sanitized_record:
                continue
            key = _implementation_asset_target_key(sanitized_record)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            sanitized.append(sanitized_record)
        if not sanitized:
            return
        self.implementation_asset_target_records.extend(sanitized)
        warning_count = _implementation_asset_target_warning_count(sanitized)
        summary = f"{len(sanitized)} delivery asset target audit record(s)"
        if warning_count:
            summary += f"; {warning_count} warning(s)"
        self.record("orchestrator", "asset_target_audit", summary)

    def record_analyst_retry(
        self,
        attempt: int,
        reason: str,
        output: str | None,
        *,
        will_retry: bool,
        retry_prompt: str | None = None,
    ) -> None:
        reason_excerpt = _short_text(reason, limit=500) or ""
        entry: dict = {
            "attempt": attempt,
            "reason": reason_excerpt,
            "will_retry": will_retry,
            "timestamp": _now(),
        }
        if output is not None:
            entry["output"] = output
        if retry_prompt is not None:
            entry["retry_prompt"] = retry_prompt
        self.analyst_retry_records.append(entry)
        action = "analyze_retry" if will_retry else "analyze_rejected"
        self.record("analyst", action, reason_excerpt[:500])

    def record_planner_retry(
        self,
        attempt: int,
        reason: str,
        output: str | None,
        *,
        max_steps: int,
        parsed_step_count: int,
        will_retry: bool,
        retry_prompt: str | None = None,
    ) -> None:
        reason_excerpt = _short_text(reason, limit=500) or ""
        entry: dict = {
            "attempt": attempt,
            "reason": reason_excerpt,
            "max_steps": max_steps,
            "parsed_step_count": parsed_step_count,
            "will_retry": will_retry,
            "timestamp": _now(),
        }
        if output is not None:
            entry["output"] = output
        if retry_prompt is not None:
            entry["retry_prompt"] = retry_prompt
        self.planner_retry_records.append(entry)
        action = "plan_retry" if will_retry else "plan_rejected"
        self.record("planner", action, reason_excerpt[:500])

    def record_delivery_budget_stop(self, *, name: str, limit: int, actual: int) -> None:
        self.delivery_budget_stop = {
            "code": DELIVERY_UNIT_BUDGET_EXCEEDED_CODE,
            "name": name,
            "limit": limit,
            "actual": actual,
            "phase": "planner",
            "timestamp": _now(),
        }
        self.record(
            "orchestrator",
            "delivery_budget_exceeded",
            f"{name} exceeded: limit={limit}, actual={actual}; split the delivery unit before implementation",
        )

    def set_delivery_stop_disposition(self, source: str, disposition: DeliveryDisposition) -> None:
        if source not in {"analyst", "implementer", "reviewer", "security_reviewer"}:
            raise ValueError("delivery stop disposition source is invalid")
        if not isinstance(disposition, DeliveryDisposition):
            raise TypeError("delivery stop disposition must be parser-validated")
        if disposition.disposition not in {
            DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT,
            DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
        }:
            raise ValueError("delivery stop disposition is not terminal")
        if disposition.disposition == DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT and source not in {
            "reviewer",
            "security_reviewer",
        }:
            raise ValueError("scope-amendment disposition source is invalid")
        self.delivery_stop_disposition = {
            **disposition.to_dict(),
            "source": source,
            "timestamp": _now(),
        }
        self.record(
            source,
            "delivery_stop_disposition",
            f"{disposition.disposition}; recommended action: {disposition.recommended_action}",
        )

    def record_delivery_disposition_parse_error(self, source: str, error_code: str) -> None:
        if source != "implementer":
            raise ValueError("delivery disposition parse-error source is invalid")
        if not isinstance(error_code, str) or not _DELIVERY_DISPOSITION_PARSE_ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("delivery disposition parse-error code is invalid")
        self.delivery_disposition_parse_error = {
            "schema_version": DELIVERY_DISPOSITION_PARSE_ERROR_SCHEMA_VERSION,
            "error_code": error_code,
            "source": source,
            "timestamp": _now(),
        }
        self.record(source, "delivery_disposition_parse_error", error_code)

    def delivery_stop_code_from_parse_error(self) -> str | None:
        value = self.delivery_disposition_parse_error
        if value is None:
            return None
        if not isinstance(value, dict) or frozenset(value) != _DELIVERY_DISPOSITION_PARSE_ERROR_KEYS:
            raise ValueError("delivery disposition parse-error state is malformed")
        schema_version = value.get("schema_version")
        error_code = value.get("error_code")
        source = value.get("source")
        timestamp = value.get("timestamp")
        if type(schema_version) is not int or schema_version != DELIVERY_DISPOSITION_PARSE_ERROR_SCHEMA_VERSION:
            raise ValueError("delivery disposition parse-error schema is invalid")
        if not isinstance(error_code, str) or not _DELIVERY_DISPOSITION_PARSE_ERROR_CODE_RE.fullmatch(error_code):
            raise ValueError("delivery disposition parse-error code is invalid")
        if source != "implementer":
            raise ValueError("delivery disposition parse-error source is invalid")
        if not isinstance(timestamp, str) or _parse_iso(timestamp) is None:
            raise ValueError("delivery disposition parse-error timestamp is invalid")
        return DELIVERY_STOP_IMPLEMENTER_DISPOSITION_INVALID

    def delivery_stop_code_from_disposition(self) -> str | None:
        value = self.delivery_stop_disposition
        if value is None:
            return None
        if not isinstance(value, dict) or frozenset(value) != _DELIVERY_STOP_DISPOSITION_KEYS:
            raise ValueError("delivery stop disposition state is malformed")
        schema_version = value.get("schema_version")
        disposition = value.get("disposition")
        summary = value.get("summary")
        action = value.get("recommended_action")
        source = value.get("source")
        timestamp = value.get("timestamp")
        if type(schema_version) is not int or schema_version != DELIVERY_DISPOSITION_SCHEMA_VERSION:
            raise ValueError("delivery stop disposition schema is invalid")
        if not isinstance(disposition, str) or disposition not in _DELIVERY_STOP_DISPOSITION_CODES:
            raise ValueError("delivery stop disposition value is invalid")
        if action != delivery_disposition_recommended_action(disposition):
            raise ValueError("delivery stop disposition recovery action is invalid")
        if (
            not isinstance(summary, str)
            or not summary
            or summary != summary.strip()
            or len(summary) > MAX_DELIVERY_DISPOSITION_SUMMARY_CHARS
            or not is_safe_delivery_public_metadata(summary)
        ):
            raise ValueError("delivery stop disposition summary is invalid")
        if not isinstance(source, str) or source not in _DELIVERY_STOP_SOURCES - {"orchestrator"}:
            raise ValueError("delivery stop disposition source is invalid")
        if disposition == DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT and source not in {
            "reviewer",
            "security_reviewer",
        }:
            raise ValueError("scope-amendment disposition source is invalid")
        if not isinstance(timestamp, str) or _parse_iso(timestamp) is None:
            raise ValueError("delivery stop disposition timestamp is invalid")
        return _DELIVERY_STOP_DISPOSITION_CODES[disposition]

    def set_delivery_terminal_stop(self, code: str, *, source: str) -> None:
        if not isinstance(code, str) or code not in DELIVERY_TERMINAL_STOP_CODES:
            raise ValueError("delivery terminal stop code is invalid")
        if not isinstance(source, str) or source not in _DELIVERY_STOP_SOURCES:
            raise ValueError("delivery terminal stop source is invalid")
        current = self.delivery_stop_code
        if current is not None and current not in DELIVERY_TERMINAL_STOP_CODES:
            raise ValueError("persisted delivery terminal stop code is invalid")
        self.failed = True
        self.done = False
        if current is not None and _DELIVERY_STOP_PRIORITIES[current] >= _DELIVERY_STOP_PRIORITIES[code]:
            return
        self.delivery_stop_code = code
        self.record(source, "delivery_terminal_stop", code)

    def record_testability_gap(
        self,
        source: str,
        message: str,
        target: str | None = None,
        reason: str | None = None,
        covered_by: str | None = None,
        recommended_action: str | None = None,
        risk: str | None = None,
    ) -> None:
        message_excerpt = _short_text(message, limit=2000) or ""
        entry: dict = {
            "source": source,
            "step": self.current_step,
            "build_iteration": self.build_iterations,
            "message": message_excerpt,
            "timestamp": _now(),
        }
        if self.active_scope:
            entry["scope"] = self.active_scope
        if target:
            entry["target"] = target
        if reason:
            entry["reason"] = reason
        if covered_by:
            entry["covered_by"] = covered_by
        if recommended_action:
            entry["recommended_action"] = recommended_action
        if risk:
            entry["risk"] = risk
        self.testability_gaps.append(entry)
        self.record(source, "testability_gap", message_excerpt[:500])

    def record_test_execution_gate_audit(self, source: str, findings: list[dict], status: str = "detected") -> None:
        if not findings:
            return
        sanitized_findings = [_sanitize_test_execution_gate_finding(finding) for finding in findings]
        entry: dict = {
            "source": source,
            "step": self.current_step,
            "build_iteration": self.build_iterations,
            "status": status,
            "findings": sanitized_findings,
            "timestamp": _now(),
        }
        if self.active_scope:
            entry["scope"] = self.active_scope
        self.test_execution_gate_records.append(entry)
        self.record(source, "test_execution_gate_audit", f"{len(findings)} newly added execution gate(s)")

    def record_synthetic_test_harness_audit(self, source: str, findings: list[dict], status: str = "detected") -> None:
        if not findings:
            return
        sanitized_findings = [_sanitize_synthetic_test_harness_finding(finding) for finding in findings]
        entry: dict = {
            "source": source,
            "step": self.current_step,
            "build_iteration": self.build_iterations,
            "status": status,
            "findings": sanitized_findings,
            "timestamp": _now(),
        }
        if self.active_scope:
            entry["scope"] = self.active_scope
        self.synthetic_test_harness_records.append(entry)
        self.record(source, "synthetic_test_harness_audit", f"{len(findings)} synthetic test harness finding(s)")

    def record_validation(
        self,
        phase: str,
        status: str,
        elapsed_s: float | None = None,
        error: str | None = None,
        check_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        entry: dict = {
            "phase": phase,
            "status": status,
            "build_iteration": self.build_iterations,
            "step": self.current_step,
            "timestamp": _now(),
        }
        if self.active_scope:
            entry["scope"] = self.active_scope
        if check_name:
            entry["check_name"] = check_name
        if elapsed_s is not None:
            entry["elapsed_s"] = round(elapsed_s, 1)
        error_excerpt = validation_error_excerpt(error, limit=1000)
        if error_excerpt:
            entry["error_excerpt"] = error_excerpt
        diagnostic_summary = diagnostic_summary_lines(error)
        if diagnostic_summary:
            entry["diagnostic_summary"] = diagnostic_summary
        if metadata:
            entry["metadata"] = metadata
        self.validation_cycle_records.append(entry)

    def start_active_operation(
        self,
        phase: str,
        agent: str | None = None,
        scope: str | None = None,
        message: str | None = None,
        heartbeat_interval_seconds: int | None = None,
    ) -> None:
        timestamp = _now()
        entry: dict = {
            "phase": phase,
            "started_at": timestamp,
            "last_heartbeat_at": timestamp,
            "heartbeat_count": 0,
        }
        if agent:
            entry["agent"] = agent
        if scope:
            entry["scope"] = scope
        if message:
            entry["message"] = message
        if heartbeat_interval_seconds is not None:
            entry["heartbeat_interval_seconds"] = heartbeat_interval_seconds
        self.active_operation = entry

    def heartbeat_active_operation(self, message: str | None = None) -> dict | None:
        if not self.active_operation:
            return None
        current = dict(self.active_operation)
        current["last_heartbeat_at"] = _now()
        current["heartbeat_count"] = int(current.get("heartbeat_count", 0)) + 1
        if message:
            current["message"] = message
        self.active_operation = current
        return current

    def clear_active_operation(self) -> None:
        self.active_operation = None


# ---------------------------------------------------------------------------
# Abstract store
# ---------------------------------------------------------------------------


class StateStore:
    def internal_paths(self) -> list[Path]:
        return []

    def load(self, task_id: str) -> Optional[TaskState]:
        raise NotImplementedError

    def save(self, state: TaskState) -> None:
        raise NotImplementedError

    def list_tasks(self) -> list[str]:
        raise NotImplementedError

    def delete(self, task_id: str) -> None:
        raise NotImplementedError

    def save_text_snapshot(self, task_id: str, name: str, snapshot: dict[str, str | None]) -> None:
        return None

    def load_text_snapshot(self, task_id: str, name: str) -> dict[str, str | None] | None:
        return None

    def delete_text_snapshot(self, task_id: str, name: str) -> None:
        return None

    def delete_text_snapshots(self, task_id: str) -> None:
        return None

    def update_active_operation(self, task_id: str, active_operation: dict | None) -> None:
        raise NotImplementedError

    def create(
        self,
        task_description: str,
        *,
        delivery_plan_id: str | None = None,
        delivery_unit_id: str | None = None,
        delivery_plan_path: str | None = None,
        delivery_unit_budget: dict[str, int] | None = None,
        delivery_constraint_context_schema_version: int | None = None,
        delivery_source_task: dict[str, str] | None = None,
        delivery_inherited_constraints: list[dict] | None = None,
        delivery_constraint_context_fingerprint: str | None = None,
        delivery_write_scope_schema_version: int | None = None,
        delivery_write_scope_mode: str | None = None,
        delivery_declared_write_paths: list[str] | None = None,
        delivery_declared_write_exact_file_paths: list[str] | None = None,
        delivery_effective_write_paths: list[str] | None = None,
        delivery_effective_write_exact_file_paths: list[str] | None = None,
        delivery_handoff_schema_version: int | None = None,
        delivery_dependency_handoffs: list[dict] | None = None,
    ) -> TaskState:
        task_id = uuid.uuid4().hex
        state = TaskState(
            task_id=task_id,
            task_description=task_description,
            delivery_plan_id=delivery_plan_id,
            delivery_unit_id=delivery_unit_id,
            delivery_plan_path=delivery_plan_path,
            delivery_unit_budget=dict(delivery_unit_budget or {}),
            delivery_constraint_context_schema_version=delivery_constraint_context_schema_version,
            delivery_source_task=copy.deepcopy(delivery_source_task),
            delivery_inherited_constraints=copy.deepcopy(delivery_inherited_constraints or []),
            delivery_constraint_context_fingerprint=delivery_constraint_context_fingerprint,
            delivery_write_scope_schema_version=delivery_write_scope_schema_version,
            delivery_write_scope_mode=delivery_write_scope_mode,
            delivery_declared_write_paths=list(delivery_declared_write_paths or []),
            delivery_declared_write_exact_file_paths=(
                list(delivery_declared_write_exact_file_paths)
                if delivery_declared_write_exact_file_paths is not None
                else None
            ),
            delivery_effective_write_paths=list(delivery_effective_write_paths or []),
            delivery_effective_write_exact_file_paths=(
                list(delivery_effective_write_exact_file_paths)
                if delivery_effective_write_exact_file_paths is not None
                else None
            ),
            delivery_handoff_schema_version=delivery_handoff_schema_version,
            delivery_dependency_handoffs=copy.deepcopy(delivery_dependency_handoffs or []),
        )
        state.runtime_metadata = runtime_metadata_snapshot()
        self.save(state)
        return state


# ---------------------------------------------------------------------------
# JSON implementation
# ---------------------------------------------------------------------------


class JsonStateStore(StateStore):
    """Stores each task as a <task_id>.json file.

    Concurrent access through the same store instance is serialized. Running the same task_id from multiple Sikula
    processes concurrently is not supported.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._lock = threading.RLock()

    def internal_paths(self) -> list[Path]:
        return [self._dir]

    def _path(self, task_id: str) -> Path:
        if not _is_valid_state_task_id(task_id):
            raise ValueError(f"Invalid task id: {task_id!r}")
        return self._dir / f"{task_id}.json"

    def _snapshot_dir(self, task_id: str) -> Path:
        if not _is_valid_state_task_id(task_id):
            raise ValueError(f"Invalid task id: {task_id!r}")
        return self._dir / "_snapshots" / task_id

    def _snapshot_path(self, task_id: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError(f"Invalid snapshot name: {name!r}")
        return self._snapshot_dir(task_id) / f"{name}.json"

    def _delete_text_snapshots_unlocked(self, task_id: str) -> None:
        shutil.rmtree(self._snapshot_dir(task_id), ignore_errors=True)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as tmp:
                tmp.write(json.dumps(data, indent=2))
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def load(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            try:
                p = self._path(task_id)
            except ValueError:
                return None
            if not p.exists():
                return None
            data = self._read_json(p)
            # --- schema migrations (run in version order before TaskState is constructed) ---
            # Migrate field renamed in refactor: gradle_synced → build_synced
            if "gradle_synced" in data and "build_synced" not in data:
                data["build_synced"] = data.pop("gradle_synced")
            original_schema_version = data.get("schema_version", 1)
            has_legacy_review_records = any(
                isinstance(record, dict) and "reviewer" in record for record in data.get("review_cycle_records", [])
            )
            has_legacy_security_records = any(
                isinstance(record, dict) and "reviewer" in record
                for record in data.get("security_review_cycle_records", [])
            )
            if (
                original_schema_version < 2
                or "security_review_cycle_records" not in data
                or has_legacy_review_records
                or has_legacy_security_records
            ):
                _migrate_review_cycle_records(data)
            if original_schema_version < SCHEMA_VERSION:
                data["schema_version"] = SCHEMA_VERSION
            # --- end migrations ---
            # Drop unknown fields (forward-compat with state files from older versions)
            known = {f.name for f in TaskState.__dataclass_fields__.values()}
            data = {k: v for k, v in data.items() if k in known}
            return TaskState(**data)

    def save(self, state: TaskState) -> None:
        with self._lock:
            terminal_for_audit = _is_terminal_for_audit(state)
            if terminal_for_audit and not state.finished_at:
                state.finished_at = _now()
            if terminal_for_audit:
                state.final_summary = _final_summary(state)
            else:
                state.finished_at = None
                state.final_summary = {}
            state.updated_at = _now()
            self._write_json(self._path(state.task_id), asdict(state))

    def list_tasks(self) -> list[str]:
        with self._lock:
            if not self._dir.exists():
                return []
            return [p.stem for p in sorted(self._dir.glob("*.json"))]

    def delete(self, task_id: str) -> None:
        if not _is_valid_state_task_id(task_id):
            return
        with self._lock:
            self._path(task_id).unlink(missing_ok=True)
            self._delete_text_snapshots_unlocked(task_id)

    def save_text_snapshot(self, task_id: str, name: str, snapshot: dict[str, str | None]) -> None:
        if not _is_valid_state_task_id(task_id):
            return
        with self._lock:
            data = {str(path): content if content is None else str(content) for path, content in snapshot.items()}
            self._write_json(self._snapshot_path(task_id, name), {"snapshot": data})

    def load_text_snapshot(self, task_id: str, name: str) -> dict[str, str | None] | None:
        if not _is_valid_state_task_id(task_id):
            return None
        with self._lock:
            path = self._snapshot_path(task_id, name)
            if not path.exists():
                return None
            data = self._read_json(path)
            snapshot = data.get("snapshot")
            if not isinstance(snapshot, dict):
                return None
            return {str(key): value if value is None else str(value) for key, value in snapshot.items()}

    def delete_text_snapshot(self, task_id: str, name: str) -> None:
        if not _is_valid_state_task_id(task_id):
            return
        with self._lock:
            self._snapshot_path(task_id, name).unlink(missing_ok=True)

    def delete_text_snapshots(self, task_id: str) -> None:
        if not _is_valid_state_task_id(task_id):
            return
        with self._lock:
            self._delete_text_snapshots_unlocked(task_id)

    def update_active_operation(self, task_id: str, active_operation: dict | None) -> None:
        if not _is_valid_state_task_id(task_id):
            return
        with self._lock:
            path = self._path(task_id)
            if not path.exists():
                return
            data = self._read_json(path)
            data["active_operation"] = active_operation
            data["updated_at"] = _now()
            self._write_json(path, data)
