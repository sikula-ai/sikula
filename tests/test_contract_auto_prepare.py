from __future__ import annotations

import pytest

from core.contract_auto_prepare import (
    ContractAutoAnswerBatch,
    auto_prepare_implementation_contract,
    parse_contract_auto_answer_output,
)


def test_parse_contract_auto_answer_output_accepts_fenced_json_and_filters_unknown_ids():
    batch = parse_contract_auto_answer_output(
        """```json
{
  "answers": {
    "acceptance.criteria": {
      "answer": "Users can invite teammates by valid email.",
      "notes": "Supported by the task title."
    },
    "unknown.question": "Ignore me"
  },
  "unanswered": [
    {"id": "privacy.data_handling", "reason": "Needs human policy."}
  ],
  "warnings": ["partial"]
}
```""",
        {"acceptance.criteria", "privacy.data_handling"},
    )

    assert batch.answers == {
        "acceptance.criteria": {
            "answer": "Users can invite teammates by valid email.",
            "notes": "Supported by the task title.",
        }
    }
    assert batch.unanswered == [{"id": "privacy.data_handling", "reason": "Needs human policy."}]
    assert "ignored answer for inactive question id: unknown.question" in batch.warnings
    assert "partial" in batch.warnings


def test_parse_contract_auto_answer_output_rejects_malformed_or_ambiguous_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_auto_answer_output("{not json", {"acceptance.criteria"})

    with pytest.raises(ValueError, match="multiple JSON objects"):
        parse_contract_auto_answer_output('{"answers": {}} {"answers": {}}', {"acceptance.criteria"})


def test_auto_prepare_implementation_contract_stops_after_no_progress():
    calls = 0

    def provider(_request):
        nonlocal calls
        calls += 1
        return ContractAutoAnswerBatch(answers={"unknown.question": {"answer": "ignored", "notes": ""}})

    result = auto_prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.contract.md",
        project_context={"validation_commands": ["pytest"]},
        answer_provider=provider,
    )

    assert calls == 1
    assert result.rounds == 0
    assert result.auto_answers == {}
    assert result.result.needs_user_input


def test_auto_prepare_implementation_contract_applies_answer_rounds():
    responses = [
        ContractAutoAnswerBatch(answers={"scope.boundaries": {"answer": "Add email invites only.", "notes": ""}}),
        ContractAutoAnswerBatch(
            answers={
                "acceptance.criteria": {
                    "answer": "Admins can invite valid emails and invalid email input is rejected.",
                    "notes": "",
                }
            }
        ),
    ]

    def provider(_request):
        return responses.pop(0)

    result = auto_prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.contract.md",
        project_context={"validation_commands": ["pytest"]},
        answer_provider=provider,
        max_rounds=2,
    )

    assert result.rounds == 2
    assert set(result.auto_answers) == {"scope.boundaries", "acceptance.criteria"}
    assert "Add email invites only." in result.result.prepared_contract_markdown
    assert result.answers["acceptance.criteria"]["answer"].startswith("Admins can invite valid emails")
