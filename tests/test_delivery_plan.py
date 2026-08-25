from __future__ import annotations

import argparse
from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_plan import (
    MAX_DELIVERY_CONSTRAINT_UNIT_IDS,
    MAX_DELIVERY_CONSTRAINTS,
    MAX_DELIVERY_UNIT_ID_LENGTH,
    DeliveryPlanIssue,
    check_delivery_plan_file,
    delivery_unit_constraint_context,
    is_valid_delivery_branch_name,
    render_delivery_plan_check,
)
from sikula import main
from sikula_cli.delivery import cmd_delivery_check


def test_delivery_cli_module_imports() -> None:
    import sikula_cli.delivery as delivery_cli

    assert callable(delivery_cli.register_parser)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_unit(root: Path, name: str, text: str = "# Unit\n") -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path, data: dict) -> Path:
    path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_source_task(root: Path, text: str = "# Source task\n") -> dict[str, str]:
    path = root / ".sikula" / "tasks" / "source-task.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": "sha256:" + sha256(text.encode("utf-8")).hexdigest(),
    }


def _base_plan(root: Path) -> dict:
    unit_1 = _write_unit(root, "01-domain.md")
    unit_2 = _write_unit(root, "02-api.md")
    return {
        "schema_version": 1,
        "plan_id": "checkout-redesign",
        "title": "Checkout redesign",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/checkout-redesign",
        "streams": [{"id": "backend", "label": "Backend"}],
        "units": [
            {
                "id": "01-domain",
                "title": "Add checkout domain model",
                "stream": "backend",
                "platform": "shared",
                "task_path": unit_1,
                "depends_on": [],
            },
            {
                "id": "02-api",
                "title": "Add checkout API",
                "stream": "backend",
                "platform": "shared",
                "task_path": unit_2,
                "depends_on": ["01-domain"],
            },
        ],
    }


@pytest.mark.parametrize(
    ("metadata", "expected_code"),
    [
        pytest.param(
            {"amend_reason": "not a stable code"},
            "amendment.amend_reason_invalid",
            id="amend_reason",
        ),
        pytest.param(
            {"budget_exceeded": []},
            "amendment.budget_exceeded_invalid",
            id="budget_type",
        ),
        pytest.param(
            {
                "budget_exceeded": {
                    "name": "max_planner_steps",
                    "limit": 2,
                    "actual": 5,
                    "details": "private",
                }
            },
            "amendment.budget_exceeded_unknown_field",
            id="budget_unknown_field",
        ),
        pytest.param(
            {"budget_exceeded": {"name": "not a code", "limit": -1, "actual": -2}},
            "amendment.budget_name_invalid",
            id="budget_values",
        ),
    ],
)
def test_delivery_plan_rejects_invalid_amendment_metadata(
    tmp_path: Path,
    metadata: dict,
    expected_code: str,
) -> None:
    data = _base_plan(tmp_path)
    data["units"][0].update(metadata)

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is False
    assert expected_code in {issue.code for issue in result.errors}


def _codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def test_delivery_plan_check_accepts_valid_single_repo_plan(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.plan_id == "checkout-redesign"
    assert result.plan.repositories[0].id == "main"
    assert result.plan.repositories[0].implicit is True
    assert result.plan.units[1].depends_on == ["01-domain"]
    rendered = render_delivery_plan_check(result)
    assert "Status: valid" in rendered
    assert "Units: 2" in rendered


def test_delivery_plan_constraint_limit_does_not_limit_existing_unit_capacity(tmp_path: Path) -> None:
    data = _base_plan(tmp_path)
    data["units"] = [
        {
            "id": f"unit-{index}",
            "task_path": _write_unit(tmp_path, f"unit-{index}.md"),
            "depends_on": [],
        }
        for index in range(101)
    ]

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is True
    assert result.plan is not None
    assert len(result.plan.units) == 101


def test_delivery_plan_check_preserves_source_task_and_inherited_constraints(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path)
    data["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Protocol changes remain owned by the external protocol project.",
            "unit_ids": ["01-domain", "02-api"],
            "disposition": "preserved",
        }
    ]

    result = check_delivery_plan_file(_write_plan(tmp_path, data))

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.source_task is not None
    assert result.plan.source_task.to_dict() == data["source_task"]
    assert [constraint.to_dict() for constraint in result.plan.constraints] == data["constraints"]
    assert result.to_dict()["plan"]["constraints"] == data["constraints"]


