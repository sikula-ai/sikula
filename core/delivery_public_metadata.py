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
_REDACTED_IDENTITY_TOKEN_RE = re.compile(r"^<redacted:[0-9a-f]{12}>$")


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


def _contains_control_characters(value: str) -> bool:
    return len(value.splitlines()) != 1 or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def _is_safe_delivery_public_identity(value: str) -> bool:
    return (
        len(value) <= MAX_DELIVERY_PUBLIC_METADATA_LENGTH
        and not _REDACTED_IDENTITY_TOKEN_RE.fullmatch(value)
        and not _contains_control_characters(value)
        and not _contains_absolute_path(value)
    )


def _contains_absolute_path(value: str) -> bool:
    return _ABSOLUTE_PATH_RE.search(value) is not None
