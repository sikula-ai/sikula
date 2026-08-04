"""Planner agent — triage and split.

Runs after AnalystAgent, before ImplementerAgent (only when run_planner is True).
Decides whether the task warrants splitting into steps or a single pass.
Sets state.plan_decided = True after any successful decision so the phase is skipped on resume.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    load_extra_rules as _load_extra_rules,
    tech_stack as _tech_stack,
)
from core.delivery_unit_metadata import delivery_unit_planner_step_limit
from core.state import TaskState

log = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 8
_MAX_PLANNER_OUTPUT_ATTEMPTS = 2

_SYSTEM_PLAN = """\
You are a senior {tech_stack} software architect.

Your job: analyze an implementation prompt and decide whether splitting it into steps
adds value — and if so, produce those steps.

Decision criteria:

Choose SINGLE_PASS when:
- The task is a focused, single-concern change (one bug, one feature in one area)
- The implementation prompt is short and touches a small number of closely related files
- The change can be implemented, reviewed, and built in one pass without losing clarity

Choose to split when:
- The task covers multiple independent concerns that can be reviewed and built separately
- Changes span different modules or layers that do not need to happen atomically
- The implementation prompt is long and clearly describes several distinct phases of work

If splitting:
- Each step must leave the codebase in a compilable state when applied after all preceding steps
- A step must include every immediate compile dependency for symbols it introduces or uses:
  resource or localization keys/IDs, route/API/command constants, dependency-injection
  or service registrations, interface/trait/protocol/abstract methods and all required
  implementations, constructor or function parameters, imports, build config entries,
  and other referenced symbols.
- Do not create a step that references code, resources, or configuration planned for a
  later step. If step N references it, step N must also create or update it.
- Keep steps small enough to review easily — typically one class or one data flow per step
- Do not split a change across steps if doing so would break the build; put tightly coupled
  changes in one step
- If a compile-safe split would be awkward or unclear, choose SINGLE_PASS instead of
  forcing a multi-step plan
- {step_count_guidance}
- Do not include test changes — a dedicated agent handles tests separately

Output format contract:
- Output exactly SINGLE_PASS, or {numbered_list_contract}
- Each numbered item must be exactly one physical line
- Do not emit numbered sub-items, outline sections, headings, bullets, or continuation lines
- Do not split one implementation phase into separate dependency/setup/detail sub-steps
- Each numbered item must be a complete compile-safe implementation step

Output exactly one of:
  SINGLE_PASS
or a numbered list (nothing else):
  1. <step description>
  2. <step description>
  ...\
"""

_USER_PLAN = """\
Implementation prompt:
{implementation_prompt}

Analyze the prompt and output SINGLE_PASS or a numbered list of steps.\
"""

_DELIVERY_UNIT_PLAN_GUIDANCE = """\

Delivery unit planner-step budget:
- This task is one delivery unit with max_planner_steps={unit_limit}; SINGLE_PASS counts as one step.
- {within_budget_guidance}
- Consolidate tightly coupled work only when the complete unit remains coherent and compile-safe.
- Do not omit scope, acceptance criteria, validation, or required dependencies merely to fit the budget.
- If the complete unit cannot fit safely, return the smallest honest numbered plan with 2 or more
  compile-safe steps, even when that exceeds planner.max_steps. An oversized result is an intentional
  signal that the unit must be split before implementation.
"""

_USER_PLAN_RETRY = """\
Implementation prompt:
{implementation_prompt}

Your previous planner output was rejected because it parsed as {parsed_step_count}
numbered items, but planner.max_steps is {max_steps}.

This usually happens when sub-points, setup details, or outline sections are emitted as
separate numbered plan items.

Return exactly SINGLE_PASS or a new numbered list with 2–{max_steps} complete
compile-safe implementation steps. Each step must be exactly one physical line.
Do not output numbered sub-items, headings, bullets, continuation lines, or text before
or after the plan.\
"""

_USER_DELIVERY_PLAN_RETRY = """\
Implementation prompt:
{implementation_prompt}

Your previous planner output parsed as {parsed_step_count} steps, exceeding this delivery unit's
max_planner_steps={unit_limit}. Re-evaluate the unit once.

