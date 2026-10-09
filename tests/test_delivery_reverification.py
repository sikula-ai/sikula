from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.delivery_checkpoint_applicability import validate_root_evidence
from core.delivery_checkpoint_evidence import checkpoint_evidence_path
from core.delivery_composition import composition_example
from core.delivery_progress import get_delivery_status, delivery_progress_path
from core.delivery_verification import with_delivery_verification_readiness
from core.delivery_verification_model import parse_delivery_verification_record
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_final_closure as closure_fixtures
from tests.test_delivery_final_closure import _contexts
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_repair import _LLM


@pytest.fixture
def closure_plan(tmp_path, request):
    return closure_fixtures.closure_plan.__wrapped__(tmp_path, request)


def _outputs(path, cfg):
    contexts = _contexts(path, cfg)
    approved = [composition_example(context) for context in contexts]
    uncertain = []
    for output in approved:
        value = json.loads(output)
        value["checkpoint_results"][0]["outcome"] = "verification_required"
        uncertain.append(json.dumps(value))
    full = [
        delivery_integration_review_control_example(set(get_delivery_status(path).plan.checkpoints[0].obligation_ids)),
        delivery_integration_review_control_example(set()),
    ]
    return approved, uncertain, full


def _advance_candidate(path, cfg):
    from core.delivery_progress import (
        read_delivery_progress,
        write_delivery_progress,
        upsert_delivery_unit_progress,
        make_delivery_unit_progress,
    )
    from tests.delivery_fixtures import assemble_delivery_fixture
    from tests.test_delivery_repair import _git

    root = Path(cfg["project"]["root_path"])
    (root / "agents/code.py").write_text("value = 2\n", encoding="utf-8", newline="\n")
    _git(root, "add", "agents/code.py")
    _git(root, "commit", "-m", "change shared implementation")
    progress_path = delivery_progress_path(root, "team-invites")
    progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
    assert not errors
    write_delivery_progress(
        progress_path,
        upsert_delivery_unit_progress(
            progress, make_delivery_unit_progress("consumer", "done", commit=_git(root, "rev-parse", "HEAD"))
        ),
    )
    assert assemble_delivery_fixture(path)[2] is None


@pytest.mark.parametrize("role", ["semantic", "security", "both"])
def test_uncertain_child_is_reverified_without_full_parent_review(closure_plan, role):
    path, cfg = closure_plan
    _advance_candidate(path, cfg)
    before = get_delivery_status(path)
    approved, uncertain, full = _outputs(path, cfg)
    if role in {"semantic", "both"}:
        semantic = _LLM(uncertain[0], full[0], approved[0])
        security = _LLM(full[1], uncertain[1], approved[1]) if role == "both" else _LLM(full[1], approved[1])
    else:
        semantic = _LLM(approved[0], full[0])
        security = _LLM(uncertain[1], full[1], approved[1])
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    after = get_delivery_status(path)
    assert after.checkpoint_verifications == before.checkpoint_verifications
    assert after.units == before.units
    assert set(after.verification.reverification["children"]) == {"storage"}
    child = parse_delivery_verification_record(after.verification.reverification["children"]["storage"]["verification"])
    assert child.passed and child.security_status == "approved"
    assert child.candidate_commit == after.assembled_commit
    assert child.gate_id != before.checkpoint_verifications["storage"].gate_id
    assert child.candidate_tree != before.checkpoint_verifications["storage"].candidate_tree
    from core.delivery_composition import build_composition_context
    from core.delivery_verification import build_delivery_verification_snapshot

    snapshot = build_delivery_verification_snapshot(after, cfg, candidate_commit=after.assembled_commit)
    context = build_composition_context(after, snapshot, cfg, record=after.verification)
    assert context["checkpoint_composition"]["children"][0]["exact_tree"]
    assert "CHILD_INTERNAL" not in json.dumps(context)
    assert "reverification" not in json.dumps(context)
    assert "obligation_results" not in json.dumps(context)
    public = json.dumps(after.to_dict())
    assert '"reverification":' not in public and "CHILD_INTERNAL" not in public
    assert "candidate-review-" not in public
    assert after.verification.reverification["fallbacks"] == {}
    assert sum("CHILD_INTERNAL" in call for call in semantic.calls) == 1
    assert sum("CHILD_INTERNAL" in call for call in security.calls) == 1
    assert len(semantic.calls) + len(security.calls) == (6 if role == "both" else 5)
    validate_root_evidence(after, after.verification)
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id="root")
    assert resumed.succeeded and not unused.calls
    from core.delivery_finalize import finalize_delivery_plan

    assert finalize_delivery_plan(path, project_config=cfg).finalized


