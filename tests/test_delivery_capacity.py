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
    from tests import delivery_capacity

    def cancel():
        raise KeyboardInterrupt()

    monkeypatch.setattr(delivery_capacity, "sikula_main", cancel)
    root = tmp_path / "fixture"
    with pytest.raises(KeyboardInterrupt):
        run_case(root, depth=1)
    report = json.loads((root / ".capacity/report.json").read_text())
    assert not report["completed"] and not report["checks_passed"]
    assert not report["calls"] and not report["operations"]


def test_local_repair_fixture_prepares_compact_routes_without_changing_limits(tmp_path: Path) -> None:
    from core.delivery_plan import check_delivery_plan_file
    from core.delivery_composition_scope import delivery_composition_scope
    from core.delivery_review_route import preview_composition_context, review_route
    from core.delivery_verification_scope import DeliveryVerificationScope
    from sikula_cli.config import load_config
    from tests.delivery_capacity import LOCAL_OUTCOMES, SOURCE

    root = tmp_path / "fixture"
    report = run_case(root, depth=2, fanout=1, scenario="local-repair", prepare_only=True)
    assert report["checks_passed"] and not report["completed"]
    assert not report.get("local_repair_lifecycle_verified")
    cfg = load_config(root / ".sikula/config.yaml")
    cfg["project"]["root_path"] = str(root)
    checked = check_delivery_plan_file(root / ".sikula/delivery/capacity/plan.yaml", project_root=root)
    assert checked.valid, checked.errors
    assert len(checked.plan.obligations) == 3 * len(LOCAL_OUTCOMES)
    for node in ("g0.g0", "g0", "root"):
        scope = DeliveryVerificationScope.from_plan(checked.plan, node)
        composition = delivery_composition_scope(checked.plan, scope)
        for role in ("semantic", "security"):
            route = review_route(scope, cfg, root, SOURCE, role, context=preview_composition_context(composition, role))
            assert route.ready and route.full_available
            assert route.composition_available == (node != "g0.g0")


def test_local_repair_oracle_rejects_full_parent_review(tmp_path: Path) -> None:
    fake = CapacityLLM(tmp_path, 2, 1, "local-repair", False)
    prompt = 'Delivery plan context:\n{"units": [], "verification_node": {"id": "g0"}}'
    prompt += "\n\nDeterministic validation summary:\n{}"
    with pytest.raises(AssertionError, match="compact authority"):
        fake.run_readonly_agent(prompt, tmp_path)


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
