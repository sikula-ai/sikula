from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from json import JSONDecodeError
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any
import unicodedata

from core.delivery_unit_metadata import (
    DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS,
    DELIVERY_UNIT_BUDGET_FIELDS,
    DELIVERY_UNIT_RISK_TAG_VALUES,
    DELIVERY_UNIT_SIZE_VALUES,
    MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS,
    DeliveryUnitBudget,
)
from core.delivery_plan import (
    DELIVERY_CONSTRAINT_KIND_VALUES,
    MAX_DELIVERY_CONSTRAINTS,
    MAX_DELIVERY_CONSTRAINT_UNIT_IDS,
    MAX_DELIVERY_UNIT_ID_LENGTH,
    DeliveryPlanSourceTask,
)
from core.delivery_public_metadata import contains_delivery_source_excerpt, is_safe_delivery_public_metadata
from core.markdown_headings import MarkdownHeading, MarkdownHeadingScanner, normalize_heading
from core.structured_output import load_schema_json_object
from core.validation_coverage import extract_validation_commands

_DELIVERY_AUTHORING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLANNING_MODES = {"fixed_window"}
DELIVERY_ASSESSMENT_MODES = frozenset({"single_run", "delivery_plan", "needs_clarification"})
DELIVERY_ASSESSMENT_REASON_CODES = frozenset(
    {
        "single_cohesive_surface",
        "single_validation_boundary",
        "multiple_independent_surfaces",
        "multiple_platforms",
        "multiple_components",
        "multiple_risk_boundaries",
        "dependency_order_required",
        "scope_unclear",
        "acceptance_criteria_unclear",
        "ownership_unclear",
        "validation_unclear",
        "decomposition_unclear",
    }
)
_DELIVERY_ASSESSMENT_REASON_CODES_BY_MODE = {
    "single_run": frozenset({"single_cohesive_surface", "single_validation_boundary"}),
    "delivery_plan": frozenset(
        {
            "multiple_independent_surfaces",
            "multiple_platforms",
            "multiple_components",
            "multiple_risk_boundaries",
            "dependency_order_required",
        }
    ),
    "needs_clarification": frozenset(
        {
            "scope_unclear",
            "acceptance_criteria_unclear",
            "ownership_unclear",
            "validation_unclear",
            "decomposition_unclear",
        }
    ),
}
_MAX_DELIVERY_ASSESSMENT_REASONS = 16
_MAX_DELIVERY_ASSESSMENT_UNITS = 100
_MAX_ASSET_ASSIGNMENT_ALIAS_LENGTH = 4096
_FENCED_JSON_RE = re.compile(
    r"^\s*(?P<fence>`{3,}|~{3,})[ \t]*json[ \t]*\r?\n(?P<content>.*)\r?\n(?P=fence)[ \t]*\s*$",
    re.DOTALL | re.IGNORECASE,
)
DELIVERY_CONSTRAINT_DRAFT_DISPOSITIONS = frozenset({"conflict", "needs_review", "preserved"})
DELIVERY_CONSTRAINT_GAP_REASONS = frozenset({"incompletely_assigned", "omitted"})
MAX_DELIVERY_UNIT_CONTEXT_GAPS = 100
MAX_DELIVERY_UNIT_CONTEXT_LITERALS = 100
MAX_DELIVERY_UNIT_CONTEXT_LITERAL_LENGTH = 1000
MAX_DELIVERY_UNIT_CONTEXT_TOTAL_LENGTH = 20_000
_TOP_LEVEL_FIELDS = {"plan_id", "title", "units", "planning_mode", "warnings", "constraints"}
_AMENDMENT_TOP_LEVEL_FIELDS = {
    "plan_id",
    "target_unit_id",
    "replacement_units",
    "disposition",
    "summary",
    "amend_reason",
    "budget_exceeded",
    "warnings",
}
_CONSTRAINT_VERIFICATION_TOP_LEVEL_FIELDS = {
    "constraints_complete",
    "constraints",
    "constraint_gaps",
    "unit_context_complete",
    "unit_context_gaps",
}
_ASSESSMENT_TOP_LEVEL_FIELDS = {"recommended_mode", "reason_codes", "units"}
_ASSESSMENT_SCHEMA_KEYS = frozenset({"recommended_mode", "reason_codes"})
_ASSESSMENT_UNIT_FIELDS = {"id", "title", "depends_on", "stream", "component", "platform"}
_UNIT_FIELDS = {
    "id",
    "title",
    "depends_on",
    "task_markdown",
    "stream",
    "component",
    "phase",
    "kind",
    "platform",
    "scope_paths",
    "asset_paths",
    "estimated_size",
    "risk_tags",
    "budget",
}
_CONSTRAINT_FIELDS = {"id", "kind", "summary", "unit_ids", "disposition"}
_CONSTRAINT_GAP_FIELDS = {"reason", "constraint_id", "kind", "summary", "affected_unit_ids"}
_UNIT_CONTEXT_GAP_FIELDS = {"unit_id", "source_literals"}
_CONSTRAINT_REPAIR_TOP_LEVEL_FIELDS = {"constraints"}
_AUTHORING_SCHEMA_KEYS = frozenset({"plan_id", "title", "units"})
_AMENDMENT_SCHEMA_KEYS = frozenset({"plan_id", "target_unit_id", "replacement_units"})
_CONSTRAINT_VERIFICATION_SCHEMA_KEYS = frozenset({"constraints_complete", "constraints"})
_UNIT_PATH_FIELDS = {
    "task_path",
    "path",
    "unit_path",
    "output_path",
    "plan_path",
    "units_dir",
    "output_dir",
}
_REQUIRED_UNIT_MARKDOWN_SECTIONS = {
    "goal": {"goal", "goals", "objective", "objectives", "summary", "overview"},
    "current_behavior": {"current behavior", "current behaviour", "existing behavior", "existing behaviour"},
    "desired_behavior": {"desired behavior", "desired behaviour", "expected behavior", "expected behaviour"},
    "acceptance_criteria": {"acceptance", "acceptance criteria", "criteria"},
    "security_privacy": {
        "security",
        "privacy",
        "security privacy",
        "security and privacy",
        "security privacy notes",
        "security and privacy notes",
        "security notes",
        "privacy notes",
    },
    "reviewer_focus": {
        "reviewer focus",
        "review focus",
        "review notes",
        "risky areas",
        "risks",
        "review checklist",
    },
    "out_of_scope": {"out of scope", "non goals", "non-goals", "not in scope", "excluded", "exclusions"},
    "verification": {
        "verification",
        "validation",
        "checks",
        "check",
        "test plan",
        "how to validate",
        "before merge",
    },
}
_ASSET_MANIFEST_HEADING = normalize_heading("Asset manifest")


@dataclass
class DeliveryAuthoringUnitDraft:
    id: str
    title: str
    depends_on: list[str]
    task_markdown: str
    stream: str | None = None
    component: str | None = None
    phase: str | None = None
    kind: str | None = None
    platform: str | None = None
    scope_paths: list[str] = field(default_factory=list)
    asset_paths: list[str] = field(default_factory=list)
    estimated_size: str | None = None
    risk_tags: list[str] = field(default_factory=list)
    budget: DeliveryUnitBudget | None = None


