from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import pytest

from core.delivery_candidate_repair import CANDIDATE_REPAIR_REQUIRED, resolve_candidate_repair
from core.delivery_progress import delivery_progress_path, get_delivery_status, read_delivery_progress
from core.delivery_repair import _read_state
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_recursive_reverification as recursive
from tests.test_delivery_recursive_reverification import _gap, _outputs
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_repair import _LLM, _draft, _repair


@pytest.fixture
def candidate(tmp_path):
    return recursive.candidate.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "role,interruption",
    [
        pytest.param("semantic", None, id="semantic"),
        pytest.param("security", "plan", id="security"),
        pytest.param("security", "progress", id="security-progress"),
    ],
)
def test_nested_candidate_gap_routes_to_leaf_repair(candidate, role, interruption):
    path, cfg = candidate
    before = get_delivery_status(path)
    outputs = _outputs(path, cfg)
    semantic = _LLM(
        outputs["root", "semantic"][role == "semantic"],
        outputs["integration", "semantic"][role == "semantic"],
        _gap({"deliver-team-invites"}),
    )
    security = _LLM(outputs["root", "security"][1], outputs["integration", "security"][1])
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == CANDIDATE_REPAIR_REQUIRED, result
    assert len(semantic.calls) == 3
    assert len(security.calls) == (2 if role == "security" else 0)
    status = get_delivery_status(path)
    assert status.checkpoint_verifications == before.checkpoint_verifications
    selected, binding = resolve_candidate_repair(status, cfg)
    assert selected.verification_node == "storage"
    assert binding["node_id"] == "root"
    unused = _LLM()
    repeated, _ = _run(path, cfg, unused, unused, node_id="root")
    assert repeated.stop_code == CANDIDATE_REPAIR_REQUIRED and not unused.calls
    preview = _repair(path, cfg, unused, dry_run=True)
    assert preview.ready, preview
    root = Path(status.project_root)
    assert _read_state(root, status.plan.plan_id, "storage") is None
    if role == "semantic":
        # An ancestor's consumed allowance must not be charged for a leaf finding.
        def ancestor_budget(project, plan_id, node_id="root"):
            return {"phase": "published"} if node_id == "root" else _read_state(project, plan_id, node_id)

        with patch("core.delivery_repair._read_state", side_effect=ancestor_budget):
            independent = _repair(path, cfg, unused, node_id="root", dry_run=True)
        assert independent.ready and not unused.calls, independent
    from core.delivery_repair_input import _input_path
    from core.delivery_progress import write_delivery_progress

    # Missing negative evidence and a superseded initiating attempt must stop
    # before authoring, even though historical checkpoint passes remain intact.
    input_path = _input_path(
        delivery_progress_path(root, status.plan.plan_id).parent,
        selected.verification.gate_id,
        selected.verification.attempt,
    )
    original_input = input_path.read_bytes()
    input_path.unlink()
    missing = _repair(path, cfg, unused, dry_run=True)
    assert not missing.ready and missing.issue.code == "delivery_repair.evidence_unavailable"
    input_path.write_bytes(original_input)
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    captured, _ = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    write_delivery_progress(
        progress_path,
        replace(captured, verification=replace(captured.verification, attempt=captured.verification.attempt + 1)),
    )
    superseded = _repair(path, cfg, unused, dry_run=True)
    assert not superseded.ready
    write_delivery_progress(progress_path, captured)
    assert not unused.calls and _read_state(root, status.plan.plan_id, "storage") is None

    # Editing only newline encoding cannot change source authority on Windows.
    source = root / status.plan.source_task.path
    original_source = source.read_bytes()
    source.write_bytes(original_source.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    llm = _LLM(_draft(markdown=(root / selected.plan.units[0].task_path).read_text()))
    if interruption:
        from core import delivery_repair

        target = "_atomic_replace_if_unchanged" if interruption == "plan" else "_append_audit"
        publish = getattr(delivery_repair, target)

        def interrupt_publication(*args, **kwargs):
            publish(*args, **kwargs)
            if interruption == "plan" or args[1].get("event") == "published":
                raise KeyboardInterrupt

        with patch.object(delivery_repair, target, side_effect=interrupt_publication):
            with pytest.raises(KeyboardInterrupt):
                _repair(path, cfg, llm)
        assert _read_state(root, status.plan.plan_id, "storage")["phase"] == "prepared"
        input_path.unlink()
        missing = _repair(path, cfg, unused, dry_run=True)
        assert not missing.ready and missing.issue.code == "delivery_repair.evidence_unavailable"
        input_path.write_bytes(original_input)
    repaired = _repair(path, cfg, llm)
    source.write_bytes(original_source)
    assert repaired.ready, repaired
    assert len(llm.calls) == 1
    assert "UNRELATED" not in llm.calls[0]
    assert "CACHE_KEY" in llm.calls[0]
    control = _read_state(root, status.plan.plan_id, "storage")
    assert control["candidate_recovery"] == binding
    assert control["phase"] == "published" and control["attempts"] == 1
    assert _read_state(root, status.plan.plan_id, "root") is None
    after = get_delivery_status(path)
    assert after.checkpoint_verifications == before.checkpoint_verifications
    assert {unit.id: unit.depends_on for unit in after.plan.units if unit.id != repaired.unit_id} == {
        unit.id: unit.depends_on for unit in before.plan.units
    }
    assert repaired.unit_id in next(cp for cp in after.plan.checkpoints if cp.id == "storage").unit_ids
    progress, errors = read_delivery_progress(
        delivery_progress_path(root, status.plan.plan_id), plan_id=status.plan.plan_id
    )
    assert not errors
    assert control["completed_progress"]["units"] == [item.to_dict() for item in progress.units]

    from core.delivery_checkpoints import checkpoint_barrier_issue
    from core.delivery_run_next import preview_delivery_run_next
    from core.delivery_finalize import finalize_delivery_plan
    from tests.test_delivery_checkpoints import _complete

    preview = preview_delivery_run_next(path)
    assert preview.ready and preview.selected_unit.id == repaired.unit_id, preview
    barrier = checkpoint_barrier_issue(after, cfg, unit_id="shipping")
    assert barrier is not None and barrier.code == "delivery_checkpoint.required"
    if role == "security":
        _finish_with_delivery_run(path, cfg, repaired.unit_id)
    else:
        _complete(path, repaired.unit_id)
        for node, obligations in (
            ("storage", {"deliver-team-invites"}),
            ("integration", {"deliver-team-invites", "visible"}),
            ("root", {"deliver-team-invites", "visible"}),
        ):
            verified, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example(obligations)), node_id=node)
            assert verified.succeeded, verified
        assert finalize_delivery_plan(path, project_config=cfg).finalized
    exhausted = _repair(path, cfg, _LLM(), node_id="storage", dry_run=True)
    assert exhausted.issue.code == "delivery_repair.budget_exhausted"


