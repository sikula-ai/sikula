"""Stable source authority fragments and delivery obligation records."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from core.markdown_document import parse_markdown_document
from core.delivery_public_metadata import project_delivery_public_identity, sanitize_delivery_public_metadata


MAX_DELIVERY_AUTHORITY_FRAGMENTS = 512
MAX_DELIVERY_OBLIGATIONS = 256
MAX_DELIVERY_OBLIGATION_SOURCE_REFS = 32
MAX_DELIVERY_OBLIGATION_UNIT_IDS = 256


@dataclass(frozen=True)
class DeliveryAuthorityFragment:
    """One deterministic, non-overlapping fragment of the source task."""

    id: str
    start_line: int
    end_line: int
    sha256: str
    text: str

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
        }


@dataclass(frozen=True)
class DeliveryObligation:
    """A source-bound outcome owned by one or more delivery units."""

    id: str
    summary: str
    source_fragment_ids: list[str]
    unit_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": project_delivery_public_identity(self.id),
            "summary": sanitize_delivery_public_metadata(self.summary),
            "source_fragment_ids": list(self.source_fragment_ids),
            "unit_ids": [project_delivery_public_identity(unit_id) for unit_id in self.unit_ids],
        }

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "source_fragment_ids": list(self.source_fragment_ids),
            "unit_ids": list(self.unit_ids),
        }


def delivery_authority_fragments(source_task: str) -> list[DeliveryAuthorityFragment]:
    """Partition source Markdown into stable heading and top-level-list fragments."""

    if not source_task:
        return []
    document = parse_markdown_document(source_task)
    raw_lines = source_task.splitlines(keepends=True)
    line_count = len(document.lines)
    boundaries = {0, line_count}
    boundaries.update(line for line, _heading in document.headings)
    boundaries.update(item.start_line for item in document.list_items if item.parent_start_line is None)
    ordered = sorted(boundaries)
    fragments: list[DeliveryAuthorityFragment] = []
    for start, end in zip(ordered, ordered[1:]):
        text = "".join(raw_lines[start:end])
        digest = sha256(text.encode("utf-8")).hexdigest()
        start_line = start + 1
        end_line = end
        fragments.append(
            DeliveryAuthorityFragment(
                id=f"source-{start_line}-{end_line}-{digest[:12]}",
                start_line=start_line,
                end_line=end_line,
                sha256=f"sha256:{digest}",
                text=text,
            )
        )
    return fragments


def delivery_authority_fragment_map(source_task: str) -> dict[str, DeliveryAuthorityFragment]:
    """Return fragments keyed by their source-bound identity."""

    return {fragment.id: fragment for fragment in delivery_authority_fragments(source_task)}
