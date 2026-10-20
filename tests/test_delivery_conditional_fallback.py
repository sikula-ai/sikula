from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent, DeliveryIntegrationReviewAgentError
from core.delivery_composition import build_node_composition_context
from core.delivery_composition_scope import DeliveryCompositionAttempt
from core.delivery_review_route import preview_composition_context, review_route
from core.delivery_verification import check_delivery_verification_readiness
from core.delivery_verification_scope import DeliveryVerificationScope
from tests.test_delivery_authority import _prepare
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input
from tests.test_delivery_composition_scope import node_input as node_input
from tests import test_delivery_nested_checkpoints as nested


@pytest.fixture
def oversized_parent_fallback(monkeypatch):
    """Exercise existing recovery scenarios with unavailable expanded parent packets."""
    prompt = DeliveryIntegrationReviewAgent._prompt

    def rendered(self, **kwargs):
        value = prompt(self, **kwargs)
        context = kwargs["plan_context"]
        if (
            context.get("verification_node", {}).get("id", "root") != "storage"
            and "checkpoint_composition" not in context
        ):
            return value + "\n" + "Expanded descendant authority.\n" * 20000
        return value

    monkeypatch.setattr(DeliveryIntegrationReviewAgent, "_prompt", rendered)


def _large_prepared(root):
    source = (
        "# Delivery\nPreserve shared behavior.\n\n## Foundation\nUse `CACHE_KEY`.\n"
        + "\U0001f600" * 14500
        + "\n\n## Future\nPresent updated data.\n"
        + "\U0001f600" * 14500
        + "\n"
    )
    data = nested._data(source)
    replacements = {
        item["id"]: [f"{item['id']}-{index}-" + "x" * 870 for index in range(20)] for item in data["obligations"]
    }
    data["obligations"] = [
        {**deepcopy(item), "id": identifier} for item in data["obligations"] for identifier in replacements[item["id"]]
    ]
    for item in [*data["source_accounting"], *data["checkpoints"]]:
        item["obligation_ids"] = [identifier for old in item["obligation_ids"] for identifier in replacements[old]]
    result = _prepare(root, source, data, verification={"final_gate_authority_complete": True})
    (root / "review-rules.md").write_bytes(b"Check integration invariants.\n" * 4000)
    for role in ("reviewer", "security_reviewer"):
        result[1][role] = {"extra_rules": "review-rules.md"}
    return result


def test_preparation_accepts_real_nested_packets_without_expanded_fallback(tmp_path):
    path, cfg, checked, *_ = _large_prepared(tmp_path)
    source = (tmp_path / "source.md").read_text(encoding="utf-8")
    from core.delivery_composition_scope import delivery_composition_scope

    for node in ("root", "integration", "storage"):
        scope = DeliveryVerificationScope.from_plan(checked.plan, node)
        composition = delivery_composition_scope(checked.plan, scope)
        readiness = check_delivery_verification_readiness(checked, cfg, node_id=node)
        assert readiness.ready, readiness.errors
        assert readiness.source_bytes < 128 * 1024 and readiness.plan_bytes < 256 * 1024
        for role in ("semantic", "security"):
            route = review_route(
                scope, cfg, tmp_path, source, role, context=preview_composition_context(composition, role)
            )
            assert route.ready
            if node == "storage":
                assert route.full_available
            else:
                assert not route.full_available, (node, role, route)
                assert route.composition_available and route.packet_bytes < 512 * 1024


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_full_packet_growth_does_not_grow_local_prompt(node_input, role):
    root, _, source, snapshot, parent, historical = node_input
    lengths = []
    for size in (10000, 600000):
        context = snapshot.scope.plan_context()
        context["units"][0]["description"] = "x" * size
        scope = replace(snapshot.scope, _context_json=json.dumps(context))
        composition = replace(snapshot.composition_scope, parent=scope)
        current = replace(snapshot, scope=scope, composition_scope=composition)
        packet = build_node_composition_context(
            root, current, DeliveryCompositionAttempt.capture(composition, historical, parent), {}, review_kind=role
        )
        route = review_route(scope, {}, root, source, role, context=packet)
        assert route.composition_available
        lengths.append(route.composition_bytes)
        assert route.full_available == (size == 10000)
    assert lengths[0] == lengths[1]


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_unavailable_full_review_stops_before_reserving_or_calling(node_input, monkeypatch, role):
    from core import delivery_verify
    from core.delivery_reverification import new_reverification
    from core.delivery_verification_validation import DeliveryVerificationValidationResult

    root, directory, source, snapshot, running, _ = node_input
    context = snapshot.scope.plan_context()
    context["units"][0]["description"] = "x" * 600000
    scope = replace(snapshot.scope, _context_json=json.dumps(context))
    snapshot = replace(snapshot, scope=scope, composition_scope=replace(snapshot.composition_scope, parent=scope))
    running = replace(running, reverification=new_reverification())
    save = Mock(side_effect=AssertionError("No fallback reservation is allowed"))
    monkeypatch.setattr(delivery_verify, "_save_reverification", save)
    llm = Mock()
    with pytest.raises(DeliveryIntegrationReviewAgentError) as error:
        delivery_verify._bounded_full_review(
            DeliveryIntegrationReviewAgent(llm, {}),
            status=None,
            snapshot=snapshot,
            running=running,
            project_config={},
            kind=role,
            source_task=source,
            worktree=root,
            validation=DeliveryVerificationValidationResult(True, False, True),
            evidence_path=directory / "verification.jsonl",
        )
    assert error.value.code == "delivery_verification.hierarchy_required"
    assert not llm.mock_calls and not save.called and not running.reverification["fallbacks"]


