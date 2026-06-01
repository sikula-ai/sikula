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

        assert lines[0] == "e: .../com/example/SourceContractTestSupport.kt:11:22 Unresolved reference <redacted>."
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

    def test_summary_skips_indented_source_context_after_failure(self):
        output = (
            "UserTokenTest > does not leak token() FAILED\n"
            "    response = {'token': 'super-secret'}\n"
            "    assert response['token'] == expected_token\n"
            "    java.lang.AssertionError at UserTokenTest.kt:17\n"
        )

        lines = diagnostic_summary_lines(output)
        combined = "\n".join(lines)

        assert lines == [
            "UserTokenTest > does not leak token() FAILED",
            "java.lang.AssertionError at UserTokenTest.kt:17",
        ]
        assert "super-secret" not in combined
        assert "expected_token" not in combined

    def test_summary_skips_compiler_source_frames_after_error(self):
        output = (
            "src/auth.ts:12:5 - error TS2322: Type '\"super-secret\"' is not assignable to type 'number'.\n"
            "12 | const token = 'super-secret'\n"
            "   |     ^^^^^\n"
            "error: Type check failed\n"
        )

        lines = diagnostic_summary_lines(output)
        combined = "\n".join(lines)

        assert lines == [
            "src/auth.ts:12:5 - error TS2322: Type <redacted> is not assignable to type <redacted>.",
            "error: Type check failed",
        ]
        assert "super-secret" not in combined
        assert "^^^^^" not in combined

    def test_summary_redacts_panic_payload_literals(self):
        output = "thread 'test_handles_token' panicked at src/lib.rs:42:5: token value 'super-secret'\n"

        lines = diagnostic_summary_lines(output)

        assert lines == ["thread <redacted> panicked at src/lib.rs:42:5: token value <redacted>"]

    def test_summary_redacts_unquoted_secret_values(self):
        output = (
            "Exception: API_KEY=sk-test-secret\n"
            "error: token=abc123\n"
            "error: password: hunter2\n"
            "error: Authorization: Bearer abc123456\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines == [
            "Exception: API_KEY=<redacted>",
            "error: token=<redacted>",
            "error: password: <redacted>",
            "error: Authorization: <redacted>",
        ]

    def test_summary_omits_pytest_assertion_rewrite_values(self):
        output = (
            "TokenTest > hides credentials() FAILED\n"
            "E       assert actual_token == expected_token\n"
            "E       AssertionError: assert 'super-secret' == 'expected-token'\n"
        )

        lines = diagnostic_summary_lines(output)
        combined = "\n".join(lines)

        assert lines == [
            "TokenTest > hides credentials() FAILED",
            "E AssertionError: assertion failed",
        ]
        assert "super-secret" not in combined
        assert "expected-token" not in combined
        assert "expected_token" not in combined

    def test_summary_redacts_junit_assertion_payload_values(self):
        output = (
            "CountryRepositoryTest > token is not exposed() FAILED\n"
            "java.lang.AssertionError: expected:<public-token> but was:<super-secret-token>\n"
            "org.opentest4j.AssertionFailedError: expected: <expected-token> but was: <actual-token>\n"
        )

        lines = diagnostic_summary_lines(output)
        combined = "\n".join(lines)

        assert lines == [
            "CountryRepositoryTest > token is not exposed() FAILED",
            "java.lang.AssertionError: assertion failed",
            "org.opentest4j.AssertionFailedError: assertion failed",
        ]
        assert "public-token" not in combined
        assert "super-secret-token" not in combined
        assert "expected-token" not in combined
        assert "actual-token" not in combined

    def test_summary_redacts_jest_assertion_comparison_values(self):
        output = (
            "TokenCardTest > hides credentials() FAILED\n"
            'Expected: "public-token"\n'
            'Received: "super-secret"\n'
            "12 | expect(token).toEqual('public-token')\n"
            "   |              ^\n"
        )

        lines = diagnostic_summary_lines(output)
        combined = "\n".join(lines)

        assert lines == [
            "TokenCardTest > hides credentials() FAILED",
            "Expected: <redacted>",
            "Received: <redacted>",
        ]
        assert "public-token" not in combined
        assert "super-secret" not in combined
        assert "expect(token)" not in combined

    def test_summary_redacts_assertion_count_values(self):
        output = (
            "LoginButton.test.tsx > calls submit() FAILED\n"
            "Expected number of calls: >= 1\n"
            "Received number of calls: 0\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines == [
            "LoginButton.test.tsx > calls submit() FAILED",
            "Expected number of calls: <redacted>",
            "Received number of calls: <redacted>",
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

    def test_shortens_short_absolute_paths_before_summary_output(self):
        output = "/Users/alice/app.py:1: error: import failed\n/home/bob/project.py:2: error: lint failed\n"

        lines = diagnostic_summary_lines(output)

        assert lines == [
            ".../app.py:1: error: import failed",
            ".../project.py:2: error: lint failed",
        ]

    def test_extracts_relative_linter_locations_without_error_token(self):
        output = "app.py:1:8: F401 `os` imported but unused\nFound 1 error.\n[*] 1 fixable with the `--fix` option.\n"

        lines = diagnostic_summary_lines(output)

        assert lines == ["app.py:1:8: F401 <redacted> imported but unused"]

    def test_extracts_relative_formatter_locations_without_error_token(self):
        output = "    src/app.py:12:1: would reformat\nAll done!\n"

        lines = diagnostic_summary_lines(output)

        assert lines == ["src/app.py:12:1: would reformat"]

    def test_extracts_typescript_and_rust_error_locations(self):
        output = (
            "tests/clientMain.test.ts(369,67): error TS2345: Argument of type 'null' is not assignable.\n"
            "src/lib.rs:42:13: error[E0308]: mismatched types\n"
        )

        lines = diagnostic_summary_lines(output)

        assert "tests/clientMain.test.ts(369,67): error TS2345" in lines[0]
        assert "src/lib.rs:42:13: error[E0308]" in lines[1]

    def test_extracts_indented_real_diagnostics(self):
        output = (
            "FAILED tests/test_auth.py::test_login\n"
            "    /tmp/project/tests/test_auth.py:42:5: AssertionError: login failed\n"
            "    error: missing generated client\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines == [
            "FAILED tests/test_auth.py::test_login",
            ".../project/tests/test_auth.py:42:5: AssertionError: assertion failed",
            "error: missing generated client",
        ]

    def test_deduplicates_alternate_shortened_forms_of_same_location(self):
        output = (
            "error: file:///tmp/worktrees/task123/project/src/tests/test_user_flow.py:12:5 "
            "AssertionError: expected detail screen\n"
            "task123.../src/tests/test_user_flow.py:12:5 AssertionError: expected detail screen\n"
        )

        lines = diagnostic_summary_lines(output)

        assert lines == ["error: .../src/tests/test_user_flow.py:12:5 AssertionError: assertion failed"]


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
