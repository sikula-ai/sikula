"""Fixer agent — runs the configured LLM as an autonomous agent to fix build or test errors.

The agent receives error output and task context, then navigates the codebase using its file
tools to locate and fix the affected files. Changed files are detected via git diff. Logical
and completeness issues are handled upstream by ReviewerAgent; the fixer's scope is strictly
limited to errors the build system or test runner reports.
"""

from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from typing import Any

from agents.base_agent import (
    AgentResult,
    BaseAgent,
    AGENT_SECURITY_PREFIX,
    guidelines_files as _guidelines_files,
    paths_outside_allowed as _paths_outside_allowed,
    record_write_path_warnings as _record_write_path_warnings,
    tech_stack as _tech_stack,
)
from core.state import TaskState

_AGENT_PROMPT = """\
You are fixing errors in a {tech_stack} codebase.
The working directory is the project root.

BEFORE YOU START — read these project guidelines:
{guidelines_files}
They define the architecture conventions, patterns, and rules you must follow.

CONSTRAINTS — follow strictly:
- You may only read files under these paths: {allowed_read_paths}
- You may only write to these directories: {allowed_write_paths}
- To delete a file, use Bash: `git rm <path>` — only for files within {allowed_write_paths}
- Bash is restricted to read-only commands and `git rm` only: `grep`, `find`, `ls`, `git rm`
  Do not run any other shell commands (`rm`, `mv`, `cp`, `curl`, `wget`, etc.)
- Fix ONLY what the errors describe — nothing more
- Do not refactor unrelated code
- Do not change behaviour beyond what the errors require
- Do not add comments or documentation unless required by the task or project guidelines.
  When modifying a function, class, or property that already has a doc comment, update it
  to stay accurate (e.g. add or remove @param entries) — do not delete it.
{test_constraint}

ORIGINAL TASK (for context):
{task_description}

IMPLEMENTATION THAT WAS APPLIED:
{implementation_prompt}

{errors_section}
"""

_BUILD_TEST_CONSTRAINT = """\
- NEVER create or modify unit tests — no files under any test source directory
  (e.g. `test/`, `__tests__/`, `spec/`), regardless of what the errors say"""

_TEST_TEST_CONSTRAINT = """\
- You MAY create and modify test files
- You MAY modify production source files when a failing test exposes a production defect
- First decide whether the failure is caused by production behaviour or by an incorrect/stale test
- Start your final response with:
  TEST FAILURE TRIAGE:
  classification: production_defect | stale_test | malformed_test | unclear
  contract_affected: <task/guideline/structured contract, or none>
  chosen_fix: production_code | test_code
- If a test encodes the original task, implementation prompt, project guidelines, or a structured
  input/output contract, fix production code instead of weakening the test
- Modify tests only when the test is malformed, stale, or inconsistent with the accepted contract
- If you choose test_code, explain which accepted contract the test conflicts with
- If the failure is caused by a malformed generated test or test harness assumption
  (for example current-working-directory assumptions, brittle source-file inspection,
  invalid test doubles, or unavailable test APIs), fix the test or test helper. Do not
  change production source, build configuration, runtime configuration, dependency
  declarations, or pipeline settings merely to satisfy a malformed test.
- Treat build files, dependency manifests, project/workspace files, generated-source config,
  and runtime configuration as production code for this triage. Changing them for a test
  failure requires `classification: production_defect` and `chosen_fix: production_code`.
- Do not delete, relax, or rewrite assertions just to make the run green"""

_CHECK_TEST_CONSTRAINT = """\
- You MAY modify production or test files if the check errors explicitly reference them
- Fix ONLY the files named in the check errors — nothing else"""

_DEFAULT_CONTEXT_FILES = ["README.md"]
_TEST_PATH_MARKERS = {
    "__tests__",
    "acceptancetest",
    "acceptancetests",
    "androidtest",
    "commontest",
    "commontests",
    "e2etest",
    "e2etests",
    "functionaltest",
    "functionaltests",
    "integrationtest",
    "integrationtests",
    "spec",
    "specs",
    "sharedtest",
    "sharedtests",
    "test",
    "testfixtures",
    "tests",
    "unittest",
    "unittests",
    "uitest",
    "uitests",
}
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
)
_TEST_FILE_PREFIXES = ("test_",)


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


def _test_constraint(state: TaskState) -> str:
    if state.errors:
        return _BUILD_TEST_CONSTRAINT
    if state.test_errors and not state.check_errors:
        return _TEST_TEST_CONSTRAINT
    # check_errors (detekt/lint) can reference test or production files — allow both
    return _CHECK_TEST_CONSTRAINT


def _write_paths_for_state(state: TaskState, sandbox: dict) -> list[str]:
    if state.errors:
        return sandbox.get("allowed_write_paths", [])

    paths: list[str] = []
    for key in ("allowed_write_paths", "allowed_test_write_paths"):
        for path in sandbox.get(key, []):
            if path not in paths:
                paths.append(path)
    return paths


def _normalize_project_path(path: str) -> str:
    return posixpath.normpath(str(path).replace("\\", "/")).lower()


def _path_parts(path: str) -> list[str]:
    normalized = _normalize_project_path(path)
    return [part for part in normalized.split("/") if part and part != "."]


def _is_test_path_marker(part: str) -> bool:
    return part in _TEST_PATH_MARKERS or part.endswith(("tests", "_test", "_tests", "-test", "-tests"))


def _root_is_test_specific(root: str) -> bool:
    return any(_is_test_path_marker(part) for part in _path_parts(root))


