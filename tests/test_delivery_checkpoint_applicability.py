from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

import pytest

from core.delivery_checkpoint_applicability import checkpoint_applicability
from core.delivery_checkpoint_evidence import load_root_evidence, root_evidence_path, store_root_evidence
from core.delivery_checkpoints import with_checkpoint_barriers
from core.delivery_finalize import finalize_delivery_plan
from core.delivery_progress import (
    DeliveryStatusResult,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    read_delivery_progress,
    render_delivery_status,
    upsert_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification import with_delivery_verification_readiness
from core.delivery_verification_model import parse_delivery_verification_record
from sikula_cli.delivery import _preview_delivery_run
from tests.delivery_fixtures import assemble_delivery_fixture
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input
from tests.test_delivery_checkpoints import checkpoint_plan as checkpoint_plan, _complete, _git, _verify_node


def _advance(path: Path, change: str = "unrelated") -> None:
    root = path.parent
    if change == "same_tree":
        _complete(path, "consumer")
    else:
        name = "src/cache.py" if change == "shared_dependency" else "src/formatting.py"
        (root / name).write_text("cache = {'stale': True}\n", encoding="utf-8")
        _git(root, "add", name)
        _git(root, "commit", "-m", "later consumer change")
        progress_path = delivery_progress_path(root, "cache")
        progress, errors = read_delivery_progress(progress_path, plan_id="cache")
        assert not errors
        write_delivery_progress(
            progress_path,
            upsert_delivery_unit_progress(
                progress, make_delivery_unit_progress("consumer", "done", commit=_git(root, "rev-parse", "HEAD"))
            ),
        )
    _, _, error = assemble_delivery_fixture(path)
    assert error is None


def _status(path: Path, cfg: dict) -> DeliveryStatusResult:
    return with_checkpoint_barriers(get_delivery_status(path), cfg)


def _outcome(path: Path, cfg: dict) -> str:
    status = _status(path, cfg)
    checkpoint = status.to_dict()["checkpoints"][0]
    outcome = checkpoint["candidate_evidence"]
    assert f"Checkpoint storage: {checkpoint['status']} (candidate evidence: {outcome})" in render_delivery_status(
        status
    )
    return outcome


@pytest.mark.parametrize("change", ["same_tree", "unrelated", "shared_dependency"])
def test_candidate_evidence_requires_exact_tree_or_fresh_semantic_results(
    checkpoint_plan: tuple[Path, dict], change: str
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    original = get_delivery_status(path).checkpoint_verifications["storage"]
    assert _outcome(path, cfg) == "exact"
    _advance(path, change)
    before = _status(path, cfg)
    assert before.to_dict()["checkpoints"][0]["status"] == "accepted_handoff"
    assert _outcome(path, cfg) == ("exact" if change == "same_tree" else "verification_required")
    result, llm = _verify_node(path, cfg, node_id="root")
    assert result.succeeded, result
    assert len(llm.calls) == 1  # The mandatory root assessment, no extra impact call.
    assert "checkpoint_evidence_fingerprint" not in llm.calls[0]
    assert "root_evidence_fingerprint" not in llm.calls[0]
    assert _outcome(path, cfg) == ("exact" if change == "same_tree" else "reverified")
    current = _status(path, cfg)
    assert current.checkpoint_verifications["storage"] == original
    decision = checkpoint_applicability(current, cfg, current.checkpoint_handoffs)["storage"]
    assert decision.candidate_commit == current.assembled_commit
    assert decision.binding.startswith("sha256:")
    root_record = current.verification
    evidence = load_root_evidence(
        path.parent, delivery_progress_path(path.parent, "cache").parent, root_record, plan_id="cache"
    )
    assert [(r.id, r.outcome) for r in evidence.obligation_results] == [("consistent-read", "satisfied")]
    assert parse_delivery_verification_record(root_record.to_dict()) == root_record
    (path.parent / root_record.evidence_path).unlink()
    again, llm = _verify_node(path, cfg, node_id="root")
    assert again.succeeded and not llm.calls
    assert _outcome(path, cfg) == decision.status
    public = json.dumps(current.to_dict())
    assert "root_evidence_fingerprint" not in public and "root-evidence-" not in public


@pytest.mark.parametrize("change", ["same_tree", "shared_dependency"])
def test_root_findings_prevent_inheriting_historical_checkpoint_pass(
    checkpoint_plan: tuple[Path, dict],
    change: str,
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    original = get_delivery_status(path).checkpoint_verifications["storage"]
    _advance(path, change)
    result, llm = _verify_node(path, cfg, "repair_required", node_id="root")
    assert len(llm.calls) == 1
    assert result.stop_code == "delivery_verification.repair_required"
    assert result.next_action == "run_delivery"
    assert _outcome(path, cfg) == "verification_required"
    current = _status(path, cfg)
    assert current.to_dict()["checkpoints"][0]["status"] == "accepted_handoff"
    decision = checkpoint_applicability(current, cfg, current.checkpoint_handoffs)["storage"]
    assert decision.binding is None
    assert get_delivery_status(path).checkpoint_verifications["storage"] == original
    assert not finalize_delivery_plan(path, project_config=cfg).finalized


@pytest.mark.parametrize("damage", ["missing", "corrupt", "removed_reference", "orphan_reference"])
@pytest.mark.parametrize("already_finalized", [False, True])
def test_missing_root_evidence_blocks_reuse_and_finalization_without_calls(
    checkpoint_plan: tuple[Path, dict], damage: str, already_finalized: bool
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _advance(path)
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    if already_finalized:
        assert finalize_delivery_plan(path, project_config=cfg).finalized
    directory = delivery_progress_path(path.parent, "cache").parent
    progress, _ = read_delivery_progress(directory / "progress.json", plan_id="cache")
    record = progress.verification
    target = root_evidence_path(directory, record.root_evidence_fingerprint)
    before = target.read_bytes()
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_text("{}")
    else:
        changed = replace(
            record, root_evidence_fingerprint=None if damage == "removed_reference" else "sha256:" + "a" * 64
        )
        write_delivery_progress(directory / "progress.json", replace(progress, verification=changed))
    assert _outcome(path, cfg) == "unavailable"
    ready = with_delivery_verification_readiness(get_delivery_status(path), cfg)
    assert ready.verification_status == "blocked"
    assert not ready.valid
    args = argparse.Namespace(plan_file=str(path), max_units=None, max_elapsed_minutes=None)
    preview = _preview_delivery_run(args, cfg, project_root=path.parent)
    assert not preview.ready
    assert any(issue.code == "delivery_verification.evidence_unavailable" for issue in preview.errors)
    result, llm = _verify_node(path, cfg, node_id="root")
    assert result.stop_code == "delivery_verification.evidence_unavailable" and not llm.calls
    assert not finalize_delivery_plan(path, project_config=cfg).finalized
    target.write_bytes(before)
    write_delivery_progress(directory / "progress.json", progress)
    assert _outcome(path, cfg) == "reverified"


@pytest.mark.parametrize("change", ["provider_policy", "authority", "terminal_stop", "assembly_ref"])
def test_applicability_is_bound_to_current_policy_authority_and_terminal_state(
    checkpoint_plan: tuple[Path, dict], change: str
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _advance(path)
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    if change == "provider_policy":
        cfg = {**cfg, "reviewer": {"model": "different-reviewer"}, "llm": {"model": "different"}}
    elif change == "authority":
        (path.parent / "read.md").write_text("Changed contract")
    elif change == "assembly_ref":
        _git(path.parent, "update-ref", "refs/heads/sikula/delivery/cache", "HEAD^")
    else:
        progress_path = delivery_progress_path(path.parent, "cache")
        progress, _ = read_delivery_progress(progress_path, plan_id="cache")
        stopped = replace(
            progress.verification,
            status="failed",
            security_status="rejected",
            stop_code="delivery_verification.repair_required",
        )
        write_delivery_progress(progress_path, replace(progress, verification=stopped))
    assert _outcome(path, cfg) == "unavailable"


def test_interrupted_root_evidence_stays_orphan_until_new_attempt_is_accepted(
    checkpoint_plan: tuple[Path, dict],
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _advance(path)
    directory = delivery_progress_path(path.parent, "cache").parent

    def interrupt(*args: Any, **kwargs: Any) -> NoReturn:
        store_root_evidence(*args, **kwargs)
        raise KeyboardInterrupt()

    with patch("core.delivery_verify.store_root_evidence", side_effect=interrupt), pytest.raises(KeyboardInterrupt):
        _verify_node(path, cfg, node_id="root")
    orphans = {p: p.read_bytes() for p in directory.glob("root-evidence-*.json")}
    assert len(orphans) == 1
    assert _outcome(path, cfg) == "verification_required"
    result, _ = _verify_node(path, cfg, node_id="root")
    assert result.succeeded
    assert _outcome(path, cfg) == "reverified"
    assert all(p.read_bytes() == content for p, content in orphans.items())


def test_root_publication_rechecks_artifact_before_accepting(checkpoint_plan: tuple[Path, dict]) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _advance(path)

    def damage(root: Path, directory: Path, *args: Any) -> str:
        fingerprint = store_root_evidence(root, directory, *args)
        root_evidence_path(directory, fingerprint).unlink()
        return fingerprint

    with patch("core.delivery_verify.store_root_evidence", side_effect=damage):
        result, _ = _verify_node(path, cfg, node_id="root")
    assert result.stop_code == "delivery_verification.evidence_unavailable"
    assert _outcome(path, cfg) == "verification_required"


def test_candidate_move_during_root_evidence_publication_cannot_accept_orphan(
    checkpoint_plan: tuple[Path, dict],
) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _advance(path)
    before = get_delivery_status(path)
    origin = before.checkpoint_verifications["storage"]
    root = path.parent
    directory = delivery_progress_path(root, "cache").parent

    def advance(*args: Any) -> str:
        fingerprint = store_root_evidence(*args)
        tree = _git(root, "rev-parse", before.assembled_commit + "^{tree}")
        commit = _git(root, "commit-tree", tree, "-p", before.assembled_commit, "-m", "concurrent assembly")
        _git(root, "update-ref", "refs/heads/sikula/delivery/cache", commit)
        return fingerprint

    with patch("core.delivery_verify.store_root_evidence", side_effect=advance):
        result, _ = _verify_node(path, cfg, node_id="root")
    assert not result.succeeded and result.status == "stale"
    assert list(directory.glob("root-evidence-*.json"))
    assert _outcome(path, cfg) == "unavailable"
    assert get_delivery_status(path).checkpoint_verifications["storage"] == origin
    _git(root, "update-ref", "refs/heads/sikula/delivery/cache", before.assembled_commit)
    assert _outcome(path, cfg) == "verification_required"
    assert _verify_node(path, cfg, node_id="root")[0].succeeded
    assert _outcome(path, cfg) == "reverified"


def test_root_artifacts_preserve_distinct_results_when_attempt_number_is_reused(evidence_input: tuple) -> None:
    root, directory, snapshot, record, assessment = evidence_input
    snapshot = replace(snapshot, scope=replace(snapshot.scope, node_id="root"))
    first = store_root_evidence(root, directory, snapshot, record, assessment)
    second_record = replace(record, completed_at="later")
    second = store_root_evidence(root, directory, snapshot, second_record, assessment)
    assert first != second
    for fingerprint, result in ((first, record), (second, second_record)):
        result = replace(result, root_evidence_fingerprint=fingerprint)
        assert load_root_evidence(root, directory, result, plan_id="cache").verification == result
        assert store_root_evidence(root, directory, snapshot, result, assessment) == fingerprint
        assert "Private review summary" not in root_evidence_path(directory, fingerprint).read_text()


@pytest.mark.parametrize("fingerprint", [True, {}, "../outside", "sha256:bad"])
def test_root_evidence_reference_rejects_invalid_identities(evidence_input: tuple, fingerprint: object) -> None:
    payload = evidence_input[3].to_dict()
    payload["root_evidence_fingerprint"] = fingerprint
    with pytest.raises(ValueError):
        parse_delivery_verification_record(payload)
    with pytest.raises(ValueError):
        root_evidence_path(evidence_input[1], fingerprint)


def test_projection_without_effective_policy_cannot_claim_current_evidence(checkpoint_plan: tuple[Path, dict]) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    status = get_delivery_status(path)
    assert status.to_dict()["checkpoints"][0]["candidate_evidence"] == "not_checked"
    assert "(candidate evidence: not_checked)" in render_delivery_status(status)
