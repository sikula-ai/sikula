from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from core.delivery_candidate_repair import resolve_candidate_repair
from core.delivery_composition import build_composition_context, composition_example, parse_composition
from core.delivery_progress import get_delivery_status
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_review import delivery_integration_review_control_example
from tests import test_delivery_nested_checkpoints as nested
from tests.test_delivery_authority import _prepare
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_conditional_fallback import oversized_parent_fallback as oversized_parent_fallback
from tests.test_delivery_repair import _LLM, _draft, _repair


def _negative(context):
    value = json.loads(composition_example(context))
    value.update(disposition="repair_required", summary="Updates are not visible.")
    value["obligation_results"] = [{"id": item["id"], "outcome": "missing"} for item in context["obligations"]]
    value["findings"] = [
        {
            "code": "integration.gap",
            "summary": "Update does not reach consumers.",
            "unit_ids": [],
            "obligation_ids": [item["id"] for item in context["obligations"]],
        }
    ]
    return json.dumps(value)


def _candidate(root, node):
    source = nested.SOURCE.replace("Use the exact `CACHE_KEY` literal.", "Use the exact `CACHE_KEY` literal. " * 10)
    data = nested._data(source)
    if node == "root":
        data["checkpoints"][0]["obligation_ids"].remove("visible")
        data["source_accounting"][-1].update(checkpoint_ids=["integration"], final_gate=True)
    prepared = _prepare(root, source, data, verification={"final_gate_authority_complete": True})
    with patch.object(nested, "_prepared", return_value=prepared):
        path, cfg = nested.nested_plan.__wrapped__(root)
    result, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert result.succeeded, result
    _complete(path, "consumer")
    if node == "root":
        nested._compose(path, cfg, "integration")
        _complete(path, "shipping")
    return path, cfg


def _context(path, cfg, node):
    status = replace(get_delivery_status(path), verification_node=node)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    return build_composition_context(status, snapshot, cfg)


