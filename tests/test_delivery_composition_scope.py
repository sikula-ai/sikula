from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from core.delivery_authority import AUTHORITY_PACKET_POLICY
from core.delivery_checkpoint_evidence import build_node_closure, checkpoint_evidence_path, store_checkpoint_evidence
from core.delivery_composition import (
    build_node_composition_context,
    composition_example,
    fingerprint,
    load_node_composition,
    parse_composition,
    store_composition,
)
from core.delivery_composition_scope import DeliveryCompositionAttempt, DeliveryCompositionScope
from core.delivery_obligations import delivery_authority_fragments
from core.delivery_progress import delivery_progress_path
from core.delivery_reverification import preflight_node_review_packets, run_node_composition_cycle
from core.delivery_plan import DeliveryPlanSourceTask
from core.delivery_verification import DeliveryVerificationSnapshot
from core.delivery_verification_model import DeliveryVerificationRecord
from core.delivery_verification_scope import DeliveryVerificationCompletedUnit
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input


NodeInput = tuple[
    Path, Path, str, DeliveryVerificationSnapshot, DeliveryVerificationRecord, dict[str, DeliveryVerificationRecord]
]


@pytest.fixture
def node_input(evidence_input: Any) -> NodeInput:
    root, _, child_snapshot, child_record, assessment = evidence_input
    source = "# Shared\nPreserve interfaces.\n\n# Child\nPrivate child implementation details.\n"
    fragments = delivery_authority_fragments(source)
    source_hash = "sha256:" + sha256(source.encode()).hexdigest()
    source_task = DeliveryPlanSourceTask("task.md", source_hash)
    child_context = {
        "plan_id": "cache",
        "units": [{"id": "storage", "description": "CHILD_CONTRACT"}],
        "constraints": [{"id": "no-secrets", "unit_ids": ["storage"]}],
        "obligations": [{"id": "consistent-read", "unit_ids": ["storage"], "summary": "CHILD_OUTCOME"}],
        "source_accounting": [],
    }
    child_scope = replace(
        child_snapshot.scope,
        source_task=source_task,
        source_fragment_ids=tuple(item.id for item in fragments),
        _context_json=json.dumps(child_context),
    )
    child_snapshot = replace(
        child_snapshot, scope=child_scope, identity=replace(child_snapshot.identity, source_fingerprint=source_hash)
    )
    child_record = replace(child_record, source_fingerprint=source_hash)
    directory = delivery_progress_path(root, "cache").parent
    digest = store_checkpoint_evidence(root, directory, child_snapshot, child_record, assessment)
    child_record = replace(child_record, checkpoint_evidence_fingerprint=digest)
    parent_context = {
        **child_context,
        "units": [*child_context["units"], {"id": "consumer", "depends_on": ["storage"]}],
        "obligations": [*child_context["obligations"], {"id": "visible", "unit_ids": ["consumer"]}],
    }
    parent = replace(
        child_scope,
        node_id="integration",
        unit_ids=("storage", "consumer"),
        obligation_ids=("consistent-read", "visible"),
        _context_json=json.dumps(parent_context),
    )
    retained = {
        **parent_context,
        "units": [{"id": "consumer", "depends_on": [], "checkpoint_dependencies": ["storage"]}],
        "obligations": [{"id": "visible", "unit_ids": ["consumer"]}],
        "constraints": [],
        "authority_packet": {
            "policy": AUTHORITY_PACKET_POLICY,
            "source_fingerprint": source_hash,
            "fragments": [{"id": fragments[0].id, "sha256": fragments[0].sha256}],
            "remaining_authority": "accepted_child_closure",
        },
        "final_authority": {
            "policy": "final-authority-closure-v1",
            "children": [
                {
                    "id": "storage",
                    "coverage_fingerprint": fingerprint({"units": ["storage"], "obligations": ["consistent-read"]}),
                    "obligation_count": 1,
                    "integration_context": "Inspect visibility through the storage interface.",
                }
            ],
        },
    }
    composition_scope = DeliveryCompositionScope(parent, (child_scope,), json.dumps(retained))
    completed = (*child_snapshot.completed_units, DeliveryVerificationCompletedUnit("consumer", "d" * 40, "e" * 64))
    identity = replace(
        child_snapshot.identity,
        gate_id="sha256:" + "9" * 64,
        completed_scope_fingerprint=fingerprint([item.to_dict() for item in completed]),
    )
    snapshot = DeliveryVerificationSnapshot(parent, completed, identity, composition_scope)
    parent_record = replace(
        child_record,
        **vars(identity),
        checkpoint_evidence_fingerprint=None,
        obligation_count=2,
        obligation_satisfied_count=2,
    )
    historical = {"storage": child_record}
    return root, directory, source, snapshot, parent_record, historical


