"""Test writer agent — writes and updates unit tests for the reviewed implementation.

Runs after ReviewerAgent (and after every fixer pass that changes production code),
before the build loop. The agent may ONLY write to test source directories; production
files are strictly off-limits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from agents.base_agent import (
    AgentResult,
    BaseAgent,
    AGENT_SECURITY_PREFIX,
    guidelines_files as _guidelines_files,
    load_extra_rules as _load_extra_rules,
    record_write_path_warnings as _record_write_path_warnings,
    tech_stack as _tech_stack,
)
from core.state import TaskState

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 40_000
_DEFAULT_COVERAGE_TARGET = 90

_AGENT_PROMPT = """\
You are writing unit tests for a {tech_stack} codebase.
The working directory is the project root.

BEFORE YOU START — read these project guidelines:
{guidelines_files}
They define the architecture, testing conventions, and patterns you must follow.

CONSTRAINTS — follow strictly:
- You may only read files under these paths: {allowed_read_paths}
- You may ONLY write to test source directories: {allowed_test_write_paths}
- To delete a test file, use Bash: `git rm <path>` — only within {allowed_test_write_paths}
- Bash is restricted to read-only commands and `git rm` only: `grep`, `find`, `ls`, `git rm`
  Do not run any other shell commands (`rm`, `mv`, `cp`, `curl`, `wget`, etc.)
- NEVER modify or delete any production source file
- NEVER delete existing tests unless they directly conflict with the new behaviour
- Do not write tests that pass trivially (empty assertions, always-true conditions)
- Do not add comments or documentation unless required by the task or project guidelines.
  When modifying a function, class, or property that already has a doc comment, update it
  to stay accurate (e.g. add or remove @param entries) — do not delete it.

TESTING RULES:
- Mirror the conventions of the existing tests exactly: the same test framework constructs,
  assertion style, naming patterns, and test double setup. Read existing tests before writing
  any new ones. Do not introduce constructs or libraries not already present in the project.
  This rule governs HOW tests are written (framework, assertions, naming) — not WHICH test
  structure to use (parametric vs individual). See the parametric rule below for that.
- Use parametric / data-driven tests when the project uses them anywhere in the test suite
  and the fit is natural — even if the specific file being edited does not currently use
  them. The parametric rule takes precedence over mirroring the existing file's structure.
  Strong signals for parametric tests:
    * Multiple inputs that exercise the same code path with different values
    * Cases that differ only in which inputs are null/absent and what the corresponding
      outputs are — group these into one parametric test rather than writing a separate
      test per nullable field
  When both signals are present, a parametric test is required, not optional.
  Do not use parametric tests where a plain test is clearer.
- Achieve at least {coverage_target}% branch and line coverage on all new or changed code.
  Think through every branch — including early returns, null checks, and error paths — before
  deciding a test is complete.
- Nullability requires explicit test cases. Every nullable parameter, return value, or state
  field that takes part in the changed code must have at least one test that exercises the
  null / absent path. Missed null branches are one of the most common causes of coverage gaps.
- Follow the nullability conventions of the project. Null paths must be tested explicitly —
  never hide them behind unsafe unwrapping. If the project guidelines define specific rules
  for null handling in tests, follow them exactly.
- For UI code, test through stable seams such as view models, public routing/state objects,
  rendered UI testing APIs already used by the project, or other project-standard test
  helpers. Do NOT write brittle tests that inspect UI framework internals, opaque view trees,
  reflection-only private storage, or component type-name strings unless the existing test
  suite already uses that exact pattern for the same UI framework.

WHAT WAS IMPLEMENTED:
{implementation_prompt}

ORIGINAL TASK DESCRIPTION:
{task_description}
{step_scope}

Use the original task description to honor explicit testing requirements.
For multi-step tasks, use CURRENT STEP as the primary scope signal. Do not add
tests for future steps that are not implemented yet.

FILES CHANGED (production code):
{files_changed}

GIT DIFF (what exactly changed):
{diff}

YOUR TASK:
1. Read the changed files in full using your Read tool to understand the new behaviour.
2. Find the existing test files for the changed modules (look in test source directories
   alongside the production files). Read them to understand existing conventions,
   used test doubles, and assertion style — follow them exactly.
3. Write or update tests that cover:
   - The new or changed behaviour introduced by this implementation
   - Edge cases and error paths visible from the public interface
   - Null / absent paths for every nullable value involved in the change
   - Any existing test that now tests a changed contract — update it to match
4. Parametric table completeness: when the change adds or modifies handling of an enum
   value or sealed class case, find ALL existing parametric test tables that enumerate
   cases of that type and add the new case to every one of them. Do not rely on having
   written a dedicated test for the new case — it must also appear in every existing table
   that covers the same type.
