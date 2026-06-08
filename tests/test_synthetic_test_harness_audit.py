"""Tests for core/synthetic_test_harness_audit.py."""

from __future__ import annotations

from core.synthetic_test_harness_audit import (
    active_findings_for_current_files,
    detect_new_synthetic_test_harnesses,
    prompt_context_for_records,
)


def test_detects_new_multi_subsystem_runtime_harness():
    after = """
class FakeEventTarget {
  addEventListener(type: string, listener: Listener) {}
  dispatchEvent(event: FakeEvent) {}
}
class FakeElement extends FakeEventTarget {
  appendChild(node: FakeElement) {}
  querySelector(selector: string) {}
}
class FakeHistory {
  pushState(state: object, title: string, path: string) {}
}
function installFakeApi() {
  return async (input: Request | string): Promise<Response> => new Response("{}");
}
"""

    findings = detect_new_synthetic_test_harnesses(path="tests/clientMain.test.ts", before=None, after=after)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["path"] == "tests/clientMain.test.ts"
    assert finding["category"] == "synthetic_runtime_harness"
    assert set(finding["subsystems"]) >= {
        "event_dispatch",
        "navigation_history",
        "network_server",
        "render_tree",
    }


def test_small_local_test_doubles_are_not_reported():
    after = """
class FakeRepository {
  findUser(id: string) {
    return { id };
  }
}

test("loads user", () => {
  const repository = new FakeRepository();
  expect(repository.findUser("1").id).toBe("1");
});
"""

    findings = detect_new_synthetic_test_harnesses(path="tests/user.test.ts", before=None, after=after)

    assert findings == []


def test_real_ui_test_library_usage_is_not_reported():
    after = """
test("opens details", async () => {
  render(<CountryList />);
  await user.click(screen.getByRole("link", { name: "Germany" }));
  expect(window.location.pathname).toBe("/countries/DEU");
  expect(await screen.findByText("Berlin")).toBeVisible();
});
"""

    findings = detect_new_synthetic_test_harnesses(path="tests/CountryList.test.tsx", before=None, after=after)

    assert findings == []


def test_project_standard_runtime_infra_usage_is_not_reported():
    after = """
import { MockWebServer } from "test-runtime";

test("opens details", async () => {
  const server = new MockWebServer();
  const navigator = createTestNavigator();
  const container = createTestContainer();
  server.enqueue({ code: "DEU" });
  await navigator.go("/countries/DEU", container);
});
"""

    findings = detect_new_synthetic_test_harnesses(path="tests/CountryList.test.tsx", before=None, after=after)

    assert findings == []


def test_type_references_to_fake_helpers_are_not_enough_to_report():
    after = """
type HarnessRefs = {
  element: FakeElement;
  history: FakeHistory;
  server: MockServer;
}
"""

    findings = detect_new_synthetic_test_harnesses(path="tests/clientMain.test.ts", before=None, after=after)

    assert findings == []


def test_cumulative_harness_is_reported_when_file_crosses_threshold():
    before = """
class FakeElement {
  appendChild(node: FakeElement) {}
}
class FakeHistory {
  pushState(state: object, title: string, path: string) {}
}
"""
    after = (
        before
        + """
class FakeMouseEvent {
  preventDefault() {}
}
"""
    )

    findings = detect_new_synthetic_test_harnesses(path="tests/clientMain.test.ts", before=before, after=after)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["baseline_subsystems"] == ["navigation_history", "render_tree"]
    assert set(finding["subsystems"]) == {"event_dispatch", "navigation_history", "render_tree"}


def test_preexisting_harness_helpers_are_not_reported_when_unchanged():
    before = """
class FakeEventTarget {
  addEventListener(type: string, listener: Listener) {}
  dispatchEvent(event: FakeEvent) {}
}
class FakeElement extends FakeEventTarget {
  appendChild(node: FakeElement) {}
  querySelector(selector: string) {}
}
class FakeHistory {
  pushState(state: object, title: string, path: string) {}
}
"""
    after = (
        before
        + """
test("uses existing helper", () => {
  expect(renderRoute("/countries/DEU")).toBe("Germany");
});
"""
    )

    findings = detect_new_synthetic_test_harnesses(path="tests/clientMain.test.ts", before=before, after=after)

    assert findings == []


