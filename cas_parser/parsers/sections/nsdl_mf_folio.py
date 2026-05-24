"""Reader for the NSDL CAS mutual-fund folio section.

The NSDL CAS prints MF folios (RTA / CAMS-KFin data) as a ten-column table::

    ISIN / UCC | ISIN Description | Folio No. | No. of Units |
    Average Cost Per Units | Total Cost | Current NAV per unit |
    Current Value | Unrealised Profit/(Loss) | Annualised Return(%)

The ISIN cell carries the UCC on a second line; the description cell holds the
scheme name (wrapped across lines). Each scheme's printed ``Current Value`` is
its position value; ``cost`` is ``Total Cost`` and ``nav`` is ``Current NAV``, so
``units * nav`` reproduces the value. Schemes sharing a folio number are grouped
under one folio whose ``total_value`` is the sum of those values. A ``Sub Total``
/ ``Total`` row closes the table. Folio-to-AMC names come from the back-matter
"Folio No. / AMC NAME" KYC table, where each cell is ``<folio>\\n<AMC name>``.

Note: the MF folio table is distinct from the "Mutual Funds (M)" demat
sub-section (units of MF schemes held in *demat* form), which is a seven-column
balance table read by :mod:`cas_parser.parsers.sections.nsdl_demat`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import MfFolio, MfScheme
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.isin import find_isin

# Column indices in the ten-column MF folio table.
_COL_ISIN = 0
_COL_DESC = 1
_COL_FOLIO = 2
_COL_UNITS = 3
_COL_COST = 5
_COL_NAV = 6
_COL_VALUE = 7

_SUBTOTAL_LABELS = {"SUB TOTAL", "TOTAL"}

# A folio identifier on the first line of a KYC cell: a compact token of
# letters/digits/``/``/``-`` (no spaces) carrying at least one digit. Allowing
# alphanumeric and ``/``-separated forms (not just pure digits) means RTA folios
# like ``1234567/89`` or ``ABC12345`` still match and get their AMC, while prose
# label lines ("Folio No.") are rejected by the no-space / has-digit shape.
_FOLIO_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]*$")


def _is_mf_folio_table(rows: list[list[str | None]]) -> int | None:
    """Return the header-row index of the MF-folio table, or None.

    The header is the first or second row (a leading ``Mutual Fund Folios (F)``
    title cell may precede it). The table is identified by its distinctive
    ``ISIN Description`` + ``Folio No.`` + ``Current NAV`` columns.
    """
    for index, row in enumerate(rows[:2]):
        joined = " ".join(clean_cell(cell).upper() for cell in row)
        if (
            "ISIN DESCRIPTION" in joined
            and "FOLIO NO" in joined
            and "CURRENT NAV" in joined
        ):
            return index
    return None


def _first_line(cell: str | None) -> str:
    """Return the first physical line of a raw (possibly multi-line) cell."""
    if not cell:
        return ""
    return cell.split("\n", 1)[0].strip()


def _build_amc_map(pages: list[dict[str, Any]]) -> dict[str, str]:
    """Map folio number -> AMC name from the back-matter KYC folio table.

    Each KYC row's first cell is ``<folio number>\\n<AMC name>`` (the AMC name may
    wrap across further lines). Only rows whose first line looks like a folio
    identifier (see ``_FOLIO_TOKEN_RE``) are used — alphanumeric / ``/``-separated
    folios are accepted, not only pure-digit ones, so non-numeric RTA folios are
    still enriched while prose header lines are skipped.

    Args:
        pages: Page payloads to scan.

    Returns:
        A ``{folio_number: amc_name}`` map.
    """
    amc_by_folio: dict[str, str] = {}
    for page in pages:
        for table in page.get("tables", []):
            for row in table:
                if not row or not row[0]:
                    continue
                lines = [line.strip() for line in row[0].split("\n") if line.strip()]
                if len(lines) < 2:
                    continue
                folio, *amc_lines = lines
                if amc_lines and _is_folio_token(folio):
                    amc_by_folio.setdefault(folio, " ".join(amc_lines))
    return amc_by_folio


def _is_folio_token(value: str) -> bool:
    """Return True when ``value`` looks like a folio identifier token.

    A folio is a compact alphanumeric token (optionally with ``/`` or ``-``) that
    contains at least one digit — so ``1234567/89`` and ``ABC12345`` qualify but
    spaced prose lines ("Folio No.") and pure-word labels do not.
    """
    return bool(_FOLIO_TOKEN_RE.match(value)) and any(ch.isdigit() for ch in value)


def parse_mf_folios(
    pages: list[dict[str, Any]],
    legend: dict[str, str] | None = None,
) -> list[MfFolio]:
    """Parse MF folios and scheme positions from the NSDL MF section pages.

    Schemes sharing a folio number are grouped under one ``MfFolio`` (in row
    order), whose ``total_value`` is the sum of its schemes' printed ``Current
    Value`` — so a one-scheme folio keeps that single value as its total. AMC
    names are enriched from the back-matter KYC folio table.

    Args:
        pages: Page payloads covering the MF section (and the KYC folio table).
        legend: Parsed footnote legend; accepted for signature parity with the
            CDSL reader (the NSDL MF table carries no inline markers).

    Returns:
        Parsed MF folios with their scheme positions.
    """
    _ = legend  # NSDL MF rows carry no inline markers; kept for parity.
    amc_by_folio = _build_amc_map(pages)
    folios: list[MfFolio] = []
    folio_by_number: dict[str, MfFolio] = {}

    for page in pages:
        for table in page.get("tables", []):
            header_index = _is_mf_folio_table(table)
            if header_index is None:
                continue
            for row in table[header_index + 1 :]:
                cells = [clean_cell(cell) for cell in row]
                if len(cells) <= _COL_VALUE:
                    continue
                label = cells[_COL_ISIN].upper()
                if label in _SUBTOTAL_LABELS or not label:
                    continue
                folio_number = cells[_COL_FOLIO].strip()
                isin = find_isin(_first_line(row[_COL_ISIN]))
                if not folio_number or isin is None:
                    continue
                units = parse_decimal(cells[_COL_UNITS]) or Decimal("0")
                value = parse_decimal(cells[_COL_VALUE])
                scheme = MfScheme(
                    scheme_name=cells[_COL_DESC].strip(),
                    isin=isin,
                    units=units,
                    nav=parse_decimal(cells[_COL_NAV]),
                    value=value,
                    cost=parse_decimal(cells[_COL_COST]),
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
        folio.total_value = (folio.total_value or Decimal("0")) + scheme.value


__all__ = ["parse_mf_folios"]
