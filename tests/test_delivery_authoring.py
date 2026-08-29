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
    apply_delivery_unit_context_gaps,
    derive_delivery_authoring_paths,
    parse_delivery_assessment_output,
    parse_delivery_amendment_authoring_output,
    parse_delivery_authoring_output,
    parse_delivery_constraint_repair_output,
    parse_delivery_constraint_verification_output,
)
from core.delivery_plan import (
    MAX_DELIVERY_CONSTRAINT_UNIT_IDS,
    MAX_DELIVERY_CONSTRAINTS,
    MAX_DELIVERY_UNIT_ID_LENGTH,
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
        "constraints": [],
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
                "asset_paths": [".sikula/task-assets/invite-reference.png"],
                "estimated_size": "small",
                "risk_tags": ["validation"],
                "budget": {"max_planner_steps": 2, "max_changed_files": 8},
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


def _amendment_data() -> dict[str, Any]:
    return {
        "plan_id": "team-invites",
        "target_unit_id": "oversized",
        "amend_reason": None,
        "budget_exceeded": None,
        "warnings": [],
        "replacement_units": [
            {
                "id": "split-a",
                "title": "Split A",
                "depends_on": [],
                "asset_paths": [".sikula/task-assets/invite-reference.png"],
                "task_markdown": _unit_markdown("Split A"),
            },
            {
                "id": "split-b",
                "title": "Split B",
                "depends_on": ["split-a"],
                "task_markdown": _unit_markdown("Split B"),
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
    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert draft.units[0].estimated_size == "small"
    assert draft.units[0].risk_tags == ["validation"]
    assert draft.units[0].budget == DeliveryUnitBudget(max_planner_steps=2, max_changed_files=8)
    assert draft.units[1].estimated_size == "medium"
    assert draft.units[1].asset_paths == []
    assert draft.units[1].risk_tags == ["cli_surface"]
    assert draft.units[1].budget == DeliveryUnitBudget(max_planner_steps=1)
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_parse_delivery_authoring_output_accepts_single_fenced_json_block(tmp_path: Path) -> None:
    output = f"```json\n{json.dumps(_draft_data())}\n```"

    draft = _parse(output, tmp_path)

    assert draft.plan_id == "team-invites"
    assert [unit.id for unit in draft.units] == ["foundation", "cli"]


def test_parse_delivery_authoring_output_normalizes_asset_paths(tmp_path: Path) -> None:
    data = _draft_data()
    data["units"][0]["asset_paths"] = [".sikula\\task-assets\\invite-reference.png"]

    draft = _parse(json.dumps(data), tmp_path)

    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]


@pytest.mark.parametrize(
    ("asset_paths", "expected_code"),
    [
        (".sikula/task-assets/reference.png", "delivery_authoring.asset_paths_invalid_type"),
        ([""], "delivery_authoring.asset_path_invalid"),
        (["assets/reference.png\nprivate"], "delivery_authoring.asset_path_invalid"),
        (
            [".sikula/task-assets/reference.png", "./.sikula/task-assets/reference.png"],
            "delivery_authoring.asset_path_duplicate",
        ),
    ],
)
def test_parse_delivery_authoring_output_rejects_invalid_asset_paths(
    tmp_path: Path,
    asset_paths: object,
    expected_code: str,
) -> None:
    data = _draft_data()
    data["units"][0]["asset_paths"] = asset_paths

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(json.dumps(data), tmp_path)

    assert exc_info.value.code == expected_code


def test_parse_delivery_authoring_output_preserves_declared_asset_aliases(tmp_path: Path) -> None:
    aliases = [
        str(tmp_path / "assets" / "reference.png"),
        "assets/nested/../reference.png",
        r"C:\workspace\assets\reference.png",
    ]

    for alias in aliases:
        data = _draft_data()
        data["units"][0]["asset_paths"] = [alias]

        draft = _parse(json.dumps(data), tmp_path)

        assert draft.units[0].asset_paths == [alias]


@pytest.mark.parametrize(
    "output",
    [
        lambda: f"Prepared plan: {json.dumps(_draft_data())}",
        lambda: f"Prepared plan:\n```json\n{json.dumps(_draft_data())}\n```",
        lambda: f"Prepared plan:\n~~~JSON\n{json.dumps(_draft_data())}\n~~~\nDone.",
        lambda: f"The source uses config {{ mode }}.\n{json.dumps(_draft_data())}",
    ],
    ids=["same-line-prose", "fenced-after-prose", "tilde-fence-with-trailing-prose", "source-brace-preamble"],
)
def test_parse_delivery_authoring_output_accepts_one_schema_object_surrounded_by_prose(
    tmp_path: Path,
    output: Callable[[], str],
) -> None:
    draft = _parse(output(), tmp_path)

    assert draft.plan_id == "team-invites"
    assert [unit.id for unit in draft.units] == ["foundation", "cli"]


def test_parse_delivery_assessment_output_accepts_fenced_schema_object_after_prose() -> None:
    payload = {
        "recommended_mode": "single_run",
        "reason_codes": ["single_cohesive_surface"],
        "units": [],
    }

    draft = parse_delivery_assessment_output(f"Assessment follows:\n```json\n{json.dumps(payload)}\n```")

    assert draft.recommended_mode == "single_run"


def test_parse_delivery_assessment_output_ignores_incomplete_schema_object_in_prose() -> None:
    payload = {
        "recommended_mode": "single_run",
        "reason_codes": ["single_cohesive_surface"],
        "units": [],
    }
    output = f'Example: {{"recommended_mode": "delivery_plan"}}\n{json.dumps(payload)}'

    draft = parse_delivery_assessment_output(output)

    assert draft.recommended_mode == "single_run"


def test_parse_delivery_assessment_output_rejects_schema_object_in_unclosed_array() -> None:
    payload = {
        "recommended_mode": "single_run",
        "reason_codes": ["single_cohesive_surface"],
        "units": [],
    }

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(f"Response: [{json.dumps(payload)}")

    assert exc_info.value.code == "delivery_authoring.json_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan_title", "Read /custom/private/task.md"),
        ("title", "Read /home/example/private/task.md"),
        ("plan_title", "Read /Users/example/private/task.md"),
        ("title", "Read /Users/example/private/task.md"),
        ("stream", r"client at C:\Users\example\private"),
        ("component", r"\\server\private\project"),
        ("phase", "Injected\nline"),
        ("kind", "x" * 1001),
        ("platform", "file:///Users/example/private"),
    ],
)
def test_parse_delivery_authoring_output_rejects_unsafe_public_metadata(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    data = _draft_data()
    if field == "plan_title":
        data["title"] = value
    else:
        data["units"][0][field] = value

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(json.dumps(data), tmp_path)

    assert exc_info.value.code == "delivery_authoring.label_invalid"
    assert value not in str(exc_info.value)


def test_parse_delivery_authoring_output_defaults_absent_optional_fields(tmp_path: Path) -> None:
    data = {
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "constraints": [],
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
    assert draft.units[0].budget == DeliveryUnitBudget(max_planner_steps=1)


def test_parse_delivery_authoring_output_preserves_inherited_constraints(tmp_path: Path) -> None:
    data = _draft_data()
    data["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Consume GET /api/v1/resource without changing its authoritative contract.",
            "unit_ids": ["foundation", "cli"],
            "disposition": "preserved",
        }
    ]

    draft = _parse(json.dumps(data), tmp_path)

    assert [constraint.to_plan_dict() for constraint in draft.constraints] == data["constraints"]


def test_constraint_verification_requires_actionable_gaps_when_incomplete() -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_verification_output(
            json.dumps({"constraints_complete": False, "constraints": []}),
            unit_ids={"foundation"},
        )

    assert exc_info.value.code == "delivery_constraint_verification.gaps_required"


def test_constraint_verification_parses_bounded_omitted_gap() -> None:
    gap = {
        "reason": "omitted",
        "kind": "authoritative_read_only_dependency",
        "summary": "The existing protocol contract remains authoritative.",
        "affected_unit_ids": ["foundation"],
    }

    verification = parse_delivery_constraint_verification_output(
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [],
                "constraint_gaps": [gap],
            }
        ),
        unit_ids={"foundation"},
    )

    assert [value.to_dict() for value in verification.constraint_gaps] == [gap]


