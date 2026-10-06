from __future__ import annotations

import argparse
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_checkpoints import checkpoint_review_rule_fingerprints
from core.delivery_plan import check_delivery_plan_file
from core.delivery_progress import (
    delivery_progress_path,
    get_delivery_status,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_run_next import preview_delivery_run_next
from sikula_cli.delivery import _preview_delivery_run
from tests.test_delivery_checkpoints import checkpoint_plan as checkpoint_plan, _git, _verify_node


def _git_input(root: Path, *args: str, content: bytes) -> str:
    return (
        subprocess.run(["git", *args], cwd=root, input=content, capture_output=True, check=True)
        .stdout.decode("ascii")
        .strip()
    )


@pytest.mark.parametrize("name", ["Task: storage.md", "Task: API: storage.md", ":(glob)*.md"])
@pytest.mark.parametrize("via_link", [False, True])
def test_checkpoint_reads_literal_colon_paths_from_git(tmp_path: Path, name: str, via_link: bool) -> None:
    # Build Git objects directly: Windows cannot check out these names, but the
    # candidate reader must apply the same literal path policy on every platform.
    _git(tmp_path, "init")
    content = b"Synthetic authority.\n"
    oid = _git_input(tmp_path, "hash-object", "-w", "--stdin", content=content)
    entries = f"100644 blob {oid}\t{name}\0".encode()
    if via_link:
        link_oid = _git_input(tmp_path, "hash-object", "-w", "--stdin", content=name.encode())
        entries += f"120000 blob {link_oid}\talias.md\0".encode()
    tree = _git_input(tmp_path, "mktree", "-z", content=entries)
    relative = "alias.md" if via_link else name
    assert checkpoint_review_rule_fingerprints(tmp_path, tree, [relative]) == {
        relative: "sha256:" + sha256(content).hexdigest()
    }


@pytest.mark.parametrize(
    "name", ["C:/task.md", "C:task.md", "/task.md", "../task.md", "dir/../task.md", r"dir\task.md"]
)
def test_checkpoint_rejects_unsafe_authority_paths_before_git(tmp_path: Path, name: str) -> None:
    with patch("core.delivery_checkpoints.subprocess.run") as git:
        with pytest.raises(ValueError, match="path is invalid"):
            checkpoint_review_rule_fingerprints(tmp_path, "a" * 40, [name])
    git.assert_not_called()


@pytest.mark.parametrize("target", ["C:/task.md", "C:task.md", "/task.md", "../task.md", r"dir\task.md"])
def test_checkpoint_rejects_unsafe_link_targets(tmp_path: Path, target: str) -> None:
    _git(tmp_path, "init")
    oid = _git_input(tmp_path, "hash-object", "-w", "--stdin", content=target.encode())
    tree = _git_input(tmp_path, "mktree", "-z", content=f"120000 blob {oid}\talias.md\0".encode())
    with pytest.raises(ValueError, match="unsafe|leaves the project"):
        checkpoint_review_rule_fingerprints(tmp_path, tree, ["alias.md"])


@pytest.mark.skipif(os.name == "nt", reason="Windows filesystems do not support colon filenames")
@pytest.mark.parametrize("artifact", ["source", "contract", "plan"])
def test_checkpoint_with_colon_authority_can_continue_existing_plan(checkpoint_plan, artifact: str) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    if artifact == "source":
        original = data["source_task"]["path"]
        data["source_task"]["path"] = "Task: storage.md"
        (root / original).rename(root / data["source_task"]["path"])
    elif artifact == "contract":
        original = data["units"][0]["task_path"]
        data["units"][0]["task_path"] = "Unit: read.md"
        (root / original).rename(root / data["units"][0]["task_path"])
    else:
        renamed = root / "Plan: storage.yaml"
        path.rename(renamed)
        path = renamed
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "captured authority fixture")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    progress = replace(
        progress,
        assembly_base_commit=commit,
        assembled_commit=commit,
        assembly_status="ready",
        units=[replace(unit, commit=commit) for unit in progress.units],
    )
    _git(root, "branch", data["final_branch"], commit)
    write_delivery_progress(progress_path, progress)
    assert check_delivery_plan_file(path).valid
    captured_plan = path.read_bytes()
    captured_progress = progress_path.read_bytes()

    preview = _preview_delivery_run(
        argparse.Namespace(plan_file=str(path), max_units=None, max_elapsed_minutes=None),
        cfg,
        project_root=root,
    )
    assert preview.ready, preview
    assert preview.units_attempted == 0
    assert progress_path.read_bytes() == captured_progress

    result, llm = _verify_node(path, cfg)

    assert result.succeeded, result
    assert llm.calls
    assert path.read_bytes() == captured_plan
    after, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    assert after.units == progress.units
    assert after.assembled_commit == commit
    status = get_delivery_status(path)
    assert status.checkpoint_verifications["storage"].passed
    assert preview_delivery_run_next(path, project_root=root).selected_unit.id == "consumer"
