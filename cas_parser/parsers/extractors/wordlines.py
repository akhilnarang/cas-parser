"""Word-line based extraction helpers.

Depository CAS holdings and transactions are frequently rendered as
word-positioned text rather than as clean PDF tables. `group_words_into_lines`
reconstructs visual lines from pdfplumber word boxes so a parser can then map
tokens to columns by x-position.
"""

from __future__ import annotations

from typing import Any


def group_words_into_lines(
    words: list[dict[str, Any]],
    y_tolerance: float = 1.8,
) -> list[list[dict[str, Any]]]:
    """Group extracted PDF words into visual lines by y-position.

    Args:
        words: pdfplumber word dicts (need `doctop` and `x0`).
        y_tolerance: Max vertical drift, in points, to treat words as one line.

    Returns:
        Lines, each a list of word dicts sorted left-to-right by `x0`.
    """
    sorted_words = sorted(
        words,
        key=lambda item: (float(item["doctop"]), float(item["x0"])),
    )
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = []
    current_y: float | None = None

    for word in sorted_words:
        y_value = float(word["doctop"])
        if current_y is None or abs(y_value - current_y) <= y_tolerance:
            current_line.append(word)
            current_y = y_value if current_y is None else (current_y + y_value) / 2
        else:
            lines.append(sorted(current_line, key=lambda item: float(item["x0"])))
            current_line = [word]
            current_y = y_value

    if current_line:
        lines.append(sorted(current_line, key=lambda item: float(item["x0"])))

    return lines


__all__ = ["group_words_into_lines"]
