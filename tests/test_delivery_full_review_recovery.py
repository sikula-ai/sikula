from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.delivery_candidate_repair import CANDIDATE_REPAIR_REQUIRED, resolve_candidate_repair
from core.delivery_candidate_control import expanded_child
from core.delivery_progress import get_delivery_status
from core.delivery_reverification import full_review_child_request, load_full_result
from core.delivery_verification_review import (
    parse_delivery_integration_review,
    delivery_integration_review_control_example,
)
from tests import test_delivery_recursive_reverification as recursive
from tests.test_delivery_recursive_reverification import _gap, _outputs
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_repair import _LLM, _repair


@pytest.fixture
def candidate(tmp_path):
    return recursive.candidate.__wrapped__(tmp_path)


def _full_integration_exchange(original):
    def exchange(**kwargs):
        if kwargs["snapshot"].scope.node_id == "integration":
            return None, kwargs["running"]
        return original(**kwargs)

    return exchange


@pytest.mark.parametrize("fault", [None, "missing", "corrupt", "unreconciled"])
def test_conditional_readiness_defers_only_intact_reconciled_child_admissions(candidate, fault):
    from core.delivery_checkpoint_evidence import checkpoint_evidence_path
    from core.delivery_checkpoints import due_delivery_checkpoint, reconcile_checkpoint_assembly
    from core.delivery_progress import delivery_progress_path
    from core.delivery_verification import check_delivery_verification_readiness
    from tests.test_delivery_checkpoints import _complete
    from tests.test_delivery_repair import _draft

    path, cfg = candidate
    outputs = _outputs(path, cfg)
    semantic = _LLM(
        outputs["root", "semantic"][1],
        outputs["integration", "semantic"][1],
        _gap({"deliver-team-invites"}),
    )
    result, _ = _run(path, cfg, semantic, node_id="root")
    assert result.stop_code == CANDIDATE_REPAIR_REQUIRED, result
    status = get_delivery_status(path)
    root = Path(status.project_root)
    repaired = _repair(path, cfg, _LLM(_draft(markdown=(root / status.plan.units[0].task_path).read_text())))
    assert repaired.ready, repaired
    _complete(path, repaired.unit_id)
    status = reconcile_checkpoint_assembly(get_delivery_status(path), persist=True)
    assert all(unit.status == "done" for unit in status.units)
    assert status.checkpoint_verifications["integration"].passed
    directory = delivery_progress_path(root, status.plan.plan_id).parent
    artifact = checkpoint_evidence_path(directory, status.checkpoint_verifications["integration"])
    if fault == "missing":
        artifact.unlink()
    elif fault == "corrupt":
        artifact.write_bytes(b"{}")
    with patch("core.delivery_verification.MAX_DELIVERY_LOCAL_OBLIGATIONS", 1):
        if fault == "unreconciled":
            with patch("core.delivery_repair.candidate_repair_reconciles_checkpoint", return_value=False):
                readiness = check_delivery_verification_readiness(status, cfg)
        else:
            readiness = check_delivery_verification_readiness(status, cfg)
    if fault:
        assert not readiness.ready
        assert {issue.code for issue in readiness.errors} == {"delivery_verification.evidence_unavailable"}
    else:
        assert readiness.ready, readiness
        # Readiness authorizes scheduling, never final acceptance of the old pass.
        assert due_delivery_checkpoint(status, cfg) == "storage"


