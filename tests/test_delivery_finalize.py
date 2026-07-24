from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

import core.delivery_finalize as delivery_finalize_module
from core.delivery_plan import DeliveryPlanIssue
from core.delivery_finalize import (
    finalize_delivery_plan,
    preview_delivery_finalize,
    render_delivery_finalize,
)
from core.delivery_progress import delivery_events_path, delivery_progress_path, get_delivery_status
from sikula import main
from sikula_cli.delivery import cmd_delivery_finalize


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _current_branch(root: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_commit(root: Path, name: str, body: str) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
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
            f"add {name}",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return _rev_parse(root, "HEAD")


def _rev_parse(root: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_conflicting_unit_commits(root: Path) -> tuple[str, str, str]:
    base = _git_commit(root, "shared.txt", "base\n")
    main_branch = _current_branch(root)
    subprocess.run(["git", "checkout", "-q", "-b", "unit-one", base], cwd=root, check=True)
    first_commit = _git_commit(root, "shared.txt", "unit one\n")
    subprocess.run(["git", "checkout", "-q", "-b", "unit-two", base], cwd=root, check=True)
    second_commit = _git_commit(root, "shared.txt", "unit two\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=root, check=True)
    return base, first_commit, second_commit


def _git_merge_commit(root: Path, current: str, incoming: str) -> str:
    tree = _rev_parse(root, f"{current}^{{tree}}")
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit-tree",
            tree,
            "-p",
            current,
            "-p",
            incoming,
            "-m",
            "resolve delivery conflict",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_unit(root: Path, name: str) -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\nUnit body should stay private.\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path, *, unit_count: int = 2, final_branch: str = "sikula/delivery/final") -> Path:
    units = []
    for idx in range(unit_count):
        unit_id = f"{idx + 1:02d}-unit"
        units.append(
            {
                "id": unit_id,
                "title": f"Unit {idx + 1}",
                "task_path": _write_unit(root, f"{unit_id}.md"),
                "depends_on": [units[idx - 1]["id"]] if idx else [],
            }
        )
    plan = {
        "schema_version": 1,
        "plan_id": "delivery-finalize-demo",
        "title": "Delivery finalize demo",
        "final_branch": final_branch,
        "units": units,
    }
    path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    return path


def _write_progress(root: Path, units: list[dict], **metadata: str) -> None:
    path = delivery_progress_path(root, "delivery-finalize-demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "delivery-finalize-demo",
                "units": units,
                **metadata,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _finalize_args(plan_path: Path, *, dry_run: bool = False, json_output: bool = False) -> argparse.Namespace:
    return argparse.Namespace(plan_file=str(plan_path), dry_run=dry_run, json=json_output)


def _cfg(root: Path) -> dict:
    return {"project": {"root_path": str(root), "build_tool": "python"}}


def test_preview_delivery_finalize_blocks_incomplete_plan(tmp_path: Path) -> None:
    _git_init(tmp_path)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": first_commit}])

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.not_done"]
    assert "Unit body should stay private" not in json.dumps(result.to_dict())


def test_preview_delivery_finalize_reports_ready_final_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    second_commit = _git_commit(tmp_path, "unit-2.txt", "unit 2\n")
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is True
    assert result.dry_run is True
    assert result.final_branch == "sikula/delivery/final"
    assert result.final_commit == second_commit
    assert result.progress_path == str(delivery_progress_path(tmp_path, "delivery-finalize-demo"))
    output = render_delivery_finalize(result)
    assert "Status: ready" in output
    assert "Dry run: yes" in output
    assert "Unit body should stay private" not in output


def test_finalize_delivery_plan_creates_branch_and_records_progress(tmp_path: Path) -> None:
    _git_init(tmp_path)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    second_commit = _git_commit(tmp_path, "unit-2.txt", "unit 2\n")
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit, "branch": "sikula/unit-1"},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit, "branch": "sikula/unit-2"},
        ],
    )

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is True
    assert result.ready is True
    assert result.final_commit == second_commit
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == second_commit
    progress = json.loads(delivery_progress_path(tmp_path, "delivery-finalize-demo").read_text(encoding="utf-8"))
    assert progress["final_branch"] == "sikula/delivery/final"
    assert progress["final_commit"] == second_commit
    assert progress["finalized_at"]
    assert progress["units"][0]["branch"] == "sikula/unit-1"
    events = delivery_events_path(tmp_path, "delivery-finalize-demo").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[-1]) == {
        "schema_version": 1,
        "plan_id": "delivery-finalize-demo",
        "event_type": "plan.finalized",
        "timestamp": progress["finalized_at"],
        "branch": "sikula/delivery/final",
        "commit": second_commit,
    }
    status = get_delivery_status(plan_path, project_root=tmp_path)
    assert status.final_commit == second_commit
    assert status.next_action == "review finalized delivery branch"