@pytest.mark.parametrize("node", ["integration", "root"])
def test_parent_composition_gap_publishes_bounded_repair_without_full_review(tmp_path, node, oversized_parent_fallback):
    from core.delivery_repair import _read_state
    from core.delivery_finalize import finalize_delivery_plan
    from tests.test_delivery_candidate_repair import _verify_repaired_node
    from tests.test_delivery_repair_authority import AuthorityLLM
    from agents.delivery_repair_authority_agent import DeliveryRepairAuthorityAgent

    path, cfg = _candidate(tmp_path, node)
    context = _context(path, cfg, node)
    responses = [_negative(context)]
    if node == "integration":
        uncertain = json.loads(responses[0])
        uncertain["checkpoint_results"][0]["outcome"] = "verification_required"
        responses = [
            json.dumps(uncertain),
            delivery_integration_review_control_example({"deliver-team-invites"}),
            *responses,
        ]
    semantic = _LLM(*responses)
    result, _ = _run(path, cfg, semantic, node_id=node)
    assert result.stop_code == "delivery_verification.repair_required", result
    assert len(semantic.calls) == (3 if node == "integration" else 1)
    before = get_delivery_status(path)
    selected, binding = resolve_candidate_repair(
        replace(
            before,
            verification_node=node,
            verification=(before.verification if node == "root" else before.checkpoint_verifications[node]),
        ),
        cfg,
    )
    assert selected.verification_node == node
    if node == "root":
        from core.delivery_verification import with_delivery_verification_readiness

        cfg["agents"] = {"delivery_preparer": {"llm": {"model": "corrected-preparer"}}}
        current = with_delivery_verification_readiness(before, cfg)
        assert current.valid and current.verification_status == "failed"
    unused = _LLM()
    repeated, _ = _run(path, cfg, unused, node_id=node)
    assert repeated.stop_code == result.stop_code and not unused.calls
    assert _repair(path, cfg, unused, dry_run=True, node_id=node).ready
    assert _read_state(tmp_path, before.plan.plan_id, node) is None
    from core.delivery_composition import composition_evidence_path
    from core.delivery_progress import delivery_progress_path

    evidence = composition_evidence_path(
        delivery_progress_path(tmp_path, before.plan.plan_id).parent,
        selected.verification.composition_evidence_fingerprint,
    )
    original = evidence.read_bytes()
    evidence.write_text("{}")
    missing = _repair(path, cfg, unused, dry_run=True, node_id=node)
    assert not missing.ready and not unused.calls
    assert _read_state(tmp_path, before.plan.plan_id, node) is None
    evidence.write_bytes(original)
    llm = _LLM(_draft(markdown=(tmp_path / before.plan.units[0].task_path).read_text()))
    authority = AuthorityLLM()
    if node == "root":
        from core import delivery_repair

        publish = delivery_repair._atomic_replace_if_unchanged

        def interrupt(*args, **kwargs):
            publish(*args, **kwargs)
            raise KeyboardInterrupt()

        with patch.object(delivery_repair, "_atomic_replace_if_unchanged", side_effect=interrupt):
            with pytest.raises(KeyboardInterrupt):
                _repair(
                    path,
                    cfg,
                    llm,
                    node_id=node,
                    authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(authority),
                )
    repaired = _repair(
        path, cfg, llm, node_id=node, authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(authority)
    )
    assert repaired.ready, repaired
    assert len(llm.calls) == 1
    state = _read_state(tmp_path, before.plan.plan_id, node)
    assert state["candidate_recovery"] == binding
    assert state["authority_renewal"]["status"] == "accepted"
    assert [packet["node_id"] for packet in authority.packets] == (["root"] if node == "root" else [node, "root"])
    packet, _ = json.JSONDecoder().raw_decode(llm.calls[0][llm.calls[0].index('{"') :])
    assert "foundation" not in packet["repair_unit"]["depends_on"]
    assert "foundation" not in packet["unit_contracts"]
    assert packet["plan"]["repair_evidence"]["children"]
    assert state["unit"]["depends_on"]  # Execution retains the actual dependency graph.
    after = get_delivery_status(path)
    assert after.plan.verified_final_gate_authority
    assert after.plan.verified_composition_authority
    assert next(cp for cp in after.plan.checkpoints if cp.id == "storage").unit_ids == ("foundation",)
    _complete(path, repaired.unit_id)
    for current in ("integration", "root"):
        if current == "integration" and node == "root":
            continue
        verified = _verify_repaired_node(path, cfg, current, {"deliver-team-invites", "visible"})
        assert verified.succeeded, verified
        if current == "integration":
            _complete(path, "shipping")
    assert finalize_delivery_plan(path, project_config=cfg).finalized
    exhausted = _repair(path, cfg, unused, node_id=node, dry_run=True)
    assert exhausted.issue.code == "delivery_repair.budget_exhausted"


def _compact_context():
    return {
        "units": [{"id": "consumer"}],
        "obligations": [{"id": "visible"}],
        "checkpoint_composition": {
            "compact": True,
            "review_kind": "semantic",
            "children": [
                {
                    "id": key,
                    "evidence_fingerprint": "sha256:" + "a" * 64,
                    "coverage_fingerprint": "sha256:" + "b" * 64,
                    "obligation_count": 100,
                    "exact_tree": True,
                }
                for key in ("storage", "api")
            ],
        },
    }


@pytest.mark.parametrize("change", ["none", "uncertain_child", "unowned", "security", "external", "satisfied"])
def test_compact_parent_repair_requires_owned_gap_and_accepted_children(change):
    context = _compact_context()
    control = json.loads(_negative(context))
    if change == "uncertain_child":
        control["checkpoint_results"][0]["outcome"] = "verification_required"
    elif change == "unowned":
        control["findings"].append({"code": "other", "summary": "Unknown scope.", "unit_ids": [], "obligation_ids": []})
    elif change == "security":
        context["checkpoint_composition"]["review_kind"] = "security"
        control["obligation_results"] = []
    elif change == "external":
        control["disposition"] = "external_dependency_gap"
    elif change == "satisfied":
        control["obligation_results"][0]["outcome"] = "satisfied"
    result = parse_composition(json.dumps(control), context, {"consumer"})
    if change in {"uncertain_child", "unowned", "satisfied"}:
        assert result.fallback
    else:
        assert not result.fallback
    assert not result.assessment.approved
    assert len(result.child_refs) == 2


