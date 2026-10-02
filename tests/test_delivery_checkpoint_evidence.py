from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
import stat
from unittest.mock import patch

import pytest

from core.delivery_checkpoint_evidence import (
    checkpoint_evidence_path,
    load_checkpoint_evidence,
    store_checkpoint_evidence,
)
from core.delivery_checkpoints import checkpoint_pass_is_usable
from core.delivery_progress import delivery_progress_path, get_delivery_status, read_delivery_progress
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification import DeliveryVerificationIdentity, DeliveryVerificationSnapshot
from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_review import DeliveryIntegrationAssessment, DeliveryObligationAssessment
from core.delivery_verification_scope import DeliveryVerificationCompletedUnit, DeliveryVerificationScope
from tests.test_delivery_checkpoints import checkpoint_plan as checkpoint_plan, _verify_node


def _digest(value) -> str:
    return "sha256:" + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def evidence_input(tmp_path):
    completed = (DeliveryVerificationCompletedUnit("storage", "a" * 40, "b" * 64),)
    scope = DeliveryVerificationScope(
        plan_id="cache",
        node_id="storage",
        source_task=None,
        unit_ids=("storage",),
        obligation_ids=("consistent-read",),
        constraint_ids=("no-secrets",),
        source_fragment_ids=("fragment",),
        security_required=True,
        policy=None,
        _context_json="{}",
    )
    identity = DeliveryVerificationIdentity(
        gate_id="sha256:" + "a" * 64,
        candidate_commit="b" * 40,
        candidate_tree="c" * 40,
        source_fingerprint="sha256:" + "d" * 64,
        plan_fingerprint="sha256:" + "e" * 64,
        completed_scope_fingerprint=_digest([unit.to_dict() for unit in completed]),
        config_fingerprint="sha256:" + "f" * 64,
        policy_fingerprint="sha256:" + "0" * 64,
    )
    record = DeliveryVerificationRecord(
        **vars(identity),
        schema_version=1,
        status="passed",
        attempt=1,
        semantic_status="approved",
        security_required=True,
        security_status="approved",
        validation_executed=True,
        obligation_count=1,
        obligation_satisfied_count=1,
        plan_content_fingerprint="sha256:" + "1" * 64,
        review_rule_fingerprints={"rules.md": "sha256:" + "2" * 64},
        evidence_path=".sikula/state/delivery/cache/checkpoint-storage-verification.jsonl",
    )
    assessment = DeliveryIntegrationAssessment(
        "approved",
        "Private review summary.",
        obligation_results=[DeliveryObligationAssessment("consistent-read", "satisfied")],
    )
    return (
        tmp_path,
        tmp_path / ".sikula/state/delivery/cache",
        DeliveryVerificationSnapshot(scope, completed, identity),
        record,
        assessment,
    )


def _store(inputs):
    root, directory, snapshot, record, assessment = inputs
    fingerprint = store_checkpoint_evidence(root, directory, snapshot, record, assessment)
    return replace(record, checkpoint_evidence_fingerprint=fingerprint)


def _load(inputs, record):
    root, directory, snapshot, _, _ = inputs
    return load_checkpoint_evidence(
        root, directory, record, plan_id=snapshot.scope.plan_id, node_id=snapshot.scope.node_id
    )


def test_evidence_roundtrip_is_typed_private_and_idempotent(evidence_input):
    root, directory, snapshot, original, assessment = evidence_input
    record = _store(evidence_input)
    evidence = _load(evidence_input, record)
    assert evidence.covers(snapshot.scope)
    assert evidence.completed_units == snapshot.completed_units
    assert evidence.obligation_results == tuple(assessment.obligation_results)
    assert evidence.verification.candidate_commit == original.candidate_commit
    assert parse_delivery_verification_record(record.to_dict()) == record
    path = checkpoint_evidence_path(directory, record)
    assert "Private review summary" not in path.read_text()
    assert "fragment" not in path.read_text()
    assert (
        store_checkpoint_evidence(root, directory, snapshot, original, assessment)
        == record.checkpoint_evidence_fingerprint
    )
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("field", ["unit_ids", "obligation_ids", "constraint_ids", "security_required"])
def test_evidence_cannot_cover_different_authority(evidence_input, field):
    record = _store(evidence_input)
    evidence = _load(evidence_input, record)
    scope = evidence_input[2].scope
    changed = replace(scope, **{field: False if field == "security_required" else ("different",)})
    assert not evidence.covers(changed)


