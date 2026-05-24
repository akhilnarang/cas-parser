"""Numeric parsing helpers for CAS values.

Money / units / NAV are `Decimal` in the schema. CAS PDFs use the Indian
grouping convention (lakhs/crores: "1,52,581.54") and units carry more decimal
places than currency ("123.456"), so these helpers strip grouping and parse to
`Decimal` without forcing a fixed scale.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# A signed decimal with optional grouping commas; matches both currency
# ("1,52,581.54") and unit ("123.456") shapes.
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _clean(value: str) -> str:
    cleaned = value.replace(",", "").replace("`", "").replace("₹", "").replace("Rs.", "")
    cleaned = re.sub(r"\s*(Cr|Dr|CR|DR)\.?\s*$", "", cleaned)
    return cleaned.strip()


def parse_decimal(value: str) -> Decimal | None:
    """Convert a numeric string to Decimal, or None when it is not numeric."""
    cleaned = _clean(value)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def extract_decimal(token: str) -> Decimal | None:
    """Extract the first decimal number found in a token, as Decimal."""
    match = NUMBER_RE.search(token.replace("`", "").replace("₹", ""))
    if not match:
        return None
    return parse_decimal(match.group(0))


def format_decimal(value: Decimal, places: int = 2) -> str:
    """Format a Decimal with comma grouping and a fixed number of places."""
    return f"{value:,.{places}f}"


__all__ = [
    "NUMBER_RE",
    "extract_decimal",
    "format_decimal",
    "parse_decimal",
]
