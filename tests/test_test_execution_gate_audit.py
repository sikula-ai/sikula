from core.test_execution_gate_audit import active_findings_for_current_files, detect_new_test_execution_gates


def _assert_public_finding_metadata(finding, *, path, line, category, reason, baseline_count=0, occurrence=1):
    assert finding["path"] == path
    assert finding["line"] == line
    assert finding["category"] == category
    assert finding["reason"] == reason
    assert finding["baseline_count"] == baseline_count
    assert finding["occurrence"] == occurrence
    assert finding["signature"].startswith(f"{category}:")
    assert "excerpt" not in finding


def test_detects_new_environment_gated_test_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after="""\
import { expect, test } from "bun:test";

if (typeof document === "undefined") {
  test("client main tests require DOM", () => {
    expect(typeof document).toBe("undefined");
  });
} else {
  test("opens detail", () => {});
}
""",
    )

    assert len(findings) == 1
    _assert_public_finding_metadata(
        findings[0],
        path="tests/clientMain.test.ts",
        line=3,
        category="environment",
        reason="environment-gated test registration",
    )


def test_detects_new_skip_and_ignore_gates():
    before = "test('keeps old behavior', () => {});\n"
    after = """\
test('keeps old behavior', () => {});
test.skip('changed behavior', () => {});
#[ignore]
fn generated_contract_test() {}
"""

    findings = detect_new_test_execution_gates(path="tests/generated.test.ts", before=before, after=after)

    assert [finding["category"] for finding in findings] == ["skip", "skip"]
    assert findings[0]["line"] == 2
    assert findings[0]["reason"] == "skipped JavaScript/TypeScript test"
    assert findings[1]["line"] == 3
    assert findings[1]["reason"] == "Rust ignored test"


def test_detects_parameterized_javascript_skip_gates():
    findings = detect_new_test_execution_gates(
        path="tests/generated.test.ts",
        before=None,
        after="""\
test.skip.each([
  ["case"],
])("changed behavior %s", () => {});
describe.skip.each([
  ["case"],
])("changed suite %s", () => {});
test.concurrent.skip("changed concurrent test", () => {});
""",
    )

    assert [finding["line"] for finding in findings] == [1, 4, 7]
    assert [finding["reason"] for finding in findings] == [
        "skipped JavaScript/TypeScript test",
        "skipped JavaScript/TypeScript test",
        "skipped JavaScript/TypeScript test",
    ]


def test_detects_playwright_and_todo_skip_gates():
    findings = detect_new_test_execution_gates(
        path="tests/generated.spec.ts",
        before=None,
        after="""\
test.fixme("changed behavior", async ({ page }) => {});
test.describe.fixme("changed group", () => {});
test.todo("changed behavior");
it.todo("changed behavior");
test.describe.configure({ mode: "skip" });
test.describe.configure({
  mode: 'skip',
});
""",
    )

    assert [finding["line"] for finding in findings] == [1, 2, 3, 4, 5, 7]
    assert [finding["reason"] for finding in findings] == [
        "Playwright fixme-skipped test",
        "Playwright fixme-skipped test",
        "JavaScript/TypeScript todo test",
        "JavaScript/TypeScript todo test",
        "Playwright skip-mode configuration",
        "Playwright skip-mode configuration",
    ]


def test_ignores_unrelated_javascript_skip_mode_values():
    findings = detect_new_test_execution_gates(
        path="tests/generated.spec.ts",
        before=None,
        after="""\
test.describe.configure({ mode: "parallel" });
const options = { mode: "skip" };
test("runs in normal validation", () => {});
""",
    )

    assert findings == []


def test_detects_junit4_ignore_gates():
    findings = detect_new_test_execution_gates(
        path="src/test/kotlin/GeneratedTest.kt",
        before=None,
        after="""\
@Ignore
@Test
fun generatedContractTest() {}

@org.junit.Ignore("requires browser")
@Test
fun generatedBrowserTest() {}

@org.junit.jupiter.api.Disabled("requires emulator")
@Test
fun generatedEmulatorTest() {}
""",
    )

    assert [finding["line"] for finding in findings] == [1, 5, 9]
    assert [finding["reason"] for finding in findings] == [
        "JUnit disabled test",
        "JUnit disabled test",
        "JUnit disabled test",
    ]


def test_detects_swift_try_xctskip_gates():
    findings = detect_new_test_execution_gates(
        path="Tests/GeneratedTests.swift",
        before=None,
        after="""\
func testGeneratedBehavior() throws {
    try XCTSkipIf(ProcessInfo.processInfo.environment["RUN_UI"] == nil)
}

func testGeneratedFallback() throws {
    try XCTSkipUnless(false)
}
""",
    )

    assert [finding["line"] for finding in findings] == [2, 6]
    assert [finding["reason"] for finding in findings] == ["XCTest skipped test", "XCTest skipped test"]


