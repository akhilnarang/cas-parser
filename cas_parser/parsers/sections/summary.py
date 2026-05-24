"""Shared reader for the portfolio-summary section of a CAS.

Parses the asset-class totals and grand total. The CDSL CAS prints a compact
"Asset Class / Value / Percentage" table whose final ``Total`` row is the
portfolio grand total; the asset rows (e.g. "Equity", "Mutual Fund Folios",
"Mutual Funds Held in Demat Form") become ``asset_class_totals``.

Parse this first to obtain the reconciliation target (``grand_total``), then
verify the detail sections against it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from cas_parser.models import CasSummary
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.utils.amounts import parse_decimal

# Header tokens (uppercased) that identify the asset-class summary table.
_ASSET_HEADER_TOKENS = ("ASSET CLASS",)
# Row labels (uppercased) that mark the grand-total line of the table.
_TOTAL_LABELS = ("TOTAL", "GRAND TOTAL")


def _is_asset_table(rows: list[list[str | None]]) -> bool:
    """Return True when a table looks like the asset-class summary table."""
    if not rows:
        return False
    header = " ".join(clean_cell(cell).upper() for cell in rows[0])
    return any(token in header for token in _ASSET_HEADER_TOKENS)


def parse_summary(pages: list[dict[str, Any]]) -> CasSummary:
    """Parse the portfolio summary (asset-class totals + grand total).

    Walks the summary section pages for the asset-class table (``Asset Class /
    Value / Percentage``). Each non-total row contributes one entry to
    ``asset_class_totals`` keyed by its printed label; the ``Total`` row sets
    ``grand_total``.

    Args:
        pages: Page payloads covering the summary section.

    Returns:
        The parsed `CasSummary`.
    """
    asset_class_totals: dict[str, Decimal] = {}
    grand_total: Decimal | None = None

    for page in pages:
        for table in page.get("tables", []):
            if not _is_asset_table(table):
                continue
            for row in table[1:]:
                cells = [clean_cell(cell) for cell in row]
                if len(cells) < 2 or not cells[0]:
                    continue
                label = cells[0]
                value = parse_decimal(cells[1])
                if value is None:
                    continue
                if label.upper() in _TOTAL_LABELS:
                    grand_total = value
                else:
                    asset_class_totals[label] = value

    return CasSummary(
        asset_class_totals=asset_class_totals,
        grand_total=grand_total,
    )


__all__ = ["parse_summary"]
