"""Init agent — generates project guidelines from codebase analysis.

Used only by `sikula init --guidelines`. Not part of the normal pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agents.base_agent import AGENT_SECURITY_PREFIX, read_only_agent_prompt
from core.llm_client import LLMClient

log = logging.getLogger(__name__)
_GUIDELINES_HEADING = "# Development Guidelines"

_SYSTEM = """\
You are analyzing a {tech_stack} codebase to write concise development guidelines for an AI coding agent.

Extract only conventions actually present in the code or existing project guidance docs — do not invent rules not evidenced by the codebase.
Prefer durable coding, testing, architecture, and agent guardrails over one-off setup or operator workflow instructions.

Focus on:
- Module and file organization patterns
- Naming conventions (types, functions, variables, files)
- Error handling patterns
- Testing conventions (structure, naming, coverage expectations)
- Key architectural constraints or invariants
- Any platform-specific patterns an agent must follow

Your response is the guidelines.md file content — raw markdown only.
Do not describe what you wrote. Do not summarize. Do not say you are done.
Start directly with this exact first line:
# Development Guidelines\
"""

_USER = """\
Analyze the codebase in the current directory and produce guidelines.md content.

Steps:
1. List the top-level directory structure to understand the project layout.
2. Read 3-5 source files from different modules to identify coding patterns.
3. Read existing project guidance docs when present (guidelines.md, AGENTS.md,
   ARCHITECTURE.md, README, CONTRIBUTING, docs/guidelines.md, docs/architecture.md).
4. Output ONLY the raw markdown — your entire response is the file content.
   Do not include any surrounding text, commentary, or summary.
5. The first line of your response must be exactly: # Development Guidelines\
"""


class InitAgent:
    def __init__(self, llm: LLMClient, tech_stack: str) -> None:
        self._llm = llm
        self._tech_stack = tech_stack

    def generate_guidelines(self, project_root: Path) -> str:
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX + _SYSTEM.format(tech_stack=self._tech_stack) + "\n\n" + _USER
        )
        output = self._llm.run_readonly_agent(prompt, cwd=project_root)
        return _clean_guidelines_output(output)


def _clean_guidelines_output(output: str) -> str:
    """Return only the generated markdown body.

    Agentic providers may emit progress messages before the final answer. That is
    useful in audit logs, but `sikula init --guidelines` writes the returned text
    directly to `.sikula/guidelines.md`, so leading chatter must be removed.
    """
    lines = output.strip().splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == _GUIDELINES_HEADING:
            return "\n".join(lines[idx:]).strip()
    for idx, line in enumerate(lines):
        if line.startswith("#"):
            return "\n".join(lines[idx:]).strip()
    return output.strip()
