#!/usr/bin/env python3
"""Sikula — LLM-powered development orchestration.

Usage (project-centric, run from project root):
  sikula init                        # create .sikula/config.yaml
  sikula init --guidelines --provider codex --model gpt-5.5
  sikula contract check task.md      # read-only implementation-contract preflight
  sikula task refine task.md --auto --output task.refined.md
  sikula contract prepare task.refined.md --output .sikula/contracts/task.contract.md
  sikula delivery check .sikula/delivery/my-plan/plan.yaml
  sikula delivery status .sikula/delivery/my-plan/plan.yaml
  sikula delivery finalize .sikula/delivery/my-plan/plan.yaml --dry-run
  sikula run task.md                 # auto-discovers .sikula/config.yaml
  sikula run --task-id <task-id>     # resume existing task
  sikula status
  sikula show <task-id>
  sikula cleanup <task-id> --force   # remove task worktree; keep state JSON
  sikula delete <task-id> --force    # remove task worktree and state JSON
  sikula review --branch feature/xyz --base-branch main --description-file pr.md

Usage (explicit config, run from anywhere):
  sikula --config /path/to/config.yaml run task.md \\
      --no-planner --no-tests --presync-clean \\
      --agent-model analyst=gpt-5.5 \\
      --agent-provider implementer=gemini --agent-model implementer=gemini-2.5-flash \\
      --agent-timeout implementer=2400

Auto-discovery: sikula walks up from the current directory to find .sikula/config.yaml.
--config overrides auto-discovery.

Isolation (default): each run creates a git worktree and a branch sikula/<task-stem>-<task-id>.
On success the changes are committed to that branch and the worktree is removed.
On failure the worktree is preserved for inspection and resume.
Use cleanup/delete to remove preserved worktrees when you no longer need them.
Use --no-isolate to run directly in the project directory (no branch, no auto-commit).

Phase flags (--flag / --no-flag): override run_* keys from the project config for this run only.
  --build / --no-build               run_build
  --presync / --no-presync           run_presync
  --presync-clean / --no-presync-clean       build.presync_clean (run clean before presync task)
  --planner / --no-planner                   run_planner
  --review / --no-review                     run_review
  --security-review / --no-security-review   run_security_review
  --test-writing / --no-test-writing         run_test_writing
  --tests / --no-tests                       run_tests
  --build-per-step / --no-build-per-step     run_build_per_step
  --checks / --no-checks                     run_checks

Contract gate flags (fresh task-file runs only):
  --require-contract-ready           abort before agents unless the task contract is ready
  --min-contract-score N             abort before agents unless readiness score is at least N

Per-agent LLM flags (repeatable, agent name uses _ or -):
  --agent-model analyst=gpt-5.5
  --agent-provider analyst=claude
  --agent-timeout implementer=2400
  CLI values layer on top of YAML agents.<name>.llm overrides.
  Valid run/review agents: analyst, planner, implementer, reviewer, security_reviewer, test_writer, fixer
  task refine --auto and contract prepare --auto accept task_preparer overrides.

--task-file accepts absolute paths or paths relative to CWD.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import re
import shlex
import shutil  # noqa: F401 - compatibility patch target for existing tests/imports
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

import yaml

from core import worktree as core_worktree
from core.diagnostics import diagnostic_identity_key, diagnostic_summary_lines
from core.version import sikula_version as _sikula_version
from sikula_cli import contract as cli_contract
from sikula_cli import cleanup as cli_cleanup
from sikula_cli import config as cli_config
from sikula_cli import delivery as cli_delivery
from sikula_cli import init as cli_init
from sikula_cli import review as cli_review
from sikula_cli import run as cli_run
from sikula_cli import status as cli_status
from sikula_cli import task as cli_task

_BASE = Path(__file__).parent
# When adding a new platform: add it here, in _build_tool() in core/orchestrator.py,
# in _build_tool_class() and _generate_config() below, in _SIGNATURES in tools/scanner.py,
# in tests/test_platform_onboarding.py, and in the test execution gate audit registry if
# the platform brings new test skip idioms.
_SUPPORTED_BUILD_TOOLS = {"cargo", "gradle-android", "gradle-jvm", "maven", "node", "xcodebuild", "python"}
_RECOVERED_DIAGNOSTIC_LIMIT = 10
_SIKULA_GITIGNORE_ENTRIES = ("state/", "worktrees/", "contract-reports/")


def _resolve_task_path(task_file: str, project_root: Path) -> Path | None:
    """Return the resolved Path for a task file, or None if not found.

    Resolution order:
      1. Absolute path — used as-is.
      2. Relative path — resolved against CWD.
    """
    p = Path(task_file)
    if p.is_absolute():
        return p if p.exists() else None
    cwd_path = Path.cwd() / p
    return cwd_path if cwd_path.exists() else None


_VALID_AGENTS = {
    "analyst",
    "planner",
    "implementer",
    "reviewer",
    "security_reviewer",
    "test_writer",
    "fixer",
}
_VALID_PREPARATION_AGENTS = {"task_preparer"}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading compatibility wrappers
# ---------------------------------------------------------------------------


def _find_project_root(start: Path | None = None) -> Path | None:
    return cli_config._find_project_root(start)


def _load_project_env(project_root: Path) -> None:
    cli_config._load_project_env(project_root)


def _sikula_worktree_base_for_path(path: Path) -> Path | None:
    return cli_config._sikula_worktree_base_for_path(path)


def _original_project_root_from_worktree(project_root: Path) -> Path | None:
    return cli_config._original_project_root_from_worktree(
        project_root,
        sikula_worktree_base_for_path=_sikula_worktree_base_for_path,
    )


def _resolve_config(config_arg: str | None) -> tuple[Path, Path | None]:
    return cli_config._resolve_config(
        config_arg,
        find_project_root=_find_project_root,
        original_project_root_from_worktree=_original_project_root_from_worktree,
    )


def _resolve_optional_config(config_arg: str | None) -> tuple[Path, Path | None] | None:
    return cli_config._resolve_optional_config(
        config_arg,
        resolve_config=_resolve_config,
        find_project_root=_find_project_root,
        original_project_root_from_worktree=_original_project_root_from_worktree,
    )


def _load_runtime_config(config_arg: str | None, *, required: bool = True) -> dict:
    return cli_config._load_runtime_config(
        config_arg,
        required=required,
        resolve_config=_resolve_config,
        resolve_optional_config=_resolve_optional_config,
        load_config=load_config,
        resolve_root_path=_resolve_root_path,
        load_project_env=_load_project_env,
    )


def _resolve_root_path(raw: str, discovered_root: Path | None, config_path: Path) -> Path:
    return cli_config._resolve_root_path(raw, discovered_root, config_path)


def _resolve_state_dir(cfg: dict) -> Path:
    return cli_config._resolve_state_dir(cfg)


def _resolve_task_description_dir(cfg: dict) -> Path:
    return cli_config._resolve_task_description_dir(cfg)


def _resolve_contract_dir(cfg: dict) -> Path:
    return cli_config._resolve_contract_dir(cfg)


def _resolve_contract_report_dir(cfg: dict) -> Path:
    return cli_config._resolve_contract_report_dir(cfg)


def _resolve_task_asset_dir(cfg: dict) -> Path:
    return cli_config._resolve_task_asset_dir(cfg)


def _resolve_project_path(cfg: dict, raw: str) -> Path:
    return cli_config._resolve_project_path(cfg, raw)


def load_config(path: Path) -> dict:
    return cli_config.load_config(path)


def _make_llm_config(base: dict, override: dict):
    from core.llm_client import LLMConfig

    return LLMConfig(
        provider=override.get("provider", base.get("provider", "codex")),
        model=override.get("model", base.get("model", "gpt-5.3-codex")),
        max_tokens=int(override.get("max_tokens", base.get("max_tokens", 16000))),
        temperature=float(override.get("temperature", base.get("temperature", 0.0))),
        agent_timeout=int(override.get("agent_timeout", base.get("agent_timeout", 1800))),
    )


def _parse_agent_llm_overrides(
    agent_models: list[str] | None,
    agent_providers: list[str] | None,
    agent_timeouts: list[str] | None,
    *,
    valid_agents: set[str] | None = None,
) -> dict[str, dict]:
    """Parse --agent-model / --agent-provider / --agent-timeout into per-agent override dicts."""
    result: dict[str, dict] = {}
    allowed_agents = valid_agents or _VALID_AGENTS

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


def _contract_score_threshold(value: str) -> int:
    try:
        score = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--min-contract-score must be an integer from 0 to 100") from exc
    if score < 0 or score > 100:
        raise argparse.ArgumentTypeError("--min-contract-score must be between 0 and 100")
    return score


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _build_tool_class(cfg: dict):
    """Return the BuildTool subclass for the configured project.

    Used only for env_files() — called before the orchestrator is created.
    When adding a new platform, extend this function, _SUPPORTED_BUILD_TOOLS,
    _generate_config(), _build_tool() in core/orchestrator.py, scanner detection, and
    tests/test_platform_onboarding.py. Also extend the test execution gate audit
    registry if the platform brings new test skip idioms.
    """
    platform = cfg.get("project", {}).get("build_tool", "gradle-android")
    if platform == "python":
        from tools.python_tool import PythonTool

        return PythonTool
    if platform == "cargo":
        from tools.cargo_tool import CargoTool

        return CargoTool
    if platform == "node":
        from tools.node_tool import NodeTool

        return NodeTool
    if platform == "xcodebuild":
        from tools.xcode_tool import XcodeTool

        return XcodeTool
    if platform == "gradle-jvm":
        from tools.gradle_jvm_tool import JvmGradleTool

        return JvmGradleTool
    if platform == "maven":
        from tools.maven_tool import MavenTool

        return MavenTool
    from tools.gradle_android_tool import AndroidGradleTool

    return AndroidGradleTool


# ---------------------------------------------------------------------------
# Git worktree helpers
# ---------------------------------------------------------------------------


def _worktree_error_message(branch: str, stderr: str) -> str:
    return core_worktree.worktree_error_message(branch, stderr)


_NO_REFERENCED_FILES_SENTINEL = "NO_REFERENCED_FILES"


_REFERENCED_FILES_PROMPT = """\
The task description below may reference files by name (images, mockups, PDFs, \
spreadsheets, specs, or any other attachment). For each file mentioned by name:
  1. Search: find . -name "<filename>"
  2. Read it if found.

Return the content of each file found, labelled with its path. \
If no files are referenced by name, or none can be found after searching, \
return exactly:
NO_REFERENCED_FILES