def test_finalize_delivery_plan_uses_head_when_plan_order_lists_dependent_first(tmp_path: Path) -> None:
    _git_init(tmp_path)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    second_commit = _git_commit(tmp_path, "unit-2.txt", "unit 2\n")
    first_unit_path = _write_unit(tmp_path, "01-unit.md")
    second_unit_path = _write_unit(tmp_path, "02-unit.md")
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plan_id": "delivery-finalize-demo",
                "title": "Delivery finalize demo",
                "final_branch": "sikula/delivery/final",
                "units": [
                    {
                        "id": "02-unit",
                        "title": "Unit 2",
                        "task_path": second_unit_path,
                        "depends_on": ["01-unit"],
                    },
                    {
                        "id": "01-unit",
                        "title": "Unit 1",
                        "task_path": first_unit_path,
                        "depends_on": [],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is True
    assert result.ready is True
    assert result.final_commit == second_commit
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == second_commit


def test_finalize_delivery_plan_does_not_update_branch_when_progress_reread_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": commit}])

    monkeypatch.setattr(
        "core.delivery_finalize.read_delivery_progress",
        lambda *args, **kwargs: (
            None,
            [DeliveryPlanIssue("error", "progress.read_failed", "Failed to read progress file.")],
        ),
    )

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["progress.read_failed"]
    assert not (tmp_path / ".git" / "refs" / "heads" / "sikula" / "delivery" / "final").exists()


def test_finalize_delivery_plan_reports_existing_progress_lock(tmp_path: Path) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": commit}])
    lock = delivery_finalize_module.acquire_delivery_progress_lock(
        tmp_path,
        "delivery-finalize-demo",
        owner="test",
    )

    try:
        result = finalize_delivery_plan(plan_path, project_root=tmp_path)
    finally:
        lock.release()

    assert result.finalized is False
    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery.locked"]
    assert not (tmp_path / ".git" / "refs" / "heads" / "sikula" / "delivery" / "final").exists()


def test_preview_delivery_finalize_reports_missing_final_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": commit}])
    monkeypatch.setattr(delivery_finalize_module, "_final_commit_candidate", lambda *args, **kwargs: None)

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.final_commit is None
    assert [issue.code for issue in result.errors] == ["delivery.final_commit_missing"]


def test_finalize_delivery_plan_uses_head_for_all_noop_units(tmp_path: Path) -> None:
    _git_init(tmp_path)
    head = _git_commit(tmp_path, "base.txt", "base\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done"}])

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is True
    assert result.final_commit == head
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == head


def test_preview_delivery_finalize_uses_base_when_noop_branch_is_behind(tmp_path: Path) -> None:
    _git_init(tmp_path)
    old_commit = _git_commit(tmp_path, "old.txt", "old\n")
    base = _git_commit(tmp_path, "base.txt", "base\n")
    subprocess.run(["git", "branch", "sikula/delivery/final", old_commit], cwd=tmp_path, check=True)
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-unit", "status": "done"}],
        assembly_base_commit=base,
    )

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert preview.ready is True
    assert preview.final_commit == base

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is True
    assert result.final_commit == base
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == base


def test_finalize_rejects_branch_ahead_of_base_without_recorded_progress(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    stale_commit = _git_commit(tmp_path, "stale.txt", "stale\n")
    subprocess.run(
        ["git", "branch", "sikula/delivery/final", stale_commit],
        cwd=tmp_path,
        check=True,
    )
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-unit", "status": "done"}],
        assembly_base_commit=base,
    )

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery.assembly_branch_diverged"]

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_diverged"]
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == stale_commit


