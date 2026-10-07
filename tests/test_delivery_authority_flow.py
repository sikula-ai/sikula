from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from core.delivery_checkpoints import checkpoint_pass_is_usable
from core.delivery_progress import (
    DeliveryProgress,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_validation import DeliveryVerificationValidationResult
from core.delivery_verify import verify_delivery_plan
from core.llm_client import LLMReadOnlyViolation
from core.state import JsonStateStore
from tests.test_delivery_authority import _prepare
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git, _draft, _repair


def _assessment(approved=True):
    return json.dumps(
        {
            "schema_version": 2,
            "disposition": "approved" if approved else "repair_required",
            "summary": "Candidate checked.",
            "findings": []
            if approved
            else [
                {
                    "code": "integration_gap",
                    "summary": "Storage integration needs an authorized correction.",
                    "unit_ids": ["foundation"],
                    "obligation_ids": ["deliver-team-invites"],
                }
            ],
            "obligation_results": [{"id": "deliver-team-invites", "outcome": "satisfied" if approved else "missing"}],
        }
    )


def _run(path, cfg, semantic, security=None, node_id="storage"):
    root = Path(cfg["project"]["root_path"])
    security = security or _LLM(
        '{"schema_version":2,"disposition":"approved","summary":"Secure.","findings":[],"obligation_results":[]}'
    )
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            project_root=root,
            node_id=node_id,
            semantic_reviewer=DeliveryIntegrationReviewAgent(semantic, cfg),
            security_reviewer=DeliveryIntegrationReviewAgent(security, cfg),
            state_store=JsonStateStore(root / ".sikula/state"),
        )
    return result, security


@pytest.fixture
def scoped_plan(tmp_path):
    path, cfg, _, _, _, _ = _prepare(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n")
    (tmp_path / "agents/code.py").write_text("value = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "independently prepared plan")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "team-invites"),
        DeliveryProgress(
            schema_version=1,
            plan_id="team-invites",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress("foundation", "done", commit=commit)],
        ),
    )
    return path, cfg


@pytest.mark.parametrize("interrupted", [False, True])
def test_scoped_gate_persists_evidence_and_resumes_without_expanding_authority(scoped_plan, interrupted):
    path, cfg = scoped_plan
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, _LLM(KeyboardInterrupt()))
    semantic = _LLM(_assessment())
    result, security = _run(path, cfg, semantic)
    assert result.succeeded, result
    assert len(semantic.calls) == len(security.calls) == 1
    assert all("CACHE_KEY" in prompt and "UNRELATED" not in prompt for prompt in semantic.calls + security.calls)
    status = get_delivery_status(path)
    assert status.checkpoint_verifications["storage"].checkpoint_evidence_fingerprint
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    # A second invocation loads immutable typed evidence without another provider call.
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused)
    assert resumed.succeeded, resumed
    assert not unused.calls
    _complete(path, "consumer")
    final_llm = _LLM(_assessment(), _assessment(), _assessment())
    final, security = _run(path, cfg, final_llm, node_id="root")
    assert final.succeeded, final
    assert final_llm.calls and all("UNRELATED" in prompt for prompt in final_llm.calls + security.calls)


def test_scoped_gate_retains_terminal_readonly_boundary(scoped_plan):
    path, cfg = scoped_plan
    result, _ = _run(path, cfg, _LLM(LLMReadOnlyViolation("Provider workspace changed")))
    assert result.stop_code == "delivery_verification.readonly_mutation"
    path.write_text(path.read_text() + "\n# unrelated comment\n")
    unused = _LLM(_assessment())
    result, _ = _run(path, cfg, unused)
    assert result.stop_code in {"delivery_verification.readonly_mutation", "delivery_checkpoint.readonly_mutation"}
    assert not unused.calls


def test_checkpoint_repair_falls_back_to_full_authority_for_widened_scope(scoped_plan):
    path, cfg = scoped_plan
    result, _ = _run(path, cfg, _LLM(_assessment(False)))
    assert result.stop_code == "delivery_verification.repair_required", result
    before = get_delivery_status(path)
    contract = Path(cfg["project"]["root_path"]) / before.plan.units[0].task_path
    author = _LLM(_draft(markdown=contract.read_text()))
    repaired = _repair(path, cfg, author, node_id="storage")
    assert repaired.ready, repaired
    status = get_delivery_status(path)
    assert "storage" not in status.plan.verified_checkpoint_authority
    assert "authority_packet" not in DeliveryVerificationScope.from_plan(status.plan, "storage").plan_context()
    _complete(path, repaired.unit_id)
    semantic = _LLM(_assessment())
    result, security = _run(path, cfg, semantic)
    assert result.succeeded, result
    assert all("UNRELATED" in prompt for prompt in semantic.calls + security.calls)
