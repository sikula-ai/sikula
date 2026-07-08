from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
from typing import Any

import yaml

from core.delivery_unit_metadata import (
    DELIVERY_UNIT_BUDGET_FIELDS,
    DELIVERY_UNIT_RISK_TAG_VALUES,
    DELIVERY_UNIT_SIZE_VALUES,
    DELIVERY_UNIT_SPLIT_RISK_TAGS,
    DeliveryUnitBudget,
)

SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION = 1
_DELIVERY_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_REF_FORBIDDEN_CHARS_RE = re.compile(r"[\000-\037\177 ~^:?*\[\\]")
_SPLIT_RECOMMENDED_TAG_SET = frozenset({"external_execution_boundary", "structured_output_contract", "cli_surface"})


def delivery_final_branch_for_plan_id(plan_id: str) -> str:
    return f"sikula/delivery/{plan_id}"


def is_valid_delivery_branch_name(branch: str) -> bool:
    if not branch or branch == "@" or branch == "HEAD":
        return False
    if (
        branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or _GIT_REF_FORBIDDEN_CHARS_RE.search(branch)
    ):
        return False
    for part in branch.split("/"):
        if not part or part.startswith(".") or part.casefold().endswith(".lock"):
            return False
    return True


@dataclass(frozen=True)
class DeliveryPlanIssue:
    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            data["path"] = self.path
        return data


@dataclass(frozen=True)
class DeliveryRepository:
    id: str
    root: str = "."
    implicit: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "root": self.root,
        }
        if self.implicit:
            data["implicit"] = True
        return data


@dataclass(frozen=True)
class DeliveryComponent:
    id: str
    path: str
    label: str | None = None
    stream: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
        }
        for key in ("label", "stream"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


@dataclass(frozen=True)
class DeliveryPlanUnit:
    id: str
    title: str | None
    task_path: str
    depends_on: list[str] = field(default_factory=list)
    stream: str | None = None
    platform: str | None = None
    phase: str | None = None
    kind: str | None = None
    repo_id: str | None = None
    component: str | None = None
    scope_paths: list[str] = field(default_factory=list)
    estimated_size: str | None = None
    risk_tags: list[str] = field(default_factory=list)
    budget: DeliveryUnitBudget | None = None
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "task_path": self.task_path,
            "depends_on": list(self.depends_on),
        }
        for key in ("title", "stream", "platform", "phase", "kind", "repo_id", "component"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.scope_paths:
            data["scope_paths"] = list(self.scope_paths)
        if self.estimated_size:
            data["estimated_size"] = self.estimated_size
        if self.risk_tags:
            data["risk_tags"] = list(self.risk_tags)
        if self.budget:
            budget_data = self.budget.to_dict()
            if budget_data:
                data["budget"] = budget_data
        return data


@dataclass(frozen=True)
class DeliveryPlan:
    schema_version: int
    plan_id: str
    title: str
    final_branch: str
    units: list[DeliveryPlanUnit]
    repositories: list[DeliveryRepository]
    stream_ids: list[str] = field(default_factory=list)
    components: list[DeliveryComponent] = field(default_factory=list)
    planning_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "title": self.title,
            "final_branch": self.final_branch,
            "repositories": [repo.to_dict() for repo in self.repositories],
            "units": [unit.to_dict() for unit in self.units],
        }
        if self.stream_ids:
            data["streams"] = list(self.stream_ids)
        if self.components:
            data["components"] = [component.to_dict() for component in self.components]
        if self.planning_mode:
            data["planning_mode"] = self.planning_mode
        return data


@dataclass(frozen=True)
class DeliveryPlanCheckResult:
    plan_path: str
    project_root: str | None
    errors: list[DeliveryPlanIssue]
    warnings: list[DeliveryPlanIssue]
    plan: DeliveryPlan | None = None

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "plan_path": self.plan_path,
            "project_root": self.project_root,
            "valid": self.valid,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }
        if self.plan:
            data["plan"] = self.plan.to_dict()
        return data


