"""Fail-closed resolution of delivery-unit production write scope."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from core.delivery_public_metadata import is_safe_delivery_public_metadata

if TYPE_CHECKING:
    from core.state import TaskState


SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION = 2
SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION = 1
DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT = "repository_default"
DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT = "unit_explicit"
DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND = "bound"
DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED = "denied"


class DeliveryWriteScopeError(ValueError):
    """Raised when a delivery write scope is malformed or has no safe intersection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryWriteScope:
    schema_version: int
    mode: str
    declared_paths: tuple[str, ...]
    declared_exact_file_paths: tuple[str, ...]
    effective_paths: tuple[str, ...]
    effective_exact_file_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "declared_paths": list(self.declared_paths),
            "declared_exact_file_paths": list(self.declared_exact_file_paths),
            "effective_paths": list(self.effective_paths),
            "effective_exact_file_paths": list(self.effective_exact_file_paths),
        }


@dataclass(frozen=True)
class DeliveryRuntimeWriteRoot:
    path: str
    resolved_path: str
    exact_file: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "resolved_path": self.resolved_path,
            "exact_file": self.exact_file,
        }


@dataclass(frozen=True)
class DeliveryRuntimeWriteScopeBinding:
    schema_version: int
    status: str
    roots: tuple[DeliveryRuntimeWriteRoot, ...]

    @property
    def effective_paths(self) -> tuple[str, ...]:
        return tuple(root.path for root in self.roots)

    @property
    def effective_exact_file_paths(self) -> tuple[str, ...]:
        return tuple(root.path for root in self.roots if root.exact_file)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "roots": [root.to_dict() for root in self.roots],
        }


@dataclass(frozen=True)
class _ScopePath:
    value: str
    resolved: Path
    exact_file: bool

    def contains(self, other: _ScopePath) -> bool:
        if self.resolved == other.resolved:
            return True
        if self.exact_file:
            return False
        return self.resolved in other.resolved.parents


def resolve_delivery_write_scope(
    *,
    project_root: Path,
    configured_write_paths: Sequence[str],
    unit_scope_paths: Sequence[str] | None,
) -> DeliveryWriteScope:
    """Resolve the bounded production write scope for one delivery child."""
    root = _project_root(project_root)
    configured = _canonical_scope_paths(
        configured_write_paths,
        project_root=root,
        code_prefix="delivery_write_scope.configured",
    )
    declared = _canonical_scope_paths(
        unit_scope_paths if unit_scope_paths is not None else (),
        project_root=root,
        code_prefix="delivery_write_scope.declared",
    )
    _reject_explicit_scope_aliases(
        declared,
        project_root=root,
        code="delivery_write_scope.declared_path_alias",
    )

    if not declared:
        effective = _reduce_scope(configured)
        return DeliveryWriteScope(
            schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
            mode=DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT,
            declared_paths=(),
            declared_exact_file_paths=(),
            effective_paths=_scope_values(effective),
            effective_exact_file_paths=_exact_file_values(effective),
        )

    effective = _intersect_scope(configured, declared)
    if not effective:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.empty_intersection",
            "The declared delivery unit scope has no writable intersection with the configured production scope.",
        )

    return DeliveryWriteScope(
        schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
        mode=DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT,
        declared_paths=_scope_values(declared),
        declared_exact_file_paths=_exact_file_values(declared),
        effective_paths=_scope_values(effective),
        effective_exact_file_paths=_exact_file_values(effective),
    )