def test_constraint_verification_rejects_incomplete_gap_without_missing_assignment() -> None:
    constraint = {
        "id": "protocol-authority",
        "kind": "repository_ownership",
        "summary": "Protocol changes remain externally owned.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_verification_output(
            json.dumps(
                {
                    "constraints_complete": False,
                    "constraints": [constraint],
                    "constraint_gaps": [
                        {
                            "reason": "incompletely_assigned",
                            "constraint_id": "protocol-authority",
                            "kind": "repository_ownership",
                            "summary": "Protocol changes remain externally owned.",
                            "affected_unit_ids": ["foundation"],
                        }
                    ],
                }
            ),
            unit_ids={"foundation"},
        )

    assert exc_info.value.code == "delivery_constraint_verification.gap_assignment_not_missing"


def test_constraint_verification_rejects_mixed_existing_and_missing_assignment_gap() -> None:
    constraint = {
        "id": "protocol-authority",
        "kind": "authoritative_read_only_dependency",
        "summary": "Protocol behavior remains defined by its authoritative contract.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    output = json.dumps(
        {
            "constraints_complete": False,
            "constraints": [constraint],
            "constraint_gaps": [
                {
                    "reason": "incompletely_assigned",
                    "constraint_id": constraint["id"],
                    "kind": constraint["kind"],
                    "summary": constraint["summary"],
                    "affected_unit_ids": ["foundation", "consumer"],
                }
            ],
        }
    )

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_verification_output(
            output,
            unit_ids={"foundation", "consumer"},
        )

    assert exc_info.value.code == "delivery_constraint_verification.gap_assignment_not_missing"