@pytest.mark.parametrize("change", ["extra", "attempt", "node", "digest", "missing"])
def test_candidate_repair_binding_rejects_malformed_control(change):
    from core.delivery_candidate_repair import validate_recovery_binding

    binding = {
        "node_id": "root",
        "gate_id": "sha256:" + "a" * 64,
        "attempt": 1,
        "repair_input_fingerprint": "sha256:" + "b" * 64,
    }
    validate_recovery_binding(binding)
    if change == "extra":
        binding["untrusted"] = "ignored"
    elif change == "attempt":
        binding["attempt"] = True
    elif change == "node":
        binding["node_id"] = "../private"
    elif change == "digest":
        binding["repair_input_fingerprint"] = "sha256:unknown"
    else:
        del binding["gate_id"]
    with pytest.raises(ValueError):
        validate_recovery_binding(binding)


def test_local_repair_does_not_require_another_consumed_ancestor_fallback(tmp_path):
    import json
    from types import SimpleNamespace
    from core.delivery_composition import fingerprint
    from core.delivery_repair_storage import write_repair_state
    from core.delivery_reverification import review_result_path, reverification_budget_exhausted
    from tests.test_delivery_candidate_control import _record, _entry

    status = SimpleNamespace(project_root=str(tmp_path), plan=SimpleNamespace(plan_id="candidate"))
    leaf = replace(
        _record("2"),
        status="failed",
        semantic_status="rejected",
        stop_code="delivery_verification.repair_required",
        reverification=None,
    )
    parent = replace(_record("1"), status="failed", semantic_status="rejected", stop_code=CANDIDATE_REPAIR_REQUIRED)
    parent.reverification["children"]["storage"] = _entry(leaf)
    # This accepted fallback predates the subsequently discovered child gap.
    payload = {
        "gate_id": parent.gate_id,
        "role": "semantic",
        "assessment": json.loads(delivery_integration_review_control_example({"deliver-team-invites"})),
        "semantic_gaps": [],
    }
    digest = fingerprint(payload)
    write_repair_state(
        tmp_path, review_result_path(delivery_progress_path(tmp_path, "candidate").parent, digest), payload
    )
    parent.reverification["fallbacks"]["semantic"] = digest
    assert not reverification_budget_exhausted(status, parent)
    assert reverification_budget_exhausted(status, replace(parent, stop_code="delivery_verification.repair_required"))
    # Missing accepted evidence still exhausts a reserved exchange in either path.
    parent.reverification["fallbacks"]["semantic"] = None
    assert reverification_budget_exhausted(status, parent)


