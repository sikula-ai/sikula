from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
from unittest.mock import patch

import pytest

import core.delivery_assembly as delivery_assembly_module
from core.delivery_assembly import (
    DeliveryAssemblyArtifact,
    DeliveryAssemblyUnit,
    assemble_delivery_artifacts,
    assemble_delivery_commits,
    delivery_artifact_content_id,
    find_delivery_artifact_commit,
    ordered_delivery_assembly_units,
    preview_delivery_artifacts,
    preview_delivery_assembly,
    rollback_delivery_artifacts,
)
from core.delivery_plan import DeliveryPlan, DeliveryPlanIssue, DeliveryPlanUnit


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


@pytest.mark.parametrize("object_format", [None, "sha256"])
def test_assemble_delivery_artifacts_uses_exact_tree_and_rolls_back_new_branch(
    tmp_path: Path,
    object_format: str | None,
) -> None:
    _git_init(tmp_path, object_format=object_format)
    base = _commit(tmp_path, "base.txt", "base\n")
    artifacts = [
        DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n"),
        DeliveryAssemblyArtifact("units/a.md", b"# A\n", must_not_exist=True),
    ]

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="a" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert len(result.assembled_commit) == len(base)
    assert result.previous_commit is None
    found, current = find_delivery_artifact_commit(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit=base,
        proposal_id="a" * 64,
        artifacts=artifacts,
    )
    assert found == result.assembled_commit
    assert current == result.assembled_commit
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:plan.yaml"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"plan: amended\n"
    )
    assert rollback_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        assembled_commit=result.assembled_commit,
        previous_commit=result.previous_commit,
    )
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/demo"],
            cwd=tmp_path,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_assemble_delivery_artifacts_applies_git_content_filters(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".gitattributes").write_text("*.md text eol=crlf\n", encoding="utf-8")
    contract = tmp_path / "units" / "a.md"
    contract.parent.mkdir()
    contract.write_bytes(b"# A\r\n")
    fingerprinted_contract = tmp_path / "units" / "b.md"
    fingerprinted_contract.write_bytes(b"# B\r\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "filtered contract"], cwd=tmp_path, check=True, capture_output=True)
    base = _rev_parse(tmp_path, "HEAD")
    artifacts = [
        DeliveryAssemblyArtifact(
            "units/a.md",
            contract.read_bytes(),
            expected_content=contract.read_bytes(),
        ),
        DeliveryAssemblyArtifact(
            "units/b.md",
            fingerprinted_contract.read_bytes(),
            expected_fingerprint=hashlib.sha256(fingerprinted_contract.read_bytes()).hexdigest(),
        ),
        DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n"),
    ]

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="f" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:units/a.md"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"# A\n"
    )
    assert (
        find_delivery_artifact_commit(
            tmp_path,
            branch="sikula/delivery/demo",
            parent_commit=base,
            proposal_id="f" * 64,
            artifacts=artifacts,
        )[0]
        == result.assembled_commit
    )


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX filter driver")
def test_preview_delivery_artifacts_rejects_external_filter_without_execution(tmp_path: Path) -> None:
    _git_init(tmp_path)
    marker = tmp_path / "filter-ran"
    driver = tmp_path / "filter-driver.sh"
    driver.write_text(
        f"#!/bin/sh\ncat\nprintf ran > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    driver.chmod(0o755)
    subprocess.run(
        ["git", "config", "filter.sideeffect.clean", str(driver)],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / ".gitattributes").write_text("*.yaml filter=sideeffect\n", encoding="utf-8")
    plan = tmp_path / "plan.yaml"
    plan.write_text("plan: original\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitattributes", "plan.yaml"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "filtered plan"], cwd=tmp_path, check=True, capture_output=True)
    parent = _rev_parse(tmp_path, "HEAD")
    subprocess.run(
        ["git", "config", "filter.sideeffect.smudge", str(driver)],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / ".git" / "info" / "attributes").write_text(
        ".gitattributes filter=sideeffect\n",
        encoding="utf-8",
    )
    marker.unlink(missing_ok=True)
    artifacts = [DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")]

    content_id = delivery_artifact_content_id(
        tmp_path,
        parent_commit=parent,
        path="plan.yaml",
        content=plan.read_bytes(),
    )
    preview = preview_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit=parent,
        artifacts=artifacts,
    )

    assert content_id is None
    assert preview.success is False
    assert preview.error is not None
    assert preview.error.code == "delivery.assembly_artifact_filter_unsupported"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX Git hook")