def test_detects_same_line_environment_gated_test_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after='if (typeof document === "undefined") test("placeholder", () => {});\n',
    )

    assert len(findings) == 1
    assert findings[0]["category"] == "environment"
    assert findings[0]["line"] == 1


def test_detects_env_var_gated_chained_javascript_registrations():
    cases = [
        """\
if (process.env.RUN_BROWSER_TESTS) {
  test.each(cases)("browser behavior %s", () => {});
}
""",
        """\
if (process.env.RUN_BROWSER_TESTS) {
  test.concurrent("browser behavior", () => {});
}
""",
        """\
if (process.env.RUN_BROWSER_TESTS) {
  test.concurrent.each(cases)("browser behavior %s", () => {});
}
""",
        """\
if (process.env.RUN_BROWSER_TESTS) {
  test.describe.parallel("browser behavior", () => {});
}
""",
    ]

    for after in cases:
        findings = detect_new_test_execution_gates(path="tests/clientMain.test.ts", before=None, after=after)

        assert len(findings) == 1
        _assert_public_finding_metadata(
            findings[0],
            path="tests/clientMain.test.ts",
            line=1,
            category="environment",
            reason="environment-gated test registration",
        )


def test_detects_multiline_environment_gated_test_registration_headers():
    cases = [
        (
            "tests/clientMain.test.ts",
            """\
if (
  process.env.RUN_BROWSER_TESTS
) {
  test("browser behavior", () => {});
}
""",
        ),
        (
            "tests/client_main_test.py",
            """\
if (
    os.environ.get("RUN_BROWSER_TESTS")
):
    def test_browser_behavior():
        pass
""",
        ),
        (
            "tests/ClientMainTest.kt",
            """\
if (
    System.getenv("RUN_BROWSER_TESTS") != null
) {
    @Test
    fun testBrowserBehavior() {}
}
""",
        ),
        (
            "tests/client_main_test.go",
            """\
if (
    os.Getenv("RUN_BROWSER_TESTS") != ""
) {
    func TestBrowserBehavior(t *testing.T) {}
}
""",
        ),
    ]

    for path, after in cases:
        findings = detect_new_test_execution_gates(path=path, before=None, after=after)

        assert len(findings) == 1
        _assert_public_finding_metadata(
            findings[0],
            path=path,
            line=1,
            category="environment",
            reason="environment-gated test registration",
        )


def test_detects_expression_style_env_gated_javascript_registrations():
    cases = [
        """\
process.env.RUN_BROWSER_TESTS && test("browser behavior", () => {});
""",
        """\
Boolean(process.env.RUN_BROWSER_TESTS) && test.each(cases)("browser behavior %s", () => {});
""",
        """\
import.meta.env.RUN_BROWSER_TESTS ? it("browser behavior", () => {}) : undefined;
""",
        """\
!process.env.RUN_BROWSER_TESTS || describe("browser behavior", () => {});
""",
        """\
ENV["RUN_BROWSER_TESTS"] && it "browser behavior" do
end
""",
    ]

    for after in cases:
        findings = detect_new_test_execution_gates(path="tests/clientMain.test.ts", before=None, after=after)

        assert len(findings) == 1
        _assert_public_finding_metadata(
            findings[0],
            path="tests/clientMain.test.ts",
            line=1,
            category="environment",
            reason="environment-gated test registration",
        )


def test_ignores_env_var_gated_javascript_test_configuration_without_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after="""\
if (process.env.CI) {
  test.describe.configure({ timeout: 60000 });
}
""",
    )

    assert findings == []


def test_ignores_multiline_environment_gate_without_test_registration():
    cases = [
        """\
if (
  process.env.CI
) {
  configureExternalService();
}

test("runs in normal validation", () => {});
""",
        """\
if (shouldConfigureExternalService) {
  const enabled = process.env.RUN_BROWSER_TESTS && configureExternalService();
}

test("runs in normal validation", () => {});
""",
    ]

    for after in cases:
        assert detect_new_test_execution_gates(path="tests/clientMain.test.ts", before=None, after=after) == []