def _path_is_under_root(path: str, root: str) -> bool:
    normalized_path = _normalize_project_path(path)
    normalized_root = _normalize_project_path(root).rstrip("/")
    if normalized_root in ("", "."):
        return True
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _is_under_specific_test_root(path: str, roots: list[str]) -> bool:
    return any(_root_is_test_specific(root) and _path_is_under_root(path, root) for root in roots)


def _looks_like_test_artifact(path: str) -> bool:
    parts = _path_parts(path)
    if any(_is_test_path_marker(part) for part in parts[:-1]):
        return True
    if not parts:
        return False
    filename = parts[-1]
    return filename.startswith(_TEST_FILE_PREFIXES) or filename.endswith(_TEST_FILE_SUFFIXES)


def _is_build_config_file(build_tool: Any, path: str) -> bool:
    if not build_tool or not hasattr(build_tool, "is_build_config_file"):
        return False
    try:
        return bool(build_tool.is_build_config_file(path))
    except Exception:
        return False


def _test_failure_production_writes(changed: list[str], sandbox: dict, build_tool: Any = None) -> list[str]:
    test_paths = sandbox.get("allowed_test_write_paths", [])
    if not test_paths:
        return list(changed)
    outside_test_paths = set(_paths_outside_allowed(changed, test_paths))
    production: list[str] = []
    for path in changed:
        if path in outside_test_paths:
            production.append(path)
            continue
        if _is_under_specific_test_root(path, test_paths):
            continue
        if _looks_like_test_artifact(path):
            continue
        if _is_build_config_file(build_tool, path):
            production.append(path)
            continue
        production.append(path)
    return production


def _triage_field(output: str, field: str) -> str:
    prefix = f"{field.lower()}:"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix):
            return stripped.split(":", 1)[1].strip().lower()
    return ""


def _has_valid_production_test_failure_triage(output: str) -> bool:
    if "test failure triage:" not in output.lower():
        return False
    return (
        _triage_field(output, "classification") == "production_defect"
        and _triage_field(output, "chosen_fix") == "production_code"
    )


def _errors_section(state: TaskState) -> str:
    sections = []
    if state.errors:
        sections.append("BUILD ERRORS:\n" + "\n\n".join(state.errors[-3:]))
    if state.test_errors:
        sections.append("TEST FAILURES:\n" + "\n\n".join(state.test_errors[-3:]))
    if state.check_errors:
        sections.append("CHECK ERRORS:\n" + "\n\n".join(state.check_errors[-3:]))
    return "\n\n".join(sections)


class FixerAgent(BaseAgent):
    name = "fixer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.errors and not state.test_errors and not state.check_errors:
            return AgentResult(success=False, message="No errors in state to fix")

        file_tool = self.tools.get("file")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        sandbox = self.project_config.get("sandbox", {})
        allowed_write_paths = _write_paths_for_state(state, sandbox)
        allowed_str = ", ".join(allowed_write_paths) if allowed_write_paths else "(not configured)"
        allowed_read_paths = sandbox.get("allowed_read_paths", ["."])
        allowed_read_str = ", ".join(allowed_read_paths)

        prompt = AGENT_SECURITY_PREFIX + _AGENT_PROMPT.format(
            tech_stack=_tech_stack(self.project_config),
            guidelines_files=_guidelines_files(self.project_config),
            allowed_read_paths=allowed_read_str,
            allowed_write_paths=allowed_str,
            test_constraint=_test_constraint(state),
            task_description=state.task_description,
            implementation_prompt=state.implementation_prompt or "(not available)",
            errors_section=_errors_section(state),
        )

        errors_snapshot = {
            "build": list(state.errors),
            "test": list(state.test_errors),
            "check": list(state.check_errors),
        }

        try:
            changed, fixer_output = self.llm.run_agent(prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.record(self.name, "fix_failed", msg[:500])
            state.fix_cycle_records.append(
                {
                    "build_iteration": state.build_iterations,
                    "step": state.current_step,
                    "scope": _scope(state),
                    "errors_before": errors_snapshot,
                    "fixer_prompt": prompt,
                    "fixer_output": None,
                    "files_written": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            return AgentResult(success=False, message=msg[:200])

        state.fix_cycle_records.append(
            {
                "build_iteration": state.build_iterations,
                "step": state.current_step,
                "scope": _scope(state),
                "errors_before": errors_snapshot,
                "fixer_prompt": prompt,
                "fixer_output": fixer_output,
                "files_written": changed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        if state.test_errors and not state.errors and not state.check_errors:
            production_writes = _test_failure_production_writes(changed, sandbox, self.tools.get("build"))
            if production_writes and not _has_valid_production_test_failure_triage(fixer_output):
                for p in changed:
                    if p not in state.files_changed:
                        state.files_changed.append(p)
                msg = (
                    "Test-failure fixer changed production files without explicit "
                    "production_defect triage: "
                    f"{production_writes}"
                )
                state.record(self.name, "fix_failed", msg)
                state.failed = True
                return AgentResult(
                    success=False,
                    message=msg[:200],
                    data={"files_written": changed},
                )

        if not changed:
            msg = "Agent made no file changes"
            state.record(self.name, "fix_failed", msg)
            return AgentResult(success=False, message=msg)

        for p in changed:
            if p not in state.files_changed:
                state.files_changed.append(p)

        state.errors.clear()
        state.test_errors.clear()
        state.check_errors.clear()
        state.record(self.name, "fix", f"files changed: {changed}")
        _record_write_path_warnings(state, self.name, changed, allowed_write_paths, "active write paths")
        return AgentResult(
            success=True,
            message=f"Fix applied to {len(changed)} file(s): {changed}",
            data={"files_written": changed},
        )