- {within_budget_guidance}
- Consolidate only if the entire unit remains coherent, complete, and compile-safe.
- Do not omit scope, acceptance criteria, validation, or required dependencies to fit the budget.
- If the unit genuinely cannot fit, return the smallest honest numbered plan with 2 or more compile-safe
  steps, even when that exceeds planner.max_steps, so Sikula can stop and require a delivery split.
- Output only SINGLE_PASS or the numbered list. Each numbered item must be exactly one physical line.
\
"""


class _PlanDecision(NamedTuple):
    single_pass: bool
    steps: list[str]


def _parse_plan(output: str) -> list[str]:
    steps = []
    for line in output.splitlines():
        m = re.match(r"^\d+\.\s+(.+)", line.strip())
        if m:
            steps.append(m.group(1).strip())
    return steps


def _is_single_pass(output: str) -> bool:
    return output.strip().upper() == "SINGLE_PASS"


class PlannerAgent(BaseAgent):
    name = "planner"

    def _max_steps(self) -> int | None:
        raw = self.project_config.get("planner", {}).get("max_steps", _DEFAULT_MAX_STEPS)
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            max_steps = raw
        elif isinstance(raw, str):
            value = raw.strip()
            if not re.fullmatch(r"\d+", value):
                return None
            max_steps = int(value)
        else:
            return None
        if max_steps < 2:
            return None
        return max_steps

    def _build_system_prompt(self, max_steps: int, file_tool, *, delivery_unit: bool = False) -> str:
        if delivery_unit:
            step_count_guidance = (
                "Follow the delivery-unit budget in the user prompt; if it cannot be met safely, "
                "return the smallest honest oversized plan."
            )
            numbered_list_contract = (
                "a numbered list with 2 or more items; exceed the unit budget only as an honest delivery split signal"
            )
        else:
            step_count_guidance = f"Aim for 2–{max_steps} steps; do not invent steps just to have more of them"
            numbered_list_contract = f"a numbered list with 2–{max_steps} items"
        return (
            AGENT_SECURITY_PREFIX
            + _SYSTEM_PLAN.format(
                tech_stack=_tech_stack(self.project_config),
                step_count_guidance=step_count_guidance,
                numbered_list_contract=numbered_list_contract,
            )
            + _load_extra_rules(self.project_config, self.name, file_tool)
        )

    @staticmethod
    def _delivery_within_budget_guidance(unit_limit: int) -> str:
        if unit_limit == 1:
            return "Prefer exactly SINGLE_PASS when the complete unit can be implemented safely in one pass."
        return "Prefer SINGLE_PASS or exactly 2 steps when the complete unit fits safely within the budget."

    def _accept_plan_decision(self, state: TaskState, decision: _PlanDecision) -> AgentResult:
        if decision.single_pass:
            log.info("Planner decided single-pass is sufficient")
            state.plan_decided = True
            state.record(self.name, "plan", "single-pass (planner decision)")
            return AgentResult(success=True, message="Single-pass")

        if len(decision.steps) < 2:
            log.info("Planner output could not be parsed into 2+ steps — using single-pass")
            state.plan_decided = True
            state.record(self.name, "plan", "single-pass fallback (fewer than 2 steps parsed)")
            return AgentResult(success=True, message="Single-pass fallback")

        state.plan = decision.steps
        state.plan_decided = True
        state.current_step = 0
        state.record(self.name, "plan", f"{len(decision.steps)} steps: {decision.steps}")
        log.info(
            "Plan (%d steps):\n%s",
            len(decision.steps),
            "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(decision.steps)),
        )
        return AgentResult(
            success=True,
            message=f"{len(decision.steps)}-step plan ready",
            data={"steps": decision.steps},
        )

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")

        file_tool = self.tools.get("file")
        max_steps = self._max_steps()
        if max_steps is None:
            msg = "Invalid planner.max_steps; expected an integer >= 2"
            state.record(self.name, "plan_failed", msg)
            return AgentResult(success=False, message=msg)

        unit_limit = None
        if state.delivery_plan_id and state.delivery_unit_id:
            unit_limit = delivery_unit_planner_step_limit(state.delivery_unit_budget)
        system = self._build_system_prompt(max_steps, file_tool, delivery_unit=unit_limit is not None)
        user = _USER_PLAN.format(implementation_prompt=state.implementation_prompt)
        if unit_limit is not None:
            user += _DELIVERY_UNIT_PLAN_GUIDANCE.format(
                unit_limit=unit_limit,
                within_budget_guidance=self._delivery_within_budget_guidance(unit_limit),
            )
        state.planner_prompt = system + "\n\n" + user

        preserved_delivery_decision: _PlanDecision | None = None
        for attempt in range(1, _MAX_PLANNER_OUTPUT_ATTEMPTS + 1):
            try:
                output = self.llm.generate(system, user)
            except RuntimeError as e:
                msg = str(e)
                state.record(self.name, "plan_failed", msg[:500])
                return AgentResult(success=False, message=msg[:200])

            state.planner_output = output
            if not output:
                if preserved_delivery_decision is not None and unit_limit is not None:
                    reason = "delivery unit re-evaluation produced empty output; preserving oversized plan"
                    state.record_planner_retry(
                        attempt,
                        reason,
                        output,
                        max_steps=unit_limit,
                        parsed_step_count=0,
                        will_retry=False,
                    )
                    return self._accept_plan_decision(state, preserved_delivery_decision)
                return AgentResult(success=False, message="Planner produced empty output")

            if _is_single_pass(output):
                return self._accept_plan_decision(state, _PlanDecision(single_pass=True, steps=[]))

            steps = _parse_plan(output)
            if preserved_delivery_decision is not None and unit_limit is not None and len(steps) < 2:
                reason = "delivery unit re-evaluation did not produce an explicit valid plan; preserving oversized plan"
                state.record_planner_retry(
                    attempt,
                    reason,
                    output,
                    max_steps=unit_limit,
                    parsed_step_count=len(steps),
                    will_retry=False,
                )
                return self._accept_plan_decision(state, preserved_delivery_decision)
            if unit_limit is not None and len(steps) > unit_limit:
                if attempt >= _MAX_PLANNER_OUTPUT_ATTEMPTS:
                    return self._accept_plan_decision(state, _PlanDecision(single_pass=False, steps=steps))
                preserved_delivery_decision = _PlanDecision(single_pass=False, steps=steps)
                reason = (
                    f"planner output parsed as {len(steps)} steps, exceeding delivery unit "
                    f"max_planner_steps={unit_limit}"
                )
                retry_user = _USER_DELIVERY_PLAN_RETRY.format(
                    implementation_prompt=state.implementation_prompt,
                    parsed_step_count=len(steps),
                    unit_limit=unit_limit,
                    within_budget_guidance=self._delivery_within_budget_guidance(unit_limit),
                )
                retry_prompt = system + "\n\n" + retry_user
                state.record_planner_retry(
                    attempt,
                    reason,
                    output,
                    max_steps=unit_limit,
                    parsed_step_count=len(steps),
                    will_retry=True,
                    retry_prompt=retry_prompt,
                )
                log.warning("Planner output exceeded delivery unit budget; re-evaluating once: %s", reason)
                user = retry_user
                continue
            if len(steps) <= max_steps:
                return self._accept_plan_decision(state, _PlanDecision(single_pass=False, steps=steps))

            reason = f"planner output parsed as {len(steps)} steps, exceeding planner.max_steps={max_steps}"
            will_retry = attempt < _MAX_PLANNER_OUTPUT_ATTEMPTS
            retry_user = None
            retry_prompt = None
            if will_retry:
                retry_user = _USER_PLAN_RETRY.format(
                    implementation_prompt=state.implementation_prompt,
                    parsed_step_count=len(steps),
                    max_steps=max_steps,
                )
                retry_prompt = system + "\n\n" + retry_user
            state.record_planner_retry(
                attempt,
                reason,
                output,
                max_steps=max_steps,
                parsed_step_count=len(steps),
                will_retry=will_retry,
                retry_prompt=retry_prompt,
            )
            log.warning("Planner output rejected: %s", reason)
            if not will_retry:
                msg = f"{reason}; planner retry exhausted"
                state.record(self.name, "plan_failed", msg[:500])
                return AgentResult(success=False, message=msg[:200])
            user = retry_user or user

        return AgentResult(success=False, message="Planner retry exhausted")
