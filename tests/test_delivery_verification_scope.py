from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from core.delivery_obligations import DeliveryObligation
from core.delivery_plan import (
    DeliveryConstraint,
    DeliveryPlan,
    DeliveryPlanSourceTask,
    DeliveryPlanUnit,
    DeliveryVerificationPolicy,
)
from core.delivery_source_accounting import DeliverySourceAccounting
from core.delivery_verification_scope import DeliveryVerificationScope


def _plan() -> DeliveryPlan:
    return DeliveryPlan(
        schema_version=2,
        plan_id="cache",
        title="PRIVATE SOURCE SUMMARY",
        final_branch="sikula/delivery/cache",
        repositories=[],
        source_task=DeliveryPlanSourceTask("source.md", "sha256:" + "a" * 64),
        verification=DeliveryVerificationPolicy("final_gate"),
        units=[
            DeliveryPlanUnit("old", None, "old.md", superseded_by=["read"], risk_tags=["privacy"]),
            DeliveryPlanUnit("read", None, "read.md", supersedes="old", scope_paths=["src/read.py"]),
            DeliveryPlanUnit("write", None, "write.md", depends_on=["read"]),
        ],
        constraints=[DeliveryConstraint("external", "prohibited_fallback", "Use the existing library", ["read"])],
        obligations=[DeliveryObligation("invalidate", "Invalidate after writes", ["source-1"], ["read", "write"])],
        source_accounting=[
            DeliverySourceAccounting("source-1", "mapped", obligation_ids=["invalidate"]),
            DeliverySourceAccounting("source-2", "context_only", rationale="PRIVATE RATIONALE"),
        ],
    )


def test_root_declares_complete_authority_without_execution_evidence() -> None:
    plan = _plan()
    scope = DeliveryVerificationScope.from_plan(plan)

    assert (scope.plan_id, scope.node_id) == ("cache", "root")
    assert scope.unit_ids == ("read", "write")
    assert scope.obligation_ids == ("invalidate",)
    assert scope.constraint_ids == ("external",)
    assert scope.source_fragment_ids == ("source-1", "source-2")
    assert scope.source_task == plan.source_task
    assert scope.security_required  # Superseded privacy work still requires review.
    assert scope.policy.mode == "final_gate"
    context = scope.plan_context()
    assert context["source_accounting"][1]["disposition"] == "context_only"
    assert context["obligations"][0]["unit_ids"] == ["read", "write"]
    assert "PRIVATE RATIONALE" not in str(context)
    assert "PRIVATE SOURCE SUMMARY" not in repr(scope)


def test_scope_detaches_nested_plan_lists_and_returns_independent_context() -> None:
    plan = _plan()
    scope = DeliveryVerificationScope.from_plan(plan)
    expected = scope.plan_context()
    plan.units[1].scope_paths.append("unauthorized/")
    plan.units[2].depends_on.clear()
    plan.obligations[0].unit_ids.clear()
    plan.obligations[0].source_fragment_ids.append("foreign")
    plan.constraints[0].unit_ids.append("write")
    plan.source_accounting[0].obligation_ids.clear()
    plan.units.clear()
    plan.obligations.clear()
    exported = scope.plan_context()
    exported["units"].clear()
    exported["source_accounting"].clear()

    assert scope.plan_context() == expected
    assert scope.unit_ids == ("read", "write")
    assert scope.obligation_ids == ("invalidate",)
    assert scope.source_fragment_ids == ("source-1", "source-2")
    with pytest.raises(FrozenInstanceError):
        scope.node_id = "other"


@pytest.mark.parametrize("accounting", [None, []])
def test_root_preserves_legacy_accounting_and_context_shape(accounting: list | None) -> None:
    plan = _plan()
    plan = replace(
        plan, schema_version=1, obligations=[], source_task=None, verification=None, source_accounting=accounting
    )
    scope = DeliveryVerificationScope.from_plan(plan)

    assert scope.source_task is None
    assert scope.policy is None
    assert scope.obligation_ids == ()
    assert scope.source_fragment_ids == ()
    assert scope.plan_context() == {
        "plan_id": plan.plan_id,
        "title": plan.title,
        "units": [unit.to_authoring_dict() for unit in plan.units if not unit.superseded],
        "constraints": [constraint.to_dict() for constraint in plan.constraints],
        "obligations": [],
        "source_accounting": accounting,
        "components": [],
    }
