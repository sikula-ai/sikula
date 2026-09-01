"""Privacy-safe PR-ready Markdown projection for completed task state."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import unicodedata

from core.delivery_plan import is_valid_delivery_branch_name
from core.state import TaskState, implementation_asset_warning_count


_MAX_CHANGED_FILES = 100
_MAX_PATH_CHARS = 500
_MAX_METADATA_CHARS = 300
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CONTRACT_READINESS_LABELS = {
    "ready": "READY",
    "warn": "WARN",
    "weak": "WEAK",
    "not_ready": "NOT READY",
}


class PrReadySummaryError(ValueError):
    """Raised when task state cannot produce a PR-ready summary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_pr_ready_summary(state: TaskState) -> str:
    """Build deterministic public Markdown without exposing raw audit content."""

    _validate_summary_state(state)

    changed_files, omitted_paths = _safe_changed_files(state.files_changed)
    branch = _summary_branch(state)
    commit = _summary_commit(state)
    contract = _contract_identity(state)
    run_config = _final_run_config(state)
    validation_failures = _validation_failure_count(state.validation_cycle_records)
    review_warning_rounds = _warning_round_count(state.review_cycle_records)
    security_warning_rounds = _warning_round_count(state.security_review_cycle_records)
    testability_gaps = _dict_record_count(state.testability_gaps)
    residual_signals = _residual_signals(
        state,
        review_warning_rounds=review_warning_rounds,
        security_warning_rounds=security_warning_rounds,
        testability_gaps=testability_gaps,
    )

    lines = [
        "## Summary",
        "",
        (
            f"Sikula completed task {_markdown_code(state.task_id)} with "
            f"{len(changed_files)} safely projected touched file(s)."
        ),
        "",
        f"- Branch: {_markdown_code(branch) if branch else 'Not recorded'}",
        f"- Commit: {_markdown_code(commit) if commit else 'Not recorded'}",
        f"- Task state: {_markdown_code(state.task_id)}",
        "",
        "## Contract",
        "",
    ]
    lines.extend(_contract_lines(contract))
    lines.extend(
        [
            "",
            "## Validation",
            "",
            f"- Build: {_phase_status(state.build_status, _configured_flag(run_config, 'run_build'))}",
            f"- Tests: {_phase_status(state.test_status, _configured_flag(run_config, 'run_tests'))}",
            f"- Checks: {_phase_status(state.check_status, _configured_flag(run_config, 'run_checks'))}",
            f"- Build attempts: {_nonnegative_int(state.build_iterations)}",
            f"- Validation records: {_dict_record_count(state.validation_cycle_records)}",
            f"- Earlier failed validation records: {validation_failures}",
            f"- Fixer attempts: {_dict_record_count(state.fix_cycle_records)}",
            f"- LLM retries: {_history_action_count(state.history, 'llm_retry')}",
            "",
            "## Reviews",
            "",
            (
                "- Code review: "
                + _gate_status(
                    state.review_approved,
                    state.review_cycle_records,
                    _configured_flag(run_config, "run_review"),
                )
            ),
            f"- Code-review rounds: {_dict_record_count(state.review_cycle_records)}",
            f"- Code-review warning rounds: {review_warning_rounds}",
            (
                "- Security review: "
                + _gate_status(
                    state.security_approved,
                    state.security_review_cycle_records,
                    _configured_flag(run_config, "run_security_review"),
                )
            ),
            f"- Security-review rounds: {_dict_record_count(state.security_review_cycle_records)}",
            f"- Security-review warning rounds: {security_warning_rounds}",
            "",
            "## Testability Gaps",
            "",
        ]
    )
    lines.extend(_testability_gap_lines(state.testability_gaps))
    lines.extend(
        [
            "",
            "## Files Touched During Run",
            "",
            "Paths are cumulative task audit records and may include changes reverted before completion.",
            "",
        ]
    )
    lines.extend(_changed_file_lines(changed_files, omitted_paths))
    lines.extend(["", "## Reviewer Focus", ""])
    lines.extend(
        _reviewer_focus_lines(
            contract=contract,
            validation_failures=validation_failures,
            testability_gaps=testability_gaps,
            review_warning_rounds=review_warning_rounds,
            security_warning_rounds=security_warning_rounds,
        )
    )
    lines.extend(["", "## Residual Risks", ""])
    if residual_signals:
        lines.extend(f"- {label}: {count}" for label, count in residual_signals)
    else:
        lines.append("- No residual risk signals are present in this bounded state projection.")
    return "\n".join(lines).rstrip() + "\n"


