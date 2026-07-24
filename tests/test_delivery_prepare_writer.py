from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.delivery_authoring import DeliveryAuthoringDraft, DeliveryAuthoringUnitDraft
from core.delivery_prepare_writer import write_delivery_prepare_artifacts
from core.delivery_unit_metadata import DeliveryUnitBudget


def _project_config(root: Path) -> dict:
    return {
        "project": {"build_tool": "python", "root_path": str(root)},
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": {"checks": [{"name": "ruff", "command": "ruff check ."}]},
    }


def _ready_task_markdown(title: str = "Delivery prepare writer") -> str:
    return f"""# {title}

## Goal

Make the delivery prepare writer create deterministic reviewable artifacts from a parsed delivery draft.

## Current behavior

- Operators must manually create plan and unit task source artifacts.
- Existing delivery validation can only inspect artifacts after they exist.
- The authoring assistant output is parsed before any deterministic source artifact writer runs.

## Desired behavior

- The writer creates the parent plan and unit task source artifacts through project-owned APIs.
- The writer keeps generated paths stable, project-relative, and derived from validated unit identifiers.
- The writer refuses invalid output locations, rejects existing artifacts by default, and rolls back failed writes.

## Acceptance criteria

- A successful write produces a valid delivery plan and one unit task file per parsed unit.
- Existing artifacts are rejected unless explicit force replacement is requested.
- Invalid, missing, malformed, or escaping paths fail without leaving finalized artifacts.
- Unit contracts with blocking readiness gaps are reported as blocked before any source file is written.
- Result metadata lists only plan status, unit readiness, artifact paths, and deterministic error codes.

## Security and privacy

- Raw prompts, provider output, task bodies, source excerpts, and local absolute paths stay out of result metadata.
- Symlink artifacts and symlink output directories are rejected before replacement.
- Error messages are deterministic and do not include machine-specific details.

## Out of scope

- Do not run generated delivery units.
- Do not create delivery progress state.
- Do not assemble or finalize a delivery branch.

## Tests

- Cover successful artifact writes, overwrite protection, rollback, validation failure, and blocked readiness.
- Cover negative path handling for absolute paths, traversal, symlinks, and path collisions.
- Assert result projections do not contain private generated unit body text.

## Verification

- `pytest`
- `ruff check .`

## Reviewer focus

- Review filesystem safety, rollback behavior, validation ordering, and privacy-safe result projection.
- Confirm the writer derives paths from trusted unit identifiers instead of draft-supplied file paths.
- Confirm existing delivery progress, run-next, and finalize behavior is not exercised by this writer.
"""


def _unit(
    unit_id: str,
    *,
    title: str | None = None,
    depends_on: list[str] | None = None,
    task_markdown: str | None = None,
    stream: str | None = None,
    component: str | None = None,
    phase: str | None = None,
    kind: str | None = None,
    platform: str | None = None,
    scope_paths: list[str] | None = None,
    estimated_size: str | None = None,
    risk_tags: list[str] | None = None,
    budget: DeliveryUnitBudget | None = None,
) -> DeliveryAuthoringUnitDraft:
    unit_title = title or f"{unit_id} title"
    return DeliveryAuthoringUnitDraft(
        id=unit_id,
        title=unit_title,
        depends_on=depends_on or [],
        task_markdown=task_markdown if task_markdown is not None else _ready_task_markdown(unit_title),
        stream=stream,
        component=component,
        phase=phase,
        kind=kind,
        platform=platform,
        scope_paths=scope_paths or [],
        estimated_size=estimated_size,
        risk_tags=risk_tags or [],
        budget=budget,
    )


def _draft(
    *,
    plan_id: str = "team-invites",
    planning_mode: str | None = "fixed_window",
    units: list[DeliveryAuthoringUnitDraft] | None = None,
) -> DeliveryAuthoringDraft:
    return DeliveryAuthoringDraft(
        plan_id=plan_id,
        title="Team invites delivery",
        units=units
        or [
            _unit(
                "foundation",
                title="Prepare foundation",
                stream="backend",
                component="api",
                phase="foundation",
                kind="feature",
                platform="shared",
                scope_paths=["core", "tests"],
                estimated_size="small",
                risk_tags=["validation"],
                budget=DeliveryUnitBudget(max_planner_steps=2, max_changed_files=8),
            ),
            _unit(
                "cli",
                title="Expose CLI behavior",
                depends_on=["foundation"],
                stream="frontend",
                phase="cli",
                kind="feature",
                platform="shared",
                scope_paths=["sikula_cli"],
                estimated_size="medium",
                risk_tags=["cli_surface"],
            ),
        ],
        planning_mode=planning_mode,
    )


