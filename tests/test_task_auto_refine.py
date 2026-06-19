from __future__ import annotations

import pytest

from core.task_auto_refine import (
    TaskAutoRefineDraft,
    auto_refine_task_description,
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