@pytest.mark.parametrize("point", ["child", "parent", "accepted"])
def test_candidate_review_resume_preserves_reservations(closure_plan, point):

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    if point == "child":
        semantic = _LLM(uncertain[0], KeyboardInterrupt())
        security = _LLM()
    else:
        semantic = _LLM(uncertain[0], full[0], KeyboardInterrupt() if point == "parent" else approved[0])
        security = _LLM(full[1], KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, semantic, security, node_id="root")
    status = get_delivery_status(path)
    child = status.verification.reverification["children"]["storage"]["verification"]
    assert child["status"] == ("running" if point == "child" else "passed")
    sem = _LLM(full[0]) if point != "accepted" else _LLM()
    sec = _LLM(full[1] if point == "accepted" else approved[1])
    resumed, _ = _run(path, cfg, sem, sec, node_id="root")
    assert resumed.succeeded, resumed
    assert len(sem.calls) == (0 if point == "accepted" else 1)
    assert get_delivery_status(path).checkpoint_verifications["storage"].attempt == 1


def test_repeated_uncertainty_uses_only_one_persistent_fallback(closure_plan):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    semantic = _LLM(uncertain[0], full[0], uncertain[0], KeyboardInterrupt())
    security = _LLM(full[1])
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, semantic, security, node_id="root")
    assert len(semantic.calls) == 4
    unused = _LLM()
    for _ in range(2):
        resumed, _ = _run(path, cfg, unused, unused, node_id="root")
        assert resumed.stop_code == "delivery_verification.reverification_budget_exhausted", resumed
    assert not unused.calls


@pytest.mark.parametrize("failure", ["readonly", "security", "external"])
def test_candidate_review_boundary_preempts_fallback(closure_plan, failure):
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    if failure == "readonly":
        semantic = _LLM(uncertain[0], LLMReadOnlyViolation("boundary"))
        security = _LLM()
    elif failure == "external":
        negative = json.loads(full[0])
        negative["disposition"] = "external_dependency_gap"
        negative["obligation_results"][0]["outcome"] = "uncertain"
        negative["findings"] = [
            {
                "code": "dependency",
                "summary": "Required service unavailable.",
                "unit_ids": ["foundation"],
                "obligation_ids": [negative["obligation_results"][0]["id"]],
            }
        ]
        semantic = _LLM(uncertain[0], json.dumps(negative))
        security = _LLM()
    else:
        negative = json.loads(full[1])
        negative.update(
            disposition="repair_required",
            findings=[{"code": "privacy", "summary": "Disclosure.", "unit_ids": ["foundation"], "obligation_ids": []}],
        )
        semantic = _LLM(uncertain[0], full[0])
        security = _LLM(json.dumps(negative))
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert not result.succeeded
    assert (
        result.stop_code
        == "delivery_verification."
        + {"readonly": "readonly_mutation", "security": "repair_required", "external": "external_dependency_gap"}[
            failure
        ]
    )
    if failure == "external":
        retried = _LLM(full[0])
        again, _ = _run(path, cfg, retried, _LLM(approved[1]), node_id="root")
        assert again.succeeded, again
        assert len(retried.calls) == 1
    else:
        unused = _LLM()
        again, _ = _run(path, cfg, unused, unused, node_id="root")
        assert again.stop_code == result.stop_code
        assert not unused.calls