@dataclass(frozen=True)
class DeliveryAuthoringConstraintDraft:
    id: str
    kind: str
    summary: str
    unit_ids: list[str]
    disposition: str

    def to_plan_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "unit_ids": list(self.unit_ids),
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class DeliveryConstraintGap:
    reason: str
    kind: str
    summary: str
    affected_unit_ids: list[str]
    constraint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "reason": self.reason,
            "kind": self.kind,
            "summary": self.summary,
            "affected_unit_ids": list(self.affected_unit_ids),
        }
        if self.constraint_id is not None:
            data["constraint_id"] = self.constraint_id
        return data


@dataclass(frozen=True)
class DeliveryUnitContextGap:
    unit_id: str
    source_literals: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "source_literals": list(self.source_literals),
        }


@dataclass(frozen=True)
class DeliveryConstraintVerification:
    constraints_complete: bool
    constraints: list[DeliveryAuthoringConstraintDraft]
    constraint_gaps: list[DeliveryConstraintGap] = field(default_factory=list)
    unit_context_complete: bool = True
    unit_context_gaps: list[DeliveryUnitContextGap] = field(default_factory=list)


@dataclass
class DeliveryAuthoringDraft:
    plan_id: str
    title: str
    units: list[DeliveryAuthoringUnitDraft]
    planning_mode: str | None = None
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[DeliveryAuthoringConstraintDraft] = field(default_factory=list)
    source_task: DeliveryPlanSourceTask | None = None
    constraint_verification: DeliveryConstraintVerification | None = None


@dataclass
class DeliveryAmendmentAuthoringDraft:
    plan_id: str
    target_unit_id: str
    replacement_units: list[DeliveryAuthoringUnitDraft]
    amend_reason: str | None = None
    budget_exceeded: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)
    disposition: str | None = None
    summary: str | None = None
    constraint_verification: DeliveryConstraintVerification | None = None


@dataclass
class DeliveryAuthoringDerivedPaths:
    plan_file: str
    units_dir: str
    unit_task_paths: dict[str, str]


@dataclass(frozen=True)
class DeliveryAssessmentUnitDraft:
    id: str
    title: str
    depends_on: list[str]
    stream: str | None = None
    component: str | None = None
    platform: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "depends_on": list(self.depends_on),
        }
        if self.stream is not None:
            data["stream"] = self.stream
        if self.component is not None:
            data["component"] = self.component
        if self.platform is not None:
            data["platform"] = self.platform
        return data


@dataclass
class DeliveryAssessmentDraft:
    recommended_mode: str
    reason_codes: list[str]
    units: list[DeliveryAssessmentUnitDraft] = field(default_factory=list)


class DeliveryAuthoringParseError(ValueError):
    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_delivery_assessment_output(output: str) -> DeliveryAssessmentDraft:
    data = _parse_output_object(output, schema_keys=_ASSESSMENT_SCHEMA_KEYS)
    _reject_unknown_fields(data, _ASSESSMENT_TOP_LEVEL_FIELDS, "assessment top-level")

    recommended_mode = _require_string(data, "recommended_mode", "recommended_mode")
    if recommended_mode not in DELIVERY_ASSESSMENT_MODES:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.mode_invalid",
            "recommended_mode must be a supported delivery assessment mode.",
        )

    reason_codes = _assessment_reason_codes(data.get("reason_codes"), recommended_mode)
    units = _parse_assessment_units(data.get("units", []))
    if recommended_mode == "delivery_plan" and len(units) < 2:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.units_required",
            "delivery_plan recommendations must include at least two proposed units.",
        )
    if recommended_mode != "delivery_plan" and units:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.units_forbidden",
            "Only delivery_plan recommendations may include proposed units.",
        )

    return DeliveryAssessmentDraft(
        recommended_mode=recommended_mode,
        reason_codes=reason_codes,
        units=units,
    )


def parse_delivery_authoring_output(
    output: str,
    *,
    expected_plan_id: str,
    project_root: str | Path,
    output_dir: str | Path,
    source_task_description: str | None = None,
) -> DeliveryAuthoringDraft:
    root = _resolve_project_root(project_root)
    selected_plan_id = _expected_plan_id(expected_plan_id, output_dir=output_dir, project_root=root)
    data = _parse_output_object(output, schema_keys=_AUTHORING_SCHEMA_KEYS)
    _reject_unknown_fields(data, _TOP_LEVEL_FIELDS, "top-level")

    plan_id = _require_string(data, "plan_id", "plan_id")
    if not _DELIVERY_AUTHORING_ID_RE.fullmatch(plan_id):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.plan_id_invalid",
            "plan_id may contain only letters, numbers, dots, underscores, and hyphens.",
        )
    if plan_id != selected_plan_id:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.plan_id_mismatch",
            "plan_id must match the selected delivery plan id.",
        )

    title = _bounded_authoring_string(data, "title", "title")
    planning_mode = _optional_planning_mode(data, "planning_mode", "planning_mode")
    warnings = _optional_string_list(data, "warnings", "warnings")
    units = _parse_units(data.get("units"), project_root=root, allow_asset_paths=True)
    constraints = _parse_constraints(
        data,
        unit_ids={unit.id for unit in units},
        source_task_description=source_task_description,
    )

    return DeliveryAuthoringDraft(
        plan_id=plan_id,
        title=title,
        units=units,
        planning_mode=planning_mode,
        warnings=warnings,
        constraints=constraints,
    )


def parse_delivery_constraint_verification_output(
    output: str,
    *,
    unit_ids: set[str],
    source_task_description: str | None = None,
    unit_task_markdown_by_id: dict[str, str] | None = None,
    require_unit_context: bool = False,
) -> DeliveryConstraintVerification:
    data = _parse_output_object(output, schema_keys=_CONSTRAINT_VERIFICATION_SCHEMA_KEYS)
    _reject_unknown_fields(data, _CONSTRAINT_VERIFICATION_TOP_LEVEL_FIELDS, "constraint verification")
    constraints_complete = data.get("constraints_complete")
    if type(constraints_complete) is not bool:
        raise DeliveryAuthoringParseError(
            "delivery_constraint_verification.complete_invalid",
            "constraints_complete must be a boolean.",
        )
    constraints = _parse_constraints(data, unit_ids=unit_ids)
    constraint_gaps = _parse_constraint_gaps(
        data.get("constraint_gaps", []),
        constraints=constraints,
        unit_ids=unit_ids,
        source_task_description=source_task_description,
    )
    if constraints_complete and constraint_gaps:
        raise DeliveryAuthoringParseError(
            "delivery_constraint_verification.gaps_unexpected",
            "Complete constraint verification must not report constraint gaps.",
        )
    if not constraints_complete and not constraint_gaps:
        raise DeliveryAuthoringParseError(
            "delivery_constraint_verification.gaps_required",
            "Incomplete constraint verification must identify at least one actionable constraint gap.",
        )
    unit_context_complete = data.get("unit_context_complete")
    if require_unit_context and type(unit_context_complete) is not bool:
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.complete_invalid",
            "unit_context_complete must be a boolean.",
        )
    if type(unit_context_complete) is not bool:
        unit_context_complete = True
    unit_context_gaps = _parse_unit_context_gaps(
        data.get("unit_context_gaps", []),
        unit_ids=unit_ids,
        source_task_description=source_task_description,
        unit_task_markdown_by_id=unit_task_markdown_by_id,
    )
    if unit_context_complete and unit_context_gaps:
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.gaps_unexpected",
            "Complete unit-context verification must not report missing source literals.",
        )
    if not unit_context_complete and not unit_context_gaps:
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.gaps_required",
            "Incomplete unit-context verification must identify missing source literals.",
        )
    return DeliveryConstraintVerification(
        constraints_complete=constraints_complete,
        constraints=constraints,
        constraint_gaps=constraint_gaps,
        unit_context_complete=unit_context_complete,
        unit_context_gaps=unit_context_gaps,
    )