@pytest.mark.parametrize("outcome", ["gap", "security_request", "bounded_recovery", "approved", "security", "readonly"])
def test_full_subgroup_rejection_requests_child_without_discarding_parent(candidate, outcome):
    from core import delivery_verify
    from core.llm_client import LLMReadOnlyViolation

    bounded_recovery = outcome == "bounded_recovery"
    if bounded_recovery:
        outcome = "security_request"
    path, cfg = candidate
    before = get_delivery_status(path)
    outputs = _outputs(path, cfg)
    semantic = _LLM(
        outputs["root", "semantic"][outcome != "security_request"],
        _gap({"deliver-team-invites", "visible"}),
        _gap({"deliver-team-invites"})
        if outcome in {"gap", "security_request"}
        else LLMReadOnlyViolation("readonly")
        if outcome == "readonly"
        else delivery_integration_review_control_example({"deliver-team-invites"}),
        # Root fallback retains the unresolved original rejection if the child passes.
        _gap({"deliver-team-invites", "visible"}),
    )
    security = _LLM(
        outputs["root", "security"][1]
        if outcome == "security_request"
        else json.dumps(
            {
                "schema_version": 2,
                "disposition": "human_review_required" if outcome == "security" else "approved",
                "summary": "Security checked.",
                "findings": [
                    {
                        "code": "security",
                        "summary": "Security decision required.",
                        "unit_ids": ["foundation"],
                        "obligation_ids": [],
                    }
                ]
                if outcome == "security"
                else [],
                "obligation_results": [],
            }
        )
    )
    with patch.object(
        delivery_verify, "_composition_exchange", new=_full_integration_exchange(delivery_verify._composition_exchange)
    ):
        if outcome == "gap":
            original_child = delivery_verify._reverify_candidate_child

            def interrupt_before_child(**kwargs):
                if kwargs["node_id"] == "storage":
                    raise KeyboardInterrupt()
                return original_child(**kwargs)

            with patch.object(delivery_verify, "_reverify_candidate_child", new=interrupt_before_child):
                with pytest.raises(KeyboardInterrupt):
                    _run(path, cfg, semantic, security, node_id="root")
            assert len(semantic.calls) == 2
            original_route = delivery_verify._route_candidate_repair

            def interrupt_after_child(status, parent, child, child_id, config):
                if child_id == "storage":
                    raise KeyboardInterrupt()
                return original_route(status, parent, child, child_id, config)

            with patch.object(delivery_verify, "_route_candidate_repair", new=interrupt_after_child):
                with pytest.raises(KeyboardInterrupt):
                    _run(path, cfg, semantic, security, node_id="root")
            assert len(semantic.calls) == 3
        result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert not result.succeeded, result
    status = get_delivery_status(path)
    integration = expanded_child(status, status.verification.reverification["children"]["integration"])
    negative = load_full_result(replace(status, verification_node="integration"), integration, "semantic")
    assert negative.disposition == "repair_required"
    assert status.checkpoint_verifications == before.checkpoint_verifications
    if outcome in {"gap", "security_request"}:
        assert result.stop_code == CANDIDATE_REPAIR_REQUIRED, result
        assert len(semantic.calls) == 3
        assert len(security.calls) == (1 if outcome == "security_request" else 0)
        selected, binding = resolve_candidate_repair(status, cfg)
        assert selected.verification_node == "storage" and binding["node_id"] == "root"
        unused = _LLM()
        assert _repair(path, cfg, unused, dry_run=True).ready
        replay, _ = _run(path, cfg, unused, unused, node_id="root")
        assert replay.stop_code == CANDIDATE_REPAIR_REQUIRED and not unused.calls
        if outcome == "security_request":
            from pathlib import Path
            from tests.test_delivery_repair import _draft
            from tests.test_delivery_candidate_repair import _finish_with_delivery_run

            root = Path(status.project_root)
            author = _LLM(_draft(markdown=(root / selected.plan.units[0].task_path).read_text()))
            repaired = _repair(path, cfg, author)
            assert repaired.ready and len(author.calls) == 1, repaired
            after = get_delivery_status(path)
            assert after.checkpoint_verifications == before.checkpoint_verifications
            assert after.plan.verified_final_gate_authority
            assert set(after.plan.verified_composition_authority) == {"integration"}
            assert [unit for unit in after.plan.units if unit.id != repaired.unit_id] == list(before.plan.units)
            if bounded_recovery:
                # Force the production failure's route on this small fixture:
                # leaves fit, but ancestors must use checked composition after
                # renewal instead of falling back to expanded full review.
                with patch("core.delivery_verification.MAX_DELIVERY_LOCAL_OBLIGATIONS", 1):
                    _finish_with_delivery_run(path, cfg, repaired.unit_id)
            else:
                _finish_with_delivery_run(path, cfg, repaired.unit_id)
    elif outcome == "approved":
        assert integration.semantic_status == "rejected", (result, integration)
        assert expanded_child(status, integration.reverification["children"]["storage"]).passed
        assert result.stop_code == "delivery_verification.repair_required", result
        unused = _LLM()
        replay, _ = _run(path, cfg, unused, unused, node_id="root")
        assert replay.stop_code == "delivery_verification.repair_required" and not unused.calls
    else:
        assert result.stop_code == (
            "delivery_verification.readonly_mutation"
            if outcome == "readonly"
            else "delivery_verification.human_review_required"
        ), result
        unused = _LLM()
        replay, _ = _run(path, cfg, unused, unused, node_id="root")
        assert not replay.succeeded and not unused.calls


@pytest.mark.parametrize(
    "change",
    ["valid", "owner_only", "retained_gap", "mixed", "unknown_owner", "unbound_finding", "partial", "no_authority"],
)
def test_full_review_confirmation_requires_complete_single_child_attribution(tmp_path, change):
    from core.delivery_composition_scope import delivery_composition_scope
    from core.delivery_verification_scope import DeliveryVerificationScope
    from tests.test_delivery_nested_checkpoints import _prepared

    _, _, checked, _, _, _ = _prepared(tmp_path)
    plan = checked.plan
    scope = delivery_composition_scope(plan, DeliveryVerificationScope.from_plan(plan, "integration"))
    data = json.loads(_gap({"deliver-team-invites", "visible"}))
    if change in {"retained_gap", "mixed"}:
        for result in data["obligation_results"]:
            if result["id"] == "visible":
                result["outcome"] = "missing"
            elif change == "retained_gap":
                result["outcome"] = "satisfied"
        if change == "retained_gap":
            data["findings"][0]["obligation_ids"] = ["visible"]
        else:
            data["findings"][0]["obligation_ids"].append("visible")
    if change == "unknown_owner":
        data["findings"][0]["unit_ids"] = ["consumer"]
    if change == "owner_only":
        data["findings"][0]["unit_ids"] = []
    if change == "unbound_finding":
        data["findings"].append({**data["findings"][0], "obligation_ids": []})
    assessment = parse_delivery_integration_review(
        json.dumps(data),
        known_unit_ids=set(scope.parent.unit_ids),
        known_obligation_ids=set(scope.parent.obligation_ids),
    )
    if change == "partial":
        assessment = replace(assessment, obligation_results=assessment.obligation_results[:1])
    if change == "no_authority":
        scope = replace(scope, retained_authority_json=None)
    assert full_review_child_request(scope, assessment, plan) == (
        "storage" if change in {"valid", "owner_only"} else None
    )
