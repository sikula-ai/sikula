from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from core.delivery_public_metadata import is_safe_delivery_public_metadata


DELIVERY_INTEGRATION_REVIEW_SCHEMA_VERSION = 1
DELIVERY_INTEGRATION_REVIEW_DISPOSITIONS = frozenset(
    {
        "approved",
        "repair_required",
        "scope_amendment_required",
        "external_dependency_gap",
        "human_review_required",
    }
)
MAX_DELIVERY_INTEGRATION_FINDINGS = 20
MAX_DELIVERY_INTEGRATION_SUMMARY_CHARS = 500
MAX_DELIVERY_INTEGRATION_FINDING_UNIT_IDS = 50


class DeliveryIntegrationReviewParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeliveryIntegrationFinding:
    code: str
    summary: str
    unit_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "summary": self.summary, "unit_ids": list(self.unit_ids)}


@dataclass(frozen=True)
class DeliveryIntegrationAssessment:
    disposition: str
    summary: str
    findings: list[DeliveryIntegrationFinding] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.disposition == "approved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DELIVERY_INTEGRATION_REVIEW_SCHEMA_VERSION,
            "disposition": self.disposition,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def parse_delivery_integration_review(
    output: str,
    *,
    known_unit_ids: set[str],
) -> DeliveryIntegrationAssessment:
    if not isinstance(output, str) or not output.strip():
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_output_empty",
            "Integration reviewer returned no output.",
        )
    final_line = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    try:
        payload = json.loads(final_line, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_json_invalid",
            "Integration reviewer must end with one exact JSON object.",
        ) from None
    if not isinstance(payload, dict):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_json_invalid",
            "Integration reviewer disposition must be a JSON object.",
        )
    if set(payload) != {"schema_version", "disposition", "summary", "findings"}:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_keys_invalid",
            "Integration reviewer disposition fields are invalid.",
        )
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != DELIVERY_INTEGRATION_REVIEW_SCHEMA_VERSION
    ):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_schema_unsupported",
            "Integration reviewer disposition schema is unsupported.",
        )
    disposition = payload["disposition"]
    if not isinstance(disposition, str) or disposition not in DELIVERY_INTEGRATION_REVIEW_DISPOSITIONS:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_disposition_invalid",
            "Integration reviewer disposition is unsupported.",
        )
    summary = _bounded_metadata(payload["summary"], "summary")
    raw_findings = payload["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_DELIVERY_INTEGRATION_FINDINGS:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_findings_invalid",
            "Integration reviewer findings must be a bounded list.",
        )
    findings = [
        _parse_finding(value, index=index, known_unit_ids=known_unit_ids) for index, value in enumerate(raw_findings)
    ]
    if disposition == "approved" and findings:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_approval_invalid",
            "Approved integration review cannot contain blocking findings.",
        )
    if disposition != "approved" and not findings:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_findings_required",
            "A non-approved integration review requires at least one finding.",
        )
    return DeliveryIntegrationAssessment(disposition=disposition, summary=summary, findings=findings)


def _parse_finding(
    value: Any,
    *,
    index: int,
    known_unit_ids: set[str],
) -> DeliveryIntegrationFinding:
    if not isinstance(value, dict) or set(value) != {"code", "summary", "unit_ids"}:
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_findings_invalid",
            f"Integration reviewer finding {index + 1} has invalid fields.",
        )
    code = _bounded_metadata(value["code"], "finding code", max_chars=100)
    summary = _bounded_metadata(value["summary"], "finding summary")
    unit_ids = value["unit_ids"]
    if (
        not isinstance(unit_ids, list)
        or len(unit_ids) > MAX_DELIVERY_INTEGRATION_FINDING_UNIT_IDS
        or any(not isinstance(unit_id, str) or unit_id not in known_unit_ids for unit_id in unit_ids)
        or len(set(unit_ids)) != len(unit_ids)
    ):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_findings_invalid",
            f"Integration reviewer finding {index + 1} references invalid units.",
        )
    return DeliveryIntegrationFinding(code=code, summary=summary, unit_ids=unit_ids)


def _bounded_metadata(value: Any, label: str, *, max_chars: int = MAX_DELIVERY_INTEGRATION_SUMMARY_CHARS) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_chars
        or len(value.splitlines()) != 1
        or not is_safe_delivery_public_metadata(value)
    ):
        raise DeliveryIntegrationReviewParseError(
            "delivery_verification.review_metadata_invalid",
            f"Integration reviewer {label} is invalid.",
        )
    return value.strip()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
