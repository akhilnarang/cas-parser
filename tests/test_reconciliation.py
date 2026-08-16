"""Holdings reconciliation tests."""

from __future__ import annotations

from decimal import Decimal

from cas_parser.models import (
    CasSummary,
    DematAccount,
    DematHolding,
    MfFolio,
    MfScheme,
)
from cas_parser.parsers.reconciliation import (
    build_reconciliation,
    reconcile_account,
    reconcile_folio,
)


def _account(total: str) -> DematAccount:
    return DematAccount(
        depository="NSDL",
        dp_id="IN300000",
        client_id="12345678",
        holdings=[
            DematHolding(name="A", quantity=Decimal(1), value=Decimal("100.00")),
            DematHolding(name="B", quantity=Decimal(1), value=Decimal("200.00")),
        ],
        total_value=Decimal(total),
    )


def test_account_reconciles_when_total_matches() -> None:
    result = reconcile_account(_account("300.00"))
    assert result.computed_total == Decimal("300.00")
    assert result.delta == Decimal("0.00")
    assert result.ok is True


def test_account_flags_mismatch_beyond_tolerance() -> None:
    result = reconcile_account(_account("350.00"))
    assert result.delta == Decimal("50.00")
    assert result.ok is False


def test_missing_holding_value_blocks_ok() -> None:
    # A holding with no parsed value is unknown, not zero — even if the partial
    # sum happens to match the reported total, the scope must not reconcile.
    account = DematAccount(
        depository="NSDL",
        dp_id="IN300000",
        client_id="12345678",
        holdings=[
            DematHolding(name="A", quantity=Decimal(1), value=Decimal("100.00")),
            DematHolding(name="B", quantity=Decimal(1)),  # no value
        ],
        total_value=Decimal("100.00"),
    )
    result = reconcile_account(account)
    assert result.incomplete is True
    assert result.ok is False


def test_folio_reconciles_against_reported_total() -> None:
    folio = MfFolio(
        folio_number="9999999999",
        schemes=[
            MfScheme(scheme_name="S", units=Decimal(10), value=Decimal("500.00")),
        ],
        total_value=Decimal("500.00"),
    )
    result = reconcile_folio(folio)
    assert result.delta == Decimal("0.00")
    assert result.ok is True


def test_portfolio_reconciliation() -> None:
    summary = CasSummary(grand_total=Decimal("300.00"))
    recon = build_reconciliation([_account("300.00")], [], summary)
    assert recon.portfolio_ok is True
    assert recon.portfolio_delta == Decimal("0.00")

    bad = build_reconciliation([_account("300.00")], [], CasSummary(grand_total=Decimal("999.00")))
    assert bad.portfolio_ok is False
    assert bad.warnings  # at least the portfolio mismatch is recorded


def _account_ref(ref_client: str, total: str) -> DematAccount:
    """A two-holding account (sum 300.00) with a per-client-id source ref."""
    return DematAccount(
        depository="NSDL",
        dp_id="IN300000",
        client_id=ref_client,
        holdings=[
            DematHolding(name="A", quantity=Decimal(1), value=Decimal("100.00")),
            DematHolding(name="B", quantity=Decimal(1), value=Decimal("200.00")),
        ],
        total_value=Decimal(total),
    )


def test_offsetting_scope_errors_do_not_green_the_portfolio() -> None:
    # Fix 1 regression: two scopes whose per-scope deltas cancel out
    # (+50 and -50) must NOT yield a green portfolio even though the grand-total
    # delta is zero. portfolio_ok requires *every* scope to reconcile.
    over = _account_ref("11111111", "350.00")  # reported 350, computed 300 (+50)
    under = _account_ref("22222222", "250.00")  # reported 250, computed 300 (-50)
    # Grand total 600 == sum of computed (300 + 300), so the *portfolio* delta is 0.
    summary = CasSummary(grand_total=Decimal("600.00"))
    recon = build_reconciliation([over, under], [], summary)

    assert recon.portfolio_delta == Decimal("0.00")  # totals net out
    assert not all(h.ok for h in recon.holdings)  # but per-scope they don't
    assert recon.portfolio_ok is False  # so the portfolio must not be green
    # Each off-by scope is reported.
    assert any("11111111" in w for w in recon.warnings)
    assert any("22222222" in w for w in recon.warnings)


def test_portfolio_not_ok_without_grand_total_even_if_scopes_ok() -> None:
    # Fix 1: portfolio_ok also requires a grand_total to be present. With every
    # scope reconciling but no summary grand_total, there is nothing to assert
    # the portfolio against, so it must stay False.
    recon = build_reconciliation([_account("300.00")], [], CasSummary())
    assert all(h.ok for h in recon.holdings)
    assert recon.portfolio_ok is False
    assert any("grand_total" in w for w in recon.warnings)


def test_portfolio_ok_requires_all_scopes_ok_and_grand_total() -> None:
    # The happy path still works: all scopes ok + matching grand total -> green.
    a = _account_ref("11111111", "300.00")
    b = _account_ref("22222222", "300.00")
    recon = build_reconciliation([a, b], [], CasSummary(grand_total=Decimal("600.00")))
    assert all(h.ok for h in recon.holdings)
    assert recon.portfolio_ok is True
    assert recon.warnings == []
