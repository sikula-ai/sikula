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
_TESTABILITY_GAP_MARKER = "TESTABILITY GAP:"
_TESTABILITY_GAP_POLICY_FAIL = "fail"
_TEST_SURFACE_POLICY_COMPLETE = "complete"
_TEST_SURFACE_POLICY_EXISTING_INFRASTRUCTURE = "existing_infrastructure"

_TEST_SURFACE_POLICY_INSTRUCTIONS = {
    _TEST_SURFACE_POLICY_COMPLETE: (
        "complete: Aim to cover the complete changed behavior. If important behavior "
        "cannot be meaningfully tested without adding missing project test infrastructure "
        "or seams, report a TESTABILITY GAP using the structured block below instead of "
        "substituting broad source-inspection tests."
    ),
    _TEST_SURFACE_POLICY_EXISTING_INFRASTRUCTURE: (
        "existing_infrastructure: Use only existing project test infrastructure and "
        "project-standard seams/helpers. Do not add new UI, browser, device, emulator, "
        "simulator, external-service, or runtime harnesses unless the task explicitly asks "
        "for that infrastructure. Missing out-of-surface harnesses are not by themselves "
        "a TESTABILITY GAP. Add the best meaningful tests available through existing seams, "
        "and report a TESTABILITY GAP only when an acceptance contract still cannot be "
        "meaningfully checked within this configured test surface. Do not use broad "
        "source-inspection tests to pretend an out-of-surface UI/browser/device/runtime "
        "behavior was meaningfully tested."
    ),
}

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
- Tests must not leave generated source/runtime files, snapshots, reports, caches, or other
  repository changes behind during normal test execution. If a test needs temporary files,
  write them to an OS temp directory or a project-ignored temp/cache path and clean them up.
- Do not change production source, build configuration, dependency declarations, or pipeline
  settings just to make a generated test harness compile or run.
- Do not add comments or documentation unless required by the task or project guidelines.
  When modifying a function, class, or property that already has a doc comment, update it
  to stay accurate (e.g. add or remove @param entries) — do not delete it.

TESTING RULES:
- Test surface policy: {test_surface_policy_instruction}
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
- Within the configured test surface, achieve at least {coverage_target}% branch and line
  coverage on all new or changed code. Think through every branch — including early returns,
  null checks, and error paths — before deciding a test is complete.
- Nullability requires explicit test cases. Every nullable parameter, return value, or state
  field that takes part in the changed code must have at least one test that exercises the
  null / absent path. Missed null branches are one of the most common causes of coverage gaps.
- Follow the nullability conventions of the project. Null paths must be tested explicitly —
  never hide them behind unsafe unwrapping. If the project guidelines define specific rules
  for null handling in tests, follow them exactly.
- Parser, validator, expression engine, schema, DSL, config loader, and rule engine changes
  require a positive/negative contract matrix. For every public parse, validate, evaluate,
  or load API touched by the change, cover at least one accepted valid input and every
  materially different rejected input class introduced or affected by the change. Include
  malformed syntax/literals, unknown identifiers or functions, invalid shapes, forbidden
  values, empty/both/none alternatives, scope or forward-reference violations, and literal
  division-by-zero where applicable. Preserve the contract dimension being tested: do not
  replace one rejected input class with a different invalid fixture just because it is easier
  to make pass.
- If expressions, rules, conditions, or config fields are used in typed contexts, test the
  expected result type explicitly. Cover success for the valid type and rejection for wrong
  result types, such as a boolean value where a numeric value is required or a numeric value
  where a boolean condition is required. Do not rely on tests that only prove syntax or
  variable-name validation. When observable through the public API, assert whether rejection
  belongs to parse/load validation, semantic validation, or runtime evaluation.
- For UI code, test through stable seams such as view models, public routing/state objects,
  rendered UI testing APIs already used by the project, or other project-standard test
  helpers. Do NOT write brittle tests that inspect UI framework internals, opaque view trees,
  reflection-only private storage, or component type-name strings unless the existing test
  suite already uses that exact pattern for the same UI framework.
- Map changed behaviour through its production entry points before choosing tests:
  user interaction handlers, API or route handlers, CLI commands, lifecycle hooks,
  callbacks, queue/background job handlers, timers, observers, or equivalent platform
  entry points. If multiple entry points reach the same changed operation and each has
  its own error handling, state transition, cancellation/absence handling, or side
  effect boundary, cover each entry point separately. Do not assume that testing a
  shared helper through one entry point proves the other entry points are safe.
- For async or deferred work started from an entry point (for example promises, futures,
  coroutines, tasks, threads, callbacks, or queued work), cover the observable success
  path and the observable failure/error path through the entry point when the configured
  test surface can do so. If meaningful failure-path coverage would require new
  infrastructure outside the configured test surface, follow the test surface policy
  instead of adding brittle tests.
- Prefer behaviour tests through public APIs, public state, public routing contracts, command
  outputs, or project-standard test helpers.
- Treat source-file inspection tests as weak coverage, not as a substitute for behaviour
  tests. Do NOT use source inspection for UI implementation details such as component
  structure, layout branches, framework modifiers, view-tree shape, composable/widget
  wiring, or literal calls inside screen/view files. Instead, test the nearest stable seam
  already available in the project: view model, reducer, presenter, public state, route
  builder, navigation contract, handler, command output, API contract, or repository/use-case.
- Source inspection is acceptable only for narrow stable static contracts that are not
  meaningfully executable through the available test surface, such as route constants,
  string/resource keys, API annotations/signatures, schema/config keys, generated registry
  entries, or other project-standard static contracts. Keep those tests focused on the
  contract, not on incidental implementation shape.