@pytest.mark.parametrize(
    "change",
    [
        "running",
        "interrupted",
        "missing_digest",
        "attempt",
        "candidate",
        "security",
        "rules",
        "plan",
        "node",
        "plan_id",
    ],
)
def test_evidence_requires_exact_accepted_control(evidence_input, change):
    record = _store(evidence_input)
    fields = {
        "running": {"status": "running"},
        "interrupted": {"status": "interrupted"},
        "missing_digest": {"checkpoint_evidence_fingerprint": None},
        "attempt": {"attempt": 2},
        "candidate": {"candidate_commit": "f" * 40},
        "security": {"security_status": "rejected"},
        "rules": {"review_rule_fingerprints": {}},
        "plan": {"plan_content_fingerprint": "sha256:" + "9" * 64},
    }
    root, directory, snapshot, _, _ = evidence_input
    with pytest.raises(ValueError):
        load_checkpoint_evidence(
            root,
            directory,
            replace(record, **fields.get(change, {})),
            plan_id="other" if change == "plan_id" else snapshot.scope.plan_id,
            node_id="other" if change == "node" else snapshot.scope.node_id,
        )


@pytest.mark.parametrize(
    "change",
    [
        "unknown_field",
        "schema",
        "bool_schema",
        "duplicate",
        "missing",
        "gap",
        "unit",
        "unit_shape",
        "constraints",
        "result_shape",
    ],
)
def test_evidence_parser_rejects_malformed_payload_even_with_matching_digest(evidence_input, change):
    root, directory, _, _, _ = evidence_input
    record = _store(evidence_input)
    path = checkpoint_evidence_path(directory, record)
    payload = read_repair_state(root, path)
    if change == "unknown_field":
        payload["unknown"] = "private"
    elif change == "schema":
        payload["schema_version"] = 2
    elif change == "bool_schema":
        payload["schema_version"] = True
    elif change == "duplicate":
        payload["obligation_results"] *= 2
    elif change == "missing":
        payload["obligation_results"] = []
    elif change == "gap":
        payload["obligation_results"][0]["outcome"] = "uncertain"
    elif change == "unit":
        payload["completed_units"][0]["commit"] = "f" * 40
    elif change == "unit_shape":
        payload["completed_units"][0]["unit_id"] = []
    elif change == "constraints":
        payload["constraint_ids"] *= 2
    else:
        payload["obligation_results"][0]["id"] = []
    write_repair_state(root, path, payload)
    with pytest.raises(ValueError):
        _load(evidence_input, replace(record, checkpoint_evidence_fingerprint=_digest(payload)))


