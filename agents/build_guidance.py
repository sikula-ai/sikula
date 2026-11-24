"""Platform-neutral ownership of build synchronization and generated outputs."""

from __future__ import annotations

from core.validation_coverage import INTERNAL_PIPELINE_CONFIG_KEY


def pipeline_sync_guidance(project_config: dict) -> str:
    enabled = project_config.get(INTERNAL_PIPELINE_CONFIG_KEY, {}).get(
        "run_build", project_config.get("run_build", True)
    )
    timing = (
        "For implementation writes with build validation enabled, changed build inputs are synchronized by the pipeline "
        "before enabled reviews; otherwise sync runs before build."
        if enabled
        else "Build validation is disabled for this run: do not assume sync will run or outputs will be regenerated."
    )
    return (
        "\n\nPipeline-owned build outputs:\n"
        "The configured build tool owns regeneration of dependency lockfiles and other designated sync outputs. "
        "Plan and implement changes to their source inputs, not manual generated-file edits. "
        "Do not ask implementation or fix agents to run build tooling, even when repository instructions "
        "describe those commands for human developers. Do not manually synthesize or edit these generated outputs. "
        + timing
        + " Review adopted outputs for correctness, signs of manual fabrication and unintended dependency changes. "
        "Do not waive stale or inconsistent output merely because later tooling might fix it. "
        "Do not reject solely for the absence of compile/test records before the build phase. "
        "Sync adoption does not authorize writes outside the active scope or establish trust in new dependencies.\n\n"
    )


def write_agent_constraints(project_config: dict) -> str:
    return pipeline_sync_guidance(project_config)


def reviewer_policy(project_config: dict) -> str:
    return pipeline_sync_guidance(project_config)
