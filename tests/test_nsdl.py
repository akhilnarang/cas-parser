"""NSDL CAS parser tests against a PII-scrubbed synthetic fixture.

The fixture (``tests/fixtures/nsdl_sample.json``) mimics the real NSDL CAS
extractor layout — the same front-matter account-summary table, per-group
PORTFOLIO COMPOSITION tables, the NSDL six-column equity holding table, the
embedded CDSL seven-column balance table, the ten-column MF folio table, and
both transaction layouts (NSDL eight-column and CDSL five-column) — but with
wholly fake investor identity and amounts that still reconcile: each demat
account's holding values sum to its reported total, each MF folio's scheme value
is its total, and all scope totals sum to the summary grand total.

It also exercises the two NSDL-specific gotchas: a ``NOT LISTED`` legend marker
on an unlisted holding, and a CDSL transaction whose ISIN anchor and continuation
movement straddle a page break (cross-page ISIN carry-forward).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cas_parser.parsers.factory import detect_source
from cas_parser.parsers.nsdl_cas import NsdlCasParser
from cas_parser.parsers.sections.nsdl_demat import (
    _parse_cdsl_holding,
    _parse_nsdl_holding,
)
from cas_parser.parsers.sections.nsdl_mf_folio import _build_amc_map, parse_mf_folios
from cas_parser.parsers.sections.nsdl_transactions import parse_transactions

_FIXTURE = Path(__file__).parent / "fixtures" / "nsdl_sample.json"


@pytest.fixture
def raw() -> dict:
    """Load the synthetic NSDL extractor payload."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_detect_source_is_nsdl(raw: dict) -> None:
    assert detect_source(raw) == "nsdl"


