from __future__ import annotations

import pytest

from core.task_auto_refine import (
    TaskAutoAnswerBatch,
    TaskAutoRefineDraft,
    auto_refine_task_description,
    parse_task_auto_answer_output,
    parse_task_auto_refine_output,
)


def test_parse_task_auto_refine_output_accepts_fenced_json():
    draft = parse_task_auto_refine_output(
        """```json
{
  "task_markdown": "# Add team invites\\n\\n## Goal\\n\\nUsers can invite teammates by email.",
  "input_language": "cs",
  "normalized_to_english": true,
  "warnings": ["scope still needs confirmation"]
}
```"""
    )

    assert draft.task_markdown.startswith("# Add team invites")
    assert draft.task_markdown.endswith("\n")
    assert draft.input_language == "cs"
    assert draft.normalized_to_english is True
    assert draft.warnings == ["scope still needs confirmation"]


def test_parse_task_auto_refine_output_rejects_missing_markdown_or_generated_markers():
    with pytest.raises(ValueError, match="non-empty task_markdown"):
        parse_task_auto_refine_output('{"task_markdown": ""}')

    with pytest.raises(ValueError, match="must not contain Sikula generated markers"):
        parse_task_auto_refine_output('{"task_markdown": "# Task\\n\\n<!-- sikula:generated-open-questions -->"}')


def test_parse_task_auto_answer_output_accepts_active_answers_and_warns_for_inactive_ids():
    batch = parse_task_auto_answer_output(
        """```json
{
  "answers": {
    "scope.boundaries": {"answer": "Add invite creation only.", "notes": "Task scope."},
    "inactive.id": {"answer": "Ignore me."}
  },
  "unanswered": [{"id": "acceptance.criteria", "reason": "Needs human confirmation."}],
  "warnings": ["one answer left open"]
}
```""",
        {"scope.boundaries", "acceptance.criteria"},
    )

    assert batch.answers == {"scope.boundaries": {"answer": "Add invite creation only.", "notes": "Task scope."}}
    assert batch.unanswered == [{"id": "acceptance.criteria", "reason": "Needs human confirmation."}]
    assert "ignored answer for inactive question id: inactive.id" in batch.warnings
    assert "one answer left open" in batch.warnings


def test_parse_task_auto_answer_output_rejects_missing_answers_object():
    with pytest.raises(ValueError, match="answers object"):
        parse_task_auto_answer_output('{"answers": []}', {"scope.boundaries"})


def test_auto_refine_task_description_runs_deterministic_recheck():
    def provider(_request):
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

## Goal

Users should be able to invite teammates by email.

## Scope

- Add invite creation from team settings.

## Acceptance criteria

- A valid email can be invited from team settings.
- Duplicate pending invites show a deterministic error.
- Empty or invalid email addresses are rejected.

## Out of scope

- Billing seat enforcement.
""",
            input_language="en",
            normalized_to_english=False,
        )

    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        normalize_provider=provider,
    )

    assert result.input_language == "en"
    assert result.normalized_to_english is False
    assert result.result.needs_user_input is False
    assert result.result.required_next_step == "prepare_implementation_contract"
    assert "## Open questions" not in result.result.prepared_task_markdown


def test_auto_refine_task_description_can_auto_answer_product_questions():
    def normalize_provider(_request):
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

Users should be able to invite teammates by email.
""",
            input_language="en",
        )

    def answer_provider(request):
        assert request.round_index == 1
        assert {question["id"] for question in request.user_questions} == {
            "scope.boundaries",
            "acceptance.criteria",
            "scope.out_of_scope",
        }
        return TaskAutoAnswerBatch(
            answers={
                "scope.boundaries": {
                    "answer": "Add invite creation from team settings.",
                    "notes": "",
                },
                "acceptance.criteria": {
                    "answer": (
                        "A valid email can be invited from team settings. "
                        "Invalid emails are rejected. Duplicate pending invites show an error."
                    ),
                    "notes": "",
                },
                "scope.out_of_scope": {
                    "answer": "Do not add billing seat enforcement or bulk invites.",
                    "notes": "",
                },
            },
            audit_records=[{"phase": "task_refine_auto_answers", "round_index": 1}],
        )

    audit_records = []
    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        normalize_provider=normalize_provider,
        answer_provider=answer_provider,
        audit_recorder=audit_records.append,
    )

    assert result.rounds == 1
    assert sorted(result.auto_answers) == ["acceptance.criteria", "scope.boundaries", "scope.out_of_scope"]
    assert result.result.needs_user_input is False
    assert result.result.required_next_step == "prepare_implementation_contract"
    assert "Add invite creation from team settings." in result.result.prepared_task_markdown
    assert "## Open questions" not in result.result.prepared_task_markdown
    assert audit_records == [{"phase": "task_refine_auto_answers", "round_index": 1}]


