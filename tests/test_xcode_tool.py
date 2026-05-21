"""Tests for tools/xcode_tool.py — XcodeTool."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.xcode_tool import XcodeTool, _BUILD_CONFIG_FILES, _BUILD_CONFIG_SUFFIXES


def _make_tool(root: Path, **kwargs) -> XcodeTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return XcodeTool(sandbox=sandbox, project_root=root, **kwargs)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestXcodeToolRun:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run(stdout="BUILD SUCCEEDED\n")):
            result = tool.compile_check()
        assert result.success
        assert "BUILD SUCCEEDED" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.xcode_tool.subprocess.run",
            return_value=_mock_run(returncode=65, stderr="** BUILD FAILED **"),
        ):
            result = tool.compile_check()
        assert not result.success
        assert "BUILD FAILED" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.xcode_tool.subprocess.run",
            side_effect=subprocess.TimeoutExpired("xcodebuild", 1),
        ):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", side_effect=OSError("xcodebuild not found")):
            result = tool.compile_check()
        assert not result.success
        assert "xcodebuild not found" in result.error

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()


class TestXcodeToolProjectArgs:
    def test_uses_xcworkspace_when_present(self, tmp_path: Path):
        ws = tmp_path / "MyApp.xcworkspace"
        ws.mkdir()
        tool = _make_tool(tmp_path, scheme="MyApp")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "-workspace" in args[0]
        assert str(ws) in args[0]

    def test_uses_xcodeproj_when_no_workspace(self, tmp_path: Path):
        proj = tmp_path / "MyApp.xcodeproj"
        proj.mkdir()
        tool = _make_tool(tmp_path, scheme="MyApp")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "-project" in args[0]
        assert str(proj) in args[0]

    def test_skips_project_xcworkspace(self, tmp_path: Path):
        (tmp_path / "project.xcworkspace").mkdir()
        proj = tmp_path / "MyApp.xcodeproj"
        proj.mkdir()
        tool = _make_tool(tmp_path, scheme="MyApp")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "-project" in args[0]

    def test_no_project_file_returns_empty_args(self, tmp_path: Path):
        tool = _make_tool(tmp_path, scheme="MyApp")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "-workspace" not in args[0]
        assert "-project" not in args[0]


class TestXcodeToolTasks:
    def test_compile_check_uses_scheme(self, tmp_path: Path):
        tool = _make_tool(tmp_path, scheme="MyScheme")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "MyScheme" in args[0]

    def test_compile_check_uses_destination(self, tmp_path: Path):
        tool = _make_tool(tmp_path, destination="generic/platform=iOS")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "generic/platform=iOS" in args[0]

    def test_run_tests_uses_test_destination(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_destination="platform=iOS Simulator,name=iPhone 15")
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "platform=iOS Simulator,name=iPhone 15" in args[0]

    def test_run_tests_writes_result_bundle(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "-resultBundlePath" in args[0]
        bundle_path = args[0][args[0].index("-resultBundlePath") + 1]
        assert bundle_path.endswith("TestResults.xcresult")

    def test_run_tests_uses_xcresult_failure_details(self, tmp_path: Path):
        tool = _make_tool(tmp_path)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "xcodebuild":
                bundle_path = Path(cmd[cmd.index("-resultBundlePath") + 1])
                bundle_path.mkdir(parents=True)
                return _mock_run(returncode=65, stdout="** TEST FAILED **")
            return _mock_run(
                stdout="""
                {
                  "actions": {
                    "_values": [
                      {
                        "actionResult": {
                          "issues": {
                            "testFailureSummaries": {
                              "_values": [
                                {
                                  "testCaseName": {
                                    "_value": "CountriesAppTests.testWiring()"
                                  },
                                  "message": {
                                    "_value": "failed - Expected CountriesListView"
                                  },
                                  "documentLocationInCreatingWorkspace": {
                                    "url": {
                                      "_value": "file:///tmp/AppTests.swift#StartingLineNumber=42"
                                    }
                                  }
                                }
                              ]
                            }
                          }
                        }
                      }
                    ]
                  }
                }
                """
            )

        with patch("tools.xcode_tool.subprocess.run", side_effect=fake_run):
            result = tool.run_tests()

        assert not result.success
        assert "CountriesAppTests.testWiring()" in result.error
        assert "/tmp/AppTests.swift:42" in result.error
        assert "Expected CountriesListView" in result.error

    def test_run_tests_falls_back_when_xcresult_unavailable(self, tmp_path: Path):
        tool = _make_tool(tmp_path)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "xcodebuild":
                bundle_path = Path(cmd[cmd.index("-resultBundlePath") + 1])
                bundle_path.mkdir(parents=True)
                return _mock_run(returncode=65, stdout="** TEST FAILED **\n")
            return _mock_run(returncode=1, stderr="xcresulttool failed")

        with patch("tools.xcode_tool.subprocess.run", side_effect=fake_run):
            result = tool.run_tests()

        assert not result.success
        assert "** TEST FAILED **" in result.error

    def test_sync_resolves_package_dependencies(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "-resolvePackageDependencies" in args[0]


class TestXcodeToolRunCheck:
    def test_uses_command_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("swiftlint", {"command": "swiftlint lint"})
        args, _ = mock.call_args
        assert args[0] == "swiftlint lint"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("swiftlint", {})
        args, _ = mock.call_args
        assert args[0] == "swiftlint"

    def test_timeout_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("swiftlint", {"command": "swiftlint lint", "timeout": "120"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 120

    def test_run_check_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.xcode_tool.subprocess.run",
            side_effect=subprocess.TimeoutExpired("swiftlint", 1),
        ):
            result = tool.run_check("swiftlint", {"command": "swiftlint lint"})
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_run_check_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.xcode_tool.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="error: swiftlint failed"),
        ):
            result = tool.run_check("swiftlint", {"command": "swiftlint lint"})
        assert not result.success
        assert "swiftlint failed" in result.error

    def test_run_check_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.xcode_tool.subprocess.run", side_effect=OSError("swiftlint not found")):
            result = tool.run_check("swiftlint", {"command": "swiftlint lint"})
        assert not result.success
        assert "swiftlint not found" in result.error


class TestXcodeToolExtractErrors:
    def test_extracts_error_lines(self):
        output = "Some verbose output\nerror: Type 'Foo' has no member 'bar'\nMore output"
        result = XcodeTool._extract_errors(output)
        assert "error: Type 'Foo' has no member 'bar'" in result
        assert "Some verbose output" not in result

    def test_extracts_build_failed_marker(self):
        output = "Compiling...\n** BUILD FAILED **\n"
        result = XcodeTool._extract_errors(output)
        assert "** BUILD FAILED **" in result

    def test_extracts_test_case_failed_line(self):
        output = "Testing started\nTest case 'CountriesAppTests.testWiring()' failed on 'iPhone 17 Pro' (0.1 seconds)\n"
        result = XcodeTool._extract_errors(output)
        assert "CountriesAppTests.testWiring()" in result

    def test_extracts_xcresult_test_failures_from_json(self):
        payload = {
            "actions": {
                "_values": [
                    {
                        "actionResult": {
                            "issues": {
                                "testFailureSummaries": {
                                    "_values": [
                                        {
                                            "testCaseName": {"_value": "CountriesAppTests.testWiring()"},
                                            "message": {"_value": "failed - Expected CountriesListView"},
                                            "documentLocationInCreatingWorkspace": {
                                                "url": {
                                                    "_value": (
                                                        "file:///tmp/CountriesTests.swift#StartingLineNumber=373"
                                                    )
                                                }
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ]
            }
        }

        result = XcodeTool._extract_xcresult_test_failures_from_json(payload)

        assert "CountriesAppTests.testWiring()" in result
        assert "/tmp/CountriesTests.swift:373" in result
        assert "Expected CountriesListView" in result

    def test_extract_xcresult_test_failures_ignores_non_object_json(self):
        assert XcodeTool._extract_xcresult_test_failures_from_json([]) == ""

    def test_returns_tail_when_no_keywords(self):
        output = "a" * 5000
        result = XcodeTool._extract_errors(output)
        assert len(result) <= 4000


class TestXcodeToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", _BUILD_CONFIG_FILES)
    def test_recognizes_build_config_files(self, filename: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(filename) is True

    @pytest.mark.parametrize("suffix", _BUILD_CONFIG_SUFFIXES)
    def test_recognizes_build_config_suffixes(self, suffix: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(f"Config{suffix}") is True

    def test_ignores_unrelated_files(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file("AppDelegate.swift") is False
        assert tool.is_build_config_file("build.gradle") is False
