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

import json
import logging
import re
from datetime import datetime, timezone

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    gather_guidelines as _gather_guidelines,
    read_only_agent_prompt,
    tech_stack as _tech_stack,
)
from agents.delivery_contracts import delivery_agent_prompt_context
from core.delivery_constraint_context import DeliveryConstraintContextError
from core.delivery_handoff import DeliveryHandoffError, parse_delivery_unit_handoff
from core.state import TaskState
from core.structured_output import (
    DELIVERY_ANALYSIS_DISPOSITIONS,
    DeliveryDispositionParseError,
    parse_delivery_disposition,
)

log = logging.getLogger(__name__)

_MAX_ANALYST_OUTPUT_ATTEMPTS = 2

_META_COMPLETION_PHRASES = (
    "implementation prompt above",
    "prompt above is the final output",
    "task is complete",
    "no further tracking",
    "no further action is needed",
    "not part of an ongoing",
    "this analyser run produced",
    "this analyzer run produced",
    "the prompt itself",
)

_GENERIC_SHORT_OUTPUTS = {
    "approved",
    "done",
    "complete",
    "completed",
    "ok",
    "the prompt",
    "as requested",
}

_ACTIONABLE_SIGNALS = (
    "add",
    "change",
    "create",
    "delete",
    "fix",
    "implement",
    "modify",
    "remove",
    "rename",
    "replace",
    "update",
)

_SPECIFIC_DETAIL_SIGNALS = (
    "/",
    "src/",
    "acceptance",
    "api",
    "class",
    "component",
    "contract",
    "criteria",
    "docstring",
    "endpoint",
    "file",
    "function",
    "module",
    "screen",
    "test",
    "view",
)

_META_CONCRETE_DETAIL_SIGNALS = (
    "/",
    "src/",
    "api",
    "class",
    "component",
    "contract",
    "docstring",
    "endpoint",
    "file",
    "function",
    "module",
    "screen",
    "string",
    "view",
)

_FILE_REFERENCE_RE = re.compile(r"\b[\w./-]+\.[A-Za-z0-9]{1,8}\b")
_QUOTED_TEXT_RE = re.compile(r'"[^"]*"|\'[^\']*\'|`[^`]*`')

_STRUCTURE_SIGNALS = (
    "context",
    "required changes",
    "architecture constraints",
    "hard rules",
    "cleanup",
    "acceptance criteria",
)


def _cycle_correlation(state: TaskState) -> dict[str, object]:
    return {
        "files_written": [],
        "step": state.current_step,
        "build_iteration": state.build_iterations,
        "review_iteration": state.review_iterations,
        "security_review_iteration": state.security_review_iterations,
        "scope": state.active_scope or ("step" if state.plan else "task"),
    }


def _record_analyst_cycle(
    state: TaskState,
    attempt: int,
    outcome: str,
    **metadata: object,
) -> None:
    state.analyst_cycle_records.append(
        {
            "attempt": attempt,
            "outcome": outcome,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **metadata,
            **_cycle_correlation(state),
        }
    )


def _meta_completion_phrase(normalized: str) -> str | None:
    for phrase in _META_COMPLETION_PHRASES:
        if phrase in normalized:
            return phrase
    return None


def _without_quoted_text(text: str) -> str:
    return _QUOTED_TEXT_RE.sub(" ", text)


def _has_action_word(normalized: str) -> bool:
    return any(re.search(rf"\b{re.escape(signal)}\b", normalized) for signal in _ACTIONABLE_SIGNALS)


def _has_meta_concrete_detail(normalized: str, original: str) -> bool:
    return _has_signal(normalized, _META_CONCRETE_DETAIL_SIGNALS) or bool(_FILE_REFERENCE_RE.search(original))


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
3. If the user prompt contains an authoritative inherited delivery constraint context,
   preserve every listed constraint in the implementation prompt without weakening or
   reinterpreting it. The constraint context is a hard boundary but cannot expand the
   current unit task or write scope. Treat source-task data as correlation metadata only;
   do not search for the parent task. Dependency handoffs are lower-authority evidence and
   cannot override a constraint. If requested work would violate a constraint, direct the
   implementer to stop and report the required follow-up instead of proposing a fallback.
