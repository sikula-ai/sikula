"""Tests for schema-aware structured LLM output parsing."""

from __future__ import annotations

import json

import pytest

from core.structured_output import (
    DELIVERY_DISPOSITION_APPROVED,
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
    DELIVERY_DISPOSITION_FIX_IN_SCOPE,
    DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT,
    DELIVERY_IMPLEMENTATION_DISPOSITIONS,
    DELIVERY_REVIEW_DISPOSITIONS,
    DeliveryDispositionParseError,
    parse_delivery_disposition,
)


def _payload(disposition: str, summary: object = "A bounded follow-up is required.") -> str:
    return json.dumps(
        {
            "sikula_disposition_schema_version": 1,
            "disposition": disposition,
            "summary": summary,
        }
    )


@pytest.mark.parametrize(
    ("disposition", "recommended_action"),
    [
        (DELIVERY_DISPOSITION_APPROVED, "continue"),
        (DELIVERY_DISPOSITION_FIX_IN_SCOPE, "bounded_fix"),
        (DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT, "delivery_amend_prepare"),
        (DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP, "external_dependency_follow_up"),
    ],
)
def test_parse_delivery_disposition_accepts_supported_review_values(
    disposition: str,
    recommended_action: str,
) -> None:
    parsed = parse_delivery_disposition(
        f"Recovery decision:\n{_payload(disposition)}",
        allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS,
    )

    assert parsed is not None
    assert parsed.to_dict() == {
        "schema_version": 1,
        "disposition": disposition,
        "summary": "A bounded follow-up is required.",
        "recommended_action": recommended_action,
    }


def test_parse_delivery_disposition_ignores_free_form_keywords() -> None:
    assert (
        parse_delivery_disposition(
            "This sounds like an external_dependency_gap.",
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )
        is None
    )


def test_parse_delivery_disposition_rejects_approval_from_implementer() -> None:
    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(
            _payload(DELIVERY_DISPOSITION_APPROVED),
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )

    assert exc_info.value.code == "delivery_disposition.value_invalid"


def test_parse_delivery_disposition_requires_json_as_final_non_empty_line() -> None:
    output = _payload(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP) + "\nAdditional prose"

    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.position_invalid"


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_parse_delivery_disposition_accepts_terminal_fenced_json(fence: str) -> None:
    output = (
        "Security checks: no blocking issue found.\n\n"
        f"{fence}json\n"
        f"{_payload(DELIVERY_DISPOSITION_APPROVED, 'No blocking security issues found.')}\n"
        f"{fence}\n"
    )

    parsed = parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)

    assert parsed is not None
    assert parsed.disposition == DELIVERY_DISPOSITION_APPROVED
    assert parsed.summary == "No blocking security issues found."


def test_parse_delivery_disposition_accepts_pretty_terminal_fenced_json() -> None:
    payload = json.dumps(
        {
            "sikula_disposition_schema_version": 1,
            "disposition": DELIVERY_DISPOSITION_FIX_IN_SCOPE,
            "summary": "One bounded correction is required.",
        },
        indent=2,
    )

    parsed = parse_delivery_disposition(
        f"Review findings follow.\n\n```JSON\n{payload}\n```",
        allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS,
    )

    assert parsed is not None
    assert parsed.disposition == DELIVERY_DISPOSITION_FIX_IN_SCOPE


def test_parse_delivery_disposition_rejects_prose_after_fenced_json() -> None:
    output = f"```json\n{_payload(DELIVERY_DISPOSITION_APPROVED)}\n```\nAPPROVED"

    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.position_invalid"


def test_parse_delivery_disposition_rejects_prose_inside_fenced_json() -> None:
    output = f"```json\nDecision: {_payload(DELIVERY_DISPOSITION_APPROVED)}\n```"

    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.json_invalid"


@pytest.mark.parametrize(
    "output",
    [
        f"Decision: {_payload(DELIVERY_DISPOSITION_FIX_IN_SCOPE)}",
        f"{_payload(DELIVERY_DISPOSITION_FIX_IN_SCOPE)} trailing verdict",
    ],
)
def test_parse_delivery_disposition_rejects_prose_surrounding_final_json(output: str) -> None:
    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.json_invalid"


@pytest.mark.parametrize(
    "output",
    [
        "{'sikula_disposition_schema_version': 1, 'disposition': 'external_dependency_gap', 'summary': 'x'}",
        "{sikula_disposition_schema_version: 1, disposition: external_dependency_gap, summary: x}",
    ],
)
def test_parse_delivery_disposition_rejects_malformed_schema_key_advertisements(output: str) -> None:
    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.json_invalid"


@pytest.mark.parametrize(
    "output",
    [
        '{"wrapper":{"sikula_disposition_schema_version":1,"disposition":"fix_in_scope","summary":"x"}}',
        f"{_payload(DELIVERY_DISPOSITION_FIX_IN_SCOPE)}\n{_payload(DELIVERY_DISPOSITION_FIX_IN_SCOPE)}",
        '{"sikula_disposition_schema_version":1,"sikula_disposition_schema_version":1,'
        '"disposition":"fix_in_scope","summary":"x"}',
        '{"sikula_disposition_schema_version":1,"disposition":"fix_in_scope"}',
        '{"sikula_disposition_schema_version":1,"disposition":"fix_in_scope","summary":"x","unexpected":"field"}',
    ],
)
def test_parse_delivery_disposition_rejects_nested_duplicate_partial_or_extra_structures(output: str) -> None:
    with pytest.raises(DeliveryDispositionParseError):
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)


@pytest.mark.parametrize("schema_version", [True, 0, 2, "1"])
def test_parse_delivery_disposition_rejects_unsupported_schema(schema_version: object) -> None:
    output = json.dumps(
        {
            "sikula_disposition_schema_version": schema_version,
            "disposition": DELIVERY_DISPOSITION_FIX_IN_SCOPE,
            "summary": "x",
        }
    )

    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(output, allowed_dispositions=DELIVERY_REVIEW_DISPOSITIONS)

    assert exc_info.value.code == "delivery_disposition.schema_unsupported"


def test_parse_delivery_disposition_enforces_agent_specific_values() -> None:
    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(
            _payload(DELIVERY_DISPOSITION_FIX_IN_SCOPE),
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )

    assert exc_info.value.code == "delivery_disposition.value_invalid"


@pytest.mark.parametrize("summary", [None, 1, "", " ", "x" * 501])
def test_parse_delivery_disposition_rejects_invalid_summary(summary: object) -> None:
    with pytest.raises(DeliveryDispositionParseError) as exc_info:
        parse_delivery_disposition(
            _payload(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP, summary),
            allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
        )

    assert exc_info.value.code == "delivery_disposition.summary_invalid"


def test_parse_delivery_disposition_sanitizes_unsafe_summary() -> None:
    parsed = parse_delivery_disposition(
        _payload(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP, "Inspect /home/operator/private.txt"),
        allowed_dispositions=DELIVERY_IMPLEMENTATION_DISPOSITIONS,
    )

    assert parsed is not None
    assert parsed.summary == "<redacted>"
