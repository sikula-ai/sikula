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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agents.analyst_agent import AnalystAgent
from agents.delivery_contracts import is_delivery_implementation_already_satisfied
from agents.fixer_agent import FixerAgent
from agents.implementer_agent import ImplementerAgent
from agents.planner_agent import PlannerAgent
from agents.reviewer_agent import ReviewerAgent
from agents.security_reviewer_agent import SecurityReviewerAgent
from agents.test_writer_agent import TestWriterAgent
from core.delivery_scope_audit import (
    DELIVERY_SCOPE_VIOLATION_CODE,
    DeliveryScopeAudit,
    DeliveryScopeAuditPolicy,
    DeliveryScopeProviderAttemptStopped,
    DeliveryScopeToolMutationStopped,
    delivery_scope_audit_recovery_required as delivery_scope_audit_recovery_required,
)
from core.delivery_unit_metadata import delivery_unit_planner_step_limit
from core.diagnostics import diagnostic_excerpt
from core.llm_client import LLMClient
from core.progress import ActiveOperationHeartbeat
from core.retry_history import llm_retry_history
from core.state import (
    DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
    DELIVERY_TERMINAL_STOP_CODES,
    StateStore,
    TaskState,
)
from core.structured_output import DELIVERY_DISPOSITION_ALREADY_SATISFIED
from core.synthetic_test_harness_audit import (
    active_findings_for_current_files as active_synthetic_harness_findings_for_current_files,
)
from core.synthetic_test_harness_audit import detect_new_synthetic_test_harnesses
from core.test_execution_gate_audit import (
    active_findings_for_current_files,
    detect_new_test_execution_gates,
    test_execution_gate_signature_counts,
)
from core.validation_artifacts import (
    DeliveryScopeSnapshotError,
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
_TEST_EXECUTION_GATE_AUDIT_MARKER = "TEST EXECUTION GATE AUDIT:"
_SYNTHETIC_HARNESS_RECOVERY_MAX_RETRIES = 1
_TEST_WRITER_AUDIT_SNAPSHOT = "test_writer_audit_before"
_DELIVERY_STOP_STATE_INVALID_CODE = "delivery_stop_state_invalid"
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
_TEST_GATE_AUDIT_SOURCE_SUFFIXES = (
    ".cs",
    ".dart",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
)


def _phase_scope_label(state: TaskState) -> str:
    return "final full-task " if state.active_scope == _SCOPE_FINAL_FULL_TASK else ""


def _agent_session_title(name: str, state: TaskState) -> str:
    task_part = state.task_id[:8]
    parts = ["sikula", name.replace("_", "-"), task_part]
    if state.plan and 0 <= state.current_step < len(state.plan):
        parts.append(f"step-{state.current_step + 1}")
    if state.active_scope == _SCOPE_FINAL_FULL_TASK:
        parts.append("final")
    return "-".join(part for part in parts if part)[:80].strip("-")


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
    normalized = path.replace("\\", "/") if os.name == "nt" else path
    return normalized.strip().strip("/")


def _native_scope_path(path: object) -> str:
    raw = str(path)
    return raw.replace("\\", "/") if os.name == "nt" else raw


def _path_matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = _normalize_artifact_path(path)
    raw_pattern = _native_scope_path(pattern).strip()
    directory_pattern = raw_pattern.endswith("/")
    normalized_pattern = raw_pattern.strip("/")
    if not normalized_path or not normalized_pattern:
        return False
    if directory_pattern:
        return normalized_path == normalized_pattern or normalized_path.startswith(f"{normalized_pattern}/")
    return normalized_path == normalized_pattern or fnmatch.fnmatch(normalized_path, normalized_pattern)


def _normalize_project_path(path: str) -> str:
    normalized = posixpath.normpath(_native_scope_path(path).strip())
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return ""
    return normalized.strip("/")


def _synthetic_harness_finding_key(finding: dict) -> tuple[str, tuple[str, ...]]:
    return (str(finding.get("path", "")), tuple(finding.get("subsystems") or []))


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
    return (
        lower_part in _TEST_PATH_MARKERS
        or lower_part.endswith(("_test", "_tests", "-test", "-tests"))
        or part.endswith(("Test", "Tests"))
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
        or lower_filename.endswith(
            tuple(suffix.lower() for suffix in _TEST_FILE_SUFFIXES if suffix.startswith((".", "_")))
        )
        or filename.endswith(tuple(suffix for suffix in _TEST_FILE_SUFFIXES if suffix[0].isupper()))
    )


def _path_looks_like_test_audit_candidate(path: str) -> bool:
    if _path_looks_like_test_artifact(path):
        return True
    parts = _path_parts(path)
    if not parts:
        return False
    return parts[-1].lower().endswith(_TEST_GATE_AUDIT_SOURCE_SUFFIXES)


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
        self._delivery_scope_audit = DeliveryScopeAudit(
            config=config,
            store=state_store,
            tools=self._tools,
            agents=lambda: self._agents,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recover_interrupted_delivery_scope(self, task_id: str) -> TaskState:
        """Recover a persisted delivery audit without entering the pipeline."""
        state = self._store.load(task_id)
        if state is None:
            raise ValueError(f"Task not found: {task_id}")
        active_invocation = not state.done and not state.failed
        if active_invocation:
            state.record_run_invocation(self._config_snapshot)
            if not state.config_snapshot and self._config_snapshot:
                state.config_snapshot = self._config_snapshot
            self._store.save(state)
            self._audit_interrupted_delivery_scope(state)
        state.clear_active_operation()
        state.pid = os.getpid()
        self._store.save(state)
        return state

    def run(
        self,
        task_id: Optional[str] = None,
        task_description: Optional[str] = None,
        label: Optional[str] = None,
        complete_invocation_history: bool = False,
        before_pipeline: Optional[Callable[[TaskState], None]] = None,
        invocation_already_recorded: bool = False,
    ) -> TaskState:
        created_state = False
        if task_id:
            state = self._store.load(task_id)
            if state is None:
                raise ValueError(f"Task not found: {task_id}")
        elif task_description:
            state = self._store.create(task_description)
            created_state = True
        else:
            raise ValueError("Provide task_id or task_description")

        display = label or state.task_description.splitlines()[0][:60]
        log.info("Task %s — %s", state.task_id, display)
        active_invocation = not state.done and not state.failed
        if active_invocation and not invocation_already_recorded:
            state.record_run_invocation(
                self._config_snapshot,
                complete_history_from_creation=(created_state or complete_invocation_history),
            )
        resume_scope_violation = active_invocation and self._audit_interrupted_delivery_scope(state)
        state.clear_active_operation()
        state.pid = os.getpid()
        # Capture effective config on first run before any pre-pipeline work.
        if active_invocation and not state.config_snapshot and self._config_snapshot:
            state.config_snapshot = self._config_snapshot
        self._store.save(state)
        if resume_scope_violation:
            return state
        if self._abort_on_delivery_terminal_stop(state):
            return state
        if active_invocation and before_pipeline is not None:
            before_pipeline(state)
        self._loop(state)
        return state

    # ------------------------------------------------------------------
    # Top-level loop
    # ------------------------------------------------------------------

    def _loop(self, state: TaskState) -> None:
        if self._abort_on_delivery_terminal_stop(state):
            return

        if state.done or state.failed:
            if state.failed:
                if state.contract_gate_blocked and not state.worktree_path and not state.worktree_branch:
                    log.info(
                        "Task %s already in terminal state (failed by contract readiness gate) — improve the task "
                        "contract and start a fresh task-file run",
                        state.task_id,
                    )
                    return
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

        if self._abort_on_validation_coverage_gaps(state):
            return

        # Phase 0: presync — generate sources before analyze (run_presync: true only)
        # Skipped on resume if already attempted. Failure is a warning, not an abort —
        # analyst proceeds with whatever is in build/ from prior builds.
        if self._config.run_presync and not state.presync_done:
            self._run_presync(state)
            if self._abort_on_delivery_terminal_stop(state):
                return

        # Phase 1: analyze (idempotent — skipped if prompt already exists)
        if not state.implementation_prompt:
            log.info("--- Phase: analyze ---")
            result = self._run_agent("analyst", state)
            if state.failed or not result.success:
                state.failed = True
                self._store.save(state)
                return

        # Phase 1.5: plan (skipped if planner already ran — plan_decided guards resume)
        delivery_child = bool(state.delivery_plan_id and state.delivery_unit_id)
        if (self._config.run_planner or delivery_child) and not state.plan_decided:
            log.info("--- Phase: plan ---")
            result = self._run_agent("planner", state)
            if state.failed or not result.success:
                state.failed = True
                self._store.save(state)
                return
            if state.plan:
                state.step_file_tracking_enabled = True
                state.step_files_changed = []
                self._store.save(state)

        if self._abort_on_delivery_planner_budget(state):
            return

        # Phases 2-5: step-by-step or single-pass
        if state.plan:
            self._run_step_loop(state)
        else:
            self._run_single_pass(state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _abort_on_delivery_terminal_stop(self, state: TaskState) -> str | None:
        try:
            if state.delivery_stop_code is not None:
                state.set_delivery_terminal_stop(state.delivery_stop_code, source="orchestrator")
            if state.delivery_stop_code != DELIVERY_STOP_UNIT_SCOPE_VIOLATION:
                parse_error_code = state.delivery_stop_code_from_parse_error()
                if parse_error_code is not None:
                    parse_error = state.delivery_disposition_parse_error or {}
                    state.set_delivery_terminal_stop(
                        parse_error_code,
                        source=str(parse_error.get("source", "orchestrator")),
                    )
                disposition_code = state.delivery_stop_code_from_disposition()
                if disposition_code is not None:
                    disposition = state.delivery_stop_disposition or {}
                    state.set_delivery_terminal_stop(
                        disposition_code,
                        source=str(disposition.get("source", "orchestrator")),
                    )
        except (TypeError, ValueError) as exc:
            message = f"{_DELIVERY_STOP_STATE_INVALID_CODE}: {exc}"
            log.error(message)
            state.record("orchestrator", "abort", message)
            state.failed = True
            state.done = False
            self._store.save(state)
            return _DELIVERY_STOP_STATE_INVALID_CODE

        code = state.delivery_stop_code
        if code is None:
            return None
        if code not in DELIVERY_TERMINAL_STOP_CODES:  # Defensive; setter validates this above.
            raise AssertionError("validated delivery terminal stop code escaped its closed set")
        log.error("Delivery child stopped with terminal outcome: %s", code)
        self._store.save(state)
        return code

    def _abort_on_delivery_planner_budget(self, state: TaskState) -> bool:
        if not (state.delivery_plan_id and state.delivery_unit_id and state.plan_decided):
            return False
        limit = delivery_unit_planner_step_limit(state.delivery_unit_budget)
        actual = len(state.plan) if state.plan else 1
        if actual <= limit:
            return False

        state.record_delivery_budget_stop(name="max_planner_steps", limit=limit, actual=actual)
        state.failed = True
        self._store.save(state)
        log.error(
            "Delivery unit %s exceeded max_planner_steps (%d > %d); stopping before implementation",
            state.delivery_unit_id,
            actual,
            limit,
        )
        return True

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

    def _record_step_files_changed(self, state: TaskState, paths: object) -> None:
        if (
            state.step_file_tracking_enabled is not True
            or not state.plan
            or state.active_scope == _SCOPE_FINAL_FULL_TASK
            or not isinstance(paths, (list, tuple, set))
            or not isinstance(state.step_files_changed, list)
        ):
            return
        for raw_path in paths:
            if not isinstance(raw_path, str):
                continue
            path = raw_path.strip()
            if path and path not in state.step_files_changed:
                state.step_files_changed.append(path)

    @staticmethod
    def _invalidate_delivery_no_change_outcome(state: TaskState, paths: object, *, source: str) -> None:
        if state.delivery_no_change_outcome is None or not isinstance(paths, (list, tuple, set)):
            return
        if not any(isinstance(path, str) and path.strip() for path in paths):
            return
        state.delivery_no_change_outcome = None
        state.record(
            "orchestrator",
            "delivery_no_change_invalidated",
            f"{source} produced production changes",
        )

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
                    files.append(abs_path.relative_to(project_root).as_posix())
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
            reviewer_result = self._run_delivery_review_agent("reviewer", state)
            self._reviewer_ran_this_session = True
            if state.failed or self._abort_on_failed_agent_result(state, "reviewer", reviewer_result):
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
            security_result = self._run_delivery_review_agent("security_reviewer", state)
            if state.failed or self._abort_on_failed_agent_result(state, "security_reviewer", security_result):
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
        if not state.files_changed and not is_delivery_implementation_already_satisfied(state):
            log.info("--- Phase: implement ---")
            result = self._run_agent("implementer", state)
            if state.failed:
                self._store.save(state)
                return
            if not result.success:
                msg = f"implementer failed: {result.message}"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
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
                    self._invalidate_delivery_no_change_outcome(state, dirty, source="adopted implementer output")
                    state.record(
                        "orchestrator", "adopt_worktree_changes", f"{len(dirty)} file(s) adopted from worktree"
                    )
                else:
                    outcome = (result.data or {}).get("implementation_outcome")
                    if (
                        outcome == DELIVERY_DISPOSITION_ALREADY_SATISFIED
                        and is_delivery_implementation_already_satisfied(state)
                    ):
                        log.info("Delivery implementation is already satisfied; continuing to configured gates")
                        state.record(
                            "orchestrator",
                            "implementation_already_satisfied",
                            "no file changes; continuing to configured gates",
                        )
                        self._store.save(state)
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
            if self._active_test_execution_gate_findings(state):
                self._refresh_test_execution_gate_errors(state)
                if self._config.run_build:
                    state.record(
                        "orchestrator",
                        "review_fast_path_blocked",
                        "test execution gate audit requires the build/fix loop",
                    )
                    self._store.save(state)
                elif self._abort_no_build_with_active_test_execution_gates(state):
                    return
            else:
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
            if self._abort_no_build_with_active_test_execution_gates(state):
                return
            state.test_status = "skipped"
            state.check_status = "skipped"
            state.done = bool(state.files_changed) or is_delivery_implementation_already_satisfied(state)
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
                if state.step_file_tracking_enabled:
                    state.step_files_changed = []
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
            if not is_delivery_implementation_already_satisfied(state):
                log.error("All steps were skipped — no file changes produced — task failed")
                state.record("orchestrator", "abort", "all steps skipped — no file changes")
                state.failed = True
                self._store.save(state)
                return
            state.record(
                "orchestrator",
                "plan_already_satisfied",
                "all steps reported already satisfied; continuing to final configured gates",
            )

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
            if self._abort_no_build_with_active_test_execution_gates(state):
                return
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
            result = self._run_agent("implementer", state)
            if state.failed:
                return False
            if not result.success:
                msg = f"implementer failed: {result.message}"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
                self._store.save(state)
                return False
            implementation_files = (result.data or {}).get("files_written", [])
            if not implementation_files:
                current_step_has_changes = bool(
                    state.step_file_tracking_enabled is True
                    and isinstance(state.step_files_changed, list)
                    and state.step_files_changed
                )
                recorded_paths = {
                    normalized
                    for normalized in (_normalize_project_path(str(path)) for path in state.files_changed)
                    if normalized
                }
                dirty = [
                    path
                    for path in self._worktree_dirty_files(state)
                    if _normalize_project_path(path) not in recorded_paths
                ]
                if dirty:
                    log.warning(
                        "Implementer made no new writes but worktree has %d unrecorded file(s) — adopting them",
                        len(dirty),
                    )
                    state.files_changed.extend(dirty)
                    self._record_step_files_changed(state, dirty)
                    self._invalidate_delivery_no_change_outcome(state, dirty, source="adopted implementer output")
                    state.record(
                        "orchestrator", "adopt_worktree_changes", f"{len(dirty)} file(s) adopted from worktree"
                    )
                elif current_step_has_changes:
                    self._invalidate_delivery_no_change_outcome(
                        state,
                        state.step_files_changed,
                        source="reconciled current-step output",
                    )
                else:
                    outcome = (result.data or {}).get("implementation_outcome")
                    if state.delivery_plan_id and state.delivery_unit_id:
                        if outcome != DELIVERY_DISPOSITION_ALREADY_SATISFIED or not (
                            is_delivery_implementation_already_satisfied(state)
                        ):
                            log.error("Delivery implementer produced an unclassified no-change step")
                            state.record(
                                "orchestrator",
                                "abort",
                                f"Step {step_num}/{total_steps}: unclassified no-change result",
                            )
                            state.failed = True
                            self._store.save(state)
                            return False
                        action = "step_already_satisfied"
                    else:
                        action = "step_skipped"
                    log.info(
                        "Implementer made no changes for step %d/%d — advancing to next step",
                        step_num,
                        total_steps,
                    )
                    state.record(
                        "orchestrator",
                        action,
                        f"Step {step_num}/{total_steps}: no file changes",
                    )
                    state.step_implemented = True
                    self._store.save(state)
                    if action == "step_skipped":
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
        try:
            with self._active_operation(state, phase="presync", message="Generating sources"):
                with self._delivery_scope_tool_mutation_boundary(state, "presync"):
                    result = build_tool.generate_sources()
        except DeliveryScopeToolMutationStopped:
            state.presync_done = True
            self._store.save(state)
            return
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
            progress = "final validation" if validation_only else f"{loop_attempt}/{self._config.max_iterations}"
            if validation_only:
                state.record(
                    "orchestrator",
                    "final_validation_after_fix",
                    "running final validation after last fixer change",
                )
                self._store.save(state)

            if self._refresh_test_execution_gate_errors(state):
                if validation_only:
                    break
                if not self._run_fix_phase(state, progress):
                    return False
                self._store.save(state)
                continue

            if not state.build_synced:
                log.info(f"--- Phase: sync ({progress}) ---")
                if not self._sync(state):
                    if state.failed:
                        return False
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
                    if state.failed:
                        return False
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
            reviewer_result = self._run_delivery_review_agent("reviewer", state)
            self._reviewer_ran_this_session = True
            if state.failed or self._abort_on_failed_agent_result(state, "reviewer", reviewer_result):
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
            if state.failed:
                return
            if not implementer_result.success:
                msg = f"implementer failed: {implementer_result.message}"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
                self._store.save(state)
                return
            files_written = (implementer_result.data or {}).get("files_written", [])
            if files_written:
                self._session_code_changed = True
                self._mark_build_sync_stale_if_needed(files_written, "review fix", state)
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
            security_result = self._run_delivery_review_agent("security_reviewer", state)
            if state.failed or self._abort_on_failed_agent_result(state, "security_reviewer", security_result):
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
            if state.failed:
                return
            if not implementer_result.success:
                msg = f"implementer failed: {implementer_result.message}"
                log.error(msg)
                state.record("orchestrator", "abort", msg)
                state.failed = True
                self._store.save(state)
                return
            files_written = (implementer_result.data or {}).get("files_written", [])
            if files_written:
                self._session_code_changed = True
                self._mark_build_sync_stale_if_needed(files_written, "security fix", state)
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

    def _validation_error_state_snapshot(self, state: TaskState) -> dict:
        return {
            "errors": list(state.errors),
            "test_errors": list(state.test_errors),
            "check_errors": list(state.check_errors),
            "build_status": state.build_status,
            "test_status": state.test_status,
            "check_status": state.check_status,
        }

    def _restore_validation_error_state(self, state: TaskState, snapshot: dict) -> None:
        state.errors = list(snapshot.get("errors") or [])
        state.test_errors = list(snapshot.get("test_errors") or [])
        state.check_errors = list(snapshot.get("check_errors") or [])
        state.build_status = snapshot.get("build_status")
        state.test_status = snapshot.get("test_status")
        state.check_status = snapshot.get("check_status")

    def _run_fix_phase(self, state: TaskState, progress: str) -> bool:
        """Run fixer, update sync/review/test flags. Returns True to continue, False if failed."""
        log.info(f"--- Phase: fix ({progress}) ---")
        validation_before = self._validation_error_state_snapshot(state)
        test_gate_before = self._test_execution_gate_snapshot()
        fixer_result = self._run_agent("fixer", state)
        if state.failed:
            return False
        if not fixer_result.success:
            msg = f"fixer failed: {fixer_result.message}"
            log.error(msg)
            state.record("orchestrator", "abort", msg)
            state.failed = True
            self._store.save(state)
            return False
        # Use files reported by this fixer call — not a set-diff on state.files_changed, which
        # would miss re-edits of files already in the list (skipped by fixer dedup on line 127).
        fixer_files = set((fixer_result.data or {}).get("files_written", []))
        gate_findings = self._audit_test_execution_gates_after_agent(
            state,
            source="fixer",
            files_written=fixer_files,
            before_snapshot=test_gate_before,
        )
        synthetic_findings = self._audit_synthetic_test_harnesses_after_agent(
            state,
            source="fixer",
            files_written=fixer_files,
            before_snapshot=test_gate_before,
        )
        if synthetic_findings:
            will_retry_synthetic_harness = _SYNTHETIC_HARNESS_RECOVERY_MAX_RETRIES > 0 and not state.failed
            restored, restore_errors = self._recover_synthetic_test_harness_after_agent(
                state,
                source="fixer",
                findings=synthetic_findings,
                before_snapshot=test_gate_before,
                will_retry=will_retry_synthetic_harness,
            )
            first_pass_remaining_files = set(
                self._agent_files_still_changed_since_snapshot(fixer_files, test_gate_before)
            )
            fixer_files = first_pass_remaining_files
            self._store.save(state)
            if restored and not restore_errors and will_retry_synthetic_harness:
                self._restore_validation_error_state(state, validation_before)
                self._store.save(state)
                log.info("Retrying fixer agent after synthetic test harness recovery")
                retry_test_gate_before = self._test_execution_gate_snapshot()
                fixer_result = self._run_agent("fixer", state)
                no_op_retry_after_retained_fix = (
                    not fixer_result.success
                    and bool(restored)
                    and (fixer_result.message or "").strip() == "Agent made no file changes"
                )
                if state.failed:
                    return False
                if no_op_retry_after_retained_fix:
                    retained = ", ".join(sorted(first_pass_remaining_files)) or "(restored tree only)"
                    log.info(
                        "Fixer retry made no additional file changes after synthetic test harness recovery; "
                        "continuing to validate current tree: %s",
                        retained,
                    )
                    state.record(
                        "orchestrator",
                        "synthetic_test_harness_recovery_noop_retry",
                        f"fixer retry made no additional file changes; validating current tree: {retained}",
                    )
                    self._store.save(state)
                    retry_files = set()
                elif self._abort_on_failed_agent_result(state, "fixer", fixer_result):
                    return False
                else:
                    retry_files = set((fixer_result.data or {}).get("files_written", []))
                gate_findings.extend(
                    self._audit_test_execution_gates_after_agent(
                        state,
                        source="fixer",
                        files_written=retry_files,
                        before_snapshot=retry_test_gate_before,
                    )
                )
                retry_synthetic_findings = self._audit_synthetic_test_harnesses_after_agent(
                    state,
                    source="fixer",
                    files_written=retry_files,
                    before_snapshot=retry_test_gate_before,
                    include_active=True,
                )
                retry_files = set(self._agent_files_still_changed_since_snapshot(retry_files, retry_test_gate_before))
                if retry_synthetic_findings:
                    self._recover_synthetic_test_harness_after_agent(
                        state,
                        source="fixer",
                        findings=retry_synthetic_findings,
                        before_snapshot=retry_test_gate_before,
                        will_retry=False,
                    )
                    retry_files = set(
                        self._agent_files_still_changed_since_snapshot(retry_files, retry_test_gate_before)
                    )
                    self._store.save(state)
                fixer_files = first_pass_remaining_files | retry_files
        self._mark_build_sync_stale_if_needed(fixer_files, "fixer", state)
        if fixer_files:
            test_only_fix = self._fixer_change_is_test_only(fixer_files)
            self._session_code_changed = True
            state.fixer_changed_code = True
            if test_only_fix:
                paths = ", ".join(sorted(fixer_files))
                state.security_approved = False
                state.security_review_iterations = 0
                if state.active_scope == _SCOPE_FINAL_FULL_TASK:
                    state.final_full_task_review_done = False
                state.record(
                    "orchestrator",
                    "test_only_fix",
                    f"review/test-writer gates preserved; security review invalidated for test-only fix: {paths}",
                )
            else:
                self._invalidate_delivery_no_change_outcome(state, fixer_files, source="fixer")
                state.review_approved = False
                state.security_approved = False
                state.review_iterations = 0
                state.security_review_iterations = 0
                state.tests_up_to_date = False
                if state.active_scope == _SCOPE_FINAL_FULL_TASK:
                    state.final_full_task_review_done = False
            self._store.save(state)
        elif gate_findings:
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

    @staticmethod
    def _path_in_write_roots(path: str, roots: tuple[str, ...] | list[str]) -> bool:
        normalized_path = _normalize_project_path(path)
        if not normalized_path:
            return False
        for raw_root in roots:
            root = _native_scope_path(raw_root).strip()
            normalized_root = _normalize_project_path(root)
            if not root:
                continue
            if posixpath.normpath(root) == ".":
                return True
            if not normalized_root:
                continue
            if any(ch in root for ch in "*?["):
                if _path_matches_pattern(normalized_path, root):
                    return True
                continue
            if _path_is_under_root(normalized_path, normalized_root):
                return True
        return False

    def _is_configured_test_write_path(self, path: str) -> bool:
        return self._path_in_write_roots(path, self._configured_test_write_paths())

    def _fixer_change_is_test_only(self, files_written) -> bool:
        paths = [str(path) for path in files_written if str(path).strip()]
        return bool(paths) and all(
            self._is_configured_test_write_path(path) and _path_looks_like_test_artifact(path) for path in paths
        )

    def _test_execution_gate_snapshot(self) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for path in self._iter_configured_test_files():
            relative = self._relative_project_path(path)
            if not relative:
                continue
            snapshot[relative] = self._read_project_text(relative)
        return snapshot

    def _test_execution_gate_count_snapshot(self, snapshot: dict[str, str | None]) -> dict[str, dict[str, int]]:
        return {path: test_execution_gate_signature_counts(text) for path, text in snapshot.items()}

    def _test_writer_restore_snapshot(self, state: TaskState, extra_paths=()) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        candidates = [
            *state.files_changed,
            *state.test_files_written,
            *state.test_writer_audit_files_written,
            *self._dirty_test_audit_paths(),
            *(str(path) for path in extra_paths),
        ]
        for raw_path in candidates:
            path = _normalize_project_path(str(raw_path))
            if (
                path
                and self._is_configured_test_write_path(path)
                and _path_looks_like_test_audit_candidate(path)
                and path not in snapshot
            ):
                snapshot[path] = self._read_project_text(path)
        return snapshot

    def _git_baseline_snapshot_for_paths(self, paths) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for raw_path in paths:
            path = _normalize_project_path(str(raw_path))
            if path:
                snapshot[path] = self._read_git_head_project_text(path)
        return snapshot

    def _begin_test_writer_audit_pending(
        self,
        state: TaskState,
        before_snapshot: dict[str, str | None],
    ) -> None:
        self._store.save_text_snapshot(state.task_id, _TEST_WRITER_AUDIT_SNAPSHOT, before_snapshot)
        state.test_writer_audit_pending = True
        state.test_writer_audit_agent_completed = False
        state.test_writer_audit_files_written = []
        state.test_writer_audit_gate_counts = self._test_execution_gate_count_snapshot(before_snapshot)
        state.tests_up_to_date = False
        self._store.save(state)

    def _load_test_writer_audit_snapshot(self, state: TaskState) -> dict[str, str | None] | None:
        return self._store.load_text_snapshot(state.task_id, _TEST_WRITER_AUDIT_SNAPSHOT)

    def _persist_test_writer_audit_restore_baselines(self, state: TaskState, paths) -> dict[str, str | None]:
        snapshot = self._load_test_writer_audit_snapshot(state) or {}
        changed = False
        for raw_path in paths:
            path = _normalize_project_path(str(raw_path))
            if (
                path
                and self._is_configured_test_write_path(path)
                and _path_looks_like_test_audit_candidate(path)
                and path not in snapshot
            ):
                baseline = self._read_git_head_project_text(path)
                snapshot[path] = baseline
                state.test_writer_audit_gate_counts[path] = test_execution_gate_signature_counts(baseline)
                changed = True
        if changed:
            self._store.save_text_snapshot(state.task_id, _TEST_WRITER_AUDIT_SNAPSHOT, snapshot)
        return snapshot

    def _record_test_writer_audit_files(self, state: TaskState, files_written) -> list[str]:
        files = list(dict.fromkeys(str(path) for path in files_written if str(path).strip()))
        self._persist_test_writer_audit_restore_baselines(state, files)
        state.test_writer_audit_agent_completed = True
        state.test_writer_audit_files_written = files
        self._store.save(state)
        return files

    def _test_files_changed_since_snapshot(self, before_snapshot: dict[str, str | None]) -> list[str]:
        paths = {path for raw_path in before_snapshot for path in [_normalize_project_path(str(raw_path))] if path}
        changed: list[str] = []
        for path in sorted(paths):
            if before_snapshot.get(path) != self._read_project_text(path):
                changed.append(path)
        return changed

    def _dirty_test_audit_paths(self) -> list[str]:
        root = self._config.project_root.resolve()
        ignored_roots = self._validation_artifact_ignored_roots(root)
        snapshot = snapshot_validation_dirty_files(root, ignored_roots=ignored_roots)
        paths = sorted(
            path
            for path in snapshot
            if self._is_configured_test_write_path(path) and _path_looks_like_test_audit_candidate(path)
        )
        if paths:
            return paths
        return sorted(
            relative
            for path in self._iter_configured_test_files()
            for relative in [self._relative_project_path(path)]
            if relative
            and self._is_configured_test_write_path(relative)
            and _path_looks_like_test_audit_candidate(relative)
            and self._path_has_pending_changes(relative)
        )

    def _pending_test_writer_audit_files(self, state: TaskState) -> list[str]:
        candidates = state.test_writer_audit_files_written
        if not candidates:
            candidates = state.test_files_written
        if not candidates:
            candidates = [
                relative
                for path in self._iter_configured_test_files()
                for relative in [self._relative_project_path(path)]
                if relative
            ]
        return list(dict.fromkeys(str(path) for path in candidates if str(path).strip()))

    def _clear_test_writer_audit_pending(self, state: TaskState) -> None:
        if (
            not state.test_writer_audit_pending
            and not state.test_writer_audit_agent_completed
            and not state.test_writer_audit_files_written
            and not state.test_writer_audit_gate_counts
        ):
            self._store.delete_text_snapshot(state.task_id, _TEST_WRITER_AUDIT_SNAPSHOT)
            return
        state.test_writer_audit_pending = False
        state.test_writer_audit_agent_completed = False
        state.test_writer_audit_files_written = []
        state.test_writer_audit_gate_counts = {}
        self._store.save(state)
        self._store.delete_text_snapshot(state.task_id, _TEST_WRITER_AUDIT_SNAPSHOT)

    def _iter_configured_test_files(self):
        project_root = self._config.project_root.resolve()
        for raw_root in self._configured_test_write_paths():
            raw = _native_scope_path(raw_root).strip()
            if not raw:
                continue
            candidates: list[Path]
            if any(ch in raw for ch in "*?["):
                candidates = [path for path in project_root.glob(raw)]
            else:
                normalized = _normalize_project_path(raw)
                root = project_root if not normalized else project_root.joinpath(*Path(normalized).parts)
                candidates = [root]

            for candidate in candidates:
                candidate = candidate.resolve(strict=False)
                if candidate.is_file():
                    relative = self._relative_project_path(candidate)
                    if relative and _path_looks_like_test_audit_candidate(relative):
                        yield candidate
                    continue
                if not candidate.is_dir():
                    continue
                for path in candidate.rglob("*"):
                    if not path.is_file():
                        continue
                    relative = self._relative_project_path(path)
                    if not relative or self._path_is_internal(relative):
                        continue
                    if _path_looks_like_test_audit_candidate(relative):
                        yield path

    def _relative_project_path(self, path: Path) -> str | None:
        try:
            relative = path.resolve(strict=False).relative_to(self._config.project_root.resolve())
        except (OSError, ValueError):
            return None
        relative_text = relative.as_posix()
        return relative_text if relative_text and relative_text != "." else None

    def _path_is_internal(self, path: str) -> bool:
        parts = _path_parts(path)
        return bool(parts) and parts[0] in {".git", ".sikula"}

    def _read_project_text(self, path: str) -> str | None:
        normalized = _normalize_project_path(path)
        if not normalized:
            return None
        file_path = self._config.project_root.joinpath(*Path(normalized).parts)
        try:
            return file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _read_git_head_project_text(self, path: str) -> str | None:
        normalized = _normalize_project_path(path)
        if not normalized:
            return None
        try:
            result = subprocess.run(
                ["git", "show", f"HEAD:./{normalized}"],
                capture_output=True,
                text=True,
                cwd=self._config.project_root,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def _audit_test_execution_gates_after_agent(
        self,
        state: TaskState,
        *,
        source: str,
        files_written,
        before_snapshot: dict[str, str | None],
        before_gate_counts: dict[str, dict[str, int]] | None = None,
    ) -> list[dict]:
        paths = sorted(
            {
                _normalize_project_path(str(path))
                for path in files_written
                if self._is_configured_test_write_path(str(path)) and _path_looks_like_test_audit_candidate(str(path))
            }
        )
        findings: list[dict] = []
        for path in paths:
            if not path:
                continue
            findings.extend(
                detect_new_test_execution_gates(
                    path=path,
                    before=before_snapshot.get(path),
                    after=self._read_project_text(path),
                    before_counts=(before_gate_counts or {}).get(path),
                )
            )
        if not findings:
            return []

        state.record_test_execution_gate_audit(source, findings)
        self._refresh_test_execution_gate_errors(state)
        return findings

    def _audit_synthetic_test_harnesses_after_agent(
        self,
        state: TaskState,
        *,
        source: str,
        files_written,
        before_snapshot: dict[str, str | None],
        include_active: bool = False,
    ) -> list[dict]:
        paths = sorted(
            {
                _normalize_project_path(str(path))
                for path in files_written
                if self._is_configured_test_write_path(str(path)) and _path_looks_like_test_audit_candidate(str(path))
            }
        )
        active_before = self._refresh_synthetic_test_harness_audits(state)
        active_keys = {_synthetic_harness_finding_key(finding) for finding in active_before}
        findings: list[dict] = []
        for path in paths:
            if not path:
                continue
            findings.extend(
                detect_new_synthetic_test_harnesses(
                    path=path,
                    before=before_snapshot.get(path),
                    after=self._read_project_text(path),
                )
            )
        new_findings = [finding for finding in findings if _synthetic_harness_finding_key(finding) not in active_keys]
        if new_findings:
            state.record_synthetic_test_harness_audit(source, new_findings)
        self._refresh_synthetic_test_harness_audits(state)
        return findings if include_active else new_findings

    def _synthetic_harness_paths(self, findings: list[dict]) -> list[str]:
        return sorted(
            {
                normalized
                for finding in findings
                for normalized in [_normalize_project_path(str(finding.get("path", "")))]
                if normalized
                and self._is_configured_test_write_path(normalized)
                and _path_looks_like_test_audit_candidate(normalized)
            }
        )

    def _path_has_pending_changes(self, path: str) -> bool:
        normalized = _normalize_project_path(path)
        if not normalized:
            return False
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--", normalized],
                capture_output=True,
                text=True,
                cwd=self._config.project_root,
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            return bool(result.stdout.strip())
        return self._read_project_text(normalized) != self._read_git_head_project_text(normalized)

    def _prune_clean_test_state_paths(
        self,
        state: TaskState,
        paths: list[str],
        *,
        before_snapshot: dict[str, str | None] | None = None,
    ) -> None:
        clean_paths = {
            path
            for path in (_normalize_project_path(candidate) for candidate in paths)
            if path and not self._path_has_pending_changes(path)
        }
        if not clean_paths:
            return
        snapshot = (
            {
                _normalize_project_path(path): content
                for path, content in before_snapshot.items()
                if _normalize_project_path(path)
            }
            if before_snapshot is not None
            else None
        )
        files_changed_prune_paths = clean_paths
        if state.review_mode in {"review_report", "review_fix"}:
            files_changed_prune_paths = {
                path for path in clean_paths if snapshot is not None and path in snapshot and snapshot[path] is None
            }
        if files_changed_prune_paths:
            state.files_changed = [
                path
                for path in state.files_changed
                if _normalize_project_path(str(path)) not in files_changed_prune_paths
            ]
            if state.step_file_tracking_enabled is True and isinstance(state.step_files_changed, list):
                state.step_files_changed = [
                    path
                    for path in state.step_files_changed
                    if _normalize_project_path(str(path)) not in files_changed_prune_paths
                ]
        state.test_files_written = [
            path for path in state.test_files_written if _normalize_project_path(str(path)) not in clean_paths
        ]

    def _agent_files_still_changed_since_snapshot(
        self,
        files_written,
        before_snapshot: dict[str, str | None],
    ) -> list[str]:
        still_changed: list[str] = []
        for raw_path in files_written:
            path = _normalize_project_path(str(raw_path))
            if not path:
                continue
            if (
                self._is_configured_test_write_path(path)
                and _path_looks_like_test_audit_candidate(path)
                and before_snapshot.get(path) == self._read_project_text(path)
            ):
                continue
            if path not in still_changed:
                still_changed.append(path)
        return still_changed

    def _restore_target_project_path(self, path: str) -> tuple[Path | None, str | None]:
        normalized = _normalize_project_path(path)
        if not normalized:
            return None, f"{path}: invalid restore path"
        project_root = self._config.project_root.resolve()
        current = project_root
        for part in Path(normalized).parts:
            current = current / part
            try:
                if current.is_symlink():
                    relative = current.relative_to(project_root).as_posix()
                    return None, f"{normalized}: restore path uses symlink component `{relative}`"
            except OSError as exc:
                return None, f"{normalized}: could not inspect restore path: {exc}"
        try:
            current.resolve(strict=False).relative_to(project_root)
        except (OSError, ValueError) as exc:
            return None, f"{normalized}: restore path escapes project root: {exc}"
        return current, None

    def _restore_test_file_paths_from_snapshot(
        self,
        state: TaskState,
        *,
        paths: list[str],
        before_snapshot: dict[str, str | None],
    ) -> tuple[list[str], list[str]]:
        restored: list[str] = []
        errors: list[str] = []
        for raw_path in paths:
            path = _normalize_project_path(raw_path)
            if not path:
                continue
            file_path, path_error = self._restore_target_project_path(path)
            if path_error or file_path is None:
                errors.append(path_error or f"{path}: invalid restore path")
                continue
            before = before_snapshot.get(path)
            try:
                if before is None:
                    if file_path.exists() or file_path.is_symlink():
                        file_path.unlink()
                    restored.append(path)
                else:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(before, encoding="utf-8")
                    restored.append(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")

        self._prune_clean_test_state_paths(state, restored, before_snapshot=before_snapshot)
        self._refresh_test_execution_gate_errors(state)
        return restored, errors

    def _restore_test_files_from_snapshot(
        self,
        state: TaskState,
        *,
        source: str,
        paths: list[str],
        before_snapshot: dict[str, str | None],
        will_retry: bool,
    ) -> tuple[list[str], list[str]]:
        restored, errors = self._restore_test_file_paths_from_snapshot(
            state,
            paths=paths,
            before_snapshot=before_snapshot,
        )
        if errors:
            action = "synthetic_test_harness_recovery_failed"
            result = f"{source} synthetic harness recovery could not restore {', '.join(paths)}: {'; '.join(errors)}"
        else:
            action = "synthetic_test_harness_recovered"
            suffix = "retrying agent" if will_retry else "continuing without the synthetic harness"
            result = f"{source} synthetic harness restored {', '.join(restored) or '(no files)'}; {suffix}"
        state.record("orchestrator", action, result[:500])
        return restored, errors

    def _restore_interrupted_test_writer_outputs(
        self,
        state: TaskState,
        before_snapshot: dict[str, str | None],
    ) -> tuple[list[str], list[str]]:
        restore_snapshot = dict(before_snapshot)
        for path in self._dirty_test_audit_paths():
            if path not in restore_snapshot:
                restore_snapshot[path] = self._read_git_head_project_text(path)
        paths = self._test_files_changed_since_snapshot(restore_snapshot)
        if not paths:
            return [], []
        restored, errors = self._restore_test_file_paths_from_snapshot(
            state,
            paths=paths,
            before_snapshot=restore_snapshot,
        )
        if errors:
            state.record(
                "orchestrator",
                "test_writer_interrupted_output_restore_failed",
                f"could not restore interrupted test-writer output: {'; '.join(errors)}"[:500],
            )
            state.failed = True
        else:
            state.record(
                "orchestrator",
                "test_writer_interrupted_output_restored",
                f"restored interrupted test-writer output for {', '.join(restored) or '(no files)'}; rerunning agent",
            )
        self._store.save(state)
        return restored, errors

    def _record_synthetic_harness_testability_gap(
        self,
        state: TaskState,
        *,
        source: str,
        findings: list[dict],
    ) -> None:
        for finding in findings:
            path = str(finding.get("path") or "<unknown>")
            target = f"synthetic runtime harness in {path}"
            reason = (
                "Generated test recovery still produced a broad synthetic runtime/framework "
                "harness instead of narrow existing-seam coverage."
            )
            if any(
                gap.get("target") == target and gap.get("reason") == reason
                for gap in state.testability_gaps
                if isinstance(gap, dict)
            ):
                continue
            message = (
                "TESTABILITY GAP:\n"
                f"target: {target}\n"
                f"reason: {reason}\n"
                "covered_by: none\n"
                "recommended_action: add project-standard runtime/integration test infrastructure, "
                "expose a stable test seam, or keep this behaviour for human-reviewed integration coverage.\n"
                "risk: medium"
            )
            state.record_testability_gap(
                "orchestrator",
                message,
                target=target,
                reason=reason,
                covered_by="none",
                recommended_action=(
                    "add project-standard runtime/integration test infrastructure, expose a stable test seam, "
                    "or keep this behaviour for human-reviewed integration coverage"
                ),
                risk="medium",
            )
            state.record(
                "orchestrator",
                "synthetic_test_harness_recovery_gap",
                f"{source} recovery removed repeated synthetic harness in {path}",
            )

    def _recover_synthetic_test_harness_after_agent(
        self,
        state: TaskState,
        *,
        source: str,
        findings: list[dict],
        before_snapshot: dict[str, str | None],
        will_retry: bool,
    ) -> tuple[list[str], list[str]]:
        paths = self._synthetic_harness_paths(findings)
        if not paths:
            return [], []
        restored, errors = self._restore_test_files_from_snapshot(
            state,
            source=source,
            paths=paths,
            before_snapshot=before_snapshot,
            will_retry=will_retry,
        )
        if not will_retry or errors:
            self._record_synthetic_harness_testability_gap(state, source=source, findings=findings)
            self._refresh_synthetic_test_harness_audits(state)
        return restored, errors

    def _refresh_synthetic_test_harness_audits(self, state: TaskState) -> list[dict]:
        active_by_key: dict[tuple[str, tuple[str, ...]], dict] = {}
        for record in state.synthetic_test_harness_records:
            if record.get("status") == "resolved":
                continue
            active = active_synthetic_harness_findings_for_current_files(self._config.project_root, [record])
            if not active:
                record["status"] = "resolved"
                record["resolved_at"] = datetime.now(timezone.utc).isoformat()
                continue
            for finding in active:
                active_by_key[_synthetic_harness_finding_key(finding)] = finding
        return list(active_by_key.values())

    def _refresh_test_execution_gate_errors(self, state: TaskState) -> bool:
        if not state.test_execution_gate_records:
            return False

        self._clear_test_execution_gate_errors(state)
        active_findings = self._active_test_execution_gate_findings(state)
        if not active_findings:
            return False

        error = self._format_test_execution_gate_error(active_findings)
        state.test_errors.append(error)
        state.test_status = "failed"
        return True

    def _clear_test_execution_gate_errors(self, state: TaskState) -> None:
        state.test_errors = [
            error for error in state.test_errors if not str(error).startswith(_TEST_EXECUTION_GATE_AUDIT_MARKER)
        ]

    def _active_test_execution_gate_findings(self, state: TaskState) -> list[dict]:
        active_by_key: dict[tuple[str, str, str, str, str], dict] = {}
        for record in state.test_execution_gate_records:
            if record.get("status") == "resolved":
                continue
            active = active_findings_for_current_files(self._config.project_root, [record])
            if not active:
                record["status"] = "resolved"
                record["resolved_at"] = datetime.now(timezone.utc).isoformat()
                continue
            for finding in active:
                key = (
                    str(finding.get("path", "")),
                    str(finding.get("signature", "")),
                    str(finding.get("occurrence", "")),
                    str(finding.get("category", "")),
                    str(finding.get("reason", "")),
                )
                active_by_key[key] = finding
        return list(active_by_key.values())

    def _format_test_execution_gate_error(self, findings: list[dict]) -> str:
        lines = [
            _TEST_EXECUTION_GATE_AUDIT_MARKER,
            "Sikula detected newly added execution-gated test coverage in test files it modified.",
            "A generated or fixed test must not pass by skipping, disabling, ignoring, or environment-gating",
            "the changed behaviour out of the configured validation pipeline.",
            "",
            "Fix this by removing the execution gate and either adding real coverage through an existing",
            "stable project seam, or deleting the placeholder coverage and reporting a structured TESTABILITY GAP.",
            "",
            "Findings:",
        ]
        for finding in findings:
            path = finding.get("path", "<unknown>")
            line = finding.get("line", "?")
            category = finding.get("category", "gate")
            reason = finding.get("reason", "execution gate")
            lines.append(f"- {path}:{line} [{category}] {reason}")
        return "\n".join(lines)

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
        self._record_step_files_changed(state, paths)
        self._invalidate_delivery_no_change_outcome(state, paths, source="adopted build sync output")
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

    def _delivery_scope_audit_enabled(self, state: TaskState, name: str) -> bool:
        return self._delivery_scope_audit.enabled(state, name)

    def _delivery_scope_audit_policy(
        self,
        state: TaskState,
        name: str,
        *,
        active_test_write_paths: tuple[str, ...] | None = None,
    ) -> DeliveryScopeAuditPolicy:
        return self._delivery_scope_audit.policy(
            state,
            name,
            active_test_write_paths=active_test_write_paths,
        )

    def _set_delivery_scope_audit_pending(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
    ) -> None:
        self._delivery_scope_audit.set_pending(state, name, policy=policy)

    def _delivery_scope_audit_snapshot(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
    ) -> dict | None:
        return self._delivery_scope_audit.snapshot(state, name, policy=policy)

    def _record_delivery_scope_snapshot_failure(self, state: TaskState, name: str) -> bool:
        return self._delivery_scope_audit.record_snapshot_failure(state, name)

    def _clear_delivery_scope_audit_pending(self, state: TaskState) -> None:
        self._delivery_scope_audit.clear_pending(state)

    def _delivery_scope_provider_attempt_boundary(
        self,
        state: TaskState,
        name: str,
        policy: DeliveryScopeAuditPolicy,
        attempt: dict[str, object],
    ):
        return self._delivery_scope_audit.provider_attempt_boundary(state, name, policy, attempt)

    def _delivery_scope_tool_mutation_boundary(self, state: TaskState, phase: str):
        return self._delivery_scope_audit.tool_mutation_boundary(state, phase)

    def _audit_delivery_scope_after_mutation(
        self,
        state: TaskState,
        name: str,
        before: dict | None,
        policy: DeliveryScopeAuditPolicy | None,
        **kwargs,
    ) -> bool:
        return self._delivery_scope_audit.audit_after_mutation(state, name, before, policy, **kwargs)

    def _audit_interrupted_delivery_scope(self, state: TaskState) -> bool:
        return self._delivery_scope_audit.recover_interrupted(state)

    def _run_agent(self, name: str, state: TaskState):
        from agents.base_agent import AgentResult

        agent = self._agents[name]
        prepare_workspace = getattr(getattr(agent, "llm", None), "prepare_write_agent_workspace", None)
        if name in {"implementer", "fixer", "test_writer"} and callable(prepare_workspace):
            try:
                prepare_workspace(self._config.project_root)
            except Exception as exc:
                message = f"provider workspace setup failed: {exc}"
                state.record(name, "workspace_setup_failed", message[:500])
                state.failed = True
                self._store.save(state)
                return AgentResult(success=False, message=message[:200])
        delivery_scope_enabled = self._delivery_scope_audit_enabled(state, name)
        delivery_scope_policy: DeliveryScopeAuditPolicy | None = None
        try:
            if delivery_scope_enabled:
                delivery_scope_policy = self._delivery_scope_audit_policy(state, name)
                self._set_delivery_scope_audit_pending(state, name, policy=delivery_scope_policy)
            delivery_scope_before = self._delivery_scope_audit_snapshot(
                state,
                name,
                policy=delivery_scope_policy,
            )
        except DeliveryScopeSnapshotError:
            self._record_delivery_scope_snapshot_failure(state, name)
            if delivery_scope_enabled:
                self._clear_delivery_scope_audit_pending(state)
            return AgentResult(success=False, message="delivery_scope_audit_unavailable")
        log.info(f"Running {name} agent...")
        t0 = time.perf_counter()
        hist_len = len(state.history)
        set_session_title = getattr(getattr(agent, "llm", None), "set_session_title", None)
        set_write_attempt_boundary = getattr(
            getattr(agent, "llm", None),
            "set_write_attempt_boundary",
            None,
        )
        previous_title = set_session_title(_agent_session_title(name, state)) if callable(set_session_title) else None
        previous_write_attempt_boundary = None
        provider_attempt_stopped = False
        if delivery_scope_policy is not None and callable(set_write_attempt_boundary):
            previous_write_attempt_boundary = set_write_attempt_boundary(
                lambda attempt: self._delivery_scope_provider_attempt_boundary(
                    state,
                    name,
                    delivery_scope_policy,
                    attempt,
                )
            )
        try:
            with self._active_operation(
                state,
                phase="agent",
                agent=name,
                message=f"Running {name} agent",
            ):
                try:
                    with llm_retry_history(agent, name, state, self._store):
                        result = agent.run(state)
                except DeliveryScopeProviderAttemptStopped as exc:
                    result = AgentResult(success=False, message=str(exc))
                    provider_attempt_stopped = True
                except Exception as exc:
                    elapsed_s = time.perf_counter() - t0
                    log.error(f"{name} raised an unexpected error ({_fmt_elapsed(elapsed_s)}): {exc}")
                    state.record(name, "error", str(exc), elapsed_s=elapsed_s)
                    state.failed = True
                    self._store.save(state)
                    result = AgentResult(success=False, message=str(exc))
                provider_attempt_stopped = provider_attempt_stopped or (
                    delivery_scope_enabled and state.delivery_stop_code == DELIVERY_STOP_UNIT_SCOPE_VIOLATION
                )
                scope_audit_stopped = provider_attempt_stopped or self._audit_delivery_scope_after_mutation(
                    state,
                    name,
                    delivery_scope_before,
                    delivery_scope_policy,
                )
                if delivery_scope_enabled:
                    self._clear_delivery_scope_audit_pending(state)
                if scope_audit_stopped:
                    result = AgentResult(success=False, message=DELIVERY_SCOPE_VIOLATION_CODE)
                delivery_stop = self._abort_on_delivery_terminal_stop(state)
                if delivery_stop is not None:
                    result = AgentResult(success=False, message=delivery_stop)
        except Exception as exc:
            elapsed_s = time.perf_counter() - t0
            log.error(f"{name} raised an unexpected error ({_fmt_elapsed(elapsed_s)}): {exc}")
            state.record(name, "error", str(exc), elapsed_s=elapsed_s)
            state.failed = True
            self._store.save(state)
            result = AgentResult(success=False, message=str(exc))
        finally:
            if callable(set_write_attempt_boundary):
                set_write_attempt_boundary(previous_write_attempt_boundary)
            if callable(set_session_title):
                set_session_title(previous_title)
        elapsed_s = time.perf_counter() - t0
        if len(state.history) > hist_len:
            state.history[-1]["elapsed_s"] = round(elapsed_s, 1)
        elapsed = _fmt_elapsed(elapsed_s)
        if result.success:
            log.info(f"{name}: {result.message} ({elapsed})")
        else:
            log.error(f"{name} failed: {result.message} ({elapsed})")
        data = result.data if isinstance(result.data, dict) else {}
        self._record_step_files_changed(state, data.get("files_written", []))
        self._store.save(state)
        return result

    def _run_delivery_review_agent(self, name: str, state: TaskState):
        """Retry one malformed delivery review without consuming a fix attempt."""
        result = self._run_agent(name, state)
        data = result.data if isinstance(result.data, dict) else {}
        parse_error = data.get("disposition_parse_error")
        if (
            result.success
            or state.failed
            or not state.delivery_plan_id
            or not state.delivery_unit_id
            or name not in {"reviewer", "security_reviewer"}
            or not isinstance(parse_error, str)
            or not parse_error
        ):
            return result

        state.record(name, "review_protocol_retry", parse_error)
        self._store.save(state)
        log.warning("Retrying %s after invalid delivery disposition (%s)", name, parse_error)
        return self._run_agent(name, state)

    def _abort_on_failed_agent_result(self, state: TaskState, name: str, result) -> bool:
        if result.success:
            return False
        if name in {"reviewer", "security_reviewer"} and (result.data or {}).get("issues"):
            return False
        msg = f"{name} failed: {result.message}"
        log.error(msg)
        state.record("orchestrator", "abort", msg)
        state.failed = True
        self._store.save(state)
        return True

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
        if not self._config.run_test_writing:
            return False
        if state.tests_up_to_date and not state.test_writer_audit_pending:
            if not self._config.run_build:
                self._abort_no_build_with_active_test_execution_gates(state)
            return False
        log.info(f"--- Phase: {_phase_scope_label(state)}test write ---")
        pending_resume = state.test_writer_audit_pending
        before_gate_counts: dict[str, dict[str, int]] | None = None
        pending_snapshot_missing = False
        pending_agent_incomplete = pending_resume and not state.test_writer_audit_agent_completed
        if pending_resume and not pending_agent_incomplete:
            saved_test_gate_before = self._load_test_writer_audit_snapshot(state)
            pending_snapshot_missing = saved_test_gate_before is None
            test_gate_before = saved_test_gate_before or {}
            before_gate_counts = dict(state.test_writer_audit_gate_counts)
            files_written = self._pending_test_writer_audit_files(state)
        else:
            with self._active_operation(
                state,
                phase="test_writer audit prep",
                message="Preparing test-writer audit baseline",
            ):
                if pending_agent_incomplete:
                    saved_test_gate_before = self._load_test_writer_audit_snapshot(state)
                    if saved_test_gate_before is None:
                        msg = "pending test-writer audit snapshot missing before agent completed"
                        state.record("orchestrator", "abort", msg)
                        state.failed = True
                        self._store.save(state)
                        return False
                    test_gate_before = saved_test_gate_before
                    self._restore_interrupted_test_writer_outputs(state, test_gate_before)
                    if state.failed:
                        return False
                    if not state.test_writer_audit_gate_counts:
                        state.test_writer_audit_gate_counts = self._test_execution_gate_count_snapshot(test_gate_before)
                    before_gate_counts = dict(state.test_writer_audit_gate_counts)
                    state.test_writer_audit_files_written = []
                    state.tests_up_to_date = False
                    self._store.save(state)
                else:
                    test_gate_before = self._test_writer_restore_snapshot(state)
                    self._begin_test_writer_audit_pending(state, test_gate_before)
                    before_gate_counts = dict(state.test_writer_audit_gate_counts)
            result = self._run_agent("test_writer", state)
            if state.failed or self._abort_on_failed_agent_result(state, "test_writer", result):
                return False
            files_written = self._record_test_writer_audit_files(
                state,
                (result.data or {}).get("files_written", []),
            )
            test_gate_before = self._load_test_writer_audit_snapshot(state) or test_gate_before
        gate_findings = self._audit_test_execution_gates_after_agent(
            state,
            source="test_writer",
            files_written=files_written,
            before_snapshot=test_gate_before,
            before_gate_counts=before_gate_counts,
        )
        synthetic_findings = self._audit_synthetic_test_harnesses_after_agent(
            state,
            source="test_writer",
            files_written=files_written,
            before_snapshot=test_gate_before,
        )
        if synthetic_findings and not state.failed:
            will_retry_synthetic_harness = _SYNTHETIC_HARNESS_RECOVERY_MAX_RETRIES > 0
            recovery_snapshot = test_gate_before
            if pending_resume and pending_snapshot_missing:
                recovery_snapshot = self._git_baseline_snapshot_for_paths(
                    self._synthetic_harness_paths(synthetic_findings)
                )
            restored, restore_errors = self._recover_synthetic_test_harness_after_agent(
                state,
                source="test_writer",
                findings=synthetic_findings,
                before_snapshot=recovery_snapshot,
                will_retry=will_retry_synthetic_harness,
            )
            first_pass_remaining_files = self._agent_files_still_changed_since_snapshot(
                files_written, recovery_snapshot
            )
            files_written = first_pass_remaining_files
            if restored and not restore_errors and will_retry_synthetic_harness:
                state.tests_up_to_date = False
                state.test_writer_audit_agent_completed = False
            self._store.save(state)
            if restored and not restore_errors and will_retry_synthetic_harness:
                if pending_resume:
                    log.info("Retrying test writer agent after pending synthetic test harness recovery")
                else:
                    log.info("Retrying test writer agent after synthetic test harness recovery")
                with self._active_operation(
                    state,
                    phase="test_writer audit prep",
                    message="Preparing test-writer audit baseline",
                ):
                    retry_test_gate_before = self._test_writer_restore_snapshot(state, extra_paths=files_written)
                    self._begin_test_writer_audit_pending(state, retry_test_gate_before)
                result = self._run_agent("test_writer", state)
                if state.failed or self._abort_on_failed_agent_result(state, "test_writer", result):
                    return False
                retry_files_written = self._record_test_writer_audit_files(
                    state,
                    (result.data or {}).get("files_written", []),
                )
                retry_test_gate_before = self._load_test_writer_audit_snapshot(state) or retry_test_gate_before
                gate_findings.extend(
                    self._audit_test_execution_gates_after_agent(
                        state,
                        source="test_writer",
                        files_written=retry_files_written,
                        before_snapshot=retry_test_gate_before,
                        before_gate_counts=state.test_writer_audit_gate_counts,
                    )
                )
                retry_synthetic_findings = self._audit_synthetic_test_harnesses_after_agent(
                    state,
                    source="test_writer",
                    files_written=retry_files_written,
                    before_snapshot=retry_test_gate_before,
                    include_active=True,
                )
                retry_files_written = self._agent_files_still_changed_since_snapshot(
                    retry_files_written,
                    retry_test_gate_before,
                )
                if retry_synthetic_findings:
                    self._recover_synthetic_test_harness_after_agent(
                        state,
                        source="test_writer",
                        findings=retry_synthetic_findings,
                        before_snapshot=retry_test_gate_before,
                        will_retry=False,
                    )
                    retry_files_written = self._agent_files_still_changed_since_snapshot(
                        retry_files_written,
                        retry_test_gate_before,
                    )
                    self._store.save(state)
                files_written = list(dict.fromkeys([*first_pass_remaining_files, *retry_files_written]))
        self._clear_test_writer_audit_pending(state)
        self._abort_no_build_with_active_test_execution_gates(state)
        self._mark_build_sync_stale_if_needed(files_written, "test writer", state)
        return bool(files_written)

    def _abort_no_build_with_active_test_execution_gates(self, state: TaskState) -> bool:
        if self._config.run_build or not self._active_test_execution_gate_findings(state):
            return False
        self._refresh_test_execution_gate_errors(state)
        msg = "test execution gate audit found issues but the build/fix loop is disabled"
        state.record("orchestrator", "abort", msg)
        state.failed = True
        self._store.save(state)
        return True

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
                try:
                    with self._active_operation(
                        state,
                        phase="check_autofix",
                        message=f"Running check/{name} autofix",
                    ):
                        with self._delivery_scope_tool_mutation_boundary(state, "check_autofix"):
                            fix_result = build_tool.run_check(f"{name}_autofix", fix_cfg)
                except DeliveryScopeToolMutationStopped:
                    state.check_status = "failed"
                    self._store.save(state)
                    return False
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
        try:
            with self._active_operation(state, phase="sync", message="Running build sync"):
                with self._delivery_scope_tool_mutation_boundary(state, "sync"):
                    result = build_tool.sync()
        except DeliveryScopeToolMutationStopped:
            return False
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
