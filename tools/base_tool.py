"""Base tool and sandbox enforcement.

Sandbox is shared across all tool instances and enforces the whitelist from config.
BaseTool subclasses must call sandbox.check_read() / check_write() before any I/O.

BuildTool is the abstract interface for platform-specific build systems.
Implement it for each platform (AndroidGradleTool for Android, NodeTool for Node.js, XcodeTool for iOS, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.diagnostics import diagnostic_excerpt


_TOOL_ERROR_LIMIT = 8000


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def tool_error_excerpt(output: str, limit: int = _TOOL_ERROR_LIMIT) -> str:
    return diagnostic_excerpt(output, limit=limit)


class Sandbox:
    """Validates filesystem paths against the configured whitelist."""

    def __init__(
        self,
        project_root: Path,
        allowed_write_paths: list[str],
        allowed_read_paths: list[str],
    ) -> None:
        self._root = project_root.resolve()
        self._write = [self._resolve(p) for p in allowed_write_paths]
        self._read = [self._resolve(p) for p in allowed_read_paths]

    def _resolve(self, relative: str) -> Path:
        return (self._root / relative).resolve()

    def _under_any(self, path: Path, roots: list[Path]) -> bool:
        resolved = path.resolve() if path.is_absolute() else (self._root / path).resolve()
        return any(resolved == root or root in resolved.parents for root in roots)

    def check_read(self, path: Path) -> None:
        if not self._under_any(path, self._read):
            raise PermissionError(f"Sandbox read denied: {path}")

    def check_write(self, path: Path) -> None:
        if not self._under_any(path, self._write):
            raise PermissionError(f"Sandbox write denied: {path}\nAllowed: {[str(p) for p in self._write]}")


class BaseTool:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError


class BuildTool(BaseTool):
    """Abstract interface for platform build systems.

    Implement one subclass per platform and register it as the "build" tool
    in Orchestrator.__init__(). The orchestrator loop calls only these core
    methods — everything else is platform-specific extras on the subclass.

    To add a new platform:
      1. Create tools/<platform>_tool.py — subclass BuildTool (or GradleBaseTool for
         Gradle variants); implement sync(), compile_check(), run_tests(), run_check(),
         is_build_config_file(); optionally override generate_sources() and env_files().
      2. Register it in core/orchestrator.py (_build_tool()) and sikula.py
         (_build_tool_class(), _generate_config(), _SUPPORTED_BUILD_TOOLS).
      3. Add detection logic to tools/scanner.py (_SIGNATURES and path detection).
      4. Add a .sikula/config.yaml in the project directory.
    """

    def generate_sources(self) -> ToolResult:
        """Generate all build-time sources before the analyst reads the codebase.

        Called by the presync phase (run_presync: true). Should run only source
        generation tasks — not compilation. The goal is to make generated types
        (DTOs, query models, …) readable by the analyst without triggering a full
        build that might fail on pre-existing errors in unrelated modules.

        Default implementation delegates to sync(). Override when the platform's
        sync() is too broad (e.g. triggers compilation) and a more targeted
        source-generation command is available.

        Android/Gradle: configurable via build.presync_task (default: generateDebugSources;
                        use openApiGenerateAll for projects with pre-existing compile errors)
        iOS/Xcode:      override to run codegen tools (Apollo, Sourcery, SwiftGen, …) if needed;
                        default sync() (SPM dependency resolution) is often sufficient
        Maven:          override to run mvn generate-sources if needed; default sync() is fine
        """
        return self.sync()

    def sync(self) -> ToolResult:
        """Resolve dependencies and generate sources before the first build.

        Android/Gradle: generateDebugSources --parallel
        iOS/Xcode:      xcodebuild -resolvePackageDependencies
        Maven:          mvn dependency:resolve
        """
        raise NotImplementedError

    def compile_check(self) -> ToolResult:
        """Fast compile/type-check without full assembly.

        Android/Gradle: compileDebugKotlin
        iOS/Xcode:      xcodebuild build (or swift build)
        Maven:          mvn compile
        """
        raise NotImplementedError

    def run_tests(self) -> ToolResult:
        """Run the project unit test suite.

        Android/Gradle: testDebugUnitTest
        iOS/Xcode:      xcodebuild test
        Maven:          mvn test
        """
        raise NotImplementedError

    def is_build_config_file(self, path: str) -> bool:
        """Return True if the file is part of the build configuration.

        Used by the orchestrator to decide whether to re-sync after a fix.
        Each platform subclass defines its own patterns.
        """
        raise NotImplementedError

    def is_sync_adoptable_file(self, path: str) -> bool:
        """Return True if sync may intentionally update this source-controlled path.

        Sync-adoptable files are generated or resolved by dependency/build tooling
        but are still expected to be part of the final branch diff, such as lockfiles
        or dependency verification metadata. The orchestrator owns adoption and
        review invalidation; platform tools only classify paths.
        """
        return False

    def is_test_only_change(self, path: str, before: str | None, after: str | None) -> bool:
        """Return True when a production-looking path contains only test-only edits.

        This hook is used only as a narrow exception to the fixer's test-failure
        production-write guard. The default is conservative: platforms must opt in
        with syntax-aware logic for mixed source/test files.
        """
        return False

    @staticmethod
    def env_files() -> list[str]:
        """Filenames of gitignored files that must be present for the build.

        Copied from the original project root to each new worktree after creation.
        Override in platform subclasses to declare platform-specific files.

        Android/Gradle: local.properties (SDK location)
        iOS/Xcode:      override if needed (e.g. .env, Secrets.xcconfig)
        """
        return []

    def run_check(self, name: str, task_config: dict) -> ToolResult:
        """Run a named quality check (lint, format check, …).

        task_config keys:
          command  — shell command to run (required; falls back to name if absent)
          timeout  — seconds before the command is killed (optional; subclass default applies)

        Subclasses provide a concrete implementation that executes the command as a
        shell process. The command is always a plain shell string — use the full
        invocation, e.g. "./gradlew lintDebug" or "python3 -m ruff check .".
        """
        raise NotImplementedError
