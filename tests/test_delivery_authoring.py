from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

import pytest

from core.delivery_authoring import (
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    derive_delivery_authoring_paths,
    parse_delivery_authoring_output,
)
from core.delivery_unit_metadata import DeliveryUnitBudget


def _unit_markdown(title: str = "Foundation") -> str:
    return f"""# {title}

## Goal

Prepare the delivery unit.

## Current behavior

The project does not yet have this delivery slice.

## Desired behavior

The delivery slice is described as product behavior with observable outcomes.

## Acceptance criteria

- The delivered behavior has a deterministic success path.
- The delivered behavior rejects invalid input safely.

## Security and privacy

- Do not log raw prompts, provider output, secrets, or source excerpts.

## Tests

- Cover the generated delivery unit behavior.

## Reviewer focus

- Check the behavior boundary and privacy-safe diagnostics.

## Out of scope

- Do not write delivery plan files in this unit.

## Validation

- `python3 -m pytest tests/test_delivery_authoring.py`
"""


def _text_heading_markdown() -> str:
    return """Goal:

Prepare the delivery unit.

Current behavior:

The project does not yet have this delivery slice.

Desired behavior:

The delivery slice is described as product behavior with observable outcomes.

Acceptance:

- The delivered behavior has a deterministic success path.

Security and privacy:

- Do not log raw prompts, provider output, secrets, or source excerpts.

Reviewer focus:

- Check the behavior boundary and privacy-safe diagnostics.

Out of scope:

- Do not write delivery plan files in this unit.

Validation:

- `python3 -m pytest tests/test_delivery_authoring.py`
"""


def _draft_data() -> dict[str, Any]:
    return {
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "planning_mode": "fixed_window",
        "warnings": ["Review the split before writing artifacts."],
        "units": [
            {
                "id": "foundation",
                "title": "Prepare foundation",
                "depends_on": [],
                "task_markdown": _unit_markdown("Foundation"),
                "stream": "backend",
                "component": "api",
                "phase": "foundation",
                "kind": "feature",
                "platform": "shared",
                "scope_paths": ["core", "tests"],
                "estimated_size": "small",
                "risk_tags": ["validation"],
                "budget": {"max_planner_steps": 3, "max_changed_files": 8},
            },
            {
                "id": "cli",
                "title": "Expose CLI behavior",
                "depends_on": ["foundation"],
                "task_markdown": _unit_markdown("CLI behavior"),
                "scope_paths": ["sikula_cli"],
                "estimated_size": "medium",
                "risk_tags": ["cli_surface"],
            },
        ],
    }


def _parse(output: object, tmp_path: Path) -> DeliveryAuthoringDraft:
    return parse_delivery_authoring_output(
        output,
        expected_plan_id="team-invites",
        project_root=tmp_path,
        output_dir=tmp_path / ".sikula" / "delivery" / "team-invites",
    )


def _output_with(mutator: Callable[[dict[str, Any]], object]) -> str:
    data = _draft_data()
    mutator(data)
    return json.dumps(data)


def _markdown_without(section_heading: str) -> str:
    lines = _unit_markdown().splitlines()
    section_start = lines.index(section_heading)
    section_end = len(lines)
    for idx in range(section_start + 1, len(lines)):
        if lines[idx].startswith("## "):
            section_end = idx
            break
    return "\n".join(lines[:section_start] + lines[section_end:])


