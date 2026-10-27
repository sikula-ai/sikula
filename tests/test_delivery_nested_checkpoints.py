from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.delivery_checkpoint_model import parse_delivery_checkpoints, DeliveryCheckpointError
from core.delivery_checkpoints import due_delivery_checkpoint, verification_node_status
from core.delivery_composition import build_composition_context, composition_example
from core.delivery_checkpoint_evidence import checkpoint_evidence_path, load_checkpoint_evidence
from core.delivery_progress import (
    DeliveryProgress,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_review import delivery_integration_review_control_example
from tests.test_delivery_authority import _draft_data, _prepare, SOURCE as BASE_SOURCE
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git


SOURCE = BASE_SOURCE.replace("Use the exact `CACHE_KEY` literal.", "Use the exact `CACHE_KEY` literal. " * 100)


def _data(source=SOURCE):
    data = _draft_data(source)
    last = deepcopy(data["units"][-1])
    last.update(id="shipping", title="Ship integration", depends_on=["consumer"])
    data["units"].append(last)
    data["checkpoints"][0].update(parent_id="integration", integration_context="Preserve the storage API.")
    data["obligations"].append(
        dict(
            id="visible",
            summary="Consumers observe updates.",
            source_fragment_ids=[data["source_accounting"][-1]["source_fragment_id"]],
            unit_ids=["consumer"],
            disposition="preserved",
        )
    )
    data["source_accounting"][-1].update(disposition="mapped", obligation_ids=["visible"])
    data["checkpoints"].insert(
        0,
        dict(
            id="integration",
            unit_ids=["foundation", "consumer"],
            obligation_ids=["deliver-team-invites", "visible"],
            integration_context="Storage updates remain visible through the consumer API.",
        ),
    )
    for record in data["source_accounting"]:
        record["checkpoint_ids"] = ["integration", *record["checkpoint_ids"]]
        record["final_gate"] = False
    return data


def _prepared(root):
    return _prepare(root, SOURCE, _data(), verification={"final_gate_authority_complete": True})


def test_preparation_reviews_authority_at_every_level(tmp_path):
    path, cfg, checked, _, llm, _ = _prepared(tmp_path)
    assert checked.plan.verified_composition_authority.keys() == {"integration"}
    assert checked.plan.verified_final_gate_authority
    assert "EVERY composing node" in llm.prompts[1]
    parent = json.loads(checked.plan.verified_composition_authority["integration"])
    assert [child["id"] for child in parent["final_authority"]["children"]] == ["storage"]
    assert [item["id"] for item in parent["obligations"]] == ["visible"]
    final = json.loads(checked.plan.verified_final_gate_authority)
    assert [child["id"] for child in final["final_authority"]["children"]] == ["integration"]
    assert not final["obligations"]
    assert "composition_authority" not in checked.plan.to_dict()


def test_authoring_rejects_context_attribution_missing_an_ancestor(tmp_path):
    from core.delivery_authoring import DeliveryAuthoringParseError, parse_delivery_authoring_output

    data = _data()
    data["source_accounting"][1]["checkpoint_ids"] = ["storage"]
    with pytest.raises(DeliveryAuthoringParseError) as error:
        parse_delivery_authoring_output(
            json.dumps(data),
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
            expected_plan_id="team-invites",
            source_task_description=SOURCE,
        )
    assert error.value.code == "source_accounting.checkpoint_incomplete"


def test_missing_child_attribution_uses_full_node_review_without_preflight_crash(tmp_path):
    import yaml
    from core.delivery_composition_scope import delivery_composition_scope
    from core.delivery_plan import check_delivery_plan_file
    from core.delivery_verification import check_delivery_verification_readiness
    from core.delivery_verification_scope import DeliveryVerificationScope

    source = SOURCE + "\n## Release\nRelease integration.\n"
    data = _data(source)
    data["source_accounting"][2].update(checkpoint_ids=[], final_gate=True)
    path, cfg, checked, *_ = _prepare(tmp_path, source, data, verification={"final_gate_authority_complete": True})
    parent = DeliveryVerificationScope.from_plan(checked.plan, "integration")
    assert delivery_composition_scope(checked.plan, parent) is not None
    raw = yaml.safe_load(path.read_text())
    del raw["checkpoint_authority"]["storage"]
    path.write_text(yaml.safe_dump(raw))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert checked.valid, checked.errors
    parent = DeliveryVerificationScope.from_plan(checked.plan, "integration")
    child = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    assert set(child.source_fragment_ids) - set(parent.source_fragment_ids)
    assert not checked.plan.verified_composition_authority
    assert delivery_composition_scope(checked.plan, parent) is None
    readiness = check_delivery_verification_readiness(checked, cfg)
    assert readiness.ready, readiness.errors


@pytest.fixture
def nested_plan(tmp_path):
    path, cfg, _, _, _, _ = _prepared(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n")
    (tmp_path / "agents/code.py").write_text("value = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "nested authority")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "team-invites"),
        DeliveryProgress(
            schema_version=1,
            plan_id="team-invites",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress("foundation", "done", commit=commit)],
        ),
    )
    return path, cfg


def _compose(path, cfg, node, interrupt=False):
    status = verification_node_status(get_delivery_status(path), node)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    contexts = [build_composition_context(status, snapshot, cfg, review_kind=role) for role in ("semantic", "security")]
    assert all(contexts)
    semantic = _LLM(composition_example(contexts[0]))
    security = _LLM(composition_example(contexts[1]))
    if interrupt:
        with patch("core.delivery_verify.store_checkpoint_evidence", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                _run(path, cfg, semantic, security, node_id=node)
        semantic, security = _LLM(), _LLM()
    result, _ = _run(path, cfg, semantic, security, node_id=node)
    assert result.succeeded, result
    if interrupt:
        assert not semantic.calls and not security.calls
    return contexts


@pytest.mark.parametrize("interrupt", [False, True])
def test_nested_admission_composition_resume_and_finalization(nested_plan, interrupt):
    path, cfg = nested_plan
    assert due_delivery_checkpoint(get_delivery_status(path), cfg) == "storage"
    blocked, _ = _run(path, cfg, _LLM(), node_id="integration")
    assert not blocked.succeeded
    result, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert result.succeeded, result
    _complete(path, "consumer")
    assert due_delivery_checkpoint(get_delivery_status(path), cfg) == "integration"
    contexts = _compose(path, cfg, "integration", interrupt)
    assert [c["id"] for c in contexts[0]["checkpoint_composition"]["children"]] == ["storage"]
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["integration"]
    directory = delivery_progress_path(path.parents[3], status.plan.plan_id).parent
    root = path.parents[3]
    payload = json.loads(checkpoint_evidence_path(directory, record).read_text())
    assert [item["id"] for item in payload["obligation_results"]] == ["visible"]
    assert [item["node_id"] for item in payload["child_evidence"]] == ["storage"]
    evidence = load_checkpoint_evidence(root, directory, record, plan_id=status.plan.plan_id, node_id="integration")
    assert {item.id for item in evidence.closure.iter_obligation_results()} == {"deliver-team-invites", "visible"}
    leaf_path = checkpoint_evidence_path(directory, status.checkpoint_verifications["storage"])
    leaf_bytes = leaf_path.read_bytes()
    leaf_path.write_text("{}")
    with pytest.raises(ValueError, match="evidence"):
        load_checkpoint_evidence(root, directory, record, plan_id=status.plan.plan_id, node_id="integration")
    leaf_path.write_bytes(leaf_bytes)
    _complete(path, "shipping")
    final = _compose(path, cfg, "root")
    assert [c["id"] for c in final[0]["checkpoint_composition"]["children"]] == ["integration"]
    assert "deliver-team-invites" not in composition_example(final[0])
    from core.delivery_finalize import finalize_delivery_plan

    result = finalize_delivery_plan(path, project_config=cfg)
    assert result.finalized, result


@pytest.mark.parametrize("change", ["cycle", "unknown", "outside", "siblings"])
def test_invalid_hierarchy_stops_before_execution(change):
    data = _data()
    if change == "cycle":
        data["checkpoints"][0]["parent_id"] = "storage"
    elif change == "unknown":
        data["checkpoints"][1]["parent_id"] = "absent"
    elif change == "outside":
        data["checkpoints"][1]["unit_ids"].append("shipping")
    else:
        sibling = deepcopy(data["checkpoints"][1])
        sibling["id"] = "duplicate"
        data["checkpoints"].append(sibling)
    with pytest.raises(DeliveryCheckpointError):
        parse_delivery_checkpoints(
            data["checkpoints"],
            [SimpleNamespace(**u) for u in data["units"]],
            [SimpleNamespace(**o) for o in data["obligations"]],
        )


def test_shared_prerequisites_are_evidence_not_sibling_ownership(tmp_path):
    from dataclasses import replace
    from core.delivery_plan import DeliveryPlan, DeliveryPlanUnit
    from core.delivery_obligations import DeliveryObligation
    from core.delivery_checkpoint_model import checkpoint_input_units, checkpoint_postorder
    from core.delivery_checkpoints import checkpoint_policy_payload
    from core.delivery_progress import DeliveryStatusUnit
    from core.delivery_verification_scope import DeliveryVerificationScope

    units = [
        DeliveryPlanUnit(key, None, key + ".md", depends_on=deps)
        for key, deps in [
            ("base", []),
            ("left", ["base"]),
            ("right", ["base"]),
            ("join", ["left", "right"]),
            ("ship", ["join"]),
        ]
    ]
    outcomes = [DeliveryObligation(key, key, ["fragment"], [key]) for key in ("base", "left", "right", "join")]
    nodes = [
        dict(
            id="integration",
            unit_ids=["base", "left", "right", "join"],
            obligation_ids=["base", "left", "right", "join"],
        )
    ]
    nodes += [dict(id=key, unit_ids=[key], obligation_ids=[key], parent_id="integration") for key in ("left", "right")]
    parsed = parse_delivery_checkpoints(nodes, units, outcomes)
    assert [node.id for node in checkpoint_postorder(parsed)] == ["left", "right", "integration"]
    assert checkpoint_input_units(parsed[1].unit_ids, units) == {"base", "left"}
    assert checkpoint_input_units(parsed[2].unit_ids, units) == {"base", "right"}
    assert set(parsed[1].unit_ids).isdisjoint(parsed[2].unit_ids)
    plan = DeliveryPlan(
        3, "shared", "Shared prerequisites", "delivery/shared", units, [], obligations=outcomes, checkpoints=parsed
    )
    for unit in units:
        (tmp_path / unit.task_path).write_text("Exact contract for " + unit.id)
    progress = [
        DeliveryStatusUnit(unit.id, "done", None, unit.task_path, unit.depends_on, commit="a" * 40) for unit in units
    ]
    status = SimpleNamespace(project_root=str(tmp_path), plan=plan, units=progress)
    scopes = [DeliveryVerificationScope.from_plan(plan, key) for key in ("left", "right")]
    original = [checkpoint_policy_payload(status, scope) for scope in scopes]
    # Both consumers bind the shared prerequisite, without claiming its outcome.
    assert all(item["scope"]["prerequisites"][0]["id"] == "base" for item in original)
    assert all("base" not in {u["id"] for u in item["scope"]["units"]} for item in original)
    progress[0] = replace(progress[0], commit="b" * 40)
    assert all(checkpoint_policy_payload(status, scope) != prior for scope, prior in zip(scopes, original))
    progress[0] = replace(progress[0], commit="a" * 40)
    (tmp_path / "ship.md").write_text("Unrelated later contract")
    assert [checkpoint_policy_payload(status, scope) for scope in scopes] == original
    (tmp_path / "base.md").write_text("Changed prerequisite contract")
    assert all(checkpoint_policy_payload(status, scope) != prior for scope, prior in zip(scopes, original))


@pytest.mark.parametrize("damage", ["receipt", "contract", "parent"])
def test_changed_intermediate_authority_cannot_reuse_compact_receipt(tmp_path, damage):
    import yaml
    from core.delivery_plan import check_delivery_plan_file

    path, _, checked, _, _, _ = _prepared(tmp_path)
    data = yaml.safe_load(path.read_text())
    if damage == "receipt":
        data["composition_authority"]["integration"] = "sha256:" + "0" * 64
    elif damage == "parent":
        data["checkpoints"][1]["integration_context"] += " Altered interface."
    else:
        contract = tmp_path / checked.plan.units[0].task_path
        contract.write_text(contract.read_text() + "\nChanged storage contract.\n")
    path.write_text(yaml.safe_dump(data))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert checked.valid, checked.errors
    assert not checked.plan.verified_composition_authority


def test_checkpoint_projection_names_parent_without_private_interface(tmp_path):
    from core.delivery_checkpoints import checkpoint_projection
    from core.delivery_progress import render_delivery_checkpoint_status

    path, _, _, _, _, _ = _prepared(tmp_path)
    projected = checkpoint_projection(get_delivery_status(path, project_root=tmp_path))
    child = next(item for item in projected if item["id"] == "storage")
    assert child["parent_id"] == "integration"
    assert "parent: integration" in render_delivery_checkpoint_status(child)
    assert "integration_context" not in json.dumps(projected)


def test_intermediate_readonly_boundary_survives_failed_audit(nested_plan):
    from core.llm_client import LLMReadOnlyViolation
    import core.delivery_verify as verify

    path, cfg = nested_plan
    result, _ = _run(path, cfg, _LLM(delivery_integration_review_control_example({"deliver-team-invites"})))
    assert result.succeeded
    _complete(path, "consumer")
    status = verification_node_status(get_delivery_status(path), "integration")
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    context = build_composition_context(status, snapshot, cfg)
    original = verify._safe_append_audit

    def audit(path, value, **kwargs):
        if value.get("event") == "review_failed":
            return False
        return original(path, value, **kwargs)

    with patch("core.delivery_verify._safe_append_audit", side_effect=audit):
        result, _ = _run(
            path, cfg, _LLM(composition_example(context)), _LLM(LLMReadOnlyViolation("readonly")), node_id="integration"
        )
    assert not result.succeeded
    record = get_delivery_status(path).checkpoint_verifications["integration"]
    assert record.stop_code == "delivery_verification.readonly_mutation"
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="integration")
    assert not again.succeeded and not unused.calls


def test_nested_repair_extends_ancestors_without_duplicate_sibling_ownership(tmp_path):
    import yaml
    from core.delivery_repair import _appended_plan
    from core.delivery_plan import check_delivery_plan_data

    path, cfg, _, _, _, _ = _prepared(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["checkpoints"].append(
        dict(
            id="consumer-check",
            parent_id="integration",
            unit_ids=["consumer"],
            obligation_ids=["visible"],
            integration_context="Consumer behavior.",
        )
    )
    data["source_accounting"][-1]["checkpoint_ids"].append("consumer-check")
    unit = dict(
        id="integration-repair-aaaaaaaaaaaaaaaa",
        task_path=".sikula/delivery/team-invites/units/integration-repair-aaaaaaaaaaaaaaaa.md",
        depends_on=["foundation"],
        scope_paths=["agents"],
        repair_node="storage",
    )
    result = yaml.safe_load(
        _appended_plan(data, unit, ["deliver-team-invites"], tmp_path, cfg, str(path), node_id="storage")
    )
    nodes = {item["id"]: item for item in result["checkpoints"]}
    assert "integration-repair-aaaaaaaaaaaaaaaa" in nodes["storage"]["unit_ids"]
    assert "integration-repair-aaaaaaaaaaaaaaaa" in nodes["integration"]["unit_ids"]
    assert nodes["consumer-check"]["unit_ids"] == ["consumer"]
    consumer = next(item for item in result["units"] if item["id"] == "consumer")
    assert "integration-repair-aaaaaaaaaaaaaaaa" in consumer["depends_on"]
    checked = check_delivery_plan_data(
        result, project_root=tmp_path, plan_path=path, virtual_task_paths={unit["task_path"]}
    )
    assert checked.valid, checked.errors
