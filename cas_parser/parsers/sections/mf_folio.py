"""Shared reader for the mutual-fund folio section of a CAS.

Parses MF folios (RTA identifiers) and their scheme positions (units, NAV,
value, cost).

CDSL layout
-----------
The CDSL CAS prints a "MUTUAL FUND UNITS HELD WITH MF/RTA" table::

    Scheme Name | ISIN | Folio No. | Closing Bal (units) | NAV |
    Cumulative Amount Invested | Valuation | Unrealised P/L | Unrealised P/L %

Each scheme row carries its own folio number. Rows that share a folio number are
grouped under one ``MfFolio`` (in row order), whose ``total_value`` is the sum of
its schemes' printed ``Valuation`` values; the common case of one scheme per
folio therefore keeps that single valuation as the folio total. A final
``Grand Total`` row prints the all-folio total but no per-folio subtotals. AMC
names are not in the holdings table — they come from the "Account Details / MF
Folios" block (``AMC Name : ... Folio No : ...``).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import MfFolio, MfScheme
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.sections.legend import detect_markers, resolve_notes
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.isin import find_isin

# Column indices in the MF holdings table.
_COL_SCHEME = 0
_COL_ISIN = 1
_COL_FOLIO = 2
_COL_UNITS = 3
_COL_NAV = 4
_COL_COST = 5
_COL_VALUE = 6

# Header tokens (uppercased) identifying the MF holdings table.
_MF_HEADER_TOKENS = ("SCHEME NAME", "FOLIO NO")
# Row labels (uppercased) that mark the all-folio grand-total line.
_TOTAL_LABELS = ("GRAND TOTAL", "TOTAL")
# "AMC Name : <amc> ... Folio No : <folio>" within the account-details block.
_AMC_BLOCK_RE = re.compile(
    r"AMC Name\s*:\s*(?P<amc>.+?)\s*\n.*?Folio No\s*:\s*(?P<folio>[\w/]+)",
    re.IGNORECASE | re.DOTALL,
)


def _is_mf_table(rows: list[list[str | None]]) -> bool:
    """Return True when a table is the MF holdings table."""
    if not rows:
        return False
    # Inspect the first couple of rows; the header sometimes wraps onto a title
    # row above the column labels.
    header = " ".join(clean_cell(cell).upper() for row in rows[:2] for cell in row)
    return all(token in header for token in _MF_HEADER_TOKENS)


def _build_amc_map(pages: list[dict[str, Any]]) -> dict[str, str]:
    """Map folio number -> AMC name from the account-details MF block."""
    amc_by_folio: dict[str, str] = {}
    for page in pages:
        for match in _AMC_BLOCK_RE.finditer(page.get("text", "")):
            folio = match.group("folio").strip()
            amc = " ".join(match.group("amc").split())
            amc_by_folio.setdefault(folio, amc)
    return amc_by_folio


def _header_index(rows: list[list[str | None]]) -> int | None:
    """Return the index of the column-label header row in an MF table."""
    for index, row in enumerate(rows):
        cells = [clean_cell(cell).upper() for cell in row]
        if cells and cells[_COL_SCHEME].startswith("SCHEME NAME"):
            return index
    return None


def parse_mf_folios(
    pages: list[dict[str, Any]], legend: dict[str, str] | None = None
) -> list[MfFolio]:
    """Parse MF folios and scheme positions from the MF section pages.

    Args:
        pages: Page payloads covering the mutual-fund section (must include the
            account-details MF block for AMC enrichment).
        legend: Parsed footnote legend (``{marker: definition}``); drives which
            scheme/ISIN markers are recorded on schemes. Defaults to empty.

    Returns:
        Parsed MF folios with their scheme positions. Schemes that share a folio
        number are grouped under one ``MfFolio`` (preserving row order and the
        first AMC seen), and the folio's ``total_value`` is the sum of its
        schemes' values. A folio with a single scheme therefore keeps that
        scheme's value as its total (the common CDSL one-scheme-per-folio case).
    """
    legend = legend or {}
    amc_by_folio = _build_amc_map(pages)
    folios: list[MfFolio] = []
    folio_by_number: dict[str, MfFolio] = {}

    for page in pages:
        for table in page.get("tables", []):
            if not _is_mf_table(table):
                continue
            header_index = _header_index(table)
            if header_index is None:
                continue
            for row in table[header_index + 1 :]:
                cells = [clean_cell(cell) for cell in row]
                if len(cells) <= _COL_VALUE:
                    continue
                label = cells[_COL_SCHEME].upper()
                if any(label.startswith(total) for total in _TOTAL_LABELS):
                    continue
                folio_number = cells[_COL_FOLIO].strip()
                if not folio_number:
                    continue
                units = parse_decimal(cells[_COL_UNITS]) or Decimal(0)
                value = parse_decimal(cells[_COL_VALUE])
                # Detect any legend-defined footnote markers on the scheme/ISIN
                # cells (only markers the legend defines are stripped/recorded).
                name_flags, scheme_name = detect_markers(cells[_COL_SCHEME], legend)
                isin_flags, isin_clean = detect_markers(cells[_COL_ISIN], legend)
                found = set(name_flags) | set(isin_flags)
                flags = [marker for marker in legend if marker in found]
                scheme = MfScheme(
                    scheme_name=scheme_name.strip(),
                    isin=find_isin(isin_clean.strip()),
                    units=units,
                    nav=parse_decimal(cells[_COL_NAV]),
                    value=value,
                    cost=parse_decimal(cells[_COL_COST]),
                    flags=flags,
                    notes=resolve_notes(flags, legend),
                )
                _add_scheme(
                    folios, folio_by_number, folio_number, scheme, amc_by_folio
                )

    return folios


def _add_scheme(
    folios: list[MfFolio],
    folio_by_number: dict[str, MfFolio],
    folio_number: str,
    scheme: MfScheme,
    amc_by_folio: dict[str, str],
) -> None:
    """Append a scheme to its folio, creating the folio on first sight.

    Schemes sharing a folio number accumulate into one ``MfFolio`` (in row
    order), and the folio's ``total_value`` becomes the running sum of its
    schemes' values. The first AMC seen for a folio is kept.
    """
    folio = folio_by_number.get(folio_number)
    if folio is None:
        folio = MfFolio(
            folio_number=folio_number,
            amc=amc_by_folio.get(folio_number),
            schemes=[],
            total_value=None,
        )
        folio_by_number[folio_number] = folio
        folios.append(folio)
    folio.schemes.append(scheme)
    if scheme.value is not None:
        folio.total_value = (folio.total_value or Decimal(0)) + scheme.value


__all__ = ["parse_mf_folios"]
