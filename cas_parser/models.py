"""Pydantic models for Consolidated Account Statement (CAS) parser output.

The schema is deliberately split into two structural realities that a
depository CAS conflates on the page:

- ``accounts`` — demat accounts (one per DP/client-id pair). These hold
  securities identified by ISIN (equities, ETFs, bonds, SGBs, ...).
- ``folios`` — mutual-fund folios. These are RTA (CAMS/KFintech) identifiers,
  not depository accounts, so they live separately from ``accounts`` even when
  an NSDL CAS prints both in one PDF.

Transactions are kept flat at the root and stamped with a ``source_ref`` join
key so callers can iterate the whole ledger without walking the tree.

Money / units / NAV are ``Decimal`` and dates are ``datetime.date``; both
serialize cleanly via ``model_dump(mode="json")`` / ``model_dump_json()``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Depository = Literal["NSDL", "CDSL"]

AssetClass = Literal[
    "equity",
    "preference_share",
    "bond",
    "debenture",
    "etf",
    "mutual_fund",
    "aif",
    "government_security",
    "sgb",
    "other",
]

TransactionScope = Literal["demat", "mf"]


class CasMeta(BaseModel):
    """Statement-level metadata."""

    source: str  # parser slug: "nsdl" | "cdsl"
    investor_name: str | None = None
    pan: str | None = None
    statement_period_start: date | None = None
    statement_period_end: date | None = None
    generated_on: date | None = None


class DematHolding(BaseModel):
    """A single security held in a demat account.

    ``flags`` holds the statement's footnote markers attached to the security's
    ISIN/name cell (e.g. ``["!!"]``), detected dynamically against the parsed
    ``CasStatement.legend`` marker set — only markers the legend actually defines
    are recorded, so the ``#`` AMC/scheme separator is never mistaken for one.
    ``notes`` resolves those markers into human-readable text (the legend's own
    definition, e.g. an unlisted-security explanation).
    """

    name: str
    isin: str | None = None
    asset_class: AssetClass = "other"
    quantity: Decimal
    price: Decimal | None = None  # closing / market price per unit
    value: Decimal | None = None  # market value of the holding
    flags: list[str] = Field(default_factory=list)  # legend markers on this row
    notes: str | None = None  # resolved legend meanings (or raw instrument text)


class DematAccount(BaseModel):
    """A demat account — one DP / client-id pair."""

    depository: Depository
    dp_id: str
    client_id: str
    dp_name: str | None = None
    holdings: list[DematHolding] = Field(default_factory=list)
    total_value: Decimal | None = None  # account value as reported in the PDF

    @property
    def source_ref(self) -> str:
        """Join key used to stamp transactions back to this account."""
        return f"{self.dp_id}/{self.client_id}"


class MfScheme(BaseModel):
    """A mutual-fund scheme position within a folio.

    ``flags`` / ``notes`` mirror :class:`DematHolding`: footnote markers attached
    to the scheme/ISIN cell and their resolved legend meanings, populated only
    when the statement marks an MF scheme.
    """

    scheme_name: str
    isin: str | None = None
    units: Decimal
    nav: Decimal | None = None
    value: Decimal | None = None  # market value of the position
    cost: Decimal | None = None  # invested cost, when the statement reports it
    flags: list[str] = Field(default_factory=list)  # legend markers on this row
    notes: str | None = None  # resolved legend meanings


class MfFolio(BaseModel):
    """A mutual-fund folio (RTA identifier, not a depository account)."""

    folio_number: str
    amc: str | None = None
    schemes: list[MfScheme] = Field(default_factory=list)
    total_value: Decimal | None = None  # folio value as reported in the PDF


class CasTransaction(BaseModel):
    """A single statement-period transaction.

    ``scope`` discriminates a demat security movement from a mutual-fund
    transaction. Demat rows carry ``quantity``; MF rows carry
    ``units`` / ``nav`` / ``amount``.
    """

    scope: TransactionScope
    source_ref: str  # "<dp_id>/<client_id>" for demat, folio number for mf
    date: date
    description: str
    isin: str | None = None
    transaction_type: str | None = None  # purchase / redemption / dividend / switch_in / ...
    quantity: Decimal | None = None  # demat: securities credited (+) / debited (-)
    units: Decimal | None = None  # mf
    nav: Decimal | None = None  # mf
    amount: Decimal | None = None  # mf
    reference: str | None = None


class CasSummary(BaseModel):
    """Portfolio summary / asset-class totals as reported in the statement.

    ``asset_class_totals`` is keyed by the class label exactly as it appears in
    the PDF (e.g. "Equities", "Mutual Funds") so the model never hard-codes a
    fixed set of buckets.
    """

    asset_class_totals: dict[str, Decimal] = Field(default_factory=dict)
    grand_total: Decimal | None = None


class HoldingReconciliation(BaseModel):
    """Holdings reconciliation result for a single scope."""

    scope: str  # account "<dp>/<client>", folio number, or "portfolio"
    reported_total: Decimal | None = None
    computed_total: Decimal
    delta: Decimal | None = None  # reported - computed; None when nothing to compare
    incomplete: bool = False  # at least one holding had no parsed value
    ok: bool


class CasReconciliation(BaseModel):
    """Aggregate correctness signal for a parsed CAS.

    The primary check is that summed holding values match the totals the
    statement itself prints — per account/folio and for the whole portfolio.
    """

    holdings: list[HoldingReconciliation] = Field(default_factory=list)
    portfolio_delta: Decimal | None = None
    portfolio_ok: bool = False
    warnings: list[str] = Field(default_factory=list)


class CasStatement(BaseModel):
    """Root output of CAS parsers.

    ``legend`` is the statement's footnote/notes block parsed into a marker ->
    definition map (e.g. ``{"!!": "Valuation has been derived ...", "##":
    "Unlisted ISIN - ...", ...}``), parsed dynamically from the document so the
    meanings are never hard-coded. The same marker set drives which footnotes are
    recognised on each holding's ``flags`` / ``notes``.
    """

    file: str
    meta: CasMeta
    accounts: list[DematAccount] = Field(default_factory=list)
    folios: list[MfFolio] = Field(default_factory=list)
    transactions: list[CasTransaction] = Field(default_factory=list)
    summary: CasSummary = Field(default_factory=CasSummary)
    legend: dict[str, str] = Field(default_factory=dict)
    reconciliation: CasReconciliation | None = None


__all__ = [
    "AssetClass",
    "CasMeta",
    "CasReconciliation",
    "CasStatement",
    "CasSummary",
    "CasTransaction",
    "DematAccount",
    "DematHolding",
    "Depository",
    "HoldingReconciliation",
    "MfFolio",
    "MfScheme",
    "TransactionScope",
]
