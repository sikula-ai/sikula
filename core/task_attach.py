"""Helpers for attaching local files as Sikula task assets."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import shutil
import unicodedata

from core.markdown_headings import MarkdownHeadingScanner
from core.task_assets import supported_asset_extensions


@dataclass(frozen=True)
class TaskAssetAttachResult:
    source_path: Path
    asset_path: Path
    project_path: str
    snippet: str
    wrote_task_file: bool
    reused_existing: bool
    sha256: str
    size_bytes: int


def attach_task_asset(
    *,
    task_file: Path,
    source_file: Path,
    project_root: Path,
    task_asset_dir: Path,
    kind: str,
    note: str = "",
    purpose: str = "",
    target: str = "",
    source_license: str = "",
    write: bool = False,
) -> TaskAssetAttachResult:
    task_file = task_file.resolve()
    source_file = source_file.expanduser().resolve()
    project_root = project_root.resolve()
    task_asset_dir = task_asset_dir.resolve()

    if not task_file.is_file():
        raise ValueError(f"task file does not exist: {task_file}")
    if not source_file.is_file():
        raise ValueError(f"asset file does not exist or is not a file: {source_file}")
    _ensure_inside_project(task_asset_dir, project_root, label="task asset directory")

    normalized_kind = _normalize_kind(kind)
    if normalized_kind == "delivery":
        if not purpose.strip():
            raise ValueError("--purpose is required for delivery assets")
        if not source_license.strip():
            raise ValueError("--source is required for delivery assets")

    suffix = source_file.suffix.lower()
    if suffix not in supported_asset_extensions():
        raise ValueError(f"unsupported asset extension: {source_file.suffix or '(none)'}")

    normalized_target = _normalize_target(target, project_root=project_root) if target.strip() else ""
    destination_dir = task_asset_dir / _safe_stem(task_file.stem)
    destination_dir.mkdir(parents=True, exist_ok=True)
    _ensure_inside_project(destination_dir, project_root, label="task asset destination directory")
    destination = _available_destination(destination_dir, source_file)
    reused_existing = False
    if destination.resolve() == source_file:
        reused_existing = True
    elif destination.exists() and _file_sha256(destination) == _file_sha256(source_file):
        reused_existing = True
    else:
        shutil.copy2(source_file, destination)

    project_path = _project_relative_path(destination, project_root)
    snippet = render_task_asset_snippet(
        project_path=project_path,
        kind=normalized_kind,
        note=note,
        purpose=purpose,
        target=normalized_target,
        source_license=source_license,
    )
    if write:
        updated = append_task_asset_snippet(task_file.read_text(encoding="utf-8"), snippet, kind=normalized_kind)
        task_file.write_text(updated, encoding="utf-8")

    return TaskAssetAttachResult(
        source_path=source_file,
        asset_path=destination,
        project_path=project_path,
        snippet=snippet,
        wrote_task_file=write,
        reused_existing=reused_existing,
        sha256="sha256:" + _file_sha256(destination),
        size_bytes=destination.stat().st_size,
    )


def render_task_asset_snippet(
    *,
    project_path: str,
    kind: str,
    note: str = "",
    purpose: str = "",
    target: str = "",
    source_license: str = "",
) -> str:
    normalized_kind = _normalize_kind(kind)
    label = "Reference asset" if normalized_kind == "reference" else "Delivery asset"
    lines = [f"- {label}: `{project_path}`"]
    if normalized_kind == "reference":
        lines.append("  - Usage: reference only.")
        if note.strip():
            lines.append(f"  - Notes: {_single_line(note)}")
        lines.append("  - Do not copy this file into production assets.")
    else:
        lines.append("  - Usage: delivery asset.")
        lines.append(f"  - Purpose: {_single_line(purpose)}")
        if target.strip():
            lines.append(f"  - Target: `{target}`")
        lines.append(f"  - Source/license: {_single_line(source_license)}")
    return "\n".join(lines)


def append_task_asset_snippet(markdown: str, snippet: str, *, kind: str) -> str:
    normalized_kind = _normalize_kind(kind)
    subsection = "Reference assets" if normalized_kind == "reference" else "Delivery assets"
    lines = markdown.splitlines()
    if not lines:
        return f"## Assets\n\n### {subsection}\n\n{snippet}\n"

    assets = _find_section(lines, "assets")
    if assets is None:
        return markdown.rstrip() + f"\n\n## Assets\n\n### {subsection}\n\n{snippet}\n"

    start, end, level = assets
    subsection_bounds = _find_section(lines[start + 1 : end], subsection.lower())
    if subsection_bounds is not None:
        sub_start, sub_end, _ = subsection_bounds
        insert_at = start + 1 + sub_end
        insert_lines = ["", snippet]
    else:
        insert_at = end
        heading_level = min(level + 1, 6)
        insert_lines = ["", f"{'#' * heading_level} {subsection}", "", snippet]

    updated_lines = [*lines[:insert_at], *insert_lines, *lines[insert_at:]]
    return "\n".join(updated_lines).rstrip() + "\n"


def _find_section(lines: list[str], normalized_heading: str) -> tuple[int, int, int] | None:
    scanner = MarkdownHeadingScanner(ignore_fenced_blocks=True)
    found: tuple[int, int] | None = None
    for index, line in enumerate(lines):
        heading = scanner.match(line)
        if heading is None:
            continue
        if found is None:
            if heading.normalized == normalized_heading and not heading.is_document_title:
                found = (index, heading.level)
            continue
        _, found_level = found
        if heading.level <= found_level:
            return found[0], index, found_level
    if found is None:
        return None
    return found[0], len(lines), found[1]


def _available_destination(destination_dir: Path, source_file: Path) -> Path:
    safe_name = _safe_filename(source_file.name)
    candidate = destination_dir / safe_name
    if _is_available_write_path(candidate) or _is_same_regular_file(candidate, source_file):
        return candidate
    if _is_regular_file(candidate) and _file_sha256(candidate) == _file_sha256(source_file):
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        numbered = destination_dir / f"{stem}-{index}{suffix}"
        if _is_available_write_path(numbered):
            return numbered
        if _is_regular_file(numbered) and _file_sha256(numbered) == _file_sha256(source_file):
            return numbered
    raise ValueError(f"could not choose a unique asset filename under {destination_dir}")


def _is_available_write_path(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _is_same_regular_file(path: Path, source_file: Path) -> bool:
    return _is_regular_file(path) and path.resolve() == source_file


def _safe_filename(name: str) -> str:
    path = Path(name)
    stem = _safe_stem(path.stem)
    suffix = _safe_suffix(path.suffix)
    return f"{stem}{suffix}"


def _safe_stem(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_value).strip("._-")
    safe = re.sub(r"-{2,}", "-", safe)
    return safe or "asset"


def _safe_suffix(value: str) -> str:
    suffix = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix)
    if not suffix.startswith("."):
        suffix = f".{suffix}" if suffix else ""
    return suffix


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in {"reference", "delivery"}:
        raise ValueError("asset kind must be 'reference' or 'delivery'")
    return normalized


def _normalize_target(value: str, *, project_root: Path) -> str:
    target = Path(value.strip())
    if target.is_absolute():
        raise ValueError("--target must be project-relative")
    candidate = (project_root / target).resolve(strict=False)
    return _project_relative_path(candidate, project_root)


def _ensure_inside_project(path: Path, project_root: Path, *, label: str) -> None:
    try:
        path.resolve(strict=False).relative_to(project_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the project root: {path}") from exc


def _project_relative_path(path: Path, project_root: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(project_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"path must be inside the project root: {path}") from exc
    return relative.as_posix()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
