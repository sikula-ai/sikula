from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
from typing import Any

import yaml

from core.delivery_public_metadata import (
    contains_delivery_source_excerpt,
    is_safe_delivery_public_metadata,
    project_delivery_public_identity,
    sanitize_delivery_public_metadata,
)
from core.delivery_unit_metadata import (
    DELIVERY_UNIT_BUDGET_FIELDS,
    DELIVERY_UNIT_RISK_TAG_VALUES,
    DELIVERY_UNIT_SIZE_VALUES,
    DELIVERY_UNIT_SPLIT_RISK_TAGS,
    MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS,
    DeliveryUnitBudget,
)

SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION = 1
SUPPORTED_DELIVERY_CONSTRAINT_CONTEXT_SCHEMA_VERSION = 1
DELIVERY_CONSTRAINT_KIND_VALUES = frozenset(
    {
        "authoritative_read_only_dependency",
        "prohibited_fallback",
        "repository_ownership",
        "security_boundary",
        "stop_and_follow_up",
    }
)
DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION = "preserved"
MAX_DELIVERY_CONSTRAINTS = 100
MAX_DELIVERY_CONSTRAINT_UNIT_IDS = 1000
_DELIVERY_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DELIVERY_SOURCE_TASK_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_DELIVERY_UNIT_ID_LENGTH = 1000
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
            "message": sanitize_delivery_public_metadata(self.message),
        }
        if self.path:
            data["path"] = sanitize_delivery_public_metadata(self.path)
        return data

    def to_public_text(self) -> str:
        data = self.to_dict()
        location = f" [{data['path']}]" if data.get("path") else ""
        return f"- {data['code']}{location}: {data['message']}"


@dataclass(frozen=True)
class DeliveryRepository:
    id: str
    root: str = "."
    implicit: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": project_delivery_public_identity(self.id),
            "root": self.root,
        }
        if self.implicit:
            data["implicit"] = True
        return data


@dataclass(frozen=True)
class DeliveryPlanSourceTask:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class DeliveryConstraint:
    id: str
    kind: str
    summary: str
    unit_ids: list[str]
    disposition: str = DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": project_delivery_public_identity(self.id),
            "kind": self.kind,
            "summary": sanitize_delivery_public_metadata(self.summary),
            "unit_ids": [project_delivery_public_identity(unit_id) for unit_id in self.unit_ids],
            "disposition": self.disposition,
        }

    def to_context_dict(self) -> dict[str, Any]:
        """Serialize private child context without applying public identity projection."""
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "unit_ids": list(self.unit_ids),
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class DeliveryComponent:
    id: str
    path: str
    label: str | None = None
    stream: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": project_delivery_public_identity(self.id),
            "path": self.path,
        }
        for key in ("label", "stream"):
            value = getattr(self, key)
            if value:
                data[key] = (
                    project_delivery_public_identity(value)
                    if key == "stream"
                    else sanitize_delivery_public_metadata(value)
                )
        return data


