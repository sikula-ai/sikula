"""Tests for core/diagnostics.py."""

from __future__ import annotations

from core.diagnostics import cargo_test_failure_excerpt, diagnostic_excerpt, diagnostic_summary_lines


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


class TestDiagnosticSummaryLines:
    def test_extracts_gradle_compiler_error_from_noisy_output(self):
        output = (
            "> Task :app:preBuild UP-TO-DATE\n"
            + ("noise\n" * 120)
            + "> Task :feature:countries:compileDebugUnitTestKotlin FAILED\n"
            + "96 actionable tasks: 16 executed, 80 up-to-date\n"
            + "e: file:///Users/me/project/feature/countries/src/test/kotlin/com/example/"
            "SourceContractTestSupport.kt:11:22 Unresolved reference 'readString'.\n"
            + "\nFAILURE: Build failed with an exception.\n"
            + "* What went wrong:\n"
            + "Execution failed for task ':feature:countries:compileDebugUnitTestKotlin'.\n"
            + "BUILD FAILED in 2s\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines[0] == "e: .../com/example/SourceContractTestSupport.kt:11:22 Unresolved reference 'readString'."
        assert not lines[0].startswith("> Task")

    def test_extracts_failed_test_name_and_exception_context(self):
        output = (
            "> Task :feature:countries:testDebugUnitTest\n\n"
            "CountriesNavigationContractTest > detail route builder uri encodes code name and flag emoji() FAILED\n"
            "    java.lang.RuntimeException at CountriesNavigationContractTest.kt:27\n"
            "\n38 tests completed, 1 failed\n"
            "BUILD FAILED in 4s\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines[:2] == [
            "CountriesNavigationContractTest > detail route builder uri encodes code name and flag emoji() FAILED",
            "java.lang.RuntimeException at CountriesNavigationContractTest.kt:27",
        ]

    def test_extracts_linter_file_locations_without_gradle_boilerplate_first(self):
        output = (
            "> Task :feature:countries:detekt FAILED\n"
            "/Users/me/project/feature/countries/src/main/kotlin/com/example/CountryDetailScreen.kt:43:19: "
            "Top level constant names should match the pattern: [A-Z][_A-Z0-9]* [TopLevelPropertyNaming]\n"
            "/Users/me/project/feature/countries/src/test/kotlin/com/example/CountriesNavigationContractTest.kt:30:1: "
            "Line detected, which is longer than the defined maximum line length in the code style. [MaxLineLength]\n"
            "BUILD FAILED in 2s\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines[0].startswith(".../com/example/CountryDetailScreen.kt:43:19")
        assert "TopLevelPropertyNaming" in lines[0]
        assert "MaxLineLength" in lines[1]

    def test_extracts_typescript_and_rust_error_locations(self):
        output = (
            "tests/clientMain.test.ts(369,67): error TS2345: Argument of type 'null' is not assignable.\n"
            "src/lib.rs:42:13: error[E0308]: mismatched types\n"
        )

        lines = diagnostic_summary_lines(output)

        assert "tests/clientMain.test.ts(369,67): error TS2345" in lines[0]
        assert "src/lib.rs:42:13: error[E0308]" in lines[1]

    def test_deduplicates_alternate_shortened_forms_of_same_location(self):
        output = (
            "error: file:///tmp/worktrees/task123/project/src/tests/test_user_flow.py:12:5 "
            "AssertionError: expected detail screen\n"
            "task123.../src/tests/test_user_flow.py:12:5 AssertionError: expected detail screen\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines == ["error: .../src/tests/test_user_flow.py:12:5 AssertionError: expected detail screen"]


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