def test_parse_delivery_authoring_output_accepts_valid_json_object(tmp_path: Path) -> None:
    draft = _parse(json.dumps(_draft_data()), tmp_path)

    assert draft.plan_id == "team-invites"
    assert draft.title == "Team invites delivery"
    assert draft.planning_mode == "fixed_window"
    assert draft.warnings == ["Review the split before writing artifacts."]
    assert draft.audit_records == []
    assert [unit.id for unit in draft.units] == ["foundation", "cli"]
    assert draft.units[0].depends_on == []
    assert draft.units[1].depends_on == ["foundation"]
    assert draft.units[0].stream == "backend"
    assert draft.units[0].component == "api"
    assert draft.units[0].phase == "foundation"
    assert draft.units[0].kind == "feature"
    assert draft.units[0].platform == "shared"
    assert draft.units[0].scope_paths == ["core", "tests"]
    assert draft.units[0].estimated_size == "small"
    assert draft.units[0].risk_tags == ["validation"]
    assert draft.units[0].budget == DeliveryUnitBudget(max_planner_steps=3, max_changed_files=8)
    assert draft.units[1].estimated_size == "medium"
    assert draft.units[1].risk_tags == ["cli_surface"]
    assert draft.units[1].budget is None
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_parse_delivery_authoring_output_accepts_single_fenced_json_block(tmp_path: Path) -> None:
    output = f"```json\n{json.dumps(_draft_data())}\n```"

    draft = _parse(output, tmp_path)

    assert draft.plan_id == "team-invites"
    assert [unit.id for unit in draft.units] == ["foundation", "cli"]


def test_parse_delivery_authoring_output_defaults_absent_optional_fields(tmp_path: Path) -> None:
    data = {
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "units": [
            {
                "id": "foundation",
                "title": "Prepare foundation",
                "depends_on": [],
                "task_markdown": _unit_markdown(),
            }
        ],
    }

    draft = _parse(json.dumps(data), tmp_path)

    assert draft.planning_mode is None
    assert draft.warnings == []
    assert draft.units[0].stream is None
    assert draft.units[0].component is None
    assert draft.units[0].phase is None
    assert draft.units[0].kind is None
    assert draft.units[0].platform is None
    assert draft.units[0].scope_paths == []
    assert draft.units[0].estimated_size is None
    assert draft.units[0].risk_tags == []
    assert draft.units[0].budget is None


def test_parse_delivery_authoring_output_accepts_text_heading_equivalents(tmp_path: Path) -> None:
    data = _draft_data()
    data["units"] = [
        {
            "id": "foundation",
            "title": "Prepare foundation",
            "depends_on": [],
            "task_markdown": _text_heading_markdown(),
        }
    ]

    draft = _parse(json.dumps(data), tmp_path)

    assert draft.units[0].task_markdown.startswith("Goal:")


