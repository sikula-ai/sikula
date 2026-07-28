"""Config discovery and path resolution helpers for the Sikula CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

import yaml
from dotenv import load_dotenv


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


def _original_project_root_from_worktree(
    project_root: Path,
    *,
    sikula_worktree_base_for_path: Callable[[Path], Path | None] | None = None,
) -> Path | None:
    """Map a Sikula task worktree project root back to the original project root.

    Isolated task worktrees live under:
      <git-root>/.sikula/worktrees/<task-id>/<project-relative-path>

    The worktree contains the tracked .sikula/config.yaml too, but task state is kept
    in the original project .sikula/state. Commands such as `status`, `show`, and
    `run --task-id` should therefore resolve config from the original project when
    invoked inside a task worktree.
    """
    root = project_root.resolve()
    worktree_base_for_path = sikula_worktree_base_for_path or _sikula_worktree_base_for_path
    worktree_base = worktree_base_for_path(root)
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


def _resolve_config(
    config_arg: str | None,
    *,
    find_project_root: Callable[[Path | None], Path | None] | None = None,
    original_project_root_from_worktree: Callable[[Path], Path | None] | None = None,
) -> tuple[Path, Path | None]:
    """Return (config_path, discovered_project_root).

    discovered_project_root is set only when .sikula/config.yaml was auto-discovered;
    it is used to resolve relative paths in the config against the true project root
    rather than CWD (which may be a subdirectory).
    """
    if config_arg:
        return Path(config_arg), None

    project_root = (find_project_root or _find_project_root)(None)
    if project_root:
        original_root = (original_project_root_from_worktree or _original_project_root_from_worktree)(project_root)
        if original_root:
            return original_root / ".sikula" / "config.yaml", original_root
        return project_root / ".sikula" / "config.yaml", project_root

    print("No config found. Run 'sikula init' to set up this project, or use --config.")
    sys.exit(1)


def _resolve_optional_config(
    config_arg: str | None,
    *,
    resolve_config: Callable[[str | None], tuple[Path, Path | None]] | None = None,
    find_project_root: Callable[[Path | None], Path | None] | None = None,
    original_project_root_from_worktree: Callable[[Path], Path | None] | None = None,
) -> tuple[Path, Path | None] | None:
    if config_arg:
        return (resolve_config or _resolve_config)(config_arg)

    project_root = (find_project_root or _find_project_root)(None)
    if not project_root:
        return None
    original_root = (original_project_root_from_worktree or _original_project_root_from_worktree)(project_root)
    if original_root:
        return original_root / ".sikula" / "config.yaml", original_root
    return project_root / ".sikula" / "config.yaml", project_root


def _load_runtime_config(
    config_arg: str | None,
    *,
    required: bool = True,
    resolve_config: Callable[[str | None], tuple[Path, Path | None]] | None = None,
    resolve_optional_config: Callable[[str | None], tuple[Path, Path | None] | None] | None = None,
    load_config: Callable[[Path], dict] | None = None,
    resolve_root_path: Callable[[str, Path | None, Path], Path] | None = None,
    load_project_env: Callable[[Path], None] | None = None,
) -> dict:
    resolve_config_fn = resolve_config or _resolve_config
    resolve_optional_config_fn = resolve_optional_config or _resolve_optional_config
    resolved = resolve_config_fn(config_arg) if required else resolve_optional_config_fn(config_arg)
    if resolved is None:
        return {}

    config_path, discovered_root = resolved
    cfg = (load_config or globals()["load_config"])(config_path)
    cfg["_config_path"] = str(config_path.resolve())
    raw = cfg.get("project", {}).get("root_path", ".")
    cfg["project"]["root_path"] = str((resolve_root_path or _resolve_root_path)(raw, discovered_root, config_path))
    (load_project_env or _load_project_env)(Path(cfg["project"]["root_path"]))
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


def _resolve_task_asset_dir(cfg: dict) -> Path:
    raw = cfg.get("tasks", {}).get("task_asset_dir", ".sikula/task-assets")
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
    return yaml.safe_load(path.read_text(encoding="utf-8"))