@dataclass(frozen=True)
class DeliveryBudgetExceeded:
    name: str
    limit: int
    actual: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "limit": self.limit, "actual": self.actual}


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
    supersedes: str | None = None
    superseded_by: list[str] = field(default_factory=list)
    amend_reason: str | None = None
    budget_exceeded: DeliveryBudgetExceeded | None = None
    source_path: str = ""

    @property
    def superseded(self) -> bool:
        return bool(self.superseded_by)

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(public=True)

    def to_authoring_dict(self) -> dict[str, Any]:
        return self._to_dict(public=False)

    def _to_dict(self, *, public: bool) -> dict[str, Any]:
        project_identity = project_delivery_public_identity if public else lambda value: value
        data: dict[str, Any] = {
            "id": project_identity(self.id),
            "task_path": self.task_path,
            "depends_on": [project_identity(value) for value in self.depends_on],
        }
        for key in (
            "title",
            "stream",
            "platform",
            "phase",
            "kind",
            "repo_id",
            "component",
            "supersedes",
            "amend_reason",
        ):
            value = getattr(self, key)
            if value:
                if key in {"stream", "repo_id", "component", "supersedes"}:
                    value = project_identity(value)
                elif public and key in {"title", "platform", "phase", "kind"}:
                    value = sanitize_delivery_public_metadata(value)
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
        if self.superseded_by:
            data["superseded_by"] = [project_identity(value) for value in self.superseded_by]
        if self.budget_exceeded:
            data["budget_exceeded"] = self.budget_exceeded.to_dict()
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
    constraints: list[DeliveryConstraint] = field(default_factory=list)
    source_task: DeliveryPlanSourceTask | None = None
    planning_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "title": sanitize_delivery_public_metadata(self.title),
            "final_branch": self.final_branch,
            "repositories": [repo.to_dict() for repo in self.repositories],
            "units": [unit.to_dict() for unit in self.units],
        }
        if self.stream_ids:
            data["streams"] = [project_delivery_public_identity(stream) for stream in self.stream_ids]
        if self.components:
            data["components"] = [component.to_dict() for component in self.components]
        if self.source_task:
            data["source_task"] = self.source_task.to_dict()
        if self.constraints:
            data["constraints"] = [constraint.to_dict() for constraint in self.constraints]
        if self.planning_mode:
            data["planning_mode"] = self.planning_mode
        return data


def delivery_unit_constraint_context(
    plan: DeliveryPlan,
    unit_id: str,
) -> tuple[int, dict[str, str] | None, list[dict[str, Any]]]:
    """Return the bounded source binding and constraints applicable to one unit."""
    source_task = plan.source_task.to_dict() if plan.source_task else None
    constraints = [constraint.to_context_dict() for constraint in plan.constraints if unit_id in constraint.unit_ids]
    return SUPPORTED_DELIVERY_CONSTRAINT_CONTEXT_SCHEMA_VERSION, source_task, constraints


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
        plan = _parse_delivery_plan(
            data,
            project_root=resolved_project_root,
            errors=errors,
            warnings=warnings,
            virtual_task_paths=frozenset(),
        )

    return DeliveryPlanCheckResult(
        plan_path=str(plan_path),
        project_root=str(resolved_project_root) if resolved_project_root else None,
        errors=errors,
        warnings=warnings,
        plan=plan,
    )


def check_delivery_plan_data(
    data: dict[str, Any],
    *,
    project_root: Path,
    plan_path: str = "<memory>",
    virtual_task_paths: set[str] | frozenset[str] | None = None,
) -> DeliveryPlanCheckResult:
    root = project_root.resolve()
    errors: list[DeliveryPlanIssue] = []
    warnings: list[DeliveryPlanIssue] = []
    plan = _parse_delivery_plan(
        data,
        project_root=root,
        errors=errors,
        warnings=warnings,
        virtual_task_paths=frozenset(virtual_task_paths or ()),
    )
    return DeliveryPlanCheckResult(
        plan_path=plan_path,
        project_root=str(root),
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
                f"Title: {sanitize_delivery_public_metadata(result.plan.title)}",
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
    return issue.to_public_text()


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
    virtual_task_paths: frozenset[str],
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
    source_task, source_task_description = _parse_source_task(
        data.get("source_task"),
        project_root=project_root,
        errors=errors,
    )
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
        virtual_task_paths=virtual_task_paths,
    )
    raw_constraints = data.get("constraints")
    constraints = _parse_constraints(
        raw_constraints,
        units=units,
        source_task_description=source_task_description,
        errors=errors,
    )
    if isinstance(raw_constraints, list) and raw_constraints and source_task is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "constraints.source_task_required",
                "Plans with inherited constraints must include a valid source_task fingerprint.",
                "source_task",
            )
        )

    if units:
        _validate_dependencies(units, errors)
        _validate_amendment_metadata(units, errors)

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
        constraints=constraints,
        source_task=source_task,
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


