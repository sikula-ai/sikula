from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from unittest.mock import Mock

import pytest

from core import delivery_checkpoint_evidence as module
from core.delivery_composition import composition_evidence_path
from core.delivery_evidence_resolution import EvidenceResolver, EvidenceResolutionLimits, EvidenceResolutionLimit
from core.delivery_repair_storage import write_repair_state
from core.delivery_verification_review import DeliveryObligationAssessment
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input


def _digest(value):
    return "sha256:" + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _graph(inputs, *, depth=3, width=2, direct_count=2, root_order=None, damage=None):
    """Private accepted-artifact fixture; the flattened oracle exists only in tests."""
    root, directory, snapshot, template, _ = inputs
    inventory = []

    def node(key, level):
        children = [node(f"{key}-{index}", level - 1) for index in range(width)] if level else []
        units = [
            {"unit_id": f"{key}-unit-{index}", "commit": "a" * 40, "handoff_fingerprint": "b" * 64}
            for index in range(direct_count)
        ]
        results = [{"id": f"{key}-outcome-{index}", "outcome": "satisfied"} for index in range(direct_count)]
        if damage and key == "root-1":
            if damage == "duplicate_unit":
                units[0]["unit_id"] = "root-0-unit-0"
            elif damage == "conflicting_unit":
                units[0].update(unit_id="root-0-unit-0", commit="f" * 40)
            elif damage == "duplicate_outcome":
                results[0]["id"] = "root-0-outcome-0"
        flat_units = sorted([*units, *(unit for child in children for unit in child[2])], key=lambda x: x["unit_id"])
        if not children:
            flat_units = list(units)
        flat_results = [*results, *(item for child in children for item in child[3])]
        if key == "root" and root_order == "reverse":
            flat_units.reverse()
        record = replace(
            template,
            gate_id=_digest(key),
            completed_scope_fingerprint=_digest(flat_units),
            obligation_count=len(flat_results),
            obligation_satisfied_count=len(flat_results),
        )
        if key == "root" and damage == "missing_outcome":
            results.pop()
        if key == "root" and damage == "changed_execution":
            units[0]["commit"] = "f" * 40
        if key == "root" and damage == "empty_direct":
            # A composing node may delegate all work to children.
            flat_units = sorted([unit for child in children for unit in child[2]], key=lambda x: x["unit_id"])
            flat_results = [item for child in children for item in child[3]]
            units, results = [], []
            record = replace(
                record,
                completed_scope_fingerprint=_digest(flat_units),
                obligation_count=len(flat_results),
                obligation_satisfied_count=len(flat_results),
            )
        if children:
            composition = {
                "gate_id": record.gate_id,
                "compact": True,
                "review_kind": "semantic",
                "control": {
                    "disposition": "approved",
                    "findings": [],
                    "obligation_results": results,
                    "checkpoint_results": [{"id": child[0], "outcome": "applicable"} for child in children],
                },
                "origins": {child[0]: child[1].checkpoint_evidence_fingerprint for child in children},
            }
            if key == "root" and damage == "changed_origin":
                composition["origins"][children[0][0]] = "sha256:" + "0" * 64
            fingerprint = _digest(composition)
            write_repair_state(root, composition_evidence_path(directory, fingerprint), composition)
            record = replace(record, composition_attempted=True, composition_evidence_fingerprint=fingerprint)
        payload = {
            "schema_version": 1,
            "plan_id": "cache",
            "node_id": key,
            "verification": module._record_binding(record),
            "completed_units": units,
            "constraint_ids": ["shared-rule", key + "-rule"],
            "obligation_results": results,
        }
        if children:
            payload["child_evidence"] = [
                {"node_id": child[0], "verification": child[1].to_dict()} for child in children
            ]
            if key == "root" and damage == "changed_attempt":
                payload["child_evidence"][0]["verification"]["attempt"] += 1
        fingerprint = _digest(payload)
        if key == "root":
            path = module.root_evidence_path(directory, fingerprint)
            record = replace(record, root_evidence_fingerprint=fingerprint)
        else:
            path = module.checkpoint_evidence_path(directory, record)
            record = replace(record, checkpoint_evidence_fingerprint=fingerprint)
        write_repair_state(root, path, payload)
        inventory.append((key, payload, flat_units, flat_results))
        return key, record, flat_units, flat_results

    _, record, units, results = node("root", depth)
    scope = replace(
        snapshot.scope,
        node_id="root",
        unit_ids=tuple(unit["unit_id"] for unit in units),
        obligation_ids=tuple(item["id"] for item in results),
        constraint_ids=tuple(sorted({key for _, payload, _, _ in inventory for key in payload["constraint_ids"]})),
    )
    return record, scope, inventory