def test_partial_parent_assessment_requires_matching_composition_authority(tmp_path):
    from core.delivery_repair_input import _parse_assessment
    from tests.test_delivery_verification import _record

    path, cfg, checked, *_ = nested._prepared(tmp_path)
    payload = {"assessment": json.loads(_negative(_compact_context())), "composition_fingerprint": "sha256:" + "a" * 64}
    payload["assessment"].pop("checkpoint_results")
    record = _record(composition_evidence_fingerprint="sha256:" + "a" * 64)
    parsed = _parse_assessment(payload, checked.plan, "integration", record=record)
    assert [item.id for item in parsed.obligation_results] == ["visible"]
    with pytest.raises(ValueError, match="composition authority"):
        _parse_assessment(
            payload, checked.plan, "integration", record=replace(record, composition_evidence_fingerprint=None)
        )
    with pytest.raises(ValueError):
        _parse_assessment({"assessment": payload["assessment"]}, checked.plan, "integration", record=record)


def test_parent_authoring_and_renewal_prompts_do_not_expand_descendant_contracts(tmp_path):
    from copy import deepcopy
    from types import SimpleNamespace
    import yaml

    from agents.delivery_repair_agent import build_delivery_repair_prompt
    from agents.delivery_repair_authority_agent import build_authority_renewal_prompt
    from core.delivery_progress import DeliveryStatusResult, DeliveryStatusUnit
    from core.delivery_repair import _repair_packet, _appended_plan
    from core.delivery_repair_authority import renewal_nodes, build_renewal
    from tests.test_delivery_verification import _record

    sizes = []
    for count in (1, 24):
        root = tmp_path / str(count)
        data = nested._data()
        data["units"][0]["task_markdown"] += "\nCHILD_PRIVATE_DETAIL " + "child detail " * 100
        for i in range(count - 1):
            unit = deepcopy(data["units"][0])
            unit["id"] = f"extra-child-{i}"
            data["units"].append(unit)
            data["obligations"][0]["unit_ids"].append(unit["id"])
            for node in data["checkpoints"]:
                node["unit_ids"].append(unit["id"])
        path, cfg, checked, *_ = _prepare(
            root, nested.SOURCE, data, verification={"final_gate_authority_complete": True}
        )
        plan = checked.plan
        if count == 24:
            # Candidate checkouts may use CRLF while preview clones use LF.
            for item in plan.units:
                contract = root / item.task_path
                contract.write_bytes(contract.read_text().replace("\n", "\r\n").encode("utf-8"))
        retained = json.loads(plan.verified_composition_authority["integration"])
        retained["repair_evidence"] = {
            "composition_fingerprint": "sha256:" + "a" * 64,
            "children": _compact_context()["checkpoint_composition"]["children"][:1],
        }
        status = DeliveryStatusResult(
            str(path),
            str(root),
            None,
            True,
            "done",
            [],
            [],
            plan=plan,
            verification_node="integration",
            verification=_record(),
            units=[DeliveryStatusUnit(u.id, "done", u.title, u.task_path, u.depends_on) for u in plan.units],
        )
        assessment = parse_composition(_negative(_compact_context()), _compact_context(), {"consumer"}).assessment
        with (
            patch("core.delivery_candidate_repair.parent_repair_authority", return_value=retained),
            patch("core.delivery_repair.preview_delivery_artifacts", return_value=SimpleNamespace(success=True)),
        ):
            packet, unit, contracts = _repair_packet(
                status,
                root,
                root,
                cfg,
                assessment,
                "integration-repair-aaaaaaaaaaaaaaaa",
                None,
                candidate_recovery=True,
            )
        author_prompt = build_delivery_repair_prompt(packet)
        assert "CHILD_PRIVATE_DETAIL" not in author_prompt
        assert "extra-child-" not in author_prompt
        assert len(unit["depends_on"]) == count + 1
        proposed = _appended_plan(
            yaml.safe_load(path.read_text()),
            unit,
            ["visible"],
            root,
            cfg,
            str(path),
            node_id="integration",
            completed_unit_ids=frozenset(u.id for u in plan.units),
        )
        state = {
            "source_plan": path.read_text(),
            "contracts": contracts,
            "task_markdown": contracts[plan.units[1].task_path],
            "unit_id": unit["id"],
            "attempts": 1,
            "verification": {"candidate_commit": "a" * 40},
            "node_id": "integration",
            "config_fingerprint": "sha256:" + "c" * 64,
            "authority_renewal": {"nodes": renewal_nodes(plan, "integration")},
        }
        packets, _ = build_renewal(state, proposed, root, root, cfg, str(path))
        prompts = [author_prompt, *[build_authority_renewal_prompt(p, cfg, root) for p in packets]]
        assert all("CHILD_PRIVATE_DETAIL" not in p and "extra-child-" not in p for p in prompts)
        sizes.append([len(p.encode("utf-8")) for p in prompts])
    assert all(abs(a - b) < 128 for a, b in zip(*sizes)), sizes