def _validate_summary_state(state: TaskState) -> None:
    if not isinstance(state.task_id, str) or not _TASK_ID_RE.fullmatch(state.task_id):
        raise PrReadySummaryError(
            "pr_summary.task_identity_invalid",
            "Task state does not contain a safe public task identity.",
        )
    if state.delivery_plan_id is not None or state.delivery_unit_id is not None:
        raise PrReadySummaryError(
            "pr_summary.delivery_unit_unsupported",
            "Delivery-unit task state is not a publishable delivery-plan result.",
        )
    if state.review_mode == "review_report":
        raise PrReadySummaryError(
            "pr_summary.review_report_unsupported",
            "Report-only review state does not represent a completed implementation branch.",
        )
    if state.review_mode is not None and state.review_mode != "review_fix":
        raise PrReadySummaryError(
            "pr_summary.review_mode_invalid",
            "Task state contains an unknown review mode.",
        )
    if state.review_mode is None and any(
        value is not None
        for value in (
            state.review_base_branch,
            state.review_delivery_mode,
            state.review_delivery_status,
            state.review_isolated_fix_commit,
            state.review_target_branch,
            state.review_target_start_commit,
        )
    ):
        raise PrReadySummaryError(
            "pr_summary.review_mode_invalid",
            "Standalone task state contains review-delivery metadata.",
        )
    if state.done is not True or state.failed is not False:
        raise PrReadySummaryError(
            "pr_summary.task_not_successful",
            "PR-ready summary requires a successfully completed task.",
        )
    if state.review_mode == "review_fix":
        _validate_review_fix_state(state)
    if not _result_finalization_complete(state):
        raise PrReadySummaryError(
            "pr_summary.finalization_incomplete",
            "Task result has not completed its required worktree finalization.",
        )


def _validate_review_fix_state(state: TaskState) -> None:
    if state.review_delivery_mode is None:
        if any(
            value is not None
            for value in (
                state.review_delivery_status,
                state.review_isolated_fix_commit,
                state.review_target_branch,
                state.review_target_start_commit,
            )
        ):
            raise PrReadySummaryError(
                "pr_summary.review_mode_invalid",
                "Non-current-branch review-fix state contains current-branch delivery metadata.",
            )
        if _safe_git_ref(state.review_base_branch) is None:
            raise PrReadySummaryError(
                "pr_summary.review_mode_invalid",
                "Review-fix task state does not contain a valid base branch.",
            )
        return
    if state.review_delivery_mode != "current_branch":
        raise PrReadySummaryError(
            "pr_summary.review_mode_invalid",
            "Review-fix task state contains an unknown delivery mode.",
        )
    if state.review_delivery_status != "delivered":
        code = (
            "pr_summary.no_publishable_changes"
            if state.review_delivery_status == "no_changes"
            else "pr_summary.delivery_incomplete"
        )
        raise PrReadySummaryError(
            code,
            "Current-branch review-fix does not contain a delivered implementation commit.",
        )
    target_branch = _safe_git_ref(state.review_target_branch)
    base_branch = _safe_git_ref(state.review_base_branch)
    target_start_commit = _safe_commit(state.review_target_start_commit)
    isolated_commit = _safe_commit(state.review_isolated_fix_commit)
    result_commit = _safe_commit(state.result_commit)
    if (
        base_branch is None
        or target_branch is None
        or target_start_commit is None
        or isolated_commit is None
        or result_commit != isolated_commit
    ):
        raise PrReadySummaryError(
            "pr_summary.review_mode_invalid",
            "Current-branch review-fix state contains incomplete or inconsistent delivery identity.",
        )


def _result_finalization_complete(state: TaskState) -> bool:
    if _worktree_paths_block_summary(state):
        return False
    if not isinstance(state.history, list):
        return False
    if _history_action_count(state.history, "cleanup"):
        return False
    branch = _summary_branch(state)
    return branch is not None and _summary_commit(state) is not None


def _worktree_paths_block_summary(state: TaskState) -> bool:
    values = (state.worktree_path, state.worktree_base)
    if all(value is None for value in values):
        return False
    if state.review_mode != "review_fix" or state.review_delivery_mode is not None:
        return True
    return any(value is not None and not _legacy_worktree_path_is_absent(value) for value in values)