def test_preview_delivery_finalize_reports_missing_unit_commit(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": "missing-ref"}])

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.unit_commit_missing"]


def test_preview_delivery_finalize_rejects_missing_recorded_assembly_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(
        tmp_path,
        [{"unit_id": "01-unit", "status": "done", "commit": commit}],
        assembly_base_commit=commit,
        assembled_commit=commit,
        assembly_status="ready",
        assembly_updated_at="2026-07-23T12:00:00+00:00",
    )

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_missing"]


def test_preview_delivery_finalize_rejects_symbolic_final_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    main_branch = _current_branch(tmp_path)
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": commit}])
    subprocess.run(
        ["git", "symbolic-ref", "refs/heads/sikula/delivery/final", f"refs/heads/{main_branch}"],
        cwd=tmp_path,
        check=True,
    )

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_symbolic"]
    assert _rev_parse(tmp_path, f"refs/heads/{main_branch}") == commit


def test_preview_delivery_finalize_rejects_checked_out_final_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    subprocess.run(["git", "branch", "sikula/delivery/final", base], cwd=tmp_path, check=True)
    unit_commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": unit_commit}])
    monkeypatch.setattr(delivery_finalize_module, "branch_checked_out", lambda root, branch: True)

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_checked_out"]


def test_finalize_delivery_plan_rejects_diverged_final_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    main_branch = _current_branch(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other", base], cwd=tmp_path, check=True)
    other_commit = _git_commit(tmp_path, "other.txt", "other\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    unit_commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    subprocess.run(["git", "branch", "sikula/delivery/final", other_commit], cwd=tmp_path, check=True)
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": unit_commit}])

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is False
    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_diverged"]
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == other_commit


def test_preview_delivery_finalize_rejects_branch_checkout_shorthand(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    main_branch = _current_branch(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other", base], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    unit_commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1, final_branch="@{-1}")
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": unit_commit}])

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["final_branch.invalid"]
    assert result.final_commit is None


@pytest.mark.parametrize("final_branch", ["HEAD", "-foo"])
def test_preview_delivery_finalize_rejects_refs_that_are_not_branch_names(tmp_path: Path, final_branch: str) -> None:
    _git_init(tmp_path)
    unit_commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1, final_branch=final_branch)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": unit_commit}])

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["final_branch.invalid"]
    assert result.final_commit is None


def test_render_delivery_finalize_outputs_errors_and_warnings(tmp_path: Path) -> None:
    result = delivery_finalize_module.DeliveryFinalizeResult(
        plan_path=str(tmp_path / "plan.yaml"),
        project_root=str(tmp_path),
        valid=False,
        ready=False,
        dry_run=False,
        finalized=False,
        status="done",
        progress_exists=True,
        final_branch="sikula/delivery/final",
        final_commit="abc123",
        progress_path=str(tmp_path / "progress.json"),
        events_path=str(tmp_path / "events.jsonl"),
        errors=[DeliveryPlanIssue("error", "delivery.example", "Example error.")],
        warnings=[DeliveryPlanIssue("warning", "delivery.warning", "Example warning.")],
        message="Blocked.",
    )

    output = render_delivery_finalize(result)

    assert "Status: blocked" in output
    assert "Project root:" in output
    assert "Plan status: done" in output
    assert "Final branch: sikula/delivery/final" in output
    assert "Final commit: abc123" in output
    assert "Progress:" in output
    assert "Events:" in output
    assert "- delivery.example: Example error." in output
    assert "- delivery.warning: Example warning." in output


def test_finalize_delivery_plan_rechecks_branch_before_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    main_branch = _current_branch(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other", base], cwd=tmp_path, check=True)
    other_commit = _git_commit(tmp_path, "other.txt", "other\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    unit_commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": unit_commit}])
    original_read_delivery_progress = delivery_finalize_module.read_delivery_progress
    branch_created = False

    def create_diverged_branch_after_preflight(*args, **kwargs):
        nonlocal branch_created
        result = original_read_delivery_progress(*args, **kwargs)
        if not branch_created:
            branch_created = True
            subprocess.run(["git", "branch", "sikula/delivery/final", other_commit], cwd=tmp_path, check=True)
        return result

    monkeypatch.setattr(delivery_finalize_module, "read_delivery_progress", create_diverged_branch_after_preflight)

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert branch_created is True
    assert result.finalized is False
    assert result.ready is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_branch_diverged"]
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == other_commit


def test_finalize_delivery_plan_assembles_independent_unit_results(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    main_branch = _current_branch(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "unit-two", base], cwd=tmp_path, check=True)
    second_commit = _git_commit(tmp_path, "unit-2.txt", "unit 2\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert preview.ready is True
    assert preview.final_commit is None
    assert "resulting commit is not known yet" in preview.message

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is True
    assert result.ready is True
    assert result.final_commit
    for commit in (first_commit, second_commit):
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, result.final_commit],
            cwd=tmp_path,
            capture_output=True,
        )
        assert ancestry.returncode == 0


def test_preview_delivery_finalize_rejects_git_without_write_tree_merge_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    base = _git_commit(tmp_path, "base.txt", "base\n")
    main_branch = _current_branch(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "unit-two", base], cwd=tmp_path, check=True)
    second_commit = _git_commit(tmp_path, "unit-2.txt", "unit 2\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    first_commit = _git_commit(tmp_path, "unit-1.txt", "unit 1\n")
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )
    monkeypatch.setattr(
        "core.delivery_assembly._merge_tree_write_tree_supported",
        lambda *_args, **_kwargs: False,
    )

    result = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert result.ready is False
    assert result.final_commit is None
    assert [issue.code for issue in result.errors] == ["delivery.assembly_git_unsupported"]


def test_finalize_delivery_plan_persists_recoverable_assembly_conflict(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base, first_commit, second_commit = _git_conflicting_unit_commits(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )

    result = finalize_delivery_plan(plan_path, project_root=tmp_path)

    assert result.finalized is False
    assert [issue.code for issue in result.errors] == ["delivery.assembly_conflict"]
    progress = json.loads(delivery_progress_path(tmp_path, "delivery-finalize-demo").read_text(encoding="utf-8"))
    assert progress["assembly_status"] == "failed"
    assert progress["assembly_unit_id"] == "02-unit"
    assert progress["assembly_error_code"] == "delivery.assembly_conflict"
    assert progress["assembled_commit"] == first_commit
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/final") == first_commit
    assert _rev_parse(tmp_path, "HEAD") == base
    assert not (tmp_path / ".git" / "MERGE_HEAD").exists()

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert preview.ready is False
    assert [issue.code for issue in preview.errors] == ["delivery.assembly_conflict"]
    assert "recorded merge conflict" in preview.errors[0].message


def test_preview_delivery_finalize_allows_recorded_resolved_assembly_conflict(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _, first_commit, second_commit = _git_conflicting_unit_commits(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-unit", "status": "done", "commit": first_commit},
            {"unit_id": "02-unit", "status": "done", "commit": second_commit},
        ],
    )
    conflict = finalize_delivery_plan(plan_path, project_root=tmp_path)
    assert [issue.code for issue in conflict.errors] == ["delivery.assembly_conflict"]
    resolved_commit = _git_merge_commit(tmp_path, first_commit, second_commit)
    subprocess.run(
        [
            "git",
            "update-ref",
            "refs/heads/sikula/delivery/final",
            resolved_commit,
            first_commit,
        ],
        cwd=tmp_path,
        check=True,
    )

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path)

    assert preview.ready is True
    assert preview.errors == []
    assert preview.final_commit == resolved_commit


def test_cmd_delivery_finalize_dry_run_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    commit = _git_commit(tmp_path, "unit.txt", "unit\n")
    plan_path = _write_plan(tmp_path, unit_count=1)
    _write_progress(tmp_path, [{"unit_id": "01-unit", "status": "done", "commit": commit}])

    cmd_delivery_finalize(_finalize_args(plan_path, dry_run=True, json_output=True), _cfg(tmp_path))

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["dry_run"] is True
    assert payload["finalized"] is False
    assert payload["final_commit"] == commit
    assert not (tmp_path / ".git" / "refs" / "heads" / "sikula" / "delivery" / "final").exists()


def test_cmd_delivery_finalize_invalid_plan_id_outputs_json_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, unit_count=1)
    data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    data["plan_id"] = "../bad"
    plan_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_finalize(_finalize_args(plan_path, dry_run=True, json_output=True), _cfg(tmp_path))

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["ready"] is False
    assert payload["progress_path"] is None
    assert payload["events_path"] is None
    assert {issue["code"] for issue in payload["errors"]} == {"plan_id.invalid"}


def test_cmd_delivery_finalize_exits_nonzero_when_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, unit_count=1)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_finalize(_finalize_args(plan_path, dry_run=True, json_output=False), _cfg(tmp_path))

    assert exc.value.code == 1
    assert "Status: blocked" in capsys.readouterr().out


def test_main_dispatches_delivery_finalize_through_runtime_config(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("schema_version: 1\n", encoding="utf-8")
    cfg = _cfg(tmp_path)

    with patch("sys.argv", ["sikula", "delivery", "finalize", str(plan_path), "--dry-run"]):
        with patch("sikula._load_runtime_config", return_value=cfg) as load_config:
            with patch("sikula.cmd_delivery_finalize") as delivery_finalize:
                main()

    load_config.assert_called_once()
    delivery_finalize.assert_called_once()
