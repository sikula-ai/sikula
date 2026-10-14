from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.delivery_candidate_control import expanded_child
from core.delivery_checkpoint_applicability import validate_root_evidence
from core.delivery_composition import build_composition_context, composition_example
from core.delivery_progress import delivery_progress_path, get_delivery_status
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM


@pytest.fixture
def candidate(tmp_path):
    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    result, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert result.succeeded, result
    _complete(path, "consumer")
    nested._compose(path, cfg, "integration")
    _complete(path, "shipping")
    return path, cfg


def _outputs(path, cfg):
    status = get_delivery_status(path)
    outputs = {}
    for node in ("root", "integration"):
        snapshot = build_delivery_verification_snapshot(
            replace(status, verification_node=node), cfg, candidate_commit=status.assembled_commit
        )
        for role in ("semantic", "security"):
            context = build_composition_context(status, snapshot, cfg, review_kind=role)
            approved = composition_example(context)
            uncertain = json.loads(approved)
            uncertain["checkpoint_results"][0]["outcome"] = "verification_required"
            outputs[node, role] = (approved, json.dumps(uncertain))
    return outputs


def _reviewers(outputs, *, interrupt=False):
    final, uncertain_final = outputs["root", "semantic"]
    intermediate, uncertain_intermediate = outputs["integration", "semantic"]
    semantic = _LLM(
        uncertain_final,
        uncertain_intermediate,
        delivery_integration_review_control_example({"deliver-team-invites"}),
        KeyboardInterrupt() if interrupt else intermediate,
        final,
    )
    # Both roles request the same descendants; accepted child work is shared.
    security = _LLM(
        delivery_integration_review_control_example(set()),
        outputs["integration", "security"][1],
        outputs["integration", "security"][0],
        outputs["root", "security"][1],
        outputs["root", "security"][0],
    )
    return semantic, security


@pytest.mark.parametrize("storage_error", [OSError, PermissionError])
def test_recursive_preflight_storage_failure_is_persisted(candidate, monkeypatch, storage_error):
    from core import delivery_composition, delivery_reverification
    from core.delivery_progress import delivery_events_path, read_delivery_progress

    path, cfg = candidate
    root = Path(cfg["project"]["root_path"])
    preflight = delivery_reverification.preflight_candidate_tree
    load_evidence = delivery_composition.load_checkpoint_evidence
    failed_reads = []

    def unavailable(*args, **kwargs):
        if kwargs["node_id"] == "storage":
            failed_reads.append(kwargs["node_id"])
            raise storage_error("Private evidence storage diagnostic")
        return load_evidence(*args, **kwargs)

    def preflight_with_storage_failure(*args, **kwargs):
        # Initial capture succeeds; the nested checkpoint becomes unreadable
        # only when recursive packet preflight loads its authority evidence.
        with patch.object(delivery_composition, "load_checkpoint_evidence", side_effect=unavailable):
            return preflight(*args, **kwargs)

    monkeypatch.setattr(delivery_reverification, "preflight_candidate_tree", preflight_with_storage_failure)
    unused = _LLM()
    result, _ = _run(path, cfg, unused, unused, node_id="root")
    expected = "delivery_verification.evidence_unavailable"
    assert failed_reads == ["storage"]
    assert not result.succeeded and result.status == "blocked"
    assert result.stop_code == expected
    assert not unused.calls
    assert "Private evidence storage diagnostic" not in json.dumps(result.to_dict())
    progress_path = delivery_progress_path(root, "team-invites")
    progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
    assert not errors
    record = progress.verification
    assert record.status == "blocked" and record.stop_code == expected
    assert record.completed_at and record.validation_executed
    assert not record.composition_attempted and record.reverification is None
    audit = [json.loads(line) for line in (root / record.evidence_path).read_text().splitlines()]
    assert audit[-1]["event"] == "blocked" and audit[-1]["record"]["stop_code"] == expected
    events = [json.loads(line) for line in delivery_events_path(root, "team-invites").read_text().splitlines()]
    assert events[-1]["event_type"] == "verification.blocked"


