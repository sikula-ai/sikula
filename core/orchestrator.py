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
                build-config files changed; triggers re-review + re-test-write if files changed

If state.plan is populated (by PlannerAgent), phases 2-4 run once per step before advancing.
Phase 5 runs per step only when run_build_per_step is true; otherwise it runs once after
all planned steps complete. If state.plan is empty, a single pass through phases 2-5 is used.
"""

from __future__ import annotations

import logging
import os
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
from core.llm_client import LLMClient
from core.retry_history import llm_retry_history
from core.state import StateStore, TaskState
from tools.base_tool import BuildTool, Sandbox
from tools.file_tool import FileTool
from tools.git_tool import GitTool
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.python_tool import PythonTool

log = logging.getLogger(__name__)


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
            compile_command=build.get("compile_command", "cargo check"),
            test_command=build.get("test_command", "cargo test"),
            timeout=build.get("timeout", 600),
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

        pc = config.project_config
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

        # Phase 0: presync — generate sources before analyze (run_presync: true only)
        # Skipped on resume if already attempted. Failure is a warning, not an abort —
        # analyst proceeds with whatever is in build/ from prior builds.
        if self._config.run_presync and not state.presync_done:
            self._run_presync(state)

        # Phase 1: analyze (idempotent — skipped if prompt already exists)
        if not state.implementation_prompt:
            log.info("--- Phase: analyze ---")
            self._run_agent("analyst", state)
            if state.failed:
                self._store.save(state)
                return

        # Phase 1.5: plan (skipped if planner already ran — plan_decided guards resume)
        if self._config.run_planner and not state.plan_decided:
            log.info("--- Phase: plan ---")
            self._run_agent("planner", state)
            if state.failed:
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
        while state.current_step < total_steps and not state.failed:
            step_idx = state.current_step
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
                # All steps implemented, reviewed, and tests written
                if not state.files_changed:
                    log.error("All steps were skipped — no file changes produced — task failed")
                    state.record("orchestrator", "abort", "all steps skipped — no file changes")
                    state.failed = True
                    self._store.save(state)
                    return
                if self._config.run_build and not self._config.run_build_per_step:
                    # Deferred build: run the build/fix loop once after all steps
                    log.info("--- Phase: build/fix (after all steps) ---")
                    self._run_build_fix_loop(state, set_done=True)
                else:
                    if not self._config.run_build:
                        state.test_status = "skipped"
                        state.check_status = "skipped"
                    state.done = True
                    self._store.save(state)
                return  # last step complete — exit step loop regardless of outcome

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
                error=result.error[-500:],
            )
            state.record_validation("presync", "failed", elapsed_s=elapsed_s, error=result.error)
            log.warning(
                "Pre-analyze sync failed (%s) — analyst will proceed with sources available in build/: %s",
                _fmt_elapsed(elapsed_s),
                result.error[-200:],
            )
        self._store.save(state)

    # ------------------------------------------------------------------
    # Build / fix loop (shared)
    # ------------------------------------------------------------------

    def _run_build_fix_loop(self, state: TaskState, set_done: bool) -> bool:
        """Sync → build → test → fix until passing or max_iterations reached.

        set_done=True  — sets state.done on success (single-pass mode)
        set_done=False — returns True on success without touching state.done (step-loop mode)
        """
        while not state.failed and state.build_iterations < self._config.max_iterations:
            state.build_iterations += 1

            if not state.build_synced:
                log.info(f"--- Phase: sync ({state.build_iterations}/{self._config.max_iterations}) ---")
                if not self._sync(state):
                    if not self._run_fix_phase(state):
                        return False
                    self._store.save(state)
                    continue

            log.info(f"--- Phase: build ({state.build_iterations}/{self._config.max_iterations}) ---")
            if not self._build(state):
                if not self._run_fix_phase(state):
                    return False
                self._store.save(state)
                continue

            if self._config.run_tests:
                log.info(f"--- Phase: test ({state.build_iterations}/{self._config.max_iterations}) ---")
                if not self._run_tests(state):
                    if not self._run_fix_phase(state):
                        return False
                    self._store.save(state)
                    continue
            else:
                state.test_status = "skipped"
                state.record_validation("test", "skipped")

            if self._config.run_checks:
                if not self._run_checks(state):
                    if not self._run_fix_phase(state):
                        return False
                    self._store.save(state)
                    continue
            else:
                state.check_status = "skipped"
                state.record_validation("check", "skipped")

            # Passing build (and tests/checks if enabled)
            if set_done:
                state.done = True
                self._store.save(state)
            return True

        if not state.failed:
            log.error(
                "Reached max build iterations (%d) without a passing build — task failed",
                self._config.max_iterations,
            )
            state.record("orchestrator", "abort", "max build iterations reached")
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
            log.info(f"--- Phase: review ({label}) ---")
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
            log.info(f"--- Phase: implement (review fix {state.review_iterations}/{max_fixes}) ---")
            self._run_agent("implementer", state)
            self._session_code_changed = True
            if state.failed:
                return
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
            log.info(f"--- Phase: security review ({sec_label}) ---")
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
            log.info(f"--- Phase: implement (security fix {state.security_review_iterations}/{max_iter}) ---")
            self._run_agent("implementer", state)
            self._session_code_changed = True
            if state.failed:
                return
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

    def _run_fix_phase(self, state: TaskState) -> bool:
        """Run fixer, update sync/review/test flags. Returns True to continue, False if failed."""
        log.info(f"--- Phase: fix ({state.build_iterations}/{self._config.max_iterations}) ---")
        fixer_result = self._run_agent("fixer", state)
        if state.failed:
            return False
        # Use files reported by this fixer call — not a set-diff on state.files_changed, which
        # would miss re-edits of files already in the list (skipped by fixer dedup on line 127).
        fixer_files = set((fixer_result.data or {}).get("files_written", []))
        build_tool: BuildTool = self._tools["build"]
        if any(build_tool.is_build_config_file(f) for f in fixer_files):
            log.info("Fixer changed build-config files — will re-sync before next build")
            state.build_synced = False
        if fixer_files:
            self._session_code_changed = True
            state.fixer_changed_code = True
            state.review_approved = False
            state.security_approved = False
            state.review_iterations = 0
            state.security_review_iterations = 0
            state.tests_up_to_date = False
            self._store.save(state)
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
        return True

    def _run_agent(self, name: str, state: TaskState):
        from agents.base_agent import AgentResult

        agent = self._agents[name]
        log.info(f"Running {name} agent...")
        t0 = time.perf_counter()
        hist_len = len(state.history)
        try:
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

    def _run_test_write_phase(self, state: TaskState) -> bool:
        if not self._config.run_test_writing or state.tests_up_to_date:
            return False
        log.info("--- Phase: test write ---")
        result = self._run_agent("test_writer", state)
        return bool((result.data or {}).get("files_written"))

    def _run_checks(self, state: TaskState) -> bool:
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
            log.info(f"--- Phase: check/{name} ({state.build_iterations}/{self._config.max_iterations}) ---")
            t0 = time.perf_counter()
            result = build_tool.run_check(name, check)
            elapsed_s = time.perf_counter() - t0
            fix_command = check.get("fix_command")
            skip_final_failure_validation = False
            if not result.success and fix_command:
                state.record_validation(
                    "check", "failed", elapsed_s=elapsed_s, error=result.error or result.output, check_name=name
                )
                log.info(f"Check {name} failed — running fix_command: {fix_command}")
                fix_cfg: dict = {"command": fix_command}
                if "timeout" in check:
                    fix_cfg["timeout"] = check["timeout"]
                t0 = time.perf_counter()
                fix_result = build_tool.run_check(f"{name}_autofix", fix_cfg)
                fix_elapsed_s = time.perf_counter() - t0
                state.record("orchestrator", f"check_{name}_autofix", "success" if fix_result.success else "failed")
                state.record_validation(
                    "check_autofix",
                    "success" if fix_result.success else "failed",
                    elapsed_s=fix_elapsed_s,
                    error=fix_result.error,
                    check_name=name,
                )
                if fix_result.success:
                    t0 = time.perf_counter()
                    result = build_tool.run_check(name, check)
                    elapsed_s = time.perf_counter() - t0
                else:
                    skip_final_failure_validation = True
                    log.warning(f"Auto-fix for {name} failed: {fix_result.error[-500:]}")
            if result.success:
                log.info(f"Check {name} OK ({_fmt_elapsed(elapsed_s)})")
                state.record("orchestrator", f"check_{name}", "success", elapsed_s=elapsed_s)
                state.record_validation("check", "success", elapsed_s=elapsed_s, check_name=name)
            else:
                log.error(f"Check {name} failed ({_fmt_elapsed(elapsed_s)}):\n{result.error[-2000:]}")
                state.check_errors.append(f"[{name}]\n{result.output}")
                state.record("orchestrator", f"check_{name}", "failed", elapsed_s=elapsed_s)
                if not skip_final_failure_validation:
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
        result = build_tool.run_tests()
        elapsed_s = time.perf_counter() - t0
        if not result.success:
            log.error(f"Tests failed ({_fmt_elapsed(elapsed_s)}):\n{result.error[-2000:]}")
            state.test_errors.append(result.error[-3000:])
            state.test_status = "failed"
            state.record("orchestrator", "test", "failed", elapsed_s=elapsed_s)
            state.record_validation("test", "failed", elapsed_s=elapsed_s, error=result.error)
            return False
        log.info(f"Tests OK ({_fmt_elapsed(elapsed_s)})")
        state.test_status = "success"
        state.record("orchestrator", "test", "success", elapsed_s=elapsed_s)
        state.record_validation("test", "success", elapsed_s=elapsed_s)
        return True

    def _sync(self, state: TaskState) -> bool:
        build_tool: BuildTool = self._tools["build"]
        log.info(f"Running build sync ({build_tool.__class__.__name__}.sync()) — this may take a few minutes...")
        t0 = time.perf_counter()
        result = build_tool.sync()
        elapsed_s = time.perf_counter() - t0
        if result.success:
            state.build_synced = True
            state.record("orchestrator", "sync", "ok", elapsed_s=elapsed_s)
            state.record_validation("sync", "success", elapsed_s=elapsed_s)
            log.info(f"Build sync OK ({_fmt_elapsed(elapsed_s)})")
        else:
            state.record("orchestrator", "sync", "failed", elapsed_s=elapsed_s)
            state.record_validation("sync", "failed", elapsed_s=elapsed_s, error=result.error)
            log.error(f"Build sync failed ({_fmt_elapsed(elapsed_s)}):\n{result.error[-2000:]}")
            state.errors.append(result.error[-3000:])
        self._store.save(state)
        return result.success

    def _build(self, state: TaskState) -> bool:
        build_tool: BuildTool = self._tools["build"]
        log.info("Running compile check...")
        t0 = time.perf_counter()
        result = build_tool.compile_check()
        elapsed_s = time.perf_counter() - t0
        state.build_status = "success" if result.success else "failed"
        state.record("orchestrator", "build", state.build_status, elapsed_s=elapsed_s)
        state.record_validation(
            "build", state.build_status, elapsed_s=elapsed_s, error=None if result.success else result.error
        )
        if not result.success:
            log.error(f"Build failed ({_fmt_elapsed(elapsed_s)}):\n{result.error[-2000:]}")
            state.errors.append(result.error[-3000:])
            return False
        log.info(f"Build OK ({_fmt_elapsed(elapsed_s)})")
        state.fixer_changed_code = False
        return True


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"
