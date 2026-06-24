"""Shared Markdown heading helpers used by task and contract parsing."""

from __future__ import annotations

from dataclasses import dataclass
import re


MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
TEXT_HEADING_RE = re.compile(r"^\s{0,3}([A-Za-z][A-Za-z0-9 /&_-]{1,60}):\s*$")
FENCED_BLOCK_RE = re.compile(r"^\s{0,3}(```+|~~~+)")


@dataclass(frozen=True)
class MarkdownHeading:
    raw: str
    normalized: str
    level: int
    kind: str
    is_document_title: bool = False

    @property
    def is_markdown(self) -> bool:
        return self.kind == "markdown"

    @property
    def is_text(self) -> bool:
        return self.kind == "text"


def normalize_heading(value: str) -> str:
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


class MarkdownHeadingScanner:
    """Stateful scanner that applies Sikula's document-title heading rule."""

    def __init__(self, *, ignore_fenced_blocks: bool = False) -> None:
        self._ignore_fenced_blocks = ignore_fenced_blocks
        self._in_fenced_block = False
        self._seen_heading = False
        self._seen_content_before_heading = False

    def match(self, line: str) -> MarkdownHeading | None:
        if self._ignore_fenced_blocks and FENCED_BLOCK_RE.match(line):
            self._in_fenced_block = not self._in_fenced_block
            return None
        if self._ignore_fenced_blocks and self._in_fenced_block:
            return None

        markdown_heading = MARKDOWN_HEADING_RE.match(line)
        if markdown_heading:
            level = len(markdown_heading.group(1))
            raw_heading = markdown_heading.group(2).strip()
            is_document_title = level == 1 and not self._seen_heading and not self._seen_content_before_heading
            self._seen_heading = True
            return MarkdownHeading(
                raw=raw_heading,
                normalized=normalize_heading(raw_heading),
                level=level,
                kind="markdown",
                is_document_title=is_document_title,
            )

        text_heading = TEXT_HEADING_RE.match(line)
        if text_heading:
            raw_heading = text_heading.group(1).strip()
            self._seen_heading = True
            return MarkdownHeading(
                raw=raw_heading,
                normalized=normalize_heading(raw_heading),
                level=0,
                kind="text",
            )

        if line.strip() and not self._seen_heading:
            self._seen_content_before_heading = True
        return None
