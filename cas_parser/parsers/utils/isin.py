"""ISIN validation and asset-class inference.

An ISIN is 12 characters: a 2-letter country code, a 9-character alphanumeric
NSIN, and a single check digit (Luhn over the base-36 expansion).

For Indian securities the NSIN's first character encodes the issuer/instrument
family, which lets us coarsely infer an asset class:

- ``INF...`` -> mutual fund units
- ``INE...`` -> equity / corporate securities
- ``IN[0-8]...`` -> government securities (G-Secs, T-bills, SGBs)

This is a coarse prefix heuristic — Sovereign Gold Bonds share the government
prefix with plain G-Secs, so callers should refine SGB/ETF/bond classification
from the security *name* where it matters.
"""

from __future__ import annotations

import re

from cas_parser.models import AssetClass

ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def find_isin(text: str) -> str | None:
    """Return the first ISIN-shaped token in `text`, validated, or None."""
    for match in ISIN_RE.finditer(text.upper()):
        candidate = match.group(0)
        if is_valid_isin(candidate):
            return candidate
    return None


def is_valid_isin(isin: str) -> bool:
    """Validate an ISIN's format and Luhn check digit."""
    value = isin.strip().upper()
    if not ISIN_RE.fullmatch(value):
        return False

    digits = ""
    for char in value:
        digits += char if char.isdigit() else str(ord(char) - 55)

    total = 0
    for index, digit in enumerate(reversed(digits)):
        number = int(digit)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def infer_asset_class(isin: str | None) -> AssetClass:
    """Coarsely infer an asset class from an ISIN prefix.

    Returns ``"other"`` for unknown or non-Indian ISINs; callers should refine
    using the security name where the prefix is ambiguous.
    """
    if not isin:
        return "other"
    value = isin.strip().upper()
    if not value.startswith("IN") or len(value) < 3:
        return "other"

    third = value[2]
    if third == "F":
        return "mutual_fund"
    if third == "E":
        return "equity"
    if third.isdigit() and third != "9":
        return "government_security"
    return "other"


__all__ = [
    "ISIN_RE",
    "find_isin",
    "infer_asset_class",
    "is_valid_isin",
]