@pytest.mark.parametrize(
    ("make_output", "expected_code"),
    [
        pytest.param(lambda root: "", "delivery_authoring.empty_output", id="empty_output"),
        pytest.param(lambda root: None, "delivery_authoring.empty_output", id="non_string_output"),
        pytest.param(lambda root: "{", "delivery_authoring.json_invalid", id="malformed_json"),
        pytest.param(
            lambda root: '{"plan_id":"team-invites","plan_id":"team-invites","title":"Team","units":[]}',
            "delivery_authoring.json_invalid",
            id="duplicate_json_key",
        ),
        pytest.param(lambda root: '{"plan_id": NaN}', "delivery_authoring.json_invalid", id="json_constant"),
        pytest.param(lambda root: "[]", "delivery_authoring.root_not_object", id="json_array"),
        pytest.param(lambda root: "{}{}", "delivery_authoring.json_invalid", id="multiple_json_objects"),
        pytest.param(lambda root: "Assistant draft:\n{}", "delivery_authoring.json_invalid", id="leading_prose"),
        pytest.param(
            lambda root: f"```json\n{json.dumps(_draft_data())}\n```\nextra",
            "delivery_authoring.output_invalid_envelope",
            id="fenced_extra_prose",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.pop("plan_id")),
            "delivery_authoring.string_required",
            id="missing_plan_id",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"title": 123})),
            "delivery_authoring.string_required",
            id="wrong_title_type",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"planning_mode": None})),
            "delivery_authoring.string_required",
            id="null_planning_mode",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"planning_mode": "unsupported"})),
            "delivery_authoring.planning_mode_invalid",
            id="invalid_planning_mode",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"warnings": None})),
            "delivery_authoring.string_list_required",
            id="null_warnings",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"warnings": ["ok", ""]})),
            "delivery_authoring.string_list_item_invalid",
            id="invalid_warning_item",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"raw_prompt": "SECRET_PROMPT"})),
            "delivery_authoring.unknown_field",
            id="unknown_top_level_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"paths": {"plan_file": "plan.yaml"}})),
            "delivery_authoring.unknown_field",
            id="top_level_paths_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"task_path": ".sikula/tasks/unit.md"})),
            "delivery_authoring.unknown_field",
            id="top_level_task_path_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"plan_path": ".sikula/delivery/plan.yaml"})),
            "delivery_authoring.unknown_field",
            id="top_level_plan_path_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"output_dir": ".sikula/delivery/team-invites"})),
            "delivery_authoring.unknown_field",
            id="top_level_output_dir_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"final_branch": "delivery/team-invites"})),
            "delivery_authoring.unknown_field",
            id="top_level_final_branch_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"repositories": [{"id": "main"}]})),
            "delivery_authoring.unknown_field",
            id="top_level_repositories_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"raw_output": "SECRET_PROVIDER_OUTPUT"})),
            "delivery_authoring.unknown_field",
            id="top_level_raw_output_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"source_excerpt": "PRIVATE SOURCE"})),
            "delivery_authoring.unknown_field",
            id="top_level_source_excerpt_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"plan_id": "-bad"})),
            "delivery_authoring.plan_id_invalid",
            id="invalid_plan_id",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"plan_id": "other"})),
            "delivery_authoring.plan_id_mismatch",
            id="plan_id_mismatch",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"units": {}})),
            "delivery_authoring.units_invalid_type",
            id="units_not_list",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.pop("units")),
            "delivery_authoring.units_invalid_type",
            id="missing_units",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"units": []})),
            "delivery_authoring.units_empty",
            id="units_empty",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data.update({"units": ["bad unit"]})),
            "delivery_authoring.unit_not_object",
            id="unit_not_object",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"repo_id": "main"})),
            "delivery_authoring.unknown_field",
            id="unknown_unit_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"task_path": "unit.md"})),
            "delivery_authoring.unit_path_field_forbidden",
            id="writer_path_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"path": "unit.md"})),
            "delivery_authoring.unit_path_field_forbidden",
            id="writer_path_field_path",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"unit_path": "unit.md"})),
            "delivery_authoring.unit_path_field_forbidden",
            id="writer_path_field_unit_path",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"output_path": "unit.md"})),
            "delivery_authoring.unit_path_field_forbidden",
            id="writer_path_field_output_path",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].pop("id")),
            "delivery_authoring.string_required",
            id="missing_unit_id",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"id": ""})),
            "delivery_authoring.string_required",
            id="empty_unit_id",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].pop("title")),
            "delivery_authoring.string_required",
            id="missing_unit_title",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].pop("depends_on")),
            "delivery_authoring.string_list_required",
            id="missing_depends_on",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].pop("task_markdown")),
            "delivery_authoring.string_required",
            id="missing_task_markdown",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"task_markdown": " "})),
            "delivery_authoring.string_required",
            id="empty_task_markdown",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"id": "bad/path"})),
            "delivery_authoring.unit_id_invalid",
            id="unit_id_with_separator",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"id": "/absolute"})),
            "delivery_authoring.unit_id_invalid",
            id="unit_id_absolute_path",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"id": "."})),
            "delivery_authoring.unit_id_invalid",
            id="unit_id_dot",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"id": "C:\\unit"})),
            "delivery_authoring.unit_id_invalid",
            id="unit_id_windows_path",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][1].update({"id": "foundation"})),
            "delivery_authoring.unit_id_duplicate",
            id="duplicate_unit_id",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"stream": None})),
            "delivery_authoring.string_required",
            id="null_optional_unit_metadata",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"component": " "})),
            "delivery_authoring.string_required",
            id="empty_optional_unit_metadata",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"estimated_size": "too-large"})),
            "delivery_authoring.estimated_size_invalid",
            id="invalid_estimated_size",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"risk_tags": "cli_surface"})),
            "delivery_authoring.risk_tags_invalid_type",
            id="risk_tags_not_list",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"risk_tags": ["cli_surface", ""]})),
            "delivery_authoring.risk_tag_invalid",
            id="risk_tag_empty",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"risk_tags": ["unknown"]})),
            "delivery_authoring.risk_tag_unknown",
            id="risk_tag_unknown",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update({"risk_tags": ["cli_surface", "cli_surface"]})
            ),
            "delivery_authoring.risk_tag_duplicate",
            id="risk_tag_duplicate",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"budget": ["max_planner_steps"]})),
            "delivery_authoring.budget_invalid_type",
            id="budget_not_object",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"budget": {"max_tokens": 100}})),
            "delivery_authoring.budget_unknown_field",
            id="budget_unknown_field",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"budget": {"max_planner_steps": 0}})),
            "delivery_authoring.budget_value_invalid",
            id="budget_zero_value",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"budget": {"max_planner_steps": True}})),
            "delivery_authoring.budget_value_invalid",
            id="budget_bool_value",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][1].update({"depends_on": "foundation"})),
            "delivery_authoring.string_list_required",
            id="depends_on_not_list",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][1].update({"depends_on": ["foundation", ""]})),
            "delivery_authoring.string_list_item_invalid",
            id="depends_on_empty_item",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][1].update({"depends_on": ["foundation", "foundation"]})
            ),
            "delivery_authoring.dependency_duplicate",
            id="duplicate_dependency",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"depends_on": ["foundation"]})),
            "delivery_authoring.dependency_self",
            id="self_dependency",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][1].update({"depends_on": ["missing"]})),
            "delivery_authoring.dependency_unknown",
            id="unknown_dependency",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"depends_on": ["cli"]})),
            "delivery_authoring.dependency_cycle",
            id="dependency_cycle",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": "core"})),
            "delivery_authoring.scope_paths_invalid_type",
            id="scope_paths_not_list",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": [123]})),
            "delivery_authoring.scope_path_invalid",
            id="scope_path_not_string",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": [" "]})),
            "delivery_authoring.scope_path_invalid",
            id="scope_path_empty",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": [str(root / "core")]})),
            "delivery_authoring.scope_path_absolute",
            id="scope_path_absolute",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": ["C:\\project\\core"]})),
            "delivery_authoring.scope_path_absolute",
            id="scope_path_windows_absolute",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": ["../outside"]})),
            "delivery_authoring.scope_path_outside_project",
            id="scope_path_escape",
        ),
        pytest.param(
            lambda root: _output_with(lambda data: data["units"][0].update({"scope_paths": ["bad\0path"]})),
            "delivery_authoring.scope_path_invalid",
            id="scope_path_invalid_filesystem_value",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update({"task_markdown": _markdown_without("## Acceptance criteria")})
            ),
            "delivery_authoring.unit_markdown_missing_section",
            id="markdown_missing_acceptance",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update(
                    {
                        "task_markdown": _unit_markdown().replace(
                            "- `python3 -m pytest tests/test_delivery_authoring.py`",
                            "- Run the configured tests.",
                        )
                    }
                )
            ),
            "delivery_authoring.unit_markdown_missing_verification_commands",
            id="markdown_missing_verification_command",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update({"task_markdown": _unit_markdown() + "\n## Asset manifest\n\n- x"})
            ),
            "delivery_authoring.unit_markdown_asset_manifest",
            id="markdown_asset_manifest",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update({"task_markdown": _unit_markdown() + "\nsikula:generated-answer"})
            ),
            "delivery_authoring.unit_markdown_generated_marker",
            id="markdown_generated_marker",
        ),
        pytest.param(
            lambda root: _output_with(
                lambda data: data["units"][0].update(
                    {
                        "task_markdown": """# Unit

```markdown
## Goal

This fenced heading does not count.
```

## Current behavior

Current.

## Desired behavior

Desired.

## Acceptance criteria

- Accepted.

## Security/privacy notes

- Private.

## Reviewer focus

- Focus.

## Out of scope

- Extra work.

## Verification

- `python3 -m pytest tests/test_delivery_authoring.py`
"""
                    }
                )
            ),
            "delivery_authoring.unit_markdown_missing_section",
            id="markdown_fenced_heading_ignored",
        ),
    ],
)
def test_parse_delivery_authoring_output_rejects_invalid_contracts(
    tmp_path: Path,
    make_output: Callable[[Path], object],
    expected_code: str,
) -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(make_output(tmp_path), tmp_path)

    assert exc_info.value.code == expected_code
    assert str(tmp_path) not in exc_info.value.message
    assert "SECRET_PROMPT" not in exc_info.value.message


