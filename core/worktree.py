"""Git and worktree helper functions shared by Sikula command layers."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterator


class WorktreeEnvironmentCopyError(RuntimeError):
    """Raised when an environment file cannot be copied without escaping its worktree."""


class DetachedWorktreeError(RuntimeError):
    """Raised when an internal detached worktree cannot be managed safely."""


def delivery_verification_git_env() -> dict[str, str]:
    """Return a repository-discovered Git environment without replacement objects."""

    env = dict(os.environ)
    for key in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        env.pop(key, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


@contextmanager
def detached_delivery_verification_worktree(project_root: Path, commit: str) -> Iterator[Path]:
    """Create a detached candidate worktree and yield its configured project root."""

    root = project_root.resolve(strict=True)
    env = delivery_verification_git_env()
    top_level = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if top_level.returncode != 0 or not top_level.stdout.strip():
        raise DetachedWorktreeError("Delivery verification could not resolve the repository root.")
    try:
        git_root = Path(top_level.stdout.strip()).resolve(strict=True)
        project_prefix = root.relative_to(git_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DetachedWorktreeError("Delivery verification project root is outside its repository.") from exc

    parent = root / ".sikula" / "worktrees" / "delivery-verification"
    _prepare_private_worktree_parent(root, parent)
    worktree = Path(tempfile.mkdtemp(prefix="candidate-", dir=parent))
    added = False
    try:
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), commit],
            cwd=git_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode != 0:
            raise DetachedWorktreeError("Delivery verification could not create its isolated worktree.")
        added = True
        if worktree.is_symlink() or not worktree.is_dir():
            raise DetachedWorktreeError("Delivery verification worktree has an unsafe filesystem identity.")
        candidate_root = worktree
        for part in project_prefix.parts:
            candidate_root /= part
            if candidate_root.is_symlink() or not candidate_root.is_dir():
                raise DetachedWorktreeError("Delivery verification project root is unavailable in the candidate.")
        try:
            candidate_root.resolve(strict=True).relative_to(worktree.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise DetachedWorktreeError("Delivery verification project root escapes the candidate worktree.") from exc
        yield candidate_root
    finally:
        cleanup_failed = False
        if added:
            cleanup = subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=git_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            cleanup_failed = cleanup.returncode != 0
        if worktree.exists() and not cleanup_failed:
            shutil.rmtree(worktree, ignore_errors=True)
        if cleanup_failed:
            raise DetachedWorktreeError("Delivery verification could not clean up its isolated worktree.")


def _prepare_private_worktree_parent(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        created = False
        try:
            if current.is_symlink():
                raise DetachedWorktreeError("Delivery verification worktree parent contains a symlink.")
            if current.exists() and not current.is_dir():
                raise DetachedWorktreeError("Delivery verification worktree parent is not a directory.")
            if not current.exists():
                current.mkdir(mode=0o700)
                created = True
            if created:
                os.chmod(current, 0o700)
        except DetachedWorktreeError:
            raise
        except OSError as exc:
            raise DetachedWorktreeError("Delivery verification worktree parent is unavailable.") from exc


def copy_worktree_environment_file(source: Path, destination: Path, worktree_root: Path) -> bool:
    """Copy one required environment file without following destination symlinks."""
    if not source.exists():
        return False
    try:
        relative = destination.relative_to(worktree_root)
    except ValueError as exc:
        raise WorktreeEnvironmentCopyError("Environment file destination is outside the worktree") from exc
    if not relative.parts or ".." in relative.parts:
        raise WorktreeEnvironmentCopyError("Environment file destination is outside the worktree")

    try:
        resolved_root = worktree_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeEnvironmentCopyError("Environment file worktree cannot be resolved safely") from exc

    current = worktree_root
    for part in relative.parts:
        current /= part
        try:
            if current.is_symlink():
                raise WorktreeEnvironmentCopyError(
                    f"Environment file destination {relative.as_posix()} contains a symlink"
                )
            current.resolve(strict=False).relative_to(resolved_root)
        except WorktreeEnvironmentCopyError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorktreeEnvironmentCopyError(
                f"Environment file destination {relative.as_posix()} cannot be resolved inside the worktree"
            ) from exc

    if destination.exists():
        return False
    shutil.copy2(source, destination)
    return True


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


def file_blob_status_at_ref(
    git_root: Path,
    ref: str,
    rel_path: str,
    *,
    expected_ref: str | None = None,
) -> tuple[bool, str]:
    """Return whether rel_path is a file blob at ref and optionally matches another ref."""
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
        if expected_ref is not None:
            expected_oid = _file_blob_oid_at_ref(git_root, expected_ref, rel_path)
            actual_oid = _file_blob_oid_at_ref(git_root, ref, rel_path)
            if expected_oid is None or actual_oid is None:
                return False, f"could not compare with file in reference '{expected_ref}'"
            if actual_oid != expected_oid:
                return False, f"differs from file in reference '{expected_ref}'"
        return True, ""

    type_label = {
        "tree": "directory",
        "commit": "submodule/gitlink",
    }.get(object_type, f"{object_type or 'non-file'} object")
    return False, f"is a {type_label} in worktree start ref '{ref}', expected a file"


def _file_blob_oid_at_ref(git_root: Path, ref: str, rel_path: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}:{rel_path}"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0]


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


def branch_checked_out(
    git_root: Path,
    branch: str,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=git_root,
        env=env,
    )
    if result.returncode != 0:
        return True
    branch_ref = f"branch refs/heads/{branch}"
    return any(line.strip() == branch_ref for line in result.stdout.splitlines())


def resolve_git_commit(
    git_root: Path,
    ref: str,
    *,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        cwd=git_root,
        env=env,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0], ""
    return None, _short_audit_line(r.stderr.strip() or r.stdout.strip() or "unknown revision")


def git_path_lines(
    git_root: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[list[str], str | None]:
    r = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=git_root,
        env=env,
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
    git_env: dict[str, str] | None = None,
) -> tuple[list[str], list[str], list[str], str | None]:
    excluded_prefixes = git_excluded_path_prefixes(git_root, exclude_paths)
    staged, error = git_path_lines(git_root, ["diff", "--cached", "--name-only"], env=git_env)
    if error:
        return [], [], [], error
    unstaged, error = git_path_lines(git_root, ["diff", "--name-only"], env=git_env)
    if error:
        return [], [], [], error
    untracked, error = git_path_lines(
        git_root,
        ["ls-files", "--others", "--exclude-standard"],
        env=git_env,
    )
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