def test_constraint_repair_rejects_source_excerpt() -> None:
    source_rule = "Only the protocol repository may change protocol files."
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_repair_output(
            json.dumps(
                {
                    "constraints": [
                        {
                            "id": "protocol-authority",
                            "kind": "repository_ownership",
                            "summary": source_rule,
                            "unit_ids": ["foundation"],
                            "disposition": "preserved",
                        }
                    ]
                }
            ),
            unit_ids={"foundation"},
            source_task_description=f"# Task\n\n- {source_rule}\n",
        )

    assert exc_info.value.code == "delivery_authoring.constraint_summary_source_excerpt"


def test_unit_context_verification_parses_exact_missing_source_lines() -> None:
    source_line = '- <screen.title> — "Resource"'
    verification = parse_delivery_constraint_verification_output(
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [],
                "constraint_gaps": [],
                "unit_context_complete": False,
                "unit_context_gaps": [
                    {
                        "unit_id": "foundation",
                        "source_literals": [source_line],
                    }
                ],
            }
        ),
        unit_ids={"foundation"},
        source_task_description=f"# Task\n\n{source_line}\n",
        unit_task_markdown_by_id={"foundation": _unit_markdown()},
        require_unit_context=True,
    )

    assert verification.unit_context_complete is False
    assert [gap.to_dict() for gap in verification.unit_context_gaps] == [
        {"unit_id": "foundation", "source_literals": [source_line]}
    ]


def test_unit_context_verification_is_required_for_delivery_authoring() -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_verification_output(
            json.dumps({"constraints_complete": True, "constraints": []}),
            unit_ids={"foundation"},
            source_task_description="# Task",
            unit_task_markdown_by_id={"foundation": _unit_markdown()},
            require_unit_context=True,
        )

    assert exc_info.value.code == "delivery_unit_context_verification.complete_invalid"


