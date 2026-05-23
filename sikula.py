#!/usr/bin/env python3
"""Sikula — LLM-powered development orchestration.

Usage (project-centric, run from project root):
  sikula init                        # create .sikula/config.yaml
  sikula init --guidelines --provider codex --model gpt-5.5
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

Per-agent LLM flags (repeatable, agent name uses _ or -):
  --agent-model analyst=gpt-5.5
  --agent-provider analyst=claude
  --agent-timeout implementer=2400
  CLI values layer on top of YAML agents.<name>.llm overrides.
  Valid agents: analyst, planner, implementer, reviewer, security_reviewer, test_writer, fixer

--task-file accepts absolute paths or paths relative to CWD.
"""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version as _pkg_version
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

_BASE = Path(__file__).parent
# When adding a new platform: add it here, in _build_tool() in core/orchestrator.py,
# in _generate_config() below, and in _SIGNATURES in tools/scanner.py.
_SUPPORTED_BUILD_TOOLS = {"cargo", "gradle-android", "gradle-jvm", "maven", "xcodebuild", "python"}


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
) -> dict[str, dict]:
    """Parse --agent-model / --agent-provider / --agent-timeout into per-agent override dicts."""
    result: dict[str, dict] = {}

    def _add(entries: list[str] | None, field: str, cast=str, flag: str | None = None) -> None:
        flag_name = f"--agent-{flag or field}"
        for entry in entries or []:
            raw_agent, sep, val = entry.partition("=")
            agent = raw_agent.strip().replace("-", "_")
            if agent not in _VALID_AGENTS:
                print(f"Unknown agent '{agent}'. Valid agents: {', '.join(sorted(_VALID_AGENTS))}")
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


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _build_tool_class(cfg: dict):
    """Return the BuildTool subclass for the configured project.

    Used only for env_files() — called before the orchestrator is created.
    When adding a new platform, extend both this function and _build_tool()
    in core/orchestrator.py.
    """
    platform = cfg.get("project", {}).get("build_tool", "gradle-android")
    if platform == "python":
        from tools.python_tool import PythonTool

        return PythonTool
    if platform == "cargo":
        from tools.cargo_tool import CargoTool

        return CargoTool
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


_REFERENCED_FILES_PROMPT = """\
The task description below may reference files by name (images, mockups, PDFs, \
spreadsheets, specs, or any other attachment). For each file mentioned by name:
  1. Search: find . -name "<filename>"
  2. Read it if found.

Return the content of each file found, labelled with its path. \
If no files are referenced by name, or none can be found after searching, \
return an empty response.

Task description:
{task_description}
"""


def _enrich_prompt_with_referenced_files(task_description: str, llm_client, project_root: Path) -> str:
    """Return contents of files referenced by name in the task description, or empty string."""
    from agents.base_agent import AGENT_SECURITY_PREFIX

    prompt = AGENT_SECURITY_PREFIX + _REFERENCED_FILES_PROMPT.format(task_description=task_description)
    try:
        result = llm_client.run_readonly_agent(prompt, cwd=project_root)
        return result.strip()
    except Exception as e:
        log.warning("Referenced file enrichment skipped: %s", e)
        return ""


def _branch_stem(task_file: str) -> str:
    stem = Path(task_file).stem
    stem = stem.lower()
    stem = re.sub(r"[\s_]+", "-", stem)
    stem = re.sub(r"[^a-z0-9-]", "", stem)
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


