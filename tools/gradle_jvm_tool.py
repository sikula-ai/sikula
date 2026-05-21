from __future__ import annotations

from pathlib import Path

from tools.base_tool import Sandbox, ToolResult
from tools.gradle_tool import GradleBaseTool

_DEFAULT_SYNC_TIMEOUT = 600
_JVM_COMPILE_TIMEOUT = 600
_JVM_TEST_TIMEOUT = 600

_JVM_COMPILE_TASK = "classes"
_JVM_TEST_TASK = "test"
_JVM_SYNC_TASK = "classes"
_JVM_PRESYNC_TASK = "classes"


class JvmGradleTool(GradleBaseTool):
    """JVM backend / Gradle build tool implementation.

    Suitable for Spring Boot, Quarkus, Micronaut, and plain Kotlin/Java projects.
    Defaults to the 'classes' lifecycle task which compiles all sources and triggers
    annotation processors (Lombok, MapStruct, OpenAPI codegen, etc.).
    """

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        compile_task: str = _JVM_COMPILE_TASK,
        test_task: str = _JVM_TEST_TASK,
        sync_task: str = _JVM_SYNC_TASK,
        presync_task: str = _JVM_PRESYNC_TASK,
        presync_clean: bool = False,
        sync_timeout: int = _DEFAULT_SYNC_TIMEOUT,
        compile_timeout: int = _JVM_COMPILE_TIMEOUT,
        test_timeout: int = _JVM_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox, project_root, sync_timeout, compile_timeout, test_timeout)
        self._compile_task = compile_task
        self._test_task = test_task
        self._sync_task = sync_task
        self._presync_task = presync_task
        self._presync_clean = presync_clean

    def generate_sources(self) -> ToolResult:
        if self._presync_clean:
            clean = self._run("clean", timeout=self._sync_timeout)
            if not clean.success:
                return clean
        return self._run(self._presync_task, "--parallel", timeout=self._sync_timeout)

    def sync(self) -> ToolResult:
        return self._run(self._sync_task, "--parallel", timeout=self._sync_timeout)

    def compile_check(self) -> ToolResult:
        return self._run(self._compile_task, timeout=self._compile_timeout)

    def run_tests(self) -> ToolResult:
        return self._run(self._test_task, timeout=self._test_timeout)
