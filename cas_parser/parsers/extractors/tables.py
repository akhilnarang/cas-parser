"""Helpers for working with pdfplumber-extracted tables."""

from __future__ import annotations


def clean_cell(cell: str | None) -> str:
    """Normalize a raw table cell: None -> "", collapse newlines, trim."""
    if cell is None:
        return ""
    return " ".join(cell.split())


def normalize_table(rows: list[list[str | None]]) -> list[list[str]]:
    """Clean every cell in a pdfplumber table.

    Args:
        rows: Raw table rows (cells may be None or contain embedded newlines).

    Returns:
        Rows with each cell cleaned via `clean_cell`.
    """
    return [[clean_cell(cell) for cell in row] for row in rows]


__all__ = ["clean_cell", "normalize_table"]
