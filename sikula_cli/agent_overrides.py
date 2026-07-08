"""CLI helpers for per-agent LLM override flags."""

from __future__ import annotations

import sys

RUNTIME_AGENT_NAMES = {
    "analyst",
    "planner",
    "implementer",
    "reviewer",
    "security_reviewer",
    "test_writer",
    "fixer",
}
PREPARATION_AGENT_NAMES = {"task_preparer"}
DELIVERY_PREPARATION_AGENT_NAMES = {"delivery_preparer"}


def parse_agent_llm_overrides(
    agent_models: list[str] | None,
    agent_providers: list[str] | None,
    agent_timeouts: list[str] | None,
    *,
    valid_agents: set[str] | None = None,
) -> dict[str, dict]:
    """Parse --agent-model / --agent-provider / --agent-timeout into per-agent override dicts."""
    result: dict[str, dict] = {}
    allowed_agents = valid_agents or RUNTIME_AGENT_NAMES

    def _add(entries: list[str] | None, field: str, cast=str, flag: str | None = None) -> None:
        flag_name = f"--agent-{flag or field}"
        for entry in entries or []:
            raw_agent, sep, val = entry.partition("=")
            agent = raw_agent.strip().replace("-", "_")
            if agent not in allowed_agents:
                print(f"Unknown agent '{agent}'. Valid agents: {', '.join(sorted(allowed_agents))}")
                sys.exit(1)
            if not sep or not val.strip():
                print(f"Invalid {flag_name} value '{entry}'. Expected format: AGENT=VALUE")
                sys.exit(1)
            try:
                result.setdefault(agent, {})[field] = cast(val.strip())
            except (ValueError, TypeError):
                print(f"Invalid {flag_name} value '{val.strip()}' for agent '{agent}': expected {cast.__name__}")
                sys.exit(1)

    _add(agent_models, "model")
    _add(agent_providers, "provider")
    _add(agent_timeouts, "agent_timeout", cast=int, flag="timeout")
    return result
