from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def _git(root: Path, args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=text, check=False)


def _git_root(cwd: Path) -> tuple[Path | None, list[str]]:
    result = _git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return None, [output or "not inside a git repository"]
    return Path(result.stdout.strip()).resolve(), []


def _has_head(root: Path) -> bool:
    result = _git(root, ["rev-parse", "--verify", "HEAD"])
    return result.returncode == 0


def _tracked_diff_errors(root: Path) -> list[str]:
    base_args = ["diff", "--check"]
    args = [*base_args, "HEAD", "--"] if _has_head(root) else [*base_args, "--"]
    result = _git(root, args)
    if result.returncode == 0:
        return []
    output = (result.stdout + result.stderr).strip()
    return output.splitlines() if output else ["git diff --check failed"]


def _untracked_files(root: Path) -> tuple[list[Path], list[str]]:
    result = _git(root, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."], text=False)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode(errors="replace").strip()
        return [], [output or "git ls-files failed"]
    return [Path(os.fsdecode(raw)) for raw in result.stdout.split(b"\0") if raw], []


def _line_body(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\n", b"\r")):
        return line[:-1]
    return line


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def _untracked_file_errors(root: Path, rel_path: Path) -> list[str]:
    display = rel_path.as_posix()
    path = root / rel_path
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"{display}: could not read file: {exc}"]
    if _is_binary(data):
        return []

    errors: list[str] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        body = _line_body(line)
        if body.endswith((b" ", b"\t")):
            errors.append(f"{display}:{line_number}: trailing whitespace.")
        indent = body[: len(body) - len(body.lstrip(b" \t"))]
        if b" \t" in indent:
            errors.append(f"{display}:{line_number}: space before tab in indent.")
    return errors


def check_whitespace(cwd: Path | None = None) -> list[str]:
    root, errors = _git_root((cwd or Path.cwd()).resolve())
    if root is None:
        return errors

    errors = _tracked_diff_errors(root)
    untracked, untracked_errors = _untracked_files(root)
    errors.extend(untracked_errors)
    for rel_path in untracked:
        errors.extend(_untracked_file_errors(root, rel_path))
    return errors


def main() -> int:
    errors = check_whitespace()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