def test_missing_fresh_child_evidence_blocks_before_resume(closure_plan):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, _LLM(uncertain[0], full[0], KeyboardInterrupt()), _LLM(full[1]), node_id="root")
    status = get_delivery_status(path)
    record = parse_delivery_verification_record(
        status.verification.reverification["children"]["storage"]["verification"]
    )
    root = Path(cfg["project"]["root_path"])
    checkpoint_evidence_path(delivery_progress_path(root, status.plan.plan_id).parent, record).write_text("{}")
    unused = _LLM()
    result, _ = _run(path, cfg, unused, unused, node_id="root")
    assert not result.succeeded and not unused.calls
    assert result.stop_code == "delivery_verification.evidence_unavailable"
    assert not with_delivery_verification_readiness(status, cfg).valid


def test_completed_fallback_is_reused_after_security_interruption(closure_plan):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    with pytest.raises(KeyboardInterrupt):
        _run(
            path,
            cfg,
            _LLM(uncertain[0], full[0], uncertain[0], full[0]),
            _LLM(full[1], KeyboardInterrupt()),
            node_id="root",
        )
    unused = _LLM()
    result, _ = _run(path, cfg, unused, _LLM(full[1]), node_id="root")
    assert result.succeeded, result
    assert not unused.calls
    assert get_delivery_status(path).verification.reverification["fallbacks"]["semantic"]


@pytest.mark.parametrize("closure_plan", [True], indirect=True)
def test_fresh_child_does_not_approve_cross_group_gap(closure_plan):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    rejection = json.loads(approved[0])
    rejection.update(
        disposition="repair_required",
        findings=[
            {
                "code": "integration",
                "summary": "Consumer fails to observe writes.",
                "unit_ids": ["consumer"],
                "obligation_ids": ["consumer-visible"],
            }
        ],
    )
    rejection["obligation_results"][0]["outcome"] = "missing"
    fallback = json.loads(
        delivery_integration_review_control_example({item.id for item in get_delivery_status(path).plan.obligations})
    )
    fallback.update(disposition="repair_required", findings=rejection["findings"])
    for outcome in fallback["obligation_results"]:
        if outcome["id"] == "consumer-visible":
            outcome["outcome"] = "missing"
    semantic = _LLM(uncertain[0], full[0], json.dumps(rejection), json.dumps(fallback))
    security = _LLM(full[1])
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == "delivery_verification.repair_required", result
    record = get_delivery_status(path).verification
    assert record.repair_input_fingerprint
    assert record.reverification["children"]["storage"]["verification"]["status"] == "passed"
    assert len(semantic.calls) == 4 and len(security.calls) == 1


def test_rendered_child_prompt_preflight_is_read_only(tmp_path, monkeypatch):
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
    from core.delivery_verification import check_delivery_verification_readiness

    path, cfg, checked, *_ = closure_fixtures._prepared(tmp_path)
    original = DeliveryIntegrationReviewAgent._prompt

    def oversized(self, **kwargs):
        if kwargs["review_kind"] == "security" and kwargs["plan_context"].get("verification_node"):
            return "x" * (512 * 1024 + 1)
        return original(self, **kwargs)

    monkeypatch.setattr(DeliveryIntegrationReviewAgent, "_prompt", oversized)
    readiness = check_delivery_verification_readiness(checked, cfg)
    assert not readiness.ready
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in readiness.errors)
    assert not delivery_progress_path(tmp_path, checked.plan.plan_id).exists()


@pytest.mark.parametrize("change", ["unknown", "recursive", "wrong_candidate", "fan_in", "role", "digest"])
def test_candidate_control_rejects_malformed_persisted_state(change):
    from core.delivery_verification_model import DeliveryVerificationRecord
    from core.delivery_reverification import new_reverification

    record = DeliveryVerificationRecord(
        1, "sha256:" + "a" * 64, "b" * 40, "c" * 40, *["sha256:" + "d" * 64] * 5, status="running", attempt=1
    )
    control = new_reverification()
    child = record.to_dict()
    control["children"]["storage"] = {"origin": "sha256:" + "e" * 64, "verification": child}
    if change == "unknown":
        control["extra"] = True
    elif change == "recursive":
        child["reverification"] = new_reverification()
    elif change == "wrong_candidate":
        child["candidate_tree"] = "f" * 40
    elif change == "fan_in":
        control["children"] = {f"group-{i}": control["children"]["storage"] for i in range(9)}
    elif change == "role":
        control["fallbacks"]["implementer"] = None
    else:
        control["reassessments"]["semantic"] = {"initial": "invalid", "accepted": True}
    value = record.to_dict()
    value.update(composition_attempted=True, reverification=control)
    with pytest.raises(ValueError):
        parse_delivery_verification_record(value)