def validate_delivery_write_scope_snapshot(
    *,
    project_root: Path,
    schema_version: object,
    mode: object,
    declared_paths: object,
    declared_exact_file_paths: object,
    effective_paths: object,
    effective_exact_file_paths: object,
    require_exact_file_match: bool = False,
    validate_current_paths: bool = True,
) -> DeliveryWriteScope | None:
    """Validate a persisted delivery-child write-scope snapshot.

    Legacy state has no marker and empty path lists. Modern state must reproduce a
    canonical resolver result without consulting the current repository-wide write
    policy, so a later config change cannot broaden the persisted child boundary.
    """
    if schema_version is None:
        if (
            mode is None
            and declared_paths == []
            and declared_exact_file_paths is None
            and effective_paths == []
            and effective_exact_file_paths is None
        ):
            return None
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )
    if type(schema_version) is not int or schema_version != SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_schema_unsupported",
            "The delivery write-scope snapshot uses an unsupported schema version.",
        )
    if mode not in (DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT, DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )
    if (
        not isinstance(declared_paths, list)
        or not isinstance(declared_exact_file_paths, list)
        or not isinstance(effective_paths, list)
        or not isinstance(effective_exact_file_paths, list)
    ):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )
    if mode == DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT and declared_paths:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )
    if mode == DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT and (not declared_paths or not effective_paths):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )

    try:
        root = _project_root(project_root)
        declared = _persisted_scope_paths(
            declared_paths,
            exact_file_paths=declared_exact_file_paths,
            project_root=root,
            code_prefix="delivery_write_scope.snapshot_declared",
            require_exact_file_match=require_exact_file_match,
            validate_current_paths=validate_current_paths,
        )
        effective = _persisted_scope_paths(
            effective_paths,
            exact_file_paths=effective_exact_file_paths,
            project_root=root,
            code_prefix="delivery_write_scope.snapshot_effective",
            require_exact_file_match=require_exact_file_match,
            validate_current_paths=validate_current_paths,
        )
        if mode == DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT and validate_current_paths:
            _reject_explicit_scope_aliases(
                declared,
                project_root=root,
                code="delivery_write_scope.snapshot_path_retargeted",
            )
    except DeliveryWriteScopeError as exc:
        if exc.code in {
            "delivery_write_scope.snapshot_path_retargeted",
            "delivery_write_scope.snapshot_path_type_changed",
        }:
            raise
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        ) from exc
    expected_effective = (
        _reduce_scope(effective)
        if mode == DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT
        else _intersect_scope(effective, declared)
    )
    resolved = DeliveryWriteScope(
        schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
        mode=mode,
        declared_paths=_scope_values(declared),
        declared_exact_file_paths=_exact_file_values(declared),
        effective_paths=_scope_values(expected_effective),
        effective_exact_file_paths=_exact_file_values(expected_effective),
    )
    if (
        resolved.declared_paths != tuple(declared_paths)
        or resolved.declared_exact_file_paths != tuple(declared_exact_file_paths)
        or resolved.effective_paths != tuple(effective_paths)
        or resolved.effective_exact_file_paths != tuple(effective_exact_file_paths)
    ):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_invalid",
            "The delivery write-scope snapshot is incomplete or malformed.",
        )
    return resolved


def delivery_write_scope_matches_unit_declaration(
    scope: DeliveryWriteScope,
    unit_scope_paths: Sequence[str] | None,
) -> bool:
    """Return whether a lexical scope snapshot matches its delivery-unit declaration."""
    try:
        declared = _canonical_scope_values(
            unit_scope_paths if unit_scope_paths is not None else (),
            code_prefix="delivery_write_scope.unit_declaration",
        )
    except DeliveryWriteScopeError:
        return False
    expected_mode = (
        DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT if declared else DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT
    )
    return scope.mode == expected_mode and scope.declared_paths == tuple(declared)


def create_delivery_runtime_write_scope_binding(
    *,
    project_root: Path,
    runtime_scope: DeliveryWriteScope,
) -> DeliveryRuntimeWriteScopeBinding:
    """Bind runtime lexical roots to their assembled-worktree identities."""
    root = _project_root(project_root)
    paths = _persisted_scope_paths(
        list(runtime_scope.effective_paths),
        exact_file_paths=list(runtime_scope.effective_exact_file_paths),
        project_root=root,
        code_prefix="delivery_write_scope.runtime_binding",
        require_exact_file_match=True,
    )
    return DeliveryRuntimeWriteScopeBinding(
        schema_version=SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
        status=DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
        roots=tuple(
            DeliveryRuntimeWriteRoot(
                path=path.value,
                resolved_path=_resolved_project_path(path.resolved, root),
                exact_file=path.exact_file,
            )
            for path in paths
        ),
    )