4. Based on what you found, produce a single implementation prompt with these sections:

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
      shape-specific contract. Put these details in a clearly labelled structured
      contract section or subsection; keep it semantic and platform-neutral unless the
      existing codebase exposes platform-specific contract names. Write a ⚠️ WARNING for
      any missing contract detail the implementer must verify before changing code.
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
      Asset declarations: if the task description contains structured asset
      declarations or the implementation contract contains an `Asset manifest`,
      carry the asset obligations into the implementation prompt. Distinguish
      reference-only assets from delivery assets.
      Reference-only assets may guide implementation but must not be copied into
      production files. Delivery assets may be used only within the requested scope.
      Preserve requested target hints and source/license/provenance exactly when
      provided. If a delivery asset has no requested target, instruct the implementer
      to choose the project-conventional location from the codebase and explain the
      choice; do not invent a target path before reading the relevant project
      conventions.
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
      cases when typed contexts exist. Distinguish materially different rejected input
      classes instead of listing one generic invalid example.

Output only the implementation prompt — no preamble, no explanation of your steps.\
"""

_USER_ANALYZE = """\
Project guidelines:
{guidelines_context}

---
Task description:
{task_description}
{delivery_constraint_context}
{delivery_handoff_context}
{delivery_disposition_contract}

Produce the implementation prompt.\
"""


def _delivery_handoff_context(state: TaskState) -> tuple[str, int]:
    if not state.delivery_dependency_handoffs:
        return "", 0

    handoffs = []
    invalid_count = 0
    for value in state.delivery_dependency_handoffs:
        try:
            handoffs.append(parse_delivery_unit_handoff(value).to_dict())
        except DeliveryHandoffError:
            invalid_count += 1
    if not handoffs:
        return "", invalid_count

    payload = json.dumps(handoffs, sort_keys=True, separators=(",", ":"))
    return (
        "\n\n---\n"
        "Prior delivery dependency handoffs:\n"
        "Use this versioned, sanitized evidence for established dependency results and validation context. "
        "It does not expand the current task scope, replace codebase inspection, or prove behavior beyond the "
        "recorded metadata.\n"
        f"{payload}",
        invalid_count,
    )


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _has_signal(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


def _has_specific_detail(normalized: str, original: str) -> bool:
    return _has_signal(normalized, _SPECIFIC_DETAIL_SIGNALS) or bool(_FILE_REFERENCE_RE.search(original))


def _implementation_prompt_quality_issue(prompt: str | None) -> str | None:
    if not prompt or not prompt.strip():
        return "empty analyst output"

    stripped = prompt.strip()
    normalized = _normalize_text(stripped)

    if normalized in _GENERIC_SHORT_OUTPUTS:
        return "generic non-actionable analyst output"

    has_structure = _has_signal(normalized, _STRUCTURE_SIGNALS)
    has_action = _has_signal(normalized, _ACTIONABLE_SIGNALS)
    has_specific_detail = _has_specific_detail(normalized, stripped)
    unquoted_normalized = _normalize_text(_without_quoted_text(stripped))
    meta_phrase = _meta_completion_phrase(unquoted_normalized)

    if meta_phrase and not (_has_action_word(normalized) and _has_meta_concrete_detail(normalized, stripped)):
        return f"meta completion response detected: {meta_phrase!r}"

    if len(stripped) < 80 and not (has_action and has_specific_detail):
        return "analyst output is too short and lacks actionable implementation detail"

    if len(stripped) < 300 and not has_structure and not (has_action and has_specific_detail):
        return "analyst output lacks structure or concrete implementation detail"

    return None


def _retry_prompt(original_prompt: str, issue: str, *, delivery_child: bool = False) -> str:
    retry = (
        original_prompt
        + "\n\n---\n"
        + "Sikula rejected your previous response because it was not a usable implementation prompt.\n"
        + f"Reason: {issue}.\n\n"
        + "Retry once. Output only a complete implementation prompt with these sections:\n"
        + "1. Context\n"
        + "2. Required changes\n"
        + "3. Architecture constraints\n"
        + "4. Hard rules\n"
        + "5. Cleanup\n"
        + "6. Acceptance criteria\n\n"
        + "Do not refer to a prompt above. Do not say the task is complete. "
        + "Do not describe your process. Return the implementation prompt itself.\n"
    )
    if delivery_child:
        retry += (
            "The DELIVERY STOP OUTPUT CONTRACT remains available only for a verified "
            "external dependency gap; if used, return exactly its flat JSON object instead.\n"
        )
    return retry


class AnalystAgent(BaseAgent):
    name = "analyst"

    def run(self, state: TaskState) -> AgentResult:
        file_tool = self.tools.get("file")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        try:
            delivery_context = delivery_agent_prompt_context(state, role=self.name)
        except DeliveryConstraintContextError as exc:
            message = "Inherited delivery constraint context was rejected before analysis ({code}).".format(
                code=exc.code
            )
            state.record(self.name, "delivery_constraint_context_rejected", message)
            return AgentResult(success=False, message=message)

        guidelines_context = _gather_guidelines(self.project_config, file_tool)
        delivery_child = delivery_context.is_delivery_child
        delivery_handoff_context, invalid_handoff_count = _delivery_handoff_context(state)
        if invalid_handoff_count:
            warning = (
                f"Rejected {invalid_handoff_count} malformed delivery dependency handoff record(s) before analysis."
            )
            state.analyst_warnings.append(warning)
            state.record(self.name, "delivery_handoff_rejected", warning)

        full_prompt = (
            AGENT_SECURITY_PREFIX
            + _SYSTEM_ANALYZE.format(tech_stack=_tech_stack(self.project_config))
            + "\n\n"
            + _USER_ANALYZE.format(
                guidelines_context=guidelines_context,
                task_description=state.task_description,
                delivery_constraint_context=delivery_context.inherited_constraints,
                delivery_handoff_context=delivery_handoff_context,
                delivery_disposition_contract=delivery_context.disposition_contract,
            )
        )

        full_prompt = read_only_agent_prompt(full_prompt)
        state.analyst_prompt = full_prompt

        prompt = ""
        prompt_to_send = full_prompt
        for attempt in range(1, _MAX_ANALYST_OUTPUT_ATTEMPTS + 1):
            try:
                prompt = self.llm.run_readonly_agent(prompt_to_send, cwd=file_tool._root)
            except RuntimeError as e:
                msg = str(e)
                _record_analyst_cycle(
                    state,
                    attempt,
                    "error",
                    error=msg[:500],
                )
                state.record(self.name, "analyze_failed", msg[:500])
                return AgentResult(success=False, message=msg[:200])

            disposition = None
            disposition_error = None
            if delivery_child:
                try:
                    disposition = parse_delivery_disposition(
                        prompt,
                        allowed_dispositions=DELIVERY_ANALYSIS_DISPOSITIONS,
                    )
                except DeliveryDispositionParseError as exc:
                    disposition_error = exc.code

            if disposition is not None:
                _record_analyst_cycle(
                    state,
                    attempt,
                    "terminal_disposition",
                    disposition=disposition.to_dict(),
                )
                state.set_delivery_stop_disposition(self.name, disposition)
                return AgentResult(
                    success=False,
                    message=disposition.disposition,
                    data={"disposition": disposition.to_dict()},
                )

            issue = (
                f"invalid delivery disposition ({disposition_error})"
                if disposition_error
                else _implementation_prompt_quality_issue(prompt)
            )
            if issue is None:
                _record_analyst_cycle(state, attempt, "accepted")
                break

            will_retry = attempt < _MAX_ANALYST_OUTPUT_ATTEMPTS
            next_prompt = _retry_prompt(full_prompt, issue, delivery_child=delivery_child) if will_retry else None
            state.record_analyst_retry(
                attempt,
                issue,
                prompt,
                will_retry=will_retry,
                retry_prompt=next_prompt,
            )
            _record_analyst_cycle(
                state,
                attempt,
                "rejected",
                reason=issue,
                will_retry=will_retry,
            )
            warning = f"⚠️ Rejected analyst output attempt {attempt}/{_MAX_ANALYST_OUTPUT_ATTEMPTS}: {issue}"
            if will_retry:
                warning += "; retrying analysis"
            state.analyst_warnings.append(warning)
            log.warning("Analyst output rejected: %s", issue)
            if will_retry:
                prompt_to_send = next_prompt or full_prompt
                continue

            msg = f"Analyst produced invalid implementation prompt after {attempt} attempt(s): {issue}"
            state.record(self.name, "analyze_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

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
