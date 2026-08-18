"""CommonMark block structure used by Sikula's Markdown contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

from core.markdown_headings import MARKDOWN_HEADING_RE, MarkdownHeading, TEXT_HEADING_RE, normalize_heading


_HIDDEN_BLOCK_TYPES = {"code_block", "fence", "html_block"}
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


@dataclass(frozen=True)
class MarkdownListItem:
    start_line: int
    end_line: int
    parent_start_line: int | None


@dataclass(frozen=True)
class ParsedMarkdownDocument:
    lines: tuple[str, ...]
    visible_lines: tuple[str, ...]
    headings: tuple[tuple[int, MarkdownHeading], ...]
    list_items: tuple[MarkdownListItem, ...]
    list_item_lines: frozenset[int]
    hidden_lines: frozenset[int]

    def headings_by_line(self) -> dict[int, MarkdownHeading]:
        return dict(self.headings)

    def list_items_by_line(self) -> dict[int, MarkdownListItem]:
        return {item.start_line: item for item in self.list_items}


def parse_markdown_document(markdown: str) -> ParsedMarkdownDocument:
    """Parse block structure while retaining zero-based source line positions."""
    lines = tuple(markdown.splitlines())
    # HTML stays as block tokens; Sikula never renders provider-authored Markdown here.
    tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    headings: list[tuple[int, MarkdownHeading]] = []
    list_items: list[MarkdownListItem] = []
    list_item_stack: list[int] = []
    list_item_lines: set[int] = set()
    visible_lines, hidden_lines = _visible_source_lines(lines, tokens)
    seen_heading = False
    seen_content_before_heading = False

    for index, token in enumerate(tokens):
        if token.type in _HIDDEN_BLOCK_TYPES and token.map is not None:
            continue
        if token.type == "list_item_open" and token.map is not None:
            start_line, end_line = token.map
            list_items.append(
                MarkdownListItem(
                    start_line=start_line,
                    end_line=end_line,
                    parent_start_line=list_item_stack[-1] if list_item_stack else None,
                )
            )
            list_item_stack.append(start_line)
            list_item_lines.add(start_line)
        elif token.type == "list_item_close":
            list_item_stack.pop()
        if token.type == "heading_open" and token.level == 0 and token.map is not None:
            level = int(token.tag[1:])
            raw = _source_heading_text(visible_lines, token.map[0], token.map[1])
            is_document_title = level == 1 and not seen_heading and not seen_content_before_heading
            headings.append(
                (
                    token.map[0],
                    MarkdownHeading(
                        raw=raw,
                        normalized=normalize_heading(raw),
                        level=level,
                        kind="markdown",
                        is_document_title=is_document_title,
                    ),
                )
            )
            seen_heading = True
            continue
        if token.type == "paragraph_open" and token.level == 0 and token.map is not None:
            start, end = token.map
            text_headings = []
            for line_index in range(start, end):
                if line_index in hidden_lines:
                    continue
                text_heading = TEXT_HEADING_RE.match(visible_lines[line_index])
                if text_heading is not None:
                    text_headings.append((line_index, text_heading.group(1).strip()))
            for line_index, raw in text_headings:
                headings.append(
                    (
                        line_index,
                        MarkdownHeading(
                            raw=raw,
                            normalized=normalize_heading(raw),
                            level=0,
                            kind="text",
                        ),
                    )
                )
            if text_headings:
                seen_heading = True
            elif not seen_heading:
                seen_content_before_heading = True
        elif token.level == 0 and token.map is not None and token.nesting >= 0 and not seen_heading:
            seen_content_before_heading = True

    headings.sort(key=lambda item: item[0])
    return ParsedMarkdownDocument(
        lines=lines,
        visible_lines=visible_lines,
        headings=tuple(headings),
        list_items=tuple(list_items),
        list_item_lines=frozenset(list_item_lines),
        hidden_lines=frozenset(hidden_lines),
    )


def _visible_source_lines(lines: tuple[str, ...], tokens: list[Token]) -> tuple[tuple[str, ...], set[int]]:
    visible_lines = list(lines)
    hidden_lines: set[int] = set()
    for token in tokens:
        if token.type in _HIDDEN_BLOCK_TYPES and token.map is not None:
            for line_index in range(token.map[0], token.map[1]):
                visible_lines[line_index] = ""
                hidden_lines.add(line_index)
            continue
        if token.type != "inline" or token.map is None or not token.children:
            continue
        start_line, end_line = token.map
        segment = "\n".join(visible_lines[start_line:end_line])
        cursor = 0
        touched_lines: set[int] = set()
        mapping_failed = False
        for child in token.children:
            if child.type != "html_inline" or not child.content.lstrip().startswith("<!--"):
                continue
            offset = segment.find(child.content, cursor)
            if offset < 0:
                for line_index in range(start_line, end_line):
                    visible_lines[line_index] = ""
                    hidden_lines.add(line_index)
                mapping_failed = True
                break
            comment_end = offset + len(child.content)
            first_touched = start_line + segment[:offset].count("\n")
            last_touched = start_line + segment[:comment_end].count("\n")
            touched_lines.update(range(first_touched, last_touched + 1))
            replacement = "".join("\n" if char == "\n" else " " for char in child.content)
            segment = segment[:offset] + replacement + segment[comment_end:]
            cursor = offset + len(child.content)
        if mapping_failed:
            continue
        if touched_lines:
            visible_lines[start_line:end_line] = segment.split("\n")
            hidden_lines.update(line_index for line_index in touched_lines if not visible_lines[line_index].strip())
    return tuple(visible_lines), hidden_lines


def _source_heading_text(lines: tuple[str, ...], start_line: int, end_line: int) -> str:
    atx_heading = MARKDOWN_HEADING_RE.match(lines[start_line])
    if atx_heading is not None:
        return atx_heading.group(2).strip()
    if lines[start_line].lstrip().startswith("#"):
        return ""
    content_end = end_line
    if content_end > start_line and _SETEXT_UNDERLINE_RE.match(lines[content_end - 1]):
        content_end -= 1
    return "\n".join(lines[start_line:content_end]).strip()
