"""Tests for core/diagnostics.py."""

from __future__ import annotations

from core.diagnostics import cargo_test_failure_excerpt, diagnostic_excerpt


class TestDiagnosticExcerpt:
    def test_empty_and_nonpositive_limits_return_empty(self):
        assert diagnostic_excerpt("", limit=200) == ""
        assert diagnostic_excerpt("error: nope", limit=0) == ""

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

    def test_many_diagnostic_ranges_keep_first_two_and_last_two(self):
        output = "".join(f"error: marker {i}\n" + ("noise\n" * 200) for i in range(6))

        excerpt = diagnostic_excerpt(output, limit=2000, context_lines=0)

        assert "error: marker 0" in excerpt
        assert "error: marker 1" in excerpt
        assert "error: marker 4" in excerpt
        assert "error: marker 5" in excerpt
        assert "... [diagnostic output omitted] ..." in excerpt

    def test_tiny_limit_returns_tail_without_truncation_marker(self):
        assert diagnostic_excerpt("0123456789", limit=5) == "56789"

    def test_tight_limit_uses_diagnostic_without_head_or_tail_context(self):
        output = "HEAD\n" + ("filler\n" * 20) + "error: compact failure\n" + ("tail\n" * 20)

        excerpt = diagnostic_excerpt(output, limit=30, context_lines=0)

        assert len(excerpt) <= 30
        assert "HEAD" not in excerpt
        assert "tail" not in excerpt


class TestCargoTestFailureExcerpt:
    def test_empty_and_nonpositive_limits_return_empty(self):
        assert cargo_test_failure_excerpt("", limit=200) == ""
        assert cargo_test_failure_excerpt("failures:\n", limit=0) == ""

    def test_short_output_is_unchanged(self):
        output = "running 1 test\nfailures:\n    test_name\n"
        assert cargo_test_failure_excerpt(output, limit=200) == output

    def test_falls_back_to_generic_excerpt_without_cargo_failures_block(self):
        output = "HEAD\n" + ("noise\n" * 100) + "error: compiler failure\n" + ("tail\n" * 100)

        excerpt = cargo_test_failure_excerpt(output, limit=300)

        assert len(excerpt) <= 300
        assert "error: compiler failure" in excerpt

    def test_preserves_ansi_failure_block_until_plain_error_summary(self):
        output = (
            "prefix\n"
            + ("running 0 tests\n" * 80)
            + "\x1b[31mfailures:\x1b[0m\n"
            + "\n---- test_handles_value stdout ----\n"
            + "thread 'test_handles_value' panicked at tests/value.rs:10:5:\n"
            + "expected Some(1), got None\n"
            + "error: test failed\n"
            + "post failure noise\n"
        )

        excerpt = cargo_test_failure_excerpt(output, limit=500)

        assert "\x1b[31mfailures:\x1b[0m" in excerpt
        assert "test_handles_value" in excerpt
        assert "expected Some(1), got None" in excerpt
        assert "error: test failed" in excerpt
        assert "post failure noise" not in excerpt

    def test_stops_at_cargo_rerun_summary_before_later_noise(self):
        output = (
            "prefix\n"
            + ("running 0 tests\n" * 80)
            + "failures:\n"
            + "    test_preserves_signal\n"
            + "error: test failed, to rerun pass `-p crate --test signal`\n"
            + ("late noise\n" * 80)
        )

        excerpt = cargo_test_failure_excerpt(output, limit=500)

        assert "test_preserves_signal" in excerpt
        assert "error: test failed, to rerun pass `-p crate --test signal`" in excerpt
        assert "late noise" not in excerpt

    def test_truncates_oversized_failure_block(self):
        output = (
            "prefix\n"
            + "failures:\n"
            + ("failure detail line with useful context\n" * 120)
            + "error: test failed, to rerun pass `-p crate --test large`\n"
        )

        excerpt = cargo_test_failure_excerpt(output, limit=250)

        assert len(excerpt) <= 250
        assert "failures:" in excerpt
        assert "error: test failed, to rerun pass `-p crate --test large`" in excerpt
        assert "... [truncated] ..." in excerpt