def test_unit_context_verification_rejects_literal_that_is_not_a_complete_source_line() -> None:
    source_line = '- <screen.title> — "Resource"'
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_constraint_verification_output(
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "constraint_gaps": [],
                    "unit_context_complete": False,
                    "unit_context_gaps": [
                        {
                            "unit_id": "foundation",
                            "source_literals": ["<screen.title>"],
                        }
                    ],
                }
            ),
            unit_ids={"foundation"},
            source_task_description=f"# Task\n\n{source_line}\n",
            unit_task_markdown_by_id={"foundation": _unit_markdown()},
            require_unit_context=True,
        )

    assert exc_info.value.code == "delivery_unit_context_verification.literal_not_source_line"


def test_apply_unit_context_gaps_changes_only_affected_task_markdown() -> None:
    unit = DeliveryAuthoringUnitDraft(
        id="foundation",
        title="Foundation",
        depends_on=[],
        task_markdown=_unit_markdown(),
        asset_paths=[".sikula/task-assets/reference.png"],
        scope_paths=["core"],
        budget=DeliveryUnitBudget(max_planner_steps=1),
    )
    source_line = '- <screen.title> — "Resource"'
    verification = parse_delivery_constraint_verification_output(
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [],
                "constraint_gaps": [],
                "unit_context_complete": False,
                "unit_context_gaps": [{"unit_id": "foundation", "source_literals": [source_line]}],
            }
        ),
        unit_ids={unit.id},
        source_task_description=source_line,
        unit_task_markdown_by_id={unit.id: unit.task_markdown},
        require_unit_context=True,
    )

    repaired = apply_delivery_unit_context_gaps([unit], verification.unit_context_gaps)

    assert repaired[0].task_markdown.startswith(unit.task_markdown.rstrip())
    assert source_line in repaired[0].task_markdown
    assert repaired[0].asset_paths == unit.asset_paths
    assert repaired[0].scope_paths == unit.scope_paths
    assert repaired[0].budget == unit.budget


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("too_many_constraints", "delivery_authoring.constraints_too_many"),
        ("non_object", "delivery_authoring.constraint_not_object"),
        ("invalid_id", "delivery_authoring.constraint_id_invalid"),
        ("duplicate_id", "delivery_authoring.constraint_id_duplicate"),
        ("empty_units", "delivery_authoring.constraint_units_empty"),
        ("too_many_units", "delivery_authoring.constraint_units_too_many"),
        ("duplicate_unit", "delivery_authoring.constraint_unit_duplicate"),
    ],
)
def test_parse_delivery_authoring_output_rejects_constraint_boundary_cases(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    data = _draft_data()
    constraint = {
        "id": "ownership",
        "kind": "repository_ownership",
        "summary": "Keep repository ownership explicit.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    data["constraints"] = [constraint]
    if case == "too_many_constraints":
        data["constraints"] = [
            {**constraint, "id": f"ownership-{index}"} for index in range(MAX_DELIVERY_CONSTRAINTS + 1)
        ]
    elif case == "non_object":
        data["constraints"] = ["ownership"]
    elif case == "invalid_id":
        constraint["id"] = "x" * (MAX_DELIVERY_UNIT_ID_LENGTH + 1)
    elif case == "duplicate_id":
        data["constraints"] = [constraint, {**constraint, "id": "OWNERSHIP"}]
    elif case == "empty_units":
        constraint["unit_ids"] = []
    elif case == "too_many_units":
        constraint["unit_ids"] = ["foundation"] * (MAX_DELIVERY_CONSTRAINT_UNIT_IDS + 1)
    elif case == "duplicate_unit":
        constraint["unit_ids"] = ["foundation", "foundation"]

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(json.dumps(data), tmp_path)

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        pytest.param(
            lambda data: data.pop("constraints"),
            "delivery_authoring.constraints_required",
            id="missing",
        ),
        pytest.param(
            lambda data: data.update({"constraints": {}}),
            "delivery_authoring.constraints_invalid_type",
            id="invalid_type",
        ),
        pytest.param(
            lambda data: data.update(
                {
                    "constraints": [
                        {
                            "id": "ownership",
                            "kind": "unknown_kind",
                            "summary": "Keep ownership explicit.",
                            "unit_ids": ["foundation"],
                            "disposition": "preserved",
                        }
                    ]
                }
            ),
            "delivery_authoring.constraint_kind_invalid",
            id="kind",
        ),
        pytest.param(
            lambda data: data.update(
                {
                    "constraints": [
                        {
                            "id": "ownership",
                            "kind": "repository_ownership",
                            "summary": "Read /Users/example/private/task.md",
                            "unit_ids": ["foundation"],
                            "disposition": "preserved",
                        }
                    ]
                }
            ),
            "delivery_authoring.label_invalid",
            id="unsafe_summary",
        ),
        pytest.param(
            lambda data: data.update(
                {
                    "constraints": [
                        {
                            "id": "ownership",
                            "kind": "repository_ownership",
                            "summary": "Keep ownership explicit.",
                            "unit_ids": ["missing"],
                            "disposition": "preserved",
                        }
                    ]
                }
            ),
            "delivery_authoring.constraint_unit_unknown",
            id="unknown_unit",
        ),
        pytest.param(
            lambda data: data.update(
                {
                    "constraints": [
                        {
                            "id": "ownership",
                            "kind": "repository_ownership",
                            "summary": "Keep ownership explicit.",
                            "unit_ids": ["foundation"],
                            "disposition": "maybe",
                        }
                    ]
                }
            ),
            "delivery_authoring.constraint_disposition_invalid",
            id="disposition",
        ),
    ],
)
def test_parse_delivery_authoring_output_rejects_invalid_constraints(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], object],
    expected_code: str,
) -> None:
    data = _draft_data()
    mutator(data)

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(json.dumps(data), tmp_path)

    assert exc_info.value.code == expected_code


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


