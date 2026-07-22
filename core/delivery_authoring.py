from __future__ import annotations

from dataclasses import dataclass, field
import json
from json import JSONDecodeError
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

from core.delivery_unit_metadata import (
    DEFAULT_DELIVERY_UNIT_MAX_PLANNER_STEPS,
    DELIVERY_UNIT_BUDGET_FIELDS,
    DELIVERY_UNIT_RISK_TAG_VALUES,
    DELIVERY_UNIT_SIZE_VALUES,
    MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS,
    DeliveryUnitBudget,
)
from core.markdown_headings import MarkdownHeading, MarkdownHeadingScanner, normalize_heading
from core.validation_coverage import extract_validation_commands

_DELIVERY_AUTHORING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLANNING_MODES = {"fixed_window"}
_FENCED_JSON_RE = re.compile(
    r"^\s*(?P<fence>`{3,}|~{3,})[ \t]*json[ \t]*\r?\n(?P<content>.*)\r?\n(?P=fence)[ \t]*\s*$",
    re.DOTALL | re.IGNORECASE,
)
_TOP_LEVEL_FIELDS = {"plan_id", "title", "units", "planning_mode", "warnings"}
_AMENDMENT_TOP_LEVEL_FIELDS = {
    "plan_id",
    "target_unit_id",
    "replacement_units",
    "amend_reason",
    "budget_exceeded",
    "warnings",
}
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
    "estimated_size",
    "risk_tags",
    "budget",
}
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
    estimated_size: str | None = None
    risk_tags: list[str] = field(default_factory=list)
    budget: DeliveryUnitBudget | None = None


@dataclass
class DeliveryAuthoringDraft:
    plan_id: str
    title: str
    units: list[DeliveryAuthoringUnitDraft]
    planning_mode: str | None = None
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DeliveryAmendmentAuthoringDraft:
    plan_id: str
    target_unit_id: str
    replacement_units: list[DeliveryAuthoringUnitDraft]
    amend_reason: str | None = None
    budget_exceeded: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    audit_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DeliveryAuthoringDerivedPaths:
    plan_file: str
    units_dir: str
    unit_task_paths: dict[str, str]


class DeliveryAuthoringParseError(ValueError):
    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_delivery_authoring_output(
    output: str,
    *,
    expected_plan_id: str,
    project_root: str | Path,
    output_dir: str | Path,
) -> DeliveryAuthoringDraft:
    root = _resolve_project_root(project_root)
    selected_plan_id = _expected_plan_id(expected_plan_id, output_dir=output_dir, project_root=root)
    data = _parse_output_object(output)
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

    title = _require_string(data, "title", "title")
    planning_mode = _optional_planning_mode(data, "planning_mode", "planning_mode")
    warnings = _optional_string_list(data, "warnings", "warnings")
    units = _parse_units(data.get("units"), project_root=root)

    return DeliveryAuthoringDraft(
        plan_id=plan_id,
        title=title,
        units=units,
        planning_mode=planning_mode,
        warnings=warnings,
    )


def parse_delivery_amendment_authoring_output(
    output: str,
    *,
    expected_plan_id: str,
    expected_target_unit_id: str,
    project_root: str | Path,
) -> DeliveryAmendmentAuthoringDraft:
    root = _resolve_project_root(project_root)
    data = _parse_output_object(output)
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
    replacement_units = _parse_units(data.get("replacement_units"), project_root=root)
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


def _parse_output_object(output: str) -> dict[str, Any]:
    if not isinstance(output, str) or not output.strip():
        raise DeliveryAuthoringParseError(
            "delivery_authoring.empty_output",
            "Delivery authoring output must be a non-empty JSON object.",
        )

    stripped = output.strip()
    fence_match = _FENCED_JSON_RE.fullmatch(stripped)
    if stripped.startswith(("```", "~~~")):
        if fence_match is None:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.output_invalid_envelope",
                "Delivery authoring output must be exactly one fenced json block or one JSON object.",
            )
        json_text = fence_match.group("content").strip()
    else:
        json_text = stripped

    try:
        value = json.loads(
            json_text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (JSONDecodeError, ValueError) as exc:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.json_invalid",
            "Delivery authoring output must contain exactly one valid JSON object.",
        ) from exc

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


def _parse_units(value: Any, *, project_root: Path) -> list[DeliveryAuthoringUnitDraft]:
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
        _reject_unit_unknown_fields(item, unit_path)

        unit_id = _require_string(item, "id", f"{unit_path}.id")
        _validate_unit_id(unit_id, f"{unit_path}.id")
        if unit_id in seen:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.unit_id_duplicate",
                "Unit IDs must be unique.",
            )
        seen.add(unit_id)

        title = _require_string(item, "title", f"{unit_path}.title")
        depends_on = _require_string_list(item, "depends_on", f"{unit_path}.depends_on")
        task_markdown = _require_string(item, "task_markdown", f"{unit_path}.task_markdown")
        _validate_unit_task_markdown(task_markdown)

        units.append(
            DeliveryAuthoringUnitDraft(
                id=unit_id,
                title=title,
                depends_on=depends_on,
                task_markdown=task_markdown,
                stream=_optional_string(item, "stream", f"{unit_path}.stream"),
                component=_optional_string(item, "component", f"{unit_path}.component"),
                phase=_optional_string(item, "phase", f"{unit_path}.phase"),
                kind=_optional_string(item, "kind", f"{unit_path}.kind"),
                platform=_optional_string(item, "platform", f"{unit_path}.platform"),
                scope_paths=_optional_scope_paths(item, "scope_paths", f"{unit_path}.scope_paths", project_root),
                estimated_size=_optional_estimated_size(item, "estimated_size", f"{unit_path}.estimated_size"),
                risk_tags=_optional_risk_tags(item, "risk_tags", f"{unit_path}.risk_tags"),
                budget=_optional_budget(item, "budget", f"{unit_path}.budget"),
            )
        )

    _validate_dependencies(units)
    return units


def _reject_unknown_fields(data: dict[str, Any], allowed_fields: set[str], location: str) -> None:
    unknown_fields = set(data) - allowed_fields
    if unknown_fields:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unknown_field",
            f"Delivery authoring draft contains an unsupported {location} field.",
        )


def _reject_unit_unknown_fields(data: dict[str, Any], unit_path: str) -> None:
    path_fields = sorted(set(data) & _UNIT_PATH_FIELDS)
    if path_fields:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_path_field_forbidden",
            "Delivery unit drafts must not include writer-facing path fields.",
        )
    _reject_unknown_fields(data, _UNIT_FIELDS, unit_path)


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


def _validate_unit_task_markdown(task_markdown: str) -> None:
    if "sikula:generated-" in task_markdown:
        raise DeliveryAuthoringParseError(
            "delivery_authoring.unit_markdown_generated_marker",
            "Unit task Markdown must not include Sikula generated-answer markers.",
        )

    sections = _scan_markdown_sections(task_markdown)
    if any(section.heading.normalized == _ASSET_MANIFEST_HEADING for section in sections if not section.is_title):
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
