"""Schema construction and JSON serialization tests."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from cas_parser.models import (
    CasMeta,
    CasStatement,
    CasSummary,
    DematAccount,
    DematHolding,
    MfFolio,
    MfScheme,
)


def _sample_statement() -> CasStatement:
    return CasStatement(
        file="sample.pdf",
        meta=CasMeta(
            source="nsdl",
            investor_name="Sample Investor",
            pan="ABCDE1234F",
            statement_period_start=date(2026, 4, 1),
            statement_period_end=date(2026, 4, 30),
        ),
        accounts=[
            DematAccount(
                depository="NSDL",
                dp_id="IN300000",
                client_id="12345678",
                holdings=[
                    DematHolding(
                        name="Sample Equity Ltd",
                        isin="INE000000001",
                        asset_class="equity",
                        quantity=Decimal(10),
                        price=Decimal("100.50"),
                        value=Decimal("1005.00"),
                    )
                ],
                total_value=Decimal("1005.00"),
            )
        ],
        folios=[
            MfFolio(
                folio_number="9999999999",
                amc="Sample AMC",
                schemes=[
                    MfScheme(
                        scheme_name="Sample Fund - Direct Growth",
                        isin="INF000000001",
                        units=Decimal("123.456"),
                        nav=Decimal("45.6789"),
                        value=Decimal("5638.00"),
                    )
                ],
            )
        ],
        summary=CasSummary(
            asset_class_totals={"Equities": Decimal("1005.00"), "Mutual Funds": Decimal("5638.00")},
            grand_total=Decimal("6643.00"),
        ),
    )


def test_statement_builds_with_decimal_and_date() -> None:
    statement = _sample_statement()
    assert statement.accounts[0].source_ref == "IN300000/12345678"
    assert statement.accounts[0].holdings[0].quantity == Decimal(10)
    assert isinstance(statement.meta.statement_period_start, date)


def test_json_round_trips_decimal_and_date() -> None:
    statement = _sample_statement()
    payload = json.loads(statement.model_dump_json())

    assert payload["meta"]["statement_period_start"] == "2026-04-01"
    # pydantic serializes Decimal to a JSON string (no float drift).
    assert payload["summary"]["grand_total"] == "6643.00"
    assert payload["folios"][0]["schemes"][0]["units"] == "123.456"

    reloaded = CasStatement.model_validate(payload)
    assert reloaded == statement
