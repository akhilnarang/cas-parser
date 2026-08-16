"""Holdings reconciliation — the primary correctness signal for a parsed CAS.

A correct parse should reproduce the totals the statement itself prints. We
verify two things wherever the data is available:

1. Per scope (each demat account, each MF folio): the sum of holding values
   equals the reported total for that scope.
2. Portfolio: the sum of all scope totals equals ``summary.grand_total``.

Transaction/unit reconciliation (opening +/- movements == closing) is *not*
done here because depository CAS PDFs do not reliably print opening balances;
parsers may add it as a soft warning when the data exists.
"""

from __future__ import annotations

from decimal import Decimal

from cas_parser.models import (
    CasReconciliation,
    CasSummary,
    DematAccount,
    HoldingReconciliation,
    MfFolio,
)

# Rupee-level tolerance: per-line values are rounded in the PDF, so summed
# holdings can differ from a printed total by a few paise/rupees.
DEFAULT_TOLERANCE = Decimal("1.00")


def _reconcile_values(
    scope: str,
    values: list[Decimal | None],
    reported: Decimal | None,
    tolerance: Decimal,
) -> HoldingReconciliation:
    """Reconcile a list of line values against a reported total.

    A missing line value (``None``) means *unknown*, not zero, so any missing
    value marks the scope ``incomplete`` and prevents an ``ok`` result — summing
    `None` as zero would otherwise fabricate a passing reconciliation.
    """
    incomplete = any(value is None for value in values)
    computed = sum((value for value in values if value is not None), Decimal(0))
    delta = None if reported is None else reported - computed
    ok = not incomplete and delta is not None and abs(delta) <= tolerance
    return HoldingReconciliation(
        scope=scope,
        reported_total=reported,
        computed_total=computed,
        delta=delta,
        incomplete=incomplete,
        ok=ok,
    )


def reconcile_account(
    account: DematAccount,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> HoldingReconciliation:
    """Reconcile one demat account's holdings against its reported total."""
    return _reconcile_values(
        account.source_ref,
        [holding.value for holding in account.holdings],
        account.total_value,
        tolerance,
    )


def reconcile_folio(
    folio: MfFolio,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> HoldingReconciliation:
    """Reconcile one MF folio's scheme values against its reported total.

    When the statement does not print a folio-level total (``total_value`` is
    None) the scope cannot be verified and ``ok`` stays False.
    """
    return _reconcile_values(
        folio.folio_number,
        [scheme.value for scheme in folio.schemes],
        folio.total_value,
        tolerance,
    )


def build_reconciliation(
    accounts: list[DematAccount],
    folios: list[MfFolio],
    summary: CasSummary,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> CasReconciliation:
    """Build the aggregate reconciliation for a parsed statement.

    Args:
        accounts: Parsed demat accounts.
        folios: Parsed MF folios.
        summary: Parsed portfolio summary (source of ``grand_total``).
        tolerance: Absolute rupee tolerance for a scope to count as reconciled.

    Returns:
        A populated ``CasReconciliation``. ``portfolio_ok`` is True only when
        every scope reconciles, a ``grand_total`` is present, and the summed
        scope totals match it within ``tolerance`` — so offsetting per-scope
        errors cannot net out into a false green portfolio.
    """
    holdings = [reconcile_account(account, tolerance) for account in accounts]
    holdings.extend(reconcile_folio(folio, tolerance) for folio in folios)

    warnings: list[str] = []
    for result in holdings:
        if result.incomplete:
            warnings.append(
                f"scope {result.scope!r} has holdings with no parsed value; "
                "cannot fully reconcile"
            )
        elif result.reported_total is None:
            warnings.append(f"no reported total to reconcile for scope {result.scope!r}")
        elif not result.ok:
            warnings.append(
                f"scope {result.scope!r} off by {result.delta} "
                f"(reported {result.reported_total}, computed {result.computed_total})"
            )

    any_incomplete = any(result.incomplete for result in holdings)
    computed_portfolio = sum(
        (result.computed_total for result in holdings), Decimal(0)
    )
    grand_total = summary.grand_total
    portfolio_delta = None if grand_total is None else grand_total - computed_portfolio
    # The portfolio is green only when *every* scope reconciles and the grand
    # total itself matches. Requiring the per-scope ``ok`` (not just the grand
    # total) prevents offsetting per-scope errors — e.g. one account over by +X
    # and another under by -X — from netting out into a false green portfolio.
    portfolio_ok = (
        not any_incomplete
        and all(result.ok for result in holdings)
        and grand_total is not None
        and portfolio_delta is not None
        and abs(portfolio_delta) <= tolerance
    )
    if grand_total is None:
        warnings.append("no portfolio grand_total in summary to reconcile against")
    elif not portfolio_ok:
        warnings.append(
            f"portfolio off by {portfolio_delta} "
            f"(reported {grand_total}, computed {computed_portfolio})"
        )

    return CasReconciliation(
        holdings=holdings,
        portfolio_delta=portfolio_delta,
        portfolio_ok=portfolio_ok,
        warnings=warnings,
    )


__all__ = [
    "DEFAULT_TOLERANCE",
    "build_reconciliation",
    "reconcile_account",
    "reconcile_folio",
]