def test_parse_delivery_authoring_output_rejects_expected_plan_id_mismatch(tmp_path: Path) -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_authoring_output(
            json.dumps(_draft_data()),
            expected_plan_id="other-plan",
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
        )

    assert exc_info.value.code == "delivery_authoring.expected_plan_id_mismatch"


def test_parse_delivery_authoring_output_rejects_invalid_expected_plan_id(tmp_path: Path) -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_authoring_output(
            json.dumps(_draft_data()),
            expected_plan_id="bad plan",
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
        )

    assert exc_info.value.code == "delivery_authoring.expected_plan_id_invalid"


def test_parse_delivery_authoring_output_rejects_output_dir_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "team-invites"

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_authoring_output(
            json.dumps(_draft_data()),
            expected_plan_id="team-invites",
            project_root=tmp_path,
            output_dir=outside,
        )

    assert exc_info.value.code == "delivery_authoring.output_dir_outside_project"
    assert str(outside) not in exc_info.value.message


def test_derive_delivery_authoring_paths_returns_project_relative_paths(tmp_path: Path) -> None:
    draft = _parse(json.dumps(_draft_data()), tmp_path)

    derived = derive_delivery_authoring_paths(
        draft,
        output_dir=tmp_path / ".sikula" / "delivery" / "team-invites",
        project_root=tmp_path,
    )

    assert derived.plan_file == ".sikula/delivery/team-invites/plan.yaml"
    assert derived.units_dir == ".sikula/delivery/team-invites/units"
    assert derived.unit_task_paths == {
        "foundation": ".sikula/delivery/team-invites/units/foundation.md",
        "cli": ".sikula/delivery/team-invites/units/cli.md",
    }
    assert str(tmp_path) not in repr(derived)
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


