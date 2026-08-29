"""Schema-aware extraction for structured LLM output."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from core.delivery_public_metadata import sanitize_delivery_public_metadata


_JSON_OBJECT_KEY_RE = re.compile(r'(?P<key>"(?:\\.|[^"\\])*")[ \t\r\n]*:')
_DELIVERY_DISPOSITION_ADVERTISEMENT_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:"sikula_disposition_schema_version"|'
    r"'sikula_disposition_schema_version'|sikula_disposition_schema_version)[ \t\r\n]*:"
)

DELIVERY_DISPOSITION_SCHEMA_VERSION = 1
DELIVERY_DISPOSITION_APPROVED = "approved"
DELIVERY_DISPOSITION_FIX_IN_SCOPE = "fix_in_scope"
DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT = "requires_scope_amendment"
DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP = "external_dependency_gap"
DELIVERY_REVIEW_ACTION_DISPOSITIONS = frozenset(
    {
        DELIVERY_DISPOSITION_FIX_IN_SCOPE,
        DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT,
        DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
    }
)
DELIVERY_REVIEW_DISPOSITIONS = DELIVERY_REVIEW_ACTION_DISPOSITIONS | {DELIVERY_DISPOSITION_APPROVED}
DELIVERY_IMPLEMENTATION_DISPOSITIONS = frozenset({DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP})
MAX_DELIVERY_DISPOSITION_SUMMARY_CHARS = 500

_DELIVERY_DISPOSITION_KEYS = frozenset(
    {
        "sikula_disposition_schema_version",
        "disposition",
        "summary",
    }
)
_DELIVERY_DISPOSITION_ACTIONS = {
    DELIVERY_DISPOSITION_APPROVED: "continue",
    DELIVERY_DISPOSITION_FIX_IN_SCOPE: "bounded_fix",
    DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT: "delivery_amend_prepare",
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP: "external_dependency_follow_up",
}


def delivery_disposition_recommended_action(disposition: object) -> str | None:
    """Return the stable recovery action for a recognized disposition."""
    return _DELIVERY_DISPOSITION_ACTIONS.get(disposition) if isinstance(disposition, str) else None


class DeliveryDispositionParseError(ValueError):
    """Raised when an advertised delivery disposition is not safe to consume."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DeliveryDisposition:
    schema_version: int
    disposition: str
    summary: str
    recommended_action: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "disposition": self.disposition,
            "summary": self.summary,
            "recommended_action": self.recommended_action,
        }


@dataclass
class _JsonContainer:
    opener: str
    start: int
    parent: int | None
    end: int | None = None


def load_schema_json_object(
    output: str,
    *,
    required_keys: frozenset[str],
    fallback_keys: frozenset[str] = frozenset(),
    object_pairs_hook: Callable[[list[tuple[str, Any]]], Any] | None = None,
    parse_constant: Callable[[str], Any] | None = None,
    output_name: str = "structured LLM output",
) -> dict[str, Any]:
    """Return one unambiguous top-level object matching the expected schema."""

    text = output.strip()
    if not text:
        raise ValueError(f"{output_name} is empty")
    if not required_keys:
        raise ValueError(f"{output_name} schema must declare required keys")

    decoder = json.JSONDecoder(object_pairs_hook=object_pairs_hook, parse_constant=parse_constant)
    matches: list[dict[str, Any]] = []
    fallback_matches: list[dict[str, Any]] = []
    malformed_fallback = False
    decoded_object = False
    containers = _json_containers(text)
    matched_ancestor: list[bool] = []
    unmatched_json_ancestor: list[bool] = []
    for container in containers:
        parent = container.parent
        matched_ancestor.append(parent is not None and (containers[parent].end is not None or matched_ancestor[parent]))
        unmatched_json_ancestor.append(
            parent is not None
            and (
                (containers[parent].end is None and _looks_like_json_container(text, containers[parent]))
                or unmatched_json_ancestor[parent]
            )
        )

    for index, container in enumerate(containers):
        if matched_ancestor[index] or unmatched_json_ancestor[index]:
            continue
        if container.end is None:
            if _looks_like_json_container(text, container):
                remainder = text[container.start :]
                key_markers = _json_object_key_markers(remainder)
                if required_keys.issubset(key_markers):
                    raise ValueError(f"{output_name} is not valid JSON") from None
                if fallback_keys.intersection(key_markers):
                    malformed_fallback = True
            continue
        candidate = text[container.start : container.end]
        try:
            payload, end = decoder.raw_decode(candidate)
        except (json.JSONDecodeError, ValueError, RecursionError):
            key_markers = _json_object_key_markers(candidate)
            if required_keys.issubset(key_markers):
                raise ValueError(f"{output_name} is not valid JSON") from None
            if fallback_keys.intersection(key_markers):
                malformed_fallback = True
            continue
        if end != len(candidate) or not isinstance(payload, dict):
            continue
        decoded_object = True
        if required_keys.issubset(payload):
            matches.append(payload)
        elif fallback_keys.intersection(payload):
            fallback_matches.append(payload)

    if not matches:
        if malformed_fallback:
            raise ValueError(f"{output_name} is not valid JSON")
        matches = fallback_matches
    if not matches:
        if text.startswith("{") and not decoded_object:
            raise ValueError(f"{output_name} is not valid JSON")
        raise ValueError(f"{output_name} did not contain the required JSON object")
    if len(matches) > 1:
        raise ValueError(f"{output_name} contains multiple JSON objects")
    return matches[0]