def apply_delivery_unit_context_gaps(
    units: list[DeliveryAuthoringUnitDraft],
    gaps: list[DeliveryUnitContextGap],
) -> list[DeliveryAuthoringUnitDraft]:
    literals_by_unit = {gap.unit_id: gap.source_literals for gap in gaps}
    repaired: list[DeliveryAuthoringUnitDraft] = []
    for unit in units:
        literals = literals_by_unit.get(unit.id)
        if not literals:
            repaired.append(unit)
            continue
        addition = "\n".join(f"> {literal}" for literal in literals)
        task_markdown = (
            unit.task_markdown.rstrip()
            + "\n\n## Authoritative source values\n\n"
            + "Use these source-defined identifiers and values verbatim:\n\n"
            + addition
            + "\n"
        )
        _validate_unit_task_markdown(task_markdown)
        repaired.append(replace(unit, task_markdown=task_markdown))
    return repaired


def parse_delivery_constraint_repair_output(
    output: str,
    *,
    unit_ids: set[str],
    source_task_description: str | None = None,
) -> list[DeliveryAuthoringConstraintDraft]:
    data = _parse_output_object(output, schema_keys=frozenset({"constraints"}))
    _reject_unknown_fields(data, _CONSTRAINT_REPAIR_TOP_LEVEL_FIELDS, "constraint repair")
    return _parse_constraints(
        data,
        unit_ids=unit_ids,
        source_task_description=source_task_description,
    )


def parse_delivery_amendment_authoring_output(
    output: str,
    *,
    expected_plan_id: str,
    expected_target_unit_id: str,
    project_root: str | Path,
    allow_asset_manifest: bool = False,
) -> DeliveryAmendmentAuthoringDraft:
    root = _resolve_project_root(project_root)
    data = _parse_output_object(output, schema_keys=_AMENDMENT_SCHEMA_KEYS)
    _reject_unknown_fields(data, _AMENDMENT_TOP_LEVEL_FIELDS, "top-level")

    plan_id = _require_string(data, "plan_id", "plan_id")
    if plan_id != expected_plan_id:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.plan_id_mismatch",
            "plan_id must match the selected delivery plan.",
        )
    target_unit_id = _require_string(data, "target_unit_id", "target_unit_id")
    if target_unit_id != expected_target_unit_id:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.target_unit_mismatch",
            "target_unit_id must match the selected split unit.",
        )
    disposition = _optional_string(data, "disposition", "disposition")
    if disposition not in {None, "external_dependency_follow_up_required"}:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.disposition_invalid",
            "disposition must be external_dependency_follow_up_required when present.",
        )
    summary = None if data.get("summary") is None else _optional_bounded_authoring_string(data, "summary", "summary")
    if disposition is None and summary is not None:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.summary_unexpected",
            "summary is allowed only with an external dependency follow-up disposition.",
        )
    if disposition is not None and summary is None:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.summary_required",
            "An external dependency follow-up disposition requires a bounded summary.",
        )
    if disposition is not None:
        if data.get("replacement_units") != []:
            raise DeliveryAuthoringParseError(
                "delivery_amend_authoring.replacements_unexpected",
                "An external dependency follow-up disposition requires an empty replacement_units list.",
            )
        replacement_units = []
    else:
        replacement_units = _parse_units(
            data.get("replacement_units"),
            project_root=root,
            allow_asset_paths=True,
            allow_asset_manifest=allow_asset_manifest,
        )
    if target_unit_id in {unit.id for unit in replacement_units}:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.target_id_reused",
            "Replacement units must use new unit ids.",
        )
    amend_reason = None if data.get("amend_reason") is None else _optional_string(data, "amend_reason", "amend_reason")
    if amend_reason and not _DELIVERY_AUTHORING_ID_RE.fullmatch(amend_reason):
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.amend_reason_invalid",
            "amend_reason must be a stable code.",
        )
    budget_exceeded = _parse_budget_exceeded(data.get("budget_exceeded"))
    warnings = _optional_string_list(data, "warnings", "warnings")
    return DeliveryAmendmentAuthoringDraft(
        plan_id=plan_id,
        target_unit_id=target_unit_id,
        replacement_units=replacement_units,
        disposition=disposition,
        summary=summary,
        amend_reason=amend_reason,
        budget_exceeded=budget_exceeded,
        warnings=warnings,
    )


def _parse_budget_exceeded(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"name", "limit", "actual"}:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.budget_exceeded_invalid",
            "budget_exceeded must contain exactly name, limit, and actual.",
        )
    name = value.get("name")
    limit = value.get("limit")
    actual = value.get("actual")
    if not isinstance(name, str) or not name.strip() or not _DELIVERY_AUTHORING_ID_RE.fullmatch(name.strip()):
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.budget_exceeded_invalid",
            "budget_exceeded.name must be a stable code.",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.budget_exceeded_invalid",
            "budget_exceeded.limit must be a non-negative integer.",
        )
    if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
        raise DeliveryAuthoringParseError(
            "delivery_amend_authoring.budget_exceeded_invalid",
            "budget_exceeded.actual must be a non-negative integer.",
        )
    return {"name": name.strip(), "limit": limit, "actual": actual}


def derive_delivery_authoring_paths(
    draft: DeliveryAuthoringDraft,
    *,
    output_dir: str | Path,
    project_root: str | Path,
) -> DeliveryAuthoringDerivedPaths:
    root = _resolve_project_root(project_root)
    output_path = _resolve_project_relative_path(output_dir, root)
    _require_path_within_project(output_path, root, code="delivery_authoring.output_dir_outside_project")

    plan_file = output_path / "plan.yaml"
    units_dir = output_path / "units"
    _require_path_within_project(plan_file, root, code="delivery_authoring.plan_file_outside_project")
    _require_path_within_project(units_dir, root, code="delivery_authoring.units_dir_outside_project")

    seen: set[str] = set()
    unit_task_paths: dict[str, str] = {}
    for unit in draft.units:
        _validate_unit_id(unit.id, "units.id")
        if unit.id in seen:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.unit_id_duplicate",
                "Unit IDs must be unique.",
            )
        seen.add(unit.id)
        unit_task_path = units_dir / f"{unit.id}.md"
        _require_path_within_project(
            unit_task_path,
            root,
            code="delivery_authoring.unit_task_path_outside_project",
        )
        unit_task_paths[unit.id] = _project_relative_path(unit_task_path, root)

    return DeliveryAuthoringDerivedPaths(
        plan_file=_project_relative_path(plan_file, root),
        units_dir=_project_relative_path(units_dir, root),
        unit_task_paths=unit_task_paths,
    )


