"""Helpers for source-like audit scans."""

from __future__ import annotations


def mask_source_literals(text: str | None) -> str:
    """Mask string/comment contents while preserving line structure and syntax shape."""

    if not text:
        return ""

    result: list[str] = []
    i = 0
    quote: str | None = None
    triple_quote: str | None = None
    block_comment = False

    while i < len(text):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(text) else ""

        if block_comment:
            if char == "\n":
                result.append("\n")
                i += 1
                continue
            if char == "*" and next_char == "/":
                result.extend("  ")
                block_comment = False
                i += 2
                continue
            result.append(" ")
            i += 1
            continue

        if triple_quote:
            if text.startswith(triple_quote, i):
                result.extend(triple_quote)
                i += len(triple_quote)
                triple_quote = None
                continue
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

        if quote:
            if char == "\n" and quote != "`":
                result.append("\n")
                quote = None
                i += 1
                continue
            if char == "\\" and i + 1 < len(text):
                result.append(" ")
                result.append("\n" if next_char == "\n" else " ")
                i += 2
                continue
            if char == quote:
                result.append(char)
                quote = None
                i += 1
                continue
            result.append("\n" if char == "\n" else " ")
            i += 1
            continue

        if char == "/" and next_char == "/":
            result.extend("  ")
            i += 2
            while i < len(text) and text[i] != "\n":
                result.append(" ")
                i += 1
            continue

        if char == "/" and next_char == "*":
            result.extend("  ")
            block_comment = True
            i += 2
            continue

        if _starts_hash_comment(text, i):
            while i < len(text) and text[i] != "\n":
                result.append(" ")
                i += 1
            continue

        if text.startswith('"""', i) or text.startswith("'''", i):
            triple_quote = text[i : i + 3]
            result.extend(triple_quote)
            i += 3
            continue

        if char in {"'", '"', "`"}:
            quote = char
            result.append(char)
            i += 1
            continue

        result.append(char)
        i += 1

    return "".join(result)


def masked_source_lines(text: str | None) -> list[str]:
    return mask_source_literals(text).splitlines()


def _starts_hash_comment(text: str, index: int) -> bool:
    if text[index] != "#":
        return False
    return index + 1 >= len(text) or text[index + 1] != "["