def _parse_source_task(
    value: Any,
    *,
    project_root: Path,
    errors: list[DeliveryPlanIssue],
) -> tuple[DeliveryPlanSourceTask | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.invalid_type",
                "source_task must be a mapping with path and sha256 fields.",
                "source_task",
            )
        )
        return None, None
    unknown_fields = set(value) - {"path", "sha256"}
    if unknown_fields:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.unknown_field",
                "source_task contains an unsupported field.",
                f"source_task.{sorted(str(field) for field in unknown_fields)[0]}",
            )
        )

    source_path = _require_string(value, "path", "source_task.path", errors)
    source_sha256 = _require_string(value, "sha256", "source_task.sha256", errors)
    if source_path is None or source_sha256 is None:
        return None, None
    if not is_safe_delivery_public_metadata(source_path):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.path_metadata_invalid",
                "source_task.path must be bounded single-line public metadata without absolute paths.",
                "source_task.path",
            )
        )
        return None, None
    path_error_count = len(errors)
    _validate_project_relative_metadata_path(
        source_path,
        project_root=project_root,
        errors=errors,
        path="source_task.path",
        code_prefix="source_task.path",
        subject="Source task path",
    )
    if len(errors) != path_error_count:
        return None, None
    if not _DELIVERY_SOURCE_TASK_SHA256_RE.fullmatch(source_sha256):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.sha256_invalid",
                "source_task.sha256 must use the sha256:<lowercase-hex> format.",
                "source_task.sha256",
            )
        )
        return None, None

    source_file = project_root / source_path
    try:
        if source_file.is_symlink():
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "source_task.symlink",
                    "source_task.path must not be a symlink.",
                    "source_task.path",
                )
            )
            return None, None
        source_text = source_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.read_failed",
                "source_task.path must reference a readable UTF-8 file.",
                "source_task.path",
            )
        )
        return None, None
    actual_sha256 = "sha256:" + sha256(source_text.encode("utf-8")).hexdigest()
    if actual_sha256 != source_sha256:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "source_task.hash_mismatch",
                "The source task changed after inherited constraints were prepared.",
                "source_task.sha256",
            )
        )
        source_text = None
    return DeliveryPlanSourceTask(path=source_path, sha256=source_sha256), source_text


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
    virtual_task_paths: frozenset[str],
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
        supersedes = _optional_string(item, "supersedes", f"{unit_path}.supersedes", errors)
        superseded_by = _optional_string_list(item, "superseded_by", f"{unit_path}.superseded_by", errors)
        amend_reason = _optional_string(item, "amend_reason", f"{unit_path}.amend_reason", errors)
        if amend_reason and not _DELIVERY_PLAN_ID_RE.fullmatch(amend_reason):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "amendment.amend_reason_invalid",
                    "amend_reason must be a stable code.",
                    f"{unit_path}.amend_reason",
                )
            )
        budget_exceeded = _optional_budget_exceeded(
            item,
            "budget_exceeded",
            f"{unit_path}.budget_exceeded",
            errors,
        )

        if unit_id:
            if len(unit_id) > MAX_DELIVERY_UNIT_ID_LENGTH or any(ord(char) < 32 for char in unit_id):
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "units.id_invalid",
                        (
                            f"Unit id must be at most {MAX_DELIVERY_UNIT_ID_LENGTH} characters "
                            "and contain no control characters."
                        ),
                        f"{unit_path}.id",
                    )
                )
            if unit_id in seen:
                errors.append(
                    DeliveryPlanIssue("error", "units.duplicate_id", f"Duplicate unit id: {unit_id}", f"{unit_path}.id")
                )
            seen.add(unit_id)
        if task_path:
            _validate_task_path(
                task_path,
                project_root=project_root,
                errors=errors,
                path=f"{unit_path}.task_path",
                virtual_task_paths=virtual_task_paths,
            )
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
                supersedes=supersedes,
                superseded_by=superseded_by,
                amend_reason=amend_reason,
                budget_exceeded=budget_exceeded,
                source_path=unit_path,
            )
            units.append(unit)
            if not unit.superseded:
                _append_unit_sizing_warnings(unit, warnings)
    return units


