"""CDSL CAS parser tests against a PII-scrubbed synthetic fixture.

The fixture (``tests/fixtures/cdsl_sample.json``) mimics the real CDSL CAS
extractor layout — the same section headers and table shapes — but with wholly
fake investor identity and amounts that still reconcile: each demat account's
holding values sum to its reported total, each MF folio's scheme value is its
total, and all scope totals sum to the summary grand total.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cas_parser.parsers.cdsl_ecas import CdslEcasParser
from cas_parser.parsers.factory import detect_source
from cas_parser.parsers.sections.demat import _parse_holding, parse_demat_accounts
from cas_parser.parsers.sections.mf_folio import parse_mf_folios
from cas_parser.parsers.sections.transactions import parse_transactions

_FIXTURE = Path(__file__).parent / "fixtures" / "cdsl_sample.json"

# The nine-column CDSL holding-table header (English tokens drive column
# resolution; the Hindi glyphs are interleaved in the real PDF but omitted here).
_HOLDING_HEADER = [
    "ISIN",
    "Security",
    "Current",
    "Frozen",
    "Pledge",
    "Pledge Setup",
    "Free Bal",
    "Market Price / Face Value",
    "Value",
]
# The nine-column CDSL transaction-table header (keyed by its "Stamp" column).
_TXN_HEADER = [
    "ISIN",
    "Security",
    "Transaction",
    "Date",
    "Opening Bal",
    "Credit",
    "Debit",
    "Closing Bal",
    "Stamp",
]


@pytest.fixture
def raw() -> dict:
    """Load the synthetic CDSL extractor payload."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_detect_source_is_cdsl(raw: dict) -> None:
    assert detect_source(raw) == "cdsl"


