"""Shared reader for the demat-holdings section of a CAS.

Parses depository account blocks (DP id / client id) and their security
holdings (ISIN, name, quantity, price, value). Depository is inferred from the
DP-ID prefix so an NSDL consolidated CAS attributes embedded CDSL accounts
correctly.

CDSL layout
-----------
The CDSL CAS prints an "Account Details" block listing every demat account
(``DP Name : ... DP ID : ... CLIENT ID : ...``), then a per-account
transaction + holding section. Each holding section is a clean nine-column
table::

    ISIN | Security | Current | Frozen | Pledge | Pledge Setup | Free Bal |
    Market Price / Face Value | Value

and ends with a ``Portfolio Value ` <total> as on <date>`` row that prints the
reported per-account total. Holding blocks appear in the same order as the
account-details headers, so we attribute each block to the next account in
sequence and close it at the ``Portfolio Value`` marker.

The holding quantity is the **Current Balance** — the total holding (free +
frozen + pledged) — not the "Free Bal" sub-balance, so ``quantity * price``
equals the printed market ``value`` even when units are pledged or frozen.
Columns are located by their English header tokens ("CURRENT", "MARKET PRICE",
"VALUE") rather than fixed indices; the Hindi glyphs are interleaved but each
cell still begins with its English label.

Footnote markers: the CAS appends terse markers to the ISIN cell (e.g.
``INE0...!!``) defined in the document's "Note:" legend. Those markers are
detected **against the parsed legend** (``parse_legend``) — only characters the
legend actually defines are stripped and recorded on ``DematHolding.flags`` /
``notes``, so the ``#`` AMC/scheme separator inside a name is never mistaken for
a footnote.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import AssetClass, DematAccount, DematHolding, Depository
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.sections.legend import detect_markers, resolve_notes
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.isin import find_isin, infer_asset_class

# "DP Name : <name> DP ID : <id> CLIENT ID : <id>" — the account-details header.
_ACCOUNT_HEADER_RE = re.compile(
    r"DP Name\s*:\s*(?P<dp_name>.+?)\s+DP ID\s*:\s*(?P<dp_id>\w+)\s+"
    r"CLIENT ID\s*:\s*(?P<client_id>\w+)",
    re.IGNORECASE,
)
# "Portfolio Value ` <amount> as on <date>" — the per-account total marker.
_PORTFOLIO_VALUE_RE = re.compile(
    r"Portfolio Value\s*`?\s*(?P<total>[\d,]+(?:\.\d+)?)", re.IGNORECASE
)
# Fallback column indices for the nine-column holding table; column resolution
# normally locates these by their English header tokens (see _resolve_columns).
_COL_ISIN = 0
_COL_NAME = 1
_COL_CURRENT = 2
_COL_PRICE = 7
_COL_VALUE = 8
# Tokens that mark a cell as empty / nil in the CDSL tables.
_NIL_TOKENS = {"", "--", "-"}

# Name fragments that refine an ISIN-derived asset class. ETFs and SGBs share
# ISIN prefixes with plain equities / G-Secs, so we refine from the name.
_ETF_HINTS = ("ETF", "BEES", "EXCHANGE TRADED FUND")
_SGB_HINTS = ("SOVEREIGN GOLD BOND", "SGB")


def depository_from_dp_id(dp_id: str) -> Depository:
    """Infer the depository from a DP-ID.

    NSDL DP-IDs are prefixed ``IN``; CDSL client/DP identifiers are numeric.
    """
    return "NSDL" if dp_id.strip().upper().startswith("IN") else "CDSL"


def _refine_asset_class(base: AssetClass, name: str) -> AssetClass:
    """Refine an ISIN-derived asset class using the security name.

    Args:
        base: Asset class inferred from the ISIN prefix.
        name: Security name as printed in the statement.

    Returns:
        A possibly more specific asset class (ETF / SGB), else ``base``.
    """
    upper = name.upper()
    if any(hint in upper for hint in _SGB_HINTS):
        return "sgb"
    if any(hint in upper for hint in _ETF_HINTS):
        return "etf"
    return base


def _resolve_columns(header: list[str]) -> dict[str, int]:
    """Map logical holding-column names to their indices from the header row.

    The English label begins each (Hindi-interleaved) header cell, so a simple
    prefix/contains match locates each column robustly even if the column order
    shifts. Falls back to the documented fixed indices for any column not found.

    Args:
        header: Cleaned, upper-cased header cells.

    Returns:
        A mapping with keys ``isin``, ``name``, ``current``, ``price`` and
        ``value``.
    """
    columns = {
        "isin": _COL_ISIN,
        "name": _COL_NAME,
        "current": _COL_CURRENT,
        "price": _COL_PRICE,
        "value": _COL_VALUE,
    }
    for index, cell in enumerate(header):
        despaced = cell.replace(" ", "")
        if cell.startswith("ISIN"):
            columns["isin"] = index
        elif cell.startswith("SECURITY"):
            columns["name"] = index
        elif cell.startswith("CURRENT"):
            columns["current"] = index
        elif "MARKET" in cell:
            columns["price"] = index
        elif despaced.startswith("VALUE") or despaced.startswith("₹VALUE"):
            columns["value"] = index
    return columns


def _is_holding_table(rows: list[list[str | None]]) -> bool:
    """Return True when a table is a nine-column ISIN/Value holding table.

    The holding table and the per-account transaction table are both nine
    columns and both start with "ISIN"; they differ by the price column
    ("MARKET" price for holdings) versus the transaction table's "STAMP" duty
    column, which we use to disambiguate.
    """
    if not rows:
        return False
    header = [clean_cell(cell).upper() for cell in rows[0]]
    if len(header) < 9:
        return False
    joined = " ".join(header)
    return (
        header[_COL_ISIN].startswith("ISIN")
        and "MARKET" in header[_COL_PRICE]
        and "STAMP" not in joined
    )


def _cell_decimal(cells: list[str], index: int) -> Decimal | None:
    """Parse the decimal at ``index``, treating nil tokens as None."""
    if index >= len(cells):
        return None
    raw = cells[index].strip()
    if raw in _NIL_TOKENS:
        return None
    return parse_decimal(raw)


def _parse_holding(
    cells: list[str], columns: dict[str, int], legend: dict[str, str]
) -> DematHolding | None:
    """Build a `DematHolding` from a cleaned holding-table row, or None.

    The quantity is the Current Balance (the total holding), so ``quantity *
    price`` reproduces the printed market ``value`` regardless of any
    pledged/frozen split. A zero-balance line (Current Balance ``--``) yields a
    zero quantity and a zero value; a *missing* value on a non-zero holding is
    left as ``None`` (an extraction failure to be surfaced by reconciliation),
    never silently coerced to zero.

    Footnote markers attached to the ISIN/name cells are detected against the
    parsed ``legend`` (so only defined markers are stripped — the ``#``
    AMC/scheme separator is never touched unless the legend defines ``#``),
    recorded on ``flags`` and resolved to ``notes``.

    Args:
        cells: Cleaned cell strings for a single table row.
        columns: Logical-name -> index map from ``_resolve_columns``.
        legend: Parsed ``{marker: definition}`` map driving marker detection.

    Returns:
        A parsed holding, or None when the row is not a security line.
    """
    raw_isin_cell = cells[columns["isin"]]
    isin_flags, isin_clean = detect_markers(raw_isin_cell, legend)
    isin = find_isin(isin_clean.strip())
    if isin is None:
        return None
    name_index = columns["name"]
    raw_name = cells[name_index] if len(cells) > name_index else ""
    # Markers occasionally ride on the name cell too; strip only legend-defined
    # ones so the ``#`` AMC/scheme separator and the rest of the name survive.
    name_flags, name = detect_markers(raw_name, legend)
    name = name.strip()
    quantity = _cell_decimal(cells, columns["current"]) or Decimal("0")
    price = _cell_decimal(cells, columns["price"])
    value = _cell_decimal(cells, columns["value"])
    if value is None and quantity == 0:
        # A genuine zero-balance line (Current Balance ``--``): the value is
        # truly zero, so coerce it. A missing value on a non-zero holding,
        # however, is an extraction failure — leave it None so reconciliation
        # marks the scope incomplete rather than fabricating a passing total.
        value = Decimal("0")
    asset_class = _refine_asset_class(infer_asset_class(isin), name)
    # Merge flags from both cells, preserving legend order without duplicates.
    found = set(isin_flags) | set(name_flags)
    flags = [marker for marker in legend if marker in found]
    return DematHolding(
        name=name,
        isin=isin,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        value=value,
        flags=flags,
        notes=resolve_notes(flags, legend),
    )


def _collect_account_headers(pages: list[dict[str, Any]]) -> list[re.Match[str]]:
    """Return the ordered ``DP Name ... DP ID ... CLIENT ID`` header matches."""
    headers: list[re.Match[str]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for match in _ACCOUNT_HEADER_RE.finditer(page.get("text", "")):
            key = (match.group("dp_id"), match.group("client_id"))
            if key in seen:
                continue
            seen.add(key)
            headers.append(match)
    return headers


def parse_demat_accounts(
    pages: list[dict[str, Any]],
    legend: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> list[DematAccount]:
    """Parse demat accounts and holdings from the demat section pages.

    The section pages must include both the "Account Details" block (for the
    DP/client identities) and the per-account holding tables. Holdings are
    attributed to accounts by order: each holding block runs up to its
    ``Portfolio Value`` marker and maps to the next account header.

    Args:
        pages: Page payloads covering the demat section.
        legend: Parsed footnote legend (``{marker: definition}``); drives which
            ISIN/name markers are recorded on holdings. Defaults to empty.
        warnings: Optional list to which a warning is appended when the number of
            ``Portfolio Value`` markers parsed does not equal the number of
            account headers. The attribution is order-based on those markers, so
            a count mismatch means holdings may be mis-attributed — surfacing it
            makes the otherwise-silent mismatch visible.

    Returns:
        Parsed demat accounts with their holdings.
    """
    legend = legend or {}
    headers = _collect_account_headers(pages)
    accounts: list[DematAccount] = []
    for match in headers:
        dp_id = match.group("dp_id").strip()
        accounts.append(
            DematAccount(
                depository=depository_from_dp_id(dp_id),
                dp_id=dp_id,
                client_id=match.group("client_id").strip(),
                dp_name=match.group("dp_name").strip() or None,
            )
        )

    if not accounts:
        return accounts

    index = 0
    marker_count = 0
    for page in pages:
        for table in page.get("tables", []):
            if not _is_holding_table(table):
                continue
            columns = _resolve_columns([clean_cell(cell).upper() for cell in table[0]])
            for row in table[1:]:
                cells = [clean_cell(cell) for cell in row]
                first = cells[columns["isin"]] if cells else ""
                total_match = _PORTFOLIO_VALUE_RE.search(first)
                if total_match:
                    marker_count += 1
                    if index < len(accounts):
                        accounts[index].total_value = parse_decimal(
                            total_match.group("total")
                        )
                        index += 1
                    continue
                holding = _parse_holding(cells, columns, legend)
                if holding is not None and index < len(accounts):
                    accounts[index].holdings.append(holding)

    if warnings is not None and marker_count != len(accounts):
        warnings.append(
            f"CDSL holding attribution: parsed {marker_count} 'Portfolio Value' "
            f"marker(s) but {len(accounts)} account header(s); order-based "
            "attribution may be misaligned"
        )

    return accounts


__all__ = ["depository_from_dp_id", "parse_demat_accounts"]