def test_preview_delivery_artifacts_does_not_execute_repository_hooks(tmp_path: Path) -> None:
    _git_init(tmp_path)
    parent = _commit(tmp_path, "plan.yaml", "plan: original\n")
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "configured-hooks"
    hooks.mkdir()
    hook = hooks / "post-index-change"
    hook.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=tmp_path, check=True)

    preview = preview_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit=parent,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
    )

    assert preview.success is True
    assert not marker.exists()


def test_assemble_delivery_artifacts_uses_parent_attributes_when_checkout_differs(tmp_path: Path) -> None:
    _git_init(tmp_path)
    operator_commit = _commit(tmp_path, "base.txt", "base\n")
    existing = tmp_path / "units" / "a.md"
    existing.parent.mkdir()
    (existing.parent / ".gitattributes").write_text("*.md text eol=lf\n", encoding="utf-8")
    existing.write_bytes(b"# A\r\n")
    subprocess.run(
        ["git", "add", "units/.gitattributes", "units/a.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "parent attributes"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    parent = _rev_parse(tmp_path, "HEAD")
    subprocess.run(
        ["git", "checkout", "--detach", operator_commit],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / ".git" / "info" / "attributes").write_text(
        "units/*.md -text\n",
        encoding="utf-8",
    )

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="1" * 64,
        branch="sikula/delivery/demo",
        parent_commit=parent,
        artifacts=[
            DeliveryAssemblyArtifact(
                "units/a.md",
                b"# A\r\n",
                expected_content=b"# A\r\n",
            ),
            DeliveryAssemblyArtifact("units/new.md", b"# New\r\n"),
        ],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:units/new.md"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"# New\n"
    )
    assert _rev_parse(tmp_path, "HEAD") == operator_commit
    assert _status(tmp_path) == ""


def test_assemble_delivery_artifacts_ignores_configured_attributes_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=tmp_path, check=True)
    parent = _commit(tmp_path, "base.txt", "base\n")
    local_attributes = tmp_path / "local-attributes"
    local_attributes.write_text("*.md text eol=lf\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.attributesFile", str(local_attributes)],
        cwd=tmp_path,
        check=True,
    )

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="3" * 64,
        branch="sikula/delivery/demo",
        parent_commit=parent,
        artifacts=[DeliveryAssemblyArtifact("units/new.md", b"# New\r\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:units/new.md"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"# New\r\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="requires a byte-oriented filesystem path")
def test_assemble_delivery_artifacts_ignores_unrelated_non_utf8_tree_paths(tmp_path: Path) -> None:
    _git_init(tmp_path)
    root = os.fsencode(tmp_path)
    filename = b"unrelated-\xff.txt"
    blob = subprocess.run(
        [b"git", b"hash-object", b"-w", b"--stdin"],
        cwd=root,
        input=b"unrelated\n",
        check=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(
        [b"git", b"update-index", b"--add", b"--cacheinfo", b"100644", blob, filename],
        cwd=root,
        check=True,
        capture_output=True,
    )
    tree = subprocess.run([b"git", b"write-tree"], cwd=root, check=True, capture_output=True).stdout.strip()
    parent = subprocess.run(
        [b"git", b"commit-tree", tree, b"-m", b"non utf8 path"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run([b"git", b"update-ref", b"HEAD", parent], cwd=root, check=True, capture_output=True)
    parent_text = parent.decode("ascii")

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="2" * 64,
        branch="sikula/delivery/demo",
        parent_commit=parent_text,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:plan.yaml"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"plan: amended\n"
    )


def test_assemble_delivery_artifacts_preserves_existing_file_mode(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "units").mkdir()
    _commit(tmp_path, "units/a.md", "# A\n")
    subprocess.run(
        ["git", "update-index", "--chmod=+x", "units/a.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-m", "executable contract"], cwd=tmp_path, check=True, capture_output=True)
    base = _rev_parse(tmp_path, "HEAD")
    artifacts = [
        DeliveryAssemblyArtifact(
            "units/a.md",
            b"# A\n",
            expected_content=b"# A\n",
        ),
        DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n"),
    ]

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="e" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    entry = subprocess.run(
        ["git", "ls-tree", result.assembled_commit, "--", "units/a.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert entry.startswith("100755 blob ")
    assert (
        find_delivery_artifact_commit(
            tmp_path,
            branch="sikula/delivery/demo",
            parent_commit=base,
            proposal_id="e" * 64,
            artifacts=artifacts,
        )[0]
        == result.assembled_commit
    )


def test_assemble_delivery_artifacts_restores_existing_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    subprocess.run(["git", "branch", "sikula/delivery/demo", base], cwd=tmp_path, check=True)
    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="b" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert result.previous_commit == base
    assert rollback_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        assembled_commit=result.assembled_commit,
        previous_commit=result.previous_commit,
    )
    assert _rev_parse(tmp_path, "sikula/delivery/demo") == base


def test_rollback_delivery_artifacts_restores_branch_behind_parent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    previous = _commit(tmp_path, "base.txt", "base\n")
    parent = _commit(tmp_path, "parent.txt", "parent\n")
    subprocess.run(["git", "branch", "sikula/delivery/demo", previous], cwd=tmp_path, check=True)

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="c" * 64,
        branch="sikula/delivery/demo",
        parent_commit=parent,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert result.previous_commit == previous
    assert rollback_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        assembled_commit=result.assembled_commit,
        previous_commit=result.previous_commit,
    )
    assert _rev_parse(tmp_path, "sikula/delivery/demo") == previous


def test_find_delivery_artifact_commit_rejects_changed_current_tree(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    artifacts = [DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")]
    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="d" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )
    assert result.success is True
    assert result.assembled_commit is not None
    worktree = tmp_path.parent / f"{tmp_path.name}-delivery-branch"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "sikula/delivery/demo"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    try:
        (worktree / "plan.yaml").write_text("plan: changed later\n", encoding="utf-8")
        subprocess.run(["git", "add", "plan.yaml"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "change amendment"], cwd=worktree, check=True, capture_output=True)
        current = _rev_parse(worktree, "HEAD")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=tmp_path, check=True)

    found, branch_commit = find_delivery_artifact_commit(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit=base,
        proposal_id="d" * 64,
        artifacts=artifacts,
    )

    assert found is None
    assert branch_commit == current


def test_assemble_delivery_artifacts_uses_git_root_relative_paths_for_nested_project(tmp_path: Path) -> None:
    _git_init(tmp_path)
    project_root = tmp_path / "apps" / "service"
    plan_path = project_root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("plan: original\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "nested project"], cwd=tmp_path, check=True, capture_output=True)
    base = _rev_parse(tmp_path, "HEAD")
    artifacts = [
        DeliveryAssemblyArtifact(
            ".sikula/delivery/demo/plan.yaml",
            b"plan: amended\n",
            expected_content=b"plan: original\n",
        ),
        DeliveryAssemblyArtifact(
            ".sikula/delivery/demo/units/a.md",
            b"# A\n",
            must_not_exist=True,
        ),
    ]

    result = assemble_delivery_artifacts(
        project_root,
        plan_id="demo",
        proposal_id="c" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is True
    assert result.assembled_commit is not None
    assert (
        subprocess.run(
            ["git", "show", f"{result.assembled_commit}:apps/service/.sikula/delivery/demo/plan.yaml"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout
        == b"plan: amended\n"
    )
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{result.assembled_commit}:.sikula/delivery/demo/plan.yaml"],
            cwd=tmp_path,
            capture_output=True,
        ).returncode
        != 0
    )
    found, current = find_delivery_artifact_commit(
        project_root,
        branch="sikula/delivery/demo",
        parent_commit=base,
        proposal_id="c" * 64,
        artifacts=artifacts,
    )
    assert found == result.assembled_commit
    assert current == result.assembled_commit


@pytest.mark.parametrize(
    ("artifacts", "error_code"),
    [
        ([], "delivery.assembly_artifacts_empty"),
        ([DeliveryAssemblyArtifact("../plan.yaml", b"plan\n")], "delivery.assembly_artifact_path_invalid"),
        ([DeliveryAssemblyArtifact("C:/plan.yaml", b"plan\n")], "delivery.assembly_artifact_path_invalid"),
        (
            [
                DeliveryAssemblyArtifact("plan.yaml", b"one\n"),
                DeliveryAssemblyArtifact("PLAN.yaml", b"two\n"),
            ],
            "delivery.assembly_artifact_path_invalid",
        ),
        (
            [
                DeliveryAssemblyArtifact("units", b"file\n"),
                DeliveryAssemblyArtifact("units/new.md", b"unit\n"),
            ],
            "delivery.assembly_artifact_path_invalid",
        ),
    ],
)
def test_assemble_delivery_artifacts_rejects_invalid_artifact_sets(
    tmp_path: Path,
    artifacts: list[DeliveryAssemblyArtifact],
    error_code: str,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="c" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == error_code


def test_assemble_delivery_artifacts_rejects_missing_parent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _commit(tmp_path, "base.txt", "base\n")

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="d" * 64,
        branch="sikula/delivery/demo",
        parent_commit="f" * 40,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_expected_commit_missing"


def test_assemble_delivery_artifacts_rejects_invalid_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="e" * 64,
        branch="invalid..branch",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_invalid"


def test_assemble_delivery_artifacts_rejects_symbolic_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "symbolic-ref", "refs/heads/sikula/delivery/demo", f"refs/heads/{current_branch}"],
        cwd=tmp_path,
        check=True,
    )

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="f" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_symbolic"


def test_assemble_delivery_artifacts_rejects_checked_out_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    subprocess.run(["git", "checkout", "-b", "sikula/delivery/demo"], cwd=tmp_path, check=True, capture_output=True)

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="1" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_checked_out"


def test_assemble_delivery_artifacts_rejects_diverged_branch(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    advanced = _commit(tmp_path, "advanced.txt", "advanced\n")
    subprocess.run(["git", "branch", "sikula/delivery/demo", advanced], cwd=tmp_path, check=True)

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="2" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_diverged"


def test_assemble_delivery_artifacts_rejects_stale_parent_fingerprint(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "plan.yaml", "plan: original\n")

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="3" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[
            DeliveryAssemblyArtifact(
                "plan.yaml",
                b"plan: amended\n",
                expected_fingerprint="0" * 64,
            )
        ],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_artifact_stale"


def test_preview_delivery_artifacts_rejects_ancestor_file_conflict(tmp_path: Path) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "units", "tracked as a file\n")
    artifacts = [DeliveryAssemblyArtifact("units/new.md", b"new unit\n", must_not_exist=True)]

    preview = preview_delivery_artifacts(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
    )
    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="a" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=artifacts,
        created_at="2026-08-05T10:00:00Z",
    )

    assert preview.success is False
    assert preview.error is not None
    assert preview.error.code == "delivery.assembly_artifact_conflict"
    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_artifact_conflict"
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/sikula/delivery/demo"],
            cwd=tmp_path,
            capture_output=True,
        ).returncode
        != 0
    )


def test_assemble_delivery_artifacts_reports_compare_and_swap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    monkeypatch.setattr(delivery_assembly_module, "_update_ref", lambda *_args, **_kwargs: False)

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="4" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_diverged"


@pytest.mark.parametrize("failure_point", ["resolve", "preflight", "create", "update"])
def test_assemble_delivery_artifacts_maps_git_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")

    def fail(*_args, **_kwargs):
        raise OSError("git unavailable")

    if failure_point == "resolve":
        monkeypatch.setattr(delivery_assembly_module, "resolve_git_commit", fail)
    elif failure_point == "preflight":
        monkeypatch.setattr(delivery_assembly_module, "_artifact_preflight_issue", fail)
    elif failure_point == "create":
        monkeypatch.setattr(
            delivery_assembly_module,
            "_create_artifact_commit",
            lambda *_args, **_kwargs: (
                None,
                DeliveryPlanIssue(
                    "error",
                    "delivery.assembly_artifact_git_failed",
                    "Git failed.",
                ),
            ),
        )
    else:
        monkeypatch.setattr(delivery_assembly_module, "_update_ref", fail)

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="5" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    expected_code = (
        "delivery.assembly_branch_diverged" if failure_point == "update" else "delivery.assembly_artifact_git_failed"
    )
    assert result.error.code == expected_code


def test_assemble_delivery_artifacts_detects_branch_race_during_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    advanced = _commit(tmp_path, "advanced.txt", "advanced\n")
    commits = iter([advanced, base])
    monkeypatch.setattr(delivery_assembly_module, "_branch_commit", lambda *_args: next(commits))

    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="6" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_branch_diverged"


@pytest.mark.parametrize("failed_command", ["read-tree", "hash-object", "update-index", "write-tree", "commit-tree"])
def test_assemble_delivery_artifacts_maps_plumbing_command_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_command: str,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    real_run = subprocess.run

    def fail_selected(command, *args, **kwargs):
        if len(command) > 1 and command[1] == failed_command:
            output = b"" if not kwargs.get("text") else ""
            return subprocess.CompletedProcess(command, 1, output, output)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(delivery_assembly_module.subprocess, "run", fail_selected)
    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="7" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_artifact_git_failed"


def test_assemble_delivery_artifacts_maps_preflight_git_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n")
    real_run = subprocess.run

    def fail_tree_read(command, *args, **kwargs):
        if command[:2] == ["git", "ls-tree"]:
            raise OSError("git unavailable")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(delivery_assembly_module.subprocess, "run", fail_tree_read)
    result = assemble_delivery_artifacts(
        tmp_path,
        plan_id="demo",
        proposal_id="8" * 64,
        branch="sikula/delivery/demo",
        parent_commit=base,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
        created_at="2026-08-05T10:00:00Z",
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "delivery.assembly_artifact_git_failed"


def test_rollback_delivery_artifacts_returns_false_on_git_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_delete(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(delivery_assembly_module, "_delete_ref", fail_delete)

    assert (
        rollback_delivery_artifacts(
            tmp_path,
            branch="sikula/delivery/demo",
            assembled_commit="a" * 40,
            previous_commit=None,
        )
        is False
    )


@pytest.mark.parametrize("failure_point", ["branch", "log_error", "log_failure", "invalid_log", "show_error"])
def test_find_delivery_artifact_commit_fails_closed_on_git_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    current = "a" * 40
    candidate = "b" * 40

    if failure_point == "branch":

        def fail_branch(*_args):
            raise OSError("git unavailable")

        monkeypatch.setattr(delivery_assembly_module, "_branch_commit", fail_branch)
    else:
        monkeypatch.setattr(delivery_assembly_module, "_branch_commit", lambda *_args: current)
        monkeypatch.setattr(delivery_assembly_module, "_is_ancestor", lambda *_args: True)

        def run_git(command, *args, **kwargs):
            if command[:2] == ["git", "log"]:
                if failure_point == "log_error":
                    raise OSError("git unavailable")
                if failure_point == "log_failure":
                    return subprocess.CompletedProcess(command, 1, "", "failed")
                output = f"invalid\n{candidate}\n" if failure_point == "invalid_log" else f"{candidate}\n"
                return subprocess.CompletedProcess(command, 0, output, "")
            if failure_point == "show_error":
                raise OSError("git unavailable")
            return subprocess.CompletedProcess(command, 0, "different subject\n", "")

        monkeypatch.setattr(delivery_assembly_module.subprocess, "run", run_git)

    found, branch_commit = find_delivery_artifact_commit(
        tmp_path,
        branch="sikula/delivery/demo",
        parent_commit="c" * 40,
        proposal_id="d" * 64,
        artifacts=[DeliveryAssemblyArtifact("plan.yaml", b"plan: amended\n")],
    )

    assert found is None
    if failure_point == "branch":
        assert branch_commit is None
    else:
        assert branch_commit == current


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