@pytest.mark.parametrize("concurrent", [False, True])
def test_child_readonly_stop_survives_failed_audit_and_candidate_change(closure_plan, monkeypatch, concurrent):
    from core import delivery_verify as verify
    from core.delivery_progress import read_delivery_progress, write_delivery_progress, mark_delivery_assembly
    from core.llm_client import LLMReadOnlyViolation
    from tests.test_delivery_repair import _git

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    root = Path(cfg["project"]["root_path"])
    before = get_delivery_status(path)
    original_audit = verify._safe_append_audit

    def audit(file, entry, **kwargs):
        return False if entry.get("event") == "review_failed" else original_audit(file, entry, **kwargs)

    class BoundaryLLM(_LLM):
        def run_readonly_agent(self, prompt, cwd):
            if not self.calls:
                return super().run_readonly_agent(prompt, cwd)
            self.calls.append(prompt)
            if concurrent:
                _git(root, "commit", "--allow-empty", "-m", "advance assembly during review")
                commit = _git(root, "rev-parse", "HEAD")
                _git(root, "update-ref", "refs/heads/" + before.plan.final_branch, commit)
                progress_path = delivery_progress_path(root, before.plan.plan_id)
                progress, errors = read_delivery_progress(progress_path, plan_id=before.plan.plan_id)
                assert not errors
                write_delivery_progress(
                    progress_path,
                    mark_delivery_assembly(
                        progress, base_commit=progress.assembly_base_commit, assembled_commit=commit, status="ready"
                    ),
                )
            raise LLMReadOnlyViolation("Provider workspace changed.")

    monkeypatch.setattr(verify, "_safe_append_audit", audit)
    result, _ = _run(path, cfg, BoundaryLLM(uncertain[0]), _LLM(), node_id="root")
    assert result.stop_code == "delivery_verification.readonly_mutation", result
    status = get_delivery_status(path)
    assert status.valid
    assert status.verification.stop_code == result.stop_code
    assert status.checkpoint_verifications == before.checkpoint_verifications
    if concurrent:
        assert status.assembled_commit != status.verification.candidate_commit
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.stop_code == result.stop_code and not unused.calls


def test_dry_run_reports_exhausted_fallback_without_writing(closure_plan):
    import argparse
    from sikula_cli.delivery import _preview_delivery_run

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, _LLM(uncertain[0], full[0], uncertain[0], KeyboardInterrupt()), _LLM(full[1]), node_id="root")
    root = Path(cfg["project"]["root_path"])
    progress_path = delivery_progress_path(root, "team-invites")
    before = progress_path.read_bytes()
    result = _preview_delivery_run(argparse.Namespace(plan_file=str(path), max_units=None), cfg, project_root=root)
    assert not result.ready
    assert any(issue.code == "delivery_verification.reverification_budget_exhausted" for issue in result.errors)
    assert progress_path.read_bytes() == before


def test_mutation_during_interrupted_child_is_terminal(closure_plan):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)

    class MutatingLLM(_LLM):
        def run_readonly_agent(self, prompt, cwd):
            if not self.calls:
                return super().run_readonly_agent(prompt, cwd)
            self.calls.append(prompt)
            (cwd / "agents/code.py").write_text("value = 99\n", encoding="utf-8", newline="\n")
            raise KeyboardInterrupt

    llm = MutatingLLM(uncertain[0])
    result, _ = _run(path, cfg, llm, _LLM(), node_id="root")
    assert result.stop_code == "delivery_verification.readonly_mutation", result
    assert len(llm.calls) == 2
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.stop_code == result.stop_code and not unused.calls