def _legacy_worktree_path_is_absent(value: object) -> bool:
    if not isinstance(value, str) or not value or _has_control_character(value):
        return False
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _contract_identity(state: TaskState) -> dict[str, object]:
    snapshot = state.implementation_contract if isinstance(state.implementation_contract, dict) else {}
    source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    path = _safe_project_relative_path(source.get("path"))
    digest = _safe_sha256(source.get("sha256"))
    status = _safe_contract_status(snapshot.get("status"))
    score = snapshot.get("readiness_score")
    if type(score) is not int or not 0 <= score <= 100:
        score = None
    sections = snapshot.get("sections_detected")
    reviewer_focus = bool(isinstance(sections, dict) and sections.get("reviewer_focus") is True)
    return {
        "path": path,
        "sha256": digest,
        "status": status,
        "score": score,
        "reviewer_focus": reviewer_focus,
    }


def _contract_lines(contract: dict[str, object]) -> list[str]:
    path = contract["path"]
    digest = contract["sha256"]
    status = contract["status"]
    score = contract["score"]
    lines = [f"- Source: {_markdown_code(path) if isinstance(path, str) else 'Not recorded'}"]
    lines.append(f"- SHA-256: {_markdown_code(digest) if isinstance(digest, str) else 'Not recorded'}")
    if isinstance(status, str) and isinstance(score, int):
        lines.append(f"- Readiness: {status} ({score}/100)")
    elif isinstance(status, str):
        lines.append(f"- Readiness: {status}")
    elif isinstance(score, int):
        lines.append(f"- Readiness score: {score}/100")
    else:
        lines.append("- Readiness: Not recorded")
    return lines


def _final_run_config(state: TaskState) -> dict:
    if isinstance(state.run_invocation_records, list):
        for record in reversed(state.run_invocation_records):
            if not isinstance(record, dict):
                continue
            snapshot = record.get("config_snapshot")
            if isinstance(snapshot, dict):
                return snapshot
    return state.config_snapshot if isinstance(state.config_snapshot, dict) else {}


def _configured_flag(config: dict, key: str) -> bool | None:
    value = config.get(key)
    return value if type(value) is bool else None


def _phase_status(value: object, enabled: bool | None) -> str:
    if enabled is False:
        return "Skipped"
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "passed", "pass"}:
        return "Passed"
    if normalized == "failed":
        return "Failed"
    if normalized in {"skipped", "disabled"}:
        return "Skipped"
    return "Not recorded"


def _gate_status(approved: bool, records: object, enabled: bool | None) -> str:
    if enabled is False:
        return "Skipped"
    if approved is True:
        return "Approved"
    if _dict_record_count(records):
        return "Not approved"
    return "Not recorded"


def _validation_failure_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, dict) and record.get("status") == "failed")


def _warning_round_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, dict) and record.get("has_warnings") is True)


def _dict_record_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, dict))


def _history_action_count(records: object, action: str) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, dict) and record.get("action") == action)


def _testability_gap_lines(records: object) -> list[str]:
    if not isinstance(records, list):
        return ["- No testability gaps recorded."]
    gaps = [record for record in records if isinstance(record, dict)]
    if not gaps:
        return ["- No testability gaps recorded."]
    risk_counts: dict[str, int] = {}
    for gap in gaps:
        risk = str(gap.get("risk") or "").strip().lower()
        if risk not in {"low", "medium", "high", "critical"}:
            risk = "unspecified"
        risk_counts[risk] = risk_counts.get(risk, 0) + 1
    risk_summary = ", ".join(f"{risk} x{count}" for risk, count in sorted(risk_counts.items()))
    return [
        f"- Recorded gaps: {len(gaps)}",
        f"- Risk labels: {risk_summary}",
        "- Raw gap text remains in local task state and is not copied into this public projection.",
    ]


def _safe_changed_files(values: object) -> tuple[list[str], int]:
    if not isinstance(values, list):
        return [], 0
    safe: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for value in values:
        path = _safe_project_relative_path(value)
        if path is None:
            omitted += 1
            continue
        if path in seen:
            continue
        seen.add(path)
        safe.append(path)
    return sorted(safe), omitted


def _changed_file_lines(changed_files: list[str], omitted_paths: int) -> list[str]:
    lines = [f"- {_markdown_code(path)}" for path in changed_files[:_MAX_CHANGED_FILES]]
    remaining = len(changed_files) - _MAX_CHANGED_FILES
    if remaining > 0:
        lines.append(f"- {remaining} additional safe path(s) omitted for brevity.")
    if omitted_paths:
        lines.append(f"- {omitted_paths} unsafe or non-project-relative path(s) omitted.")
    if not lines:
        lines.append("- No touched files recorded.")
    return lines