def _parse_output_object(output: str, *, schema_keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(output, str) or not output.strip():
        raise DeliveryAuthoringParseError(
            "delivery_authoring.empty_output",
            "Delivery authoring output must be a non-empty JSON object.",
        )

    stripped = output.strip()
    fence_match = _FENCED_JSON_RE.fullmatch(stripped)
    json_text = fence_match.group("content").strip() if fence_match is not None else stripped

    try:
        value = json.loads(
            json_text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (JSONDecodeError, ValueError):
        try:
            value = load_schema_json_object(
                stripped,
                required_keys=schema_keys,
                object_pairs_hook=_object_pairs_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except ValueError as exc:
            code = (
                "delivery_authoring.output_invalid_envelope"
                if stripped.startswith(("```", "~~~"))
                else "delivery_authoring.json_invalid"
            )
            message = (
                "Delivery authoring output must contain one unambiguous JSON object, optionally surrounded by prose."
            )
            raise DeliveryAuthoringParseError(code, message) from exc

    if not isinstance(value, dict):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.root_not_object",
            "Delivery authoring output must be a JSON object.",
        )
    return value


def _object_pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _expected_plan_id(expected_plan_id: str, *, output_dir: str | Path, project_root: Path) -> str:
    if not isinstance(expected_plan_id, str):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.expected_plan_id_invalid",
            "Selected delivery plan id is invalid.",
        )
    plan_id = expected_plan_id.strip()
    if not plan_id or not _DELIVERY_AUTHORING_ID_RE.fullmatch(plan_id):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.expected_plan_id_invalid",
            "Selected delivery plan id is invalid.",
        )
    output_path = _resolve_project_relative_path(output_dir, project_root)
    _require_path_within_project(output_path, project_root, code="delivery_authoring.output_dir_outside_project")
    if output_path.name != plan_id:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.expected_plan_id_mismatch",
            "Selected delivery plan id must match the output directory name.",
        )
    return plan_id


def _parse_units(
    value: Any,
    *,
    project_root: Path,
    allow_asset_paths: bool = False,
    allow_asset_manifest: bool = False,
) -> list[DeliveryAuthoringUnitDraft]:
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.units_invalid_type",
            "units must be a non-empty list of delivery unit objects.",
        )
    if not value:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.units_empty",
            "units must include at least one delivery unit.",
        )

    units: list[DeliveryAuthoringUnitDraft] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        unit_path = f"units[{idx}]"
        if not isinstance(item, dict):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.unit_not_object",
                "Delivery unit entries must be JSON objects.",
            )
        _reject_unit_unknown_fields(item, unit_path, allow_asset_paths=allow_asset_paths)

        unit_id = _require_string(item, "id", f"{unit_path}.id")
        _validate_unit_id(unit_id, f"{unit_path}.id")
        if unit_id in seen:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.unit_id_duplicate",
                "Unit IDs must be unique.",
            )
        seen.add(unit_id)

        title = _bounded_authoring_string(item, "title", f"{unit_path}.title")
        depends_on = _require_string_list(item, "depends_on", f"{unit_path}.depends_on")
        task_markdown = _require_string(item, "task_markdown", f"{unit_path}.task_markdown")
        _validate_unit_task_markdown(task_markdown, allow_asset_manifest=allow_asset_manifest)

        units.append(
            DeliveryAuthoringUnitDraft(
                id=unit_id,
                title=title,
                depends_on=depends_on,
                task_markdown=task_markdown,
                stream=_optional_bounded_authoring_string(item, "stream", f"{unit_path}.stream"),
                component=_optional_bounded_authoring_string(item, "component", f"{unit_path}.component"),
                phase=_optional_bounded_authoring_string(item, "phase", f"{unit_path}.phase"),
                kind=_optional_bounded_authoring_string(item, "kind", f"{unit_path}.kind"),
                platform=_optional_bounded_authoring_string(item, "platform", f"{unit_path}.platform"),
                scope_paths=_optional_scope_paths(item, "scope_paths", f"{unit_path}.scope_paths", project_root),
                asset_paths=(
                    _optional_asset_paths(item, "asset_paths", f"{unit_path}.asset_paths") if allow_asset_paths else []
                ),
                estimated_size=_optional_estimated_size(item, "estimated_size", f"{unit_path}.estimated_size"),
                risk_tags=_optional_risk_tags(item, "risk_tags", f"{unit_path}.risk_tags"),
                budget=_optional_budget(item, "budget", f"{unit_path}.budget"),
            )
        )

    _validate_dependencies(units)
    return units


def _parse_constraints(
    data: dict[str, Any],
    *,
    unit_ids: set[str],
    source_task_description: str | None = None,
) -> list[DeliveryAuthoringConstraintDraft]:
    if "constraints" not in data:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.constraints_required",
            "constraints must explicitly list inherited hard constraints or be an empty list.",
        )
    value = data.get("constraints")
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.constraints_invalid_type",
            "constraints must be a list of inherited hard-constraint objects.",
        )
    if len(value) > MAX_DELIVERY_CONSTRAINTS:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.constraints_too_many",
            "constraints contains too many inherited hard constraints.",
        )

    constraints: list[DeliveryAuthoringConstraintDraft] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        constraint_path = f"constraints[{index}]"
        if not isinstance(item, dict):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_not_object",
                "Constraint entries must be JSON objects.",
            )
        _reject_unknown_fields(item, _CONSTRAINT_FIELDS, constraint_path)
        constraint_id = _require_string(item, "id", f"{constraint_path}.id")
        if len(constraint_id) > MAX_DELIVERY_UNIT_ID_LENGTH or not _DELIVERY_AUTHORING_ID_RE.fullmatch(constraint_id):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_id_invalid",
                "Constraint ids must use only letters, numbers, dots, underscores, and hyphens.",
            )
        normalized_id = constraint_id.casefold()
        if normalized_id in seen_ids:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_id_duplicate",
                "Constraint ids must be case-insensitively unique.",
            )
        seen_ids.add(normalized_id)

        kind = _require_string(item, "kind", f"{constraint_path}.kind")
        if kind not in DELIVERY_CONSTRAINT_KIND_VALUES:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_kind_invalid",
                "Constraint kind must be a supported inherited hard-constraint kind.",
            )
        summary = _bounded_authoring_string(item, "summary", f"{constraint_path}.summary")
        if source_task_description is not None and contains_delivery_source_excerpt(summary, source_task_description):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_summary_source_excerpt",
                "Constraint summaries must paraphrase source-task rules without copying source text.",
            )
        applies_to = _require_string_list(item, "unit_ids", f"{constraint_path}.unit_ids")
        if not applies_to:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_units_empty",
                "Each inherited hard constraint must apply to at least one unit.",
            )
        if len(applies_to) > MAX_DELIVERY_CONSTRAINT_UNIT_IDS:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_units_too_many",
                "Constraint unit_ids contains too many delivery unit references.",
            )
        if len(applies_to) != len(set(applies_to)):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_unit_duplicate",
                "Constraint unit_ids must not contain duplicates.",
            )
        if any(unit_id not in unit_ids for unit_id in applies_to):
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_unit_unknown",
                "Constraint unit_ids must reference generated delivery units only.",
            )
        disposition = _require_string(item, "disposition", f"{constraint_path}.disposition")
        if disposition not in DELIVERY_CONSTRAINT_DRAFT_DISPOSITIONS:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.constraint_disposition_invalid",
                "Constraint disposition must be preserved, needs_review, or conflict.",
            )
        constraints.append(
            DeliveryAuthoringConstraintDraft(
                id=constraint_id,
                kind=kind,
                summary=summary,
                unit_ids=applies_to,
                disposition=disposition,
            )
        )
    return constraints


