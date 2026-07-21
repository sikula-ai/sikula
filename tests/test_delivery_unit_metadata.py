from core.delivery_unit_metadata import (
    DeliveryUnitBudget,
    delivery_unit_budget_snapshot,
    delivery_unit_planner_step_limit,
)


def test_delivery_unit_planner_step_limit_defaults_to_single_step() -> None:
    assert delivery_unit_planner_step_limit(None) == 1
    assert delivery_unit_planner_step_limit({}) == 1
    assert delivery_unit_planner_step_limit({"max_planner_steps": True}) == 1
    assert delivery_unit_planner_step_limit({"max_planner_steps": 0}) == 1


def test_delivery_unit_planner_step_limit_accepts_only_supported_range() -> None:
    assert delivery_unit_planner_step_limit(DeliveryUnitBudget(max_planner_steps=1)) == 1
    assert delivery_unit_planner_step_limit(DeliveryUnitBudget(max_planner_steps=2)) == 2
    assert delivery_unit_planner_step_limit(DeliveryUnitBudget(max_planner_steps=3)) == 2


def test_delivery_unit_budget_snapshot_is_allowlisted_and_includes_effective_limit() -> None:
    source = {
        "max_changed_files": 4,
        "max_planner_steps": 2,
        "unknown": 9,
        "max_review_cycles": False,
    }

    snapshot = delivery_unit_budget_snapshot(source)

    assert snapshot == {"max_changed_files": 4, "max_planner_steps": 2}
    assert source["unknown"] == 9


def test_delivery_unit_budget_snapshot_defaults_dataclass_limit() -> None:
    budget = DeliveryUnitBudget(max_changed_modules=3)

    assert delivery_unit_budget_snapshot(budget) == {
        "max_planner_steps": 1,
        "max_changed_modules": 3,
    }