@pytest.mark.parametrize("change", ["authority", "global_plan_limit"])
def test_local_packets_do_not_bypass_authority_or_global_limits(tmp_path, change):
    import yaml
    from core.delivery_plan import check_delivery_plan_file

    path, cfg, checked, *_ = _large_prepared(tmp_path)
    if change == "authority":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        del data["composition_authority"]
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
        checked = check_delivery_plan_file(path, project_root=tmp_path)
        assert checked.valid, checked.errors
    else:
        checked = replace(checked, plan_bytes=256 * 1024 + 1)
    result = check_delivery_verification_readiness(checked, cfg)
    assert not result.ready
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in result.errors)


@pytest.mark.parametrize("change", ["binary", "private", "missing"])
def test_candidate_without_supported_route_stops_in_preflight(node_input, change):
    from types import SimpleNamespace
    from core.delivery_checkpoint_evidence import checkpoint_evidence_path
    from core.delivery_reverification import CandidateReviewBoundsError, preflight_candidate_tree
    from tests.test_delivery_composition_refresh import _changed_node

    root, directory, source, snapshot, parent, historical = _changed_node(
        node_input, "bytes" if change == "missing" else change
    )
    context = snapshot.scope.plan_context()
    context["units"][0]["description"] = "x" * 600000
    scope = replace(snapshot.scope, _context_json=json.dumps(context))
    snapshot = replace(snapshot, scope=scope, composition_scope=replace(snapshot.composition_scope, parent=scope))
    status = SimpleNamespace(project_root=str(root), verification=None, checkpoint_verifications=historical)
    if change == "missing":
        checkpoint_evidence_path(directory, historical["storage"]).unlink()
    with pytest.raises(ValueError if change == "missing" else CandidateReviewBoundsError):
        preflight_candidate_tree(status, {}, snapshot, root, source)


def test_consumed_fallback_remains_a_budget_stop_even_if_its_packet_no_longer_fits(node_input, monkeypatch):
    from core import delivery_verify
    from core.delivery_reverification import new_reverification

    _, _, _, snapshot, running, _ = node_input
    control = new_reverification()
    control["fallbacks"]["semantic"] = None
    running = replace(running, reverification=control)
    route = Mock(side_effect=AssertionError("Consumed budget must preempt route sizing"))
    monkeypatch.setattr("core.delivery_review_route.review_route", route)
    with pytest.raises(DeliveryIntegrationReviewAgentError) as error:
        delivery_verify._bounded_full_review(
            Mock(), status=None, snapshot=snapshot, running=running, project_config={}, kind="semantic"
        )
    assert error.value.code == "delivery_verification.reverification_budget_exhausted"
    assert not route.called


