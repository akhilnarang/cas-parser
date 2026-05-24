"""Reader for the NSDL CAS demat-holdings section.

The NSDL consolidated CAS prints demat accounts from *both* depositories, each
in its own layout, then asset-class sub-sections within each account:

NSDL Demat Account (six-column equity table)
--------------------------------------------
::

    ISIN / Stock Symbol | Company Name | Face Value in ` | No. of Shares |
    Market Price in ` | Value in `

The ISIN cell's second line is the NSE/BSE stock symbol or the ``NOT LISTED``
marker; the company name wraps across several lines in its own cell.

CDSL Demat Account (seven-column balance table — embedded in the NSDL CAS)
-------------------------------------------------------------------------
::

    ISIN | SECURITY | Current/Free/Lent Bal. | Safekeep/LockedIn/PledgeSetup |
    Pledged/Earmarked/Pledgee | Market Price / Face Value in ` | Value in `

The balance columns pack three sub-balances per cell (one per line); the holding
quantity is the **Current Balance** — the first line of the third column — so
``quantity * price`` reproduces the printed value even when units are pledged or
earmarked. The SECURITY cell wraps and uses the ``#`` AMC/separator convention.

Both layouts attribute holdings to the account whose header cell most recently
appeared (``<NSDL|CDSL> Demat Account\\n<DP NAME>\\nDP ID: <id> Client ID:
<id>``), and each account closes at its ``Total`` row. Per-account reported
totals come from the front-matter account-summary table, keyed by
``<dp_id>/<client_id>``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import AssetClass, DematAccount, DematHolding
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.sections.demat import depository_from_dp_id
from cas_parser.parsers.sections.nsdl_legend import NOT_LISTED_MARKER
from cas_parser.parsers.sections.legend import resolve_notes
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.isin import find_isin, infer_asset_class

# "<NSDL|CDSL> Demat Account\n<DP NAME>\nDP ID: <id> Client ID: <id>" — the
# per-account header cell. The DP NAME (possibly multi-line) sits between the
# account-type line and the DP-ID line. Matched within a single table cell, so
# the capture is clean (unlike the visually-interleaved page text).
_ACCOUNT_HEADER_RE = re.compile(
    r"(?P<dep>NSDL|CDSL)\s+Demat Account\s*\n(?P<dp_name>.+?)\s*\n"
    r"DP ID\s*:\s*(?P<dp_id>\w+)\s+Client ID\s*:\s*(?P<client_id>\w+)",
    re.IGNORECASE | re.DOTALL,
)

# Name fragments that refine an ISIN-derived asset class (shared with the CDSL
# reader's intent): ETFs/SGBs share ISIN prefixes with equities / G-Secs.
_ETF_HINTS = ("ETF", "BEES", "EXCHANGE TRADED FUND")
_SGB_HINTS = ("SOVEREIGN GOLD BOND", "SGB")

# Row labels that close an asset-class sub-section or the whole account.
_SUBTOTAL_LABELS = {"SUB TOTAL", "TOTAL"}
_NIL_TOKENS = {"", "--", "-"}


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


def _resolve_value(value: Decimal | None, quantity: Decimal) -> Decimal | None:
    """Resolve a holding's market value, honouring missing extractions.

    A genuine zero-balance line (quantity zero) with no parsed value is truly
    worth zero, so it is coerced to ``Decimal("0")``. A missing value on a
    non-zero holding, however, is an extraction failure — it is left ``None`` so
    reconciliation marks the scope incomplete rather than fabricating a passing
    total by treating the unknown value as zero.
    """
    if value is None and quantity == 0:
        return Decimal("0")
    return value


def _header_row_index(rows: list[list[str | None]]) -> int | None:
    """Return the index of the ``ISIN ...`` column-header row, if present.

    The header is within the first three rows: a holding table may be preceded by
    an asset-class header cell (e.g. ``Mutual Funds (M)``) and an ``Equity
    Shares`` sub-title. Continuation tables that wrap onto a new page start
    directly with the column header.
    """
    for index, row in enumerate(rows[:3]):
        if row and clean_cell(row[0]).upper().startswith("ISIN"):
            return index
    return None


def _nsdl_header_index(rows: list[list[str | None]]) -> int | None:
    """Return the header-row index for a six-column NSDL equity table, else None."""
    index = _header_row_index(rows)
    if index is None:
        return None
    joined = " ".join(clean_cell(cell).upper() for cell in rows[index])
    if "COMPANY NAME" in joined and "NO. OF" in joined and "MARKET" in joined:
        return index
    return None


def _cdsl_header_index(rows: list[list[str | None]]) -> int | None:
    """Return the header-row index for a seven-column CDSL table, else None."""
    index = _header_row_index(rows)
    if index is None:
        return None
    header = rows[index]
    joined = " ".join(clean_cell(cell).upper() for cell in header)
    if (
        len(header) >= 7
        and "SECURITY" in joined
        and "CURRENT BAL" in joined.replace(".", "")
    ):
        return index
    return None


def _first_line(cell: str | None) -> str:
    """Return the first physical line of a raw (possibly multi-line) cell."""
    if not cell:
        return ""
    return cell.split("\n", 1)[0].strip()


def _join_lines(cell: str | None) -> str:
    """Collapse a multi-line cell into one space-joined string."""
    return clean_cell(cell)


def _parse_nsdl_holding(
    row: list[str | None], legend: dict[str, str]
) -> DematHolding | None:
    """Build a `DematHolding` from a six-column NSDL equity row, or None.

    The quantity is the printed ``No. of Shares`` and the value is ``Value in
    ```, so ``quantity * price`` reproduces the printed value. A ``NOT LISTED``
    second line in the ISIN cell is recorded as a legend flag. A missing value on
    a non-zero holding is kept ``None`` (see ``_resolve_value``), not coerced.

    Args:
        row: Raw table row (cells may be None / multi-line).
        legend: Parsed ``{marker: definition}`` map driving marker resolution.

    Returns:
        A parsed holding, or None when the row is not a security line.
    """
    isin_cell = row[0] or ""
    isin = find_isin(_first_line(isin_cell))
    if isin is None:
        return None
    name = _join_lines(row[1]) if len(row) > 1 else ""
    quantity = parse_decimal(clean_cell(row[3])) if len(row) > 3 else None
    price = parse_decimal(clean_cell(row[4])) if len(row) > 4 else None
    value = parse_decimal(clean_cell(row[5])) if len(row) > 5 else None

    quantity = quantity if quantity is not None else Decimal("0")
    value = _resolve_value(value, quantity)
    flags = _listing_flags(isin_cell, legend)
    asset_class = _refine_asset_class(infer_asset_class(isin), name)
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


def _parse_cdsl_holding(
    row: list[str | None],
    section_class: AssetClass | None,
    legend: dict[str, str],
) -> DematHolding | None:
    """Build a `DematHolding` from a seven-column CDSL balance row, or None.

    The quantity is the Current Balance (first line of the third cell) — the
    total holding — so ``quantity * price`` reproduces the printed value even
    when units are pledged/earmarked. The asset class is inferred from the ISIN
    and refined from the name; ``section_class`` (the asset-class sub-header) is
    used as a fallback when the ISIN prefix is ambiguous. A missing value on a
    non-zero holding is kept ``None`` (see ``_resolve_value``), not coerced.

    Args:
        row: Raw table row (cells may be None / multi-line).
        section_class: Asset class from the current sub-section header, if known.
        legend: Parsed ``{marker: definition}`` map.

    Returns:
        A parsed holding, or None when the row is not a security line.
    """
    isin = find_isin(_first_line(row[0]))
    if isin is None:
        return None
    name = _join_lines(row[1]) if len(row) > 1 else ""
    quantity = parse_decimal(_first_line(row[2])) if len(row) > 2 else None
    price = parse_decimal(clean_cell(row[5])) if len(row) > 5 else None
    value = parse_decimal(clean_cell(row[6])) if len(row) > 6 else None

    quantity = quantity if quantity is not None else Decimal("0")
    value = _resolve_value(value, quantity)
    base = infer_asset_class(isin)
    if base == "other" and section_class is not None:
        base = section_class
    asset_class = _refine_asset_class(base, name)
    return DematHolding(
        name=name,
        isin=isin,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        value=value,
        flags=[],
        notes=None,
    )


def _listing_flags(isin_cell: str, legend: dict[str, str]) -> list[str]:
    """Return the listing flags (``NOT LISTED``) present on an ISIN cell."""
    if NOT_LISTED_MARKER in isin_cell.upper() and NOT_LISTED_MARKER in legend:
        return [NOT_LISTED_MARKER]
    return []


# Asset-class section headers (e.g. "Government Securities (G)") that precede a
# CDSL balance table; used to disambiguate ISIN-ambiguous instruments.
_SECTION_CLASS_BY_CODE: dict[str, AssetClass] = {
    "E": "equity",
    "P": "preference_share",
    "M": "mutual_fund",
    "A": "aif",
    "C": "bond",
    "G": "government_security",
    "SGB": "sgb",
}
_SECTION_HEADER_RE = re.compile(r"^(?P<label>.+?)\s*\((?P<code>[A-Z]{1,3})\)\s*$")


def _section_class(cell: str) -> AssetClass | None:
    """Map an asset-class section header cell to an asset class, if recognised."""
    match = _SECTION_HEADER_RE.match(cell.strip())
    if not match:
        return None
    return _SECTION_CLASS_BY_CODE.get(match.group("code"))


def _last_section_class(text: str) -> AssetClass | None:
    """Return the last asset-class sub-header found in a page's text, if any."""
    found: AssetClass | None = None
    for raw_line in text.split("\n"):
        maybe = _section_class(raw_line)
        if maybe is not None:
            found = maybe
    return found


def _collect_account_headers(
    pages: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """Return ordered ``(dp_id, client_id, dp_name)`` account headers.

    Headers are read from the one-cell account-header tables (where the DP NAME
    extracts cleanly), in page order, and de-duplicated so an account appearing
    on multiple pages is registered once, in first-seen order. The page *text*
    interleaves the header with the account-holders column, so the cell form is
    the reliable source.
    """
    headers: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for table in page.get("tables", []):
            for row in table:
                for cell in row:
                    if not cell:
                        continue
                    match = _ACCOUNT_HEADER_RE.search(cell)
                    if not match:
                        continue
                    dp_id = match.group("dp_id").strip()
                    client_id = match.group("client_id").strip()
                    key = (dp_id, client_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    dp_name = " ".join(match.group("dp_name").split())
                    headers.append((dp_id, client_id, dp_name))
    return headers


def parse_demat_accounts(
    pages: list[dict[str, Any]],
    legend: dict[str, str] | None = None,
    account_totals: dict[str, Decimal] | None = None,
) -> list[DematAccount]:
    """Parse demat accounts and holdings from the NSDL holdings section.

    Both the NSDL six-column and CDSL seven-column holding tables are read.
    Holdings are attributed to the account whose header cell most recently
    appeared in page order (tracked across pages, so a table continuing onto the
    next page stays with its account). Per-account reported totals are taken from
    the front-matter account-summary table.

    Args:
        pages: Page payloads covering the demat section.
        legend: Parsed ``{marker: definition}`` map; drives holding markers.
        account_totals: ``<dp_id>/<client_id>`` -> reported value from the
            account-summary table; populates each account's ``total_value``.

    Returns:
        Parsed demat accounts with holdings and reported totals.
    """
    legend = legend or {}
    account_totals = account_totals or {}

    headers = _collect_account_headers(pages)
    accounts: list[DematAccount] = []
    by_ref: dict[str, DematAccount] = {}
    for dp_id, client_id, dp_name in headers:
        account = DematAccount(
            depository=depository_from_dp_id(dp_id),
            dp_id=dp_id,
            client_id=client_id,
            dp_name=dp_name or None,
            total_value=account_totals.get(f"{dp_id}/{client_id}"),
        )
        accounts.append(account)
        by_ref[account.source_ref] = account

    if not accounts:
        return accounts

    current: DematAccount | None = None
    section_class: AssetClass | None = None
    for page in pages:
        # An asset-class sub-header ("Mutual Funds (M)", "Government Securities
        # (G)") appears in the page text. Track the last one seen so a
        # continuation table that wraps onto a new page (and starts directly with
        # the ISIN column header) still inherits its asset class.
        page_section = _last_section_class(page.get("text", ""))
        if page_section is not None:
            section_class = page_section

        for table in page.get("tables", []):
            if not table:
                continue
            opened = _account_in_table(table)
            if opened is not None and opened in by_ref:
                current = by_ref[opened]
            # An asset-class header may also sit as the first cell of the table.
            maybe_class = _section_class(clean_cell(table[0][0]))
            if maybe_class is not None:
                section_class = maybe_class

            nsdl_index = _nsdl_header_index(table)
            cdsl_index = _cdsl_header_index(table)
            if nsdl_index is not None and current is not None:
                _append_nsdl_rows(current, table[nsdl_index + 1 :], legend)
            elif cdsl_index is not None and current is not None:
                _append_cdsl_rows(
                    current, table[cdsl_index + 1 :], section_class, legend
                )

    return accounts


def _account_in_table(rows: list[list[str | None]]) -> str | None:
    """Return the ``<dp_id>/<client_id>`` ref if a row opens an account block."""
    for row in rows:
        for cell in row:
            if not cell:
                continue
            match = _ACCOUNT_HEADER_RE.search(cell)
            if match:
                return f"{match.group('dp_id').strip()}/{match.group('client_id').strip()}"
    return None


def _append_nsdl_rows(
    account: DematAccount,
    rows: list[list[str | None]],
    legend: dict[str, str],
) -> None:
    """Append parsed NSDL six-column holding rows (post-header) to an account."""
    for row in rows:
        label = clean_cell(row[0]).upper() if row else ""
        if label in _SUBTOTAL_LABELS or not label:
            continue
        holding = _parse_nsdl_holding(row, legend)
        if holding is not None:
            account.holdings.append(holding)


def _append_cdsl_rows(
    account: DematAccount,
    rows: list[list[str | None]],
    section_class: AssetClass | None,
    legend: dict[str, str],
) -> None:
    """Append parsed CDSL seven-column holding rows (post-header) to an account."""
    for row in rows:
        label = clean_cell(row[0]).upper() if row else ""
        if label in _SUBTOTAL_LABELS or not label:
            continue
        holding = _parse_cdsl_holding(row, section_class, legend)
        if holding is not None:
            account.holdings.append(holding)


__all__ = ["parse_demat_accounts"]