def test_checkpoint_can_initiate_candidate_review(tmp_path):
    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    result, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert result.succeeded, result
    _complete(path, "consumer")
    before = get_delivery_status(path)
    status = replace(before, verification_node="integration")
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    semantic_context = build_composition_context(status, snapshot, cfg)
    security_context = build_composition_context(status, snapshot, cfg, review_kind="security")
    uncertain = json.loads(composition_example(semantic_context))
    uncertain["checkpoint_results"][0]["outcome"] = "verification_required"
    semantic = _LLM(
        json.dumps(uncertain),
        delivery_integration_review_control_example({"deliver-team-invites"}),
        composition_example(semantic_context),
    )
    security = _LLM(delivery_integration_review_control_example(set()), composition_example(security_context))
    result, _ = _run(path, cfg, semantic, security, node_id="integration")
    assert result.succeeded, result
    after = get_delivery_status(path)
    assert after.verification == before.verification
    assert after.checkpoint_verifications["storage"] == before.checkpoint_verifications["storage"]
    parent = after.checkpoint_verifications["integration"]
    assert parent.passed
    child = expanded_child(after, parent.reverification["children"]["storage"])
    assert child.passed and child.candidate_commit == parent.candidate_commit
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id="integration")
    assert resumed.succeeded and not unused.calls
    _complete(path, "shipping")
    nested._compose(path, cfg, "root")
    from core.delivery_finalize import finalize_delivery_plan

    assert finalize_delivery_plan(path, project_config=cfg).finalized


@pytest.mark.parametrize("interrupt", [False, "before_evidence", "after_evidence"])
def test_recursive_candidate_review_and_resume(candidate, interrupt):
    path, cfg = candidate
    before = get_delivery_status(path)
    outputs = _outputs(path, cfg)
    semantic, security = _reviewers(outputs, interrupt=interrupt)
    if interrupt:
        # Interrupt before publication of the intermediate checkpoint evidence,
        # after accepting its complete composition. Resume must reuse both roles.
        original = __import__("core.delivery_verify", fromlist=["store_checkpoint_evidence"]).store_checkpoint_evidence
        semantic, security = _reviewers(outputs)

        def stop(root, directory, snapshot, *args, **kwargs):
            if snapshot.scope.node_id == "integration":
                if interrupt == "after_evidence":
                    original(root, directory, snapshot, *args, **kwargs)
                raise KeyboardInterrupt()
            return original(root, directory, snapshot, *args, **kwargs)

        with patch("core.delivery_verify.store_checkpoint_evidence", side_effect=stop):
            with pytest.raises(KeyboardInterrupt):
                _run(path, cfg, semantic, security, node_id="root")
        semantic = _LLM(outputs["root", "semantic"][0])
        security = _LLM(outputs["root", "security"][1], outputs["root", "security"][0])
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    status = get_delivery_status(path)
    assert status.checkpoint_verifications == before.checkpoint_verifications
    assert status.units == before.units
    parent_entry = status.verification.reverification["children"]["integration"]
    assert set(parent_entry["verification"]["reverification"]) == {"reference"}
    parent = expanded_child(status, parent_entry)
    child = expanded_child(status, parent.reverification["children"]["storage"])
    assert parent.passed and child.passed
    assert parent.candidate_commit == child.candidate_commit == status.assembled_commit
    assert not parent.reverification["fallbacks"]
    assert not status.verification.reverification["fallbacks"]
    assert set(parent.reverification["reassessments"]) == {"semantic", "security"}
    validate_root_evidence(status, status.verification)
    from core.delivery_finalize import finalize_delivery_plan

    assert finalize_delivery_plan(path, project_config=cfg).finalized
    assert "candidate-node-" not in json.dumps(status.to_dict())


def test_missing_recursive_control_preempts_calls(candidate):
    path, cfg = candidate
    outputs = _outputs(path, cfg)
    semantic, security = _reviewers(outputs)
    # The root has accepted its child, but has not reassessed its integration.
    semantic.outputs[-1] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, semantic, security, node_id="root")
    status = get_delivery_status(path)
    digest = status.verification.reverification["children"]["integration"]["verification"]["reverification"][
        "reference"
    ]
    directory = delivery_progress_path(Path(status.project_root), status.plan.plan_id).parent
    (directory / f"candidate-node-{digest[7:]}.json").unlink()
    unused = _LLM()
    result, _ = _run(path, cfg, unused, unused, node_id="root")
    assert result.stop_code == "delivery_verification.evidence_unavailable"
    assert not unused.calls