Task description:
{task_description}
"""


def _enrich_prompt_with_referenced_files(task_description: str, llm_client, project_root: Path) -> str:
    """Return contents of files referenced by name in the task description, or empty string."""
    from agents.base_agent import AGENT_SECURITY_PREFIX, read_only_agent_prompt

    prompt = read_only_agent_prompt(
        AGENT_SECURITY_PREFIX + _REFERENCED_FILES_PROMPT.format(task_description=task_description)
    )
    try:
        result = llm_client.run_readonly_agent(prompt, cwd=project_root).strip()
        if result == _NO_REFERENCED_FILES_SENTINEL:
            return ""
        return result
    except Exception as e:
        log.warning("Referenced file enrichment skipped: %s", e)
        return ""


def _branch_stem(task_file: str) -> str:
    stem = Path(task_file).stem
    stem = stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem)
    return stem.strip("-") or "task"


def _ensure_gitignore(git_root: Path) -> None:
    core_worktree.ensure_gitignore(git_root)


def _ensure_project_gitignore_entry(project_root: Path, entry: str) -> None:
    core_worktree.ensure_project_gitignore_entry(project_root, entry)


def _ensure_sikula_gitignore(sikula_dir: Path) -> None:
    gitignore = sikula_dir / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines = existing.splitlines()
    missing = [entry for entry in _SIKULA_GITIGNORE_ENTRIES if entry not in {line.strip() for line in lines}]
    if not missing:
        return
    content = existing
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f"{entry}\n" for entry in missing)
    gitignore.write_text(content)


def _ensure_provider_gitignore_entry(project_root: Path, provider: str | None) -> None:
    entries = {
        "claude": ".claude/",
        "gemini": ".gemini/",
    }
    entry = entries.get((provider or "").lower())
    if entry:
        _ensure_project_gitignore_entry(project_root, entry)


def _find_git_root(path: Path) -> Path | None:
    return core_worktree.find_git_root(path)


def _git_relative_path(git_root: Path, path: Path) -> str | None:
    return core_worktree.git_relative_path(git_root, path)


def _tracked_clean_file_status(git_root: Path, path: Path) -> tuple[bool, str]:
    return core_worktree.tracked_clean_file_status(git_root, path)


def _current_branch_name(git_root: Path) -> tuple[str | None, str | None]:
    return core_worktree.current_branch_name(git_root)


def _resolve_git_commit(git_root: Path, ref: str) -> tuple[str | None, str]:
    return core_worktree.resolve_git_commit(git_root, ref)


def _git_path_lines(git_root: Path, args: list[str]) -> tuple[list[str], str | None]:
    return core_worktree.git_path_lines(git_root, args)


def _git_excluded_path_prefixes(git_root: Path, exclude_paths: Sequence[Path] | None) -> set[str]:
    return core_worktree.git_excluded_path_prefixes(git_root, exclude_paths)


def _filter_git_paths(paths: list[str], excluded_prefixes: set[str]) -> list[str]:
    return core_worktree.filter_git_paths(paths, excluded_prefixes)


def _current_worktree_changes(
    git_root: Path,
    *,
    exclude_paths: Sequence[Path] | None = None,
) -> tuple[list[str], list[str], list[str], str | None]:
    return core_worktree.current_worktree_changes(git_root, exclude_paths=exclude_paths)


def _print_current_branch_clean_error(
    staged: list[str],
    unstaged: list[str],
    untracked: list[str],
    error: str | None,
) -> None:
    print("Error: --current-branch requires a clean current worktree before review fixes start.")
    if error:
        print(f"  {error}")
    if staged:
        print("Staged changes:")
        for path in staged:
            print(f"  {path}")
    if unstaged:
        print("Unstaged changes:")
        for path in unstaged:
            print(f"  {path}")
    if untracked:
        print("Untracked files:")
        for path in untracked:
            print(f"  {path}")
    print("Commit, stash, or remove these changes and rerun the command.")


_RUN_PHASE_CONTEXT_AGENTS = (
    ("run_planner", "planner"),
    ("run_review", "reviewer"),
    ("run_security_review", "security_reviewer"),
    ("run_test_writing", "test_writer"),
)


def _worktree_context_files(
    cfg: dict,
    *,
    include_config: bool,
    agent_names: Sequence[str],
) -> list[tuple[str, Path]]:
    """Return files that must be present unchanged in an isolated worktree."""
    result: list[tuple[str, Path]] = []

    raw_config_path = cfg.get("_config_path")
    if include_config and raw_config_path:
        result.append(("config", Path(raw_config_path).resolve()))

    project_root_raw = cfg.get("project", {}).get("root_path")
    if not project_root_raw:
        return result
    project_root = Path(project_root_raw).resolve()
    guidelines = cfg.get("guidelines", {}).get("context_files", [])
    if isinstance(guidelines, list):
        for raw in guidelines:
            path = Path(str(raw))
            abs_path = path if path.is_absolute() else project_root / path
            result.append(("guidelines", abs_path.resolve()))

    for agent_name in agent_names:
        raw = cfg.get(agent_name, {}).get("extra_rules")
        if not raw:
            continue
        path = Path(str(raw))
        abs_path = path if path.is_absolute() else project_root / path
        result.append((f"{agent_name}.extra_rules", abs_path.resolve()))

    deduped: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for kind, path in result:
        if path in seen:
            continue
        seen.add(path)
        deduped.append((kind, path))
    return deduped


def _run_context_agent_names(cfg: dict, overrides: dict | None = None) -> tuple[str, ...]:
    overrides = overrides or {}
    return tuple(
        agent_name
        for phase_name, agent_name in _RUN_PHASE_CONTEXT_AGENTS
        if _run_phase_flag(cfg, overrides, phase_name)
    )


def _isolation_context_files(cfg: dict, overrides: dict | None = None) -> list[tuple[str, Path]]:
    """Return files that must be present unchanged in isolated task worktrees."""
    return _worktree_context_files(
        cfg,
        include_config=True,
        agent_names=_run_context_agent_names(cfg, overrides),
    )


def _git_file_blob_status_at_ref(git_root: Path, ref: str, rel_path: str) -> tuple[bool, str]:
    return core_worktree.file_blob_status_at_ref(git_root, ref, rel_path)


def _require_worktree_context_files(
    cfg: dict,
    git_root: Path,
    *,
    start_ref: str,
    include_config: bool,
    agent_names: Sequence[str],
    command_label: str,
    worktree_label: str,
    show_no_isolate_hint: bool = False,
) -> None:
    """Fail fast when files read from a new worktree would be missing or stale."""
    problems: list[tuple[str, str, str]] = []
    for kind, path in _worktree_context_files(cfg, include_config=include_config, agent_names=agent_names):
        rel = _git_relative_path(git_root, path)
        if rel is None:
            continue

        ok, reason = _tracked_clean_file_status(git_root, path)
        if not ok:
            problems.append((kind, rel, reason))
            continue

        ok, reason = _git_file_blob_status_at_ref(git_root, start_ref, rel)
        if not ok:
            problems.append((kind, rel, reason))

    if not problems:
        return

    context_label = "config/prompt-context files" if include_config else "prompt-context files"
    print(f"Error: {command_label} requires Sikula {context_label} to be committed before creating a worktree.")
    print(
        f"The {worktree_label} starts from {start_ref}, so untracked, uncommitted, or absent context files are not visible there."
    )
    print("Problem files:")
    for kind, rel, reason in problems:
        print(f"  - {rel} ({kind}): {reason}")
    if include_config:
        add_paths = " ".join(rel for _, rel, _ in problems)
        print(f"Run: git add {add_paths} && git commit -m 'Add Sikula config'")
    else:
        print("Commit local context changes, then ensure the reviewed branch contains those commits before retrying.")
    if show_no_isolate_hint:
        print("Or use --no-isolate for a local experiment.")
    sys.exit(1)


def _require_committed_config_for_isolated_run(cfg: dict, git_root: Path, overrides: dict | None = None) -> None:
    """Fail fast when config/prompt context will not exist unchanged in a new worktree."""
    _require_worktree_context_files(
        cfg,
        git_root,
        start_ref="HEAD",
        include_config=True,
        agent_names=_run_context_agent_names(cfg, overrides),
        command_label="isolated run",
        worktree_label="task worktree",
        show_no_isolate_hint=True,
    )


def _require_worktree_context_for_review(
    cfg: dict,
    git_root: Path,
    start_ref: str,
    agent_names: Sequence[str],
) -> None:
    """Fail fast when review prompt context will not exist unchanged in the review worktree."""
    _require_worktree_context_files(
        cfg,
        git_root,
        start_ref=start_ref,
        include_config=False,
        agent_names=agent_names,
        command_label="review",
        worktree_label="review worktree",
    )


def _create_worktree(git_root: Path, worktree_base: Path, branch: str) -> tuple[bool, str]:
    worktree_base.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "worktree", "add", str(worktree_base), "-b", branch],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    return r.returncode == 0, r.stderr.strip()


def _default_worktree_commit_message(state: object) -> str:
    branch_short = (state.worktree_branch or state.task_id).removeprefix("sikula/")
    stem = (
        branch_short.removesuffix(f"-{state.task_id}") if branch_short.endswith(f"-{state.task_id}") else branch_short
    )
    return f"sikula: {stem}\n\nTask ID: {state.task_id}"


def _commit_worktree_changes(
    worktree_base: Path,
    state: object,
    commit_msg: str | None = None,
) -> tuple[bool, bool, str | None, str | None]:
    subprocess.run(["git", "add", "-A"], cwd=worktree_base, check=False)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
    )
    if not status.stdout.strip():
        return True, False, None, None

    message = commit_msg or _default_worktree_commit_message(state)
    r = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True,
        text=True,
        cwd=worktree_base,
        check=False,
    )
    if r.returncode != 0:
        error = _short_audit_line(r.stderr.strip() or r.stdout.strip() or "git commit failed")
        return False, False, None, error

    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
        check=False,
    )
    commit_sha = None
    if r.returncode == 0:
        commit_sha = r.stdout.strip()
        state.result_commit = commit_sha
    return True, True, commit_sha, None


def _finalize_worktree(
    worktree_base: Path,
    git_root: Path,
    state,
    commit_msg: str | None = None,
) -> tuple[bool, bool, str | None]:
    """Commit all changes and remove the worktree. Returns (success, committed, commit_sha)."""
    commit_ok, committed, commit_sha, error = _commit_worktree_changes(worktree_base, state, commit_msg=commit_msg)
    if not commit_ok:
        log.error("Failed to commit worktree changes: %s", error or "git commit failed")
        return False, False, None
    r = subprocess.run(
        ["git", "worktree", "remove", str(worktree_base)],
        cwd=git_root,
        check=False,
    )
    if r.returncode != 0:
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_base)],
            cwd=git_root,
            check=False,
        )
    return r.returncode == 0, committed, commit_sha


def _current_branch_delivery_terminal(state: object) -> bool:
    return state.review_delivery_status in {"delivered", "no_changes"}


def _current_branch_delivery_pending(state: object) -> bool:
    return (
        state.review_mode == "review_fix"
        and state.review_delivery_mode == "current_branch"
        and not _current_branch_delivery_terminal(state)
    )


def _current_branch_delivery_needs_finalization(state: object) -> bool:
    return bool(
        state.done
        and not state.failed
        and _current_branch_delivery_pending(state)
        and (state.worktree_base or state.worktree_path)
    )


def _current_branch_delivery_cleaned(state: object) -> bool:
    return bool(
        state.done
        and not state.failed
        and _current_branch_delivery_pending(state)
        and not state.worktree_base
        and not state.worktree_path
    )


def _print_current_branch_delivery_failure(worktree_base: Path, reason: str) -> None:
    print("Current-branch delivery failed:")
    print(f"  {reason}")
    print(f"Worktree preserved for inspection/resume: {worktree_base}")


def _mark_current_branch_delivery_failed(state: object, store: object, worktree_base: Path, reason: str) -> None:
    safe_reason = _short_audit_line(reason, limit=500)
    state.review_delivery_status = "failed"
    state.review_delivery_result = safe_reason
    state.record("sikula", "review_delivery_failed", safe_reason)
    store.save(state)
    _print_current_branch_delivery_failure(worktree_base, safe_reason)


def _remove_delivered_worktree(state: object, store: object, git_root: Path, worktree_base: Path) -> bool:
    if not worktree_base.exists():
        state.worktree_path = None
        state.worktree_base = None
        store.save(state)
        return True
    if not _remove_worktree(worktree_base, git_root, force=False) and not _remove_worktree(
        worktree_base, git_root, force=True
    ):
        return False
    state.worktree_path = None
    state.worktree_base = None
    store.save(state)
    return True


def _worktree_clean_error(
    git_root: Path,
    *,
    label: str,
    exclude_paths: Sequence[Path] | None = None,
) -> str | None:
    staged, unstaged, untracked, error = _current_worktree_changes(git_root, exclude_paths=exclude_paths)
    if error:
        return f"could not verify {label} cleanliness: {error}"
    problems = []
    if staged:
        problems.append(f"staged changes ({len(staged)})")
    if unstaged:
        problems.append(f"unstaged changes ({len(unstaged)})")
    if untracked:
        problems.append(f"untracked files ({len(untracked)})")
    if not problems:
        return None
    return f"{label} is not clean: " + ", ".join(problems)


def _current_worktree_clean_error(git_root: Path, *, exclude_paths: Sequence[Path] | None = None) -> str | None:
    return _worktree_clean_error(git_root, label="current worktree", exclude_paths=exclude_paths)


def _state_store_internal_paths(store: object) -> list[Path]:
    internal_paths = getattr(store, "internal_paths", None)
    if not callable(internal_paths):
        return []
    return [Path(path) for path in internal_paths()]


def _recorded_isolated_fix_reuse_error(worktree_base: Path, recorded_commit: str) -> str | None:
    current_head, head_error = _resolve_git_commit(worktree_base, "HEAD")
    if current_head is None:
        return f"could not resolve isolated worktree HEAD: {head_error}"
    if current_head != recorded_commit:
        return f"isolated worktree HEAD changed from recorded fix commit {recorded_commit[:12]} to {current_head[:12]}"
    return _worktree_clean_error(worktree_base, label="isolated worktree")


def _current_branch_delivery_safety_error(
    git_root: Path,
    target_branch: str,
    target_start_commit: str,
    delivered_commit: str | None = None,
    clean_exclude_paths: Sequence[Path] | None = None,
) -> tuple[str | None, str | None]:
    branch_name, branch_error = _current_branch_name(git_root)
    if branch_name != target_branch:
        if branch_error == "detached":
            reason = f"current HEAD is detached; expected branch '{target_branch}'"
        elif branch_name:
            reason = f"current branch is '{branch_name}', expected '{target_branch}'"
        else:
            reason = f"could not determine current branch; expected '{target_branch}'"
        return reason, None

    clean_error = _current_worktree_clean_error(git_root, exclude_paths=clean_exclude_paths)
    if clean_error:
        return clean_error, None

    current_head, head_error = _resolve_git_commit(git_root, "HEAD")
    if current_head is None:
        return f"could not resolve current HEAD: {head_error}", None
    if delivered_commit and current_head == delivered_commit:
        return None, current_head
    if current_head != target_start_commit:
        return f"current branch HEAD changed from {target_start_commit[:12]} to {current_head[:12]}", current_head
    return None, current_head


def _deliver_current_branch_review_fix(
    worktree_base: Path,
    git_root: Path,
    state: object,
    store: object,
    commit_msg: str,
) -> tuple[bool, bool, str | None]:
    target_branch = state.review_target_branch or state.worktree_branch
    target_start_commit = state.review_target_start_commit
    clean_exclude_paths = _state_store_internal_paths(store)
    if not target_branch:
        _mark_current_branch_delivery_failed(state, store, worktree_base, "missing current-branch target metadata")
        return False, False, None
    if not target_start_commit:
        _mark_current_branch_delivery_failed(
            state,
            store,
            worktree_base,
            "missing current-branch start commit metadata",
        )
        return False, False, None

    commit_sha = state.review_isolated_fix_commit
    committed = bool(commit_sha)
    if commit_sha:
        resolved_commit, commit_error = _resolve_git_commit(worktree_base, commit_sha)
        if resolved_commit is None:
            _mark_current_branch_delivery_failed(
                state,
                store,
                worktree_base,
                f"isolated fix commit '{commit_sha}' could not be resolved: {commit_error}",
            )
            return False, False, None
        commit_sha = resolved_commit
        reuse_error = _recorded_isolated_fix_reuse_error(worktree_base, commit_sha)
        if reuse_error:
            _mark_current_branch_delivery_failed(state, store, worktree_base, reuse_error)
            return False, committed, commit_sha
        state.result_commit = commit_sha
        state.review_delivery_status = "committed"
        state.review_delivery_result = f"isolated fix commit ready: {commit_sha}"
        store.save(state)
    else:
        commit_ok, committed, commit_sha, error = _commit_worktree_changes(worktree_base, state, commit_msg=commit_msg)
        if not commit_ok:
            _mark_current_branch_delivery_failed(
                state,
                store,
                worktree_base,
                f"isolated fix commit failed: {error or 'git commit failed'}",
            )
            return False, False, None
        if not committed:
            isolated_head, _head_error = _resolve_git_commit(worktree_base, "HEAD")
            if isolated_head and isolated_head != target_start_commit:
                commit_sha = isolated_head
                committed = True
            else:
                safety_error, _ = _current_branch_delivery_safety_error(
                    git_root,
                    target_branch,
                    target_start_commit,
                    clean_exclude_paths=clean_exclude_paths,
                )
                if safety_error:
                    _mark_current_branch_delivery_failed(state, store, worktree_base, safety_error)
                    return False, False, None
                state.review_delivery_status = "no_changes"
                state.review_delivery_result = "no changes to deliver"
                state.record("sikula", "review_delivery", state.review_delivery_result)
                store.save(state)
                if _remove_delivered_worktree(state, store, git_root, worktree_base):
                    return True, False, None
                _mark_current_branch_delivery_failed(
                    state,
                    store,
                    worktree_base,
                    "worktree cleanup failed after no-change result",
                )
                return False, False, None
        if not commit_sha:
            _mark_current_branch_delivery_failed(
                state,
                store,
                worktree_base,
                "could not determine isolated fix commit",
            )
            return False, False, None
        state.review_isolated_fix_commit = commit_sha
        state.result_commit = commit_sha
        state.review_delivery_status = "committed"
        state.review_delivery_result = f"isolated fix commit created: {commit_sha}"
        state.record("sikula", "review_delivery_committed", state.review_delivery_result)
        store.save(state)

    safety_error, current_head = _current_branch_delivery_safety_error(
        git_root,
        target_branch,
        target_start_commit,
        delivered_commit=commit_sha,
        clean_exclude_paths=clean_exclude_paths,
    )
    if safety_error:
        _mark_current_branch_delivery_failed(state, store, worktree_base, safety_error)
        return False, committed, commit_sha
    if current_head == commit_sha:
        state.review_delivery_status = "delivered"
        state.review_delivery_result = f"delivered {commit_sha} to {target_branch}"
        state.record("sikula", "review_delivery_delivered", state.review_delivery_result)
        store.save(state)
        if not _remove_delivered_worktree(state, store, git_root, worktree_base):
            state.record("sikula", "cleanup_failed", "current-branch review worktree cleanup failed")
            state.review_delivery_result = f"{state.review_delivery_result}; worktree cleanup failed"
            store.save(state)
            log.warning("Could not remove delivered worktree: %s", worktree_base)
        return True, committed, commit_sha

    merge = subprocess.run(
        ["git", "merge", "--ff-only", commit_sha],
        capture_output=True,
        text=True,
        cwd=git_root,
        check=False,
    )
    if merge.returncode != 0:
        error = _short_audit_line(merge.stderr.strip() or merge.stdout.strip() or "git merge --ff-only failed")
        _mark_current_branch_delivery_failed(state, store, worktree_base, f"fast-forward merge failed: {error}")
        return False, committed, commit_sha

    state.review_delivery_status = "delivered"
    state.review_delivery_result = f"delivered {commit_sha} to {target_branch}"
    state.record("sikula", "review_delivery_delivered", state.review_delivery_result)
    store.save(state)
    if not _remove_delivered_worktree(state, store, git_root, worktree_base):
        state.record("sikula", "cleanup_failed", "current-branch review worktree cleanup failed")
        state.review_delivery_result = f"{state.review_delivery_result}; worktree cleanup failed"
        store.save(state)
        log.warning("Could not remove delivered worktree: %s", worktree_base)
    return True, committed, commit_sha


def _worktree_dirty(worktree_base: Path) -> bool:
    return core_worktree.worktree_dirty(worktree_base)


def _remove_worktree(worktree_base: Path, git_root: Path, *, force: bool) -> bool:
    return core_worktree.remove_worktree(worktree_base, git_root, force=force)


def _path_is_within(path: Path, base: Path) -> bool:
    return core_worktree.path_is_within(path, base)


def _run_phase_flag(cfg: dict, overrides: dict, key: str) -> bool:
    cli_val = overrides.get(key)
    return bool(cfg.get(key, False) if cli_val is None else cli_val)


def _effective_agent_llm_cfg(cfg: dict, overrides: dict, name: str) -> dict:
    agents_cfg = cfg.get("agents", {})
    cli_agent_overrides: dict[str, dict] = overrides.get("agent_llms", {})
    yaml_agent = agents_cfg.get(name, {}).get("llm", {})
    cli_agent = cli_agent_overrides.get(name, {})
    return {**yaml_agent, **cli_agent}


def _run_config_snapshot(cfg: dict, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    sandbox = cfg.get("sandbox", {})
    build_snapshot = dict(cfg.get("build", {}))
    if "presync_clean" in overrides:
        build_snapshot["presync_clean"] = overrides["presync_clean"]
    max_review_iterations = int(sandbox.get("max_review_iterations", 3))
    base_llm_cfg = cfg.get("llm", {})

    def _agent_snapshot(name: str) -> dict:
        c = _make_llm_config(base_llm_cfg, _effective_agent_llm_cfg(cfg, overrides, name))
        snap: dict = {
            "provider": c.provider,
            "model": c.model,
            "agent_timeout": c.agent_timeout,
        }
        extra_rules = cfg.get(name, {}).get("extra_rules")
        if extra_rules:
            snap["extra_rules"] = extra_rules
        return snap

    return {
        "project": cfg.get("project", {}).get("name"),
        "run_presync": _run_phase_flag(cfg, overrides, "run_presync"),
        "run_planner": _run_phase_flag(cfg, overrides, "run_planner"),
        "run_review": _run_phase_flag(cfg, overrides, "run_review"),
        "run_security_review": _run_phase_flag(cfg, overrides, "run_security_review"),
        "run_test_writing": _run_phase_flag(cfg, overrides, "run_test_writing"),
        "run_build": _run_phase_flag(cfg, overrides, "run_build"),
        "run_tests": _run_phase_flag(cfg, overrides, "run_tests"),
        "run_build_per_step": _run_phase_flag(cfg, overrides, "run_build_per_step"),
        "run_checks": _run_phase_flag(cfg, overrides, "run_checks"),
        "max_iterations": int(sandbox.get("max_iterations", 10)),
        "max_review_iterations": max_review_iterations,
        "max_security_review_iterations": int(sandbox.get("max_security_review_iterations", max_review_iterations)),
        "progress": {
            "heartbeat_interval_seconds": _heartbeat_interval_seconds(cfg),
        },
        "sandbox": {
            "allowed_write_paths": sandbox.get("allowed_write_paths", []),
            "allowed_test_write_paths": sandbox.get("allowed_test_write_paths", []),
            "allowed_read_paths": sandbox.get("allowed_read_paths", ["."]),
        },
        "build": build_snapshot,
        "planner": cfg.get("planner", {}),
        "test_writer": cfg.get("test_writer", {}),
        "agents": {name: _agent_snapshot(name) for name in sorted(_VALID_AGENTS)},
    }


def build_orchestrator(cfg: dict, overrides: dict | None = None, state_store=None):
    from core.llm_client import create_llm_client
    from core.orchestrator import Orchestrator, OrchestratorConfig
    from core.state import JsonStateStore

    overrides = overrides or {}

    # presync_clean lives under build: in the config — patch the nested dict in-place so
    # the value reaches the build tool (reads it from project_config["build"]["presync_clean"]).
    if "presync_clean" in overrides:
        cfg.setdefault("build", {})["presync_clean"] = overrides["presync_clean"]

    config_snapshot = _run_config_snapshot(cfg, overrides)
    run_build = config_snapshot["run_build"]
    run_presync = config_snapshot["run_presync"]
    run_review = config_snapshot["run_review"]
    run_security_review = config_snapshot["run_security_review"]
    run_test_writing = config_snapshot["run_test_writing"]
    run_tests = config_snapshot["run_tests"]
    run_planner = config_snapshot["run_planner"]
    run_build_per_step = config_snapshot["run_build_per_step"]
    run_checks = config_snapshot["run_checks"]
    max_iterations = config_snapshot["max_iterations"]
    max_review_iterations = config_snapshot["max_review_iterations"]
    max_security_review_iterations = config_snapshot["max_security_review_iterations"]
    heartbeat_interval_seconds = config_snapshot["progress"]["heartbeat_interval_seconds"]

    project_root = Path(cfg["project"]["root_path"])
    sandbox = cfg.get("sandbox", {})
    base_llm_cfg = cfg.get("llm", {})
    if state_store is None:
        state_store = JsonStateStore(_resolve_state_dir(cfg))

    default_llm = create_llm_client(_make_llm_config(base_llm_cfg, {}))
    agent_llms = {
        name: create_llm_client(_make_llm_config(base_llm_cfg, _effective_agent_llm_cfg(cfg, overrides, name)))
        for name in _VALID_AGENTS
        if _effective_agent_llm_cfg(cfg, overrides, name)
    }

    return Orchestrator(
        config=OrchestratorConfig(
            project_root=project_root,
            max_iterations=max_iterations,
            max_review_iterations=max_review_iterations,
            max_security_review_iterations=max_security_review_iterations,
            allowed_write_paths=sandbox.get("allowed_write_paths", []),
            allowed_read_paths=sandbox.get("allowed_read_paths", ["."]),
            run_presync=run_presync,
            run_build=run_build,
            run_build_per_step=run_build_per_step,
            run_test_writing=run_test_writing,
            run_tests=run_tests,
            run_review=run_review,
            run_security_review=run_security_review,
            run_planner=run_planner,
            run_checks=run_checks,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            project_config=cfg,
        ),
        llm=default_llm,
        agent_llms=agent_llms,
        config_snapshot=config_snapshot,
        state_store=state_store,
    )


# ---------------------------------------------------------------------------
# Failed-task reset
# ---------------------------------------------------------------------------


def _reset_failed_state(task_id: str, cfg: dict, store) -> None:
    """Reset a failed task so it can be resumed.

    Clears the failed flag. If files_changed is empty (e.g. because the implementer
    ran but file-change detection produced a false negative), auto-populates it from
    the current git diff so the orchestrator skips the implement phase on resume.
    Uses state.worktree_path as the git cwd when the task ran in an isolated worktree.
    """
    state = store.load(task_id)

    if not state:
        print(f"Task {task_id} not found")
        sys.exit(1)

    if not state.failed:
        print(f"Task {task_id} is not in failed state — nothing to reset")
        return

    if _contract_gate_blocked_without_worktree(state):
        print(f"Task {task_id} failed before worktree creation because the contract readiness gate blocked delivery.")
        print("--reset-failed cannot safely resume it; prepare the task contract and start a fresh run.")
        print(f"Suggested next step: {_contract_gate_next_action(state)}")
        print(f"Inspect state: sikula show {task_id}")
        sys.exit(1)

    if state.review_mode == "review_report":
        print(f"Task {task_id} is a report-only review task and cannot be reset or resumed.")
        print("Re-run 'sikula review' to start a fresh review.")
        print(f"Inspect state: sikula show {task_id}")
        sys.exit(1)

    state.failed = False
    # Reset iteration counters so their loops are not immediately blocked on resume.
    # review_approved, security_approved, and tests_up_to_date are intentionally kept —
    # if review/security already passed or tests were written, those phases should not run again.
    state.review_iterations = 0
    state.security_review_iterations = 0
    state.build_iterations = 0
    state.build_loop_key = None
    state.build_loop_start_iteration = 0
    # Clear pending error blobs so the fixer doesn't see stale errors from before the
    # failure if the build re-fails on the first resumed iteration.
    state.errors.clear()
    state.test_errors.clear()
    state.check_errors.clear()

    if not state.files_changed:
        git_cwd = Path(state.worktree_path) if state.worktree_path else Path(cfg["project"]["root_path"])
        allowed = cfg.get("sandbox", {}).get("allowed_write_paths", [])

        modified = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "HEAD"],
            capture_output=True,
            text=True,
            cwd=git_cwd,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            cwd=git_cwd,
        )

        candidates: list[str] = []
        for line in (modified.stdout + "\n" + untracked.stdout).splitlines():
            path = line.strip()
            if not path:
                continue
            if not allowed or any(path.startswith(a) for a in allowed):
                candidates.append(path)

        if candidates:
            state.files_changed = candidates
            print(f"Auto-detected {len(candidates)} changed file(s) from git diff:")
            for p in candidates:
                print(f"  {p}")
        else:
            print("No changed files detected in allowed_write_paths — implement phase will run again on resume")

    state.record("orchestrator", "reset_failed", "manual reset via --reset-failed")
    store.save(state)
    print(f"Task {task_id} reset — resuming")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _create_task_preparation_agent(args: argparse.Namespace, cfg: dict):
    from agents.task_preparation_agent import TaskPreparationAgent
    from core.llm_client import create_llm_client

    overrides = {
        "agent_llms": _parse_agent_llm_overrides(
            getattr(args, "agent_model", None),
            getattr(args, "agent_provider", None),
            getattr(args, "agent_timeout", None),
            valid_agents=_VALID_PREPARATION_AGENTS,
        )
    }
    base_llm_cfg = cfg.get("llm", {}) if isinstance(cfg.get("llm"), dict) else {}
    llm = create_llm_client(_make_llm_config(base_llm_cfg, _effective_agent_llm_cfg(cfg, overrides, "task_preparer")))
    return TaskPreparationAgent(llm=llm, project_config=cfg)


def _run_task_refine_auto(
    *,
    args: argparse.Namespace,
    cfg: dict,
    project_root: Path,
    source_path: Path,
    task_text: str,
    task_name: str,
    output_path: Path,
    answers: dict[str, dict],
):
    from core.task_auto_refine import auto_refine_task_description

    asset_path_candidates = _task_refine_asset_path_candidates(task_text, source_path=source_path, cfg=cfg)
    audit_recorder, _audit_path = _make_auto_preparation_audit_recorder(
        generated_by="sikula.task_refine",
        source_path=source_path,
        source_text=task_text,
        output_path=output_path,
        cfg=cfg,
    )
    agent = _create_task_preparation_agent(args, cfg)
    return auto_refine_task_description(
        task_text,
        task_name=task_name,
        asset_path_candidates=asset_path_candidates,
        answers=answers,
        normalize_provider=lambda request: agent.normalize_task_description(
            request,
            project_root=project_root,
            audit_recorder=audit_recorder,
        ),
        answer_provider=lambda request: agent.propose_task_refinement_answers(
            request,
            project_root=project_root,
            audit_recorder=audit_recorder,
        ),
        audit_recorder=audit_recorder,
    )


def _task_refine_asset_path_candidates(task_text: str, *, source_path: Path, cfg: dict) -> list[dict[str, object]]:
    try:
        from core.task_assets import detect_asset_references, detect_undeclared_asset_paths

        asset_references = detect_asset_references(
            task_text,
            source_path=source_path,
            project_config=cfg,
            document_kind="task_description",
        )
        candidates = detect_undeclared_asset_paths(
            task_text,
            project_config=cfg,
            asset_references=asset_references,
            document_kind="task_description",
        )
    except (OSError, ValueError):
        return []

    safe_candidates: list[dict[str, object]] = []
    for candidate in candidates:
        path = str(candidate.get("path") or "").strip()
        if not path:
            continue
        line = candidate.get("line")
        safe_candidate: dict[str, object] = {"path": path}
        if isinstance(line, int) and line > 0:
            safe_candidate["line"] = line
        safe_candidates.append(safe_candidate)
    return safe_candidates


def _task_refine_context() -> cli_task.TaskRefineContext:
    return cli_task.TaskRefineContext(
        resolve_task_path=_resolve_task_path,
        resolve_answers_path=_resolve_answers_path,
        resolve_output_path=_resolve_output_path,
        default_refined_task_path=_default_refined_task_path,
        load_prepare_answers=_load_prepare_answers,
        collect_prepare_answers_interactive=_collect_prepare_answers_interactive,
        run_task_refine_auto=_run_task_refine_auto,
        write_prepare_answers_template=_write_prepare_answers_template,
        prepare_answers_path=_prepare_answers_path,
        print_existing_output_next_step_note=_print_existing_output_next_step_note,
        print_existing_output_hint=_print_existing_output_hint,
        print_open_question_details=_print_open_question_details,
        print_task_refinement_scope_note=_print_task_refinement_scope_note,
    )


def cmd_task_refine(args: argparse.Namespace, cfg: dict) -> None:
    return cli_task.cmd_task_refine(args, cfg, _task_refine_context())


def _task_context() -> cli_task.TaskContext:
    return cli_task.TaskContext(
        resolve_task_path=_resolve_task_path,
        resolve_task_asset_dir=_resolve_task_asset_dir,
    )


def cmd_task_attach(args: argparse.Namespace, cfg: dict) -> None:
    return cli_task.cmd_task_attach(args, cfg, _task_context())


def _print_task_refinement_scope_note() -> None:
    print(
        "Note: task refine only resolves product task-description questions; "
        "contract prepare may still ask delivery questions."
    )


def _print_open_question_details(user_questions: list[dict]) -> None:
    if not user_questions:
        return
    print("Open question details:")
    for index, question in enumerate(user_questions, start=1):
        question_id = str(question.get("id") or "").strip()
        question_text = str(question.get("question") or "").strip()
        if question_id and question_text:
            print(f"{index}. [{question_id}] {question_text}")
        elif question_text:
            print(f"{index}. {question_text}")


def _print_contract_prepare_project_context_required(result, task_file: str) -> None:
    blocker_messages = {
        "missing_project_context": "No project context was provided.",
        "missing_validation_commands": "No effective validation commands were found in the Sikula project config.",
    }
    print("Contract preparation needs project context before writing an implementation contract.")
    blockers = [
        blocker_messages.get(str(blocker), str(blocker))
        for blocker in getattr(result, "ready_to_run_blockers", [])
        if str(blocker) in blocker_messages
    ]
    if blockers:
        print("Project context blockers:")
        for blocker in blockers:
            print(f"- {blocker}")
    _print_open_question_details(getattr(result, "user_questions", []))
    print("Next step:")
    print("- Run from a Sikula-configured project, or pass --config /path/to/.sikula/config.yaml.")
    print("- Configure effective build, test, lint, or check validation commands, then rerun:")
    print(f"  sikula contract prepare {task_file}")


def _print_existing_output_next_step_note(output_path: Path) -> None:
    print("Output note:")
    print(f"- {output_path} already exists.")
    print("- Choose a new --output path, or remove/rename that file before rerunning.")


def _print_existing_output_hint(output_path: Path) -> None:
    print(f"Hint: {output_path} already exists.", file=sys.stderr)
    print(
        "Choose a new --output path, or remove/rename the existing file if you intend to replace it.", file=sys.stderr
    )


def _contract_context() -> cli_contract.ContractContext:
    return cli_contract.ContractContext(
        resolve_task_path=_resolve_task_path,
        project_config=_contract_cli_project_config,
        resolve_contract_report_dir=_resolve_contract_report_dir,
    )


def cmd_contract_check(args: argparse.Namespace, cfg: dict) -> None:
    return cli_contract.cmd_contract_check(args, cfg, _contract_context())


def _run_contract_prepare_auto(
    *,
    args: argparse.Namespace,
    cfg: dict,
    project_root: Path,
    source_path: Path,
    task_text: str,
    output_path: Path,
    project_context: dict | None,
    generated_answer_entries: list[dict],
    answers: dict[str, dict],
):
    from core.contract_auto_prepare import auto_prepare_implementation_contract

    agent = _create_task_preparation_agent(args, cfg)
    audit_recorder, _audit_path = _make_auto_preparation_audit_recorder(
        generated_by="sikula.contract_prepare",
        source_path=source_path,
        source_text=task_text,
        output_path=output_path,
        cfg=cfg,
    )
    return auto_prepare_implementation_contract(
        task_text,
        contract_name=str(source_path),
        project_context=project_context,
        project_config=_contract_cli_project_config(cfg),
        generated_answer_entries=generated_answer_entries,
        initial_answers=answers,
        answer_provider=lambda request: agent.propose_contract_answers(
            request,
            project_root=project_root,
            audit_recorder=audit_recorder,
        ),
        audit_recorder=audit_recorder,
    )


def _contract_prepare_context() -> cli_contract.ContractPrepareContext:
    return cli_contract.ContractPrepareContext(
        resolve_task_path=_resolve_task_path,
        project_config=_contract_cli_project_config,
        prepare_project_context_from_config=_prepare_project_context_from_config,
        resolve_output_path=_resolve_output_path,
        default_contract_path=_default_contract_path,
        resolve_contract_report_dir=_resolve_contract_report_dir,
        load_prepare_answers=_load_prepare_answers,
        collect_prepare_answers_interactive=_collect_prepare_answers_interactive,
        resolve_answers_path=_resolve_answers_path,
        existing_prepare_answers_path=_existing_prepare_answers_path,
        prepare_default_answers_has_current_filled_values=_prepare_default_answers_has_current_filled_values,
        run_contract_prepare_auto=_run_contract_prepare_auto,
        write_prepare_answers_template=_write_prepare_answers_template,
        prepare_answers_path=_prepare_answers_path,
        print_project_context_required=_print_contract_prepare_project_context_required,
        print_existing_output_next_step_note=_print_existing_output_next_step_note,
        print_existing_output_hint=_print_existing_output_hint,
        print_open_question_details=_print_open_question_details,
    )


def cmd_contract_prepare(args: argparse.Namespace, cfg: dict) -> None:
    return cli_contract.cmd_contract_prepare(args, cfg, _contract_prepare_context())


def _resolve_answers_path(value: str) -> Path:
    answers_path = Path(value)
    if not answers_path.is_absolute():
        answers_path = (Path.cwd() / answers_path).resolve()
    return answers_path


def _resolve_output_path(value: str) -> Path:
    output_path = Path(value)
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()
    return output_path


def _default_refined_task_path(task_path: Path, cfg: dict) -> Path:
    suffix = str(cfg.get("tasks", {}).get("refined_suffix") or ".refined.md")
    stem = _strip_known_task_suffixes(task_path.stem)
    if cfg.get("_config_path"):
        task_description_dir = _resolve_task_description_dir(cfg)
    else:
        task_description_dir = _infer_task_local_description_dir(task_path)
    return task_description_dir / f"{stem}{suffix}"


def _default_contract_path(task_path: Path, cfg: dict) -> Path:
    suffix = str(cfg.get("tasks", {}).get("contract_suffix") or ".contract.md")
    stem = _strip_known_task_suffixes(task_path.stem)
    return _resolve_contract_dir(cfg) / f"{stem}{suffix}"


def _strip_known_task_suffixes(stem: str) -> str:
    for suffix in (".refined", ".contract", ".v2", ".v3"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _read_interactive_contract_answer(prompt: str, current_answer: str) -> tuple[str, bool]:
    default_inserted = _prepare_interactive_line_editing(current_answer)
    prompt_text = prompt if default_inserted or not current_answer else f"{prompt} [{current_answer}]"
    try:
        return input(prompt_text + ": ").strip(), default_inserted
    finally:
        if default_inserted:
            _clear_interactive_line_editing()


def _prepare_interactive_line_editing(current_answer: str) -> bool:
    try:
        import readline
    except (ImportError, OSError):
        return False

    for binding in ("set editing-mode emacs", "bind -e"):
        try:
            readline.parse_and_bind(binding)
        except (AttributeError, OSError, ValueError):
            continue

    if not current_answer:
        return False

    try:
        readline.set_startup_hook(lambda: readline.insert_text(current_answer))
    except (AttributeError, OSError, ValueError):
        return False
    return True


def _clear_interactive_line_editing() -> None:
    try:
        import readline

        readline.set_startup_hook()
    except (ImportError, AttributeError, OSError, ValueError):
        pass


def _should_store_interactive_answer(response: str, current_answer: str, default_inserted: bool) -> bool:
    return bool(response) or not current_answer or default_inserted


def _collect_prepare_answers_interactive(
    *,
    generated_by: str,
    label: str,
    source_path: Path,
    source_text: str,
    project_root: Path,
    questions: list[dict],
    cfg: dict,
    answers_path: Path | None = None,
) -> dict[str, dict]:
    if not sys.stdin.isatty():
        raise ValueError(f"interactive {label} requires an interactive terminal on stdin")

    explicit_answers_path = answers_path is not None
    answers_path = answers_path or _prepare_answers_path(source_path, cfg, generated_by=generated_by)
    artifact_base = _prepare_answers_artifact_base(answers_path.parent, cfg)
    answers_data = _prepare_answers_template(
        generated_by=generated_by,
        source_path=source_path,
        source_text=source_text,
        project_root=artifact_base,
        questions=questions,
    )
    if answers_path.exists():
        existing = _load_prepare_answers_data(answers_path)
        if explicit_answers_path:
            _validate_prepare_answers_for_source(existing, source_path=source_path, source_text=source_text)
        answers_data = _merge_prepare_answers(existing, answers_data, archive_stale=not explicit_answers_path)

    answers = answers_data.setdefault("answers", {})
    print(f"Interactive {label} answers: {answers_path}")
    if not questions:
        print("No follow-up questions found; answers file is unchanged.")
        answers_path.parent.mkdir(parents=True, exist_ok=True)
        answers_path.write_text(yaml.safe_dump(answers_data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return {}

    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str) or not question["id"]:
            raise ValueError(f"invalid question entry for {label}")
        question_id = question["id"]
        answer_entry = answers.setdefault(question_id, {"answer": "", "notes": ""})
        if not isinstance(answer_entry, dict):
            raise ValueError(f"invalid answer entry for {question_id}")
        current_answer = str(answer_entry.get("answer") or "").strip()
        print("")
        print(f"[{question_id}] {question.get('question', '')}")
        why = str(question.get("why_it_matters") or "").strip()
        if why:
            print(f"Why it matters: {why}")
        prompt = f"Answer [{question_id}]"
        response, default_inserted = _read_interactive_contract_answer(prompt, current_answer)
        if _should_store_interactive_answer(response, current_answer, default_inserted):
            answer_entry["answer"] = response
        answer_entry.setdefault("notes", "")

    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(yaml.safe_dump(answers_data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print("")
    print(f"{label.capitalize()} answers written: {answers_path}")
    return {key: value for key, value in answers.items() if isinstance(key, str) and isinstance(value, dict)}


def _write_prepare_answers_template(
    *,
    generated_by: str,
    source_path: Path,
    source_text: str,
    project_root: Path,
    questions: list[dict],
    cfg: dict,
    answers: dict[str, dict] | None = None,
    answers_path: Path | None = None,
) -> Path:
    explicit_answers_path = answers_path is not None
    answers_path = answers_path or _prepare_answers_path(source_path, cfg, generated_by=generated_by)
    artifact_base = _prepare_answers_artifact_base(answers_path.parent, cfg)
    answers_data = _prepare_answers_template(
        generated_by=generated_by,
        source_path=source_path,
        source_text=source_text,
        project_root=artifact_base,
        questions=questions,
    )
    if answers_path.exists():
        existing = _load_prepare_answers_data(answers_path)
        if explicit_answers_path:
            _validate_prepare_answers_for_source(existing, source_path=source_path, source_text=source_text)
        answers_data = _merge_prepare_answers(existing, answers_data, archive_stale=not explicit_answers_path)
    if answers:
        _prefill_prepare_answers(answers_data, answers)
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(yaml.safe_dump(answers_data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return answers_path


def _make_auto_preparation_audit_recorder(
    *,
    generated_by: str,
    source_path: Path,
    source_text: str,
    output_path: Path,
    cfg: dict,
):
    audit_path = _prepare_auto_preparation_audit_path(source_path, cfg, generated_by=generated_by)
    artifact_base = _prepare_answers_artifact_base(audit_path.parent, cfg)
    task_metadata = {
        "path": _contract_preflight_path(source_path, artifact_base),
        "sha256": _text_sha256(source_text),
    }
    output_metadata = {
        "path": _contract_preflight_path(output_path, artifact_base),
    }

    def record_auto_preparation_audit(record: dict) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "generated_by": generated_by,
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "task": task_metadata,
            "output": output_metadata,
            "record": record,
        }
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            handle.write("\n")

    return record_auto_preparation_audit, audit_path


def _prepare_auto_preparation_audit_path(source_path: Path, cfg: dict, *, generated_by: str) -> Path:
    answers_path = _prepare_answers_path(source_path, cfg, generated_by=generated_by)
    suffix = ".answers.yaml"
    if answers_path.name.endswith(suffix):
        return answers_path.with_name(f"{answers_path.name[: -len(suffix)]}.auto-llm.jsonl")
    return answers_path.with_suffix(".auto-llm.jsonl")


def _prefill_prepare_answers(answers_data: dict, answers: dict[str, dict]) -> None:
    template_answers = answers_data.setdefault("answers", {})
    if not isinstance(template_answers, dict):
        return
    for question_id, answer in answers.items():
        if not isinstance(question_id, str) or not isinstance(answer, dict):
            continue
        entry = template_answers.setdefault(question_id, {"answer": "", "notes": ""})
        if not isinstance(entry, dict):
            entry = {"answer": "", "notes": ""}
            template_answers[question_id] = entry
        existing_answer = str(entry.get("answer") or "").strip()
        existing_notes = str(entry.get("notes") or "").strip()
        if existing_answer or existing_notes:
            continue
        entry["answer"] = str(answer.get("answer") or "")
        entry["notes"] = str(answer.get("notes") or "")


def _prepare_answers_path(source_path: Path, cfg: dict, *, generated_by: str) -> Path:
    phase = generated_by.removeprefix("sikula.").replace("_", "-")
    stem = _strip_known_task_suffixes(source_path.stem)
    report_dir = _prepare_answers_report_dir(source_path, cfg)
    artifact_base = _prepare_answers_artifact_base(report_dir, cfg)
    base = report_dir / f"{stem}.{phase}.answers.yaml"
    if _prepare_answers_path_available_for_task(base, source_path, artifact_base, generated_by):
        return base

    hashed_stem = f"{stem}-{sha256(str(source_path.resolve()).encode('utf-8')).hexdigest()[:8]}"
    hashed = report_dir / f"{hashed_stem}.{phase}.answers.yaml"
    if not _prepare_answers_path_available_for_task(hashed, source_path, artifact_base, generated_by):
        raise FileExistsError(f"answers path already exists for a different task: {hashed}")
    return hashed


def _existing_prepare_answers_path(source_path: Path, cfg: dict, *, generated_by: str) -> Path | None:
    try:
        answers_path = _prepare_answers_path(source_path, cfg, generated_by=generated_by)
    except FileExistsError:
        return None
    return answers_path if answers_path.exists() else None


def _prepare_default_answers_has_current_filled_values(
    *,
    answers_path: Path,
    generated_by: str,
    source_path: Path,
    source_text: str,
    project_root: Path,
    questions: list[dict],
    cfg: dict,
) -> bool:
    existing = _load_prepare_answers_data(answers_path)
    if _prepare_answers_task_sha(existing) != _text_sha256(source_text):
        _write_prepare_answers_template(
            generated_by=generated_by,
            source_path=source_path,
            source_text=source_text,
            project_root=project_root,
            questions=questions,
            cfg=cfg,
        )
        return False
    _validate_prepare_answers_for_source(existing, source_path=source_path, source_text=source_text)
    return bool(_filled_prepare_answers(existing.get("answers")))


def _prepare_answers_report_dir(source_path: Path, cfg: dict) -> Path:
    if cfg.get("_config_path"):
        return _resolve_contract_report_dir(cfg)
    return _infer_task_local_contract_report_dir(source_path)


def _prepare_answers_artifact_base(report_dir: Path, cfg: dict) -> Path:
    project_root = cfg.get("project", {}).get("root_path")
    if project_root:
        return Path(project_root).resolve()
    if report_dir.name == "contract-reports" and report_dir.parent.name == ".sikula":
        return report_dir.parent.parent.resolve()
    return Path.cwd().resolve()


def _prepare_answers_path_available_for_task(
    path: Path,
    source_path: Path,
    artifact_base: Path,
    generated_by: str,
) -> bool:
    if not path.exists():
        return True
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("schema_version") != 1 or data.get("generated_by") != generated_by:
        return False
    return _prepare_answers_task_path_matches(data.get("task"), source_path, artifact_base)


def _prepare_answers_task_path_matches(task: object, source_path: Path, artifact_base: Path) -> bool:
    if not isinstance(task, dict):
        return False
    value = task.get("path")
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        if not path.is_absolute():
            path = artifact_base / path
        return path.resolve() == source_path.resolve()
    except OSError:
        return False


def _infer_task_local_contract_report_dir(source_path: Path) -> Path:
    resolved = source_path.resolve()
    for parent in resolved.parents:
        if parent.name == "tasks" and parent.parent.name == ".sikula":
            return parent.parent / "contract-reports"
        if parent.name == ".sikula":
            return parent / "contract-reports"
    return Path.cwd().resolve() / ".sikula" / "contract-reports"


def _infer_task_local_description_dir(source_path: Path) -> Path:
    resolved = source_path.resolve()
    for parent in resolved.parents:
        if parent.name == "tasks" and parent.parent.name == ".sikula":
            return parent
        if parent.name == ".sikula":
            return parent / "tasks"
    return Path.cwd().resolve() / ".sikula" / "tasks"


def _prepare_answers_template(
    *,
    generated_by: str,
    source_path: Path,
    source_text: str,
    project_root: Path,
    questions: list[dict],
) -> dict:
    return {
        "schema_version": 1,
        "generated_by": generated_by,
        "task": {
            "path": _contract_preflight_path(source_path, project_root),
            "sha256": _text_sha256(source_text),
        },
        "questions": questions,
        "answers": {
            question["id"]: {
                "answer": "",
                "notes": "",
            }
            for question in questions
            if isinstance(question, dict) and isinstance(question.get("id"), str)
        },
    }


def _merge_prepare_answers(existing: dict, next_data: dict, *, archive_stale: bool = False) -> dict:
    previous_answers = _prepare_previous_answers(existing)
    if previous_answers:
        next_data["previous_answers"] = previous_answers

    existing_answers = existing.get("answers")
    next_answers = next_data.get("answers")
    if not isinstance(existing_answers, dict) or not isinstance(next_answers, dict):
        return next_data
    existing_sha = _prepare_answers_task_sha(existing)
    next_sha = _prepare_answers_task_sha(next_data)
    if existing_sha != next_sha:
        if archive_stale:
            archived = _archive_prepare_answers(existing)
            if archived:
                next_data.setdefault("previous_answers", []).append(archived)
        return next_data
    for question_id, template in list(next_answers.items()):
        answer = existing_answers.get(question_id)
        if isinstance(answer, dict):
            next_answers[question_id] = {
                "answer": answer.get("answer", ""),
                "notes": answer.get("notes", ""),
            }
        else:
            next_answers[question_id] = template
    for question_id, answer in existing_answers.items():
        if question_id in next_answers or not isinstance(question_id, str) or not isinstance(answer, dict):
            continue
        normalized = {
            "answer": answer.get("answer", ""),
            "notes": answer.get("notes", ""),
        }
        if str(normalized["answer"] or "").strip() or str(normalized["notes"] or "").strip():
            next_answers[question_id] = normalized
    return next_data


def _prepare_answers_task_sha(data: dict) -> str | None:
    task = data.get("task")
    if not isinstance(task, dict):
        return None
    value = task.get("sha256")
    return value if isinstance(value, str) and value else None


def _prepare_previous_answers(data: dict) -> list[dict]:
    previous = data.get("previous_answers")
    if not isinstance(previous, list):
        return []
    return [entry for entry in previous if isinstance(entry, dict)]


def _archive_prepare_answers(data: dict) -> dict | None:
    filled = _filled_prepare_answers(data.get("answers"))
    if not filled:
        return None
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    return {
        "archived_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "task": {
            "path": task.get("path"),
            "sha256": task.get("sha256"),
        },
        "questions": data.get("questions") if isinstance(data.get("questions"), list) else [],
        "answers": filled,
    }


def _filled_prepare_answers(value) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    filled: dict[str, dict] = {}
    for question_id, answer in value.items():
        if not isinstance(question_id, str) or not isinstance(answer, dict):
            continue
        answer_text = answer.get("answer", "")
        notes = answer.get("notes", "")
        if answer_text or notes:
            filled[question_id] = {
                "answer": answer_text,
                "notes": notes,
            }
    return filled


def _load_prepare_answers(path: Path, *, source_path: Path, source_text: str) -> dict[str, dict]:
    data = _load_prepare_answers_data(path)
    _validate_prepare_answers_for_source(data, source_path=source_path, source_text=source_text)
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError(f"answers file is missing the answers mapping: {path}")
    return {key: value for key, value in answers.items() if isinstance(key, str) and isinstance(value, dict)}


def _load_prepare_answers_data(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid answers YAML: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"answers file must contain a mapping: {path}")
    generated_by = data.get("generated_by")
    if generated_by not in {"sikula.task_refine", "sikula.contract_prepare", "sikula.contract_check"}:
        raise ValueError(f"answers file was not generated by Sikula prepare/check tooling: {path}")
    if data.get("schema_version") != 1:
        raise ValueError(f"Unsupported answers schema version: {data.get('schema_version')!r}")
    return data


def _validate_prepare_answers_for_source(data: dict, *, source_path: Path, source_text: str) -> None:
    task = data.get("task")
    if not isinstance(task, dict):
        raise ValueError("answers file is missing task metadata")
    expected_sha = _text_sha256(source_text)
    actual_sha = task.get("sha256")
    if actual_sha != expected_sha:
        raise ValueError(
            f"answers were generated for a different task revision ({actual_sha or 'missing hash'} != {expected_sha})"
        )


def _text_sha256(text: str) -> str:
    return "sha256:" + sha256(text.strip().encode("utf-8")).hexdigest()


def _is_placeholder_validation_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(token.strip().strip("\"'`<>[]{}():,;").upper() == "TODO" for token in tokens)


def _contract_prepare_validation_commands(configured: list[dict[str, str]]) -> list[str]:
    commands: list[str] = []
    for entry in configured:
        command = str(entry.get("command") or "").strip()
        if not command or entry.get("phase") == "check_autofix":
            continue
        if _is_placeholder_validation_command(command):
            continue
        commands.append(command)
    return commands


def _prepare_project_context_from_config(cfg: dict) -> dict | None:
    from core.state import TaskState
    from core.validation_coverage import configured_validation_commands

    effective = _contract_cli_project_config(cfg)
    if effective is None:
        return None
    project = effective.get("project", {}) if isinstance(effective.get("project"), dict) else {}
    build = effective.get("build", {}) if isinstance(effective.get("build"), dict) else {}
    build_tool = str(project.get("build_tool") or "").strip()
    configured: list[dict[str, str]] = []
    if build_tool in _SUPPORTED_BUILD_TOOLS:
        state = TaskState(task_id="contract_prepare", task_description="")
        configured = configured_validation_commands(effective, state)
    stack_parts = [
        str(project.get("language") or "").strip(),
        str(project.get("platform") or "").strip(),
        build_tool,
        str(project.get("ui") or "").strip(),
    ]
    stack = " / ".join(part for part in stack_parts if part and part != "TODO")
    return {
        "stack": stack or None,
        "package_manager": build.get("package_manager"),
        "validation_commands": _contract_prepare_validation_commands(configured),
    }


def _contract_preflight_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _contract_preflight_snapshot(result, task_path: Path, project_root: Path) -> dict:
    validation = result.validation or {}
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "path": _contract_preflight_path(task_path, project_root),
            "format": result.source.get("format"),
            "sha256": result.source.get("sha256"),
        },
        "readiness_score": result.readiness_score,
        "status": result.status,
        "ready_for_autonomous_delivery": result.ready_for_autonomous_delivery,
        "sections_detected": dict(result.sections_detected),
        "scores": dict(result.scores),
        "gaps": [
            {
                "id": gap.id,
                "severity": gap.severity,
                "category": gap.category,
                "message": gap.message,
            }
            for gap in result.gaps
        ],
        "clarifying_question_ids": [question.id for question in result.clarifying_questions],
        "suggested_sections": list(result.suggested_sections),
        "validation": {
            "task_command_count": len(validation.get("task_commands") or []),
            "configured_command_count": len(validation.get("configured_commands") or []),
            "covered_command_count": len(validation.get("covered_commands") or []),
            "coverage_gap_count": len(validation.get("coverage_gaps") or []),
        },
    }


def _contract_preflight_asset_records(result) -> list[dict]:
    return [dict(reference) for reference in getattr(result, "asset_references", []) if isinstance(reference, dict)]


def _record_contract_asset_drift(state, asset_records: list[dict], store, *, phase: str) -> None:
    from core.task_asset_drift import detect_declared_asset_hash_drift

    drift_records = detect_declared_asset_hash_drift(asset_records, phase=phase)
    if not drift_records:
        return
    before_count = len(getattr(state, "implementation_asset_drift_records", []))
    state.record_implementation_asset_drift(drift_records)
    if len(getattr(state, "implementation_asset_drift_records", [])) != before_count:
        store.save(state)


def _record_snapshot_asset_drift(state, project_root: Path, store, *, phase: str) -> None:
    from core.task_asset_drift import detect_snapshot_asset_drift

    asset_records = getattr(state, "implementation_asset_records", [])
    if not asset_records:
        return
    drift_records = detect_snapshot_asset_drift(asset_records, project_root=project_root, phase=phase)
    if not drift_records:
        return
    before_count = len(getattr(state, "implementation_asset_drift_records", []))
    state.record_implementation_asset_drift(drift_records)
    if len(getattr(state, "implementation_asset_drift_records", [])) != before_count:
        store.save(state)


def _record_asset_target_audit(state, project_root: Path, store, *, phase: str) -> None:
    from core.task_asset_targets import audit_delivery_asset_targets

    asset_records = getattr(state, "implementation_asset_records", [])
    if not asset_records:
        return
    target_records = audit_delivery_asset_targets(
        asset_records,
        files_changed=getattr(state, "files_changed", []),
        project_root=project_root,
        phase=phase,
    )
    if not target_records:
        return
    before_count = len(getattr(state, "implementation_asset_target_records", []))
    state.record_implementation_asset_targets(target_records)
    if len(getattr(state, "implementation_asset_target_records", [])) != before_count:
        store.save(state)


def _contract_preflight_error_snapshot(task_path: Path, project_root: Path, error: Exception) -> dict:
    source_format = "text" if task_path.suffix.lower() == ".txt" else "markdown"
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "path": _contract_preflight_path(task_path, project_root),
            "format": source_format,
            "sha256": None,
        },
        "status": "error",
        "ready_for_autonomous_delivery": False,
        "error": _short_audit_line(str(error), limit=500),
    }


def _build_contract_preflight_snapshot(task_path: Path, cfg: dict, project_root: Path) -> dict:
    snapshot, _asset_records = _build_contract_preflight_snapshot_and_assets(task_path, cfg, project_root)
    return snapshot


def _build_contract_preflight_snapshot_and_assets(
    task_path: Path, cfg: dict, project_root: Path
) -> tuple[dict, list[dict]]:
    from core.contract_check import check_contract_file

    try:
        result = check_contract_file(
            task_path,
            project_config=cfg or None,
            document_kind="implementation_contract",
        )
        return _contract_preflight_snapshot(result, task_path, project_root), _contract_preflight_asset_records(result)
    except Exception as exc:
        log.warning("Implementation contract preflight failed: %s", exc)
        return _contract_preflight_error_snapshot(task_path, project_root, exc), []


def _contract_preflight_config(cfg: dict, overrides: dict) -> dict:
    from core.validation_coverage import INTERNAL_PIPELINE_CONFIG_KEY

    effective = dict(cfg)
    effective[INTERNAL_PIPELINE_CONFIG_KEY] = {
        "run_build": _run_phase_flag(cfg, overrides, "run_build"),
        "run_tests": _run_phase_flag(cfg, overrides, "run_tests"),
        "run_checks": _run_phase_flag(cfg, overrides, "run_checks"),
    }
    return effective


def _contract_cli_project_config(cfg: dict) -> dict | None:
    if not cfg:
        return None
    return _contract_preflight_config(cfg, {})


def _contract_preflight_record_result(snapshot: dict) -> str:
    status = str(snapshot.get("status") or "unknown").upper()
    score = snapshot.get("readiness_score")
    if isinstance(score, int):
        return f"{status} {score}/100"
    return status


def _contract_gate_task_path(state) -> str | None:
    snapshot = getattr(state, "implementation_contract", None)
    if not isinstance(snapshot, dict):
        return None
    source = snapshot.get("source")
    if not isinstance(source, dict):
        return None
    path = source.get("path")
    return path if isinstance(path, str) and path.strip() else None


def _contract_gate_blocked_without_worktree(state) -> bool:
    return bool(
        getattr(state, "contract_gate_blocked", False)
        and not getattr(state, "worktree_path", None)
        and not getattr(state, "worktree_branch", None)
    )


def _contract_gate_next_action(state) -> str:
    path = _contract_gate_task_path(state)
    if path:
        return f"sikula contract check {path} --write-report"
    return f"sikula show {state.task_id}"


def _print_contract_preflight_summary(snapshot: dict) -> None:
    status = str(snapshot.get("status") or "unknown").upper()
    score = snapshot.get("readiness_score")
    if status == "ERROR":
        print(f"Implementation contract: unavailable (warning-only): {snapshot.get('error', 'unknown error')}")
        return

    gaps = snapshot.get("gaps") if isinstance(snapshot.get("gaps"), list) else []
    blocking = sum(1 for gap in gaps if isinstance(gap, dict) and gap.get("severity") == "blocking")
    questions = snapshot.get("clarifying_question_ids")
    question_count = len(questions) if isinstance(questions, list) else 0
    score_text = f"{score}/100" if isinstance(score, int) else "unknown score"
    details: list[str] = []
    if gaps:
        details.append(f"{len(gaps)} gap(s)")
    if blocking:
        details.append(f"{blocking} blocking")
    if question_count:
        details.append(f"{question_count} follow-up question(s)")
    suffix = f" ({', '.join(details)})" if details else ""
    print(f"Implementation contract: {status} {score_text}{suffix}")


def _contract_readiness_gate_failures(
    snapshot: dict,
    *,
    require_ready: bool,
    min_score: int | None,
) -> list[str]:
    if not require_ready and min_score is None:
        return []

    failures: list[str] = []
    status = str(snapshot.get("status") or "unknown").upper()
    score = snapshot.get("readiness_score")

    if require_ready and not snapshot.get("ready_for_autonomous_delivery"):
        if status == "ERROR":
            failures.append("contract check is unavailable; strict readiness requires a valid contract check")
        elif isinstance(score, int):
            failures.append(f"contract is not ready ({status} {score}/100)")
        else:
            failures.append(f"contract is not ready ({status})")

    if min_score is not None:
        if not isinstance(score, int):
            failures.append(f"contract score is unavailable; minimum required score is {min_score}/100")
        elif score < min_score:
            failures.append(f"contract score is {score}/100; minimum required score is {min_score}/100")

    return failures


def _print_contract_readiness_gate_failure(snapshot: dict, failures: list[str], task_id: str) -> None:
    print("Implementation contract gate failed:")
    for failure in failures:
        print(f"- {failure}")

    source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    path = source.get("path")
    print("Next steps:")
    if path:
        print(f"- Refine the task directly, then rerun: sikula run {path}")
        print(f"- Or generate answers: sikula contract check {path} --write-report")
        print(
            f"- Then prepare a contract: sikula contract prepare {path} "
            "--answers .sikula/contract-reports/<task>.answers.yaml --output .sikula/contracts/<task>.contract.md"
        )
    else:
        print("- Refine the task directly, then rerun sikula run.")
        print(
            "- Or generate answers with sikula contract check --write-report and apply them with sikula contract prepare."
        )
    print(f"Task state saved: sikula show {task_id}")


def _fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _short_audit_line(value: str | None, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _validation_status(value: str | None) -> str:
    return value or "skipped"


def _review_status(approved: bool, records: list[dict]) -> str:
    if approved:
        return "approved"
    if records:
        return "issues found"
    return "skipped"


def _validation_failure_summary(records: list[dict]) -> str | None:
    counts: dict[str, int] = {}
    for record in records:
        if record.get("status") != "failed":
            continue
        label = _validation_failure_label(record)
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return None
    parts = [f"{label} x{count}" for label, count in sorted(counts.items())]
    return ", ".join(parts)


def _current_failed_validation_records(state) -> list[dict]:
    active_phases = {
        phase
        for phase, status in (
            ("build", getattr(state, "build_status", None)),
            ("test", getattr(state, "test_status", None)),
            ("check", getattr(state, "check_status", None)),
        )
        if status == "failed"
    }
    if not active_phases:
        return []

    latest_by_key: dict[tuple[str, str], tuple[int, dict]] = {}
    for index, record in enumerate(getattr(state, "validation_cycle_records", [])):
        phase = str(record.get("phase") or "")
        if phase not in active_phases:
            continue
        status = record.get("status")
        if status not in {"failed", "success", "skipped"}:
            continue
        check_name = str(record.get("check_name") or "") if phase == "check" else ""
        latest_by_key[(phase, check_name)] = (index, record)

    return [
        record
        for _, record in sorted(latest_by_key.values(), key=lambda item: item[0])
        if record.get("status") == "failed"
    ]


def _validation_failure_diagnostics(records: list[dict], limit: int = _RECOVERED_DIAGNOSTIC_LIMIT) -> list[str]:
    failed_records = [record for record in records if record.get("status") == "failed"]
    label_counts: dict[str, int] = {}
    for record in failed_records:
        label = _validation_failure_label(record)
        label_counts[label] = label_counts.get(label, 0) + 1

    groups: list[tuple[str, list[str]]] = []
    label_seen: dict[str, int] = {}
    for record in failed_records:
        label = _validation_failure_label(record)
        label_seen[label] = label_seen.get(label, 0) + 1
        group_label = f"{label} #{label_seen[label]}" if label_counts[label] > 1 else label
        lines = _validation_record_diagnostic_lines(record)
        if lines:
            groups.append((group_label, lines))

    diagnostics: list[str] = []
    seen: set[str] = set()
    for label, lines in groups:
        if not lines:
            continue
        _append_validation_diagnostic(diagnostics, seen, label, lines[0])
        if len(diagnostics) >= limit:
            return diagnostics

    remaining: list[tuple[int, int, int, str, str]] = []
    for group_index, (label, lines) in enumerate(groups):
        for line_index, line in enumerate(lines[1:], start=1):
            remaining.append((_audit_diagnostic_line_priority(line), line_index, group_index, label, line))

    for _, _, _, label, line in sorted(remaining):
        _append_validation_diagnostic(diagnostics, seen, label, line)
        if len(diagnostics) >= limit:
            return diagnostics
    return diagnostics


def _append_validation_diagnostic(diagnostics: list[str], seen: set[str], label: str, line: str) -> None:
    item = f"{label}: {_short_audit_line(line, limit=220)}"
    key = diagnostic_identity_key(line)
    if key in seen:
        return
    seen.add(key)
    diagnostics.append(item)


def _validation_failure_label(record: dict) -> str:
    label = str(record.get("phase") or "validation")
    if record.get("check_name"):
        label = f"{label}:{record['check_name']}"
    return label


def _validation_record_diagnostic_lines(record: dict) -> list[str]:
    summary = record.get("diagnostic_summary")
    if isinstance(summary, list):
        lines = [str(line) for line in summary if line]
    elif isinstance(summary, str):
        lines = [line for line in summary.splitlines() if line.strip()]
    else:
        lines = diagnostic_summary_lines(record.get("error_excerpt"))
    return [
        line
        for _, line in sorted(
            enumerate(lines),
            key=lambda item: (_audit_diagnostic_line_priority(item[1]), item[0]),
        )
    ]


def _audit_diagnostic_line_priority(line: str) -> int:
    normalized = line.strip().lower()
    if not normalized:
        return 4
    if normalized.startswith(("w:", "warning:")) or " warning" in normalized:
        return 3
    if re.search(r"(^|[./\\])[\w./\\-]+:\d+(?::\d+)?:", normalized):
        return 0
    if ">" in normalized and normalized.endswith("failed"):
        return 0
    if any(
        marker in normalized
        for marker in (
            "error",
            "exception",
            "assertion",
            "unresolved reference",
            "cannot find",
            "not assignable",
            "undefined",
            "missing",
            "panic",
            "panicked",
        )
    ):
        return 0
    if " failed" in normalized:
        return 1
    return 2


def _extract_reviewer_warnings(output: str) -> list[str]:
    if not output:
        return []
    lines = output.splitlines()
    in_warnings = False
    extracted = []
    current_title: str | None = None
    current_details: dict[str, str] = {}

    def _flush_structured_warning() -> None:
        nonlocal current_title, current_details
        if not current_title:
            return
        details = []
        for key in ("file", "concern", "suggestion"):
            value = current_details.get(key)
            if value:
                details.append(f"{key}: {value}")
        suffix = f" — {'; '.join(details)}" if details else ""
        extracted.append(_short_audit_line(f"{current_title}{suffix}"))
        current_title = None
        current_details = {}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_warnings:
                _flush_structured_warning()
            in_warnings = stripped.lower() == "## warnings"
            continue
        if not in_warnings:
            continue
        if stripped.startswith("### "):
            _flush_structured_warning()
            current_title = stripped[4:].strip()
            current_details = {}
            continue
        field = re.match(r"(?i)^(file|concern|suggestion):\s*(.+)$", stripped)
        if current_title and field:
            current_details[field.group(1).lower()] = field.group(2).strip()
            continue
        if in_warnings and stripped.startswith(("-", "*")):
            _flush_structured_warning()
            text = stripped[1:].strip()
            if text:
                extracted.append(_short_audit_line(text))
    _flush_structured_warning()
    return extracted


def _llm_warning_summary_lines(records: list[dict], task_id: str) -> list[str]:
    warning_count = _llm_warning_count(records)
    if not warning_count:
        return []
    return [f"{warning_count} warning(s) recorded (see: sikula show {task_id})"]


def _llm_warning_count(records: list[dict]) -> int:
    count = 0
    for record in records:
        if record.get("has_warnings"):
            count += max(1, len(_extract_reviewer_warnings(record.get("reviewer_output", ""))))
    return count


def _reviewer_warning_summary_lines(state) -> list[str]:
    return _llm_warning_summary_lines(getattr(state, "review_cycle_records", []), state.task_id)


def _security_warning_summary_lines(state) -> list[str]:
    return _llm_warning_summary_lines(getattr(state, "security_review_cycle_records", []), state.task_id)


def _task_audit_warnings(state) -> list[str]:
    warnings: list[str] = []
    for warning in getattr(state, "analyst_warnings", []):
        warnings.append(f"analyst: {_short_audit_line(warning)}")

    for entry in getattr(state, "history", []):
        if entry.get("action") == "write_path_warning":
            agent = entry.get("agent") or "agent"
            warnings.append(f"{agent}: {_short_audit_line(entry.get('result'))}")

    gate_records = getattr(state, "test_execution_gate_records", [])
    active_gate_records = [record for record in gate_records if record.get("status") != "resolved"]
    if active_gate_records:
        warnings.append(
            f"test execution gate audits: {len(active_gate_records)} active (see: sikula show {state.task_id})"
        )

    synthetic_harness_records = getattr(state, "synthetic_test_harness_records", [])
    active_synthetic_records = [record for record in synthetic_harness_records if record.get("status") != "resolved"]
    if active_synthetic_records:
        warnings.append(
            f"synthetic test harness audits: {len(active_synthetic_records)} active (see: sikula show {state.task_id})"
        )

    asset_drift_records = getattr(state, "implementation_asset_drift_records", [])
    if asset_drift_records:
        warnings.append(f"asset drift audits: {len(asset_drift_records)} warning(s) (see: sikula show {state.task_id})")

    asset_target_records = getattr(state, "implementation_asset_target_records", [])
    asset_target_warning_count = sum(
        1 for record in asset_target_records if record.get("status") in {"missing", "outside_project"}
    )
    if asset_target_warning_count:
        warnings.append(
            f"delivery asset target audits: {asset_target_warning_count} warning(s) (see: sikula show {state.task_id})"
        )

    artifacts = getattr(state, "validation_artifact_records", [])
    if artifacts:
        cleaned = sum(1 for record in artifacts if record.get("status") == "cleaned")
        blocked = sum(1 for record in artifacts if record.get("status") == "blocked")
        cleanup_failed = sum(1 for record in artifacts if record.get("status") == "cleanup_failed")
        details = []
        if cleaned:
            details.append(f"{cleaned} cleaned")
        if blocked:
            details.append(f"{blocked} blocked")
        if cleanup_failed:
            details.append(f"{cleanup_failed} cleanup failed")
        suffix = f" ({', '.join(details)})" if details else ""
        warnings.append(f"validation artifacts: {len(artifacts)}{suffix}")

    llm_retries = sum(1 for entry in getattr(state, "history", []) if entry.get("action") == "llm_retry")
    if llm_retries:
        warnings.append(f"LLM retries: {llm_retries}")

    production_triage_count = sum(
        1 for record in getattr(state, "fix_cycle_records", []) if record.get("triage_pass") == "production_confirmed"
    )
    if production_triage_count and not state.done:
        warnings.append(f"production-confirmed test failure triage: {production_triage_count}")

    return warnings


def _testability_gap_key(gap: dict) -> tuple[str, str, str, str]:
    return (
        str(gap.get("target") or "").strip().lower(),
        str(gap.get("reason") or gap.get("message") or "").strip().lower(),
        str(gap.get("covered_by") or "").strip().lower(),
        str(gap.get("risk") or "").strip().lower(),
    )


def _unique_testability_gaps(gaps: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        key = _testability_gap_key(gap)
        if key in seen:
            continue
        seen.add(key)
        unique.append(gap)
    return unique


def _testability_gap_label(gap: dict) -> str:
    target = _short_audit_line(gap.get("target") or gap.get("message") or "unspecified target", limit=120)
    risk = _short_audit_line(gap.get("risk"), limit=40)
    label = f"gap: {target}"
    if risk:
        label += f" [{risk}]"
    details = []
    if gap.get("reason"):
        details.append(f"reason: {_short_audit_line(gap.get('reason'), limit=120)}")
    if gap.get("covered_by"):
        details.append(f"covered_by: {_short_audit_line(gap.get('covered_by'), limit=120)}")
    if details:
        label += " — " + "; ".join(details)
    return label


def _testability_gap_sample_lines(state, limit: int = _RECOVERED_DIAGNOSTIC_LIMIT) -> list[str]:
    gaps = _unique_testability_gaps(getattr(state, "testability_gaps", []))
    lines = [_testability_gap_label(gap) for gap in gaps[:limit]]
    remaining = len(gaps) - limit
    if remaining > 0:
        lines.append(f"... {remaining} more unique gap(s) (see: sikula show {state.task_id})")
    return lines


def _task_warning_count(state) -> int:
    return (
        len(_task_audit_warnings(state))
        + _llm_warning_count(getattr(state, "review_cycle_records", []))
        + _llm_warning_count(getattr(state, "security_review_cycle_records", []))
        + len(_unique_testability_gaps(getattr(state, "testability_gaps", [])))
    )


def _task_recovered_issues(state) -> list[str]:
    recovered: list[str] = []
    validation_failures = _validation_failure_summary(getattr(state, "validation_cycle_records", []))
    if state.done and validation_failures:
        recovered.append(
            f"validation recovered after failed {validation_failures} "
            f"(showing up to {_RECOVERED_DIAGNOSTIC_LIMIT} sampled diagnostics; see: sikula show {state.task_id})"
        )
        recovered.extend(_validation_failure_diagnostics(getattr(state, "validation_cycle_records", [])))

    production_triage_count = sum(
        1 for record in getattr(state, "fix_cycle_records", []) if record.get("triage_pass") == "production_confirmed"
    )
    if state.done and production_triage_count:
        recovered.append(f"fixer used production-confirmed test failure triage: {production_triage_count}")

    gate_records = getattr(state, "test_execution_gate_records", [])
    resolved_gate_records = [record for record in gate_records if record.get("status") == "resolved"]
    if state.done and resolved_gate_records:
        recovered.append(f"test execution gate audits recovered: {len(resolved_gate_records)}")

    synthetic_harness_records = getattr(state, "synthetic_test_harness_records", [])
    resolved_synthetic_records = [record for record in synthetic_harness_records if record.get("status") == "resolved"]
    if state.done and resolved_synthetic_records:
        recovered.append(f"synthetic test harness audits recovered: {len(resolved_synthetic_records)}")

    return recovered


def _task_failed_issues(state) -> list[str]:
    if not state.failed or state.done:
        return []

    active_validation_records = _current_failed_validation_records(state)
    validation_failures = _validation_failure_summary(active_validation_records)
    if not validation_failures:
        return []

    failed = [
        f"validation failed: {validation_failures} "
        f"(showing up to {_RECOVERED_DIAGNOSTIC_LIMIT} sampled diagnostics; see: sikula show {state.task_id})"
    ]
    failed.extend(_validation_failure_diagnostics(active_validation_records))
    return failed


def _print_limited_lines(lines: list[str], task_id: str, limit: int = 8) -> None:
    for line in lines[:limit]:
        print(f"  - {line}")
    remaining = len(lines) - limit
    if remaining > 0:
        print(f"  - ... {remaining} more (see: sikula show {task_id})")


def _print_task_audit_report(state) -> int:
    warnings = _task_audit_warnings(state)
    recovered = _task_recovered_issues(state)
    failed = _task_failed_issues(state)

    print("Validation:")
    print(f"  build: {_validation_status(state.build_status)}")
    print(f"  test:  {_validation_status(state.test_status)}")
    print(f"  check: {_validation_status(state.check_status)}")
    print("Reviews:")
    print(f"  reviewer:          {_review_status(state.review_approved, state.review_cycle_records)}")
    print(f"  security reviewer: {_review_status(state.security_approved, state.security_review_cycle_records)}")
    print(f"  test writer runs:  {len(state.test_write_records)}")

    if warnings:
        print("Audit warnings:")
        _print_limited_lines(warnings, state.task_id, limit=len(warnings))

    review_lines = _reviewer_warning_summary_lines(state)
    if review_lines:
        print("Reviewer warnings:")
        _print_limited_lines(review_lines, state.task_id, limit=len(review_lines))

    sec_lines = _security_warning_summary_lines(state)
    if sec_lines:
        print("Security warnings:")
        _print_limited_lines(sec_lines, state.task_id, limit=len(sec_lines))

    gap_lines = _testability_gap_sample_lines(state)
    if gap_lines:
        print("Testability gaps:")
        _print_limited_lines(gap_lines, state.task_id, limit=len(gap_lines))

    if recovered:
        print("Recovered issues:")
        _print_limited_lines(recovered, state.task_id, limit=len(recovered))

    if failed:
        print("Failed issues:")
        _print_limited_lines(failed, state.task_id, limit=len(failed))

    return _task_warning_count(state)


def _run_context() -> cli_run.RunContext:
    return cli_run.RunContext(
        supported_build_tools=_SUPPORTED_BUILD_TOOLS,
        parse_agent_llm_overrides=_parse_agent_llm_overrides,
        resolve_state_dir=_resolve_state_dir,
        sikula_worktree_base_for_path=_sikula_worktree_base_for_path,
        reset_failed_state=_reset_failed_state,
        resolve_task_path=_resolve_task_path,
        find_git_root=_find_git_root,
        require_committed_config_for_isolated_run=_require_committed_config_for_isolated_run,
        run_config_snapshot=_run_config_snapshot,
        contract_preflight_config=_contract_preflight_config,
        build_contract_preflight_snapshot_and_assets=_build_contract_preflight_snapshot_and_assets,
        record_contract_asset_drift=_record_contract_asset_drift,
        contract_preflight_record_result=_contract_preflight_record_result,
        print_contract_preflight_summary=_print_contract_preflight_summary,
        contract_readiness_gate_failures=_contract_readiness_gate_failures,
        print_contract_readiness_gate_failure=_print_contract_readiness_gate_failure,
        branch_stem=_branch_stem,
        ensure_gitignore=_ensure_gitignore,
        create_worktree=_create_worktree,
        build_tool_class=_build_tool_class,
        record_snapshot_asset_drift=_record_snapshot_asset_drift,
        build_orchestrator=build_orchestrator,
        current_branch_delivery_needs_finalization=_current_branch_delivery_needs_finalization,
        current_branch_delivery_cleaned=_current_branch_delivery_cleaned,
        path_is_within=_path_is_within,
        record_asset_target_audit=_record_asset_target_audit,
        current_branch_delivery_pending=_current_branch_delivery_pending,
        deliver_current_branch_review_fix=_deliver_current_branch_review_fix,
        default_worktree_commit_message=_default_worktree_commit_message,
        finalize_worktree=_finalize_worktree,
        current_branch_delivery_terminal=_current_branch_delivery_terminal,
        task_warning_count=_task_warning_count,
        contract_gate_blocked_without_worktree=_contract_gate_blocked_without_worktree,
        contract_gate_next_action=_contract_gate_next_action,
        fmt_time=_fmt_time,
        print_task_audit_report=_print_task_audit_report,
        logger=log,
    )


def cmd_run(args: argparse.Namespace, cfg: dict) -> None:
    return cli_run.cmd_run(args, cfg, _run_context())


def _status_context() -> cli_status.StatusContext:
    return cli_status.StatusContext(
        resolve_state_dir=_resolve_state_dir,
        current_branch_delivery_needs_finalization=_current_branch_delivery_needs_finalization,
        current_branch_delivery_cleaned=_current_branch_delivery_cleaned,
        contract_gate_blocked_without_worktree=_contract_gate_blocked_without_worktree,
        contract_gate_next_action=_contract_gate_next_action,
        pid_running=_pid_running,
    )


def _pid_running(pid: int) -> bool:
    return cli_status._pid_running(pid)


def _status_label(state) -> str:
    return cli_status._status_label(state, _status_context())


def _active_operation_label(active_operation: dict) -> str:
    return cli_status._active_operation_label(active_operation)


def _active_operation_is_fresh(active_operation: dict) -> bool:
    return cli_status._active_operation_is_fresh(active_operation)


def _active_operation_elapsed(active_operation: dict | None) -> str | None:
    return cli_status._active_operation_elapsed(active_operation)


def _status_step(state) -> str:
    return cli_status._status_step(state)


def _status_updated(state) -> str:
    return cli_status._status_updated(state)


def _status_next_action(state, status: str) -> str:
    return cli_status._status_next_action(state, status, _status_context())


def _status_row(state) -> dict:
    return cli_status._status_row(state, _status_context())


def _status_matches(row: dict, filters: set[str]) -> bool:
    return cli_status._status_matches(row, filters)


def cmd_status(cfg: dict, args: argparse.Namespace | None = None) -> None:
    return cli_status.cmd_status(cfg, args, _status_context())


def cmd_show(task_id: str, cfg: dict) -> None:
    return cli_status.cmd_show(task_id, cfg, _status_context())


def _cleanup_context() -> cli_cleanup.CleanupContext:
    return cli_cleanup.CleanupContext(
        resolve_state_dir=_resolve_state_dir,
        path_is_within=_path_is_within,
        worktree_dirty=_worktree_dirty,
        find_git_root=_find_git_root,
        remove_worktree=_remove_worktree,
    )


def cmd_cleanup(args: argparse.Namespace, cfg: dict) -> None:
    return cli_cleanup.cmd_cleanup(args, cfg, _cleanup_context())


def _print_review_summary(
    state, branch: str, base_branch: str, total_s: float, run_security_review: bool = True
) -> None:
    print(f"\n{'=' * 60}")
    print(f"Review:  {branch}  vs  {base_branch}")
    print(f"Files:   {len(state.files_changed)} changed")
    print(f"Time:    {_fmt_time(total_s)}")
    print(f"{'=' * 60}")
    delivery_pending = _current_branch_delivery_needs_finalization(state)
    delivery_cleaned = _current_branch_delivery_cleaned(state)
    approved = (
        state.review_approved
        and (state.security_approved if run_security_review else True)
        and not delivery_pending
        and not delivery_cleaned
    )
    if delivery_pending:
        result = "DELIVERY FAILED" if state.review_delivery_status == "failed" else "DELIVERY PENDING"
    elif delivery_cleaned:
        result = "DELIVERY CLEANED"
    else:
        result = "APPROVED" if approved else "ISSUES FOUND"
    print(f"Result:  {result}")
    _print_task_audit_report(state)
    print(f"\nState ID: {state.task_id}  (sikula show {state.task_id})")


def _heartbeat_interval_seconds(cfg: dict) -> int:
    progress_cfg = cfg.get("progress", {})
    heartbeat_interval_seconds = int(progress_cfg.get("heartbeat_interval_seconds", 60))
    if heartbeat_interval_seconds <= 0:
        heartbeat_interval_seconds = 0
    return heartbeat_interval_seconds


def _run_review_agent_with_retry_history(agent, name: str, state, store, heartbeat_interval_seconds: int = 0):
    from core.progress import ActiveOperationHeartbeat
    from core.retry_history import llm_retry_history

    with ActiveOperationHeartbeat(
        store,
        state,
        phase="agent",
        agent=name,
        message=f"Running {name}",
        interval_s=heartbeat_interval_seconds,
    ):
        with llm_retry_history(agent, name, state, store):
            return agent.run(state)


def _exception_summary(exc: BaseException, limit: int = 1000) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:limit]


def _record_report_only_review_failure(state, store, exc: BaseException) -> None:
    state.done = False
    state.failed = True
    state.active_operation = None
    state.test_status = state.test_status or "skipped"
    state.check_status = state.check_status or "skipped"
    state.record(
        "sikula",
        "review_failed",
        f"report-only review failed: {exc.__class__.__name__}",
        error=_exception_summary(exc),
    )
    store.save(state)


def _cleanup_report_only_review_worktree(state, store, git_root: Path, worktree_base: Path) -> None:
    result = "report-only review worktree already missing"
    if worktree_base.exists():
        if not _remove_worktree(worktree_base, git_root, force=False):
            log.warning("Could not remove report-only review worktree: %s", worktree_base)
            state.record("sikula", "cleanup_failed", "report-only review worktree cleanup failed")
            store.save(state)
            return
        result = "report-only review worktree removed"

    state.record("sikula", "cleanup", result)
    state.worktree_path = None
    state.worktree_base = None
    store.save(state)


def _enrich_review_state_prompt(
    state, store, description: str, base_llm_cfg: dict, cfg: dict, project_root: Path
) -> None:
    from core.llm_client import create_llm_client

    enrichment_llm = create_llm_client(
        _make_llm_config(base_llm_cfg, cfg.get("agents", {}).get("analyst", {}).get("llm", {}))
    )
    extra = _enrich_prompt_with_referenced_files(description, enrichment_llm, project_root)
    if extra:
        state.implementation_prompt = description + "\n\n---\n\nFiles referenced in the task:\n\n" + extra
        store.save(state)
        log.info("implementation_prompt enriched with design file contents")


def _run_report_only_review(
    *,
    args: argparse.Namespace,
    cfg: dict,
    state,
    store,
    task_id: str,
    task_label: str,
    description: str,
    branch: str,
    base_branch: str,
    files_changed: list[str],
    base_llm_cfg: dict,
    run_security_review: bool,
    heartbeat_interval_seconds: int,
    worktree_project_root: Path,
    git_root: Path,
    worktree_base: Path,
    t_start: float,
) -> float:
    try:
        _enrich_review_state_prompt(state, store, description, base_llm_cfg, cfg, worktree_project_root)

        from agents.reviewer_agent import ReviewerAgent
        from agents.security_reviewer_agent import SecurityReviewerAgent
        from core.llm_client import create_llm_client
        from tools.base_tool import Sandbox
        from tools.file_tool import FileTool
        from tools.git_tool import GitTool

        sandbox_cfg = cfg.get("sandbox", {})
        sandbox = Sandbox(
            project_root=worktree_project_root,
            allowed_write_paths=sandbox_cfg.get("allowed_write_paths", []),
            allowed_read_paths=sandbox_cfg.get("allowed_read_paths", ["."]),
        )
        file_tool = FileTool(sandbox=sandbox, project_root=worktree_project_root)
        git_tool = GitTool(sandbox=sandbox, project_root=worktree_project_root)
        tools = {"file": file_tool, "git": git_tool}

        agent_llm_overrides = _parse_agent_llm_overrides(args.agent_model, args.agent_provider, args.agent_timeout)
        agents_cfg = cfg.get("agents", {})

        def _review_agent_snapshot(name: str) -> dict:
            snap: dict = {
                "provider": _make_llm_config(base_llm_cfg, agents_cfg.get(name, {}).get("llm", {})).provider,
                "model": _make_llm_config(base_llm_cfg, agents_cfg.get(name, {}).get("llm", {})).model,
            }
            extra_rules = cfg.get(name, {}).get("extra_rules")
            if extra_rules:
                snap["extra_rules"] = extra_rules
            return snap

        state.config_snapshot = {
            "project": cfg.get("project", {}).get("name"),
            "run_security_review": run_security_review,
            "progress": {
                "heartbeat_interval_seconds": heartbeat_interval_seconds,
            },
            "sandbox": {
                "allowed_write_paths": sandbox_cfg.get("allowed_write_paths", []),
                "allowed_test_write_paths": sandbox_cfg.get("allowed_test_write_paths", []),
                "allowed_read_paths": sandbox_cfg.get("allowed_read_paths", ["."]),
            },
            "test_writer": cfg.get("test_writer", {}),
            "agents": {name: _review_agent_snapshot(name) for name in ("reviewer", "security_reviewer")},
        }
        store.save(state)

        def _llm(name: str):
            yaml_agent = agents_cfg.get(name, {}).get("llm", {})
            cli_agent = agent_llm_overrides.get(name, {})
            return create_llm_client(_make_llm_config(base_llm_cfg, {**yaml_agent, **cli_agent}))

        log.info("Task %s — %s", task_id, task_label)
        log.info("Reviewing '%s' vs '%s' (%d file(s) changed)...", branch, base_branch, len(files_changed))

        log.info("--- Phase: review ---")
        reviewer = ReviewerAgent(llm=_llm("reviewer"), tools=tools, project_config=cfg)
        _run_review_agent_with_retry_history(reviewer, "reviewer", state, store, heartbeat_interval_seconds)
        store.save(state)

        if state.review_approved and run_security_review:
            log.info("--- Phase: security review ---")
            security_reviewer = SecurityReviewerAgent(llm=_llm("security_reviewer"), tools=tools, project_config=cfg)
            _run_review_agent_with_retry_history(
                security_reviewer,
                "security_reviewer",
                state,
                store,
                heartbeat_interval_seconds,
            )
            store.save(state)

        approved = state.review_approved and (state.security_approved if run_security_review else True)
        state.test_status = "skipped"
        state.check_status = "skipped"
        state.done = approved
        state.failed = not approved
        total_s = time.time() - t_start
        store.save(state)
        return total_s
    except BaseException as exc:
        _record_report_only_review_failure(state, store, exc)
        raise
    finally:
        try:
            _cleanup_report_only_review_worktree(state, store, git_root, worktree_base)
        except Exception:
            log.exception("Report-only review worktree cleanup failed")


def _review_context() -> cli_review.ReviewContext:
    return cli_review.ReviewContext(
        supported_build_tools=_SUPPORTED_BUILD_TOOLS,
        find_git_root=_find_git_root,
        ensure_gitignore=_ensure_gitignore,
        current_branch_name=_current_branch_name,
        current_worktree_changes=_current_worktree_changes,
        print_current_branch_clean_error=_print_current_branch_clean_error,
        resolve_git_commit=_resolve_git_commit,
        worktree_error_message=_worktree_error_message,
        build_tool_class=_build_tool_class,
        resolve_state_dir=_resolve_state_dir,
        heartbeat_interval_seconds=_heartbeat_interval_seconds,
        enrich_review_state_prompt=_enrich_review_state_prompt,
        parse_agent_llm_overrides=_parse_agent_llm_overrides,
        build_orchestrator=build_orchestrator,
        deliver_current_branch_review_fix=_deliver_current_branch_review_fix,
        finalize_worktree=_finalize_worktree,
        run_report_only_review=_run_report_only_review,
        current_branch_delivery_needs_finalization=_current_branch_delivery_needs_finalization,
        require_worktree_context_for_review=_require_worktree_context_for_review,
        print_review_summary=_print_review_summary,
        logger=log,
    )


def cmd_review(args: argparse.Namespace, cfg: dict) -> None:
    return cli_review.cmd_review(args, cfg, _review_context())


# ---------------------------------------------------------------------------
# Init command
# ---------------------------------------------------------------------------


def _generate_config(  # noqa: PLR0912
    build_tool: str | None,
    language: str | None,
    platform: str | None,
    guidelines_files: list[str],
    project_name: str,
    provider: str | None,
    model: str | None,
    write_paths: list[str] | None = None,
    test_write_paths: list[str] | None = None,
    xcode_scheme: str | None = None,
    node_package_manager: str | None = None,
    node_sync_command: str | None = None,
    node_compile_command: str | None = None,
    node_test_command: str | None = None,
    node_checks: list[dict[str, str | int]] | None = None,
) -> str:
    return cli_init.generate_config(
        build_tool=build_tool,
        language=language,
        platform=platform,
        guidelines_files=guidelines_files,
        project_name=project_name,
        provider=provider,
        model=model,
        write_paths=write_paths,
        test_write_paths=test_write_paths,
        xcode_scheme=xcode_scheme,
        node_package_manager=node_package_manager,
        node_sync_command=node_sync_command,
        node_compile_command=node_compile_command,
        node_test_command=node_test_command,
        node_checks=node_checks,
    )


def _load_init_config(path: Path, *, strict: bool = False) -> dict:
    return cli_init.load_init_config(path, strict=strict)


def _init_llm_value(args: argparse.Namespace, existing_cfg: dict, key: str) -> str | None:
    return cli_init.init_llm_value(args, existing_cfg, key)


def _init_tech_stack(existing_cfg: dict) -> str:
    return cli_init.init_tech_stack(existing_cfg)


def _config_references_guidelines(config_path: Path, guidelines_ref: str) -> bool:
    return cli_init.config_references_guidelines(config_path, guidelines_ref)


def _insert_guidelines_reference(config_path: Path, guidelines_ref: str) -> bool:
    return cli_init.insert_guidelines_reference(config_path, guidelines_ref)


def _generate_guidelines_for_init(project_root: Path, tech: str, provider: str, model: str) -> str:
    return cli_init.generate_guidelines_for_init(project_root, tech, provider, model)


def _init_context() -> cli_init.InitContext:
    return cli_init.InitContext(
        load_project_env=_load_project_env,
        load_init_config=_load_init_config,
        init_llm_value=_init_llm_value,
        init_tech_stack=_init_tech_stack,
        generate_guidelines_for_init=_generate_guidelines_for_init,
        insert_guidelines_reference=_insert_guidelines_reference,
        find_git_root=_find_git_root,
        ensure_project_gitignore_entry=_ensure_project_gitignore_entry,
        ensure_provider_gitignore_entry=_ensure_provider_gitignore_entry,
        ensure_sikula_gitignore=_ensure_sikula_gitignore,
        generate_config=_generate_config,
    )


def _cmd_init_guidelines_only(args: argparse.Namespace, project_root: Path, config_path: Path) -> None:
    return cli_init.cmd_init_guidelines_only(args, project_root, config_path, _init_context())


def cmd_init(args: argparse.Namespace) -> None:
    return cli_init.cmd_init(args, _init_context())


def cmd_delivery_check(args: argparse.Namespace, cfg: dict) -> None:
    return cli_delivery.cmd_delivery_check(args, cfg)


def cmd_delivery_status(args: argparse.Namespace, cfg: dict) -> None:
    return cli_delivery.cmd_delivery_status(args, cfg)


def cmd_delivery_finalize(args: argparse.Namespace, cfg: dict) -> None:
    return cli_delivery.cmd_delivery_finalize(args, cfg)


def _run_delivery_child_task(args: argparse.Namespace, cfg: dict) -> cli_delivery.DeliveryChildRunResult:
    try:
        cmd_run(args, cfg)
    except SystemExit as exc:
        child_task_id = getattr(args, "created_task_id", None)
        if isinstance(exc.code, int):
            return cli_delivery.DeliveryChildRunResult(exit_code=exc.code, child_task_id=child_task_id)
        if exc.code is None:
            return cli_delivery.DeliveryChildRunResult(exit_code=0, child_task_id=child_task_id)
        return cli_delivery.DeliveryChildRunResult(exit_code=1, child_task_id=child_task_id)
    return cli_delivery.DeliveryChildRunResult(exit_code=0, child_task_id=getattr(args, "created_task_id", None))


def _delivery_run_next_context() -> cli_delivery.DeliveryRunNextContext:
    return cli_delivery.DeliveryRunNextContext(
        run_task=_run_delivery_child_task,
        resolve_state_dir=_resolve_state_dir,
    )


def cmd_delivery_run_next(args: argparse.Namespace, cfg: dict) -> None:
    return cli_delivery.cmd_delivery_run_next(args, cfg, _delivery_run_next_context())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Sikula",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_sikula_version()}")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to project config YAML (overrides auto-discovery of .sikula/config.yaml)",
    )

    sub = parser.add_subparsers(dest="command")

    task_p = sub.add_parser("task", help="Prepare product task descriptions")
    task_sub = task_p.add_subparsers(dest="task_command")
    cli_task.register_refine_parser(task_sub)
    cli_task.register_attach_parser(task_sub)

    contract_p = sub.add_parser("contract", help="Inspect or prepare implementation contracts")
    contract_sub = contract_p.add_subparsers(dest="contract_command")
    cli_contract.register_check_parser(contract_sub)
    cli_contract.register_prepare_parser(contract_sub)

    delivery_p = cli_delivery.register_parser(sub)

    run_p = cli_run.register_parser(sub, contract_score_threshold=_contract_score_threshold)

    cli_status.register_parser(sub)
    cli_cleanup.register_parser(sub)
    cli_review.register_parser(sub)

    cli_init.register_parser(sub)

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
        return

    if args.command == "delivery" and args.delivery_command in {None, "check", "status"}:
        cfg = {}
    else:
        cfg = _load_runtime_config(
            args.config,
            required=args.command not in {"contract", "task"},
        )

    if args.command == "run":
        task_file = args.task_file_pos or args.task_file
        if not task_file and not args.task_id:
            run_p.error("provide TASK_FILE or --task-id")
        cmd_run(args, cfg)
    elif args.command == "task":
        if args.task_command == "refine":
            cmd_task_refine(args, cfg)
        elif args.task_command == "attach":
            cmd_task_attach(args, cfg)
        else:
            task_p.print_help()
            sys.exit(1)
    elif args.command == "contract":
        if args.contract_command == "check":
            cmd_contract_check(args, cfg)
        elif args.contract_command == "prepare":
            cmd_contract_prepare(args, cfg)
        else:
            contract_p.print_help()
            sys.exit(1)
    elif args.command == "delivery":
        if args.delivery_command == "check":
            cmd_delivery_check(args, cfg)
        elif args.delivery_command == "status":
            cmd_delivery_status(args, cfg)
        elif args.delivery_command == "run-next":
            cmd_delivery_run_next(args, cfg)
        elif args.delivery_command == "finalize":
            cmd_delivery_finalize(args, cfg)
        else:
            delivery_p.print_help()
            sys.exit(1)
    elif args.command == "status":
        cmd_status(cfg, args)
    elif args.command == "show":
        cmd_show(args.task_id, cfg)
    elif args.command == "cleanup":
        args.delete_state = False
        cmd_cleanup(args, cfg)
    elif args.command == "delete":
        args.delete_state = True
        cmd_cleanup(args, cfg)
    elif args.command == "review":
        cmd_review(args, cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
