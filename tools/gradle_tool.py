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
from core.diagnostics import validation_error_excerpt
from tools.base_tool import BuildTool, Sandbox, ToolResult

log = logging.getLogger(__name__)

_BUILD_CONFIG_SUFFIXES = (".gradle", ".gradle.kts", ".properties", ".toml")
_BUILD_CONFIG_DIRS = ("gradle/", "buildsrc/", "build-logic/")
_SYNC_ADOPTABLE_FILES = ("gradle.lockfile",)
_SYNC_ADOPTABLE_PATHS = (
    "gradle/dependency-locks/",
    "gradle/verification-metadata.xml",
    "gradle/verification-keyring.keys",
)

_DEFAULT_SYNC_TIMEOUT = 1800
_DEFAULT_COMPILE_TIMEOUT = 1800
_DEFAULT_TEST_TIMEOUT = 1800


class GradleBaseTool(BuildTool):
    """Shared Gradle mechanics for all Gradle-based build tools.

    Subclass this for each Gradle platform variant (Android, JVM backend, …).
    Provides _run(), _run_shell(), run_check(), and is_build_config_file().
    """

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        sync_timeout: int = _DEFAULT_SYNC_TIMEOUT,
        compile_timeout: int = _DEFAULT_COMPILE_TIMEOUT,
        test_timeout: int = _DEFAULT_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        # Windows cannot execute the extensionless POSIX wrapper (WinError 193);
        # Gradle ships gradlew.bat alongside it for the command processor.
        self._gradlew = self._root / ("gradlew.bat" if os.name == "nt" else "gradlew")
        self._sync_timeout = sync_timeout
        self._compile_timeout = compile_timeout
        self._test_timeout = test_timeout

    def _run(self, *args: str, timeout: int = 300) -> ToolResult:
        log.info(f"$ {self._gradlew} {' '.join(args)}  [cwd: {self._root}]  (timeout {timeout}s)")
        try:
            command, executable, batch_env = resolve_windows_batch_command([str(self._gradlew), *args])
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "errors": "replace",
                "cwd": self._root,
                "timeout": timeout,
            }
            if executable is not None:
                run_kwargs["env"] = batch_env
                r = run_windows_batch_process(command, executable=executable, **run_kwargs)
            else:
                r = subprocess.run(
                    command,
                    **run_kwargs,
                )
            output = r.stdout + r.stderr
            if r.returncode != 0:
                return ToolResult(success=False, output=output, error=validation_error_excerpt(output, limit=8000))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Gradle timed out")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _run_shell(self, command: str, timeout: int = 300) -> ToolResult:
        log.info(f"$ {command}  [cwd: {self._root}]  (timeout {timeout}s)")
        try:
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "errors": "replace",
                "cwd": self._root,
                "timeout": timeout,
            }
            if os.name == "nt":
                r = run_windows_shell_process(command, **run_kwargs)
            else:
                r = subprocess.run(command, shell=True, **run_kwargs)
            output = r.stdout + r.stderr
            if r.returncode != 0:
                return ToolResult(success=False, output=output, error=validation_error_excerpt(output, limit=8000))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        command = task_config.get("command", name)
        timeout = int(task_config.get("timeout", self._compile_timeout))
        return self._run_shell(command, timeout=timeout)

    def is_build_config_file(self, path: str) -> bool:
        p = path.replace("\\", "/").lower()
        return any(p.endswith(s) for s in _BUILD_CONFIG_SUFFIXES) or any(d in p for d in _BUILD_CONFIG_DIRS)

    def is_sync_adoptable_file(self, path: str) -> bool:
        p = path.replace("\\", "/").lower()
        name = Path(p).name
        return name in _SYNC_ADOPTABLE_FILES or any(
            p == adoptable_path or p.startswith(adoptable_path) for adoptable_path in _SYNC_ADOPTABLE_PATHS
        )

    def is_ephemeral_build_path(self, path: str) -> bool:
        return any(part in {".gradle", "build"} for part in self._delivery_scope_path_parts(path))
