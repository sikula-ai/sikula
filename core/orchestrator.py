"""Main orchestration loop.

OrchestratorConfig is built from project.yaml in sikula.py.
To add a new agent (e.g. ReviewerAgent), register it in _agents and call it in _loop().

Loop phases:
  0. presync       — (run_presync only) BuildTool.generate_sources() before analyze; ensures
                     build-generated sources (OpenAPI DTOs, …) exist; failure is non-fatal
  1. analyze       — runs once, produces implementation_prompt
  1.5 plan         — (run_planner only) breaks prompt into steps; populates state.plan
  2. implement     — runs per step (or once if no plan); state.step_implemented guards re-runs
  3. review loop   — (run_review only) reviewer + implementer fix until approved
  3.5 security review — (run_security_review only) security reviewer; blocking issues go back
                        to implementer + review loop; warnings logged, pipeline continues
  4. test write    — (run_test_writing only) TestWriterAgent writes/updates unit tests
  5. build/fix loop (run_build only):
       sync   — BuildTool.sync(); runs before first build and after any fix
                that touches a build-config file (detected by BuildTool.is_build_config_file())
       build  — BuildTool.compile_check()
       test   — BuildTool.run_tests() (run_tests only); runs after a passing build
       checks — BuildTool.run_check() per entry in build.checks (run_checks only); runs after tests
       fix    — FixerAgent; runs after build, test, or check failure; triggers re-sync if
                build-config files changed; marks review/security/test writing stale if files
                changed. Semantic gates run after deterministic validation is green.

If state.plan is populated (by PlannerAgent), phases 2-4 run once per step before advancing.
After the last step, review/security/test-writing rerun once in final full-task scope so the
complete diff is checked against the original task. Phase 5 runs per step only when
run_build_per_step is true; a final full-task build/fix loop still runs after all planned
steps complete. If state.plan is empty, a single pass through phases 2-5 is used.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
import os
import posixpath
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents.analyst_agent import AnalystAgent
from agents.fixer_agent import FixerAgent
from agents.implementer_agent import ImplementerAgent
from agents.planner_agent import PlannerAgent
from agents.reviewer_agent import ReviewerAgent
from agents.security_reviewer_agent import SecurityReviewerAgent
from agents.test_writer_agent import TestWriterAgent
from core.diagnostics import diagnostic_excerpt
from core.llm_client import LLMClient
from core.progress import ActiveOperationHeartbeat
from core.retry_history import llm_retry_history
from core.state import StateStore, TaskState
from core.validation_artifacts import (
    detect_validation_artifacts,
    restore_validation_artifacts,
    snapshot_validation_dirty_files,
)
from core.validation_coverage import INTERNAL_PIPELINE_CONFIG_KEY, validation_coverage_gaps
from tools.base_tool import BuildTool, Sandbox
from tools.file_tool import FileTool
from tools.git_tool import GitTool
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool

log = logging.getLogger(__name__)

_SCOPE_FINAL_FULL_TASK = "final_full_task"
_FIXER_ERROR_LIMIT = 6000
_LOG_ERROR_LIMIT = 2000
_VALIDATION_ARTIFACT_ERROR_LIMIT = 2000
_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES = 5
_TEST_PATH_MARKERS = {
    "__tests__",
    "androidtest",
    "spec",
    "specs",
    "test",
    "testfixtures",
    "tests",
    "unittest",
    "unittests",
}
_TEST_FILE_PREFIXES = ("test_", "test-")
_TEST_FILE_SUFFIXES = (
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    "_spec.py",
    "_test.py",
    "_tests.py",
    "Spec.java",
    "Spec.kt",
    "Spec.swift",
    "Test.java",
    "Test.kt",
    "Test.swift",
    "Tests.java",
    "Tests.kt",
    "Tests.swift",
)


def _phase_scope_label(state: TaskState) -> str:
    return "final full-task " if state.active_scope == _SCOPE_FINAL_FULL_TASK else ""


def _build_loop_key(state: TaskState) -> str:
    if state.active_scope == _SCOPE_FINAL_FULL_TASK or state.plan_completed:
        return _SCOPE_FINAL_FULL_TASK
    if state.plan:
        return f"step:{state.current_step}"
    return "task"


def _build_loop_attempts_used(state: TaskState) -> int:
    return max(0, state.build_iterations - state.build_loop_start_iteration)


def _build_loop_active_for_current_scope(state: TaskState) -> bool:
    return bool(state.build_loop_key) and state.build_loop_key == _build_loop_key(state)


def _build_loop_can_validate(state: TaskState, max_iterations: int) -> bool:
    attempts_used = _build_loop_attempts_used(state)
    return attempts_used < max_iterations or (attempts_used == max_iterations and state.fixer_changed_code)


def _normalize_artifact_path(path: str) -> str:
    return path.replace("\\", "/").strip().strip("/")


def _path_matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = _normalize_artifact_path(path)
    raw_pattern = pattern.replace("\\", "/").strip()
    directory_pattern = raw_pattern.endswith("/")
    normalized_pattern = raw_pattern.strip("/")
    if not normalized_path or not normalized_pattern:
        return False
    if directory_pattern:
        return normalized_path == normalized_pattern or normalized_path.startswith(f"{normalized_pattern}/")
    return normalized_path == normalized_pattern or fnmatch.fnmatch(normalized_path, normalized_pattern)


def _normalize_project_path(path: str) -> str:
    normalized = posixpath.normpath(str(path).replace("\\", "/").strip())
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return ""
    return normalized.strip("/")


def _path_is_under_root(path: str, root: str) -> bool:
    normalized_path = _normalize_project_path(path)
    normalized_root = _normalize_project_path(root)
    if not normalized_path or not normalized_root:
        return False
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _path_parts(path: str) -> list[str]:
    normalized = _normalize_project_path(path)
    return [part for part in normalized.split("/") if part]


def _is_test_path_marker(part: str) -> bool:
    lower_part = part.lower()
    return lower_part in _TEST_PATH_MARKERS or lower_part.endswith(
        ("test", "tests", "_test", "_tests", "-test", "-tests")
    )


def _path_looks_like_test_artifact(path: str) -> bool:
    parts = _path_parts(path)
    if any(_is_test_path_marker(part) for part in parts[:-1]):
        return True
    if not parts:
        return False
    filename = parts[-1]
    lower_filename = filename.lower()
    return (
        lower_filename.startswith(_TEST_FILE_PREFIXES)
        or lower_filename.endswith(tuple(suffix.lower() for suffix in _TEST_FILE_SUFFIXES if suffix.startswith((".", "_"))))
        or filename.endswith(tuple(suffix for suffix in _TEST_FILE_SUFFIXES if suffix[0].isupper()))
    )


@dataclass
class OrchestratorConfig:
    project_root: Path
    max_iterations: int = 10
    max_review_iterations: int = 3
    max_security_review_iterations: int = 3
    allowed_write_paths: list[str] = None  # type: ignore[assignment]
    allowed_read_paths: list[str] = None  # type: ignore[assignment]
    run_presync: bool = False
    run_build: bool = True
    run_build_per_step: bool = False
    run_test_writing: bool = True
    run_tests: bool = True
    run_review: bool = True
    run_security_review: bool = True
    run_checks: bool = True
    run_planner: bool = True
    heartbeat_interval_seconds: int = 60
    project_config: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.allowed_write_paths is None:
            self.allowed_write_paths = []
        if self.allowed_read_paths is None:
            self.allowed_read_paths = ["."]
        if self.project_config is None:
            self.project_config = {}


def _build_tool(sandbox: Sandbox, root: Path, project_config: dict) -> BuildTool:
    platform = project_config.get("project", {}).get("build_tool", "gradle-android")
    build = project_config.get("build", {})
    if platform == "python":
        return PythonTool(
            sandbox,
            root,
            compile_command=build.get("compile_command", "ruff check ."),
            test_command=build.get("test_command", "pytest"),
            timeout=build.get("timeout", 300),
        )
    if platform == "cargo":
        return CargoTool(
            sandbox,
            root,
            sync_command=build.get("sync_command"),
            compile_command=build.get("compile_command", "cargo check"),
            test_command=build.get("test_command", "cargo test"),
            timeout=build.get("timeout", 600),
        )
    if platform == "node":
        timeout = build.get("timeout")
        return NodeTool(
            sandbox,
            root,
            package_manager=build.get("package_manager"),
            sync_command=build.get("sync_command"),
            compile_command=build.get("compile_command"),
            test_command=build.get("test_command"),
            sync_timeout=build.get("sync_timeout", timeout or 600),
            compile_timeout=build.get("compile_timeout", timeout or 600),
            test_timeout=build.get("test_timeout", timeout or 600),
        )
    if platform == "xcodebuild":
        from tools.xcode_tool import XcodeTool

        return XcodeTool(
            sandbox,
            root,
            scheme=build.get("scheme", "Countries"),
            destination=build.get("destination", "generic/platform=iOS Simulator"),
            test_destination=build.get("test_destination", "platform=iOS Simulator,OS=latest,name=iPhone 16"),
            compile_timeout=build.get("compile_timeout", 1800),
            test_timeout=build.get("test_timeout", 1800),
        )
    if platform == "gradle-jvm":
        from tools.gradle_jvm_tool import JvmGradleTool

        return JvmGradleTool(
            sandbox,
            root,
            compile_task=build.get("compile_task", "classes"),
            test_task=build.get("test_task", "test"),
            sync_task=build.get("sync_task", "classes"),
            presync_task=build.get("presync_task", "classes"),
            presync_clean=bool(build.get("presync_clean", False)),
            sync_timeout=build.get("sync_timeout", 600),
            compile_timeout=build.get("compile_timeout", 600),
            test_timeout=build.get("test_timeout", 600),
        )
    if platform == "maven":
        from tools.maven_tool import MavenTool

        return MavenTool(
            sandbox,
            root,
            compile_command=build.get("compile_command"),
            test_command=build.get("test_command"),
            sync_command=build.get("sync_command"),
            presync_command=build.get("presync_command"),
            presync_clean=bool(build.get("presync_clean", False)),
            sync_timeout=build.get("sync_timeout", 300),
            compile_timeout=build.get("compile_timeout", 600),
            test_timeout=build.get("test_timeout", 600),
        )
    # gradle-android (default)
    return AndroidGradleTool(
        sandbox,
        root,
        compile_task=build.get("compile_task", "compileDebugKotlin"),
        test_task=build.get("test_task", "testDebugUnitTest"),
        presync_task=build.get("presync_task", "generateDebugSources"),
        presync_clean=bool(build.get("presync_clean", False)),
        sync_timeout=build.get("sync_timeout", 1800),
        compile_timeout=build.get("compile_timeout", 1800),
        test_timeout=build.get("test_timeout", 1800),
    )


class Orchestrator:
    def __init__(
        self,
        config: OrchestratorConfig,
        llm: LLMClient,
        state_store: StateStore,
        agent_llms: dict[str, LLMClient] | None = None,
        config_snapshot: dict | None = None,
    ) -> None:
        self._config = config
        self._store = state_store
        self._config_snapshot = config_snapshot or {}

        sandbox = Sandbox(
            project_root=config.project_root,
            allowed_write_paths=config.allowed_write_paths,
            allowed_read_paths=config.allowed_read_paths,
        )
        root = config.project_root

        self._tools = {
            "file": FileTool(sandbox, root),
            "git": GitTool(sandbox, root),
            # "build" must implement BuildTool — selected by project.build_tool in config YAML.
            # Add new platforms here alongside their BuildTool subclass.
            "build": _build_tool(sandbox, root, config.project_config),
        }

        pc = copy.deepcopy(config.project_config)
        pc[INTERNAL_PIPELINE_CONFIG_KEY] = {
            "run_build": config.run_build,
            "run_tests": config.run_tests,
            "run_checks": config.run_checks,
        }
        self._agent_project_config = pc
        _llm = lambda name: (agent_llms or {}).get(name, llm)  # noqa: E731

        self._session_code_changed = False
        self._reviewer_ran_this_session = False

        # Agent registry — add new agents here
        self._agents = {
            "analyst": AnalystAgent(_llm("analyst"), self._tools, pc),
            "planner": PlannerAgent(_llm("planner"), self._tools, pc),
            "implementer": ImplementerAgent(_llm("implementer"), self._tools, pc),
            "reviewer": ReviewerAgent(_llm("reviewer"), self._tools, pc),
            "security_reviewer": SecurityReviewerAgent(_llm("security_reviewer"), self._tools, pc),
            "test_writer": TestWriterAgent(_llm("test_writer"), self._tools, pc),
            "fixer": FixerAgent(_llm("fixer"), self._tools, pc),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        task_id: Optional[str] = None,
        task_description: Optional[str] = None,
        label: Optional[str] = None,
    ) -> TaskState:
        if task_id:
            state = self._store.load(task_id)
            if state is None:
                raise ValueError(f"Task not found: {task_id}")
        elif task_description:
            state = self._store.create(task_description)
        else:
            raise ValueError("Provide task_id or task_description")

        display = label or state.task_description.splitlines()[0][:60]
        log.info("Task %s — %s", state.task_id, display)
        state.clear_active_operation()
        state.pid = os.getpid()
        self._store.save(state)
        self._loop(state)
        return state

    # ------------------------------------------------------------------
    # Top-level loop
    # ------------------------------------------------------------------

    def _loop(self, state: TaskState) -> None:
        if state.done or state.failed:
            if state.failed:
                log.info(
                    "Task %s already in terminal state (failed) — use --reset-failed to retry",
                    state.task_id,
                )
                return
            log.info(
                "Task %s already in terminal state (%s) — nothing to do",
                state.task_id,
                "done",
            )
            return

        # Capture effective config on first run; never overwritten on resume so the
        # original run settings are always visible in the state JSON.
        if not state.config_snapshot and self._config_snapshot:
            state.config_snapshot = self._config_snapshot
            self._store.save(state)

        if self._abort_on_validation_coverage_gaps(state):
            return

        # Phase 0: presync — generate sources before analyze (run_presync: true only)
        # Skipped on resume if already attempted. Failure is a warning, not an abort —
        # analyst proceeds with whatever is in build/ from prior builds.
        if self._config.run_presync and not state.presync_done:
            self._run_presync(state)

        # Phase 1: analyze (idempotent — skipped if prompt already exists)
        if not state.implementation_prompt:
            log.info("--- Phase: analyze ---")
            result = self._run_agent("analyst", state)
            if state.failed or not result.success:
                state.failed = True
                self._store.save(state)
                return

        # Phase 1.5: plan (skipped if planner already ran — plan_decided guards resume)
        if self._config.run_planner and not state.plan_decided:
            log.info("--- Phase: plan ---")
            result = self._run_agent("planner", state)
            if state.failed or not result.success:
                state.failed = True
                self._store.save(state)
                return

        # Phases 2-5: step-by-step or single-pass
        if state.plan:
            self._run_step_loop(state)
        else:
            self._run_single_pass(state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _abort_on_validation_coverage_gaps(self, state: TaskState) -> bool:
        if state.review_mode in {"review_report", "review_fix"}:
            return False

        gaps = validation_coverage_gaps(self._agent_project_config, state)
        if not gaps:
            return False

        commands = ", ".join(f"`{command}`" for command in gaps)
        msg = (
            "validation coverage gap: task-described validation command(s) are not covered "
            f"by the effective configured pipeline: {commands}. Update the Sikula config file "
            "used for this run (default .sikula/config.yaml, or the file passed with --config) "
            "or the task description and rerun; this cannot be fixed inside the current task "
            "worktree because the orchestrator already loaded the effective pipeline config."
        )
        log.error(msg)
        state.record("orchestrator", "abort", msg)
        state.record_validation("validation_coverage", "failed", error=msg)
        state.failed = True
        self._store.save(state)
        return True

    def _worktree_dirty_files(self, state: TaskState) -> list[str]:
        """Return project-root-relative paths of uncommitted changes in the worktree.

        Uses worktree_base (git root) so paths match those produced by FileTool.
        Falls back to worktree_path if worktree_base is not set (older state files).
        Returns [] when running without isolation (no worktree_path).
        """
        git_cwd = state.worktree_base or state.worktree_path
        if not git_cwd:
            return []
        project_root = Path(state.worktree_path) if state.worktree_path else Path(git_cwd)
        git_root = Path(git_cwd)
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=git_cwd,
        )
        if r.returncode != 0:
            return []
        files = []
        for line in r.stdout.splitlines():
            if len(line) > 3:
                abs_path = git_root / line[3:].strip()
                try:
                    files.append(str(abs_path.relative_to(project_root)))
                except ValueError:
                    pass  # file outside project root — skip
        return files

    def _refresh_review_fix_diff(self, state: TaskState) -> bool:
        """Refresh PR-style diff for review-fix mode, including uncommitted worktree changes."""
        if state.review_mode != "review_fix":
            return True
        if not state.review_base_branch:
            msg = "review-fix task is missing review_base_branch; cannot refresh review diff safely"
            log.error(msg)
            state.record("orchestrator", "abort", msg)
            state.failed = True
            self._store.save(state)
            return False

        git_cwd = Path(state.worktree_base or self._config.project_root)
        merge_base = subprocess.run(
            ["git", "merge-base", state.review_base_branch, "HEAD"],
            capture_output=True,
            text=True,
            cwd=git_cwd,
        )
        if merge_base.returncode != 0:
            msg = f"failed to find merge-base for review diff: {merge_base.stderr.strip()}"
            log.error(msg)
            state.record("orchestrator", "abort", msg[:500])
            state.failed = True
            self._store.save(state)
            return False

        diff = subprocess.run(
            ["git", "diff", merge_base.stdout.strip()],
            capture_output=True,
            text=True,
            cwd=git_cwd,
        )
        if diff.returncode != 0:
            msg = f"failed to refresh review diff: {diff.stderr.strip()}"
            log.error(msg)
            state.record("orchestrator", "abort", msg[:500])
            state.failed = True
            self._store.save(state)
            return False

        state.review_diff = diff.stdout
        self._store.save(state)
        return True

    def _review_fix_review_test_changes(self, state: TaskState, test_files_changed: bool) -> None:
        if state.review_mode != "review_fix" or not test_files_changed:
            return

        if self._config.run_review:
            state.review_approved = False
            state.review_iterations = 0
            self._store.save(state)

            log.info("--- Phase: final review (test changes) ---")
            if not self._refresh_review_fix_diff(state):
                return
            self._run_agent("reviewer", state)
            self._reviewer_ran_this_session = True
            if state.failed:
                return
            if not state.review_approved:
                msg = "reviewer rejected test writer changes"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
                self._store.save(state)
                return

        if self._config.run_security_review:
            state.security_approved = False
            state.security_review_iterations = 0
            self._store.save(state)

            log.info("--- Phase: final security review (test changes) ---")
            if not self._refresh_review_fix_diff(state):
                return
            self._run_agent("security_reviewer", state)
            if state.failed:
                return
            if not state.security_approved:
                msg = "security reviewer rejected test writer changes"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
                self._store.save(state)

    # ------------------------------------------------------------------
    # Single-pass (no plan)
    # ------------------------------------------------------------------

    def _run_single_pass(self, state: TaskState) -> None:
        """Phases 2-5 as a single pass when no multi-step plan is in use."""
        # Phase 2: implement (idempotent — skipped if files already changed)
        has_preexisting_changes = bool(state.files_changed)
        if not state.files_changed:
            log.info("--- Phase: implement ---")
            self._run_agent("implementer", state)
            if state.failed:
                self._store.save(state)
                return
            if not state.files_changed:
                dirty = self._worktree_dirty_files(state)
                if dirty:
                    log.warning(
                        "Implementer made no new writes but worktree has %d uncommitted file(s) — adopting them",
                        len(dirty),
                    )
                    state.files_changed.extend(dirty)
                    state.record(
                        "orchestrator", "adopt_worktree_changes", f"{len(dirty)} file(s) adopted from worktree"
                    )
                else:
                    log.error("Implementer produced no file changes — aborting task")
                    state.record("orchestrator", "abort", "implementer produced no file changes")
                    state.failed = True
                    self._store.save(state)
                    return

        if self._config.run_build and (state.fixer_changed_code or _build_loop_active_for_current_scope(state)):
            self._run_build_fix_loop(state, set_done=True)
            return

        # Phase 3: review loop
        self._run_review_loop(state)
        if state.failed:
            self._store.save(state)
            return

        # Phase 3.5: security review + fix loop
        self._run_security_review_and_fix_loop(state)
        if state.failed:
            self._store.save(state)
            return

        # Skip test-write and build when: this is a review task (review_diff is set), files were
        # pre-existing (implementer was skipped), reviewer ran and approved in this session, and no
        # code was written during this run. Avoids re-running CI on an already-validated branch.
        if (
            state.review_diff
            and has_preexisting_changes
            and self._reviewer_ran_this_session
            and not self._session_code_changed
            and not state.fixer_changed_code
        ):
            state.test_status = "skipped"
            state.check_status = "skipped"
            state.done = True
            self._store.save(state)
            return

        # Phase 4: test write
        test_files_changed = self._run_test_write_phase(state)
        if state.failed:
            self._store.save(state)
            return
        self._review_fix_review_test_changes(state, test_files_changed)
        if state.failed:
            self._store.save(state)
            return

        # Phase 5: build/fix loop
        if not self._config.run_build:
            state.test_status = "skipped"
            state.check_status = "skipped"
            state.done = bool(state.files_changed)
            if not state.done:
                log.warning("Implementation produced no file changes")
            self._store.save(state)
            return

        self._run_build_fix_loop(state, set_done=True)

    # ------------------------------------------------------------------
    # Step loop (multi-step plan)
    # ------------------------------------------------------------------

    def _run_step_loop(self, state: TaskState) -> None:
        """Iterate over steps in state.plan, running the full implement→build cycle per step."""
        total_steps = len(state.plan)
        if state.plan_completed:
            self._run_after_plan_completed(state)
            return

        while state.current_step < total_steps and not state.failed:
            step_idx = state.current_step
            state.active_scope = None
            step_label = f"Step {step_idx + 1}/{total_steps}: {state.plan[step_idx]}"
            log.info("--- %s ---", step_label[:100])
            state.record("orchestrator", "step_start", step_label)

            ok = self._run_single_step(state, step_idx)
            if not ok or state.failed:
                self._store.save(state)
                return

            state.record("orchestrator", "step_done", f"Step {step_idx + 1}/{total_steps}")

            if step_idx < total_steps - 1:
                # Advance to next step; reset per-step flags
                state.current_step += 1
                state.step_implemented = False
                state.review_approved = False
                state.review_issues.clear()
                state.review_iterations = 0
                state.security_approved = False
                state.security_review_iterations = 0
                state.tests_up_to_date = False
                self._store.save(state)
            else:
                state.plan_completed = True
                state.final_full_task_review_done = False
                self._store.save(state)
                self._run_after_plan_completed(state)
                return  # last step complete — exit step loop regardless of outcome

    def _run_after_plan_completed(self, state: TaskState) -> None:
        """Run whole-task gates that apply after every planned step has completed."""
        if not state.files_changed:
            log.error("All steps were skipped — no file changes produced — task failed")
            state.record("orchestrator", "abort", "all steps skipped — no file changes")
            state.failed = True
            self._store.save(state)
            return

        if self._config.run_build and (state.fixer_changed_code or _build_loop_active_for_current_scope(state)):
            state.active_scope = _SCOPE_FINAL_FULL_TASK
            self._store.save(state)
            log.info("--- Phase: final build/fix (resume active loop) ---")
            self._run_build_fix_loop(state, set_done=True)
            return

        if not state.final_full_task_review_done:
            self._run_final_full_task_gate(state)
            if state.failed:
                self._store.save(state)
                return

        if not self._config.run_build:
            state.test_status = "skipped"
            state.check_status = "skipped"
            state.done = True
            self._store.save(state)
            return

        state.active_scope = _SCOPE_FINAL_FULL_TASK
        self._store.save(state)
        log.info("--- Phase: final build/fix (after all steps) ---")
        self._run_build_fix_loop(state, set_done=True)

    def _run_final_full_task_gate(self, state: TaskState) -> None:
        """Review/security/test the complete planned task before final validation."""
        state.active_scope = _SCOPE_FINAL_FULL_TASK
        log.info("--- Phase: final full-task gate ---")
        if self._config.run_review:
            state.review_approved = False
            state.review_issues.clear()
            state.review_iterations = 0
        if self._config.run_security_review:
            state.security_approved = False
            state.security_review_iterations = 0
        if self._config.run_test_writing:
            state.tests_up_to_date = False
        self._store.save(state)

        self._run_review_loop(state)
        if state.failed:
            return

        self._run_security_review_and_fix_loop(state)
        if state.failed:
            return

        self._run_test_write_phase(state)
        if state.failed:
            return

        state.final_full_task_review_done = True
        self._store.save(state)

    def _run_single_step(self, state: TaskState, step_idx: int) -> bool:
        """Run one step: implement → review → test write → build/fix. Returns True on success."""
        total_steps = len(state.plan)
        step_num = step_idx + 1

        # Implement this step (idempotent via step_implemented flag)
        if not state.step_implemented:
            log.info(f"--- Phase: implement (Step {step_num}/{total_steps}) ---")
            self._run_agent("implementer", state)
            if state.failed:
                return False
            if not state.files_changed:
                dirty = self._worktree_dirty_files(state)
                if dirty:
                    log.warning(
                        "Implementer made no new writes but worktree has %d uncommitted file(s) — adopting them",
                        len(dirty),
                    )
                    state.files_changed.extend(dirty)
                    state.record(
                        "orchestrator", "adopt_worktree_changes", f"{len(dirty)} file(s) adopted from worktree"
                    )
                else:
                    log.info(
                        "Implementer made no changes for step %d/%d — advancing to next step",
                        step_num,
                        total_steps,
                    )
                    state.record(
                        "orchestrator",
                        "step_skipped",
                        f"Step {step_num}/{total_steps}: no file changes",
                    )
                    state.step_implemented = True
                    self._store.save(state)
                    return True
            state.step_implemented = True
            self._store.save(state)

        if (
            self._config.run_build
            and self._config.run_build_per_step
            and (state.fixer_changed_code or _build_loop_active_for_current_scope(state))
        ):
            return self._run_build_fix_loop(state, set_done=False)

        # Review loop for this step
        self._run_review_loop(state)
        if state.failed:
            return False

        # Security review for this step
        self._run_security_review_and_fix_loop(state)
        if state.failed:
            return False

        # Test write for this step
        test_files_changed = self._run_test_write_phase(state)
        if state.failed:
            return False
        self._review_fix_review_test_changes(state, test_files_changed)
        if state.failed:
            return False

        # Build/fix for this step — only when run_build_per_step is explicitly enabled.
        # Default: build is deferred to after the last step (_run_step_loop handles it).
        if not self._config.run_build or not self._config.run_build_per_step:
            return True

        return self._run_build_fix_loop(state, set_done=False)

    # ------------------------------------------------------------------
    # Pre-analyze sync (optional — ensures generated sources exist)
    # ------------------------------------------------------------------

    def _run_presync(self, state: TaskState) -> None:
        """Run BuildTool.generate_sources() before the analyst to ensure generated sources are present.

        Failure is non-fatal: the analyst proceeds with whatever build/ already contains.
        presync_done is set regardless of outcome so resume skips this phase.
        """
        build_tool: BuildTool = self._tools["build"]
        log.info("--- Phase: presync (generating sources before analyze) ---")
        log.info("Running %s.generate_sources()...", build_tool.__class__.__name__)
        t0 = time.perf_counter()
        with self._active_operation(state, phase="presync", message="Generating sources"):
            result = build_tool.generate_sources()
        elapsed_s = time.perf_counter() - t0
        state.presync_done = True
        if result.success:
            state.record("orchestrator", "presync", "ok", elapsed_s=elapsed_s)
            state.record_validation("presync", "success", elapsed_s=elapsed_s)
            log.info("Pre-analyze sync OK (%s)", _fmt_elapsed(elapsed_s))
        else:
            state.record(
                "orchestrator",
                "presync",
                "failed",
                elapsed_s=elapsed_s,
                error=diagnostic_excerpt(result.error, limit=500),
            )
            state.record_validation("presync", "failed", elapsed_s=elapsed_s, error=result.error)
            log.warning(
                "Pre-analyze sync failed (%s) — analyst will proceed with sources available in build/: %s",
                _fmt_elapsed(elapsed_s),
                diagnostic_excerpt(result.error, limit=200),
            )
        self._store.save(state)

    # ------------------------------------------------------------------
    # Build / fix loop (shared)
    # ------------------------------------------------------------------

    def _run_build_fix_loop(self, state: TaskState, set_done: bool) -> bool:
        """Sync → build → test → fix until passing or this loop reaches max_iterations.

        set_done=True  — sets state.done on success (single-pass mode)
        set_done=False — returns True on success without touching state.done (step-loop mode)
        """
        loop_key = _build_loop_key(state)
        if state.build_loop_key != loop_key:
            state.build_loop_key = loop_key
            state.build_loop_start_iteration = state.build_iterations
            self._store.save(state)

        while not state.failed and _build_loop_can_validate(state, self._config.max_iterations):
            validation_only = _build_loop_attempts_used(state) >= self._config.max_iterations
            state.build_iterations += 1
            loop_attempt = _build_loop_attempts_used(state)
            progress = (
                "final validation"
                if validation_only
                else f"{loop_attempt}/{self._config.max_iterations}"
            )
            if validation_only:
                state.record(
                    "orchestrator",
                    "final_validation_after_fix",
                    "running final validation after last fixer change",
                )
                self._store.save(state)

            if not state.build_synced:
                log.info(f"--- Phase: sync ({progress}) ---")
                if not self._sync(state):
                    if validation_only:
                        break
                    if not self._run_fix_phase(state, progress):
                        return False
                    self._store.save(state)
                    continue

            log.info(f"--- Phase: build ({progress}) ---")
            if not self._build(state):
                if validation_only:
                    break
                if not self._run_fix_phase(state, progress):
                    return False
                self._store.save(state)
                continue

            if self._config.run_tests:
                log.info(f"--- Phase: test ({progress}) ---")
                if not self._run_tests(state):
                    if validation_only:
                        break
                    if not self._run_fix_phase(state, progress):
                        return False
                    self._store.save(state)
                    continue
            else:
                state.test_status = "skipped"
                state.record_validation("test", "skipped")

            if self._config.run_checks:
                if not self._run_checks(state, progress):
                    if validation_only:
                        break
                    if not self._run_fix_phase(state, progress):
                        return False
                    self._store.save(state)
                    continue
            else:
                state.check_status = "skipped"
                state.record_validation("check", "skipped")

            gates_changed = self._run_post_validation_semantic_gates(state)
            if state.failed:
                return False
            if gates_changed:
                self._store.save(state)
                continue

            # Passing build (and tests/checks if enabled)
            state.build_loop_key = None
            state.build_loop_start_iteration = 0
            if set_done:
                state.done = True
            self._store.save(state)
            return True

        if not state.failed:
            log.error(
                "Reached max build iterations (%d) for current build/fix loop without a passing build — task failed",
                self._config.max_iterations,
            )
            state.record("orchestrator", "abort", "max build iterations reached for current build/fix loop")
            state.failed = True
        self._store.save(state)
        return False

    # ------------------------------------------------------------------
    # Review / test-write / fix phases (shared helpers)
    # ------------------------------------------------------------------

    def _run_review_loop(self, state: TaskState) -> None:
        if not self._config.run_review or state.review_approved:
            return
        max_fixes = self._config.max_review_iterations
        # review_iterations counts fix attempts (implements triggered by review issues).
        # Each fix always gets a follow-up review — abort only after reviewing the last fix.
        while not state.review_approved and not state.failed:
            label = "initial" if state.review_iterations == 0 else f"after fix {state.review_iterations}/{max_fixes}"
            log.info(f"--- Phase: {_phase_scope_label(state)}review ({label}) ---")
            if not self._refresh_review_fix_diff(state):
                return
            self._run_agent("reviewer", state)
            self._reviewer_ran_this_session = True
            if state.failed:
                return
            if state.review_approved:
                return
            if not state.review_issues:
                log.error("Reviewer produced no decision — aborting task")
                state.record("orchestrator", "abort", "reviewer produced no decision")
                state.failed = True
                return

            # Issues found — abort if fix limit reached, otherwise implement and loop back for review.
            if state.review_iterations >= max_fixes:
                break
            state.review_iterations += 1
            scope = _phase_scope_label(state)
            log.info(f"--- Phase: implement ({scope}review fix {state.review_iterations}/{max_fixes}) ---")
            implementer_result = self._run_agent("implementer", state)
            self._session_code_changed = True
            if state.failed:
                return
            self._mark_build_sync_stale_if_needed(
                (implementer_result.data or {}).get("files_written", []),
                "review fix",
                state,
            )
            state.tests_up_to_date = False
            self._store.save(state)

        if not state.review_approved and not state.failed:
            log.error(
                "Reached max review fix attempts (%d) without approval — task failed",
                max_fixes,
            )
            state.record(
                "orchestrator",
                "abort",
                "max review iterations reached without approval",
            )
            state.failed = True

    def _run_security_review_and_fix_loop(self, state: TaskState) -> None:
        """Run security review; if blocking issues found, fix and re-review until approved or limit reached."""
        if not self._config.run_security_review or state.security_approved:
            return

        max_iter = self._config.max_security_review_iterations

        # security_review_iterations counts fix attempts (same semantics as review_iterations).
        # Each fix always gets a follow-up security review — abort only after reviewing the last fix.
        while not state.security_approved and not state.failed:
            sec_label = (
                "initial"
                if state.security_review_iterations == 0
                else f"after fix {state.security_review_iterations}/{max_iter}"
            )
            log.info(f"--- Phase: {_phase_scope_label(state)}security review ({sec_label}) ---")
            if not self._refresh_review_fix_diff(state):
                return
            self._run_agent("security_reviewer", state)
            if state.failed:
                return
            if state.security_approved:
                return
            if not state.review_issues:
                log.error("Security reviewer produced no decision — aborting task")
                state.record("orchestrator", "abort", "security reviewer produced no decision")
                state.failed = True
                return

            # Blocking issues found — abort if fix limit reached, otherwise implement and loop back.
            if state.security_review_iterations >= max_iter:
                break
            state.security_review_iterations += 1
            state.review_approved = False
            state.review_iterations = 0
            scope = _phase_scope_label(state)
            log.info(f"--- Phase: implement ({scope}security fix {state.security_review_iterations}/{max_iter}) ---")
            implementer_result = self._run_agent("implementer", state)
            self._session_code_changed = True
            if state.failed:
                return
            self._mark_build_sync_stale_if_needed(
                (implementer_result.data or {}).get("files_written", []),
                "security fix",
                state,
            )
            state.tests_up_to_date = False
            self._store.save(state)
            self._run_review_loop(state)
            if state.failed:
                return

        if not state.security_approved and not state.failed:
            log.error(
                "Reached max security review iterations (%d) without approval — task failed",
                max_iter,
            )
            state.record(
                "orchestrator",
                "abort",
                "max security review iterations reached without approval",
            )
            state.failed = True

    def _run_fix_phase(self, state: TaskState, progress: str) -> bool:
        """Run fixer, update sync/review/test flags. Returns True to continue, False if failed."""
        log.info(f"--- Phase: fix ({progress}) ---")
        fixer_result = self._run_agent("fixer", state)
        if state.failed:
            return False
        # Use files reported by this fixer call — not a set-diff on state.files_changed, which
        # would miss re-edits of files already in the list (skipped by fixer dedup on line 127).
        fixer_files = set((fixer_result.data or {}).get("files_written", []))
        self._mark_build_sync_stale_if_needed(fixer_files, "fixer", state)
        if fixer_files:
            test_only_fix = self._fixer_change_is_test_only(fixer_files)
            self._session_code_changed = True
            state.fixer_changed_code = True
            if test_only_fix:
                paths = ", ".join(sorted(fixer_files))
                state.record("orchestrator", "test_only_fix", f"semantic gates preserved for test-only fix: {paths}")
            else:
                state.review_approved = False
                state.security_approved = False
                state.review_iterations = 0
                state.security_review_iterations = 0
                state.tests_up_to_date = False
                if state.active_scope == _SCOPE_FINAL_FULL_TASK:
                    state.final_full_task_review_done = False
            self._store.save(state)
        return True

    def _semantic_gates_pending(self, state: TaskState) -> bool:
        return (
            (self._config.run_review and not state.review_approved)
            or (self._config.run_security_review and not state.security_approved)
            or (self._config.run_test_writing and not state.tests_up_to_date)
        )

    def _run_post_validation_semantic_gates(self, state: TaskState) -> bool:
        """Run stale semantic gates after build/test/check pass.

        Returns True when a gate may have changed files, requiring another deterministic
        validation pass before the task or step can be accepted.
        """
        if not self._semantic_gates_pending(state):
            return False

        review_fixes_before = state.review_iterations
        security_fixes_before = state.security_review_iterations

        log.info(f"--- Phase: {_phase_scope_label(state)}post-validation semantic gates ---")
        self._run_review_loop(state)
        if state.failed:
            return False

        self._run_security_review_and_fix_loop(state)
        if state.failed:
            return False

        test_files_changed = self._run_test_write_phase(state)
        if state.failed:
            return False
        self._review_fix_review_test_changes(state, test_files_changed)
        if state.failed:
            return False

        if state.active_scope == _SCOPE_FINAL_FULL_TASK:
            state.final_full_task_review_done = True
            self._store.save(state)

        return (
            state.review_iterations > review_fixes_before
            or state.security_review_iterations > security_fixes_before
            or test_files_changed
        )

    def _mark_build_sync_stale_if_needed(self, files_written, source: str, state: TaskState) -> None:
        if not files_written:
            return
        build_tool: BuildTool = self._tools["build"]
        if any(build_tool.is_build_config_file(f) for f in files_written):
            log.info("%s changed build-config files — will re-sync before next build", source.capitalize())
            state.build_synced = False

    def _configured_test_write_paths(self) -> list[str]:
        raw = self._config.project_config.get("sandbox", {}).get("allowed_test_write_paths", [])
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    def _is_configured_test_write_path(self, path: str) -> bool:
        normalized_path = _normalize_project_path(path)
        if not normalized_path:
            return False
        for raw_root in self._configured_test_write_paths():
            root = raw_root.replace("\\", "/").strip()
            if not root or _normalize_project_path(root) == "":
                continue
            if any(ch in root for ch in "*?["):
                if _path_matches_pattern(normalized_path, root):
                    return True
                continue
            if _path_is_under_root(normalized_path, root):
                return True
        return False

    def _fixer_change_is_test_only(self, files_written) -> bool:
        paths = [str(path) for path in files_written if str(path).strip()]
        return bool(paths) and all(
            self._is_configured_test_write_path(path) and _path_looks_like_test_artifact(path)
            for path in paths
        )

    def _configured_sync_adopt_paths(self) -> list[str]:
        raw = self._config.project_config.get("build", {}).get("sync_adopt_paths", [])
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    def _is_configured_sync_adopt_path(self, path: str) -> bool:
        return any(_path_matches_pattern(path, pattern) for pattern in self._configured_sync_adopt_paths())

    def _is_builtin_sync_adoptable_artifact(self, build_tool: BuildTool, path: str, artifact) -> bool:
        return artifact.after_status == "tracked" and build_tool.is_sync_adoptable_file(path)

    def _is_sync_adoptable_artifact(self, build_tool: BuildTool, path: str, artifact) -> bool:
        if self._is_configured_sync_adopt_path(path):
            return True
        return self._is_builtin_sync_adoptable_artifact(build_tool, path, artifact)

    def _project_relative_artifact_path(self, artifact_root: Path, path: str) -> str | None:
        normalized = _normalize_artifact_path(path)
        if not normalized:
            return None
        artifact_path = artifact_root.joinpath(*Path(normalized).parts).resolve(strict=False)
        try:
            relative = artifact_path.relative_to(self._config.project_root.resolve())
        except (OSError, ValueError):
            return None
        relative_text = relative.as_posix()
        return relative_text if relative_text and relative_text != "." else None

    def _adopt_sync_outputs(self, state: TaskState, artifact_records: list[dict]) -> None:
        if not artifact_records:
            return
        paths = sorted({record["path"] for record in artifact_records})
        for path in paths:
            if path not in state.files_changed:
                state.files_changed.append(path)
        self._session_code_changed = True
        state.review_approved = False
        state.security_approved = False
        state.review_iterations = 0
        state.security_review_iterations = 0
        state.tests_up_to_date = False
        if state.active_scope == _SCOPE_FINAL_FULL_TASK:
            state.final_full_task_review_done = False
        log.info("Build sync adopted reviewable output file(s): %s", ", ".join(paths))
        state.record("orchestrator", "sync_outputs_adopted", f"{len(paths)} file(s) adopted from build sync")

    def _record_sync_artifact_changes(
        self,
        state: TaskState,
        *,
        before: dict,
        adopt_known_outputs: bool,
    ) -> tuple[bool, dict, str | None]:
        build_tool: BuildTool = self._tools["build"]
        cleanup_passes = 0
        cleaned_records: list[dict] = []
        root = self._validation_artifact_root(state)

        while True:
            after = self._validation_artifact_snapshot(state)
            artifacts = detect_validation_artifacts(before, after)
            adopted_records = []
            outside_project_artifacts = []
            unexpected_artifacts = []
            for artifact in artifacts:
                if not adopt_known_outputs or artifact.after_status == "clean":
                    unexpected_artifacts.append(artifact)
                    continue

                project_path = self._project_relative_artifact_path(root, artifact.path)
                if project_path and self._is_sync_adoptable_artifact(build_tool, project_path, artifact):
                    record = artifact.to_record()
                    record["path"] = project_path
                    adopted_records.append(record)
                    continue

                if (
                    project_path is None
                    and artifact.after_status != "clean"
                    and self._is_builtin_sync_adoptable_artifact(build_tool, artifact.path, artifact)
                ):
                    outside_project_artifacts.append(artifact)
                    continue

                unexpected_artifacts.append(artifact)

            if outside_project_artifacts:
                artifact_records = [artifact.to_record() for artifact in outside_project_artifacts]
                cleanup_errors = restore_validation_artifacts(root, before, outside_project_artifacts)
                error = self._sync_artifact_message(artifact_records).replace(
                    "unexpected repository artifact(s)",
                    "adoptable repository output(s) outside project root",
                    1,
                )
                if cleanup_errors:
                    error += "; cleanup failed: " + "; ".join(cleanup_errors)
                self._record_sync_artifact_cleanup(
                    state,
                    artifact_records=artifact_records,
                    status="failed" if cleanup_errors else "blocked",
                    error=error,
                    cleanup_errors=cleanup_errors or None,
                )
                metadata: dict = {"outside_project": artifact_records}
                if adopted_records:
                    self._adopt_sync_outputs(state, adopted_records)
                    metadata["adopted"] = adopted_records
                metadata["cleanup_failed" if cleanup_errors else "cleaned"] = artifact_records
                return False, metadata, error

            if not unexpected_artifacts:
                self._adopt_sync_outputs(state, adopted_records)
                metadata: dict = {}
                if adopted_records:
                    metadata["adopted"] = adopted_records
                if cleaned_records:
                    metadata["cleaned"] = cleaned_records
                return True, metadata, None

            artifact_records = [artifact.to_record() for artifact in unexpected_artifacts]
            if cleanup_passes >= _VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES:
                error = (
                    "sync command produced additional unexpected repository artifact(s) after "
                    f"{_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES} cleanup passes"
                )
                self._record_sync_artifact_cleanup(
                    state,
                    artifact_records=artifact_records,
                    status="failed",
                    error=error,
                )
                metadata = {"cleanup_failed": artifact_records}
                if adopted_records:
                    self._adopt_sync_outputs(state, adopted_records)
                    metadata["adopted"] = adopted_records
                return False, metadata, error

            cleanup_passes += 1
            cleanup_errors = restore_validation_artifacts(root, before, unexpected_artifacts)
            if cleanup_errors:
                error = self._sync_artifact_message(artifact_records) + "; cleanup failed: " + "; ".join(cleanup_errors)
                self._record_sync_artifact_cleanup(
                    state,
                    artifact_records=artifact_records,
                    status="failed",
                    error=error,
                    cleanup_errors=cleanup_errors,
                )
                metadata = {"cleanup_failed": artifact_records}
                if adopted_records:
                    self._adopt_sync_outputs(state, adopted_records)
                    metadata["adopted"] = adopted_records
                return False, metadata, error

            cleaned_records.extend(artifact_records)
            self._record_sync_artifact_cleanup(
                state,
                artifact_records=artifact_records,
                status="cleaned",
                error=self._sync_artifact_message(artifact_records),
            )

    def _sync_artifact_message(self, artifact_records: list[dict]) -> str:
        paths = ", ".join(f"`{record['path']}`" for record in artifact_records[:10])
        if len(artifact_records) > 10:
            paths += f", ... ({len(artifact_records)} total)"
        return f"sync command produced unexpected repository artifact(s): {paths}"

    def _record_sync_artifact_cleanup(
        self,
        state: TaskState,
        *,
        artifact_records: list[dict],
        status: str,
        error: str,
        cleanup_errors: list[str] | None = None,
    ) -> None:
        if status == "cleaned":
            record_status = "cleaned"
        elif status == "blocked":
            record_status = "blocked"
        else:
            record_status = "cleanup_failed"
        record: dict = {
            "phase": "sync",
            "status": record_status,
            "build_iteration": state.build_iterations,
            "step": state.current_step,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "artifacts": artifact_records,
        }
        if state.active_scope:
            record["scope"] = state.active_scope
        if cleanup_errors:
            record["cleanup_errors"] = cleanup_errors
        state.validation_artifact_records.append(record)
        if status == "cleaned":
            log.warning("%s — cleaned automatically", error)
            state.record("orchestrator", "validation_artifacts_cleaned", error)
            state.record_validation("validation_artifact", "cleaned", error=error)
            return
        if status == "blocked":
            log.error(error)
            state.record("orchestrator", "sync_outputs_blocked", error[:500])
            state.record_validation("validation_artifact", "failed", error=error)
            self._append_validation_artifact_error(state, "sync", error)
            return
        log.error(error)
        state.record("orchestrator", "validation_artifacts_cleanup_failed", error[:500])
        state.record_validation("validation_artifact", "failed", error=error)
        self._append_validation_artifact_error(state, "sync", error)

    def _validation_artifact_root(self, state: TaskState) -> Path:
        if state.worktree_base:
            return Path(state.worktree_base).resolve()
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self._config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
        return self._config.project_root.resolve()

    def _validation_artifact_ignored_roots(self, root: Path) -> tuple[str, ...]:
        artifact_root = root.resolve()
        ignored_roots: list[str] = []
        for internal_path in self._store.internal_paths():
            try:
                relative = Path(internal_path).resolve(strict=False).relative_to(artifact_root)
            except (OSError, ValueError):
                continue
            relative_text = relative.as_posix().rstrip("/")
            if relative_text and relative_text != ".":
                ignored_roots.append(relative_text)
        return tuple(ignored_roots)

    def _validation_artifact_snapshot(self, state: TaskState) -> dict:
        root = self._validation_artifact_root(state)
        return snapshot_validation_dirty_files(
            root,
            ignored_roots=self._validation_artifact_ignored_roots(root),
        )

    def _record_validation_artifacts(
        self,
        state: TaskState,
        *,
        phase: str,
        before: dict,
        check_name: str | None = None,
        new_untracked_only: bool = False,
    ) -> bool:
        """Clean non-ignored repository changes produced by a validation command.

        Returns True when there were no artifacts or cleanup succeeded. A cleanup
        failure is treated as a validation failure so the fixer can try to repair it
        without letting artifacts leak into the final commit.
        """

        cleanup_passes = 0
        while True:
            after = self._validation_artifact_snapshot(state)
            artifacts = detect_validation_artifacts(before, after)
            if new_untracked_only:
                artifacts = [
                    artifact
                    for artifact in artifacts
                    if artifact.before_status == "clean" and artifact.after_status == "untracked"
                ]
            if not artifacts:
                return True

            if cleanup_passes >= _VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES:
                error = (
                    f"{phase} command produced additional repository artifact(s) after "
                    f"{_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES} cleanup passes"
                )
                if check_name:
                    error = (
                        f"check/{check_name} command produced additional repository artifact(s) after "
                        f"{_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES} cleanup passes"
                    )
                log.error(error)
                state.record("orchestrator", "validation_artifacts_cleanup_failed", error[:500])
                state.record_validation(
                    "validation_artifact",
                    "failed",
                    error=error,
                    check_name=check_name,
                )
                self._append_validation_artifact_error(state, phase, error)
                return False

            cleanup_passes += 1
            cleanup_errors = restore_validation_artifacts(self._validation_artifact_root(state), before, artifacts)
            cleaned = not cleanup_errors
            artifact_records = [artifact.to_record() for artifact in artifacts]
            record: dict = {
                "phase": phase,
                "status": "cleaned" if cleaned else "cleanup_failed",
                "build_iteration": state.build_iterations,
                "step": state.current_step,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "artifacts": artifact_records,
            }
            if state.active_scope:
                record["scope"] = state.active_scope
            if check_name:
                record["check_name"] = check_name
            if cleanup_errors:
                record["cleanup_errors"] = cleanup_errors
            state.validation_artifact_records.append(record)

            paths = ", ".join(f"`{artifact.path}`" for artifact in artifacts[:10])
            if len(artifacts) > 10:
                paths += f", ... ({len(artifacts)} total)"
            message = f"{phase} command produced unexpected repository artifact(s): {paths}"
            if check_name:
                message = f"check/{check_name} command produced unexpected repository artifact(s): {paths}"
                if phase == "check_autofix":
                    message = f"check/{check_name} autofix command produced unexpected repository artifact(s): {paths}"

            if cleaned:
                log.warning("%s — cleaned automatically", message)
                state.record("orchestrator", "validation_artifacts_cleaned", message)
                state.record_validation(
                    "validation_artifact",
                    "cleaned",
                    error=message,
                    check_name=check_name,
                )
                continue

            error = message + "; cleanup failed: " + "; ".join(cleanup_errors)
            log.error(error)
            state.record("orchestrator", "validation_artifacts_cleanup_failed", error[:500])
            state.record_validation(
                "validation_artifact",
                "failed",
                error=error,
                check_name=check_name,
            )
            self._append_validation_artifact_error(state, phase, error)
            return False

    def _append_validation_artifact_error(self, state: TaskState, phase: str, error: str) -> None:
        excerpt = diagnostic_excerpt(error, limit=_VALIDATION_ARTIFACT_ERROR_LIMIT)
        if phase == "test":
            state.test_errors.append(excerpt)
            state.test_status = "failed"
        elif phase in {"check", "check_autofix"}:
            state.check_errors.append(excerpt)
            state.check_status = "failed"
        else:
            state.errors.append(excerpt)
            state.build_status = "failed"

    def _run_agent(self, name: str, state: TaskState):
        from agents.base_agent import AgentResult

        agent = self._agents[name]
        log.info(f"Running {name} agent...")
        t0 = time.perf_counter()
        hist_len = len(state.history)
        try:
            with self._active_operation(
                state,
                phase="agent",
                agent=name,
                message=f"Running {name} agent",
            ):
                with llm_retry_history(agent, name, state, self._store):
                    result = agent.run(state)
        except Exception as exc:
            elapsed_s = time.perf_counter() - t0
            log.error(f"{name} raised an unexpected error ({_fmt_elapsed(elapsed_s)}): {exc}")
            state.record(name, "error", str(exc), elapsed_s=elapsed_s)
            state.failed = True
            self._store.save(state)
            return AgentResult(success=False, message=str(exc))
        elapsed_s = time.perf_counter() - t0
        if len(state.history) > hist_len:
            state.history[-1]["elapsed_s"] = round(elapsed_s, 1)
        elapsed = _fmt_elapsed(elapsed_s)
        if result.success:
            log.info(f"{name}: {result.message} ({elapsed})")
        else:
            log.error(f"{name} failed: {result.message} ({elapsed})")
        self._store.save(state)
        return result

    def _active_operation(
        self,
        state: TaskState,
        *,
        phase: str,
        agent: str | None = None,
        message: str | None = None,
    ) -> ActiveOperationHeartbeat:
        return ActiveOperationHeartbeat(
            self._store,
            state,
            phase=phase,
            agent=agent,
            scope=state.active_scope,
            message=message,
            interval_s=self._config.heartbeat_interval_seconds,
        )

    def _run_test_write_phase(self, state: TaskState) -> bool:
        if not self._config.run_test_writing or state.tests_up_to_date:
            return False
        log.info(f"--- Phase: {_phase_scope_label(state)}test write ---")
        result = self._run_agent("test_writer", state)
        files_written = (result.data or {}).get("files_written", [])
        self._mark_build_sync_stale_if_needed(files_written, "test writer", state)
        return bool(files_written)

    def _run_checks(self, state: TaskState, progress: str) -> bool:
        """Run all configured quality checks in order. Returns True only if all pass."""
        build_tool: BuildTool = self._tools["build"]
        checks = self._config.project_config.get("build", {}).get("checks", [])
        if not checks:
            state.check_status = "skipped"
            state.record_validation("check", "skipped")
            self._store.save(state)
            return True
        all_passed = True
        for check in checks:
            name = check.get("name", "check")
            log.info(f"--- Phase: check/{name} ({progress}) ---")
            t0 = time.perf_counter()
            artifact_before = self._validation_artifact_snapshot(state)
            with self._active_operation(state, phase="check", message=f"Running check/{name}"):
                result = build_tool.run_check(name, check)
            elapsed_s = time.perf_counter() - t0
            artifacts_ok = self._record_validation_artifacts(
                state,
                phase="check",
                before=artifact_before,
                check_name=name,
            )
            fix_command = check.get("fix_command")
            skip_final_failure_validation = False
            if artifacts_ok and not result.success and fix_command:
                state.record_validation(
                    "check", "failed", elapsed_s=elapsed_s, error=result.error or result.output, check_name=name
                )
                log.info(f"Check {name} failed — running fix_command: {fix_command}")
                fix_cfg: dict = {"command": fix_command}
                if "timeout" in check:
                    fix_cfg["timeout"] = check["timeout"]
                t0 = time.perf_counter()
                autofix_artifact_before = self._validation_artifact_snapshot(state)
                with self._active_operation(
                    state,
                    phase="check_autofix",
                    message=f"Running check/{name} autofix",
                ):
                    fix_result = build_tool.run_check(f"{name}_autofix", fix_cfg)
                fix_elapsed_s = time.perf_counter() - t0
                autofix_artifacts_ok = self._record_validation_artifacts(
                    state,
                    phase="check_autofix",
                    before=autofix_artifact_before,
                    check_name=name,
                    new_untracked_only=True,
                )
                fix_success = fix_result.success and autofix_artifacts_ok
                autofix_error = fix_result.error
                if not autofix_error and not autofix_artifacts_ok:
                    autofix_error = (
                        state.check_errors[-1] if state.check_errors else "validation artifacts cleanup failed"
                    )
                state.record("orchestrator", f"check_{name}_autofix", "success" if fix_success else "failed")
                state.record_validation(
                    "check_autofix",
                    "success" if fix_success else "failed",
                    elapsed_s=fix_elapsed_s,
                    error=autofix_error or None,
                    check_name=name,
                )
                if fix_success:
                    t0 = time.perf_counter()
                    artifact_before = self._validation_artifact_snapshot(state)
                    with self._active_operation(state, phase="check", message=f"Running check/{name}"):
                        result = build_tool.run_check(name, check)
                    elapsed_s = time.perf_counter() - t0
                    artifacts_ok = self._record_validation_artifacts(
                        state,
                        phase="check",
                        before=artifact_before,
                        check_name=name,
                    )
                else:
                    skip_final_failure_validation = True
                    log.warning(f"Auto-fix for {name} failed: {diagnostic_excerpt(autofix_error, limit=500)}")
            if result.success and artifacts_ok:
                log.info(f"Check {name} OK ({_fmt_elapsed(elapsed_s)})")
                state.record("orchestrator", f"check_{name}", "success", elapsed_s=elapsed_s)
                state.record_validation("check", "success", elapsed_s=elapsed_s, check_name=name)
            else:
                if artifacts_ok:
                    check_error = diagnostic_excerpt(result.error or result.output, limit=_FIXER_ERROR_LIMIT)
                else:
                    check_error = (
                        state.check_errors[-1] if state.check_errors else "validation artifacts cleanup failed"
                    )
                log.error(f"Check {name} failed ({_fmt_elapsed(elapsed_s)}):\n{check_error}")
                if artifacts_ok:
                    state.check_errors.append(f"[{name}]\n{check_error}")
                state.record("orchestrator", f"check_{name}", "failed", elapsed_s=elapsed_s)
                if artifacts_ok and not skip_final_failure_validation:
                    state.record_validation(
                        "check",
                        "failed",
                        elapsed_s=elapsed_s,
                        error=result.error or result.output,
                        check_name=name,
                    )
                all_passed = False
        state.check_status = "success" if all_passed else "failed"
        self._store.save(state)
        return all_passed

    def _run_tests(self, state: TaskState) -> bool:
        build_tool: BuildTool = self._tools["build"]
        log.info("Running tests...")
        t0 = time.perf_counter()
        artifact_before = self._validation_artifact_snapshot(state)
        with self._active_operation(state, phase="test", message="Running tests"):
            result = build_tool.run_tests()
        elapsed_s = time.perf_counter() - t0
        artifacts_ok = self._record_validation_artifacts(state, phase="test", before=artifact_before)
        if not result.success:
            test_error = diagnostic_excerpt(result.error, limit=_FIXER_ERROR_LIMIT)
            log.error(
                f"Tests failed ({_fmt_elapsed(elapsed_s)}):\n{diagnostic_excerpt(result.error, limit=_LOG_ERROR_LIMIT)}"
            )
            state.test_errors.append(test_error)
            state.test_status = "failed"
            state.record("orchestrator", "test", "failed", elapsed_s=elapsed_s)
            state.record_validation("test", "failed", elapsed_s=elapsed_s, error=result.error)
            return False
        if not artifacts_ok:
            log.error(
                "Tests produced unexpected repository artifacts and cleanup failed (%s)",
                _fmt_elapsed(elapsed_s),
            )
            state.record("orchestrator", "test", "failed", elapsed_s=elapsed_s)
            return False
        log.info(f"Tests OK ({_fmt_elapsed(elapsed_s)})")
        state.test_status = "success"
        state.record("orchestrator", "test", "success", elapsed_s=elapsed_s)
        state.record_validation("test", "success", elapsed_s=elapsed_s)
        return True

    def _sync(self, state: TaskState) -> bool:
        build_tool: BuildTool = self._tools["build"]
        log.info(f"Running build sync ({build_tool.__class__.__name__}.sync()) — this may take a few minutes...")
        artifact_before = self._validation_artifact_snapshot(state)
        t0 = time.perf_counter()
        with self._active_operation(state, phase="sync", message="Running build sync"):
            result = build_tool.sync()
        elapsed_s = time.perf_counter() - t0
        artifacts_ok, sync_output_metadata, artifact_error = self._record_sync_artifact_changes(
            state,
            before=artifact_before,
            adopt_known_outputs=result.success,
        )
        metadata = dict(result.metadata)
        if sync_output_metadata:
            metadata["sync_outputs"] = sync_output_metadata
        sync_success = result.success and artifacts_ok
        if sync_success:
            state.build_synced = True
            state.record("orchestrator", "sync", "ok", elapsed_s=elapsed_s)
            state.record_validation("sync", "success", elapsed_s=elapsed_s, metadata=metadata)
            log.info(f"Build sync OK ({_fmt_elapsed(elapsed_s)})")
        else:
            error = result.error
            if artifact_error:
                error = f"{error}\n{artifact_error}" if error else artifact_error
            state.record("orchestrator", "sync", "failed", elapsed_s=elapsed_s)
            state.record_validation("sync", "failed", elapsed_s=elapsed_s, error=error, metadata=metadata)
            log.error(
                f"Build sync failed ({_fmt_elapsed(elapsed_s)}):\n{diagnostic_excerpt(error, limit=_LOG_ERROR_LIMIT)}"
            )
            state.errors.append(diagnostic_excerpt(error, limit=_FIXER_ERROR_LIMIT))
        self._store.save(state)
        return sync_success

    def _build(self, state: TaskState) -> bool:
        build_tool: BuildTool = self._tools["build"]
        log.info("Running compile check...")
        t0 = time.perf_counter()
        artifact_before = self._validation_artifact_snapshot(state)
        with self._active_operation(state, phase="build", message="Running compile check"):
            result = build_tool.compile_check()
        elapsed_s = time.perf_counter() - t0
        artifacts_ok = self._record_validation_artifacts(state, phase="build", before=artifact_before)
        state.build_status = "success" if result.success and artifacts_ok else "failed"
        state.record("orchestrator", "build", state.build_status, elapsed_s=elapsed_s)
        state.record_validation(
            "build",
            state.build_status,
            elapsed_s=elapsed_s,
            error=None if result.success and artifacts_ok else result.error,
        )
        if not result.success:
            log.error(
                f"Build failed ({_fmt_elapsed(elapsed_s)}):\n{diagnostic_excerpt(result.error, limit=_LOG_ERROR_LIMIT)}"
            )
            state.errors.append(diagnostic_excerpt(result.error, limit=_FIXER_ERROR_LIMIT))
            return False
        if not artifacts_ok:
            log.error(
                "Build produced unexpected repository artifacts and cleanup failed (%s)",
                _fmt_elapsed(elapsed_s),
            )
            return False
        log.info(f"Build OK ({_fmt_elapsed(elapsed_s)})")
        state.fixer_changed_code = False
        return True


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"