def _parse_constraint_gaps(
    value: Any,
    *,
    constraints: list[DeliveryAuthoringConstraintDraft],
    unit_ids: set[str],
    source_task_description: str | None,
) -> list[DeliveryConstraintGap]:
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_constraint_verification.gaps_invalid_type",
            "constraint_gaps must be a list of actionable constraint gaps.",
        )
    if len(value) > MAX_DELIVERY_CONSTRAINTS:
        raise DeliveryAuthoringParseError(
            "delivery_constraint_verification.gaps_too_many",
            "constraint_gaps contains too many entries.",
        )

    constraints_by_id = {constraint.id.casefold(): constraint for constraint in constraints}
    gaps: list[DeliveryConstraintGap] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for index, item in enumerate(value):
        gap_path = f"constraint_gaps[{index}]"
        if not isinstance(item, dict):
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_not_object",
                "Constraint gap entries must be JSON objects.",
            )
        _reject_unknown_fields(item, _CONSTRAINT_GAP_FIELDS, gap_path)
        reason = _require_string(item, "reason", f"{gap_path}.reason")
        if reason not in DELIVERY_CONSTRAINT_GAP_REASONS:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_reason_invalid",
                "Constraint gap reason must be omitted or incompletely_assigned.",
            )
        kind = _require_string(item, "kind", f"{gap_path}.kind")
        if kind not in DELIVERY_CONSTRAINT_KIND_VALUES:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_kind_invalid",
                "Constraint gap kind must be a supported inherited hard-constraint kind.",
            )
        summary = _bounded_authoring_string(item, "summary", f"{gap_path}.summary")
        if source_task_description is not None and contains_delivery_source_excerpt(summary, source_task_description):
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_summary_source_excerpt",
                "Constraint gap summaries must paraphrase source-task rules without copying source text.",
            )
        affected_unit_ids = _require_string_list(
            item,
            "affected_unit_ids",
            f"{gap_path}.affected_unit_ids",
        )
        if not affected_unit_ids:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_units_empty",
                "Each constraint gap must identify at least one affected unit.",
            )
        if len(affected_unit_ids) > MAX_DELIVERY_CONSTRAINT_UNIT_IDS:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_units_too_many",
                "Constraint gap affected_unit_ids contains too many unit references.",
            )
        if len(affected_unit_ids) != len(set(affected_unit_ids)):
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_unit_duplicate",
                "Constraint gap affected_unit_ids must not contain duplicates.",
            )
        if any(unit_id not in unit_ids for unit_id in affected_unit_ids):
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_unit_unknown",
                "Constraint gap affected_unit_ids must reference generated delivery units only.",
            )

        constraint_id_value = item.get("constraint_id")
        constraint_id: str | None = None
        if reason == "omitted":
            if constraint_id_value is not None:
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_verification.gap_constraint_unexpected",
                    "Omitted constraint gaps must not identify an existing constraint.",
                )
        else:
            if not isinstance(constraint_id_value, str) or not constraint_id_value.strip():
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_verification.gap_constraint_required",
                    "Incompletely assigned gaps must identify an existing constraint.",
                )
            constraint_id = constraint_id_value.strip()
            existing = constraints_by_id.get(constraint_id.casefold())
            if existing is None:
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_verification.gap_constraint_unknown",
                    "Constraint gap constraint_id must reference a supplied constraint.",
                )
            if existing.id != constraint_id or existing.kind != kind or existing.summary != summary:
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_verification.gap_constraint_mismatch",
                    "An incompletely assigned gap must preserve the supplied constraint identity.",
                )
            if any(unit_id in existing.unit_ids for unit_id in affected_unit_ids):
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_verification.gap_assignment_not_missing",
                    "An incompletely assigned gap may identify only missing unit assignments.",
                )

        identity = (reason, constraint_id or f"{kind}:{summary}", tuple(affected_unit_ids))
        if identity in seen:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.gap_duplicate",
                "Constraint gaps must not contain duplicate entries.",
            )
        seen.add(identity)
        gaps.append(
            DeliveryConstraintGap(
                reason=reason,
                constraint_id=constraint_id,
                kind=kind,
                summary=summary,
                affected_unit_ids=affected_unit_ids,
            )
        )
    return gaps