def _attempt(inputs: NodeInput, record: DeliveryVerificationRecord | None = None) -> DeliveryCompositionAttempt:
    _, _, _, snapshot, parent, historical = inputs
    return DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, record or parent)


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_explicit_node_packet_excludes_unrelated_and_child_local_details(node_input: NodeInput, role: str) -> None:
    root, _, source, snapshot, parent, historical = node_input
    attempt = _attempt(node_input)
    context = build_node_composition_context(root, snapshot, attempt, {}, review_kind=role)
    assert context["verification_node"] == {"id": "integration"}
    prompt_args = dict(
        cwd=root,
        review_kind=role,
        source_task=source,
        validation_summary={},
        candidate_commit=parent.candidate_commit,
        candidate_tree=parent.candidate_tree,
        known_obligation_ids=set(snapshot.scope.obligation_ids),
    )
    reviewer = DeliveryIntegrationReviewAgent(None, {})
    prompt = reviewer._prompt(plan_context=context, **prompt_args)
    template = composition_example(context)
    assert "Private child implementation details" not in prompt
    assert "CHILD_CONTRACT" not in prompt and "CHILD_OUTCOME" not in prompt
    assert "verification node composes" in prompt
    assert {item["id"] for item in json.loads(template)["checkpoint_results"]} == {"storage"}
    assert "consistent-read" not in template
    # Extra delivery records cannot enter an explicit capture, even if unusable.
    historical["unrelated"] = object()
    child = snapshot.composition_scope.children[0]
    child_context = child.plan_context()
    child_context["units"][0]["description"] = "x" * 500000
    child = replace(child, _context_json=json.dumps(child_context))
    scope = replace(snapshot.composition_scope, children=(child,))
    noisy = replace(snapshot, composition_scope=scope)
    changed = build_node_composition_context(
        root, noisy, DeliveryCompositionAttempt.capture(scope, historical, parent), {}, review_kind=role
    )
    assert changed == context
    assert reviewer._prompt(plan_context=changed, **prompt_args) == prompt
    assert composition_example(changed) == template


@pytest.mark.parametrize("damage", ["direct", "constraint", "fragment", "children"])
def test_composition_capture_requires_exact_parent_coverage(node_input: NodeInput, damage: str) -> None:
    scope = node_input[3].composition_scope
    retained = scope.retained_authority()
    if damage == "direct":
        retained["obligations"] = []
    elif damage == "constraint":
        scope = replace(
            scope,
            parent=replace(scope.parent, constraint_ids=(*scope.parent.constraint_ids, "uncovered")),
            retained_authority_json=None,
        )
    elif damage == "fragment":
        scope = replace(
            scope,
            parent=replace(scope.parent, source_fragment_ids=(*scope.parent.source_fragment_ids, "uncovered")),
            retained_authority_json=None,
        )
    else:
        retained["final_authority"]["children"] = []
    with pytest.raises(ValueError):
        replace(scope, retained_authority_json=json.dumps(retained))


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_node_composition_evidence_roundtrip_and_typed_closure(node_input: NodeInput, role: str) -> None:
    root, directory, _, snapshot, parent, _ = node_input
    context = build_node_composition_context(root, snapshot, _attempt(node_input), {}, review_kind=role)
    result = parse_composition(composition_example(context), context, set(snapshot.scope.unit_ids))
    digest = store_composition(root, directory, parent.gate_id, context, result)
    prefix = "" if role == "semantic" else "security_"
    accepted = replace(
        parent, **{prefix + "composition_attempted": True, prefix + "composition_evidence_fingerprint": digest}
    )
    attempt = _attempt(node_input, accepted)
    loaded = load_node_composition(root, attempt, accepted, review_kind=role)
    assert not loaded.fallback and loaded.assessment.approved
    if role == "semantic":
        payload = build_node_closure(root, directory, snapshot, accepted, loaded.assessment, loaded, attempt)
        assert payload["node_id"] == "integration"
        assert [item["id"] for item in payload["obligation_results"]] == ["visible"]
        assert [item["unit_id"] for item in payload["completed_units"]] == ["consumer"]
        assert [item["node_id"] for item in payload["child_evidence"]] == ["storage"]
    with pytest.raises(ValueError, match="attempt changed"):
        load_node_composition(root, attempt, replace(accepted, attempt=2), review_kind=role)
    child = node_input[-1]["storage"]
    checkpoint_evidence_path(directory, child).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence"):
        load_node_composition(root, attempt, accepted, review_kind=role)


