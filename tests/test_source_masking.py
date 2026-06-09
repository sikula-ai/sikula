"""Tests for core/source_masking.py."""

from core.source_masking import masked_source_lines


def test_masked_source_lines_preserve_escaped_physical_newline_count():
    text = """\
const source = "foo\\
bar";
test.skip("real skip", () => {});
"""

    assert len(masked_source_lines(text)) == len(text.splitlines())
