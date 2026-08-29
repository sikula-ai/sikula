from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from agents.base_agent import AGENT_SECURITY_PREFIX, READONLY_AGENT_PREFIX
from agents.delivery_preparation_agent import (
    DeliveryPreparationAgent,
    DeliveryPreparationAgentError,
)
from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAssessmentDraft,
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
)


class CapturingLLM:
    def __init__(self, output: str, *, verification_output: str | Exception | None = None) -> None:
        self.output = output
        self.outputs = [
            output,
            verification_output
            or json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
        self.system_prompts: list[str] = []
        self.prompts: list[str] = []
        self.readonly_agent_calls: list[str] = []
        self.agent_calls: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.system_prompts.append(system)
        self.prompts.append(user)
        output = self.outputs[min(len(self.prompts) - 1, len(self.outputs) - 1)]
        if isinstance(output, Exception):
            raise output
        return output

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.readonly_agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        self.agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")


class FailingLLM:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.system_prompts: list[str] = []
        self.prompts: list[str] = []
        self.readonly_agent_calls: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.system_prompts.append(system)
        self.prompts.append(user)
        raise self.error

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.readonly_agent_calls.append(prompt)
        raise AssertionError("delivery preparation must use plain generation")


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

## Security and privacy

- Do not expose raw prompts, provider output, or source excerpts.

## Tests

- Cover generated delivery plan authoring output.

## Reviewer focus

- Check the behavior boundary and privacy-safe diagnostics.

## Out of scope

- Do not write delivery plan files in this unit.

## Validation

- `python3 -m pytest tests/test_delivery_preparation_agent.py`
"""


def _authoring_output(*, planning_mode: str | None = "fixed_window", warnings: list[str] | None = None) -> str:
    data = {
        "plan_id": "team-invites",
        "title": "Team invites delivery",
        "constraints": [],
        "units": [
            {
                "id": "foundation",
                "title": "Prepare foundation",
                "depends_on": [],
                "task_markdown": _unit_markdown("Foundation"),
                "scope_paths": ["agents"],
                "asset_paths": [".sikula/task-assets/invite-reference.png"],
                "estimated_size": "small",
                "risk_tags": ["automation_behavior"],
                "budget": {"max_planner_steps": 1},
            }
        ],
    }
    if planning_mode is not None:
        data["planning_mode"] = planning_mode
    if warnings is not None:
        data["warnings"] = warnings
    return json.dumps(data)


def _amendment_output(*, target_unit_id: str = "oversized") -> str:
    return json.dumps(
        {
            "plan_id": "team-invites",
            "target_unit_id": target_unit_id,
            "amend_reason": "unit_budget_exceeded",
            "budget_exceeded": {"name": "max_planner_steps", "limit": 2, "actual": 5},
            "warnings": [],
            "replacement_units": [
                {
                    "id": "invite-storage",
                    "title": "Invite storage",
                    "depends_on": [],
                    "asset_paths": [".sikula/task-assets/invite-reference.png"],
                    "task_markdown": _unit_markdown("Invite storage"),
                    "estimated_size": "small",
                    "risk_tags": ["data_persistence"],
                    "budget": {"max_planner_steps": 1},
                },
                {
                    "id": "invite-cli",
                    "title": "Invite CLI",
                    "depends_on": ["invite-storage"],
                    "task_markdown": _unit_markdown("Invite CLI"),
                    "estimated_size": "small",
                    "risk_tags": ["cli_surface"],
                    "budget": {"max_planner_steps": 1},
                },
            ],
        }
    )


def _assessment_output() -> str:
    return json.dumps(
        {
            "recommended_mode": "delivery_plan",
            "reason_codes": ["multiple_platforms", "dependency_order_required"],
            "units": [
                {
                    "id": "shared",
                    "title": "Shared behavior",
                    "depends_on": [],
                    "component": "shared",
                    "platform": "shared",
                },
                {
                    "id": "platform-a",
                    "title": "Platform A",
                    "depends_on": ["shared"],
                    "component": "client-a",
                    "platform": "platform-a",
                },
                {
                    "id": "platform-b",
                    "title": "Platform B",
                    "depends_on": ["shared"],
                    "component": "client-b",
                    "platform": "platform-b",
                },
            ],
        }
    )


def _author_delivery_plan(
    agent: DeliveryPreparationAgent,
    *,
    tmp_path: Path,
    task_path: str | Path = ".sikula/tasks/team-invites.md",
    project_context: dict | None = None,
    audit_records: list[dict] | None = None,
) -> DeliveryAuthoringDraft:
    return agent.author_delivery_plan(
        task_description="Add team invite authoring without exposing raw task text.",
        task_path=task_path,
        plan_id="team-invites",
        project_root=tmp_path,
        output_dir=".sikula/delivery/team-invites",
        project_context=project_context,
        audit_recorder=None if audit_records is None else audit_records.append,
    )


def _assess_delivery_mode(
    agent: DeliveryPreparationAgent,
    *,
    tmp_path: Path,
    audit_records: list[dict] | None = None,
) -> DeliveryAssessmentDraft:
    return agent.assess_delivery_mode(
        task_description="Implement the same observable feature across two project platforms.",
        task_path=".sikula/tasks/cross-platform-feature.md",
        project_root=tmp_path,
        project_context={
            "stack": "mixed client monorepo",
            "validation_commands": ["project-test-command"],
        },
        audit_recorder=None if audit_records is None else audit_records.append,
    )


def _author_delivery_amendment(
    agent: DeliveryPreparationAgent,
    *,
    tmp_path: Path,
    audit_records: list[dict] | None = None,
) -> DeliveryAmendmentAuthoringDraft:
    return agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id="oversized",
        target_task_description="Split the oversized invite behavior without exposing this body.",
        target_unit={"id": "oversized", "depends_on": ["foundation"]},
        downstream_units=[{"id": "release", "depends_on": ["oversized"]}],
        project_root=tmp_path,
        project_context={"validation_commands": ["python3 -m pytest tests/"]},
        audit_recorder=None if audit_records is None else audit_records.append,
    )


def test_author_delivery_plan_calls_generate_and_records_success(tmp_path: Path) -> None:
    (tmp_path / "guidelines.md").write_text("# Project Guidelines\nKeep prompts platform-neutral.\n")
    llm = CapturingLLM(_authoring_output(warnings=["Review before writing artifacts."]))
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={
            "project": {"platform": "backend", "language": "Python"},
            "guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000},
        },
    )
    audit_records: list[dict] = []

    draft = _author_delivery_plan(
        agent,
        tmp_path=tmp_path,
        project_context={"validation_commands": ["python3 -m pytest tests/"], "delivery": {"mode": "slice"}},
        audit_records=audit_records,
    )

    assert draft.plan_id == "team-invites"
    assert draft.planning_mode == "fixed_window"
    assert draft.warnings == ["Review before writing artifacts."]
    assert draft.source_task is not None
    assert draft.source_task.path == ".sikula/tasks/team-invites.md"
    assert draft.source_task.sha256 == (
        "sha256:" + sha256(b"Add team invite authoring without exposing raw task text.").hexdigest()
    )
    assert [unit.id for unit in draft.units] == ["foundation"]
    assert draft.units[0].scope_paths == ["agents"]
    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert draft.units[0].estimated_size == "small"
    assert draft.units[0].risk_tags == ["automation_behavior"]
    assert draft.units[0].budget is not None
    assert draft.units[0].budget.to_dict() == {"max_planner_steps": 1}
    assert llm.system_prompts == ["", ""]
    assert llm.readonly_agent_calls == []
    assert llm.agent_calls == []
    prompt = llm.prompts[0]
    assert prompt.startswith(AGENT_SECURITY_PREFIX)
    assert READONLY_AGENT_PREFIX in prompt
    assert "Project stack: backend / Python" in prompt
    assert "Selected delivery plan id: team-invites" in prompt
    assert "Source task file: .sikula/tasks/team-invites.md" in prompt
    assert "Delivery output directory: .sikula/delivery/team-invites" in prompt
    assert "- guidelines.md" in prompt
    assert "# Project Guidelines" in prompt
    assert '"python3 -m pytest tests/"' in prompt
    assert "Do not include writer-facing path fields" in prompt
    assert "constraints must explicitly list every hard source-task constraint" in prompt
    assert "authoritative_read_only_dependency" in prompt
    assert '"disposition": "preserved"' in prompt
    assert "Do not write, edit, delete, move, rename, format, or create files." in prompt
    assert "Prefer small units with one primary production surface" in prompt
    assert "Security and privacy" in prompt
    assert "Security/privacy notes" not in prompt
    assert "Security and privacy, Reviewer focus" in prompt
    assert "Security and privacy, Tests" not in prompt
    assert "where applicable" not in prompt
    assert "must include all of these exact contract-ready section headings" in prompt
    assert "Validation sections must include explicit commands" in prompt
    assert "asset_paths must contain only paths declared" in prompt
    assert "least one relevant unit" in prompt
    assert "must not include an asset-root section" in prompt
    assert "Deterministic writer code renders assigned source declarations" in prompt
    assert "UI/API/CLI behavior" in prompt
    assert "data model or persistence changes" in prompt
    assert "automation or prompt-driven behavior" in prompt
    assert "External provider, tool, or integration boundary changes" in prompt
    assert '"estimated_size": "small"' in prompt
    assert '"risk_tags": ["cli_surface"]' in prompt
    assert '"budget": {"max_planner_steps": 1, "max_changed_files": 8}' in prompt
    assert '"component": "optional non-empty string"' in prompt
    assert "Design every unit for a single implementation pass" in prompt
    assert "Never set max_planner_steps to 3 or more" in prompt
    assert "Add team invite authoring without exposing raw task text." in prompt
    assert len(audit_records) == 2
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["round_index"] == 1
    assert record["prompt"] == prompt
    assert record["raw_output"] == llm.output
    assert record["parsed"] == {
        "status": "parsed",
        "plan_id": "team-invites",
        "unit_ids": ["foundation"],
        "unit_count": 1,
        "planning_mode": "fixed_window",
        "warnings": ["Review before writing artifacts."],
    }
    verification_record = audit_records[1]
    assert verification_record["phase"] == "delivery_prepare_constraint_verification"
    assert verification_record["parsed"] == {
        "status": "parsed",
        "constraints_complete": True,
        "constraint_ids": [],
        "dispositions": [],
        "constraint_gaps": [],
        "unit_context_complete": True,
        "unit_context_gaps": [],
    }
    assert "independent read-only delivery-constraint verifier" in llm.prompts[1]
    assert '"asset_paths": [' in llm.prompts[1]
    assert ".sikula/task-assets/invite-reference.png" in llm.prompts[1]
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_author_delivery_plan_repairs_one_omitted_constraint_and_reverifies(tmp_path: Path) -> None:
    gap = {
        "reason": "omitted",
        "kind": "repository_ownership",
        "summary": "Protocol file changes remain owned by the protocol repository.",
        "affected_unit_ids": ["foundation"],
    }
    repaired_constraint = {
        "id": "protocol-repository-ownership",
        "kind": "repository_ownership",
        "summary": gap["summary"],
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    llm = CapturingLLM(_authoring_output())
    llm.outputs = [
        _authoring_output(),
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [],
                "constraint_gaps": [gap],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
        json.dumps({"constraints": [repaired_constraint]}),
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [repaired_constraint],
                "constraint_gaps": [],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
    ]
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    draft = agent.author_delivery_plan(
        task_description="Only the protocol repository may change protocol files.",
        task_path=".sikula/tasks/team-invites.md",
        plan_id="team-invites",
        project_root=tmp_path,
        output_dir=".sikula/delivery/team-invites",
        audit_recorder=audit_records.append,
    )

    assert draft.constraint_verification is not None
    assert draft.constraint_verification.constraints_complete is True
    assert [constraint.to_plan_dict() for constraint in draft.constraints] == [repaired_constraint]
    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert len(llm.prompts) == 4
    assert "Only the protocol repository may change protocol files." in llm.prompts[1]
    assert "Repair only the structured constraint list" in llm.prompts[2]
    assert json.dumps([gap], indent=2, sort_keys=True) in llm.prompts[2]
    assert "source_task_to_units_after_bounded_repair" in llm.prompts[3]
    assert [record["phase"] for record in audit_records] == [
        "delivery_prepare_authoring",
        "delivery_prepare_constraint_verification",
        "delivery_prepare_constraint_repair",
        "delivery_prepare_constraint_verification",
    ]
    assert [record["round_index"] for record in audit_records] == [1, 1, 1, 2]
    assert audit_records[1]["parsed"]["constraint_gaps"] == [gap]


def test_author_delivery_plan_adds_missing_exact_source_values_and_reverifies(tmp_path: Path) -> None:
    first_literal = '- <resource.title> — "Resource"'
    second_literal = '- <resource.submit> — "Save"'
    authored = json.loads(_authoring_output())
    authored["units"][0]["task_markdown"] = _unit_markdown("Foundation").replace(
        "Prepare the delivery unit.",
        "Prepare the delivery unit using the provided localization keys.",
    )
    gap = {
        "unit_id": "foundation",
        "source_literals": [first_literal, second_literal],
    }
    llm = CapturingLLM(json.dumps(authored))
    llm.outputs = [
        json.dumps(authored),
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [],
                "constraint_gaps": [],
                "unit_context_complete": False,
                "unit_context_gaps": [gap],
            }
        ),
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [],
                "constraint_gaps": [],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
    ]
    audit_records: list[dict] = []

    agent = DeliveryPreparationAgent(llm=llm)
    draft = agent.author_delivery_plan(
        task_description=(f"# Resource\n\n## Context\n\nLocalization keys:\n\n{first_literal}\n{second_literal}\n"),
        task_path=".sikula/tasks/resource.md",
        plan_id="team-invites",
        project_root=tmp_path,
        output_dir=".sikula/delivery/team-invites",
        audit_recorder=audit_records.append,
    )

    assert len(llm.prompts) == 3
    assert first_literal in draft.units[0].task_markdown
    assert second_literal in draft.units[0].task_markdown
    assert "## Authoritative source values" in draft.units[0].task_markdown
    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert draft.units[0].scope_paths == ["agents"]
    assert draft.constraint_verification is not None
    assert draft.constraint_verification.unit_context_complete is True
    assert [record["phase"] for record in audit_records] == [
        "delivery_prepare_authoring",
        "delivery_prepare_constraint_verification",
        "delivery_prepare_constraint_verification",
    ]
    assert audit_records[1]["parsed"]["unit_context_gaps"] == [{"unit_id": "foundation", "source_literal_count": 2}]


def test_author_delivery_plan_repairs_only_identified_missing_assignment(tmp_path: Path) -> None:
    authored = json.loads(_authoring_output())
    authored["units"].append(
        {
            "id": "consumer",
            "title": "Prepare consumer",
            "depends_on": ["foundation"],
            "task_markdown": _unit_markdown("Consumer"),
            "scope_paths": ["core"],
            "asset_paths": [],
            "estimated_size": "small",
            "risk_tags": ["api_surface"],
            "budget": {"max_planner_steps": 1},
        }
    )
    existing = {
        "id": "protocol-authority",
        "kind": "authoritative_read_only_dependency",
        "summary": "Protocol behavior remains defined by its authoritative contract.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    authored["constraints"] = [existing]
    gap = {
        "reason": "incompletely_assigned",
        "constraint_id": "protocol-authority",
        "kind": existing["kind"],
        "summary": existing["summary"],
        "affected_unit_ids": ["consumer"],
    }
    repaired = {**existing, "unit_ids": ["foundation", "consumer"]}
    llm = CapturingLLM(json.dumps(authored))
    llm.outputs = [
        json.dumps(authored),
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [existing],
                "constraint_gaps": [gap],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
        json.dumps({"constraints": [repaired]}),
        json.dumps(
            {
                "constraints_complete": True,
                "constraints": [repaired],
                "constraint_gaps": [],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
    ]

    draft = _author_delivery_plan(DeliveryPreparationAgent(llm=llm), tmp_path=tmp_path)

    assert [constraint.to_plan_dict() for constraint in draft.constraints] == [repaired]
    assert [unit.id for unit in draft.units] == ["foundation", "consumer"]
    assert draft.units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert draft.units[1].asset_paths == []


def test_author_delivery_plan_does_not_repair_incomplete_conflicting_verification(tmp_path: Path) -> None:
    authored = json.loads(_authoring_output())
    existing = {
        "id": "protocol-authority",
        "kind": "repository_ownership",
        "summary": "Protocol changes remain externally owned.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    authored["constraints"] = [existing]
    conflicting = {**existing, "disposition": "conflict"}
    gap = {
        "reason": "omitted",
        "kind": "security_boundary",
        "summary": "Credential handling remains inside the existing trust boundary.",
        "affected_unit_ids": ["foundation"],
    }
    llm = CapturingLLM(json.dumps(authored))
    llm.outputs = [
        json.dumps(authored),
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [conflicting],
                "constraint_gaps": [gap],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
    ]

    draft = _author_delivery_plan(DeliveryPreparationAgent(llm=llm), tmp_path=tmp_path)

    assert len(llm.prompts) == 2
    assert [constraint.to_plan_dict() for constraint in draft.constraints] == [existing]
    assert draft.constraint_verification is not None
    assert draft.constraint_verification.constraints[0].disposition == "conflict"


def test_author_delivery_plan_rejects_repair_that_rewrites_existing_constraint(tmp_path: Path) -> None:
    authored = json.loads(_authoring_output())
    existing = {
        "id": "protocol-authority",
        "kind": "authoritative_read_only_dependency",
        "summary": "Protocol behavior remains defined by its authoritative contract.",
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    authored["constraints"] = [existing]
    gap = {
        "reason": "omitted",
        "kind": "security_boundary",
        "summary": "Credential handling remains inside the existing trust boundary.",
        "affected_unit_ids": ["foundation"],
    }
    rewritten = {**existing, "summary": "A different protocol ownership rule."}
    addition = {
        "id": "credential-trust-boundary",
        "kind": "security_boundary",
        "summary": gap["summary"],
        "unit_ids": ["foundation"],
        "disposition": "preserved",
    }
    llm = CapturingLLM(json.dumps(authored))
    llm.outputs = [
        json.dumps(authored),
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [existing],
                "constraint_gaps": [gap],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
        json.dumps({"constraints": [rewritten, addition]}),
    ]
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(
            DeliveryPreparationAgent(llm=llm),
            tmp_path=tmp_path,
            audit_records=audit_records,
        )

    assert exc_info.value.code == "delivery_constraint_repair.existing_constraint_changed"
    assert len(llm.prompts) == 3
    assert audit_records[-1]["phase"] == "delivery_prepare_constraint_repair"
    assert audit_records[-1]["parsed"]["status"] == "failed"


def test_author_delivery_plan_rejects_repair_that_rephrases_omitted_gap(tmp_path: Path) -> None:
    gap = {
        "reason": "omitted",
        "kind": "repository_ownership",
        "summary": "Protocol file changes remain owned by the protocol repository.",
        "affected_unit_ids": ["foundation"],
    }
    llm = CapturingLLM(_authoring_output())
    llm.outputs = [
        _authoring_output(),
        json.dumps(
            {
                "constraints_complete": False,
                "constraints": [],
                "constraint_gaps": [gap],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
        json.dumps(
            {
                "constraints": [
                    {
                        "id": "protocol-repository-ownership",
                        "kind": gap["kind"],
                        "summary": "A different ownership rule.",
                        "unit_ids": gap["affected_unit_ids"],
                        "disposition": "preserved",
                    }
                ]
            }
        ),
    ]

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(DeliveryPreparationAgent(llm=llm), tmp_path=tmp_path)

    assert exc_info.value.code == "delivery_constraint_repair.omitted_constraint_mismatch"


def test_author_delivery_plan_rejects_source_excerpt_before_constraint_verification(tmp_path: Path) -> None:
    source_rule = "Only the protocol repository may change protocol files."
    authored = json.loads(_authoring_output())
    authored["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "repository_ownership",
            "summary": source_rule,
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
    ]
    llm = CapturingLLM(json.dumps(authored))
    agent = DeliveryPreparationAgent(llm=llm)

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        agent.author_delivery_plan(
            task_description=f"# Task\n\n- {source_rule}\n",
            task_path=".sikula/tasks/team-invites.md",
            plan_id="team-invites",
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
        )

    assert exc_info.value.code == "delivery_authoring.constraint_summary_source_excerpt"
    assert len(llm.prompts) == 1


def test_author_delivery_plan_fails_safely_when_constraint_verifier_provider_fails(tmp_path: Path) -> None:
    llm = CapturingLLM(_authoring_output(), verification_output=RuntimeError("PRIVATE provider failure"))
    agent = DeliveryPreparationAgent(llm=llm)

    with pytest.raises(DeliveryPreparationAgentError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path)

    assert str(exc_info.value) == "Delivery constraint verification assistant failed."
    assert exc_info.value.__cause__ is None
    assert len(llm.prompts) == 2


def test_author_delivery_plan_audits_malformed_constraint_verification(tmp_path: Path) -> None:
    llm = CapturingLLM(
        _authoring_output(),
        verification_output=json.dumps({"constraints_complete": "yes", "constraints": []}),
    )
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert exc_info.value.code == "delivery_constraint_verification.complete_invalid"
    assert audit_records[-1]["phase"] == "delivery_prepare_constraint_verification"
    assert audit_records[-1]["parsed"]["status"] == "failed"
    assert audit_records[-1]["parsed"]["error_code"] == exc_info.value.code


def test_author_delivery_plan_rejects_constraint_verifier_identity_mismatch(tmp_path: Path) -> None:
    authored = json.loads(_authoring_output())
    authored["constraints"] = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Protocol changes remain owned by the protocol repository.",
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
    ]
    llm = CapturingLLM(
        json.dumps(authored),
        verification_output=json.dumps(
            {
                "constraints_complete": True,
                "constraints": [],
                "unit_context_complete": True,
                "unit_context_gaps": [],
            }
        ),
    )
    agent = DeliveryPreparationAgent(llm=llm)

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path)

    assert exc_info.value.code == "delivery_constraint_verification.constraints_mismatch"


def test_constraint_verification_unit_payload_omits_absent_optional_fields() -> None:
    payload = DeliveryPreparationAgent._verification_unit_payload(
        DeliveryAuthoringUnitDraft("unit", "Unit", [], _unit_markdown("Unit"))
    )

    assert "budget" not in payload
    assert "component" not in payload
    assert payload["asset_paths"] == []


def test_author_delivery_amendment_uses_plain_generation_and_records_audit(tmp_path: Path) -> None:
    llm = CapturingLLM(_amendment_output())
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    draft = _author_delivery_amendment(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert draft.plan_id == "team-invites"
    assert draft.target_unit_id == "oversized"
    assert [unit.id for unit in draft.replacement_units] == ["invite-storage", "invite-cli"]
    assert draft.replacement_units[0].asset_paths == [".sikula/task-assets/invite-reference.png"]
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []
    assert llm.agent_calls == []
    prompt = llm.prompts[0]
    assert prompt.startswith(AGENT_SECURITY_PREFIX)
    assert READONLY_AGENT_PREFIX in prompt
    assert "Do not propose edits to existing units" in prompt
    assert "amend_reason must be omitted, null, or a stable code" in prompt
    assert '"amend_reason": null' in prompt
    assert "Verified recovery metadata supplied by deterministic Sikula code:\n```json\nnull" in prompt
    assert (
        "The source plan declares no top-level components. No component IDs are allowed for replacements. "
        "Every replacement unit MUST omit the component field entirely. Do not emit component: null and do "
        "not invent component IDs."
    ) in prompt
    assert '"component": "optional non-empty string"' not in prompt
    assert "optional stable reason" not in prompt
    assert '"id": "oversized"' in prompt
    assert "Split the oversized invite behavior" in prompt
    assert "assign every declared path to at least one" in prompt
    assert "absolute in-project declaration, use its project-relative equivalent" in prompt
    assert "Replacement task_markdown must not include an asset-root section" in prompt
    assert audit_records[0]["phase"] == "delivery_amend_prepare_authoring"
    assert audit_records[0]["parsed"]["replacement_ids"] == ["invite-storage", "invite-cli"]


def test_author_delivery_amendment_verifies_applicable_constraints_without_failure_evidence(
    tmp_path: Path,
) -> None:
    verification_output = json.dumps(
        {
            "constraints_complete": True,
            "constraints": [
                {
                    "id": "protocol-authority",
                    "kind": "authoritative_read_only_dependency",
                    "summary": "Protocol changes remain owned by the protocol repository.",
                    "unit_ids": ["invite-storage", "invite-cli"],
                    "disposition": "preserved",
                }
            ],
        }
    )
    llm = CapturingLLM(_amendment_output(), verification_output=verification_output)
    agent = DeliveryPreparationAgent(llm=llm)
    constraints = [
        {
            "id": "protocol-authority",
            "kind": "authoritative_read_only_dependency",
            "summary": "Protocol changes remain owned by the protocol repository.",
            "unit_ids": ["oversized"],
            "disposition": "preserved",
        }
    ]

    draft = agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id="oversized",
        target_task_description="Split the selected constrained unit.",
        target_unit={"id": "oversized", "depends_on": []},
        downstream_units=[],
        project_root=tmp_path,
        applicable_constraints=constraints,
    )

    assert draft.constraint_verification is not None
    assert draft.constraint_verification.constraints_complete is True
    assert [constraint.unit_ids for constraint in draft.constraint_verification.constraints] == [
        ["invite-storage", "invite-cli"]
    ]
    assert len(llm.prompts) == 2
    assert json.dumps(constraints, indent=2, sort_keys=True) in llm.prompts[0]
    assert "amendment_target_to_replacements" in llm.prompts[1]


def test_author_delivery_amendment_accepts_fenced_output_after_prose(tmp_path: Path) -> None:
    raw_json = _amendment_output()
    llm = CapturingLLM(f"I split the target into two focused units.\n\n```json\n{raw_json}\n```")
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    draft = _author_delivery_amendment(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert [unit.id for unit in draft.replacement_units] == ["invite-storage", "invite-cli"]
    assert audit_records[0]["raw_output"] == llm.output
    assert audit_records[0]["parsed"]["status"] == "parsed"


def test_author_delivery_amendment_parses_external_follow_up_disposition(tmp_path: Path) -> None:
    output = json.dumps(
        {
            "plan_id": "team-invites",
            "target_unit_id": "oversized",
            "disposition": "external_dependency_follow_up_required",
            "summary": "The protocol repository owns the required change.",
            "amend_reason": None,
            "budget_exceeded": None,
            "warnings": [],
            "replacement_units": [],
        }
    )
    agent = DeliveryPreparationAgent(llm=CapturingLLM(output))
    audit_records: list[dict] = []

    draft = _author_delivery_amendment(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert draft.disposition == "external_dependency_follow_up_required"
    assert draft.replacement_units == []
    assert audit_records[0]["parsed"]["disposition"] == "external_dependency_follow_up_required"
    assert audit_records[0]["parsed"]["replacement_count"] == 0


def test_author_delivery_amendment_includes_verified_recovery_metadata(tmp_path: Path) -> None:
    llm = CapturingLLM(_amendment_output())
    agent = DeliveryPreparationAgent(llm=llm)
    budget_exceeded = {"name": "max_planner_steps", "limit": 2, "actual": 5}

    agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id="oversized",
        target_task_description="Split the selected unit.",
        target_unit={"id": "oversized", "depends_on": []},
        downstream_units=[],
        project_root=tmp_path,
        amend_reason="unit_budget_exceeded",
        budget_exceeded=budget_exceeded,
    )

    prompt = llm.prompts[0]
    assert '"amend_reason": "unit_budget_exceeded"' in prompt
    assert '"actual": 5' in prompt
    assert '"limit": 2' in prompt


def test_author_delivery_amendment_includes_only_structured_failure_evidence(tmp_path: Path) -> None:
    llm = CapturingLLM(_amendment_output())
    agent = DeliveryPreparationAgent(llm=llm)
    evidence = {
        "schema_version": 1,
        "plan_id": "team-invites",
        "unit_id": "oversized",
        "child_task_id": "child-1",
        "failure_code": "unit_scope_violation",
        "recommended_action": "delivery_amend_prepare",
        "write_scope": {"declared_paths": ["agents"], "effective_paths": ["agents"]},
        "changed_files": {"count": 1, "paths": ["agents/example.py"], "omitted_paths_count": 0},
    }

    agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id="oversized",
        target_task_description="Split the selected unit.",
        target_unit={"id": "oversized", "depends_on": []},
        downstream_units=[],
        project_root=tmp_path,
        failure_evidence=evidence,
    )

    prompt = llm.prompts[0]
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    assert (
        f"Correlated failed-child boundary evidence supplied by deterministic Sikula code:\n```json\n{rendered}"
        in prompt
    )
    assert "use its inherited constraints, write scope" in prompt
    assert "external_dependency_follow_up_required" in prompt


def test_author_delivery_amendment_renders_component_allowlist_with_exact_ids(tmp_path: Path) -> None:
    component_ids = ("API", 'Web"UI', "mobile-client")
    llm = CapturingLLM(_amendment_output())
    agent = DeliveryPreparationAgent(llm=llm)

    agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id="oversized",
        target_task_description="Split the selected unit.",
        target_unit={"id": "oversized", "depends_on": []},
        downstream_units=[],
        project_root=tmp_path,
        component_ids=component_ids,
    )

    prompt = llm.prompts[0]
    assert (
        "The source plan declares these component IDs as the complete allowlist. Preserve case and spelling exactly:"
    ) in prompt
    assert f"```json\n{json.dumps(list(component_ids), indent=2)}\n```" in prompt
    assert (
        "Replacement units may omit component or set it to exactly one of the listed IDs. "
        "Do not invent, normalize, lowercase, or otherwise alter component IDs."
    ) in prompt
    assert 'Web\\"UI' in prompt
    assert 'web"ui' not in prompt
    assert "The source plan declares no top-level components." not in prompt


def test_author_delivery_amendment_json_escapes_plan_valid_target_id(tmp_path: Path) -> None:
    target_unit_id = 'part"1\\segment\nnext'
    llm = CapturingLLM(_amendment_output(target_unit_id=target_unit_id))
    agent = DeliveryPreparationAgent(llm=llm)

    draft = agent.author_delivery_amendment(
        plan_id="team-invites",
        target_unit_id=target_unit_id,
        target_task_description="Split the selected unit.",
        target_unit={"id": target_unit_id, "depends_on": []},
        downstream_units=[],
        project_root=tmp_path,
    )

    encoded_id = json.dumps(target_unit_id)
    assert draft.target_unit_id == target_unit_id
    assert f"Target unit id: {encoded_id}" in llm.prompts[0]
    assert f'"target_unit_id": {encoded_id}' in llm.prompts[0]


def test_author_delivery_amendment_records_parse_failure(tmp_path: Path) -> None:
    llm = CapturingLLM("Assistant draft:\nSECRET_PROVIDER_OUTPUT")
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_amendment(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert exc_info.value.code == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_amend_prepare_authoring"
    assert record["round_index"] == 1
    assert record["prompt"] == llm.prompts[0]
    assert record["raw_output"] == llm.output
    assert record["parsed"]["status"] == "failed"
    assert record["parsed"]["error_type"] == "DeliveryAuthoringParseError"
    assert record["parsed"]["error_code"] == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in record["parsed"]["error"]


def test_author_delivery_amendment_wraps_provider_failure_without_audit(tmp_path: Path) -> None:
    llm = FailingLLM(RuntimeError("provider timeout with SECRET_PROVIDER_OUTPUT"))
    agent = DeliveryPreparationAgent(llm=llm)

    with pytest.raises(DeliveryPreparationAgentError) as exc_info:
        _author_delivery_amendment(agent, tmp_path=tmp_path)

    assert str(exc_info.value) == "Delivery amendment authoring assistant failed."
    assert exc_info.value.__cause__ is None
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []


def test_author_delivery_plan_handles_absent_context_and_audit_recorder(tmp_path: Path) -> None:
    llm = CapturingLLM(_authoring_output(planning_mode=None))
    agent = DeliveryPreparationAgent(llm=llm)

    draft = _author_delivery_plan(agent, tmp_path=tmp_path)

    assert draft.planning_mode is None
    assert draft.warnings == []
    prompt = llm.prompts[0]
    assert "Project stack: software" in prompt
    assert "- README.md" in prompt
    assert "No configured guidelines content found." in prompt
    assert "Project context:\n```json\n{}" in prompt
    assert "Configured validation commands:\n```json\n[]" in prompt


def test_author_delivery_plan_filters_guidelines_paths_and_truncates_content(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("abcdef")
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={
            "guidelines": {
                "context_files": [None, "", "/absolute.md", "../outside.md", "missing.md", "good.md"],
                "max_file_chars": 3,
            }
        },
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "=== good.md ===\nabc\n... [truncated; inspect good.md for full content]" in prompt
    assert "=== /absolute.md ===" not in prompt
    assert "=== ../outside.md ===" not in prompt
    assert "=== missing.md ===" not in prompt


def test_author_delivery_plan_honors_zero_guidelines_char_limit(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("SECRET_GUIDELINE_CONTEXT")
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={"guidelines": {"context_files": ["good.md"], "max_file_chars": 0}},
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "=== good.md ===\n" in prompt
    assert "SECRET_GUIDELINE_CONTEXT" not in prompt
    assert "[truncated; inspect good.md for full content]" not in prompt


def test_author_delivery_plan_uses_default_guidelines_limit_when_config_value_is_invalid(tmp_path: Path) -> None:
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={"guidelines": {"context_files": ["missing.md"], "max_file_chars": "many"}},
    )

    _author_delivery_plan(agent, tmp_path=tmp_path)

    prompt = llm.prompts[0]
    assert "- missing.md" in prompt
    assert "No configured guidelines content found." in prompt


def test_author_delivery_plan_redacts_outside_project_task_path_in_prompt(tmp_path: Path) -> None:
    outside_task = tmp_path.parent / "secret-task.md"
    llm = CapturingLLM(_authoring_output())
    agent = DeliveryPreparationAgent(llm=llm)

    draft = _author_delivery_plan(agent, tmp_path=tmp_path, task_path=outside_task)

    prompt = llm.prompts[0]
    assert "Source task file: <outside-project>" in prompt
    assert str(outside_task) not in prompt
    assert draft.source_task is None


def test_author_delivery_plan_records_parse_failure_and_raises_safe_error(tmp_path: Path) -> None:
    llm = CapturingLLM("Assistant draft:\nSECRET_PROVIDER_OUTPUT")
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert exc_info.value.code == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert "Add team invite authoring" not in str(exc_info.value)
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["raw_output"] == llm.output
    assert record["parsed"]["status"] == "failed"
    assert record["parsed"]["error_type"] == "DeliveryAuthoringParseError"
    assert record["parsed"]["error_code"] == "delivery_authoring.json_invalid"
    assert "SECRET_PROVIDER_OUTPUT" not in record["parsed"]["error"]


def test_author_delivery_plan_wraps_provider_failure_with_safe_exception(tmp_path: Path) -> None:
    llm = FailingLLM(RuntimeError("provider timeout with SECRET_PROVIDER_OUTPUT"))
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryPreparationAgentError) as exc_info:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert str(exc_info.value) == "Delivery authoring assistant failed."
    assert exc_info.value.__cause__ is None
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "delivery_prepare_authoring"
    assert record["raw_output"] is None
    assert record["parsed"] == {
        "status": "failed",
        "error_type": "RuntimeError",
        "error_code": "delivery_prepare.authoring_failed",
        "error": "provider timeout with SECRET_PROVIDER_OUTPUT",
    }


def test_assess_delivery_mode_uses_platform_neutral_read_only_prompt_and_records_audit(
    tmp_path: Path,
) -> None:
    (tmp_path / "guidelines.md").write_text("# Rules\nKeep orchestration platform-neutral.\n")
    llm = CapturingLLM(_assessment_output())
    agent = DeliveryPreparationAgent(
        llm=llm,
        project_config={
            "project": {"platform": "multi", "language": "mixed"},
            "guidelines": {"context_files": ["guidelines.md"], "max_file_chars": 5000},
        },
    )
    audit_records: list[dict] = []

    draft = _assess_delivery_mode(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert draft.recommended_mode == "delivery_plan"
    assert draft.reason_codes == ["multiple_platforms", "dependency_order_required"]
    assert [unit.id for unit in draft.units] == ["shared", "platform-a", "platform-b"]
    assert draft.units[1].platform == "platform-a"
    assert draft.units[2].depends_on == ["shared"]
    assert llm.system_prompts == [""]
    assert llm.readonly_agent_calls == []
    assert llm.agent_calls == []
    prompt = llm.prompts[0]
    assert prompt.startswith(AGENT_SECURITY_PREFIX)
    assert READONLY_AGENT_PREFIX in prompt
    assert "Use the same decision flow for every project and platform." in prompt
    assert "Treat platform, stack, component, scope, validation, and risk information as project data." in prompt
    assert "Do not classify primarily from task length." in prompt
    assert "Do not return free-form rationale" in prompt
    assert "Project stack: multi / mixed" in prompt
    assert "Source task file: .sikula/tasks/cross-platform-feature.md" in prompt
    assert '"project-test-command"' in prompt
    assert "Implement the same observable feature across two project platforms." in prompt
    assert audit_records == [
        {
            "phase": "delivery_assessment",
            "round_index": 1,
            "prompt": prompt,
            "raw_output": llm.output,
            "parsed": {
                "status": "parsed",
                "recommended_mode": "delivery_plan",
                "reason_codes": ["multiple_platforms", "dependency_order_required"],
                "unit_ids": ["shared", "platform-a", "platform-b"],
                "unit_count": 3,
            },
        }
    ]


def test_assess_delivery_mode_records_parse_failure_without_exposing_output(tmp_path: Path) -> None:
    llm = CapturingLLM('{"recommended_mode":"delivery_plan","raw_output":"SECRET"}')
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        _assess_delivery_mode(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert exc_info.value.code == "delivery_authoring.unknown_field"
    assert "SECRET" not in str(exc_info.value)
    assert audit_records[0]["phase"] == "delivery_assessment"
    assert audit_records[0]["raw_output"] == llm.output
    assert audit_records[0]["parsed"]["status"] == "failed"
    assert audit_records[0]["parsed"]["error_code"] == "delivery_authoring.unknown_field"


def test_assess_delivery_mode_wraps_provider_failure_with_safe_exception(tmp_path: Path) -> None:
    llm = FailingLLM(RuntimeError("provider timeout with SECRET_PROVIDER_OUTPUT"))
    agent = DeliveryPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(DeliveryPreparationAgentError) as exc_info:
        _assess_delivery_mode(agent, tmp_path=tmp_path, audit_records=audit_records)

    assert str(exc_info.value) == "Delivery assessment assistant failed."
    assert exc_info.value.__cause__ is None
    assert "SECRET_PROVIDER_OUTPUT" not in str(exc_info.value)
    assert audit_records[0]["phase"] == "delivery_assessment"
    assert audit_records[0]["raw_output"] is None
    assert audit_records[0]["parsed"]["error_code"] == "delivery_assessment.authoring_failed"