def denied_delivery_runtime_write_scope_binding() -> DeliveryRuntimeWriteScopeBinding:
    """Return explicit evidence that initial runtime scope construction was denied."""
    return DeliveryRuntimeWriteScopeBinding(
        schema_version=SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
        status=DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED,
        roots=(),
    )


def validate_delivery_runtime_write_scope_binding(
    *,
    project_root: Path,
    binding: object,
    upper_bound_paths: Sequence[str],
    upper_bound_exact_file_paths: Sequence[str],
    validate_current_paths: bool = True,
) -> DeliveryRuntimeWriteScopeBinding | None:
    """Validate an immutable lexical-to-resolved runtime scope binding.

    The binding may narrow the stable creation-time upper bound but can never
    broaden it. Runtime callers require every bound lexical root to still resolve
    to the same project identity and preserve its exact-file type. Amendment
    evidence may instead validate persisted identities after that live identity
    caused a terminal scope violation.
    """
    if binding is None:
        return None
    if not isinstance(binding, dict) or set(binding) != {"schema_version", "status", "roots"}:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_invalid",
            "The delivery runtime write-scope binding is incomplete or malformed.",
        )
    schema_version = binding.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION
    ):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_schema_unsupported",
            "The delivery runtime write-scope binding uses an unsupported schema version.",
        )
    status = binding.get("status")
    roots_value = binding.get("roots")
    if status not in (
        DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
        DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED,
    ) or not isinstance(roots_value, list):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_invalid",
            "The delivery runtime write-scope binding is incomplete or malformed.",
        )
    if status == DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED:
        if roots_value:
            raise DeliveryWriteScopeError(
                "delivery_write_scope.runtime_binding_invalid",
                "A denied delivery runtime write-scope binding cannot contain roots.",
            )
        return denied_delivery_runtime_write_scope_binding()

    root = _project_root(project_root)
    upper = _persisted_scope_paths(
        list(upper_bound_paths),
        exact_file_paths=list(upper_bound_exact_file_paths),
        project_root=root,
        code_prefix="delivery_write_scope.runtime_upper_bound",
        require_exact_file_match=False,
        validate_current_paths=validate_current_paths,
    )
    parsed_roots: list[DeliveryRuntimeWriteRoot] = []
    runtime_paths: list[_ScopePath] = []
    lexical_runtime_paths: list[_ScopePath] = []
    for value in roots_value:
        if not isinstance(value, dict) or set(value) != {"path", "resolved_path", "exact_file"}:
            raise DeliveryWriteScopeError(
                "delivery_write_scope.runtime_binding_invalid",
                "A delivery runtime write-scope root is malformed.",
            )
        raw_path = value.get("path")
        resolved_path = value.get("resolved_path")
        exact_file = value.get("exact_file")
        if not isinstance(resolved_path, str) or type(exact_file) is not bool:
            raise DeliveryWriteScopeError(
                "delivery_write_scope.runtime_binding_changed",
                "A delivery runtime write-scope root changed its resolved identity or type.",
            )
        if validate_current_paths:
            path = _canonical_scope_path(
                raw_path,
                project_root=root,
                code_prefix="delivery_write_scope.runtime_binding",
            )
            if resolved_path != _resolved_project_path(path.resolved, root) or (exact_file and not path.exact_file):
                raise DeliveryWriteScopeError(
                    "delivery_write_scope.runtime_binding_changed",
                    "A delivery runtime write-scope root changed its resolved identity or type.",
                )
            resolved_identity = path.resolved
        else:
            try:
                path_value = _canonical_scope_value(
                    raw_path,
                    code_prefix="delivery_write_scope.runtime_binding",
                )
                canonical_resolved_path = _canonical_scope_value(
                    resolved_path,
                    code_prefix="delivery_write_scope.runtime_binding_resolved",
                )
            except DeliveryWriteScopeError as exc:
                raise DeliveryWriteScopeError(
                    "delivery_write_scope.runtime_binding_changed",
                    "A delivery runtime write-scope root changed its persisted identity.",
                ) from exc
            if resolved_path != canonical_resolved_path:
                raise DeliveryWriteScopeError(
                    "delivery_write_scope.runtime_binding_changed",
                    "A delivery runtime write-scope root changed its persisted identity.",
                )
            path = _ScopePath(
                value=path_value,
                resolved=root.joinpath(*PurePosixPath(path_value).parts),
                exact_file=False,
            )
            resolved_identity = root.joinpath(*PurePosixPath(resolved_path).parts)
        parsed_roots.append(
            DeliveryRuntimeWriteRoot(
                path=path.value,
                resolved_path=resolved_path,
                exact_file=exact_file,
            )
        )
        runtime_paths.append(_ScopePath(value=path.value, resolved=resolved_identity, exact_file=exact_file))
        lexical_runtime_paths.append(_ScopePath(value=path.value, resolved=path.resolved, exact_file=exact_file))

    reduced_runtime = _reduce_scope(runtime_paths)
    if [path.value for path in reduced_runtime] != [root.path for root in parsed_roots]:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_invalid",
            "The delivery runtime write-scope binding is not canonical.",
        )
    bounded_paths = lexical_runtime_paths if not validate_current_paths else reduced_runtime
    if any(not any(_typed_scope_contains(bound, path) for bound in upper) for path in bounded_paths):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_broadened",
            "The delivery runtime write-scope binding exceeds its persisted upper bound.",
        )
    return DeliveryRuntimeWriteScopeBinding(
        schema_version=SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
        status=DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
        roots=tuple(parsed_roots),
    )


