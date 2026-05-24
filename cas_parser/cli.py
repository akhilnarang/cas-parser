"""CLI entrypoint for depository CAS parsing.

Workflow:
1) extract raw PDF structure (prompting for the password — your PAN — when the
   CAS is encrypted),
2) detect the source parser (nsdl | cdsl) from the PDF metadata, falling back
   to the page-1 title,
3) print rich tables for quick inspection,
4) optionally export JSON.
"""

import getpass
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from cas_parser.extractor import extract_raw_pdf, is_pdf_encrypted
from cas_parser.models import CasStatement
from cas_parser.parsers.factory import detect_source, get_parser
from cas_parser.parsers.utils import format_date, format_decimal


def password_candidates(password: str) -> list[str]:
    """Build candidate passwords from user input.

    The NSDL/CDSL CAS password is the holder's PAN; PANs are uppercase, so we
    try the input as typed and uppercased.
    """
    candidates = [password]
    upper = password.upper()
    if upper != password:
        candidates.append(upper)
    return candidates


def extract_with_password_prompt(pdf_path: Path) -> dict[str, Any]:
    """Extract raw PDF data, prompting for the password (PAN) when required."""
    if not is_pdf_encrypted(pdf_path):
        return extract_raw_pdf(pdf_path, passwords=None)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        password = getpass.getpass("CAS is encrypted. Enter password (your PAN): ")
        try:
            return extract_raw_pdf(
                pdf_path,
                passwords=password_candidates(password),
            )
        except ValueError as error:
            if "Failed to decrypt PDF" in str(error) and attempt < max_attempts:
                print(f"Incorrect password ({attempt}/{max_attempts}). Try again.")
                continue
            raise

    raise ValueError("Failed to decrypt PDF.")


def _money(value: Decimal | None) -> str:
    """Render an optional currency value (2 dp) for display."""
    return format_decimal(value) if value is not None else ""


def _qty(value: Decimal | None) -> str:
    """Render an optional quantity / units / NAV at full precision.

    Unlike `_money`, this keeps the Decimal's own scale (units carry more than
    two places, NAV often four) rather than rounding to currency precision.
    """
    return f"{value:,f}" if value is not None else ""


# Display-only column widths for the long free-text columns (security/scheme
# names, transaction descriptions). The STORED model values are always the
# complete, untruncated strings parsed from the statement; these caps only bound
# how wide the name column may grow on screen, and Rich *wraps* (never clips) the
# full text within that width. A generous cap keeps long names legible (some
# ETF / MF `AMC#scheme` strings run quite long) instead of cutting them off
# mid-scheme.
_NAME_DISPLAY_WIDTH = 60
_DESCRIPTION_DISPLAY_WIDTH = 48


def print_statement(statement: CasStatement) -> None:
    """Render a parsed CAS as Rich tables."""
    console = Console()
    meta = statement.meta

    console.print(f"Source: {meta.source}")
    if meta.investor_name:
        console.print(f"Investor: {meta.investor_name}")
    if meta.pan:
        console.print(f"PAN: {meta.pan}")
    if meta.statement_period_start and meta.statement_period_end:
        console.print(
            f"Period: {format_date(meta.statement_period_start)} to "
            f"{format_date(meta.statement_period_end)}"
        )
    console.print()

    for account in statement.accounts:
        title = f"{account.depository} demat {account.source_ref}"
        if account.dp_name:
            title += f" ({account.dp_name})"
        table = Table(title=title)
        table.add_column("ISIN", style="cyan", no_wrap=True)
        # The full name is stored on the model; wrap (don't clip) it for display.
        table.add_column("Security", style="white", max_width=_NAME_DISPLAY_WIDTH)
        table.add_column("Class", style="yellow", no_wrap=True)
        table.add_column("Qty", justify="right", style="white")
        table.add_column("Price", justify="right", style="magenta")
        table.add_column("Value", justify="right", style="green")
        # Footnote markers + their resolved legend meaning (e.g. unlisted).
        table.add_column("Notes", style="red", max_width=_DESCRIPTION_DISPLAY_WIDTH)
        for holding in account.holdings:
            table.add_row(
                holding.isin or "",
                holding.name,
                holding.asset_class,
                _qty(holding.quantity),
                _money(holding.price),
                _money(holding.value),
                holding.notes or "",
            )
        console.print(table)

    for folio in statement.folios:
        title = f"MF folio {folio.folio_number} ({folio.amc or '—'})"
        if folio.total_value is not None:
            title += f"  value {_money(folio.total_value)}"
        table = Table(title=title)
        table.add_column("ISIN", style="cyan", no_wrap=True)
        # The full scheme name is stored on the model; wrap it for display.
        table.add_column("Scheme", style="white", max_width=_NAME_DISPLAY_WIDTH)
        table.add_column("Units", justify="right", style="white")
        table.add_column("NAV", justify="right", style="magenta")
        table.add_column("Value", justify="right", style="green")
        for scheme in folio.schemes:
            table.add_row(
                scheme.isin or "",
                scheme.scheme_name,
                _qty(scheme.units),
                _qty(scheme.nav),
                _money(scheme.value),
            )
        console.print(table)

    if statement.transactions:
        table = Table(title="Transactions")
        table.add_column("Date", style="cyan", no_wrap=True)
        table.add_column("Scope", style="yellow", no_wrap=True)
        # Human-readable transaction type (e.g. "Early Pay-in"); the raw code is
        # preserved in the description. Direction is evident from the signed qty.
        table.add_column("Type", style="yellow", no_wrap=True)
        # The full description is stored on the model; wrap it for display.
        table.add_column(
            "Description", style="white", max_width=_DESCRIPTION_DISPLAY_WIDTH
        )
        table.add_column("Units/Qty", justify="right", style="white")
        table.add_column("Amount", justify="right", style="green")
        for txn in statement.transactions:
            table.add_row(
                format_date(txn.date),
                txn.scope,
                txn.transaction_type or "",
                txn.description,
                _qty(txn.units if txn.units is not None else txn.quantity),
                _money(txn.amount),
            )
        console.print(table)

    summary = statement.summary
    if summary.asset_class_totals or summary.grand_total is not None:
        table = Table(title="Portfolio Summary")
        table.add_column("Asset Class", style="white")
        table.add_column("Value", justify="right", style="green")
        for label, value in summary.asset_class_totals.items():
            table.add_row(label, _money(value))
        if summary.grand_total is not None:
            table.add_row("[bold]Grand Total[/bold]", f"[bold]{_money(summary.grand_total)}[/bold]")
        console.print(table)

    if statement.legend:
        table = Table(title="Legend (footnote markers)")
        table.add_column("Marker", style="red", no_wrap=True)
        table.add_column("Meaning", style="white")
        for marker, definition in statement.legend.items():
            table.add_row(marker, definition)
        console.print(table)

    recon = statement.reconciliation
    if recon:
        console.print()
        status = "green" if recon.portfolio_ok else "red bold"
        delta = "—" if recon.portfolio_delta is None else _money(recon.portfolio_delta)
        console.print(f"Portfolio reconciliation delta: [{status}]{delta}[/{status}]")
        for warning in recon.warnings:
            console.print(f"  [yellow]warn:[/yellow] {warning}")


