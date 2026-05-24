"""Numeric parsing helper tests."""

from __future__ import annotations

from decimal import Decimal

from cas_parser.parsers.utils.amounts import (
    extract_decimal,
    format_decimal,
    parse_decimal,
)


def test_parse_decimal_indian_grouping() -> None:
    assert parse_decimal("1,52,581.54") == Decimal("1,52,581.54".replace(",", ""))
    assert parse_decimal("1,52,581.54") == Decimal("152581.54")


def test_parse_decimal_units_and_symbols() -> None:
    assert parse_decimal("123.456") == Decimal("123.456")
    assert parse_decimal("₹ 1,000.00") == Decimal("1000.00")
    assert parse_decimal("500.00 Cr") == Decimal("500.00")
    assert parse_decimal("not a number") is None
    assert parse_decimal("") is None


def test_extract_decimal_from_token() -> None:
    assert extract_decimal("Value: 1,005.00 only") == Decimal("1005.00")
    assert extract_decimal("nope") is None


def test_format_decimal() -> None:
    assert format_decimal(Decimal("152581.54")) == "152,581.54"
    assert format_decimal(Decimal("123.456"), places=3) == "123.456"