def test_delivery_plan_check_rejects_stale_source_task_fingerprint(tmp_path: Path) -> None:
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path)
    data["source_task"]["sha256"] = "sha256:" + "0" * 64

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is False
    assert "source_task.hash_mismatch" in _codes(result)


def test_delivery_plan_source_task_hash_uses_logical_utf8_text_across_crlf(tmp_path: Path) -> None:
    source_text = "# Source task\n\nPreserve the boundary.\n"
    source_path = tmp_path / ".sikula" / "tasks" / "source-task.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source_text.replace("\n", "\r\n").encode("utf-8"))
    data = _base_plan(tmp_path)
    data["source_task"] = {
        "path": source_path.relative_to(tmp_path).as_posix(),
        "sha256": "sha256:" + sha256(source_text.encode("utf-8")).hexdigest(),
    }

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is True


def test_delivery_plan_check_rejects_and_omits_verbatim_constraint_summary(tmp_path: Path) -> None:
    source_rule = "Only the protocol repository may change protocol files."
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path, f"# Source task\n\n- **{source_rule}**\n")
    data["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "repository_ownership",
            "summary": source_rule,
            "unit_ids": ["01-domain"],
            "disposition": "preserved",
        }
    ]

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)
    serialized = json.dumps(result.to_dict())

    assert result.valid is False
    assert "constraints.summary_source_excerpt" in _codes(result)
    assert source_rule not in serialized


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        pytest.param(
            lambda data: data.update({"source_task": []}),
            "source_task.invalid_type",
            id="source_task_type",
        ),
        pytest.param(
            lambda data: data["source_task"].update({"sha256": "sha256:not-a-hash"}),
            "source_task.sha256_invalid",
            id="source_task_hash",
        ),
        pytest.param(
            lambda data: data.pop("source_task"),
            "constraints.source_task_required",
            id="source_task_required",
        ),
        pytest.param(
            lambda data: data.update({"constraints": {}}),
            "constraints.invalid_type",
            id="constraints_type",
        ),
        pytest.param(
            lambda data: data["constraints"][0].update({"kind": "unknown"}),
            "constraints.kind_invalid",
            id="kind",
        ),
        pytest.param(
            lambda data: data["constraints"][0].update({"summary": "Read /Users/example/private/task.md"}),
            "constraints.summary_invalid",
            id="summary",
        ),
        pytest.param(
            lambda data: data["constraints"][0].update({"unit_ids": ["missing"]}),
            "constraints.unit_unknown",
            id="unit",
        ),
        pytest.param(
            lambda data: data["constraints"][0].update({"unit_ids": ["01-domain", "01-domain"]}),
            "constraints.unit_id_duplicate",
            id="duplicate_unit",
        ),
        pytest.param(
            lambda data: data["constraints"][0].update({"disposition": "conflict"}),
            "constraints.disposition_unresolved",
            id="disposition",
        ),
    ],
)
def test_delivery_plan_check_rejects_invalid_inherited_constraints(
    tmp_path: Path,
    mutator: Callable[[dict], object],
    expected_code: str,
) -> None:
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path)
    data["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Protocol changes remain owned by the external protocol project.",
            "unit_ids": ["01-domain"],
            "disposition": "preserved",
        }
    ]
    mutator(data)

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is False
    assert expected_code in _codes(result)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("unknown_field", "source_task.unknown_field"),
        ("missing_path", "source_task.path.missing"),
        ("unsafe_metadata", "source_task.path_metadata_invalid"),
        ("parent_traversal", "source_task.path_parent_traversal"),
        ("missing_file", "source_task.read_failed"),
    ],
)
def test_delivery_plan_check_rejects_source_task_boundary_cases(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path)
    if case == "unknown_field":
        data["source_task"]["private"] = "unsupported"
    elif case == "missing_path":
        data["source_task"].pop("path")
    elif case == "unsafe_metadata":
        data["source_task"]["path"] = "task.md\nprivate"
    elif case == "parent_traversal":
        data["source_task"]["path"] = ".sikula/tasks/../tasks/source-task.md"
    elif case == "missing_file":
        data["source_task"]["path"] = ".sikula/tasks/missing.md"

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is False
    assert expected_code in _codes(result)


