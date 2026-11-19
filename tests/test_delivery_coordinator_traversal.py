from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from core import delivery_checkpoints as checkpoints
from core.delivery_checkpoint_evidence import checkpoint_evidence_path
from core.delivery_evidence_resolution import EvidenceResolutionLimit
from core.delivery_finalize import finalize_delivery_plan
from core.delivery_progress import _read_delivery_status, delivery_progress_path
from core.delivery_verification import with_delivery_verification_readiness
from core.state import JsonStateStore
from sikula_cli.delivery import DeliveryRunNextContext, _delivery_run_readiness, _run_delivery_plan
from tests import test_delivery_checkpoints as fixtures
from tests.test_delivery_checkpoints import _complete, _git, _verify_node


@pytest.fixture
def checkpoint_plan(tmp_path):
    return fixtures.checkpoint_plan.__wrapped__(tmp_path)


@pytest.mark.parametrize("state", ["accepted", "due", "policy_changed", "boundary"])
def test_coordinator_read_phase_preserves_status_and_selection(checkpoint_plan, state):
    path, cfg = checkpoint_plan
    if state != "due":
        assert _verify_node(path, cfg)[0].succeeded
    status = _read_delivery_status(path)
    if state == "policy_changed":
        cfg = {**cfg, "agents": {"reviewer": {"llm": {"model": "changed"}}}}
    if state == "boundary":
        record = replace(
            status.checkpoint_verifications["storage"],
            status="blocked",
            security_status="rejected",
            stop_code="delivery_verification.security_rejected",
        )
        status = replace(status, checkpoint_verifications={"storage": record})
    with patch("subprocess.run", wraps=subprocess.run) as separate:
        expected_status = with_delivery_verification_readiness(status, cfg)
        expected_node = checkpoints.due_delivery_checkpoint(expected_status, cfg)
    with patch("subprocess.run", wraps=subprocess.run) as shared:
        projected, node = _delivery_run_readiness(status, cfg)
    assert projected.to_dict() == expected_status.to_dict()
    assert node == expected_node
    if state == "accepted":
        # Count physical Git work, independently of cache/helper implementation.
        def payload_reads(calls):
            return sum("ls-tree" in call.args[0] or "cat-file" in call.args[0] for call in calls)

        assert payload_reads(shared.call_args_list) < payload_reads(separate.call_args_list)


def test_coordinator_selection_rechecks_live_branch_after_projection(checkpoint_plan, monkeypatch):
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    status = _read_delivery_status(path)
    captured = []

    def project(current, config, *, resolver):
        result = with_delivery_verification_readiness(current, config, resolver=resolver)
        assert result.checkpoint_handoffs == frozenset({"storage"})
        captured.append(resolver)
        _git(path.parent, "update-ref", f"refs/heads/{status.plan.final_branch}", status.assembled_commit + "^")
        return result

    # Use a new descendant so its parent is available independently of fixture history.
    _complete(path, "consumer")
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    status = _read_delivery_status(path)
    monkeypatch.setattr("core.delivery_verification.with_delivery_verification_readiness", project)
    _, node = _delivery_run_readiness(status, cfg)
    assert node == "storage"
    with pytest.raises(EvidenceResolutionLimit):
        captured[0]._check()


def _context(root: Path) -> DeliveryRunNextContext:
    def forbidden(*args, **kwargs):
        pytest.fail("No provider or unit work is permitted")

    return DeliveryRunNextContext(
        run_task=forbidden,
        verify_plan=forbidden,
        resolve_state_dir=lambda cfg: root / ".sikula/state",
        state_store=JsonStateStore(root / ".sikula/state"),
    )


def _args(path: Path) -> argparse.Namespace:
    return argparse.Namespace(plan_file=str(path), max_units=1, max_elapsed_minutes=None, reset_failed=False)


def test_coordinator_integrity_failure_preempts_unit_and_provider_work(checkpoint_plan, monkeypatch):
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    status = _read_delivery_status(path)
    evidence = checkpoint_evidence_path(
        delivery_progress_path(path.parent, status.plan.plan_id).parent, status.checkpoint_verifications["storage"]
    )
    original = checkpoints.due_delivery_checkpoint

    def select(*args, **kwargs):
        node = original(*args, **kwargs)
        evidence.write_bytes(evidence.read_bytes() + b" ")
        return node

    monkeypatch.setattr(checkpoints, "due_delivery_checkpoint", select)
    result = _run_delivery_plan(_args(path), cfg, _context(path.parent), project_root=path.parent)
    assert result.stop_code == "delivery_checkpoint.evidence_unavailable"
    assert not result.ready and not result.succeeded and result.units_attempted == 0
    checkpoint = result.to_dict()["checkpoints"][0]
    assert checkpoint["candidate_evidence"] == "unavailable"
    assert checkpoint["status"] != "accepted_handoff"


def test_completed_run_avoids_preliminary_whole_plan_projections(checkpoint_plan):
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _complete(path, "consumer")
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    assert finalize_delivery_plan(path, project_root=path.parent, project_config=cfg).finalized
    with patch.object(
        checkpoints, "_checkpoint_local_pass_is_usable", wraps=checkpoints._checkpoint_local_pass_is_usable
    ) as checks:
        result = _run_delivery_plan(_args(path), cfg, _context(path.parent), project_root=path.parent)
    assert result.finalized and result.units_attempted == 0
    # Readiness, fresh selection, finalizer admission and public result projection.
    # Finalizer routing must not add a fifth hierarchy projection.
    assert checks.call_count <= 4
    assert result.to_dict()["checkpoints"][0]["status"] == "accepted_handoff"


@pytest.mark.parametrize("change", ["branch", "evidence", "security_stop"])
def test_finalizer_rechecks_authority_after_coordinator_readiness(checkpoint_plan, monkeypatch, change):
    from core.delivery_progress import read_delivery_progress, write_delivery_progress
    from sikula_cli import delivery

    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _complete(path, "consumer")
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    assert finalize_delivery_plan(path, project_root=path.parent, project_config=cfg).finalized
    original = delivery._finalize_delivery_run
    reached = []

    def finalize(*args, **kwargs):
        assert kwargs["readiness_checked"]
        status = kwargs["status"]
        assert status.verification_status == "passed"
        reached.append(True)
        progress_path = delivery_progress_path(path.parent, status.plan.plan_id)
        if change == "branch":
            _git(path.parent, "update-ref", f"refs/heads/{status.plan.final_branch}", status.assembled_commit + "^")
        elif change == "evidence":
            evidence = checkpoint_evidence_path(progress_path.parent, status.checkpoint_verifications["storage"])
            evidence.write_bytes(b"{")
        else:
            progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
            assert progress is not None and not errors
            write_delivery_progress(
                progress_path,
                replace(
                    progress,
                    checkpoint_verifications={
                        **progress.checkpoint_verifications,
                        "storage": replace(
                            progress.checkpoint_verifications["storage"],
                            status="blocked",
                            security_status="rejected",
                            stop_code="delivery_verification.security_rejected",
                        ),
                    },
                ),
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "_finalize_delivery_run", finalize)
    result = _run_delivery_plan(_args(path), cfg, _context(path.parent), project_root=path.parent)
    assert reached == [True]
    assert not result.finalized and not result.succeeded and result.units_attempted == 0
    assert result.stop_code == "delivery.run.finalize_failed"
    assert result.errors
    if change == "security_stop":
        assert "delivery_verification.security_rejected" in {issue.code for issue in result.errors}
