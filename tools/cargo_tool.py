from __future__ import annotations

import difflib
import logging
import re
import subprocess
from pathlib import Path

from core.diagnostics import cargo_test_failure_excerpt
from tools.base_tool import BuildTool, Sandbox, ToolResult, tool_error_excerpt

log = logging.getLogger(__name__)

_BUILD_CONFIG_FILES = ("Cargo.toml", "Cargo.lock")

_DEFAULT_SYNC_COMMAND = "cargo fetch --locked"
_DEFAULT_COMPILE_COMMAND = "cargo check"
_DEFAULT_TEST_COMMAND = "cargo test"
_DEFAULT_TIMEOUT = 600
_CARGO_ERROR_LIMIT = 8000
_RUST_HUNK_RE = re.compile(
    r"@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_RUST_CFG_TEST_RE = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]")
_RUST_TEST_MODULE_RE = re.compile(r"\bmod\s+tests\b")
_CARGO_WORKSPACE_HEADER_RE = re.compile(r"^\s*\[workspace\]\s*(?:#.*)?$")
_CARGO_LOCK_NEEDS_UPDATE_RE = re.compile(
    r"(?:lock file .* needs to be updated|Cargo\.lock needs to be updated).*--locked",
    re.IGNORECASE | re.DOTALL,
)


def _is_cargo_test_command(command: str) -> bool:
    return bool(re.match(r"^\s*cargo\s+(?:\S+\s+)*test(?:\s|$)", command))


def _cargo_error_excerpt(command: str, output: str) -> str:
    if _is_cargo_test_command(command):
        return cargo_test_failure_excerpt(output, limit=_CARGO_ERROR_LIMIT)
    return tool_error_excerpt(output)


def _manifest_declares_workspace(path: Path) -> bool:
    try:
        return any(_CARGO_WORKSPACE_HEADER_RE.match(line) for line in path.read_text().splitlines())
    except OSError:
        return False


def _cargo_workspace_root(root: Path) -> Path:
    current = root.resolve()
    for candidate in (current, *current.parents):
        manifest = candidate / "Cargo.toml"
        if manifest.exists() and _manifest_declares_workspace(manifest):
            return candidate
    return current


def _default_sync_command(root: Path) -> str:
    if (_cargo_workspace_root(root) / "Cargo.lock").exists():
        return _DEFAULT_SYNC_COMMAND
    return "cargo fetch"


def _cargo_lockfile_needs_update(output: str) -> bool:
    return bool(_CARGO_LOCK_NEEDS_UPDATE_RE.search(output))


def _rust_raw_string_end(line: str, start: int) -> int | None:
    if line[start] != "r":
        return None
    index = start + 1
    hashes = 0
    while index < len(line) and line[index] == "#":
        hashes += 1
        index += 1
    if index >= len(line) or line[index] != '"':
        return None
    terminator = '"' + ("#" * hashes)
    end = line.find(terminator, index + 1)
    return len(line) if end == -1 else end + len(terminator)


def _rust_brace_delta(line: str, in_block_comment: bool) -> tuple[int, bool]:
    delta = 0
    index = 0
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end == -1:
                return delta, True
            in_block_comment = False
            index = end + 2
            continue

        if line.startswith("//", index):
            break
        if line.startswith("/*", index):
            in_block_comment = True
            index += 2
            continue

        raw_end = _rust_raw_string_end(line, index)
        if raw_end is not None:
            index = raw_end
            continue

        ch = line[index]
        if ch == '"':
            index += 1
            escaped = False
            while index < len(line):
                current = line[index]
                if current == '"' and not escaped:
                    index += 1
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                index += 1
            continue
        if ch == "'":
            if index + 2 < len(line) and line[index + 2] == "'":
                index += 3
                continue
            if index + 3 < len(line) and line[index + 1] == "\\" and line[index + 3] == "'":
                index += 4
                continue
        if ch == "{":
            delta += 1
        elif ch == "}":
            delta -= 1
        index += 1

    return delta, in_block_comment


def _rust_code_line(line: str, in_block_comment: bool) -> tuple[str, bool]:
    code: list[str] = []
    index = 0
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end == -1:
                return "".join(code), True
            in_block_comment = False
            index = end + 2
            continue

        if line.startswith("//", index):
            break
        if line.startswith("/*", index):
            in_block_comment = True
            index += 2
            continue

        raw_end = _rust_raw_string_end(line, index)
        if raw_end is not None:
            code.append(" ")
            index = raw_end
            continue

        ch = line[index]
        if ch == '"':
            code.append(" ")
            index += 1
            escaped = False
            while index < len(line):
                current = line[index]
                if current == '"' and not escaped:
                    index += 1
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                index += 1
            continue
        if ch == "'":
            if index + 2 < len(line) and line[index + 2] == "'":
                code.append(" ")
                index += 3
                continue
            if index + 3 < len(line) and line[index + 1] == "\\" and line[index + 3] == "'":
                code.append(" ")
                index += 4
                continue

        code.append(ch)
        index += 1

    return "".join(code), in_block_comment


