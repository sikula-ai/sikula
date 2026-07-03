"""Run command parser helpers for the Sikula CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable


def register_parser(
    subparsers,
    *,
    contract_score_threshold: Callable[[str], int],
) -> argparse.ArgumentParser:
    run_p = subparsers.add_parser("run", help="Run a task")
    run_p.add_argument(
        "task_file_pos",
        nargs="?",
        default=None,
        metavar="TASK_FILE",
        help="Path to task .txt/.md file (positional shorthand for --task-file)",
    )
    run_p.add_argument("--task-file", help="Path to task .txt/.md file (absolute or relative to CWD)")
    run_p.add_argument("--task-id", help="Resume existing task by ID")
    run_p.add_argument(
        "--reset-failed",
        action="store_true",
        default=False,
        help="Reset a failed task before resuming; requires --task-id",
    )
    run_p.add_argument(
        "--no-isolate",
        action="store_true",
        default=False,
        help="Run in the project directory directly without creating a git worktree branch",
    )

    # Phase toggle flags: default None means use config; True/False forces a value.
    boolean_action = argparse.BooleanOptionalAction
    run_p.add_argument("--build", action=boolean_action, default=None, help="Override run_build")
    run_p.add_argument("--presync", action=boolean_action, default=None, help="Override run_presync")
    run_p.add_argument(
        "--presync-clean",
        action=boolean_action,
        default=None,
        help="Override build.presync_clean (run clean before presync task)",
    )
    run_p.add_argument("--planner", action=boolean_action, default=None, help="Override run_planner")
    run_p.add_argument("--review", action=boolean_action, default=None, help="Override run_review")
    run_p.add_argument(
        "--security-review",
        action=boolean_action,
        default=None,
        help="Override run_security_review",
    )
    run_p.add_argument("--test-writing", action=boolean_action, default=None, help="Override run_test_writing")
    run_p.add_argument("--tests", action=boolean_action, default=None, help="Override run_tests")
    run_p.add_argument(
        "--build-per-step",
        action=boolean_action,
        default=None,
        help="Override run_build_per_step",
    )
    run_p.add_argument("--checks", action=boolean_action, default=None, help="Override run_checks")
    run_p.add_argument(
        "--require-contract-ready",
        action="store_true",
        default=False,
        help="Abort fresh task-file runs before agents unless the implementation contract is ready",
    )
    run_p.add_argument(
        "--min-contract-score",
        type=contract_score_threshold,
        default=None,
        metavar="0-100",
        help="Abort fresh task-file runs before agents unless the implementation contract score is at least this value",
    )

    # Per-agent LLM overrides are repeatable and layer on top of agents.<name>.llm.
    run_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one agent, e.g. --agent-model analyst=gpt-5.5",
    )
    run_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one agent, e.g. --agent-provider implementer=claude",
    )
    run_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override agent_timeout for one agent, e.g. --agent-timeout implementer=2400",
    )
    return run_p