def test_writer_rejects_unsafe_public_metadata_before_writing(tmp_path: Path) -> None:
    draft = _draft(units=[_unit("foundation", title="Read /Users/example/private/task.md")])

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.prepared is False
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == "delivery_prepare.metadata_invalid"
    assert result.errors[0].path == "units[0].title"
    assert str(tmp_path) not in str(result.to_dict())
    assert not (tmp_path / ".sikula" / "delivery").exists()


def test_write_delivery_prepare_artifacts_writes_valid_plan_and_units(tmp_path: Path) -> None:
    result = write_delivery_prepare_artifacts(
        _draft(),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    plan_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    foundation_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "foundation.md"
    cli_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "cli.md"
    plan_data = yaml.safe_load(plan_file.read_text(encoding="utf-8"))

    assert result.status == "ready"
    assert result.prepared is True
    assert result.failure_reason is None
    assert result.unit_task_paths == {
        "foundation": ".sikula/delivery/team-invites/units/foundation.md",
        "cli": ".sikula/delivery/team-invites/units/cli.md",
    }
    assert [artifact.to_dict() for artifact in result.written_artifacts] == [
        {"kind": "plan", "path": ".sikula/delivery/team-invites/plan.yaml"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/foundation.md"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/cli.md"},
    ]
    assert result.plan_validation.to_dict() == {
        "status": "valid",
        "valid": True,
        "errors": [],
        "warnings": [],
    }
    assert result.unit_readiness.status == "ready"
    assert [unit.unit_id for unit in result.unit_readiness.units] == ["foundation", "cli"]
    assert all(unit.blocking_gap_count == 0 for unit in result.unit_readiness.units)
    assert all(unit.ready_for_autonomous_delivery for unit in result.unit_readiness.units)
    assert plan_data == {
        "schema_version": 1,
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/team-invites",
        "repositories": [{"id": "main", "root": "."}],
        "streams": ["backend", "frontend"],
        "units": [
            {
                "id": "foundation",
                "title": "Prepare foundation",
                "task_path": ".sikula/delivery/team-invites/units/foundation.md",
                "depends_on": [],
                "stream": "backend",
                "platform": "shared",
                "phase": "foundation",
                "kind": "feature",
                "scope_paths": ["core", "tests"],
                "estimated_size": "small",
                "risk_tags": ["validation"],
                "budget": {"max_planner_steps": 2, "max_changed_files": 8},
            },
            {
                "id": "cli",
                "title": "Expose CLI behavior",
                "task_path": ".sikula/delivery/team-invites/units/cli.md",
                "depends_on": ["foundation"],
                "stream": "frontend",
                "platform": "shared",
                "phase": "cli",
                "kind": "feature",
                "scope_paths": ["sikula_cli"],
                "estimated_size": "medium",
                "risk_tags": ["cli_surface"],
            },
        ],
    }
    assert "component" not in plan_data["units"][0]
    assert foundation_file.read_text(encoding="utf-8").endswith("\n")
    assert cli_file.read_text(encoding="utf-8").endswith("\n")
    assert not (tmp_path / ".sikula" / "state" / "delivery" / "team-invites").exists()
    assert str(tmp_path) not in repr(result.to_dict())
    assert "Raw prompts" not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_canonicalizes_generated_unit_headings(tmp_path: Path) -> None:
    task_markdown = _ready_task_markdown("Prepare foundation").replace(
        "## Security and privacy",
        "## Security/privacy notes",
    )

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation", task_markdown=task_markdown)]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    foundation_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "foundation.md"
    written_markdown = foundation_file.read_text(encoding="utf-8")

    assert result.status == "ready"
    assert result.prepared is True
    assert result.unit_readiness.status == "ready"
    assert result.unit_readiness.units[0].blocking_gap_count == 0
    assert "## Security and privacy" in written_markdown
    assert "## Security/privacy notes" not in written_markdown
    assert not (tmp_path / ".sikula" / "state" / "delivery" / "team-invites").exists()
    assert str(tmp_path) not in repr(result.to_dict())
    assert "Raw prompts" not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_omits_absent_optional_fields(tmp_path: Path) -> None:
    draft = _draft(planning_mode=None, units=[_unit("single", title="Single unit")])

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=None,
    )

    plan_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    plan_data = yaml.safe_load(plan_file.read_text(encoding="utf-8"))
    unit = plan_data["units"][0]

    assert result.status == "ready"
    assert result.plan_validation.valid is True
    assert "planning_mode" not in plan_data
    assert "streams" not in plan_data
    assert "stream" not in unit
    assert "platform" not in unit
    assert "phase" not in unit
    assert "kind" not in unit
    assert "scope_paths" not in unit
    assert "estimated_size" not in unit
    assert "risk_tags" not in unit
    assert "budget" not in unit


