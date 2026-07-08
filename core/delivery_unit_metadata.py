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
