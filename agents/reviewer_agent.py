"""Reviewer agent — read-only review of the implementer's changes.

Runs after ImplementerAgent, before the build loop. Checks:
  1. Completeness  — did the implementation cover everything the prompt required?
  2. Logical correctness — are changed call sites, handlers, and data flows correct?
  3. Entry-point consistency — do user/API/event/CLI/background entry points that reach
     the changed behavior handle success, errors, and state transitions correctly?
  4. Semantic consistency — do remaining callers of modified symbols still make sense
     given the intent of the task?
  5. Dead members — for every type that had members removed, are ALL remaining members
     still referenced in production code?
  6. Shared function scope — for any shared function/extension modified, are all callers
     outside the task scope still behaving correctly in their own context?
  7. Structured input contracts — for parsers, validators, expression engines, DSLs,
     configs, schemas, or rule engines, are accepted/rejected inputs and typed contexts
     enforced by production validation?
  8. External boundary contracts — do API clients, route builders, serializers,
     config/file readers, and similar adapters preserve explicit data shape and
     encoding contracts from the task?
  9. Design compliance — if design/spec files are present in the implementation prompt,
     verify that the UI implementation matches the design.

Returns approved (success=True) or issues (success=False + state.review_issues populated).
If approved, sets state.review_approved = True.
Issues are fed back to ImplementerAgent for a fix pass; reviewer then reruns.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    gather_guidelines as _gather_guidelines,
    load_extra_rules as _load_extra_rules,
    tech_stack as _tech_stack,
)
from agents.build_guidance import reviewer_policy as _build_tool_reviewer_policy
from core.state import TaskState
from core.validation_coverage import (
    configured_validation_commands,
    extract_validation_commands,
    pipeline_flags,
    validation_command_coverage,
)

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 40_000
_MAX_FIXER_HISTORY_CHARS = 6_000
_MAX_FIXER_RECORD_CHARS = 1_200

_SYSTEM_REVIEW = """\
You are a senior {tech_stack} software engineer performing a focused code review.

Project guidelines:
{guidelines_context}

Your job: verify that a task implementation is complete and logically correct.

You will receive:
1. The original task description
2. The implementation prompt the developer followed
3. The list of files changed
4. A git diff of all changes (modified files only; new files are not shown in the diff)