def test_delivery_plan_check_rejects_symlink_and_non_utf8_source_tasks(tmp_path: Path) -> None:
    data = _base_plan(tmp_path)
    source = tmp_path / ".sikula" / "tasks" / "source-task.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\xff")
    data["source_task"] = {
        "path": source.relative_to(tmp_path).as_posix(),
        "sha256": "sha256:" + sha256(b"\xff").hexdigest(),
    }

    invalid_utf8 = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert "source_task.read_failed" in _codes(invalid_utf8)

    target = source.with_name("source-target.md")
    target.write_text("# Source task\n", encoding="utf-8")
    source.unlink()
    source.symlink_to(target.name)
    data["source_task"]["sha256"] = "sha256:" + sha256(target.read_bytes()).hexdigest()

    symlink = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert "source_task.symlink" in _codes(symlink)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("too_many", "constraints.too_many"),
        ("non_object", "constraints.item_invalid"),
        ("unknown_field", "constraints.unknown_field"),
        ("invalid_id", "constraints.id_invalid"),
        ("duplicate_id", "constraints.duplicate_id"),
        ("empty_units", "constraints.unit_ids_empty"),
        ("too_many_units", "constraints.unit_ids_too_many"),
        ("superseded_unit", "constraints.unit_superseded"),
    ],
)
def test_delivery_plan_check_rejects_constraint_boundary_cases(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    data = _base_plan(tmp_path)
    data["source_task"] = _write_source_task(tmp_path)
    constraint = {
        "id": "protocol-authority",
        "kind": "authoritative_read_only_dependency",
        "summary": "Protocol changes remain owned by the external protocol project.",
        "unit_ids": ["01-domain"],
        "disposition": "preserved",
    }
    data["constraints"] = [constraint]
    if case == "too_many":
        data["constraints"] = [
            {**constraint, "id": f"constraint-{index}"} for index in range(MAX_DELIVERY_CONSTRAINTS + 1)
        ]
    elif case == "non_object":
        data["constraints"] = ["constraint"]
    elif case == "unknown_field":
        constraint["private"] = "unsupported"
    elif case == "invalid_id":
        constraint["id"] = "x" * (MAX_DELIVERY_UNIT_ID_LENGTH + 1)
    elif case == "duplicate_id":
        data["constraints"] = [constraint, {**constraint, "id": "PROTOCOL-AUTHORITY"}]
    elif case == "empty_units":
        constraint["unit_ids"] = []
    elif case == "too_many_units":
        constraint["unit_ids"] = ["01-domain"] * (MAX_DELIVERY_CONSTRAINT_UNIT_IDS + 1)
    elif case == "superseded_unit":
        data["units"][0]["superseded_by"] = ["02-api"]

    result = check_delivery_plan_file(_write_plan(tmp_path, data), project_root=tmp_path)

    assert result.valid is False
    assert expected_code in _codes(result)


def test_delivery_plan_check_redacts_unsafe_metadata_from_public_projection(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    private_path = "/Users/example/private/task.md"
    data["title"] = f"Read {private_path}"
    data["units"][0]["title"] = f"Implement from {private_path}"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)
    payload = result.to_dict()
    rendered = render_delivery_plan_check(result)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.title == f"Read {private_path}"
    assert payload["plan"]["title"] == "<redacted>"
    assert payload["plan"]["units"][0]["title"] == "<redacted>"
    assert "<redacted>" in rendered
    assert private_path not in json.dumps(payload)
    assert private_path not in rendered


def test_delivery_plan_check_redacts_unsafe_metadata_from_validation_issues(tmp_path: Path) -> None:
    _git_init(tmp_path)
    private_stream_id = "/Users/example/private/stream"
    data = _base_plan(tmp_path)
    data["streams"] = [
        {"id": private_stream_id},
        {"id": private_stream_id},
    ]

    result = check_delivery_plan_file(_write_plan(tmp_path, data))
    payload = result.to_dict()
    rendered = render_delivery_plan_check(result)

    assert result.valid is False
    assert "streams.duplicate_id" in {issue.code for issue in result.errors}
    assert private_stream_id not in json.dumps(payload)
    assert private_stream_id not in rendered
    assert "<redacted>" in json.dumps(payload)
    assert "<redacted>" in rendered


def test_delivery_plan_check_projects_unsafe_identity_references_consistently(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    repository_id = "/Users/example/private/repository"
    first_unit_id = "/Users/example/private/domain"
    second_unit_id = r"C:\Users\example\private\api"
    data["repositories"] = [{"id": repository_id, "root": "."}]
    data["units"][0]["id"] = first_unit_id
    data["units"][0]["repo_id"] = repository_id
    data["units"][1]["id"] = second_unit_id
    data["units"][1]["repo_id"] = repository_id
    data["units"][1]["depends_on"] = [first_unit_id]
    source_task = tmp_path / ".sikula" / "tasks" / "source.md"
    source_task.parent.mkdir(parents=True, exist_ok=True)
    source_body = "# Source\n\nPreserve the private ownership boundary.\n"
    source_task.write_text(source_body, encoding="utf-8")
    data["source_task"] = {
        "path": source_task.relative_to(tmp_path).as_posix(),
        "sha256": f"sha256:{sha256(source_body.encode()).hexdigest()}",
    }
    data["constraints"] = [
        {
            "id": "ownership",
            "kind": "repository_ownership",
            "summary": "Preserve repository ownership.",
            "unit_ids": [first_unit_id],
            "disposition": "preserved",
        }
    ]

    result = check_delivery_plan_file(_write_plan(tmp_path, data))
    payload = result.to_dict()

    assert result.valid is True
    projected_repo = payload["plan"]["repositories"][0]["id"]
    projected_first = payload["plan"]["units"][0]["id"]
    projected_second = payload["plan"]["units"][1]["id"]
    assert projected_repo == payload["plan"]["units"][0]["repo_id"]
    assert projected_repo == payload["plan"]["units"][1]["repo_id"]
    assert projected_first == payload["plan"]["units"][1]["depends_on"][0]
    assert projected_first != projected_second
    assert result.plan is not None
    _, _, private_constraints = delivery_unit_constraint_context(result.plan, first_unit_id)
    assert private_constraints[0]["unit_ids"] == [first_unit_id]
    serialized = json.dumps(payload)
    assert repository_id not in serialized
    assert first_unit_id not in serialized
    assert second_unit_id not in serialized


def test_delivery_plan_check_preserves_monorepo_component_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["components"] = [
        {
            "id": "api",
            "label": "API package",
            "path": "packages/api",
            "stream": "backend",
        }
    ]
    data["units"][0]["component"] = "api"
    data["units"][0]["scope_paths"] = ["packages/api/src", "packages/api/package.json"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.components[0].to_dict() == {
        "id": "api",
        "path": "packages/api",
        "label": "API package",
        "stream": "backend",
    }
    payload = result.to_dict()
    assert payload["plan"]["components"] == [
        {
            "id": "api",
            "path": "packages/api",
            "label": "API package",
            "stream": "backend",
        }
    ]
    assert payload["plan"]["units"][0]["component"] == "api"
    assert payload["plan"]["units"][0]["scope_paths"] == ["packages/api/src", "packages/api/package.json"]


def test_delivery_plan_check_preserves_unit_sizing_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["estimated_size"] = "medium"
    data["units"][0]["risk_tags"] = ["structured_output_contract", "validation"]
    data["units"][0]["budget"] = {
        "max_planner_steps": 2,
        "max_elapsed_minutes": 45,
        "max_review_cycles": 2,
        "max_security_cycles": 1,
        "max_changed_files": 8,
        "max_changed_modules": 2,
        "max_generated_test_files": 3,
    }
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    unit = result.plan.units[0]
    assert unit.estimated_size == "medium"
    assert unit.risk_tags == ["structured_output_contract", "validation"]
    assert unit.budget is not None
    assert unit.budget.to_dict() == {
        "max_planner_steps": 2,
        "max_elapsed_minutes": 45,
        "max_review_cycles": 2,
        "max_security_cycles": 1,
        "max_changed_files": 8,
        "max_changed_modules": 2,
        "max_generated_test_files": 3,
    }
    payload = result.to_dict()
    assert payload["plan"]["units"][0]["estimated_size"] == "medium"
    assert payload["plan"]["units"][0]["risk_tags"] == ["structured_output_contract", "validation"]
    assert payload["plan"]["units"][0]["budget"]["max_planner_steps"] == 2


def test_delivery_plan_check_warns_when_unit_combines_high_risk_surfaces(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["estimated_size"] = "large"
    data["units"][0]["risk_tags"] = ["external_execution_boundary", "structured_output_contract", "cli_surface"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert [warning.code for warning in result.warnings] == ["units.split_recommended"]
    assert result.warnings[0].path == "units[0].risk_tags"
    rendered = render_delivery_plan_check(result)
    assert "units.split_recommended" in rendered


def test_delivery_plan_check_warns_for_broad_product_surface_combination(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["estimated_size"] = "large"
    data["units"][0]["risk_tags"] = ["ui_surface", "api_surface", "data_persistence"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert [warning.code for warning in result.warnings] == ["units.split_recommended"]
    assert result.warnings[0].path == "units[0].risk_tags"


def test_delivery_plan_check_does_not_warn_for_low_risk_narrow_unit(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["estimated_size"] = "small"
    data["units"][0]["risk_tags"] = ["validation"]
    data["units"][0]["budget"] = {"max_planner_steps": 2}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.warnings == []


def test_delivery_plan_check_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["schema_version"] = 2
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "schema_version.unsupported" in _codes(result)


def test_delivery_plan_check_rejects_plan_id_that_cannot_be_used_for_state_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["plan_id"] = "../bad"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan_id.invalid" in _codes(result)


@pytest.mark.parametrize("unit_id", ["unit\x01one", "x" * 1001])
def test_delivery_plan_check_rejects_unit_id_that_cannot_be_bounded_in_handoff(tmp_path: Path, unit_id: str) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["id"] = unit_id
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.id_invalid" in _codes(result)


def test_delivery_branch_name_allows_hyphen_prefixed_later_path_component(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["final_branch"] = "sikula/delivery/team/-hotfix"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert is_valid_delivery_branch_name("sikula/delivery/team/-hotfix") is True
    assert result.valid is True
    assert "final_branch.invalid" not in _codes(result)


@pytest.mark.parametrize(
    "final_branch",
    [
        "-foo",
        "sikula/delivery/foo..bar",
        "sikula/delivery/foo.",
        "sikula/delivery/foo.lock",
    ],
)
def test_delivery_plan_check_rejects_invalid_final_branch(tmp_path: Path, final_branch: str) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["final_branch"] = final_branch
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "final_branch.invalid" in _codes(result)


def test_delivery_plan_check_reports_duplicate_and_unknown_dependencies(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["id"] = "01-domain"
    data["units"][1]["depends_on"] = ["missing-unit"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.duplicate_id" in _codes(result)
    assert "dependencies.unknown_unit" in _codes(result)


def test_delivery_plan_check_preserves_unit_index_for_dependency_errors_after_skipped_units(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["id"] = ""
    data["units"][1]["depends_on"] = ["missing-unit"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    dependency_errors = [issue for issue in result.errors if issue.code == "dependencies.unknown_unit"]
    assert len(dependency_errors) == 1
    assert dependency_errors[0].path == "units[1].depends_on[0]"


def test_delivery_plan_check_reports_dependency_cycles(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["depends_on"] = ["02-api"]
    data["units"][1]["depends_on"] = ["01-domain"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "dependencies.cycle" in _codes(result)


def test_delivery_plan_check_rejects_missing_and_escaping_task_paths(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = ".sikula/delivery/demo/units/missing.md"
    data["units"][1]["task_path"] = "../outside.md"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.task_path_missing" in _codes(result)
    assert "units.task_path_outside_project" in _codes(result)


def test_delivery_plan_check_reports_invalid_task_path_string(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = "bad\0"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    invalid_path_errors = [issue for issue in result.errors if issue.code == "units.task_path_invalid"]
    assert len(invalid_path_errors) == 1
    assert invalid_path_errors[0].path == "units[0].task_path"
    assert "bad" not in invalid_path_errors[0].message


def test_delivery_plan_check_rejects_multi_repo_plan_before_multi_repo_support(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [
        {"id": "android", "root": "."},
        {"id": "ios", "root": "../ios"},
    ]
    data["units"][1]["repo_id"] = "ios"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.multiple_unsupported" in _codes(result)
    assert "units.repo_id_unknown" in _codes(result)


def test_delivery_plan_check_allows_explicit_single_repo(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [{"id": "main", "root": "."}]
    data["units"][0]["repo_id"] = "main"
    data["units"][1]["repo_id"] = "main"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.repositories[0].implicit is False


def test_delivery_plan_check_rejects_unknown_stream_when_streams_are_declared(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["stream"] = "web"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.stream_unknown" in _codes(result)


def test_delivery_plan_check_warns_when_unit_stream_is_missing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    del data["units"][1]["stream"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert [warning.code for warning in result.warnings] == ["units.stream_missing"]
    rendered = render_delivery_plan_check(result)
    assert "Warnings:" in rendered
    assert "units.stream_missing" in rendered


def test_delivery_plan_check_reports_invalid_component_shapes_and_references(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["components"] = [
        {"id": "api", "path": "packages/api", "stream": "backend"},
        "bad component",
        {"id": "api", "path": "../outside", "stream": "web"},
        {"label": "No id"},
    ]
    data["units"][0]["component"] = "missing"
    data["units"][1]["scope_paths"] = [str(tmp_path / "absolute"), "../outside", "bad\0"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "components.item_invalid" in _codes(result)
    assert "components.duplicate_id" in _codes(result)
    assert "components.path_outside_project" in _codes(result)
    assert "components.stream_unknown" in _codes(result)
    assert "components[3].id.missing" in _codes(result)
    assert "components[3].path.missing" in _codes(result)
    assert "units.component_unknown" in _codes(result)
    assert "units.scope_path_absolute" in _codes(result)
    assert "units.scope_path_outside_project" in _codes(result)
    assert "units.scope_path_invalid" in _codes(result)


def test_delivery_plan_check_rejects_parent_traversal_in_contained_scope_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["scope_paths"] = ["src/../tests_proj"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.scope_path_parent_traversal" in _codes(result)


def test_delivery_plan_check_resolves_relative_plan_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = check_delivery_plan_file(plan_path.relative_to(tmp_path))

    assert result.valid is True
    assert result.plan_path == str(plan_path.resolve())


def test_delivery_plan_check_reports_missing_plan_file(tmp_path: Path) -> None:
    _git_init(tmp_path)

    result = check_delivery_plan_file(tmp_path / ".sikula" / "delivery" / "missing.yaml")

    assert result.valid is False
    assert "plan.missing" in _codes(result)


def test_delivery_plan_check_reports_plan_path_that_is_not_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_dir = tmp_path / ".sikula" / "delivery"
    plan_dir.mkdir(parents=True)

    result = check_delivery_plan_file(plan_dir)

    assert result.valid is False
    assert "plan.not_file" in _codes(result)


def test_delivery_plan_check_reports_invalid_yaml(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("schema_version: [unterminated\nsecret: OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.parse_failed" in _codes(result)
    message = result.errors[0].message
    assert "ParserError" in message
    assert "line " in message
    assert "OPENAI_API_KEY" not in message
    assert "sk-test" not in message
    assert "unterminated" not in message


def test_delivery_plan_check_reports_non_utf8_plan_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(b"\xff\xfe\x00")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.read_failed" in _codes(result)


def test_delivery_plan_check_reports_non_mapping_yaml(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_missing_git_root(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "project.git_root_missing" in _codes(result)


def test_delivery_plan_check_reports_plan_outside_supplied_project_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path, project_root=other)

    assert result.valid is False
    assert "plan.path_outside_project" in _codes(result)


def test_delivery_plan_check_reports_invalid_repository_shapes(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = {"id": "main"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_empty_repositories(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = []
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.empty" in _codes(result)


def test_delivery_plan_check_reports_invalid_repository_entry(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = ["main"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.item_invalid" in _codes(result)


def test_delivery_plan_check_rejects_non_root_single_repository(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [{"id": "main", "root": "packages/app"}]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.root_unsupported" in _codes(result)


def test_delivery_plan_check_reports_invalid_stream_shapes(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["streams"] = {"id": "backend"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "streams.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_invalid_and_duplicate_stream_entries(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["streams"] = ["backend", 123, {"id": "backend"}, {"label": "No ID"}]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "streams.item_invalid" in _codes(result)
    assert "streams.duplicate_id" in _codes(result)
    assert "streams[3].id.missing" in _codes(result)


def test_delivery_plan_check_reports_invalid_units_container(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = {"id": "unit"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_empty_units(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = []
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.empty" in _codes(result)


def test_delivery_plan_check_reports_invalid_unit_entry_and_fields(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = [
        "bad unit",
        {
            "id": "",
            "title": 123,
            "task_path": 42,
            "depends_on": "01-domain",
            "stream": "",
            "platform": 123,
            "phase": 123,
            "kind": 123,
            "repo_id": 123,
            "component": 123,
            "scope_paths": "packages/api",
        },
    ]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.item_invalid" in _codes(result)
    assert "units[1].id.missing" in _codes(result)
    assert "units[1].title.invalid_type" in _codes(result)
    assert "units[1].task_path.missing" in _codes(result)
    assert "units[1].depends_on.invalid_type" in _codes(result)
    assert "units[1].stream.invalid_type" in _codes(result)
    assert "units[1].platform.invalid_type" in _codes(result)
    assert "units[1].phase.invalid_type" in _codes(result)
    assert "units[1].kind.invalid_type" in _codes(result)
    assert "units[1].repo_id.invalid_type" in _codes(result)
    assert "units[1].component.invalid_type" in _codes(result)
    assert "units[1].scope_paths.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_invalid_unit_sizing_metadata(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["estimated_size"] = "too-large"
    data["units"][0]["risk_tags"] = ["external_execution_boundary", "external_execution_boundary", "unknown", ""]
    data["units"][0]["budget"] = {
        "max_tokens": 100,
        "max_planner_steps": 0,
        "max_elapsed_minutes": True,
    }
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.estimated_size_invalid" in _codes(result)
    assert "units.risk_tag_duplicate" in _codes(result)
    assert "units.risk_tag_unknown" in _codes(result)
    assert "units.risk_tag_invalid" in _codes(result)
    assert "units.budget_unknown_field" in _codes(result)
    assert "units.budget_value_invalid" in _codes(result)


def test_delivery_plan_check_rejects_planner_step_budget_that_requires_split(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["budget"] = {"max_planner_steps": 3}

    result = check_delivery_plan_file(_write_plan(tmp_path, data))

    assert result.valid is False
    assert "units.planner_step_budget_invalid" in _codes(result)


def test_delivery_plan_check_reports_mixed_type_budget_keys_without_crashing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["budget"] = {1: 2, "max_tokens": 100}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    budget_errors = [issue for issue in result.errors if issue.code == "units.budget_unknown_field"]
    assert len(budget_errors) == 1
    assert budget_errors[0].path == "units[0].budget[0]"


def test_delivery_plan_check_reports_invalid_unit_sizing_container_types(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["risk_tags"] = "external_execution_boundary"
    data["units"][0]["budget"] = ["max_planner_steps"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.risk_tags_invalid_type" in _codes(result)
    assert "units.budget_invalid_type" in _codes(result)


def test_delivery_plan_check_reports_invalid_dependency_entries(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["depends_on"] = ["01-domain", ""]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units[1].depends_on.item_invalid" in _codes(result)


def test_delivery_plan_check_reports_absolute_task_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = str(tmp_path / ".sikula" / "delivery" / "demo" / "units" / "01-domain.md")
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.task_path_absolute" in _codes(result)


def test_delivery_plan_check_reports_self_dependency(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["depends_on"] = ["01-domain"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "dependencies.self_reference" in _codes(result)


def test_delivery_plan_result_json_includes_issue_paths(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, {"schema_version": "1"})

    result = check_delivery_plan_file(plan_path)
    payload = result.to_dict()

    assert payload["errors"][0]["path"]


def test_delivery_plan_issue_json_omits_empty_path() -> None:
    assert DeliveryPlanIssue("error", "code", "message").to_dict() == {
        "severity": "error",
        "code": "code",
        "message": "message",
    }


def test_delivery_plan_check_json_output_does_not_embed_unit_task_contents(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)
    secret_text = "# Unit\n\nDo not echo this raw unit body.\n"
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = _write_unit(tmp_path, "01-domain.md", secret_text)
    plan_path = _write_plan(tmp_path, data)

    cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=True), {})

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert "Do not echo this raw unit body" not in json.dumps(payload)


def test_delivery_check_cli_exits_nonzero_for_invalid_plan(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, {"schema_version": 1})

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=False), {})

    assert exc.value.code == 1
    assert "Status: invalid" in capsys.readouterr().out


def test_main_dispatches_delivery_check_without_loading_project_config(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("schema_version: 1\n", encoding="utf-8")

    with patch("sys.argv", ["sikula", "delivery", "check", str(plan_path)]):
        with patch("sikula._load_runtime_config", return_value={}) as load_config:
            with patch("sikula.cmd_delivery_check") as delivery_check:
                main()

    load_config.assert_not_called()
    delivery_check.assert_called_once()
