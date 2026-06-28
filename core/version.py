"""Version helpers shared by the CLI and task state metadata."""

from __future__ import annotations

import re
import subprocess
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]


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


def _git_root(path: Path = _BASE) -> Path | None:
    root = _git_output(["rev-parse", "--show-toplevel"], path)
    if not root:
        return None
    return Path(root).resolve()


def _is_sikula_source_checkout(path: Path = _BASE) -> bool:
    root = _git_root(path)
    if root is None:
        return False
    if not (root / "sikula.py").is_file() or not (root / "core" / "version.py").is_file():
        return False
    try:
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r'(?m)^name\s*=\s*"sikula"\s*$', pyproject))


def dev_version_suffix(path: Path = _BASE) -> str:
    if not _is_git_checkout(path) or not _is_sikula_source_checkout(path):
        return ""
    branch = _git_output(["branch", "--show-current"], path)
    commit = _git_output(["rev-parse", "--short", "HEAD"], path)
    parts = [re.sub(r"[^A-Za-z0-9.]+", ".", p).strip(".") for p in (branch, commit) if p]
    return "-dev" + (f"+{'.'.join(parts)}" if parts else "")


def sikula_version() -> str:
    try:
        base_version = _pkg_version("sikula")
    except PackageNotFoundError:
        base_version = "dev"
    if base_version == "dev":
        return "dev"
    return base_version + dev_version_suffix()
