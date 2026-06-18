from __future__ import annotations

from pathlib import Path

import pytest

from core.contract_check import prepare_task_description


def test_prepare_task_description_vague_brief_asks_product_questions_without_file_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)

    result = prepare_task_description(
        "Users should be able to invite teammates by email.",
        task_name="team-invites.md",
    )

    data = result.to_dict()
    assert result.stage == "needs_user_input"
    assert result.needs_user_input is True
    assert result.required_next_step == "answer_questions"
    assert result.required_user_action == "answer_task_description_questions"
    assert {question.id for question in result.questions_for_user} >= {
        "scope.boundaries",
        "acceptance.criteria",
    }
    assert result.prepared_task_markdown.startswith("# team invites")
    assert "## Goal" in result.prepared_task_markdown
    assert "## Open questions" in result.prepared_task_markdown
    assert "sikula:generated-" not in result.prepared_task_markdown
    assert "ready_to_run" not in data
    assert "check" not in data
    assert not (tmp_path / ".sikula").exists()


def test_prepare_task_description_specific_brief_is_ready_for_contract_preparation():
    task = """# Add team invites

## Goal

Users should be able to invite teammates by email.

## Scope

- Add invite creation from the team settings screen.
- Send the invite to the entered email address.
- Keep existing team member permissions unchanged.

## Acceptance criteria

- A valid email can be invited from team settings.
- Duplicate pending invites show a deterministic error.
- Empty or invalid email addresses are rejected.
- Existing team members are not invited again.

## Out of scope

- Billing seat enforcement.
- Bulk invites.
"""

    result = prepare_task_description(task, task_name="team-invites.md")

    assert result.stage == "ready"
    assert result.needs_user_input is False
    assert result.required_next_step == "prepare_implementation_contract"
    assert result.suggested_next_steps == [
        "Use the returned task description as input to prepare_implementation_contract with project context."
    ]
    assert result.questions_for_user == []
    assert result.prepared_task_markdown == task.rstrip() + "\n"
    assert "ready_to_run" not in result.to_dict()


def test_prepare_task_description_applies_answers_and_rechecks_open_questions():
    first = prepare_task_description(
        "Users should be able to invite teammates by email.",
        task_name="team-invites.md",
    )

    second = prepare_task_description(
        first.resume_arguments["brief"],
        task_name="team-invites.md",
        answers={
            "scope.boundaries": "Add invite creation from team settings. Keep billing unchanged.",
            "acceptance.criteria": (
                "A valid email can be invited. Duplicate invites show an error. Empty emails are rejected."
            ),
            "scope.out_of_scope": "Billing, roles, and bulk invites are out of scope.",
        },
    )

    assert "- Add invite creation from team settings. Keep billing unchanged." in second.prepared_task_markdown
    assert "- A valid email can be invited. Duplicate invites show an error. Empty emails are rejected." in (
        second.prepared_task_markdown
    )
    assert "sikula:generated-answer" not in second.prepared_task_markdown
    assert "scope.boundaries" in second.answered_question_ids
    assert "scope.boundaries" not in second.revised_answer_question_ids
    assert "acceptance.criteria" not in second.revised_answer_question_ids
    assert second.questions_for_user == []
    assert second.required_next_step == "prepare_implementation_contract"


def test_prepare_task_description_preserves_product_context_without_validation_readiness():
    result = prepare_task_description(
        "Add country detail screen for Android and iOS examples.",
        task_name="country-detail.md",
        product_context={
            "audience": "Mobile users browsing countries",
            "product_area": "Countries examples",
            "known_constraints": "Keep the existing countries list behaviour unchanged.",
        },
    )

    data = result.to_dict()
    assert "Android and iOS examples" in result.prepared_task_markdown
    assert "## Product context" in result.prepared_task_markdown
    assert "- Audience: Mobile users browsing countries" in result.prepared_task_markdown
    assert result.resume_arguments["product_context"] == {
        "audience": "Mobile users browsing countries",
        "product_area": "Countries examples",
        "known_constraints": "Keep the existing countries list behaviour unchanged.",
    }
    assert "validation" not in data
    assert "ready_to_run" not in data


def test_prepare_task_description_revises_previous_generated_answers():
    first = prepare_task_description(
        "Users should be able to invite teammates by email.",
        task_name="team-invites.md",
        answers={"acceptance.criteria": "ok"},
    )

    assert "acceptance.criteria" in first.revised_answer_question_ids
    assert "- ok" not in first.prepared_task_markdown

    second = prepare_task_description(
        first.resume_arguments["brief"],
        task_name="team-invites.md",
        answers={
            "acceptance.criteria": (
                "Valid emails can be invited. Duplicate pending invites show a deterministic error. "
                "Empty emails are rejected."
            )
        },
    )

    assert "- ok" not in second.prepared_task_markdown
    assert (
        "- Valid emails can be invited. Duplicate pending invites show a deterministic error. Empty emails are rejected."
        in (second.prepared_task_markdown)
    )
