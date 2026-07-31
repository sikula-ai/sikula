from __future__ import annotations

import pytest

from core.contract_auto_prepare import (
    ContractAutoAnswerBatch,
    auto_prepare_implementation_contract,
    load_auto_json_object,
    parse_contract_auto_answer_output,
)


@pytest.mark.parametrize(
    "output",
    [
        '{"value": {"nested": true}}',
        'Here is the response: {"value": {"nested": true}}',
        'Here is the response:\n{"value": {"nested": true}}\nDone.',
        'Here is the response:\n    {"value": {"nested": true}}\nDone.',
        '```json\n{"value": {"nested": true}}\n```',
        'Here is the response:\n```json\n{"value": {"nested": true}}\n```\nDone.',
        'Here is the response:\n```JSON\n{"value": {"nested": true}}\n```',
        'Here is the response:\n```json title="response"\n{"value": {"nested": true}}\n```',
        'Here is the response:\n```\n{"value": {"nested": true}}\n```',
        'Here is the response:\n~~~json\n{"value": {"nested": true}}\n~~~',
        'Here is the response:\r\n```json\r\n{"value": {"nested": true}}\r\n```\r\nDone.',
        'The source uses `config { "mode" to currentMode }`.\n{"value": {"nested": true}}',
        'The source uses ``config { "mode" to currentMode }``.\n{"value": {"nested": true}}',
        ('```kotlin\nconfig { "mode" to currentMode }\n```\n```json\n{"value": {"nested": true}}\n```'),
        ('```javascript\nconst fixture = {"fixture": "source"};\n```\n{"value": {"nested": true}}'),
        ('```json\n{"fixture": "source"}\n```\n{"value": {"nested": true}}'),
        ('```\nrefresh()\n```\n{"value": {"nested": true}}\n```\nif (stale) { refresh() }\n```'),
        ('~~~javascript\nconst fixture = {"fixture": "source"};\n~~~~\n{"value": {"nested": true}}'),
        ('```javascript\nfunction refresh() {\n    ```\n\t```\n}\n```\n{"value": {"nested": true}}'),
        ('```\n{ result -> render(result) }\n```\n{"value": {"nested": true}}'),
        ('    config { "mode": currentMode }\n{"value": {"nested": true}}'),
        ('Here is the response:\n```json\n{"value": {"nested": true}}'),
        ('```json\n{"value": {"nested": true}}\ntrailing prose\n```'),
        ('Here is the response:\n~~~json\n{"value": {"nested": true}}'),
        ('The parser searches for `[` before decoding.\n{"value": {"nested": true}}'),
        ("[" * 1_000 + '\n{"value": {"nested": true}}'),
    ],
    ids=[
        "raw",
        "same-line-prose",
        "raw-after-prose",
        "indented-raw-after-prose",
        "whole-json-fence",
        "json-fence-after-prose",
        "case-insensitive-json-fence",
        "json-fence-with-info-attributes",
        "unlabelled-fence-after-prose",
        "tilde-json-fence-after-prose",
        "crlf-json-fence-after-prose",
        "inline-source-code",
        "double-backtick-source-code",
        "source-fence-before-response-fence",
        "source-fence-before-raw-response",
        "schema-distinguishes-json-source-fixture",
        "unlabelled-source-fences-around-raw-response",
        "tilde-source-fence-before-raw-response",
        "indented-markers-inside-source-fence",
        "brace-led-callback-before-raw-response",
        "indented-source-block-before-raw-response",
        "unclosed-json-fence",
        "trailing-prose-inside-fence",
        "unclosed-tilde-fence",
        "unmatched-preamble-bracket",
        "many-unmatched-preamble-brackets",
    ],
)
def test_load_auto_json_object_accepts_supported_response_formats(output: str):
    assert load_auto_json_object(output, required_keys=frozenset({"value"})) == {"value": {"nested": True}}


