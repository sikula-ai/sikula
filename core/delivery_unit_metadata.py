from __future__ import annotations

from dataclasses import dataclass

DELIVERY_UNIT_SIZE_VALUES = ("small", "medium", "large")

DELIVERY_UNIT_RISK_TAG_VALUES = (
    "api_surface",
    "audit_artifacts",
    "auth_permissions",
    "automation_behavior",
    "build_pipeline",
    "cli_surface",
    "configuration",
    "data_persistence",
    "docs_coverage",
    "execution_boundary",
    "external_execution_boundary",
    "external_integration",
    "migration",
    "privacy",
    "public_output_contract",
    "release",
    "security_boundary",
    "structured_output_contract",
    "test_hardening",
    "ui_surface",
    "validation",
)

DELIVERY_UNIT_SPLIT_RISK_TAGS = frozenset(
    {
        "audit_artifacts",
        "api_surface",
        "cli_surface",
        "configuration",
        "data_persistence",
        "execution_boundary",
        "external_execution_boundary",
        "external_integration",
        "migration",
        "privacy",
        "public_output_contract",
        "security_boundary",
        "structured_output_contract",
        "ui_surface",
    }
)

DELIVERY_UNIT_BUDGET_FIELDS = (
    "max_planner_steps",
    "max_elapsed_minutes",
    "max_review_cycles",
    "max_security_cycles",
    "max_changed_files",
    "max_changed_modules",
    "max_generated_test_files",
)

DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS = 1
MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS = 2
DELIVERY_UNIT_BUDGET_EXCEEDED_CODE = "unit_budget_exceeded"


@dataclass(frozen=True)
class DeliveryUnitBudget:
    max_planner_steps: int | None = None
    max_elapsed_minutes: int | None = None
    max_review_cycles: int | None = None
    max_security_cycles: int | None = None
    max_changed_files: int | None = None
    max_changed_modules: int | None = None
    max_generated_test_files: int | None = None

    def to_dict(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for field_name in DELIVERY_UNIT_BUDGET_FIELDS:
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result


def delivery_unit_planner_step_limit(budget: DeliveryUnitBudget | dict | None) -> int:
    if isinstance(budget, DeliveryUnitBudget):
        raw = budget.max_planner_steps
    elif isinstance(budget, dict):
        raw = budget.get("max_planner_steps")
    else:
        raw = None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS
    return min(raw, MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS)


def delivery_unit_budget_snapshot(budget: DeliveryUnitBudget | dict | None) -> dict[str, int]:
    if isinstance(budget, DeliveryUnitBudget):
        result = budget.to_dict()
    elif isinstance(budget, dict):
        result = {
            key: value
            for key, value in budget.items()
            if key in DELIVERY_UNIT_BUDGET_FIELDS
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        }
    else:
        result = {}
    result["max_planner_steps"] = delivery_unit_planner_step_limit(budget)
    return result
