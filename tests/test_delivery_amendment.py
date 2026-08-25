from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from unittest.mock import patch

import pytest
import yaml

import core.delivery_amendment as delivery_amendment_module
import core.worktree as worktree_module
import sikula
from core.delivery_amendment import (
    DeliveryAmendmentApplyResult,
    DeliveryAmendmentDependencyIdentity,
    DeliveryAmendmentError,
    DeliveryAmendmentFailureEvidence,
    DeliveryAmendmentProposal,
    DeliveryAmendmentReviewEvidence,
    apply_delivery_amendment,
    capture_delivery_amendment_failure_evidence,
    capture_delivery_amendment_source_snapshot,
    create_delivery_amendment_proposal,
    delivery_amendment_proposal_path,
    inspect_delivery_amendment_target,
    preview_delivery_amendment,
)
from core.delivery_constraint_context import delivery_constraint_context_fingerprint
from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAuthoringConstraintDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    DeliveryConstraintVerification,
)
from core.delivery_assembly import DeliveryArtifactAssemblyResult
from core.delivery_finalize import preview_delivery_finalize
from core.delivery_plan import (
    DeliveryBudgetExceeded,
    DeliveryPlanIssue,
    check_delivery_plan_file,
    delivery_unit_constraint_context,
)
from core.delivery_progress import (
    DeliveryProgressLockError,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    render_delivery_status,
)
from core.delivery_run_next import preview_delivery_run_next
from core.state import JsonStateStore, StateStore, TaskState
from core.delivery_write_scope import SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION
from core.delivery_unit_metadata import DeliveryUnitBudget
from sikula_cli.delivery import (
    DeliveryChildRunResult,
    DeliveryAmendPrepareContext,
    DeliveryAmendPrepareResult,
    DeliveryRunNextContext,
    _bind_authoritative_amendment_metadata,
    cmd_delivery_amend_apply,
    cmd_delivery_amend_prepare,
    cmd_delivery_run_next,
    register_parser,
    render_delivery_amend_apply,
    render_delivery_amend_prepare,
)


