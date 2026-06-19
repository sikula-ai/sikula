#!/usr/bin/env python3
"""Sikula — LLM-powered development orchestration.

Usage (project-centric, run from project root):
  sikula init                        # create .sikula/config.yaml
  sikula init --guidelines --provider codex --model gpt-5.5
  sikula contract check task.md      # read-only implementation-contract preflight
  sikula task refine task.md --auto --output task.refined.md
  sikula contract prepare task.refined.md --output .sikula/contracts/task.contract.md
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
from importlib.metadata import PackageNotFoundError, version as _pkg_version
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.diagnostics import diagnostic_identity_key, diagnostic_summary_lines

_BASE = Path(__file__).parent
# When adding a new platform: add it here, in _build_tool() in core/orchestrator.py,
# in _build_tool_class() and _generate_config() below, in _SIGNATURES in tools/scanner.py,
# in tests/test_platform_onboarding.py, and in the test execution gate audit registry if
# the platform brings new test skip idioms.
_SUPPORTED_BUILD_TOOLS = {"cargo", "gradle-android", "gradle-jvm", "maven", "node", "xcodebuild", "python"}
_RECOVERED_DIAGNOSTIC_LIMIT = 8
_SIKULA_GITIGNORE_ENTRIES = ("state/", "worktrees/", "contract-reports/")


def _git_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_git_checkout(path: Path = _BASE) -> bool:
    return _git_output(["rev-parse", "--is-inside-work-tree"], path) == "true"


def _dev_version_suffix(path: Path = _BASE) -> str:
    if not _is_git_checkout(path):
        return ""
    branch = _git_output(["branch", "--show-current"], path)
    commit = _git_output(["rev-parse", "--short", "HEAD"], path)
    parts = [re.sub(r"[^A-Za-z0-9.]+", ".", p).strip(".") for p in (branch, commit) if p]
    return "-dev" + (f"+{'.'.join(parts)}" if parts else "")


def _sikula_version() -> str:
    try:
        base_version = _pkg_version("sikula")
    except PackageNotFoundError:
        base_version = "dev"
    if base_version == "dev":
        return "dev"
    return base_version + _dev_version_suffix()


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


def _find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from start (default CWD) to find the nearest .sikula/config.yaml."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        if (directory / ".sikula" / "config.yaml").exists():
            return directory
    return None


def _load_project_env(project_root: Path) -> None:
    """Load project-local environment variables for provider CLIs and SDKs."""
    load_dotenv(project_root / ".env", override=False)


def _sikula_worktree_base_for_path(path: Path) -> Path | None:
    """Return the task worktree base when path is inside .sikula/worktrees/<task-id>."""
    root = path.resolve()
    for candidate in [root, *root.parents]:
        if candidate.parent.name == "worktrees" and candidate.parent.parent.name == ".sikula":
            return candidate
    return None


def _original_project_root_from_worktree(project_root: Path) -> Path | None:
    """Map a Sikula task worktree project root back to the original project root.

    Isolated task worktrees live under:
      <git-root>/.sikula/worktrees/<task-id>/<project-relative-path>

    The worktree contains the tracked .sikula/config.yaml too, but task state is kept
    in the original project .sikula/state. Commands such as `status`, `show`, and
    `run --task-id` should therefore resolve config from the original project when
    invoked inside a task worktree.
    """
    root = project_root.resolve()
    worktree_base = _sikula_worktree_base_for_path(root)
    if not worktree_base:
        return None
    git_root = worktree_base.parent.parent.parent
    try:
        rel = root.relative_to(worktree_base)
    except ValueError:
        return None
    original_root = (git_root / rel).resolve()
    if (original_root / ".sikula" / "config.yaml").exists():
        return original_root
    return None


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
# Config loading
# ---------------------------------------------------------------------------


def _resolve_config(config_arg: str | None) -> tuple[Path, Path | None]:
    """Return (config_path, discovered_project_root).

    discovered_project_root is set only when .sikula/config.yaml was auto-discovered;
    it is used to resolve relative paths in the config against the true project root
    rather than CWD (which may be a subdirectory).
    """
    if config_arg:
        return Path(config_arg), None

    # Auto-discover .sikula/config.yaml by walking up from CWD.
    project_root = _find_project_root()
    if project_root:
        original_root = _original_project_root_from_worktree(project_root)
        if original_root:
            return original_root / ".sikula" / "config.yaml", original_root
        return project_root / ".sikula" / "config.yaml", project_root

    print("No config found. Run 'sikula init' to set up this project, or use --config.")
    sys.exit(1)


def _resolve_optional_config(config_arg: str | None) -> tuple[Path, Path | None] | None:
    if config_arg:
        return _resolve_config(config_arg)

    project_root = _find_project_root()
    if not project_root:
        return None
    original_root = _original_project_root_from_worktree(project_root)
    if original_root:
        return original_root / ".sikula" / "config.yaml", original_root
    return project_root / ".sikula" / "config.yaml", project_root


def _load_runtime_config(config_arg: str | None, *, required: bool = True) -> dict:
    resolved = _resolve_config(config_arg) if required else _resolve_optional_config(config_arg)
    if resolved is None:
        return {}

    config_path, discovered_root = resolved
    cfg = load_config(config_path)
    cfg["_config_path"] = str(config_path.resolve())
    raw = cfg.get("project", {}).get("root_path", ".")
    cfg["project"]["root_path"] = str(_resolve_root_path(raw, discovered_root, config_path))
    _load_project_env(Path(cfg["project"]["root_path"]))
    return cfg


def _resolve_root_path(raw: str, discovered_root: Path | None, config_path: Path) -> Path:
    """Resolve project root_path to an absolute Path.

    Absolute raw values are returned as-is.
    Relative values are resolved against discovered_root (auto-discovery) or
    config_path.parent.parent (explicit --config, where config lives at .sikula/config.yaml).
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    root_base = discovered_root if discovered_root is not None else config_path.parent.parent
    return (root_base / p).resolve()


def _resolve_state_dir(cfg: dict) -> Path:
    """Resolve state_dir relative to project_root; absolute paths are used as-is."""
    raw = cfg.get("tasks", {}).get("state_dir", ".sikula/state")
    return _resolve_project_path(cfg, raw)


def _resolve_task_description_dir(cfg: dict) -> Path:
    raw = cfg.get("tasks", {}).get("task_description_dir", ".sikula/tasks")
    return _resolve_project_path(cfg, raw)


def _resolve_contract_dir(cfg: dict) -> Path:
    raw = cfg.get("tasks", {}).get("contract_dir", ".sikula/contracts")
    return _resolve_project_path(cfg, raw)


def _resolve_contract_report_dir(cfg: dict) -> Path:
    raw = cfg.get("tasks", {}).get("contract_report_dir", ".sikula/contract-reports")
    return _resolve_project_path(cfg, raw)


def _resolve_project_path(cfg: dict, raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    project_root_raw = cfg.get("project", {}).get("root_path", ".")
    project_root = Path(project_root_raw).resolve()
    return project_root / p


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config not found: {path}")
        sys.exit(1)
    return yaml.safe_load(path.read_text())


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
    """Return a human-readable error message for a failed `git worktree add`."""
    if "already checked out" in stderr or "is already used by worktree" in stderr:
        return (
            f"Branch '{branch}' is already checked out.\n"
            f"If you are currently on '{branch}', switch away first:\n"
            f"  git checkout main\n"
            "If a previous --fix run left a stale worktree, remove it:\n"
            "  git worktree list   # find the path\n"
            "  git worktree remove <path>"
        )
    return f"Failed to create worktree for branch '{branch}': {stderr}"


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
    from agents.base_agent import AGENT_SECURITY_PREFIX

    prompt = AGENT_SECURITY_PREFIX + _REFERENCED_FILES_PROMPT.format(task_description=task_description)
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
    entry = ".sikula/worktrees/"
    exclude = git_root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if exclude.exists() and any(line.strip() == entry for line in exclude.read_text().splitlines()):
        return
    with exclude.open("a") as f:
        f.write(f"\n{entry}\n")


def _ensure_project_gitignore_entry(project_root: Path, entry: str) -> None:
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if any(line.strip() == entry for line in existing.splitlines()):
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with gitignore.open("a") as f:
        f.write(f"{prefix}{entry}\n")


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
    """Return the git repository root containing path, or None if not in a git repo."""
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=path,
    )
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip()).resolve()


def _git_relative_path(git_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return None


def _tracked_clean_file_status(git_root: Path, path: Path) -> tuple[bool, str]:
    """Return whether path exists, is tracked, and matches HEAD in git_root."""
    rel = _git_relative_path(git_root, path)
    if rel is None:
        return True, ""
    if not path.exists():
        return False, "does not exist"

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if tracked.returncode != 0:
        return False, "not tracked by git"

    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", rel], cwd=git_root)
    if staged.returncode != 0:
        return False, "has staged changes not committed to HEAD"

    unstaged = subprocess.run(["git", "diff", "--quiet", "--", rel], cwd=git_root)
    if unstaged.returncode != 0:
        return False, "has unstaged changes"

    return True, ""


def _isolation_context_files(cfg: dict) -> list[tuple[str, Path]]:
    """Return files that must be present unchanged in isolated task worktrees."""
    result: list[tuple[str, Path]] = []

    raw_config_path = cfg.get("_config_path")
    if raw_config_path:
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

    deduped: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for kind, path in result:
        if path in seen:
            continue
        seen.add(path)
        deduped.append((kind, path))
    return deduped


def _require_committed_config_for_isolated_run(cfg: dict, git_root: Path) -> None:
    """Fail fast when config/guidelines context will not exist unchanged in a new worktree."""
    problems: list[tuple[str, str, str]] = []
    for kind, path in _isolation_context_files(cfg):
        rel = _git_relative_path(git_root, path)
        if rel is None:
            continue

        ok, reason = _tracked_clean_file_status(git_root, path)
        if not ok:
            problems.append((kind, rel, reason))

    if not problems:
        return

    print("Error: isolated run requires Sikula config/context files to be committed before creating a worktree.")
    print("The task worktree starts from HEAD, so untracked or uncommitted config/guidelines are not visible there.")
    print("Problem files:")
    for kind, rel, reason in problems:
        print(f"  - {rel} ({kind}): {reason}")
    add_paths = " ".join(rel for _, rel, _ in problems)
    print(f"Run: git add {add_paths} && git commit -m 'Add Sikula config'")
    print("Or use --no-isolate for a local experiment.")
    sys.exit(1)


def _create_worktree(git_root: Path, worktree_base: Path, branch: str) -> tuple[bool, str]:
    worktree_base.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "worktree", "add", str(worktree_base), "-b", branch],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    return r.returncode == 0, r.stderr.strip()