def check_delivery_plan_file(path: str | Path, *, project_root: Path | None = None) -> DeliveryPlanCheckResult:
    plan_path = Path(path).expanduser()
    if not plan_path.is_absolute():
        plan_path = (Path.cwd() / plan_path).resolve()
    else:
        plan_path = plan_path.resolve()

    errors: list[DeliveryPlanIssue] = []
    warnings: list[DeliveryPlanIssue] = []

    resolved_project_root = project_root.resolve() if project_root else _find_git_root(plan_path.parent)
    if resolved_project_root is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "project.git_root_missing",
                "Delivery plan MVP requires the plan file to live inside one Git repository.",
            )
        )
        resolved_project_root = plan_path.parent
    elif not _path_is_within(plan_path, resolved_project_root):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "plan.path_outside_project",
                "Delivery plan file is outside the detected project Git root.",
            )
        )

    data = _load_plan_yaml(plan_path, errors)
    plan: DeliveryPlan | None = None
    if isinstance(data, dict):
        plan = _parse_delivery_plan(data, project_root=resolved_project_root, errors=errors, warnings=warnings)

    return DeliveryPlanCheckResult(
        plan_path=str(plan_path),
        project_root=str(resolved_project_root) if resolved_project_root else None,
        errors=errors,
        warnings=warnings,
        plan=plan,
    )


def render_delivery_plan_check(result: DeliveryPlanCheckResult) -> str:
    lines = [
        f"Delivery plan check: {result.plan_path}",
        f"Status: {'valid' if result.valid else 'invalid'}",
    ]
    if result.project_root:
        lines.append(f"Project root: {result.project_root}")
    if result.plan:
        lines.extend(
            [
                f"Plan ID: {result.plan.plan_id}",
                f"Title: {result.plan.title}",
                f"Final branch: {result.plan.final_branch}",
                f"Units: {len(result.plan.units)}",
            ]
        )
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for issue in result.errors:
            lines.append(_format_issue(issue))
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for issue in result.warnings:
            lines.append(_format_issue(issue))
    if result.valid:
        lines.append("")
        lines.append("Ready for delivery plan MVP.")
    return "\n".join(lines) + "\n"


def _format_issue(issue: DeliveryPlanIssue) -> str:
    location = f" [{issue.path}]" if issue.path else ""
    return f"- {issue.code}{location}: {issue.message}"


def _load_plan_yaml(path: Path, errors: list[DeliveryPlanIssue]) -> Any:
    if not path.exists():
        errors.append(DeliveryPlanIssue("error", "plan.missing", f"Plan file not found: {path}"))
        return None
    if not path.is_file():
        errors.append(DeliveryPlanIssue("error", "plan.not_file", f"Plan path is not a file: {path}"))
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(DeliveryPlanIssue("error", "plan.read_failed", f"Failed to read plan file: {exc}"))
        return None
    except yaml.YAMLError as exc:
        errors.append(DeliveryPlanIssue("error", "plan.parse_failed", _safe_yaml_error_message(exc)))
        return None
    if not isinstance(data, dict):
        errors.append(DeliveryPlanIssue("error", "plan.invalid_type", "Delivery plan must be a YAML mapping."))
        return None
    return data


def _safe_yaml_error_message(exc: yaml.YAMLError) -> str:
    message = f"Failed to parse plan YAML ({type(exc).__name__})"
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None:
        line = getattr(mark, "line", None)
        column = getattr(mark, "column", None)
        if isinstance(line, int) and isinstance(column, int):
            message += f" at line {line + 1}, column {column + 1}"
        elif isinstance(line, int):
            message += f" at line {line + 1}"
    return message + "."


def _parse_delivery_plan(
    data: dict[str, Any],
    *,
    project_root: Path,
    errors: list[DeliveryPlanIssue],
    warnings: list[DeliveryPlanIssue],
) -> DeliveryPlan | None:
    schema_version = _require_int(data, "schema_version", "schema_version", errors)
    if schema_version is not None and schema_version != SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "schema_version.unsupported",
                (
                    f"Unsupported delivery plan schema_version {schema_version}; "
                    f"expected {SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION}."
                ),
                "schema_version",
            )
        )

    plan_id = _require_string(data, "plan_id", "plan_id", errors)
    if plan_id and not _DELIVERY_PLAN_ID_RE.fullmatch(plan_id):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "plan_id.invalid",
                "plan_id may contain only letters, numbers, dots, underscores, and hyphens.",
                "plan_id",
            )
        )
    title = _require_string(data, "title", "title", errors)
    final_branch = _require_string(data, "final_branch", "final_branch", errors)
    if final_branch and not is_valid_delivery_branch_name(final_branch):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "final_branch.invalid",
                "final_branch must be a valid local branch name.",
                "final_branch",
            )
        )
    planning_mode = _optional_string(data, "planning_mode", "planning_mode", errors)
    repositories = _parse_repositories(data.get("repositories"), errors)
    repo_ids = {repo.id for repo in repositories}
    stream_ids = _parse_streams(data.get("streams"), errors)
    components = _parse_components(
        data.get("components"),
        project_root=project_root,
        stream_ids=set(stream_ids),
        errors=errors,
    )
    units = _parse_units(
        data.get("units"),
        project_root=project_root,
        repo_ids=repo_ids,
        stream_ids=set(stream_ids),
        component_ids={component.id for component in components},
        errors=errors,
        warnings=warnings,
    )

    if units:
        _validate_dependencies(units, errors)

    if schema_version is None or plan_id is None or title is None or final_branch is None:
        return None
    return DeliveryPlan(
        schema_version=schema_version,
        plan_id=plan_id,
        title=title,
        final_branch=final_branch,
        units=units,
        repositories=repositories,
        stream_ids=stream_ids,
        components=components,
        planning_mode=planning_mode,
    )