def apply_delivery_write_scope_to_config(project_config: dict, state: TaskState) -> DeliveryWriteScope | None:
    """Apply a validated delivery-child production scope before runtime construction."""
    try:
        project = project_config.get("project")
        if not isinstance(project, dict):
            raise TypeError("invalid project config")
        project_root_value = project.get("root_path")
        if not isinstance(project_root_value, (str, Path)) or not str(project_root_value):
            raise TypeError("invalid project root")
        project_root = Path(project_root_value)
        scope = validate_delivery_write_scope_snapshot(
            project_root=project_root,
            schema_version=state.delivery_write_scope_schema_version,
            mode=state.delivery_write_scope_mode,
            declared_paths=state.delivery_declared_write_paths,
            declared_exact_file_paths=state.delivery_declared_write_exact_file_paths,
            effective_paths=state.delivery_effective_write_paths,
            effective_exact_file_paths=state.delivery_effective_write_exact_file_paths,
            require_exact_file_match=True,
        )
        runtime_binding = validate_delivery_runtime_write_scope_binding(
            project_root=project_root,
            binding=state.delivery_runtime_write_scope_binding,
            upper_bound_paths=scope.effective_paths if scope is not None else (),
            upper_bound_exact_file_paths=scope.effective_exact_file_paths if scope is not None else (),
        )
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, DeliveryWriteScopeError):
            raise
        raise DeliveryWriteScopeError(
            "delivery_write_scope.config_invalid",
            "The delivery write-scope runtime configuration is invalid.",
        ) from exc

    if scope is None:
        if runtime_binding is not None:
            raise DeliveryWriteScopeError(
                "delivery_write_scope.runtime_binding_unbound",
                "The delivery runtime write-scope binding has no creation-time scope.",
            )
        return None
    if not (state.delivery_plan_id and state.delivery_unit_id):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.snapshot_unbound",
            "The delivery write-scope snapshot is not bound to a delivery child.",
        )
    sandbox = project_config.get("sandbox")
    if not isinstance(sandbox, dict):
        raise DeliveryWriteScopeError(
            "delivery_write_scope.config_invalid",
            "The delivery write-scope runtime configuration is invalid.",
        )
    if not scope.effective_paths and runtime_binding is None:
        sandbox["allowed_write_paths"] = []
        return scope
    try:
        if runtime_binding is None:
            runtime_scope = resolve_delivery_write_scope(
                project_root=project_root,
                configured_write_paths=sandbox.get("allowed_write_paths", []),
                unit_scope_paths=scope.effective_paths,
            )
        else:
            runtime_scope = resolve_delivery_write_scope_from_binding(
                project_root=project_root,
                configured_write_paths=sandbox.get("allowed_write_paths", []),
                upper_bound=scope,
                binding=runtime_binding,
            )
    except DeliveryWriteScopeError as exc:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_intersection_invalid",
            "The persisted delivery scope has no safe intersection with the current production scope.",
        ) from exc
    sandbox["allowed_write_paths"] = list(runtime_scope.effective_paths)
    return runtime_scope


