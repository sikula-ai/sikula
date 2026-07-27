from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from core.subprocess_utils import run_windows_shell_process
from tools.base_tool import BuildTool, Sandbox, ToolResult, tool_error_excerpt

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
)

_DEFAULT_COMPILE_COMMAND = "ruff check ."
_DEFAULT_TEST_COMMAND = "pytest"
_DEFAULT_TIMEOUT = 300


class PythonTool(BuildTool):
    """Python build tool implementation."""

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

    def _run(self, command: str | list[str], timeout: int | None = None) -> ToolResult:
        t = timeout or self._timeout
        display_command = command if isinstance(command, str) else subprocess.list2cmdline(command)
        log.info(f"$ {display_command}  [cwd: {self._root}]  (timeout {t}s)")
        try:
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "errors": "replace",
                "cwd": self._root,
                "timeout": t,
            }
            if isinstance(command, str) and os.name == "nt":
                r = run_windows_shell_process(command, **run_kwargs)
            else:
                r = subprocess.run(command, shell=isinstance(command, str), **run_kwargs)
            output = r.stdout + r.stderr
            if r.returncode not in (0, 5):  # pytest exit 5 = no tests collected
                return ToolResult(success=False, output=output, error=tool_error_excerpt(output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {display_command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def sync(self) -> ToolResult:
        """Install dependencies from requirements.txt if present."""
        req = self._root / "requirements.txt"
        if req.exists():
            return self._run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        return ToolResult(success=True, output="No requirements.txt — skipping sync")

    def compile_check(self) -> ToolResult:
        """Run compile/lint check. Configurable via build.compile_command in project YAML."""
        return self._run(self._compile_command)

    def run_tests(self) -> ToolResult:
        """Run tests. Configurable via build.test_command in project YAML."""
        return self._run(self._test_command)

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        """Run a named check. task_config keys: command (required), timeout (optional)."""
        command = task_config.get("command", name)
        timeout = int(task_config.get("timeout", self._timeout))
        return self._run(command, timeout=timeout)

    def is_build_config_file(self, path: str) -> bool:
        return Path(path).name in _BUILD_CONFIG_FILES