@pytest.mark.parametrize("boundary", [False, True])
def test_superseded_invocation_preserves_descendant_reservations(candidate, boundary):
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = candidate
    outputs = _outputs(path, cfg)
    semantic, security = _reviewers(outputs)
    newer = _LLM(KeyboardInterrupt())
    captured = []

    class Concurrent(_LLM):
        def run_readonly_agent(self, prompt, cwd):
            if len(self.calls) == 1:
                # Resume while the intermediate child's first composition is in flight.
                # The newer attempt reserves its fallback before being interrupted.
                with pytest.raises(KeyboardInterrupt):
                    _run(path, cfg, newer, _LLM(), node_id="root")
                captured.append(get_delivery_status(path).verification)
                if boundary:
                    raise LLMReadOnlyViolation("Old invocation detected a read-only violation")
            return super().run_readonly_agent(prompt, cwd)

    semantic = Concurrent(*semantic.outputs)
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert not result.succeeded
    assert len(captured) == 1 and len(newer.calls) == 1
    status = get_delivery_status(path)
    assert status.verification.attempt == captured[0].attempt == 2
    parent = expanded_child(status, status.verification.reverification["children"]["integration"])
    assert parent.reverification["fallbacks"] == {"semantic": None}
    assert parent.composition_evidence_fingerprint is None
    if boundary:
        assert status.verification.stop_code == "delivery_verification.readonly_mutation"
    else:
        assert status.verification == captured[0]
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id="root")
    assert resumed.stop_code == (
        "delivery_verification.readonly_mutation"
        if boundary
        else "delivery_verification.reverification_budget_exhausted"
    )
    assert not unused.calls


def test_parent_refresh_cannot_adopt_a_newer_initiating_attempt(candidate, monkeypatch):
    from core import delivery_verify as verify

    path, cfg = candidate
    semantic, security = _reviewers(_outputs(path, cfg))
    newer = _LLM(KeyboardInterrupt())
    original = verify._current_candidate_record
    captured = []
    triggered = False

    def resume_before_parent_read(status, snapshot):
        nonlocal triggered
        if not snapshot.candidate_route and not triggered:
            triggered = True
            # The child is already accepted. A newer invocation reserves parent
            # reassessment before the older caller refreshes its local control.
            with pytest.raises(KeyboardInterrupt):
                _run(path, cfg, newer, _LLM(), node_id="root")
            captured.append(get_delivery_status(path).verification)
        return original(status, snapshot)

    monkeypatch.setattr(verify, "_current_candidate_record", resume_before_parent_read)
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert not result.succeeded
    assert len(captured) == 1 and len(newer.calls) == 1
    assert len(semantic.calls) == 4
    current = get_delivery_status(path).verification
    assert current == captured[0] and current.attempt == 2
    assert current.reverification["reassessments"]["semantic"]["accepted"] is False


def test_subtree_budget_preempts_nested_provider(candidate, monkeypatch):
    import core.delivery_candidate_control as control

    path, cfg = candidate
    monkeypatch.setattr(control, "MAX_CANDIDATE_NODES", 2)
    outputs = _outputs(path, cfg)
    semantic, security = _reviewers(outputs)
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == "delivery_verification.reverification_budget_exhausted", result
    assert len(semantic.calls) == 2 and not security.calls
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id="root")
    assert resumed.stop_code == result.stop_code
    assert not unused.calls


def _gap(obligations):
    value = json.loads(delivery_integration_review_control_example(set(obligations)))
    value.update(
        disposition="repair_required",
        findings=[
            {
                "code": "regression",
                "summary": "An update is no longer visible to consumers.",
                "unit_ids": ["foundation"],
                "obligation_ids": ["deliver-team-invites"],
            }
        ],
    )
    for outcome in value["obligation_results"]:
        if outcome["id"] == "deliver-team-invites":
            outcome["outcome"] = "missing"
    return json.dumps(value)