def test_meta_extracted(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    meta = statement.meta
    assert meta.source == "nsdl"
    assert meta.investor_name == "JANE Q EXAMPLE"
    assert meta.pan == "ABCDE1234F"
    assert meta.statement_period_start is not None
    assert meta.statement_period_end is not None
    assert meta.statement_period_start.isoformat() == "2026-03-01"
    assert meta.statement_period_end.isoformat() == "2026-03-31"


def test_generated_on_from_pdf_creation_date(raw: dict) -> None:
    # The NSDL CAS body prints no generation date; it comes from the PDF
    # /CreationDate metadata stamp (D:YYYYMMDD...).
    statement = NsdlCasParser().parse(raw)
    generated = statement.meta.generated_on
    assert generated is not None
    assert generated.isoformat() == "2026-04-17"


def test_accounts_split_by_depository(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    # Joint group: one NSDL + one CDSL account; single group: one NSDL account.
    assert len(statement.accounts) == 3

    by_ref = {account.source_ref: account for account in statement.accounts}
    # DP-ID prefix drives the depository tag inside the consolidated CAS.
    assert by_ref["IN300000/10000001"].depository == "NSDL"
    assert by_ref["IN300001/10000002"].depository == "NSDL"
    assert by_ref["12000000/20000001"].depository == "CDSL"

    nsdl = [a for a in statement.accounts if a.depository == "NSDL"]
    cdsl = [a for a in statement.accounts if a.depository == "CDSL"]
    assert len(nsdl) == 2
    assert len(cdsl) == 1
    assert all(account.dp_name for account in statement.accounts)


def test_nsdl_holdings_quantity_price_value(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    by_ref = {account.source_ref: account for account in statement.accounts}
    nsdl = by_ref["IN300000/10000001"]
    assert len(nsdl.holdings) == 2
    assert nsdl.total_value == Decimal("3000.00")

    by_isin = {holding.isin: holding for holding in nsdl.holdings}
    alpha = by_isin["INE000A01012"]
    assert alpha.name == "EXAMPLE ALPHA LIMITED"
    assert alpha.quantity == Decimal(10)
    assert alpha.price == Decimal("100.00")
    assert alpha.value == Decimal("1,000.00".replace(",", ""))
    assert alpha.quantity * alpha.price == alpha.value
    assert alpha.asset_class == "equity"


def test_cdsl_holding_quantity_is_current_balance(raw: dict) -> None:
    # The embedded CDSL holding has Current Bal 20 (Free 15, Pledged 5). The
    # quantity must be the Current Balance (the total holding), so quantity *
    # price reproduces the printed value even with a pledged split.
    statement = NsdlCasParser().parse(raw)
    by_ref = {account.source_ref: account for account in statement.accounts}
    cdsl = by_ref["12000000/20000001"]
    assert len(cdsl.holdings) == 1
    gamma = cdsl.holdings[0]
    assert gamma.quantity == Decimal("20.000")
    assert gamma.price == Decimal("100.00")
    assert gamma.value == Decimal("2000.00")
    assert gamma.quantity * gamma.price == gamma.value
    # The multi-line "#"-separated security name round-trips complete.
    assert gamma.name.startswith("EXAMPLE GAMMA")
    assert "#" in gamma.name
    assert gamma.name.endswith("AFTER SUB DIVISION")


def test_all_holdings_consistent(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    tolerance = Decimal("1.00")
    holdings = [h for a in statement.accounts for h in a.holdings]
    assert len(holdings) == 4
    for holding in holdings:
        assert holding.isin
        assert holding.name
        assert holding.price is not None
        assert holding.value is not None
        assert abs(holding.quantity * holding.price - holding.value) <= tolerance


def test_not_listed_legend_marker_and_flag(raw: dict) -> None:
    """The unlisted holding carries a NOT LISTED flag resolved from the legend.

    The NSDL legend is built from the PORTFOLIO COMPOSITION asset-class codes
    plus the inline ``NOT LISTED`` marker; the unlisted holding records the
    marker and resolves its meaning into ``notes``.
    """
    statement = NsdlCasParser().parse(raw)
    assert "NOT LISTED" in statement.legend
    # Asset-class codes are parsed dynamically from the composition table.
    assert statement.legend["E"].startswith("Asset class")
    assert "SGB" in statement.legend

    holdings = [h for a in statement.accounts for h in a.holdings]
    by_isin = {h.isin: h for h in holdings}
    unlisted = by_isin["INE000B01010"]
    assert unlisted.flags == ["NOT LISTED"]
    assert unlisted.notes == statement.legend["NOT LISTED"]

    flagged = [h for h in holdings if h.flags]
    assert len(flagged) == 1
    assert flagged[0].isin == "INE000B01010"


def test_mf_folios(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    assert len(statement.folios) == 2

    folios = {folio.folio_number: folio for folio in statement.folios}
    assert set(folios) == {"70000001", "70000002"}
    assert folios["70000001"].amc == "Example One Mutual Fund"
    assert folios["70000002"].amc == "Example Two Mutual Fund"

    for folio in statement.folios:
        assert folio.total_value is not None
        assert len(folio.schemes) == 1
        scheme = folio.schemes[0]
        assert scheme.isin
        assert scheme.units is not None
        assert scheme.nav is not None
        assert scheme.cost is not None
        assert folio.total_value == scheme.value
        # units * nav reproduces the printed valuation.
        assert scheme.units * scheme.nav == scheme.value

    assert folios["70000001"].total_value == Decimal("4000.00")
    assert folios["70000002"].total_value == Decimal("6000.00")


def test_transactions_collected_and_stamped(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    # 1 NSDL receipt + 2 CDSL movements (PAYOUT-CR on page 6, NSEDR on page 7).
    assert len(statement.transactions) == 3

    account_refs = {account.source_ref for account in statement.accounts}
    assert all(txn.source_ref in account_refs for txn in statement.transactions)
    assert all(txn.scope == "demat" for txn in statement.transactions)
    assert all(txn.date for txn in statement.transactions)
    assert all(txn.isin for txn in statement.transactions)
    assert all(txn.transaction_type for txn in statement.transactions)


def test_transaction_types_humanized(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    types = {txn.transaction_type for txn in statement.transactions}
    # NSDL narration -> "Receipt"; CDSL terse codes -> humanized labels. The
    # fused "NSEDR" (NSE + DR direction) resolves to the NSE clearing label.
    assert types == {"Receipt", "Payout", "NSE Clearing House"}


def test_transaction_isin_carries_across_page_break(raw: dict) -> None:
    # Regression guard for cross-page ISIN carry-forward: the GAMMA security's
    # ISIN anchor (and its PAYOUT-CR movement) sit on the last transaction page,
    # while its NSEDR continuation movement opens the next page in a header-less
    # table within the same CDSL account block. The carried ISIN must survive the
    # page break (resetting only at a new account header), so every CDSL movement
    # inherits the GAMMA ISIN.
    statement = NsdlCasParser().parse(raw)
    gamma_txns = [
        txn for txn in statement.transactions if txn.isin == "INE000C01018"
    ]
    assert len(gamma_txns) == 2
    nsedr = next(
        txn for txn in gamma_txns if txn.transaction_type == "NSE Clearing House"
    )
    assert nsedr.isin == "INE000C01018"
    # NSEDR is a debit (DR direction fused onto the code) -> negative quantity.
    assert nsedr.quantity == Decimal("-5.000")
    # The PAYOUT-CR credit is positive (a receipt of units).
    payout = next(txn for txn in gamma_txns if txn.transaction_type == "Payout")
    assert payout.quantity == Decimal("5.000")


def test_summary_and_reconciliation(raw: dict) -> None:
    statement = NsdlCasParser().parse(raw)
    assert statement.summary.grand_total == Decimal("20000.00")
    # Composition asset-class rows are aggregated across both holder groups.
    assert statement.summary.asset_class_totals["Equities"] == Decimal("10000.00")
    assert statement.summary.asset_class_totals["Mutual Fund Folios"] == Decimal(
        "10000.00"
    )
    assert sum(
        statement.summary.asset_class_totals.values(), Decimal(0)
    ) == statement.summary.grand_total

    recon = statement.reconciliation
    assert recon is not None
    assert recon.portfolio_ok is True
    assert recon.portfolio_delta == Decimal("0.00")
    assert recon.warnings == []
    assert all(scope.ok for scope in recon.holdings)


# --- Fix 2: two scheme rows sharing a folio group into one MfFolio -----------


def test_two_schemes_same_folio_group_into_one_folio() -> None:
    """Two MF scheme rows under one folio number become one folio (summed value).

    The NSDL ten-column MF table prints one row per scheme; rows sharing a folio
    number must accumulate into a single ``MfFolio`` whose ``total_value`` is the
    sum of the schemes' ``Current Value`` (here 1000 + 2000 = 3000).
    """
    header = [
        "ISIN\nUCC",
        "ISIN Description",
        "Folio No.",
        "No. of\nUnits",
        "Average\nCost Per Units\n`",
        "Total Cost\n`",
        "Current NAV\nper unit\nin `",
        "Current Value\nin `",
        "Unrealised\nProfit/(Loss)\n`",
        "Annualised\nReturn(%)",
    ]
    pages = [
        {
            "text": "",
            "tables": [
                [
                    ["Mutual Fund Folios (F)"] + [None] * 9,
                    header,
                    [
                        "INF000A01AB9\nUCC0000001",
                        "Scheme One Growth",
                        "55500001",
                        "100.000",
                        "10.0000",
                        "1,000.00",
                        "10.0000",
                        "1,000.00",
                        "0.00",
                        "0.00",
                    ],
                    [
                        "INF000A01AC7\nUCC0000002",
                        "Scheme Two Growth",
                        "55500001",
                        "100.000",
                        "20.0000",
                        "2,000.00",
                        "20.0000",
                        "2,000.00",
                        "0.00",
                        "0.00",
                    ],
                    ["Sub Total"] + [None] * 4 + ["3,000.00"] + [None] * 4,
                ]
            ],
        }
    ]
    folios = parse_mf_folios(pages)
    assert len(folios) == 1
    folio = folios[0]
    assert folio.folio_number == "55500001"
    assert len(folio.schemes) == 2
    # Order preserved.
    assert folio.schemes[0].scheme_name == "Scheme One Growth"
    assert folio.schemes[1].scheme_name == "Scheme Two Growth"
    # total_value is the SUM of the two scheme values, not a single row's value.
    assert folio.total_value == Decimal("3000.00")
    assert folio.total_value == sum(
        (s.value for s in folio.schemes), Decimal(0)
    )


# --- Fix 3: a qty>0 blank-value holding yields value=None -------------------


def test_nsdl_holding_blank_value_with_nonzero_qty_is_none() -> None:
    # An NSDL six-column row with a non-zero quantity but a blank Value cell is an
    # extraction failure: value must stay None (not coerced to 0.00).
    row = ["INE000A01012\nEXAMPLA.NSE", "EXAMPLE ALPHA LIMITED", "1.00", "10", "100.00", ""]
    holding = _parse_nsdl_holding(row, {})
    assert holding is not None
    assert holding.quantity == Decimal(10)
    assert holding.value is None


def test_nsdl_holding_blank_value_with_zero_qty_is_zero() -> None:
    # A genuine zero-balance line (qty 0) with a blank value resolves to 0.
    row = ["INE000A01012\nEXAMPLA.NSE", "EXAMPLE ALPHA LIMITED", "1.00", "0", "100.00", ""]
    holding = _parse_nsdl_holding(row, {})
    assert holding is not None
    assert holding.quantity == Decimal(0)
    assert holding.value == Decimal(0)


def test_cdsl_embedded_holding_blank_value_with_nonzero_qty_is_none() -> None:
    # Same rule for the embedded CDSL seven-column balance row.
    row = [
        "INE000C01018",
        "EXAMPLE GAMMA LIMITED",
        "20.000\n15.000\n0.000",
        "0.000\n0.000\n0.000",
        "5.000\n0.000\n0.000",
        "100.00",
        "",
    ]
    holding = _parse_cdsl_holding(row, None, {})
    assert holding is not None
    assert holding.quantity == Decimal("20.000")
    assert holding.value is None


def test_nsdl_qty_pos_blank_value_makes_scope_incomplete() -> None:
    """A qty>0 blank-value holding leaves the scope incomplete / not reconciled.

    With the missing value left as None, reconciliation marks the account
    incomplete and the portfolio cannot be green.
    """
    from cas_parser.models import CasSummary, DematAccount
    from cas_parser.parsers.reconciliation import build_reconciliation

    row = ["INE000A01012\nEXAMPLA.NSE", "EXAMPLE ALPHA LIMITED", "1.00", "10", "100.00", ""]
    holding = _parse_nsdl_holding(row, {})
    assert holding is not None
    account = DematAccount(
        depository="NSDL",
        dp_id="IN300000",
        client_id="10000001",
        holdings=[holding],
        total_value=Decimal("1000.00"),
    )
    recon = build_reconciliation(
        [account], [], CasSummary(grand_total=Decimal("1000.00"))
    )
    assert recon.holdings[0].incomplete is True
    assert recon.holdings[0].ok is False
    assert recon.portfolio_ok is False


# --- Fix 4: a spacer-column NSDL (CDSL-shape) transaction keeps the sign -----


def test_spacer_column_cdsl_transaction_keeps_debit_sign() -> None:
    """A CDSL-shape txn header with a spacer column resolves Credit/Debit by name.

    The header has a spacer between Particulars and Credit (Credit at index 3,
    Debit at index 4). A debit row of the same width must come out negative; a
    fixed ``credit=cells[2]`` / ``debit=cells[3]`` would invert the sign.
    """
    pages = [
        {
            "text": "",
            "tables": [
                [
                    [
                        (
                            "CDSL Demat Account\nEXAMPLE CDSL DP\n"
                            "DP ID: 12000000 Client ID: 20000001"
                        ),
                        None,
                        "Summary of Transactions",
                        None,
                        None,
                        None,
                    ],
                    # Header carries a spacer at index 2 (Credit -> 3, Debit -> 4).
                    ["Date", "Transaction Particulars", "", "Credit", "Debit", "Current\nBalance"],
                    [
                        "ISIN : INE000C01018 - EXAMPLE GAMMA LIMITED",
                        None,
                        None,
                        None,
                        None,
                        None,
                    ],
                    # A same-width (6-col) debit row: Debit value sits at index 4.
                    ["10-Mar-2026", "NSEDR SE:123 CM:M1 456", "", "", "5.000", "10.000"],
                    # A same-width (6-col) credit row: Credit value sits at index 3.
                    ["12-Mar-2026", "PAYOUT-CR CM M1 789", "", "7.000", "", "17.000"],
                ]
            ],
        }
    ]
    txns = parse_transactions(pages)
    assert len(txns) == 2
    by_type = {t.transaction_type: t for t in txns}
    # Debit -> negative; Credit -> positive.
    assert by_type["NSE Clearing House"].quantity == Decimal("-5.000")
    assert by_type["Payout"].quantity == Decimal("7.000")
    # All attributed to the CDSL account and inherit the anchor ISIN.
    assert all(t.source_ref == "12000000/20000001" for t in txns)
    assert all(t.isin == "INE000C01018" for t in txns)


# --- Fix 7: an alphanumeric / slash folio still gets its AMC -----------------


def test_alphanumeric_folio_gets_amc() -> None:
    """A non-numeric folio (alphanumeric / slash) is still AMC-enriched.

    The KYC folio table's first cell is ``<folio>\\n<AMC>``; dropping the
    pure-digit restriction means ``ABC12345/67`` is recognised as a folio and
    mapped to its AMC, while prose label lines are still skipped.
    """
    pages = [
        {
            "text": "",
            "tables": [
                [
                    ["Folio No.\nAMC NAME", "HOLDER DETAILS"],
                    ["ABC12345/67\nExample Alpha Mutual Fund", "Sole Holder"],
                    ["7654321\nExample Beta Mutual Fund", "Sole Holder"],
                ]
            ],
        }
    ]
    amc = _build_amc_map(pages)
    assert amc["ABC12345/67"] == "Example Alpha Mutual Fund"
    assert amc["7654321"] == "Example Beta Mutual Fund"
    # The header label line ("Folio No.") is not treated as a folio.
    assert "Folio No." not in amc
