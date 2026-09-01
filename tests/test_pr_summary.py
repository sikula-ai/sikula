"""Tests for the privacy-safe PR-ready task summary projection."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core.pr_summary import (
    PrReadySummaryError,
    build_pr_ready_summary,
)
from core.state import TaskState


def _completed_state() -> TaskState:
    state = TaskState(
        task_id="abc123",
        task_description="# Private task\n\nDo not expose this body.",
        done=True,
        worktree_branch="sikula/pr-summary-abc123",
        result_commit="a" * 40,
    )
    state.config_snapshot = {
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "run_review": True,
        "run_security_review": True,
    }
    state.implementation_contract = {
        "source": {
            "path": ".sikula/contracts/pr-summary.contract.md",
            "sha256": "sha256:" + "b" * 64,
        },
        "status": "ready",
        "readiness_score": 96,
        "sections_detected": {"reviewer_focus": True},
    }
    state.files_changed = ["core/pr_summary.py", "tests/test_pr_summary.py"]
    state.build_status = "success"
    state.test_status = "success"
    state.check_status = "success"
    state.build_iterations = 2
    state.review_approved = True
    state.security_approved = True
    state.review_cycle_records = [{"approved": True, "has_warnings": False}]
    state.security_review_cycle_records = [{"approved": True, "has_warnings": False}]
    state.validation_cycle_records = [
        {"phase": "test", "status": "failed"},
        {"phase": "test", "status": "success"},
    ]
    state.fix_cycle_records = [{"outcome": "fixed"}]
    state.history = [{"action": "llm_retry"}]
    return state


def _current_branch_review_fix_state() -> TaskState:
    state = _completed_state()
    state.review_mode = "review_fix"
    state.review_base_branch = "main"
    state.review_delivery_mode = "current_branch"
    state.review_delivery_status = "delivered"
    state.review_target_branch = "feature/review-target"
    state.review_target_start_commit = "b" * 40
    state.review_isolated_fix_commit = "c" * 40
    state.result_commit = "c" * 40
    return state


def test_build_pr_ready_summary_projects_completed_state_without_mutation() -> None:
    state = _completed_state()
    before = deepcopy(state)

    markdown = build_pr_ready_summary(state)

    assert state == before
    assert markdown.startswith("## Summary\n")
    assert "## Contract" in markdown
    assert "## Validation" in markdown
    assert "## Reviews" in markdown
    assert "## Testability Gaps" in markdown
    assert "## Files Touched During Run" in markdown
    assert "may include changes reverted before completion" in markdown
    assert "## Reviewer Focus" in markdown
    assert "## Residual Risks" in markdown
    assert "- Build: Passed" in markdown
    assert "- Tests: Passed" in markdown
    assert "- Checks: Passed" in markdown
    assert "- Earlier failed validation records: 1" in markdown
    assert "- Code review: Approved" in markdown
    assert "- Security review: Approved" in markdown
    assert "`core/pr_summary.py`" in markdown
    assert "`tests/test_pr_summary.py`" in markdown
    assert ".sikula/contracts/pr-summary.contract.md" in markdown
    assert "Private task" not in markdown


def test_build_pr_ready_summary_omits_raw_audit_content_and_unsafe_paths() -> None:
    state = _completed_state()
    canary = "PRIVATE_CANARY_VALUE"
    state.task_description = canary
    state.analyst_prompt = f"prompt {canary}"
    state.implementation_prompt = f"implementation {canary}"
    state.review_cycle_records = [
        {
            "approved": True,
            "has_warnings": True,
            "reviewer_prompt": f"review prompt {canary}",
            "reviewer_output": f"warning details {canary}",
        }
    ]
    state.security_review_cycle_records = [
        {
            "approved": True,
            "has_warnings": True,
            "reviewer_output": f"security details {canary}",
        }
    ]
    state.validation_cycle_records = [
        {
            "phase": "test",
            "status": "failed",
            "error_excerpt": f"/Users/alice/private.py: {canary}",
        }
    ]
    state.testability_gaps = [
        {
            "target": f"private target {canary}",
            "reason": f"private reason {canary}",
            "risk": "high",
        }
    ]
    state.errors = [f"build error {canary}"]
    state.files_changed = [
        "safe/file.py",
        "safe/file.py",
        "docs/name`with-tick.md",
        "/Users/alice/private.py",
        "../outside.py",
        r"C:\private\file.py",
        "https://example.invalid/private.py",
        "safe/visual\u202espoof.py",
    ]
    state.implementation_contract["source"]["path"] = "/Users/alice/private.contract.md"

    markdown = build_pr_ready_summary(state)

    assert canary not in markdown
    assert "/Users/alice" not in markdown
    assert "../outside.py" not in markdown
    assert r"C:\private" not in markdown
    assert "- Branch: `sikula/pr-summary-abc123`" in markdown
    assert "- Source: Not recorded" in markdown
    assert "`safe/file.py`" in markdown
    assert "``docs/name`with-tick.md``" in markdown
    assert "5 unsafe or non-project-relative path(s) omitted" in markdown
    assert "- Recorded gaps: 1" in markdown
    assert "- Risk labels: high x1" in markdown
    assert "- Remaining build errors: 1" in markdown
    assert "- Code-review warning rounds: 1" in markdown
    assert "- Security-review warning rounds: 1" in markdown


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda state: setattr(state, "done", False), "pr_summary.task_not_successful"),
        (lambda state: setattr(state, "failed", True), "pr_summary.task_not_successful"),
        (lambda state: setattr(state, "delivery_plan_id", "plan-1"), "pr_summary.delivery_unit_unsupported"),
        (lambda state: setattr(state, "delivery_plan_id", []), "pr_summary.delivery_unit_unsupported"),
        (lambda state: setattr(state, "delivery_unit_id", "unit-1"), "pr_summary.delivery_unit_unsupported"),
        (lambda state: setattr(state, "review_mode", "review_report"), "pr_summary.review_report_unsupported"),
        (lambda state: setattr(state, "review_mode", "review-report"), "pr_summary.review_mode_invalid"),
        (lambda state: setattr(state, "review_mode", []), "pr_summary.review_mode_invalid"),
        (lambda state: setattr(state, "task_id", "/private/task"), "pr_summary.task_identity_invalid"),
    ],
)
def test_build_pr_ready_summary_rejects_non_publishable_state(mutate, code: str) -> None:
    state = _completed_state()
    mutate(state)

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == code


def test_build_pr_ready_summary_rejects_incomplete_current_branch_delivery() -> None:
    state = _completed_state()
    state.review_mode = "review_fix"
    state.review_delivery_mode = "current_branch"
    state.review_delivery_status = "pending"

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.delivery_incomplete"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: setattr(state, "review_delivery_mode", "current_branch"),
        lambda state: (
            setattr(state, "review_mode", "review_fix"),
            setattr(state, "review_delivery_mode", "future_mode"),
        ),
        lambda state: (
            setattr(state, "review_mode", "review_fix"),
            setattr(state, "review_delivery_mode", []),
        ),
        lambda state: (
            setattr(state, "review_mode", "review_fix"),
            setattr(state, "review_delivery_status", "delivered"),
        ),
        lambda state: (
            setattr(state, "review_mode", "review_fix"),
            setattr(state, "review_base_branch", "main"),
            setattr(state, "review_isolated_fix_commit", "c" * 40),
        ),
        lambda state: (
            setattr(state, "review_mode", "review_fix"),
            setattr(state, "review_base_branch", "main"),
            setattr(state, "review_target_branch", "feature/review-target"),
        ),
    ],
)
def test_build_pr_ready_summary_rejects_inconsistent_review_discriminators(mutate) -> None:
    state = _completed_state()
    mutate(state)

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.review_mode_invalid"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("review_base_branch", "main"),
        ("review_target_branch", "feature/review-target"),
        ("review_target_start_commit", "b" * 40),
        ("review_isolated_fix_commit", "c" * 40),
    ],
)
def test_build_pr_ready_summary_rejects_review_identity_on_standalone_state(field_name: str, value: str) -> None:
    state = _completed_state()
    setattr(state, field_name, value)

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.review_mode_invalid"


def test_build_pr_ready_summary_does_not_use_review_identity_fallbacks_for_standalone_state() -> None:
    state = _completed_state()
    state.worktree_branch = None
    state.result_commit = None
    state.review_target_branch = "feature/review-target"
    state.review_isolated_fix_commit = "c" * 40

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.review_mode_invalid"


def test_build_pr_ready_summary_rejects_preserved_worktree() -> None:
    state = _completed_state()
    state.worktree_path = "/private/worktree/project"
    state.worktree_base = "/private/worktree"

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_accepts_absent_legacy_review_fix_worktree(tmp_path) -> None:
    state = _completed_state()
    state.review_mode = "review_fix"
    state.review_base_branch = "main"
    state.worktree_path = str(tmp_path / "removed-worktree" / "project")
    state.worktree_base = str(tmp_path / "removed-worktree")

    markdown = build_pr_ready_summary(state)

    assert "## Summary" in markdown
    assert str(tmp_path) not in markdown


@pytest.mark.parametrize("path_kind", ["existing", "relative"])
def test_build_pr_ready_summary_rejects_untrusted_legacy_review_fix_worktree(tmp_path, path_kind: str) -> None:
    state = _completed_state()
    state.review_mode = "review_fix"
    state.review_base_branch = "main"
    if path_kind == "relative":
        state.worktree_path = "removed-worktree/project"
    else:
        candidate = tmp_path / "target"
        candidate.mkdir()
        state.worktree_path = str(candidate)
    state.worktree_base = None

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


@pytest.mark.parametrize("commit", [None, "abc1234", "a" * 39, "a" * 41, "not-a-commit"])
def test_build_pr_ready_summary_rejects_missing_or_malformed_publishable_commit(commit: object) -> None:
    state = _completed_state()
    state.result_commit = commit  # type: ignore[assignment]

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_accepts_finalized_isolated_commit() -> None:
    state = _completed_state()

    markdown = build_pr_ready_summary(state)

    assert "## Summary" in markdown


def test_build_pr_ready_summary_accepts_full_sha256_commit() -> None:
    state = _completed_state()
    state.result_commit = "b" * 64

    markdown = build_pr_ready_summary(state)

    assert f"- Commit: `{'b' * 64}`" in markdown


@pytest.mark.parametrize("branch", ["feature+tests", "release@candidate", "příliš/žluťoučký"])
def test_build_pr_ready_summary_accepts_valid_git_branch_names(branch: str) -> None:
    state = _completed_state()
    state.worktree_branch = branch

    markdown = build_pr_ready_summary(state)

    assert f"- Branch: `{branch}`" in markdown


def test_build_pr_ready_summary_rejects_no_isolate_result_without_commit() -> None:
    state = _completed_state()
    state.worktree_branch = None
    state.result_commit = None

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_rejects_explicit_cleanup_after_completion() -> None:
    state = _completed_state()
    state.history.append({"action": "cleanup", "result": "worktree removed"})

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: setattr(state, "worktree_branch", None),
        lambda state: setattr(state, "worktree_branch", "/private/branch"),
        lambda state: setattr(state, "history", {"action": "cleanup"}),
    ],
)
def test_build_pr_ready_summary_rejects_untrusted_finalization_evidence(mutate) -> None:
    state = _completed_state()
    mutate(state)

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_uses_current_branch_delivery_identity() -> None:
    state = _current_branch_review_fix_state()
    state.worktree_branch = "/Users/alice/private-branch"
    state.review_approved = "yes"

    markdown = build_pr_ready_summary(state)

    assert "- Branch: `feature/review-target`" in markdown
    assert f"- Commit: `{'c' * 40}`" in markdown
    assert "- Code review: Not approved" in markdown


def test_build_pr_ready_summary_rejects_current_branch_cleanup_failure() -> None:
    state = _current_branch_review_fix_state()
    state.worktree_path = "/private/preserved-worktree/project"
    state.worktree_base = "/private/preserved-worktree"
    state.history.append({"action": "cleanup_failed", "result": "private cleanup details"})

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_surfaces_recovered_current_branch_cleanup_failure() -> None:
    state = _current_branch_review_fix_state()
    state.history.append({"action": "cleanup_failed", "result": "private cleanup details"})

    markdown = build_pr_ready_summary(state)

    assert "- Worktree cleanup failures: 1" in markdown
    assert "private cleanup details" not in markdown


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: setattr(state, "review_target_branch", None),
        lambda state: setattr(state, "review_target_start_commit", None),
        lambda state: setattr(state, "review_isolated_fix_commit", None),
        lambda state: setattr(state, "result_commit", "d" * 40),
    ],
)
def test_build_pr_ready_summary_rejects_incomplete_current_branch_identity(mutate) -> None:
    state = _current_branch_review_fix_state()
    mutate(state)

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.review_mode_invalid"


def test_build_pr_ready_summary_uses_final_invocation_phase_flags() -> None:
    state = _completed_state()
    state.config_snapshot = {
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "run_review": True,
        "run_security_review": True,
    }
    state.run_invocation_records = [
        {"config_snapshot": dict(state.config_snapshot)},
        {
            "config_snapshot": {
                "run_build": False,
                "run_tests": False,
                "run_checks": False,
                "run_review": False,
                "run_security_review": False,
            }
        },
        {"config_snapshot": "malformed"},
    ]
    state.build_status = "failed"
    state.test_status = "failed"
    state.check_status = "failed"

    markdown = build_pr_ready_summary(state)

    assert "- Build: Skipped" in markdown
    assert "- Tests: Skipped" in markdown
    assert "- Checks: Skipped" in markdown
    assert "- Code review: Skipped" in markdown
    assert "- Security review: Skipped" in markdown


def test_build_pr_ready_summary_counts_manifest_asset_warnings() -> None:
    state = _completed_state()
    state.implementation_asset_records = [
        {"path": "private-missing", "status": "missing", "kind": "reference"},
        {"path": "private-dirty", "status": "available", "git_status": "dirty", "kind": "reference"},
        {"path": "private-ambiguous", "status": "available", "kind": "ambiguous"},
        {"path": "private-delivery", "status": "available", "kind": "delivery"},
        {
            "path": "private-clean",
            "status": "available",
            "git_status": "tracked",
            "kind": "delivery",
            "source_license": "MIT",
        },
    ]

    markdown = build_pr_ready_summary(state)

    assert "- Implementation asset warnings: 4" in markdown
    assert "private-missing" not in markdown
    assert "private-delivery" not in markdown


def test_build_pr_ready_summary_ignores_non_scalar_asset_target_status() -> None:
    state = _completed_state()
    state.implementation_asset_target_records = [
        {"status": ["missing"]},
        {"status": {"private": "outside_project"}},
        {"status": "missing"},
    ]

    markdown = build_pr_ready_summary(state)

    assert "- Asset target warnings: 1" in markdown
    assert "outside_project" not in markdown


def test_build_pr_ready_summary_omits_unknown_contract_readiness_status() -> None:
    state = _completed_state()
    state.implementation_contract["status"] = "private_canary_token"

    markdown = build_pr_ready_summary(state)

    assert "private_canary_token" not in markdown
    assert "PRIVATE CANARY TOKEN" not in markdown
    assert "- Readiness score: 96/100" in markdown


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_build_pr_ready_summary_rejects_unicode_line_separators(separator: str) -> None:
    state = _completed_state()
    state.worktree_branch = f"feature{separator}private"

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.finalization_incomplete"


def test_build_pr_ready_summary_rejects_no_change_review_fix() -> None:
    state = _completed_state()
    state.review_mode = "review_fix"
    state.review_delivery_mode = "current_branch"
    state.review_delivery_status = "no_changes"
    state.result_commit = None
    state.review_isolated_fix_commit = None

    with pytest.raises(PrReadySummaryError) as exc_info:
        build_pr_ready_summary(state)

    assert exc_info.value.code == "pr_summary.no_publishable_changes"
