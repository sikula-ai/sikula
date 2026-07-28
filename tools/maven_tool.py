from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from core.subprocess_utils import (
    resolve_windows_batch_command,
    run_windows_batch_process,
    run_windows_shell_process,
)
from tools.base_tool import BuildTool, Sandbox, ToolResult, tool_error_excerpt

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = ("pom.xml",)
_BUILD_CONFIG_DIRS = (".mvn/",)

_DEFAULT_SYNC_TIMEOUT = 300
_DEFAULT_COMPILE_TIMEOUT = 600
_DEFAULT_TEST_TIMEOUT = 600


class MavenTool(BuildTool):
    """JVM backend / Maven build tool implementation.

    Uses the platform Maven wrapper when present, falls back to mvn on PATH.
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

        wrapper = self._root / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        self._mvn_bin = str(wrapper) if wrapper.exists() else "mvn"
        self._compile_command: str | list[str] = compile_command or [self._mvn_bin, "compile"]
        self._test_command: str | list[str] = test_command or [self._mvn_bin, "test"]
        self._sync_command: str | list[str] = sync_command or [
            self._mvn_bin,
            "dependency:resolve",
            "--batch-mode",
        ]
        self._presync_command: str | list[str] = presync_command or [
            self._mvn_bin,
            "generate-sources",
            "--batch-mode",
        ]

    def _run(self, command: str | list[str], timeout: int) -> ToolResult:
        display_command = command if isinstance(command, str) else subprocess.list2cmdline(command)
        log.info(f"$ {display_command}  [cwd: {self._root}]  (timeout {timeout}s)")
        try:
            run_command: str | list[str] = command
            run_kwargs: dict[str, object] = {
                "capture_output": True,
                "text": True,
                "errors": "replace",
                "cwd": self._root,
                "timeout": timeout,
            }
            if isinstance(command, str):
                if os.name == "nt":
                    r = run_windows_shell_process(run_command, **run_kwargs)
                else:
                    run_kwargs["shell"] = True
                    r = subprocess.run(
                        run_command,
                        **run_kwargs,
                    )
            else:
                run_command, executable, batch_env = resolve_windows_batch_command(command)
                if executable is not None:
                    run_kwargs["env"] = batch_env
                    r = run_windows_batch_process(run_command, executable=executable, **run_kwargs)
                else:
                    r = subprocess.run(
                        run_command,
                        **run_kwargs,
                    )
            output = r.stdout + r.stderr
            if r.returncode != 0:
                return ToolResult(success=False, output=output, error=tool_error_excerpt(output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {display_command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def generate_sources(self) -> ToolResult:
        if self._presync_clean:
            clean = self._run([self._mvn_bin, "clean"], self._sync_timeout)
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
