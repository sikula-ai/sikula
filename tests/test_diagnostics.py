"""Tests for core/diagnostics.py."""

from __future__ import annotations

from core.diagnostics import diagnostic_excerpt


class TestDiagnosticExcerpt:
    def test_short_output_is_unchanged(self):
        output = "error: missing semicolon\n"
        assert diagnostic_excerpt(output, limit=200) == output

    def test_preserves_failure_block_when_tail_is_noisy(self):
        output = (
            "Compiling workspace\n"
            + "".join(f"build line {i}\n" for i in range(120))
            + "thread 'test_rejects_wrong_result_type' panicked at assertion failed\n"
            + "left: Ok(ParsedConfig)\n"
            + "right: Err(ValidationError)\n"
            + "failures:\n"
            + "    test_rejects_wrong_result_type\n"
            + "test result: FAILED. 42 passed; 1 failed\n"
            + "".join(f"Running unrelated test binary {i}\n" for i in range(180))
            + "error: test failed, to rerun pass `-p example_crate --test validation_tests`\n"
        )

        excerpt = diagnostic_excerpt(output, limit=1400)

        assert len(excerpt) <= 1400
        assert "test_rejects_wrong_result_type" in excerpt
        assert "left: Ok(ParsedConfig)" in excerpt
        assert "error: test failed" in excerpt

    def test_preserves_ansi_colored_failure_markers(self):
        output = "prefix\n" + ("noise\n" * 100) + "\x1b[31mFAILED\x1b[0m tests/test_api.py::test_login\n"

        excerpt = diagnostic_excerpt(output, limit=300)

        assert "\x1b[31mFAILED\x1b[0m tests/test_api.py::test_login" in excerpt

    def test_fallback_keeps_head_and_tail_when_no_markers_exist(self):
        output = "HEAD\n" + ("middle\n" * 100) + "TAIL\n"

        excerpt = diagnostic_excerpt(output, limit=120)

        assert len(excerpt) <= 120
        assert "HEAD" in excerpt
        assert "TAIL" in excerpt
        assert "... [truncated] ..." in excerpt

    def test_multiple_failure_blocks_keep_first_and_last_blocks(self):
        output = (
            "start\n"
            + "error: first compiler error\n"
            + ("noise\n" * 100)
            + "AssertionError: last test assertion\n"
            + "end\n"
        )

        excerpt = diagnostic_excerpt(output, limit=500)

        assert "first compiler error" in excerpt
        assert "last test assertion" in excerpt
