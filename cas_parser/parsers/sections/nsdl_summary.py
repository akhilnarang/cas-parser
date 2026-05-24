"""Reader for the NSDL CAS portfolio summary.

The NSDL CAS front matter prints two distinct summary structures, both before
the detail sections:

1. An **account summary table** (page 2) — ``Your Demat Account and Mutual Fund
   Folios`` — grouped by holder ("In the joint Names of" / "In the Single Name
   of"). Each group lists every demat account (``Account Type`` / ``Account
   Details`` carrying the DP NAME + ``DP ID:`` + ``Client ID:`` + a ``Value in
   ```) and a combined ``Mutual Fund Folios`` row, then a per-group ``Total`` and
   finally a single ``Grand Total``. This is the cleanest reconciliation source:
   the per-account values map straight to DP/client identities and sum exactly to
   the grand total.
2. A per-group **PORTFOLIO COMPOSITION** table — ``ASSET CLASS / Value in ` / %``
   — whose rows carry the asset-class code in parentheses (``Equities (E)``,
   ``Mutual Fund Folios (F)``, ``Government Securities (G)``, ...). These rows are
   aggregated across both groups into ``CasSummary.asset_class_totals``.

The composition table extracts as a single mashed cell, so it is parsed from the
page *text* with a line regex; the account table extracts cleanly, so it is read
from the table grid.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import CasSummary
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.utils.amounts import parse_decimal

# "Equities (E) 90,307.50 38.20%" — a PORTFOLIO COMPOSITION asset-class line.
_COMPOSITION_LINE_RE = re.compile(
    r"^(?P<label>.+?)\s*\((?P<code>[A-Z]{1,3})\)\s+"
    r"(?P<value>[\d,]+\.\d{2})\s+[\d.]+%\s*$"
)
# "Grand Total 75,41,791.71" in the account-summary table.
_GRAND_TOTAL_LABEL = "GRAND TOTAL"
# "DP ID:IN301330 Client ID:17847669" inside an Account Details cell.
_DP_CLIENT_RE = re.compile(
    r"DP ID\s*:\s*(?P<dp_id>\w+)\s+Client ID\s*:\s*(?P<client_id>\w+)",
    re.IGNORECASE,
)


def _is_account_summary_table(rows: list[list[str | None]]) -> bool:
    """Return True for the page-2 ``Account Type / Account Details`` table."""
    flat = " ".join(clean_cell(cell).upper() for row in rows for cell in row)
    return "ACCOUNT TYPE" in flat and "ACCOUNT DETAILS" in flat


def parse_account_summary(
    pages: list[dict[str, Any]],
) -> tuple[dict[str, Decimal], list[Decimal], Decimal | None]:
    """Parse the page-2 account-summary table.

    Walks the ``Account Type / Account Details`` table, reading the per-account
    reported value (keyed by ``<dp_id>/<client_id>`` from the Account Details
    cell), the per-group ``Mutual Fund Folios`` combined values, and the single
    ``Grand Total``.

    Args:
        pages: Page payloads covering the front-matter summary section.

    Returns:
        A tuple of ``(account_totals, mf_group_totals, grand_total)`` where
        ``account_totals`` maps ``<dp_id>/<client_id>`` to its reported value,
        ``mf_group_totals`` is the list of per-group combined MF-folio values
        (in page order) and ``grand_total`` is the portfolio grand total.
    """
    account_totals: dict[str, Decimal] = {}
    mf_group_totals: list[Decimal] = []
    grand_total: Decimal | None = None

    for page in pages:
        for table in page.get("tables", []):
            if not _is_account_summary_table(table):
                continue
            for row in table:
                cells = [clean_cell(cell) for cell in row]
                label = cells[0].upper() if cells else ""
                details = cells[1] if len(cells) > 1 else ""
                value = _row_value(cells)
                if label.startswith("MUTUAL FUND FOLIOS"):
                    if value is not None:
                        mf_group_totals.append(value)
                    continue
                match = _DP_CLIENT_RE.search(details)
                if match and value is not None:
                    ref = f"{match.group('dp_id')}/{match.group('client_id')}"
                    account_totals[ref] = value
                    continue
                if _GRAND_TOTAL_LABEL in " ".join(cells).upper() and value is not None:
                    grand_total = value

    return account_totals, mf_group_totals, grand_total


def _row_value(cells: list[str]) -> Decimal | None:
    """Return the rightmost parseable ``Value in ``` amount in a row."""
    for cell in reversed(cells):
        value = parse_decimal(cell)
        if value is not None and ("." in cell or "," in cell):
            return value
    return None


def parse_composition(pages: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Aggregate the per-group PORTFOLIO COMPOSITION asset-class totals.

    Each holder group prints its own composition table; their non-zero
    asset-class rows are summed by label so the combined totals span the whole
    portfolio.

    Args:
        pages: Page payloads covering the front-matter summary section.

    Returns:
        A ``{asset_class_label: total}`` map aggregated across all groups,
        omitting zero-valued classes.
    """
    totals: dict[str, Decimal] = {}
    for page in pages:
        for raw_line in page.get("text", "").split("\n"):
            match = _COMPOSITION_LINE_RE.match(raw_line.strip())
            if not match:
                continue
            value = parse_decimal(match.group("value"))
            if value is None or value == 0:
                continue
            label = match.group("label").strip()
            totals[label] = totals.get(label, Decimal("0")) + value
    return totals


def parse_summary(pages: list[dict[str, Any]]) -> CasSummary:
    """Parse the NSDL portfolio summary (asset-class totals + grand total).

    Aggregates the per-group PORTFOLIO COMPOSITION asset-class rows and reads the
    portfolio ``Grand Total`` from the account-summary table — the reconciliation
    target.

    Args:
        pages: Page payloads covering the front-matter summary section.

    Returns:
        The parsed `CasSummary`.
    """
    asset_class_totals = parse_composition(pages)
    _, _, grand_total = parse_account_summary(pages)
    return CasSummary(
        asset_class_totals=asset_class_totals,
        grand_total=grand_total,
    )


__all__ = ["parse_account_summary", "parse_composition", "parse_summary"]
