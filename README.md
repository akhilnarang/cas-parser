# cas-parser

Parser for **depository Consolidated Account Statements (CAS)** — the
password-protected PDFs issued by **NSDL** and **CDSL** — into a stable schema
covering both **holdings** and **transactions**.

## Sources

| Slug   | Document              | Covers                                              |
| ------ | --------------------- | --------------------------------------------------- |
| `nsdl` | NSDL consolidated CAS | Demat holdings + mutual funds (+ NPS, if held)      |
| `cdsl` | CDSL consolidated CAS | Demat holdings + mutual funds + NPS                 |

Both are full SEBI consolidated statements — demat holdings, mutual funds, and
NPS — issued by the two depositories. The source is **detected automatically**
from the PDF metadata (the depository names itself in `/Title`/`/Creator`/etc.),
falling back to the page-1 issuer title ("National Securities Depository
Limited" → `nsdl`, "Central Depository Services (India) Limited" → `cdsl`), so
there's no flag to pass.

> **Status:** both the `nsdl` and `cdsl` parsers are implemented and verified
> against real statements — demat holdings, mutual-fund folios, transactions
> (with readable types), the footnote legend, and full metadata — with holdings
> reconciliation (`reconciliation.portfolio_ok`) as the correctness signal. To
> add another CAS source, see the `add-cas-parser` skill
> (`.agents/skills/add-cas-parser/SKILL.md`).

## Usage

```bash
uv run cas-parser path/to/statement.pdf
```

The source (`nsdl` / `cdsl`) is detected from the PDF metadata, falling back to
the page-1 title. Depository CAS PDFs are encrypted; the password is the
holder's **PAN**. The CLI prompts for it and tries the value as typed and
uppercased.

Flags:

- `-v` / `-vv` / `-vvv` — write JSON (`parsed`, `+debug`, `+raw`). The raw/debug
  parts are written even if parsing fails, so `-vvv` is enough to inspect a
  layout. Note the single dash: `-vvv`, not `--vvv`.
- `--raw-only` — extract and write the raw JSON **without parsing or detection**.
  Useful when even detection can't identify the issuer (garbled/unknown PDF).
- `--export-json PATH` — write the parsed JSON
- `--output, -o PATH` — output path for `-v` / `--raw-only`

```bash
# Dump the raw extraction to inspect the layout (no parser needed):
uv run cas-parser path/to/statement.pdf --raw-only -o raw.json
```

## Output schema

`CasStatement` (see `cas_parser/models.py`):

- `meta` — source, investor name, PAN, statement period
- `accounts: list[DematAccount]` — `depository`, `dp_id`, `client_id`, `holdings[]`, `total_value`
  - `DematHolding` — `isin?`, `name`, `asset_class`, `quantity`, `price?`, `value?`
- `folios: list[MfFolio]` — `folio_number`, `amc?`, `schemes[]`
  - `MfScheme` — `isin?`, `scheme_name`, `units`, `nav?`, `value?`
- `transactions: list[CasTransaction]` — `scope` (`demat`/`mf`), `source_ref`, `date`, ...
- `summary: CasSummary` — `asset_class_totals`, `grand_total`
- `reconciliation: CasReconciliation` — correctness signal (see below)

Money / units / NAV are `Decimal`; dates are `datetime.date`. Both serialize
cleanly via `model_dump(mode="json")` / `model_dump_json()`.

## Correctness signal

A correct parse reproduces the totals the statement prints. `reconciliation`
checks that summed holding values match the reported per-account / per-folio
totals and the portfolio `grand_total`. `portfolio_ok` (and per-scope `ok`) is
the primary parse-correctness check.

## Development

```bash
uv sync
uv run pytest
uv run ruff check cas_parser/
uv run ty check cas_parser/
```

Privacy: never commit real CAS PDFs or real PII. Test fixtures must be
PII-scrubbed.