def test_candidate_capture_freezes_accepted_control_and_rejects_foreign_children(node_input: NodeInput) -> None:
    _, _, _, snapshot, parent, historical = node_input
    control = {"children": {}, "reassessments": {}, "fallbacks": {}}
    parent = replace(parent, composition_attempted=True, reverification=control)
    attempt = DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent)
    control["children"]["foreign"] = {}
    assert attempt.origin("storage") == historical["storage"]
    with pytest.raises(ValueError, match="outside"):
        DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent)
    with pytest.raises(ValueError, match="outside"):
        attempt.origin("foreign")
    with pytest.raises(ValueError, match="candidate changed"):
        attempt.assert_candidate(replace(snapshot.identity, candidate_commit="f" * 40))


@pytest.mark.parametrize("historical", [{}, {"storage": None}])
def test_missing_admission_uses_the_evidence_error_boundary(node_input: NodeInput, historical: dict) -> None:
    snapshot, parent = node_input[3:5]
    with pytest.raises(ValueError, match="admission is unavailable"):
        DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent)


def test_node_preflight_sizes_only_immediate_scopes_and_excludes_final_checks(node_input: NodeInput) -> None:
    root, _, source, snapshot, _, _ = node_input
    with patch("core.delivery_verification_validation.preview_delivery_validation_summary", return_value={}) as preview:
        preflight_node_review_packets(snapshot.composition_scope, {}, root, source)
    assert len(preview.call_args_list) == 2
    assert all(call.kwargs["include_final_checks"] is False for call in preview.call_args_list)
    with patch("core.delivery_verification_validation.preview_delivery_validation_summary", return_value={}) as preview:
        preflight_node_review_packets(replace(snapshot.composition_scope, include_final_checks=True), {}, root, source)
    assert [call.kwargs["include_final_checks"] for call in preview.call_args_list] == [True, False]


def test_common_cycle_checks_all_requested_children_before_work(node_input: NodeInput) -> None:
    root, _, _, snapshot, parent, _ = node_input
    context = build_node_composition_context(root, snapshot, _attempt(node_input), {})
    control = json.loads(composition_example(context))
    control["checkpoint_results"][0]["outcome"] = "verification_required"
    result = parse_composition(json.dumps(control), context, set(snapshot.scope.unit_ids))
    result.checkpoints.append({"id": "foreign", "outcome": "verification_required"})
    parent = replace(
        parent,
        composition_attempted=True,
        composition_evidence_fingerprint="sha256:" + "0" * 64,
        reverification={"children": {}, "reassessments": {}, "fallbacks": {}},
    )
    review = Mock()
    with pytest.raises(ValueError, match="outside"):
        run_node_composition_cycle(
            snapshot.composition_scope,
            parent,
            "semantic",
            exchange=lambda record, retry: (None, record),
            validate=lambda record: None,
            load=lambda record: result,
            review_child=review,
            semantic_fallback_covers=lambda record: False,
        )
    review.assert_not_called()