def test_meta_extracted(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    meta = statement.meta
    assert meta.source == "cdsl"
    assert meta.investor_name == "JANE Q EXAMPLE"
    assert meta.pan == "ABCDE1234F"
    assert meta.statement_period_start is not None
    assert meta.statement_period_end is not None
    assert meta.statement_period_start.isoformat() == "2026-04-01"
    assert meta.statement_period_end.isoformat() == "2026-04-30"


def test_generated_on_from_pdf_creation_date(raw: dict) -> None:
    # The CDSL CAS body prints no generation date; it comes from the PDF
    # /CreationDate metadata stamp (D:YYYYMMDD...).
    statement = CdslEcasParser().parse(raw)
    generated = statement.meta.generated_on
    assert generated is not None
    assert generated.isoformat() == "2026-05-09"


def test_accounts_and_holdings(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    assert len(statement.accounts) == 2

    first, second = statement.accounts
    assert first.source_ref == "12345678/11111111"
    assert second.source_ref == "12345678/22222222"
    assert all(account.depository == "CDSL" for account in statement.accounts)
    assert all(account.dp_name for account in statement.accounts)

    assert len(first.holdings) == 2
    assert len(second.holdings) == 4
    assert first.total_value == Decimal("3000.00")
    assert second.total_value == Decimal("13000.00")


def test_holding_name_complete_multiline_and_hash(raw: dict) -> None:
    """A long, multi-line AMC#scheme security name round-trips complete.

    Regression guard for the name-truncation bug: the stored name must keep its
    leading character, join every line of a multi-line table cell, and retain
    the full scheme text past the ``#`` AMC-scheme separator — no ``[:48]``-style
    clipping in the stored model value (display caps live only in the CLI).
    """
    statement = CdslEcasParser().parse(raw)
    by_isin = {
        holding.isin: holding
        for account in statement.accounts
        for holding in account.holdings
    }
    holding = by_isin["INF00ZENITH6"]
    expected = (
        "ZENITH ASSET MANAGEMENT CO LTD#ZENITH MUTUAL FUND- "
        "ZENITH NIFTY MICROCAP 250 INDEX FUND-DIRECT-GROWTH"
    )
    # Full string, leading char intact, far longer than any display cap.
    assert holding.name == expected
    assert holding.name.startswith("ZENITH ASSET")
    assert len(holding.name) > 48
    # The AMC#scheme separator is preserved and the scheme survives past it.
    assert "#" in holding.name
    amc, _, scheme = holding.name.partition("#")
    assert amc == "ZENITH ASSET MANAGEMENT CO LTD"
    assert scheme.endswith("INDEX FUND-DIRECT-GROWTH")

    # No stored security or scheme name is empty or ends mid-token where the raw
    # statement had more (truncation would have cut at a fixed width).
    for account in statement.accounts:
        for held in account.holdings:
            assert held.name
            assert not held.name.endswith("-")
    for folio in statement.folios:
        for sch in folio.schemes:
            assert sch.scheme_name


def test_holding_quantity_is_current_balance(raw: dict) -> None:
    # The second account's first holding has a frozen split (Current 20,
    # Free 15). quantity must be the Current Balance (the total holding) so
    # quantity * price reproduces the printed value; the Free sub-balance would
    # not.
    statement = CdslEcasParser().parse(raw)
    frozen_holding = statement.accounts[1].holdings[0]
    assert frozen_holding.quantity == Decimal("20.000")
    assert frozen_holding.price == Decimal("200.000")
    assert frozen_holding.value == Decimal("4000.00")
    assert frozen_holding.quantity * frozen_holding.price == frozen_holding.value


def test_holding_quantity_price_value_consistent(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    tolerance = Decimal("1.00")
    for account in statement.accounts:
        for holding in account.holdings:
            assert holding.price is not None
            assert holding.value is not None
            product = holding.quantity * holding.price
            assert abs(product - holding.value) <= tolerance


def test_holding_asset_class_inference_and_refinement(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    by_isin = {
        holding.isin: holding
        for account in statement.accounts
        for holding in account.holdings
    }
    # Plain INE equities stay equity.
    assert by_isin["INE000A01012"].asset_class == "equity"
    # An INF instrument whose name says "ETF" refines mutual_fund -> etf.
    etf = by_isin["INF000E01EF4"]
    assert etf.asset_class == "etf"


def test_mf_folios(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    assert len(statement.folios) == 2

    folios = {folio.folio_number: folio for folio in statement.folios}
    assert set(folios) == {"10000001", "10000002"}
    assert folios["10000001"].amc == "Example One Mutual Fund"
    assert folios["10000001"].total_value == Decimal("6000.00")
    assert folios["10000002"].total_value == Decimal("7000.00")
    assert all(len(folio.schemes) == 1 for folio in statement.folios)

    # Bug-2 guard: every folio carries its printed per-folio valuation
    # (Valuation column), and it equals the folio's single scheme value.
    for folio in statement.folios:
        assert folio.total_value is not None
        assert folio.total_value == folio.schemes[0].value


def test_mf_grand_total_valuation_represented(raw: dict) -> None:
    """The MF section's all-folio Grand Total valuation is not dropped.

    The CDSL MF table prints a ``Grand Total`` valuation across folios. It is
    surfaced through the summary's "Mutual Fund Folios" asset-class total and
    must equal the sum of the per-folio valuations.
    """
    statement = CdslEcasParser().parse(raw)
    mf_total = statement.summary.asset_class_totals["Mutual Fund Folios"]
    summed_folios = sum(
        (folio.total_value for folio in statement.folios if folio.total_value),
        Decimal("0"),
    )
    assert mf_total == summed_folios == Decimal("13000.00")


def test_mf_scheme_fields_populated(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    folios = {folio.folio_number: folio for folio in statement.folios}

    scheme_one = folios["10000001"].schemes[0]
    assert scheme_one.isin == "INF000A01AB9"
    assert scheme_one.units == Decimal("100.000")
    assert scheme_one.nav == Decimal("60.0000")
    assert scheme_one.cost == Decimal("5500.00")
    assert scheme_one.value == Decimal("6000.00")
    # units * nav reproduces the printed valuation.
    assert scheme_one.units * scheme_one.nav == scheme_one.value

    scheme_two = folios["10000002"].schemes[0]
    assert scheme_two.cost == Decimal("7500.00")
    assert scheme_two.units * scheme_two.nav == scheme_two.value


def test_transactions_collected_and_stamped(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    # 3 rows for account A (one continuation row) + 3 for account B
    # (PAYOUT-CR plus a BSECH-CR and an unknown FOOBAR-CR continuation row).
    assert len(statement.transactions) == 6

    account_refs = {account.source_ref for account in statement.accounts}
    assert all(txn.source_ref in account_refs for txn in statement.transactions)
    assert all(txn.scope == "demat" for txn in statement.transactions)

    # Credit is positive, debit negative.
    signs = {txn.quantity and txn.quantity.is_signed() for txn in statement.transactions}
    assert True in signs  # at least one debit (negative)


def test_transaction_type_description_reference(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    # Every transaction has the three particulars-derived fields populated.
    assert all(txn.transaction_type for txn in statement.transactions)
    assert all(txn.description for txn in statement.transactions)
    assert all(txn.reference for txn in statement.transactions)

    # transaction_type is now the human-readable label, not the raw code; the
    # redundant -CR/-DR direction is dropped (it lives in the signed quantity).
    types = {txn.transaction_type for txn in statement.transactions}
    assert types == {
        "Payout",
        "Early Pay-in",
        "Inter-Depository Transfer",
        "BSE Clearing House",
        "Foobar",  # unknown code -> graceful title-cased fallback
    }
    # The raw code is preserved as the first token of the description, so the
    # original value round-trips losslessly even after humanizing.
    raw_codes = {txn.description.split()[0] for txn in statement.transactions}
    assert raw_codes == {
        "PAYOUT-CR",
        "EP-DR",
        "INTDEP-CR",
        "BSECH-CR",
        "FOOBAR-CR",
    }

    by_ref = {txn.reference: txn for txn in statement.transactions}
    # An "EP-DR Txn:<n>" debit: readable type, Txn number as reference,
    # full narration (with raw code) as description, negative quantity.
    ep_dr = by_ref["55501234"]
    assert ep_dr.transaction_type == "Early Pay-in"
    assert ep_dr.quantity == Decimal("-2.000")
    assert ep_dr.description.startswith("EP-DR Txn:55501234")
    assert "Txn:55501234" in ep_dr.description
    # A continuation row (blank ISIN/name) inherits the security ISIN of the
    # row above it. In the fixture this EP-DR continuation row is the FIRST row
    # of a new page within the same account block: its security (EXAMPLE ALPHA,
    # INE000A01012) is introduced by the PAYOUT-CR row on the *previous* page.
    # The carried ISIN must therefore survive the page break — a per-page reset
    # would drop it to None.
    assert ep_dr.isin == "INE000A01012"

    # An "INTDEP-CR" credit falls back to the first reference token after the
    # code (no explicit Txn: tag).
    intdep = by_ref["77012345"]
    assert intdep.transaction_type == "Inter-Depository Transfer"
    assert intdep.quantity == Decimal("5.000")

    # A known BSECH-CR maps to its readable label.
    bsech = by_ref["88033445"]
    assert bsech.transaction_type == "BSE Clearing House"
    assert bsech.description.startswith("BSECH-CR")

    # An unknown code degrades gracefully: title-cased prefix, raw code retained.
    foobar = by_ref["99055667"]
    assert foobar.transaction_type == "Foobar"
    assert foobar.description.startswith("FOOBAR-CR")


def test_transaction_isin_carries_across_page_break(raw: dict) -> None:
    # Regression guard for cross-page ISIN carry-forward: the EP-DR continuation
    # row opens a new transaction page while continuing the security from the
    # prior page. The carried ISIN must persist across pages (resetting only at
    # a new BO ID account block), so every parsed demat transaction has an ISIN.
    statement = CdslEcasParser().parse(raw)
    assert all(txn.isin for txn in statement.transactions)
    # The cross-page EP-DR row inherits the alpha-equity ISIN from the page
    # before it, not the beta-equity row that follows it on its own page.
    ep_dr = next(txn for txn in statement.transactions if txn.reference == "55501234")
    assert ep_dr.isin == "INE000A01012"


def test_legend_parsed_into_marker_map(raw: dict) -> None:
    """The footnote legend ("Note:" block) is parsed into a marker -> meaning map.

    The fixture's legend defines ``!!`` (unlisted) and ``##`` (under
    liquidation); both must appear with their exact definition text.
    """
    statement = CdslEcasParser().parse(raw)
    assert statement.legend  # non-empty
    assert statement.legend["!!"] == "Unlisted security"
    assert statement.legend["##"] == "Under Liquidation / Winding Up"
    # The legend never spuriously defines the "#" AMC/scheme separator.
    assert "#" not in statement.legend


def test_holding_flags_and_notes_resolved_from_legend(raw: dict) -> None:
    """A holding whose ISIN carries a legend marker records it and resolves it.

    The ZENITH holding's ISIN cell is ``INF00ZENITH6!!``: the ``!!`` marker must
    land in ``flags`` and resolve, via the parsed legend, into ``notes`` — while
    the clean ISIN and the full multi-line AMC#scheme name survive untouched.
    """
    statement = CdslEcasParser().parse(raw)
    by_isin = {
        holding.isin: holding
        for account in statement.accounts
        for holding in account.holdings
    }
    marked = by_isin["INF00ZENITH6"]
    assert marked.flags == ["!!"]
    assert marked.notes == statement.legend["!!"] == "Unlisted security"

    # Exactly one holding carries a flag in the fixture; the rest carry none.
    flagged = [
        holding
        for account in statement.accounts
        for holding in account.holdings
        if holding.flags
    ]
    assert len(flagged) == 1
    assert flagged[0].isin == "INF00ZENITH6"


def test_marker_strip_preserves_name_and_hash_separator(raw: dict) -> None:
    """Stripping a defined marker must not corrupt the name or the "#" separator.

    The ``#`` AMC/scheme separator is NOT in the legend, so it must remain part
    of the name even though ``!!`` (which IS in the legend) is stripped from the
    ISIN cell. The full multi-line scheme name must round-trip complete.
    """
    statement = CdslEcasParser().parse(raw)
    by_isin = {
        holding.isin: holding
        for account in statement.accounts
        for holding in account.holdings
    }
    marked = by_isin["INF00ZENITH6"]
    # The ISIN itself is clean (no trailing marker).
    assert marked.isin == "INF00ZENITH6"
    assert "!" not in marked.isin
    # The "#" separator (undefined by the legend) survives in the name.
    assert "#" in marked.name
    amc, _, scheme = marked.name.partition("#")
    assert amc == "ZENITH ASSET MANAGEMENT CO LTD"
    assert scheme.endswith("INDEX FUND-DIRECT-GROWTH")
    # No legend marker leaked into the stored name.
    assert "!!" not in marked.name


def test_unknown_transaction_code_humanized_not_crashed() -> None:
    """An unknown CDSL code never crashes; it degrades to a title-cased label."""
    from cas_parser.parsers.utils.transaction_codes import humanize_transaction_code

    assert humanize_transaction_code("EP-DR") == "Early Pay-in"
    assert humanize_transaction_code("PAYOUT-CR") == "Payout"
    assert humanize_transaction_code("BSECH-CR") == "BSE Clearing House"
    assert humanize_transaction_code("FOOBAR-CR") == "Foobar"  # unknown
    assert humanize_transaction_code("MYSTERY") == "Mystery"  # no direction
    assert humanize_transaction_code(None) is None
    assert humanize_transaction_code("") is None


def test_summary_and_reconciliation(raw: dict) -> None:
    statement = CdslEcasParser().parse(raw)
    assert statement.summary.grand_total == Decimal("29000.00")
    assert "Equity" in statement.summary.asset_class_totals
    assert "Mutual Funds Held in Demat Form" in statement.summary.asset_class_totals
    # Asset-class rows sum to the grand total.
    assert sum(
        statement.summary.asset_class_totals.values(), Decimal("0")
    ) == statement.summary.grand_total

    recon = statement.reconciliation
    assert recon is not None
    assert recon.portfolio_ok is True
    assert recon.portfolio_delta == Decimal("0.00")
    assert recon.warnings == []
    assert all(scope.ok for scope in recon.holdings)


# --- Fix 2: two CDSL scheme rows sharing a folio group into one MfFolio ------

# The nine-column CDSL MF holdings-table header (keyed by SCHEME NAME / FOLIO NO).
_MF_HEADER = [
    "Scheme Name",
    "ISIN",
    "Folio No.",
    "Closing Bal (Units)",
    "NAV (`)",
    "Cumulative Amount Invested",
    "Valuation (`)",
    "Unrealised Profit/Loss",
    "Unrealised Profit/Loss(%)",
]


def test_cdsl_two_schemes_same_folio_group_into_one_folio() -> None:
    """Two CDSL MF rows under one folio number become one folio (summed value)."""
    pages = [
        {
            "text": (
                "MF Folios\n"
                "AMC Name : Example One Mutual Fund\n"
                "Folio No : 50000001 Mode of Holding : Single\n"
            ),
            "tables": [
                [
                    _MF_HEADER,
                    [
                        "Scheme One - Direct - Growth",
                        "INF000A01AB9",
                        "50000001",
                        "100.000",
                        "10.0000",
                        "1,000.00",
                        "1,000.00",
                        "0.00",
                        "0.00",
                    ],
                    [
                        "Scheme Two - Direct - Growth",
                        "INF000A01AC7",
                        "50000001",
                        "100.000",
                        "20.0000",
                        "2,000.00",
                        "2,000.00",
                        "0.00",
                        "0.00",
                    ],
                    ["Grand Total", "", "", "", "", "", "3,000.00", "", ""],
                ]
            ],
        }
    ]
    folios = parse_mf_folios(pages)
    assert len(folios) == 1
    folio = folios[0]
    assert folio.folio_number == "50000001"
    assert folio.amc == "Example One Mutual Fund"
    assert len(folio.schemes) == 2
    assert folio.schemes[0].scheme_name == "Scheme One - Direct - Growth"
    assert folio.schemes[1].scheme_name == "Scheme Two - Direct - Growth"
    # total_value is the SUM of both scheme values.
    assert folio.total_value == Decimal("3000.00")


# --- Fix 3: a qty>0 blank-value CDSL holding yields value=None --------------


def _holding_columns() -> dict[str, int]:
    from cas_parser.parsers.sections.demat import _resolve_columns

    return _resolve_columns([cell.upper() for cell in _HOLDING_HEADER])


def test_cdsl_holding_blank_value_with_nonzero_qty_is_none() -> None:
    # A non-zero Current Balance with a blank Value cell is an extraction
    # failure: value stays None rather than being coerced to 0.00.
    row = ["INE000A01012", "EXAMPLE ALPHA", "8.000", "--", "--", "--", "8.000", "125.000", ""]
    holding = _parse_holding(row, _holding_columns(), {})
    assert holding is not None
    assert holding.quantity == Decimal("8.000")
    assert holding.value is None


def test_cdsl_holding_blank_value_with_zero_qty_is_zero() -> None:
    # A genuine zero-balance line (Current ``--``) with a blank value -> 0.
    row = ["INE000A01012", "EXAMPLE ALPHA", "--", "--", "--", "--", "--", "0.000", ""]
    holding = _parse_holding(row, _holding_columns(), {})
    assert holding is not None
    assert holding.quantity == Decimal("0")
    assert holding.value == Decimal("0")


# --- Fix 5: two BO-ID blocks on one page attribute correctly ----------------


def _bo_id(dp_id: str, client_id: str) -> str:
    """Bold-doubled 32-digit BO ID for a <dp_id><client_id> pair (each glyph x2)."""
    raw = dp_id + client_id
    return "".join(ch * 2 for ch in raw)


def test_two_bo_id_blocks_on_one_page_attribute_by_position() -> None:
    """Two account blocks on one page stamp their tables to the nearest BO-ID.

    The page carries two BO-ID headers (account A then account B). Each
    transaction table must be attributed to the BO-ID that precedes it in the
    page text, not unconditionally to the first BO-ID on the page.
    """
    bo_a = _bo_id("12345678", "11111111")  # -> 12345678/11111111
    bo_b = _bo_id("12345678", "22222222")  # -> 12345678/22222222
    # Distinctive per-block tokens used to locate each table in the page text.
    text = (
        f"DP Name : ALPHA BO ID : {bo_a}\n"
        "STATEMENT OF TRANSACTIONS\n"
        "INE000A01012 02-04-2026 10.000\n"  # table A content (above bo_b)
        f"DP Name : BETA BO ID : {bo_b}\n"
        "STATEMENT OF TRANSACTIONS\n"
        "INE000B01028 03-04-2026 7.000\n"  # table B content (below bo_b)
    )
    table_a = [
        _TXN_HEADER,
        [
            "INE000A01012",
            "EXAMPLE ALPHA",
            "PAYOUT-CR CM M1 SETT 111",
            "02-04-2026",
            "0.000",
            "10.000",
            "--",
            "10.000",
            "0",
        ],
    ]
    table_b = [
        _TXN_HEADER,
        [
            "INE000B01028",
            "EXAMPLE BETA",
            "EP-DR Txn:222",
            "03-04-2026",
            "10.000",
            "--",
            "7.000",
            "3.000",
            "0",
        ],
    ]
    pages = [{"text": text, "tables": [table_a, table_b]}]
    txns = parse_transactions(pages)
    assert len(txns) == 2
    by_ref = {t.source_ref: t for t in txns}
    # Table A -> account A (the first BO-ID); table B -> account B (the second).
    assert "12345678/11111111" in by_ref
    assert "12345678/22222222" in by_ref
    assert by_ref["12345678/11111111"].isin == "INE000A01012"
    assert by_ref["12345678/22222222"].isin == "INE000B01028"
    # Sign: A is a credit (+), B is a debit (-).
    assert by_ref["12345678/11111111"].quantity == Decimal("10.000")
    assert by_ref["12345678/22222222"].quantity == Decimal("-7.000")


# --- Fix 8: marker/header count mismatch surfaces a warning -----------------


def test_holding_attribution_mismatch_appends_warning() -> None:
    """Fewer ``Portfolio Value`` markers than account headers -> a warning.

    Two account headers but only one holding block (one Portfolio Value marker)
    means the order-based attribution is misaligned; the guard records it.
    """
    pages = [
        {
            "text": (
                "Account Details\n"
                "DP Name : ALPHA DP ID : 12345678 CLIENT ID : 11111111\n"
                "DP Name : BETA DP ID : 12345678 CLIENT ID : 22222222\n"
            ),
            "tables": [],
        },
        {
            "text": "HOLDING STATEMENT\n",
            "tables": [
                [
                    _HOLDING_HEADER,
                    [
                        "INE000A01012",
                        "EXAMPLE ALPHA",
                        "8.000",
                        "--",
                        "--",
                        "--",
                        "8.000",
                        "125.000",
                        "1,000.00",
                    ],
                    # Only ONE Portfolio Value marker for TWO account headers.
                    ["Portfolio Value ` 1,000.00 as on 30-04-2026"] + [""] * 8,
                ]
            ],
        },
    ]
    warnings: list[str] = []
    accounts = parse_demat_accounts(pages, {}, warnings)
    assert len(accounts) == 2
    assert warnings  # the mismatch is surfaced
    assert any("attribution" in w.lower() for w in warnings)


def test_holding_attribution_no_warning_when_counts_match() -> None:
    # Matching marker/header counts (one each) -> no warning.
    pages = [
        {
            "text": (
                "Account Details\n"
                "DP Name : ALPHA DP ID : 12345678 CLIENT ID : 11111111\n"
            ),
            "tables": [],
        },
        {
            "text": "HOLDING STATEMENT\n",
            "tables": [
                [
                    _HOLDING_HEADER,
                    [
                        "INE000A01012",
                        "EXAMPLE ALPHA",
                        "8.000",
                        "--",
                        "--",
                        "--",
                        "8.000",
                        "125.000",
                        "1,000.00",
                    ],
                    ["Portfolio Value ` 1,000.00 as on 30-04-2026"] + [""] * 8,
                ]
            ],
        },
    ]
    warnings: list[str] = []
    accounts = parse_demat_accounts(pages, {}, warnings)
    assert len(accounts) == 1
    assert warnings == []
