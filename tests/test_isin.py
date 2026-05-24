"""ISIN validation and asset-class inference tests."""

from __future__ import annotations

from cas_parser.parsers.utils.isin import find_isin, infer_asset_class, is_valid_isin


def test_valid_isin_check_digit() -> None:
    # US0378331005 is a textbook-valid ISIN (correct Luhn check digit).
    assert is_valid_isin("US0378331005") is True
    # Same body, wrong check digit.
    assert is_valid_isin("US0378331004") is False


def test_invalid_isin_format() -> None:
    assert is_valid_isin("ABC") is False
    assert is_valid_isin("IN12345") is False
    assert is_valid_isin("") is False


def test_infer_asset_class_from_prefix() -> None:
    # Synthetic ISIN-shaped strings; infer_asset_class keys only on the 3rd char.
    assert infer_asset_class("INF000000001") == "mutual_fund"
    assert infer_asset_class("INE000000001") == "equity"
    assert infer_asset_class("IN0000000001") == "government_security"
    assert infer_asset_class("IN9000000001") == "other"
    assert infer_asset_class(None) == "other"
    assert infer_asset_class("US0000000001") == "other"


def test_find_isin_in_text() -> None:
    text = "Holding US0378331005 quantity 10"
    assert find_isin(text) == "US0378331005"
    assert find_isin("no isin here") is None
