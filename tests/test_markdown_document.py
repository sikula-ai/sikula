from markdown_it.token import Token

import core.markdown_document as markdown_document_module
from core.markdown_document import parse_markdown_document


def test_commonmark_blocks_hide_nested_asset_syntax() -> None:
    markdown = """<div>
## Assets
- Asset: `assets/hidden.png`
</div>

## Assets
- Asset: `assets/visible.png`
"""

    document = parse_markdown_document(markdown)

    assert document.hidden_lines == frozenset({0, 1, 2, 3})
    assert [(line, heading.raw) for line, heading in document.headings] == [(5, "Assets")]
    assert document.list_item_lines == frozenset({6})


def test_commonmark_and_sikula_text_headings_keep_source_lines() -> None:
    markdown = """# Title

Scope:

Assets
------
"""

    document = parse_markdown_document(markdown)

    assert [
        (line, heading.raw, heading.level, heading.kind, heading.is_document_title)
        for line, heading in document.headings
    ] == [
        (0, "Title", 1, "markdown", True),
        (2, "Scope", 0, "text", False),
        (4, "Assets", 2, "markdown", False),
    ]


def test_adjacent_sikula_text_headings_are_retained_from_one_paragraph() -> None:
    markdown = """Assets:
Reference assets:
- Path: `assets/reference.png`
"""

    document = parse_markdown_document(markdown)

    assert [(line, heading.raw, heading.kind) for line, heading in document.headings] == [
        (0, "Assets", "text"),
        (1, "Reference assets", "text"),
    ]


def test_text_headings_inside_inline_html_comments_stay_hidden() -> None:
    markdown = """Visible prose <!--
Asset manifest:
Reference assets:
-->
Assets:
"""

    document = parse_markdown_document(markdown)

    assert tuple(line.rstrip() for line in document.visible_lines) == ("Visible prose", "", "", "", "Assets:")
    assert document.hidden_lines == frozenset({1, 2, 3})
    assert [(line, heading.raw) for line, heading in document.headings] == [(4, "Assets")]


def test_inline_html_comments_do_not_hide_visible_text_on_the_same_line() -> None:
    markdown = "- Path: `assets/reference.png` <!-- explanatory note -->\n"

    document = parse_markdown_document(markdown)

    assert document.visible_lines[0].rstrip() == "- Path: `assets/reference.png`"
    assert document.hidden_lines == frozenset()
    assert document.list_item_lines == frozenset({0})


def test_inline_html_comments_do_not_change_visible_heading_text() -> None:
    markdown = "## Assets <!-- explanatory note -->\n"

    document = parse_markdown_document(markdown)

    assert [(line, heading.raw, heading.level) for line, heading in document.headings] == [(0, "Assets", 2)]


def test_comment_only_heading_has_no_active_heading_text() -> None:
    markdown = "## <!-- Assets -->\n"

    document = parse_markdown_document(markdown)

    assert [(line, heading.raw, heading.normalized) for line, heading in document.headings] == [(0, "", "")]


def test_inline_comment_mapping_failure_hides_the_complete_token_range() -> None:
    inline = Token("inline", "", 0)
    inline.map = [0, 1]
    missing_comment = Token("html_inline", "", 0)
    missing_comment.content = "<!-- missing -->"
    present_comment = Token("html_inline", "", 0)
    present_comment.content = "<!-- present -->"
    inline.children = [missing_comment, present_comment]

    visible_lines, hidden_lines = markdown_document_module._visible_source_lines(
        ("visible <!-- present -->",),
        [inline],
    )

    assert visible_lines == ("",)
    assert hidden_lines == {0}


def test_nested_headings_are_not_task_sections() -> None:
    markdown = """> ## Assets
>
> - Asset: `assets/quoted.png`

- Example

  ## Asset manifest
"""

    document = parse_markdown_document(markdown)

    assert document.headings == ()


def test_list_items_retain_source_ranges_and_parent_items() -> None:
    markdown = """- Asset: `assets/icon.svg`
  - Usage: reference only and
    keep this continuation.
- Keep existing icons unchanged.
"""

    document = parse_markdown_document(markdown)

    assert [(item.start_line, item.end_line, item.parent_start_line) for item in document.list_items] == [
        (0, 3, None),
        (1, 3, 0),
        (3, 4, None),
    ]