def test_active_findings_track_current_file_contents(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "clientMain.test.ts"
    test_file.write_text("class FakeHistory {}\n", encoding="utf-8")
    record = {
        "status": "detected",
        "findings": [
            {
                "path": "tests/clientMain.test.ts",
                "evidence": [
                    {"category": "navigation_history", "lines": [{"line": 1, "excerpt": "class FakeHistory {}"}]}
                ],
            }
        ],
    }

    assert active_findings_for_current_files(tmp_path, [record])

    test_file.write_text("test('narrow seam', () => {});\n", encoding="utf-8")

    assert active_findings_for_current_files(tmp_path, [record]) == []


def test_cumulative_active_finding_resolves_when_only_baseline_helpers_remain(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "clientMain.test.ts"
    test_file.write_text(
        "\n".join(
            [
                "class FakeElement { appendChild() {}; querySelector() {} }",
                "class FakeHistory { pushState() {} }",
            ]
        ),
        encoding="utf-8",
    )
    record = {
        "status": "detected",
        "findings": [
            {
                "path": "tests/clientMain.test.ts",
                "subsystems": ["event_dispatch", "navigation_history", "render_tree"],
                "baseline_subsystems": ["navigation_history", "render_tree"],
                "evidence": [
                    {"category": "render_tree", "lines": [{"line": 1, "excerpt": "class FakeElement {}"}]},
                    {"category": "navigation_history", "lines": [{"line": 2, "excerpt": "class FakeHistory {}"}]},
                    {"category": "event_dispatch", "lines": [{"line": 3, "excerpt": "class FakeMouseEvent {}"}]},
                ],
            }
        ],
    }

    assert active_findings_for_current_files(tmp_path, [record]) == []


def test_active_findings_use_subsystems_without_source_excerpts(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "clientMain.test.ts"
    test_file.write_text(
        "\n".join(
            [
                "class FakeEventTarget { addEventListener() {}; dispatchEvent() {} }",
                "class FakeHistory { pushState() {} }",
                "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
            ]
        ),
        encoding="utf-8",
    )
    record = {
        "status": "detected",
        "findings": [
            {
                "path": "tests/clientMain.test.ts",
                "subsystems": ["event_dispatch", "navigation_history", "network_server"],
                "baseline_subsystems": [],
                "evidence": [
                    {"category": "event_dispatch", "lines": [{"line": 1}]},
                    {"category": "navigation_history", "lines": [{"line": 2}]},
                    {"category": "network_server", "lines": [{"line": 3}]},
                ],
            }
        ],
    }

    assert active_findings_for_current_files(tmp_path, [record])


def test_prompt_context_is_non_blocking_and_actionable():
    records = [
        {
            "status": "detected",
            "findings": [
                {
                    "path": "tests/clientMain.test.ts",
                    "subsystems": ["event_dispatch", "navigation_history", "network_server"],
                    "recommendation": "Replace with narrower existing-seam coverage.",
                    "evidence": [
                        {
                            "category": "event_dispatch",
                            "lines": [{"line": 10, "excerpt": "class FakeEventTarget {}"}],
                        }
                    ],
                }
            ],
        }
    ]

    context = prompt_context_for_records(records)

    assert "SYNTHETIC TEST HARNESS AUDIT CONTEXT (non-blocking)" in context
    assert "broad" in context
    assert "harness does not remain in branch output" in context
    assert "tests/clientMain.test.ts" in context
    assert "event_dispatch, navigation_history, network_server" in context
    assert "event_dispatch line 10" in context
    assert "class FakeEventTarget" not in context
