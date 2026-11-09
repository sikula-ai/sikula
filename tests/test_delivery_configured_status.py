"""Configured status retains full barriers with one checkpoint projection."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import subprocess
from unittest.mock import patch

import pytest

from core import delivery_checkpoints as checkpoints
from core.delivery_checkpoint_evidence import checkpoint_evidence_path
from core.delivery_evidence_resolution import EvidenceResolutionLimit, EVIDENCE_BUDGET_STOP
from core.delivery_progress import (
    delivery_progress_path,
    get_delivery_status,
    read_delivery_progress,
    select_next_delivery_unit,
    write_delivery_progress,
)
from core.delivery_verification import get_delivery_verification_status, with_delivery_verification_readiness
from sikula_cli.delivery import cmd_delivery_status
from tests.test_delivery_checkpoints import checkpoint_plan as checkpoint_plan, _verify_node, _git


def test_configured_status_projects_once_and_rechecks_policy_and_evidence(checkpoint_plan, capsys):
    path, cfg = checkpoint_plan
    cfg["agents"] = {"reviewer": {"llm": {"model": "checkpoint-review-model"}}}
    assert _verify_node(path, cfg)[0].succeeded
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    record = progress.checkpoint_verifications["storage"]
    artifact = checkpoint_evidence_path(progress_path.parent, record)
    source = path.parent / "source.md"
    # Derive the authority path rather than depending on fixture filenames.
    contract = path.parent / get_delivery_status(path).plan.units[0].task_path
    originals = {p: p.read_bytes() for p in (path, progress_path, artifact, source, contract)}
    branch = f"refs/heads/{get_delivery_status(path).plan.final_branch}"
    candidate = _git(path.parent, "rev-parse", branch)
    measured = []
    real_run = subprocess.run

    def counted(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        if command and command[0] == "git":
            measured.append(1)
        return real_run(*args, **kwargs)

    for change in ("none", "policy", "missing_evidence", "changed_evidence", "source", "contract", "plan", "progress"):
        for p, content in originals.items():
            p.write_bytes(content)
        effective = deepcopy(cfg)
        if change == "policy":
            effective["agents"]["reviewer"]["llm"]["model"] = "other-model"
        elif change == "missing_evidence":
            artifact.unlink()
        elif change == "changed_evidence":
            modified = json.loads(originals[artifact])
            modified["schema_version"] = 999
            artifact.write_text(json.dumps(modified), encoding="utf-8")
        elif change in {"source", "contract", "plan"}:
            target = {"source": source, "contract": contract, "plan": path}[change]
            target.write_bytes(originals[target] + b"\nchanged authority\n")
        elif change == "progress":
            progress_path.write_bytes(b"{")
        measured.clear()
        with patch.object(subprocess, "run", new=counted):
            before = with_delivery_verification_readiness(get_delivery_status(path), effective)
        before_calls = len(measured)
        measured.clear()
        with (
            patch.object(subprocess, "run", new=counted),
            patch.object(
                checkpoints, "with_checkpoint_barriers", wraps=checkpoints.with_checkpoint_barriers
            ) as projection,
        ):
            after = get_delivery_verification_status(path, effective, project_root=path.parent)
        assert after.to_dict() == before.to_dict(), change
        assert select_next_delivery_unit(after) == select_next_delivery_unit(before), change
        assert projection.call_count <= 1
        if change == "none":
            assert projection.call_count == 1
            assert projection.call_args.args[1] == effective
            assert 0 < len(measured) < before_calls
            assert select_next_delivery_unit(after).id == "consumer"
        else:
            assert not after.valid or select_next_delivery_unit(after) is None, change
        assert _git(path.parent, "rev-parse", branch) == candidate
    for p, content in originals.items():
        p.write_bytes(content)
    # Public text and JSON use the same configured projection and retain policy overrides.
    for json_output in (True, False):
        args = argparse.Namespace(
            plan_file=str(path), json=json_output, agent_model=["reviewer=checkpoint-review-model"], agent_provider=None
        )
        with patch.object(
            checkpoints, "with_checkpoint_barriers", wraps=checkpoints.with_checkpoint_barriers
        ) as projection:
            cmd_delivery_status(args, cfg)
        output = capsys.readouterr().out
        assert projection.call_count == 1
        if json_output:
            assert json.loads(output)["checkpoints"][0]["status"] == "accepted_handoff"
        else:
            assert "Checkpoint storage: accepted_handoff" in output
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize("boundary", ["security_rejected", "readonly_mutation"])
def test_configured_status_preserves_terminal_barriers_without_sidecars(checkpoint_plan, boundary):
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    record = progress.checkpoint_verifications["storage"]
    checkpoint_evidence_path(progress_path.parent, record).unlink()
    terminal = replace(
        record,
        status="blocked",
        stop_code=f"delivery_verification.{boundary}",
        security_status="rejected" if boundary == "security_rejected" else "not_run",
    )
    write_delivery_progress(progress_path, replace(progress, checkpoint_verifications={"storage": terminal}))
    with patch.object(checkpoints, "_with_checkpoint_barriers", side_effect=AssertionError("read sidecars")):
        status = get_delivery_verification_status(path, cfg)
    assert checkpoints.checkpoint_boundary_issue(status).code == terminal.stop_code
    assert select_next_delivery_unit(status) is None
    assert next(unit for unit in status.units if unit.id == "consumer").blocked_by_checkpoints == ["storage"]


def test_configured_status_retains_resource_failure(checkpoint_plan):
    path, cfg = checkpoint_plan
    with patch.object(checkpoints, "_with_checkpoint_barriers", side_effect=EvidenceResolutionLimit("exhausted")):
        status = get_delivery_verification_status(path, cfg)
    assert not status.valid
    assert status.errors[-1].code == EVIDENCE_BUDGET_STOP
