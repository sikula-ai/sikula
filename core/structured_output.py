"""Schema-aware extraction for structured LLM output."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable


_JSON_OBJECT_KEY_RE = re.compile(r'(?P<key>"(?:\\.|[^"\\])*")[ \t\r\n]*:')


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