@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("", "empty"),
        ("No structured response.", "did not contain"),
        ('{"fixture": true}', "required JSON object"),
        ('{"wrapper": {"value": true}}', "required JSON object"),
        ('[{"fixture": true}, {"value": true}]', "required JSON object"),
        ("{not json", "not valid JSON"),
        ('{"wrapper": {"value": true}', "not valid JSON"),
        ('{"value": invalid} {"value": true}', "not valid JSON"),
        ('{"value": true} {"value": false}', "multiple JSON objects"),
        (
            '```json\n{"value": true}\n```\n```json\n{"value": false}\n```',
            "multiple JSON objects",
        ),
        ('```json\n{"value": true} {"value": false}\n```', "multiple JSON objects"),
        (
            '{"value": true}\n```json\n{"value": false}\n```',
            "multiple JSON objects",
        ),
        ('Here is the response:\n```json\n{"value": invalid}\n```', "not valid JSON"),
        ('```json\n["not", "an", "object"]\n```', "required JSON object"),
        ('Broken `example {"value": invalid}\n{"value": true}', "not valid JSON"),
        ('Broken `example {"value": invalid}``\n{"value": true}', "not valid JSON"),
    ],
    ids=[
        "empty",
        "missing-object",
        "object-without-required-key",
        "nested-object-with-required-key",
        "array-with-required-object",
        "malformed",
        "malformed-parent-with-valid-child",
        "balanced-malformed-before-valid",
        "multiple-raw-objects",
        "multiple-response-fences",
        "multiple-objects-in-response-fence",
        "raw-and-fenced-responses",
        "malformed-response-fence",
        "non-object-response-fence",
        "unclosed-inline-code",
        "mismatched-inline-code-runs",
    ],
)
def test_load_auto_json_object_rejects_invalid_or_ambiguous_formats(output: str, error: str):
    with pytest.raises(ValueError, match=error):
        load_auto_json_object(output, required_keys=frozenset({"value"}))


def test_load_auto_json_object_rejects_unbalanced_object_remainder():
    with pytest.raises(ValueError, match="not valid JSON"):
        load_auto_json_object("{" * 1_000, required_keys=frozenset({"value"}))


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


def test_parse_contract_auto_answer_output_accepts_json_after_same_line_prose():
    batch = parse_contract_auto_answer_output(
        'Here is the response: {"answers": {"reviewer.focus": {"answer": "Check refresh."}}}',
        {"reviewer.focus"},
    )

    assert batch.answers == {"reviewer.focus": {"answer": "Check refresh.", "notes": ""}}


def test_parse_contract_auto_answer_output_ignores_generic_preamble_object():
    batch = parse_contract_auto_answer_output(
        '{"warnings": ["source diagnostic"]}\n{"answers": {"reviewer.focus": {"answer": "Check refresh."}}}',
        {"reviewer.focus"},
    )

    assert batch.answers == {"reviewer.focus": {"answer": "Check refresh.", "notes": ""}}


def test_parse_contract_auto_answer_output_rejects_malformed_or_ambiguous_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_auto_answer_output("{not json", {"acceptance.criteria"})

    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_auto_answer_output(
            '{"wrapper": {"answers": {}}',
            {"acceptance.criteria"},
        )

    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_auto_answer_output(
            '{"answers": invalid} {"answers": {}}',
            {"acceptance.criteria"},
        )

    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_auto_answer_output(
            'Broken `example {"answers": invalid}\n{"answers": {}}',
            {"acceptance.criteria"},
        )

    with pytest.raises(ValueError, match="multiple JSON objects"):
        parse_contract_auto_answer_output('{"answers": {}} {"answers": {}}', {"acceptance.criteria"})


def test_parse_contract_auto_answer_output_accepts_warning_only_no_progress():
    batch = parse_contract_auto_answer_output('{"warnings": ["no answer envelope"]}', {"acceptance.criteria"})

    assert batch.answers == {}
    assert batch.warnings == ["no answer envelope"]


def test_parse_contract_auto_answer_output_skips_source_brace_before_json():
    batch = parse_contract_auto_answer_output(
        """I have enough grounding. The `error?.let { ... return }` early return explains the behavior.

{
  "answers": {
    "reviewer.focus": {
      "answer": "Verify that refresh remains available while an error is shown.",
      "notes": "Supported by the current screen behavior."
    }
  },
  "unanswered": [],
  "warnings": []
}
""",
        {"reviewer.focus"},
    )

    assert batch.answers == {
        "reviewer.focus": {
            "answer": "Verify that refresh remains available while an error is shown.",
            "notes": "Supported by the current screen behavior.",
        }
    }


def test_parse_contract_auto_answer_output_skips_json_like_source_brace_before_json():
    batch = parse_contract_auto_answer_output(
        """The Kotlin mapping uses `config { "mode" to currentMode }` in the existing implementation.

{"answers": {}, "unanswered": [], "warnings": []}
""",
        {"reviewer.focus"},
    )

    assert batch.answers == {}
    assert batch.unanswered == []


def test_parse_contract_auto_answer_output_skips_fenced_source_brace_before_json():
    batch = parse_contract_auto_answer_output(
        """The existing implementation uses this mapping:
```kotlin
config { "mode" to currentMode }
```

The fixture is also relevant:
```javascript
const fixture = {"fixture": {"example": "not the response"}};
```

{"answers": {}, "unanswered": [], "warnings": []}
""",
        {"reviewer.focus"},
    )

    assert batch.answers == {}
    assert batch.unanswered == []


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
