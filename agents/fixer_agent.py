"""Fixer agent — runs the configured LLM as an autonomous agent to fix build or test errors.

The agent receives error output and task context, then navigates the codebase using its file
tools to locate and fix the affected files. Changed files are detected via git diff. Logical
and completeness issues are handled upstream by ReviewerAgent; the fixer's scope is strictly
limited to errors the build system or test runner reports.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agents.base_agent import (
    AgentResult,
    BaseAgent,
    AGENT_SECURITY_PREFIX,
    guidelines_files as _guidelines_files,
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
- You MAY create and modify test files — that is the purpose of this fix
- NEVER modify production source files to make tests pass; only fix the test code"""

_CHECK_TEST_CONSTRAINT = """\
- You MAY modify test files if the check errors explicitly reference them
- Fix ONLY the files named in the check errors — nothing else"""

_DEFAULT_CONTEXT_FILES = ["README.md"]


def _test_constraint(state: TaskState) -> str:
    if state.errors:
        return _BUILD_TEST_CONSTRAINT
    if state.test_errors and not state.check_errors:
        return _TEST_TEST_CONSTRAINT
    # check_errors (detekt/lint) can reference test or production files — allow both
    return _CHECK_TEST_CONSTRAINT


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
        if not state.errors and (state.test_errors or state.check_errors):
            # Fixing test failures or check violations — check errors (e.g. detekt) can reference
            # test files, so the agent needs write access to test directories
            allowed_write_paths = sandbox.get("allowed_test_write_paths") or sandbox.get("allowed_write_paths", [])
        else:
            allowed_write_paths = sandbox.get("allowed_write_paths", [])
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
                "errors_before": errors_snapshot,
                "fixer_prompt": prompt,
                "fixer_output": fixer_output,
                "files_written": changed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
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