def _parse_unit_context_gaps(
    value: Any,
    *,
    unit_ids: set[str],
    source_task_description: str | None,
    unit_task_markdown_by_id: dict[str, str] | None,
) -> list[DeliveryUnitContextGap]:
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.gaps_invalid_type",
            "unit_context_gaps must be a list.",
        )
    if len(value) > MAX_DELIVERY_UNIT_CONTEXT_GAPS:
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.gaps_too_many",
            "unit_context_gaps contains too many entries.",
        )
    if value and (source_task_description is None or unit_task_markdown_by_id is None):
        raise DeliveryAuthoringParseError(
            "delivery_unit_context_verification.authority_required",
            "Unit-context gaps require the authoritative source task and candidate unit Markdown.",
        )

    source_lines = {line.strip() for line in (source_task_description or "").splitlines() if line.strip()}
    gaps: list[DeliveryUnitContextGap] = []
    seen_units: set[str] = set()
    total_length = 0
    for index, item in enumerate(value):
        gap_path = f"unit_context_gaps[{index}]"
        if not isinstance(item, dict):
            raise DeliveryAuthoringParseError(
                "delivery_unit_context_verification.gap_not_object",
                "Unit-context gap entries must be JSON objects.",
            )
        _reject_unknown_fields(item, _UNIT_CONTEXT_GAP_FIELDS, gap_path)
        unit_id = _require_string(item, "unit_id", f"{gap_path}.unit_id")
        if unit_id not in unit_ids:
            raise DeliveryAuthoringParseError(
                "delivery_unit_context_verification.unit_unknown",
                "Unit-context gaps must reference generated delivery units only.",
            )
        if unit_id in seen_units:
            raise DeliveryAuthoringParseError(
                "delivery_unit_context_verification.unit_duplicate",
                "Unit-context gaps must contain at most one entry per unit.",
            )
        seen_units.add(unit_id)

        raw_literals = item.get("source_literals")
        if not isinstance(raw_literals, list) or not raw_literals:
            raise DeliveryAuthoringParseError(
                "delivery_unit_context_verification.literals_required",
                "Each unit-context gap must identify at least one missing source literal.",
            )
        if len(raw_literals) > MAX_DELIVERY_UNIT_CONTEXT_LITERALS:
            raise DeliveryAuthoringParseError(
                "delivery_unit_context_verification.literals_too_many",
                "A unit-context gap contains too many source literals.",
            )

        literals: list[str] = []
        seen_literals: set[str] = set()
        task_markdown = (unit_task_markdown_by_id or {}).get(unit_id, "")
        for literal in raw_literals:
            if (
                not isinstance(literal, str)
                or not literal
                or literal != literal.strip()
                or len(literal) > MAX_DELIVERY_UNIT_CONTEXT_LITERAL_LENGTH
                or len(literal.splitlines()) != 1
                or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in literal)
            ):
                raise DeliveryAuthoringParseError(
                    "delivery_unit_context_verification.literal_invalid",
                    "Source literals must be bounded, single-line source-task values.",
                )
            if literal not in source_lines:
                raise DeliveryAuthoringParseError(
                    "delivery_unit_context_verification.literal_not_source_line",
                    "Source literals must exactly match complete non-empty source-task lines.",
                )
            if literal in task_markdown:
                raise DeliveryAuthoringParseError(
                    "delivery_unit_context_verification.literal_already_present",
                    "Unit-context gaps may identify only source literals missing from the unit task.",
                )
            if literal in seen_literals:
                raise DeliveryAuthoringParseError(
                    "delivery_unit_context_verification.literal_duplicate",
                    "Source literals must not be duplicated within one unit-context gap.",
                )
            seen_literals.add(literal)
            total_length += len(literal)
            if total_length > MAX_DELIVERY_UNIT_CONTEXT_TOTAL_LENGTH:
                raise DeliveryAuthoringParseError(
                    "delivery_unit_context_verification.literals_too_large",
                    "Unit-context gaps contain too much source literal content.",
                )
            literals.append(literal)
        gaps.append(DeliveryUnitContextGap(unit_id=unit_id, source_literals=literals))
    return gaps


def _assessment_reason_codes(value: Any, recommended_mode: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.reason_codes_required",
            "reason_codes must be a non-empty list of stable reason codes.",
        )
    if len(value) > _MAX_DELIVERY_ASSESSMENT_REASONS:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.reason_codes_too_many",
            "reason_codes contains too many entries.",
        )

    allowed = _DELIVERY_ASSESSMENT_REASON_CODES_BY_MODE[recommended_mode]
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in DELIVERY_ASSESSMENT_REASON_CODES:
            raise DeliveryAuthoringParseError(
                "delivery_assessment.reason_code_invalid",
                "reason_codes must contain supported stable codes only.",
            )
        if item not in allowed:
            raise DeliveryAuthoringParseError(
                "delivery_assessment.reason_code_mode_mismatch",
                "reason_codes must be compatible with recommended_mode.",
            )
        if item in seen:
            raise DeliveryAuthoringParseError(
                "delivery_assessment.reason_code_duplicate",
                "reason_codes must not contain duplicates.",
            )
        seen.add(item)
        result.append(item)
    return result


def _parse_assessment_units(value: Any) -> list[DeliveryAssessmentUnitDraft]:
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_assessment.units_invalid_type",
            "units must be a list of proposed unit objects.",
        )
    if len(value) > _MAX_DELIVERY_ASSESSMENT_UNITS:
        raise DeliveryAuthoringParseError(
            "delivery_assessment.units_too_many",
            "units contains too many proposed units.",
        )

    units: list[DeliveryAssessmentUnitDraft] = []
    seen: set[str] = set()
    seen_casefolded: set[str] = set()
    for idx, item in enumerate(value):
        unit_path = f"units[{idx}]"
        if not isinstance(item, dict):
            raise DeliveryAuthoringParseError(
                "delivery_assessment.unit_not_object",
                "Assessment unit entries must be JSON objects.",
            )
        _reject_unknown_fields(item, _ASSESSMENT_UNIT_FIELDS, unit_path)
        unit_id = _require_string(item, "id", f"{unit_path}.id")
        _validate_unit_id(unit_id, f"{unit_path}.id")
        if unit_id in seen or unit_id.casefold() in seen_casefolded:
            raise DeliveryAuthoringParseError(
                "delivery_assessment.unit_id_duplicate",
                "Assessment unit IDs must be case-insensitively unique.",
            )
        seen.add(unit_id)
        seen_casefolded.add(unit_id.casefold())
        units.append(
            DeliveryAssessmentUnitDraft(
                id=unit_id,
                title=_bounded_assessment_string(item, "title", f"{unit_path}.title"),
                depends_on=_require_string_list(item, "depends_on", f"{unit_path}.depends_on"),
                stream=_optional_bounded_assessment_string(item, "stream", f"{unit_path}.stream"),
                component=_optional_bounded_assessment_string(item, "component", f"{unit_path}.component"),
                platform=_optional_bounded_assessment_string(item, "platform", f"{unit_path}.platform"),
            )
        )
    _validate_assessment_dependencies(units)
    return units


def _bounded_assessment_string(data: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(data, key, path)
    if not is_safe_delivery_public_metadata(value):
        raise DeliveryAuthoringParseError(
            "delivery_assessment.label_invalid",
            f"{path} must be bounded single-line delivery assessment metadata without absolute paths.",
        )
    return value


def _optional_bounded_assessment_string(data: dict[str, Any], key: str, path: str) -> str | None:
    value = _optional_string(data, key, path)
    if value is not None and not is_safe_delivery_public_metadata(value):
        raise DeliveryAuthoringParseError(
            "delivery_assessment.label_invalid",
            f"{path} must be bounded single-line delivery assessment metadata without absolute paths.",
        )
    return value


def _bounded_authoring_string(data: dict[str, Any], key: str, path: str) -> str:
    value = _require_string(data, key, path)
    if not is_safe_delivery_public_metadata(value):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.label_invalid",
            f"{path} must be bounded single-line delivery metadata without absolute paths.",
        )
    return value


def _optional_bounded_authoring_string(data: dict[str, Any], key: str, path: str) -> str | None:
    value = _optional_string(data, key, path)
    if value is not None and not is_safe_delivery_public_metadata(value):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.label_invalid",
            f"{path} must be bounded single-line delivery metadata without absolute paths.",
        )
    return value


