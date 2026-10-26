"""Lossless message splitting using Telegram's UTF-16 length units."""
from __future__ import annotations


def split_messages(text: str, limit: int = 4000) -> list[str]:
    """Prefer complete lines, keeping every character and line separator.

    Astral Unicode characters occupy two UTF-16 units but remain indivisible.
    A minimum limit of two permits every Unicode code point to fit.
    """
    if type(limit) is not int or limit < 2:
        raise ValueError('limit must be an integer of at least 2 UTF-16 units')
    parts: list[str] = []
    start = 0
    while start < len(text):
        end, units, newline_end = start, 0, start
        while end < len(text):
            codepoint = ord(text[end])
            next_end = end + 1
            # Python normally stores an astral character as one code point.
            # Keep an explicitly represented UTF-16 surrogate pair together too.
            if (0xD800 <= codepoint <= 0xDBFF and next_end < len(text)
                    and 0xDC00 <= ord(text[next_end]) <= 0xDFFF):
                width = 2
                next_end += 1
            else:
                width = 2 if codepoint > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end = next_end
            if text[end - 1] == '\n':
                newline_end = end
        if end < len(text) and newline_end > start:
            end = newline_end
        parts.append(text[start:end])
        start = end
    return parts