def test_detects_env_var_gated_test_registration_across_common_runtimes():
    cases = [
        (
            "tests/clientMain.test.ts",
            """\
if (process.env.RUN_BROWSER_TESTS) {
  test("browser behavior", () => {});
}
""",
            1,
        ),
        (
            "tests/clientMain.test.ts",
            """\
if (process.env.RUN_BROWSER_TESTS)
  test("browser behavior", () => {});
""",
            1,
        ),
        (
            "tests/clientMain.test.ts",
            """\
if (import.meta.env.RUN_BROWSER_TESTS) {
  it("browser behavior", () => {});
}
""",
            1,
        ),
        (
            "tests/client_main_test.py",
            """\
import os

if os.environ.get("RUN_BROWSER_TESTS"):
    def test_browser_behavior():
        pass
""",
            3,
        ),
        (
            "tests/ClientMainTest.kt",
            """\
if (System.getenv("RUN_BROWSER_TESTS") != null) {
    @Test
    fun testBrowserBehavior() {}
}
""",
            1,
        ),
        (
            "Tests/ClientMainTests.swift",
            """\
if ProcessInfo.processInfo.environment["RUN_BROWSER_TESTS"] != nil {
    func testBrowserBehavior() {}
}
""",
            1,
        ),
        (
            "tests/client_main_test.go",
            """\
if (os.Getenv("RUN_BROWSER_TESTS") != "") {
    func TestBrowserBehavior(t *testing.T) {}
}
""",
            1,
        ),
        (
            "tests/ClientMainTest.php",
            """\
if (getenv("RUN_BROWSER_TESTS")) {
    public function testBrowserBehavior(): void {}
}
""",
            1,
        ),
        (
            "tests/client_main_spec.rb",
            """\
if ENV["RUN_BROWSER_TESTS"]
  it "tests browser behavior" do
  end
end
""",
            1,
        ),
    ]

    for path, after, expected_line in cases:
        findings = detect_new_test_execution_gates(path=path, before=None, after=after)

        assert len(findings) == 1
        _assert_public_finding_metadata(
            findings[0],
            path=path,
            line=expected_line,
            category="environment",
            reason="environment-gated test registration",
        )


def test_ignores_env_var_check_that_does_not_gate_test_registration():
    cases = [
        (
            "tests/clientMain.test.ts",
            """\
if (process.env.CI) {
  configureExternalService();
}

test("runs in normal validation", () => {});
""",
        ),
        (
            "tests/clientMain.test.ts",
            """\
const enabled = process.env.RUN_BROWSER_TESTS && configureExternalService();

test("runs in normal validation", () => {});
""",
        ),
        (
            "tests/client_main_test.py",
            """\
import os

if os.environ.get("CI"):
    configure_external_service()

def test_runs_in_normal_validation():
    pass
""",
        ),
        (
            "tests/client_main_spec.rb",
            """\
if ENV["CI"]
  configure_external_service
end

it "runs in normal validation" do
end
""",
        ),
    ]

    for path, after in cases:
        assert detect_new_test_execution_gates(path=path, before=None, after=after) == []


def test_ignores_preexisting_skip_gate_when_other_lines_change():
    before = """\
test.skip('external service contract', () => {});
test('old assertion', () => {
  expect(value).toBe(1);
});
"""
    after = """\
test.skip('external service contract', () => {});
test('old assertion', () => {
  expect(value).toBe(2);
});
"""

    findings = detect_new_test_execution_gates(path="tests/existing.test.ts", before=before, after=after)

    assert findings == []


def test_detects_only_new_occurrence_when_identical_skip_already_exists():
    before = """\
test.skip("external service contract", () => {});
test("normal behavior", () => {});
"""
    after = """\
test.skip("external service contract", () => {});
test("normal behavior", () => {});
test.skip("external service contract", () => {});
"""

    findings = detect_new_test_execution_gates(path="tests/existing.test.ts", before=before, after=after)

    assert len(findings) == 1
    _assert_public_finding_metadata(
        findings[0],
        path="tests/existing.test.ts",
        line=3,
        category="skip",
        reason="skipped JavaScript/TypeScript test",
        baseline_count=1,
        occurrence=2,
    )


def test_active_findings_resolve_against_added_occurrence_not_matching_baseline_text(tmp_path):
    test_file = tmp_path / "tests" / "existing.test.ts"
    test_file.parent.mkdir()
    before = """\
test.skip("external service contract", () => {});
test("normal behavior", () => {});
"""
    after = """\
test.skip("external service contract", () => {});
test("normal behavior", () => {});
test.skip("external service contract", () => {});
"""
    findings = detect_new_test_execution_gates(path="tests/existing.test.ts", before=before, after=after)
    record = {"status": "detected", "findings": findings}

    test_file.write_text(after, encoding="utf-8")
    active = active_findings_for_current_files(tmp_path, [record])

    assert len(active) == 1
    assert "excerpt" not in active[0]

    test_file.write_text(before, encoding="utf-8")

    assert active_findings_for_current_files(tmp_path, [record]) == []


def test_legacy_excerpt_findings_do_not_match_same_text_elsewhere(tmp_path):
    test_file = tmp_path / "tests" / "existing.test.ts"
    test_file.parent.mkdir()
    test_file.write_text(
        """\
test.skip("external service contract", () => {});
test("normal behavior", () => {});
""",
        encoding="utf-8",
    )
    record = {
        "status": "detected",
        "findings": [
            {
                "path": "tests/existing.test.ts",
                "line": 3,
                "category": "skip",
                "reason": "skipped JavaScript/TypeScript test",
                "excerpt": 'test.skip("external service contract", () => {});',
            }
        ],
    }

    assert active_findings_for_current_files(tmp_path, [record]) == []


def test_ignores_environment_check_that_does_not_gate_test_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after="""\
const hasDocument = typeof document !== "undefined";
test("reports runtime availability", () => {
  expect(hasDocument).toBe(false);
});
""",
    )

    assert findings == []