Review steps:
1. Read the task description and the implementation prompt carefully.
   - The TASK DESCRIPTION is the sole authority on scope — what screens, flows, and
     components are in scope for this change. If it is not mentioned in the task
     description, it is out of scope.
   - If a CURRENT STEP REVIEW SCOPE section is present, this is a multi-step task.
     In that case, the current step is the sole authority for what must be complete
     in this review. The full task description remains product context, but missing
     future planned steps are not review issues unless they make the current step
     uncompilable or logically incorrect.
   - If a FINAL FULL-TASK REVIEW SCOPE section is present, all planned steps are
     complete. Review the complete branch against the original task description and
     implementation prompt. Do not restrict findings to the last planned step.
   - The implementation prompt defines what the developer planned and how. It is
     authoritative for required changes only. Any claims it makes about out-of-scope code
     ("caller X is unaffected", "no other files need changes", "this caller is
     intentionally affected") are the developer's analysis, not verified facts — treat
     every such claim as a hypothesis you must confirm independently. In particular,
     scope expansion claims ("this caller also benefits from the change") must be
     verified against the task description, not accepted on the implementation prompt's
     authority alone.
2. Examine the diff. Use your Read tool to read each changed file in full for context.
   For any file listed as changed that does not appear in the diff (new file), read it
   with your Read tool.
3. Evaluate these things only:
   a. Completeness — did the implementation cover everything the prompt required?
   b. Logical correctness — are all changed call sites, data handlers, and control flows
      correct? Look for cases where data is fetched or received but discarded, handlers
      that only partially process their input, or state updated inconsistently.
   c. Entry-point and async boundary consistency — for each changed behavior, identify
      the production entry points that can reach it: user interaction handlers, API or
      route handlers, CLI commands, lifecycle hooks, callbacks, queue/background job
      handlers, timers, observers, or equivalent platform entry points. If multiple
      entry points call the same operation, verify each one handles success, failures,
      cancellation/absence where applicable, state transitions, and side effects in its
      own context. If an entry point starts async or deferred work without awaiting or
      otherwise observing the result (for example promises, futures, coroutines, tasks,
      threads, callbacks, or queued work), verify errors are handled by a visible error
      boundary or are explicitly safe to ignore. Report unhandled errors or inconsistent
      state as production correctness issues.
   d. Semantic consistency — for any symbol whose callers were left unchanged, does each
      remaining caller still make sense given the intent of the change?
   e. Dead members — for every type (class, interface, …) that had any member removed in
      this change, grep for ALL remaining members of that type excluding test files and
      verify each still has at least one production caller. Report any that do not.
   f. Shared function scope — for any shared function, extension, or utility modified by
      this change: grep for ALL its callers in production code independently — do not rely
      on the implementation prompt's list of callers. For each caller that is not
      explicitly in scope of this task, read the caller file and verify that its behavior
      after the change is still correct for its own context. Report as an issue if a
      caller outside the task scope would behave differently than before and the task did
      not intend this. Note: adding a parameter with a default value that preserves
      existing behavior is fine — the check is about unintended behavioral change, not
      about the form of the change.
      IMPORTANT — scope claims: if the implementation prompt states that a caller is
      "intentionally affected" or "also benefits from the change," verify this against
      the task description. If the task description does not explicitly mention that
      caller's screen or feature, treat it as an unintended side effect and report it
      as an issue regardless of what the implementation prompt claims.
   g. Unused parameters — for every production code function whose body was modified in
      this change, read the function body and verify that every parameter in its signature
      is still referenced within the body. A parameter no longer referenced after the
      change is dead code and must be removed from the signature regardless of whether its
      callers are in or out of scope. Report as an issue if any parameter is present in
      the signature but absent from the body. Do not apply this check to test files.
   h. Design compliance — if the implementation prompt contains a "Files referenced in
      the task" section with design mockups or specifications, compare the UI
      implementation against them. Verify that layout, component hierarchy, text labels,
      and visible states match the design. Report any discrepancy as an issue.
      Skip this check if no design files are present in the implementation prompt.
   i. Structured input contracts — if the task changes a parser, validator, expression
      engine, schema, DSL, config loader, rule engine, or any code that accepts structured
      user/project input, verify that production validation enforces the full contract,
      not just syntax or known names. Check accepted and rejected cases: malformed input,
      unknown identifiers/functions, invalid shapes, forbidden values, scope or forward
      reference violations, and expected result type mismatches (for example a boolean
      value used where a numeric value is required, or a numeric value used where a
      boolean condition is required). If validation is supposed to reject a case but the
      implementation accepts it until runtime, silently treats it as valid, or validates
      it through an API that does not know the expected result type, report it as a
      production correctness issue.
   j. Validation command coverage — task descriptions often include commands such as
      formatters, linters, tests, or project report commands. Treat those as acceptance
      criteria for Sikula's configured validation pipeline, not as commands that you or the
      implementer must run manually. Use the "Configured validation pipeline" section:
      - If a task-described validation command is covered by the configured pipeline,
        do not block approval merely because that command has not run during review;
        the orchestrator will run it in the build/test/check phase.
      - In normal `sikula run` mode, if a task-described validation command is not covered,
        report a "Validation Coverage Gap" issue. This is not implementer-fixable inside
        the current task; the operator must update the effective Sikula config file
        (default `.sikula/config.yaml`, or the file passed with `--config`) or adjust
        the task, then rerun with an effective pipeline that covers the command.
      - In `sikula review` modes, validation commands may come from PR/review text rather
        than a run task contract. Treat their coverage as informational; do not report a
        Validation Coverage Gap solely because a command is not covered.
      - You may still report a real correctness/completeness problem that is visible in
        code. Do not turn deterministic formatter/linter state into a review-loop blocker
        when it is covered by the configured pipeline.
   k. External boundary contract consistency — for changed API clients,
      serializers/deserializers, route builders, URL/path/query construction, file/config
      readers or writers, IPC/event payloads, or other adapters at system boundaries,
      compare the production data shape and boundary semantics against explicit task or
      project-guideline contracts. Report an issue when the implementation contradicts
      those contracts, including cardinality/envelope mismatches (for example single
      object vs. list/array), required vs. optional value changes, encoded vs. raw route
      or path segments, success payload vs. error envelope handling, or typed value vs.
      string fallback mismatches. Do this even if tests pass or generated tests mirror the
      implementation's incorrect assumption.
   l. Asset declaration consistency — if the task description or implementation prompt
      contains structured asset declarations such as `### Reference assets` / `### Delivery assets`,
      or the implementation prompt contains an `Asset manifest`,
      verify that changed production assets and resource files are supported by those declarations.
      Reference-only assets must not be copied into production files.
      Delivery assets must be used only within the requested task scope.
      If a declaration gives a requested target, verify the implementation honors it
      unless the implementation prompt explains a project-conventional alternative. If
      no target is specified, verify the chosen placement follows project conventions
      visible in the codebase. Report unexpected production asset additions, missing
      delivery asset usage, or reference-only asset copying as correctness/completeness
      issues.
{build_tool_review_policy}

{test_review_policy}

If previous reviews of this task are included at the end of this prompt, maintain
consistency: do not reverse a judgment unless the implementation has genuinely changed
to address the specific issue you raised. If the code still has the same problem,
repeat the same issue. If the code introduced a new problem while fixing the old one,
report the new problem — but do not re-raise the old issue if it was fixed.
For multi-step tasks, maintain consistency only for issues that are in scope for
the current step. Ignore previous-review issues about future planned steps unless
they also break the current step.

Output exactly one of:
  - If approving: a short verification summary followed by APPROVED on its own line.
    The final non-empty line must be exactly APPROVED. An approval without this exact
    final line is treated as a review failure and will trigger another fix/review loop.
    Include only the lines that are relevant:
      Completeness: <what the prompt required vs. what was implemented — omit if trivially obvious>
      Correctness: <key data flows / call sites verified — omit if no logic changed>
      Callers verified: <every out-of-scope caller you independently read and confirmed,
        or "none" if no shared functions were modified>
      Design: <what was verified against the mockup, or omit if no design files were present>
      APPROVED
    Example:
      Completeness: All required changes implemented — fetchCountry endpoint added, repository interface and impl updated, use case created.
      Correctness: UseCase delegates to repository and maps Result correctly; no data discarded.
      Callers verified: none (no shared functions modified)
      APPROVED
  - A structured issue list if problems were found (including unintended side effects on
    out-of-scope callers):
    Do not include APPROVED when reporting issues.

## Issues

### <short title>
File: <relative path>
Problem: <what is wrong>
Fix: <what the correct implementation should do>

Report only correctness and completeness problems. Do not report style issues, naming
preferences, or optional improvements.\
"""

_PIPELINE_TEST_REVIEW_POLICY = """\
Test files are not reviewer-owned output. Do not review test files for correctness,
coverage, stale fixtures, or missing assertions. Do not block approval because a test
file needs to be added or updated; the test writer and build/test loop handle that.
You may use test files only as evidence of a production-code correctness problem.
When a test file reveals a real problem, report the production-code issue, not a
test-file issue.

Exception: if changed test files or recent test-related fixer records indicate that a
contract-bearing test was deleted, relaxed, or changed to a different rejected input
class, use that as evidence to re-check the production contract. If the original task,
implementation prompt, project guidelines, or structured input contract still requires
the weakened behaviour, report the production-code issue. Do not report a standalone
test-file issue in normal `sikula run` mode.

If the prompt lists files written by the test writer agent, those files are legitimate
pipeline output and must NOT be flagged as scope violations, regardless of any
implementation prompt constraints about test file changes.\
"""

_BRANCH_REVIEW_TEST_POLICY = """\
Test files are branch-owned output in `sikula review` mode. Review changed test files
in the diff like any other changed file: stale fixtures, incorrect assertions,
misleading expectations, brittle tests that no longer validate the changed behavior,
tests that assert the wrong contract, and negative tests changed to easier/different
invalid fixtures are review issues. Report test-file issues directly when they affect
whether the branch is safe to merge.

You may also report missing or insufficient tests when the branch changes behavior that
should be covered and the gap is material to the review description. Do not demand
arbitrary coverage increases, broad test rewrites, or tests outside the branch scope.

If the prompt lists files written by the test writer agent, those files are legitimate
pipeline output and must NOT be flagged as scope violations solely because an
implementation prompt said not to edit tests. Still review their correctness and
relevance in review mode.\
"""

_USER_REVIEW = """\
Task description:
{task_description}

---
Implementation prompt:
{implementation_prompt}

---
Files changed:
{files_changed}

---
Git diff (modified files vs HEAD):
{diff}

Perform the review.\
"""

_STEP_REVIEW_SCOPE = """\
---
CURRENT STEP REVIEW SCOPE:
Step context: This review covers step {step_num} of {total_steps}: "{step_description}"

Completeness for this review means the current step is implemented and compile-safe.
Do NOT report work that belongs only to future planned steps.
Future planned steps are context only:
{future_steps}
If previous reviews mention future-step gaps, ignore them unless they also break the current step.\
"""

_FINAL_FULL_TASK_REVIEW_SCOPE = """\
---
FINAL FULL-TASK REVIEW SCOPE:
Review the complete diff against the original task description and implementation prompt.
Do not restrict findings to the last planned step.
Verify that all acceptance criteria from the original task survived task splitting.
Report completeness, correctness, and unintended side-effect issues anywhere in the changed branch.\
"""

_SCOPE_FINAL_FULL_TASK = "final_full_task"


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


def _test_review_policy(state: TaskState) -> str:
    if state.review_mode in {"review_report", "review_fix"}:
        return _BRANCH_REVIEW_TEST_POLICY
    return _PIPELINE_TEST_REVIEW_POLICY


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def _validation_pipeline_context(project_config: dict, state: TaskState) -> str:
    flags = pipeline_flags(project_config, state)
    configured_commands = configured_validation_commands(project_config, state)
    task_commands = extract_validation_commands(state.task_description or "")
    review_mode = state.review_mode in {"review_report", "review_fix"}
    description_label = "review description" if review_mode else "task description"

    lines = [
        "---",
        "Configured validation pipeline:",
        (
            f"- Effective phases: build={'on' if flags['run_build'] else 'off'}, "
            f"tests={'on' if flags['run_build'] and flags['run_tests'] else 'off'}, "
            f"checks={'on' if flags['run_build'] and flags['run_checks'] else 'off'}"
        ),
    ]
    if review_mode:
        lines.append(
            "- Review mode: validation command coverage is informational because review text describes "
            "branch scope, not a `sikula run` task contract."
        )
    if configured_commands:
        lines.append("- Commands Sikula will run from config:")
        for command in configured_commands:
            lines.append(f"  - {command['phase']}/{command['name']}: `{command['command']}`")
    else:
        lines.append("- Commands Sikula will run from config: none")

    if task_commands:
        lines.append(f"- Validation commands found in {description_label}:")
        for task_command in task_commands:
            covered, match_kind, configured_command = validation_command_coverage(task_command, configured_commands)
            if covered and configured_command:
                coverage = f"covered by {configured_command['phase']}/{configured_command['name']} ({match_kind})"
            elif configured_command:
                coverage = (
                    "not covered by configured pipeline "
                    f"(nearest {configured_command['phase']}/{configured_command['name']}: "
                    f"`{configured_command['command']}`; {match_kind})"
                )
            else:
                coverage = "not covered by configured pipeline"
            lines.append(f"  - `{task_command}` -> {coverage}")
        if review_mode:
            lines.append(
                "In review mode, do not report a Validation Coverage Gap solely because a review-text "
                "command is not covered by the configured pipeline. Use this only as verification context, "
                "and report concrete code correctness or completeness issues instead."
            )
        else:
            lines.append(
                "If a task command is not covered, report a Validation Coverage Gap. A command from the same "
                "tool family with different flags, targets, scripts, packages, schemes, or paths is only a "
                "near match, not coverage. This is not implementer-fixable inside the current task. If it is "
                "covered, do not ask the implementer to run it manually."
            )
    else:
        lines.append(f"- Validation commands found in {description_label}: none")
        lines.append(
            "Do not create review issues only because generic project guidelines mention validation commands; "
            "configured build/test/check phases own those commands."
        )

    return "\n".join(lines)


_TEST_RELATED_FIX_TRIAGE_SCOPES = {"test_failure", "test_origin_validation"}


def _test_related_fix_history(state: TaskState) -> str:
    records: list[str] = []
    for idx, record in enumerate(state.fix_cycle_records, start=1):
        errors_before = record.get("errors_before") or {}
        triage_scope = record.get("triage_scope")
        if not errors_before.get("test") and triage_scope not in _TEST_RELATED_FIX_TRIAGE_SCOPES:
            continue
        files = record.get("files_written") or []
        files_text = ", ".join(files) if files else "(none)"
        triage_pass = record.get("triage_pass")
        triage_pass_text = f"\nTriage pass: {triage_pass}" if triage_pass else ""
        confirmed_triage = (record.get("confirmed_test_failure_triage") or "").strip()
        confirmed_triage_text = ""
        if confirmed_triage:
            confirmed_triage_text = "\nConfirmed production triage:\n" + _truncate(
                confirmed_triage,
                _MAX_FIXER_RECORD_CHARS,
            )
        output = (record.get("fixer_output") or "").strip() or "(no fixer output captured)"
        output = _truncate(output, _MAX_FIXER_RECORD_CHARS)
        label = "Test-origin validation fix" if triage_scope == "test_origin_validation" else "Test-failure fix"
        records.append(
            f"[{label} {idx}]\nFiles written: {files_text}"
            f"{triage_pass_text}{confirmed_triage_text}\nFixer output:\n{output}"
        )

    if not records:
        return ""
    return _truncate("\n\n".join(records[-3:]), _MAX_FIXER_HISTORY_CHARS)


class ReviewerAgent(BaseAgent):
    name = "reviewer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")
        if not state.files_changed:
            return AgentResult(success=False, message="No changed files to review")

        file_tool = self.tools.get("file")
        git_tool = self.tools.get("git")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        diff = ""
        if state.review_diff is not None:
            diff = state.review_diff[:_MAX_DIFF_CHARS]
            if len(state.review_diff) > _MAX_DIFF_CHARS:
                diff += "\n... (diff truncated)"
        elif git_tool:
            result = git_tool.diff_head()
            if result.success and result.output.strip():
                diff = result.output[:_MAX_DIFF_CHARS]
                if len(result.output) > _MAX_DIFF_CHARS:
                    diff += "\n... (diff truncated)"
        if not diff:
            diff = "(diff not available — use Read tool to inspect changed files)"

        step_scope = ""
        if state.active_scope == _SCOPE_FINAL_FULL_TASK:
            step_scope = _FINAL_FULL_TASK_REVIEW_SCOPE
        elif state.plan:
            step_idx = state.current_step
            future_steps = state.plan[step_idx + 1 :]
            future_text = "\n".join(f"  - {step}" for step in future_steps) if future_steps else "  - none"
            step_scope = _STEP_REVIEW_SCOPE.format(
                step_num=step_idx + 1,
                total_steps=len(state.plan),
                step_description=state.plan[step_idx],
                future_steps=future_text,
            )

        full_prompt = (
            _SYSTEM_REVIEW.format(
                tech_stack=_tech_stack(self.project_config),
                guidelines_context=_gather_guidelines(self.project_config, file_tool),
                build_tool_review_policy=_build_tool_reviewer_policy(self.project_config),
                test_review_policy=_test_review_policy(state),
            )
            + _load_extra_rules(self.project_config, self.name, file_tool)
            + "\n\n"
            + step_scope
            + ("\n\n" if step_scope else "")
            + _validation_pipeline_context(self.project_config, state)
            + "\n\n"
            + _USER_REVIEW.format(
                task_description=state.task_description,
                implementation_prompt=state.implementation_prompt,
                files_changed="\n".join(f"  - {f}" for f in state.files_changed),
                diff=diff,
            )
        )
        if state.test_files_written:
            files_list = "\n".join(f"  - {f}" for f in state.test_files_written)
            full_prompt += f"\n\n---\nFiles written by the test writer agent (not subject to implementer constraints):\n{files_list}"

        test_related_fix_history = _test_related_fix_history(state)
        if test_related_fix_history:
            full_prompt += (
                "\n\n---\nRecent test-related fixer records. Use this only to audit whether "
                "a test or test-origin validation fix weakened a task, guideline, or structured "
                "input contract:\n"
                f"{test_related_fix_history}"
            )

        reviewer_history = []
        for record in state.review_cycle_records:
            if record.get("reviewer") not in (None, "reviewer"):
                continue
            if state.active_scope == _SCOPE_FINAL_FULL_TASK:
                if record.get("scope") != _SCOPE_FINAL_FULL_TASK:
                    continue
            elif state.plan:
                if record.get("scope") == _SCOPE_FINAL_FULL_TASK or record.get("step") != state.current_step:
                    continue
            reviewer_history.append(record["reviewer_output"])
        if reviewer_history:
            history_text = "\n\n---\n".join(f"[Review {i + 1}]\n{r}" for i, r in enumerate(reviewer_history))
            full_prompt += f"\n\n---\nYour previous reviews of this task (maintain consistency):\n{history_text}"

        full_prompt = AGENT_SECURITY_PREFIX + full_prompt

        try:
            output = self.llm.run_readonly_agent(full_prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.record(self.name, "review_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

        if not output:
            return AgentResult(success=False, message="Reviewer produced empty output")

        last_line = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
        approved = re.sub(r"[^A-Za-z]", "", last_line).upper() == "APPROVED"

        state.review_cycle_records.append(
            {
                "step": state.current_step,
                "build_iteration": state.build_iterations,
                "review_iteration": state.review_iterations,
                "scope": _scope(state),
                "reviewer_prompt": full_prompt,
                "reviewer_output": output,
                "approved": approved,
                "has_warnings": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        if approved:
            state.review_approved = True
            state.review_issues.clear()
            state.record(self.name, "review", "approved")
            log.info(f"Review approved:\n{output}")
            return AgentResult(success=True, message="Review approved")

        state.review_approved = False
        state.review_issues = [output]
        state.record(self.name, "review", f"issues found ({len(output)} chars)")
        log.info(f"Review issues:\n{output}")
        return AgentResult(success=False, message="Review found issues", data={"issues": output})
