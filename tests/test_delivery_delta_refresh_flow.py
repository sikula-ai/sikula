from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from core.delivery_candidate_control import expanded_child
from core.delivery_checkpoint_applicability import validate_root_evidence
from core.delivery_composition import CompositionRefresh, build_composition_context, composition_example
from core.delivery_progress import (
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    read_delivery_progress,
    upsert_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_review import delivery_integration_review_control_example
from tests.delivery_fixtures import assemble_delivery_fixture
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority_flow import _run, _assessment
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_conditional_fallback import oversized_parent_fallback as oversized_parent_fallback
from tests.test_delivery_repair import _LLM, _git


def _candidate(root, node):
    path, cfg = nested.nested_plan.__wrapped__(root)
    result, _ = _run(path, cfg, _LLM(_assessment()))
    assert result.succeeded, result
    if node == "root":
        _complete(path, "consumer")
        nested._compose(path, cfg, "integration")
    status = get_delivery_status(path)
    (root / "agents/code.py").write_bytes(b"value = 'large downstream change'\r\n" * 3000)
    _git(root, "add", "agents/code.py")
    tree = _git(root, "write-tree")
    commit = _git(root, "commit-tree", tree, "-p", status.assembled_commit, "-m", "downstream change")
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    unit = "shipping" if node == "root" else "consumer"
    write_delivery_progress(
        progress_path, upsert_delivery_unit_progress(progress, make_delivery_unit_progress(unit, "done", commit=commit))
    )
    _, _, error = assemble_delivery_fixture(path)
    assert error is None, error
    return path, cfg


def _outputs(path, cfg, node):
    before = get_delivery_status(path)
    outputs = {}
    for key in ["integration", "root"] if node == "root" else ["integration"]:
        snapshot = build_delivery_verification_snapshot(
            replace(before, verification_node=key), cfg, candidate_commit=before.assembled_commit
        )
        for role in ("semantic", "security"):
            request = build_composition_context(before, snapshot, cfg, review_kind=role)
            assert isinstance(request, CompositionRefresh), request
            outputs[key, role] = composition_example(request.preview_context)
    return outputs


@pytest.mark.parametrize("node", ["integration", "root"])
def test_large_delta_refreshes_children_then_reviews_parent_integration(tmp_path, node, oversized_parent_fallback):
    path, cfg = _candidate(tmp_path, node)
    before = get_delivery_status(path)
    outputs = _outputs(path, cfg, node)
    parents = ["integration", "root"] if node == "root" else ["integration"]
    semantic = _LLM(_assessment(), *(outputs[key, "semantic"] for key in parents))
    security = _LLM(delivery_integration_review_control_example(set()), *(outputs[key, "security"] for key in parents))
    result, _ = _run(path, cfg, semantic, security, node_id=node)
    assert result.succeeded, result
    after = get_delivery_status(path)
    parent = after.verification if node == "root" else after.checkpoint_verifications[node]
    assert parent.passed and not parent.reverification["fallbacks"]
    assert not parent.reverification["reassessments"]
    assert len(semantic.calls) == len(security.calls) == len(parents) + 1
    assert all(len(prompt.encode()) < 32 * 1024 for prompt in semantic.calls + security.calls)
    assert "large downstream change" not in "".join(semantic.calls + security.calls)
    for prompt in semantic.calls[1:] + security.calls[1:]:
        assert '"checkpoint_composition"' in prompt
    assert [unit for unit in after.units if unit.status == "done"] == [
        unit for unit in before.units if unit.status == "done"
    ]
    assert after.checkpoint_verifications["storage"] == before.checkpoint_verifications["storage"]
    for key in reversed(parents):
        request = parent.reverification["refreshes"]["semantic"]
        assert request["review_reserved"]
        assert {item["reason"] for item in request["nodes"].values()} == {"delta_bytes"}
        child_id = "integration" if key == "root" else "storage"
        parent = expanded_child(after, parent.reverification["children"][child_id])
        assert parent.passed and parent.candidate_commit == after.assembled_commit
    if node == "root":
        validate_root_evidence(after, after.verification)
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id=node)
    assert resumed.succeeded and not unused.calls
    if node != "root":
        _complete(path, "shipping")
        nested._compose(path, cfg, "root")
    from core.delivery_finalize import finalize_delivery_plan

    finalized = finalize_delivery_plan(path, project_config=cfg)
    assert finalized.finalized, finalized