def _amend_context(root: Path, author: Callable[..., DeliveryAmendmentAuthoringDraft]) -> DeliveryAmendPrepareContext:
    return DeliveryAmendPrepareContext(author, JsonStateStore(root / ".sikula" / "state"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
def test_amendment_evidence_preserves_posix_backslash_filename(tmp_path: Path) -> None:
    physical_name = r"scripts\payload"
    (tmp_path / physical_name).write_text("outside lexical scripts directory\n", encoding="utf-8")

    assert delivery_amendment_module._safe_delivery_amendment_path(physical_name, root=tmp_path) == physical_name
    assert delivery_amendment_module._safe_delivery_amendment_worktree_path(physical_name) == physical_name


def test_automatic_amendment_metadata_is_bound_deterministically() -> None:
    draft = replace(_draft(), amend_reason=None, budget_exceeded=None)
    setattr(draft, "audit_path", ".sikula/contract-reports/amend.auto-llm.jsonl")
    budget = DeliveryBudgetExceeded(name="max_planner_steps", limit=1, actual=3)

    bound = _bind_authoritative_amendment_metadata(
        draft,
        amend_reason="unit_budget_exceeded",
        budget_exceeded=budget,
    )

    assert bound.amend_reason == "unit_budget_exceeded"
    assert bound.budget_exceeded == budget.to_dict()
    assert bound.audit_path == ".sikula/contract-reports/amend.auto-llm.jsonl"


def test_automatic_amendment_metadata_rejects_model_conflict() -> None:
    budget = DeliveryBudgetExceeded(name="max_planner_steps", limit=1, actual=3)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        _bind_authoritative_amendment_metadata(
            _draft(),
            amend_reason="unit_budget_exceeded",
            budget_exceeded=budget,
        )

    assert exc_info.value.issue.code == "delivery_amend.authoring_recovery_metadata_mismatch"


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _git_with_identity(*args: str) -> list[str]:
    return [
        "git",
        "-c",
        "user.name=Sikula Test",
        "-c",
        "user.email=sikula@example.test",
        *args,
    ]


def _commit_content_id(root: Path, commit: str, path: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{commit}:{path}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _filtered_content_id(root: Path, path: str, content: bytes) -> str:
    return (
        subprocess.run(
            ["git", "hash-object", f"--path={path}", "--stdin"],
            cwd=root,
            check=True,
            input=content,
            capture_output=True,
        )
        .stdout.decode("ascii")
        .strip()
    )


def _commit(root: Path, name: str, body: str) -> str:
    path = root / name
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit",
            "-m",
            name,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _task_markdown(title: str) -> str:
    return f"""# {title}

## Goal

Deliver one smaller behavior.

## Current behavior

The behavior is part of an oversized unit.

## Desired behavior

The behavior is delivered independently.

## Acceptance criteria

- The focused behavior is implemented and validated.

## Security and privacy

- Do not expose prompts, source excerpts, or child task state.

## Reviewer focus

- Verify the focused behavior boundary.

## Out of scope

- Do not implement the other replacement units.

## Validation

- `python3 -m pytest tests/test_delivery_amendment.py`
"""


def _publish_amendment_locally(
    plan_path: Path,
    project_root: Path,
    proposal: DeliveryAmendmentProposal,
    *,
    omit_unit_id: str | None = None,
    changed_unit_id: str | None = None,
) -> None:
    context = inspect_delivery_amendment_target(plan_path, proposal.target_unit_id, project_root=project_root)
    amended_plan, _ = delivery_amendment_module._amended_plan_data(plan_path, context, proposal)
    plan_path.write_bytes(yaml.safe_dump(amended_plan, sort_keys=False, allow_unicode=True).encode("utf-8"))
    for unit in proposal.replacement_units:
        if unit.id == omit_unit_id:
            continue
        path = project_root / unit.task_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "changed replacement\n" if unit.id == changed_unit_id else unit.task_markdown
        path.write_bytes(content.encode("utf-8"))


def _project_config(root: Path) -> dict:
    return {
        "project": {"build_tool": "python", "root_path": str(root)},
        "run_tests": True,
        "build": {"test_command": "python3 -m pytest tests/test_delivery_amendment.py"},
    }


def _write_plan(root: Path) -> Path:
    units_dir = root / ".sikula" / "delivery" / "demo" / "units"
    units_dir.mkdir(parents=True)
    units = []
    for unit_id, dependencies in (("a", []), ("b", ["a"]), ("c", ["b"]), ("d", ["c"])):
        task_path = units_dir / f"{unit_id}.md"
        task_path.write_text(_task_markdown(unit_id.upper()), encoding="utf-8")
        units.append(
            {
                "id": unit_id,
                "title": unit_id.upper(),
                "task_path": task_path.relative_to(root).as_posix(),
                "depends_on": dependencies,
                "stream": "core",
                "platform": "python",
                "phase": "implementation",
                "kind": "feature",
            }
        )
    plan_path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plan_id": "amend-demo",
                "title": "Amend demo",
                "final_branch": "sikula/delivery/amend-demo",
                "streams": ["core"],
                "units": units,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return plan_path


def _add_plan_component(plan_path: Path, component_id: str) -> None:
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["components"] = [
        {
            "id": component_id,
            "path": "src",
            "stream": "core",
        }
    ]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")


def _add_target_constraint(root: Path, plan_path: Path) -> None:
    source_task = root / ".sikula" / "tasks" / "source.md"
    source_task.parent.mkdir(parents=True, exist_ok=True)
    source_text = "# Source task\n\nPreserve the protocol authority boundary.\n"
    source_task.write_text(source_text, encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["source_task"] = {
        "path": source_task.relative_to(root).as_posix(),
        "sha256": f"sha256:{hashlib.sha256(source_text.encode('utf-8')).hexdigest()}",
    }
    plan["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Protocol changes remain owned by the external protocol project.",
            "unit_ids": ["a", "c", "d"],
            "disposition": "preserved",
        },
        {
            "id": "foundation-boundary",
            "kind": "security_boundary",
            "summary": "Foundation behavior remains independently constrained.",
            "unit_ids": ["b"],
            "disposition": "preserved",
        },
    ]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")


def _write_progress(root: Path, first_commit: str, second_commit: str, *, target_status: str = "pending") -> Path:
    target: dict[str, str] = {
        "unit_id": "c",
        "status": target_status,
        "updated_at": "2026-07-20T10:02:00Z",
    }
    if target_status == "running":
        target.update(child_task_id="task-c", started_at="2026-07-20T10:02:00Z")
    elif target_status == "failed":
        target.update(
            child_task_id="task-c",
            branch="sikula/task-c",
            failure_code="planner_failed",
            started_at="2026-07-20T10:02:00Z",
            completed_at="2026-07-20T10:03:00Z",
        )
    progress_path = delivery_progress_path(root, "amend-demo")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "amend-demo",
                "units": [
                    {
                        "unit_id": "a",
                        "status": "done",
                        "child_task_id": "task-a",
                        "branch": "sikula/a",
                        "commit": first_commit,
                        "started_at": "2026-07-20T10:00:00Z",
                        "completed_at": "2026-07-20T10:00:30Z",
                        "updated_at": "2026-07-20T10:00:30Z",
                    },
                    {
                        "unit_id": "b",
                        "status": "done",
                        "child_task_id": "task-b",
                        "branch": "sikula/b",
                        "commit": second_commit,
                        "started_at": "2026-07-20T10:01:00Z",
                        "completed_at": "2026-07-20T10:01:30Z",
                        "updated_at": "2026-07-20T10:01:30Z",
                    },
                    target,
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return progress_path


def _draft() -> DeliveryAmendmentAuthoringDraft:
    return DeliveryAmendmentAuthoringDraft(
        plan_id="amend-demo",
        target_unit_id="c",
        amend_reason="unit_budget_exceeded",
        budget_exceeded={"name": "max_planner_steps", "limit": 2, "actual": 5},
        replacement_units=[
            DeliveryAuthoringUnitDraft(
                "c-1",
                "C1",
                [],
                _task_markdown("C1"),
                scope_paths=["src"],
                estimated_size="small",
                risk_tags=["validation"],
                budget=DeliveryUnitBudget(max_planner_steps=1),
            ),
            DeliveryAuthoringUnitDraft("c-2", "C2", ["c-1"], _task_markdown("C2")),
            DeliveryAuthoringUnitDraft("c-3", "C3", ["c-1"], _task_markdown("C3")),
        ],
    )


def _setup(root: Path, *, target_status: str = "pending") -> tuple[Path, Path, Path]:
    _git_init(root)
    first_commit = _commit(root, "a.txt", "a\n")
    second_commit = _commit(root, "b.txt", "b\n")
    plan_path = _write_plan(root)
    progress_path = _write_progress(root, first_commit, second_commit, target_status=target_status)
    proposal_root = root / ".sikula" / "contract-reports"
    return plan_path, progress_path, proposal_root


def _write_terminal_child_state(
    root: Path,
    plan_path: Path,
    progress_path: Path,
    *,
    failure_code: str,
) -> TaskState:
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    target_progress = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
    target_progress["failure_code"] = failure_code
    progress_path.write_text(json.dumps(progress, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checked = check_delivery_plan_file(plan_path, project_root=root)
    assert checked.plan is not None
    schema_version, source_task, constraints = delivery_unit_constraint_context(checked.plan, "c")
    plan_relative = plan_path.relative_to(root).as_posix()
    disposition_by_code = {
        "scope_amendment_required": "requires_scope_amendment",
        "external_dependency_gap": "external_dependency_gap",
    }
    action_by_code = {
        "scope_amendment_required": "delivery_amend_prepare",
        "external_dependency_gap": "external_dependency_follow_up",
    }
    state = TaskState(
        task_id="task-c",
        task_description="PRIVATE SOURCE TASK BODY",
        delivery_plan_id="amend-demo",
        delivery_unit_id="c",
        delivery_plan_path=plan_relative,
        delivery_constraint_context_schema_version=schema_version,
        delivery_source_task=source_task,
        delivery_inherited_constraints=constraints,
        delivery_constraint_context_fingerprint=delivery_constraint_context_fingerprint(
            schema_version=schema_version,
            plan_id="amend-demo",
            unit_id="c",
            plan_path=plan_relative,
            source_task=source_task,
            constraints=constraints,
        ),
        delivery_write_scope_schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
        delivery_write_scope_mode="repository_default",
        delivery_declared_write_paths=[],
        delivery_declared_write_exact_file_paths=[],
        delivery_effective_write_paths=["."],
        delivery_effective_write_exact_file_paths=[],
    )
    state.failed = True
    state.done = False
    state.delivery_stop_code = failure_code
    state.files_changed = ["src/partial.py", str(root / "private" / "secret.py")]
    if disposition := disposition_by_code.get(failure_code):
        disposition_value = {
            "schema_version": 1,
            "disposition": disposition,
            "summary": "A required protocol change is owned outside the current unit boundary.",
            "recommended_action": action_by_code[failure_code],
        }
        state.delivery_stop_disposition = {
            **disposition_value,
            "source": "security_reviewer",
            "timestamp": "2026-07-20T10:03:00Z",
        }
        state.security_review_cycle_records = [
            {
                "approved": False,
                "reviewer": "security_reviewer",
                "disposition": disposition_value,
                "reviewer_output": "PRIVATE REVIEW OUTPUT",
            }
        ]
    JsonStateStore(root / ".sikula" / "state").save(state)
    return state


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_capture_amendment_failure_evidence_is_correlated_and_sanitized(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="external_dependency_gap",
    )
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.failure_code == "external_dependency_gap"
    assert evidence.recommended_action == "external_dependency_follow_up"
    assert evidence.requires_external_follow_up is True
    assert evidence.declared_write_paths == ()
    assert evidence.effective_write_paths == (".",)
    assert evidence.changed_count == 2
    assert evidence.changed_paths == ("src/partial.py",)
    assert evidence.omitted_changed_paths_count == 1
    assert evidence.security_reviewer.records_count == 1
    assert evidence.security_reviewer.dispositions[0]["disposition"] == "external_dependency_gap"


def test_capture_amendment_failure_evidence_ignores_prior_delivery_approval(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="external_dependency_gap",
    )
    state.security_review_cycle_records.insert(
        0,
        {
            "approved": True,
            "reviewer": "security_reviewer",
            "disposition": {
                "schema_version": 1,
                "disposition": "approved",
                "summary": "No blocking security issues were found in the earlier review.",
                "recommended_action": "continue",
            },
            "reviewer_output": "PRIVATE EARLIER REVIEW OUTPUT",
        },
    )
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.security_reviewer.records_count == 2
    assert evidence.security_reviewer.issues_count == 1
    assert [item["disposition"] for item in evidence.security_reviewer.dispositions] == ["external_dependency_gap"]


def test_capture_amendment_failure_evidence_uses_injected_state_store(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="external_dependency_gap",
    )
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    class InjectedStateStore(StateStore):
        def load(self, task_id: str) -> TaskState | None:
            return state if task_id == state.task_id else None

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=InjectedStateStore(),
    )

    assert evidence is not None
    assert evidence.child_task_id == state.task_id
    assert evidence.stop_disposition == {
        "schema_version": 1,
        "disposition": "external_dependency_gap",
        "summary": "A required protocol change is owned outside the current unit boundary.",
        "recommended_action": "external_dependency_follow_up",
        "source": "security_reviewer",
    }
    serialized = json.dumps(evidence.to_dict(), sort_keys=True)
    assert "PRIVATE SOURCE TASK BODY" not in serialized
    assert "PRIVATE REVIEW OUTPUT" not in serialized
    assert str(tmp_path) not in serialized
    assert "secret.py" not in serialized


def test_capture_amendment_failure_evidence_uses_runtime_narrowed_scope(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    state.delivery_runtime_write_scope_binding = {
        "schema_version": 1,
        "status": "bound",
        "roots": [{"path": "src", "resolved_path": "src", "exact_file": False}],
    }
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.declared_write_paths == ()
    assert evidence.effective_write_paths == ("src",)


def test_capture_amendment_failure_evidence_uses_child_worktree_for_all_scope_evidence(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    next(unit for unit in plan["units"] if unit["id"] == "c")["scope_paths"] = ["src/alias"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "operator-target").mkdir()
    (tmp_path / "src" / "alias").symlink_to("operator-target", target_is_directory=True)
    child_root = tmp_path / ".sikula" / "worktrees" / state.task_id / "project"
    (child_root / "src" / "alias").mkdir(parents=True)
    state.worktree_path = str(child_root)
    state.worktree_base = str(child_root.parent)
    state.delivery_write_scope_mode = "unit_explicit"
    state.delivery_declared_write_paths = ["src/alias"]
    state.delivery_effective_write_paths = ["src/alias"]
    state.delivery_runtime_write_scope_binding = {
        "schema_version": 1,
        "status": "bound",
        "roots": [{"path": "src/alias", "resolved_path": "src/alias", "exact_file": False}],
    }
    state.files_changed = ["src/alias/partial.py"]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.declared_write_paths == ("src/alias",)
    assert evidence.effective_write_paths == ("src/alias",)
    assert evidence.changed_paths == ("src/alias/partial.py",)


def test_capture_amendment_failure_evidence_preserves_scope_retarget_stop(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    next(unit for unit in plan["units"] if unit["id"] == "c")["scope_paths"] = ["src/alias"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    (tmp_path / "src" / "alias").mkdir(parents=True)
    child_root = tmp_path / ".sikula" / "worktrees" / state.task_id / "project"
    (child_root / "src" / "apps").mkdir(parents=True)
    (child_root / "src" / "alias").symlink_to("apps", target_is_directory=True)
    state.worktree_path = str(child_root)
    state.worktree_base = str(child_root.parent)
    state.delivery_write_scope_mode = "unit_explicit"
    state.delivery_declared_write_paths = ["src/alias"]
    state.delivery_effective_write_paths = ["src/alias"]
    state.delivery_runtime_write_scope_binding = {
        "schema_version": 1,
        "status": "denied",
        "roots": [],
    }
    state.files_changed = []
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.declared_write_paths == ("src/alias",)
    assert evidence.effective_write_paths == ("src/alias",)
    assert evidence.recommended_action == "delivery_amend_prepare"


def test_capture_amendment_failure_evidence_preserves_post_binding_scope_retarget(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    next(unit for unit in plan["units"] if unit["id"] == "c")["scope_paths"] = ["src/alias"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    child_root = tmp_path / ".sikula" / "worktrees" / state.task_id / "project"
    alias = child_root / "src" / "alias"
    alias.mkdir(parents=True)
    outside = tmp_path / "outside-child"
    outside.mkdir()
    alias.rmdir()
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    state.worktree_path = str(child_root)
    state.worktree_base = str(child_root.parent)
    state.delivery_write_scope_mode = "unit_explicit"
    state.delivery_declared_write_paths = ["src/alias"]
    state.delivery_effective_write_paths = ["src/alias"]
    state.delivery_runtime_write_scope_binding = {
        "schema_version": 1,
        "status": "bound",
        "roots": [{"path": "src/alias", "resolved_path": "src/alias", "exact_file": False}],
    }
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.declared_write_paths == ("src/alias",)
    assert evidence.effective_write_paths == ("src/alias",)
    assert evidence.recommended_action == "delivery_amend_prepare"


def test_capture_amendment_failure_evidence_rejects_child_correlation_mismatch(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    state.delivery_unit_id = "other-unit"
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        capture_delivery_amendment_failure_evidence(
            target,
            state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        )

    assert exc_info.value.issue.code == "delivery_amend.failure_evidence_mismatch"


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("constraint_context_invalid", "delivery_amend.failure_evidence_invalid"),
        ("constraint_context_absent", "delivery_amend.failure_evidence_invalid"),
        ("write_scope_invalid", "delivery_amend.failure_evidence_invalid"),
        ("write_scope_absent", "delivery_amend.failure_evidence_mismatch"),
        ("runtime_write_scope_invalid", "delivery_amend.failure_evidence_invalid"),
        ("handoffs_malformed", "delivery_amend.failure_evidence_invalid"),
        ("review_malformed", "delivery_amend.failure_evidence_invalid"),
        ("changed_files_malformed", "delivery_amend.failure_evidence_invalid"),
        ("validation_malformed", "delivery_amend.failure_evidence_invalid"),
        ("stop_disposition_invalid", "delivery_amend.failure_evidence_invalid"),
        ("stop_disposition_absent", "delivery_amend.failure_evidence_mismatch"),
    ],
)
def test_capture_amendment_failure_evidence_fails_closed_on_malformed_child_state(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="external_dependency_gap",
    )
    if case == "constraint_context_invalid":
        state.delivery_constraint_context_schema_version = 999
    elif case == "constraint_context_absent":
        state.delivery_constraint_context_schema_version = None
        state.delivery_source_task = None
        state.delivery_inherited_constraints = []
        state.delivery_constraint_context_fingerprint = None
    elif case == "write_scope_invalid":
        state.delivery_write_scope_schema_version = 999
    elif case == "write_scope_absent":
        state.delivery_write_scope_schema_version = None
        state.delivery_write_scope_mode = None
        state.delivery_declared_write_paths = []
        state.delivery_declared_write_exact_file_paths = None
        state.delivery_effective_write_paths = []
        state.delivery_effective_write_exact_file_paths = None
    elif case == "runtime_write_scope_invalid":
        state.delivery_runtime_write_scope_binding = {"schema_version": 999, "status": "bound", "roots": []}
    elif case == "handoffs_malformed":
        state.delivery_dependency_handoffs = None  # type: ignore[assignment]
    elif case == "review_malformed":
        state.review_cycle_records = None  # type: ignore[assignment]
    elif case == "changed_files_malformed":
        state.files_changed = None  # type: ignore[assignment]
    elif case == "validation_malformed":
        state.validation_cycle_records = None  # type: ignore[assignment]
    elif case == "stop_disposition_invalid":
        state.delivery_stop_disposition = {"disposition": "external_dependency_gap"}
    elif case == "stop_disposition_absent":
        state.delivery_stop_disposition = None
    state_path = tmp_path / ".sikula" / "state" / f"{state.task_id}.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    for field_name in (
        "delivery_constraint_context_schema_version",
        "delivery_source_task",
        "delivery_inherited_constraints",
        "delivery_constraint_context_fingerprint",
        "delivery_write_scope_schema_version",
        "delivery_write_scope_mode",
        "delivery_declared_write_paths",
        "delivery_declared_write_exact_file_paths",
        "delivery_effective_write_paths",
        "delivery_effective_write_exact_file_paths",
        "delivery_runtime_write_scope_binding",
        "delivery_dependency_handoffs",
        "review_cycle_records",
        "files_changed",
        "validation_cycle_records",
        "delivery_stop_disposition",
    ):
        persisted[field_name] = getattr(state, field_name)
    state_path.write_text(json.dumps(persisted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        capture_delivery_amendment_failure_evidence(
            target,
            state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        )

    assert exc_info.value.issue.code == expected_code


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("changed_count_bool", "delivery_amend.failure_evidence_invalid"),
        ("violation_count_negative", "delivery_amend.failure_evidence_invalid"),
        ("changed_paths_malformed", "delivery_amend.failure_evidence_invalid"),
        ("counts_inconsistent", "delivery_amend.failure_evidence_invalid"),
        ("outside_paths_malformed", "delivery_amend.failure_evidence_invalid"),
        ("outside_counts_inconsistent", "delivery_amend.failure_evidence_invalid"),
        ("total_violation_count_inconsistent", "delivery_amend.failure_evidence_invalid"),
    ],
)
def test_capture_amendment_scope_audit_rejects_invalid_counts_and_paths(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    metadata: dict[str, object] = {
        "code": "unit_scope_violation",
        "changed_count": 2,
        "violation_count": 1,
        "changed_paths": ["src/partial.py", "docs/escaped.md"],
        "violation_paths": ["docs/escaped.md"],
    }
    if case == "changed_count_bool":
        metadata["changed_count"] = True
    elif case == "violation_count_negative":
        metadata["violation_count"] = -1
    elif case == "changed_paths_malformed":
        metadata["changed_paths"] = "src/partial.py"
    elif case == "counts_inconsistent":
        metadata["violation_count"] = 0
    elif case == "outside_paths_malformed":
        metadata["outside_project_count"] = 1
        metadata["outside_project_paths"] = "shared/escaped.py"
    elif case == "outside_counts_inconsistent":
        metadata["outside_project_count"] = 0
        metadata["outside_project_paths"] = ["shared/escaped.py"]
    elif case == "total_violation_count_inconsistent":
        metadata["outside_project_count"] = 1
        metadata["outside_project_paths"] = ["shared/escaped.py"]
    state.validation_cycle_records = [
        {
            "phase": "delivery_scope_audit",
            "status": "failed",
            "metadata": metadata,
        }
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        capture_delivery_amendment_failure_evidence(
            target,
            state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        )

    assert exc_info.value.issue.code == expected_code


def test_capture_amendment_scope_violation_uses_bounded_audit_paths(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    state.validation_cycle_records = [
        {
            "phase": "delivery_scope_audit",
            "status": "failed",
            "metadata": {
                "code": "unit_scope_violation",
                "changed_count": 3,
                "violation_count": 1,
                "changed_paths": ["docs/escaped.md", "src/partial.py"],
                "violation_paths": ["docs/escaped.md"],
            },
        }
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.recommended_action == "delivery_amend_prepare"
    assert evidence.changed_count == 3
    assert evidence.changed_paths == ("docs/escaped.md", "src/partial.py")
    assert evidence.omitted_changed_paths_count == 1
    assert evidence.violation_count == 1
    assert evidence.violation_paths == ("docs/escaped.md",)


def test_capture_amendment_preserves_separate_outside_project_violation_paths(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    state.files_changed = ["src/partial.py"]
    state.validation_cycle_records = [
        {
            "phase": "delivery_scope_audit",
            "status": "failed",
            "metadata": {
                "code": "unit_scope_violation",
                "changed_count": 2,
                "changed_paths": ["src/partial.py"],
                "violation_count": 1,
                "violation_paths": [],
                "outside_project_count": 1,
                "outside_project_paths": ["shared/escaped.py"],
            },
        }
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.schema_version == 2
    assert evidence.changed_count == 2
    assert evidence.changed_paths == ("src/partial.py",)
    assert evidence.omitted_changed_paths_count == 1
    assert evidence.violation_count == 1
    assert evidence.violation_paths == ()
    assert evidence.outside_project_count == 1
    assert evidence.outside_project_paths == ("shared/escaped.py",)
    assert evidence.omitted_outside_project_paths_count == 0
    assert evidence.to_dict()["scope_violations"]["outside_project"] == {
        "count": 1,
        "paths": ["shared/escaped.py"],
        "omitted_paths_count": 0,
    }


def test_capture_amendment_bounds_and_sanitizes_outside_project_paths(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    state.files_changed = ["src/partial.py"]
    outside_values = [
        "shared/escaped.py",
        "shared/escaped.py",
        "../private.py",
        *(f"shared/generated-{index}.py" for index in range(101)),
    ]
    outside_count = len(outside_values)
    state.validation_cycle_records = [
        {
            "phase": "delivery_scope_audit",
            "status": "failed",
            "metadata": {
                "code": "unit_scope_violation",
                "changed_count": outside_count + 1,
                "changed_paths": ["src/partial.py"],
                "violation_count": outside_count,
                "violation_paths": [],
                "outside_project_count": outside_count,
                "outside_project_paths": outside_values,
            },
        }
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.outside_project_count == outside_count
    assert len(evidence.outside_project_paths) == delivery_amendment_module._MAX_DELIVERY_AMENDMENT_EVIDENCE_PATHS
    assert "shared/escaped.py" in evidence.outside_project_paths
    assert "../private.py" not in evidence.outside_project_paths
    assert evidence.omitted_outside_project_paths_count == 4


def test_capture_amendment_uses_paths_from_successful_scope_audit(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    state.files_changed = ["src/tracked.py"]
    state.validation_cycle_records = [
        {
            "phase": "delivery_scope_audit",
            "status": "passed",
            "metadata": {
                "code": "delivery_scope_audit_passed",
                "changed_count": 2,
                "changed_paths": ["src/tracked.py", "src/ignored.env"],
                "violation_count": 0,
            },
        }
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.failure_code == "scope_amendment_required"
    assert evidence.changed_count == 2
    assert evidence.changed_paths == ("src/ignored.env", "src/tracked.py")
    assert evidence.omitted_changed_paths_count == 0


def test_capture_amendment_ignores_unrelated_and_legacy_successful_audits(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    state.files_changed = ["src/tracked.py"]
    state.validation_cycle_records = [
        {"malformed": True},
        {"phase": "build", "status": "failed"},
        {"phase": "delivery_scope_audit", "status": "unknown", "metadata": {}},
        {
            "phase": "delivery_scope_audit",
            "status": "passed",
            "metadata": {"code": "unrelated", "changed_count": 99},
        },
        {
            "phase": "delivery_scope_audit",
            "status": "passed",
            "metadata": {"code": "delivery_scope_audit_passed", "changed_count": 0},
        },
    ]
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.changed_count == 1
    assert evidence.changed_paths == ("src/tracked.py",)
    assert evidence.omitted_changed_paths_count == 0


def test_scope_violation_recovery_ignores_lower_priority_external_disposition(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    state.delivery_stop_disposition = {
        "schema_version": 1,
        "disposition": "external_dependency_gap",
        "summary": "The implementer first reported an external dependency.",
        "recommended_action": "external_dependency_follow_up",
        "source": "implementer",
        "timestamp": "2026-07-20T10:03:00Z",
    }
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    target = inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.failure_code == "unit_scope_violation"
    assert evidence.recommended_action == "delivery_amend_prepare"
    assert state.delivery_stop_disposition["disposition"] == "external_dependency_gap"
    assert evidence.stop_disposition is None
    assert evidence.requires_external_follow_up is False


def test_pending_middle_split_preserves_progress_and_rewires_to_all_leaves(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=tmp_path, check=True)
    retained_contract = plan_path.parent / "units" / "a.md"
    retained_lf = retained_contract.read_bytes().replace(b"\r\n", b"\n")
    retained_contract.write_bytes(retained_lf.replace(b"\n", b"\r\n"))
    progress_before = json.loads(progress_path.read_text(encoding="utf-8"))
    events_path = delivery_events_path(tmp_path, "amend-demo")
    events_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "amend-demo",
                "event_type": "unit.done",
                "timestamp": "2026-07-20T10:01:30Z",
                "unit_id": "b",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    events_before = events_path.read_bytes()
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    assert result.rewired_unit_ids == ["d"]
    progress_after = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress_after["units"] == progress_before["units"]
    assert progress_after["assembly_status"] == "ready"
    assert progress_after["assembly_base_commit"] == proposal.source_assembly_base_commit
    assert progress_after["assembled_commit"] != proposal.source_assembled_commit
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    by_id = {unit["id"]: unit for unit in plan["units"]}
    assert by_id["a"]["depends_on"] == []
    assert by_id["b"]["depends_on"] == ["a"]
    assert by_id["c"]["superseded_by"] == ["c-1", "c-2", "c-3"]
    assert by_id["c"]["amend_reason"] == "unit_budget_exceeded"
    assert by_id["c"]["budget_exceeded"] == {"name": "max_planner_steps", "limit": 2, "actual": 5}
    assert by_id["c-1"]["depends_on"] == ["b"]
    assert by_id["c-2"]["depends_on"] == ["c-1"]
    assert by_id["c-3"]["depends_on"] == ["c-1"]
    assert by_id["d"]["depends_on"] == ["c-2", "c-3"]
    assert all((tmp_path / by_id[unit_id]["task_path"]).is_file() for unit_id in ("c-1", "c-2", "c-3"))

    status = get_delivery_status(plan_path, project_root=tmp_path)
    status_by_id = {unit.id: unit for unit in status.units}
    assert status.valid is True
    assert status_by_id["c"].status == "superseded"
    assert status_by_id["c"].superseded_by == ["c-1", "c-2", "c-3"]
    assert status_by_id["c-1"].eligible is True
    assert status_by_id["d"].blocked_by == ["c-2", "c-3"]
    assert "c: superseded (replaced by: c-1, c-2, c-3)" in render_delivery_status(status)
    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)
    assert exc_info.value.issue.code == "delivery_amend.target_superseded"

    assert events_path.read_bytes().startswith(events_before)
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [event["event_type"] for event in events] == [
        "unit.done",
        "plan.amend_started",
        "unit.split_recommended",
        "unit.superseded",
        "unit.replacement_added",
        "unit.replacement_added",
        "unit.replacement_added",
        "plan.amended",
    ]
    assert events[-1]["replacement_ids"] == ["c-1", "c-2", "c-3"]
    assert events[-1]["rewired_unit_ids"] == ["d"]
    assert events[-1]["branch"] == "sikula/delivery/amend-demo"
    assert events[-1]["commit"] == progress_after["assembled_commit"]
    assembled_commit = progress_after["assembled_commit"]
    for unit in plan["units"]:
        task_path = unit["task_path"]
        worktree_content = (tmp_path / task_path).read_bytes()
        assert _commit_content_id(tmp_path, assembled_commit, task_path) == _filtered_content_id(
            tmp_path, task_path, worktree_content
        )


def test_amendment_preserves_assigned_target_assets_in_replacement_tasks(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    asset_path = ".sikula/task-assets/success-check.svg"
    target_path = "app/assets/success-check.svg"
    asset_file = tmp_path / asset_path
    asset_file.parent.mkdir(parents=True)
    asset_file.write_bytes(b"<svg />")
    declared_hash = "sha256:" + hashlib.sha256(asset_file.read_bytes()).hexdigest()
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(
        target_task.read_text(encoding="utf-8").rstrip()
        + f"""

## Assets

- Delivery asset: `{asset_path}`
  - Target: `{target_path}`
  - Source/license: provided by product team.
  - SHA-256: `{declared_hash}`
""",
        encoding="utf-8",
    )
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        asset_paths=[asset_path],
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        draft,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )
    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )

    assert result.applied is True
    replacement = proposal.replacement_units[0].task_markdown
    assert f"- Delivery asset: `{asset_path}`" in replacement
    assert f"  - Target: `{target_path}`" in replacement
    assert "  - Source/license: provided by product team." in replacement
    assert f"  - SHA-256: `{declared_hash}`" in replacement
    for unit in proposal.replacement_units[1:]:
        assert asset_path not in unit.task_markdown
    assert (tmp_path / proposal.replacement_units[0].task_path).read_text(encoding="utf-8") == replacement


def test_amendment_preserves_prepared_asset_manifest_in_replacement_tasks(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    asset_path = ".sikula/task-assets/reference.png"
    asset_file = tmp_path / asset_path
    asset_file.parent.mkdir(parents=True)
    asset_file.write_bytes(b"reference")
    declared_hash = "sha256:" + hashlib.sha256(asset_file.read_bytes()).hexdigest()
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(
        target_task.read_text(encoding="utf-8").rstrip()
        + f"""

## Assets

- Reference asset: `{asset_path}`
  - Usage: reference only.
  - Notes: Preserve the original spacing shown in this reference.
  - Do not modify or copy this source asset.

## Asset manifest

### Reference assets

- Path: `{asset_path}`
  - SHA-256: `{declared_hash}`
  - Purpose: reference context for the implementation contract.
  - Usage: reference only; do not copy this asset into production files.
""",
        encoding="utf-8",
    )
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        asset_paths=[asset_path],
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        draft,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )
    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )

    assert result.applied is True
    replacement = proposal.replacement_units[0].task_markdown
    assert "## Asset manifest" in replacement
    assert f"- Path: `{asset_path}`" in replacement
    assert f"  - SHA-256: `{declared_hash}`" in replacement
    assert "  - Notes: Preserve the original spacing shown in this reference." in replacement
    assert "  - Do not modify or copy this source asset." in replacement
    for unit in proposal.replacement_units[1:]:
        assert "## Asset manifest" not in unit.task_markdown


def test_amendment_blocks_unassigned_target_asset(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    asset_path = ".sikula/task-assets/reference.png"
    asset_file = tmp_path / asset_path
    asset_file.parent.mkdir(parents=True)
    asset_file.write_bytes(b"reference")
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(
        target_task.read_text(encoding="utf-8").rstrip()
        + f"""

## Assets

- Reference asset: `{asset_path}`
""",
        encoding="utf-8",
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
            project_config=_project_config(tmp_path),
        )

    assert exc_info.value.issue.code == "delivery_amend.source_asset_unassigned"
    assert not proposal_root.exists()


def test_amendment_blocks_assets_hidden_by_unterminated_replacement_block(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    asset_path = ".sikula/task-assets/reference.png"
    asset_file = tmp_path / asset_path
    asset_file.parent.mkdir(parents=True)
    asset_file.write_bytes(b"reference")
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(
        target_task.read_text(encoding="utf-8").rstrip()
        + f"""

## Assets

- Reference asset: `{asset_path}`
""",
        encoding="utf-8",
    )
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        task_markdown=draft.replacement_units[0].task_markdown.rstrip() + "\n\n<!--\n",
        asset_paths=[asset_path],
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
            project_config=_project_config(tmp_path),
        )

    assert exc_info.value.issue.code == "delivery_amend.unit_asset_render_invalid"
    assert not proposal_root.exists()


def test_amendment_redacts_asset_assignment_check_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    private_detail = str(tmp_path / "private-asset.png")

    def fail_assignment(*args, **kwargs):
        raise OSError(private_detail)

    monkeypatch.setattr(delivery_amendment_module, "render_delivery_asset_assignments", fail_assignment)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "delivery_amend.asset_assignment_check_failed"
    assert private_detail not in str(exc_info.value)
    assert not proposal_root.exists()


def test_constrained_middle_split_reassigns_constraint_to_every_replacement(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    _add_target_constraint(tmp_path, plan_path)
    asset_path = ".sikula/task-assets/protocol-reference.png"
    asset_file = tmp_path / asset_path
    asset_file.parent.mkdir(parents=True)
    asset_file.write_bytes(b"protocol reference")
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(
        target_task.read_text(encoding="utf-8").rstrip()
        + f"\n\n## Assets\n\n- Reference asset: `{asset_path}`\n  - Usage: reference only.\n",
        encoding="utf-8",
    )
    before = check_delivery_plan_file(plan_path, project_root=tmp_path)
    assert before.valid is True
    asset_draft = _draft()
    asset_draft.replacement_units[0] = replace(
        asset_draft.replacement_units[0],
        asset_paths=[asset_path],
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            asset_draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
            project_config=_project_config(tmp_path),
        )
    assert exc_info.value.issue.code == "delivery_amend.constraint_verification_required"

    draft = replace(
        asset_draft,
        constraint_verification=DeliveryConstraintVerification(
            constraints_complete=True,
            constraints=[
                DeliveryAuthoringConstraintDraft(
                    id="protocol-authority",
                    kind="authoritative_read_only_dependency",
                    summary="Protocol changes remain owned by the external protocol project.",
                    unit_ids=["c-1", "c-2", "c-3"],
                    disposition="preserved",
                )
            ],
        ),
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        draft,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )
    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )

    assert result.applied is True
    assert f"- Reference asset: `{asset_path}`" in proposal.replacement_units[0].task_markdown
    amended = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    constraint = amended["constraints"][0]
    assert constraint["unit_ids"] == ["a", "c-1", "c-2", "c-3", "d"]
    assert "c" not in constraint["unit_ids"]
    assert amended["constraints"][1]["unit_ids"] == ["b"]

    checked = check_delivery_plan_file(plan_path, project_root=tmp_path)
    assert checked.valid is True
    assert checked.plan is not None
    for replacement_id in proposal.replacement_ids:
        _, source_task, inherited = delivery_unit_constraint_context(checked.plan, replacement_id)
        assert source_task == amended["source_task"]
        assert inherited == [constraint]


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("mismatch", "delivery_amend.constraint_verification_invalid"),
        ("incomplete", "delivery_amend.constraint_verification_incomplete"),
        ("conflict", "delivery_amend.constraint_conflict"),
        ("needs_review", "delivery_amend.constraint_review_required"),
    ],
)
def test_constrained_amendment_rejects_untrusted_verification(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    _add_target_constraint(tmp_path, plan_path)
    verification_constraints = []
    if case != "mismatch":
        verification_constraints = [
            DeliveryAuthoringConstraintDraft(
                id="protocol-authority",
                kind="authoritative_read_only_dependency",
                summary="Protocol changes remain owned by the external protocol project.",
                unit_ids=["c-1", "c-2", "c-3"],
                disposition="preserved" if case == "incomplete" else case,
            )
        ]
    draft = replace(
        _draft(),
        constraint_verification=DeliveryConstraintVerification(
            constraints_complete=case != "incomplete",
            constraints=verification_constraints,
        ),
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == expected_code


def test_prepare_stores_componentless_replacements_without_component_metadata(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)

    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert [unit.component for unit in proposal.replacement_units] == [None, None, None]
    assert all("component" not in unit for unit in payload["replacement_units"])


def test_proposal_without_downstream_rewiring_round_trips_and_applies(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = DeliveryAmendmentAuthoringDraft(
        plan_id="amend-demo",
        target_unit_id="d",
        replacement_units=[
            DeliveryAuthoringUnitDraft("d-1", "D1", [], _task_markdown("D1")),
            DeliveryAuthoringUnitDraft("d-2", "D2", ["d-1"], _task_markdown("D2")),
        ],
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "d", draft, project_root=tmp_path, proposal_root=proposal_root
    )
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert proposal.rewired_unit_ids == []
    assert result.applied is True
    assert result.rewired_unit_ids == []


def test_proposal_with_multiple_budget_fields_round_trips_and_applies(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        budget=DeliveryUnitBudget(
            max_planner_steps=1,
            max_changed_files=2,
            max_changed_modules=1,
            max_generated_test_files=1,
        ),
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", draft, project_root=tmp_path, proposal_root=proposal_root
    )
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True


def test_prepare_stores_replacement_with_declared_component(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    _add_plan_component(plan_path, "ApiV2")
    draft = _draft()
    draft.replacement_units[0] = replace(draft.replacement_units[0], component="ApiV2")

    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", draft, project_root=tmp_path, proposal_root=proposal_root
    )

    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal.replacement_units[0].component == "ApiV2"
    assert payload["replacement_units"][0]["component"] == "ApiV2"
    assert all("component" not in unit for unit in payload["replacement_units"][1:])


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_apply_sets_normal_source_permissions_on_replacement_tasks(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    previous_umask = os.umask(0o027)
    try:
        result = apply_delivery_amendment(
            plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
        )
    finally:
        os.umask(previous_umask)

    assert result.applied is True
    assert {stat.S_IMODE((tmp_path / unit.task_path).stat().st_mode) for unit in proposal.replacement_units} == {0o640}


def test_superseded_unit_no_longer_emits_split_sizing_warning(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    target = next(unit for unit in plan["units"] if unit["id"] == "c")
    target["risk_tags"] = ["external_execution_boundary", "structured_output_contract", "cli_surface"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    before = get_delivery_status(plan_path, project_root=tmp_path)
    assert "units.split_recommended" in {warning.code for warning in before.warnings}
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    after = get_delivery_status(plan_path, project_root=tmp_path)

    assert result.applied is True
    assert "units.split_recommended" not in {warning.code for warning in result.warnings}
    assert "units.split_recommended" not in {warning.code for warning in after.warnings}


def test_prepare_rejects_unapplied_completed_dependency_commit(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"][1]["commit"] = "deadbeef"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == "delivery.unit_commit_unapplied"


@pytest.mark.parametrize("recorded_assembly", [False, True])
def test_prepare_and_apply_use_assembly_without_changing_operator_checkout(
    tmp_path: Path,
    recorded_assembly: bool,
) -> None:
    _git_init(tmp_path)
    base_commit = _commit(tmp_path, "base.txt", "base\n")
    completed_commit = _commit(tmp_path, "completed.txt", "completed\n")
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", completed_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "operator-plan", base_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    plan_path = _write_plan(tmp_path)
    progress_path = _write_progress(tmp_path, base_commit, completed_commit)
    if recorded_assembly:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        progress.update(
            assembly_base_commit=base_commit,
            assembled_commit=completed_commit,
            assembly_status="ready",
            assembly_updated_at="2026-07-20T10:01:30Z",
        )
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
    proposal_root = tmp_path / ".sikula" / "contract-reports"
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        _draft(),
        project_root=tmp_path,
        proposal_root=proposal_root,
    )
    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert result.applied is True
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()
        == head_before
    )
    assert subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tmp_path, check=False).returncode == 0
    final_commit = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert final_commit != completed_commit
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", completed_commit, final_commit], cwd=tmp_path, check=False
        ).returncode
        == 0
    )
    plan_in_branch = subprocess.run(
        ["git", "show", f"{final_commit}:{plan_path.relative_to(tmp_path).as_posix()}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout
    assert plan_in_branch == plan_path.read_bytes()
    for unit in proposal.replacement_units:
        task_in_branch = subprocess.run(
            ["git", "show", f"{final_commit}:{unit.task_path}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        assert task_in_branch == unit.task_markdown.encode("utf-8")


def test_repeated_apply_repairs_missing_progress_and_terminal_event(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    events_path = delivery_events_path(tmp_path, proposal.plan_id)
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events if event["event_type"] != "plan.amended")
        + json.dumps(
            {
                "schema_version": 1,
                "plan_id": proposal.plan_id,
                "event_type": "unrelated.event",
                "timestamp": "2026-08-05T10:00:00Z",
                "proposal_id": "other",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    for key in ("assembly_base_commit", "assembled_commit", "assembly_status", "assembly_updated_at"):
        progress.pop(key, None)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    branch_before = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", f"{branch_before}^{{tree}}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch_after = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit-tree",
            tree,
            "-p",
            branch_before,
            "-m",
            "later delivery work",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/heads/sikula/delivery/amend-demo", branch_after, branch_before],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    repaired = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert repaired.applied is True
    repaired_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert repaired_progress["assembled_commit"] == branch_after
    repaired_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in repaired_events].count("plan.amended") == 1
    assert next(event for event in repaired_events if event["event_type"] == "plan.amended")["commit"] == branch_before
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == branch_after
    )


def test_repeated_apply_integrates_exact_locally_published_artifacts(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    preview = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert preview.ready is True
    assert preview.applied is False
    assert "assembly integration" in preview.message
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    assembled_commit = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["assembled_commit"] == assembled_commit
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert next(event for event in events if event["event_type"] == "plan.amended")["commit"] == assembled_commit


def test_repeated_apply_rejects_changed_target_task_before_recovery_assembly(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(target_task.read_text(encoding="utf-8") + "\nChanged after prepare.\n", encoding="utf-8")

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.target_task_stale"]
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_repeated_apply_rechecks_replacement_readiness_before_recovery_assembly(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        task_markdown=draft.replacement_units[0].task_markdown.replace(
            "python3 -m pytest tests/test_delivery_amendment.py",
            "npm test",
        ),
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", draft, project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.replacement_contract_not_ready"]
    assert result.errors[0].path == ".sikula/delivery/demo/units/c-1.md"
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


@pytest.mark.parametrize("replacement_status", ["running", "done"])
def test_repeated_apply_rejects_replacement_progress_before_recovery_assembly(
    tmp_path: Path,
    replacement_status: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    replacement_progress = {
        "unit_id": "c-1",
        "status": replacement_status,
        "child_task_id": "task-c-1",
        "started_at": "2026-07-20T11:00:00Z",
        "updated_at": "2026-07-20T11:30:00Z",
    }
    if replacement_status == "done":
        replacement_progress.update(
            branch="sikula/c-1",
            commit=proposal.source_assembled_commit,
            completed_at="2026-07-20T11:30:00Z",
        )
    progress["units"].append(replacement_progress)
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    progress_before = progress_path.read_bytes()

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.progress_stale"]
    assert progress_path.read_bytes() == progress_before
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    events = [json.loads(line) for line in delivery_events_path(tmp_path, proposal.plan_id).read_text().splitlines()]
    assert "plan.amended" not in [event["event_type"] for event in events]


def test_repeated_apply_compares_canonical_source_plan_blob(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.yaml text eol=crlf\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes", str(plan_path.parent.relative_to(tmp_path))],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _git_with_identity("commit", "-m", "track delivery sources"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", source_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_base_commit=progress["units"][0]["commit"],
        assembled_commit=source_commit,
        assembly_status="ready",
        assembly_updated_at="2026-07-20T10:02:00Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    plan_path.write_bytes(plan_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    source_bytes = plan_path.read_bytes()
    filtered_source = subprocess.run(
        [
            "git",
            "cat-file",
            "--filters",
            f"--path={plan_path.relative_to(tmp_path).as_posix()}",
            f"{source_commit}:{plan_path.relative_to(tmp_path).as_posix()}",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout
    assert b"\r\n" not in source_bytes
    assert b"\r\n" in filtered_source
    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        _draft(),
        project_root=tmp_path,
        proposal_root=proposal_root,
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert result.applied is True


@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("missing", "units.task_path_missing"),
        ("changed", "delivery_amend.replacement_task_stale"),
    ],
)
def test_repeated_apply_rejects_incomplete_locally_published_artifacts(
    tmp_path: Path,
    mode: str,
    error_code: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    affected_unit = proposal.replacement_units[0].id
    _publish_amendment_locally(
        plan_path,
        tmp_path,
        proposal,
        omit_unit_id=affected_unit if mode == "missing" else None,
        changed_unit_id=affected_unit if mode == "changed" else None,
    )

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == [error_code]


def test_repeated_apply_rejects_assembly_advanced_without_artifact_commit(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    advanced = _commit(tmp_path, "unrelated.txt", "unrelated delivery work\n")
    subprocess.run(["git", "branch", "sikula/delivery/amend-demo", advanced], cwd=tmp_path, check=True)
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.assembly_artifact_missing"]


def test_repeated_apply_rejects_assembly_change_during_artifact_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    monkeypatch.setattr(
        delivery_amendment_module,
        "find_delivery_artifact_commit",
        lambda *_args, **_kwargs: (None, "a" * 40),
    )

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.assembly_artifact_missing"]


def test_repeated_apply_recovers_missing_progress_after_local_publication(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress_path.unlink()
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    assert proposal.progress_fingerprint == hashlib.sha256(b"null").hexdigest()
    delivery_amendment_module.append_delivery_progress_event(
        delivery_events_path(tmp_path, proposal.plan_id),
        delivery_amendment_module._amendment_event(
            proposal,
            "plan.amend_started",
            timestamp="2026-07-20T10:02:30Z",
        ),
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert (
        progress["assembled_commit"]
        == subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def test_repeated_apply_does_not_recreate_progress_deleted_after_success(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress_path.unlink()
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    delivery_amendment_module.append_delivery_progress_event(
        delivery_events_path(tmp_path, proposal.plan_id),
        delivery_amendment_module._amendment_event(
            proposal,
            "plan.amend_started",
            timestamp="2026-07-20T10:02:30Z",
        ),
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"] = [
        {
            "unit_id": "c-1",
            "status": "done",
            "child_task_id": "task-c-1",
            "branch": "sikula/c-1",
            "commit": proposal.source_assembled_commit,
            "started_at": "2026-07-20T11:00:00Z",
            "completed_at": "2026-07-20T11:30:00Z",
            "updated_at": "2026-07-20T11:30:00Z",
        }
    ]
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    delivery_amendment_module.append_delivery_progress_event(
        delivery_events_path(tmp_path, proposal.plan_id),
        delivery_amendment_module.DeliveryProgressEvent(
            plan_id=proposal.plan_id,
            event_type="unit.done",
            timestamp="2026-07-20T11:30:00Z",
            unit_id="c-1",
        ),
    )
    progress_path.unlink()
    branch_before = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    repeated = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert repeated.applied is False
    assert [issue.code for issue in repeated.errors] == ["delivery_amend.progress_invalid"]
    assert not progress_path.exists()
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == branch_before
    )


def test_repeated_apply_converts_recovery_io_failure_to_blocked_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress_before = progress_path.read_bytes()

    def fail_assembly(*_args, **_kwargs):
        raise OSError("simulated assembly I/O failure")

    monkeypatch.setattr(delivery_amendment_module, "assemble_delivery_artifacts", fail_assembly)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.write_failed"]
    assert progress_path.read_bytes() == progress_before
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_repeated_apply_recovers_from_legacy_branch_behind_source(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    previous_branch_commit = progress["units"][0]["commit"]
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", previous_branch_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    preview = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert preview.ready is True
    assert result.applied is True
    amendment_commit = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        subprocess.run(
            ["git", "rev-parse", f"{amendment_commit}^"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == proposal.source_assembled_commit
    )


def test_repeated_apply_blocks_checked_out_branch_when_recovery_requires_commit(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    subprocess.run(
        ["git", "checkout", "-b", "sikula/delivery/amend-demo", proposal.source_assembled_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    preview = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery.assembly_branch_checked_out"]


def test_repeated_apply_allows_checked_out_branch_when_artifact_commit_is_current(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    worktree = tmp_path.parent / f"{tmp_path.name}-final-branch"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    try:
        preview = preview_delivery_amendment(
            plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
        )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=tmp_path, check=True)

    assert preview.ready is True
    assert preview.errors == []


def test_repeated_apply_rejects_descendant_that_changes_amendment_artifact(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    replacement = proposal.replacement_units[0]
    worktree = tmp_path.parent / f"{tmp_path.name}-changed-final-branch"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    try:
        changed_path = worktree / replacement.task_path
        changed_path.write_text("changed after amendment\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", replacement.task_path], cwd=worktree, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Sikula Test",
                "-c",
                "user.email=sikula@example.test",
                "commit",
                "-m",
                "change amendment artifact",
            ],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=tmp_path, check=True)

    preview = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery_amend.assembly_artifact_missing"]


def test_repeated_apply_preserves_unresolved_assembly_failure(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_status="failed",
        assembly_base_commit=proposal.source_assembly_base_commit,
        assembled_commit=proposal.source_assembled_commit,
        assembly_unit_id="b",
        assembly_error_code="delivery.assembly_conflict",
        assembly_updated_at="2026-07-20T10:04:00Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    progress_before = progress_path.read_bytes()

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_conflict"]
    assert progress_path.read_bytes() == progress_before


def test_repeated_apply_rejects_progress_lost_after_proposal(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress_path.unlink()

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.progress_invalid"]


def test_repeated_apply_reports_recovery_assembly_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    monkeypatch.setattr(
        delivery_amendment_module,
        "assemble_delivery_artifacts",
        lambda *_args, **_kwargs: DeliveryArtifactAssemblyResult(
            success=False,
            branch="sikula/delivery/amend-demo",
            parent_commit=proposal.source_assembled_commit,
            assembled_commit=None,
            error=DeliveryPlanIssue("error", "delivery.assembly_artifact_git_failed", "Git failed."),
        ),
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_artifact_git_failed"]


@pytest.mark.parametrize("git_error", [False, True])
def test_repeated_apply_rejects_branch_change_after_recovery_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_error: bool,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)

    def branch_commit(*_args):
        if git_error:
            raise OSError("git unavailable")
        return None

    monkeypatch.setattr(delivery_amendment_module, "delivery_branch_commit", branch_commit)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_diverged"]


def test_repeated_apply_reports_success_event_reconciliation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    events_path = delivery_events_path(tmp_path, proposal.plan_id)
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events if event["event_type"] != "plan.amended"),
        encoding="utf-8",
    )

    def fail_events(*_args, **_kwargs):
        raise OSError("event log unavailable")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", fail_events)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.event_write_failed"]


@pytest.mark.parametrize("malformed_history", [b"\xff\n", b"\xff\n{"])
def test_repeated_apply_rejects_malformed_event_history(tmp_path: Path, malformed_history: bytes) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    delivery_events_path(tmp_path, proposal.plan_id).write_bytes(malformed_history)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.events_invalid"]


def test_repeated_apply_recovers_truncated_final_success_event(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    events_path = delivery_events_path(tmp_path, proposal.plan_id)
    lines = events_path.read_bytes().splitlines(keepends=True)
    events_path.write_bytes(b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2])

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events].count("plan.amended") == 1
    assert events_path.read_bytes().endswith(b"\n")


def test_repeated_apply_restores_truncated_event_when_reconciliation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    assert first.applied is True
    events_path = delivery_events_path(tmp_path, proposal.plan_id)
    lines = events_path.read_bytes().splitlines(keepends=True)
    truncated = b"".join(lines[:-1]) + lines[-1][: len(lines[-1]) // 2]
    events_path.write_bytes(truncated)

    def fail_events(*_args, **_kwargs):
        raise OSError("event log unavailable")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", fail_events)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.event_write_failed"]
    assert events_path.read_bytes() == truncated


def test_prepare_rejects_replacement_path_already_present_only_in_final_branch(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    operator_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "-b", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    conflict_path = plan_path.parent / "units" / "c-1.md"
    conflict_path.write_text("existing assembly artifact\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", conflict_path.relative_to(tmp_path).as_posix()],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit",
            "-m",
            "existing assembly artifact",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    branch_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", operator_branch], cwd=tmp_path, check=True, capture_output=True)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_base_commit=progress["units"][0]["commit"],
        assembled_commit=branch_commit,
        assembly_status="ready",
        assembly_updated_at="2026-07-20T10:01:30Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    plan_before = plan_path.read_bytes()

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == "delivery.assembly_artifact_conflict"
    assert plan_path.read_bytes() == plan_before
    assert not conflict_path.exists()
    assert not list(proposal_root.rglob("*.json"))
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == branch_commit
    )


@pytest.mark.parametrize("stale_artifact", ["plan", "retained_contract"])
def test_prepare_rejects_stale_source_artifact_in_final_branch(tmp_path: Path, stale_artifact: str) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    operator_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "add", plan_path.parent.relative_to(tmp_path).as_posix()],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _git_with_identity("commit", "-m", "track delivery sources"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    if stale_artifact == "plan":
        stale_path = plan_path
        stale_plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        stale_plan["title"] = "Stale assembly plan"
        stale_path.write_text(yaml.safe_dump(stale_plan, sort_keys=False), encoding="utf-8")
    else:
        stale_path = plan_path.parent / "units" / "a.md"
        stale_path.write_text("stale retained contract\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", stale_path.relative_to(tmp_path).as_posix()],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _git_with_identity("commit", "-m", "change assembly plan"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    stale_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", operator_branch], cwd=tmp_path, check=True, capture_output=True)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_base_commit=progress["units"][0]["commit"],
        assembled_commit=stale_commit,
        assembly_status="ready",
        assembly_updated_at="2026-07-20T10:01:30Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    plan_before = plan_path.read_bytes()

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == "delivery.assembly_artifact_stale"
    assert plan_path.read_bytes() == plan_before
    assert not list(proposal_root.rglob("*.json"))
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == stale_commit
    )


def test_prepare_rejects_non_pending_downstream_unit(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"].append(
        {
            "unit_id": "d",
            "status": "running",
            "child_task_id": "task-d",
            "started_at": "2026-07-20T11:00:00Z",
        }
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    assert exc_info.value.issue.code == "delivery_amend.downstream_state_unsafe"


def test_prepare_allows_checked_out_final_branch_but_apply_preview_blocks_mutation(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    subprocess.run(
        ["git", "checkout", "-b", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    preview = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery.assembly_branch_checked_out"]


def test_prepare_rejects_unknown_target(tmp_path: Path) -> None:
    plan_path, _, _ = _setup(tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "missing", project_root=tmp_path)

    assert exc_info.value.issue.code == "delivery_amend.target_unknown"


def test_amend_prepare_redacts_unsafe_unknown_target_from_public_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    private_target = "/Users/example/private/unit"
    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit=private_target,
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg)

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exc_info.value.code == 1
    assert payload["target_unit_id"].startswith("<redacted:")
    assert payload["errors"][0]["message"] == "<redacted>"
    assert private_target not in output


@pytest.mark.parametrize(
    "private_path",
    [
        ".git/private-target.md",
        ".sikula/state/private-target.md",
        ".sikula/worktrees/private-target.md",
        ".sikula/contract-reports/private-target.md",
    ],
)
def test_amend_prepare_rejects_private_target_before_authoring(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    private_path: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    target_path = tmp_path / private_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("private prompt and provider output\n", encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    next(unit for unit in plan["units"] if unit["id"] == "c")["task_path"] = private_path
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    author_called = False

    def author(**kwargs):
        nonlocal author_called
        author_called = True
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.target_task_forbidden"]
    assert "private prompt" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("config_key", "private_dir_name"),
    [
        ("state_dir", ".private-task-state"),
        ("contract_report_dir", ".private-contract-reports"),
    ],
)
def test_amend_prepare_rejects_target_under_configured_private_root_before_authoring(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_key: str,
    private_dir_name: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    private_dir = tmp_path / private_dir_name
    target_path = private_dir / "private-target.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("configured private task content\n", encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    next(unit for unit in plan["units"] if unit["id"] == "c")["task_path"] = target_path.relative_to(
        tmp_path
    ).as_posix()
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    author_called = False

    def author(**kwargs):
        nonlocal author_called
        author_called = True
        return _draft()

    tasks = {"contract_report_dir": str(proposal_root), config_key: private_dir_name}
    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {**_project_config(tmp_path), "tasks": tasks}

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.target_task_forbidden"]
    assert "configured private task content" not in json.dumps(payload)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlink support")
@pytest.mark.parametrize("unit_id", ["a", "c"])
def test_amend_prepare_rejects_symlink_contract_before_authoring(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unit_id: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    contract_path = plan_path.parent / "units" / f"{unit_id}.md"
    source_path = contract_path.with_suffix(".source.md")
    contract_path.rename(source_path)
    contract_path.symlink_to(source_path.name)
    author_called = False

    def author(**kwargs):
        nonlocal author_called
        author_called = True
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.source_contract_unsafe"]
    assert not list(proposal_root.rglob("*.json"))


@pytest.mark.parametrize(
    "attribute_pattern",
    [
        ".sikula/delivery/demo/units/a.md",
        ".sikula/delivery/demo/units/c-*.md",
    ],
)
def test_amend_prepare_rejects_filtered_contract_before_storing_proposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    attribute_pattern: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    (tmp_path / ".gitattributes").write_text(f"{attribute_pattern} filter=blocked\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes", plan_path.parent.relative_to(tmp_path).as_posix()],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _git_with_identity("commit", "-m", "track filtered delivery sources"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", source_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_base_commit=progress["units"][0]["commit"],
        assembled_commit=source_commit,
        assembly_status="ready",
        assembly_updated_at="2026-07-20T10:02:00Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    author_called = False

    def author(**kwargs):
        nonlocal author_called
        author_called = True
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is True
    assert [issue["code"] for issue in payload["errors"]] == ["delivery.assembly_artifact_filter_unsupported"]
    assert not list(proposal_root.rglob("*.json"))


def test_amend_prepare_rejects_target_symlink_race_before_model_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    target_path = plan_path.parent / "units" / "c.md"
    saved_path = target_path.with_suffix(".saved.md")
    private_path = tmp_path / ".sikula" / "state" / "private-authoring.md"
    private_path.write_text("private prompt must not reach model\n", encoding="utf-8")
    agent_created = False

    def create_agent(*args, **kwargs):
        nonlocal agent_created
        agent_created = True
        raise AssertionError("authoring agent must not be created")

    def author_with_race(**kwargs):
        target_path.rename(saved_path)
        target_path.symlink_to(private_path)
        try:
            return sikula._run_delivery_amend_prepare_authoring(**kwargs)
        finally:
            target_path.unlink()
            saved_path.rename(target_path)

    monkeypatch.setattr(sikula, "_create_delivery_preparation_agent", create_agent)
    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author_with_race))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert agent_created is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.target_task_forbidden"]
    assert "private prompt" not in json.dumps(payload)


def test_prepare_rejects_finalized_plan(tmp_path: Path) -> None:
    plan_path, progress_path, _ = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["final_branch"] = "sikula/delivery/amend-demo"
    progress["final_commit"] = progress["units"][1]["commit"]
    progress["finalized_at"] = "2026-07-20T12:00:00Z"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    assert exc_info.value.issue.code == "delivery_amend.plan_finalized"


@pytest.mark.parametrize(
    ("draft", "error_code"),
    [
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="other-plan",
                target_unit_id="c",
                replacement_units=[
                    DeliveryAuthoringUnitDraft("x", "X", [], _task_markdown("X")),
                    DeliveryAuthoringUnitDraft("y", "Y", ["x"], _task_markdown("Y")),
                ],
            ),
            "delivery_amend.authoring_mismatch",
        ),
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="amend-demo",
                target_unit_id="c",
                replacement_units=[DeliveryAuthoringUnitDraft("x", "X", [], _task_markdown("X"))],
            ),
            "delivery_amend.replacements_too_few",
        ),
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="amend-demo",
                target_unit_id="c",
                replacement_units=[
                    DeliveryAuthoringUnitDraft("d", "D replacement", [], _task_markdown("D replacement")),
                    DeliveryAuthoringUnitDraft("x", "X", ["d"], _task_markdown("X")),
                ],
            ),
            "delivery_amend.replacement_id_conflict",
        ),
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="amend-demo",
                target_unit_id="c",
                replacement_units=[
                    DeliveryAuthoringUnitDraft("x", "X", ["outside"], _task_markdown("X")),
                    DeliveryAuthoringUnitDraft("y", "Y", ["x"], _task_markdown("Y")),
                ],
            ),
            "delivery_amend.replacement_dependency_external",
        ),
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="amend-demo",
                target_unit_id="c",
                replacement_units=[
                    DeliveryAuthoringUnitDraft("split", "Split", [], _task_markdown("Split")),
                    DeliveryAuthoringUnitDraft("SPLIT", "Split again", ["split"], _task_markdown("Split again")),
                ],
            ),
            "delivery_amend.replacement_duplicate",
        ),
        (
            DeliveryAmendmentAuthoringDraft(
                plan_id="amend-demo",
                target_unit_id="c",
                replacement_units=[
                    DeliveryAuthoringUnitDraft("../escape", "Escape", [], _task_markdown("Escape")),
                    DeliveryAuthoringUnitDraft("safe", "Safe", ["../escape"], _task_markdown("Safe")),
                ],
            ),
            "delivery_amend.replacement_id_invalid",
        ),
    ],
)
def test_prepare_rejects_invalid_authored_replacement_graph(
    tmp_path: Path,
    draft: DeliveryAmendmentAuthoringDraft,
    error_code: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", draft, project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == error_code
    assert not list(proposal_root.rglob("*.json"))
    assert not (plan_path.parent / "escape.md").exists()


def test_prepare_rejects_replacement_id_already_present_in_parent_progress(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"].append(
        {
            "unit_id": "C-1",
            "status": "done",
            "commit": progress["units"][1]["commit"],
        }
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == "delivery_amend.replacement_progress_conflict"


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_prepare_rejects_preexisting_replacement_task_path(
    tmp_path: Path,
    existing_kind: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    replacement_path = plan_path.parent / "units" / "c-1.md"
    if existing_kind == "file":
        replacement_path.write_text("operator-owned task\n", encoding="utf-8")
    else:
        target = tmp_path / "operator-owned-task.md"
        target.write_text("operator-owned task\n", encoding="utf-8")
        replacement_path.symlink_to(target)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "delivery_amend.replacement_task_conflict"
    assert not list(proposal_root.rglob("*.json"))


@pytest.mark.parametrize("changed_source", ["plan", "target_task", "retained_contract", "progress"])
def test_prepare_rechecks_source_fingerprints_at_publish_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_source: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    target_task = plan_path.parent / "units" / "c.md"
    retained_contract = plan_path.parent / "units" / "a.md"
    real_readiness = delivery_amendment_module._replacement_contract_readiness_errors

    def check_readiness_then_edit(*args, **kwargs):
        result = real_readiness(*args, **kwargs)
        if changed_source == "plan":
            plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
            plan["title"] = "Changed during proposal preparation"
            plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        elif changed_source == "target_task":
            target_task.write_text(
                target_task.read_text(encoding="utf-8") + "\n- Changed during proposal preparation.\n",
                encoding="utf-8",
            )
        elif changed_source == "retained_contract":
            retained_contract.write_text(
                retained_contract.read_text(encoding="utf-8") + "\n- Changed during proposal preparation.\n",
                encoding="utf-8",
            )
        else:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
            target["updated_at"] = "2026-07-20T13:00:00Z"
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
        return result

    monkeypatch.setattr(
        delivery_amendment_module,
        "_replacement_contract_readiness_errors",
        check_readiness_then_edit,
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    expected_code = {
        "plan": "delivery_amend.plan_stale",
        "target_task": "delivery_amend.target_task_stale",
        "retained_contract": "delivery_amend.retained_contract_stale",
        "progress": "delivery_amend.progress_stale",
    }[changed_source]
    assert exc_info.value.issue.code == expected_code
    assert not list(proposal_root.rglob("*.json"))


@pytest.mark.parametrize("changed_input", ["progress", "replacement_path"])
def test_prepare_removes_proposal_when_inputs_change_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_input: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    real_write = delivery_amendment_module._write_new_proposal

    def write_then_change(path, proposal):
        real_write(path, proposal)
        if changed_input == "progress":
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
            target["updated_at"] = "2026-07-20T14:00:00Z"
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
        else:
            replacement_path = tmp_path / proposal.replacement_units[0].task_path
            replacement_path.write_text("operator-created task\n", encoding="utf-8")

    monkeypatch.setattr(delivery_amendment_module, "_write_new_proposal", write_then_change)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    expected_code = {
        "progress": "delivery_amend.progress_stale",
        "replacement_path": "delivery_amend.replacement_task_conflict",
    }[changed_input]
    assert exc_info.value.issue.code == expected_code
    assert not list(proposal_root.rglob("*.json"))


def test_prepare_removes_proposal_when_assembly_advances_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    assembled_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", assembled_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_status="ready",
        assembly_base_commit=assembled_commit,
        assembled_commit=assembled_commit,
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    real_write = delivery_amendment_module._write_new_proposal

    def write_then_advance_assembly(path, proposal):
        real_write(path, proposal)
        tree = subprocess.run(
            ["git", "rev-parse", f"{assembled_commit}^{{tree}}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        concurrent_commit = subprocess.run(
            _git_with_identity("commit-tree", tree, "-p", assembled_commit, "-m", "concurrent assembly"),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-ref",
                "refs/heads/sikula/delivery/amend-demo",
                concurrent_commit,
                assembled_commit,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(delivery_amendment_module, "_write_new_proposal", write_then_advance_assembly)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "delivery_amend.assembly_stale"
    assert not list(proposal_root.rglob("*.json"))


def test_prepare_rejects_replacement_contract_with_uncovered_validation(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        task_markdown=draft.replacement_units[0].task_markdown.replace(
            "python3 -m pytest tests/test_delivery_amendment.py",
            "npm test",
        ),
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
            project_config=_project_config(tmp_path),
        )

    assert exc_info.value.issue.code == "delivery_amend.replacement_contract_not_ready"
    assert "gap.validation.coverage" in exc_info.value.issue.message
    assert not list(proposal_root.rglob("*.json"))


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("stream", "unknown-stream", "units.stream_unknown"),
        ("component", "unknown-component", "units.component_unknown"),
    ],
)
def test_prepare_rejects_invalid_amended_plan_before_publishing_proposal(
    tmp_path: Path,
    field: str,
    value: str,
    error_code: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(draft.replacement_units[0], **{field: value})

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == error_code
    assert not list(proposal_root.rglob("*.json"))


def test_preview_rechecks_replacement_contract_readiness_with_current_config(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(
        draft.replacement_units[0],
        task_markdown=draft.replacement_units[0].task_markdown.replace(
            "python3 -m pytest tests/test_delivery_amendment.py",
            "npm test",
        ),
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        draft,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    result = preview_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.replacement_contract_not_ready"]
    assert result.errors[0].path == ".sikula/delivery/demo/units/c-1.md"


@pytest.mark.parametrize("changed_output", ["plan", "rewired_ids"])
def test_apply_rejects_recomputed_output_that_differs_from_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_output: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    real_amended_plan_data = delivery_amendment_module._amended_plan_data

    def changed_amended_plan_data(*args, **kwargs):
        plan_data, rewired_ids = real_amended_plan_data(*args, **kwargs)
        if changed_output == "plan":
            plan_data["title"] = "Changed replay output"
        else:
            rewired_ids = [*rewired_ids, "unexpected-unit"]
        return plan_data, rewired_ids

    monkeypatch.setattr(delivery_amendment_module, "_amended_plan_data", changed_amended_plan_data)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_output_mismatch"]
    assert plan_path.read_bytes() == plan_before
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_apply_supports_plan_valid_target_id_with_spaces(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    target = next(unit for unit in plan["units"] if unit["id"] == "c")
    target["id"] = "unit one"
    next(unit for unit in plan["units"] if unit["id"] == "d")["depends_on"] = ["unit one"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    next(unit for unit in progress["units"] if unit["unit_id"] == "c")["unit_id"] = "unit one"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    draft = _draft()
    draft.target_unit_id = "unit one"

    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "unit one", draft, project_root=tmp_path, proposal_root=proposal_root
    )
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    assert result.target_unit_id == "unit one"


def test_preview_is_no_write_and_uses_stored_exact_proposal(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    before = _snapshot(tmp_path)
    refs_before = subprocess.run(["git", "show-ref"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    objects_before = subprocess.run(
        ["git", "count-objects", "-v"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is True
    assert result.dry_run is True
    assert result.applied is False
    assert _snapshot(tmp_path) == before
    assert (
        subprocess.run(["git", "show-ref"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
        == refs_before
    )
    assert (
        subprocess.run(["git", "count-objects", "-v"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
        == objects_before
    )
    assert not delivery_events_path(tmp_path, "amend-demo").exists()


def test_apply_rejects_identical_plan_at_different_source_path(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    sibling_plan_path = plan_path.with_name("plan-copy.yaml")
    sibling_plan_path.write_bytes(plan_path.read_bytes())
    sibling_before = sibling_plan_path.read_bytes()

    result = apply_delivery_amendment(
        sibling_plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal.source_plan_path == ".sikula/delivery/demo/plan.yaml"
    assert payload["source_plan_path"] == proposal.source_plan_path
    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_plan_mismatch"]
    assert sibling_plan_path.read_bytes() == sibling_before
    assert not delivery_events_path(tmp_path, "amend-demo").exists()


def test_preview_rejects_proposal_content_that_no_longer_matches_its_id(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["replacement_units"][0]["title"] = "Tampered title"
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_fingerprint_mismatch"]


def test_preview_rejects_unsupported_proposal_version(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_version"]


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda payload: payload.update(unexpected=True), "delivery_amend.proposal_invalid"),
        (lambda payload: payload.pop("source_plan_path"), "delivery_amend.proposal_invalid"),
        (lambda payload: payload.pop("target_task_fingerprint"), "delivery_amend.proposal_invalid"),
        (lambda payload: payload.update(proposal_id="different-id"), "delivery_amend.proposal_id_mismatch"),
        (
            lambda payload: payload.update(replacement_units=payload["replacement_units"][:1]),
            "delivery_amend.proposal_invalid",
        ),
        (
            lambda payload: payload["replacement_units"].__setitem__(
                1, {**payload["replacement_units"][1], "id": payload["replacement_units"][0]["id"]}
            ),
            "delivery_amend.replacement_duplicate",
        ),
        (
            lambda payload: payload["replacement_units"][0].update(unexpected=True),
            "delivery_amend.proposal_invalid",
        ),
        (
            lambda payload: payload["replacement_units"][0].update(budget={"max_planner_steps": 0}),
            "delivery_amend.proposal_invalid",
        ),
        (lambda payload: payload.update(amend_reason="free form reason"), "delivery_amend.proposal_invalid"),
        (
            lambda payload: payload.update(budget_exceeded={"name": "raw budget details", "limit": 2, "actual": 5}),
            "delivery_amend.budget_invalid",
        ),
    ],
)
def test_preview_rejects_malformed_proposal_shapes(
    tmp_path: Path,
    mutate,
    error_code: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    mutate(payload)
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.valid is False
    assert [issue.code for issue in result.errors] == [error_code]


def test_preview_rejects_non_object_proposal_and_invalid_plan(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    proposal_path.write_text("[]\n", encoding="utf-8")

    malformed = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )
    invalid_plan = preview_delivery_amendment(
        tmp_path / "missing-plan.yaml",
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert [issue.code for issue in malformed.errors] == ["delivery_amend.proposal_invalid"]
    assert invalid_plan.valid is False
    assert invalid_plan.errors[0].code == "plan.missing"


def test_preview_rejects_stale_progress_fingerprint(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"][2]["updated_at"] = "2026-07-20T11:00:00Z"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.progress_stale"]


def test_preview_rejects_stale_target_task_fingerprint(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    target_task = plan_path.parent / "units" / "c.md"
    target_task.write_text(target_task.read_text(encoding="utf-8") + "\n- Added requirement.\n", encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.target_task_stale"]


def test_preview_rejects_changed_parent_missing_retained_contract(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    retained_contract = plan_path.parent / "units" / "a.md"
    retained_contract.write_bytes(retained_contract.read_bytes() + b"\n- Added after prepare.\n")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.retained_contract_stale"]
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_mutating_apply_records_failed_event_for_stale_progress(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"][2]["updated_at"] = "2026-07-20T11:00:00Z"
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.progress_stale"]
    assert plan_path.read_bytes() == plan_before
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]
    assert events[-1]["failure_code"] == "delivery_amend.progress_stale"


def test_mutating_apply_records_failed_event_for_stale_plan(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["title"] = "Changed after proposal"
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.plan_stale"]
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert events[-1]["failure_code"] == "delivery_amend.plan_stale"


def test_mutating_apply_records_failed_event_when_target_becomes_running(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
    target.update(
        status="running",
        child_task_id="task-c",
        started_at="2026-07-20T11:00:00Z",
        updated_at="2026-07-20T11:00:00Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.target_running"]
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]
    assert events[-1]["failure_code"] == "delivery_amend.target_running"


def test_repeated_apply_is_idempotent_without_duplicate_success_events(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    first = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    second = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert first.applied is True
    assert second.applied is True
    assert second.errors == []
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("plan.amended") == 1
    assert event_types.count("plan.amend_started") == 1


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_plan_blob_id",
        "retained_contract_fingerprints",
        "source_assembly_base_commit",
        "source_assembled_commit",
        "amended_plan_fingerprint",
    ],
)
def test_load_rejects_proposal_without_required_assembly_metadata(tmp_path: Path, missing_field: str) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload.pop(missing_field)
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        delivery_amendment_module.load_delivery_amendment_proposal(
            proposal_path,
            expected_proposal_id=proposal.proposal_id,
            project_root=tmp_path,
        )

    assert exc_info.value.issue.code == "delivery_amend.proposal_invalid"


def test_apply_normalizes_and_deduplicates_shared_contract_paths(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    plan_data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    shared_path = plan_data["units"][0]["task_path"]
    plan_data["units"][-1]["task_path"] = f"./{shared_path}"
    plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert result.applied is True
    assembled_commit = json.loads(delivery_progress_path(tmp_path, proposal.plan_id).read_text(encoding="utf-8"))[
        "assembled_commit"
    ]
    assert _commit_content_id(tmp_path, assembled_commit, shared_path) == _filtered_content_id(
        tmp_path, shared_path, (tmp_path / shared_path).read_bytes()
    )


def test_amendment_anchors_legacy_branch_behind_head_to_head(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    stale_branch_commit = progress["units"][0]["commit"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", stale_branch_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        _draft(),
        project_root=tmp_path,
        proposal_root=proposal_root,
    )
    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert proposal.source_assembly_base_commit == head
    assert proposal.source_assembled_commit == head
    assert result.applied is True
    amended_commit = subprocess.run(
        ["git", "rev-parse", "sikula/delivery/amend-demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        subprocess.run(
            ["git", "rev-parse", f"{amended_commit}^"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == head
    )


def test_amendment_rejects_unrecorded_final_branch_with_unrelated_history(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    empty_tree = subprocess.run(
        ["git", "mktree"],
        cwd=tmp_path,
        check=True,
        input="",
        capture_output=True,
        text=True,
    ).stdout.strip()
    unrelated_commit = subprocess.run(
        _git_with_identity("commit-tree", empty_tree, "-m", "unrelated"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", unrelated_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "delivery.assembly_branch_diverged"
    assert not list(proposal_root.rglob("*.json"))


def test_apply_preserves_unresolved_assembly_failure(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        _draft(),
        project_root=tmp_path,
        proposal_root=proposal_root,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", head],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress.update(
        assembly_status="failed",
        assembly_base_commit=head,
        assembled_commit=head,
        assembly_unit_id="b",
        assembly_error_code="delivery.assembly_conflict",
        assembly_updated_at="2026-07-20T10:04:00Z",
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    progress_before = progress_path.read_bytes()
    plan_before = plan_path.read_bytes()

    result = apply_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_conflict"]
    assert progress_path.read_bytes() == progress_before
    assert plan_path.read_bytes() == plan_before


@pytest.mark.parametrize(
    ("target_status", "error_code"),
    [("done", "delivery_amend.target_done"), ("running", "delivery_amend.target_running")],
)
def test_done_and_running_units_cannot_be_split(
    tmp_path: Path,
    target_status: str,
    error_code: str,
) -> None:
    plan_path, _, _ = _setup(tmp_path, target_status=target_status)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    assert exc_info.value.issue.code == error_code


@pytest.mark.parametrize(
    ("target_status", "error_code"),
    [
        ("done", "amendment.completed_unit_superseded"),
        ("running", "amendment.running_unit_superseded"),
        ("waiting", "amendment.unsafe_unit_superseded"),
        ("canceled", "amendment.unsafe_unit_superseded"),
    ],
)
def test_status_rejects_manually_superseded_unsafe_unit_state(
    tmp_path: Path,
    target_status: str,
    error_code: str,
) -> None:
    plan_path, _, _ = _setup(tmp_path, target_status=target_status)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    target = next(unit for unit in plan["units"] if unit["id"] == "c")
    downstream = next(unit for unit in plan["units"] if unit["id"] == "d")
    replacement_path = plan_path.parent / "units" / "c-1.md"
    replacement_path.write_text(_task_markdown("C1"), encoding="utf-8")
    target["superseded_by"] = ["c-1"]
    replacement = {
        "id": "c-1",
        "title": "C1",
        "task_path": replacement_path.relative_to(tmp_path).as_posix(),
        "depends_on": ["b"],
        "supersedes": "c",
    }
    plan["units"].insert(plan["units"].index(target) + 1, replacement)
    downstream["depends_on"] = ["c-1"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

    status = get_delivery_status(plan_path, project_root=tmp_path)

    assert status.valid is False
    assert [issue.code for issue in status.errors] == [error_code]


@pytest.mark.parametrize(
    "lineage",
    [
        {"c": ("c", ["c"])},
        {"c": ("d", ["d"]), "d": ("c", ["c"])},
    ],
)
def test_plan_check_rejects_supersession_lineage_cycles(
    tmp_path: Path,
    lineage: dict[str, tuple[str, list[str]]],
) -> None:
    plan_path, _, _ = _setup(tmp_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    by_id = {unit["id"]: unit for unit in plan["units"]}
    for unit_id, (supersedes, superseded_by) in lineage.items():
        by_id[unit_id]["supersedes"] = supersedes
        by_id[unit_id]["superseded_by"] = superseded_by
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

    result = check_delivery_plan_file(plan_path, project_root=tmp_path)

    assert result.valid is False
    assert "amendment.supersession_cycle" in {issue.code for issue in result.errors}


def test_plan_check_rejects_replacement_root_without_target_prerequisites(tmp_path: Path) -> None:
    plan_path, _, _ = _setup(tmp_path)
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    by_id = {unit["id"]: unit for unit in plan["units"]}
    target = by_id["c"]
    target["superseded_by"] = ["c-1", "c-2"]
    replacements = [
        {
            "id": "c-1",
            "title": "C1",
            "task_path": ".sikula/delivery/demo/units/c-1.md",
            "depends_on": [],
            "supersedes": "c",
        },
        {
            "id": "c-2",
            "title": "C2",
            "task_path": ".sikula/delivery/demo/units/c-2.md",
            "depends_on": ["c-1"],
            "supersedes": "c",
        },
    ]
    for replacement in replacements:
        (tmp_path / replacement["task_path"]).write_text(_task_markdown(replacement["title"]), encoding="utf-8")
    target_index = plan["units"].index(target)
    plan["units"][target_index + 1 : target_index + 1] = replacements
    by_id["d"]["depends_on"] = ["c-2"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

    result = check_delivery_plan_file(plan_path, project_root=tmp_path)

    assert result.valid is False
    assert "amendment.replacement_dependencies_invalid" in {issue.code for issue in result.errors}


def test_failed_target_child_state_remains_inspectable_after_supersession(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    progress_before = json.loads(progress_path.read_text(encoding="utf-8"))
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    progress_after = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress_after["units"] == progress_before["units"]
    assert progress_after["assembly_status"] == "ready"
    target = next(unit for unit in get_delivery_status(plan_path).units if unit.id == "c")
    assert target.status == "superseded"
    assert target.child_task_id == "task-c"
    assert target.failure_code == "planner_failed"
    assert target.branch == "sikula/task-c"


def test_split_preserves_superseded_historical_dependencies(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["units"] = [unit for unit in progress["units"] if unit["unit_id"] == "a"]
    progress_path.write_text(json.dumps(progress), encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    by_id = {unit["id"]: unit for unit in plan["units"]}
    historical_target = by_id["c"]
    historical_target["superseded_by"] = ["c-old-1", "c-old-2"]
    old_replacements = [
        {
            "id": "c-old-1",
            "title": "Old C1",
            "task_path": ".sikula/delivery/demo/units/c-old-1.md",
            "depends_on": ["b"],
            "supersedes": "c",
        },
        {
            "id": "c-old-2",
            "title": "Old C2",
            "task_path": ".sikula/delivery/demo/units/c-old-2.md",
            "depends_on": ["c-old-1"],
            "supersedes": "c",
        },
    ]
    for replacement in old_replacements:
        (tmp_path / replacement["task_path"]).write_text(_task_markdown(replacement["title"]), encoding="utf-8")
    target_index = plan["units"].index(historical_target)
    plan["units"][target_index + 1 : target_index + 1] = old_replacements
    by_id["d"]["depends_on"] = ["c-old-2"]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    draft = DeliveryAmendmentAuthoringDraft(
        plan_id="amend-demo",
        target_unit_id="b",
        replacement_units=[
            DeliveryAuthoringUnitDraft("b-1", "B1", [], _task_markdown("B1")),
            DeliveryAuthoringUnitDraft("b-2", "B2", ["b-1"], _task_markdown("B2")),
        ],
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "b", draft, project_root=tmp_path, proposal_root=proposal_root
    )

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is True
    assert result.rewired_unit_ids == ["c-old-1"]
    amended = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    amended_by_id = {unit["id"]: unit for unit in amended["units"]}
    assert amended_by_id["c"]["depends_on"] == ["b"]
    assert amended_by_id["c-old-1"]["depends_on"] == ["b-2"]


def test_amend_parser_exposes_agent_overrides_only_for_prepare() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    prepare = parser.parse_args(
        [
            "delivery",
            "amend",
            "prepare",
            "plan.yaml",
            "--split-unit",
            "c",
            "--agent-model",
            "delivery_preparer=model",
        ]
    )
    apply = parser.parse_args(["delivery", "amend", "apply", "plan.yaml", "--proposal", "proposal-id", "--dry-run"])

    assert prepare.delivery_command == "amend"
    assert prepare.delivery_amend_command == "prepare"
    assert prepare.agent_model == ["delivery_preparer=model"]
    assert apply.delivery_amend_command == "apply"
    assert apply.dry_run is True
    assert not hasattr(apply, "agent_model")
    assert not hasattr(apply, "agent_provider")
    assert not hasattr(apply, "agent_timeout")


@pytest.mark.parametrize(
    ("argv", "command_name"),
    [
        (
            ["sikula", "delivery", "amend", "prepare", "plan.yaml", "--split-unit", "c"],
            "cmd_delivery_amend_prepare",
        ),
        (
            ["sikula", "delivery", "amend", "apply", "plan.yaml", "--proposal", "proposal-id", "--dry-run"],
            "cmd_delivery_amend_apply",
        ),
    ],
)
def test_main_dispatches_delivery_amend_commands_through_runtime_config(
    argv: list[str],
    command_name: str,
) -> None:
    cfg = {"project": {"root_path": "/project"}}
    with (
        patch("sys.argv", argv),
        patch("sikula._load_runtime_config", return_value=cfg) as load_config,
        patch(f"sikula.{command_name}") as command,
    ):
        sikula.main()

    load_config.assert_called_once_with(None, required=True)
    command.assert_called_once()
    args, called_cfg = command.call_args.args
    assert called_cfg is cfg
    assert args.delivery_command == "amend"


def test_main_amend_commands_delegate_to_delivery_cli() -> None:
    args = argparse.Namespace()
    cfg = {"project": {"root_path": "/project"}}

    with (
        patch("sikula.cli_delivery.cmd_delivery_amend_prepare", return_value="prepared") as prepare,
        patch("sikula.cli_delivery.cmd_delivery_amend_apply", return_value="applied") as apply,
    ):
        assert sikula.cmd_delivery_amend_prepare(args, cfg) == "prepared"
        assert sikula.cmd_delivery_amend_apply(args, cfg) == "applied"

    prepare.assert_called_once()
    apply.assert_called_once_with(args, cfg)


def test_amend_prepare_external_failure_returns_follow_up_without_proposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="external_dependency_gap",
    )
    calls = []

    def author(**kwargs):
        calls.append(kwargs)
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["prepared"] is False
    assert payload["proposal_id"] is None
    assert payload["proposal_path"] is None
    assert payload["recommended_action"] == "external_dependency_follow_up"
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.external_dependency_follow_up_required"]
    assert calls == []
    assert not list(proposal_root.rglob("*.json"))
    serialized = json.dumps(payload)
    assert "PRIVATE SOURCE TASK BODY" not in serialized
    assert "PRIVATE REVIEW OUTPUT" not in serialized
    assert str(tmp_path) not in serialized


def test_amend_prepare_scope_stop_uses_evidence_and_can_publish_valid_proposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )
    evidence_seen = None

    def author(**kwargs):
        nonlocal evidence_seen
        evidence_seen = kwargs["failure_evidence"]
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert payload["prepared"] is True
    assert payload["proposal_id"] is not None
    assert payload["recommended_action"] is None
    assert evidence_seen is not None
    assert evidence_seen.failure_code == "scope_amendment_required"
    assert len(list(proposal_root.rglob("*.json"))) == 1


def test_amend_prepare_uses_winning_scope_violation_over_stale_external_disposition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="unit_scope_violation",
    )
    state.delivery_stop_disposition = {
        "schema_version": 1,
        "disposition": "external_dependency_gap",
        "summary": "The implementer first reported an external dependency.",
        "recommended_action": "external_dependency_follow_up",
        "source": "implementer",
        "timestamp": "2026-07-20T10:03:00Z",
    }
    JsonStateStore(tmp_path / ".sikula" / "state").save(state)
    evidence_seen = None

    def author(**kwargs):
        nonlocal evidence_seen
        evidence_seen = kwargs["failure_evidence"]
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert payload["prepared"] is True
    assert payload["recommended_action"] is None
    assert evidence_seen is not None
    assert evidence_seen.failure_code == "unit_scope_violation"
    assert evidence_seen.stop_disposition is None
    assert evidence_seen.requires_external_follow_up is False


def test_amend_prepare_scope_stop_can_return_external_follow_up_without_proposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )

    def author(**_kwargs):
        return replace(
            _draft(),
            replacement_units=[],
            disposition="external_dependency_follow_up_required",
            summary="The required protocol change remains externally owned.",
        )

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["recommended_action"] == "external_dependency_follow_up"
    assert payload["proposal_id"] is None
    assert not list(proposal_root.rglob("*.json"))


def test_amend_prepare_rejects_child_evidence_changed_during_authoring(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path, target_status="failed")
    state = _write_terminal_child_state(
        tmp_path,
        plan_path,
        progress_path,
        failure_code="scope_amendment_required",
    )

    def author(**_kwargs):
        state.files_changed.append("src/raced.py")
        JsonStateStore(tmp_path / ".sikula" / "state").save(state)
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.authoring_inputs_stale"]
    assert not list(proposal_root.rglob("*.json"))


def test_amend_prepare_cli_stores_allowlisted_proposal_projection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    calls = []

    def author(**kwargs):
        calls.append(kwargs)
        draft = _draft()
        setattr(draft, "audit_path", ".sikula/contract-reports/amend.auto-llm.jsonl")
        return draft

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert payload["prepared"] is True
    assert payload["target_unit_id"] == "c"
    assert payload["replacement_ids"] == ["c-1", "c-2", "c-3"]
    assert payload["proposal_path"].startswith(".sikula/contract-reports/delivery-amendments/amend-demo/")
    assert payload["audit_path"] == ".sikula/contract-reports/amend.auto-llm.jsonl"
    assert "task_markdown" not in json.dumps(payload)
    assert "component_ids" not in payload
    assert "components" not in payload
    assert len(calls) == 1
    assert calls[0]["target"].target.id == "c"
    assert not delivery_events_path(tmp_path, "amend-demo").exists()


def test_amend_prepare_cli_reports_authoring_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)

    def author(**kwargs):
        return replace(_draft(), warnings=["Review the proposed split."])

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert [issue["code"] for issue in payload["warnings"]] == ["delivery_amend.authoring_warnings_present"]


def test_amend_prepare_cli_requires_main_command_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg)

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.authoring_context_missing"]


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        pytest.param(
            DeliveryAuthoringParseError("delivery_authoring.json_invalid", "Invalid JSON."),
            "delivery_amend.authoring_invalid",
            "Delivery amendment authoring returned an invalid proposal.",
            id="invalid_draft",
        ),
        pytest.param(
            RuntimeError("provider failed"),
            "delivery_amend.authoring_failed",
            "Delivery amendment proposal preparation failed.",
            id="provider_failure",
        ),
    ],
)
def test_amend_prepare_cli_projects_authoring_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_code: str,
    expected_message: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    setattr(error, "audit_path", str(proposal_root / "amend-audit.jsonl"))

    def author(**kwargs):
        raise error

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert [issue["code"] for issue in payload["errors"]] == [expected_code]
    assert payload["message"] == expected_message
    assert payload["audit_path"] == ".sikula/contract-reports/amend-audit.jsonl"


def test_amend_renderers_include_optional_details(tmp_path: Path) -> None:
    issue = DeliveryPlanIssue("warning", "delivery_amend.review", "Review the split.", "units/c.md")
    prepare = DeliveryAmendPrepareResult(
        plan_path=str(tmp_path / "plan.yaml"),
        project_root=str(tmp_path),
        status="ready",
        prepared=True,
        plan_id="amend-demo",
        target_unit_id="c",
        proposal_id="proposal-1",
        replacement_ids=["c-1", "c-2"],
        proposal_path=str(tmp_path / "proposal.json"),
        audit_path=str(tmp_path / "audit.jsonl"),
        errors=[DeliveryPlanIssue("error", "delivery_amend.example", "Example failure.")],
        warnings=[issue],
        message="Proposal prepared.",
    )
    apply = DeliveryAmendmentApplyResult(
        plan_path=str(tmp_path / "plan.yaml"),
        project_root=str(tmp_path),
        proposal_id="proposal-1",
        target_unit_id="c",
        replacement_ids=["c-1", "c-2"],
        rewired_unit_ids=["d"],
        dry_run=True,
        ready=True,
        applied=False,
        proposal_path=str(tmp_path / "proposal.json"),
        errors=[DeliveryPlanIssue("error", "delivery_amend.example", "Example failure.")],
        warnings=[issue],
        message="Ready to apply.",
    )

    prepare_text = render_delivery_amend_prepare(prepare)
    apply_text = render_delivery_amend_apply(apply)

    assert "Plan ID: amend-demo" in prepare_text
    assert "Proposal artifact: proposal.json" in prepare_text
    assert "Authoring audit: audit.jsonl" in prepare_text
    assert "Errors:" in prepare_text
    assert "Warnings:" in prepare_text
    assert "Status: ready" in apply_text
    assert "Rewired downstream units: d" in apply_text
    assert "Dry run: yes" in apply_text
    assert "Errors:" in apply_text
    assert "Warnings:" in apply_text


def test_main_amend_authoring_adapter_attaches_audit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    _add_target_constraint(tmp_path, plan_path)
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )
    snapshot = capture_delivery_amendment_source_snapshot(target)
    audit_path = proposal_root / "amend-audit.jsonl"

    class Agent:
        def author_delivery_amendment(self, **kwargs):
            assert kwargs["target_unit_id"] == "c"
            assert kwargs["downstream_units"][0]["id"] == "d"
            assert kwargs["component_ids"] == []
            assert kwargs["applicable_constraints"] == [
                {
                    "id": "protocol-authority",
                    "kind": "authoritative_read_only_dependency",
                    "summary": "Protocol changes remain owned by the external protocol project.",
                    "unit_ids": ["a", "c", "d"],
                    "disposition": "preserved",
                }
            ]
            assert kwargs["failure_evidence"] == {"failure_code": "unit_scope_violation"}
            kwargs["audit_recorder"]({"phase": "test"})
            return _draft()

    class FailureEvidence:
        def to_prompt_dict(self):
            return {"failure_code": "unit_scope_violation"}

    audit_records = []
    monkeypatch.setattr(sikula, "_create_delivery_preparation_agent", lambda args, cfg: Agent())
    monkeypatch.setattr(
        sikula,
        "_make_auto_preparation_audit_recorder",
        lambda **kwargs: (audit_records.append, audit_path),
    )

    draft = sikula._run_delivery_amend_prepare_authoring(
        args=argparse.Namespace(),
        cfg=_project_config(tmp_path),
        target=target,
        source_snapshot=snapshot,
        failure_evidence=FailureEvidence(),
    )

    assert draft.audit_path == ".sikula/contract-reports/amend-audit.jsonl"
    assert audit_records == [{"phase": "test"}]


def test_main_amend_authoring_adapter_projects_failure_evidence_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )
    snapshot = capture_delivery_amendment_source_snapshot(target)
    private_plan_id = str(tmp_path / "private-plan")
    private_target_id = str(tmp_path / "private-target")
    private_target_child_id = str(tmp_path / "private-target-child")
    private_constraint_id = str(tmp_path / "private-constraint")
    private_unit_id = str(tmp_path / "private-unit")
    private_child_id = str(tmp_path / "private-child")
    empty_review = DeliveryAmendmentReviewEvidence(0, 0, (), ())
    failure_evidence = DeliveryAmendmentFailureEvidence(
        plan_id=private_plan_id,
        unit_id=private_target_id,
        child_task_id=private_target_child_id,
        failure_code="unit_scope_violation",
        recommended_action="delivery_amend_prepare",
        inherited_constraints=(
            {
                "id": private_constraint_id,
                "kind": "authoritative_read_only_dependency",
                "summary": "Use the established contract without changing its owner.",
                "unit_ids": ["c", private_unit_id],
                "disposition": "preserved",
            },
        ),
        declared_write_paths=("src",),
        effective_write_paths=("src",),
        changed_paths=(),
        changed_count=0,
        omitted_changed_paths_count=0,
        violation_paths=(),
        violation_count=0,
        outside_project_paths=(),
        outside_project_count=0,
        omitted_outside_project_paths_count=0,
        reviewer=empty_review,
        security_reviewer=empty_review,
        stop_disposition=None,
        dependency_handoffs=(
            DeliveryAmendmentDependencyIdentity(
                plan_id="amend-demo",
                unit_id=private_unit_id,
                child_task_id=private_child_id,
            ),
        ),
        fingerprint=f"sha256:{'a' * 64}",
    )

    class Agent:
        def author_delivery_amendment(self, **kwargs):
            projected = kwargs["failure_evidence"]
            serialized = json.dumps(projected)
            assert str(tmp_path) not in serialized
            assert projected["plan_id"].startswith("<redacted:")
            assert projected["unit_id"].startswith("<redacted:")
            assert projected["child_task_id"].startswith("<redacted:")
            assert projected["inherited_constraints"][0]["unit_ids"][0] == "c"
            assert projected["inherited_constraints"][0]["unit_ids"][1].startswith("<redacted:")
            assert projected["inherited_constraints"][0]["id"].startswith("<redacted:")
            assert projected["dependency_handoffs"][0]["unit_id"].startswith("<redacted:")
            assert projected["dependency_handoffs"][0]["child_task_id"].startswith("<redacted:")
            return _draft()

    monkeypatch.setattr(sikula, "_create_delivery_preparation_agent", lambda args, cfg: Agent())
    monkeypatch.setattr(
        sikula,
        "_make_auto_preparation_audit_recorder",
        lambda **kwargs: (lambda record: None, proposal_root / "amend-audit.jsonl"),
    )

    raw_evidence = failure_evidence.to_dict()
    assert raw_evidence["plan_id"] == private_plan_id
    assert raw_evidence["unit_id"] == private_target_id
    assert raw_evidence["child_task_id"] == private_target_child_id
    assert raw_evidence["inherited_constraints"][0]["unit_ids"][1] == private_unit_id
    assert raw_evidence["dependency_handoffs"][0]["child_task_id"] == private_child_id

    sikula._run_delivery_amend_prepare_authoring(
        args=argparse.Namespace(),
        cfg=_project_config(tmp_path),
        target=target,
        source_snapshot=snapshot,
        failure_evidence=failure_evidence,
    )


def test_main_amend_authoring_adapter_preserves_raw_plan_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    legacy_metadata = {
        "stream": "/Users/alice/private-stream",
        "component": "/Users/alice/private-component",
        "phase": "/Users/alice/private-phase",
        "kind": "/Users/alice/private-kind",
        "platform": "/Users/alice/private-platform",
    }
    plan_data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan_data["streams"] = [legacy_metadata["stream"]]
    extra_component_id = "CaseSensitive.Component"
    plan_data["components"] = [
        {
            "id": legacy_metadata["component"],
            "path": ".",
            "stream": legacy_metadata["stream"],
        },
        {
            "id": extra_component_id,
            "path": "src",
            "stream": legacy_metadata["stream"],
        },
    ]
    for unit in plan_data["units"]:
        unit.update(legacy_metadata)
    plan_path.write_text(yaml.safe_dump(plan_data, sort_keys=False), encoding="utf-8")

    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )
    snapshot = capture_delivery_amendment_source_snapshot(target)
    audit_path = proposal_root / "amend-audit.jsonl"

    class Agent:
        def author_delivery_amendment(self, **kwargs):
            assert kwargs["component_ids"] == [legacy_metadata["component"], extra_component_id]
            for key, value in legacy_metadata.items():
                assert kwargs["target_unit"][key] == value
                assert kwargs["downstream_units"][0][key] == value
            return _draft()

    monkeypatch.setattr(sikula, "_create_delivery_preparation_agent", lambda args, cfg: Agent())
    monkeypatch.setattr(
        sikula,
        "_make_auto_preparation_audit_recorder",
        lambda **kwargs: (lambda record: None, audit_path),
    )

    draft = sikula._run_delivery_amend_prepare_authoring(
        args=argparse.Namespace(),
        cfg=_project_config(tmp_path),
        target=target,
        source_snapshot=snapshot,
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path,
        "c",
        draft,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
        expected_source_snapshot=snapshot,
    )

    for key, value in legacy_metadata.items():
        assert {getattr(unit, key) for unit in proposal.replacement_units} == {value}
    preview = preview_delivery_amendment(
        plan_path,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=_project_config(tmp_path),
    )
    assert preview.ready is True


def test_main_amend_authoring_adapter_attaches_audit_path_to_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    target = inspect_delivery_amendment_target(
        plan_path,
        "c",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )
    snapshot = capture_delivery_amendment_source_snapshot(target)
    audit_path = proposal_root / "amend-audit.jsonl"

    class Agent:
        def author_delivery_amendment(self, **kwargs):
            raise RuntimeError("provider failed")

    monkeypatch.setattr(sikula, "_create_delivery_preparation_agent", lambda args, cfg: Agent())
    monkeypatch.setattr(
        sikula,
        "_make_auto_preparation_audit_recorder",
        lambda **kwargs: (lambda record: None, audit_path),
    )

    with pytest.raises(RuntimeError) as exc_info:
        sikula._run_delivery_amend_prepare_authoring(
            args=argparse.Namespace(),
            cfg=_project_config(tmp_path),
            target=target,
            source_snapshot=snapshot,
        )

    assert exc_info.value.audit_path == ".sikula/contract-reports/amend-audit.jsonl"


def test_main_run_next_context_uses_shared_amend_authoring_adapter(tmp_path: Path) -> None:
    context = sikula._delivery_run_next_context(_project_config(tmp_path))

    assert context.run_amendment_authoring is sikula._run_delivery_amend_prepare_authoring


def test_amend_prepare_cli_reports_git_execution_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    author_called = False

    def author(**kwargs):
        nonlocal author_called
        author_called = True
        return _draft()

    def fail_git(*args, **kwargs):
        raise OSError("git executable unavailable")

    monkeypatch.setattr(worktree_module.subprocess, "run", fail_git)
    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.git_check_failed"]
    assert payload["prepared"] is False


def test_amendment_results_redact_outside_project_plan_paths(tmp_path: Path) -> None:
    outside_plan = tmp_path.parent / f"{tmp_path.name}-private" / "missing-plan.yaml"
    issue = DeliveryPlanIssue("error", "plan.missing", f"Plan file not found: {outside_plan}")
    prepare_result = DeliveryAmendPrepareResult(
        plan_path=str(outside_plan),
        project_root=str(tmp_path),
        status="blocked",
        prepared=False,
        plan_id=None,
        target_unit_id="c",
        errors=[issue],
        message="Delivery amendment proposal preparation is blocked.",
    )
    apply_result = preview_delivery_amendment(
        outside_plan,
        "unknown-proposal",
        project_root=tmp_path,
        proposal_root=tmp_path / ".sikula" / "contract-reports",
    )

    prepare_payload = prepare_result.to_dict()
    apply_payload = apply_result.to_dict()
    serialized = json.dumps({"prepare": prepare_payload, "apply": apply_payload})

    assert prepare_payload["plan_path"] is None
    assert apply_payload["plan_path"] is None
    assert str(outside_plan) not in serialized
    assert "<redacted>" in serialized


def test_amendment_results_project_unsafe_identity_references(tmp_path: Path) -> None:
    target_id = "/Users/example/private/target"
    replacement_id = r"C:\Users\example\private\replacement"
    prepare_result = DeliveryAmendPrepareResult(
        plan_path=str(tmp_path / "plan.yaml"),
        project_root=str(tmp_path),
        status="ready",
        prepared=True,
        plan_id="amend-demo",
        target_unit_id=target_id,
        replacement_ids=[replacement_id],
        errors=[
            DeliveryPlanIssue(
                "error",
                "amendment.target_unknown",
                f"Unknown target unit: {target_id}",
            )
        ],
        message=f"Prepared replacement for {target_id}.",
    )
    apply_result = DeliveryAmendmentApplyResult(
        plan_path=str(tmp_path / "plan.yaml"),
        project_root=str(tmp_path),
        proposal_id="proposal-1",
        target_unit_id=target_id,
        replacement_ids=[replacement_id],
        rewired_unit_ids=[target_id],
        dry_run=True,
        ready=True,
        applied=False,
        proposal_path=None,
        errors=[
            DeliveryPlanIssue(
                "error",
                "amendment.target_unknown",
                f"Unknown target unit: {target_id}",
            )
        ],
        warnings=[],
        message=f"Ready to replace {target_id}.",
    )

    prepare_payload = prepare_result.to_dict()
    apply_payload = apply_result.to_dict()
    prepare_text = render_delivery_amend_prepare(prepare_result)
    apply_text = render_delivery_amend_apply(apply_result)

    assert prepare_payload["target_unit_id"] == apply_payload["target_unit_id"]
    assert prepare_payload["replacement_ids"] == apply_payload["replacement_ids"]
    assert apply_payload["target_unit_id"] == apply_payload["rewired_unit_ids"][0]
    serialized = json.dumps({"prepare": prepare_payload, "apply": apply_payload})
    assert target_id not in serialized
    assert replacement_id not in serialized
    assert target_id not in prepare_text
    assert replacement_id not in prepare_text
    assert target_id not in apply_text
    assert replacement_id not in apply_text


@pytest.mark.parametrize("changed_input", ["plan", "target_task", "progress"])
def test_amend_prepare_cli_rejects_inputs_changed_during_authoring(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changed_input: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)

    def author(**kwargs):
        if changed_input == "plan":
            plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
            plan["title"] = "Changed while authoring"
            plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        elif changed_input == "target_task":
            task_path = plan_path.parent / "units" / "c.md"
            task_path.write_text(
                task_path.read_text(encoding="utf-8") + "\n- Added while authoring.\n",
                encoding="utf-8",
            )
        else:
            progress_path = delivery_progress_path(tmp_path, "amend-demo")
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            target_progress = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
            target_progress["updated_at"] = "2026-07-20T12:00:00Z"
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
        return _draft()

    args = argparse.Namespace(
        plan_file=str(plan_path),
        split_unit="c",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_prepare(args, cfg, _amend_context(tmp_path, author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["prepared"] is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.authoring_inputs_stale"]
    assert not list(proposal_root.rglob("*.json"))


def test_amend_apply_cli_dry_run_does_not_accept_or_invoke_agent_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    before = _snapshot(tmp_path)
    args = argparse.Namespace(
        plan_file=str(plan_path),
        proposal=proposal.proposal_id,
        dry_run=True,
        json=True,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    cmd_delivery_amend_apply(args, cfg)

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert _snapshot(tmp_path) == before


def test_amend_apply_cli_projects_lock_filesystem_failure_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    def fail_lock(*args, **kwargs):
        raise OSError("lock parent is not writable")

    monkeypatch.setattr(delivery_amendment_module, "acquire_delivery_progress_lock", fail_lock)
    args = argparse.Namespace(
        plan_file=str(plan_path),
        proposal=proposal.proposal_id,
        dry_run=False,
        json=True,
    )
    cfg = {
        **_project_config(tmp_path),
        "tasks": {"contract_report_dir": str(proposal_root)},
    }

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_amend_apply(args, cfg)

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["ready"] is False
    assert payload["applied"] is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.lock_failed"]
    assert payload["message"] == "Delivery plan amendment is blocked."


def test_preview_rejects_unknown_proposal_without_writing_state(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    before = _snapshot(tmp_path)

    result = preview_delivery_amendment(
        plan_path, "unknown-proposal", project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_unknown"]
    assert _snapshot(tmp_path) == before


def test_inspect_rejects_invalid_plan(tmp_path: Path) -> None:
    plan_path = tmp_path / "missing-plan.yaml"

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    assert exc_info.value.issue.code == "plan.missing"


@pytest.mark.parametrize("target_status", ["waiting", "canceled"])
def test_inspect_rejects_unsafe_target_status(tmp_path: Path, target_status: str) -> None:
    plan_path, _, _ = _setup(tmp_path, target_status=target_status)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "c", project_root=tmp_path)

    assert exc_info.value.issue.code == "delivery_amend.target_state_unsafe"


def test_proposal_path_rejects_unsafe_identifier(tmp_path: Path) -> None:
    with pytest.raises(DeliveryAmendmentError) as exc_info:
        delivery_amendment_proposal_path(tmp_path, "amend-demo", "../proposal")

    assert exc_info.value.issue.code == "delivery_amend.proposal_id_invalid"


def test_preview_rejects_malformed_proposal_artifact(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal_path = delivery_amendment_proposal_path(proposal_root, "amend-demo", "malformed")
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("{", encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path,
        "malformed",
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_invalid"]


def test_preview_rejects_replacement_task_created_after_prepare(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    replacement_path = tmp_path / proposal.replacement_units[0].task_path
    replacement_path.write_text("operator-created file\n", encoding="utf-8")

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.replacement_task_conflict"]


def test_prepare_rejects_symlinked_proposal_storage_directory(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal_root.mkdir(parents=True)
    outside = tmp_path / "outside-proposals"
    outside.mkdir()
    (proposal_root / "delivery-amendments").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root)

    assert exc_info.value.issue.code == "delivery_amend.proposal_path_symlink"
    assert list(outside.iterdir()) == []


def test_prepare_interruption_leaves_no_partial_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)

    def fail_fsync(*args, **kwargs):
        raise OSError("simulated proposal fsync failure")

    monkeypatch.setattr(delivery_amendment_module.os, "fsync", fail_fsync)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "delivery_amend.proposal_write_failed"
    assert not list(proposal_root.rglob("*.json"))
    assert not list(proposal_root.rglob("*.tmp"))


def test_prepare_atomic_publish_does_not_overwrite_concurrent_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    real_link = delivery_amendment_module.os.link
    concurrent_payload = "concurrent proposal\n"

    def publish_competing_proposal(source, destination):
        Path(destination).write_text(concurrent_payload, encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(delivery_amendment_module.os, "link", publish_competing_proposal)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    proposal_files = list(proposal_root.rglob("*.json"))
    assert exc_info.value.issue.code == "delivery_amend.proposal_exists"
    assert len(proposal_files) == 1
    assert proposal_files[0].read_text(encoding="utf-8") == concurrent_payload
    assert not list(proposal_root.rglob("*.tmp"))


def test_preview_rejects_symlinked_proposal_plan_directory(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, proposal_path = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    outside = tmp_path / "outside-plan-proposals"
    proposal_path.parent.rename(outside)
    proposal_path.parent.symlink_to(outside, target_is_directory=True)

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.proposal_path_symlink"]


@pytest.mark.parametrize(
    ("prefix", "forbidden_parent"),
    [
        ((), (".git",)),
        ((), (".sikula", "state")),
        ((), (".sikula", "worktrees")),
        ((), (".sikula", "contract-reports")),
        (("vendor", "tool"), (".git",)),
        (("vendor", "tool"), (".sikula", "state")),
        (("vendor", "tool"), (".sikula", "worktrees")),
        (("vendor", "tool"), (".sikula", "contract-reports")),
    ],
)
def test_amendment_rejects_runtime_and_vcs_plan_destinations(
    tmp_path: Path,
    prefix: tuple[str, ...],
    forbidden_parent: tuple[str, ...],
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    forbidden_plan = tmp_path.joinpath(*prefix, *forbidden_parent, "amend-plan.yaml")
    forbidden_plan.parent.mkdir(parents=True, exist_ok=True)
    forbidden_plan.write_bytes(plan_path.read_bytes())
    before = forbidden_plan.read_bytes()

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            forbidden_plan,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=proposal_root,
        )
    preview = preview_delivery_amendment(
        forbidden_plan,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )
    applied = apply_delivery_amendment(
        forbidden_plan,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
    )

    assert exc_info.value.issue.code == "delivery_amend.plan_destination_forbidden"
    assert [issue.code for issue in preview.errors] == ["delivery_amend.plan_destination_forbidden"]
    assert [issue.code for issue in applied.errors] == ["delivery_amend.plan_destination_forbidden"]
    assert applied.applied is False
    assert forbidden_plan.read_bytes() == before
    assert not (forbidden_plan.parent / "units" / "c-1.md").exists()


@pytest.mark.parametrize("config_key", ["state_dir", "contract_report_dir"])
def test_amendment_rejects_plan_under_configured_private_root(
    tmp_path: Path,
    config_key: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    private_dir = tmp_path / f"private-{config_key}"
    private_plan = private_dir / "plan.yaml"
    private_plan.parent.mkdir(parents=True)
    private_plan.write_bytes(plan_path.read_bytes())
    before = private_plan.read_bytes()
    cfg = {**_project_config(tmp_path), "tasks": {config_key: private_dir.relative_to(tmp_path).as_posix()}}

    preview = preview_delivery_amendment(
        private_plan,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=cfg,
    )
    applied = apply_delivery_amendment(
        private_plan,
        proposal.proposal_id,
        project_root=tmp_path,
        proposal_root=proposal_root,
        project_config=cfg,
    )

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery_amend.plan_destination_forbidden"]
    assert applied.applied is False
    assert [issue.code for issue in applied.errors] == ["delivery_amend.plan_destination_forbidden"]
    assert private_plan.read_bytes() == before


@pytest.mark.parametrize("config_key", ["state_dir", "contract_report_dir"])
def test_prepare_rejects_replacement_tasks_under_configured_private_root(
    tmp_path: Path,
    config_key: str,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    custom_dir = tmp_path / "custom"
    custom_plan = custom_dir / "plan.yaml"
    custom_plan.parent.mkdir(parents=True)
    custom_plan.write_bytes(plan_path.read_bytes())
    private_units_dir = custom_dir / "units"
    effective_proposal_root = private_units_dir if config_key == "contract_report_dir" else proposal_root
    cfg = {
        **_project_config(tmp_path),
        "tasks": {
            "contract_report_dir": str(effective_proposal_root),
            config_key: private_units_dir.relative_to(tmp_path).as_posix(),
        },
    }

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            custom_plan,
            "c",
            _draft(),
            project_root=tmp_path,
            proposal_root=effective_proposal_root,
            project_config=cfg,
        )

    assert exc_info.value.issue.code == "delivery_amend.replacement_task_forbidden"
    assert not private_units_dir.exists()
    assert not list(effective_proposal_root.rglob("*.json"))


def test_apply_blocks_when_delivery_lock_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    def fail_lock(*args, **kwargs):
        raise DeliveryProgressLockError("held")

    monkeypatch.setattr(delivery_amendment_module, "acquire_delivery_progress_lock", fail_lock)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.locked"]
    assert result.message == "Delivery plan amendment is blocked."


def test_apply_blocks_when_delivery_lock_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )

    def fail_lock(*args, **kwargs):
        raise OSError("read-only delivery state directory")

    monkeypatch.setattr(delivery_amendment_module, "acquire_delivery_progress_lock", fail_lock)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.lock_failed"]
    assert result.message == "Delivery plan amendment is blocked."


def test_apply_stops_before_artifact_writes_when_start_event_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()

    def fail_event(*args, **kwargs):
        raise OSError("simulated start event failure")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_event", fail_event)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.event_write_failed"]
    assert result.message == "Delivery plan amendment is blocked."
    assert plan_path.read_bytes() == plan_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)


def test_apply_surfaces_terminal_event_failure_with_artifact_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    real_append = delivery_amendment_module.append_delivery_progress_event
    append_count = 0

    def append_start_then_fail(path, event):
        nonlocal append_count
        append_count += 1
        if append_count == 2:
            raise OSError("simulated terminal event failure")
        real_append(path, event)

    def fail_artifact_write(*args, **kwargs):
        raise OSError("simulated artifact write failure")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_event", append_start_then_fail)
    monkeypatch.setattr(delivery_amendment_module, "_write_amended_artifacts", fail_artifact_write)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == [
        "delivery_amend.write_failed",
        "delivery_amend.event_write_failed",
    ]
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started"]


def test_apply_records_terminal_event_before_propagating_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()

    def interrupt_success_events(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(delivery_amendment_module, "_append_success_events", interrupt_success_events)

    with pytest.raises(KeyboardInterrupt):
        apply_delivery_amendment(
            plan_path,
            proposal.proposal_id,
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]
    assert events[-1]["failure_code"] == "delivery_amend.interrupted"


def test_apply_does_not_clobber_replacement_task_created_at_publish_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    concurrent_path = tmp_path / proposal.replacement_units[1].task_path
    concurrent_content = "concurrent operator task\n"
    real_publish = delivery_amendment_module._atomic_write_new

    def publish_with_race(path, content, *, mode, root):
        if path == concurrent_path:
            path.write_text(concurrent_content, encoding="utf-8")
        return real_publish(path, content, mode=mode, root=root)

    monkeypatch.setattr(delivery_amendment_module, "_atomic_write_new", publish_with_race)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.replacement_task_conflict"]
    assert plan_path.read_bytes() == plan_before
    assert concurrent_path.read_text(encoding="utf-8") == concurrent_content
    assert not (tmp_path / proposal.replacement_units[0].task_path).exists()
    assert not (tmp_path / proposal.replacement_units[2].task_path).exists()


def test_apply_rejects_replacement_parent_swapped_to_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    units_dir = plan_path.parent / "units"
    parked_dir = plan_path.parent / "units-parked"
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-units"
    outside_dir.mkdir()
    real_publish = delivery_amendment_module._atomic_write_new
    raced = False

    def publish_with_parent_race(path, content, *, mode, root):
        nonlocal raced
        if not raced:
            raced = True
            units_dir.rename(parked_dir)
            units_dir.symlink_to(outside_dir, target_is_directory=True)
        try:
            return real_publish(path, content, mode=mode, root=root)
        finally:
            if units_dir.is_symlink():
                units_dir.unlink()
                parked_dir.rename(units_dir)

    monkeypatch.setattr(delivery_amendment_module, "_atomic_write_new", publish_with_parent_race)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.replacement_task_unsafe"]
    assert plan_path.read_bytes() == plan_before
    assert not list(outside_dir.iterdir())
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)


def test_apply_compares_plan_inside_atomic_replace_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    real_replace = delivery_amendment_module._atomic_replace_if_unchanged

    def edit_then_replace(path, content, *, expected_fingerprint):
        plan = yaml.safe_load(path.read_text(encoding="utf-8"))
        plan["title"] = "Concurrent boundary edit"
        path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        return real_replace(path, content, expected_fingerprint=expected_fingerprint)

    monkeypatch.setattr(delivery_amendment_module, "_atomic_replace_if_unchanged", edit_then_replace)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.plan_stale"]
    assert yaml.safe_load(plan_path.read_text(encoding="utf-8"))["title"] == "Concurrent boundary edit"
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)


@pytest.mark.parametrize("changed_source", ["target_task", "progress"])
def test_apply_rechecks_non_plan_sources_after_plan_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_source: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    target_task = plan_path.parent / "units" / "c.md"
    real_replace = delivery_amendment_module._atomic_replace_if_unchanged

    def replace_then_edit(path, content, *, expected_fingerprint):
        result = real_replace(path, content, expected_fingerprint=expected_fingerprint)
        if changed_source == "target_task":
            target_task.write_text(
                target_task.read_text(encoding="utf-8") + "\n- Concurrent post-publish edit.\n",
                encoding="utf-8",
            )
        else:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
            target["updated_at"] = "2026-07-20T13:30:00Z"
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
        return result

    monkeypatch.setattr(delivery_amendment_module, "_atomic_replace_if_unchanged", replace_then_edit)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    expected_code = {
        "target_task": "delivery_amend.target_task_stale",
        "progress": "delivery_amend.progress_stale",
    }[changed_source]
    assert result.applied is False
    assert [issue.code for issue in result.errors] == [expected_code]
    assert plan_path.read_bytes() == plan_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)


def test_apply_rollback_preserves_concurrent_plan_edit_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    real_replace = delivery_amendment_module._atomic_replace_if_unchanged

    def replace_then_edit_plan(path, content, *, expected_fingerprint):
        result = real_replace(path, content, expected_fingerprint=expected_fingerprint)
        plan = yaml.safe_load(path.read_text(encoding="utf-8"))
        plan["title"] = "Concurrent edit after publish"
        path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        return result

    monkeypatch.setattr(delivery_amendment_module, "_atomic_replace_if_unchanged", replace_then_edit_plan)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.rollback_failed"]
    assert yaml.safe_load(plan_path.read_text(encoding="utf-8"))["title"] == "Concurrent edit after publish"
    assert all((tmp_path / unit.task_path).is_file() for unit in proposal.replacement_units)
    assert check_delivery_plan_file(plan_path, project_root=tmp_path).valid is True


def test_apply_rejects_modified_published_replacement_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    modified_path = tmp_path / proposal.replacement_units[0].task_path
    modified_content = "concurrent replacement edit\n"
    real_check = delivery_amendment_module._assert_published_artifacts_current

    def modify_then_check(artifacts, root):
        modified_path.write_text(modified_content, encoding="utf-8")
        return real_check(artifacts, root)

    monkeypatch.setattr(delivery_amendment_module, "_assert_published_artifacts_current", modify_then_check)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.rollback_failed"]
    assert plan_path.read_bytes() == plan_before
    assert modified_path.read_text(encoding="utf-8") == modified_content
    events = [json.loads(line) for line in delivery_events_path(tmp_path, proposal.plan_id).read_text().splitlines()]
    assert "plan.amended" not in [event["event_type"] for event in events]


@pytest.mark.parametrize("changed_source", ["plan", "target_task", "progress"])
def test_apply_rechecks_source_fingerprints_at_write_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_source: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    target_task = plan_path.parent / "units" / "c.md"
    real_write = delivery_amendment_module._write_amended_artifacts

    def edit_source_then_write(*args, **kwargs):
        if changed_source == "plan":
            plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
            plan["title"] = "Concurrent plan edit"
            plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        elif changed_source == "target_task":
            target_task.write_text(
                target_task.read_text(encoding="utf-8") + "\n- Concurrent task edit.\n",
                encoding="utf-8",
            )
        else:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
            target["updated_at"] = "2026-07-20T12:30:00Z"
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(delivery_amendment_module, "_write_amended_artifacts", edit_source_then_write)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    expected_code = {
        "plan": "delivery_amend.plan_stale",
        "target_task": "delivery_amend.target_task_stale",
        "progress": "delivery_amend.progress_stale",
    }[changed_source]
    assert result.applied is False
    assert [issue.code for issue in result.errors] == [expected_code]
    assert result.message == "Delivery plan amendment is blocked."
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    if changed_source == "plan":
        assert yaml.safe_load(plan_path.read_text(encoding="utf-8"))["title"] == "Concurrent plan edit"
    elif changed_source == "target_task":
        assert target_task.read_text(encoding="utf-8").endswith("- Concurrent task edit.\n")
    else:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        target = next(unit for unit in progress["units"] if unit["unit_id"] == "c")
        assert target["updated_at"] == "2026-07-20T12:30:00Z"
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]
    assert events[-1]["failure_code"] == expected_code


def test_apply_restores_tracked_artifacts_when_success_event_batch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()

    def fail_events(*args, **kwargs):
        raise OSError("simulated event write failure")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", fail_events)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.write_failed"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((plan_path.parent / "units" / f"{unit_id}.md").exists() for unit_id in proposal.replacement_ids)
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]


def test_apply_restores_legacy_branch_behind_parent_when_success_events_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    previous_branch_commit = progress["units"][0]["commit"]
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", previous_branch_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()

    def fail_events(*args, **kwargs):
        raise OSError("simulated event write failure")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", fail_events)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.write_failed"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == previous_branch_commit
    )


def test_recovery_restores_branch_progress_and_events_when_event_reconciliation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    _publish_amendment_locally(plan_path, tmp_path, proposal)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    previous_branch_commit = progress["units"][0]["commit"]
    subprocess.run(
        ["git", "branch", "sikula/delivery/amend-demo", previous_branch_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    progress_before = progress_path.read_bytes()
    events_path = delivery_events_path(tmp_path, "amend-demo")

    def fail_events(*args, **kwargs):
        raise OSError("simulated event reconciliation failure")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", fail_events)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.event_write_failed"]
    assert progress_path.read_bytes() == progress_before
    assert not events_path.exists()
    assert (
        subprocess.run(
            ["git", "rev-parse", "sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == previous_branch_commit
    )


def test_apply_rolls_back_when_final_branch_changes_after_artifact_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()
    monkeypatch.setattr(delivery_amendment_module, "delivery_branch_commit", lambda *_args: None)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_diverged"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/amend-demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_apply_restores_local_artifacts_when_assembly_ref_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()
    monkeypatch.setattr(delivery_amendment_module, "delivery_branch_commit", lambda *_args: None)
    monkeypatch.setattr(delivery_amendment_module, "rollback_delivery_artifacts", lambda *_args, **_kwargs: False)

    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.rollback_failed"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]


def test_apply_removes_written_success_events_when_event_flush_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    events_path = delivery_events_path(tmp_path, "amend-demo")
    events_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "amend-demo",
                "event_type": "unit.done",
                "timestamp": "2026-07-20T10:01:30Z",
                "unit_id": "b",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    events_before = events_path.read_bytes()
    real_append = delivery_amendment_module.append_delivery_progress_events

    def append_then_fail(path, events):
        real_append(path, events)
        raise OSError("simulated flush failure after event write")

    monkeypatch.setattr(delivery_amendment_module, "append_delivery_progress_events", append_then_fail)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.write_failed"]
    assert plan_path.read_bytes() == plan_before
    assert events_path.read_bytes().startswith(events_before)
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["unit.done", "plan.amend_started", "plan.amend_failed"]


def test_apply_restores_tracked_artifacts_after_partial_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()
    real_atomic_write_new = delivery_amendment_module._atomic_write_new
    write_count = 0

    def fail_third_write(path, content, *, mode, root):
        nonlocal write_count
        write_count += 1
        if write_count == 3:
            raise OSError("simulated partial write failure")
        return real_atomic_write_new(path, content, mode=mode, root=root)

    monkeypatch.setattr(delivery_amendment_module, "_atomic_write_new", fail_third_write)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.write_failed"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]


def test_apply_restores_tracked_artifacts_when_final_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    plan_before = plan_path.read_bytes()
    progress_before = progress_path.read_bytes()
    real_check = delivery_amendment_module.check_delivery_plan_file
    check_count = 0

    def fail_final_check(*args, **kwargs):
        nonlocal check_count
        check_count += 1
        checked = real_check(*args, **kwargs)
        if check_count == 3:
            return replace(
                checked,
                errors=[
                    *checked.errors,
                    delivery_amendment_module.DeliveryPlanIssue(
                        "error", "test.final_validation", "Simulated final validation failure."
                    ),
                ],
            )
        return checked

    monkeypatch.setattr(delivery_amendment_module, "check_delivery_plan_file", fail_final_check)
    result = apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert check_count == 3
    assert result.applied is False
    assert [issue.code for issue in result.errors] == ["delivery_amend.final_validation_failed"]
    assert plan_path.read_bytes() == plan_before
    assert progress_path.read_bytes() == progress_before
    assert not any((tmp_path / unit.task_path).exists() for unit in proposal.replacement_units)
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["plan.amend_started", "plan.amend_failed"]


def test_run_next_after_amendment_without_existing_progress(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress_path.unlink()
    draft = DeliveryAmendmentAuthoringDraft(
        plan_id="amend-demo",
        target_unit_id="a",
        replacement_units=[
            DeliveryAuthoringUnitDraft("a-1", "A1", [], _task_markdown("A1")),
            DeliveryAuthoringUnitDraft("a-2", "A2", ["a-1"], _task_markdown("A2")),
        ],
    )
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "a", draft, project_root=tmp_path, proposal_root=proposal_root
    )
    assert apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    ).applied
    assert progress_path.exists() is True
    amendment_commit = json.loads(progress_path.read_text(encoding="utf-8"))["assembled_commit"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    state_dir = tmp_path / ".sikula" / "state"

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
        assert run_args.worktree_start_ref == amendment_commit
        store = JsonStateStore(state_dir)
        state = store.create("replacement child")
        store.save(state)
        run_args.created_task_id = state.task_id
        run_args.delivery_child_created_callback(state.task_id)
        state.done = True
        state.worktree_branch = "sikula/a-1-child"
        state.result_commit = head
        store.save(state)
        return DeliveryChildRunResult(exit_code=0, child_task_id=state.task_id)

    args = argparse.Namespace(
        plan_file=str(plan_path),
        dry_run=False,
        reset_failed=False,
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    cfg = {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "tasks": {"state_dir": str(state_dir)},
    }
    context = DeliveryRunNextContext(
        run_task=runner,
        resolve_state_dir=lambda _: state_dir,
        state_store=JsonStateStore(state_dir),
    )

    cmd_delivery_run_next(args, cfg, context)

    payload = json.loads(capsys.readouterr().out)
    assert payload["succeeded"] is True
    assert payload["selected_unit"]["id"] == "a-1"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert [unit["unit_id"] for unit in progress["units"]] == ["a-1"]
    assert progress["units"][0]["status"] == "done"


def test_run_next_and_finalize_use_effective_amended_graph(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    proposal, _ = create_delivery_amendment_proposal(
        plan_path, "c", _draft(), project_root=tmp_path, proposal_root=proposal_root
    )
    assert apply_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    ).applied

    run_next = preview_delivery_run_next(plan_path, project_root=tmp_path)
    assert run_next.ready is True
    assert run_next.selected_unit is not None
    assert run_next.selected_unit.id == "c-1"

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    progress["units"].extend(
        {"unit_id": unit_id, "status": "done", "commit": head} for unit_id in ["c-1", "c-2", "c-3", "d"]
    )
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    status = get_delivery_status(plan_path, project_root=tmp_path)
    assert status.status == "done"
    assert next(unit for unit in status.units if unit.id == "c").status == "superseded"
    finalize = preview_delivery_finalize(plan_path, project_root=tmp_path)
    assert finalize.ready is True
    assert finalize.final_commit == progress["assembled_commit"]
