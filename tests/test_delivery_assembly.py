from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from core.delivery_assembly import (
    DeliveryAssemblyUnit,
    assemble_delivery_commits,
    ordered_delivery_assembly_units,
    preview_delivery_assembly,
)
from core.delivery_plan import DeliveryPlan, DeliveryPlanUnit


def _git_init(root: Path, *, object_format: str | None = None) -> None:
    command = ["git", "init"]
    if object_format is not None:
        command.append(f"--object-format={object_format}")
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if result.returncode != 0 and object_format is not None:
        pytest.skip(f"Git does not support {object_format} repositories")
    result.check_returncode()
    subprocess.run(["git", "config", "user.name", "Sikula Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "sikula@example.test"], cwd=root, check=True)


def _commit(root: Path, name: str, body: str) -> str:
    path = root / name
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"write {name}"], cwd=root, check=True, capture_output=True)
    return _rev_parse(root, "HEAD")


def _rev_parse(root: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
    )
    return result.returncode == 0


def _status(root: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_assemble_delivery_commits_fast_forwards_without_mutating_checkout(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    unit_commit = _commit(tmp_path, "unit.txt", "unit\n")
    head_before = _rev_parse(tmp_path, "HEAD")

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[DeliveryAssemblyUnit("unit", unit_commit)],
    )

    assert result.success is True
    assert result.assembled_commit == unit_commit
    assert [outcome.outcome for outcome in result.outcomes] == ["fast_forward"]
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/demo") == unit_commit
    assert _rev_parse(tmp_path, "HEAD") == head_before
    assert _status(tmp_path) == ""


def test_assemble_delivery_commits_creates_branch_in_sha256_repository(tmp_path: Path) -> None:
    _git_init(tmp_path, object_format="sha256")
    base = _commit(tmp_path, "base.txt", "base\n")
    unit_commit = _commit(tmp_path, "unit.txt", "unit\n")

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[DeliveryAssemblyUnit("unit", unit_commit)],
    )

    assert result.success is True
    assert result.assembled_commit == unit_commit
    assert len(unit_commit) == 64
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/demo") == unit_commit


def test_preview_delivery_assembly_validates_without_creating_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    unit_commit = _commit(tmp_path, "unit.txt", "unit\n")

    result = preview_delivery_assembly(
        tmp_path,
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[DeliveryAssemblyUnit("unit", unit_commit)],
    )

    assert result.success is True
    assert result.assembled_commit == base
    missing_branch = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/sikula/delivery/demo"],
        cwd=tmp_path,
    )
    assert missing_branch.returncode == 1
    assert _status(tmp_path) == ""


def test_assemble_delivery_commits_rejects_symbolic_branch_without_moving_target(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    unit_commit = _commit(tmp_path, "unit.txt", "unit\n")
    subprocess.run(
        ["git", "symbolic-ref", "refs/heads/sikula/delivery/demo", f"refs/heads/{main_branch}"],
        cwd=tmp_path,
        check=True,
    )
    target_before = _rev_parse(tmp_path, f"refs/heads/{main_branch}")

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[DeliveryAssemblyUnit("unit", unit_commit)],
    )

    assert result.success is False
    assert result.error
    assert result.error.code == "delivery.assembly_branch_symbolic"
    assert _rev_parse(tmp_path, f"refs/heads/{main_branch}") == target_before
    symbolic_target = subprocess.run(
        ["git", "symbolic-ref", "refs/heads/sikula/delivery/demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert symbolic_target == f"refs/heads/{main_branch}"


def test_preview_delivery_assembly_rejects_git_without_write_tree_merge_support(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "unit-a", base], cwd=tmp_path, check=True)
    unit_a = _commit(tmp_path, "a.txt", "a\n")
    subprocess.run(["git", "checkout", "-q", "-b", "unit-b", base], cwd=tmp_path, check=True)
    unit_b = _commit(tmp_path, "b.txt", "b\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)

    original_run = subprocess.run

    def run_without_write_tree(command, *args, **kwargs):
        if command[:3] == ["git", "merge-tree", "--write-tree"]:
            return subprocess.CompletedProcess(command, 129, "", "error: unknown option `write-tree`")
        return original_run(command, *args, **kwargs)

    with patch("core.delivery_assembly.subprocess.run", side_effect=run_without_write_tree):
        result = preview_delivery_assembly(
            tmp_path,
            branch="sikula/delivery/demo",
            base_commit=base,
            expected_commit=None,
            units=[
                DeliveryAssemblyUnit("unit-a", unit_a),
                DeliveryAssemblyUnit("unit-b", unit_b),
            ],
        )

    assert result.success is False
    assert result.failed_unit_id == "unit-b"
    assert result.error
    assert result.error.code == "delivery.assembly_git_unsupported"
    assert "Git 2.38 or newer" in result.error.message
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/sikula/delivery/demo"],
            cwd=tmp_path,
        ).returncode
        == 1
    )


def test_assembly_preflight_uses_base_when_existing_branch_is_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    old_commit = _commit(tmp_path, "base.txt", "base\n")
    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "unit", old_commit], cwd=tmp_path, check=True)
    unit_commit = _commit(tmp_path, "unit.txt", "unit\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    base = _commit(tmp_path, "new-base.txt", "new base\n")
    subprocess.run(["git", "branch", "sikula/delivery/demo", old_commit], cwd=tmp_path, check=True)
    monkeypatch.setattr(
        "core.delivery_assembly._merge_tree_write_tree_supported",
        lambda *_args, **_kwargs: False,
    )

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[DeliveryAssemblyUnit("unit", unit_commit)],
    )

    assert result.success is False
    assert result.failed_unit_id == "unit"
    assert result.error
    assert result.error.code == "delivery.assembly_git_unsupported"
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/demo") == old_commit


