from core.test_execution_gate_audit import detect_new_test_execution_gates


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

    assert findings == [
        {
            "path": "tests/clientMain.test.ts",
            "line": 3,
            "category": "environment",
            "reason": "environment-gated test registration",
            "excerpt": 'if (typeof document === "undefined") {',
        }
    ]


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
""",
    )

    assert [finding["line"] for finding in findings] == [1, 4]
    assert [finding["reason"] for finding in findings] == [
        "skipped JavaScript/TypeScript test",
        "skipped JavaScript/TypeScript test",
    ]


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
""",
    )

    assert [finding["line"] for finding in findings] == [1, 5]
    assert [finding["reason"] for finding in findings] == [
        "JUnit disabled test",
        "JUnit disabled test",
    ]


def test_detects_same_line_environment_gated_test_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after='if (typeof document === "undefined") test("placeholder", () => {});\n',
    )

    assert len(findings) == 1
    assert findings[0]["category"] == "environment"
    assert findings[0]["line"] == 1


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

        assert findings == [
            {
                "path": path,
                "line": expected_line,
                "category": "environment",
                "reason": "environment-gated test registration",
                "excerpt": after.splitlines()[expected_line - 1].strip(),
            }
        ]


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