@pytest.mark.parametrize("depth,width", [(1, 8), (3, 3), (12, 1)])
def test_real_loader_retains_only_direct_typed_results_and_matches_flat_oracle(
    evidence_input, monkeypatch, depth, width
):
    record, scope, inventory = _graph(evidence_input, depth=depth, width=width)
    unit_type, result_type = module.DeliveryVerificationCompletedUnit, module.DeliveryObligationAssessment
    units, results = Mock(wraps=unit_type), Mock(wraps=result_type)
    monkeypatch.setattr(module, "DeliveryVerificationCompletedUnit", units)
    monkeypatch.setattr(module, "DeliveryObligationAssessment", results)
    root, directory, *_ = evidence_input
    with EvidenceResolver() as resolver:
        loaded = module.load_root_evidence(root, directory, record, plan_id="cache", resolver=resolver)
        assert loaded.covers(scope)
    expected_units = {unit["unit_id"]: unit for unit in inventory[-1][2]}
    assert {unit.unit_id: unit.to_dict() for unit in loaded.closure.iter_completed_units()} == expected_units
    assert {result.id for result in loaded.closure.iter_obligation_results()} == set(scope.obligation_ids)
    assert list(loaded.closure.iter_constraint_ids()) == sorted(scope.constraint_ids)
    # Legacy expansion reconstructed a typed copy at every ancestor. No caller
    # can hide that regression behind compact artifact storage or a raw JSON cache.
    assert units.call_count == len(expected_units)
    assert results.call_count == len(scope.obligation_ids)
    pending, seen = [loaded.closure], []
    while pending:
        current = pending.pop()
        seen.extend(current.direct_completed_units)
        pending.extend(current.children)
    assert len(seen) == len({id(item) for item in seen}) == len(expected_units)
    assert len(loaded.closure.direct_completed_units) == 2
    assert sum(len(entry[2]) for entry in inventory) > units.call_count
    for field in ("unit_ids", "constraint_ids", "obligation_ids"):
        assert not loaded.covers(replace(scope, **{field: getattr(scope, field) + ("extra",)}))
        assert not loaded.covers(replace(scope, **{field: getattr(scope, field)[1:]}))


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_unit",
        "conflicting_unit",
        "duplicate_outcome",
        "missing_outcome",
        "changed_execution",
        "changed_origin",
        "changed_attempt",
    ],
)
def test_rehashed_closure_cannot_hide_invalid_exact_evidence(evidence_input, damage):
    record, _, _ = _graph(evidence_input, depth=1, damage=damage)
    root, directory, *_ = evidence_input
    with pytest.raises(ValueError):
        module.load_root_evidence(root, directory, record, plan_id="cache")


def test_delegated_only_parent_and_shared_constraints(evidence_input):
    record, scope, _ = _graph(evidence_input, depth=2, damage="empty_direct")
    loaded = module.load_root_evidence(*evidence_input[:2], record, plan_id="cache")
    assert not loaded.closure.direct_completed_units and not loaded.closure.direct_obligation_results
    assert loaded.covers(scope)
    assert list(loaded.closure.iter_constraint_ids()).count("shared-rule") == 1


def test_execution_hash_preserves_explicit_declaration_order(evidence_input):
    record, scope, _ = _graph(evidence_input, depth=1, root_order="reverse")
    loaded = module.load_root_evidence(*evidence_input[:2], record, plan_id="cache", unit_order=scope.unit_ids)
    assert loaded.covers(scope)
    for order in (None, tuple(reversed(scope.unit_ids)), scope.unit_ids[:-1], (*scope.unit_ids, scope.unit_ids[0])):
        with pytest.raises(ValueError):
            module.load_root_evidence(*evidence_input[:2], record, plan_id="cache", unit_order=order)


def test_composition_outcomes_are_matched_without_expanding_a_map(evidence_input):
    record, scope, _ = _graph(evidence_input, depth=2)
    loaded = module.load_root_evidence(*evidence_input[:2], record, plan_id="cache")
    direct = list(loaded.closure.direct_obligation_results)
    inherited = len(scope.obligation_ids) - len(direct)
    assert loaded.matches_results(direct, inherited_count=inherited)
    assert not loaded.matches_results(direct, inherited_count=inherited + 1)
    assert not loaded.matches_results(direct, inherited_count=None)
    assert not loaded.matches_results([replace(direct[0], outcome="gap"), *direct[1:]], inherited_count=inherited)
    assert not loaded.matches_results(
        [DeliveryObligationAssessment("unknown", "satisfied"), *direct[1:]], inherited_count=inherited
    )
    assert not loaded.matches_results([direct[0], direct[0]], inherited_count=inherited)


@pytest.mark.parametrize("limit", ["expanded_items", "materialized_bytes", "depth"])
def test_compact_validation_still_obeys_operation_budget(evidence_input, limit):
    record, _, _ = _graph(evidence_input, depth=2)
    with pytest.raises(EvidenceResolutionLimit):
        with EvidenceResolver(replace(EvidenceResolutionLimits(), **{limit: 1})) as resolver:
            module.load_root_evidence(*evidence_input[:2], record, plan_id="cache", resolver=resolver)


def test_streamed_execution_binding_uses_less_peak_memory_than_flat_payload(evidence_input):
    import gc
    import tracemalloc

    record, _, _ = _graph(evidence_input, depth=2, width=3, direct_count=32)
    evidence = module.load_root_evidence(*evidence_input[:2], record, plan_id="cache")
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:

        def peak(operation):
            gc.collect()
            before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            operation()
            _, high = tracemalloc.get_traced_memory()
            return high - before

        def flat():
            assert (
                _digest([unit.to_dict() for unit in evidence.closure.iter_completed_units()])
                == record.completed_scope_fingerprint
            )

        with EvidenceResolver() as resolver:
            streaming_peak = peak(lambda: module._validate_closure_coverage(evidence, None, resolver))
        flat_peak = peak(flat)
        # Compare the actual binding validation with a test-only flattened oracle,
        # not total pytest/coverage process allocations or an absolute platform size.
        assert streaming_peak < flat_peak / 2
    finally:
        if started:
            tracemalloc.stop()
