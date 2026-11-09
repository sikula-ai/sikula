"""Completion validates fresh authority and accepted evidence without scheduling."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from core import delivery_checkpoints as checkpoints, delivery_verify as verify
from core.delivery_checkpoint_evidence import checkpoint_evidence_path, root_evidence_path
from core.delivery_progress import (
    DeliveryProgress,
    DeliveryStatusResult,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    read_delivery_progress,
    select_next_delivery_unit,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot, DeliveryVerificationSnapshot
from core.delivery_verification_model import DeliveryVerificationRecord
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git


Completion = tuple[Path, dict[str, Any], DeliveryStatusResult, DeliveryVerificationSnapshot]


def _with_record(progress: DeliveryProgress, record: DeliveryVerificationRecord, node: str) -> DeliveryProgress:
    if node == "root":
        return replace(progress, verification=record)
    return replace(progress, checkpoint_verifications={**progress.checkpoint_verifications, node: record})


@pytest.fixture(params=["integration", "root"])
def completion(request: pytest.FixtureRequest, tmp_path: Path) -> Completion:
    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    passed, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert passed.succeeded
    _complete(path, "consumer")
    nested._compose(path, cfg, "integration")
    node = request.param
    if node == "root":
        _complete(path, "shipping")
        nested._compose(path, cfg, "root")
    status = checkpoints.verification_node_status(get_delivery_status(path), node)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    progress_path = delivery_progress_path(tmp_path, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors and status.verification.passed
    # Replay completion of the same captured attempt using its actual accepted evidence.
    running = replace(status.verification, status="running", completed_at=None)
    write_delivery_progress(progress_path, _with_record(progress, running, node))
    return path, cfg, status, snapshot


def _persist(
    case: Completion, *, cfg: dict[str, Any] | None = None, terminal: DeliveryVerificationRecord | None = None
) -> DeliveryVerificationRecord | None:
    path, original_cfg, status, snapshot = case
    root = Path(status.project_root)
    return verify._persist_terminal_if_current(
        path,
        root,
        status.plan.plan_id,
        original_cfg if cfg is None else cfg,
        snapshot.identity,
        status.verification if terminal is None else terminal,
        delivery_events_path(root, status.plan.plan_id),
        node_id=status.verification_node,
    )


def test_completion_preserves_results_authority_evidence_and_boundaries(completion: Completion) -> None:
    path, cfg, status, _ = completion
    root, node = Path(status.project_root), status.verification_node
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    events_path = delivery_events_path(root, status.plan.plan_id)
    originals = {p: p.read_bytes() for p in (path, progress_path, events_path)}
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    branch = f"refs/heads/{status.plan.final_branch}"
    original_commit = _git(root, "rev-parse", branch)
    advanced = _git(root, "commit-tree", status.verification.candidate_tree, "-p", original_commit, "-m", "advance")
    measured = []
    real_run = subprocess.run

    def counted(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        command = args[0] if args else kwargs.get("args", [])
        if command and command[0] == "git":
            measured.append(1)
        return real_run(*args, **kwargs)

    def restore() -> None:
        for p, content in originals.items():
            p.write_bytes(content)
        _git(root, "update-ref", branch, original_commit)

    # Compare real persistence, including events, on identical running state.
    for outcome in ("passed", "rejected", "interrupted"):
        terminal = status.verification
        if outcome != "passed":
            terminal = replace(
                terminal,
                status="blocked" if outcome == "rejected" else "interrupted",
                semantic_status="rejected" if outcome == "rejected" else "not_run",
                stop_code=f"delivery_verification.{'repair_required' if outcome == 'rejected' else 'interrupted'}",
                checkpoint_evidence_fingerprint=None,
                root_evidence_fingerprint=None,
            )
        restore()
        measured.clear()
        with patch.object(subprocess, "run", new=counted):
            with patch.object(verify, "_read_delivery_status", new=get_delivery_status):
                before = _persist(completion, terminal=terminal)
        before_calls = len(measured)
        before_files = (progress_path.read_bytes(), events_path.read_bytes())
        before_public = get_delivery_status(path).to_dict()
        restore()
        measured.clear()
        with (
            patch.object(subprocess, "run", new=counted),
            patch.object(checkpoints, "with_checkpoint_barriers", side_effect=AssertionError("eager projection")),
        ):
            after = _persist(completion, terminal=terminal)
        assert before == after and after.status == terminal.status
        assert (progress_path.read_bytes(), events_path.read_bytes()) == before_files
        assert 0 < len(measured) < before_calls
        assert get_delivery_status(path).to_dict() == before_public
        if node == "integration":
            selected = select_next_delivery_unit(get_delivery_status(path))
            assert (selected.id if selected else None) == ("shipping" if outcome == "passed" else None)

    # Mutations share expensive setup, but each starts from the original attempt.
    for change in ("plan", "progress", "checkpoint_ids", "attempt", "finished", "branch", "config"):
        restore()
        changed_cfg = deepcopy(cfg)
        if change == "plan":
            path.write_bytes(originals[path] + b"\n# changed authority\n")
        elif change == "progress":
            progress_path.write_bytes(b"{")
        elif change == "checkpoint_ids":
            write_delivery_progress(
                progress_path, replace(progress, checkpoint_ids=(*progress.checkpoint_ids, "unexpected"))
            )
        elif change in {"attempt", "finished"}:
            record = status.verification
            record = replace(record, attempt=record.attempt + 1, status="running") if change == "attempt" else record
            write_delivery_progress(progress_path, _with_record(progress, record, node))
        elif change == "branch":
            _git(root, "update-ref", branch, advanced)
        else:
            changed_cfg["agents"] = {"reviewer": {"llm": {"model": "changed"}}}
        before_files = (progress_path.read_bytes(), events_path.read_bytes())
        assert _persist(completion, cfg=changed_cfg) is None, change
        assert (progress_path.read_bytes(), events_path.read_bytes()) == before_files, change
    restore()
    _assert_completion_evidence_and_boundaries(completion)


def _assert_completion_evidence_and_boundaries(completion: Completion) -> None:
    path, _, status, _ = completion
    root, node = Path(status.project_root), status.verification_node
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    own_evidence = (
        root_evidence_path(progress_path.parent, status.verification.root_evidence_fingerprint)
        if node == "root"
        else checkpoint_evidence_path(progress_path.parent, status.verification)
    )
    leaf = checkpoint_evidence_path(progress_path.parent, status.checkpoint_verifications["storage"])
    for evidence in (own_evidence, leaf):
        content = evidence.read_bytes()
        for mutation in ("missing", "changed"):
            write_delivery_progress(progress_path, progress)
            if mutation == "missing":
                evidence.unlink()
            else:
                evidence.write_bytes(b"{}")
            result = _persist(completion)
            assert result is not None and not result.passed
            assert result.stop_code == (
                "delivery_verification.evidence_unavailable"
                if node == "root"
                else "delivery_checkpoint.evidence_unavailable"
            )
            assert (
                result.root_evidence_fingerprint is None
                if node == "root"
                else result.checkpoint_evidence_fingerprint is None
            )
            evidence.write_bytes(content)

    # A returned security/read-only boundary survives lost evidence and assembly drift.
    leaf.unlink()
    branch = f"refs/heads/{status.plan.final_branch}"
    advanced = _git(
        root, "commit-tree", status.verification.candidate_tree, "-p", status.assembled_commit, "-m", "advance"
    )
    _git(root, "update-ref", branch, advanced)
    for stop in ("security_rejected", "readonly_mutation"):
        write_delivery_progress(progress_path, replace(progress, assembled_commit=advanced))
        terminal = replace(
            status.verification,
            status="blocked",
            security_status="rejected" if stop == "security_rejected" else status.verification.security_status,
            stop_code=f"delivery_verification.{stop}",
            checkpoint_evidence_fingerprint=None,
            root_evidence_fingerprint=None,
        )
        with patch.object(verify, "_read_delivery_status", side_effect=AssertionError("boundary must persist first")):
            result = _persist(completion, terminal=terminal)
        assert result.stop_code == terminal.stop_code
        saved, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
        assert not errors and saved.assembled_commit == advanced
        assert (saved.verification if node == "root" else saved.checkpoint_verifications[node]) == result
        assert select_next_delivery_unit(get_delivery_status(path)) is None