@pytest.mark.parametrize("role", ["semantic", "security"])
@pytest.mark.parametrize("node", ["integration", "root"])
@pytest.mark.parametrize("composing", [False, True])
def test_runtime_packet_sizing_uses_only_scoped_validation_policy(node_input, monkeypatch, role, node, composing):
    from core import delivery_verify
    from core.delivery_verification import MAX_DELIVERY_VERIFICATION_PACKET_BYTES
    from core.delivery_verification_validation import DeliveryVerificationValidationResult

    root, _, source, snapshot, running, _ = node_input
    full_context = snapshot.scope.plan_context()
    full_context["units"][0]["description"] = "Full descendant authority.\n" * 300
    if node != "root":
        full_context["verification_node"] = {"id": node}
    scope = replace(snapshot.scope, node_id=node, _context_json=json.dumps(full_context))
    snapshot = replace(
        snapshot,
        scope=scope,
        composition_scope=replace(snapshot.composition_scope, parent=scope, include_final_checks=node == "root"),
    )
    context = preview_composition_context(snapshot.composition_scope, role) if composing else None
    cfg = {
        "reviewer": {"extra_rules": "review-rules.md"},
        "security_reviewer": {"extra_rules": "review-rules.md"},
        "delivery": {"verification": {"final_checks": [{"command": "echo " + "x" * 3000}]}},
    }
    rules = root / "review-rules.md"
    rules.write_bytes(b"Review invariants.\n")
    validation = DeliveryVerificationValidationResult(True, False, True)
    checkpoint_summary = validation.to_review_dict(cfg, project_root=root, include_final_checks=False)
    baseline = review_route(scope, cfg, root, source, role, context=context, validation=checkpoint_summary)
    size = baseline.composition_bytes if composing else baseline.full_bytes
    rules.write_bytes(rules.read_bytes() + b"x" * (MAX_DELIVERY_VERIFICATION_PACKET_BYTES - 512 - size))
    readiness_route = review_route(scope, cfg, root, source, role, context=context)
    available = readiness_route.composition_available if composing else readiness_route.full_available
    assert available == (node != "root")
    reviewer = DeliveryIntegrationReviewAgent(None, cfg)
    if composing:
        assert (
            delivery_verify._composition_packet_fits(context, snapshot, reviewer, root, source, validation, cfg, role)
            == available
        )
    else:
        review = Mock()
        monkeypatch.setattr(delivery_verify, "_run_review", review)
        args = dict(
            status=None,
            snapshot=snapshot,
            running=replace(running, reverification=None),
            project_config=cfg,
            worktree=root,
            source_task=source,
            validation=validation,
            kind=role,
        )
        if available:
            result, _ = delivery_verify._bounded_full_review(reviewer, **args)
            assert result is review.return_value
            review.assert_called_once()
        else:
            with pytest.raises(DeliveryIntegrationReviewAgentError) as error:
                delivery_verify._bounded_full_review(reviewer, **args)
            assert error.value.code == "delivery_verification.hierarchy_required"
            review.assert_not_called()


def test_large_nested_plan_executes_resumes_and_finalizes_with_local_packets(tmp_path):
    from core.delivery_finalize import finalize_delivery_plan
    from core.delivery_progress import (
        DeliveryProgress,
        delivery_progress_path,
        get_delivery_status,
        make_delivery_unit_progress,
        write_delivery_progress,
    )
    from core.delivery_verification_review import delivery_integration_review_control_example
    from tests.test_delivery_authority_flow import _run
    from tests.test_delivery_checkpoints import _complete
    from tests.test_delivery_repair import _LLM, _git

    path, cfg, checked, *_ = _large_prepared(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.autocrlf", "true")
    (tmp_path / ".gitignore").write_bytes(b".sikula/state/\n.sikula/worktrees/\n")
    (tmp_path / "agents/code.py").write_bytes(b"value = 1\r\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "bounded hierarchy")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, checked.plan.plan_id),
        DeliveryProgress(
            schema_version=1,
            plan_id=checked.plan.plan_id,
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress("foundation", "done", commit=commit)],
        ),
    )
    leaf = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    llm = _LLM(delivery_integration_review_control_example(set(leaf.obligation_ids)))
    result, security = _run(path, cfg, llm)
    assert result.succeeded, result
    assert max(len(prompt.encode()) for prompt in llm.calls + security.calls) < 512 * 1024
    _complete(path, "consumer")
    nested._compose(path, cfg, "integration", interrupt=True)
    _complete(path, "shipping")
    nested._compose(path, cfg, "root")
    status = get_delivery_status(path)
    assert check_delivery_verification_readiness(status, cfg).ready
    assert not status.verification.reverification["fallbacks"]
    assert not status.checkpoint_verifications["integration"].reverification["fallbacks"]
    result = finalize_delivery_plan(path, project_config=cfg)
    assert result.finalized, result
