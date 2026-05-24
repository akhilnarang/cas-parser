---
name: add-cas-parser
description: Implement or extend a depository CAS parser (NSDL/CDSL) — holdings and transactions. Use when building the nsdl or cdsl parser, or adding a new CAS source.
---

# Add / Implement a CAS Source Parser

**This skill is interactive.** It requires running Python/Bash to extract PDF
data, iterating on the parser, and testing. Do not run this in the background.
If you need tool permissions, ask for them.

Arguments: `$ARGUMENTS` — source slug (`nsdl` | `cdsl` | new) and path
to a sample CAS PDF.

## Privacy first

CAS PDFs contain PAN, names, account/folio numbers and holdings. **Never commit
the PDF, and never paste real values into code, comments, tests, or commit
messages.** Test fixtures must be PII-scrubbed JSON.

## Step 1: Study the codebase

Read these to understand the patterns:

- `cas_parser/models.py` — output schema (`CasStatement` and nested models).
- `cas_parser/parsers/base.py` — `CasParser` ABC and `_attach_reconciliation()`.
- `cas_parser/parsers/sections/splitter.py` — `SectionSplitter` (anchor-based
  page slicing) that coordinators use.
- `cas_parser/parsers/sections/{demat,mf_folio,summary}.py` — the section
  readers you will implement/extend.
- `cas_parser/parsers/reconciliation.py` — the correctness signal.
- `cas_parser/parsers/utils/` (`dates`, `amounts`, `isin`) and
  `cas_parser/parsers/extractors/` (`wordlines`, `tables`) — reuse these.
- `cas_parser/parsers/registry.py` and `cli.py` — registration + the slug enum.

## Step 2: Extract raw PDF data

**MANDATORY. Do not write any parsing code before completing this step.**

The visual layout and what pdfplumber extracts are often very different. Use
`--raw-only` to dump the raw extraction without parsing (it skips the parser
entirely), then inspect it:

```bash
uv run cas-parser <pdf> --raw-only -o raw.json   # prompts for the PAN password
```

Or interactively from the `cas-parser` directory:

```python
from pathlib import Path
from cas_parser.extractor import extract_raw_pdf
raw = extract_raw_pdf(Path("<pdf>"), passwords=["<PAN>"])
print(raw["page_count"])
for page in raw["pages"]:
    print("== page", page["page_number"], "==")
    print(page["text"][:1500])
```

Then, interactively (multiple rounds):

1. **Read each page's text** to locate the section header strings (summary,
   depository/demat accounts, mutual-fund folios, transactions). These become
   your `SectionSplitter` anchors.
2. **Print all tables on each page** (`page["tables"]`). Determine whether
   holdings/transactions come out as clean tables or need word-line
   reconstruction.
3. **If tables are empty**, holdings are word-positioned. Use
   `group_words_into_lines(page["words"])` and inspect `x0` positions to find
   column boundaries.
4. **Find the reported totals** (per account, per folio, portfolio grand total)
   — these are your reconciliation targets.

## Step 3: Confirm the section anchors

For a NEW source, set the anchor constants in its coordinator to the **exact**
header wording you found (replace your initial placeholder anchors). The existing
`nsdl_cas.py` / `cdsl_ecas.py` anchors are already confirmed against real
statements.

Verify the split:

```python
from cas_parser.parsers.sections import SectionSplitter
splitter = SectionSplitter(raw["pages"])
print(splitter.split({"summary": SUMMARY_ANCHORS, "demat": DEMAT_ANCHORS, "mf": MF_ANCHORS}))
```

## Step 4: Implement the section readers

Implement, in `parsers/sections/`:

- `summary.parse_summary(pages) -> CasSummary` — asset-class totals + grand total.
- `demat.parse_demat_accounts(pages) -> list[DematAccount]` — accounts +
  holdings; set `depository` via `depository_from_dp_id(dp_id)`; set
  `asset_class` via `infer_asset_class(isin)` (refine SGB/ETF from the name);
  populate `total_value` from the reported per-account figure.
- `mf_folio.parse_mf_folios(pages) -> list[MfFolio]` — folios + scheme positions.

Reuse `parse_decimal`/`extract_decimal` for numbers, `parse_date` for dates,
`find_isin`/`is_valid_isin` for ISINs, and the `extractors/` helpers. Keep
`isin` nullable and stash unparseable instrument text in `DematHolding.notes`.

Then collect transactions (demat security movements; MF purchase/redeem/
dividend/switch) into `CasTransaction`, stamping `source_ref` with the account
`source_ref` or the folio number.

## Step 5: Wire the coordinator

In `parsers/{source}.py::parse()`:

1. Split sections with the `SectionSplitter`.
2. Parse summary first, then demat accounts, then MF folios, then transactions.
3. Build the `CasStatement` and `return self._attach_reconciliation(statement)`.

## Step 6: Register (only for a brand-new source)

- `parsers/registry.py`: import the class and add it to `PARSER_REGISTRY`.
- On the parser class: set `source = "<slug>"` and `title_markers = (...)` with
  the page-1 issuer strings, so `factory.detect_source` can route to it. There
  is no `--source` flag or CLI enum to update — selection is automatic.
- `factory.py` / `cli.py` route through detection — no change needed.

## Step 7: Test and iterate

Run: `uv run cas-parser <pdf> -vvv` (source is auto-detected)

Check:

- [ ] Demat accounts + holdings extracted (counts match the PDF)
- [ ] MF folios + schemes extracted (NSDL CAS)
- [ ] Transactions extracted (when the document includes them)
- [ ] **`reconciliation.portfolio_ok` is true** and per-scope `ok` is true — the
      critical check
- [ ] Investor name, PAN, statement period extracted
- [ ] `depository` tagged correctly (NSDL vs CDSL within a consolidated CAS)
- [ ] `isin` / `asset_class` populated; `notes` used for unparseable instruments
- [ ] `uv run ruff check cas_parser/`
- [ ] `uv run ty check cas_parser/`
- [ ] `uv run pytest`

Add a PII-scrubbed fixture under `tests/fixtures/` and a test asserting
reconciliation passes on it.

If reconciliation fails or holdings are missing, go back to step 2 and examine
the raw data more closely. This is iterative.

## Gotchas

- **Summary is near the front** of an NSDL CAS, before the detail — parse it
  first for the reconciliation targets.
- **No tagged structure** — sections are split only by header strings; anchors
  must match the real wording.
- **Continuation rows across pages** — reuse the column mapping from the first
  header; stitch rows that wrap.
- **Indian number grouping** ("1,52,581.54") and unit precision ("123.456") —
  `parse_decimal` handles both; never use floats.
- **DP-ID prefix** identifies the depository inside a consolidated CAS (`IN...`
  → NSDL, numeric → CDSL).
- **MF folios are RTA data**, not depository accounts — put them in `folios`.
- **Both CAS are consolidated** (demat + mutual funds + NPS). NPS has no schema
  container yet — see AGENTS.md "Known Limitations" before extracting it.
- **Devanagari extracts garbled** — every heading is printed in English + Hindi;
  anchor only on the English strings.
- **Missing ISIN** for SGBs / unlisted instruments in older formats — keep
  `isin` nullable.

## Self-improvement

If you discover new patterns, edge cases, or pitfalls while building a parser,
update this skill file with what you learned.
