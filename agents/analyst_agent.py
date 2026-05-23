"""Analyst agent — reads a task description and produces an implementation prompt.

Single-pass design:
  1. Load project guidelines (static files listed under guidelines.context_files in YAML).
  2. Run LLM as a read-only agent (Read/grep/find tools, no writes) with task + guidelines.
     The agent browses the codebase to find relevant files before generating the prompt.

The implementation_prompt is passed to ImplementerAgent, which runs the LLM as an autonomous
agent with file read/write tools and applies the changes directly.

Platform-specific settings (project_config / YAML):
  guidelines.context_files — static docs loaded as guidelines context
  guidelines.max_file_chars — max characters read per guidelines file (default 3 000)
"""

from __future__ import annotations

import logging

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    gather_guidelines as _gather_guidelines,
    tech_stack as _tech_stack,
)
from core.state import TaskState

log = logging.getLogger(__name__)

_SYSTEM_ANALYZE = """\
You are a senior {tech_stack} software architect with read access to the project codebase.

Your job: analyze a feature or bug task, explore the codebase to understand the relevant code,
and produce a precise, actionable implementation prompt for a developer.

Steps:
1. Read the project guidelines provided in the prompt.
2. Use your tools (Read, grep, find) to locate and read the files relevant to the task.
   Referenced files: if the task description mentions any files by name (design mockups,
   screenshots, images, PDFs, specs, …), locate them first with
   `find . -name "<filename>"` and read them before proceeding — do not assume they are
   absent without searching. Only write a ⚠️ WARNING if a file genuinely cannot be found
   after an explicit search.
3. Based on what you found, produce a single implementation prompt with these sections:

   1. Context: which layer/module is affected and why
   2. Required changes: list the exact files to create or modify with specific changes for each,
      based on what you actually found in the codebase — not guesses.
      Completeness rule: for every file you include in this section, you must read
      the entire file before finalising the change list. Do not rely solely on grep
      hits — grep finds the first/most obvious occurrence but misses others in the
      same file (e.g. a symbol used in both init and onRefresh). Read the full file,
      then list every occurrence of each affected symbol.
      API contract: for every endpoint the task references or that will be added or
      modified, extract the complete response contract and include it in the
      implementation prompt: (a) response shape — single object or collection; (b) for
      any response type that does not already exist in the codebase, the field names and
      their types. Determine this from these sources in order of priority:
        (1) the task description;
        (2) API contract documentation in the project (OpenAPI/Swagger specs, GraphQL
            schemas, .proto files, generated type files) — search for these files and
            read the relevant definitions.
      If neither source provides a complete answer, write a ⚠️ WARNING for each missing
      piece — the implementer must verify before implementing.
      Structured input contract: when the task touches a parser, validator, expression
      engine, schema, DSL, config loader, rule engine, or any code accepting structured
      user/project input, include the full validation contract in the implementation
      prompt. Identify accepted inputs, rejected inputs, expected result types for each
      context, variable/function scope, literal handling, and whether errors must be
      caught during validation or may occur at runtime. If the codebase has separate
      generic and expected-type validation APIs, state which API each context must use.
      Do not stop at syntax or known-name checks when the task requires a typed or
      shape-specific contract. Write a ⚠️ WARNING for any missing contract detail the
      implementer must verify before changing code.
      String resources: for every user-visible string introduced by the task, include
      the exact key and value in the implementation prompt. Determine them from:
        (1) explicit string definitions in the task description — use keys and values
            verbatim; if platform-specific notation is used, use it directly;
        (2) prose descriptions of text in the task description — extract the value,
            derive the key from project string naming conventions (read existing string
            resource files to learn the convention).
      If the project uses a translation management tool (detectable from project files
      or mentioned in guidelines) and new strings are introduced without specified keys,
      write a ⚠️ WARNING — invented keys will break the translation workflow.
      If new user-visible text is introduced without any value specified in the task
      description, write a ⚠️ WARNING for each.
   3. Architecture constraints: patterns to follow from the project guidelines
   4. Hard rules:
      - Minimal changes only — touch nothing outside the described scope
      - Do not refactor unrelated code
      - Do not add unrelated comments or documentation
      - Do not suggest any changes to test files (files under test/, __tests__/,
        spec/, or any other test source directory) — a dedicated TestAgent
        handles tests separately; omit test changes entirely from the prompt
   5. Cleanup: identify all code that becomes dead after the change.
      Core rule: whenever you remove a reference to any named symbol (class, function,
      method, constant, property, extension function, type alias, …), grep for that
      symbol across the codebase excluding test files. If it has zero remaining
      references in production code, remove its definition too — then apply the same
      rule to everything that definition referenced. Repeat recursively until no further
      dead code is found. A symbol referenced only inside test files counts as dead
      in production — remove its definition (the TestAgent will update the tests).
      After removing any member from a type definition (class, interface, …), verify
      that ALL remaining members of that type still have at least one production
      caller — not just the ones directly referenced by the deleted code.
      Semantic caller check: for each remaining caller you find, read it and ask
      whether it exists solely to support the behaviour the task is eliminating. If
      yes, it is also in scope — add it to Required Changes and continue the dead
      code sweep from there.
   6. Acceptance criteria: what a correct implementation looks like. For parser,
      validator, expression engine, schema, DSL, config loader, or rule engine changes,
      include explicit accepted and rejected cases, including wrong expected result type
      cases when typed contexts exist.

Output only the implementation prompt — no preamble, no explanation of your steps.\
"""

_USER_ANALYZE = """\
Project guidelines:
{guidelines_context}

---
Task description:
{task_description}

Produce the implementation prompt.\
"""


class AnalystAgent(BaseAgent):
    name = "analyst"

    def run(self, state: TaskState) -> AgentResult:
        file_tool = self.tools.get("file")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        guidelines_context = _gather_guidelines(self.project_config, file_tool)

        full_prompt = (
            AGENT_SECURITY_PREFIX
            + _SYSTEM_ANALYZE.format(tech_stack=_tech_stack(self.project_config))
            + "\n\n"
            + _USER_ANALYZE.format(
                guidelines_context=guidelines_context,
                task_description=state.task_description,
            )
        )

        state.analyst_prompt = full_prompt

        try:
            prompt = self.llm.run_readonly_agent(full_prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.record(self.name, "analyze_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

        if not prompt:
            return AgentResult(success=False, message="Analyst produced empty output")

        warnings = [line.strip() for line in prompt.splitlines() if line.strip().startswith("⚠️")]
        if warnings:
            state.analyst_warnings.extend(warnings)
            for w in warnings:
                log.warning("Analyst warning: %s", w)
            state.record(self.name, "analyze_warnings", f"{len(warnings)} warning(s)")

        state.implementation_prompt = prompt
        state.record(self.name, "analyze", f"prompt generated ({len(prompt)} chars)")

        return AgentResult(
            success=True,
            message="Implementation prompt ready",
            data={"implementation_prompt": prompt},
        )
