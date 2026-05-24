"""CDSL demat transaction-code -> human-readable label mapping.

The CDSL CAS prints a terse code in each transaction's "Particulars" cell,
e.g. ``EP-DR``, ``PAYOUT-CR``, ``INTDEP-CR``, ``BSECH-CR``. The code is two
parts joined by ``-``:

- a *category* prefix (``EP``, ``PAYOUT``, ``INTDEP``, ``BSECH``, ...), and
- a ``CR`` / ``DR`` *direction* suffix (credit / debit).

The direction is redundant with the signed ``quantity`` the parser already
stores (credit positive, debit negative), so the readable label drops it and
expands only the category. The original terse code is **never discarded**: it is
still the first token of ``CasTransaction.description`` (the full narration), so
the raw value round-trips losslessly even after ``transaction_type`` is
humanized.

``CODE_LABELS`` is the extensible map of *category prefixes* to readable
labels. To support a new code, add its prefix here. Unknown prefixes never crash
or drop data: ``humanize_transaction_code`` falls back to a title-cased version
of the raw prefix (with the raw token preserved in the description), so a new
CDSL code degrades gracefully into a legible label.
"""

from __future__ import annotations

# Category prefix (the part before the "-CR"/"-DR" suffix) -> readable label.
# Extend this dict to teach the parser a new CDSL transaction category. Every
# code observed in real CDSL CAS dumps is mapped here; the suffix carries only
# the credit/debit direction, which is already reflected in the signed quantity.
CODE_LABELS: dict[str, str] = {
    "EP": "Early Pay-in",
    "PAYOUT": "Payout",
    "INTDEP": "Inter-Depository Transfer",
    "BSECH": "BSE Clearing House",
    "NSE": "NSE Clearing House",
    "BSE": "BSE Clearing House",
    # Common adjacent codes kept for forward-compatibility; harmless when absent.
    "NSECH": "NSE Clearing House",
    "OFFMKT": "Off-Market Transfer",
    "IPO": "IPO Allotment",
    "CORPACT": "Corporate Action",
    "MARGIN": "Margin Pledge",
    "REL": "Margin Pledge Release",
}

# Direction suffixes the code appends. The CDSL CAS hyphenates them ("PAYOUT-CR")
# while the NSDL CAS sometimes fuses them ("NSEDR" -> NSE + DR); both are peeled
# before label lookup because the credit/debit direction is already carried by
# the signed quantity.
_DIRECTION_SUFFIXES = ("CR", "DR")


def _humanize_token(token: str) -> str:
    """Title-case a raw, unmapped code token for a legible fallback label.

    ``"FOOBAR" -> "Foobar"``. The original raw token is preserved by the caller
    (it stays in the transaction description), so this only affects display.
    """
    return token.title() if token else token


def humanize_transaction_code(code: str | None) -> str | None:
    """Expand a CDSL transaction code into a human-readable label.

    Splits the code into its category prefix and ``CR``/``DR`` direction suffix,
    drops the direction (it is redundant with the signed quantity), and maps the
    prefix through :data:`CODE_LABELS`. An unmapped prefix falls back to a
    title-cased version of the raw prefix so no code ever crashes or is dropped.

    Args:
        code: The raw transaction code (e.g. ``"EP-DR"``, ``"PAYOUT-CR"``), or
            None when the particulars carried no code.

    Returns:
        The readable label (e.g. ``"Early Pay-in"``, ``"Payout"``), or None when
        ``code`` is None/empty.

    Examples:
        >>> humanize_transaction_code("EP-DR")
        'Early Pay-in'
        >>> humanize_transaction_code("PAYOUT-CR")
        'Payout'
        >>> humanize_transaction_code("FOOBAR-CR")  # unknown -> graceful
        'Foobar'
        >>> humanize_transaction_code(None) is None
        True
    """
    if not code:
        return None
    token = code.strip().upper()
    if not token:
        return None

    # Peel a trailing "-CR"/"-DR" direction segment, if present.
    prefix = token
    if "-" in token:
        head, _, tail = token.rpartition("-")
        if tail in _DIRECTION_SUFFIXES and head:
            prefix = head
    else:
        # The NSDL CAS fuses the direction onto the code ("NSEDR" -> NSE + DR).
        # Peel a trailing CR/DR only when a known category prefix remains.
        for suffix in _DIRECTION_SUFFIXES:
            if token.endswith(suffix):
                head = token[: -len(suffix)]
                if head in CODE_LABELS:
                    prefix = head
                break

    label = CODE_LABELS.get(prefix)
    if label is not None:
        return label
    # Unknown category: humanize the raw prefix so the label stays legible while
    # the raw code remains intact in the transaction description.
    return _humanize_token(prefix)


__all__ = ["CODE_LABELS", "humanize_transaction_code"]
