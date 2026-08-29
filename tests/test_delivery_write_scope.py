"""Tests for delivery-unit production write-scope resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.delivery_write_scope import (
    DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
    DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT,
    DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT,
    SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
    SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
    DeliveryWriteScopeError,
    create_delivery_runtime_write_scope_binding,
    resolve_delivery_write_scope,
    resolve_delivery_write_scope_from_binding,
    validate_delivery_runtime_write_scope_binding,
    validate_delivery_write_scope_snapshot,
)


@pytest.fixture
def scope_project(tmp_path: Path) -> Path:
    for directory in ("agents", "core", "core/nested", "docs"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for file_path in ("README.md", "core/state.py", "pyproject.toml"):
        (tmp_path / file_path).write_text("fixture\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("declared", [None, []])
def test_empty_unit_scope_preserves_repository_default_mode(scope_project: Path, declared: list[str] | None) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["core/", "README.md"],
        unit_scope_paths=declared,
    )

    assert result.schema_version == SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION
    assert result.mode == DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT
    assert result.declared_paths == ()
    assert result.effective_paths == ("README.md", "core")


@pytest.mark.parametrize(
    ("configured", "declared", "expected"),
    [
        (["."], ["core/"], ("core",)),
        (["core/"], ["core/state.py"], ("core/state.py",)),
        (["core/state.py"], ["core/"], ("core/state.py",)),
        (["."], ["pyproject.toml"], ("pyproject.toml",)),
        (["core/", "agents/"], ["core/state.py", "agents/"], ("agents", "core/state.py")),
        (["core/"], ["core/nested/"], ("core/nested",)),
    ],
)
def test_explicit_scope_uses_narrower_path_intersections(
    scope_project: Path,
    configured: list[str],
    declared: list[str],
    expected: tuple[str, ...],
) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=configured,
        unit_scope_paths=declared,
    )

    assert result.mode == DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT
    assert result.effective_paths == expected


def test_explicit_scope_retains_normalized_declaration_for_audit(scope_project: Path) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["."],
        unit_scope_paths=["core/", "core/state.py", "core/"],
    )

    assert result.declared_paths == ("core", "core/state.py")
    assert result.effective_paths == ("core",)
    assert result.to_dict() == {
        "schema_version": 2,
        "mode": "unit_explicit",
        "declared_paths": ["core", "core/state.py"],
        "declared_exact_file_paths": ["core/state.py"],
        "effective_paths": ["core"],
        "effective_exact_file_paths": [],
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
def test_existing_posix_backslash_scope_remains_an_exact_file(scope_project: Path) -> None:
    scoped_file = scope_project / "scope\\name"
    scoped_file.write_text("fixture\n", encoding="utf-8")

    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["."],
        unit_scope_paths=["scope\\name"],
    )

    assert result.declared_paths == ("scope\\name",)
    assert result.declared_exact_file_paths == ("scope\\name",)
    assert result.effective_paths == ("scope\\name",)
    assert result.effective_exact_file_paths == ("scope\\name",)


@pytest.mark.skipif(os.name != "nt", reason="Windows treats backslashes as path separators")
def test_windows_backslash_scope_is_normalized_to_posix_metadata(scope_project: Path) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["."],
        unit_scope_paths=["core\\state.py"],
    )

    assert result.declared_paths == ("core/state.py",)
    assert result.declared_exact_file_paths == ("core/state.py",)


def test_default_scope_collapses_redundant_configured_roots(scope_project: Path) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["core/state.py", "core/", "core/nested", "core/"],
        unit_scope_paths=None,
    )

    assert result.effective_paths == ("core",)


def test_nonexistent_scope_roots_use_deterministic_prefix_intersection(scope_project: Path) -> None:
    result = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["generated/"],
        unit_scope_paths=["generated/api/schema.json"],
    )

    assert result.effective_paths == ("generated/api/schema.json",)


def test_existing_file_scope_does_not_authorize_descendants(scope_project: Path) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=scope_project,
            configured_write_paths=["pyproject.toml"],
            unit_scope_paths=["pyproject.toml/generated"],
        )

    assert exc_info.value.code == "delivery_write_scope.empty_intersection"


@pytest.mark.parametrize(
    ("configured", "declared"),
    [
        (["agents/"], ["core/"]),
        ([], ["core/"]),
    ],
)
def test_disjoint_explicit_scope_fails_closed(
    scope_project: Path,
    configured: list[str],
    declared: list[str],
) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=scope_project,
            configured_write_paths=configured,
            unit_scope_paths=declared,
        )

    assert exc_info.value.code == "delivery_write_scope.empty_intersection"
    assert str(scope_project) not in str(exc_info.value)


@pytest.mark.parametrize(
    ("configured", "declared", "expected_code"),
    [
        ("core/", None, "delivery_write_scope.configured_paths_invalid"),
        (["../outside"], None, "delivery_write_scope.configured_path_parent_traversal"),
        (["core/"], ["../outside"], "delivery_write_scope.declared_path_parent_traversal"),
        (["core/"], ["/absolute/path"], "delivery_write_scope.declared_path_absolute"),
        (["core/"], ["C:\\absolute\\path"], "delivery_write_scope.declared_path_absolute"),
        (["core/"], [""], "delivery_write_scope.declared_path_invalid"),
        (["core/"], ["core/unsafe\npath"], "delivery_write_scope.declared_path_invalid"),
    ],
)
def test_malformed_scope_paths_are_rejected(
    scope_project: Path,
    configured: object,
    declared: object,
    expected_code: str,
) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=scope_project,
            configured_write_paths=configured,
            unit_scope_paths=declared,
        )

    assert exc_info.value.code == expected_code


def test_symlink_scope_that_escapes_project_is_rejected(scope_project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (scope_project / "external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=scope_project,
            configured_write_paths=["."],
            unit_scope_paths=["external/"],
        )

    assert exc_info.value.code == "delivery_write_scope.declared_path_outside_project"
    assert str(outside) not in str(exc_info.value)


@pytest.mark.parametrize("scope_path", ["core-alias", "core-alias/nested"])
def test_internal_symlink_unit_scope_is_rejected(scope_project: Path, scope_path: str) -> None:
    alias = scope_project / "core-alias"
    alias.symlink_to("core", target_is_directory=True)

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=scope_project,
            configured_write_paths=["."],
            unit_scope_paths=[scope_path],
        )

    assert exc_info.value.code == "delivery_write_scope.declared_path_alias"


def test_explicit_scope_snapshot_rejects_internal_symlink_retarget(scope_project: Path) -> None:
    alias = scope_project / "alias"
    alias.symlink_to("core", target_is_directory=True)

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_write_scope_snapshot(
            project_root=scope_project,
            schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
            mode=DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT,
            declared_paths=["alias"],
            declared_exact_file_paths=[],
            effective_paths=["alias"],
            effective_exact_file_paths=[],
            require_exact_file_match=True,
        )

    assert exc_info.value.code == "delivery_write_scope.snapshot_path_retargeted"


def test_explicit_scope_snapshot_can_be_validated_as_lexical_failure_evidence(scope_project: Path) -> None:
    alias = scope_project / "alias"
    alias.symlink_to("core", target_is_directory=True)

    scope = validate_delivery_write_scope_snapshot(
        project_root=scope_project,
        schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
        mode=DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT,
        declared_paths=["alias"],
        declared_exact_file_paths=[],
        effective_paths=["alias"],
        effective_exact_file_paths=[],
        validate_current_paths=False,
    )

    assert scope is not None
    assert scope.declared_paths == ("alias",)
    assert scope.effective_paths == ("alias",)


def test_repository_default_symlink_scope_preserves_configured_lexical_root(scope_project: Path) -> None:
    (scope_project / "core-alias").symlink_to("core", target_is_directory=True)

    scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["core-alias/"],
        unit_scope_paths=None,
    )

    assert scope.mode == DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT
    assert scope.effective_paths == ("core-alias",)


def test_project_root_must_be_a_directory(tmp_path: Path) -> None:
    project_file = tmp_path / "project.txt"
    project_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        resolve_delivery_write_scope(
            project_root=project_file,
            configured_write_paths=["."],
            unit_scope_paths=None,
        )

    assert exc_info.value.code == "delivery_write_scope.project_root_invalid"


def test_legacy_snapshot_without_marker_remains_unscoped(scope_project: Path) -> None:
    result = validate_delivery_write_scope_snapshot(
        project_root=scope_project,
        schema_version=None,
        mode=None,
        declared_paths=[],
        declared_exact_file_paths=None,
        effective_paths=[],
        effective_exact_file_paths=None,
    )

    assert result is None


@pytest.mark.parametrize(
    ("mode", "declared", "declared_exact", "effective", "effective_exact"),
    [
        (DELIVERY_WRITE_SCOPE_MODE_REPOSITORY_DEFAULT, [], [], ["core"], []),
        (DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT, ["core"], [], ["core/state.py"], ["core/state.py"]),
    ],
)
def test_modern_snapshot_reproduces_canonical_persisted_scope(
    scope_project: Path,
    mode: str,
    declared: list[str],
    declared_exact: list[str],
    effective: list[str],
    effective_exact: list[str],
) -> None:
    result = validate_delivery_write_scope_snapshot(
        project_root=scope_project,
        schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
        mode=mode,
        declared_paths=declared,
        declared_exact_file_paths=declared_exact,
        effective_paths=effective,
        effective_exact_file_paths=effective_exact,
    )

    assert result is not None
    assert result.mode == mode
    assert result.declared_paths == tuple(declared)
    assert result.effective_paths == tuple(effective)


@pytest.mark.parametrize(
    ("schema_version", "mode", "declared", "effective", "expected_code"),
    [
        (None, None, ["core"], [], "delivery_write_scope.snapshot_invalid"),
        (1, "unit_explicit", ["core"], ["core"], "delivery_write_scope.snapshot_schema_unsupported"),
        (2, "unknown", [], [], "delivery_write_scope.snapshot_invalid"),
        (2, "repository_default", ["core"], ["core"], "delivery_write_scope.snapshot_invalid"),
        (2, "unit_explicit", [], [], "delivery_write_scope.snapshot_invalid"),
        (2, "unit_explicit", ["core"], ["agents"], "delivery_write_scope.snapshot_invalid"),
        (2, "unit_explicit", ["core/"], ["core/state.py"], "delivery_write_scope.snapshot_invalid"),
    ],
)
def test_malformed_or_noncanonical_snapshot_fails_closed(
    scope_project: Path,
    schema_version: object,
    mode: object,
    declared: object,
    effective: object,
    expected_code: str,
) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_write_scope_snapshot(
            project_root=scope_project,
            schema_version=schema_version,
            mode=mode,
            declared_paths=declared,
            declared_exact_file_paths=[],
            effective_paths=effective,
            effective_exact_file_paths=[],
        )

    assert exc_info.value.code == expected_code
    assert str(scope_project) not in str(exc_info.value)


def test_runtime_scope_binding_can_narrow_but_not_broaden_upper_bound(scope_project: Path) -> None:
    runtime_scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["core/state.py"],
        unit_scope_paths=["core"],
    )
    binding = create_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        runtime_scope=runtime_scope,
    )
    narrowed = validate_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        binding=binding.to_dict(),
        upper_bound_paths=["core"],
        upper_bound_exact_file_paths=[],
    )

    assert narrowed is not None
    assert narrowed.effective_paths == ("core/state.py",)

    broadened = binding.to_dict()
    broadened["roots"] = [{"path": "docs", "resolved_path": "docs", "exact_file": False}]
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding=broadened,
            upper_bound_paths=["core"],
            upper_bound_exact_file_paths=[],
        )

    assert exc_info.value.code == "delivery_write_scope.runtime_binding_broadened"


@pytest.mark.parametrize(
    ("binding", "expected_code"),
    [
        ({"schema_version": 1, "status": "bound"}, "delivery_write_scope.runtime_binding_invalid"),
        (
            {"schema_version": 999, "status": "bound", "roots": []},
            "delivery_write_scope.runtime_binding_schema_unsupported",
        ),
        (
            {
                "schema_version": 1,
                "status": "bound",
                "roots": [
                    {"path": "core/state.py", "resolved_path": "core/state.py", "exact_file": True},
                    {"path": "core", "resolved_path": "core", "exact_file": False},
                ],
            },
            "delivery_write_scope.runtime_binding_invalid",
        ),
    ],
)
def test_runtime_scope_binding_rejects_malformed_schema_and_noncanonical_roots(
    scope_project: Path,
    binding: object,
    expected_code: str,
) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding=binding,
            upper_bound_paths=["core"],
            upper_bound_exact_file_paths=[],
        )

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_runtime_revalidation_rejects_exact_file_type_drift(scope_project: Path, replacement: str) -> None:
    scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["."],
        unit_scope_paths=["pyproject.toml"],
    )
    target = scope_project / "pyproject.toml"
    target.unlink()
    if replacement == "directory":
        target.mkdir()

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_write_scope_snapshot(
            project_root=scope_project,
            schema_version=scope.schema_version,
            mode=scope.mode,
            declared_paths=list(scope.declared_paths),
            declared_exact_file_paths=list(scope.declared_exact_file_paths),
            effective_paths=list(scope.effective_paths),
            effective_exact_file_paths=list(scope.effective_exact_file_paths),
            require_exact_file_match=True,
        )

    assert exc_info.value.code == "delivery_write_scope.snapshot_path_type_changed"


def test_runtime_binding_rejects_exact_file_replayed_as_prefix(scope_project: Path) -> None:
    binding = {
        "schema_version": SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
        "status": DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
        "roots": [{"path": "pyproject.toml", "resolved_path": "pyproject.toml", "exact_file": False}],
    }
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding=binding,
            upper_bound_paths=["pyproject.toml"],
            upper_bound_exact_file_paths=["pyproject.toml"],
        )

    assert exc_info.value.code == "delivery_write_scope.runtime_binding_broadened"


@pytest.mark.parametrize(
    ("declared_exact", "effective_exact"),
    [
        (None, []),
        ([], [1]),
        ([], ["agents"]),
    ],
)
def test_snapshot_rejects_missing_or_malformed_exact_file_metadata(
    scope_project: Path,
    declared_exact: object,
    effective_exact: object,
) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_write_scope_snapshot(
            project_root=scope_project,
            schema_version=SUPPORTED_DELIVERY_WRITE_SCOPE_SCHEMA_VERSION,
            mode=DELIVERY_WRITE_SCOPE_MODE_UNIT_EXPLICIT,
            declared_paths=["core"],
            declared_exact_file_paths=declared_exact,
            effective_paths=["core"],
            effective_exact_file_paths=effective_exact,
        )

    assert exc_info.value.code == "delivery_write_scope.snapshot_invalid"


def test_runtime_binding_requires_typed_exact_file_metadata(scope_project: Path) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding={
                "schema_version": SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
                "status": DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
                "roots": [{"path": "core", "resolved_path": "core", "exact_file": None}],
            },
            upper_bound_paths=["core"],
            upper_bound_exact_file_paths=[],
        )

    assert exc_info.value.code == "delivery_write_scope.runtime_binding_changed"


def test_runtime_binding_is_immutable_when_internal_symlink_is_retargeted(scope_project: Path) -> None:
    alias = scope_project / "alias"
    alias.symlink_to("core", target_is_directory=True)
    runtime_scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["alias"],
        unit_scope_paths=None,
    )
    binding = create_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        runtime_scope=runtime_scope,
    )

    alias.unlink()
    alias.symlink_to("docs", target_is_directory=True)

    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding=binding.to_dict(),
            upper_bound_paths=["alias"],
            upper_bound_exact_file_paths=[],
        )

    assert exc_info.value.code == "delivery_write_scope.runtime_binding_changed"


def test_runtime_binding_preserves_original_identity_for_terminal_evidence(scope_project: Path) -> None:
    alias = scope_project / "alias"
    alias.symlink_to("core", target_is_directory=True)
    runtime_scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["alias"],
        unit_scope_paths=None,
    )
    binding = create_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        runtime_scope=runtime_scope,
    )
    alias.unlink()
    alias.symlink_to("docs", target_is_directory=True)

    validated = validate_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        binding=binding.to_dict(),
        upper_bound_paths=["alias"],
        upper_bound_exact_file_paths=[],
        validate_current_paths=False,
    )

    assert validated == binding


def test_terminal_evidence_runtime_binding_cannot_broaden_lexical_upper_bound(scope_project: Path) -> None:
    with pytest.raises(DeliveryWriteScopeError) as exc_info:
        validate_delivery_runtime_write_scope_binding(
            project_root=scope_project,
            binding={
                "schema_version": SUPPORTED_DELIVERY_RUNTIME_WRITE_SCOPE_BINDING_SCHEMA_VERSION,
                "status": DELIVERY_RUNTIME_WRITE_SCOPE_STATUS_BOUND,
                "roots": [{"path": "docs", "resolved_path": "docs", "exact_file": False}],
            },
            upper_bound_paths=["core"],
            upper_bound_exact_file_paths=[],
            validate_current_paths=False,
        )

    assert exc_info.value.code == "delivery_write_scope.runtime_binding_broadened"


def test_current_policy_can_only_narrow_an_existing_runtime_binding(scope_project: Path) -> None:
    upper_bound = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["core"],
        unit_scope_paths=["core"],
    )
    binding = create_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        runtime_scope=upper_bound,
    )

    narrowed = resolve_delivery_write_scope_from_binding(
        project_root=scope_project,
        configured_write_paths=["core/state.py"],
        upper_bound=upper_bound,
        binding=binding,
    )
    broadened_config = resolve_delivery_write_scope_from_binding(
        project_root=scope_project,
        configured_write_paths=["."],
        upper_bound=upper_bound,
        binding=binding,
    )

    assert narrowed.effective_paths == ("core/state.py",)
    assert broadened_config.effective_paths == ("core",)


def test_runtime_prefix_binding_allows_nonexistent_root_to_become_exact_file(scope_project: Path) -> None:
    runtime_scope = resolve_delivery_write_scope(
        project_root=scope_project,
        configured_write_paths=["generated.py"],
        unit_scope_paths=["generated.py"],
    )
    binding = create_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        runtime_scope=runtime_scope,
    )
    (scope_project / "generated.py").write_text("generated\n", encoding="utf-8")

    validated = validate_delivery_runtime_write_scope_binding(
        project_root=scope_project,
        binding=binding.to_dict(),
        upper_bound_paths=["generated.py"],
        upper_bound_exact_file_paths=[],
    )

    assert validated == binding