def test_parse_delivery_amendment_authoring_output_accepts_null_amend_reason(tmp_path: Path) -> None:
    output = json.dumps(_amendment_data())

    draft = parse_delivery_amendment_authoring_output(
        output,
        expected_plan_id="team-invites",
        expected_target_unit_id="oversized",
        project_root=tmp_path,
    )

    assert draft.amend_reason is None
    assert [unit.id for unit in draft.replacement_units] == ["split-a", "split-b"]
    assert draft.replacement_units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]


def test_parse_delivery_amendment_authoring_output_preserves_declared_asset_alias(tmp_path: Path) -> None:
    aliases = [
        str(tmp_path / "assets" / "invite-reference.png"),
        r"C:\workspace\assets\invite-reference.png",
    ]

    for alias in aliases:
        data = _amendment_data()
        data["replacement_units"][0]["asset_paths"] = [alias]

        draft = parse_delivery_amendment_authoring_output(
            json.dumps(data),
            expected_plan_id="team-invites",
            expected_target_unit_id="oversized",
            project_root=tmp_path,
        )

        assert draft.replacement_units[0].asset_paths == [alias]


def test_parse_delivery_amendment_authoring_output_accepts_external_follow_up(tmp_path: Path) -> None:
    data = {
        "plan_id": "team-invites",
        "target_unit_id": "oversized",
        "disposition": "external_dependency_follow_up_required",
        "summary": "The required protocol change remains owned by the protocol repository.",
        "amend_reason": None,
        "budget_exceeded": None,
        "warnings": [],
        "replacement_units": [],
    }

    draft = parse_delivery_amendment_authoring_output(
        json.dumps(data),
        expected_plan_id="team-invites",
        expected_target_unit_id="oversized",
        project_root=tmp_path,
    )

    assert draft.disposition == "external_dependency_follow_up_required"
    assert draft.summary == data["summary"]
    assert draft.replacement_units == []


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"disposition": "unknown", "summary": "External work."}, "delivery_amend_authoring.disposition_invalid"),
        (
            {"disposition": "external_dependency_follow_up_required", "summary": None},
            "delivery_amend_authoring.summary_required",
        ),
        ({"summary": "Unexpected summary."}, "delivery_amend_authoring.summary_unexpected"),
        (
            {
                "disposition": "external_dependency_follow_up_required",
                "summary": "Read /Users/example/private/task.md",
            },
            "delivery_authoring.label_invalid",
        ),
        (
            {
                "disposition": "external_dependency_follow_up_required",
                "summary": "External work.",
            },
            "delivery_amend_authoring.replacements_unexpected",
        ),
    ],
)
def test_parse_delivery_amendment_authoring_output_rejects_malformed_external_follow_up(
    tmp_path: Path,
    updates: dict[str, Any],
    expected_code: str,
) -> None:
    data = _amendment_data()
    data.update(updates)
    if updates.get("disposition") == "external_dependency_follow_up_required" and expected_code != (
        "delivery_amend_authoring.replacements_unexpected"
    ):
        data["replacement_units"] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_amendment_authoring_output(
            json.dumps(data),
            expected_plan_id="team-invites",
            expected_target_unit_id="oversized",
            project_root=tmp_path,
        )

    assert exc_info.value.code == expected_code


