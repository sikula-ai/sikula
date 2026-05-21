from __future__ import annotations

from pathlib import Path

from tools.base_tool import Sandbox, ToolResult
from tools.gradle_tool import GradleBaseTool, _DEFAULT_COMPILE_TIMEOUT, _DEFAULT_SYNC_TIMEOUT, _DEFAULT_TEST_TIMEOUT

_ANDROID_COMPILE_TASK = "compileDebugKotlin"
_ANDROID_TEST_TASK = "testDebugUnitTest"
_ANDROID_PRESYNC_TASK = "generateDebugSources"


class AndroidGradleTool(GradleBaseTool):
    """Android / Gradle build tool implementation."""

    @staticmethod
    def env_files() -> list[str]:
        return ["local.properties"]

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        compile_task: str = _ANDROID_COMPILE_TASK,
        test_task: str = _ANDROID_TEST_TASK,
        presync_task: str = _ANDROID_PRESYNC_TASK,
        presync_clean: bool = False,
        sync_timeout: int = _DEFAULT_SYNC_TIMEOUT,
        compile_timeout: int = _DEFAULT_COMPILE_TIMEOUT,
        test_timeout: int = _DEFAULT_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox, project_root, sync_timeout, compile_timeout, test_timeout)
        self._compile_task = compile_task
        self._test_task = test_task
        self._presync_task = presync_task
        self._presync_clean = presync_clean

    def generate_sources(self) -> ToolResult:
        if self._presync_clean:
            clean = self._run("clean", timeout=self._sync_timeout)
            if not clean.success:
                return clean
        return self._run(self._presync_task, "--parallel", timeout=self._sync_timeout)

    def sync(self) -> ToolResult:
        return self._run("generateDebugSources", "--parallel", timeout=self._sync_timeout)

    def compile_check(self) -> ToolResult:
        return self._run(self._compile_task, timeout=self._compile_timeout)

    def run_tests(self) -> ToolResult:
        return self._run(self._test_task, timeout=self._test_timeout)

    def assemble(self) -> ToolResult:
        return self._run("assembleDebug", timeout=600)

    def lint(self) -> ToolResult:
        return self._run("lintDebug", timeout=300)