def parse_delivery_disposition(
    output: str,
    *,
    allowed_dispositions: frozenset[str],
) -> DeliveryDisposition | None:
    """Parse one flat, explicitly advertised delivery disposition.

    Free-form output without the schema marker is not a disposition. Once the marker
    appears, malformed, nested, duplicated, conflicting, or unsupported data is an
    error rather than an invitation to infer intent from prose.
    """

    if not isinstance(output, str):
        return None
    marker_count = len(_DELIVERY_DISPOSITION_ADVERTISEMENT_RE.findall(output))
    if marker_count == 0:
        return None
    if marker_count != 1:
        raise DeliveryDispositionParseError(
            "delivery_disposition.marker_ambiguous",
            "Delivery disposition must advertise exactly one schema marker.",
        )
    if not allowed_dispositions or not allowed_dispositions.issubset(DELIVERY_REVIEW_DISPOSITIONS):
        raise ValueError("allowed delivery dispositions must be a non-empty supported set")

    json_text = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    if not _DELIVERY_DISPOSITION_ADVERTISEMENT_RE.search(json_text):
        raise DeliveryDispositionParseError(
            "delivery_disposition.position_invalid",
            "Delivery disposition JSON must be the final non-empty output line.",
        )

    try:
        payload = json.loads(
            json_text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise DeliveryDispositionParseError(
            "delivery_disposition.json_invalid",
            "Delivery disposition must be one unambiguous flat JSON object with no surrounding text.",
        ) from exc

    if not isinstance(payload, dict):
        raise DeliveryDispositionParseError(
            "delivery_disposition.json_invalid",
            "Delivery disposition must be one unambiguous flat JSON object with no surrounding text.",
        )

    if frozenset(payload) != _DELIVERY_DISPOSITION_KEYS:
        raise DeliveryDispositionParseError(
            "delivery_disposition.keys_invalid",
            "Delivery disposition contains missing or unsupported fields.",
        )

    schema_version = payload.get("sikula_disposition_schema_version")
    if type(schema_version) is not int or schema_version != DELIVERY_DISPOSITION_SCHEMA_VERSION:
        raise DeliveryDispositionParseError(
            "delivery_disposition.schema_unsupported",
            "Delivery disposition uses an unsupported schema version.",
        )

    disposition = payload.get("disposition")
    if not isinstance(disposition, str) or disposition not in allowed_dispositions:
        raise DeliveryDispositionParseError(
            "delivery_disposition.value_invalid",
            "Delivery disposition is not supported for this agent output.",
        )
    recommended_action = delivery_disposition_recommended_action(disposition)
    if recommended_action is None:  # Guarded by the supported allowlist check above.
        raise AssertionError("supported delivery disposition has no recovery action")

    summary = payload.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary.strip()) > MAX_DELIVERY_DISPOSITION_SUMMARY_CHARS
    ):
        raise DeliveryDispositionParseError(
            "delivery_disposition.summary_invalid",
            "Delivery disposition summary must be a non-empty bounded string.",
        )
    sanitized_summary = sanitize_delivery_public_metadata(summary.strip()) or "<redacted>"

    return DeliveryDisposition(
        schema_version=DELIVERY_DISPOSITION_SCHEMA_VERSION,
        disposition=disposition,
        summary=sanitized_summary,
        recommended_action=recommended_action,
    )


def _object_pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_object_key_markers(candidate: str) -> frozenset[str]:
    keys: set[str] = set()
    for match in _JSON_OBJECT_KEY_RE.finditer(candidate):
        try:
            key = json.loads(match.group("key"))
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
        if isinstance(key, str):
            keys.add(key)
    return frozenset(keys)


def _looks_like_json_object(text: str, start: int) -> bool:
    position = start + 1
    while position < len(text) and text[position].isspace():
        position += 1
    return position < len(text) and text[position] in {'"', "}"}


def _looks_like_json_array(text: str, start: int) -> bool:
    position = start + 1
    while position < len(text) and text[position].isspace():
        position += 1
    return position < len(text) and text[position] in '[{"-0123456789tfn'


def _looks_like_json_container(text: str, container: _JsonContainer) -> bool:
    if container.opener == "{":
        return _looks_like_json_object(text, container.start)
    return _looks_like_json_array(text, container.start)


def _json_containers(text: str) -> list[_JsonContainer]:
    containers: list[_JsonContainer] = []
    stack: list[int] = []
    in_string = False
    prose_string = False
    escaped = False
    for position, char in enumerate(text):
        if in_string:
            if prose_string and char in "\r\n":
                in_string = False
                prose_string = False
                escaped = False
            elif escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                prose_string = False
            continue
        if char == '"':
            in_string = True
            prose_string = not stack
        elif char in "[{":
            parent = stack[-1] if stack else None
            containers.append(_JsonContainer(opener=char, start=position, parent=parent))
            stack.append(len(containers) - 1)
        elif char in "]}":
            expected = "[" if char == "]" else "{"
            while stack and containers[stack[-1]].opener != expected:
                stack.pop()
            if not stack:
                continue
            container_index = stack.pop()
            containers[container_index].end = position + 1
    return containers