def _validate_assessment_dependencies(units: list[DeliveryAssessmentUnitDraft]) -> None:
    unit_ids = {unit.id for unit in units}
    deps_by_id: dict[str, list[str]] = {}
    for unit in units:
        seen: set[str] = set()
        deps_by_id[unit.id] = []
        for dependency in unit.depends_on:
            if dependency in seen:
                raise DeliveryAuthoringParseError(
                    "delivery_assessment.dependency_duplicate",
                    "depends_on must not contain duplicate dependencies.",
                )
            seen.add(dependency)
            if dependency == unit.id:
                raise DeliveryAuthoringParseError(
                    "delivery_assessment.dependency_self",
                    "Assessment units cannot depend on themselves.",
                )
            if dependency not in unit_ids:
                raise DeliveryAuthoringParseError(
                    "delivery_assessment.dependency_unknown",
                    "depends_on must reference known assessment unit IDs only.",
                )
            deps_by_id[unit.id].append(dependency)
    if _find_dependency_cycle(deps_by_id):
        raise DeliveryAuthoringParseError(
            "delivery_assessment.dependency_cycle",
            "Assessment unit dependencies must not contain cycles.",
        )


def _reject_unknown_fields(data: dict[str, Any], allowed_fields: set[str], location: str) -> None:
    unknown_fields = set(data) - allowed_fields
    if unknown_fields:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unknown_field",
            f"Delivery authoring draft contains an unsupported {location} field.",
        )


def _reject_unit_unknown_fields(data: dict[str, Any], unit_path: str, *, allow_asset_paths: bool) -> None:
    path_fields = sorted(set(data) & _UNIT_PATH_FIELDS)
    if path_fields:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_path_field_forbidden",
            "Delivery unit drafts must not include writer-facing path fields.",
        )
    allowed_fields = _UNIT_FIELDS if allow_asset_paths else _UNIT_FIELDS - {"asset_paths"}
    _reject_unknown_fields(data, allowed_fields, unit_path)


def _require_string(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAuthoringParseError(
            "delivery_authoring.string_required",
            f"{path} must be a non-empty string.",
        )
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, path: str) -> str | None:
    if key not in data:
        return None
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAuthoringParseError(
            "delivery_authoring.string_required",
            f"{path} must be a non-empty string when present.",
        )
    return value.strip()


def _optional_planning_mode(data: dict[str, Any], key: str, path: str) -> str | None:
    value = _optional_string(data, key, path)
    if value is None:
        return None
    if value not in _PLANNING_MODES:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.planning_mode_invalid",
            f"{path} must be a supported planning mode.",
        )
    return value


def _require_string_list(data: dict[str, Any], key: str, path: str) -> list[str]:
    if key not in data:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.string_list_required",
            f"{path} must be a list of non-empty strings.",
        )
    return _string_list(data.get(key), path)


def _optional_string_list(data: dict[str, Any], key: str, path: str) -> list[str]:
    if key not in data:
        return []
    return _string_list(data.get(key), path)


def _optional_estimated_size(data: dict[str, Any], key: str, path: str) -> str | None:
    value = _optional_string(data, key, path)
    if value is None:
        return None
    if value not in DELIVERY_UNIT_SIZE_VALUES:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.estimated_size_invalid",
            f"{path} must be one of: {', '.join(DELIVERY_UNIT_SIZE_VALUES)}.",
        )
    return value


def _optional_risk_tags(data: dict[str, Any], key: str, path: str) -> list[str]:
    if key not in data:
        return []
    value = data.get(key)
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.risk_tags_invalid_type",
            f"{path} must be a list of supported risk tag strings.",
        )
    result: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, str) or not item.strip():
            raise DeliveryAuthoringParseError(
                "delivery_authoring.risk_tag_invalid",
                f"{item_path} must be a non-empty risk tag string.",
            )
        tag = item.strip()
        if tag not in DELIVERY_UNIT_RISK_TAG_VALUES:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.risk_tag_unknown",
                f"{item_path} must be a supported delivery unit risk tag.",
            )
        if tag in seen:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.risk_tag_duplicate",
                f"{item_path} duplicates a previous risk tag.",
            )
        seen.add(tag)
        result.append(tag)
    return result


def _optional_budget(data: dict[str, Any], key: str, path: str) -> DeliveryUnitBudget | None:
    if key not in data:
        return DeliveryUnitBudget(max_planner_steps=DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS)
    value = data.get(key)
    if not isinstance(value, dict):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.budget_invalid_type",
            f"{path} must be an object with supported positive integer budget fields.",
        )

    unknown_fields = sorted(set(value) - set(DELIVERY_UNIT_BUDGET_FIELDS))
    if unknown_fields:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.budget_unknown_field",
            f"{path} contains an unsupported budget field.",
        )

    kwargs: dict[str, int] = {}
    for field_name in DELIVERY_UNIT_BUDGET_FIELDS:
        if field_name not in value:
            continue
        field_value = value[field_name]
        if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 1:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.budget_value_invalid",
                f"{path}.{field_name} must be a positive integer.",
            )
        if field_name == "max_planner_steps" and field_value > MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.planner_step_budget_invalid",
                f"{path}.max_planner_steps must be 1 or 2; three or more planner steps require a split.",
            )
        kwargs[field_name] = field_value
    kwargs.setdefault("max_planner_steps", DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS)
    return DeliveryUnitBudget(**kwargs)


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.string_list_required",
            f"{path} must be a list of non-empty strings.",
        )
    result: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise DeliveryAuthoringParseError(
                "delivery_authoring.string_list_item_invalid",
                f"{path}[{idx}] must be a non-empty string.",
            )
        value_item = item.strip()
        result.append(value_item)
    return result


def _validate_unit_id(unit_id: str, path: str) -> None:
    if (
        not unit_id
        or len(unit_id) > MAX_DELIVERY_UNIT_ID_LENGTH
        or unit_id in {".", ".."}
        or "/" in unit_id
        or "\\" in unit_id
        or Path(unit_id).is_absolute()
        or PureWindowsPath(unit_id).is_absolute()
        or PureWindowsPath(unit_id).drive
        or not _DELIVERY_AUTHORING_ID_RE.fullmatch(unit_id)
    ):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_id_invalid",
            f"{path} must be a stable path-safe delivery unit id.",
        )


def _optional_scope_paths(data: dict[str, Any], key: str, path: str, project_root: Path) -> list[str]:
    if key not in data:
        return []
    value = data.get(key)
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.scope_paths_invalid_type",
            f"{path} must be a list of project-relative paths.",
        )
    result: list[str] = []
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, str) or not item.strip():
            raise DeliveryAuthoringParseError(
                "delivery_authoring.scope_path_invalid",
                f"{item_path} must be a non-empty project-relative path.",
            )
        normalized = _validate_scope_path(item.strip(), project_root, item_path)
        result.append(normalized)
    return result


def _validate_scope_path(path_value: str, project_root: Path, path: str) -> str:
    windows_path = PureWindowsPath(path_value)
    posix_path = PurePosixPath(path_value)
    if (
        Path(path_value).is_absolute()
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
    ):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.scope_path_absolute",
            f"{path} must be project-relative.",
        )
    if _has_parent_traversal(path_value):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.scope_path_outside_project",
            f"{path} must not contain parent-directory traversal.",
        )
    normalized_path = _canonical_scope_path(path_value)
    try:
        resolved = (project_root / normalized_path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.scope_path_invalid",
            f"{path} must be a valid project-relative path.",
        ) from exc
    _require_path_within_project(resolved, project_root, code="delivery_authoring.scope_path_outside_project")
    return _project_relative_path(resolved, project_root)