def test_assembly_rejects_branch_ahead_of_base_without_recorded_progress(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    stale_commit = _commit(tmp_path, "stale.txt", "stale\n")
    subprocess.run(
        ["git", "branch", "sikula/delivery/demo", stale_commit],
        cwd=tmp_path,
        check=True,
    )

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[],
    )

    assert result.success is False
    assert result.error
    assert result.error.code == "delivery.assembly_branch_diverged"
    assert "without recorded assembly progress" in result.error.message
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/demo") == stale_commit


def test_assemble_delivery_commits_merges_independent_results_and_preserves_ancestry(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "unit-a", base], cwd=tmp_path, check=True)
    unit_a = _commit(tmp_path, "a.txt", "a\n")
    subprocess.run(["git", "checkout", "-q", "-b", "unit-b", base], cwd=tmp_path, check=True)
    unit_b = _commit(tmp_path, "b.txt", "b\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    head_before = _rev_parse(tmp_path, "HEAD")

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[
            DeliveryAssemblyUnit("unit-a", unit_a),
            DeliveryAssemblyUnit("unit-b", unit_b),
        ],
    )

    assert result.success is True
    assert result.assembled_commit
    assert [outcome.outcome for outcome in result.outcomes] == ["fast_forward", "merged"]
    assert _is_ancestor(tmp_path, unit_a, result.assembled_commit)
    assert _is_ancestor(tmp_path, unit_b, result.assembled_commit)
    assert _rev_parse(tmp_path, "HEAD") == head_before
    assert _status(tmp_path) == ""

    repeated = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=result.assembled_commit,
        units=[
            DeliveryAssemblyUnit("unit-a", unit_a),
            DeliveryAssemblyUnit("unit-b", unit_b),
        ],
    )

    assert repeated.success is True
    assert repeated.assembled_commit == result.assembled_commit
    assert [outcome.outcome for outcome in repeated.outcomes] == ["already_applied", "already_applied"]


def test_assemble_delivery_commits_reports_conflict_without_leaving_git_state(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "shared.txt", "base\n")
    main_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "unit-a", base], cwd=tmp_path, check=True)
    unit_a = _commit(tmp_path, "shared.txt", "unit a\n")
    subprocess.run(["git", "checkout", "-q", "-b", "unit-b", base], cwd=tmp_path, check=True)
    unit_b = _commit(tmp_path, "shared.txt", "unit b\n")
    subprocess.run(["git", "checkout", "-q", main_branch], cwd=tmp_path, check=True)
    head_before = _rev_parse(tmp_path, "HEAD")

    result = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=None,
        units=[
            DeliveryAssemblyUnit("unit-a", unit_a),
            DeliveryAssemblyUnit("unit-b", unit_b),
        ],
    )

    assert result.success is False
    assert result.failed_unit_id == "unit-b"
    assert result.error
    assert result.error.code == "delivery.assembly_conflict"
    assert result.assembled_commit == unit_a
    assert _rev_parse(tmp_path, "refs/heads/sikula/delivery/demo") == unit_a
    assert _rev_parse(tmp_path, "HEAD") == head_before
    assert _status(tmp_path) == ""
    assert not (tmp_path / ".git" / "MERGE_HEAD").exists()

    resolved_tree = subprocess.run(
        ["git", "rev-parse", f"{unit_a}^{{tree}}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolved_commit = subprocess.run(
        ["git", "commit-tree", resolved_tree, "-p", unit_a, "-p", unit_b, "-m", "resolve delivery conflict"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/heads/sikula/delivery/demo", resolved_commit, unit_a],
        cwd=tmp_path,
        check=True,
    )

    recovered = assemble_delivery_commits(
        tmp_path,
        plan_id="demo",
        branch="sikula/delivery/demo",
        base_commit=base,
        expected_commit=unit_a,
        units=[
            DeliveryAssemblyUnit("unit-a", unit_a),
            DeliveryAssemblyUnit("unit-b", unit_b),
        ],
    )

    assert recovered.success is True
    assert recovered.assembled_commit == resolved_commit
    assert [outcome.outcome for outcome in recovered.outcomes] == ["already_applied", "already_applied"]


def test_ordered_delivery_assembly_units_uses_dependency_order() -> None:
    plan = DeliveryPlan(
        schema_version=1,
        plan_id="demo",
        title="Demo",
        final_branch="sikula/delivery/demo",
        repositories=[],
        units=[
            DeliveryPlanUnit("feature", "Feature", "feature.md", depends_on=["foundation"]),
            DeliveryPlanUnit("independent", "Independent", "independent.md"),
            DeliveryPlanUnit("foundation", "Foundation", "foundation.md"),
        ],
    )

    units = ordered_delivery_assembly_units(
        plan,
        {
            "feature": "feature-commit",
            "independent": "independent-commit",
            "foundation": "foundation-commit",
        },
    )

    assert [unit.unit_id for unit in units] == ["foundation", "feature", "independent"]