- If meaningful behaviour coverage would require adding new test infrastructure outside
  the configured test surface, follow the test surface policy. Under the complete policy,
  output the following block and make no file changes for that gap. Under the
  existing_infrastructure policy, do not report a gap merely because out-of-surface
  infrastructure is absent; first add meaningful coverage through existing project seams.
  Do not replace missing coverage with broad source-inspection tests. If a narrow
  source-inspection test is still justified, keep it self-contained: resolve paths robustly
  from the test file or repository root, do not depend on the test runner's current working
  directory, and do not require production source, build configuration, dependency
  declarations, runtime configuration, or pipeline settings to change just so the inspection
  test can pass.
  TESTABILITY GAP:
  target: <behaviour or contract that remains untested>
  reason: <missing seam, missing test harness, unavailable helper, etc.>
  recommended_action: <project-level test infrastructure or seam needed>
  risk: low | medium | high

WHAT WAS IMPLEMENTED:
{implementation_prompt}

ORIGINAL TASK DESCRIPTION:
{task_description}
{step_scope}

Use the original task description to honor explicit testing requirements.
For multi-step tasks, use CURRENT STEP as the primary scope signal. Do not add
tests for future steps that are not implemented yet.
If a FINAL FULL-TASK TEST SCOPE section is present, all planned steps are complete.
Write or update tests for the complete task, not just the last planned step.

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
   - Each production entry point that reaches a changed operation when the entry points
     have distinct error handling, state transitions, or side effects
   - Null / absent paths for every nullable value involved in the change
   - Structured input contract cases for parser/validator/expression/DSL/config/schema
     changes, including wrong expected result type rejection where typed contexts exist
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
_SCOPE_FINAL_FULL_TASK = "final_full_task"


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


def _step_scope(state: TaskState) -> str:
    if state.active_scope == _SCOPE_FINAL_FULL_TASK:
        return (
            "\nFINAL FULL-TASK TEST SCOPE:\n"
            "All planned steps have been implemented. Cover the complete original task and "
            "the complete current diff. Do not restrict tests to the last planned step.\n"
        )
    if not state.plan:
        return ""
    step_idx = state.current_step
    if step_idx < 0 or step_idx >= len(state.plan):
        return "\nCURRENT STEP:\n(unknown — current_step is outside the plan)\n"
    return f"\nCURRENT STEP:\nStep {step_idx + 1}/{len(state.plan)}: {state.plan[step_idx]}\n"


def _parse_testability_gaps(output: str | None) -> list[dict]:
    if not output or _TESTABILITY_GAP_MARKER.lower() not in output.lower():
        return []

    gaps: list[dict] = []
    current: list[str] = []
    for line in output.splitlines():
        if line.strip().lower().startswith(_TESTABILITY_GAP_MARKER.lower()):
            if current:
                gaps.append(_gap_from_lines(current))
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        gaps.append(_gap_from_lines(current))
    return gaps


def _gap_from_lines(lines: list[str]) -> dict:
    message = "\n".join(lines).strip()
    gap = {"message": message}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        normalized_key = key.strip().lower().replace(" ", "_")
        if normalized_key in {"target", "reason", "recommended_action", "risk"}:
            gap[normalized_key] = value.strip()
    return gap


def _testability_gap_policy(project_config: dict) -> str:
    policy = str(project_config.get("test_writer", {}).get("testability_gap_policy", "warn")).strip().lower()
    return _TESTABILITY_GAP_POLICY_FAIL if policy == _TESTABILITY_GAP_POLICY_FAIL else "warn"


def _test_surface_policy(project_config: dict) -> str:
    policy = (
        str(
            project_config.get("test_writer", {}).get(
                "test_surface_policy",
                _TEST_SURFACE_POLICY_EXISTING_INFRASTRUCTURE,
            )
        )
        .strip()
        .lower()
    )
    if policy == _TEST_SURFACE_POLICY_COMPLETE:
        return _TEST_SURFACE_POLICY_COMPLETE
    return _TEST_SURFACE_POLICY_EXISTING_INFRASTRUCTURE


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
        test_surface_policy = _test_surface_policy(self.project_config)

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
            test_surface_policy_instruction=_TEST_SURFACE_POLICY_INSTRUCTIONS[test_surface_policy],
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
                    "scope": _scope(state),
                    "test_surface_policy": test_surface_policy,
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
                "scope": _scope(state),
                "test_surface_policy": test_surface_policy,
                "test_writer_prompt": prompt,
                "test_writer_output": agent_output,
                "files_written": list(changed),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        gaps = _parse_testability_gaps(agent_output)
        for gap in gaps:
            state.record_testability_gap(
                self.name,
                gap["message"],
                target=gap.get("target"),
                reason=gap.get("reason"),
                recommended_action=gap.get("recommended_action"),
                risk=gap.get("risk"),
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
            if gaps and _testability_gap_policy(self.project_config) == _TESTABILITY_GAP_POLICY_FAIL:
                state.failed = True
                msg = f"Testability gap reported by test writer ({len(gaps)} gap(s))"
                return AgentResult(
                    success=False,
                    message=msg,
                    data={"files_written": changed, "testability_gaps": gaps},
                )
            return AgentResult(
                success=True,
                message=f"Tests written/updated in {len(changed)} file(s): {changed}",
                data={"files_written": changed, "testability_gaps": gaps},
            )

        state.record(self.name, "test_write", "no changes needed")
        if gaps and _testability_gap_policy(self.project_config) == _TESTABILITY_GAP_POLICY_FAIL:
            state.failed = True
            msg = f"Testability gap reported by test writer ({len(gaps)} gap(s))"
            return AgentResult(success=False, message=msg, data={"testability_gaps": gaps})
        data = {"testability_gaps": gaps} if gaps else {}
        return AgentResult(success=True, message="No test changes needed", data=data)