def test_auto_refine_task_description_can_revise_prior_auto_answers():
    def normalize_provider(_request):
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

Users should be able to invite teammates by email.

## Scope

- Add invite creation from team settings.

## Out of scope

- Do not add billing seat enforcement.
"""
        )

    seen_rounds = []

    def answer_provider(request):
        seen_rounds.append(request.round_index)
        if request.round_index == 1:
            return TaskAutoAnswerBatch(
                answers={
                    "acceptance.criteria": {
                        "answer": "It works.",
                        "notes": "",
                    }
                }
            )
        return TaskAutoAnswerBatch(
            answers={
                "acceptance.criteria": {
                    "answer": (
                        "A valid email can be invited from team settings. Empty or invalid email input is rejected. "
                        "Duplicate pending invites show a deterministic error."
                    ),
                    "notes": "",
                }
            }
        )

    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        normalize_provider=normalize_provider,
        answer_provider=answer_provider,
    )

    assert seen_rounds == [1, 2]
    assert result.rounds == 2
    assert result.result.needs_user_input is False
    assert result.auto_answers == {
        "acceptance.criteria": {
            "answer": (
                "A valid email can be invited from team settings. Empty or invalid email input is rejected. "
                "Duplicate pending invites show a deterministic error."
            ),
            "notes": "",
        }
    }
    assert "It works." not in result.result.prepared_task_markdown
    assert "Duplicate pending invites show a deterministic error." in result.result.prepared_task_markdown


def test_auto_refine_task_description_keeps_existing_human_answers_over_auto_answers():
    def normalize_provider(_request):
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

Users should be able to invite teammates by email.
"""
        )

    def answer_provider(_request):
        return TaskAutoAnswerBatch(
            answers={
                "scope.boundaries": {
                    "answer": "LLM replacement scope that should not be used.",
                    "notes": "",
                },
                "acceptance.criteria": {
                    "answer": (
                        "A valid email can be invited from team settings. "
                        "Invalid emails are rejected. Duplicate pending invites show an error."
                    ),
                    "notes": "",
                },
                "scope.out_of_scope": {
                    "answer": "Do not add billing seat enforcement.",
                    "notes": "",
                },
            }
        )

    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        answers={
            "scope.boundaries": "Human scope wins.",
        },
        normalize_provider=normalize_provider,
        answer_provider=answer_provider,
    )

    assert result.auto_answers == {
        "acceptance.criteria": {
            "answer": (
                "A valid email can be invited from team settings. "
                "Invalid emails are rejected. Duplicate pending invites show an error."
            ),
            "notes": "",
        },
        "scope.out_of_scope": {
            "answer": "Do not add billing seat enforcement.",
            "notes": "",
        },
    }
    assert "Human scope wins." in result.result.prepared_task_markdown
    assert "LLM replacement scope" not in result.result.prepared_task_markdown


def test_auto_refine_task_description_keeps_product_questions_open():
    def provider(_request):
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

## Goal

Users should be able to invite teammates by email.
""",
            input_language="en",
        )

    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        normalize_provider=provider,
    )

    assert result.result.needs_user_input is True
    assert {"scope.boundaries", "acceptance.criteria"} <= set(result.result.open_question_ids)
    assert "## Open questions" in result.result.prepared_task_markdown


def test_auto_refine_task_description_records_audit_records():
    events = []

    def provider(_request):
        events.append("provider")
        return TaskAutoRefineDraft(
            task_markdown="""# Add team invites

## Goal

Users should be able to invite teammates by email.
""",
            audit_records=[{"phase": "task_refine_auto", "round_index": 1}],
        )

    def audit_recorder(record):
        events.append(("audit", record["round_index"]))

    result = auto_refine_task_description(
        "invite teammates",
        task_name="team-invites.md",
        normalize_provider=provider,
        audit_recorder=audit_recorder,
    )

    assert events == ["provider", ("audit", 1)]
    assert result.audit_records == [{"phase": "task_refine_auto", "round_index": 1}]
