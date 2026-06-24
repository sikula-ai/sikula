from __future__ import annotations

from pathlib import Path
from typing import Any

from core import contract_prepare_adapter


def _ready_contract() -> str:
    return """# Team invites

## Scope
- Add invite creation endpoint.
- Add invite acceptance endpoint.
- Add pending invite model.

## Acceptance criteria
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.
- Duplicate pending invite returns a deterministic error.
- Expired invite token cannot be accepted.
- Accepted invite token cannot be reused.

## Security and privacy
- Invite tokens must be unguessable.
- Invite tokens must not be logged.
- Error messages must not reveal whether an email already has an account.

## Out of scope
- Billing seat enforcement.
- Bulk invites.
- Full team settings redesign.

## Tests
- Permission tests for allowed and denied inviter roles.
- Token lifecycle tests for expired and reused tokens.
- Duplicate invite test.

## Validation
- `pytest`
- `ruff check .`

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
"""


def test_prepare_task_description_response_returns_product_questions_without_delivery_fields(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    response = contract_prepare_adapter.prepare_task_description_response(
        "Users should be able to invite teammates by email.",
        task_name="team-invites.md",
    )

    assert response["schema_version"] == 1
    assert response["workflow"] == "prepare_task_description"
    assert response["stage"] == "needs_user_input"
    assert response["needs_user_input"] is True
    assert response["required_next_step"] == "answer_questions"
    assert response["required_user_action"] == "answer_task_description_questions"
    assert response["user_questions"]
    assert "acceptance.criteria" in response["answers_template"]
    assert response["authoritative_output_markdown"] == response["prepared_task_markdown"]
    assert "ready_to_run" not in response
    assert "ready_to_save" not in response
    assert "ready_to_run_blockers" not in response
    assert "check" not in response
    assert "recheck" not in response
    assert not (tmp_path / ".sikula").exists()


def test_prepare_task_description_response_ready_result_points_to_contract_preparation():
    response = contract_prepare_adapter.prepare_task_description_response(
        """# Add team invites

## Goal

Users should be able to invite teammates by email.

## Scope

- Add invite creation from team settings.
- Send the invite to the entered email address.
- Keep existing billing unchanged.

## Acceptance criteria

- A valid email can be invited.
- Duplicate pending invites show a deterministic error.
- Empty emails are rejected.
- Existing members are not invited again.

## Out of scope

- Billing seat enforcement.
- Bulk invites.
""",
        task_name="team-invites.md",
    )

    assert response["workflow"] == "prepare_task_description"
    assert response["needs_user_input"] is False
    assert response["required_next_step"] == "prepare_implementation_contract"
    assert response["primary_user_action"] == "prepare_implementation_contract"
    assert response["user_questions"] == []


def test_prepare_response_returns_questions_without_file_side_effects(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    response = contract_prepare_adapter.prepare_implementation_contract_response(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
    )

    assert response["schema_version"] == 1
    assert response["workflow"] == "prepare_implementation_contract"
    assert response["stage"] == "needs_project_context"
    assert response["needs_user_input"] is True
    assert response["ready_to_run"] is False
    assert "ready_to_run_blockers" in response
    assert "check" in response
    assert response["required_next_step"] == "provide_project_context"
    assert response["user_questions"]
    assert response["ready_to_run_blockers"][0] == "missing_project_context"
    assert response["open_question_ids"] == [question["id"] for question in response["user_questions"]]
    assert "acceptance.criteria" in response["answers_template"]
    assert response["authoritative_output_markdown"].startswith("# Add team invites")
    assert response["resume_arguments"]["status_applies_to_sha256"] == response["status_applies_to_sha256"]
    assert all("sikula run" not in step for step in response["suggested_next_steps"])
    assert not (tmp_path / ".sikula").exists()


def test_prepare_response_ready_contract_includes_safe_run_guidance():
    response = contract_prepare_adapter.prepare_implementation_contract_response(
        _ready_contract(),
        contract_name="../../Team Invites; rm -rf *.md",
        project_context={
            "validation_commands": ["pytest", "ruff check ."],
        },
    )

    assert response["needs_user_input"] is False
    assert response["ready_to_save"] is True
    assert response["ready_to_run"] is True
    assert response["safe_task_path"] == ".sikula/contracts/team-invites-rm-rf.contract.md"
    assert response["suggested_next_steps"] == [
        "Save the prepared contract to `.sikula/contracts/team-invites-rm-rf.contract.md`.",
        "Run `sikula run .sikula/contracts/team-invites-rm-rf.contract.md` from a locally configured Sikula project.",
    ]
    assert response["check"]["source"]["sha256"] == response["status_applies_to_sha256"]


def test_prepare_response_ready_contract_requires_project_context():
    response = contract_prepare_adapter.prepare_implementation_contract_response(
        _ready_contract(),
        contract_name="team-invites.md",
    )

    assert response["stage"] == "needs_project_context"
    assert response["needs_user_input"] is True
    assert response["ready_to_save"] is False
    assert response["ready_to_run"] is False
    assert response["required_next_step"] == "provide_project_context"
    assert response["ready_to_run_blockers"] == ["missing_project_context"]
    assert "validation_commands" in response["suggested_next_steps"][0]


def test_prepare_response_reserved_manifest_points_to_revision_not_questions():
    response = contract_prepare_adapter.prepare_implementation_contract_response(
        """This task was copied from a prepared implementation contract.

# Asset manifest

- Path: `.sikula/task-assets/success-check.svg`
  - Usage: delivery asset.
  - Requested target: `app/assets/success-check.svg`
  - Source/license: provided by product team for this project.

## Scope
- Add the success state icon.

## Acceptance criteria
- The success screen shows the provided success icon.

## Validation
- `pytest`
""",
        contract_name="copied-contract.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert response["required_next_step"] == "revise_contract"
    assert response["required_user_action"] == "revise_contract"
    assert response["needs_user_input"] is False
    assert response["user_questions"] == []
    assert response["answers_template"] == {}
    assert any(gap["id"] == "gap.assets.manifest_reserved" for gap in response["unresolved_gaps"])
    assert "needs user input" not in response["assistant_response_markdown"]
    assert "task description revisions" in response["assistant_response_markdown"]


def test_prepare_response_marks_repeated_answer_questions():
    response = contract_prepare_adapter.prepare_implementation_contract_response(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "ok"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "acceptance.negative_cases" in response["answered_question_ids"]
    assert {question["id"] for question in response["user_questions"]}.issubset(set(response["open_question_ids"]))
    assert "acceptance.negative_cases" in response["revised_answer_question_ids"]
    question = next(
        question for question in response["user_questions"] if question["id"] == "acceptance.negative_cases"
    )
    assert question["requires_revised_answer"] is True
    assert response["answers_template"]["acceptance.negative_cases"]["requires_revised_answer"] is True
    assert response["anti_loop_guidance"]["max_prepare_attempts_without_new_user_input"] == 1


def test_prepare_task_description_response_adapter_only_maps_core_result(monkeypatch):
    class FakeResult:
        def to_dict(self) -> dict[str, Any]:
            return {
                "stage": "custom_stage",
                "needs_user_input": False,
                "required_next_step": "custom_next_step",
                "answers_template": {"q": {"answer": ""}},
                "resume_arguments": {"brief": "x"},
                "authoritative_output_markdown": "x",
                "suggested_next_steps": ["custom step"],
                "user_questions": [{"id": "q"}],
                "primary_user_action": "custom_action",
                "required_user_action": "custom_action",
                "assistant_response_markdown": "custom markdown",
                "answered_question_ids": ["q"],
                "open_question_ids": ["q"],
                "revised_answer_question_ids": ["q"],
                "anti_loop_guidance": {"custom": True},
                "prepared_task_markdown": "x",
                "assumptions": ["a"],
                "non_goals": ["n"],
                "ready_to_run": True,
                "check": {"should": "not leak"},
            }

    def fake_prepare_task_description(*_args, **_kwargs):
        return FakeResult()

    monkeypatch.setattr(contract_prepare_adapter, "prepare_task_description", fake_prepare_task_description)

    response = contract_prepare_adapter.prepare_task_description_response("brief")

    assert response == {
        "schema_version": 1,
        "workflow": "prepare_task_description",
        "stage": "custom_stage",
        "needs_user_input": False,
        "required_next_step": "custom_next_step",
        "answers_template": {"q": {"answer": ""}},
        "resume_arguments": {"brief": "x"},
        "authoritative_output_markdown": "x",
        "suggested_next_steps": ["custom step"],
        "user_questions": [{"id": "q"}],
        "primary_user_action": "custom_action",
        "required_user_action": "custom_action",
        "assistant_response_markdown": "custom markdown",
        "answered_question_ids": ["q"],
        "open_question_ids": ["q"],
        "revised_answer_question_ids": ["q"],
        "anti_loop_guidance": {"custom": True},
        "prepared_task_markdown": "x",
        "assumptions": ["a"],
        "non_goals": ["n"],
    }


def test_prepare_implementation_contract_response_adapter_only_maps_core_result(monkeypatch):
    class FakeResult:
        def to_dict(self) -> dict[str, Any]:
            return {
                "stage": "custom_stage",
                "needs_user_input": False,
                "ready_to_save": False,
                "ready_to_run": False,
                "required_next_step": "custom_next_step",
                "answers_template": {"q": {"answer": ""}},
                "resume_arguments": {"contract_markdown": "x"},
                "authoritative_output_markdown": "x",
                "unresolved_gaps": [{"id": "gap.custom"}],
                "suggested_next_steps": ["custom step"],
                "user_questions": [{"id": "q"}],
                "primary_user_action": "custom_action",
                "required_user_action": "custom_action",
                "assistant_response_markdown": "custom markdown",
                "status_applies_to_sha256": "sha256:abc",
                "safe_task_path": ".sikula/contracts/custom.contract.md",
                "ready_to_run_blockers": ["custom blocker"],
                "answered_question_ids": ["q"],
                "open_question_ids": ["q"],
                "revised_answer_question_ids": ["q"],
                "anti_loop_guidance": {"custom": True},
                "check": {"readiness_score": 1},
                "recheck": None,
            }

    def fake_prepare_implementation_contract(*_args, **_kwargs):
        return FakeResult()

    monkeypatch.setattr(
        contract_prepare_adapter, "prepare_implementation_contract", fake_prepare_implementation_contract
    )

    response = contract_prepare_adapter.prepare_implementation_contract_response("brief")

    assert response == {
        "schema_version": 1,
        "workflow": "prepare_implementation_contract",
        "stage": "custom_stage",
        "needs_user_input": False,
        "ready_to_save": False,
        "ready_to_run": False,
        "required_next_step": "custom_next_step",
        "answers_template": {"q": {"answer": ""}},
        "resume_arguments": {"contract_markdown": "x"},
        "authoritative_output_markdown": "x",
        "unresolved_gaps": [{"id": "gap.custom"}],
        "suggested_next_steps": ["custom step"],
        "user_questions": [{"id": "q"}],
        "primary_user_action": "custom_action",
        "required_user_action": "custom_action",
        "assistant_response_markdown": "custom markdown",
        "status_applies_to_sha256": "sha256:abc",
        "safe_task_path": ".sikula/contracts/custom.contract.md",
        "ready_to_run_blockers": ["custom blocker"],
        "answered_question_ids": ["q"],
        "open_question_ids": ["q"],
        "revised_answer_question_ids": ["q"],
        "anti_loop_guidance": {"custom": True},
        "check": {"readiness_score": 1},
        "recheck": None,
    }
