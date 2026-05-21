"""Planner agent — triage and split.

Runs after AnalystAgent, before ImplementerAgent (only when run_planner is True).
Decides whether the task warrants splitting into steps or a single pass.
Sets state.plan_decided = True after any successful decision so the phase is skipped on resume.
"""

from __future__ import annotations

import logging
import re

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    load_extra_rules as _load_extra_rules,
    tech_stack as _tech_stack,
)
from core.state import TaskState

log = logging.getLogger(__name__)

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
- Aim for 2–{max_steps} steps; do not invent steps just to have more of them
- Do not include test changes — a dedicated agent handles tests separately

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


def _parse_plan(output: str) -> list[str]:
    steps = []
    for line in output.splitlines():
        m = re.match(r"^\d+\.\s+(.+)", line.strip())
        if m:
            steps.append(m.group(1).strip())
    return steps


class PlannerAgent(BaseAgent):
    name = "planner"

    def _max_steps(self) -> int:
        return self.project_config.get("planner", {}).get("max_steps", 8)

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")

        file_tool = self.tools.get("file")
        system = (
            AGENT_SECURITY_PREFIX
            + _SYSTEM_PLAN.format(tech_stack=_tech_stack(self.project_config), max_steps=self._max_steps())
            + _load_extra_rules(self.project_config, self.name, file_tool)
        )
        user = _USER_PLAN.format(implementation_prompt=state.implementation_prompt)
        state.planner_prompt = system + "\n\n" + user
        try:
            output = self.llm.generate(system, user)
        except RuntimeError as e:
            msg = str(e)
            state.record(self.name, "plan_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

        if not output:
            return AgentResult(success=False, message="Planner produced empty output")

        if any(line.strip().upper() == "SINGLE_PASS" for line in output.splitlines()):
            log.info("Planner decided single-pass is sufficient")
            state.plan_decided = True
            state.record(self.name, "plan", "single-pass (planner decision)")
            return AgentResult(success=True, message="Single-pass")

        steps = _parse_plan(output)
        if len(steps) < 2:
            log.info("Planner output could not be parsed into 2+ steps — using single-pass")
            state.plan_decided = True
            state.record(self.name, "plan", "single-pass fallback (fewer than 2 steps parsed)")
            return AgentResult(success=True, message="Single-pass fallback")

        state.plan = steps
        state.plan_decided = True
        state.current_step = 0
        state.record(self.name, "plan", f"{len(steps)} steps: {steps}")
        log.info(
            "Plan (%d steps):\n%s",
            len(steps),
            "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(steps)),
        )
        return AgentResult(success=True, message=f"{len(steps)}-step plan ready", data={"steps": steps})
