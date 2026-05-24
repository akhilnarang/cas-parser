"""Shared reader for the transaction section of a CAS.

CDSL layout
-----------
The CDSL CAS prints a per-account "STATEMENT OF TRANSACTIONS" block as a clean
nine-column table::

    ISIN | Security | Transaction (particulars) | Date | Opening | Credit |
    Debit | Closing | Stamp Duty

Each account's transaction block is introduced by a bold ``BO ID`` header whose
digits are doubled by the bold rendering (e.g. ``1122008833...`` for BO ID
``12083400...``); the 16-digit BO ID is ``<dp_id><client_id>``. Rows for the
same security wrap onto continuation rows with blank ISIN/name cells, so the
last seen ISIN/name is carried forward.

The "Transaction" cell packs three things on separate lines: a leading
transaction code with a ``-CR`` / ``-DR`` direction suffix (the statement's own
transaction category, e.g. ``EP-DR``, ``PAYOUT-CR``, ``INTDEP-CR``,
``BSECH-CR``), an optional ``Txn:<ref>`` instruction reference, and trailing
settlement / counterparty reference numbers. These map to ``transaction_type``
(a human-readable label), ``description`` (the full cleaned narration) and
``reference`` (the instruction / settlement reference).

The terse CDSL code is humanized for ``transaction_type`` — its prefix is
expanded via :mod:`cas_parser.parsers.utils.transaction_codes` (e.g. ``EP`` ->
``"Early Pay-in"``, ``PAYOUT`` -> ``"Payout"``) and the ``-CR``/``-DR``
direction suffix is dropped because the direction is already carried by the
signed ``quantity``. The *raw* code is never lost: ``description`` is the full
narration whose first token is the original code (e.g. ``"EP-DR Txn:..."``), so
the original value round-trips losslessly.

Demat transactions carry a signed ``quantity``: a credit is positive, a debit
negative. The parsed transactions are stamped with the owning account's
``source_ref`` (``<dp_id>/<client_id>``).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import CasTransaction
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.dates import parse_date
from cas_parser.parsers.utils.isin import find_isin
from cas_parser.parsers.utils.transaction_codes import humanize_transaction_code

# Bold-doubled "BO ID :: <doubled digits>" header that opens an account block.
# Bold rendering doubles glyphs ("BBOO IDID :: 1122..."), so allow repeated
# B/O/I/D letters and colons before the digit run.
_BO_ID_RE = re.compile(r"B[BO\s]*[ID]+\s*:+\s*([0-9]+)", re.IGNORECASE)

# Column indices in the nine-column transaction table.
_COL_ISIN = 0
_COL_NAME = 1
_COL_PARTICULARS = 2
_COL_DATE = 3
_COL_CREDIT = 5
_COL_DEBIT = 6

_NIL_TOKENS = {"", "--", "-", "0", "0.000", "0.00"}
_FOOTNOTE_MARKERS = "!@#$*"

# Leading transaction code on the particulars' first line, e.g. "EP-DR",
# "PAYOUT-CR", "INTDEP-CR", "BSECH-CR" (two+ letters, optional "-XX" segments).
_TXN_CODE_RE = re.compile(r"^([A-Z]{2,}(?:-[A-Z]{2,})+|[A-Z]{2,})")
# Explicit "Txn:<ref>" instruction reference anywhere in the cell.
_TXN_REF_RE = re.compile(r"Txn\s*:\s*([0-9A-Za-z]+)", re.IGNORECASE)
# A clearing-member marker that follows the code but is not itself a reference.
_CM_MARKER_RE = re.compile(r"^CM\b")


def _dedouble(digits: str) -> str:
    """Collapse bold-doubled digits (``1122...`` -> ``12...``).

    The CDSL bold rendering duplicates every glyph, so a 32-character run is the
    16-digit BO ID. Non-doubled runs are returned unchanged.
    """
    if len(digits) % 2 == 0 and all(
        digits[i] == digits[i + 1] for i in range(0, len(digits), 2)
    ):
        return digits[::2]
    return digits


def _source_ref_from_bo_id(bo_id: str) -> str | None:
    """Convert a 16-digit BO ID into a ``<dp_id>/<client_id>`` source ref."""
    collapsed = _dedouble(bo_id)
    if len(collapsed) != 16:
        return None
    return f"{collapsed[:8]}/{collapsed[8:]}"


def _is_transaction_table(rows: list[list[str | None]]) -> bool:
    """Return True when a table is the nine-column transaction table.

    The transaction header renders in bold, so its labels come out glyph-
    doubled and space-interleaved ("IIS SSII NNN", "SD tटa uाm�typp"). The
    "STAMP" duty column is unique to this table among the CDSL tables, so we key
    on a nine-column table whose header mentions stamp duty.
    """
    if not rows:
        return False
    header = [clean_cell(cell).upper() for cell in rows[0]]
    if len(header) < 9:
        return False
    # De-space the header so glyph-interleaved labels ("S T A M P") still match.
    joined = " ".join(header).replace(" ", "")
    return "STAMP" in joined


def _cell(cells: list[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""


def _signed_quantity(credit: str, debit: str) -> Decimal | None:
    """Return signed quantity: credit positive, debit negative, else None."""
    if credit.strip() not in _NIL_TOKENS:
        return parse_decimal(credit)
    if debit.strip() not in _NIL_TOKENS:
        value = parse_decimal(debit)
        return None if value is None else -value
    return None


def _transaction_code(particulars: str) -> str | None:
    """Extract the raw statement transaction code from the particulars.

    The leading token on the first line is the CDSL transaction category with a
    direction suffix (e.g. ``EP-DR``, ``PAYOUT-CR``, ``INTDEP-CR``).

    Args:
        particulars: Raw transaction-cell text (newlines preserved).

    Returns:
        The leading raw transaction code, or None when no code is present.
    """
    first_line = particulars.split("\n", 1)[0].strip()
    match = _TXN_CODE_RE.match(first_line)
    return match.group(1) if match else None


def _transaction_type(particulars: str) -> str | None:
    """Return the human-readable transaction-type label for the particulars.

    Extracts the raw CDSL code (see :func:`_transaction_code`) and expands it via
    :func:`humanize_transaction_code` — the category prefix becomes a readable
    label and the redundant ``-CR``/``-DR`` direction is dropped (the direction
    is already carried by the signed quantity). The raw code is preserved in the
    transaction ``description`` so nothing is lost.

    Args:
        particulars: Raw transaction-cell text (newlines preserved).

    Returns:
        The readable label (e.g. ``"Early Pay-in"``), or None when no code is
        present.
    """
    return humanize_transaction_code(_transaction_code(particulars))


def _transaction_reference(particulars: str) -> str | None:
    """Extract the instruction / settlement reference from the particulars.

    Prefers an explicit ``Txn:<ref>`` instruction number; otherwise falls back
    to the first reference token that follows the leading code (skipping the
    clearing-member ``CM`` marker), then to the first token of the next line.

    Args:
        particulars: Raw transaction-cell text (newlines preserved).

    Returns:
        A single reference token, or None when none is present.
    """
    flat = " ".join(particulars.split())
    txn_match = _TXN_REF_RE.search(flat)
    if txn_match:
        return txn_match.group(1)

    lines = [line.strip() for line in particulars.split("\n") if line.strip()]
    if not lines:
        return None
    # Strip the leading code (and an optional "CM" clearing-member marker) from
    # the first line; whatever remains starts with the reference.
    rest = _TXN_CODE_RE.sub("", lines[0]).strip()
    rest = _CM_MARKER_RE.sub("", rest).strip()
    if rest:
        return rest.split()[0]
    if len(lines) >= 2:
        return lines[1].split()[0]
    return None


def _bo_id_refs_with_offsets(text: str) -> list[tuple[int, str]]:
    """Return ``(text offset, source_ref)`` for every BO-ID header on a page.

    A page can carry more than one account block (two BO-ID headers), so all of
    them are collected with their position in the page text; a table is later
    attributed to the nearest one that precedes it.
    """
    offsets: list[tuple[int, str]] = []
    for match in _BO_ID_RE.finditer(text):
        ref = _source_ref_from_bo_id(match.group(1))
        if ref is not None:
            offsets.append((match.start(), ref))
    return offsets


def _table_text_offset(table: list[list[str | None]], text: str) -> int | None:
    """Return where a table's content first appears in the page text, or None.

    pdfplumber's page ``text`` includes the table cell text in reading order, so
    locating a distinctive cell of the table inside that text gives the table's
    vertical position relative to the BO-ID headers. The first non-empty cell that
    is found in the text wins.
    """
    for row in table:
        for cell in row:
            token = clean_cell(cell)
            if not token:
                continue
            index = text.find(token)
            if index != -1:
                return index
    return None


def _ref_for_table(
    table: list[list[str | None]],
    text: str,
    bo_offsets: list[tuple[int, str]],
    current_ref: str | None,
) -> str | None:
    """Pick the source ref for a transaction table by nearest preceding BO-ID.

    Among the page's BO-ID headers, choose the one with the greatest text offset
    that still precedes the table's own offset, so two account blocks on one page
    attribute their tables correctly. Falls back to the first BO-ID on the page
    (when the table's position cannot be located) and finally to ``current_ref``
    carried from a previous page (continuation tables on a header-less page).
    """
    if not bo_offsets:
        return current_ref
    table_offset = _table_text_offset(table, text)
    if table_offset is None:
        return bo_offsets[0][1]
    preceding = [ref for offset, ref in bo_offsets if offset <= table_offset]
    if preceding:
        return preceding[-1]
    # The table sits above the first BO-ID header on the page — attribute it to
    # the account carried from the previous page if known, else the first header.
    return current_ref or bo_offsets[0][1]


def parse_transactions(pages: list[dict[str, Any]]) -> list[CasTransaction]:
    """Parse demat transactions from the transaction-section pages.

    Walks pages in order. Each transaction table is attributed to the account of
    the ``BO ID`` header that *immediately precedes it* on the page (by text
    position), so two account blocks sharing a page are stamped correctly rather
    than all going to the first BO-ID. Continuation rows (blank ISIN/name) reuse
    the last security context. Each row's particulars cell yields
    ``transaction_type`` (the code), ``description`` (the narration) and
    ``reference`` (the instruction/settlement reference).

    Args:
        pages: Page payloads covering the transaction section.

    Returns:
        Parsed demat transactions stamped with each account's source ref.
    """
    transactions: list[CasTransaction] = []
    current_ref: str | None = None
    # Last security ISIN seen in the current account block. A security's rows
    # wrap onto continuation rows with blank ISIN/name cells, and that wrap can
    # straddle a page break, so the carried ISIN must persist across pages and
    # only reset when a new account (BO ID) block opens — mirroring how demat.py
    # carries per-account holding state across pages without per-page resets.
    last_isin: str | None = None

    for page in pages:
        text = page.get("text", "")
        bo_offsets = _bo_id_refs_with_offsets(text)

        for table in page.get("tables", []):
            if not _is_transaction_table(table):
                continue
            ref = _ref_for_table(table, text, bo_offsets, current_ref)
            if ref is None:
                continue
            # Reset the carried ISIN whenever the attributed account changes (a
            # new BO-ID block opens), so a continuation ISIN never leaks across
            # account boundaries — including two blocks on the same page.
            if ref != current_ref:
                current_ref = ref
                last_isin = None
            last_isin = _append_table_rows(transactions, table, ref, last_isin)

    return transactions


def _append_table_rows(
    transactions: list[CasTransaction],
    table: list[list[str | None]],
    current_ref: str,
    last_isin: str | None,
) -> str | None:
    """Parse a transaction table's rows for ``current_ref`` and append them.

    Returns the updated carried ISIN (continuation rows with blank ISIN/name
    inherit the last seen security).
    """
    for row in table[1:]:
        cells = [clean_cell(cell) for cell in row]
        # A security's first row prints its ISIN; its further movements wrap onto
        # continuation rows with blank ISIN/name cells. Every CDSL transaction in
        # this table is a *security* movement (e.g. EP-DR early-payin debit,
        # PAYOUT-CR / INTDEP-CR / BSECH-CR credits) — none is a cash payout,
        # charge/fee or opening-balance line — so a blank ISIN cell is always a
        # continuation, never a genuinely security-less row. We carry the last
        # seen ISIN forward (across page breaks, until the next account block) so
        # those continuation rows inherit their security.
        isin = find_isin(_cell(cells, _COL_ISIN).rstrip(_FOOTNOTE_MARKERS))
        if isin is not None:
            last_isin = isin
        txn_date = parse_date(_cell(cells, _COL_DATE))
        if txn_date is None:
            continue
        quantity = _signed_quantity(
            _cell(cells, _COL_CREDIT), _cell(cells, _COL_DEBIT)
        )
        # Keep the raw particulars (newlines intact) for code/reference
        # extraction, but flatten newlines for the stored narration.
        raw_particulars = row[_COL_PARTICULARS] or ""
        narration = " ".join(raw_particulars.split())
        transactions.append(
            CasTransaction(
                scope="demat",
                source_ref=current_ref,
                date=txn_date,
                description=narration,
                isin=isin or last_isin,
                transaction_type=_transaction_type(raw_particulars),
                quantity=quantity,
                reference=_transaction_reference(raw_particulars),
            )
        )
    return last_isin


__all__ = ["parse_transactions"]