def parse_cas(
    pdf: Path = typer.Argument(..., help="Path to the CAS PDF file"),
    raw_only: bool = typer.Option(
        False,
        "--raw-only",
        help="Extract and write raw JSON without parsing (skips detection)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output JSON path (for -v or --raw-only)",
    ),
    export_json: Path | None = typer.Option(
        None,
        "--export-json",
        help="Write parsed JSON",
    ),
    verbose: int = typer.Option(
        0,
        "-v",
        count=True,
        help="Write JSON (-v parsed, -vv +debug, -vvv +raw). raw/debug are "
        "written even if the parser is still a stub.",
    ),
) -> None:
    """Parse a depository CAS PDF and print normalized tables."""
    if not pdf.exists():
        raise typer.BadParameter(f"File not found: {pdf}")
    if pdf.suffix.lower() != ".pdf":
        raise typer.BadParameter("Input must be a .pdf file")

    try:
        raw_data = extract_with_password_prompt(pdf)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    if raw_only:
        raw_path = output or (Path.cwd() / "cas_raw.json")
        raw_path.write_text(
            json.dumps(raw_data, indent=2, ensure_ascii=True, default=str),
            encoding="utf-8",
        )
        typer.echo(f"Wrote raw extraction to {raw_path}")
        return

    try:
        source = detect_source(raw_data)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Detected source: {source}")

    parser_impl = get_parser(source)
    parsed: CasStatement | None = None
    try:
        parsed = parser_impl.parse(raw_data)
    except NotImplementedError as error:
        # The parser is still a stub. With no output requested there's nothing
        # to show, so surface the error; otherwise fall through and still dump
        # the raw/debug payload (-vv/-vvv) like the other parsers do.
        if verbose == 0 and export_json is None:
            raise typer.BadParameter(str(error)) from error
        typer.echo(f"Note: {error}")

    if parsed is not None:
        print_statement(parsed)
        typer.echo(
            f"Accounts: {len(parsed.accounts)}  Folios: {len(parsed.folios)}  "
            f"Transactions: {len(parsed.transactions)}"
        )

    if verbose > 0:
        output_path: Path = output or (Path.cwd() / f"cas_run_{source}.json")
        payload: dict[str, Any] = {}
        if parsed is not None:
            parsed_dict = parsed.model_dump(mode="json")
            parsed_dict["source_parser"] = parser_impl.source
            payload["parsed"] = parsed_dict
        if verbose >= 2:
            payload["debug"] = parser_impl.build_debug(raw_data)
        if verbose >= 3:
            payload["raw"] = raw_data

        if not payload:
            # -v but the parser is a stub, so no parsed output exists.
            typer.echo("Nothing to write at -v (parser not implemented); use -vvv for raw.")
        else:
            # Preserve the bare-parsed shape when that's all there is (-v).
            output_obj: Any = payload["parsed"] if list(payload) == ["parsed"] else payload
            output_path.write_text(
                json.dumps(output_obj, indent=2, ensure_ascii=True, default=str),
                encoding="utf-8",
            )
            typer.echo(f"Wrote extraction to {output_path}")

    if export_json is not None:
        if parsed is None:
            typer.echo("Nothing to export: parser not implemented yet.")
        else:
            parsed_dict = parsed.model_dump(mode="json")
            parsed_dict["source_parser"] = parser_impl.source
            export_json.write_text(
                json.dumps(parsed_dict, indent=2, ensure_ascii=True, default=str),
                encoding="utf-8",
            )
            typer.echo(f"Wrote parsed JSON to {export_json}")


def main() -> None:
    """Program entrypoint for console script execution."""
    typer.run(parse_cas)


if __name__ == "__main__":
    main()