def test_parse_delivery_amendment_authoring_output_accepts_fenced_schema_object_after_prose(tmp_path: Path) -> None:
    output = (
        "Based on the target unit, I will split it into two smaller units.\n\n"
        f"```json\n{json.dumps(_amendment_data())}\n```"
    )

    draft = parse_delivery_amendment_authoring_output(
        output,
        expected_plan_id="team-invites",
        expected_target_unit_id="oversized",
        project_root=tmp_path,
    )

    assert [unit.id for unit in draft.replacement_units] == ["split-a", "split-b"]


def test_parse_delivery_amendment_authoring_output_ignores_incomplete_schema_object_in_prose(
    tmp_path: Path,
) -> None:
    incomplete = {
        "target_unit_id": "oversized",
        "replacement_units": [],
    }
    output = f"Example: {json.dumps(incomplete)}\n{json.dumps(_amendment_data())}"

    draft = parse_delivery_amendment_authoring_output(
        output,
        expected_plan_id="team-invites",
        expected_target_unit_id="oversized",
        project_root=tmp_path,
    )

    assert [unit.id for unit in draft.replacement_units] == ["split-a", "split-b"]


def test_parse_delivery_authoring_output_ignores_incomplete_schema_object_in_prose(tmp_path: Path) -> None:
    incomplete = {"plan_id": "example", "units": []}

    draft = _parse(f"Example: {json.dumps(incomplete)}\n{json.dumps(_draft_data())}", tmp_path)

    assert draft.plan_id == "team-invites"


