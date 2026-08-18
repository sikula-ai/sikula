"""Deterministic source-asset assignment for delivery unit tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

from core.markdown_document import ParsedMarkdownDocument, parse_markdown_document
from core.markdown_headings import normalize_heading
from core.task_assets import (
    StructuredAssetDeclaration,
    parse_structured_asset_declarations,
    parse_structured_asset_path_value,
)


_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_CANONICAL_DECLARATION_RE = re.compile(
    r"^- (Path|Asset|Reference asset|Delivery asset): `([^`\r\n]+)`[ \t]*$",
    re.IGNORECASE,
)
_DECLARATION_LIKE_RE = re.compile(
    r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+"
    r"(?P<label>Path|Asset|Reference asset|Delivery asset)[ \t]*:(?P<value>.*)$",
    re.IGNORECASE,
)
_LINKED_HEADING_RE = re.compile(r"^\[([^\]]+)\]\([^\r\n]+\)$")
_HEADING_ATTRIBUTE_RE = re.compile(r"[ \t]*\{[^{}\r\n]*\}[ \t]*$")
_ASSET_ROOT_HEADINGS = {"asset", "assets", "task asset", "task assets", "asset manifest"}
_ASSET_SUBSECTIONS = {"reference assets", "delivery assets"}


@dataclass(frozen=True)
class _AssetSemantics:
    kind: str
    target_specified: bool
    requested_target: str
    provenance_specified: bool
    source_license: str
    declared_sha256: str


@dataclass(frozen=True)
class DeliveryAssetAssignmentUnit:
    unit_id: str
    task_markdown: str
    asset_paths: list[str]


@dataclass(frozen=True)
class _SourceAsset:
    project_path: str
    raw_path: str
    root_heading: str
    subsection: str | None
    lines: tuple[str, ...]
    line: int
    semantics: _AssetSemantics | None = None


class DeliveryAssetAssignmentError(ValueError):
    def __init__(self, code: str, message: str, *, unit_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.unit_id = unit_id


def render_delivery_asset_assignments(
    source_task_description: str,
    units: list[DeliveryAssetAssignmentUnit],
    *,
    source_task_path: str | Path | None,
    project_root: Path,
    project_config: dict | None,
    allow_source_asset_manifest: bool = False,
) -> dict[str, str]:
    """Append exact source declarations to the units selected by authoring."""
    del source_task_path, project_config
    root = project_root.resolve()
    source_assets = _parse_source_assets(
        source_task_description,
        root,
        allow_manifest=allow_source_asset_manifest,
    )
    source_by_path = {asset.project_path: asset for asset in source_assets}
    aliases = _source_aliases(source_assets, root)

    assigned_paths: set[str] = set()
    assigned_by_unit: dict[str, set[str]] = {}
    for unit in units:
        _validate_unit_markdown(unit, aliases)
        canonical_paths: list[str] = []
        for alias in unit.asset_paths:
            project_path = aliases.get(_alias_key(alias))
            if project_path is None:
                try:
                    project_path = _canonical_project_path(alias, root)
                except ValueError:
                    project_path = None
            if project_path not in source_by_path:
                raise DeliveryAssetAssignmentError(
                    "asset_assignment_unknown",
                    "A delivery unit assigns an asset that is not declared by the source task.",
                    unit_id=unit.unit_id,
                )
            canonical_paths.append(project_path)

        canonical_path_set = set(canonical_paths)
        if len(canonical_path_set) != len(canonical_paths):
            raise DeliveryAssetAssignmentError(
                "asset_assignment_duplicate",
                "A delivery unit assigns the same source asset more than once.",
                unit_id=unit.unit_id,
            )
        assigned_by_unit[unit.unit_id] = canonical_path_set
        assigned_paths.update(canonical_path_set)

    if set(source_by_path) - assigned_paths:
        raise DeliveryAssetAssignmentError(
            "source_asset_unassigned",
            "At least one source task asset is not assigned to any delivery unit.",
        )

    rendered: dict[str, str] = {}
    for unit in units:
        assigned = assigned_by_unit[unit.unit_id]
        selected = [asset for asset in source_assets if asset.project_path in assigned]
        task_markdown, asset_heading_line = _append_assigned_assets(unit.task_markdown, selected)
        if asset_heading_line is not None:
            _assert_rendered_assets_visible(
                task_markdown,
                asset_heading_line,
                selected,
                unit.unit_id,
                project_root=root,
            )
        rendered[unit.unit_id] = task_markdown
    return rendered


def _parse_source_assets(markdown: str, project_root: Path, *, allow_manifest: bool) -> list[_SourceAsset]:
    document = parse_markdown_document(markdown)
    raw_lines = list(document.lines)
    visible_lines = list(document.visible_lines)
    headings = document.headings_by_line()
    list_items = document.list_items_by_line()
    assets: list[_SourceAsset] = []
    in_assets = False
    root_heading: str | None = None
    manifest_seen = False
    subsection: str | None = None
    index = 0

    while index < len(raw_lines):
        line = visible_lines[index]
        if index in document.hidden_lines:
            if in_assets:
                raise _noncanonical_source_asset()
            index += 1
            continue
        scanned_heading = headings.get(index)
        heading = _heading(line) if scanned_heading is not None and scanned_heading.is_markdown else None
        if scanned_heading is not None and not scanned_heading.is_document_title:
            heading_name = _asset_heading_name(scanned_heading.raw)
            canonical_root = heading == (2, "Assets") or (allow_manifest and heading == (2, "Asset manifest"))
            if heading_name in _ASSET_ROOT_HEADINGS and not canonical_root:
                if heading_name == "asset manifest" and not allow_manifest:
                    raise DeliveryAssetAssignmentError(
                        "source_asset_manifest_reserved",
                        "The source task must not contain the reserved Asset manifest section.",
                    )
                raise _noncanonical_source_asset()
            if (
                in_assets
                and heading_name in _ASSET_SUBSECTIONS
                and (heading is None or heading[0] != 3 or heading[1].casefold() != heading_name)
            ):
                raise _noncanonical_source_asset()
            if in_assets and heading is None:
                in_assets = False
                root_heading = None
                subsection = None
                index += 1
                continue
        if heading is not None:
            level, title = heading
            normalized = title.casefold()
            if normalized == "asset manifest" and not (
                allow_manifest or (scanned_heading is not None and scanned_heading.is_document_title)
            ):
                raise DeliveryAssetAssignmentError(
                    "source_asset_manifest_reserved",
                    "The source task must not contain the reserved Asset manifest section.",
                )
            if level == 2:
                in_assets = normalized == "assets" or (allow_manifest and normalized == "asset manifest")
                root_heading = title if in_assets else None
                manifest_seen = manifest_seen or (allow_manifest and normalized == "asset manifest")
                subsection = None
            elif in_assets and level == 3:
                if normalized not in _ASSET_SUBSECTIONS:
                    raise _noncanonical_source_asset()
                subsection = title
            elif in_assets:
                if level <= 2:
                    in_assets = False
                    root_heading = None
                    subsection = None
                else:
                    raise _noncanonical_source_asset()
            index += 1
            continue

        list_item = list_items.get(index)
        if list_item is None:
            if in_assets and line.strip():
                raise _noncanonical_source_asset()
            index += 1
            continue
        if not _looks_like_asset_declaration(line, allow_path=in_assets):
            if in_assets and (
                list_item.parent_start_line is not None
                or _list_item_contains_asset_declaration(document, index, list_item.end_line)
            ):
                raise _noncanonical_source_asset()
            index = list_item.end_line
            continue

        declaration = _CANONICAL_DECLARATION_RE.fullmatch(line)
        if not in_assets or declaration is None:
            raise _noncanonical_source_asset()

        raw_path = declaration.group(2).strip()
        if parse_structured_asset_path_value(raw_path) is None:
            raise DeliveryAssetAssignmentError(
                "source_asset_path_invalid",
                "A source task asset path must use a supported local asset format.",
            )
        try:
            project_path = _canonical_project_path(raw_path, project_root)
        except ValueError:
            raise DeliveryAssetAssignmentError(
                "source_asset_path_invalid",
                "A source task asset path must resolve inside the project.",
            ) from None

        block = [raw_lines[index]]
        cursor = index + 1
        while cursor < list_item.end_line:
            child = visible_lines[cursor]
            if cursor in document.hidden_lines:
                raise _noncanonical_source_asset()
            if not child.strip():
                cursor += 1
                continue
            child_item = list_items.get(cursor)
            if (
                child_item is None
                or child_item.parent_start_line != index
                or not child.startswith("  - ")
                or not child[4:].strip()
                or _looks_like_asset_declaration(child, allow_path=True)
                or any(line_index in document.hidden_lines for line_index in range(cursor, child_item.end_line))
            ):
                raise _noncanonical_source_asset()
            block.extend(raw_lines[cursor : child_item.end_line])
            cursor = child_item.end_line
        if root_heading is None:
            raise _noncanonical_source_asset()
        assets.append(
            _SourceAsset(
                project_path=project_path,
                raw_path=raw_path,
                root_heading=root_heading,
                subsection=subsection,
                lines=tuple(block),
                line=index + 1,
            )
        )
        index = list_item.end_line

    assets = _bind_source_asset_semantics(markdown, assets, project_root, allow_manifest=allow_manifest)
    return _merge_source_assets(assets, prefer_manifest=manifest_seen)


def _bind_source_asset_semantics(
    markdown: str,
    assets: list[_SourceAsset],
    project_root: Path,
    *,
    allow_manifest: bool,
) -> list[_SourceAsset]:
    document_kind = "implementation_contract" if allow_manifest else "task_description"
    declarations = parse_structured_asset_declarations(markdown, document_kind=document_kind)
    declarations_by_line = {declaration.line: declaration for declaration in declarations}
    if len(declarations_by_line) != len(declarations) or set(declarations_by_line) != {asset.line for asset in assets}:
        raise _noncanonical_source_asset()

    bound: list[_SourceAsset] = []
    for asset in assets:
        declaration = declarations_by_line[asset.line]
        try:
            declaration_path = _canonical_project_path(declaration.path, project_root)
        except ValueError:
            raise DeliveryAssetAssignmentError(
                "source_asset_path_invalid",
                "A source task asset path must resolve inside the project.",
            ) from None
        if declaration_path != asset.project_path or _content_lines(declaration.source_lines) != _content_lines(
            asset.lines
        ):
            raise _noncanonical_source_asset()
        bound.append(replace(asset, semantics=_asset_semantics(declaration)))
    return bound


def _merge_source_assets(assets: list[_SourceAsset], *, prefer_manifest: bool) -> list[_SourceAsset]:
    grouped: dict[str, list[_SourceAsset]] = {}
    for asset in assets:
        grouped.setdefault(asset.project_path, []).append(asset)

    merged: list[_SourceAsset] = []
    for declarations in grouped.values():
        manifest_declarations = [asset for asset in declarations if asset.root_heading == "Asset manifest"]
        source_declarations = [asset for asset in declarations if asset.root_heading != "Asset manifest"]
        if len(manifest_declarations) > 1 or len(source_declarations) > 1:
            raise _source_asset_conflict()
        if manifest_declarations and source_declarations:
            merged.append(_merge_manifest_asset(manifest_declarations[0], source_declarations[0]))
        else:
            merged.append(declarations[0])
    if not prefer_manifest:
        return merged
    return [replace(asset, root_heading="Asset manifest") for asset in merged]


def _validate_unit_markdown(unit: DeliveryAssetAssignmentUnit, source_aliases: dict[str, str]) -> None:
    document = parse_markdown_document(unit.task_markdown)
    for _line, heading in document.headings:
        if not heading.is_document_title and _asset_heading_name(heading.raw) in _ASSET_ROOT_HEADINGS:
            raise _unit_asset_section_forbidden(unit.unit_id)
    if parse_structured_asset_declarations(unit.task_markdown, document_kind="all"):
        raise _unit_asset_section_forbidden(unit.unit_id)
    for index in document.list_item_lines:
        line = document.visible_lines[index]
        if _looks_like_unit_asset_declaration(line, source_aliases):
            raise _unit_asset_section_forbidden(unit.unit_id)


def _unit_asset_section_forbidden(unit_id: str) -> DeliveryAssetAssignmentError:
    return DeliveryAssetAssignmentError(
        "unit_asset_section_forbidden",
        "Generated unit task Markdown must not include asset declarations.",
        unit_id=unit_id,
    )


def _noncanonical_source_asset() -> DeliveryAssetAssignmentError:
    return DeliveryAssetAssignmentError(
        "source_asset_noncanonical",
        "Delivery source assets must use direct canonical list items under ## Assets.",
    )


def _looks_like_asset_declaration(line: str, *, allow_path: bool) -> bool:
    match = _DECLARATION_LIKE_RE.match(line)
    if match is None:
        return False
    if allow_path:
        return True
    if match.group("label").casefold() == "path":
        return False
    return parse_structured_asset_path_value(match.group("value")) is not None


def _looks_like_unit_asset_declaration(line: str, source_aliases: dict[str, str]) -> bool:
    match = _DECLARATION_LIKE_RE.match(line)
    if match is None:
        return False
    label = match.group("label").casefold()
    if label in {"reference asset", "delivery asset"}:
        return True
    parsed_path = parse_structured_asset_path_value(match.group("value"))
    if label == "path":
        return parsed_path is not None and _alias_key(parsed_path) in source_aliases
    return parsed_path is not None or _CANONICAL_DECLARATION_RE.fullmatch(line) is not None


def _list_item_contains_asset_declaration(
    document: ParsedMarkdownDocument,
    start_line: int,
    end_line: int,
) -> bool:
    return any(
        start_line < item.start_line < end_line
        and _looks_like_asset_declaration(document.visible_lines[item.start_line], allow_path=True)
        for item in document.list_items
    )


def _source_aliases(assets: list[_SourceAsset], project_root: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for asset in assets:
        candidates = {asset.raw_path, unquote(asset.raw_path), asset.project_path}
        candidates.add(str(project_root / asset.project_path))
        for candidate in candidates:
            key = _alias_key(candidate)
            existing = aliases.get(key)
            if existing is not None and existing != asset.project_path:
                raise DeliveryAssetAssignmentError(
                    "source_asset_conflict",
                    "Source asset path aliases must identify one project asset.",
                )
            aliases[key] = asset.project_path
    return aliases


def _alias_key(path: str) -> str:
    return PureWindowsPath(unquote(path.strip())).as_posix()


def _canonical_project_path(path: str, project_root: Path) -> str:
    candidate = unquote(path.strip())
    windows_path = PureWindowsPath(candidate)
    if not windows_path.is_absolute() and urlsplit(candidate).scheme:
        raise ValueError("URI path")
    if windows_path.is_absolute() and not Path(candidate).is_absolute():
        if project_root.drive.casefold() != windows_path.drive.casefold():
            raise ValueError("foreign absolute path")
        candidate_path = Path(windows_path.as_posix())
    else:
        candidate_path = Path(windows_path.as_posix())
    resolved = candidate_path.resolve() if candidate_path.is_absolute() else (project_root / candidate_path).resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise ValueError("path outside project") from exc


def _append_assigned_assets(task_markdown: str, assets: list[_SourceAsset]) -> tuple[str, int | None]:
    if not assets:
        return task_markdown, None
    prefix = task_markdown.rstrip()
    asset_heading_line = len(prefix.splitlines()) + 1
    lines = [prefix]
    active_group: tuple[str, str | None] | None = None
    for asset in assets:
        group = (asset.root_heading, asset.subsection)
        if group != active_group:
            if lines[-1]:
                lines.append("")
            reopen_root = (
                active_group is None
                or asset.root_heading != active_group[0]
                or (active_group[1] is not None and asset.subsection is None)
            )
            if reopen_root:
                lines.extend([f"## {asset.root_heading}", ""])
            if asset.subsection is not None:
                lines.extend([f"### {asset.subsection}", ""])
            active_group = group
        elif lines[-1]:
            lines.append("")
        lines.extend(asset.lines)
    return "\n".join(lines).rstrip() + "\n", asset_heading_line


def _assert_rendered_assets_visible(
    task_markdown: str,
    asset_heading_line: int,
    assets: list[_SourceAsset],
    unit_id: str,
    *,
    project_root: Path,
) -> None:
    document = parse_markdown_document(task_markdown)
    heading = document.headings_by_line().get(asset_heading_line)
    expected_root = assets[0].root_heading
    if heading is None or not heading.is_markdown or heading.level != 2 or heading.raw != expected_root:
        raise _unit_asset_render_invalid(unit_id)
    cursor = asset_heading_line + 1
    for asset in assets:
        declaration_line = _find_line(document, asset.lines[0], start=cursor)
        if declaration_line is None or declaration_line not in document.list_item_lines:
            raise _unit_asset_render_invalid(unit_id)
        cursor = declaration_line + 1
    declarations = parse_structured_asset_declarations(task_markdown, document_kind="all")
    if len(declarations) != len(assets):
        raise _unit_asset_render_invalid(unit_id)
    for asset, declaration in zip(assets, declarations, strict=True):
        try:
            declaration_path = _canonical_project_path(declaration.path, project_root)
        except ValueError:
            raise _unit_asset_render_invalid(unit_id) from None
        if (
            asset.semantics is None
            or declaration_path != asset.project_path
            or _asset_semantics(declaration) != asset.semantics
            or _content_lines(declaration.source_lines) != _content_lines(asset.lines)
        ):
            raise _unit_asset_render_invalid(unit_id)


def _asset_semantics(declaration: StructuredAssetDeclaration) -> _AssetSemantics:
    return _AssetSemantics(
        kind=declaration.kind,
        target_specified=declaration.target_specified,
        requested_target=declaration.requested_target,
        provenance_specified=declaration.provenance_specified,
        source_license=declaration.source_license,
        declared_sha256=declaration.declared_sha256,
    )


def _merge_manifest_asset(manifest: _SourceAsset, source: _SourceAsset) -> _SourceAsset:
    if manifest.semantics is None or source.semantics is None:
        raise _source_asset_conflict()
    semantics = _AssetSemantics(
        kind=_merge_asset_kind(manifest.semantics.kind, source.semantics.kind),
        target_specified=manifest.semantics.target_specified or source.semantics.target_specified,
        requested_target=_merge_asset_value(
            manifest.semantics.requested_target,
            source.semantics.requested_target,
        ),
        provenance_specified=(manifest.semantics.provenance_specified or source.semantics.provenance_specified),
        source_license=_merge_asset_value(
            manifest.semantics.source_license,
            source.semantics.source_license,
        ),
        declared_sha256=_merge_asset_value(
            manifest.semantics.declared_sha256,
            source.semantics.declared_sha256,
        ),
    )
    preserve_source_kind = manifest.semantics.kind == "ambiguous" and source.semantics.kind != "ambiguous"
    declaration_line = source.lines[0] if preserve_source_kind else manifest.lines[0]
    subsection = source.subsection if preserve_source_kind and source.subsection is not None else manifest.subsection
    return replace(
        manifest,
        subsection=subsection,
        lines=(declaration_line, *manifest.lines[1:], *source.lines[1:]),
        semantics=semantics,
    )


def _merge_asset_kind(primary: str, supplemental: str) -> str:
    explicit = {kind for kind in (primary, supplemental) if kind != "ambiguous"}
    if len(explicit) > 1:
        raise _source_asset_conflict()
    return next(iter(explicit), "ambiguous")


def _merge_asset_value(primary: str, supplemental: str) -> str:
    if primary and supplemental and primary != supplemental:
        raise _source_asset_conflict()
    return primary or supplemental


def _source_asset_conflict() -> DeliveryAssetAssignmentError:
    return DeliveryAssetAssignmentError(
        "source_asset_conflict",
        "Repeated source asset declarations contain conflicting constraints.",
    )


def _content_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line for line in lines if line.strip())


def _find_line(document: ParsedMarkdownDocument, value: str, *, start: int) -> int | None:
    for index in range(start, len(document.lines)):
        if document.lines[index] == value:
            return index
    return None


def _unit_asset_render_invalid(unit_id: str) -> DeliveryAssetAssignmentError:
    return DeliveryAssetAssignmentError(
        "unit_asset_render_invalid",
        "Generated unit task Markdown does not preserve its assigned source assets.",
        unit_id=unit_id,
    )


def _heading(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING_RE.fullmatch(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _asset_heading_name(raw_heading: str) -> str:
    undecorated = _HEADING_ATTRIBUTE_RE.sub("", raw_heading.strip())
    linked = _LINKED_HEADING_RE.fullmatch(undecorated)
    return normalize_heading(linked.group(1) if linked is not None else undecorated)