@pytest.mark.parametrize("change", ["authority", "owner", "finding", "parent", "security", "readonly"])
def test_only_independently_attributed_local_semantic_gaps_qualify(tmp_path, monkeypatch, change):
    from types import SimpleNamespace
    from core import delivery_candidate_repair as module
    from core.delivery_verification_review import parse_delivery_integration_review
    from tests.test_delivery_candidate_control import _record
    from tests.test_delivery_nested_checkpoints import _prepared

    _, cfg, checked, *_ = _prepared(tmp_path)
    assessment = parse_delivery_integration_review(
        _gap({"deliver-team-invites"}),
        known_unit_ids={"foundation"},
        known_obligation_ids={"deliver-team-invites"},
    )
    monkeypatch.setattr(module, "load_repair_input", lambda *args, **kwargs: assessment)
    record = replace(
        _record("1"),
        status="failed",
        stop_code="delivery_verification.repair_required",
        semantic_status="rejected",
        repair_input_fingerprint="sha256:" + "f" * 64,
    )
    status = SimpleNamespace(plan=checked.plan, verification_node="storage", project_root=str(tmp_path))
    assert module.eligible_local_finding(status, record, cfg)
    if change == "authority":
        status.plan = replace(status.plan, verified_checkpoint_authority={})
    elif change == "owner":
        status.plan = replace(
            status.plan,
            obligations=[
                replace(item, unit_ids=[*item.unit_ids, "consumer"]) if item.id == "deliver-team-invites" else item
                for item in status.plan.obligations
            ],
        )
    elif change == "finding":
        assessment = replace(assessment, findings=[replace(assessment.findings[0], unit_ids=["consumer"])])
    elif change == "parent":
        status.verification_node = "integration"
    elif change == "security":
        record = replace(record, security_status="rejected")
    else:
        record = replace(record, stop_code="delivery_verification.readonly_mutation")
    assert not module.eligible_local_finding(status, record, cfg)