def resolve_delivery_write_scope_from_binding(
    *,
    project_root: Path,
    configured_write_paths: Sequence[str],
    upper_bound: DeliveryWriteScope,
    binding: DeliveryRuntimeWriteScopeBinding,
) -> DeliveryWriteScope:
    """Apply current policy as a monotonic narrowing of an immutable binding."""
    if binding.status == DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_DENIED:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_denied",
            "The delivery runtime write-scope binding records a denied construction.",
        )
    root = _project_root(project_root)
    configured = _canonical_scope_paths(
        configured_write_paths,
        project_root=root,
        code_prefix="delivery_write_scope.configured",
    )
    bound = [
        _ScopePath(
            value=value.path,
            resolved=root.joinpath(*PurePosixPath(value.resolved_path).parts),
            exact_file=value.exact_file,
        )
        for value in binding.roots
    ]
    effective = _intersect_scope(configured, bound)
    if bound and not effective:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_intersection_invalid",
            "The persisted delivery scope has no safe intersection with the current production scope.",
        )
    return DeliveryWriteScope(
        schema_version=upper_bound.schema_version,
        mode=upper_bound.mode,
        declared_paths=upper_bound.declared_paths,
        declared_exact_file_paths=upper_bound.declared_exact_file_paths,
        effective_paths=_scope_values(effective),
        effective_exact_file_paths=_exact_file_values(effective),
    )


def _project_root(project_root: Path) -> Path:
    try:
        root = project_root.resolve()
        if not root.is_dir():
            raise ValueError("project root is not a directory")
        return root
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.project_root_invalid",
            "The delivery project root is invalid.",
        ) from exc


def _resolved_project_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise DeliveryWriteScopeError(
            "delivery_write_scope.runtime_binding_outside_project",
            "A delivery runtime write-scope root escapes the project root.",
        ) from exc
    value = relative.as_posix()
    return value if value else "."


def _reject_explicit_scope_aliases(
    paths: Sequence[_ScopePath],
    *,
    project_root: Path,
    code: str,
) -> None:
    for path in paths:
        lexical = project_root.joinpath(*PurePosixPath(path.value).parts)
        if lexical != path.resolved:
            raise DeliveryWriteScopeError(
                code,
                "Explicit delivery unit scope paths must not use filesystem aliases.",
            )


def _canonical_scope_paths(
    values: Sequence[str],
    *,
    project_root: Path,
    code_prefix: str,
) -> list[_ScopePath]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_paths_invalid",
            "Delivery write scope paths must be a sequence of project-relative strings.",
        )

    result: list[_ScopePath] = []
    seen: set[str] = set()
    for value in values:
        path = _canonical_scope_path(value, project_root=project_root, code_prefix=code_prefix)
        if path.value not in seen:
            result.append(path)
            seen.add(path.value)
    return result


def _persisted_scope_paths(
    values: object,
    *,
    exact_file_paths: object,
    project_root: Path,
    code_prefix: str,
    require_exact_file_match: bool,
    validate_current_paths: bool = True,
) -> list[_ScopePath]:
    if not isinstance(values, list) or not isinstance(exact_file_paths, list):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_paths_invalid",
            "Persisted delivery write scope paths must be canonical lists.",
        )
    if require_exact_file_match and not validate_current_paths:
        raise DeliveryWriteScopeError(
            f"{code_prefix}_paths_invalid",
            "Lexical delivery write scope validation cannot verify current file types.",
        )
    current = (
        _canonical_scope_paths(values, project_root=project_root, code_prefix=code_prefix)
        if validate_current_paths
        else _lexical_scope_paths(values, project_root=project_root, code_prefix=code_prefix)
    )
    if _scope_values(current) != tuple(values):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_paths_invalid",
            "Persisted delivery write scope paths are not canonical.",
        )
    if any(not isinstance(value, str) for value in exact_file_paths):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_exact_files_invalid",
            "Persisted exact-file delivery scope metadata is malformed.",
        )
    exact_file_set = set(exact_file_paths)
    expected_order = tuple(path.value for path in current if path.value in exact_file_set)
    if expected_order != tuple(exact_file_paths):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_exact_files_invalid",
            "Persisted exact-file delivery scope metadata is malformed.",
        )
    if require_exact_file_match:
        changed = next((path for path in current if path.value in exact_file_set and not path.exact_file), None)
        if changed is not None:
            raise DeliveryWriteScopeError(
                "delivery_write_scope.snapshot_path_type_changed",
                "An exact-file delivery scope root changed type in the authoritative worktree.",
            )
    return [
        _ScopePath(value=path.value, resolved=path.resolved, exact_file=path.value in exact_file_set)
        for path in current
    ]