5. Do not write tests for unchanged code — with one exception: callers of modified
   functions (see step 6).
6. Callers of modified functions — for every function whose signature or behaviour was
   changed by this implementation, grep for ALL its callers in production code
   independently — do not rely on the implementation prompt's list of callers or its
   claims about which callers are unaffected. For each caller that is NOT in the files
   changed list (i.e. it was not explicitly updated as part of this task), check whether
   existing tests exercise its path through the modified function. If no adequate tests
   exist, add them. Rationale: a caller that relies on a default parameter value or on
   preserved behaviour has an implicit dependency on the change — leaving it untested
   means a future regression in that path goes undetected.

If no new tests are needed (e.g. the change was purely structural with no observable
behaviour difference), output a brief explanation and make no file changes.
"""

_DEFAULT_CONTEXT_FILES = ["README.md"]


def _step_scope(state: TaskState) -> str:
    if not state.plan:
        return ""
    step_idx = state.current_step
    if step_idx < 0 or step_idx >= len(state.plan):
        return "\nCURRENT STEP:\n(unknown — current_step is outside the plan)\n"
    return f"\nCURRENT STEP:\nStep {step_idx + 1}/{len(state.plan)}: {state.plan[step_idx]}\n"


class TestWriterAgent(BaseAgent):
    name = "test_writer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")
        if not state.files_changed:
            return AgentResult(success=False, message="No changed files to write tests for")

        file_tool = self.tools.get("file")
        git_tool = self.tools.get("git")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        sandbox_cfg = self.project_config.get("sandbox", {})
        allowed_test_write_paths = sandbox_cfg.get("allowed_test_write_paths", [])
        if not allowed_test_write_paths:
            log.warning("sandbox.allowed_test_write_paths not configured — test writer skipped")
            state.tests_up_to_date = True
            return AgentResult(success=True, message="Skipped: allowed_test_write_paths not configured")

        allowed_str = ", ".join(allowed_test_write_paths)
        allowed_read_paths = sandbox_cfg.get("allowed_read_paths", ["."])
        allowed_read_str = ", ".join(allowed_read_paths)
        coverage_target = self.project_config.get("test_writer", {}).get("coverage_target", _DEFAULT_COVERAGE_TARGET)

        diff = ""
        if git_tool:
            result = git_tool.diff_head()
            if result.success and result.output.strip():
                diff = result.output[:_MAX_DIFF_CHARS]
                if len(result.output) > _MAX_DIFF_CHARS:
                    diff += "\n... (diff truncated)"
        if not diff:
            diff = "(diff not available — use Read tool to inspect changed files directly)"

        prompt = AGENT_SECURITY_PREFIX + _AGENT_PROMPT.format(
            tech_stack=_tech_stack(self.project_config),
            guidelines_files=_guidelines_files(self.project_config),
            allowed_read_paths=allowed_read_str,
            allowed_test_write_paths=allowed_str,
            coverage_target=coverage_target,
            implementation_prompt=state.implementation_prompt,
            task_description=state.task_description,
            step_scope=_step_scope(state),
            files_changed="\n".join(f"  - {f}" for f in state.files_changed),
            diff=diff,
        )

        prompt += _load_extra_rules(self.project_config, self.name, file_tool)

        agent_output = None
        changed: list[str] = []
        try:
            changed, agent_output = self.llm.run_agent(prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.test_write_records.append(
                {
                    "step": state.current_step,
                    "build_iteration": state.build_iterations,
                    "test_writer_prompt": prompt,
                    "test_writer_output": None,
                    "files_written": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            state.record(self.name, "test_write_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

        state.test_write_records.append(
            {
                "step": state.current_step,
                "build_iteration": state.build_iterations,
                "test_writer_prompt": prompt,
                "test_writer_output": agent_output,
                "files_written": list(changed),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        state.tests_up_to_date = True
        if changed:
            state.files_changed.extend(p for p in changed if p not in state.files_changed)
            state.test_files_written.extend(p for p in changed if p not in state.test_files_written)
            state.record(self.name, "test_write", f"files changed: {changed}")
            _record_write_path_warnings(
                state,
                self.name,
                changed,
                allowed_test_write_paths,
                "allowed_test_write_paths",
            )
            return AgentResult(
                success=True,
                message=f"Tests written/updated in {len(changed)} file(s): {changed}",
                data={"files_written": changed},
            )

        state.record(self.name, "test_write", "no changes needed")
        return AgentResult(success=True, message="No test changes needed")
