from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
from unittest.mock import patch

import pytest
import yaml

import core.delivery_amendment as delivery_amendment_module
import sikula
from core.delivery_amendment import (
    DeliveryAmendmentApplyResult,
    DeliveryAmendmentError,
    apply_delivery_amendment,
    capture_delivery_amendment_source_snapshot,
    create_delivery_amendment_proposal,
    delivery_amendment_proposal_path,
    inspect_delivery_amendment_target,
    preview_delivery_amendment,
)
from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
)
from core.delivery_finalize import preview_delivery_finalize
from core.delivery_plan import DeliveryBudgetExceeded, DeliveryPlanIssue, check_delivery_plan_file
from core.delivery_progress import (
    DeliveryProgressLockError,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    render_delivery_status,
)
from core.delivery_run_next import preview_delivery_run_next
from core.state import JsonStateStore
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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_pending_middle_split_preserves_progress_and_rewires_to_all_leaves(tmp_path: Path) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    progress_before = progress_path.read_bytes()
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
    assert progress_path.read_bytes() == progress_before
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


def test_prepare_rejects_unknown_target(tmp_path: Path) -> None:
    plan_path, _, _ = _setup(tmp_path)

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        inspect_delivery_amendment_target(plan_path, "missing", project_root=tmp_path)

    assert exc_info.value.issue.code == "delivery_amend.target_unknown"


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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert author_called is False
    assert [issue["code"] for issue in payload["errors"]] == ["delivery_amend.target_task_forbidden"]
    assert "configured private task content" not in json.dumps(payload)


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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author_with_race))

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


@pytest.mark.parametrize("changed_source", ["plan", "target_task", "progress"])
def test_prepare_rechecks_source_fingerprints_at_publish_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_source: str,
) -> None:
    plan_path, progress_path, proposal_root = _setup(tmp_path)
    target_task = plan_path.parent / "units" / "c.md"
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


def test_prepare_rejects_invalid_amended_plan_before_publishing_proposal(tmp_path: Path) -> None:
    plan_path, _, proposal_root = _setup(tmp_path)
    draft = _draft()
    draft.replacement_units[0] = replace(draft.replacement_units[0], stream="unknown-stream")

    with pytest.raises(DeliveryAmendmentError) as exc_info:
        create_delivery_amendment_proposal(
            plan_path,
            "c",
            draft,
            project_root=tmp_path,
            proposal_root=proposal_root,
        )

    assert exc_info.value.issue.code == "units.stream_unknown"
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

    result = preview_delivery_amendment(
        plan_path, proposal.proposal_id, project_root=tmp_path, proposal_root=proposal_root
    )

    assert result.ready is True
    assert result.dry_run is True
    assert result.applied is False
    assert _snapshot(tmp_path) == before
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


def test_repeated_apply_records_failure_without_duplicate_success_events(tmp_path: Path) -> None:
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
    assert second.applied is False
    assert [issue.code for issue in second.errors] == ["delivery_amend.target_superseded"]
    events = [json.loads(line) for line in delivery_events_path(tmp_path, "amend-demo").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("plan.amended") == 1
    assert event_types[-2:] == ["plan.amend_started", "plan.amend_failed"]
    assert events[-1]["failure_code"] == "delivery_amend.target_superseded"


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
    assert json.loads(progress_path.read_text(encoding="utf-8")) == progress_before
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

    cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

    payload = json.loads(capsys.readouterr().out)
    assert payload["prepared"] is True
    assert payload["target_unit_id"] == "c"
    assert payload["replacement_ids"] == ["c-1", "c-2", "c-3"]
    assert payload["proposal_path"].startswith(".sikula/contract-reports/delivery-amendments/amend-demo/")
    assert payload["audit_path"] == ".sikula/contract-reports/amend.auto-llm.jsonl"
    assert "task_markdown" not in json.dumps(payload)
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

    cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

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
            kwargs["audit_recorder"]({"phase": "test"})
            return _draft()

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
    )

    assert draft.audit_path == ".sikula/contract-reports/amend-audit.jsonl"
    assert audit_records == [{"phase": "test"}]


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

    monkeypatch.setattr(delivery_amendment_module.subprocess, "run", fail_git)
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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

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
        cmd_delivery_amend_prepare(args, cfg, DeliveryAmendPrepareContext(author))

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
    assert progress_path.exists() is False
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    state_dir = tmp_path / ".sikula" / "state"

    def runner(run_args: argparse.Namespace, run_cfg: dict) -> DeliveryChildRunResult:
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
    context = DeliveryRunNextContext(run_task=runner, resolve_state_dir=lambda _: state_dir)

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
    assert finalize.final_commit == head
