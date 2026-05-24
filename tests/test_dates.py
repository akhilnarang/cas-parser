"""Date parsing helper tests."""

from __future__ import annotations

from datetime import date

from cas_parser.parsers.utils.dates import format_date, parse_date


def test_parse_full_dates() -> None:
    assert parse_date("01/04/2026") == date(2026, 4, 1)
    assert parse_date("01-Apr-2026") == date(2026, 4, 1)
    assert parse_date("1 Apr 2026") == date(2026, 4, 1)


def test_reject_partial_or_garbage_tokens() -> None:
    # dateutil would fabricate the missing day/month from a default — these must
    # be rejected rather than silently completed.
    assert parse_date("Mar 2024") is None
    assert parse_date("2024") is None
    assert parse_date("") is None
    assert parse_date("not a date") is None


def test_format_date() -> None:
    assert format_date(date(2026, 4, 1)) == "01/04/2026"