def _parse_constraints(
    value: Any,
    *,
    units: list[DeliveryPlanUnit],
    source_task_description: str | None,
    errors: list[DeliveryPlanIssue],
) -> list[DeliveryConstraint]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "constraints.invalid_type",
                "constraints must be a list of inherited hard-constraint mappings.",
                "constraints",
            )
        )
        return []
    if len(value) > MAX_DELIVERY_CONSTRAINTS:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "constraints.too_many",
                f"constraints must contain at most {MAX_DELIVERY_CONSTRAINTS} entries.",
                "constraints",
            )
        )
        value = value[:MAX_DELIVERY_CONSTRAINTS]

    unit_by_id = {unit.id: unit for unit in units}
    constraints: list[DeliveryConstraint] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        constraint_path = f"constraints[{index}]"
        if not isinstance(item, dict):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.item_invalid",
                    "Constraint entries must be mappings.",
                    constraint_path,
                )
            )
            continue
        unknown_fields = set(item) - {"id", "kind", "summary", "unit_ids", "disposition"}
        if unknown_fields:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.unknown_field",
                    "Constraint entry contains an unsupported field.",
                    f"{constraint_path}.{sorted(str(field) for field in unknown_fields)[0]}",
                )
            )

        constraint_id = _require_string(item, "id", f"{constraint_path}.id", errors)
        kind = _require_string(item, "kind", f"{constraint_path}.kind", errors)
        summary = _require_string(item, "summary", f"{constraint_path}.summary", errors)
        unit_ids = _optional_string_list(item, "unit_ids", f"{constraint_path}.unit_ids", errors)
        disposition = _require_string(item, "disposition", f"{constraint_path}.disposition", errors)

        if constraint_id:
            normalized_id = constraint_id.casefold()
            if len(constraint_id) > MAX_DELIVERY_UNIT_ID_LENGTH or not _DELIVERY_PLAN_ID_RE.fullmatch(constraint_id):
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "constraints.id_invalid",
                        "Constraint id must be a stable identifier using letters, numbers, dots, underscores, or hyphens.",
                        f"{constraint_path}.id",
                    )
                )
            if normalized_id in seen_ids:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "constraints.duplicate_id",
                        "Constraint ids must be case-insensitively unique.",
                        f"{constraint_path}.id",
                    )
                )
            seen_ids.add(normalized_id)
        if kind and kind not in DELIVERY_CONSTRAINT_KIND_VALUES:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.kind_invalid",
                    "Constraint kind must be one of the supported inherited hard-constraint kinds.",
                    f"{constraint_path}.kind",
                )
            )
        if summary and not is_safe_delivery_public_metadata(summary):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.summary_invalid",
                    "Constraint summary must be bounded single-line public metadata without absolute paths.",
                    f"{constraint_path}.summary",
                )
            )
        elif (
            summary
            and source_task_description is not None
            and contains_delivery_source_excerpt(summary, source_task_description)
        ):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.summary_source_excerpt",
                    "Constraint summaries must paraphrase source-task rules without copying source text.",
                    f"{constraint_path}.summary",
                )
            )
            summary = None
        elif summary and source_task_description is None:
            summary = None
        if not unit_ids:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.unit_ids_empty",
                    "Each inherited constraint must apply to at least one delivery unit.",
                    f"{constraint_path}.unit_ids",
                )
            )
        if len(unit_ids) > MAX_DELIVERY_CONSTRAINT_UNIT_IDS:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.unit_ids_too_many",
                    f"Constraint unit_ids must contain at most {MAX_DELIVERY_CONSTRAINT_UNIT_IDS} entries.",
                    f"{constraint_path}.unit_ids",
                )
            )
            unit_ids = unit_ids[:MAX_DELIVERY_CONSTRAINT_UNIT_IDS]
        seen_unit_ids: set[str] = set()
        for unit_index, unit_id in enumerate(unit_ids):
            unit_path = f"{constraint_path}.unit_ids[{unit_index}]"
            if unit_id in seen_unit_ids:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "constraints.unit_id_duplicate",
                        "Constraint unit_ids must not contain duplicates.",
                        unit_path,
                    )
                )
                continue
            seen_unit_ids.add(unit_id)
            unit = unit_by_id.get(unit_id)
            if unit is None:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "constraints.unit_unknown",
                        "Constraint unit_ids must reference known delivery units.",
                        unit_path,
                    )
                )
            elif unit.superseded:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "constraints.unit_superseded",
                        "Inherited constraints must be reassigned before a constrained unit is superseded.",
                        unit_path,
                    )
                )
        if disposition and disposition != DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "constraints.disposition_unresolved",
                    "Published delivery plans may contain only preserved inherited constraints.",
                    f"{constraint_path}.disposition",
                )
            )
        if constraint_id and kind and summary and disposition:
            constraints.append(
                DeliveryConstraint(
                    id=constraint_id,
                    kind=kind,
                    summary=summary,
                    unit_ids=unit_ids,
                    disposition=disposition,
                )
            )
    return constraints


