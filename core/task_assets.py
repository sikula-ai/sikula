"""Deterministic task asset parsing and contract manifest helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import mimetypes
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


class AssetKind(str, Enum):
    REFERENCE = "reference"
    DELIVERY = "delivery"
    AMBIGUOUS = "ambiguous"


class AssetStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    OUTSIDE_PROJECT = "outside_project"
    NOT_FILE = "not_file"


@dataclass(frozen=True)
class AssetReference:
    path: str
    line: int
    kind: str = AssetKind.AMBIGUOUS.value
    status: str = ""
    project_path: str = ""
    raw_paths: list[str] = field(default_factory=list)
    match_aliases: list[str] = field(default_factory=list)
    target_specified: bool = False
    requested_target: str = ""
    provenance_specified: bool = False
    source_license: str = ""
    sha256: str = ""
    size_bytes: int | None = None
    mime_type: str = ""
    git_status: str = ""

    def to_dict(self, *, include_internal: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "line": self.line, "kind": self.kind}
        optional_values: list[tuple[str, Any]] = [
            ("project_path", self.project_path),
            ("status", self.status),
            ("target_specified", self.target_specified if self.target_specified else None),
            ("requested_target", self.requested_target),
            ("provenance_specified", self.provenance_specified if self.provenance_specified else None),
            ("source_license", self.source_license),
            ("sha256", self.sha256),
            ("size_bytes", self.size_bytes),
            ("mime_type", self.mime_type),
            ("git_status", self.git_status),
        ]
        for key, value in optional_values:
            if value is not None and value != "":
                data[key] = value
        if include_internal:
            if self.raw_paths:
                data["_raw_paths"] = list(self.raw_paths)
            if self.match_aliases:
                data["_match_aliases"] = list(self.match_aliases)
        return data


@dataclass(frozen=True)
class _StructuredAssetDeclaration:
    path: str
    line: int
    kind: str = AssetKind.AMBIGUOUS.value
    raw_path: str = ""
    target_specified: bool = False
    requested_target: str = ""
    provenance_specified: bool = False
    source_license: str = ""


def _answer_text(answer: dict[str, Any] | None) -> str:
    if not isinstance(answer, dict):
        return ""
    value = answer.get("answer", "")
    return str(value).strip() if value is not None else ""


def _answer_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _clean_answer_bullet(value: str) -> str:
    return re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", value).strip()


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_heading(value: str) -> str:
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_TEXT_HEADING_RE = re.compile(r"^\s{0,3}([A-Za-z][A-Za-z0-9 /&_-]{1,60}):\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_FENCED_BLOCK_RE = re.compile(r"^\s{0,3}(```+|~~~+)")
_ASSET_REFERENCE_SECTION_HEADINGS = {"reference asset", "reference assets"}
_ASSET_DELIVERY_SECTION_HEADINGS = {"delivery asset", "delivery assets"}
_ASSET_MANIFEST_BODY_HEADINGS = {
    "asset manifest",
    *_ASSET_REFERENCE_SECTION_HEADINGS,
    *_ASSET_DELIVERY_SECTION_HEADINGS,
}
_ASSET_MANIFEST_BODY_HEADING_HINT_RE = re.compile(
    r"\bmanifest\b|\basset entries?\b|\basset metadata\b",
    re.IGNORECASE,
)
_ASSET_PATH_FIELD_LABEL_KINDS = {
    "path": "",
    "asset": "",
    "reference asset": AssetKind.REFERENCE.value,
    "delivery asset": AssetKind.DELIVERY.value,
}
_ASSET_USAGE_FIELD_LABELS = {"usage", "use"}
_ASSET_TARGET_FIELD_LABELS = {"target", "target path", "destination", "destination path", "requested target"}
_ASSET_PROVENANCE_FIELD_LABELS = {"source/license", "source license", "license", "licence", "provenance"}
_ASSET_MANIFEST_METADATA_FIELD_LABELS = {"sha256", "sha 256", "purpose", "target resolution"}
_ASSET_STRUCTURED_ROOT_HEADINGS = {"asset", "assets", "task asset", "task assets", "asset manifest"}
_ASSET_ROOT_SECTION_HEADINGS = {
    "asset",
    "assets",
    "attachment",
    "attachments",
    "task asset",
    "task assets",
    "task attachment",
    "task attachments",
}
_ASSET_SUBSECTION_HEADINGS = {
    *_ASSET_REFERENCE_SECTION_HEADINGS,
    *_ASSET_DELIVERY_SECTION_HEADINGS,
    "mockup",
    "mockups",
    "screenshot",
    "screenshots",
}
_ASSET_EXTENSIONS = {
    ".avif",
    ".csv",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".otf",
    ".pdf",
    ".png",
    ".svg",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
    ".yaml",
    ".yml",
    ".zip",
}
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_PLAIN_ASSET_PATH_RE = re.compile(
    r"(?<![\w/.-])"
    r"((?:\.{1,2}/|\.sikula/|[A-Za-z0-9_.-]+/)"
    r"[A-Za-z0-9_./@%+=:, -]*?"
    r"\.(?:avif|csv|gif|ico|jpe?g|json|md|otf|pdf|png|svg|ttf|txt|webp|woff2?|xml|ya?ml|zip))"
    r"(?![\w/.-])",
    re.IGNORECASE,
)
_ASSET_FILENAME_RE = re.compile(
    r"(?<![\w/.-])"
    r"([A-Za-z0-9_.@%+=-]+\.(?:avif|csv|gif|ico|jpe?g|json|md|otf|pdf|png|svg|ttf|txt|webp|woff2?|xml|ya?ml|zip))"
    r"(?![\w/.-])",
    re.IGNORECASE,
)
_ASSET_REFERENCE_HINT_RE = re.compile(
    r"\b(reference assets?|reference[-\s]+only|do[-\s]+not[-\s]+copy|screenshots?|mockups?|design reference|"
    r"layout reference|spec excerpt)\b",
    re.IGNORECASE,
)
_ASSET_DELIVERY_HINT_RE = re.compile(
    r"\bdelivery asset\b|\buse\b.+\b(?:as|for)\b|\buse this file\b|\bcopy\b|\binclude\b|\bship\b|"
    r"\bproduction asset\b|\btarget\s*:|\bsource/license\s*:|\bprovided by\b",
    re.IGNORECASE,
)
_ASSET_UNDECLARED_HINT_RE = re.compile(
    r"\b(reference assets?|reference[-\s]+only|do[-\s]+not[-\s]+copy|design reference|"
    r"layout reference|visual reference|reference (?:asset|image|screenshot|mockup)|spec excerpt|"
    r"delivery assets?|production asset|source/license\s*:)\b",
    re.IGNORECASE,
)
_GENERATED_ANSWER_ENTRY_END_MARKER = "<!-- /sikula:generated-answer -->"


def public_asset_reference(reference: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in reference.items() if not str(key).startswith("_")}


def _generated_answer_entry_marker(question_id: str) -> str:
    return f"<!-- sikula:generated-answer: {question_id} -->"


def asset_reference_ready_for_manifest(reference: dict[str, Any]) -> bool:
    if reference.get("status") != "available":
        return False
    kind = reference.get("kind")
    if kind == "reference":
        return True
    if kind == "delivery":
        return bool(str(reference.get("source_license") or "").strip())
    return False


def asset_manifest_reference_lines(reference: dict[str, Any]) -> list[str]:
    project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
    kind = str(reference.get("kind") or "reference").strip()
    lines = [f"- Path: `{project_path}`"]

    sha256_value = str(reference.get("sha256") or "").strip()
    if sha256_value:
        lines.append(f"  - SHA-256: `{sha256_value}`")

    lines.append(_asset_manifest_purpose_line(kind))

    if kind == "delivery":
        lines.append("  - Usage: delivery asset; use this file only for the requested implementation.")
    else:
        lines.append("  - Usage: reference only; do not copy this asset into production files.")

    requested_target = str(reference.get("requested_target") or "").strip()
    if requested_target:
        lines.append(f"  - Requested target: `{requested_target}`")
    elif kind == "delivery":
        lines.append("  - Target resolution: analyst should choose the project-conventional location.")
    source_license = str(reference.get("source_license") or "").strip()
    if kind == "delivery" and source_license:
        lines.append(f"  - Source/license: {source_license}")
    return lines


def _asset_manifest_purpose_line(kind: str) -> str:
    if kind == "delivery":
        return "  - Purpose: delivery asset referenced by the implementation contract."
    return "  - Purpose: reference context for the implementation contract."


def asset_answer_entry_lines(
    asset_references: list[dict[str, Any]],
    answers: dict[str, dict[str, Any]],
    *,
    project_root: Path,
) -> list[str]:
    lines: list[str] = []

    declarations_answer = _answer_text(answers.get("assets.declarations", {}))
    if declarations_answer:
        declaration_entries = _asset_declaration_answer_entries(declarations_answer, project_root=project_root)
        if declaration_entries:
            lines.append(_generated_answer_entry_marker("assets.declarations"))
            lines.extend(_asset_answer_entries_with_subsections(declaration_entries))
            lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    local_files_answer = _answer_text(answers.get("assets.local_files", {}))
    metadata_references = asset_references
    if local_files_answer:
        metadata_references = _asset_references_with_local_file_replacements(
            asset_references,
            local_files_answer,
            project_root=project_root,
        )
        lines.append(_generated_answer_entry_marker("assets.local_files"))
        lines.extend(
            _asset_answer_entries_with_subsections(
                _asset_local_file_answer_entries(local_files_answer, asset_references, project_root=project_root)
            )
        )
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    intent_answer = _answer_text(answers.get("assets.intent", {}))
    if intent_answer:
        question_id = "assets.intent"
        lines.append(_generated_answer_entry_marker(question_id))
        lines.extend(
            _asset_answer_entries_with_subsections(
                _asset_intent_answer_entries(intent_answer, metadata_references, project_root=project_root)
            )
        )
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    provenance = _answer_text(answers.get("assets.provenance", {}))
    if provenance:
        delivery_refs = [
            reference
            for reference in metadata_references
            if reference.get("kind") == "delivery" and not reference.get("source_license")
        ]
        lines.append(_generated_answer_entry_marker("assets.provenance"))
        if delivery_refs:
            lines.extend(
                _asset_answer_entries_with_subsections(
                    _asset_provenance_answer_entries(provenance, delivery_refs, project_root=project_root)
                )
            )
        else:
            for answer_line in _answer_lines(provenance):
                lines.append(f"- {_clean_answer_bullet(answer_line)}")
        lines.append(_GENERATED_ANSWER_ENTRY_END_MARKER)

    return lines


def _asset_answer_entries_with_subsections(entries: list[str]) -> list[str]:
    blocks = _asset_answer_entry_blocks(entries)
    grouped: dict[str, list[list[str]]] = {"reference": [], "delivery": [], "ambiguous": []}
    for block in blocks:
        grouped.setdefault(_asset_answer_entry_block_kind(block), []).append(block)

    lines: list[str] = []
    for kind, heading in (
        ("reference", "### Reference assets"),
        ("delivery", "### Delivery assets"),
        ("ambiguous", "### Assets to classify"),
    ):
        kind_blocks = grouped.get(kind, [])
        if not kind_blocks:
            continue
        if lines:
            lines.append("")
        lines.extend([heading, ""])
        for block_index, block in enumerate(kind_blocks):
            if block_index:
                lines.append("")
            lines.extend(block)
    return lines


def _asset_answer_entry_blocks(entries: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in entries:
        if line.startswith("- ") and current:
            blocks.append(current)
            current = [line]
            continue
        if current:
            current.append(line)
        else:
            current = [line]
    if current:
        blocks.append(current)
    return blocks


def _asset_answer_entry_block_kind(block: list[str]) -> str:
    if not block:
        return "ambiguous"
    field = _structured_asset_field(_clean_answer_bullet(block[0]))
    if field is not None:
        label, _value = field
        label_kind = _structured_asset_path_label_kind(label)
        if label_kind:
            return label_kind
    for line in block[1:]:
        field = _structured_asset_field(_clean_answer_bullet(line))
        if field is None:
            continue
        label, value = field
        if label in _ASSET_USAGE_FIELD_LABELS:
            usage_kind = _structured_asset_usage_kind(value)
            if usage_kind:
                return usage_kind
    return AssetKind.AMBIGUOUS.value


def _asset_declaration_answer_entries(answer_text: str, *, project_root: Path) -> list[str]:
    entries: list[str] = []
    for answer_line in _answer_lines(answer_text):
        entry = _asset_declaration_answer_entry(answer_line, project_root=project_root)
        if entry:
            entries.append(entry)
    return entries


def _asset_declaration_answer_entry(answer_line: str, *, project_root: Path) -> str:
    cleaned = _clean_answer_bullet(answer_line)
    field = _structured_asset_field(cleaned)
    if field is not None:
        label, value = field
        if label in _ASSET_PATH_FIELD_LABEL_KINDS:
            path = _asset_path_from_answer_line(value, project_root=project_root)
            if path:
                return f"- {_structured_asset_path_output_label(label)}: `{path}`"

    path = _asset_path_from_answer_line(cleaned, project_root=project_root)
    if not path:
        return ""
    output_label = "Asset"
    usage_kind = _structured_asset_usage_kind(cleaned)
    if usage_kind == AssetKind.REFERENCE.value:
        output_label = "Reference asset"
    elif usage_kind == AssetKind.DELIVERY.value:
        output_label = "Delivery asset"
    return f"- {output_label}: `{path}`"


def _asset_references_with_local_file_replacements(
    asset_references: list[dict[str, Any]],
    answer_text: str,
    *,
    project_root: Path,
) -> list[dict[str, Any]]:
    replacements = _asset_path_replacements(asset_references, answer_text, project_root=project_root)
    if not replacements:
        return asset_references

    replacement_by_reference_id = {id(reference): replacement_path for reference, replacement_path in replacements}
    updated_references: list[dict[str, Any]] = []
    for reference in asset_references:
        replacement_path = replacement_by_reference_id.get(id(reference))
        if replacement_path is None:
            updated_references.append(reference)
            continue
        updated_references.append(
            _asset_reference_with_replacement_path(
                reference,
                replacement_path,
                project_root=project_root,
            )
        )
    return updated_references


def _asset_reference_with_replacement_path(
    reference: dict[str, Any],
    replacement_path: str,
    *,
    project_root: Path,
) -> dict[str, Any]:
    updated = {key: value for key, value in reference.items() if not str(key).startswith("_")}
    match_aliases = [
        value
        for value in _asset_reference_match_values(reference)
        if value != replacement_path and value != replacement_path.lstrip("./")
    ]
    if match_aliases:
        updated["_match_aliases"] = list(dict.fromkeys(match_aliases))
    updated["path"] = replacement_path
    updated["project_path"] = replacement_path

    resolved_path = project_root / replacement_path
    if not resolved_path.exists():
        updated["status"] = "missing"
        return updated
    if not resolved_path.is_file():
        updated["status"] = "not_file"
        return updated

    updated["status"] = "available"
    updated["sha256"] = _file_sha256(resolved_path)
    updated["size_bytes"] = resolved_path.stat().st_size
    mime_type, _encoding = mimetypes.guess_type(resolved_path.name)
    if mime_type:
        updated["mime_type"] = mime_type
    else:
        updated.pop("mime_type", None)
    updated["git_status"] = _asset_git_status(project_root, replacement_path)
    return updated


def _asset_intent_answer_entries(
    intent_answer: str,
    asset_references: list[dict[str, Any]],
    *,
    project_root: Path,
) -> list[str]:
    answer_lines = _answer_lines(intent_answer)
    ambiguous_refs = [reference for reference in asset_references if reference.get("kind") == "ambiguous"]
    if len(ambiguous_refs) == 1 and len(answer_lines) == 1:
        source_text, intent_text = _asset_intent_answer_parts(answer_lines[0], project_root=project_root)
        answer_line = _clean_answer_bullet(intent_text)
        if not source_text and answer_line and not _asset_path_from_answer_line(answer_line, project_root=project_root):
            project_path = str(ambiguous_refs[0].get("project_path") or ambiguous_refs[0].get("path") or "").strip()
            if project_path:
                return [f"- Asset: `{project_path}`", f"  - Usage: {answer_line}"]

    reference_lookup = _asset_reference_lookup(ambiguous_refs)
    used_reference_ids: set[int] = set()
    entries: list[str] = []
    for answer_line in answer_lines:
        source_text, intent_text = _asset_intent_answer_parts(answer_line, project_root=project_root)
        intent = _clean_answer_bullet(intent_text)
        if not intent:
            continue
        source_key = _asset_answer_source_key(source_text, project_root=project_root)
        if source_key:
            reference = reference_lookup.get(source_key)
            if reference is None or id(reference) in used_reference_ids:
                continue
            project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
            if not project_path:
                continue
            entries.append(f"- Asset: `{project_path}`")
            entries.append(f"  - Usage: {intent}")
            used_reference_ids.add(id(reference))
            continue
        entries.append(f"- {intent}")
    return entries


def _asset_intent_answer_parts(answer_line: str, *, project_root: Path) -> tuple[str, str]:
    cleaned = _clean_answer_bullet(answer_line)
    for separator in ("->", "=>", ":"):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            if separator == ":" and not _asset_answer_source_key(left, project_root=project_root):
                continue
            return left.strip(), right.strip()
    return "", cleaned


def _asset_provenance_answer_entries(
    provenance: str,
    delivery_refs: list[dict[str, Any]],
    *,
    project_root: Path,
) -> list[str]:
    entries: list[str] = []
    path_specific_provenance = _asset_provenance_by_path(
        provenance,
        project_root=project_root,
        asset_references=delivery_refs,
    )
    common_provenance = _single_line(provenance) if not path_specific_provenance else ""

    for reference in delivery_refs:
        project_path = str(reference.get("project_path") or reference.get("path") or "").strip()
        if not project_path:
            continue
        provenance_text = _asset_provenance_for_reference(reference, path_specific_provenance) or common_provenance
        if not provenance_text:
            continue
        entries.append(f"- Delivery asset: `{project_path}`")
        entries.append(f"  - Source/license: {provenance_text}")
    return entries


def _asset_provenance_by_path(
    provenance: str,
    *,
    project_root: Path,
    asset_references: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    provenance_by_path: dict[str, str] = {}
    references_by_basename = _asset_references_by_unique_basename(asset_references or [])
    for answer_line in _answer_lines(provenance):
        cleaned = _clean_answer_bullet(answer_line)
        matched = False
        for candidate in _asset_path_candidates(cleaned):
            normalized = _normalize_asset_path_candidate(candidate)
            if not normalized:
                continue
            normalized = _asset_answer_path_for_project(normalized, project_root=project_root)
            if not normalized:
                continue
            provenance_text = _asset_provenance_text_without_path(cleaned, candidate)
            if not provenance_text:
                continue
            for key in _asset_path_match_keys(normalized):
                provenance_by_path[key] = provenance_text
            matched = True
            break
        if matched:
            continue
        for match in _ASSET_FILENAME_RE.finditer(cleaned):
            candidate = match.group(1)
            reference = references_by_basename.get(candidate)
            if reference is None:
                continue
            provenance_text = _asset_provenance_text_without_path(cleaned, candidate)
            if not provenance_text:
                continue
            for key in _asset_reference_match_keys(reference):
                provenance_by_path[key] = provenance_text
            break
    return provenance_by_path


def _asset_references_by_unique_basename(asset_references: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    reference_by_basename: dict[str, dict[str, Any]] = {}
    basename_counts: dict[str, int] = {}
    for reference in asset_references:
        basenames = {
            Path(str(value or "")).name
            for value in _asset_reference_match_values(reference)
            if Path(str(value or "")).name
        }
        for basename in basenames:
            basename_counts[basename] = basename_counts.get(basename, 0) + 1
            reference_by_basename[basename] = reference
    return {
        basename: reference
        for basename, reference in reference_by_basename.items()
        if basename_counts.get(basename) == 1
    }


def _asset_provenance_for_reference(reference: dict[str, Any], provenance_by_path: dict[str, str]) -> str:
    for value in _asset_reference_match_values(reference):
        for key in _asset_path_match_keys(str(value or "")):
            provenance = provenance_by_path.get(key)
            if provenance:
                return provenance
    return ""


def _asset_path_match_keys(path_text: str) -> set[str]:
    normalized = Path(path_text).as_posix().strip()
    if not normalized:
        return set()
    return {normalized, normalized.lstrip("./")}


def _asset_provenance_text_without_path(answer_line: str, path_text: str) -> str:
    path_index = answer_line.find(path_text)
    if path_index < 0:
        return _single_line(answer_line)
    before = answer_line[:path_index]
    after = answer_line[path_index + len(path_text) :]
    candidate = after if after.strip(" `\"'.,;:-") else before
    candidate = candidate.strip(" `\"'")
    candidate = re.sub(r"^\s*[.,;:-]+\s*", "", candidate)
    candidate = re.sub(
        r"^\s*(?:source/license|source|license|licence|provenance)\s*:\s*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = candidate.strip(" `\"'")
    return _single_line(candidate)


def _replace_unresolved_asset_reference_paths(
    task_text: str,
    asset_references: list[dict[str, Any]],
    answer_text: str,
    *,
    project_root: Path,
) -> str:
    replacements = _asset_path_replacements(asset_references, answer_text, project_root=project_root)
    if not replacements:
        return task_text

    updated = task_text
    for reference, replacement_path in replacements:
        updated = _replace_asset_declaration_token(updated, reference, replacement_path)
    return updated.strip()


def _replace_asset_declaration_token(task_text: str, reference: dict[str, Any], replacement_path: str) -> str:
    lines = task_text.splitlines()
    for line_index, line in enumerate(lines):
        if _asset_line_is_target_metadata(line):
            continue
        lines[line_index] = _replace_asset_token_in_line(line, reference, replacement_path)
    return "\n".join(lines)


def _asset_line_is_target_metadata(line: str) -> bool:
    field = _structured_asset_field(_clean_answer_bullet(line))
    if field is None:
        return False
    label, value = field
    return label in _ASSET_TARGET_FIELD_LABELS and bool(value)


def _replace_asset_token_in_line(line: str, reference: dict[str, Any], replacement_path: str) -> str:
    updated = line
    for token in sorted(_asset_reference_replacement_tokens(reference), key=len, reverse=True):
        updated = _replace_standalone_asset_token(updated, token, replacement_path)
    for candidate in _asset_path_candidates(updated):
        normalized = _normalize_asset_path_candidate(candidate)
        if normalized and _asset_path_match_keys(normalized) & _asset_reference_match_keys(reference):
            updated = updated.replace(candidate, replacement_path, 1)
    return updated


def _replace_standalone_asset_token(line: str, token: str, replacement_path: str) -> str:
    if not token:
        return line
    pattern = re.compile(rf"(?<![\w./@%+=:-]){re.escape(token)}(?![\w/@%+=:-])")
    return pattern.sub(replacement_path, line)


def _asset_reference_replacement_tokens(reference: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for raw_path in reference.get("_raw_paths") or []:
        if isinstance(raw_path, str) and raw_path.strip():
            tokens.append(raw_path.strip())
    normalized_path = str(reference.get("path") or "").strip()
    if normalized_path:
        tokens.append(normalized_path)
    return list(dict.fromkeys(tokens))


def _asset_path_replacements(
    asset_references: list[dict[str, Any]],
    answer_text: str,
    *,
    project_root: Path,
) -> list[tuple[dict[str, Any], str]]:
    unresolved_references = _unresolved_asset_references(asset_references)
    answer_specs = _asset_local_file_answer_specs(answer_text, project_root=project_root)
    reference_lookup = _asset_reference_lookup(unresolved_references)
    used_reference_ids: set[int] = set()
    replacements: list[tuple[dict[str, Any], str]] = []
    for spec in answer_specs:
        replacement_path = spec["replacement_path"]
        reference = _asset_reference_for_answer_spec(
            spec,
            unresolved_references,
            reference_lookup,
            used_reference_ids,
        )
        if reference is None:
            continue
        replacements.append((reference, replacement_path))
        used_reference_ids.add(id(reference))
    return replacements


def _asset_local_file_answer_specs(answer_text: str, *, project_root: Path) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for answer_line in _answer_lines(answer_text):
        source_text, replacement_text = _asset_answer_mapping_parts(answer_line, project_root=project_root)
        replacement_path = _asset_path_from_answer_line(replacement_text, project_root=project_root)
        if not replacement_path:
            continue
        specs.append(
            {
                "source_key": _asset_answer_source_key(source_text, project_root=project_root),
                "replacement_path": replacement_path,
            }
        )
    return specs


def _asset_answer_mapping_parts(answer_line: str, *, project_root: Path) -> tuple[str, str]:
    cleaned = _clean_answer_bullet(answer_line)
    for separator in ("->", "=>"):
        if separator in cleaned:
            left, right = cleaned.split(separator, 1)
            return left.strip(), right.strip()
    if ":" in cleaned:
        left, right = cleaned.split(":", 1)
        if _asset_answer_source_key(left, project_root=project_root) and _asset_path_from_answer_line(
            right,
            project_root=project_root,
        ):
            return left.strip(), right.strip()
    return "", cleaned


def _asset_reference_for_answer_spec(
    spec: dict[str, str],
    unresolved_references: list[dict[str, Any]],
    reference_lookup: dict[str, dict[str, Any]],
    used_reference_ids: set[int],
) -> dict[str, Any] | None:
    source_key = spec.get("source_key")
    if source_key:
        reference = reference_lookup.get(source_key)
        if reference is not None and id(reference) not in used_reference_ids:
            return reference
        return None
    for reference in unresolved_references:
        if id(reference) not in used_reference_ids:
            return reference
    return None


def _asset_reference_lookup(asset_references: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    references_by_key: dict[str, dict[str, Any]] = {}
    key_counts: dict[str, int] = {}
    for reference in asset_references:
        for key in _asset_reference_match_keys(reference):
            key_counts[key] = key_counts.get(key, 0) + 1
            references_by_key[key] = reference
    return {key: reference for key, reference in references_by_key.items() if key_counts.get(key) == 1}


def _asset_reference_match_keys(reference: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in _asset_reference_match_values(reference):
        normalized = Path(str(value or "")).as_posix().strip()
        if not normalized:
            continue
        keys.add(normalized)
        keys.add(normalized.lstrip("./"))
        keys.add(Path(normalized).name)
    return {key for key in keys if key}


def _asset_reference_match_values(reference: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (reference.get("path"), reference.get("project_path")):
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    for field_name in ("_raw_paths", "_match_aliases"):
        field_value = reference.get(field_name)
        if isinstance(field_value, str):
            candidates: Iterable[Any] = (field_value,)
        else:
            candidates = field_value or []
        for value in candidates:
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return list(dict.fromkeys(values))


def _asset_answer_source_key(answer_line: str, *, project_root: Path) -> str:
    cleaned = _clean_answer_bullet(answer_line)
    for candidate in _asset_path_candidates(cleaned):
        normalized = _normalize_asset_path_candidate(candidate)
        if not normalized:
            continue
        normalized = _asset_answer_path_for_project(normalized, project_root=project_root)
        if normalized:
            return normalized
    for match in _ASSET_FILENAME_RE.finditer(cleaned):
        return match.group(1)
    return ""


def _asset_path_from_answer_line(answer_line: str, *, project_root: Path) -> str:
    cleaned = _clean_answer_bullet(answer_line)
    normalized_paths: list[str] = []
    for candidate in _asset_path_candidates(cleaned):
        normalized = _normalize_asset_path_candidate(candidate)
        if not normalized:
            continue
        normalized = _asset_answer_path_for_project(normalized, project_root=project_root)
        if normalized:
            normalized_paths.append(normalized)
    if not normalized_paths:
        return ""
    for normalized in reversed(normalized_paths):
        if (project_root / normalized).is_file():
            return normalized
    return normalized_paths[-1]


def _asset_answer_path_for_project(path_text: str, *, project_root: Path) -> str:
    path = Path(path_text)
    candidate = path if path.is_absolute() else project_root / path
    try:
        relative = candidate.resolve(strict=False).relative_to(project_root.resolve(strict=False))
    except (OSError, ValueError):
        return ""
    return relative.as_posix()


def _asset_local_file_answer_entries(
    answer_text: str,
    asset_references: list[dict[str, Any]],
    *,
    project_root: Path,
) -> list[str]:
    unresolved_references = _unresolved_asset_references(asset_references)
    fallback_kind = _asset_replacement_kind(unresolved_references)
    answer_specs = _asset_local_file_answer_specs(answer_text, project_root=project_root)
    reference_lookup = _asset_reference_lookup(unresolved_references)
    used_reference_ids: set[int] = set()
    entries: list[str] = []
    for spec in answer_specs:
        answer_path = spec["replacement_path"]
        reference = _asset_reference_for_answer_spec(
            spec,
            unresolved_references,
            reference_lookup,
            used_reference_ids,
        )
        if reference is None and spec.get("source_key"):
            continue
        replacement_kind = str(reference.get("kind") or fallback_kind) if reference else fallback_kind
        entries.append(_asset_local_file_answer_line(answer_path, replacement_kind))
        if reference:
            entries.extend(_asset_preserved_metadata_lines(reference))
            used_reference_ids.add(id(reference))
    return entries


def _unresolved_asset_references(asset_references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        reference
        for reference in asset_references
        if reference.get("status") in {"missing", "outside_project", "not_file"}
    ]


def _asset_replacement_kind(asset_references: list[dict[str, Any]]) -> str:
    unresolved_kinds = [str(reference.get("kind") or "").strip() for reference in asset_references]
    if unresolved_kinds and all(kind == "delivery" for kind in unresolved_kinds):
        return "delivery"
    if unresolved_kinds and all(kind == "reference" for kind in unresolved_kinds):
        return "reference"
    return "asset"


def _asset_local_file_answer_line(answer_line: str, replacement_kind: str) -> str:
    if _ASSET_REFERENCE_HINT_RE.search(answer_line) or _ASSET_DELIVERY_HINT_RE.search(answer_line):
        return f"- {answer_line}"

    label = {
        "delivery": "Delivery asset",
        "reference": "Reference asset",
    }.get(replacement_kind, "Asset")
    value = answer_line
    if _normalize_asset_path_candidate(answer_line):
        value = f"`{answer_line}`"
    return f"- {label}: {value}"


def _asset_preserved_metadata_lines(reference: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    requested_target = str(reference.get("requested_target") or "").strip()
    if requested_target:
        lines.append(f"  - Target path: `{requested_target}`")
    source_license = str(reference.get("source_license") or "").strip()
    if source_license:
        lines.append(f"  - Source/license: {source_license}")
    return lines


def detect_asset_references(
    text: str,
    *,
    source_path: Path | str | None,
    project_config: dict | None,
) -> list[dict[str, Any]]:
    project_root = _asset_project_root(source_path, project_config)
    references_by_path: dict[str, dict[str, Any]] = {}
    reference_order: list[str] = []
    for declaration in _parse_structured_asset_declarations(text):
        reference = _asset_reference_metadata(
            declaration.path,
            project_root=project_root,
            line_number=declaration.line,
            raw_path=declaration.raw_path,
            kind=declaration.kind,
            target_specified=declaration.target_specified,
            requested_target=declaration.requested_target,
            provenance_specified=declaration.provenance_specified,
            source_license=declaration.source_license,
        )
        if reference is None:
            continue
        reference_key = _asset_reference_identity_key(reference, declaration.path)
        existing = references_by_path.get(reference_key)
        if existing is None:
            references_by_path[reference_key] = reference
            reference_order.append(reference_key)
        else:
            _merge_asset_reference(existing, reference)
    return [references_by_path[path] for path in reference_order]


def detect_undeclared_asset_paths(
    text: str,
    *,
    project_config: dict | None,
    asset_references: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    declared_keys: set[str] = set()
    for reference in asset_references or []:
        declared_keys.update(_asset_path_match_keys(str(reference.get("path") or "")))
        declared_keys.update(_asset_path_match_keys(str(reference.get("project_path") or "")))

    paths: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    heading_stack: list[tuple[int, str]] = []
    seen_heading = False
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        markdown_heading = _HEADING_RE.match(line)
        text_heading = _TEXT_HEADING_RE.match(line)
        if markdown_heading:
            heading_level = len(markdown_heading.group(1))
            raw_heading = markdown_heading.group(2).strip()
            is_document_title = (
                heading_level == 1
                and not seen_heading
                and _normalize_heading(raw_heading) == "asset manifest"
                and not asset_manifest_h1_has_structured_body(lines, line_index + 1)
            )
            seen_heading = True
            if is_document_title:
                continue
            heading_stack = _asset_update_heading_stack(
                heading_stack,
                level=heading_level,
                heading=raw_heading,
            )
            continue
        if text_heading:
            seen_heading = True
            heading_stack = _asset_update_heading_stack(
                heading_stack,
                level=_asset_text_heading_level(heading_stack),
                heading=text_heading.group(1).strip(),
            )
            continue
        context = _asset_reference_context(heading_stack, lines, line_index)
        in_structured_asset_root = _asset_heading_stack_has_structured_asset_root(heading_stack)
        for raw_path in _asset_path_candidates(line):
            normalized_path = _normalize_asset_path_candidate(raw_path)
            if not normalized_path:
                continue
            if _asset_path_match_keys(normalized_path) & declared_keys:
                continue
            if in_structured_asset_root:
                is_bare_asset_path = _asset_line_is_bare_asset_path(line, normalized_path)
                if not is_bare_asset_path and not _asset_path_in_task_asset_dir(normalized_path, project_config):
                    continue
            elif not _undeclared_asset_path_should_warn(normalized_path, context, project_config):
                continue
            key = (line_index + 1, normalized_path)
            if key in seen:
                continue
            seen.add(key)
            paths.append({"path": normalized_path, "line": line_index + 1})
    return paths


def _asset_line_is_bare_asset_path(line: str, normalized_path: str) -> bool:
    cleaned = _clean_answer_bullet(line).strip()
    markdown_link = _MARKDOWN_LINK_RE.fullmatch(cleaned)
    if markdown_link:
        return _normalize_asset_path_candidate(markdown_link.group(1)) == normalized_path
    code_span = _CODE_SPAN_RE.fullmatch(cleaned)
    if code_span:
        return _normalize_asset_path_candidate(code_span.group(1)) == normalized_path
    cleaned = cleaned.strip("<>`\"'")
    cleaned = cleaned.rstrip(".,;:")
    return _normalize_asset_path_candidate(cleaned) == normalized_path


def _parse_structured_asset_declarations(text: str) -> list[_StructuredAssetDeclaration]:
    declarations: list[_StructuredAssetDeclaration] = []
    heading_stack: list[tuple[int, str]] = []
    seen_heading = False
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        markdown_heading = _HEADING_RE.match(line)
        text_heading = _TEXT_HEADING_RE.match(line)
        if markdown_heading:
            heading_level = len(markdown_heading.group(1))
            raw_heading = markdown_heading.group(2).strip()
            is_document_title = (
                heading_level == 1
                and not seen_heading
                and _normalize_heading(raw_heading) == "asset manifest"
                and not asset_manifest_h1_has_structured_body(lines, line_index + 1)
            )
            seen_heading = True
            if is_document_title:
                continue
            heading_stack = _asset_update_heading_stack(
                heading_stack,
                level=heading_level,
                heading=raw_heading,
            )
            continue
        if text_heading:
            seen_heading = True
            heading_stack = _asset_update_heading_stack(
                heading_stack,
                level=_asset_text_heading_level(heading_stack),
                heading=text_heading.group(1).strip(),
            )
            continue
        if not _asset_heading_stack_has_structured_asset_root(heading_stack):
            continue

        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        field = _structured_asset_field(bullet.group(1))
        if field is None:
            continue
        label, value = field
        label_kind = _structured_asset_path_label_kind(label)
        if label not in _ASSET_PATH_FIELD_LABEL_KINDS:
            continue
        raw_path = _structured_asset_path_value(value)
        path = _normalize_asset_path_candidate(raw_path or "") if raw_path else None
        if not path:
            continue

        item_lines = _asset_reference_context_lines(lines, line_index)
        metadata = _structured_asset_item_metadata(item_lines[1:])
        kind = _structured_asset_resolved_kind(
            _structured_asset_section_kind(heading_stack),
            label_kind,
            metadata.get("usage_kind", ""),
        )
        requested_target = metadata.get("requested_target", "")
        source_license = metadata.get("source_license", "")
        declarations.append(
            _StructuredAssetDeclaration(
                path=path,
                line=line_index + 1,
                kind=kind,
                raw_path=raw_path or "",
                target_specified=bool(requested_target),
                requested_target=requested_target,
                provenance_specified=bool(source_license),
                source_license=source_license,
            )
        )
    return declarations


def _structured_asset_item_metadata(lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in lines:
        field = _structured_asset_field(_clean_answer_bullet(line))
        if field is None:
            continue
        label, value = field
        if label in _ASSET_USAGE_FIELD_LABELS and not metadata.get("usage_kind"):
            metadata["usage_kind"] = _structured_asset_usage_kind(value)
        elif label in _ASSET_TARGET_FIELD_LABELS:
            metadata.setdefault("requested_target", _asset_path_metadata_value(value) or "")
        elif label in _ASSET_PROVENANCE_FIELD_LABELS:
            metadata.setdefault("source_license", _structured_asset_text_value(value))
    return metadata


def _structured_asset_field(value: str) -> tuple[str, str] | None:
    if ":" not in value:
        return None
    label, detail = value.split(":", 1)
    normalized_label = _structured_asset_label(label)
    if not normalized_label:
        return None
    return normalized_label, detail.strip()


def _structured_asset_label(value: str) -> str:
    normalized = value.strip().casefold().replace("_", " ").replace("-", " ")
    normalized = re.sub(r"\s*/\s*", "/", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _structured_asset_path_label_kind(label: str) -> str:
    return _ASSET_PATH_FIELD_LABEL_KINDS.get(label, "")


def _structured_asset_path_output_label(label: str) -> str:
    label_kind = _structured_asset_path_label_kind(label)
    if label_kind == AssetKind.REFERENCE.value:
        return "Reference asset"
    if label_kind == AssetKind.DELIVERY.value:
        return "Delivery asset"
    return "Asset"


def _structured_asset_path_value(value: str) -> str | None:
    direct_value = _asset_path_metadata_value(value)
    if direct_value and _normalize_asset_path_candidate(direct_value):
        return direct_value
    for candidate in _asset_path_candidates(value):
        normalized = _normalize_asset_path_candidate(candidate)
        if normalized:
            return candidate
    return None


def _structured_asset_text_value(value: str) -> str:
    cleaned = _single_line(value).strip()
    cleaned = cleaned.strip("`\"'")
    return cleaned.strip()


def _structured_asset_section_kind(heading_stack: list[tuple[int, str]]) -> str:
    for _level, heading in reversed(heading_stack):
        normalized = _normalize_heading(heading)
        if normalized in _ASSET_REFERENCE_SECTION_HEADINGS:
            return AssetKind.REFERENCE.value
        if normalized in _ASSET_DELIVERY_SECTION_HEADINGS:
            return AssetKind.DELIVERY.value
        if _asset_structured_root_heading(heading):
            break
    return ""


def _structured_asset_usage_kind(value: str) -> str:
    normalized = _normalize_heading(value)
    if normalized in {"reference", "reference asset", "reference only", "read only reference"}:
        return AssetKind.REFERENCE.value
    if "do not copy" in normalized:
        return AssetKind.REFERENCE.value
    if normalized in {"delivery", "delivery asset", "production asset", "ship", "shippable asset"}:
        return AssetKind.DELIVERY.value
    if "delivery asset" in normalized or "production asset" in normalized:
        return AssetKind.DELIVERY.value
    return ""


def _structured_asset_resolved_kind(*kinds: str) -> str:
    explicit_kinds = [kind for kind in kinds if kind in {AssetKind.REFERENCE.value, AssetKind.DELIVERY.value}]
    if not explicit_kinds:
        return AssetKind.AMBIGUOUS.value
    first_kind = explicit_kinds[0]
    if any(kind != first_kind for kind in explicit_kinds[1:]):
        return AssetKind.AMBIGUOUS.value
    return first_kind


def _asset_heading_stack_has_structured_asset_root(heading_stack: list[tuple[int, str]]) -> bool:
    return any(_asset_structured_root_heading(heading) for _level, heading in heading_stack)


def asset_manifest_h1_has_structured_body(
    lines: list[str],
    start_index: int,
    *,
    ignore_fenced_blocks: bool = False,
) -> bool:
    in_fenced_block = False
    for line in lines[start_index:]:
        if ignore_fenced_blocks and _FENCED_BLOCK_RE.match(line):
            in_fenced_block = not in_fenced_block
            continue
        if ignore_fenced_blocks and in_fenced_block:
            continue
        if not line.strip():
            continue
        markdown_heading = _HEADING_RE.match(line)
        if markdown_heading:
            if len(markdown_heading.group(1)) == 1:
                return False
            return _asset_manifest_body_heading(markdown_heading.group(2))
        text_heading = _TEXT_HEADING_RE.match(line)
        if text_heading:
            return _asset_manifest_body_heading(text_heading.group(1))
        bullet = _BULLET_RE.match(line)
        if bullet:
            field = _structured_asset_field(bullet.group(1))
            return bool(field and _asset_manifest_body_field_label(field[0]))
    return False


def _asset_manifest_body_heading(heading: str) -> bool:
    normalized = _normalize_heading(heading)
    return normalized in _ASSET_MANIFEST_BODY_HEADINGS or bool(_ASSET_MANIFEST_BODY_HEADING_HINT_RE.search(normalized))


def _asset_manifest_body_field_label(label: str) -> bool:
    return (
        label in _ASSET_PATH_FIELD_LABEL_KINDS
        or label in _ASSET_USAGE_FIELD_LABELS
        or label in _ASSET_TARGET_FIELD_LABELS
        or label in _ASSET_PROVENANCE_FIELD_LABELS
        or label in _ASSET_MANIFEST_METADATA_FIELD_LABELS
    )


def _asset_structured_root_heading(heading: str) -> bool:
    return _normalize_heading(heading) in _ASSET_STRUCTURED_ROOT_HEADINGS


def _undeclared_asset_path_should_warn(path_text: str, context: str, project_config: dict | None) -> bool:
    return _asset_path_in_task_asset_dir(path_text, project_config) or bool(_ASSET_UNDECLARED_HINT_RE.search(context))


def _asset_update_heading_stack(
    heading_stack: list[tuple[int, str]],
    *,
    level: int,
    heading: str,
) -> list[tuple[int, str]]:
    return [item for item in heading_stack if item[0] < level] + [(level, heading)]


def _asset_text_heading_level(heading_stack: list[tuple[int, str]]) -> int:
    if heading_stack and _asset_subsection_heading(heading_stack[-1][1]):
        return heading_stack[-1][0]
    for level, heading in reversed(heading_stack):
        if _asset_root_section_heading(heading):
            return level + 1
    return 2


def _asset_root_section_heading(heading: str) -> bool:
    return _normalize_heading(heading) in _ASSET_ROOT_SECTION_HEADINGS


def _asset_subsection_heading(heading: str) -> bool:
    return _normalize_heading(heading) in _ASSET_SUBSECTION_HEADINGS


def _asset_context_headings(heading_stack: list[tuple[int, str]]) -> list[str]:
    if not heading_stack:
        return []
    return [heading for level, heading in heading_stack if level > 1]


def _asset_reference_context(heading_stack: list[tuple[int, str]], lines: list[str], line_index: int) -> str:
    local_lines = _asset_reference_context_lines(lines, line_index)
    return "\n".join([*_asset_context_headings(heading_stack), *local_lines])


def _asset_reference_context_lines(lines: list[str], line_index: int) -> list[str]:
    line = lines[line_index]
    bullet_match = _BULLET_RE.match(line)
    if not bullet_match:
        return [line]

    base_indent = _line_indent(line)
    context_lines = [line]
    next_index = line_index + 1
    while next_index < len(lines):
        candidate = lines[next_index]
        if _HEADING_RE.match(candidate):
            break
        if candidate.strip() and _BULLET_RE.match(candidate) and _line_indent(candidate) <= base_indent:
            break
        context_lines.append(candidate)
        next_index += 1
    return context_lines


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _asset_path_candidates(line: str) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for pattern in (_MARKDOWN_LINK_RE, _CODE_SPAN_RE, _PLAIN_ASSET_PATH_RE):
        matches.extend((match.start(1), match.end(1), match.group(1)) for match in pattern.finditer(line))
    matches.sort(key=lambda item: item[0])

    candidates: list[str] = []
    previous_asset_start: int | None = None
    previous_asset_end: int | None = None
    for start_index, end_index, value in matches:
        if _asset_candidate_starts_with_metadata_label(value):
            continue
        if _asset_candidate_is_target_path(line, start_index):
            continue
        if _asset_candidate_is_provenance_detail_path(line, start_index):
            continue
        if _asset_candidate_is_destination_path(
            line,
            start_index,
            previous_asset_start=previous_asset_start,
            previous_asset_end=previous_asset_end,
        ):
            continue
        candidates.append(value)
        previous_asset_start = start_index
        previous_asset_end = end_index
    return candidates


def _asset_candidate_starts_with_metadata_label(value: str) -> bool:
    return bool(re.match(r"\s*(?:source/license|license|licence|provenance)\s*:", value, re.IGNORECASE))


def _asset_candidate_is_target_path(line: str, start_index: int) -> bool:
    prefix = line[:start_index].lower().rstrip("`'\" ")
    return bool(re.search(r"\b(?:target|destination)(?:\s+path)?\s*:\s*$|\bcopy\s+to\s*:\s*$", prefix))


def _asset_candidate_is_provenance_detail_path(line: str, start_index: int) -> bool:
    prefix = line[:start_index].lower()
    return bool(re.search(r"\b(?:source/license|license|licence|provenance)\s*:\s*", prefix))


def _asset_candidate_is_destination_path(
    line: str,
    start_index: int,
    *,
    previous_asset_start: int | None,
    previous_asset_end: int | None,
) -> bool:
    if previous_asset_start is None or previous_asset_end is None:
        return False
    transfer_prefix = line[:previous_asset_start].casefold()
    if not re.search(r"\b(copy|move|place|install|save|write|add|include|use)\b", transfer_prefix):
        return False
    between_paths = line[previous_asset_end:start_index].casefold().strip(" `\"'")
    return _asset_destination_separator_between_paths(between_paths)


def _asset_destination_separator_between_paths(text: str) -> bool:
    words = re.findall(r"[a-z][a-z-]*", text.casefold())
    if not words or words[0] not in {"to", "into", "under", "at", "as", "for"}:
        return False
    if any(
        word
        in {
            "reference",
            "compare",
            "compared",
            "according",
            "based",
            "basis",
            "from",
            "with",
            "using",
            "via",
            "against",
        }
        for word in words[1:]
    ):
        return False
    return len(words) <= 5


def _asset_reference_identity_key(reference: dict[str, Any], fallback_path: str) -> str:
    project_path = str(reference.get("project_path") or "").strip()
    if project_path:
        return project_path
    normalized_fallback = Path(fallback_path).as_posix().strip()
    return normalized_fallback or str(fallback_path).strip()


def _merge_asset_reference(existing: dict[str, Any], update: dict[str, Any]) -> None:
    if _asset_reference_kind_rank(update.get("kind")) > _asset_reference_kind_rank(existing.get("kind")):
        existing["kind"] = update["kind"]
    update_path = str(update.get("path") or "").strip()
    if update_path and update_path != str(existing.get("path") or "").strip():
        existing.setdefault("_raw_paths", [])
        if update_path not in existing["_raw_paths"]:
            existing["_raw_paths"].append(update_path)
    for raw_path in update.get("_raw_paths") or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        existing.setdefault("_raw_paths", [])
        if raw_path not in existing["_raw_paths"]:
            existing["_raw_paths"].append(raw_path)
    for key in (
        "target_specified",
        "requested_target",
        "provenance_specified",
        "source_license",
        "mime_type",
        "sha256",
        "size_bytes",
        "git_status",
    ):
        if update.get(key) and not existing.get(key):
            existing[key] = update[key]


def _asset_reference_kind_rank(kind: Any) -> int:
    return {"delivery": 3, "reference": 2, "ambiguous": 1}.get(str(kind or ""), 0)


def _normalize_asset_path_candidate(raw_path: str) -> str | None:
    candidate = unquote(raw_path.strip().strip("\"'<>"))
    candidate = candidate.rstrip(".,;:")
    if not candidate or candidate.startswith("#"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() not in {"file"}:
        return None
    if parsed.scheme.lower() == "file":
        candidate = parsed.path
    if not _asset_candidate_has_supported_extension(candidate):
        return None
    if not _asset_candidate_has_directory(candidate):
        return None
    return candidate


def _asset_candidate_has_supported_extension(path: str) -> bool:
    return Path(path).suffix.lower() in _ASSET_EXTENSIONS


def _asset_candidate_has_directory(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_absolute() or "/" in path or "\\" in path


def _asset_path_in_task_asset_dir(path_text: str, project_config: dict | None) -> bool:
    normalized_path = Path(path_text).as_posix().lstrip("./")
    configured_dirs = [".sikula/task-assets"]
    tasks = project_config.get("tasks") if isinstance(project_config, dict) else None
    task_asset_dir = tasks.get("task_asset_dir") if isinstance(tasks, dict) else None
    if isinstance(task_asset_dir, str) and task_asset_dir.strip():
        configured_dirs.append(task_asset_dir.strip())
    for configured_dir in configured_dirs:
        normalized_dir = Path(configured_dir).as_posix().strip("/").lstrip("./")
        if normalized_dir and (normalized_path == normalized_dir or normalized_path.startswith(normalized_dir + "/")):
            return True
    return False


def _asset_project_root(source_path: Path | str | None, project_config: dict | None) -> Path:
    if project_config:
        project = project_config.get("project") if isinstance(project_config.get("project"), dict) else {}
        root = project.get("root_path") if isinstance(project, dict) else None
        if isinstance(root, str) and root.strip():
            return Path(root).resolve()
    if source_path is not None:
        try:
            path = Path(str(source_path)).resolve()
            for parent in path.parents:
                if parent.name == "tasks" and parent.parent.name == ".sikula":
                    return parent.parent.parent.resolve()
                if parent.name == "contracts" and parent.parent.name == ".sikula":
                    return parent.parent.parent.resolve()
                if parent.name == ".sikula":
                    return parent.parent.resolve()
            if path.exists():
                return path.parent.resolve()
        except OSError:
            pass
    return Path.cwd().resolve()


def _asset_reference_metadata(
    path_text: str,
    *,
    project_root: Path,
    line_number: int,
    raw_path: str | None = None,
    kind: str = AssetKind.AMBIGUOUS.value,
    target_specified: bool = False,
    requested_target: str | None = None,
    provenance_specified: bool = False,
    source_license: str | None = None,
) -> dict[str, Any] | None:
    requested_path = Path(path_text)
    candidate_path = requested_path if requested_path.is_absolute() else project_root / requested_path
    resolved_path = candidate_path.resolve(strict=False)
    try:
        project_path = resolved_path.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        project_path = ""
    raw_paths = [raw_path] if raw_path and raw_path != path_text else []
    requested_target = requested_target or ""
    source_license = source_license or ""

    if not project_path:
        return AssetReference(
            path=path_text,
            line=line_number,
            kind=kind,
            status=AssetStatus.OUTSIDE_PROJECT.value,
            raw_paths=raw_paths,
            target_specified=target_specified,
            requested_target=requested_target,
            provenance_specified=provenance_specified,
            source_license=source_license or "",
        ).to_dict(include_internal=True)

    if not resolved_path.exists():
        return AssetReference(
            path=path_text,
            line=line_number,
            kind=kind,
            status=AssetStatus.MISSING.value,
            project_path=project_path,
            raw_paths=raw_paths,
            target_specified=target_specified,
            requested_target=requested_target,
            provenance_specified=provenance_specified,
            source_license=source_license or "",
        ).to_dict(include_internal=True)
    if not resolved_path.is_file():
        return AssetReference(
            path=path_text,
            line=line_number,
            kind=kind,
            status=AssetStatus.NOT_FILE.value,
            project_path=project_path,
            raw_paths=raw_paths,
            target_specified=target_specified,
            requested_target=requested_target,
            provenance_specified=provenance_specified,
            source_license=source_license or "",
        ).to_dict(include_internal=True)

    mime_type, _encoding = mimetypes.guess_type(resolved_path.name)
    return AssetReference(
        path=path_text,
        line=line_number,
        kind=kind,
        status=AssetStatus.AVAILABLE.value,
        project_path=project_path,
        raw_paths=raw_paths,
        target_specified=target_specified,
        requested_target=requested_target,
        provenance_specified=provenance_specified,
        source_license=source_license or "",
        sha256=_file_sha256(resolved_path),
        size_bytes=resolved_path.stat().st_size,
        mime_type=mime_type or "",
        git_status=_asset_git_status(project_root, project_path),
    ).to_dict(include_internal=True)


def _asset_path_metadata_value(value: str) -> str | None:
    cleaned = _single_line(value)
    markdown_link = _MARKDOWN_LINK_RE.search(cleaned)
    if markdown_link:
        return _clean_asset_path_metadata_token(markdown_link.group(1))
    code_span = _CODE_SPAN_RE.search(cleaned)
    if code_span:
        return _clean_asset_path_metadata_token(code_span.group(1))
    return _clean_asset_path_metadata_token(cleaned)


def _clean_asset_path_metadata_token(value: str) -> str | None:
    cleaned = _single_line(value).strip()
    cleaned = cleaned.strip("<>")
    cleaned = cleaned.strip("`\"'")
    cleaned = cleaned.rstrip(".,;:")
    cleaned = cleaned.rstrip("`\"'")
    cleaned = cleaned.strip()
    return cleaned or None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _asset_git_status(project_root: Path, project_path: str) -> str:
    if _git_command(project_root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return "unknown"
    status = _git_command_stdout(project_root, "status", "--porcelain", "--ignored", "--", project_path)
    status_line = status.stdout.strip().splitlines()[0] if status.returncode == 0 and status.stdout.strip() else ""
    if status_line.startswith("!!"):
        return "ignored"
    if status_line.startswith("??"):
        return "untracked"
    if status_line:
        return "dirty"
    if _git_command(project_root, "ls-files", "--error-unmatch", "--", project_path).returncode == 0:
        return "tracked"
    if _git_command(project_root, "check-ignore", "-q", "--", project_path).returncode == 0:
        return "ignored"
    return "untracked"


def _git_command_stdout(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="")


def _git_command(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=["git", *args], returncode=1)


def asset_project_root(source_path: Path | str | None, project_config: dict | None) -> Path:
    return _asset_project_root(source_path, project_config)


def replace_unresolved_asset_reference_paths(
    task_text: str,
    asset_references: list[dict[str, Any]],
    answer_text: str,
    *,
    project_root: Path,
) -> str:
    return _replace_unresolved_asset_reference_paths(
        task_text,
        asset_references,
        answer_text,
        project_root=project_root,
    )