def _parse_repositories(value: Any, errors: list[DeliveryPlanIssue]) -> list[DeliveryRepository]:
    if value is None:
        return [DeliveryRepository(id="main", root=".", implicit=True)]
    if not isinstance(value, list):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "repositories.invalid_type",
                "repositories must be a list; delivery plan MVP supports at most one repository.",
                "repositories",
            )
        )
        return [DeliveryRepository(id="main", root=".", implicit=True)]
    if not value:
        errors.append(
            DeliveryPlanIssue(
                "error", "repositories.empty", "repositories must include one repository.", "repositories"
            )
        )
        return [DeliveryRepository(id="main", root=".", implicit=True)]
    if len(value) > 1:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "repositories.multiple_unsupported",
                "Delivery plan MVP supports one Git repository only; multi-repo plans are post-MVP.",
                "repositories",
            )
        )

    item = value[0]
    if not isinstance(item, dict):
        errors.append(
            DeliveryPlanIssue(
                "error", "repositories.item_invalid", "repository entries must be mappings.", "repositories[0]"
            )
        )
        return [DeliveryRepository(id="main", root=".", implicit=True)]

    repo_id = _require_string(item, "id", "repositories[0].id", errors) or "main"
    root = _optional_string(item, "root", "repositories[0].root", errors) or "."
    if root != ".":
        errors.append(
            DeliveryPlanIssue(
                "error",
                "repositories.root_unsupported",
                "Delivery plan MVP supports only repository root '.'.",
                "repositories[0].root",
            )
        )
    return [DeliveryRepository(id=repo_id, root=root)]


def _parse_streams(value: Any, errors: list[DeliveryPlanIssue]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(DeliveryPlanIssue("error", "streams.invalid_type", "streams must be a list.", "streams"))
        return []
    seen: set[str] = set()
    stream_ids: list[str] = []
    for idx, item in enumerate(value):
        path = f"streams[{idx}]"
        if isinstance(item, str):
            stream_id = item.strip()
        elif isinstance(item, dict):
            stream_id = _require_string(item, "id", f"{path}.id", errors) or ""
        else:
            errors.append(DeliveryPlanIssue("error", "streams.item_invalid", "stream entries must be mappings.", path))
            continue
        if not stream_id:
            continue
        if stream_id in seen:
            errors.append(DeliveryPlanIssue("error", "streams.duplicate_id", f"Duplicate stream id: {stream_id}", path))
            continue
        seen.add(stream_id)
        stream_ids.append(stream_id)
    return stream_ids


def _parse_components(
    value: Any,
    *,
    project_root: Path,
    stream_ids: set[str],
    errors: list[DeliveryPlanIssue],
) -> list[DeliveryComponent]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(DeliveryPlanIssue("error", "components.invalid_type", "components must be a list.", "components"))
        return []

    seen: set[str] = set()
    components: list[DeliveryComponent] = []
    for idx, item in enumerate(value):
        component_path = f"components[{idx}]"
        if not isinstance(item, dict):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "components.item_invalid",
                    "component entries must be mappings.",
                    component_path,
                )
            )
            continue
        component_id = _require_string(item, "id", f"{component_path}.id", errors)
        path = _require_string(item, "path", f"{component_path}.path", errors)
        label = _optional_string(item, "label", f"{component_path}.label", errors)
        stream = _optional_string(item, "stream", f"{component_path}.stream", errors)

        if component_id:
            if component_id in seen:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "components.duplicate_id",
                        f"Duplicate component id: {component_id}",
                        f"{component_path}.id",
                    )
                )
            seen.add(component_id)
        if path:
            _validate_project_relative_metadata_path(
                path,
                project_root=project_root,
                errors=errors,
                path=f"{component_path}.path",
                code_prefix="components.path",
                subject="Component path",
            )
        if stream_ids and stream and stream not in stream_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "components.stream_unknown",
                    f"Component references unknown stream: {stream}",
                    f"{component_path}.stream",
                )
            )
        if component_id and path:
            components.append(DeliveryComponent(id=component_id, path=path, label=label, stream=stream))
    return components


