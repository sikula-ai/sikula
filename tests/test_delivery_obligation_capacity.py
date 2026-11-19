"""Whole-plan outcome capacity must not enlarge individual provider exchanges."""

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent, DeliveryIntegrationReviewAgentError
from core.delivery_authoring import DeliveryAuthoringParseError, parse_delivery_authoring_output
from core.delivery_composition_scope import delivery_composition_scope
from core.delivery_obligations import (
    MAX_DELIVERY_LOCAL_OBLIGATIONS,
    MAX_DELIVERY_OBLIGATIONS,
    delivery_authority_fragments,
)
from core.delivery_plan import check_delivery_plan_file
from core.delivery_review_route import preview_composition_context, review_route
from core.delivery_source_accounting import SourceAccountingError, parse_source_accounting
from core.delivery_verification import (
    MAX_DELIVERY_VERIFICATION_PACKET_BYTES,
    check_delivery_verification_readiness,
)
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_review import delivery_integration_review_control_example
from sikula_cli.config import load_config
from tests.delivery_capacity import LOCAL_OUTCOMES, SOURCE, nodes, run_case
from tests.test_delivery_authoring import _draft_data


def test_large_hierarchy_publishes_reloads_and_keeps_each_review_local(tmp_path):
    root = tmp_path / "fixture"
    report = run_case(root, depth=4, fanout=2, scenario="local-repair", prepare_only=True)
    assert report["checks_passed"] and report["prepared"] and not report["completed"]
    assert report["units"] == 31 and report["checkpoints"] == 30
    assert report["operations"][-1]["calls"] == 0
    path = root / ".sikula/delivery/capacity/plan.yaml"
    checked = check_delivery_plan_file(path, project_root=root)
    assert checked.valid, checked.errors
    plan = checked.plan
    assert len(plan.obligations) == 372
    assert any(len(item.obligation_ids) == 372 for item in plan.source_accounting)
    cfg = load_config(root / ".sikula/config.yaml")
    cfg["project"]["root_path"] = str(root)
    assert check_delivery_verification_readiness(checked, cfg).ready
    for node in nodes(4, 2):
        scope = DeliveryVerificationScope.from_plan(plan, node)
        composition = delivery_composition_scope(plan, scope)
        for role in ("semantic", "security"):
            context = preview_composition_context(composition, role)
            route = review_route(scope, cfg, root, SOURCE, role, context=context)
            assert route.ready
            if node == "root":
                assert not route.full_available and route.composition_available
                assert len(context["obligations"]) == len(LOCAL_OUTCOMES)
                direct = {item["id"] for item in context["obligations"]}
                assert all(set(item["obligation_ids"]) <= direct for item in context["source_accounting"])

    # Count alone cannot admit a flat plan or a plan whose checked delegation
    # is unavailable, even if the complete rendered prompt fits the byte cap.
    for unsupported in (
        replace(plan, checkpoints=[]),
        replace(plan, verified_final_gate_authority=None),
    ):
        readiness = check_delivery_verification_readiness(replace(checked, plan=unsupported), cfg)
        assert not readiness.ready
        assert "delivery_verification.hierarchy_required" in {issue.code for issue in readiness.errors}

    # Global overflow still blocks parsing/publication; it is never a valid
    # prefix of outcomes that could silently lose the rest of the source.
    data = yaml.safe_load(path.read_text())
    example = data["obligations"][0]
    data["obligations"] = [{**example, "id": f"outcome-{i}"} for i in range(MAX_DELIVERY_OBLIGATIONS + 1)]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    invalid = check_delivery_plan_file(path, project_root=root)
    assert not invalid.valid and "obligations.too_many" in {error.code for error in invalid.errors}


@pytest.mark.parametrize("count", [256, 257])
def test_flat_authoring_retains_its_local_outcome_limit(tmp_path, count):
    source = "# Source\n\nDeliver independently verifiable outcomes.\n"
    fragment = delivery_authority_fragments(source)[-1].id
    data = _draft_data()
    data["obligations"] = [
        {
            "id": f"outcome-{i}",
            "summary": "The assigned behavior is delivered.",
            "source_fragment_ids": [fragment],
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
        for i in range(count)
    ]
    args = dict(
        expected_plan_id="team-invites",
        project_root=tmp_path,
        output_dir=tmp_path / ".sikula/delivery/team-invites",
        source_task_description=source,
        require_obligations=True,
    )
    if count <= MAX_DELIVERY_LOCAL_OBLIGATIONS:
        assert len(parse_delivery_authoring_output(json.dumps(data), **args).obligations) == count
    else:
        with pytest.raises(DeliveryAuthoringParseError) as error:
            parse_delivery_authoring_output(json.dumps(data), **args)
        assert error.value.code == "delivery_authoring.obligations_too_many"


@pytest.mark.parametrize("role", ["semantic", "security"])
@pytest.mark.parametrize("count", [256, 257])
def test_runtime_enforces_local_outcome_limit_before_provider_call(tmp_path, role, count):
    llm = Mock()
    reviewer = DeliveryIntegrationReviewAgent(llm, {})
    context = {"obligations": [{"id": f"outcome-{i}"} for i in range(count)]}
    args = dict(
        cwd=tmp_path,
        review_kind=role,
        source_task="Source",
        plan_context=context,
        validation_summary={},
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        known_obligation_ids={item["id"] for item in context["obligations"]} if role == "semantic" else set(),
    )
    assert len(reviewer._prompt(**args).encode()) < MAX_DELIVERY_VERIFICATION_PACKET_BYTES
    if count == 256:
        llm.run_readonly_agent.return_value = delivery_integration_review_control_example(args["known_obligation_ids"])
        result = reviewer.review(**args, known_unit_ids=set())
        assert result.assessment.approved
        llm.run_readonly_agent.assert_called_once()
        return
    with pytest.raises(DeliveryIntegrationReviewAgentError) as error:
        reviewer.review(**args, known_unit_ids=set())
    assert error.value.code == "delivery_verification.hierarchy_required"
    llm.run_readonly_agent.assert_not_called()


@pytest.mark.parametrize("fault", [None, "overflow", "duplicate", "unknown", "missing", "provenance"])
def test_large_accounting_keeps_exact_bounded_source_coverage(fault):
    count = MAX_DELIVERY_OBLIGATIONS + (fault == "overflow")
    sources = {f"outcome-{i}": {"source"} for i in range(count)}
    refs = list(sources)
    if fault == "duplicate":
        refs[-1] = refs[0]
    elif fault == "unknown":
        refs[-1] = "not-declared"
    elif fault == "missing":
        refs.pop()
    elif fault == "provenance":
        sources[refs[-1]] = set()
    records = [
        {
            "source_fragment_id": "source",
            "disposition": "mapped",
            "obligation_ids": refs,
            "constraint_ids": [],
            "rationale": "The shared source governs all declared outcomes.",
        }
    ]
    args = dict(fragment_ids={"source"}, obligation_sources=sources, constraint_ids=set())
    if fault:
        with pytest.raises(SourceAccountingError):
            parse_source_accounting(records, **args, private_rationales=True)
    else:
        parsed = parse_source_accounting(records, **args, private_rationales=True)
        restored = parse_source_accounting([r.to_dict() for r in parsed], **args, private_rationales=False)
        assert restored[0].obligation_ids == refs