@pytest.mark.parametrize("target", ["child", "fallback"])
def test_successful_mutating_review_survives_audit_failure(closure_plan, monkeypatch, target):
    from core import delivery_verify as verify

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    original_audit = verify._safe_append_audit
    mutation_call = 2 if target == "child" else 4

    class MutatingLLM(_LLM):
        mutated = False

        def run_readonly_agent(self, prompt, cwd):
            result = super().run_readonly_agent(prompt, cwd)
            if len(self.calls) == mutation_call:
                (cwd / "agents/code.py").write_text("value = 99\n", encoding="utf-8", newline="\n")
                self.mutated = True
            return result

    llm = MutatingLLM(uncertain[0], full[0], uncertain[0], full[0])

    def audit(file, entry, **kwargs):
        if llm.mutated and entry.get("event") == "semantic_review":
            return False
        return original_audit(file, entry, **kwargs)

    monkeypatch.setattr(verify, "_safe_append_audit", audit)
    result, _ = _run(path, cfg, llm, _LLM(full[1]), node_id="root")
    assert result.stop_code == "delivery_verification.readonly_mutation", result
    assert len(llm.calls) == mutation_call
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.stop_code == result.stop_code and not unused.calls


def test_preflight_counts_executed_check_names_even_if_validation_is_reusable(tmp_path):
    from core.delivery_verification import check_delivery_verification_readiness, MAX_DELIVERY_VERIFICATION_PACKET_BYTES

    path, cfg, checked, *_ = closure_fixtures._prepared(tmp_path)
    cfg = {
        **cfg,
        "run_checks": True,
        "build": {**cfg.get("build", {}), "checks": [{"name": "č" * 45000, "command": "true"}]},
    }
    result = check_delivery_verification_readiness(checked, cfg)
    assert result.packet_bytes < MAX_DELIVERY_VERIFICATION_PACKET_BYTES
    assert not result.ready
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in result.errors)
    assert not delivery_progress_path(tmp_path, checked.plan.plan_id).exists()


@pytest.mark.parametrize("node", ["root", "storage"])
def test_review_prompt_only_includes_applicable_validation_policy(tmp_path, node):
    from types import SimpleNamespace
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
    from core.delivery_verification_validation import DeliveryVerificationValidationResult
    from core.delivery_verify import _run_review

    cfg = {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "delivery": {"verification": {"final_checks": [{"name": "release", "command": "FINAL_ONLY_CHECK"}]}},
    }
    llm = _LLM(delivery_integration_review_control_example(set()))
    result = _run_review(
        DeliveryIntegrationReviewAgent(llm, cfg),
        worktree=tmp_path,
        kind="semantic",
        source_task="Check the implementation.",
        plan_context={} if node == "root" else {"verification_node": {"id": node}},
        validation=DeliveryVerificationValidationResult(True, False, True),
        identity=SimpleNamespace(gate_id="sha256:" + "a" * 64, candidate_commit="b" * 40, candidate_tree="c" * 40),
        known_unit_ids=set(),
        known_obligation_ids=set(),
        evidence_path=tmp_path / "verification.jsonl",
        audit_root=tmp_path,
    )
    assert result.assessment.approved
    assert ("FINAL_ONLY_CHECK" in llm.calls[0]) is (node == "root")


