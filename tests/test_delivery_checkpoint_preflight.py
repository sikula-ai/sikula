"""Checkpoint recovery checks retain fresh authority and ordered blockers."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from core import delivery_checkpoints as checkpoints
from core.delivery_checkpoint_evidence import checkpoint_evidence_path
from core.delivery_evidence_resolution import EVIDENCE_BUDGET_STOP
from core.delivery_progress import delivery_progress_path, get_delivery_status
from core.delivery_verification import with_delivery_verification_readiness
from core.delivery_verification_review import delivery_integration_review_control_example
from core.state import JsonStateStore
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git


@pytest.fixture
def accepted_parent(tmp_path):
    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    assert _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))[0].succeeded
    _complete(path, "consumer")
    nested._compose(path, cfg, "integration")
    status = checkpoints.verification_node_status(get_delivery_status(path), "integration")
    assert status.verification.passed
    return status, cfg, JsonStateStore(tmp_path / ".sikula/state")


def test_preflight_shares_reads_but_rechecks_handoffs_and_keeps_fresh_projection(accepted_parent):
    status, cfg, store = accepted_parent
    original_run = subprocess.run
    with patch.object(subprocess, "run", wraps=original_run) as git:
        # Without an enclosing resolver, each traversal creates its own read
        # operation, as before. Compare identical authority and successful checks.
        assert checkpoints._checkpoint_preflight_issue.__wrapped__(status, cfg, store) is None
    before_calls = sum(call.args[0][0] == "git" for call in git.call_args_list)
    decisions = []
    for _ in range(2):
        with (
            patch.object(
                checkpoints, "_checkpoint_local_pass_is_usable", wraps=checkpoints._checkpoint_local_pass_is_usable
            ) as checked,
            patch.object(subprocess, "run", wraps=original_run) as git,
        ):
            assert checkpoints.checkpoint_preflight_issue(status, cfg, store) is None
        assert 0 < sum(call.args[0][0] == "git" for call in git.call_args_list) < before_calls
        # Barrier, configured projection and immediate-child authority checks all
        # remain. Only the first and last share immutable reads.
        assert Counter(call.args[1].id for call in checked.call_args_list) == {"storage": 3, "integration": 2}
        resolvers = {id(call.kwargs["resolver"]): call.kwargs["resolver"] for call in checked.call_args_list}
        assert len(resolvers) == 2
        decisions.append(resolvers)
    assert not decisions[0].keys() & decisions[1].keys()

    changed = deepcopy(cfg)
    changed["agents"] = {"reviewer": {"llm": {"model": "changed"}}}
    assert checkpoints.checkpoint_preflight_issue(status, changed, store).code == "delivery_checkpoint.required"


@pytest.mark.parametrize("change", ["ref", "contract", "policy"])
def test_preflight_rechecks_mutable_authority_after_readiness(accepted_parent, change):
    status, cfg, store = accepted_parent
    root = Path(status.project_root)

    def readiness(current, config):
        projected = with_delivery_verification_readiness(current, config)
        if change == "ref":
            _git(root, "update-ref", "-d", "refs/heads/" + status.plan.final_branch)
        elif change == "contract":
            contract = root / status.plan.units[0].task_path
            contract.write_bytes(contract.read_bytes() + b"\nChanged requirement.\n")
        else:
            config["agents"] = {"reviewer": {"llm": {"model": "changed"}}}
        return projected

    with patch("core.delivery_verification.with_delivery_verification_readiness", side_effect=readiness):
        issue = checkpoints.checkpoint_preflight_issue(status, cfg, store)
    assert issue.code == "delivery_checkpoint.required"


@pytest.mark.parametrize("early_return", [False, True])
def test_preflight_rechecks_barrier_integrity_after_readiness(accepted_parent, early_return):
    status, cfg, store = accepted_parent
    root = Path(status.project_root)
    artifact = checkpoint_evidence_path(
        delivery_progress_path(root, status.plan.plan_id).parent, status.checkpoint_verifications["storage"]
    )
    original = artifact.read_bytes()

    def readiness(current, config):
        projected = with_delivery_verification_readiness(current, config)
        artifact.write_bytes(original + b" ")
        if early_return:
            projected = replace(
                projected,
                verification=replace(
                    projected.verification,
                    status="blocked",
                    stop_code="delivery_verification.external_dependency_gap",
                ),
            )
        return projected

    with patch("core.delivery_verification.with_delivery_verification_readiness", side_effect=readiness):
        issue = checkpoints.checkpoint_preflight_issue(status, cfg, store)
    assert issue.code == "delivery_checkpoint.evidence_unavailable"


def test_preflight_keeps_budget_failures_and_terminal_boundaries(accepted_parent):
    status, cfg, store = accepted_parent

    def exhausted(*args, resolver, **kwargs):
        resolver._charge("reads", resolver.limits.reads + 1)

    with patch.object(checkpoints, "_checkpoint_local_pass_is_usable", side_effect=exhausted):
        assert checkpoints.checkpoint_preflight_issue(status, cfg, store).code == EVIDENCE_BUDGET_STOP
    for code, security in (("security_rejected", "rejected"), ("readonly_mutation", "approved")):
        boundary = replace(
            status,
            verification=replace(
                status.verification,
                status="blocked",
                security_status=security,
                stop_code="delivery_verification." + code,
            ),
        )
        with patch.object(
            checkpoints, "_checkpoint_local_pass_is_usable", side_effect=AssertionError("boundary must preempt reads")
        ):
            assert checkpoints.checkpoint_preflight_issue(boundary, cfg, store).code == boundary.verification.stop_code
