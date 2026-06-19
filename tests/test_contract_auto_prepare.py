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


def test_auto_prepare_implementation_contract_preserves_supplied_answers_for_revised_questions():
    def provider(request):
        assert "acceptance.criteria" in {question["id"] for question in request.user_questions}
        return ContractAutoAnswerBatch(
            answers={
                "acceptance.criteria": {
                    "answer": "Auto-generated acceptance criteria should not replace human text.",
                    "notes": "Auto-generated notes should not replace human notes.",
                },
                "scope.boundaries": {
                    "answer": "Add email invites only.",
                    "notes": "Supported by the task.",
                },
            }
        )

    result = auto_prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.contract.md",
        project_context={"validation_commands": ["pytest"]},
        initial_answers={
            "acceptance.criteria": {
                "answer": "Human-filled acceptance criteria should stay.",
                "notes": "Human-filled notes should stay.",
            }
        },
        answer_provider=provider,
        max_rounds=1,
    )

    assert result.rounds == 1
    assert result.auto_answers == {
        "scope.boundaries": {
            "answer": "Add email invites only.",
            "notes": "Supported by the task.",
        }
    }
    assert result.answers["acceptance.criteria"] == {
        "answer": "Human-filled acceptance criteria should stay.",
        "notes": "Human-filled notes should stay.",
    }
    assert "Auto-generated acceptance criteria" not in result.result.prepared_contract_markdown
    assert "Human-filled acceptance criteria should stay." in result.result.prepared_contract_markdown


def test_auto_prepare_implementation_contract_records_audit_records():
    events = []

    def provider(request):
        events.append(("provider", request.round_index))
        return ContractAutoAnswerBatch(
            answers={"scope.boundaries": {"answer": "Add email invites only.", "notes": ""}},
            audit_records=[{"phase": "contract_prepare_auto", "round_index": request.round_index}],
        )

    def audit_recorder(record):
        events.append(("audit", record["round_index"]))

    result = auto_prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.contract.md",
        project_context={"validation_commands": ["pytest"]},
        answer_provider=provider,
        audit_recorder=audit_recorder,
        max_rounds=1,
    )

    assert events == [("provider", 1), ("audit", 1)]
    assert result.audit_records == [{"phase": "contract_prepare_auto", "round_index": 1}]
    assert result.rounds == 1
