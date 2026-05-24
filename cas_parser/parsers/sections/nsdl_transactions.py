"""Reader for the NSDL CAS transaction section.

The NSDL consolidated CAS prints a per-account "Summary of Transactions" block,
introduced by the same ``<NSDL|CDSL> Demat Account ... DP ID: ... Client ID:
...`` header used in the holdings section, in two depository-specific layouts.

NSDL Demat Account transactions (eight columns)
-----------------------------------------------
::

    Date | Order No | Description | Instruction Details | Opening Balance |
    Debit | Credit | Closing Balance

Each security's movements are introduced by a standalone ``ISIN : <isin> -
<name>`` anchor row; the per-movement rows that follow carry no ISIN, so the
anchor's ISIN is carried forward. Credit is positive, Debit negative.

CDSL Demat Account transactions (five columns — embedded in the NSDL CAS)
------------------------------------------------------------------------
::

    Date | Transaction Particulars | Credit | Debit | Current Balance

Same ``ISIN : <isin> - <name>`` anchor convention; ``Opening Balance`` /
``Closing Balance`` rows are skipped. The particulars' leading token is a terse
code (``BSECH-CR``, ``INTDEP-CR``, ``PAYOUT-CR``, ``NSEDR``) humanized via
:mod:`cas_parser.parsers.utils.transaction_codes`.

Cross-page carry-forward: a security's movements (and even its ISIN anchor's
following rows) can straddle a page break, so the carried ISIN persists across
pages and resets only when a new account block opens — mirroring the CDSL
transaction reader.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from cas_parser.models import CasTransaction
from cas_parser.parsers.extractors.tables import clean_cell
from cas_parser.parsers.utils.amounts import parse_decimal
from cas_parser.parsers.utils.dates import parse_date
from cas_parser.parsers.utils.isin import is_valid_isin
from cas_parser.parsers.utils.transaction_codes import humanize_transaction_code

# "<NSDL|CDSL> Demat Account ... DP ID: <id> Client ID: <id>" account header.
_ACCOUNT_HEADER_RE = re.compile(
    r"(?P<dep>NSDL|CDSL)\s+Demat Account.*?DP ID\s*:\s*(?P<dp_id>\w+)\s+"
    r"Client ID\s*:\s*(?P<client_id>\w+)",
    re.IGNORECASE | re.DOTALL,
)
# "ISIN : <isin> - <name>" security anchor (the name may spill to a later cell).
_ISIN_ANCHOR_RE = re.compile(r"ISIN\s*:\s*([A-Z]{2}[A-Z0-9]{9}[0-9])", re.IGNORECASE)
# Leading terse transaction code on a CDSL particulars cell (e.g. "BSECH-CR",
# "INTDEP-CR", "PAYOUT-CR", "NSEDR").
_TXN_CODE_RE = re.compile(r"^([A-Z]{2,}(?:-[A-Z]{2,})?)")

_NIL_TOKENS = {"", "--", "-", "0", "0.000", "0.00"}
_BALANCE_LABELS = ("OPENING BALANCE", "CLOSING BALANCE")

# Fallback column indices for the CDSL five-column transaction table
# (Date | Transaction Particulars | Credit | Debit | Current Balance). The
# extractor sometimes inserts a spacer column that shifts Credit/Debit one to the
# right (e.g. the header becomes ``Date | Particulars | '' | Credit | Debit |
# Current Balance``); locating Credit/Debit by their header tokens corrects the
# sign that a fixed ``credit=cells[2]`` / ``debit=cells[3]`` would otherwise
# invert. These indices are only the fallback when the header lacks the tokens.
_CDSL_COL_DATE = 0
_CDSL_COL_PARTICULARS = 1
_CDSL_COL_CREDIT = 2
_CDSL_COL_DEBIT = 3
_CDSL_COL_BALANCE = 4


def _resolve_cdsl_columns(header: list[str | None]) -> dict[str, int]:
    """Map CDSL txn columns to indices from the header row, with fixed fallbacks.

    Resolves Date / Particulars / Credit / Debit / Current Balance from the
    header tokens so a spacer column that shifts Credit/Debit (and would
    otherwise invert the transaction sign) is handled. Any column whose token is
    not found keeps its documented fixed index.

    Args:
        header: The raw header row cells.

    Returns:
        A mapping with keys ``date``, ``particulars``, ``credit``, ``debit`` and
        ``balance``.
    """
    columns = {
        "date": _CDSL_COL_DATE,
        "particulars": _CDSL_COL_PARTICULARS,
        "credit": _CDSL_COL_CREDIT,
        "debit": _CDSL_COL_DEBIT,
        "balance": _CDSL_COL_BALANCE,
    }
    for index, cell in enumerate(header):
        token = clean_cell(cell).upper()
        if not token:
            continue
        if token.startswith("DATE"):
            columns["date"] = index
        elif "PARTICULARS" in token:
            columns["particulars"] = index
        elif token.startswith("CREDIT"):
            columns["credit"] = index
        elif token.startswith("DEBIT"):
            columns["debit"] = index
        elif "CURRENT" in token or "BALANCE" in token:
            columns["balance"] = index
    return columns


def _cdsl_columns_for_width(width: int) -> dict[str, int]:
    """Infer CDSL txn column indices for a header-less row by its width.

    A six-column row carries the spacer between Particulars and Credit, so
    Credit/Debit/Balance shift right by one; a five-column row uses the fixed
    indices.
    """
    if width >= 6:
        return {
            "date": 0,
            "particulars": 1,
            "credit": 3,
            "debit": 4,
            "balance": 5,
        }
    return {
        "date": _CDSL_COL_DATE,
        "particulars": _CDSL_COL_PARTICULARS,
        "credit": _CDSL_COL_CREDIT,
        "debit": _CDSL_COL_DEBIT,
        "balance": _CDSL_COL_BALANCE,
    }


def _is_nsdl_txn_header(rows: list[list[str | None]]) -> int | None:
    """Return the header-row index of the NSDL eight-column txn table, or None."""
    for index, row in enumerate(rows[:2]):
        joined = " ".join(clean_cell(cell).upper() for cell in row)
        if "ORDER NO" in joined and "INSTRUCTION DETAILS" in joined:
            return index
    return None


def _is_cdsl_txn_header(rows: list[list[str | None]]) -> int | None:
    """Return the header-row index of the CDSL five-column txn table, or None."""
    for index, row in enumerate(rows[:2]):
        joined = " ".join(clean_cell(cell).upper() for cell in row)
        if "TRANSACTION PARTICULARS" in joined and "CURRENT" in joined:
            return index
    return None


def _row_isin(row: list[str | None]) -> str | None:
    """Return the ISIN if the row is an ``ISIN : <isin> ...`` anchor row."""
    for cell in row:
        if not cell:
            continue
        match = _ISIN_ANCHOR_RE.search(cell)
        if match and is_valid_isin(match.group(1)):
            return match.group(1).upper()
    return None


def _signed_quantity(credit: str, debit: str) -> Decimal | None:
    """Return signed quantity: credit positive, debit negative, else None."""
    if credit.strip() not in _NIL_TOKENS:
        return parse_decimal(credit)
    if debit.strip() not in _NIL_TOKENS:
        value = parse_decimal(debit)
        return None if value is None else -value
    return None


def _account_ref_in_table(rows: list[list[str | None]]) -> str | None:
    """Return the ``<dp_id>/<client_id>`` ref if a row opens an account block."""
    for row in rows:
        for cell in row:
            if not cell:
                continue
            match = _ACCOUNT_HEADER_RE.search(cell)
            if match:
                return (
                    f"{match.group('dp_id').strip()}/{match.group('client_id').strip()}"
                )
    return None


def _parse_nsdl_rows(
    rows: list[list[str | None]],
    current_ref: str,
    last_isin: str | None,
) -> tuple[list[CasTransaction], str | None]:
    """Parse NSDL eight-column transaction rows for the current account.

    Args:
        rows: Table rows after the column header.
        current_ref: The account source ref these rows belong to.
        last_isin: ISIN carried forward from the last anchor row (across pages).

    Returns:
        ``(transactions, last_isin)`` — parsed rows and the updated carried ISIN.
    """
    transactions: list[CasTransaction] = []
    for row in rows:
        anchor = _row_isin(row)
        if anchor is not None:
            last_isin = anchor
            continue
        cells = [clean_cell(cell) for cell in row]
        txn_date = parse_date(cells[0]) if cells else None
        if txn_date is None:
            continue
        # Columns: Date, Order No, Description, Instruction Details, Opening,
        # Debit, Credit, Closing. The trailing four are balances/movements.
        numeric = [c for c in cells if parse_decimal(c) is not None]
        if len(numeric) < 4:
            continue
        _opening, debit, credit, _closing = numeric[-4:]
        description = _nsdl_narration(cells)
        quantity = _signed_quantity(credit, debit)
        transactions.append(
            CasTransaction(
                scope="demat",
                source_ref=current_ref,
                date=txn_date,
                description=description,
                isin=last_isin,
                transaction_type=_nsdl_transaction_type(description),
                quantity=quantity,
                reference=_first_order_no(cells),
            )
        )
    return transactions, last_isin


def _parse_cdsl_rows(
    rows: list[list[str | None]],
    current_ref: str,
    last_isin: str | None,
    columns: dict[str, int],
    header_width: int,
) -> tuple[list[CasTransaction], str | None]:
    """Parse CDSL five-column transaction rows for the current account.

    Args:
        rows: Table rows after the column header.
        current_ref: The account source ref these rows belong to.
        last_isin: ISIN carried forward from the last anchor row (across pages).
        columns: Header-resolved column indices (date/particulars/credit/debit/
            balance) so a spacer-shifted Credit/Debit keeps the correct sign.
        header_width: The header row's cell count. A data row of the same width
            shares the header's spacer alignment and uses ``columns``; a data row
            of a different width (the extractor sometimes drops the header's
            spacer on the value rows) is resolved from its own width instead, so
            the Credit/Debit sign stays correct either way.

    Returns:
        ``(transactions, last_isin)`` — parsed rows and the updated carried ISIN.
    """
    transactions: list[CasTransaction] = []
    for row in rows:
        anchor = _row_isin(row)
        if anchor is not None:
            last_isin = anchor
            continue
        cells = [clean_cell(cell) for cell in row]
        # Use the header-resolved columns only when this row matches the header's
        # width; otherwise infer the columns from the row's own width (the spacer
        # presence can differ between the header and its value rows).
        row_columns = columns if len(cells) == header_width else _cdsl_columns_for_width(
            len(cells)
        )
        date_index = row_columns["date"]
        txn_date = parse_date(cells[date_index]) if len(cells) > date_index else None
        if txn_date is None:
            continue
        particulars = (
            cells[row_columns["particulars"]]
            if len(cells) > row_columns["particulars"]
            else ""
        )
        if particulars.upper() in _BALANCE_LABELS:
            continue
        # Credit/Debit are located by header/width (see _resolve_cdsl_columns)
        # so a spacer column does not invert the sign.
        credit = (
            cells[row_columns["credit"]] if len(cells) > row_columns["credit"] else ""
        )
        debit = (
            cells[row_columns["debit"]] if len(cells) > row_columns["debit"] else ""
        )
        quantity = _signed_quantity(credit, debit)
        transactions.append(
            CasTransaction(
                scope="demat",
                source_ref=current_ref,
                date=txn_date,
                description=particulars,
                isin=last_isin,
                transaction_type=_cdsl_transaction_type(particulars),
                quantity=quantity,
                reference=_cdsl_reference(particulars),
            )
        )
    return transactions, last_isin


def _nsdl_narration(cells: list[str]) -> str:
    """Build an NSDL row's description from its narration cells.

    The NSDL transaction table extracts with shifting empty columns, so the
    description / instruction-details text cannot be read by fixed index. The
    narration is every cell that is neither the leading date, a pure number
    (balance/movement), nor the long order-number token.

    Args:
        cells: Cleaned row cells.

    Returns:
        The joined narration, " / "-separated.
    """
    parts: list[str] = []
    for index, cell in enumerate(cells):
        if index == 0 or not cell:
            continue
        if parse_decimal(cell) is not None:
            continue
        if cell.isdigit():
            continue
        parts.append(cell)
    return " / ".join(parts)


def _nsdl_transaction_type(description: str) -> str | None:
    """Derive a readable transaction type for an NSDL narration.

    The NSDL demat statement narrates rather than coding ("By CM ..., T+1
    NORMAL" with "Standing Instruction to receive credit"), so the direction
    words drive the label.

    Args:
        description: The joined Description / Instruction Details narration.

    Returns:
        A readable label, or None when nothing recognisable is present.
    """
    upper = description.upper()
    if "RECEIVE" in upper or upper.startswith("BY "):
        return "Receipt"
    if "DELIVER" in upper:
        return "Delivery"
    return None


def _cdsl_transaction_type(particulars: str) -> str | None:
    """Return the readable type for a CDSL particulars cell via its leading code."""
    match = _TXN_CODE_RE.match(particulars.strip())
    return humanize_transaction_code(match.group(1)) if match else None


def _cdsl_reference(particulars: str) -> str | None:
    """Return the settlement reference token from a CDSL particulars cell.

    Prefers the ``SETT <ref>`` settlement number; otherwise the last numeric
    token in the cell.

    Args:
        particulars: The raw particulars narration.

    Returns:
        A single reference token, or None when none is present.
    """
    flat = " ".join(particulars.split())
    sett = re.search(r"SETT\s+([0-9]+)", flat, re.IGNORECASE)
    if sett:
        return sett.group(1)
    numbers = re.findall(r"\d{6,}", flat)
    return numbers[-1] if numbers else None


def parse_transactions(pages: list[dict[str, Any]]) -> list[CasTransaction]:
    """Parse demat transactions from the NSDL transaction-section pages.

    Walks pages in order, switching the current account whenever an account
    header appears and resetting the carried ISIN, then parses each NSDL eight-
    column or CDSL five-column transaction table. ISIN anchors carry forward
    across rows and page breaks within an account.

    Args:
        pages: Page payloads covering the transaction section.

    Returns:
        Parsed demat transactions stamped with each account's source ref.
    """
    transactions: list[CasTransaction] = []
    current_ref: str | None = None
    last_isin: str | None = None

    for page in pages:
        for table in page.get("tables", []):
            if not table:
                continue
            opened = _account_ref_in_table(table)
            if opened is not None:
                current_ref = opened
                last_isin = None

            if current_ref is None:
                continue

            nsdl_index = _is_nsdl_txn_header(table)
            cdsl_index = _is_cdsl_txn_header(table)
            if nsdl_index is not None:
                rows, last_isin = _parse_nsdl_rows(
                    table[nsdl_index + 1 :], current_ref, last_isin
                )
                transactions.extend(rows)
            elif cdsl_index is not None:
                header = table[cdsl_index]
                columns = _resolve_cdsl_columns(header)
                rows, last_isin = _parse_cdsl_rows(
                    table[cdsl_index + 1 :],
                    current_ref,
                    last_isin,
                    columns,
                    len(header),
                )
                transactions.extend(rows)
            else:
                # A continuation table (no header) of movement rows for the
                # current account — try both shapes by column count.
                rows, last_isin = _parse_continuation(table, current_ref, last_isin)
                transactions.extend(rows)

    return transactions


def _parse_continuation(
    table: list[list[str | None]],
    current_ref: str,
    last_isin: str | None,
) -> tuple[list[CasTransaction], str | None]:
    """Parse a header-less continuation table of movement/anchor rows.

    Such tables wrap onto a new page mid-account. The shape is inferred per row:
    an ``ISIN :`` anchor updates the carried ISIN; an eight-or-more-column row is
    NSDL-shaped (Debit/Credit before Closing), a five/six-column row is
    CDSL-shaped (Credit/Debit after particulars).

    Args:
        table: The header-less table rows.
        current_ref: The account source ref these rows belong to.
        last_isin: ISIN carried forward from the last anchor row.

    Returns:
        ``(transactions, last_isin)`` — parsed rows and the updated carried ISIN.
    """
    transactions: list[CasTransaction] = []
    for row in table:
        anchor = _row_isin(row)
        if anchor is not None:
            last_isin = anchor
            continue
        cells = [clean_cell(cell) for cell in row]
        txn_date = parse_date(cells[0]) if cells else None
        if txn_date is None:
            continue
        # Skip header echoes ("Date", "Order No", ...) that lack a parseable date
        # (handled above) and balance rows.
        if len(cells) > 1 and cells[1].upper() in _BALANCE_LABELS:
            continue
        numeric = [c for c in cells if parse_decimal(c) is not None]
        if len(numeric) >= 4 and _looks_nsdl(cells):
            _opening, debit, credit, _closing = numeric[-4:]
            description = _nsdl_narration(cells)
            transactions.append(
                CasTransaction(
                    scope="demat",
                    source_ref=current_ref,
                    date=txn_date,
                    description=description,
                    isin=last_isin,
                    transaction_type=_nsdl_transaction_type(description),
                    quantity=_signed_quantity(credit, debit),
                    reference=_first_order_no(cells),
                )
            )
        elif len(cells) >= 4:
            # Infer Credit/Debit positions from the row width: a six-column row
            # carries the spacer that shifts them right, so resolving by width
            # (not fixed cells[2]/cells[3]) keeps the sign correct.
            columns = _cdsl_columns_for_width(len(cells))
            particulars = cells[columns["particulars"]]
            credit = (
                cells[columns["credit"]] if len(cells) > columns["credit"] else ""
            )
            debit = cells[columns["debit"]] if len(cells) > columns["debit"] else ""
            transactions.append(
                CasTransaction(
                    scope="demat",
                    source_ref=current_ref,
                    date=txn_date,
                    description=particulars,
                    isin=last_isin,
                    transaction_type=_cdsl_transaction_type(particulars),
                    quantity=_signed_quantity(credit, debit),
                    reference=_cdsl_reference(particulars),
                )
            )
    return transactions, last_isin


def _looks_nsdl(cells: list[str]) -> bool:
    """Heuristically tell an NSDL movement row from a CDSL one.

    An NSDL row carries the narration ("By CM ...", "Standing Instruction ...")
    in its text cells; a CDSL row's second cell is a terse code/particulars.
    """
    joined = " ".join(cells).upper()
    return "BY CM" in joined or "INSTRUCTION" in joined or "BENEFICIARY" in joined


def _first_order_no(cells: list[str]) -> str | None:
    """Return the first long numeric token (an NSDL order number), if any."""
    for cell in cells[1:]:
        if cell.isdigit() and len(cell) >= 8:
            return cell
    return None


__all__ = ["parse_transactions"]