def _parse_units(
    value: Any,
    *,
    project_root: Path,
    repo_ids: set[str],
    stream_ids: set[str],
    component_ids: set[str],
    errors: list[DeliveryPlanIssue],
    warnings: list[DeliveryPlanIssue],
) -> list[DeliveryPlanUnit]:
    if not isinstance(value, list):
        errors.append(DeliveryPlanIssue("error", "units.invalid_type", "units must be a non-empty list.", "units"))
        return []
    if not value:
        errors.append(
            DeliveryPlanIssue("error", "units.empty", "units must include at least one delivery unit.", "units")
        )
        return []

    seen: set[str] = set()
    units: list[DeliveryPlanUnit] = []
    for idx, item in enumerate(value):
        unit_path = f"units[{idx}]"
        if not isinstance(item, dict):
            errors.append(DeliveryPlanIssue("error", "units.item_invalid", "unit entries must be mappings.", unit_path))
            continue
        unit_id = _require_string(item, "id", f"{unit_path}.id", errors)
        title = _optional_string(item, "title", f"{unit_path}.title", errors)
        task_path = _require_string(item, "task_path", f"{unit_path}.task_path", errors)
        depends_on = _optional_string_list(item, "depends_on", f"{unit_path}.depends_on", errors)
        stream = _optional_string(item, "stream", f"{unit_path}.stream", errors)
        platform = _optional_string(item, "platform", f"{unit_path}.platform", errors)
        phase = _optional_string(item, "phase", f"{unit_path}.phase", errors)
        kind = _optional_string(item, "kind", f"{unit_path}.kind", errors)
        repo_id = _optional_string(item, "repo_id", f"{unit_path}.repo_id", errors)
        component = _optional_string(item, "component", f"{unit_path}.component", errors)
        scope_paths = _optional_string_list(item, "scope_paths", f"{unit_path}.scope_paths", errors)
        estimated_size = _optional_estimated_size(item, "estimated_size", f"{unit_path}.estimated_size", errors)
        risk_tags = _optional_risk_tags(item, "risk_tags", f"{unit_path}.risk_tags", errors)
        budget = _optional_budget(item, "budget", f"{unit_path}.budget", errors)

        if unit_id:
            if unit_id in seen:
                errors.append(
                    DeliveryPlanIssue("error", "units.duplicate_id", f"Duplicate unit id: {unit_id}", f"{unit_path}.id")
                )
            seen.add(unit_id)
        if task_path:
            _validate_task_path(task_path, project_root=project_root, errors=errors, path=f"{unit_path}.task_path")
        if repo_id and repo_id not in repo_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.repo_id_unknown",
                    f"Unit references unsupported repository id: {repo_id}",
                    f"{unit_path}.repo_id",
                )
            )
        if stream_ids and stream and stream not in stream_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.stream_unknown",
                    f"Unit references unknown stream: {stream}",
                    f"{unit_path}.stream",
                )
            )
        if stream_ids and not stream:
            warnings.append(
                DeliveryPlanIssue(
                    "warning",
                    "units.stream_missing",
                    "Unit has no stream even though the plan defines streams.",
                    f"{unit_path}.stream",
                )
            )
        if component and component not in component_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.component_unknown",
                    f"Unit references unknown component: {component}",
                    f"{unit_path}.component",
                )
            )
        for scope_idx, scope_path in enumerate(scope_paths):
            _validate_project_relative_metadata_path(
                scope_path,
                project_root=project_root,
                errors=errors,
                path=f"{unit_path}.scope_paths[{scope_idx}]",
                code_prefix="units.scope_path",
                subject="Unit scope path",
            )
        if unit_id and task_path:
            unit = DeliveryPlanUnit(
                id=unit_id,
                title=title,
                task_path=task_path,
                depends_on=depends_on,
                stream=stream,
                platform=platform,
                phase=phase,
                kind=kind,
                repo_id=repo_id,
                component=component,
                scope_paths=scope_paths,
                estimated_size=estimated_size,
                risk_tags=risk_tags,
                budget=budget,
                source_path=unit_path,
            )
            units.append(unit)
            _append_unit_sizing_warnings(unit, warnings)
    return units