@pytest.mark.parametrize(
    "output",
    [
        lambda: f"{json.dumps(_draft_data())}\n{json.dumps(_draft_data())}",
        lambda: f"Wrapped response: {json.dumps([{'fixture': True}, _draft_data()])}",
        lambda: f"[{json.dumps(_draft_data())}",
        lambda: f'{{"plan_id":"team-invites","title":"Team invites","units": invalid}}\n{json.dumps(_draft_data())}',
        lambda: (
            f'{{"plan\\u005fid":"team-invites","title":"Team invites","units": invalid}}\n{json.dumps(_draft_data())}'
        ),
        lambda: (
            'Draft: {"plan_id":"team-invites","plan_id":"team-invites",'
            f'"title":"Team invites","units":[]}}\n{json.dumps(_draft_data())}'
        ),
        lambda: (
            'Draft: {"plan_id":"team-invites","plan\\u005fid":"other-plan",'
            f'"title":"Team invites","units":[]}}\n{json.dumps(_draft_data())}'
        ),
    ],
    ids=[
        "multiple-schema-objects",
        "schema-object-inside-array",
        "schema-object-inside-unclosed-array",
        "malformed-before-valid",
        "escaped-key-malformed-before-valid",
        "duplicate-key-before-valid",
        "escaped-duplicate-key-before-valid",
    ],
)
def test_parse_delivery_authoring_output_rejects_ambiguous_or_nested_schema_objects(
    tmp_path: Path,
    output: Callable[[], str],
) -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _parse(output(), tmp_path)

    assert exc_info.value.code == "delivery_authoring.json_invalid"


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        pytest.param(
            lambda data: data.update({"plan_id": "other-plan"}),
            "delivery_amend_authoring.plan_id_mismatch",
            id="plan_id_mismatch",
        ),
        pytest.param(
            lambda data: data.update({"target_unit_id": "other-unit"}),
            "delivery_amend_authoring.target_unit_mismatch",
            id="target_unit_mismatch",
        ),
        pytest.param(
            lambda data: data["replacement_units"][0].update({"id": "oversized"}),
            "delivery_amend_authoring.target_id_reused",
            id="target_id_reused",
        ),
        pytest.param(
            lambda data: data.update({"amend_reason": "not a stable code"}),
            "delivery_amend_authoring.amend_reason_invalid",
            id="amend_reason_invalid",
        ),
        pytest.param(
            lambda data: data.update({"budget_exceeded": {"name": "max_planner_steps"}}),
            "delivery_amend_authoring.budget_exceeded_invalid",
            id="budget_shape_invalid",
        ),
        pytest.param(
            lambda data: data.update({"budget_exceeded": {"name": "not a code", "limit": 2, "actual": 5}}),
            "delivery_amend_authoring.budget_exceeded_invalid",
            id="budget_name_invalid",
        ),
        pytest.param(
            lambda data: data.update({"budget_exceeded": {"name": "max_planner_steps", "limit": True, "actual": 5}}),
            "delivery_amend_authoring.budget_exceeded_invalid",
            id="budget_limit_invalid",
        ),
        pytest.param(
            lambda data: data.update({"budget_exceeded": {"name": "max_planner_steps", "limit": 2, "actual": -1}}),
            "delivery_amend_authoring.budget_exceeded_invalid",
            id="budget_actual_invalid",
        ),
    ],
)
def test_parse_delivery_amendment_authoring_output_rejects_invalid_metadata(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], None],
    expected_code: str,
) -> None:
    data = {
        "plan_id": "team-invites",
        "target_unit_id": "oversized",
        "amend_reason": "unit_budget_exceeded",
        "budget_exceeded": {"name": "max_planner_steps", "limit": 2, "actual": 5},
        "warnings": [],
        "replacement_units": [
            {
                "id": "split-a",
                "title": "Split A",
                "depends_on": [],
                "task_markdown": _unit_markdown("Split A"),
            }
        ],
    }
    mutator(data)

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_amendment_authoring_output(
            json.dumps(data),
            expected_plan_id="team-invites",
            expected_target_unit_id="oversized",
            project_root=tmp_path,
        )

    assert exc_info.value.code == expected_code


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
            lambda root: '```json\n{"plan_id":"team-invites","units": invalid}\n```',
            "delivery_authoring.output_invalid_envelope",
            id="malformed_fenced_response",
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
            lambda root: _output_with(lambda data: data["units"][0].update({"budget": {"max_planner_steps": 3}})),
            "delivery_authoring.planner_step_budget_invalid",
            id="planner_step_budget_requires_split",
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


def test_parse_delivery_authoring_output_rejects_verbatim_constraint_summary(tmp_path: Path) -> None:
    source_rule = "Only the protocol repository may change protocol files."
    data = _draft_data()
    data["constraints"] = [
        {
            "id": "protocol-ownership",
            "kind": "repository_ownership",
            "summary": source_rule,
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
    ]

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_authoring_output(
            json.dumps(data),
            expected_plan_id="team-invites",
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
            source_task_description=f"# Task\n\n- **{source_rule}**\n",
        )

    assert exc_info.value.code == "delivery_authoring.constraint_summary_source_excerpt"
    assert source_rule not in exc_info.value.message


def test_parse_delivery_authoring_output_accepts_paraphrased_constraint_summary(tmp_path: Path) -> None:
    data = _draft_data()
    data["constraints"] = [
        {
            "id": "protocol-ownership",
            "kind": "repository_ownership",
            "summary": "Protocol edits stay under external repository ownership.",
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
    ]

    draft = parse_delivery_authoring_output(
        json.dumps(data),
        expected_plan_id="team-invites",
        project_root=tmp_path,
        output_dir=".sikula/delivery/team-invites",
        source_task_description="# Task\n\nOnly the protocol repository may change protocol files.\n",
    )

    assert draft.constraints[0].summary == "Protocol edits stay under external repository ownership."


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
