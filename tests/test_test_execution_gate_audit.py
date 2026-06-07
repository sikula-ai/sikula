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


def test_detects_same_line_environment_gated_test_registration():
    findings = detect_new_test_execution_gates(
        path="tests/clientMain.test.ts",
        before=None,
        after='if (typeof document === "undefined") test("placeholder", () => {});\n',
    )

    assert len(findings) == 1
    assert findings[0]["category"] == "environment"
    assert findings[0]["line"] == 1


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