@pytest.mark.parametrize("recovery", ["repair", "approved", "interrupted", "spent", "resume"])
def test_security_requested_child_gap_invalidates_earlier_semantic_approval(closure_plan, recovery, monkeypatch):
    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    gap = json.loads(full[0])
    gap.update(
        disposition="repair_required",
        findings=[
            {
                "code": "regression",
                "summary": "Storage no longer preserves updates.",
                "unit_ids": ["foundation"],
                "obligation_ids": [gap["obligation_results"][0]["id"]],
            }
        ],
    )
    gap["obligation_results"][0]["outcome"] = "missing"
    gap = json.dumps(gap)
    if recovery == "spent":
        semantic = _LLM("malformed", "malformed", full[0], gap)
    else:
        semantic = _LLM(
            approved[0],
            gap,
            KeyboardInterrupt() if recovery == "interrupted" else full[0] if recovery == "approved" else gap,
        )
    security = _LLM(uncertain[1], full[1])
    if recovery == "resume":
        from core import delivery_verify as verify
        from core.delivery_reverification import semantic_gap_ids

        original_save = verify._save_reverification
        interrupted = False

        def save(*args, **kwargs):
            nonlocal interrupted
            record = original_save(*args, **kwargs)
            if semantic_gap_ids(record) and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return record

        monkeypatch.setattr(verify, "_save_reverification", save)
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, semantic, security, node_id="root")
        result, _ = _run(path, cfg, semantic, security, node_id="root")
    elif recovery == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, semantic, security, node_id="root")
        unused = _LLM()
        result, _ = _run(path, cfg, unused, unused, node_id="root")
        assert not unused.calls
    else:
        result, _ = _run(path, cfg, semantic, security, node_id="root")
    record = get_delivery_status(path).verification
    assert record.reverification["children"]["storage"]["verification"]["semantic_status"] == "rejected"
    if recovery == "approved":
        assert result.succeeded, result
        assert len(semantic.calls) == 3 and len(security.calls) == 2
        validate_root_evidence(get_delivery_status(path), record)
        unused = _LLM()
        resumed, _ = _run(path, cfg, unused, unused, node_id="root")
        assert resumed.succeeded and not unused.calls
    else:
        assert not result.succeeded, result
        expected = "repair_required" if recovery in {"repair", "resume"} else "reverification_budget_exhausted"
        assert result.stop_code == "delivery_verification." + expected, result
        assert len(security.calls) == 1
        if recovery in {"repair", "resume"}:
            assert record.repair_input_fingerprint
            assert len(semantic.calls) == 3
        else:
            projected = with_delivery_verification_readiness(get_delivery_status(path), cfg)
            assert any(issue.code == result.stop_code for issue in projected.errors)


def test_security_fallback_rejection_survives_concurrent_assembly(closure_plan):
    from core.delivery_progress import read_delivery_progress, write_delivery_progress, mark_delivery_assembly
    from tests.test_delivery_repair import _git

    path, cfg = closure_plan
    approved, uncertain, full = _outputs(path, cfg)
    root = Path(cfg["project"]["root_path"])
    before = get_delivery_status(path)
    rejected = json.loads(full[1])
    rejected.update(
        disposition="repair_required",
        findings=[
            {
                "code": "privacy",
                "summary": "Candidate exposes private values.",
                "unit_ids": ["foundation"],
                "obligation_ids": [],
            }
        ],
    )

    class AdvancingLLM(_LLM):
        def run_readonly_agent(self, prompt, cwd):
            result = super().run_readonly_agent(prompt, cwd)
            if len(self.calls) == 4:
                _git(root, "commit", "--allow-empty", "-m", "advance during security fallback")
                commit = _git(root, "rev-parse", "HEAD")
                _git(root, "update-ref", "refs/heads/" + before.plan.final_branch, commit)
                progress_path = delivery_progress_path(root, before.plan.plan_id)
                progress, errors = read_delivery_progress(progress_path, plan_id=before.plan.plan_id)
                assert not errors
                write_delivery_progress(
                    progress_path,
                    mark_delivery_assembly(
                        progress, base_commit=progress.assembly_base_commit, assembled_commit=commit, status="ready"
                    ),
                )
            return result

    semantic = _LLM(approved[0], full[0])
    security = AdvancingLLM(uncertain[1], full[1], uncertain[1], json.dumps(rejected))
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == "delivery_verification.repair_required", result
    after = get_delivery_status(path)
    assert after.verification.security_status == "rejected"
    assert after.assembled_commit != after.verification.candidate_commit
    unused = _LLM()
    resumed, _ = _run(path, cfg, unused, unused, node_id="root")
    assert resumed.stop_code == result.stop_code and not unused.calls