def test_security_requested_parent_gap_routes_to_its_own_repair_budget(tmp_path, oversized_parent_fallback):
    from core.delivery_candidate_repair import CANDIDATE_REPAIR_REQUIRED
    from core.delivery_repair import _read_state
    from tests.test_delivery_repair import _git

    path, cfg = _candidate(tmp_path, "integration")
    nested._compose(path, cfg, "integration")
    _complete(path, "shipping")
    _git(tmp_path, "config", "core.autocrlf", "true")
    final = _context(path, cfg, "root")
    parent = _context(path, cfg, "integration")
    status = get_delivery_status(path)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    security_context = build_composition_context(status, snapshot, cfg, review_kind="security")
    uncertain = json.loads(composition_example(security_context))
    uncertain["checkpoint_results"][0]["outcome"] = "verification_required"
    semantic = _LLM(composition_example(final), _negative(parent))
    security = _LLM(json.dumps(uncertain))
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == CANDIDATE_REPAIR_REQUIRED, result
    assert len(semantic.calls) == 2 and len(security.calls) == 1
    selected, binding = resolve_candidate_repair(get_delivery_status(path), cfg)
    assert selected.verification_node == "integration" and binding["node_id"] == "root"
    assert get_delivery_status(path).checkpoint_verifications == status.checkpoint_verifications
    unused = _LLM()
    preview = _repair(path, cfg, unused, dry_run=True)
    assert preview.ready and not unused.calls, preview
    assert _read_state(tmp_path, status.plan.plan_id, "integration") is None
    # A preparer-only correction does not discard the accepted review authority.
    cfg["agents"] = {"delivery_preparer": {"llm": {"model": "corrected-preparer"}}}
    author = _LLM(_draft(markdown=(tmp_path / status.plan.units[1].task_path).read_text()))
    repaired = _repair(path, cfg, author)
    assert repaired.ready, repaired
    assert len(author.calls) == 1
    after = get_delivery_status(path)
    assert after.plan.verified_final_gate_authority and after.plan.verified_composition_authority
    assert {u.id: u.depends_on for u in after.plan.units if u.id != repaired.unit_id} == {
        u.id: u.depends_on for u in status.plan.units
    }
    assert _read_state(tmp_path, status.plan.plan_id, "root") is None
    control = _read_state(tmp_path, status.plan.plan_id, "integration")
    assert control["candidate_recovery"] == binding and control["attempts"] == 1