def _optional_asset_paths(data: dict[str, Any], key: str, path: str) -> list[str]:
    if key not in data:
        return []
    value = data.get(key)
    if not isinstance(value, list):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.asset_paths_invalid_type",
            f"{path} must be a list of declared source asset paths.",
        )
    result: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, str) or not item.strip():
            raise DeliveryAuthoringParseError(
                "delivery_authoring.asset_path_invalid",
                f"{item_path} must be a non-empty declared source asset path.",
            )
        alias = item.strip()
        if len(alias) > _MAX_ASSET_ASSIGNMENT_ALIAS_LENGTH or "\n" in alias or "\r" in alias or "\x00" in alias:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.asset_path_invalid",
                f"{item_path} must be a bounded single-line declared source asset path.",
            )
        windows_alias = PureWindowsPath(alias)
        normalized = windows_alias.as_posix()
        if normalized in seen:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.asset_path_duplicate",
                f"{item_path} duplicates a previous asset path.",
            )
        seen.add(normalized)
        result.append(alias if windows_alias.is_absolute() else normalized)
    return result


def _has_parent_traversal(path_value: str) -> bool:
    return ".." in PurePosixPath(path_value).parts or ".." in PureWindowsPath(path_value).parts


def _canonical_scope_path(path_value: str) -> str:
    return PureWindowsPath(path_value).as_posix()


def _validate_dependencies(units: list[DeliveryAuthoringUnitDraft]) -> None:
    unit_ids = {unit.id for unit in units}
    deps_by_id: dict[str, list[str]] = {}
    for unit in units:
        seen: set[str] = set()
        deps_by_id[unit.id] = []
        for dependency in unit.depends_on:
            if dependency in seen:
                raise DeliveryAuthoringParseError(
                    "delivery_authoring.dependency_duplicate",
                    "depends_on must not contain duplicate dependencies.",
                )
            seen.add(dependency)
            if dependency == unit.id:
                raise DeliveryAuthoringParseError(
                    "delivery_authoring.dependency_self",
                    "Delivery units cannot depend on themselves.",
                )
            if dependency not in unit_ids:
                raise DeliveryAuthoringParseError(
                    "delivery_authoring.dependency_unknown",
                    "depends_on must reference known delivery unit IDs only.",
                )
            deps_by_id[unit.id].append(dependency)

    cycle = _find_dependency_cycle(deps_by_id)
    if cycle:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.dependency_cycle",
            "Delivery unit dependencies must not contain cycles.",
        )


def _find_dependency_cycle(deps_by_id: dict[str, list[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(unit_id: str) -> list[str]:
        if unit_id in visiting:
            start = stack.index(unit_id)
            return stack[start:] + [unit_id]
        if unit_id in visited:
            return []
        visiting.add(unit_id)
        stack.append(unit_id)
        for dependency in deps_by_id.get(unit_id, []):
            cycle = visit(dependency)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(unit_id)
        visited.add(unit_id)
        return []

    for unit_id in deps_by_id:
        cycle = visit(unit_id)
        if cycle:
            return cycle
    return []


def _validate_unit_task_markdown(task_markdown: str, *, allow_asset_manifest: bool = False) -> None:
    if "sikula:generated-" in task_markdown:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_markdown_generated_marker",
            "Unit task Markdown must not include Sikula generated-answer markers.",
        )

    sections = _scan_markdown_sections(task_markdown)
    if not allow_asset_manifest and any(
        section.heading.normalized == _ASSET_MANIFEST_HEADING for section in sections if not section.is_title
    ):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_markdown_asset_manifest",
            "Unit task Markdown must not include an Asset manifest section.",
        )

    missing_section = _first_missing_required_section(sections)
    if missing_section:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_markdown_missing_section",
            f"Unit task Markdown must include a non-empty {missing_section} section.",
        )

    verification_content = _required_section_content(
        sections,
        _REQUIRED_UNIT_MARKDOWN_SECTIONS["verification"],
    )
    if not extract_validation_commands(verification_content):
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_markdown_missing_verification_commands",
            "Unit task Markdown must include at least one explicit verification command.",
        )


@dataclass(frozen=True)
class _MarkdownSection:
    heading: MarkdownHeading
    content: str

    @property
    def is_title(self) -> bool:
        return self.heading.is_document_title


def _scan_markdown_sections(markdown: str) -> list[_MarkdownSection]:
    lines = markdown.splitlines()
    scanner = MarkdownHeadingScanner(ignore_fenced_blocks=True)
    headings: list[tuple[int, MarkdownHeading]] = []
    for idx, line in enumerate(lines):
        heading = scanner.match(line)
        if heading is not None:
            headings.append((idx, heading))

    sections: list[_MarkdownSection] = []
    for idx, (line_idx, heading) in enumerate(headings):
        end_idx = len(lines)
        for next_line_idx, next_heading in headings[idx + 1 :]:
            if _heading_ends_section(heading, next_heading):
                end_idx = next_line_idx
                break
        sections.append(
            _MarkdownSection(
                heading=heading,
                content="\n".join(lines[line_idx + 1 : end_idx]).strip(),
            )
        )
    return sections


def _heading_ends_section(current: MarkdownHeading, next_heading: MarkdownHeading) -> bool:
    if current.is_document_title:
        return True
    if current.is_text:
        return True
    if next_heading.is_text:
        return True
    return next_heading.level <= current.level


def _first_missing_required_section(sections: list[_MarkdownSection]) -> str | None:
    for section_name, aliases in _REQUIRED_UNIT_MARKDOWN_SECTIONS.items():
        if not _required_section_content(sections, aliases):
            return section_name.replace("_", " ")
    return None


def _required_section_content(sections: list[_MarkdownSection], aliases: set[str]) -> str:
    normalized_aliases = {normalize_heading(alias) for alias in aliases}
    return "\n".join(
        section.content
        for section in sections
        if not section.is_title and section.heading.normalized in normalized_aliases
    ).strip()


def _resolve_project_root(project_root: str | Path) -> Path:
    try:
        return Path(project_root).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.project_root_invalid",
            "Project root must be a valid filesystem path.",
        ) from exc


def _resolve_project_relative_path(path: str | Path, project_root: Path) -> Path:
    try:
        raw_path = Path(path)
        if raw_path.is_absolute():
            return raw_path.resolve()
        return (project_root / raw_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.path_invalid",
            "Delivery authoring path must be a valid filesystem path.",
        ) from exc


def _require_path_within_project(path: Path, project_root: Path, *, code: str) -> None:
    if not _path_is_within(path, project_root):
        raise DeliveryAuthoringParseError(
            code,
            "Delivery authoring paths must resolve inside the project root.",
        )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _project_relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.path_outside_project",
            "Delivery authoring paths must resolve inside the project root.",
        ) from exc
