from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from tools.base_tool import BuildTool, Sandbox, ToolResult

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = frozenset({"Package.swift", "Package.resolved"})
_BUILD_CONFIG_SUFFIXES = frozenset({".xcconfig"})

_DEFAULT_SCHEME = "Countries"
_DEFAULT_DESTINATION = "generic/platform=iOS Simulator"
_DEFAULT_TEST_DESTINATION = "platform=iOS Simulator,OS=latest,name=iPhone 16"
_DEFAULT_COMPILE_TIMEOUT = 1800
_DEFAULT_TEST_TIMEOUT = 1800


class XcodeTool(BuildTool):
    """iOS / Xcode build tool implementation."""

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        scheme: str = _DEFAULT_SCHEME,
        destination: str = _DEFAULT_DESTINATION,
        test_destination: str = _DEFAULT_TEST_DESTINATION,
        compile_timeout: int = _DEFAULT_COMPILE_TIMEOUT,
        test_timeout: int = _DEFAULT_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        self._scheme = scheme
        self._destination = destination
        self._test_destination = test_destination
        self._compile_timeout = compile_timeout
        self._test_timeout = test_timeout

    def _project_args(self) -> list[str]:
        for p in sorted(self._root.iterdir()):
            if p.suffix == ".xcworkspace" and p.name != "project.xcworkspace":
                return ["-workspace", str(p)]
        for p in sorted(self._root.iterdir()):
            if p.suffix == ".xcodeproj":
                return ["-project", str(p)]
        return []

    def _run(self, args: list[str], timeout: int, result_bundle_path: Path | None = None) -> ToolResult:
        cmd = ["xcodebuild"] + args
        log.info("$ %s  [cwd: %s]  (timeout %ds)", " ".join(cmd), self._root, timeout)
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self._root,
                timeout=timeout,
            )
            output = r.stdout + r.stderr
            if r.returncode != 0:
                error = ""
                if result_bundle_path is not None:
                    error = self._extract_xcresult_test_failures(result_bundle_path)
                return ToolResult(success=False, output=output, error=error or self._extract_errors(output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="xcodebuild timed out")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _run_shell(self, command: str, timeout: int) -> ToolResult:
        log.info("$ %s  [cwd: %s]  (timeout %ds)", command, self._root, timeout)
        try:
            r = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self._root,
                timeout=timeout,
            )
            output = r.stdout + r.stderr
            if r.returncode != 0:
                return ToolResult(success=False, output=output, error=self._extract_errors(output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    @staticmethod
    def _extract_errors(output: str) -> str:
        """Extract error and warning lines from verbose xcodebuild output."""
        keywords = (
            "error:",
            "warning:",
            "Build FAILED",
            "** BUILD FAILED **",
            "** TEST FAILED **",
            "Test case '",
            " failed on '",
            " failed (",
        )
        lines = [line for line in output.splitlines() if any(k in line for k in keywords)]
        return "\n".join(lines)[-4000:] if lines else output[-4000:]

    @staticmethod
    def _extract_xcresult_test_failures(result_bundle_path: Path) -> str:
        if not result_bundle_path.exists():
            return ""

        commands = [
            [
                "xcrun",
                "xcresulttool",
                "get",
                "object",
                "--legacy",
                "--path",
                str(result_bundle_path),
                "--format",
                "json",
            ],
            [
                "xcrun",
                "xcresulttool",
                "get",
                "--path",
                str(result_bundle_path),
                "--format",
                "json",
            ],
        ]
        for cmd in commands:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except Exception:
                continue
            if result.returncode != 0:
                continue
            try:
                return XcodeTool._extract_xcresult_test_failures_from_json(json.loads(result.stdout))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return ""

    @staticmethod
    def _extract_xcresult_test_failures_from_json(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""

        failures: list[str] = []
        for action in XcodeTool._xc_values(payload.get("actions")):
            action_result = action.get("actionResult", {})
            issues = action_result.get("issues", {})
            summaries = issues.get("testFailureSummaries", {})
            for failure in XcodeTool._xc_values(summaries):
                test_case = XcodeTool._xc_value(failure.get("testCaseName"))
                message = XcodeTool._xc_value(failure.get("message"))
                location = XcodeTool._xcresult_location(failure)
                parts = [p for p in (test_case, location, message) if p]
                if parts:
                    failures.append(" - ".join(parts))
        return "\n".join(failures)[-4000:]

    @staticmethod
    def _xc_values(value: object) -> list[dict]:
        if isinstance(value, dict):
            values = value.get("_values")
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
        return []

    @staticmethod
    def _xc_value(value: object) -> str:
        if isinstance(value, dict):
            raw = value.get("_value")
            if isinstance(raw, str):
                return raw
        return ""

    @staticmethod
    def _xcresult_location(failure: dict) -> str:
        location = failure.get("documentLocationInCreatingWorkspace", {})
        url = XcodeTool._xc_value(location.get("url") if isinstance(location, dict) else None)
        if not url:
            return ""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        query = parse_qs(parsed.fragment or parsed.query)
        line = next(iter(query.get("StartingLineNumber", [])), "")
        return f"{path}:{line}" if line else path

    # ------------------------------------------------------------------
    # BuildTool interface
    # ------------------------------------------------------------------

    def sync(self) -> ToolResult:
        return self._run(
            self._project_args() + ["-resolvePackageDependencies", "-scheme", self._scheme],
            timeout=self._compile_timeout,
        )

    def compile_check(self) -> ToolResult:
        return self._run(
            self._project_args()
            + [
                "build",
                "-scheme",
                self._scheme,
                "-destination",
                self._destination,
                "-configuration",
                "Debug",
            ],
            timeout=self._compile_timeout,
        )

    def run_tests(self) -> ToolResult:
        with tempfile.TemporaryDirectory(prefix="sikula-xcode-") as tmp:
            result_bundle_path = Path(tmp) / "TestResults.xcresult"
            return self._run(
                self._project_args()
                + [
                    "test",
                    "-scheme",
                    self._scheme,
                    "-destination",
                    self._test_destination,
                    "-configuration",
                    "Debug",
                    "-resultBundlePath",
                    str(result_bundle_path),
                ],
                timeout=self._test_timeout,
                result_bundle_path=result_bundle_path,
            )

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        command = task_config.get("command", name)
        timeout = int(task_config.get("timeout", self._compile_timeout))
        return self._run_shell(command, timeout)

    def is_build_config_file(self, path: str) -> bool:
        p = Path(path)
        return p.name in _BUILD_CONFIG_FILES or p.suffix in _BUILD_CONFIG_SUFFIXES
