"""Implementer agent — runs the configured LLM as an autonomous agent to implement the task.

The agent receives the implementation prompt and navigates the codebase using its file tools.
Changed files are detected via git diff before/after the agent call.
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
from agents.build_guidance import write_agent_constraints as _write_agent_constraints
from core.state import TaskState

_AGENT_PROMPT = """\
You are implementing a change to a {tech_stack} codebase.
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
- Do not modify or delete files outside these directories under any circumstances
- Make MINIMAL changes — only what the task requires
- Do not refactor unrelated code
{build_tool_constraints}- Do not introduce new usages of deprecated APIs when a non-deprecated alternative exists and switching to it is a drop-in change; if migration requires broader refactoring, using the deprecated API is acceptable within the minimal-changes constraint
- If the task or implementation prompt contains an `Asset manifest` or structured asset declarations such as
  `### Reference assets` / `### Delivery assets`, treat those declarations as part
  of the delivery contract. Use delivery assets only within the requested scope,
  do not copy reference-only assets into production files, and do not invent missing provenance, license, or target information.
  When a delivery asset has no requested target, choose the project-conventional
  location from the codebase and keep the change minimal.
- Do not add comments or documentation unless required by the task or project guidelines.
  When modifying a function, class, or property that already has a doc comment, update it
  to stay accurate (e.g. add or remove @param entries) — do not delete it.
- NEVER create or modify unit tests — no files under any test source directory
  (e.g. `test/`, `__tests__/`, `spec/`), regardless of what the task says;
  a dedicated agent handles tests separately

TASK:
{implementation_prompt}
{step_section}{review_fix_section}

CLEANUP (run after all task changes are applied):
Core rule: whenever you remove a reference to any named symbol — class, function,
method, constant, property, extension function, type alias, or any other definition —
grep for that symbol within your readable paths ({allowed_read_paths}) EXCLUDING test
files (directories: test/, __tests__/, spec/, and any other test source directory).
If it has zero remaining references in production code, remove its definition. Then apply the
same rule to every symbol that definition referenced. Repeat recursively until no
further dead code is found.
A symbol referenced only inside test files has zero production references — remove its
definition. Never modify test files; a dedicated TestAgent handles them separately.
Only remove a symbol when you have confirmed via grep (excluding test dirs) that it
has zero production references.
After removing any member from a type definition (class, interface, …), verify that
ALL remaining members of that type still have at least one production caller — not
just the ones directly referenced by the deleted code.
After all changes and dead-code removal are complete, go through every modified file
and remove any unused dependencies at the top of the file (imports, requires, includes,
or the equivalent construct in the language) that are no longer referenced in that file.
"""


_STEP_SECTION = """\

CURRENT STEP ({step_num}/{total_steps}): {step_description}

You are implementing step {step_num} of {total_steps} in a multi-step plan.
Focus ONLY on the changes described in CURRENT STEP above.
Do NOT implement future steps — they will be handled in separate passes.\
"""

_FINAL_FULL_TASK_SECTION = """\

FINAL FULL-TASK PHASE:
All planned steps have been implemented. Fix issues against the complete original task,
the implementation prompt, and the complete current diff. Do NOT restrict changes to
the last planned step.\
"""

_REVIEW_FIX_SECTION = """\

REVIEW ISSUES TO FIX:
A previous review or security review of your implementation found the following
blocking or corrective problems. This remediation scope takes priority over the current step boundary:
fix the listed issue even when it requires touching files outside CURRENT STEP, while
still respecting the allowed write paths and keeping the change minimal.

Do not report the current step as already complete until each listed issue is
addressed. If a listed issue is already fixed or cannot be fixed safely, leave files
unchanged and explain that exact reason with file-specific evidence.

The original task requirements above still apply in full.

{issues}\
"""

_DEFAULT_CONTEXT_FILES = ["README.md"]

_SCOPE_FINAL_FULL_TASK = "final_full_task"


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


class ImplementerAgent(BaseAgent):
    name = "implementer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")

        file_tool = self.tools.get("file")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        sandbox = self.project_config.get("sandbox", {})
        allowed_write_paths = sandbox.get("allowed_write_paths", [])
        allowed_read_paths = sandbox.get("allowed_read_paths", ["."])
        allowed_str = ", ".join(allowed_write_paths) if allowed_write_paths else "(not configured)"
        allowed_read_str = ", ".join(allowed_read_paths)
        tech_stack = _tech_stack(self.project_config)

        step_section = ""
        if state.plan and state.active_scope != _SCOPE_FINAL_FULL_TASK:
            step_idx = state.current_step
            step_section = _STEP_SECTION.format(
                step_num=step_idx + 1,
                total_steps=len(state.plan),
                step_description=state.plan[step_idx],
            )
        elif state.active_scope == _SCOPE_FINAL_FULL_TASK:
            step_section = _FINAL_FULL_TASK_SECTION

        review_fix_section = ""
        if state.review_issues:
            review_fix_section = _REVIEW_FIX_SECTION.format(issues="\n\n".join(state.review_issues))

        prompt = AGENT_SECURITY_PREFIX + _AGENT_PROMPT.format(
            tech_stack=tech_stack,
            allowed_read_paths=allowed_read_str,
            allowed_write_paths=allowed_str,
            guidelines_files=_guidelines_files(self.project_config),
            build_tool_constraints=_write_agent_constraints(self.project_config),
            implementation_prompt=state.implementation_prompt,
            step_section=step_section,
            review_fix_section=review_fix_section,
        )

        step_description = (
            state.plan[state.current_step]
            if state.plan and state.active_scope != _SCOPE_FINAL_FULL_TASK and state.current_step < len(state.plan)
            else None
        )

        try:
            changed, agent_output = self.llm.run_agent(prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.record(self.name, "implement_failed", msg[:500])
            state.implement_cycle_records.append(
                {
                    "step": state.current_step,
                    "build_iteration": state.build_iterations,
                    "review_iteration": state.review_iterations,
                    "security_review_iteration": state.security_review_iterations,
                    "scope": _scope(state),
                    "step_description": step_description,
                    "implementer_prompt": prompt,
                    "implementer_output": None,
                    "files_written": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            return AgentResult(success=False, message=msg[:200])

        state.implement_cycle_records.append(
            {
                "step": state.current_step,
                "build_iteration": state.build_iterations,
                "review_iteration": state.review_iterations,
                "security_review_iteration": state.security_review_iterations,
                "scope": _scope(state),
                "step_description": step_description,
                "implementer_prompt": prompt,
                "implementer_output": agent_output,
                "files_written": changed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        if not changed:
            msg = "Agent made no file changes"
            state.record(self.name, "implement_skipped", msg)
            return AgentResult(success=True, message=msg)

        state.files_changed.extend(p for p in changed if p not in state.files_changed)
        state.record(self.name, "implement", f"files changed: {changed}")
        _record_write_path_warnings(state, self.name, changed, allowed_write_paths, "allowed_write_paths")
        return AgentResult(
            success=True,
            message=f"Written {len(changed)} file(s): {changed}",
            data={"files_written": changed},
        )