def _validate_task_path(task_path: str, *, project_root: Path, errors: list[DeliveryPlanIssue], path: str) -> None:
    try:
        raw_path = Path(task_path)
    except (OSError, ValueError):
        _add_invalid_task_path_error(errors, path)
        return
    if raw_path.is_absolute():
        errors.append(
            DeliveryPlanIssue(
                "error",
                "units.task_path_absolute",
                "Unit task_path must be project-relative so the plan remains portable.",
                path,
            )
        )
        return
    try:
        resolved = (project_root / raw_path).resolve()
    except (OSError, ValueError):
        _add_invalid_task_path_error(errors, path)
        return
    if not _path_is_within(resolved, project_root):
        errors.append(
            DeliveryPlanIssue(
                "error", "units.task_path_outside_project", "Unit task_path escapes the project root.", path
            )
        )
        return
    try:
        task_file_exists = resolved.is_file()
    except (OSError, ValueError):
        _add_invalid_task_path_error(errors, path)
        return
    if not task_file_exists:
        errors.append(
            DeliveryPlanIssue("error", "units.task_path_missing", f"Unit task_path does not exist: {task_path}", path)
        )


def _add_invalid_task_path_error(errors: list[DeliveryPlanIssue], path: str) -> None:
    errors.append(
        DeliveryPlanIssue(
            "error",
            "units.task_path_invalid",
            "Unit task_path is not a valid project-relative filesystem path.",
            path,
        )
    )


def _validate_project_relative_metadata_path(
    path_value: str,
    *,
    project_root: Path,
    errors: list[DeliveryPlanIssue],
    path: str,
    code_prefix: str,
    subject: str,
) -> None:
    try:
        raw_path = Path(path_value)
    except (OSError, ValueError):
        _add_invalid_metadata_path_error(errors, path, code_prefix, subject)
        return
    if raw_path.is_absolute():
        errors.append(
            DeliveryPlanIssue(
                "error",
                f"{code_prefix}_absolute",
                f"{subject} must be project-relative so the plan remains portable.",
                path,
            )
        )
        return
    try:
        resolved = (project_root / raw_path).resolve()
    except (OSError, ValueError):
        _add_invalid_metadata_path_error(errors, path, code_prefix, subject)
        return
    if not _path_is_within(resolved, project_root):
        errors.append(
            DeliveryPlanIssue(
                "error",
                f"{code_prefix}_outside_project",
                f"{subject} escapes the project root.",
                path,
            )
        )


def _add_invalid_metadata_path_error(
    errors: list[DeliveryPlanIssue],
    path: str,
    code_prefix: str,
    subject: str,
) -> None:
    errors.append(
        DeliveryPlanIssue(
            "error",
            f"{code_prefix}_invalid",
            f"{subject} is not a valid project-relative filesystem path.",
            path,
        )
    )


def _optional_estimated_size(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[DeliveryPlanIssue],
) -> str | None:
    value = _optional_string(data, key, path, errors)
    if value is None:
        return None
    if value not in DELIVERY_UNIT_SIZE_VALUES:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "units.estimated_size_invalid",
                f"estimated_size must be one of: {', '.join(DELIVERY_UNIT_SIZE_VALUES)}.",
                path,
            )
        )
        return None
    return value


def _optional_risk_tags(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[DeliveryPlanIssue],
) -> list[str]:
    if key not in data or data.get(key) is None:
        return []
    value = data.get(key)
    if not isinstance(value, list):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "units.risk_tags_invalid_type",
                "risk_tags must be a list of supported risk tag strings.",
                path,
            )
        )
        return []
    result: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.risk_tag_invalid",
                    "risk_tags entries must be non-empty strings.",
                    item_path,
                )
            )
            continue
        tag = item.strip()
        if tag not in DELIVERY_UNIT_RISK_TAG_VALUES:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.risk_tag_unknown",
                    "risk_tags entries must use supported delivery unit risk tags.",
                    item_path,
                )
            )
            continue
        if tag in seen:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.risk_tag_duplicate",
                    "risk_tags entries must not contain duplicates.",
                    item_path,
                )
            )
            continue
        seen.add(tag)
        result.append(tag)
    return result