def _validate_task_path(
    task_path: str,
    *,
    project_root: Path,
    errors: list[DeliveryPlanIssue],
    path: str,
    virtual_task_paths: frozenset[str],
) -> None:
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
        relative_path = resolved.relative_to(project_root).as_posix()
    except ValueError:
        _add_invalid_task_path_error(errors, path)
        return
    if relative_path in virtual_task_paths:
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
    posix_path = PurePosixPath(path_value)
    windows_path = PureWindowsPath(path_value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
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
        return
    if ".." in posix_path.parts or ".." in windows_path.parts:
        errors.append(
            DeliveryPlanIssue(
                "error",
                f"{code_prefix}_parent_traversal",
                f"{subject} must not contain parent-directory traversal.",
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
        if field_name == "max_planner_steps" and field_value > MAX_DELIVERY_UNIT_MAX_PLANNER_STEPS:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "units.planner_step_budget_invalid",
                    "max_planner_steps must be 1 or 2; three or more planner steps require a split.",
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


def _validate_amendment_metadata(units: list[DeliveryPlanUnit], errors: list[DeliveryPlanIssue]) -> None:
    by_id = {unit.id: unit for unit in units}
    superseded_ids = {unit.id for unit in units if unit.superseded}
    for unit in units:
        unit_path = unit.source_path or "units"
        if len(unit.superseded_by) != len(set(unit.superseded_by)):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "amendment.superseded_by_duplicate",
                    "superseded_by must not contain duplicate unit ids.",
                    f"{unit_path}.superseded_by",
                )
            )
        for index, replacement_id in enumerate(unit.superseded_by):
            replacement = by_id.get(replacement_id)
            path = f"{unit_path}.superseded_by[{index}]"
            if replacement is None:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "amendment.replacement_unknown",
                        f"superseded_by references unknown unit id: {replacement_id}",
                        path,
                    )
                )
            elif replacement.supersedes != unit.id:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "amendment.replacement_mismatch",
                        f"Replacement unit {replacement_id} must supersede {unit.id}.",
                        path,
                    )
                )

        if unit.supersedes:
            target = by_id.get(unit.supersedes)
            if target is None:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "amendment.target_unknown",
                        f"supersedes references unknown unit id: {unit.supersedes}",
                        f"{unit_path}.supersedes",
                    )
                )
            elif unit.id not in target.superseded_by:
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "amendment.target_mismatch",
                        f"Superseded unit {unit.supersedes} must list replacement {unit.id}.",
                        f"{unit_path}.supersedes",
                    )
                )
            else:
                _validate_replacement_dependencies(unit, target, by_id, errors)

        if not unit.superseded:
            for index, dependency in enumerate(unit.depends_on):
                if dependency in superseded_ids:
                    errors.append(
                        DeliveryPlanIssue(
                            "error",
                            "amendment.active_dependency_superseded",
                            f"Active unit cannot depend on superseded unit: {dependency}",
                            f"{unit_path}.depends_on[{index}]",
                        )
                    )

    lineage = {unit.id: [unit.supersedes] if unit.supersedes in by_id else [] for unit in units}
    cycle = _find_dependency_cycle(lineage)
    if cycle:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "amendment.supersession_cycle",
                "Delivery amendment lineage contains a cycle: " + " -> ".join(cycle),
                "units",
            )
        )