@pytest.mark.parametrize(
    ("units", "expected_code"),
    [
        pytest.param(
            [
                DeliveryAuthoringUnitDraft(
                    id="unit-a",
                    title="Unit A",
                    depends_on=[],
                    task_markdown="not checked by path derivation",
                ),
                DeliveryAuthoringUnitDraft(
                    id="unit-a",
                    title="Unit A duplicate",
                    depends_on=[],
                    task_markdown="not checked by path derivation",
                ),
            ],
            "delivery_authoring.unit_id_duplicate",
            id="duplicate_unit_id",
        ),
        pytest.param(
            [
                DeliveryAuthoringUnitDraft(
                    id="../unit-a",
                    title="Unit A",
                    depends_on=[],
                    task_markdown="not checked by path derivation",
                )
            ],
            "delivery_authoring.unit_id_invalid",
            id="invalid_unit_id",
        ),
    ],
)
def test_derive_delivery_authoring_paths_revalidates_unit_ids(
    tmp_path: Path,
    units: list[DeliveryAuthoringUnitDraft],
    expected_code: str,
) -> None:
    draft = DeliveryAuthoringDraft(plan_id="team-invites", title="Team invites", units=units)

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        derive_delivery_authoring_paths(
            draft,
            output_dir=".sikula/delivery/team-invites",
            project_root=tmp_path,
        )

    assert exc_info.value.code == expected_code


def test_derive_delivery_authoring_paths_rejects_output_dir_outside_project(tmp_path: Path) -> None:
    draft = DeliveryAuthoringDraft(
        plan_id="team-invites",
        title="Team invites",
        units=[
            DeliveryAuthoringUnitDraft(
                id="unit-a",
                title="Unit A",
                depends_on=[],
                task_markdown="not checked by path derivation",
            )
        ],
    )
    outside = tmp_path.parent / "team-invites"

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        derive_delivery_authoring_paths(draft, output_dir=outside, project_root=tmp_path)

    assert exc_info.value.code == "delivery_authoring.output_dir_outside_project"