def _canonical_scope_path(
    value: object,
    *,
    project_root: Path,
    code_prefix: str,
) -> _ScopePath:
    canonical = _canonical_scope_value(value, code_prefix=code_prefix)
    try:
        resolved = (project_root / canonical).resolve()
        resolved.relative_to(project_root)
        exact_file = resolved.is_file()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryWriteScopeError(
            f"{code_prefix}_path_outside_project",
            "A delivery write scope path is invalid or escapes the project root.",
        ) from exc
    return _ScopePath(value=canonical, resolved=resolved, exact_file=exact_file)


def _canonical_scope_value(value: object, *, code_prefix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryWriteScopeError(
            f"{code_prefix}_path_invalid",
            "A delivery write scope path is not a non-empty string.",
        )
    raw = value.strip()
    posix_path = PurePosixPath(raw)
    windows_path = PureWindowsPath(raw)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or Path(raw).is_absolute()
    ):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_path_absolute",
            "Delivery write scope paths must be project-relative.",
        )
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise DeliveryWriteScopeError(
            f"{code_prefix}_path_parent_traversal",
            "Delivery write scope paths must not contain parent-directory traversal.",
        )
    if not is_safe_delivery_public_metadata(raw):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_path_invalid",
            "A delivery write scope path contains unsafe metadata.",
        )

    normalized = windows_path.as_posix() if os.name == "nt" else posix_path.as_posix()
    lexical = PurePosixPath(normalized).as_posix()
    return lexical if lexical else "."


def _canonical_scope_values(values: Sequence[str], *, code_prefix: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DeliveryWriteScopeError(
            f"{code_prefix}_paths_invalid",
            "Delivery write scope paths must be a sequence of project-relative strings.",
        )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = _canonical_scope_value(value, code_prefix=code_prefix)
        if canonical not in seen:
            result.append(canonical)
            seen.add(canonical)
    return result


def _lexical_scope_paths(
    values: Sequence[str],
    *,
    project_root: Path,
    code_prefix: str,
) -> list[_ScopePath]:
    return [
        _ScopePath(
            value=value,
            resolved=project_root.joinpath(*PurePosixPath(value).parts),
            exact_file=False,
        )
        for value in _canonical_scope_values(values, code_prefix=code_prefix)
    ]


def _reduce_scope(paths: Sequence[_ScopePath]) -> list[_ScopePath]:
    ordered = sorted(paths, key=lambda path: (len(path.resolved.parts), path.value))
    result: list[_ScopePath] = []
    for path in ordered:
        if any(existing.contains(path) for existing in result):
            continue
        result.append(path)
    return result


def _intersect_scope(configured: Sequence[_ScopePath], declared: Sequence[_ScopePath]) -> list[_ScopePath]:
    intersections: list[_ScopePath] = []
    for configured_path in configured:
        for declared_path in declared:
            if configured_path.contains(declared_path):
                intersections.append(declared_path)
            elif declared_path.contains(configured_path):
                intersections.append(configured_path)
    return _reduce_scope(intersections)


def _scope_values(paths: Sequence[_ScopePath]) -> tuple[str, ...]:
    return tuple(path.value for path in paths)


def _exact_file_values(paths: Sequence[_ScopePath]) -> tuple[str, ...]:
    return tuple(path.value for path in paths if path.exact_file)


def _typed_scope_contains(bound: _ScopePath, path: _ScopePath) -> bool:
    if bound.resolved == path.resolved and bound.exact_file and not path.exact_file:
        return False
    return bound.contains(path)