def _reviewer_focus_lines(
    *,
    contract: dict[str, object],
    validation_failures: int,
    testability_gaps: int,
    review_warning_rounds: int,
    security_warning_rounds: int,
) -> list[str]:
    path = contract["path"]
    if contract["reviewer_focus"] is True and isinstance(path, str):
        lines = [f"- Follow the Reviewer focus section in {_markdown_code(path)}."]
    elif contract["reviewer_focus"] is True:
        lines = ["- Follow the Reviewer focus section in the implementation contract."]
    else:
        lines = ["- Verify the changed behavior against the implementation contract and acceptance criteria."]
    if validation_failures:
        lines.append(
            f"- Pay particular attention to areas involved in {validation_failures} earlier validation failure(s)."
        )
    if testability_gaps:
        lines.append(f"- Confirm the disposition of {testability_gaps} recorded testability gap(s).")
    if review_warning_rounds or security_warning_rounds:
        lines.append(
            "- Review the warning-bearing review rounds summarized above before merge "
            f"({review_warning_rounds} code, {security_warning_rounds} security)."
        )
    return lines


def _residual_signals(
    state: TaskState,
    *,
    review_warning_rounds: int,
    security_warning_rounds: int,
    testability_gaps: int,
) -> list[tuple[str, int]]:
    signals = [
        ("Testability gaps", testability_gaps),
        ("Code-review warning rounds", review_warning_rounds),
        ("Security-review warning rounds", security_warning_rounds),
        ("Analyst warnings", _text_record_count(state.analyst_warnings)),
        ("Open review issues", _text_record_count(state.review_issues)),
        ("Remaining build errors", _text_record_count(state.errors)),
        ("Remaining test errors", _text_record_count(state.test_errors)),
        ("Remaining check errors", _text_record_count(state.check_errors)),
        ("Write-scope warnings", _history_action_count(state.history, "write_path_warning")),
        ("Worktree cleanup failures", _history_action_count(state.history, "cleanup_failed")),
        ("Implementation asset warnings", implementation_asset_warning_count(state.implementation_asset_records)),
        ("Asset drift records", _dict_record_count(state.implementation_asset_drift_records)),
        ("Asset target warnings", _asset_target_warning_count(state.implementation_asset_target_records)),
        ("Active test execution gate audits", _active_audit_count(state.test_execution_gate_records)),
        ("Active synthetic harness audits", _active_audit_count(state.synthetic_test_harness_records)),
    ]
    return [(label, count) for label, count in signals if count > 0]


def _text_record_count(values: object) -> int:
    if not isinstance(values, list):
        return 0
    return sum(1 for value in values if isinstance(value, str) and value.strip())


def _asset_target_warning_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(
        1
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("status"), str)
        and record.get("status") in {"missing", "outside_project"}
    )


def _active_audit_count(records: object) -> int:
    if not isinstance(records, list):
        return 0
    return sum(1 for record in records if isinstance(record, dict) and record.get("status") != "resolved")


def _safe_project_relative_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or len(path) > _MAX_PATH_CHARS or _has_control_character(path):
        return None
    if path.startswith(("/", "\\", "~")) or "\\" in path or ":" in path or _WINDOWS_ABSOLUTE_RE.match(path):
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    return pure.as_posix()


def _safe_git_ref(value: object) -> str | None:
    text = _bounded_metadata(value)
    if text is None or not is_valid_delivery_branch_name(text):
        return None
    return text


def _safe_commit(value: object) -> str | None:
    text = _bounded_metadata(value)
    return text.lower() if text and _COMMIT_RE.fullmatch(text) else None


def _summary_branch(state: TaskState) -> str | None:
    if state.review_mode == "review_fix" and state.review_delivery_mode == "current_branch":
        return _safe_git_ref(state.review_target_branch)
    return _safe_git_ref(state.worktree_branch)


def _summary_commit(state: TaskState) -> str | None:
    return _safe_commit(state.result_commit)


def _safe_sha256(value: object) -> str | None:
    text = _bounded_metadata(value)
    return text.lower() if text and _SHA256_RE.fullmatch(text) else None


def _safe_contract_status(value: object) -> str | None:
    return _CONTRACT_READINESS_LABELS.get(value) if isinstance(value, str) else None


def _bounded_metadata(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_METADATA_CHARS or _has_control_character(text):
        return None
    return text


def _nonnegative_int(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def _has_control_character(value: str) -> bool:
    return value.splitlines() != [value] or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def _markdown_code(value: str) -> str:
    longest_run = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    delimiter = "`" * (longest_run + 1)
    content = f" {value} " if value.startswith("`") or value.endswith("`") else value
    return f"{delimiter}{content}{delimiter}"