def test_write_delivery_prepare_artifacts_deduplicates_streams_in_unit_order(tmp_path: Path) -> None:
    draft = _draft(
        units=[
            _unit("api-a", title="API A", stream="backend"),
            _unit("api-b", title="API B", stream="backend"),
            _unit("cli", title="CLI", stream="frontend"),
        ]
    )

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    plan_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    plan_data = yaml.safe_load(plan_file.read_text(encoding="utf-8"))

    assert result.status == "ready"
    assert plan_data["streams"] == ["backend", "frontend"]


def test_write_delivery_prepare_artifacts_blocks_unit_readiness_gaps_before_writing(tmp_path: Path) -> None:
    weak_markdown = "# Weak unit\n\nToo vague."
    draft = _draft(units=[_unit("weak", title="Weak unit", task_markdown=weak_markdown)])

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.prepared is False
    assert result.failure_reason == "unit_readiness_blocked"
    assert result.errors[0].to_dict() == {
        "severity": "error",
        "code": "delivery_prepare.unit_readiness_blocked",
        "message": "Generated unit task contracts have blocking readiness gaps.",
        "path": None,
    }
    assert result.plan_validation.status == "not_run"
    assert result.unit_readiness.status == "blocked"
    assert result.unit_readiness.units[0].unit_id == "weak"
    assert result.unit_readiness.units[0].blocking_gap_count > 0
    assert "gap.acceptance.criteria" in result.unit_readiness.units[0].blocking_gap_ids
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_write_delivery_prepare_artifacts_refuses_existing_artifacts_without_force_and_replaces_with_force(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    plan_file = output_dir / "plan.yaml"
    unit_file = output_dir / "units" / "foundation.md"
    output_dir.joinpath("units").mkdir(parents=True)
    plan_file.write_text("old plan\n", encoding="utf-8")
    unit_file.write_text("old unit\n", encoding="utf-8")

    blocked = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert blocked.status == "blocked"
    assert blocked.failure_reason == "write_failed"
    assert blocked.errors[0].code == "delivery_prepare.existing_artifacts"
    assert blocked.errors[0].path == ".sikula/delivery/team-invites/plan.yaml"
    assert plan_file.read_text(encoding="utf-8") == "old plan\n"
    assert unit_file.read_text(encoding="utf-8") == "old unit\n"

    ready = write_delivery_prepare_artifacts(
        _draft(
            units=[
                _unit(
                    "foundation",
                    title="Replacement foundation",
                    task_markdown=_ready_task_markdown("Replacement foundation"),
                )
            ]
        ),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
        force=True,
    )

    assert ready.status == "ready"
    assert "Replacement foundation" in unit_file.read_text(encoding="utf-8")
    assert "old unit" not in unit_file.read_text(encoding="utf-8")
    replacement_plan = yaml.safe_load(plan_file.read_text(encoding="utf-8"))
    assert replacement_plan["units"][0]["title"] == "Replacement foundation"


def test_write_delivery_prepare_artifacts_rolls_back_when_plan_validation_fails(tmp_path: Path) -> None:
    draft = _draft(units=[_unit("foundation", title="Prepare foundation", scope_paths=["../outside"])])

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.prepared is False
    assert result.failure_reason == "plan_validation_failed"
    assert result.errors[0].code == "delivery_prepare.plan_validation_failed"
    assert result.plan_validation.status == "invalid"
    assert result.plan_validation.valid is False
    assert result.plan_validation.errors[0]["code"] == "units.scope_path_outside_project"
    assert result.written_artifacts == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()
    assert str(tmp_path) not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_restores_existing_artifacts_when_forced_validation_fails(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    units_dir = output_dir / "units"
    plan_file = output_dir / "plan.yaml"
    foundation_file = units_dir / "foundation.md"
    cli_file = units_dir / "cli.md"
    units_dir.mkdir(parents=True)
    plan_file.write_text("old plan\n", encoding="utf-8")
    foundation_file.write_text("old foundation\n", encoding="utf-8")

    result = write_delivery_prepare_artifacts(
        _draft(
            units=[
                _unit("foundation", title="Prepare foundation", scope_paths=["../outside"]),
                _unit("cli", title="Expose CLI behavior"),
            ]
        ),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
        force=True,
    )

    assert result.status == "blocked"
    assert result.failure_reason == "plan_validation_failed"
    assert result.written_artifacts == []
    assert plan_file.read_text(encoding="utf-8") == "old plan\n"
    assert foundation_file.read_text(encoding="utf-8") == "old foundation\n"
    assert not cli_file.exists()
    assert result.plan_validation.errors[0]["code"] == "units.scope_path_outside_project"
    assert str(tmp_path) not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_blocks_unit_readiness_check_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_check_contract(*args, **kwargs):
        raise RuntimeError("SECRET readiness failure at private path")

    monkeypatch.setattr("core.delivery_prepare_writer.check_contract", fail_check_contract)

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.prepared is False
    assert result.failure_reason == "write_failed"
    assert result.errors[0].to_dict() == {
        "severity": "error",
        "code": "delivery_prepare.unit_readiness_check_failed",
        "message": "Delivery prepare failed while checking unit readiness.",
        "path": None,
    }
    assert result.plan_validation.status == "not_run"
    assert result.unit_readiness.status == "not_run"
    assert result.written_artifacts == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()
    assert "SECRET" not in repr(result.to_dict())
    assert str(tmp_path) not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_rolls_back_when_plan_validation_check_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_check_delivery_plan_file(*args, **kwargs):
        raise RuntimeError("SECRET validation failure at private path")

    monkeypatch.setattr("core.delivery_prepare_writer.check_delivery_plan_file", fail_check_delivery_plan_file)

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.prepared is False
    assert result.failure_reason == "write_failed"
    assert result.errors[0].to_dict() == {
        "severity": "error",
        "code": "delivery_prepare.plan_validation_check_failed",
        "message": "Delivery prepare failed while validating written artifacts.",
        "path": None,
    }
    assert result.plan_validation.status == "not_run"
    assert result.unit_readiness.status == "ready"
    assert result.written_artifacts == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()
    assert "SECRET" not in repr(result.to_dict())
    assert str(tmp_path) not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_rolls_back_when_file_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text

    def fail_unit_write(self: Path, *args, **kwargs):
        if self.name == "foundation.md":
            raise OSError("disk full at private absolute path")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_unit_write)

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == "delivery_prepare.write_failed"
    assert result.written_artifacts == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()
    assert "disk full" not in repr(result.to_dict())
    assert str(tmp_path) not in repr(result.to_dict())


@pytest.mark.parametrize(
    ("case_name", "expected_code", "expected_path"),
    [
        pytest.param(
            "output_file",
            "delivery_prepare.output_not_directory",
            ".sikula/delivery/team-invites",
            id="output_path_file",
        ),
        pytest.param(
            "units_file",
            "delivery_prepare.units_dir_not_directory",
            ".sikula/delivery/team-invites/units",
            id="units_path_file",
        ),
        pytest.param(
            "unit_target_directory",
            "delivery_prepare.target_not_file",
            ".sikula/delivery/team-invites/units/foundation.md",
            id="unit_target_directory",
        ),
    ],
)
def test_write_delivery_prepare_artifacts_rejects_non_directory_targets(
    tmp_path: Path,
    case_name: str,
    expected_code: str,
    expected_path: str,
) -> None:
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    if case_name == "output_file":
        output_dir.parent.mkdir(parents=True)
        blocked_path = output_dir
    elif case_name == "units_file":
        output_dir.mkdir(parents=True)
        blocked_path = output_dir / "units"
    else:
        blocked_path = output_dir / "units" / "foundation.md"
        blocked_path.mkdir(parents=True)
    if case_name != "unit_target_directory":
        blocked_path.write_text("not a directory\n", encoding="utf-8")

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
        force=True,
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == expected_code
    assert result.errors[0].path == expected_path
    assert result.written_artifacts == []
    if case_name == "unit_target_directory":
        assert blocked_path.is_dir()
    else:
        assert blocked_path.read_text(encoding="utf-8") == "not a directory\n"


@pytest.mark.parametrize(
    ("output_dir", "expected_code"),
    [
        pytest.param("/tmp/outside", "delivery_prepare.output_absolute", id="posix_absolute"),
        pytest.param("C:\\outside", "delivery_prepare.output_absolute", id="windows_absolute"),
        pytest.param("../outside", "delivery_prepare.output_traversal", id="posix_parent_traversal"),
        pytest.param("..\\outside", "delivery_prepare.output_traversal", id="windows_parent_traversal"),
        pytest.param(
            ".sikula/state/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="state_runtime_artifact",
        ),
        pytest.param(
            ".sikula/worktrees/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="worktree_runtime_artifact",
        ),
        pytest.param(
            ".sikula/contract-reports/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="contract_report_runtime_artifact",
        ),
        pytest.param(".git/team-invites", "delivery_prepare.output_runtime_artifact", id="git_metadata_artifact"),
    ],
)
def test_write_delivery_prepare_artifacts_rejects_unsafe_output_paths(
    tmp_path: Path,
    output_dir: str,
    expected_code: str,
) -> None:
    result = write_delivery_prepare_artifacts(
        _draft(),
        output_dir=output_dir,
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == expected_code
    assert result.paths.plan_file == ""
    assert result.paths.units_dir == ""
    assert result.paths.unit_task_paths == {}
    assert not (tmp_path / ".sikula").exists()


def test_write_delivery_prepare_artifacts_rejects_invalid_generated_final_branch(tmp_path: Path) -> None:
    result = write_delivery_prepare_artifacts(
        _draft(plan_id="foo..bar"),
        output_dir=".sikula/delivery/foo..bar",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == "delivery_prepare.final_branch_invalid"
    assert result.written_artifacts == []
    assert not (tmp_path / ".sikula" / "delivery" / "foo..bar").exists()


@pytest.mark.parametrize(
    ("artifact_name", "expected_code"),
    [
        pytest.param("output_dir", "delivery_prepare.output_symlink", id="output_dir_symlink"),
        pytest.param("plan_file", "delivery_prepare.symlink_artifact", id="plan_file_symlink"),
        pytest.param("units_dir", "delivery_prepare.units_dir_symlink", id="units_dir_symlink"),
        pytest.param("unit_file", "delivery_prepare.symlink_artifact", id="unit_file_symlink"),
    ],
)
def test_write_delivery_prepare_artifacts_rejects_symlink_paths(
    tmp_path: Path,
    artifact_name: str,
    expected_code: str,
) -> None:
    delivery_root = tmp_path / ".sikula" / "delivery"
    output_dir = delivery_root / "team-invites"
    target = tmp_path / "outside-target"
    target.parent.mkdir(parents=True, exist_ok=True)
    delivery_root.mkdir(parents=True)
    if artifact_name == "output_dir":
        target.mkdir()
        output_dir.symlink_to(target, target_is_directory=True)
    else:
        output_dir.mkdir()
        if artifact_name == "plan_file":
            target.write_text("linked plan\n", encoding="utf-8")
            (output_dir / "plan.yaml").symlink_to(target)
        elif artifact_name == "units_dir":
            target.mkdir()
            (output_dir / "units").symlink_to(target, target_is_directory=True)
        else:
            units_dir = output_dir / "units"
            units_dir.mkdir()
            target.write_text("linked unit\n", encoding="utf-8")
            (units_dir / "foundation.md").symlink_to(target)

    result = write_delivery_prepare_artifacts(
        _draft(units=[_unit("foundation", title="Prepare foundation")]),
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
        force=True,
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == expected_code
    assert str(target) not in repr(result.to_dict())


def test_write_delivery_prepare_artifacts_rejects_casefold_path_collisions(tmp_path: Path) -> None:
    draft = _draft(units=[_unit("Unit", title="Upper unit"), _unit("unit", title="Lower unit")])

    result = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/team-invites",
        project_root=tmp_path,
        project_config=_project_config(tmp_path),
    )

    assert result.status == "blocked"
    assert result.failure_reason == "write_failed"
    assert result.errors[0].code == "delivery_prepare.path_collision"
    assert result.errors[0].path == ".sikula/delivery/team-invites/units/unit.md"
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()