def _optional_budget(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[DeliveryPlanIssue],
) -> DeliveryUnitBudget | None:
    if key not in data or data.get(key) is None:
        return None
    value = data.get(key)
    if not isinstance(value, dict):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "units.budget_invalid_type",
                "budget must be an object with supported positive integer fields.",
                path,
            )
        )
        return None
    unknown_fields = [field_name for field_name in value if field_name not in DELIVERY_UNIT_BUDGET_FIELDS]
    if unknown_fields:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "units.budget_unknown_field",
                "budget contains an unsupported field.",
                _budget_unknown_field_path(path, value, unknown_fields[0]),
            )
        )
    kwargs: dict[str, int] = {}
    for field_name in DELIVERY_UNIT_BUDGET_FIELDS:
        if field_name not in value:
            continue
        field_value = value[field_name]
        if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 1:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.budget_value_invalid",
                    f"{field_name} must be a positive integer.",
                    f"{path}.{field_name}",
                )
            )
            continue
        kwargs[field_name] = field_value
    if not kwargs:
        return None
    return DeliveryUnitBudget(**kwargs)


def _budget_unknown_field_path(path: str, value: dict[Any, Any], field_name: Any) -> str:
    if isinstance(field_name, str) and field_name:
        return f"{path}.{field_name}"
    for idx, key in enumerate(value):
        if key == field_name:
            return f"{path}[{idx}]"
    return path


def _append_unit_sizing_warnings(unit: DeliveryPlanUnit, warnings: list[DeliveryPlanIssue]) -> None:
    risk_tags = set(unit.risk_tags)
    split_risk_count = len(risk_tags & DELIVERY_UNIT_SPLIT_RISK_TAGS)
    if _SPLIT_RECOMMENDED_TAG_SET.issubset(risk_tags) or split_risk_count >= 3:
        warnings.append(
            DeliveryPlanIssue(
                "warning",
                "units.split_recommended",
                "Unit combines multiple high-risk delivery surfaces; consider splitting it before execution.",
                f"{unit.source_path}.risk_tags" if unit.source_path else None,
            )
        )
    elif unit.estimated_size == "large" and split_risk_count >= 2:
        warnings.append(
            DeliveryPlanIssue(
                "warning",
                "units.large_high_risk",
                "Large unit includes multiple high-risk delivery surfaces; verify the sizing before execution.",
                f"{unit.source_path}.risk_tags" if unit.source_path else None,
            )
        )


def _validate_dependencies(units: list[DeliveryPlanUnit], errors: list[DeliveryPlanIssue]) -> None:
    unit_ids = {unit.id for unit in units}
    deps_by_id = {unit.id: unit.depends_on for unit in units}
    for unit in units:
        for dep_idx, dependency in enumerate(unit.depends_on):
            unit_path = unit.source_path or "units"
            path = f"{unit_path}.depends_on[{dep_idx}]"
            if dependency == unit.id:
                errors.append(
                    DeliveryPlanIssue("error", "dependencies.self_reference", "Unit cannot depend on itself.", path)
                )
            elif dependency not in unit_ids:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "dependencies.unknown_unit",
                        f"Unit depends on unknown unit id: {dependency}",
                        path,
                    )
                )

    known_deps = {unit_id: [dep for dep in deps if dep in unit_ids] for unit_id, deps in deps_by_id.items()}
    cycle = _find_dependency_cycle(known_deps)
    if cycle:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "dependencies.cycle",
                "Delivery unit dependencies contain a cycle: " + " -> ".join(cycle),
                "units",
            )
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


def _require_string(data: dict[str, Any], key: str, path: str, errors: list[DeliveryPlanIssue]) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(DeliveryPlanIssue("error", f"{path}.missing", f"{key} must be a non-empty string.", path))
        return None
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, path: str, errors: list[DeliveryPlanIssue]) -> str | None:
    if key not in data or data.get(key) is None:
        return None
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(DeliveryPlanIssue("error", f"{path}.invalid_type", f"{key} must be a non-empty string.", path))
        return None
    return value.strip()


def _require_int(data: dict[str, Any], key: str, path: str, errors: list[DeliveryPlanIssue]) -> int | None:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(DeliveryPlanIssue("error", f"{path}.missing", f"{key} must be an integer.", path))
        return None
    return value


def _optional_string_list(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[DeliveryPlanIssue],
) -> list[str]:
    if key not in data or data.get(key) is None:
        return []
    value = data.get(key)
    if not isinstance(value, list):
        errors.append(DeliveryPlanIssue("error", f"{path}.invalid_type", f"{key} must be a list of strings.", path))
        return []
    result: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    f"{path}.item_invalid",
                    f"{key} entries must be non-empty strings.",
                    f"{path}[{idx}]",
                )
            )
            continue
        result.append(item.strip())
    return result


def _find_git_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
