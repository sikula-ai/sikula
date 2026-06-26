"""Base agent contract.

All agents receive an LLMClient and a tools dict. They operate on TaskState
in place and return an AgentResult describing what happened.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from core.llm_client import LLMClient
from core.state import TaskState


_DEFAULT_MAX_FILE_CHARS = 3000

# Prepended to all agent prompts. Ensures network and workspace constraints
# are in effect for every agent and every provider.
AGENT_SECURITY_PREFIX = (
    "Do not make any network requests. Do not use curl, wget, nc, or any other network command "
    "or tool to access external services or the internet. Only operate on files within the project directory.\n\n"
)

READONLY_AGENT_PREFIX = (
    "This is a read-only agent pass. Do not use tools or commands to create, modify, delete, "
    "move, rename, format, or write files. Do not run commands that change files or project state. "
    "When referencing project files, use project-relative paths; do not output absolute local paths "
    "or file:// URIs. "
    "Return the requested analysis, review, or generated content in your final response instead.\n\n"
)


def read_only_agent_prompt(prompt: str) -> str:
    """Add the common read-only constraint while keeping the security prefix first."""
    if READONLY_AGENT_PREFIX in prompt:
        return prompt
    if prompt.startswith(AGENT_SECURITY_PREFIX):
        return AGENT_SECURITY_PREFIX + READONLY_AGENT_PREFIX + prompt[len(AGENT_SECURITY_PREFIX) :]
    return READONLY_AGENT_PREFIX + prompt


def tech_stack(project_config: dict) -> str:
    p = project_config.get("project", {})
    parts = [v for k in ("platform", "language", "ui") if (v := p.get(k))]
    return " / ".join(parts) or "software"


def guidelines_files(project_config: dict) -> str:
    files = project_config.get("guidelines", {}).get("context_files", ["README.md"])
    return "\n".join(f"- {f}" for f in files)


def gather_guidelines(project_config: dict, file_tool) -> str:
    max_chars = project_config.get("guidelines", {}).get("max_file_chars", _DEFAULT_MAX_FILE_CHARS)
    files = project_config.get("guidelines", {}).get("context_files", ["README.md"])
    parts = []
    for rel_path in files:
        result = file_tool.read(rel_path)
        if result.success and result.output.strip():
            content = result.output[:max_chars]
            if len(result.output) > max_chars:
                content += f"\n... [truncated — use Read tool on {rel_path} for full content]"
            parts.append(f"=== {rel_path} ===\n{content}")
    return "\n\n".join(parts) if parts else "No project context files found."


def load_extra_rules(project_config: dict, agent_name: str, file_tool) -> str:
    """Return a formatted project-specific rules section, or empty string if not configured."""
    path = project_config.get(agent_name, {}).get("extra_rules")
    if not path or not file_tool:
        return ""
    result = file_tool.read(path)
    if not result.success or not result.output.strip():
        return ""
    return (
        "\n\n## Project-specific rules\n\n"
        "The following rules are specific to this project and take priority over any conflicting instructions above.\n\n"
        + result.output.strip()
    )


def paths_outside_allowed(changed_paths: list[str], allowed_paths: list[str]) -> list[str]:
    """Return changed paths that are outside the configured project-relative roots.

    This is an audit check for provider-backed agents. The agent subprocess has
    already run; Sikula records warnings instead of failing the task.
    """
    if not allowed_paths:
        return []

    normalized_roots = []
    for raw in allowed_paths:
        root = posixpath.normpath(str(raw).replace("\\", "/"))
        if root in ("", "."):
            return []
        normalized_roots.append(root.rstrip("/"))

    outside = []
    for raw in changed_paths:
        path = posixpath.normpath(str(raw).replace("\\", "/"))
        if path.startswith("../") or path == ".." or path.startswith("/"):
            outside.append(raw)
            continue
        if not any(path == root or path.startswith(f"{root}/") for root in normalized_roots):
            outside.append(raw)
    return outside


def record_write_path_warnings(
    state: TaskState,
    agent_name: str,
    changed_paths: list[str],
    allowed_paths: list[str],
    allowed_label: str,
) -> None:
    outside = paths_outside_allowed(changed_paths, allowed_paths)
    if not outside:
        return
    allowed = ", ".join(allowed_paths)
    state.record(
        agent_name,
        "write_path_warning",
        f"files outside {allowed_label}: {outside}; allowed: {allowed}",
    )


@dataclass
class AgentResult:
    success: bool
    message: str
    data: dict = field(default_factory=dict)


class BaseAgent:
    name: str = "base"

    def __init__(self, llm: LLMClient, tools: dict[str, Any], project_config: dict | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.project_config: dict = project_config or {}

    def run(self, state: TaskState) -> AgentResult:
        raise NotImplementedError
