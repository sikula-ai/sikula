from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from tools.base_tool import BuildTool, Sandbox, ToolResult, tool_error_excerpt

log = logging.getLogger(__name__)

_PACKAGE_MANAGER_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)

_BUILD_CONFIG_FILES = frozenset(
    {
        ".npmrc",
        ".pnp.cjs",
        ".pnp.loader.mjs",
        ".yarnrc",
        ".yarnrc.yml",
        "angular.json",
        "bun.lock",
        "bun.lockb",
        "jsconfig.json",
        "lerna.json",
        "nest-cli.json",
        "nx.json",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "rush.json",
        "turbo.json",
        "yarn.lock",
    }
)
_BUILD_CONFIG_PREFIXES = (
    "astro.config.",
    "babel.config.",
    "eslint.config.",
    "jest.config.",
    "next.config.",
    "nuxt.config.",
    "playwright.config.",
    "postcss.config.",
    "prettier.config.",
    "remix.config.",
    "rollup.config.",
    "svelte.config.",
    "tailwind.config.",
    "tsconfig.",
    "tsup.config.",
    "vite.config.",
    "vitest.config.",
    "vue.config.",
    "webpack.config.",
)
_BUILD_CONFIG_DIRS = (".yarn/", "patches/")

_DEFAULT_SYNC_TIMEOUT = 600
_DEFAULT_COMPILE_TIMEOUT = 600
_DEFAULT_TEST_TIMEOUT = 600

_COMPILE_SCRIPT_CANDIDATES = ("typecheck", "type-check", "check-types", "check", "build")
_FORMAT_CHECK_SCRIPT_CANDIDATES = ("format:check", "format-check", "prettier:check")
_FORMAT_FIX_SCRIPT_CANDIDATES = ("format", "format:write", "prettier:write", "prettier")


def _read_package_json(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_node_package_scripts(root: Path) -> dict[str, str]:
    scripts = _read_package_json(root).get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(command) for name, command in scripts.items()}


def detect_node_package_manager(root: Path, configured: str | None = None) -> str:
    if configured in {"npm", "pnpm", "yarn", "bun"}:
        return configured

    package_manager_field = _read_package_json(root).get("packageManager")
    if isinstance(package_manager_field, str) and "@" in package_manager_field:
        package_manager = package_manager_field.split("@", 1)[0]
        if package_manager in {"npm", "pnpm", "yarn", "bun"}:
            return package_manager

    for lockfile, package_manager in _PACKAGE_MANAGER_LOCKFILES:
        if (root / lockfile).exists():
            return package_manager

    return "npm"


def detect_node_language(root: Path) -> str:
    package_json = _read_package_json(root)
    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = package_json.get(key, {})
        if isinstance(value, dict):
            dependencies.update(value)

    if "typescript" in dependencies or any((root / name).exists() for name in ("tsconfig.json", "tsconfig.base.json")):
        return "TypeScript"

    source_roots = ("src", "app", "pages", "components", "lib", "server", "client", "packages", "apps")
    for source_root in source_roots:
        path = root / source_root
        if path.is_dir() and any(p.suffix in {".ts", ".tsx", ".mts", ".cts"} for p in path.rglob("*")):
            return "TypeScript"

    return "JavaScript"


def node_script_command(package_manager: str, script: str) -> str:
    if package_manager == "npm":
        return f"npm run {script}"
    if package_manager in {"pnpm", "yarn"}:
        return f"{package_manager} {script}"
    if package_manager == "bun":
        return f"bun run {script}"
    return f"{package_manager} run {script}"


def node_test_command(package_manager: str) -> str:
    if package_manager == "npm":
        return "npm test"
    if package_manager in {"pnpm", "yarn"}:
        return f"{package_manager} test"
    if package_manager == "bun":
        return "bun run test"
    return f"{package_manager} test"


def node_tsc_command(package_manager: str) -> str:
    if package_manager == "npm":
        return "npx tsc --noEmit"
    if package_manager == "pnpm":
        return "pnpm exec tsc --noEmit"
    if package_manager == "yarn":
        return "yarn tsc --noEmit"
    if package_manager == "bun":
        return "bunx tsc --noEmit"
    return "npx tsc --noEmit"


def default_node_sync_command(root: Path, package_manager: str) -> str:
    if package_manager == "npm":
        if (root / "package-lock.json").exists() or (root / "npm-shrinkwrap.json").exists():
            return "npm ci"
        return "npm install"
    if package_manager == "pnpm":
        return "pnpm install --frozen-lockfile" if (root / "pnpm-lock.yaml").exists() else "pnpm install"
    if package_manager == "yarn":
        return "yarn install --frozen-lockfile" if (root / "yarn.lock").exists() else "yarn install"
    if package_manager == "bun":
        if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
            return "bun install --frozen-lockfile"
        return "bun install"
    return f"{package_manager} install"


def default_node_compile_command(root: Path, package_manager: str, scripts: dict[str, str]) -> str:
    for script in _COMPILE_SCRIPT_CANDIDATES:
        if script in scripts:
            return node_script_command(package_manager, script)
    if any((root / name).exists() for name in ("tsconfig.json", "tsconfig.base.json")):
        return node_tsc_command(package_manager)
    return node_script_command(package_manager, "build")


def default_node_test_command(package_manager: str, scripts: dict[str, str]) -> str:
    if "test" in scripts:
        return node_test_command(package_manager)
    return node_test_command(package_manager)


def default_node_checks(package_manager: str, scripts: dict[str, str]) -> list[dict[str, str | int]]:
    checks: list[dict[str, str | int]] = []
    if "lint" in scripts:
        checks.append({"name": "lint", "command": node_script_command(package_manager, "lint"), "timeout": 120})

    format_check = next((script for script in _FORMAT_CHECK_SCRIPT_CANDIDATES if script in scripts), "")
    if format_check:
        check: dict[str, str | int] = {
            "name": "format",
            "command": node_script_command(package_manager, format_check),
            "timeout": 120,
        }
        format_fix = next((script for script in _FORMAT_FIX_SCRIPT_CANDIDATES if script in scripts), "")
        if format_fix:
            check["fix_command"] = node_script_command(package_manager, format_fix)
        checks.append(check)

    return checks


class NodeTool(BuildTool):
    """Node.js / TypeScript / JavaScript build tool implementation."""

    def __init__(
        self,
        sandbox: Sandbox,
        project_root: Path,
        package_manager: str | None = None,
        sync_command: str | None = None,
        compile_command: str | None = None,
        test_command: str | None = None,
        sync_timeout: int = _DEFAULT_SYNC_TIMEOUT,
        compile_timeout: int = _DEFAULT_COMPILE_TIMEOUT,
        test_timeout: int = _DEFAULT_TEST_TIMEOUT,
    ) -> None:
        super().__init__(sandbox)
        self._root = project_root.resolve()
        self._package_manager = detect_node_package_manager(self._root, package_manager)
        self._scripts = read_node_package_scripts(self._root)
        self._sync_timeout = sync_timeout
        self._compile_timeout = compile_timeout
        self._test_timeout = test_timeout
        self._sync_command = sync_command or default_node_sync_command(self._root, self._package_manager)
        self._compile_command = compile_command or default_node_compile_command(
            self._root, self._package_manager, self._scripts
        )
        self._test_command = test_command or default_node_test_command(self._package_manager, self._scripts)

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
        name = Path(p).name
        return (
            name in _BUILD_CONFIG_FILES
            or name.startswith(_BUILD_CONFIG_PREFIXES)
            or any(directory in p for directory in _BUILD_CONFIG_DIRS)
        )