@pytest.mark.parametrize("intermediate_approved", [False, True])
def test_security_requested_descendant_gap_invalidates_ancestor_approvals(candidate, intermediate_approved):
    path, cfg = candidate
    output = _outputs(path, cfg)
    semantic = _LLM(
        output["root", "semantic"][0],
        output["integration", "semantic"][0],
        _gap({"deliver-team-invites"}),
        delivery_integration_review_control_example({"deliver-team-invites", "visible"})
        if intermediate_approved
        else _gap({"deliver-team-invites", "visible"}),
        _gap({"deliver-team-invites", "visible"}),
    )
    security = _LLM(
        output["root", "security"][1],
        output["integration", "security"][1],
        delivery_integration_review_control_example(set()),
    )
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == "delivery_verification.repair_required", result
    assert len(semantic.calls) == 5 and len(security.calls) == (3 if intermediate_approved else 2)
    status = get_delivery_status(path)
    parent = expanded_child(status, status.verification.reverification["children"]["integration"])
    leaf = expanded_child(status, parent.reverification["children"]["storage"])
    assert status.verification.semantic_status == leaf.semantic_status == "rejected"
    assert parent.semantic_status == ("approved" if intermediate_approved else "rejected")
    assert status.verification.repair_input_fingerprint
    assert status.verification.reverification["children"]["integration"]["semantic_gap"]
    from core.delivery_finalize import finalize_delivery_plan

    assert not finalize_delivery_plan(path, project_config=cfg).finalized


@pytest.mark.parametrize(
    "violation,damage", [("readonly", None), ("security", None), ("readonly", "missing"), ("security", "unreadable")]
)
def test_nested_boundary_survives_audit_failure_and_candidate_change(candidate, monkeypatch, violation, damage):
    from core import delivery_verify as verify
    from core.delivery_progress import read_delivery_progress, write_delivery_progress, mark_delivery_assembly
    from core.llm_client import LLMReadOnlyViolation
    from tests.test_delivery_repair import _git

    path, cfg = candidate
    outputs = _outputs(path, cfg)
    root = Path(cfg["project"]["root_path"])
    semantic, security = _reviewers(outputs)
    original_audit = verify._safe_append_audit
    progress_path = delivery_progress_path(root, "team-invites")
    final_branch = get_delivery_status(path).plan.final_branch

    def advance_assembly():
        _git(root, "commit", "--allow-empty", "-m", "concurrent assembly")
        changed = _git(root, "rev-parse", "HEAD")
        progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
        assert not errors
        _git(root, "update-ref", "refs/heads/" + final_branch, changed)
        write_delivery_progress(
            progress_path,
            mark_delivery_assembly(
                progress, base_commit=progress.assembly_base_commit, assembled_commit=changed, status="ready"
            ),
        )

    def audit(file, entry, **kwargs):
        return False if entry.get("event") == "review_failed" else original_audit(file, entry, **kwargs)

    monkeypatch.setattr(verify, "_safe_append_audit", audit)

    class Boundary(_LLM):
        def run_readonly_agent(self, prompt, cwd):
            if violation == "readonly" and len(self.calls) < 2:
                return super().run_readonly_agent(prompt, cwd)
            self.calls.append(prompt)
            advance_assembly()
            progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
            assert not errors
            control = progress.verification.reverification["children"]["integration"]["verification"]["reverification"]
            sidecar = progress_path.parent / f"candidate-node-{control['reference'][7:]}.json"
            assert sidecar.is_file()
            if damage == "missing":
                sidecar.unlink()
            elif damage == "unreadable":
                from core import delivery_candidate_control as control_module

                original_read = control_module.read_repair_state

                def unreadable(project_root, file):
                    if file == sidecar:
                        raise PermissionError("Candidate control unavailable")
                    return original_read(project_root, file)

                monkeypatch.setattr(control_module, "read_repair_state", unreadable)
            if violation == "readonly":
                raise LLMReadOnlyViolation("read-only candidate changed")
            return json.dumps(
                {
                    "schema_version": 2,
                    "disposition": "repair_required",
                    "summary": "Unsafe candidate.",
                    "findings": [
                        {"code": "privacy", "summary": "Disclosure.", "unit_ids": ["foundation"], "obligation_ids": []}
                    ],
                    "obligation_results": [],
                }
            )

    if violation == "readonly":
        semantic = Boundary(*semantic.outputs)
    else:
        security = Boundary()
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    expected = "delivery_verification." + ("readonly_mutation" if violation == "readonly" else "repair_required")
    assert result.stop_code == expected, result
    assert get_delivery_status(path).verification.stop_code == expected
    if violation == "security":
        assert get_delivery_status(path).verification.security_status == "rejected"
    if damage:
        advance_assembly()
    unused = _LLM()
    for _ in range(2):
        resumed, _ = _run(path, cfg, unused, unused, node_id="root")
        assert resumed.stop_code == expected
    assert not unused.calls
