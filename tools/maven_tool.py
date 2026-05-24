from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from tools.base_tool import BuildTool, Sandbox, ToolResult, tool_error_excerpt

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = ("pom.xml",)
_BUILD_CONFIG_DIRS = (".mvn/",)

_DEFAULT_SYNC_TIMEOUT = 300
_DEFAULT_COMPILE_TIMEOUT = 600
_DEFAULT_TEST_TIMEOUT = 600


class MavenTool(BuildTool):
    """JVM backend / Maven build tool implementation.

    Uses ./mvnw (Maven wrapper) when present, falls back to mvn on PATH.
    Suitable for Spring Boot, Quarkus, Micronaut, and plain Java/Kotlin projects.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        compile_command: str | None = None,
        test_command: str | None = None,
        sync_command: str | None = None,
        presync_command: str | None = None,
        presync_clean: bool = False,
        sync_timeout: int = _DEFAULT_SYNC_TIMEOUT,
        compile_timeout: int = _DEFAULT_COMPILE_TIMEOUT,
        test_timeout: int = _DEFAULT_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        self._presync_clean = presync_clean
        self._sync_timeout = sync_timeout
        self._compile_timeout = compile_timeout
        self._test_timeout = test_timeout

        mvn = "./mvnw" if (self._root / "mvnw").exists() else "mvn"
        self._mvn_bin = mvn
        self._compile_command = compile_command or f"{mvn} compile"
        self._test_command = test_command or f"{mvn} test"
        self._sync_command = sync_command or f"{mvn} dependency:resolve --batch-mode"
        self._presync_command = presync_command or f"{mvn} generate-sources --batch-mode"

    def _run(self, command: str, timeout: int) -> ToolResult:
        log.info(f"$ {command}  [cwd: {self._root}]  (timeout {timeout}s)")
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
                return ToolResult(success=False, output=output, error=tool_error_excerpt(output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def generate_sources(self) -> ToolResult:
        if self._presync_clean:
            clean = self._run(f"{self._mvn_bin} clean", self._sync_timeout)
            if not clean.success:
                return clean
        return self._run(self._presync_command, self._sync_timeout)

    def sync(self) -> ToolResult:
        return self._run(self._sync_command, self._sync_timeout)

    def compile_check(self) -> ToolResult:
        return self._run(self._compile_command, self._compile_timeout)

    def run_tests(self) -> ToolResult:
        return self._run(self._test_command, self._test_timeout)

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        command = task_config.get("command", name)
        timeout = int(task_config.get("timeout", self._compile_timeout))
        return self._run(command, timeout)

    def is_build_config_file(self, path: str) -> bool:
        p = path.replace("\\", "/")
        return Path(p).name in _BUILD_CONFIG_FILES or any(d in p for d in _BUILD_CONFIG_DIRS)