def _finalize_worktree(
    worktree_base: Path,
    git_root: Path,
    state,
    commit_msg: str | None = None,
) -> tuple[bool, bool, str | None]:
    """Commit all changes and remove the worktree. Returns (success, committed, commit_sha)."""
    subprocess.run(["git", "add", "-A"], cwd=worktree_base, check=False)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
    )
    committed = False
    commit_sha = None
    if status.stdout.strip():
        if commit_msg is None:
            branch_short = state.worktree_branch.removeprefix("sikula/")
            stem = (
                branch_short.removesuffix(f"-{state.task_id}")
                if branch_short.endswith(f"-{state.task_id}")
                else branch_short
            )
            commit_msg = f"sikula: {stem}\n\nTask ID: {state.task_id}"
        r = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
            cwd=worktree_base,
            check=False,
        )
        if r.returncode != 0:
            log.error("Failed to commit worktree changes: %s", r.stderr.strip())
            return False, False, None
        committed = True
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_base,
            check=False,
        )
        if r.returncode == 0:
            commit_sha = r.stdout.strip()
            state.result_commit = commit_sha
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


def _worktree_dirty(worktree_base: Path) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
    )
    return bool(status.stdout.strip()) if status.returncode == 0 else True


def _remove_worktree(worktree_base: Path, git_root: Path, *, force: bool) -> bool:
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree_base))
    result = subprocess.run(cmd, cwd=git_root, check=False)
    return result.returncode == 0


def _path_is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


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
        answers=answers,
        normalize_provider=lambda request: agent.normalize_task_description(
            request,
            project_root=project_root,
            audit_recorder=audit_recorder,
        ),
        audit_recorder=audit_recorder,
    )