def build_orchestrator(cfg: dict, overrides: dict | None = None, state_store=None):
    from core.llm_client import create_llm_client
    from core.orchestrator import Orchestrator, OrchestratorConfig
    from core.state import JsonStateStore

    overrides = overrides or {}

    def _phase(key: str) -> bool:
        """Return CLI override if set, else config value, else False."""
        cli_val = overrides.get(key)
        return cfg.get(key, False) if cli_val is None else cli_val

    run_build = _phase("run_build")
    run_presync = _phase("run_presync")
    run_review = _phase("run_review")
    run_security_review = _phase("run_security_review")
    run_test_writing = _phase("run_test_writing")
    run_tests = _phase("run_tests")
    run_planner = _phase("run_planner")
    run_build_per_step = _phase("run_build_per_step")
    run_checks = _phase("run_checks")

    # presync_clean lives under build: in the config — patch the nested dict in-place so
    # the value reaches the build tool (reads it from project_config["build"]["presync_clean"]).
    if "presync_clean" in overrides:
        cfg.setdefault("build", {})["presync_clean"] = overrides["presync_clean"]

    project_root = Path(cfg["project"]["root_path"])
    sandbox = cfg.get("sandbox", {})
    base_llm_cfg = cfg.get("llm", {})
    agents_cfg = cfg.get("agents", {})
    cli_agent_overrides: dict[str, dict] = overrides.get("agent_llms", {})

    def _agent_llm_cfg(name: str) -> dict:
        yaml_agent = agents_cfg.get(name, {}).get("llm", {})
        cli_agent = cli_agent_overrides.get(name, {})
        return {**yaml_agent, **cli_agent}  # CLI wins over YAML

    max_iterations = int(sandbox.get("max_iterations", 10))
    max_review_iterations = int(sandbox.get("max_review_iterations", 3))
    max_security_review_iterations = int(sandbox.get("max_security_review_iterations", max_review_iterations))
    if state_store is None:
        state_store = JsonStateStore(_resolve_state_dir(cfg))

    default_llm = create_llm_client(_make_llm_config(base_llm_cfg, {}))
    agent_llms = {
        name: create_llm_client(_make_llm_config(base_llm_cfg, _agent_llm_cfg(name)))
        for name in _VALID_AGENTS
        if _agent_llm_cfg(name)
    }

    def _agent_snapshot(name: str) -> dict:
        c = _make_llm_config(base_llm_cfg, _agent_llm_cfg(name))
        snap: dict = {
            "provider": c.provider,
            "model": c.model,
            "agent_timeout": c.agent_timeout,
        }
        extra_rules = cfg.get(name, {}).get("extra_rules")
        if extra_rules:
            snap["extra_rules"] = extra_rules
        return snap

    config_snapshot = {
        "project": cfg.get("project", {}).get("name"),
        "run_presync": run_presync,
        "run_planner": run_planner,
        "run_review": run_review,
        "run_security_review": run_security_review,
        "run_test_writing": run_test_writing,
        "run_build": run_build,
        "run_tests": run_tests,
        "run_build_per_step": run_build_per_step,
        "run_checks": run_checks,
        "max_iterations": max_iterations,
        "max_review_iterations": max_review_iterations,
        "max_security_review_iterations": max_security_review_iterations,
        "sandbox": {
            "allowed_write_paths": sandbox.get("allowed_write_paths", []),
            "allowed_test_write_paths": sandbox.get("allowed_test_write_paths", []),
            "allowed_read_paths": sandbox.get("allowed_read_paths", ["."]),
        },
        "build": cfg.get("build", {}),
        "agents": {name: _agent_snapshot(name) for name in sorted(_VALID_AGENTS)},
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


def _fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


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
        store.save(state)

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
    if state.done:
        status = "✓ DONE"
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
    if state.pid and not _pid_running(state.pid):
        return "INTERRUPTED"
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
        return f"sikula run --task-id {state.task_id} --reset-failed"
    if status == "CLEANED":
        return f"sikula show {state.task_id}"
    if status == "INTERRUPTED":
        return f"sikula run --task-id {state.task_id}"
    return "wait" if state.pid and _pid_running(state.pid) else f"sikula run --task-id {state.task_id}"


def _status_row(state) -> dict:
    status = _status_label(state)
    task_label = state.task_file
    if not task_label:
        task_label = state.task_description.splitlines()[0][:60] if state.task_description else "(no description)"
    return {
        "id": state.task_id,
        "status": status,
        "step": _status_step(state),
        "build": state.build_iterations if state.build_iterations else None,
        "updated": state.updated_at,
        "updated_human": _status_updated(state),
        "task": task_label,
        "next_action": _status_next_action(state, status),
    }


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
    print(f"\nState ID: {state.task_id}  (sikula show {state.task_id})")


def _run_review_agent_with_retry_history(agent, name: str, state, store):
    from core.retry_history import llm_retry_history

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
            "sandbox": {
                "allowed_write_paths": sandbox_cfg.get("allowed_write_paths", []),
                "allowed_test_write_paths": sandbox_cfg.get("allowed_test_write_paths", []),
                "allowed_read_paths": sandbox_cfg.get("allowed_read_paths", ["."]),
            },
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
        _run_review_agent_with_retry_history(reviewer, "reviewer", state, store)
        store.save(state)

        if state.review_approved and run_security_review:
            log.info("--- Phase: security review ---")
            security_reviewer = SecurityReviewerAgent(llm=_llm("security_reviewer"), tools=tools, project_config=cfg)
            _run_review_agent_with_retry_history(security_reviewer, "security_reviewer", state, store)
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
  checks:
    - name: ruff-check
      command: "python3 -m ruff check ."
      timeout: 60
    - name: ruff-format
      command: "python3 -m ruff format --check ."
      fix_command: "python3 -m ruff format ."
      timeout: 60
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
"""
    elif build_tool == "maven":
        build_section = """\
build:
  # Uses ./mvnw if present, falls back to mvn on PATH.
  # Override compile_command / test_command to customize (e.g. add -DskipTests=true).
  compile_timeout: 600
  test_timeout: 600
  sync_timeout: 300
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
"""
    else:
        build_section = """\
build:
  # TODO: configure compile and test commands for your project.
  compile_command: "TODO"
  test_command: "TODO"
  timeout: 600
"""

    platform_line = f"  platform: {platform}\n" if platform else ""
    language_line = f"  language: {language}\n" if language else "  language: TODO\n"
    if build_tool == "gradle-android":
        ui_line = "  # TODO: set ui to your UI framework — e.g. 'Jetpack Compose (Material 3)' or 'XML layouts'\n  # ui: Jetpack Compose (Material 3)\n"
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

tasks:
  state_dir: .sikula/state/

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
  coverage_target: 90
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

    gitignore_path = sikula_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("state/\nworktrees/\n")

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
    )
    config_path.write_text(config)

    todos: list[str] = []
    if not result.build_tool:
        todos.append("project.build_tool — set to: cargo / gradle-android / gradle-jvm / maven / xcodebuild / python")
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

    config_path, discovered_root = _resolve_config(args.config)
    cfg = load_config(config_path)
    cfg["_config_path"] = str(config_path.resolve())
    raw = cfg.get("project", {}).get("root_path", ".")
    cfg["project"]["root_path"] = str(_resolve_root_path(raw, discovered_root, config_path))
    _load_project_env(Path(cfg["project"]["root_path"]))

    if args.command == "run":
        task_file = args.task_file_pos or args.task_file
        if not task_file and not args.task_id:
            run_p.error("provide TASK_FILE or --task-id")
        cmd_run(args, cfg)
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