def test_refresh_resume_reuses_accepted_descendants(tmp_path, oversized_parent_fallback):
    from core import delivery_verify

    path, cfg = _candidate(tmp_path, "root")
    outputs = _outputs(path, cfg, "root")
    original = delivery_verify._composition_exchange

    def interrupt(**kwargs):
        if kwargs["snapshot"].scope.node_id == "integration":
            raise KeyboardInterrupt()
        return original(**kwargs)

    semantic = _LLM(_assessment())
    security = _LLM(delivery_integration_review_control_example(set()))
    with patch.object(delivery_verify, "_composition_exchange", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, semantic, security, node_id="root")
    assert len(semantic.calls) == len(security.calls) == 1
    semantic = _LLM(outputs["integration", "semantic"], outputs["root", "semantic"])
    security = _LLM(outputs["integration", "security"], outputs["root", "security"])
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    assert len(semantic.calls) == len(security.calls) == 2


@pytest.mark.parametrize("outcome", ["gap", "security", "readonly"])
def test_refresh_preserves_child_repair_and_terminal_stops(tmp_path, outcome, oversized_parent_fallback):
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = _candidate(tmp_path, "integration")
    semantic = _LLM(_assessment(approved=outcome != "gap"))
    security = _LLM(
        LLMReadOnlyViolation("Private provider diagnostic")
        if outcome == "readonly"
        else json.dumps(
            {
                "schema_version": 2,
                "disposition": "repair_required",
                "summary": "Unsafe.",
                "findings": [
                    {"code": "privacy", "summary": "Disclosure.", "unit_ids": ["foundation"], "obligation_ids": []}
                ],
                "obligation_results": [],
            }
        )
    )
    result, _ = _run(path, cfg, semantic, security, node_id="integration")
    assert not result.succeeded
    expected = {
        "gap": "delivery_verification.candidate_repair_required",
        "security": "delivery_verification.repair_required",
        "readonly": "delivery_verification.readonly_mutation",
    }[outcome]
    assert result.stop_code == expected, result
    assert len(semantic.calls) == 1 and len(security.calls) == (0 if outcome == "gap" else 1)
    if outcome != "gap":
        unused = _LLM()
        again, _ = _run(path, cfg, unused, unused, node_id="integration")
        resume_code = "delivery_verification.security_rejected" if outcome == "security" else expected
        assert again.stop_code == resume_code and not unused.calls
        record = get_delivery_status(path).checkpoint_verifications["integration"]
        assert record.stop_code == expected
        if outcome == "security":
            assert record.security_status == "rejected"
        assert "Private provider diagnostic" not in json.dumps(result.to_dict())


def test_unsupported_candidate_is_blocked_before_verification_reservation(
    tmp_path, monkeypatch, oversized_parent_fallback
):
    from tests import test_delivery_delta_refresh_flow as fixtures
    from core.delivery_verification import check_delivery_verification_readiness

    git = fixtures._git

    def add_binary(root, *args):
        if args == ("add", "agents/code.py"):
            (root / "agents/data.bin").write_bytes(b"\0opaque dependency")
            git(root, "add", "agents/data.bin")
        return git(root, *args)

    monkeypatch.setattr(fixtures, "_git", add_binary)
    path, cfg = _candidate(tmp_path, "integration")
    status = replace(get_delivery_status(path), verification_node="integration")
    readiness = check_delivery_verification_readiness(status, cfg)
    assert not readiness.ready
    assert [issue.code for issue in readiness.errors] == ["delivery_verification.hierarchy_required"]
    unused = _LLM()
    result, _ = _run(path, cfg, unused, unused, node_id="integration")
    assert not result.ready and not unused.calls
    assert "integration" not in get_delivery_status(path).checkpoint_verifications
