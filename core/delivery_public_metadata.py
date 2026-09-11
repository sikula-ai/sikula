"""Validation and projection helpers for public delivery metadata."""

from __future__ import annotations

from hashlib import sha256
import re
import unicodedata

MAX_DELIVERY_PUBLIC_METADATA_LENGTH = 1000
REDACTED_DELIVERY_PUBLIC_METADATA = "<redacted>"

_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?i:file://)[^\s\"'`)\]}]+"
    r"|(?<!\w)[A-Za-z]:[\\/][^\s\"'`)\]}]+"
    r"|(?<![\w.\\-])\\\\[^\s\"'`)\]}]+"
    r"|(?<![\w.\\-])\\(?!\\)[^\s\"'`)\]}]+"
    r"|(?<![\w.:/-])//[^\s\"'`)\]}]+"
    r"|(?<![\w./-])/(?!/)[^\s\"'`)\]}]+"
    r")"
)
_HTTP_ROUTE_PREFIX_RE = re.compile(
    r"(?i)(?:^|[\s(])(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT|endpoint|route)\s+$"
)
_HTTP_ROUTE_SUFFIX_RE = re.compile(r"(?i)^\s+(?:endpoint|route)\b")
_REDACTED_IDENTITY_TOKEN_RE = re.compile(r"^<redacted:[0-9a-f]{12}>$")
_DELIVERY_SOURCE_EXCERPT_MIN_LENGTH = 24
_MARKDOWN_LINE_PREFIX_RE = re.compile(r"^(?:>{1,3}\s*|#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|\[[ xX]\]\s+)")


def is_safe_delivery_public_metadata(value: str) -> bool:
    return (
        len(value) <= MAX_DELIVERY_PUBLIC_METADATA_LENGTH
        and not _contains_control_characters(value)
        and not _contains_absolute_path(value)
    )


def project_delivery_public_identity(value: str | None) -> str | None:
    if value is None or _is_safe_delivery_public_identity(value):
        return value
    fingerprint = sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"<redacted:{fingerprint}>"


def sanitize_delivery_public_metadata(value: str | None) -> str | None:
    if value is None or is_safe_delivery_public_metadata(value):
        return value
    return REDACTED_DELIVERY_PUBLIC_METADATA


def contains_delivery_source_excerpt(value: str, source_text: str) -> bool:
    """Return whether bounded public metadata copies substantive source text."""
    candidate = _normalize_delivery_source_excerpt(value)
    if len(candidate) < _DELIVERY_SOURCE_EXCERPT_MIN_LENGTH:
        return False
    for raw_line in source_text.splitlines():
        source_line = _normalize_delivery_source_excerpt(raw_line, strip_markdown_prefix=True)
        if len(source_line) < _DELIVERY_SOURCE_EXCERPT_MIN_LENGTH:
            continue
        if candidate in source_line or source_line in candidate:
            return True
    return False


def _contains_control_characters(value: str) -> bool:
    return len(value.splitlines()) != 1 or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def _normalize_delivery_source_excerpt(value: str, *, strip_markdown_prefix: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if strip_markdown_prefix:
        while True:
            without_prefix = _MARKDOWN_LINE_PREFIX_RE.sub("", normalized, count=1).strip()
            if without_prefix == normalized:
                break
            normalized = without_prefix
        normalized = normalized.strip("`*_~")
    return " ".join(normalized.split()).casefold()


def _is_safe_delivery_public_identity(value: str) -> bool:
    return (
        len(value) <= MAX_DELIVERY_PUBLIC_METADATA_LENGTH
        and not _REDACTED_IDENTITY_TOKEN_RE.fullmatch(value)
        and not _contains_control_characters(value)
        and not _contains_absolute_path(value)
    )


def _contains_absolute_path(value: str) -> bool:
    for match in _ABSOLUTE_PATH_RE.finditer(value):
        if _is_explicit_http_route(value, match):
            continue
        return True
    return False


def _is_explicit_http_route(value: str, match: re.Match[str]) -> bool:
    candidate = match.group("path")
    if not candidate.startswith("/") or candidate.startswith("//"):
        return False

    _, separator, route_tail = candidate.partition("?")
    if not separator:
        _, separator, route_tail = candidate.partition("#")
    if separator and ("/" in route_tail or "\\" in route_tail):
        return False

    return bool(
        _HTTP_ROUTE_PREFIX_RE.search(value[: match.start("path")])
        or _HTTP_ROUTE_SUFFIX_RE.match(value[match.end("path") :])
    )
