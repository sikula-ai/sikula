"""Cheap checks for the opt-in capacity experiment; no external providers."""

import json
from pathlib import Path

import pytest

from tests.delivery_capacity import CapacityLLM, create_fixture, nodes, run_case


@pytest.mark.parametrize("depth,fanout,count", [(0, 2, 1), (1, 2, 3), (2, 2, 7), (3, 4, 85), (5, 4, 1365)])
def test_capacity_shapes_have_unique_postordered_nodes(depth, fanout, count):
    result = nodes(depth, fanout)
    assert len(result) == len(set(result)) == count
    assert result[-1] == "root"
    positions = {node: index for index, node in enumerate(result)}
    for node in result[:-1]:
        parent = node.rsplit(".", 1)[0] if "." in node else "root"
        assert positions[node] < positions[parent]


def test_capacity_fixture_refuses_existing_directory(tmp_path):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_fixture(tmp_path)
    assert sentinel.read_text() == "keep"


def test_synthetic_implementation_refuses_writes_outside_fixture(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    fake = CapacityLLM(fixture, 1, 2, "happy", False)
    with pytest.raises(ValueError, match="inside its fixture"):
        fake.run_agent("", tmp_path)
    assert not (tmp_path / "src").exists()
    assert not fake.implemented


def test_preparation_measurement_is_not_reported_as_completed_delivery(tmp_path):
    root = tmp_path / "fixture"
    report = run_case(root, depth=1, prepare_only=True)
    assert report["prepared"] and not report["completed"]
    assert report["units"] == 3 and report["checkpoints"] == 2
    assert report["implementations"] == 0
    assert report["operations"][-1]["phase"] == "prepare_replay"
    assert report["operations"][-1]["calls"] == 0
    counts = report["host_work_totals"]
    assert counts["git_subprocess_run_calls"] > 0
    assert counts["checkpoint_handoff_checks"] == 0
    assert counts == {key: sum(operation["host_work"][key] for operation in report["operations"]) for key in counts}
    assert json.loads((root / ".capacity/report.json").read_text()) == report


def test_capacity_experiment_preserves_prerequisite_stop(tmp_path):
    report = run_case(tmp_path / "fixture", depth=1, scenario="prerequisite")
    assert report["expected_terminal_stop"] and not report["completed"]
    assert report["implementations"] == 0
    assert report["operations"][-1]["calls"] == 0


def test_capacity_limit_is_reported_without_execution_or_raised_guards(tmp_path):
    from core.delivery_preparation_tree import MAX_PREPARATION_NODES

    report = run_case(tmp_path / "fixture", depth=3, fanout=4, prepare_only=True)
    assert report["requested_units"] == 85
    assert not report["completed"] and not report["checks_passed"]
    assert report["implementations"] == 0 and report["stop_phase"] == "prepare"
    assert "delivery_prepare.hierarchy_required" in report["operations"][0]["error_codes"]
    assert len({call["node"] for call in report["calls"] if call["stage"] == "author"}) == MAX_PREPARATION_NODES


def test_operator_interrupt_is_not_treated_as_injected_resume(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from core import delivery_checkpoints
    from tests import delivery_capacity

    original_run = subprocess.run
    original_check = delivery_checkpoints._checkpoint_local_pass_is_usable

    def cancel():
        # Count attempted work even when Git fails and the CLI is interrupted.
        # The instrumentation must preserve both exceptions and return values.
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.run(["git", "rev-parse", "--verify", "refs/heads/nonexistent"], check=True, capture_output=True)
        assert not delivery_checkpoints._checkpoint_local_pass_is_usable(
            SimpleNamespace(valid=False, checkpoint_verifications={}), SimpleNamespace(id="absent"), {}, resolver=None
        )
        raise KeyboardInterrupt()

    monkeypatch.setattr(delivery_capacity, "sikula_main", cancel)
    root = tmp_path / "fixture"
    with pytest.raises(KeyboardInterrupt):
        run_case(root, depth=1)
    report = json.loads((root / ".capacity/report.json").read_text())
    assert not report["completed"] and not report["checks_passed"]
    assert not report["calls"] and not report["operations"]
    assert report["host_work_totals"] == {"git_subprocess_run_calls": 1, "checkpoint_handoff_checks": 1}
    assert subprocess.run is original_run
    assert delivery_checkpoints._checkpoint_local_pass_is_usable is original_check


@pytest.mark.parametrize("fanout", [1, 2])
def test_local_repair_fixture_prepares_compact_routes_without_changing_limits(tmp_path: Path, fanout: int) -> None:
    from core.delivery_plan import check_delivery_plan_file
    from core.delivery_composition_scope import delivery_composition_scope
    from core.delivery_review_route import preview_composition_context, review_route
    from core.delivery_verification_scope import DeliveryVerificationScope
    from sikula_cli.config import load_config
    from tests.delivery_capacity import LOCAL_OUTCOMES, SOURCE, children

    root = tmp_path / "fixture"
    report = run_case(root, depth=2, fanout=fanout, scenario="local-repair", prepare_only=True)
    assert report["checks_passed"] and not report["completed"]
    assert not report.get("local_repair_lifecycle_verified")
    cfg = load_config(root / ".sikula/config.yaml")
    cfg["project"]["root_path"] = str(root)
    checked = check_delivery_plan_file(root / ".sikula/delivery/capacity/plan.yaml", project_root=root)
    assert checked.valid, checked.errors
    assert len(checked.plan.obligations) == len(nodes(2, fanout)) * len(LOCAL_OUTCOMES)
    for node in nodes(2, fanout):
        scope = DeliveryVerificationScope.from_plan(checked.plan, node)
        composition = delivery_composition_scope(checked.plan, scope)
        for role in ("semantic", "security"):
            route = review_route(scope, cfg, root, SOURCE, role, context=preview_composition_context(composition, role))
            assert route.ready and route.full_available
            assert route.composition_available == bool(children(node, 2, fanout))


def test_local_repair_oracle_rejects_unmeasured_parent_review(tmp_path: Path) -> None:
    fake = CapacityLLM(tmp_path, 2, 1, "local-repair", False)
    prompt = 'Delivery plan context:\n{"units": [], "verification_node": {"id": "g0"}}'
    prompt += "\n\nDeterministic validation summary:\n{}"
    with pytest.raises(AssertionError, match="matching runtime route"):
        fake.run_readonly_agent(prompt, tmp_path)


@pytest.fixture
def parent_route(tmp_path):
    """Use real prepared authority and rendering; no production sizing overrides."""
    from types import SimpleNamespace
    from core.delivery_composition_scope import delivery_composition_scope
    from core.delivery_plan import check_delivery_plan_file
    from core.delivery_verification_scope import DeliveryVerificationScope
    from core.delivery_verification_validation import DeliveryVerificationValidationResult
    from sikula_cli.config import load_config

    root = tmp_path / "fixture"
    assert run_case(root, depth=2, fanout=1, scenario="local-repair", prepare_only=True)["checks_passed"]
    cfg = load_config(root / ".sikula/config.yaml")
    cfg["project"]["root_path"] = str(root)
    plan = check_delivery_plan_file(root / ".sikula/delivery/capacity/plan.yaml").plan
    scope = DeliveryVerificationScope.from_plan(plan, "g0")
    snapshot = SimpleNamespace(
        scope=scope,
        identity=SimpleNamespace(candidate_commit="a" * 40, candidate_tree="b" * 40),
    )
    return (
        root,
        cfg,
        snapshot,
        delivery_composition_scope(plan, scope),
        DeliveryVerificationValidationResult(True, False, True),
    )


def _measure_parent_route(parent_route, role, *, crossover=False, oversized=False):
    from dataclasses import replace
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
    from core.delivery_review_route import preview_composition_context, review_route
    from core import delivery_verify
    from tests.delivery_capacity import observe_review_routes, SOURCE

    root, cfg, snapshot, composition, validation = parent_route
    fake = CapacityLLM(root, 2, 1, "local-repair", False)
    reviewer = DeliveryIntegrationReviewAgent(fake, cfg)
    context = preview_composition_context(composition, role)
    summary = validation.to_review_dict(cfg, project_root=root, include_final_checks=False)
    if oversized:
        full = snapshot.scope.plan_context()
        full["units"][0]["description"] = "Complete descendant authority.\n" * 20000
        snapshot.scope = replace(snapshot.scope, _context_json=json.dumps(full))
    if crossover:
        child = context["checkpoint_composition"]["children"][0]
        child["exact_tree"] = False
        baseline = review_route(snapshot.scope, cfg, root, SOURCE, role, context=context, validation=summary)
        assert baseline.composition_available
        # A larger real textual delta can make small full review cheaper.
        child["delta"] = "x" * (baseline.full_bytes - baseline.composition_bytes + 113)
    args = context, snapshot, reviewer, root, SOURCE, validation, cfg, role
    expected = delivery_verify._composition_packet_fits(*args)
    with observe_review_routes(fake):
        assert delivery_verify._composition_packet_fits(*args) == expected

    def render(*, compact, commit=None):
        return reviewer._prompt(
            cwd=root,
            review_kind=role,
            source_task=SOURCE,
            plan_context=context if compact else snapshot.scope.plan_context(),
            validation_summary=summary,
            candidate_commit=commit or snapshot.identity.candidate_commit,
            candidate_tree=snapshot.identity.candidate_tree,
            known_obligation_ids=set(snapshot.scope.obligation_ids) if role == "semantic" else set(),
        )

    return fake, render, expected


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_capacity_oracle_accepts_measured_smaller_full_parent_review(parent_route, role):
    fake, render, compact_available = _measure_parent_route(parent_route, role, crossover=True)
    assert not compact_available
    result = json.loads(fake.run_readonly_agent(render(compact=False), fake.root))
    assert result["disposition"] == "approved"
    call = fake.calls[-1]
    assert call["review_route"] == "full" and not call["composed"]
    assert call["composition_packet_bytes"] - call["full_packet_bytes"] == 113


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_capacity_strict_oracle_still_rejects_a_smaller_full_parent_review(parent_route, role):
    fake, render, _ = _measure_parent_route(parent_route, role, crossover=True)
    fake.require_compact_parents = True
    with pytest.raises(AssertionError, match="compact authority"):
        fake.run_readonly_agent(render(compact=False), fake.root)


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("role", ["semantic", "security"])
def test_capacity_oracle_rejects_full_expansion_when_composition_is_required(parent_route, role, oversized):
    fake, render, compact_available = _measure_parent_route(parent_route, role, oversized=oversized)
    assert compact_available
    with pytest.raises(AssertionError, match="no larger than composition|prompt exceeded"):
        fake.run_readonly_agent(render(compact=False), fake.root)
    result = json.loads(fake.run_readonly_agent(render(compact=True), fake.root))
    assert result["disposition"] == "approved"
    assert fake.calls[-1]["review_route"] == "composition"


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_capacity_oracle_cannot_reuse_sizing_for_another_candidate(parent_route, role):
    fake, render, _ = _measure_parent_route(parent_route, role, crossover=True)
    with pytest.raises(AssertionError, match="matching runtime route"):
        fake.run_readonly_agent(render(compact=False, commit="c" * 40), fake.root)
    assert not fake.calls


def test_compact_parent_mode_rejects_other_scenarios_before_creating_fixture(tmp_path):
    root = tmp_path / "fixture"
    with pytest.raises(ValueError, match="local-repair"):
        run_case(root, depth=1, require_compact_parents=True)
    assert not root.exists()


def test_local_repair_requires_checkpoint_before_creating_fixture(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    with pytest.raises(ValueError, match="checkpoint level"):
        run_case(root, depth=0, scenario="local-repair")
    assert not root.exists()


def test_renewal_oracle_uses_top_level_identity_not_nested_child_identity(tmp_path: Path) -> None:
    from agents.delivery_repair_authority_agent import build_authority_renewal_prompt

    fake = CapacityLLM(tmp_path, 2, 1, "local-repair", False)
    packet = {
        "after": {"node_id": "child", "packet_fingerprint": "sha256:" + "a" * 64},
        "node_id": "root",
        "packet_fingerprint": "sha256:" + "b" * 64,
    }
    prompt = build_authority_renewal_prompt(packet, {}, tmp_path)
    result = json.loads(fake.run_readonly_agent(prompt, tmp_path))
    assert result["packet_fingerprint"] == packet["packet_fingerprint"]
    assert fake.calls[-1]["node"] == "root" and fake.renewals == 1


@pytest.mark.parametrize("changed", ["ownership", "checkpoint_authority", "composition_authority", "admission"])
def test_branching_oracle_rejects_unrelated_checkpoint_rewrites(changed: str) -> None:
    from copy import deepcopy
    from tests.delivery_capacity import assert_unaffected_checkpoints

    before = {
        "checkpoints": [
            {"id": "g0", "unit_ids": ["affected"]},
            {"id": "g1", "unit_ids": ["untouched"]},
        ],
        "checkpoint_authority": {"g0": "first", "g1": "second"},
        "composition_authority": {"g0": "first-parent", "g1": "second-parent"},
    }
    progress = {"checkpoint_verifications": {"g0": {"attempt": 1}, "g1": {"attempt": 1}}}
    after, current = deepcopy(before), deepcopy(progress)
    # A repaired ancestor changes; unrelated historical evidence must not.
    after["checkpoints"][0]["unit_ids"].append("repair")
    after["checkpoint_authority"]["g0"] = "renewed"
    current["checkpoint_verifications"]["g0"]["attempt"] = 2
    current["verification"] = {"reverification": {"children": {"g1": {"current_candidate": True}}}}
    assert_unaffected_checkpoints(before, after, progress, current, {"g0", "root"})
    if changed == "ownership":
        after["checkpoints"][1]["unit_ids"].append("repair")
    elif changed == "admission":
        current["checkpoint_verifications"]["g1"]["attempt"] = 2
    else:
        after[changed]["g1"] = "rewritten"
    with pytest.raises(AssertionError, match="Unrelated"):
        assert_unaffected_checkpoints(before, after, progress, current, {"g0", "root"})