@pytest.mark.parametrize(
    "damage", ["missing", "changed", "oversized", "deep_json", "symlink", "hardlink", "parent_link"]
)
def test_evidence_rejects_unavailable_or_unsafe_storage(evidence_input, damage):
    root, directory, _, _, _ = evidence_input
    record = _store(evidence_input)
    path = checkpoint_evidence_path(directory, record)
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_text("{}")
    elif damage == "oversized":
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    elif damage == "deep_json":
        path.write_text('{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}")
    elif damage == "parent_link":
        directory.rename(directory.with_name("saved"))
        try:
            directory.symlink_to(directory.with_name("saved"), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks unavailable")
    else:
        target = root / "private.json"
        path.rename(target)
        try:
            if damage == "symlink":
                path.symlink_to(target)
            else:
                path.hardlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("Links unavailable")
    with pytest.raises((OSError, ValueError)):
        _load(evidence_input, record)


def test_evidence_does_not_overwrite_conflicting_attempt(evidence_input):
    root, directory, snapshot, original, assessment = evidence_input
    record = _store(evidence_input)
    path = checkpoint_evidence_path(directory, record)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store_checkpoint_evidence(root, directory, snapshot, replace(original, completed_at="later"), assessment)
    assert path.read_bytes() == before


@pytest.mark.parametrize("outcome", ["missing", "conflicting", "uncertain", "unknown_id"])
def test_evidence_never_manufactures_obligation_closure(evidence_input, outcome):
    root, directory, snapshot, original, assessment = evidence_input
    assessment = replace(
        assessment,
        obligation_results=[
            DeliveryObligationAssessment(
                "unknown" if outcome == "unknown_id" else "consistent-read",
                "satisfied" if outcome == "unknown_id" else outcome,
            )
        ],
    )
    with pytest.raises(ValueError):
        store_checkpoint_evidence(root, directory, snapshot, original, assessment)
    assert not directory.exists()


def test_gate_publishes_and_reuses_evidence_without_audit_replay(checkpoint_plan):
    path, cfg = checkpoint_plan
    result, llm = _verify_node(path, cfg)
    assert result.succeeded, result
    assert len(llm.calls) == 1
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["storage"]
    directory = delivery_progress_path(path.parent, "cache").parent
    evidence = load_checkpoint_evidence(path.parent, directory, record, plan_id="cache", node_id="storage")
    assert evidence.covers(DeliveryVerificationScope.from_plan(status.plan, "storage"))
    assert [(item.id, item.outcome) for item in evidence.obligation_results] == [("consistent-read", "satisfied")]
    (path.parent / record.evidence_path).unlink()
    result, llm = _verify_node(path, cfg)
    assert result.succeeded and not llm.calls
    assert checkpoint_pass_is_usable(get_delivery_status(path), status.plan.checkpoints[0], cfg)
    assert "checkpoint_evidence_fingerprint" not in json.dumps(status.to_dict())
    assert "checkpoint-evidence-" not in json.dumps(result.to_dict())


def test_returning_to_previous_policy_preserves_distinct_checkpoint_attempts(checkpoint_plan):
    path, cfg = checkpoint_plan
    directory = delivery_progress_path(path.parent, "cache").parent
    records = []
    artifacts = []
    for model in ("review-a", "review-b", "review-a"):
        effective = {**cfg, "agents": {"reviewer": {"llm": {"model": model}}}}
        result, llm = _verify_node(path, effective)
        assert result.succeeded, result
        assert len(llm.calls) == 1
        record = get_delivery_status(path).checkpoint_verifications["storage"]
        records.append(record)
        artifact = checkpoint_evidence_path(directory, record)
        artifacts.append((artifact, artifact.read_bytes()))
    assert records[0].gate_id == records[2].gate_id != records[1].gate_id
    assert len({record.attempt for record in records}) == 3
    for record in records:
        evidence = load_checkpoint_evidence(path.parent, directory, record, plan_id="cache", node_id="storage")
        assert evidence.verification == record
    assert all(artifact.read_bytes() == original for artifact, original in artifacts)
    result, llm = _verify_node(path, effective)
    assert result.succeeded and not llm.calls


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_gate_blocks_damaged_evidence_before_provider(checkpoint_plan, damage):
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["storage"]
    target = checkpoint_evidence_path(delivery_progress_path(path.parent, "cache").parent, record)
    if damage == "missing":
        target.unlink()
    else:
        target.write_text("{}")
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    result, llm = _verify_node(path, cfg)
    assert result.stop_code == "delivery_checkpoint.evidence_unavailable"
    assert not llm.calls


def test_gate_evidence_write_failure_never_publishes_a_pass(checkpoint_plan):
    path, cfg = checkpoint_plan
    with patch("core.delivery_checkpoint_evidence.write_repair_state", side_effect=OSError("private detail")):
        result, llm = _verify_node(path, cfg)
    assert len(llm.calls) == 1
    assert result.stop_code == "delivery_checkpoint.evidence_unavailable"
    status = get_delivery_status(path)
    assert status.checkpoint_verifications["storage"].status == "blocked"
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    assert "private detail" not in json.dumps(result.to_dict())


def test_interrupted_evidence_write_leaves_no_accepted_orphan_and_resume_converges(checkpoint_plan):
    path, cfg = checkpoint_plan
    original = store_checkpoint_evidence

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    with patch("core.delivery_verify.store_checkpoint_evidence", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            _verify_node(path, cfg)
    status = get_delivery_status(path)
    old = status.checkpoint_verifications["storage"]
    directory = delivery_progress_path(path.parent, "cache").parent
    assert checkpoint_evidence_path(directory, old).exists()
    assert old.status == "interrupted"
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    with pytest.raises(ValueError):
        load_checkpoint_evidence(path.parent, directory, old, plan_id="cache", node_id="storage")
    result, _ = _verify_node(path, cfg)
    assert result.succeeded
    progress, errors = read_delivery_progress(directory / "progress.json", plan_id="cache")
    assert not errors
    assert progress.checkpoint_verifications["storage"].attempt == old.attempt + 1
    assert checkpoint_evidence_path(directory, old).exists()


def test_gate_rechecks_evidence_before_acceptance(checkpoint_plan):
    path, cfg = checkpoint_plan
    original = store_checkpoint_evidence

    def damage(root, directory, snapshot, record, assessment):
        fingerprint = original(root, directory, snapshot, record, assessment)
        checkpoint_evidence_path(directory, record).unlink()
        return fingerprint

    with patch("core.delivery_verify.store_checkpoint_evidence", side_effect=damage):
        result, _ = _verify_node(path, cfg)
    assert result.stop_code == "delivery_checkpoint.evidence_unavailable"
    assert get_delivery_status(path).checkpoint_verifications["storage"].status == "blocked"


def test_changed_authority_during_publication_leaves_only_unaccepted_evidence(checkpoint_plan):
    path, cfg = checkpoint_plan
    original = store_checkpoint_evidence

    def advance(root, directory, snapshot, record, assessment):
        fingerprint = original(root, directory, snapshot, record, assessment)
        path.write_text(path.read_text() + "\n# concurrent plan change\n")
        return fingerprint

    with patch("core.delivery_verify.store_checkpoint_evidence", side_effect=advance):
        result, _ = _verify_node(path, cfg)
    assert result.stop_code == "delivery_verification.candidate_changed"
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["storage"]
    directory = delivery_progress_path(path.parent, "cache").parent
    assert record.status == "stale"
    assert checkpoint_evidence_path(directory, record).exists()
    with pytest.raises(ValueError):
        load_checkpoint_evidence(path.parent, directory, record, plan_id="cache", node_id="storage")
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)


def test_failed_control_publication_preserves_evidence_and_resume_uses_new_attempt(checkpoint_plan):
    from core.delivery_progress import write_delivery_progress

    path, cfg = checkpoint_plan

    def fail_pass(target, progress):
        record = progress.checkpoint_verifications.get("storage")
        if record and record.passed:
            raise OSError("control unavailable")
        write_delivery_progress(target, progress)

    with patch("core.delivery_verify.write_delivery_progress", side_effect=fail_pass):
        with pytest.raises(OSError):
            _verify_node(path, cfg)
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["storage"]
    directory = delivery_progress_path(path.parent, "cache").parent
    orphan = checkpoint_evidence_path(directory, record)
    original_bytes = orphan.read_bytes()
    assert record.status == "running"
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    result, _ = _verify_node(path, cfg)
    assert result.succeeded
    assert get_delivery_status(path).checkpoint_verifications["storage"].attempt == record.attempt + 1
    assert orphan.read_bytes() == original_bytes