def test_checkpoint_initiated_repair_preserves_completed_consumers_and_allows_preparer_correction(tmp_path):
    from copy import deepcopy
    import json
    from core.delivery_checkpoints import verification_node_status
    from core.delivery_composition import build_composition_context, composition_example
    from core.delivery_verification import build_delivery_verification_snapshot
    from tests.test_delivery_checkpoints import _complete
    from tests import test_delivery_nested_checkpoints as nested

    path, cfg = nested.nested_plan.__wrapped__(tmp_path)
    passed, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert passed.succeeded
    _complete(path, "consumer")
    before = get_delivery_status(path)
    status = verification_node_status(before, "integration")
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    output = json.loads(composition_example(build_composition_context(status, snapshot, cfg)))
    output["checkpoint_results"][0]["outcome"] = "verification_required"
    semantic = _LLM(json.dumps(output), _gap({"deliver-team-invites"}))
    result, _ = _run(path, cfg, semantic, node_id="integration")
    assert result.stop_code == CANDIDATE_REPAIR_REQUIRED, result
    assert len(semantic.calls) == 2
    unused = _LLM()
    changed_reviewer = deepcopy(cfg)
    changed_reviewer["agents"] = {"reviewer": {"llm": {"model": "changed"}}}
    invalid = _repair(path, changed_reviewer, unused, node_id="integration", dry_run=True)
    assert not invalid.ready and not unused.calls
    cfg["agents"] = {"delivery_preparer": {"llm": {"model": "corrected"}}}
    preview = _repair(path, cfg, unused, node_id="integration", dry_run=True)
    assert preview.ready, preview
    root = Path(cfg["project"]["root_path"])
    from core.delivery_progress import write_delivery_progress

    progress_path = delivery_progress_path(root, before.plan.plan_id)
    captured, _ = read_delivery_progress(progress_path, plan_id=before.plan.plan_id)
    write_delivery_progress(
        progress_path,
        replace(
            captured,
            verification=replace(
                captured.checkpoint_verifications["integration"],
                status="blocked",
                security_status="rejected",
                stop_code="delivery_verification.security_rejected",
            ),
        ),
    )
    blocked = _repair(path, cfg, unused, node_id="integration", dry_run=True)
    assert not blocked.ready and not unused.calls
    assert _read_state(root, before.plan.plan_id, "storage") is None
    write_delivery_progress(progress_path, captured)
    author = _LLM(_draft(markdown=(root / before.plan.units[0].task_path).read_text()))
    repaired = _repair(path, cfg, author, node_id="integration")
    assert repaired.ready, repaired
    assert len(author.calls) == 1
    after = get_delivery_status(path)
    units = {unit.id: unit for unit in after.plan.units}
    assert units["consumer"].depends_on == before.plan.units[1].depends_on
    assert _read_state(root, after.plan.plan_id, "storage")["candidate_recovery"]["node_id"] == "integration"
    _complete(path, repaired.unit_id)
    for node, obligations in (
        ("storage", {"deliver-team-invites"}),
        ("integration", {"deliver-team-invites", "visible"}),
    ):
        verified, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example(obligations)), node_id=node)
        assert verified.succeeded, verified
    from core.delivery_checkpoints import checkpoint_barrier_issue

    assert checkpoint_barrier_issue(get_delivery_status(path), cfg, unit_id="shipping") is None


def _finish_with_delivery_run(path, cfg, repair_id):
    from core.state import JsonStateStore
    from sikula_cli.delivery import DeliveryRunNextContext, DeliveryChildRunResult, _run_delivery_plan
    from tests.test_delivery_run import _args
    from tests.test_delivery_repair import _git
    from tests.delivery_fixtures import bind_delivery_child

    root = Path(cfg["project"]["root_path"])
    store = JsonStateStore(root / ".sikula/state")
    calls = []
    reviews = []

    def run_child(args, config):
        calls.append(args.delivery_unit_id)
        assert args.delivery_unit_id == repair_id
        status = get_delivery_status(path)
        commit = status.assembled_commit
        tree = _git(root, "rev-parse", commit + "^{tree}")
        child_commit = _git(root, "commit-tree", tree, "-p", commit, "-m", "execute scoped repair")
        child = store.create(
            Path(args.task_file).read_text().strip(),
            delivery_plan_id=args.delivery_plan_id,
            delivery_unit_id=args.delivery_unit_id,
            delivery_plan_path=args.delivery_plan_path,
            delivery_unit_budget=args.delivery_unit_budget,
        )
        bind_delivery_child(child, root, source_task=status.plan.source_task.to_dict(), write_paths=("agents/",))
        store.save(child)
        args.delivery_child_created_callback(child.task_id)
        child.done = True
        child.result_commit = child_commit
        store.save(child)
        return DeliveryChildRunResult(exit_code=0, child_task_id=child.task_id)

    def verify(args, config):
        node = getattr(args, "verification_node", None) or "root"
        reviews.append(node)
        obligations = {"deliver-team-invites"} if node == "storage" else {"deliver-team-invites", "visible"}
        result, _ = _run(path, config, _LLM(delivery_integration_review_control_example(obligations)), node_id=node)
        return result

    context = DeliveryRunNextContext(
        state_store=store,
        run_task=run_child,
        resolve_state_dir=lambda config: root / ".sikula/state",
        verify_plan=verify,
    )
    args = _args()
    args.plan_file = str(path)
    result = _run_delivery_plan(args, cfg, context, project_root=root)
    assert result.finalized and result.succeeded, result
    assert calls == [repair_id]
    assert reviews == ["storage", "integration", "root"]
