from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from tools.base_tool import BuildTool, Sandbox, ToolResult

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = ("Cargo.toml", "Cargo.lock")

_DEFAULT_COMPILE_COMMAND = "cargo check"
_DEFAULT_TEST_COMMAND = "cargo test"
_DEFAULT_TIMEOUT = 600


class CargoTool(BuildTool):
    """Rust/Cargo build tool implementation."""

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        compile_command: str = _DEFAULT_COMPILE_COMMAND,
        test_command: str = _DEFAULT_TEST_COMMAND,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        self._compile_command = compile_command
        self._test_command = test_command
        self._timeout = timeout

    def _run(self, command: str, timeout: int | None = None) -> ToolResult:
        t = timeout or self._timeout
        log.info(f"$ {command}  [cwd: {self._root}]  (timeout {t}s)")
        try:
            r = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=self._root,
                timeout=t,
            )
            output = r.stdout + r.stderr
            if r.returncode != 0:
                return ToolResult(success=False, output=output, error=output[-4000:])
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def sync(self) -> ToolResult:
        return self._run("cargo fetch")

    def compile_check(self) -> ToolResult:
        return self._run(self._compile_command)

    def run_tests(self) -> ToolResult:
        return self._run(self._test_command)

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        command = task_config.get("command", name)
        timeout = int(task_config.get("timeout", self._timeout))
        return self._run(command, timeout=timeout)

    def is_build_config_file(self, path: str) -> bool:
        return Path(path).name in _BUILD_CONFIG_FILES
