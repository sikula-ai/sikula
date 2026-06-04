"""Build-tool-specific prompt guidance for agents."""

from __future__ import annotations


def _build_tool(project_config: dict) -> str:
    return str(project_config.get("project", {}).get("build_tool", "")).lower()


def write_agent_constraints(project_config: dict) -> str:
    if _build_tool(project_config) != "cargo":
        return ""
    return (
        "- For Cargo/Rust projects, do not manually synthesize or edit Cargo.lock package\n"
        "  entries. If Cargo.toml changes require lockfile updates, rely on configured\n"
        "  Cargo sync/build tooling.\n"
    )


def reviewer_policy(project_config: dict) -> str:
    if _build_tool(project_config) != "cargo":
        return ""
    return (
        "   Build-tool-specific policy: Cargo lockfiles should be produced or validated\n"
        "      by Cargo tooling, not hand-written by agents. If Cargo.lock changes,\n"
        "      review it for signs of manual fabrication or inconsistent dependency resolution.\n"
        "      Do not ask the implementer to run Cargo tooling manually, and do not block solely\n"
        "      because a sync/build validation record does not exist yet; the build/fix phase\n"
        "      owns running configured sync/build/check commands after review.\n"
    )
