"""Date parsing helpers backed by python-dateutil.

CAS PDFs use several date formats (DD-MMM-YYYY, DD/MM/YYYY, DD Mon YYYY). These
helpers return `datetime.date` objects — the schema stores real dates, not
strings — and `format_date` renders DD/MM/YYYY for display.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from dateutil import parser as date_parser

_DEFAULT_FORMAT_HINTS = (
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%m-%Y",
    "%d-%m-%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
    "%d %b %y",
    "%d %B %Y",
    "%B %d, %Y",
)


def _normalize_token(token: str) -> str:
    normalized = token.strip().replace("’", "'").replace("`", "")
    return re.sub(r"\s+", " ", normalized)


def format_date(value: date) -> str:
    """Format a date as DD/MM/YYYY."""
    return value.strftime("%d/%m/%Y")


def parse_date(
    token: str,
    dayfirst: bool = True,
    format_hints: list[str] | None = None,
) -> date | None:
    """Parse a date token using explicit hints first, then dateutil.

    Args:
        token: Raw date text.
        dayfirst: Interpret ambiguous numeric dates as day-first (Indian).
        format_hints: Extra `strptime` formats to try before the defaults.

    Returns:
        Parsed `date`, or None when the token is not a recognizable date.
    """
    normalized = _normalize_token(token)
    if not normalized:
        return None

    for hint in [*(format_hints or []), *_DEFAULT_FORMAT_HINTS]:
        try:
            return datetime.strptime(normalized, hint).date()  # noqa: DTZ007 — parses a date-only token; time and tz are dropped by .date()
        except ValueError:
            continue

    return _parse_with_dateutil(normalized, dayfirst)


def _parse_with_dateutil(normalized: str, dayfirst: bool) -> date | None:
    """Fallback parse that rejects partial tokens.

    A CAS date must be complete. dateutil fills missing components from a
    default date, so a partial token like "Mar 2024" would yield a fabricated
    day. Parsing against two different defaults and requiring identical results
    rejects any token that did not specify all three components itself.
    """
    try:
        # The default anchors only exist to detect partial tokens; only .date()
        # is compared, so a tzinfo would have no effect.
        first = date_parser.parse(
            normalized, dayfirst=dayfirst, fuzzy=False, default=datetime(2000, 1, 1)  # noqa: DTZ001
        )
        second = date_parser.parse(
            normalized, dayfirst=dayfirst, fuzzy=False, default=datetime(2001, 6, 15)  # noqa: DTZ001
        )
    except (ValueError, OverflowError, TypeError):
        return None

    return first.date() if first.date() == second.date() else None


__all__ = [
    "format_date",
    "parse_date",
]
