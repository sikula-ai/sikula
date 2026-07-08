"""Deterministic delivery-prepare artifact writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

from core.contract_check import ContractCheckResult, check_contract
from core.delivery_authoring import (
    DeliveryAuthoringDerivedPaths,
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    derive_delivery_authoring_paths,
)
from core.delivery_plan import (
    SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION,
    check_delivery_plan_file,
    delivery_final_branch_for_plan_id,
    is_valid_delivery_branch_name,
)


_PLAN_VALIDATION_NOT_RUN = "not_run"
_PLAN_VALIDATION_VALID = "valid"
_PLAN_VALIDATION_INVALID = "invalid"
_UNIT_READINESS_NOT_RUN = "not_run"
_UNIT_READINESS_READY = "ready"
_UNIT_READINESS_BLOCKED = "blocked"
_FAILURE_UNIT_READINESS_BLOCKED = "unit_readiness_blocked"
_FAILURE_PLAN_VALIDATION_FAILED = "plan_validation_failed"
_FAILURE_WRITE_FAILED = "write_failed"
_ROLLBACK_FAILED_MESSAGE = "Delivery prepare failed while restoring artifacts; inspect the selected output directory."
_FORBIDDEN_OUTPUT_ROOTS = (
    (".git",),
    (".sikula", "state"),
    (".sikula", "worktrees"),
    (".sikula", "contract-reports"),
)


@dataclass(frozen=True)
class DeliveryPrepareWriteIssue:
    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True)
class DeliveryPrepareWrittenArtifact:
    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path}


@dataclass(frozen=True)
class DeliveryPreparePlanValidationSummary:
    status: str = _PLAN_VALIDATION_NOT_RUN
    valid: bool | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.valid,
            "errors": [dict(issue) for issue in self.errors],
            "warnings": [dict(issue) for issue in self.warnings],
        }


@dataclass(frozen=True)
class DeliveryPrepareUnitReadinessSummary:
    unit_id: str
    path: str
    readiness_score: int
    status: str
    ready_for_autonomous_delivery: bool
    blocking_gap_count: int
    warning_gap_count: int
    blocking_gap_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "path": self.path,
            "readiness_score": self.readiness_score,
            "status": self.status,
            "ready_for_autonomous_delivery": self.ready_for_autonomous_delivery,
            "blocking_gap_count": self.blocking_gap_count,
            "warning_gap_count": self.warning_gap_count,
            "blocking_gap_ids": list(self.blocking_gap_ids),
        }


@dataclass(frozen=True)
class DeliveryPrepareUnitReadinessAggregate:
    status: str = _UNIT_READINESS_NOT_RUN
    units: list[DeliveryPrepareUnitReadinessSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "units": [unit.to_dict() for unit in self.units],
        }


@dataclass(frozen=True)
class DeliveryPrepareWriteResult:
    status: str
    prepared: bool
    paths: DeliveryAuthoringDerivedPaths
    unit_task_paths: dict[str, str]
    written_artifacts: list[DeliveryPrepareWrittenArtifact]
    plan_validation: DeliveryPreparePlanValidationSummary
    unit_readiness: DeliveryPrepareUnitReadinessAggregate
    errors: list[DeliveryPrepareWriteIssue] = field(default_factory=list)
    warnings: list[DeliveryPrepareWriteIssue] = field(default_factory=list)
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "prepared": self.prepared,
            "paths": {
                "plan_file": self.paths.plan_file,
                "units_dir": self.paths.units_dir,
            },
            "unit_task_paths": dict(self.unit_task_paths),
            "written_artifacts": [artifact.to_dict() for artifact in self.written_artifacts],
            "plan_validation": self.plan_validation.to_dict(),
            "unit_readiness": self.unit_readiness.to_dict(),
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class _ArtifactTarget:
    kind: str
    rel_path: str
    path: Path
    content: str


@dataclass(frozen=True)
class _ArtifactBackup:
    path: Path
    existed: bool
    content: bytes | None = None
    mode: int | None = None


@dataclass(frozen=True)
class _WriteTransaction:
    written_artifacts: list[DeliveryPrepareWrittenArtifact]
    backups: list[_ArtifactBackup]
    created_dirs: list[Path]


@dataclass(frozen=True)
class _LexicalArtifactPaths:
    output_path: Path
    plan_file: Path
    units_dir: Path
    unit_task_paths: dict[str, Path]


class _DeliveryPrepareWriteError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        path: str | None = None,
        *,
        issues: list[DeliveryPrepareWriteIssue] | None = None,
        written_artifacts: list[DeliveryPrepareWrittenArtifact] | None = None,
    ) -> None:
        super().__init__(message)
        self.issue = DeliveryPrepareWriteIssue("error", code, message, path)
        self.issues = issues or [self.issue]
        self.written_artifacts = written_artifacts or []


def write_delivery_prepare_artifacts(
    draft: DeliveryAuthoringDraft,
    *,
    output_dir: str | Path,
    project_root: str | Path,
    project_config: dict | None = None,
    force: bool = False,
) -> DeliveryPrepareWriteResult:
    try:
        root = _resolve_project_root(project_root)
    except _DeliveryPrepareWriteError as exc:
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=exc.issues,
            written_artifacts=exc.written_artifacts,
        )

    if _is_absolute_path(output_dir):
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.output_absolute",
                    "Output path must be project-relative.",
                )
            ],
        )

    if _has_parent_traversal(output_dir):
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.output_traversal",
                    "Output path must not contain parent-directory traversal.",
                )
            ],
        )

    if _is_forbidden_output_root(output_dir):
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.output_runtime_artifact",
                    "Output path must not be inside Sikula runtime, debug, or VCS metadata directories.",
                )
            ],
        )

    lexical_paths = _lexical_artifact_paths(draft, output_dir, root, skip_unsafe_unit_ids=True)
    try:
        _validate_lexical_artifact_symlinks(lexical_paths, root=root)
    except _DeliveryPrepareWriteError as exc:
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=exc.issues,
            written_artifacts=exc.written_artifacts,
        )

    try:
        paths = derive_delivery_authoring_paths(draft, output_dir=output_dir, project_root=root)
    except DeliveryAuthoringParseError as exc:
        return _blocked_result(
            DeliveryAuthoringDerivedPaths(plan_file="", units_dir="", unit_task_paths={}),
            unit_task_paths={},
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    exc.code,
                    exc.message,
                )
            ],
        )
    final_branch = delivery_final_branch_for_plan_id(draft.plan_id)
    if not is_valid_delivery_branch_name(final_branch):
        return _blocked_result(
            paths,
            unit_task_paths=paths.unit_task_paths,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.final_branch_invalid",
                    "Selected plan_id would create an invalid final branch name.",
                    paths.plan_file,
                )
            ],
        )
    unit_task_paths = dict(paths.unit_task_paths)
    lexical_paths = _lexical_artifact_paths(draft, output_dir, root, skip_unsafe_unit_ids=False)

    try:
        plan_file = _resolve_project_artifact(paths.plan_file, root)
        output_path = plan_file.parent
        _validate_artifact_paths(paths, root=root, output_path=output_path)
    except _DeliveryPrepareWriteError as exc:
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=exc.issues,
            written_artifacts=exc.written_artifacts,
        )

    try:
        unit_readiness = _check_unit_readiness(draft, unit_task_paths, project_config=project_config)
    except (OSError, RuntimeError, ValueError, KeyError):
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.unit_readiness_check_failed",
                    "Delivery prepare failed while checking unit readiness.",
                )
            ],
        )
    if unit_readiness.status == _UNIT_READINESS_BLOCKED:
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            unit_readiness=unit_readiness,
            failure_reason=_FAILURE_UNIT_READINESS_BLOCKED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.unit_readiness_blocked",
                    "Generated unit task contracts have blocking readiness gaps.",
                )
            ],
        )

    try:
        plan_yaml = _render_plan_yaml(draft, unit_task_paths)
        targets = _artifact_targets(
            draft,
            paths=paths,
            lexical_paths=lexical_paths,
            plan_yaml=plan_yaml,
        )
        _validate_filesystem_targets(
            targets,
            output_path=lexical_paths.output_path,
            units_dir=lexical_paths.units_dir,
            root=root,
            force=force,
        )
        transaction = _write_targets(targets, root=root)
    except _DeliveryPrepareWriteError as exc:
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            unit_readiness=unit_readiness,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=exc.issues,
            written_artifacts=exc.written_artifacts,
        )
    except (OSError, RuntimeError, ValueError, yaml.YAMLError):
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            unit_readiness=unit_readiness,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.write_failed",
                    "Delivery prepare failed while writing artifacts.",
                )
            ],
        )

    try:
        plan_validation = _plan_validation_summary(
            check_delivery_plan_file(lexical_paths.plan_file, project_root=root),
            root,
        )
    except (OSError, RuntimeError, ValueError):
        rollback_errors = _rollback_targets(transaction.backups, transaction.created_dirs, root=root)
        errors = [
            DeliveryPrepareWriteIssue(
                "error",
                "delivery_prepare.plan_validation_check_failed",
                "Delivery prepare failed while validating written artifacts.",
            )
        ]
        errors.extend(rollback_errors)
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            unit_readiness=unit_readiness,
            failure_reason=_FAILURE_WRITE_FAILED,
            errors=errors,
            written_artifacts=transaction.written_artifacts if rollback_errors else [],
        )
    if plan_validation.valid is not True:
        rollback_errors = _rollback_targets(transaction.backups, transaction.created_dirs, root=root)
        errors = [
            DeliveryPrepareWriteIssue(
                "error",
                "delivery_prepare.plan_validation_failed",
                "Generated delivery plan artifacts failed validation.",
            )
        ]
        errors.extend(rollback_errors)
        return _blocked_result(
            paths,
            unit_task_paths=unit_task_paths,
            plan_validation=plan_validation,
            unit_readiness=unit_readiness,
            failure_reason=_FAILURE_WRITE_FAILED if rollback_errors else _FAILURE_PLAN_VALIDATION_FAILED,
            errors=errors,
            written_artifacts=transaction.written_artifacts if rollback_errors else [],
        )

    return DeliveryPrepareWriteResult(
        status="ready",
        prepared=True,
        paths=paths,
        unit_task_paths=unit_task_paths,
        written_artifacts=transaction.written_artifacts,
        plan_validation=plan_validation,
        unit_readiness=unit_readiness,
    )


def _blocked_result(
    paths: DeliveryAuthoringDerivedPaths,
    *,
    unit_task_paths: dict[str, str],
    failure_reason: str,
    errors: list[DeliveryPrepareWriteIssue],
    plan_validation: DeliveryPreparePlanValidationSummary | None = None,
    unit_readiness: DeliveryPrepareUnitReadinessAggregate | None = None,
    written_artifacts: list[DeliveryPrepareWrittenArtifact] | None = None,
) -> DeliveryPrepareWriteResult:
    return DeliveryPrepareWriteResult(
        status="blocked",
        prepared=False,
        paths=paths,
        unit_task_paths=unit_task_paths,
        written_artifacts=written_artifacts or [],
        plan_validation=plan_validation or DeliveryPreparePlanValidationSummary(),
        unit_readiness=unit_readiness or DeliveryPrepareUnitReadinessAggregate(),
        errors=errors,
        failure_reason=failure_reason,
    )


def _resolve_project_root(project_root: str | Path) -> Path:
    try:
        return Path(project_root).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.project_root_invalid",
            "Project root must be a valid filesystem path.",
        ) from exc


def _resolve_project_artifact(path: str, root: Path) -> Path:
    try:
        resolved = (root / path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.path_invalid",
            "Delivery prepare artifact path is invalid.",
            path,
        ) from exc
    if not _path_is_within(resolved, root):
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.path_outside_project",
            "Delivery prepare artifact path must stay inside the project root.",
            path,
        )
    return resolved


def _lexical_artifact_paths(
    draft: DeliveryAuthoringDraft,
    output_dir: str | Path,
    root: Path,
    *,
    skip_unsafe_unit_ids: bool,
) -> _LexicalArtifactPaths:
    output_path = _lexical_project_relative_path(output_dir, root)
    units_dir = output_path / "units"
    unit_task_paths: dict[str, Path] = {}
    for unit in draft.units:
        if skip_unsafe_unit_ids and not _unit_id_can_form_lexical_path(unit.id):
            continue
        unit_task_paths[unit.id] = units_dir / f"{unit.id}.md"
    return _LexicalArtifactPaths(
        output_path=output_path,
        plan_file=output_path / "plan.yaml",
        units_dir=units_dir,
        unit_task_paths=unit_task_paths,
    )


def _lexical_project_relative_path(path: str | Path, root: Path) -> Path:
    raw_path = Path(path)
    if raw_path.is_absolute():
        return raw_path
    return root / raw_path


def _unit_id_can_form_lexical_path(unit_id: str) -> bool:
    return (
        isinstance(unit_id, str)
        and bool(unit_id)
        and unit_id not in {".", ".."}
        and "/" not in unit_id
        and "\\" not in unit_id
        and not _is_absolute_path(unit_id)
        and not _has_parent_traversal(unit_id)
    )


def _validate_lexical_artifact_symlinks(paths: _LexicalArtifactPaths, *, root: Path) -> None:
    _reject_symlink_component(
        paths.output_path,
        root,
        code="delivery_prepare.output_symlink",
        message="Output directory must not be a symlink.",
    )
    _reject_symlink_component(
        paths.plan_file,
        root,
        code="delivery_prepare.symlink_artifact",
        message="Delivery prepare refuses to replace symlink artifacts.",
    )
    _reject_symlink_component(
        paths.units_dir,
        root,
        code="delivery_prepare.units_dir_symlink",
        message="Units directory must not be a symlink.",
    )
    for unit_path in paths.unit_task_paths.values():
        _reject_symlink_component(
            unit_path,
            root,
            code="delivery_prepare.symlink_artifact",
            message="Delivery prepare refuses to replace symlink artifacts.",
        )


def _reject_symlink_component(path: Path, root: Path, *, code: str, message: str) -> None:
    for component in _existing_lexical_components(path, root):
        if component.is_symlink():
            raise _DeliveryPrepareWriteError(code, message, _project_relative_path(component, root))


def _existing_lexical_components(path: Path, root: Path) -> list[Path]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return [path]

    components: list[Path] = []
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        components.append(current)
    return components


def _validate_artifact_paths(
    paths: DeliveryAuthoringDerivedPaths,
    *,
    root: Path,
    output_path: Path,
) -> None:
    plan_file = _resolve_project_artifact(paths.plan_file, root)
    units_dir = _resolve_project_artifact(paths.units_dir, root)
    if not _path_is_within(plan_file, output_path) or not _path_is_within(units_dir, output_path):
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.path_outside_output",
            "Delivery prepare artifact paths must stay inside the selected output directory.",
        )
    target_keys: set[str] = set()
    for rel_path in [paths.plan_file, *paths.unit_task_paths.values()]:
        if _has_parent_traversal(rel_path):
            raise _DeliveryPrepareWriteError(
                "delivery_prepare.path_traversal",
                "Delivery prepare artifact paths must not contain parent-directory traversal.",
                rel_path,
            )
        target = _resolve_project_artifact(rel_path, root)
        key = target.as_posix().casefold()
        if key in target_keys:
            raise _DeliveryPrepareWriteError(
                "delivery_prepare.path_collision",
                "Delivery prepare artifact paths must not collide.",
                rel_path,
            )
        target_keys.add(key)


def _check_unit_readiness(
    draft: DeliveryAuthoringDraft,
    unit_task_paths: dict[str, str],
    *,
    project_config: dict | None,
) -> DeliveryPrepareUnitReadinessAggregate:
    units: list[DeliveryPrepareUnitReadinessSummary] = []
    for unit in draft.units:
        result = check_contract(
            unit.task_markdown,
            source_path=unit_task_paths[unit.id],
            source_format="markdown",
            project_config=project_config,
            document_kind="task_description",
        )
        units.append(_unit_readiness_summary(unit.id, unit_task_paths[unit.id], result))
    status = _UNIT_READINESS_BLOCKED if any(unit.blocking_gap_count > 0 for unit in units) else _UNIT_READINESS_READY
    return DeliveryPrepareUnitReadinessAggregate(status=status, units=units)


def _unit_readiness_summary(
    unit_id: str,
    path: str,
    result: ContractCheckResult,
) -> DeliveryPrepareUnitReadinessSummary:
    blocking_gap_ids = [gap.id for gap in result.gaps if gap.severity == "blocking"]
    warning_gap_count = len([gap for gap in result.gaps if gap.severity != "blocking"])
    return DeliveryPrepareUnitReadinessSummary(
        unit_id=unit_id,
        path=path,
        readiness_score=result.readiness_score,
        status=result.status,
        ready_for_autonomous_delivery=result.ready_for_autonomous_delivery and not blocking_gap_ids,
        blocking_gap_count=len(blocking_gap_ids),
        warning_gap_count=warning_gap_count,
        blocking_gap_ids=blocking_gap_ids,
    )


def _render_plan_yaml(draft: DeliveryAuthoringDraft, unit_task_paths: dict[str, str]) -> str:
    plan_data: dict[str, Any] = {
        "schema_version": SUPPORTED_DELIVERY_PLAN_SCHEMA_VERSION,
        "plan_id": draft.plan_id,
        "title": draft.title,
    }
    if draft.planning_mode:
        plan_data["planning_mode"] = draft.planning_mode
    plan_data["final_branch"] = delivery_final_branch_for_plan_id(draft.plan_id)
    plan_data["repositories"] = [{"id": "main", "root": "."}]
    streams = _distinct_streams(draft)
    if streams:
        plan_data["streams"] = streams
    plan_data["units"] = [_unit_plan_entry(unit, unit_task_paths[unit.id]) for unit in draft.units]
    return yaml.safe_dump(plan_data, sort_keys=False, default_flow_style=False)


def _distinct_streams(draft: DeliveryAuthoringDraft) -> list[str]:
    seen: set[str] = set()
    streams: list[str] = []
    for unit in draft.units:
        if not unit.stream or unit.stream in seen:
            continue
        seen.add(unit.stream)
        streams.append(unit.stream)
    return streams


def _unit_plan_entry(unit: DeliveryAuthoringUnitDraft, task_path: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": unit.id,
        "title": unit.title,
        "task_path": task_path,
        "depends_on": list(unit.depends_on),
    }
    for key in ("stream", "platform", "phase", "kind"):
        value = getattr(unit, key)
        if value:
            entry[key] = value
    if unit.scope_paths:
        entry["scope_paths"] = list(unit.scope_paths)
    return entry


def _artifact_targets(
    draft: DeliveryAuthoringDraft,
    *,
    paths: DeliveryAuthoringDerivedPaths,
    lexical_paths: _LexicalArtifactPaths,
    plan_yaml: str,
) -> list[_ArtifactTarget]:
    targets = [
        _ArtifactTarget(
            "plan",
            paths.plan_file,
            lexical_paths.plan_file,
            plan_yaml,
        )
    ]
    for unit in draft.units:
        rel_path = paths.unit_task_paths[unit.id]
        targets.append(
            _ArtifactTarget(
                "unit_task",
                rel_path,
                lexical_paths.unit_task_paths[unit.id],
                unit.task_markdown.rstrip("\n") + "\n",
            )
        )
    return targets


def _validate_filesystem_targets(
    targets: list[_ArtifactTarget],
    *,
    output_path: Path,
    units_dir: Path,
    root: Path,
    force: bool,
) -> None:
    if output_path.is_symlink():
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.output_symlink",
            "Output directory must not be a symlink.",
            _project_relative_path(output_path, root),
        )
    if output_path.exists() and not output_path.is_dir():
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.output_not_directory",
            "Output path already exists and is not a directory.",
            _project_relative_path(output_path, root),
        )
    if units_dir.is_symlink():
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.units_dir_symlink",
            "Units directory must not be a symlink.",
            _project_relative_path(units_dir, root),
        )
    if units_dir.exists() and not units_dir.is_dir():
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.units_dir_not_directory",
            "Units path already exists and is not a directory.",
            _project_relative_path(units_dir, root),
        )
    existing_artifacts = _existing_delivery_artifacts(output_path / "plan.yaml", units_dir, root)
    symlink_artifact = next((artifact for artifact in existing_artifacts if artifact.path.is_symlink()), None)
    if symlink_artifact is not None:
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.symlink_artifact",
            "Delivery prepare refuses to replace symlink artifacts.",
            symlink_artifact.rel_path,
        )
    for target in targets:
        if target.path.exists() and not target.path.is_file():
            raise _DeliveryPrepareWriteError(
                "delivery_prepare.target_not_file",
                "Delivery prepare target already exists and is not a regular file.",
                target.rel_path,
            )
    if existing_artifacts and not force:
        first_path = existing_artifacts[0].rel_path
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.existing_artifacts",
            "Existing delivery plan artifacts require force to replace.",
            first_path,
        )


@dataclass(frozen=True)
class _ExistingArtifact:
    rel_path: str
    path: Path


def _existing_delivery_artifacts(plan_file: Path, units_dir: Path, root: Path) -> list[_ExistingArtifact]:
    artifacts: list[_ExistingArtifact] = []
    if plan_file.exists() or plan_file.is_symlink():
        artifacts.append(_ExistingArtifact(_project_relative_path(plan_file, root), plan_file))
    if units_dir.is_symlink():
        artifacts.append(_ExistingArtifact(_project_relative_path(units_dir, root), units_dir))
        return artifacts
    if units_dir.exists() and not units_dir.is_dir():
        artifacts.append(_ExistingArtifact(_project_relative_path(units_dir, root), units_dir))
        return artifacts
    if units_dir.is_dir():
        for unit_path in _iter_unit_artifacts(units_dir, root):
            artifacts.append(_ExistingArtifact(_project_relative_path(unit_path, root), unit_path))
    return artifacts


def _iter_unit_artifacts(units_dir: Path, root: Path) -> list[Path]:
    artifacts: list[Path] = []
    pending = [units_dir]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir())
        except OSError as exc:
            raise _DeliveryPrepareWriteError(
                "delivery_prepare.artifact_scan_failed",
                "Delivery prepare failed while scanning existing artifacts.",
                _project_relative_path(current, root),
            ) from exc
        for child in children:
            if child.is_symlink() or child.is_file():
                artifacts.append(child)
            elif child.is_dir():
                pending.append(child)
    return sorted(artifacts)


def _write_targets(targets: list[_ArtifactTarget], *, root: Path) -> _WriteTransaction:
    backups = [_backup_target(target.path) for target in targets]
    created_dirs = _ensure_target_directories(targets, root=root)
    try:
        for target in targets:
            target.path.write_text(target.content, encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as exc:
        rollback_errors = _rollback_targets(backups, created_dirs, root=root)
        written_artifacts = [DeliveryPrepareWrittenArtifact(target.kind, target.rel_path) for target in targets]
        issues = [
            DeliveryPrepareWriteIssue(
                "error",
                "delivery_prepare.write_failed",
                "Delivery prepare failed while writing artifacts.",
            )
        ]
        issues.extend(rollback_errors)
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.write_failed",
            "Delivery prepare failed while writing artifacts.",
            issues=issues,
            written_artifacts=written_artifacts if rollback_errors else [],
        ) from exc
    return _WriteTransaction(
        written_artifacts=[DeliveryPrepareWrittenArtifact(target.kind, target.rel_path) for target in targets],
        backups=backups,
        created_dirs=created_dirs,
    )


def _backup_target(path: Path) -> _ArtifactBackup:
    if not path.exists():
        return _ArtifactBackup(path=path, existed=False)
    try:
        stat_result = path.stat()
        return _ArtifactBackup(
            path=path,
            existed=True,
            content=path.read_bytes(),
            mode=stat_result.st_mode,
        )
    except OSError as exc:
        raise _DeliveryPrepareWriteError(
            "delivery_prepare.backup_failed",
            "Delivery prepare failed while preparing artifact backups.",
        ) from exc


def _ensure_target_directories(targets: list[_ArtifactTarget], *, root: Path) -> list[Path]:
    created: list[Path] = []
    directories = sorted({target.path.parent for target in targets}, key=lambda path: len(path.parts))
    for directory in directories:
        missing = _missing_directories(directory)
        created.extend(missing)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            rollback_errors = _rollback_targets([], created, root=root)
            issues = [
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.mkdir_failed",
                    "Delivery prepare failed while creating artifact directories.",
                )
            ]
            issues.extend(rollback_errors)
            raise _DeliveryPrepareWriteError(
                "delivery_prepare.mkdir_failed",
                "Delivery prepare failed while creating artifact directories.",
                issues=issues,
            ) from exc
    return created


def _missing_directories(directory: Path) -> list[Path]:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return missing


def _rollback_targets(
    backups: list[_ArtifactBackup],
    created_dirs: list[Path],
    *,
    root: Path,
) -> list[DeliveryPrepareWriteIssue]:
    issues: list[DeliveryPrepareWriteIssue] = []
    for backup in reversed(backups):
        try:
            if backup.existed:
                if backup.path.is_symlink():
                    backup.path.unlink()
                backup.path.write_bytes(backup.content or b"")
                if backup.mode is not None:
                    backup.path.chmod(backup.mode)
            elif backup.path.exists() or backup.path.is_symlink():
                backup.path.unlink()
        except OSError:
            issues.append(_rollback_failed_issue(backup.path, root))
    for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            issues.append(_rollback_failed_issue(directory, root))
    return issues


def _rollback_failed_issue(path: Path, root: Path) -> DeliveryPrepareWriteIssue:
    return DeliveryPrepareWriteIssue(
        "error",
        "delivery_prepare.rollback_failed",
        _ROLLBACK_FAILED_MESSAGE,
        _project_relative_path(path, root),
    )


def _plan_validation_summary(result: Any, root: Path) -> DeliveryPreparePlanValidationSummary:
    valid = bool(result.valid)
    return DeliveryPreparePlanValidationSummary(
        status=_PLAN_VALIDATION_VALID if valid else _PLAN_VALIDATION_INVALID,
        valid=valid,
        errors=[_safe_plan_issue(issue, root) for issue in result.errors],
        warnings=[_safe_plan_issue(issue, root) for issue in result.warnings],
    )


def _safe_plan_issue(issue: Any, root: Path) -> dict[str, Any]:
    data = issue.to_dict()
    message = data.get("message")
    if isinstance(message, str):
        data["message"] = _redact_project_root(message, root)
    path = data.get("path")
    if isinstance(path, str):
        data["path"] = _redact_project_root(path, root)
    return data


def _redact_project_root(value: str, root: Path) -> str:
    root_text = str(root)
    root_posix = root.as_posix()
    return value.replace(root_text, ".").replace(root_posix, ".")


def _is_absolute_path(path_value: str | Path) -> bool:
    raw = str(path_value)
    windows_path = PureWindowsPath(raw)
    return Path(raw).is_absolute() or bool(windows_path.drive or windows_path.root)


def _has_parent_traversal(path_value: str | Path) -> bool:
    raw = str(path_value)
    return ".." in PurePosixPath(raw).parts or ".." in PureWindowsPath(raw).parts


def _is_forbidden_output_root(path_value: str | Path) -> bool:
    raw = str(path_value)
    return _has_forbidden_output_parts(PurePosixPath(raw).parts) or (
        _has_forbidden_output_parts(PureWindowsPath(raw).parts)
    )


def _has_forbidden_output_parts(parts: tuple[str, ...]) -> bool:
    normalized = tuple(part.casefold() for part in parts if part not in {"", "."})
    return any(normalized[: len(root)] == root for root in _FORBIDDEN_OUTPUT_ROOTS)


def _project_relative_path(path: Path, root: Path) -> str:
    try:
        root_path = root if root.is_absolute() else root.resolve()
        candidate = path if path.is_absolute() else root_path / path
        rel_path = candidate.relative_to(root_path).as_posix()
        if _has_parent_traversal(rel_path):
            return "<redacted>"
        return rel_path
    except (OSError, RuntimeError, ValueError):
        return "<redacted>"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True