def cmd_task_refine(args: argparse.Namespace, cfg: dict) -> None:
    from core.contract_check import prepare_task_description

    if args.auto and args.interactive:
        print("Failed to refine task: --auto cannot be combined with --interactive", file=sys.stderr)
        sys.exit(2)

    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = _resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    task_text = task_path.read_text(encoding="utf-8")
    answers: dict[str, dict] = {}
    answers_supplied = bool(args.interactive or args.answers)
    if args.interactive:
        try:
            first = prepare_task_description(task_text, task_name=task_path.name)
            answers = _collect_prepare_answers_interactive(
                generated_by="sikula.task_refine",
                label="task refinement",
                source_path=task_path,
                source_text=task_text,
                project_root=project_root,
                questions=first.user_questions,
                cfg=cfg,
                answers_path=_resolve_answers_path(args.answers) if args.answers else None,
            )
        except (EOFError, OSError, ValueError) as exc:
            print(f"Failed to collect task refinement answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.answers:
        try:
            answers = _load_prepare_answers(
                _resolve_answers_path(args.answers), source_path=task_path, source_text=task_text
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to load task refinement answers: {exc}", file=sys.stderr)
            sys.exit(1)

    output_path = _resolve_output_path(args.output) if args.output else _default_refined_task_path(task_path, cfg)
    if args.auto:
        if output_path.exists():
            print(f"Failed to refine task: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
            _print_existing_output_hint(output_path)
            sys.exit(1)
        try:
            auto_result = _run_task_refine_auto(
                args=args,
                cfg=cfg,
                project_root=project_root,
                source_path=task_path,
                task_text=task_text,
                task_name=task_path.name,
                output_path=output_path,
                answers=answers,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Failed to auto-refine task: {exc}", file=sys.stderr)
            sys.exit(1)
        result = auto_result.result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.prepared_task_markdown, encoding="utf-8")

        print(f"Refined task description written: {output_path}")
        print("Auto-normalized task description: yes")
        if auto_result.input_language:
            print(f"Input language: {auto_result.input_language}")
        if auto_result.normalized_to_english:
            print("Normalized to English: yes")
        if auto_result.warnings:
            print("Auto-refine warnings:")
            for warning in auto_result.warnings:
                print(f"- {warning}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        _print_open_question_details(result.user_questions)
        _print_task_refinement_scope_note()
        if result.needs_user_input:
            answers_path = _write_prepare_answers_template(
                generated_by="sikula.task_refine",
                source_path=output_path,
                source_text=result.prepared_task_markdown,
                project_root=project_root,
                questions=result.user_questions,
                cfg=cfg,
            )
            print("Next step:")
            print(f"- Fill the answers file, then run: sikula task refine {output_path} --answers {answers_path}")
            print("- Use a new --output path, or remove/rename the refined task written above first.")
        else:
            print(f"Next step: sikula contract prepare {output_path}")
        return

    result = prepare_task_description(task_text, task_name=task_path.name, answers=answers)
    if result.needs_user_input and not answers_supplied:
        answers_path = _write_prepare_answers_template(
            generated_by="sikula.task_refine",
            source_path=task_path,
            source_text=task_text,
            project_root=project_root,
            questions=result.user_questions,
            cfg=cfg,
        )
        print("Task refinement needs answers before writing a refined task description.")
        print(f"Task refinement answers template written: {answers_path}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        _print_open_question_details(result.user_questions)
        _print_task_refinement_scope_note()
        print("Next step:")
        print(f"- Fill the answers file, then run: sikula task refine {args.task_file} --answers {answers_path}")
        print(f"- Or answer in the terminal: sikula task refine {args.task_file} --interactive")
        if output_path.exists():
            _print_existing_output_next_step_note(output_path)
        sys.exit(1)

    if output_path.exists():
        print(f"Failed to refine task: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        _print_existing_output_hint(output_path)
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.prepared_task_markdown, encoding="utf-8")

    print(f"Refined task description written: {output_path}")
    print(f"Applied answers: {len(result.answered_question_ids)}")
    print(f"Open questions: {len(result.open_question_ids)}")
    _print_open_question_details(result.user_questions)
    _print_task_refinement_scope_note()
    if result.needs_user_input:
        answers_path = (
            _resolve_answers_path(args.answers)
            if args.answers
            else _prepare_answers_path(
                task_path,
                cfg,
                generated_by="sikula.task_refine",
            )
        )
        print("Next step:")
        print(f"- Fill/update the answers file: {answers_path}")
        print("- Then rerun task refine with a new --output path, or remove/rename the output written above first.")
    else:
        print(f"Next step: sikula contract prepare {output_path}")


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


def cmd_contract_check(args: argparse.Namespace, cfg: dict) -> None:
    from core.contract_check import check_contract_file, render_contract_check, write_contract_report

    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = _resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    result = check_contract_file(task_path, project_config=_contract_cli_project_config(cfg))
    write_result = None
    if args.write_report:
        report_root = project_root if cfg.get("project", {}).get("root_path") else None
        report_dir = _resolve_contract_report_dir(cfg) if cfg.get("_config_path") else None
        try:
            write_result = write_contract_report(
                result,
                task_path=task_path,
                project_root=report_root,
                report_dir=report_dir,
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to write contract report: {exc}", file=sys.stderr)
            sys.exit(1)

    if args.json:
        data = result.to_dict()
        if write_result:
            data["written_report"] = write_result.to_dict()
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render_contract_check(result), end="")
        if write_result:
            print("Generated contract report artifacts:")
            print(f"- {write_result.report_path}")
            print(f"- {write_result.answers_path}")


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
        contract_name=output_path.name,
        project_context=project_context,
        generated_answer_entries=generated_answer_entries,
        initial_answers=answers,
        answer_provider=lambda request: agent.propose_contract_answers(
            request,
            project_root=project_root,
            audit_recorder=audit_recorder,
        ),
        audit_recorder=audit_recorder,
    )


def cmd_contract_prepare(args: argparse.Namespace, cfg: dict) -> None:
    from core.contract_check import (
        load_generated_answer_entries_for_contract,
        prepare_implementation_contract,
        render_contract_check,
        write_prepared_contract,
    )

    if args.auto and args.interactive:
        print("Failed to prepare contract: --auto cannot be combined with --interactive", file=sys.stderr)
        sys.exit(2)

    project_root = Path(cfg.get("project", {}).get("root_path") or Path.cwd()).resolve()
    task_path = _resolve_task_path(args.task_file, project_root)
    if task_path is None:
        print(f"Task file not found: {args.task_file}")
        sys.exit(1)
    if not task_path.is_file():
        print(f"Task path is not a file: {args.task_file}", file=sys.stderr)
        sys.exit(1)

    task_text = task_path.read_text(encoding="utf-8")
    project_context = _prepare_project_context_from_config(cfg)
    output_path = _resolve_output_path(args.output) if args.output else _default_contract_path(task_path, cfg)
    report_root = project_root if cfg.get("project", {}).get("root_path") else None
    report_dir = _resolve_contract_report_dir(cfg) if cfg.get("_config_path") else None
    generated_answer_entries = load_generated_answer_entries_for_contract(
        task_path,
        source_text=task_text,
        project_root=report_root,
        report_dir=report_dir,
    )
    if project_context is None or not project_context.get("validation_commands"):
        result = prepare_implementation_contract(
            task_text,
            contract_name=output_path.name,
            project_context=project_context,
            generated_answer_entries=generated_answer_entries,
        )
        if result.required_next_step == "provide_project_context":
            _print_contract_prepare_project_context_required(result, args.task_file)
            if output_path.exists():
                _print_existing_output_next_step_note(output_path)
            sys.exit(1)

    answers: dict[str, dict] = {}
    answers_supplied = bool(args.interactive or args.answers)
    existing_default_answers_path = None
    if args.interactive:
        try:
            first = prepare_implementation_contract(
                task_text,
                contract_name=task_path.name,
                project_context=project_context,
                generated_answer_entries=generated_answer_entries,
            )
            answers = _collect_prepare_answers_interactive(
                generated_by="sikula.contract_prepare",
                label="contract preparation",
                source_path=task_path,
                source_text=task_text,
                project_root=project_root,
                questions=first.user_questions,
                cfg=cfg,
                answers_path=_resolve_answers_path(args.answers) if args.answers else None,
            )
        except (EOFError, OSError, ValueError) as exc:
            print(f"Failed to collect contract answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.answers:
        try:
            answers = _load_prepare_answers(
                _resolve_answers_path(args.answers), source_path=task_path, source_text=task_text
            )
        except (OSError, ValueError) as exc:
            print(f"Failed to load contract answers: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.auto:
        existing_default_answers_path = _existing_prepare_answers_path(
            task_path,
            cfg,
            generated_by="sikula.contract_prepare",
        )
        if existing_default_answers_path:
            try:
                existing_answers_data = _load_prepare_answers_data(existing_default_answers_path)
            except (OSError, ValueError) as exc:
                print(f"Failed to inspect existing contract answers: {exc}", file=sys.stderr)
                sys.exit(1)
            if _filled_prepare_answers(existing_answers_data.get("answers")):
                print(
                    "Failed to auto-prepare contract: existing contract answers contain filled values; "
                    f"rerun with --answers {existing_default_answers_path}",
                    file=sys.stderr,
                )
                sys.exit(1)

    result = prepare_implementation_contract(
        task_text,
        contract_name=output_path.name,
        answers=answers,
        project_context=project_context,
        generated_answer_entries=generated_answer_entries,
    )
    if result.required_next_step == "provide_project_context":
        _print_contract_prepare_project_context_required(result, args.task_file)
        if output_path.exists():
            _print_existing_output_next_step_note(output_path)
        sys.exit(1)

    auto_answer_count = 0
    if args.auto and output_path.exists():
        print(f"Failed to prepare contract: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        _print_existing_output_hint(output_path)
        sys.exit(1)

    if args.auto and result.user_questions:
        try:
            auto_result = _run_contract_prepare_auto(
                args=args,
                cfg=cfg,
                project_root=project_root,
                source_path=task_path,
                task_text=task_text,
                output_path=output_path,
                project_context=project_context,
                generated_answer_entries=generated_answer_entries,
                answers=answers,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Failed to auto-prepare contract: {exc}", file=sys.stderr)
            sys.exit(1)
        result = auto_result.result
        answers = auto_result.answers
        auto_answer_count = len(auto_result.auto_answers)
        if args.answers and auto_answer_count:
            try:
                _write_prepare_answers_template(
                    generated_by="sikula.contract_prepare",
                    source_path=task_path,
                    source_text=task_text,
                    project_root=project_root,
                    questions=result.user_questions,
                    cfg=cfg,
                    answers=answers,
                    answers_path=_resolve_answers_path(args.answers),
                )
            except (OSError, ValueError) as exc:
                print(f"Failed to update contract answers: {exc}", file=sys.stderr)
                sys.exit(1)
        elif existing_default_answers_path and auto_answer_count and not result.needs_user_input:
            try:
                _write_prepare_answers_template(
                    generated_by="sikula.contract_prepare",
                    source_path=task_path,
                    source_text=task_text,
                    project_root=project_root,
                    questions=result.user_questions,
                    cfg=cfg,
                    answers=answers,
                )
            except (OSError, ValueError) as exc:
                print(f"Failed to update contract answers: {exc}", file=sys.stderr)
                sys.exit(1)

    if result.needs_user_input and not answers_supplied:
        answers_path = _write_prepare_answers_template(
            generated_by="sikula.contract_prepare",
            source_path=task_path,
            source_text=task_text,
            project_root=project_root,
            questions=result.user_questions,
            cfg=cfg,
            answers=answers if args.auto else None,
        )
        print("Contract preparation needs answers before writing an implementation contract.")
        print(f"Contract preparation answers template written: {answers_path}")
        if args.auto:
            print(f"Auto-applied answers: {auto_answer_count}")
        print(f"Applied answers: {len(result.answered_question_ids)}")
        print(f"Open questions: {len(result.open_question_ids)}")
        _print_open_question_details(result.user_questions)
        print("Next step:")
        print(f"- Fill the answers file, then run: sikula contract prepare {args.task_file} --answers {answers_path}")
        print(f"- Or answer in the terminal: sikula contract prepare {args.task_file} --interactive")
        if output_path.exists():
            _print_existing_output_next_step_note(output_path)
        sys.exit(1)

    if output_path.exists():
        print(f"Failed to prepare contract: refusing to overwrite existing output file: {output_path}", file=sys.stderr)
        _print_existing_output_hint(output_path)
        sys.exit(1)

    try:
        write_prepared_contract(
            result,
            output_path=output_path,
            project_root=report_root,
            report_dir=report_dir,
        )
    except (OSError, ValueError) as exc:
        print(f"Failed to prepare contract: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Implementation contract written: {output_path}")
    if args.auto:
        print(f"Auto-applied answers: {auto_answer_count}")
    print(f"Applied answers: {len(result.answered_question_ids)}")
    print(f"Open questions: {len(result.open_question_ids)}")
    print("")
    print(render_contract_check(result.recheck_result or result.check_result), end="")
    if result.ready_to_run:
        print("")
        print(f"Next step: sikula run {output_path}")
    elif result.needs_user_input:
        answers_path = (
            _resolve_answers_path(args.answers)
            if args.answers
            else _prepare_answers_path(
                task_path,
                cfg,
                generated_by="sikula.contract_prepare",
            )
        )
        print("")
        print("Next step:")
        print(f"- Fill/update the answers file: {answers_path}")
        print(
            "- Then rerun contract prepare with a new --output path, or remove/rename the output written above first."
        )
    else:
        print("")
        print(f"Next step: review the contract check output above before running sikula run {output_path}")


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
    from core.contract_check import check_contract_file

    try:
        result = check_contract_file(task_path, project_config=cfg or None)
        return _contract_preflight_snapshot(result, task_path, project_root)
    except Exception as exc:
        log.warning("Implementation contract preflight failed: %s", exc)
        return _contract_preflight_error_snapshot(task_path, project_root, exc)


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


def _task_audit_warnings(state) -> list[str]:
    warnings: list[str] = []
    for warning in getattr(state, "analyst_warnings", []):
        warnings.append(f"analyst: {_short_audit_line(warning)}")

    for entry in getattr(state, "history", []):
        if entry.get("action") == "write_path_warning":
            agent = entry.get("agent") or "agent"
            warnings.append(f"{agent}: {_short_audit_line(entry.get('result'))}")

    review_warning_count = sum(1 for record in getattr(state, "review_cycle_records", []) if record.get("has_warnings"))
    if review_warning_count:
        warnings.append(f"reviewer warnings: {review_warning_count}")

    security_warning_count = sum(
        1 for record in getattr(state, "security_review_cycle_records", []) if record.get("has_warnings")
    )
    if security_warning_count:
        warnings.append(f"security reviewer warnings: {security_warning_count}")

    testability_gaps = getattr(state, "testability_gaps", [])
    if testability_gaps:
        unique_count = len(_unique_testability_gaps(testability_gaps))
        detail = f"{len(testability_gaps)}"
        if unique_count != len(testability_gaps):
            detail += f" ({unique_count} unique)"
        warnings.append(f"testability gaps: {detail} (see: sikula show {state.task_id})")

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


def _testability_gap_sample_lines(state, limit: int = 3) -> list[str]:
    gaps = _unique_testability_gaps(getattr(state, "testability_gaps", []))
    lines = [_testability_gap_label(gap) for gap in gaps[:limit]]
    remaining = len(gaps) - limit
    if remaining > 0:
        lines.append(f"... {remaining} more unique gap(s) (see: sikula show {state.task_id})")
    return lines


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
        _print_limited_lines(warnings, state.task_id)
        gap_lines = _testability_gap_sample_lines(state)
        if gap_lines:
            print("Testability gaps:")
            _print_limited_lines(gap_lines, state.task_id, limit=len(gap_lines))

    if recovered:
        print("Recovered issues:")
        _print_limited_lines(recovered, state.task_id, limit=_RECOVERED_DIAGNOSTIC_LIMIT + 2)

    if failed:
        print("Failed issues:")
        _print_limited_lines(failed, state.task_id, limit=_RECOVERED_DIAGNOSTIC_LIMIT + 2)

    return len(warnings)


def cmd_run(args: argparse.Namespace, cfg: dict) -> None:
    from core.state import JsonStateStore

    build_tool = cfg.get("project", {}).get("build_tool")
    if build_tool not in _SUPPORTED_BUILD_TOOLS:
        supported = ", ".join(sorted(_SUPPORTED_BUILD_TOOLS))
        val = repr(build_tool) if build_tool else "not set"
        print(f"Unsupported build_tool: {val}. Set project.build_tool in .sikula/config.yaml to one of: {supported}")
        sys.exit(1)

    overrides: dict = {
        "run_build": args.build,
        "run_presync": args.presync,
        "run_planner": args.planner,
        "run_review": args.review,
        "run_security_review": args.security_review,
        "run_test_writing": args.test_writing,
        "run_tests": args.tests,
        "run_build_per_step": args.build_per_step,
        "run_checks": args.checks,
        "agent_llms": _parse_agent_llm_overrides(args.agent_model, args.agent_provider, args.agent_timeout),
    }
    if args.presync_clean is not None:
        overrides["presync_clean"] = args.presync_clean

    state_dir = _resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    isolate = not args.no_isolate
    original_project_root = Path(cfg["project"]["root_path"]).resolve()
    current_task_worktree_base = _sikula_worktree_base_for_path(Path.cwd())
    worktree_base: Path | None = None  # git root of the worktree (for git ops)
    leave_current_worktree_before_finalize = False
    already_terminal = False

    if args.reset_failed:
        if not args.task_id:
            print("--reset-failed requires --task-id")
            sys.exit(1)
        _reset_failed_state(args.task_id, cfg, store)

    t_start = time.time()

    if not args.task_file and getattr(args, "task_file_pos", None):
        args.task_file = args.task_file_pos

    if args.task_file:
        if current_task_worktree_base:
            print("Refusing to start a new task from inside a Sikula task worktree.")
            print("Run this command from the original project, or use 'sikula run --task-id <task-id>' to resume.")
            sys.exit(1)
        task_path = _resolve_task_path(args.task_file, original_project_root)
        if task_path is None:
            print(f"Task file not found: {args.task_file}")
            sys.exit(1)

        git_root = _find_git_root(original_project_root)
        if git_root is None:
            print(f"Error: project root is not inside a git repository: {original_project_root}")
            print("  Run 'git init && git add -A && git commit -m init' to initialize a repository.")
            sys.exit(1)
        if isolate:
            _require_committed_config_for_isolated_run(cfg, git_root)

        description = task_path.read_text().strip()
        state = store.create(description)
        state.task_file = Path(args.task_file).name
        state.config_snapshot = _run_config_snapshot(cfg, overrides)
        preflight_cfg = _contract_preflight_config(cfg, overrides)
        state.implementation_contract = _build_contract_preflight_snapshot(
            task_path, preflight_cfg, original_project_root
        )
        state.record("orchestrator", "contract_check", _contract_preflight_record_result(state.implementation_contract))
        store.save(state)
        _print_contract_preflight_summary(state.implementation_contract)
        gate_failures = _contract_readiness_gate_failures(
            state.implementation_contract,
            require_ready=bool(getattr(args, "require_contract_ready", False)),
            min_score=getattr(args, "min_contract_score", None),
        )
        if gate_failures:
            state.failed = True
            state.contract_gate_blocked = True
            state.record("orchestrator", "contract_gate_failed", "; ".join(gate_failures))
            store.save(state)
            _print_contract_readiness_gate_failure(state.implementation_contract, gate_failures, state.task_id)
            sys.exit(1)

        if isolate:
            branch = f"sikula/{_branch_stem(args.task_file)}-{state.task_id}"
            worktree_base = git_root / ".sikula" / "worktrees" / state.task_id
            # effective project root within the worktree mirrors the relative path from git root
            rel = original_project_root.relative_to(git_root)
            worktree_project_root = worktree_base / rel
            _ensure_gitignore(git_root)
            ok, err = _create_worktree(git_root, worktree_base, branch)
            if not ok:
                print(f"Failed to create git worktree: {err}")
                sys.exit(1)
            # Copy gitignored environment files that the build needs but are not tracked.
            for name in _build_tool_class(cfg).env_files():
                src = original_project_root / name
                dst = worktree_project_root / name
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
                    log.info("Copied %s to worktree", name)
            state.worktree_path = str(worktree_project_root)
            state.worktree_base = str(worktree_base)
            state.worktree_branch = branch
            store.save(state)
            log.info("Worktree created: %s (branch: %s)", worktree_base, branch)
            cfg["project"]["root_path"] = str(worktree_project_root)

        orch = build_orchestrator(cfg, overrides, state_store=store)
        state = orch.run(task_id=state.task_id, label=Path(args.task_file).name)

    elif args.task_id:
        state = store.load(args.task_id)
        if not state:
            print(f"Task {args.task_id} not found")
            sys.exit(1)
        already_terminal = state.done or state.failed
        is_review_fix_resume = state.review_mode == "review_fix"
        if state.review_mode == "review_report" and not already_terminal:
            print(f"Task {args.task_id} is a report-only review task and cannot be resumed.")
            print("Re-run 'sikula review' to start a fresh review.")
            sys.exit(1)
        if is_review_fix_resume:
            overrides["run_planner"] = False
            overrides["run_review"] = True
            if args.security_review is None and state.config_snapshot:
                saved_security_review = state.config_snapshot.get("run_security_review")
                if saved_security_review is not None:
                    overrides["run_security_review"] = saved_security_review

        if already_terminal:
            pass
        elif state.worktree_path:
            wt = Path(state.worktree_path)
            if wt.exists():
                worktree_base = Path(state.worktree_base) if state.worktree_base else wt
                if _path_is_within(Path.cwd(), worktree_base):
                    leave_current_worktree_before_finalize = True
                cfg["project"]["root_path"] = str(wt)
            else:
                print(f"Worktree no longer exists: {wt}")
                print("Delete the task state and re-run with --task-file, or restore the worktree manually.")
                sys.exit(1)
        elif state.worktree_branch and not state.done and not state.failed:
            print(f"Task {args.task_id} has no worktree path recorded.")
            print("It was likely cleaned up already, so it cannot be resumed safely.")
            print(f"Use 'sikula show {args.task_id}' for audit, or start a new task with --task-file.")
            sys.exit(1)

        orch = build_orchestrator(cfg, overrides, state_store=store)
        state = orch.run(task_id=args.task_id)

    else:
        raise AssertionError("unreachable — task_file/task_id check is in main()")

    total_s = time.time() - t_start

    if worktree_base and state.done:
        if leave_current_worktree_before_finalize:
            os.chdir(original_project_root)
        git_root = _find_git_root(original_project_root) or original_project_root
        commit_msg = None
        if state.review_mode == "review_fix" and state.worktree_branch:
            commit_msg = f"sikula: review fixes for {state.worktree_branch}\n\nTask ID: {state.task_id}"
        success, committed, _ = _finalize_worktree(worktree_base, git_root, state, commit_msg=commit_msg)
        store.save(state)
        if success:
            state.worktree_path = None
            state.worktree_base = None
            store.save(state)
            if committed:
                log.info("Changes committed to branch %s", state.worktree_branch)
            log.info("Worktree removed: %s", worktree_base)
        else:
            log.warning("Could not finalize worktree — inspect manually: %s", worktree_base)
    elif worktree_base and not state.done:
        log.info("Worktree preserved for inspection/resume: %s", worktree_base)

    longest_label, longest_s = "-", 0.0
    for h in state.history:
        dur = h.get("elapsed_s", 0.0)
        if dur > longest_s:
            longest_s = dur
            longest_label = f"{h['agent']}/{h['action']}"

    max_iter = cfg.get("sandbox", {}).get("max_iterations", 10)
    audit_warning_count = len(_task_audit_warnings(state))
    if state.done:
        status = f"✓ DONE with warnings ({audit_warning_count})" if audit_warning_count else "✓ DONE"
    elif state.failed:
        status = "✗ FAILED"
    else:
        status = "⚠ INCOMPLETE"
    print(f"\nTask {state.task_id}: {status}")
    if already_terminal:
        if state.done:
            print("This task is already complete; no work was run.")
        else:
            print("This task has failed; no work was run.")
            if _contract_gate_blocked_without_worktree(state):
                print("The contract readiness gate blocked delivery before a worktree was created.")
                print(f"Suggested next step: {_contract_gate_next_action(state)}")
            else:
                print(f"Use --reset-failed to retry: sikula run --task-id {state.task_id} --reset-failed")
        print()
        print("Previous run:")
    else:
        print(f"Total time:      {_fmt_time(total_s)}")
    if longest_s > 0:
        print(f"Longest phase:   {longest_label} ({_fmt_time(longest_s)})")
    print(f"Build attempts:  {state.build_iterations} total (max {max_iter}/loop)")
    print(f"Total phases:    {len(state.history)}")
    if state.worktree_branch:
        print(f"Branch:          {state.worktree_branch}")
    if state.files_changed:
        print("Files changed:")
        for f in state.files_changed:
            print(f"  {f}")
    _print_task_audit_report(state)
    if state.errors:
        print(f"Errors:          {len(state.errors)} remaining (see: sikula show {state.task_id})")

    sys.exit(0 if state.done else 1)


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _status_label(state) -> str:
    if state.done:
        return "DONE"
    if state.failed:
        return "FAILED"
    if state.worktree_branch and not state.worktree_path:
        return "CLEANED"
    if state.active_operation and _active_operation_is_fresh(state.active_operation):
        return _active_operation_label(state.active_operation)
    if state.pid and not _pid_running(state.pid):
        return "INTERRUPTED"
    if state.active_operation:
        return _active_operation_label(state.active_operation)
    final_scope = state.active_scope == "final_full_task"
    if state.build_status == "failed":
        return "final build failed" if final_scope else "build failed"
    if state.build_iterations and state.build_status != "success":
        return "final building" if final_scope else "building"
    if state.tests_up_to_date:
        return "final validation" if final_scope else "testing"
    if state.security_approved:
        return "final test writing" if final_scope else "writing tests"
    if state.review_approved:
        return "final security review" if final_scope else "security review"
    if state.files_changed:
        return "final review" if final_scope else "reviewing"
    if state.plan_decided:
        return "implementing"
    if state.presync_done:
        return "analyzing"
    return "starting"


def _active_operation_label(active_operation: dict) -> str:
    agent = active_operation.get("agent")
    if agent:
        return str(agent)
    phase = str(active_operation.get("phase", "running"))
    if active_operation.get("scope") == "final_full_task":
        return f"final {phase}"
    return phase


def _active_operation_is_fresh(active_operation: dict) -> bool:
    last_heartbeat_at = active_operation.get("last_heartbeat_at")
    try:
        from datetime import datetime, timezone

        last_heartbeat = datetime.fromisoformat(last_heartbeat_at)
        if last_heartbeat.tzinfo is None:
            last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
        age_s = max(0, int((datetime.now(timezone.utc) - last_heartbeat.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return False
    interval_s = int(active_operation.get("heartbeat_interval_seconds") or 60)
    return age_s <= max(120, interval_s * 2 + 10)


def _active_operation_elapsed(active_operation: dict | None) -> str | None:
    if not active_operation:
        return None
    started_at = active_operation.get("started_at")
    try:
        from datetime import datetime, timezone

        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        return f"{elapsed // 60}m"
    return f"{elapsed // 3600}h {elapsed % 3600 // 60}m"


def _status_step(state) -> str:
    if not state.plan:
        return "-"
    total = len(state.plan)
    current = max(1, min(state.current_step + 1, total))
    return f"{current}/{total}"


def _status_updated(state) -> str:
    try:
        from datetime import datetime, timezone

        updated = datetime.fromisoformat(state.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return "-"
    if elapsed < 60:
        return f"{elapsed}s ago"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"


def _status_next_action(state, status: str) -> str:
    if status == "DONE":
        return "review branch" if state.worktree_branch else "review changes"
    if status == "FAILED":
        if _contract_gate_blocked_without_worktree(state):
            return _contract_gate_next_action(state)
        return f"sikula run --task-id {state.task_id} --reset-failed"
    if status == "CLEANED":
        return f"sikula show {state.task_id}"
    if status == "INTERRUPTED":
        return f"sikula run --task-id {state.task_id}"
    if state.active_operation and _active_operation_is_fresh(state.active_operation):
        return "wait"
    return "wait" if state.pid and _pid_running(state.pid) else f"sikula run --task-id {state.task_id}"


def _status_row(state) -> dict:
    status = _status_label(state)
    task_label = state.task_file
    if not task_label:
        task_label = state.task_description.splitlines()[0][:60] if state.task_description else "(no description)"
    row = {
        "id": state.task_id,
        "status": status,
        "step": _status_step(state),
        "build": state.build_iterations if state.build_iterations else None,
        "updated": state.updated_at,
        "updated_human": _status_updated(state),
        "task": task_label,
        "next_action": _status_next_action(state, status),
    }
    if state.active_operation and (status != "INTERRUPTED" or _active_operation_is_fresh(state.active_operation)):
        row["active_operation"] = state.active_operation
        row["active_elapsed"] = _active_operation_elapsed(state.active_operation)
    return row


def _status_matches(row: dict, filters: set[str]) -> bool:
    if not filters:
        return True
    status = row["status"].lower().replace(" ", "_")
    if status in filters:
        return True
    if "active" in filters and row["status"] not in {"DONE", "FAILED", "CLEANED"}:
        return True
    return False


def cmd_status(cfg: dict, args: argparse.Namespace | None = None) -> None:
    from core.state import JsonStateStore

    args = args or argparse.Namespace(json=False, verbose=False, status_filter=[])
    state_dir = _resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    tasks = store.list_tasks()
    if not tasks:
        if args.json:
            print("[]")
        else:
            print("No tasks yet.")
        return
    states = [s for tid in tasks if (s := store.load(tid)) is not None]
    states.sort(key=lambda s: s.created_at)
    filters = {f.lower().replace("-", "_") for f in args.status_filter}
    rows = [row for s in states if _status_matches((row := _status_row(s)), filters)]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No matching tasks.")
        return
    if args.verbose:
        print(f"{'ID':<32}  {'STATUS':<16}  {'STEP':>5}  {'BUILD':>5}  {'UPDATED':>8}  TASK")
        for row in rows:
            build_col = str(row["build"]) if row["build"] is not None else "-"
            print(
                f"{row['id']:<32}  {row['status']:<16}  {row['step']:>5}  "
                f"{build_col:>5}  {row['updated_human']:>8}  {row['task']}"
            )
            if row.get("active_operation"):
                active = row["active_operation"]
                elapsed = row.get("active_elapsed") or "-"
                message = active.get("message") or row["status"]
                print(f"{'':<32}  active: {message} ({elapsed})")
            print(f"{'':<32}  next: {row['next_action']}")
        return
    print(f"{'ID':<32}  {'STATUS':<16}  {'STEP':>5}  {'BUILD':>5}  {'UPDATED':>8}  TASK")
    for row in rows:
        build_col = str(row["build"]) if row["build"] is not None else "-"
        print(
            f"{row['id']:<32}  {row['status']:<16}  {row['step']:>5}  "
            f"{build_col:>5}  {row['updated_human']:>8}  {row['task']}"
        )


def cmd_show(task_id: str, cfg: dict) -> None:
    from core.state import JsonStateStore

    state_dir = _resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = store.load(task_id)
    if not state:
        print(f"Task {task_id} not found")
        sys.exit(1)
    print(json.dumps(state.__dict__, indent=2))


def cmd_cleanup(args: argparse.Namespace, cfg: dict) -> None:
    """Remove a task worktree, optionally deleting the persisted state as well."""
    from core.state import JsonStateStore

    state_dir = _resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = store.load(args.task_id)
    if not state:
        print(f"Task {args.task_id} not found")
        sys.exit(1)

    action = "delete" if args.delete_state else "cleanup"
    dry_run = not args.force
    removed_worktree = False
    clear_worktree_refs = False

    print(f"Task {state.task_id}: {action.upper()}{' (dry run)' if dry_run else ''}")

    if state.worktree_base or state.worktree_path:
        worktree_base = Path(state.worktree_base or state.worktree_path)
        if worktree_base.exists():
            if not dry_run and _path_is_within(Path.cwd(), worktree_base):
                print(f"Refusing to remove the current working tree: {worktree_base}")
                print("Run this command from the original project or another directory.")
                sys.exit(1)
            dirty = _worktree_dirty(worktree_base)
            if dirty and not dry_run and not args.discard:
                print(f"Worktree has uncommitted changes: {worktree_base}")
                print("Refusing to remove it. Re-run with --discard to delete those changes.")
                sys.exit(1)
            if dry_run:
                print(f"Would remove worktree: {worktree_base}")
                if dirty:
                    print("Worktree has uncommitted changes; applying this cleanup requires --discard.")
            else:
                git_root = (
                    _find_git_root(Path(cfg["project"]["root_path"]).resolve())
                    or Path(cfg["project"]["root_path"]).resolve()
                )
                if not _remove_worktree(worktree_base, git_root, force=args.discard):
                    print(f"Failed to remove worktree: {worktree_base}")
                    sys.exit(1)
                removed_worktree = True
                clear_worktree_refs = True
                print(f"Removed worktree: {worktree_base}")
        else:
            print(f"Worktree already missing: {worktree_base}")
            clear_worktree_refs = True
    else:
        print("Task has no isolated worktree recorded.")

    if args.delete_state:
        if dry_run:
            print(f"Would delete state: {state_dir / (state.task_id + '.json')}")
        else:
            store.delete(state.task_id)
            print(f"Deleted state: {state.task_id}")
    elif not dry_run:
        store.delete_text_snapshots(state.task_id)
        state.record(
            "sikula",
            "cleanup",
            "worktree removed" if removed_worktree else "worktree already missing or not recorded",
        )
        if clear_worktree_refs:
            state.worktree_path = None
            state.worktree_base = None
        store.save(state)

    if dry_run:
        print("No changes made. Re-run with --force to apply.")


def _print_review_summary(
    state, branch: str, base_branch: str, total_s: float, run_security_review: bool = True
) -> None:
    print(f"\n{'=' * 60}")
    print(f"Review:  {branch}  vs  {base_branch}")
    print(f"Files:   {len(state.files_changed)} changed")
    print(f"Time:    {_fmt_time(total_s)}")
    print(f"{'=' * 60}")
    approved = state.review_approved and (state.security_approved if run_security_review else True)
    print(f"Result:  {'APPROVED' if approved else 'ISSUES FOUND'}")
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


def cmd_review(args: argparse.Namespace, cfg: dict) -> None:
    """Checkout an existing branch in a worktree and run code + security review."""
    import uuid

    from core.state import JsonStateStore, TaskState, runtime_metadata_snapshot

    build_tool = cfg.get("project", {}).get("build_tool")
    if args.fix and build_tool not in _SUPPORTED_BUILD_TOOLS:
        supported = ", ".join(sorted(_SUPPORTED_BUILD_TOOLS))
        val = repr(build_tool) if build_tool else "not set"
        print(f"Unsupported build_tool: {val}. Set project.build_tool in .sikula/config.yaml to one of: {supported}")
        sys.exit(1)

    if args.description and args.description_file:
        print("Error: use either --description or --description-file, not both.")
        sys.exit(1)

    if args.description_file:
        desc_path = Path(args.description_file)
        if not desc_path.is_absolute():
            desc_path = Path.cwd() / desc_path
        if not desc_path.exists():
            print(f"Description file not found: {desc_path}")
            sys.exit(1)
        description = desc_path.read_text().strip()
    elif args.description:
        description = args.description.strip()
    else:
        print("Error: sikula review requires --description or --description-file.")
        print("The description is used as the review scope; without it the reviewer has to guess intent.")
        sys.exit(1)

    if not description:
        print("Error: review description is empty.")
        sys.exit(1)

    branch = args.branch
    base_branch = args.base_branch
    original_project_root = Path(cfg["project"]["root_path"]).resolve()

    git_root = _find_git_root(original_project_root)
    if git_root is None:
        print(f"Error: project root is not inside a git repository: {original_project_root}")
        print("  Run 'git init' first.")
        sys.exit(1)

    _ensure_gitignore(git_root)
    task_id = uuid.uuid4().hex
    worktree_base = git_root / ".sikula" / "worktrees" / task_id
    worktree_base.parent.mkdir(parents=True, exist_ok=True)

    if args.fix:
        # Fix mode writes commits back to the branch — use a real branch checkout so that
        # _finalize_worktree advances the branch ref.  If the branch is already checked out
        # in the caller's worktree, git worktree add will fail with a clear error; the user
        # should switch away first (e.g. git checkout main).
        r = subprocess.run(
            ["git", "worktree", "add", str(worktree_base), branch],
            capture_output=True,
            text=True,
            cwd=git_root,
        )
        if r.returncode != 0:
            print(_worktree_error_message(branch, r.stderr.strip()))
            sys.exit(1)
    else:
        # Report-only mode never commits — use detached HEAD so that the review can run
        # even when the caller is currently on the branch being reviewed.
        sha_r = subprocess.run(
            ["git", "rev-parse", branch],
            capture_output=True,
            text=True,
            cwd=git_root,
        )
        if sha_r.returncode != 0:
            print(f"Branch '{branch}' not found: {sha_r.stderr.strip()}")
            sys.exit(1)
        r = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_base), sha_r.stdout.strip()],
            capture_output=True,
            text=True,
            cwd=git_root,
        )
        if r.returncode != 0:
            print(_worktree_error_message(branch, r.stderr.strip()))
            sys.exit(1)

    log.info("Worktree created: %s (branch: %s)", worktree_base, branch)

    rel = original_project_root.relative_to(git_root)
    worktree_project_root = worktree_base / rel

    if args.fix:
        # Copy gitignored environment files the build needs (e.g. local.properties on Android).
        for name in _build_tool_class(cfg).env_files():
            src = original_project_root / name
            dst = worktree_project_root / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                log.info("Copied %s to worktree", name)

    # Compute three-dot diff: all commits introduced by branch vs base
    diff_r = subprocess.run(
        ["git", "diff", f"{base_branch}...{branch}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if diff_r.returncode != 0:
        print(f"Failed to compute diff between '{base_branch}' and '{branch}': {diff_r.stderr.strip()}")
        subprocess.run(["git", "worktree", "remove", str(worktree_base)], cwd=git_root, check=False)
        sys.exit(1)
    review_diff = diff_r.stdout

    files_r = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...{branch}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    files_changed = (
        [line.strip() for line in files_r.stdout.splitlines() if line.strip()] if files_r.returncode == 0 else []
    )

    if not files_changed:
        print(f"No files changed between '{base_branch}' and '{branch}'")
        subprocess.run(["git", "worktree", "remove", str(worktree_base)], cwd=git_root, check=False)
        sys.exit(0)

    state_dir = _resolve_state_dir(cfg)
    store = JsonStateStore(state_dir)
    state = TaskState(
        task_id=task_id,
        task_description=description,
        implementation_prompt=description,
        files_changed=files_changed,
        review_diff=review_diff,
        review_mode="review_fix" if args.fix else "review_report",
        review_base_branch=base_branch,
        plan_decided=True,
        worktree_path=str(worktree_project_root),
        worktree_base=str(worktree_base),
        worktree_branch=branch,
        runtime_metadata=runtime_metadata_snapshot(),
    )
    store.save(state)
    task_label = Path(args.description_file).name if args.description_file else description.splitlines()[0][:60]

    t_start = time.time()
    cli_security_review = getattr(args, "security_review", None)
    run_security_review = cfg.get("run_security_review", True) if cli_security_review is None else cli_security_review
    heartbeat_interval_seconds = _heartbeat_interval_seconds(cfg)

    base_llm_cfg = cfg.get("llm", {})

    # Analyst is skipped in review mode — enrich implementation_prompt with any design/spec
    # files referenced by name in the task description so reviewer and fixer have visual context.
    from core.llm_client import create_llm_client as _create_llm_client

    _enrichment_llm = _create_llm_client(
        _make_llm_config(base_llm_cfg, cfg.get("agents", {}).get("analyst", {}).get("llm", {}))
    )
    _extra = _enrich_prompt_with_referenced_files(description, _enrichment_llm, worktree_project_root)
    if _extra:
        state.implementation_prompt = description + "\n\n---\n\nFiles referenced in the task:\n\n" + _extra
        store.save(state)
        log.info("implementation_prompt enriched with design file contents")

    if args.fix:
        # Fix mode: full orchestrator loop — review, fix, build, checks all per config.
        # Only planner is always disabled (no planning needed for an existing branch).
        cfg["project"]["root_path"] = str(worktree_project_root)

        overrides = {
            "run_planner": False,
            "run_review": True,
            "run_security_review": run_security_review,
            "agent_llms": _parse_agent_llm_overrides(
                getattr(args, "agent_model", None),
                getattr(args, "agent_provider", None),
                getattr(args, "agent_timeout", None),
            ),
        }
        orch = build_orchestrator(cfg, overrides, state_store=store)
        state = orch.run(task_id=task_id, label=task_label)
        total_s = time.time() - t_start

        if state.done:
            fix_msg = f"sikula: review fixes for {branch}\n\nTask ID: {state.task_id}"
            success, committed, _ = _finalize_worktree(worktree_base, git_root, state, commit_msg=fix_msg)
            store.save(state)
            if success:
                if committed:
                    log.info("Changes committed to branch %s", branch)
                else:
                    log.info("No fixes needed — worktree removed")
            else:
                log.warning("Could not finalize worktree — inspect manually: %s", worktree_base)
        else:
            log.info("Worktree preserved for inspection/resume: %s", worktree_base)
    else:
        # Report-only mode: run agents directly without modifying the branch
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
        subprocess.run(["git", "worktree", "remove", str(worktree_base)], cwd=git_root, check=False)

    approved = state.review_approved and (state.security_approved if run_security_review else True)
    _print_review_summary(state, branch, base_branch, total_s, run_security_review=run_security_review)
    sys.exit(0 if approved else 1)


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
    if guidelines_files:
        guidelines_block = "  context_files:\n" + "\n".join(f"    - {f}" for f in guidelines_files)
    else:
        guidelines_block = "  context_files: []"

    wp = write_paths or ["src/"]
    twp = test_write_paths or ["tests/"]
    wp_list = "\n".join(f"    - {p}" for p in wp)
    twp_list = "\n".join(f"    - {p}" for p in twp)
    write_paths_comment = "" if write_paths else "  # TODO: restrict to dirs agents may write production code to.\n"
    test_paths_comment = "" if test_write_paths else "  # TODO: restrict to dirs the test writer may write to.\n"

    provider_comment = "" if provider else "  # TODO: change to your provider (codex/claude/gemini/opencode)"
    model_comment = "" if model else "  # TODO: change to your model"
    agent_model_comment = "" if model else "  # TODO: consider a stronger model"

    build_section = ""
    if build_tool == "cargo":
        build_section = """\
build:
  compile_command: "cargo check"
  test_command: "cargo test"
  timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Cargo.lock.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
  checks:
    - name: clippy
      command: "cargo clippy -- -D warnings"
      timeout: 120
    - name: fmt
      command: "cargo fmt --check"
      fix_command: "cargo fmt"
      timeout: 60
"""
    elif build_tool == "python":
        build_section = """\
build:
  compile_command: "python3 -m compileall -q ."
  test_command: "python3 -m pytest"
  timeout: 300
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # PythonTool has no built-in lockfile default, so use this for intentional generated sync outputs.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
  checks:
    - name: ruff-check
      command: "python3 -m ruff check ."
      timeout: 60
    - name: ruff-format
      command: "python3 -m ruff format --check ."
      fix_command: "python3 -m ruff format ."
      timeout: 60
"""
    elif build_tool == "node":
        package_manager = node_package_manager or "npm"
        sync_command = node_sync_command or (
            "bun install --frozen-lockfile" if package_manager == "bun" else f"{package_manager} install"
        )
        compile_command = node_compile_command or (
            "bun run build" if package_manager == "bun" else f"{package_manager} run build"
        )
        test_command = node_test_command or ("bun run test" if package_manager == "bun" else f"{package_manager} test")
        checks = node_checks or []
        if checks:
            check_lines: list[str] = ["  checks:"]
            for check in checks:
                check_lines.append(f"    - name: {check['name']}")
                check_lines.append(f'      command: "{check["command"]}"')
                if "fix_command" in check:
                    check_lines.append(f'      fix_command: "{check["fix_command"]}"')
                check_lines.append(f"      timeout: {check.get('timeout', 120)}")
            checks_block = "\n".join(check_lines)
        else:
            checks_block = "  checks: []"
        build_section = f"""\
build:
  package_manager: {package_manager}
  sync_command: "{sync_command}"
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked package-manager lockfiles.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/api/"
  #   - "schema/generated/**/*.json"
  compile_command: "{compile_command}"
  test_command: "{test_command}"
  sync_timeout: 600
  compile_timeout: 600
  test_timeout: 600
{checks_block}
"""
    elif build_tool == "gradle-android":
        build_section = """\
build:
  # Gradle task run by generate_sources() in the presync phase (run_presync: true).
  # Use openApiGenerateAll if generateDebugSources fails due to pre-existing compile errors.
  presync_task: generateDebugSources
  presync_clean: true
  # assembleDebug catches Kotlin errors + resource errors (R class, strings.xml, layouts).
  # Switch to compileDebugKotlin for faster builds on pure Kotlin tasks.
  # TODO: verify these tasks exist in your project (run: ./gradlew tasks).
  compile_task: assembleDebug
  test_task: testDebugUnitTest
  sync_timeout: 1800
  compile_timeout: 1800
  test_timeout: 1800
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Gradle lock/verification metadata.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "app/src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "gradle-jvm":
        build_section = """\
build:
  # 'classes' compiles all sources and triggers annotation processors (Lombok, MapStruct, etc.).
  # Switch to compileKotlin or compileJava for faster builds if your project has no codegen.
  compile_task: classes
  test_task: test
  sync_task: classes
  presync_task: classes
  compile_timeout: 600
  test_timeout: 600
  sync_timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Gradle lock/verification metadata.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "maven":
        build_section = """\
build:
  # Uses ./mvnw if present, falls back to mvn on PATH.
  # Override compile_command / test_command to customize (e.g. add -DskipTests=true).
  compile_timeout: 600
  test_timeout: 600
  sync_timeout: 300
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Maven has no built-in lockfile default, so use this for intentional generated sync outputs.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "xcodebuild":
        scheme_val = xcode_scheme if xcode_scheme else "TODO"
        scheme_comment = "" if xcode_scheme else "  # TODO: set scheme to match your Xcode project.\n"
        build_section = f"""\
build:
{scheme_comment}  scheme: "{scheme_val}"
  destination: "generic/platform=iOS Simulator"
  # TODO: set name to a simulator available on your machine (run: xcrun simctl list devices).
  test_destination: "platform=iOS Simulator,OS=latest,name=iPhone 16"
  compile_timeout: 1800
  test_timeout: 1800
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked SwiftPM Package.resolved.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "Sources/Generated/"
  #   - "schema/generated/**/*.json"
"""
    else:
        build_section = """\
build:
  # TODO: configure compile and test commands for your project.
  compile_command: "TODO"
  test_command: "TODO"
  timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
"""

    platform_line = f"  platform: {platform}\n" if platform else ""
    language_line = f"  language: {language}\n" if language else "  language: TODO\n"
    if build_tool == "gradle-android":
        ui_line = (
            "  # TODO: set ui to your UI framework — e.g. 'Jetpack Compose (Material 3)' "
            "or 'XML layouts'\n  # ui: Jetpack Compose (Material 3)\n"
        )
    elif build_tool == "xcodebuild":
        ui_line = "  # TODO: set ui to your UI framework — e.g. 'SwiftUI' or 'UIKit'\n  # ui: SwiftUI\n"
    else:
        ui_line = ""
    presync_line = (
        "run_presync: true" if build_tool in ("gradle-android", "gradle-jvm", "maven") else "run_presync: false"
    )

    return f"""\
# Sikula project configuration — generated by `sikula init`.
# Review any TODO comments before running your first task.

project:
  name: {project_name}
  root_path: .
  build_tool: {build_tool or "TODO"}
{language_line}{platform_line}{ui_line}
sandbox:
{write_paths_comment}  allowed_write_paths:
{wp_list}
{test_paths_comment}  allowed_test_write_paths:
{twp_list}
  allowed_read_paths:
    - .
  max_iterations: 10
  max_review_iterations: 3
  max_security_review_iterations: 3

tasks:
  task_description_dir: .sikula/tasks/
  contract_dir: .sikula/contracts/
  contract_report_dir: .sikula/contract-reports/
  refined_suffix: .refined.md
  contract_suffix: .contract.md
  state_dir: .sikula/state/

progress:
  heartbeat_interval_seconds: 60

llm:
  provider: {provider or "codex"}{provider_comment}
  model: {model or "gpt-5.3-codex"}{model_comment}
  agent_timeout: 1800

agents:
  analyst:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}
  reviewer:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}
  security_reviewer:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}

run_planner: true
{presync_line}
run_build_per_step: false
run_review: true
run_security_review: true
run_build: true
run_test_writing: true
run_tests: true
run_checks: true

{build_section}
security:
  # Optional: describe what this application does, what data it handles, and who the users
  # are. The security reviewer uses this to focus on relevant threat categories.
  # Example: "Mobile app. Handles user auth tokens stored in EncryptedSharedPreferences.
  #   Network calls go to our own backend — responses are semi-trusted. No PII beyond email."
  context: ""

guidelines:
{guidelines_block}
  max_file_chars: 30000

planner:
  max_steps: 6

test_writer:
  # Minimum branch+line coverage target within the configured test surface (percentage).
  coverage_target: 90
  # Test surface policy:
  # existing_infrastructure = stay within existing project test infra; missing heavy
  # UI/browser/device/runtime harnesses are not gaps by themselves.
  # complete = opt in to TESTABILITY GAP reports when important behaviour needs missing
  # test infra outside the existing surface.
  test_surface_policy: existing_infrastructure
  # What to do when safe tests require missing project seams/infrastructure:
  # warn = record a visible audit warning; fail = fail the task.
  testability_gap_policy: warn
"""


def _load_init_config(path: Path, *, strict: bool = False) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        if strict:
            print(f"Could not read config: {path}")
            print(f"  {e}")
            sys.exit(1)
        return {}
    except yaml.YAMLError as e:
        if strict:
            print(f"Invalid config YAML: {path}")
            print(f"  {e}")
            sys.exit(1)
        return {}
    return data if isinstance(data, dict) else {}


def _init_llm_value(args: argparse.Namespace, existing_cfg: dict, key: str) -> str | None:
    value = getattr(args, key, None)
    if value:
        return value
    llm_cfg = existing_cfg.get("llm", {})
    return llm_cfg.get(key) if isinstance(llm_cfg, dict) else None


def _init_tech_stack(existing_cfg: dict) -> str:
    project_cfg = existing_cfg.get("project", {})
    if not isinstance(project_cfg, dict):
        return "software"
    parts = [
        project_cfg.get("language"),
        project_cfg.get("platform"),
        project_cfg.get("build_tool"),
    ]
    return "/".join(str(p) for p in parts if p) or "software"


def _config_references_guidelines(config_path: Path, guidelines_ref: str) -> bool:
    cfg = _load_init_config(config_path)
    guidelines_cfg = cfg.get("guidelines", {})
    if not isinstance(guidelines_cfg, dict):
        return False
    context_files = guidelines_cfg.get("context_files", [])
    return isinstance(context_files, list) and guidelines_ref in context_files


def _insert_guidelines_reference(config_path: Path, guidelines_ref: str) -> bool:
    """Add guidelines_ref to guidelines.context_files with a minimal text edit."""
    if _config_references_guidelines(config_path, guidelines_ref):
        return False

    text = config_path.read_text()
    lines = text.splitlines(keepends=True)
    newline = "\n"

    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    guidelines_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip().split("#", 1)[0].strip() == "guidelines:":
            guidelines_idx = idx
            break

    if guidelines_idx is None:
        suffix = "" if text.endswith(("\n", "\r\n")) or not text else newline
        block = f"{suffix}guidelines:{newline}  context_files:{newline}    - {guidelines_ref}{newline}"
        config_path.write_text(text + block)
        return True

    guidelines_indent = _indent(lines[guidelines_idx])
    block_end = len(lines)
    for idx in range(guidelines_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#") and _indent(lines[idx]) <= guidelines_indent:
            block_end = idx
            break

    context_idx: int | None = None
    for idx in range(guidelines_idx + 1, block_end):
        if lines[idx].lstrip().startswith("context_files:"):
            context_idx = idx
            break

    if context_idx is None:
        item = f"{' ' * (guidelines_indent + 2)}context_files:{newline}"
        item += f"{' ' * (guidelines_indent + 4)}- {guidelines_ref}{newline}"
        lines.insert(guidelines_idx + 1, item)
        config_path.write_text("".join(lines))
        return True

    prefix, _, suffix = lines[context_idx].partition("context_files:")
    inline_value = suffix.split("#", 1)[0].strip()
    item_indent = _indent(lines[context_idx]) + 2
    if inline_value:
        cfg = _load_init_config(config_path)
        existing = cfg.get("guidelines", {}).get("context_files", []) if isinstance(cfg.get("guidelines"), dict) else []
        if not isinstance(existing, list):
            existing = []
        refs = [guidelines_ref] + [str(ref) for ref in existing if ref != guidelines_ref]
        replacement = f"{prefix}context_files:{newline}"
        replacement += "".join(f"{' ' * item_indent}- {ref}{newline}" for ref in refs)
        lines[context_idx] = replacement
        config_path.write_text("".join(lines))
        return True

    lines.insert(context_idx + 1, f"{' ' * item_indent}- {guidelines_ref}{newline}")
    config_path.write_text("".join(lines))
    return True


def _generate_guidelines_for_init(project_root: Path, tech: str, provider: str, model: str) -> str:
    from agents.init_agent import InitAgent
    from core.llm_client import LLMConfig, create_llm_client

    llm_cfg = LLMConfig(provider=provider, model=model)
    llm = create_llm_client(llm_cfg)
    agent = InitAgent(llm, tech)
    return agent.generate_guidelines(project_root)


def _cmd_init_guidelines_only(args: argparse.Namespace, project_root: Path, config_path: Path) -> None:
    existing_cfg = _load_init_config(config_path, strict=True)
    provider = _init_llm_value(args, existing_cfg, "provider")
    model = _init_llm_value(args, existing_cfg, "model")
    if not provider or not model:
        print("--provider and --model are required when using --guidelines unless llm.provider/model exist in config")
        print("  e.g. sikula init --guidelines --provider codex --model gpt-5.5")
        sys.exit(1)

    sikula_dir = project_root / ".sikula"
    sikula_dir.mkdir(exist_ok=True)
    print(f"Config already exists: {config_path}")
    print("Generating guidelines without rewriting the existing config ...")
    _ensure_provider_gitignore_entry(project_root, provider)
    try:
        guidelines_content = _generate_guidelines_for_init(
            project_root, _init_tech_stack(existing_cfg), provider, model
        )
    except RuntimeError as e:
        print(f"Warning: guidelines generation failed: {e}")
        return

    gl_path = sikula_dir / "guidelines.md"
    gl_path.write_text(guidelines_content)
    guidelines_ref = ".sikula/guidelines.md"
    updated = _insert_guidelines_reference(config_path, guidelines_ref)
    print("  Generated  : .sikula/guidelines.md")
    if updated:
        print("  Updated    : .sikula/config.yaml (guidelines.context_files)")
    else:
        print("  Config     : already references .sikula/guidelines.md")


def cmd_init(args: argparse.Namespace) -> None:
    from tools.scanner import scan

    project_root = Path.cwd()
    _load_project_env(project_root)
    sikula_dir = project_root / ".sikula"
    config_path = sikula_dir / "config.yaml"
    existing_cfg = _load_init_config(config_path) if config_path.exists() else {}

    if config_path.exists() and args.guidelines and not args.force:
        _cmd_init_guidelines_only(args, project_root, config_path)
        return

    if config_path.exists() and not args.force:
        print(f"Config already exists: {config_path}")
        print("Use --force to overwrite.")
        sys.exit(1)

    provider = _init_llm_value(args, existing_cfg, "provider")
    model = _init_llm_value(args, existing_cfg, "model")
    if args.guidelines and (not provider or not model):
        print("--provider and --model are both required when using --guidelines")
        print("  e.g. sikula init --guidelines --provider codex --model gpt-5.5")
        sys.exit(1)

    print(f"Scanning {project_root} ...")
    result = scan(project_root)

    if result.ambiguous_tools:
        print(f"Multiple build tools detected: {', '.join(result.ambiguous_tools)}")
        print(f"Defaulting to: {result.build_tool} — edit .sikula/config.yaml to change.")

    if result.build_tool:
        tech = f"{result.language}/{result.build_tool}" if result.language else result.build_tool
        print(f"  build_tool : {result.build_tool}")
        print(f"  language   : {result.language}")
        if result.platform:
            print(f"  platform   : {result.platform}")
        if result.build_tool == "node" and result.package_manager:
            print(f"  package manager: {result.package_manager}")
    else:
        tech = "software"
        print("  No build tool detected — config will need manual setup.")

    if result.guidelines_files:
        print(f"  guidelines : {', '.join(result.guidelines_files)}")

    guidelines_content: str | None = None
    if args.guidelines:
        print("Generating guidelines (this may take a moment) ...")
        try:
            guidelines_content = _generate_guidelines_for_init(project_root, tech, provider, model)
        except RuntimeError as e:
            print(f"Warning: guidelines generation failed: {e}")
            print("Continuing without generated guidelines.")

    if _find_git_root(project_root) is None:
        print("Warning: not inside a git repository — git is required to run tasks.")
        print("  Run 'git init && git add -A && git commit -m init' before running tasks.")
    _ensure_project_gitignore_entry(project_root, ".env")
    if args.guidelines:
        _ensure_provider_gitignore_entry(project_root, provider)

    sikula_dir.mkdir(exist_ok=True)
    (sikula_dir / "tasks").mkdir(exist_ok=True)
    (sikula_dir / "contracts").mkdir(exist_ok=True)
    _ensure_sikula_gitignore(sikula_dir)

    guidelines_files = list(result.guidelines_files)
    # If a previously generated guidelines file exists, keep it in the config even when
    # --guidelines is not passed (e.g. sikula init --force without --guidelines).
    existing_gl = ".sikula/guidelines.md"
    if (sikula_dir / "guidelines.md").exists() and existing_gl not in guidelines_files:
        guidelines_files = [existing_gl] + guidelines_files
    if guidelines_content:
        gl_path = sikula_dir / "guidelines.md"
        gl_path.write_text(guidelines_content)
        guidelines_files = [existing_gl] + [f for f in guidelines_files if f != existing_gl and f != "guidelines.md"]
        print("  Generated  : .sikula/guidelines.md")

    if result.xcode_scheme:
        print(f"  scheme     : {result.xcode_scheme}")
    if result.write_paths:
        print(f"  write_paths: {', '.join(result.write_paths)}")
    if result.test_write_paths:
        print(f"  test_paths : {', '.join(result.test_write_paths)}")

    config = _generate_config(
        build_tool=result.build_tool,
        language=result.language,
        platform=result.platform,
        guidelines_files=guidelines_files,
        project_name=project_root.name,
        provider=provider,
        model=model,
        write_paths=result.write_paths or None,
        test_write_paths=result.test_write_paths or None,
        xcode_scheme=result.xcode_scheme,
        node_package_manager=result.package_manager,
        node_sync_command=result.node_sync_command,
        node_compile_command=result.node_compile_command,
        node_test_command=result.node_test_command,
        node_checks=result.node_checks,
    )
    config_path.write_text(config)

    todos: list[str] = []
    if not result.build_tool:
        todos.append(
            "project.build_tool — set to: cargo / gradle-android / gradle-jvm / maven / node / xcodebuild / python"
        )
    if not result.language:
        todos.append("project.language — set to your project's primary language")
    if result.build_tool in ("gradle-android", "xcodebuild"):
        ui_examples = (
            "Jetpack Compose (Material 3)' or 'XML layouts"
            if result.build_tool == "gradle-android"
            else "SwiftUI' or 'UIKit"
        )
        todos.append(f"project.ui — set to your UI framework (e.g. '{ui_examples}')")
    if result.build_tool == "gradle-android":
        todos.append(
            "build.compile_task / build.test_task — verify the Gradle tasks match your project (run: ./gradlew tasks)"
        )
    if result.build_tool == "gradle-jvm":
        todos.append(
            "build.compile_task / build.test_task — verify the Gradle tasks (default: classes / test); "
            "run ./gradlew tasks to list available tasks"
        )
    if result.build_tool == "node":
        todos.append(
            "build.sync_command / compile_command / test_command — verify the package-manager commands match "
            "your project scripts"
        )
    if result.build_tool == "xcodebuild" and not result.xcode_scheme:
        todos.append("build.scheme — set to your Xcode scheme name (run: xcodebuild -list)")
    if result.build_tool == "xcodebuild":
        todos.append("build.test_destination — set name to an available simulator (run: xcrun simctl list devices)")
    if not result.write_paths:
        todos.append("sandbox.allowed_write_paths — set to dirs where agents may write production code")
    if not result.test_write_paths:
        todos.append("sandbox.allowed_test_write_paths — set to dirs where the test writer may write")
    if not provider:
        todos.append("llm.provider / llm.model — set to your LLM provider and model")
    if not guidelines_content:
        meaningful_docs = [f for f in guidelines_files if f != "README.md"]
        if not meaningful_docs:
            todos.append(
                "guidelines.context_files — no coding-convention docs found; add architecture/guidelines files "
                "or auto-generate with: sikula init --guidelines --provider <provider> --model <model>"
            )
        else:
            todos.append(
                "guidelines.context_files — verify these files describe architecture and coding conventions "
                "(agents rely on them critically; or auto-generate with: sikula init --guidelines)"
            )

    print(f"\nCreated: {config_path}")
    if todos:
        print("\nTODOs to fill in before first run:")
        for item in todos:
            print(f"  • {item}")
    else:
        print("Config is ready — run: sikula run <task.md>")
    print("\nBefore the first isolated run, commit the Sikula config:")
    print("  git add .sikula/config.yaml .sikula/.gitignore")
    if guidelines_content:
        print("  git add .sikula/guidelines.md")
    print("  git commit -m 'Add Sikula config'")


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
    task_refine_p = task_sub.add_parser("refine", help="Refine a product task description")
    task_refine_p.add_argument("task_file", metavar="TASK_FILE", help="Path to task .txt/.md file")
    task_refine_p.add_argument("--answers", help="Path to a Sikula answers YAML file")
    task_refine_p.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Use a read-only LLM assistant to normalize the task description before deterministic refinement",
    )
    task_refine_p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Prompt for product task answers before writing the refined task description",
    )
    task_refine_p.add_argument(
        "--output",
        help="Write the refined Markdown task description to this file; defaults to tasks.<stem>.refined.md",
    )
    task_refine_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for task_preparer, e.g. --agent-model task_preparer=gpt-5.5",
    )
    task_refine_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for task_preparer, e.g. --agent-provider task_preparer=claude",
    )
    task_refine_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for task_preparer, e.g. --agent-timeout task_preparer=1200",
    )

    contract_p = sub.add_parser("contract", help="Inspect or prepare implementation contracts")
    contract_sub = contract_p.add_subparsers(dest="contract_command")
    contract_check_p = contract_sub.add_parser("check", help="Check a task file as an implementation contract")
    contract_check_p.add_argument("task_file", metavar="TASK_FILE", help="Path to task .txt/.md file")
    contract_check_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")
    contract_check_p.add_argument(
        "--write-report",
        action="store_true",
        default=False,
        help="Write .sikula/contract-reports check report and answers template artifacts",
    )
    contract_prepare_p = contract_sub.add_parser(
        "prepare",
        help="Create a project-aware Markdown implementation contract",
    )
    contract_prepare_p.add_argument("task_file", metavar="TASK_FILE", help="Path to refined task .txt/.md file")
    contract_prepare_p.add_argument(
        "--answers",
        help="Path to .sikula/contract-reports/*.answers.yaml created by Sikula prepare/check tooling",
    )
    contract_prepare_p.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Use a read-only LLM assistant to answer supported contract-preparation questions",
    )
    contract_prepare_p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Prompt for missing contract answers before writing the implementation contract",
    )
    contract_prepare_p.add_argument(
        "--output",
        help="Write the implementation contract to this file; defaults to contracts.<stem>.contract.md",
    )
    contract_prepare_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for task_preparer, e.g. --agent-model task_preparer=gpt-5.5",
    )
    contract_prepare_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for task_preparer, e.g. --agent-provider task_preparer=claude",
    )
    contract_prepare_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for task_preparer, e.g. --agent-timeout task_preparer=1200",
    )

    run_p = sub.add_parser("run", help="Run a task")
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

    # Phase toggle flags — each overrides the matching run_* key in the project config.
    # Default None means "use config value"; True/False means "force on/off for this run".
    _boa = argparse.BooleanOptionalAction
    run_p.add_argument("--build", action=_boa, default=None, help="Override run_build")
    run_p.add_argument("--presync", action=_boa, default=None, help="Override run_presync")
    run_p.add_argument(
        "--presync-clean",
        action=_boa,
        default=None,
        help="Override build.presync_clean (run clean before presync task)",
    )
    run_p.add_argument("--planner", action=_boa, default=None, help="Override run_planner")
    run_p.add_argument("--review", action=_boa, default=None, help="Override run_review")
    run_p.add_argument(
        "--security-review",
        action=_boa,
        default=None,
        help="Override run_security_review",
    )
    run_p.add_argument("--test-writing", action=_boa, default=None, help="Override run_test_writing")
    run_p.add_argument("--tests", action=_boa, default=None, help="Override run_tests")
    run_p.add_argument(
        "--build-per-step",
        action=_boa,
        default=None,
        help="Override run_build_per_step",
    )
    run_p.add_argument("--checks", action=_boa, default=None, help="Override run_checks")
    run_p.add_argument(
        "--require-contract-ready",
        action="store_true",
        default=False,
        help="Abort fresh task-file runs before agents unless the implementation contract is ready",
    )
    run_p.add_argument(
        "--min-contract-score",
        type=_contract_score_threshold,
        default=None,
        metavar="0-100",
        help="Abort fresh task-file runs before agents unless the implementation contract score is at least this value",
    )

    # Per-agent LLM overrides — repeatable; layer on top of agents.<name>.llm in the project config.
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

    status_p = sub.add_parser("status", help="List all tasks")
    status_p.add_argument("--json", action="store_true", default=False, help="Print task status rows as JSON")
    status_p.add_argument("--verbose", action="store_true", default=False, help="Include next suggested action")
    status_p.add_argument(
        "--active",
        dest="status_filter",
        action="append_const",
        const="active",
        default=[],
        help="Show only active or interrupted tasks",
    )
    status_p.add_argument(
        "--done",
        dest="status_filter",
        action="append_const",
        const="done",
        help="Show only completed tasks",
    )
    status_p.add_argument(
        "--failed",
        dest="status_filter",
        action="append_const",
        const="failed",
        help="Show only failed tasks",
    )
    status_p.add_argument(
        "--cleaned",
        dest="status_filter",
        action="append_const",
        const="cleaned",
        help="Show only cleaned audit-only tasks",
    )

    show_p = sub.add_parser("show", help="Show full task state as JSON")
    show_p.add_argument("task_id")

    cleanup_p = sub.add_parser("cleanup", help="Remove a task worktree but keep its state JSON")
    cleanup_p.add_argument("task_id")
    cleanup_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Apply cleanup. Without this flag, cleanup only prints what would happen.",
    )
    cleanup_p.add_argument(
        "--discard",
        action="store_true",
        default=False,
        help="Allow removing a dirty worktree and discarding uncommitted changes.",
    )

    delete_p = sub.add_parser("delete", help="Delete a task worktree and its state JSON")
    delete_p.add_argument("task_id")
    delete_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Apply deletion. Without this flag, delete only prints what would happen.",
    )
    delete_p.add_argument(
        "--discard",
        action="store_true",
        default=False,
        help="Allow removing a dirty worktree and discarding uncommitted changes.",
    )

    review_p = sub.add_parser("review", help="Review an existing branch (report-only or --fix)")
    review_p.add_argument("--branch", required=True, help="Branch to review (must already exist)")
    review_p.add_argument("--base-branch", default="main", help="Base branch to diff against (default: main)")
    review_context = review_p.add_mutually_exclusive_group(required=True)
    review_context.add_argument(
        "--description", default=None, help="Human-readable PR description (context for the reviewer)"
    )
    review_context.add_argument(
        "--description-file",
        default=None,
        metavar="FILE",
        help="Path to a file containing the PR description",
    )
    review_p.add_argument(
        "--fix",
        action="store_true",
        default=False,
        help="Apply fixes: run implementer on review issues and commit them to the branch",
    )
    review_p.add_argument(
        "--security-review",
        action=_boa,
        default=None,
        help="Override run_security_review (default: from project config)",
    )
    review_p.add_argument(
        "--agent-model",
        action="append",
        default=None,
        metavar="AGENT=MODEL",
        help="Override model for one agent (repeatable)",
    )
    review_p.add_argument(
        "--agent-provider",
        action="append",
        default=None,
        metavar="AGENT=PROVIDER",
        help="Override provider for one agent (repeatable)",
    )
    review_p.add_argument(
        "--agent-timeout",
        action="append",
        default=None,
        metavar="AGENT=SECONDS",
        help="Override timeout for one agent (repeatable)",
    )

    init_p = sub.add_parser("init", help="Initialize a new .sikula project config")
    init_p.add_argument("--force", action="store_true", default=False, help="Overwrite existing config")
    init_p.add_argument(
        "--guidelines",
        action="store_true",
        default=False,
        help="Use LLM to generate .sikula/guidelines.md from codebase analysis",
    )
    init_p.add_argument(
        "--provider",
        default=None,
        help="LLM provider for --guidelines (codex/claude/gemini/opencode); falls back to config when present",
    )
    init_p.add_argument("--model", default=None, help="LLM model for --guidelines; falls back to config when present")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
        return

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
