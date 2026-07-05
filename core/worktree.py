"""Git and worktree helper functions shared by Sikula command layers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import subprocess


def _short_audit_line(value: str | None, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def worktree_error_message(branch: str | None, stderr: str) -> str:
    """Return a human-readable error message for a failed `git worktree add`."""
    branch_name = str(branch)
    if "already checked out" in stderr or "is already used by worktree" in stderr:
        return (
            f"Branch '{branch_name}' is already checked out.\n"
            f"If you are currently on '{branch_name}', switch away first:\n"
            f"  git checkout main\n"
            "If a previous --fix run left a stale worktree, remove it:\n"
            "  git worktree list   # find the path\n"
            "  git worktree remove <path>"
        )
    return f"Failed to create worktree for branch '{branch_name}': {stderr}"


def ensure_gitignore(git_root: Path) -> None:
    entry = ".sikula/worktrees/"
    exclude = git_root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if exclude.exists() and any(line.strip() == entry for line in exclude.read_text().splitlines()):
        return
    with exclude.open("a") as f:
        f.write(f"\n{entry}\n")


def ensure_project_gitignore_entry(project_root: Path, entry: str) -> None:
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if any(line.strip() == entry for line in existing.splitlines()):
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with gitignore.open("a") as f:
        f.write(f"{prefix}{entry}\n")


def find_git_root(path: Path) -> Path | None:
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


def git_relative_path(git_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return None


def tracked_clean_file_status(git_root: Path, path: Path) -> tuple[bool, str]:
    """Return whether path exists, is tracked, and matches HEAD in git_root."""
    rel = git_relative_path(git_root, path)
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


def file_blob_status_at_ref(git_root: Path, ref: str, rel_path: str) -> tuple[bool, str]:
    """Return whether rel_path resolves to a file blob at ref."""
    result = subprocess.run(
        ["git", "cat-file", "-t", f"{ref}:{rel_path}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if result.returncode != 0:
        return False, f"not present in worktree start ref '{ref}'"

    object_type = result.stdout.strip()
    if object_type == "blob":
        return True, ""

    type_label = {
        "tree": "directory",
        "commit": "submodule/gitlink",
    }.get(object_type, f"{object_type or 'non-file'} object")
    return False, f"is a {type_label} in worktree start ref '{ref}', expected a file"


def current_branch_name(git_root: Path) -> tuple[str | None, str | None]:
    r = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if r.returncode == 0:
        branch = r.stdout.strip()
        return (branch, None) if branch else (None, "unknown")
    if r.stderr.strip():
        return None, "unknown"

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if head.returncode == 0:
        return None, "detached"
    return None, "unknown"


def resolve_git_commit(git_root: Path, ref: str) -> tuple[str | None, str]:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0], ""
    return None, _short_audit_line(r.stderr.strip() or r.stdout.strip() or "unknown revision")


def git_path_lines(git_root: Path, args: list[str]) -> tuple[list[str], str | None]:
    r = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if r.returncode != 0:
        return [], _short_audit_line(r.stderr.strip() or r.stdout.strip() or "git command failed")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()], None


def git_excluded_path_prefixes(git_root: Path, exclude_paths: Sequence[Path] | None) -> set[str]:
    if not exclude_paths:
        return set()
    root = git_root.resolve()
    prefixes: set[str] = set()
    for path in exclude_paths:
        try:
            rel = Path(path).resolve().relative_to(root)
        except ValueError:
            continue
        rel_text = rel.as_posix()
        if rel_text and rel_text != ".":
            prefixes.add(rel_text)
    return prefixes


def filter_git_paths(paths: list[str], excluded_prefixes: set[str]) -> list[str]:
    if not excluded_prefixes:
        return paths
    filtered = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in excluded_prefixes):
            continue
        filtered.append(path)
    return filtered


def current_worktree_changes(
    git_root: Path,
    *,
    exclude_paths: Sequence[Path] | None = None,
) -> tuple[list[str], list[str], list[str], str | None]:
    excluded_prefixes = git_excluded_path_prefixes(git_root, exclude_paths)
    staged, error = git_path_lines(git_root, ["diff", "--cached", "--name-only"])
    if error:
        return [], [], [], error
    unstaged, error = git_path_lines(git_root, ["diff", "--name-only"])
    if error:
        return [], [], [], error
    untracked, error = git_path_lines(git_root, ["ls-files", "--others", "--exclude-standard"])
    if error:
        return [], [], [], error
    staged = filter_git_paths(staged, excluded_prefixes)
    unstaged = filter_git_paths(unstaged, excluded_prefixes)
    untracked = filter_git_paths(untracked, excluded_prefixes)
    return staged, unstaged, untracked, None


def worktree_dirty(worktree_base: Path) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=worktree_base,
    )
    return bool(status.stdout.strip()) if status.returncode == 0 else True


def remove_worktree(worktree_base: Path, git_root: Path, *, force: bool) -> bool:
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree_base))
    result = subprocess.run(cmd, cwd=git_root, check=False)
    return result.returncode == 0


def path_is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
