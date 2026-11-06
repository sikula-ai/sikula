"""Fresh persistence authority without an unrelated whole-plan status projection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from core import delivery_checkpoints as checkpoints, delivery_verify as verify
from core.delivery_checkpoint_evidence import checkpoint_evidence_path
from core.delivery_progress import (
    delivery_progress_path,
    get_delivery_status,
    read_delivery_progress,
    select_next_delivery_unit,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git


@pytest.fixture
def pending_review(tmp_path):
    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    passed, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert passed.succeeded
    _complete(path, "consumer")
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, _LLM(KeyboardInterrupt()), node_id="integration")
    progress_path = delivery_progress_path(Path(cfg["project"]["root_path"]), "team-invites")
    progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
    assert not errors
    records = dict(progress.checkpoint_verifications)
    assert records["integration"].status == "interrupted"
    # Model the captured running attempt before the provider result is persisted.
    records["integration"] = replace(records["integration"], status="running", completed_at=None, stop_code=None)
    write_delivery_progress(progress_path, replace(progress, checkpoint_verifications=records))
    status = checkpoints.verification_node_status(get_delivery_status(path), "integration")
    assert status.verification.status == "running"
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    return path, cfg, status, snapshot


def _update(case, *, cfg=None, boundary=False):
    _, original_cfg, status, snapshot = case
    changes = (
        dict(status="blocked", stop_code="delivery_verification.readonly_mutation")
        if boundary
        else dict(composition_attempted=True)
    )
    return verify._update_candidate_record(
        status, snapshot, status.verification, original_cfg if cfg is None else cfg, changes, boundary=boundary
    )


def test_persistence_keeps_authority_checks_without_whole_plan_projection(pending_review):
    path, _, status, _ = pending_review
    real_run = subprocess.run
    measured = []

    def counted(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        if command and command[0] == "git":
            measured.append(1)
        return real_run(*args, **kwargs)

    # Same persisted attempt and identical update: only the status projection differs.
    with patch.object(subprocess, "run", new=counted):
        with patch.object(verify, "_read_delivery_status", new=get_delivery_status):
            before = _update(pending_review)
        before_calls = len(measured)
        measured.clear()
        with (
            patch.object(checkpoints, "with_checkpoint_barriers", side_effect=AssertionError("eager projection")),
            patch.object(
                checkpoints, "checkpoint_pass_is_usable", wraps=checkpoints.checkpoint_pass_is_usable
            ) as child,
        ):
            after = _update(pending_review)
        after_calls = len(measured)
    assert before == after
    assert 0 < after_calls < before_calls
    assert [call.args[1].id for call in child.call_args_list] == ["storage"]
    current = get_delivery_status(path)
    shipping = next(unit for unit in current.units if unit.id == "shipping")
    assert "integration" in shipping.blocked_by_checkpoints
    assert not shipping.eligible and select_next_delivery_unit(current) is None
    assert current.checkpoint_verifications["storage"] == status.checkpoint_verifications["storage"]


def test_persistence_rejects_changed_authority_without_writing(pending_review):
    path, original_cfg, status, _ = pending_review
    root = Path(status.project_root)
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    child_path = checkpoint_evidence_path(progress_path.parent, status.checkpoint_verifications["storage"])
    original_files = {file: file.read_bytes() for file in (path, progress_path, child_path)}
    branch = f"refs/heads/{status.plan.final_branch}"
    original_commit = _git(root, "rev-parse", branch)
    # Reuse one real interrupted-review fixture; each mutation is isolated and
    # restored. This avoids repeating expensive Git/provider setup for every veto.
    for change in ("plan", "progress", "attempt", "branch", "child", "config"):
        cfg = deepcopy(original_cfg)
        try:
            if change == "plan":
                path.write_bytes(original_files[path] + b"\n# changed authority\n")
            elif change == "progress":
                progress_path.write_bytes(b"{")
            elif change == "attempt":
                records = dict(progress.checkpoint_verifications)
                records["integration"] = replace(records["integration"], attempt=records["integration"].attempt + 1)
                write_delivery_progress(progress_path, replace(progress, checkpoint_verifications=records))
            elif change == "branch":
                commit = _git(
                    root, "commit-tree", status.verification.candidate_tree, "-p", original_commit, "-m", "advance"
                )
                _git(root, "update-ref", branch, commit)
            elif change == "child":
                child_path.unlink()
            else:
                cfg["agents"] = {"reviewer": {"llm": {"model": "changed"}}}
            before = progress_path.read_bytes()
            with pytest.raises(ValueError):
                _update(pending_review, cfg=cfg)
            assert progress_path.read_bytes() == before, change
        finally:
            for file, content in original_files.items():
                file.write_bytes(content)
            if change == "branch":
                _git(root, "update-ref", branch, original_commit)
    assert _update(pending_review).composition_attempted


def test_terminal_persistence_does_not_require_fresh_projection(pending_review):
    _, _, status, _ = pending_review
    root = Path(status.project_root)
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    checkpoint_evidence_path(directory, status.checkpoint_verifications["storage"]).unlink()
    with patch.object(verify, "_read_delivery_status", side_effect=AssertionError("boundary must persist first")):
        result = _update(pending_review, boundary=True)
    assert result.stop_code == "delivery_verification.readonly_mutation"
    current = get_delivery_status(status.plan_path)
    assert current.checkpoint_verifications["integration"].stop_code == result.stop_code
    assert select_next_delivery_unit(current) is None