def _rust_is_cfg_test_attr(code_line: str) -> bool:
    stripped = code_line.strip()
    return stripped.startswith("#") and bool(_RUST_CFG_TEST_RE.match(stripped))


def _rust_test_module_ranges(content: str) -> list[tuple[int, int]]:
    lines = content.splitlines()
    deltas: list[int] = []
    in_block_comment = False
    for line in lines:
        delta, in_block_comment = _rust_brace_delta(line, in_block_comment)
        deltas.append(delta)

    ranges: list[tuple[int, int]] = []
    pending_cfg_test = False
    in_block_comment = False
    index = 0
    while index < len(lines):
        line = lines[index]
        code_line, in_block_comment = _rust_code_line(line, in_block_comment)
        stripped = code_line.strip()
        line_number = index + 1

        if _rust_is_cfg_test_attr(code_line):
            pending_cfg_test = True
            index += 1
            continue

        if pending_cfg_test:
            if not stripped or stripped.startswith("#["):
                index += 1
                continue
            if _RUST_TEST_MODULE_RE.search(code_line) and "{" in code_line:
                depth = deltas[index]
                end_index = index + 1
                while depth > 0 and end_index < len(lines):
                    depth += deltas[end_index]
                    end_index += 1
                if depth == 0:
                    ranges.append((line_number, end_index))
                    index = end_index
                    pending_cfg_test = False
                    continue
            pending_cfg_test = False

        index += 1

    return ranges


def _parse_unified_hunks(diff_lines: list[str]) -> list[tuple[int, int, int, int]]:
    hunks: list[tuple[int, int, int, int]] = []
    for line in diff_lines:
        match = _RUST_HUNK_RE.match(line)
        if not match:
            continue
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        hunks.append(
            (
                int(match.group("old_start")),
                old_count,
                int(match.group("new_start")),
                new_count,
            )
        )
    return hunks


def _range_within_ranges(start: int, count: int, ranges: list[tuple[int, int]]) -> bool:
    if count <= 0:
        return any(range_start <= start <= range_end for range_start, range_end in ranges)
    end = start + count - 1
    return any(start >= range_start and end <= range_end for range_start, range_end in ranges)


class CargoTool(BuildTool):
    """Rust/Cargo build tool implementation."""

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        sync_command: str | None = None,
        compile_command: str = _DEFAULT_COMPILE_COMMAND,
        test_command: str = _DEFAULT_TEST_COMMAND,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        self._sync_command_is_default = sync_command is None
        self._sync_command = sync_command or _default_sync_command(self._root)
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
                return ToolResult(success=False, output=output, error=_cargo_error_excerpt(command, output))
            return ToolResult(success=True, output=output)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Command timed out: {command}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def sync(self) -> ToolResult:
        result = self._run(self._sync_command)
        if (
            not result.success
            and self._sync_command_is_default
            and self._sync_command == _DEFAULT_SYNC_COMMAND
            and _cargo_lockfile_needs_update(result.output or result.error)
        ):
            log.info("Cargo lockfile needs an update during locked sync; retrying with cargo fetch")
            retry = self._run("cargo fetch")
            retry.metadata["sync_retry"] = {
                "reason": "cargo_lockfile_needs_update",
                "initial_command": self._sync_command,
                "initial_status": "failed",
                "initial_error_excerpt": tool_error_excerpt(result.error or result.output, limit=1000),
                "retry_command": "cargo fetch",
                "retry_status": "success" if retry.success else "failed",
            }
            return retry
        return result

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

    def is_sync_adoptable_file(self, path: str) -> bool:
        return Path(path).name == "Cargo.lock"

    def is_test_only_change(self, path: str, before: str | None, after: str | None) -> bool:
        if Path(path).suffix != ".rs" or before is None or after is None or before == after:
            return False

        before_ranges = _rust_test_module_ranges(before)
        if not before_ranges:
            return False
        after_ranges = _rust_test_module_ranges(after)
        if not after_ranges:
            return False

        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=0,
                lineterm="",
            )
        )
        hunks = _parse_unified_hunks(diff_lines)
        if not hunks:
            return False

        for old_start, old_count, new_start, new_count in hunks:
            if not _range_within_ranges(old_start, old_count, before_ranges):
                return False
            if not _range_within_ranges(new_start, new_count, after_ranges):
                return False

        return True