def _validate_replacement_dependencies(
    replacement: DeliveryPlanUnit,
    target: DeliveryPlanUnit,
    by_id: dict[str, DeliveryPlanUnit],
    errors: list[DeliveryPlanIssue],
) -> None:
    sibling_ids = set(target.superseded_by)
    internal_dependencies = [
        dependency for dependency in replacement.depends_on if _supersession_lineage(dependency, by_id) & sibling_ids
    ]
    valid = False
    if internal_dependencies:
        valid = len(internal_dependencies) == len(replacement.depends_on)
    else:
        expected_upstream: set[str] = set()
        for dependency in target.depends_on:
            expected_upstream.update(_effective_replacement_leaves(dependency, by_id))
        valid = set(replacement.depends_on) == expected_upstream
    if not valid:
        unit_path = replacement.source_path or "units"
        errors.append(
            DeliveryPlanIssue(
                "error",
                "amendment.replacement_dependencies_invalid",
                "Replacement dependencies must reference sibling replacement lineage or all active target prerequisites.",
                f"{unit_path}.depends_on",
            )
        )


def _supersession_lineage(unit_id: str, by_id: dict[str, DeliveryPlanUnit]) -> set[str]:
    lineage: set[str] = set()
    current = by_id.get(unit_id)
    while current is not None and current.id not in lineage:
        lineage.add(current.id)
        current = by_id.get(current.supersedes) if current.supersedes else None
    return lineage


def _effective_replacement_leaves(
    unit_id: str,
    by_id: dict[str, DeliveryPlanUnit],
    *,
    visiting: frozenset[str] = frozenset(),
) -> set[str]:
    unit = by_id.get(unit_id)
    if unit is None or not unit.superseded_by or unit_id in visiting:
        return {unit_id}
    replacement_ids = {replacement_id for replacement_id in unit.superseded_by if replacement_id in by_id}
    depended_on: set[str] = set()
    for replacement_id in replacement_ids:
        replacement = by_id[replacement_id]
        for dependency in replacement.depends_on:
            depended_on.update(_supersession_lineage(dependency, by_id) & replacement_ids)
    direct_leaves = replacement_ids - depended_on
    if not direct_leaves:
        return {unit_id}
    leaves: set[str] = set()
    next_visiting = visiting | {unit_id}
    for leaf_id in direct_leaves:
        leaves.update(_effective_replacement_leaves(leaf_id, by_id, visiting=next_visiting))
    return leaves


def _optional_budget_exceeded(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[DeliveryPlanIssue],
) -> DeliveryBudgetExceeded | None:
    if key not in data:
        return None
    value = data.get(key)
    if not isinstance(value, dict):
        errors.append(
            DeliveryPlanIssue("error", "amendment.budget_exceeded_invalid", f"{key} must be a mapping.", path)
        )
        return None
    unknown = sorted(str(item) for item in value if item not in {"name", "limit", "actual"})
    if unknown:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "amendment.budget_exceeded_unknown_field",
                f"{key} contains unsupported fields: {', '.join(unknown)}.",
                path,
            )
        )
    name = _require_string(value, "name", f"{path}.name", errors)
    limit = _require_int(value, "limit", f"{path}.limit", errors)
    actual = _require_int(value, "actual", f"{path}.actual", errors)
    if limit is not None and limit < 0:
        errors.append(
            DeliveryPlanIssue(
                "error", "amendment.budget_limit_invalid", "Budget limit must not be negative.", f"{path}.limit"
            )
        )
    if actual is not None and actual < 0:
        errors.append(
            DeliveryPlanIssue(
                "error", "amendment.budget_actual_invalid", "Budget actual must not be negative.", f"{path}.actual"
            )
        )
    if name is not None and not _DELIVERY_PLAN_ID_RE.fullmatch(name):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "amendment.budget_name_invalid",
                "Budget name must be a stable code.",
                f"{path}.name",
            )
        )
    if (
        name is None
        or not _DELIVERY_PLAN_ID_RE.fullmatch(name)
        or limit is None
        or actual is None
        or limit < 0
        or actual < 0
    ):
        return None
    return DeliveryBudgetExceeded(name=name, limit=limit, actual=actual)


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
