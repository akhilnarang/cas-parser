# AGENTS

Guidance for contributors and coding agents working on this repository.

## Project Purpose

`cas-parser` parses **depository Consolidated Account Statement (CAS)** PDFs —
issued by **NSDL** and **CDSL** — into normalized, comparable output covering
both **holdings** and **transactions** (demat securities + mutual funds).

Primary goals:

- robust extraction from password-protected, untagged CAS PDFs,
- a stable schema across the NSDL and CDSL layouts,
- holdings reconciliation as the primary correctness signal,
- privacy-safe development (no account-specific hardcoding).

## High-Level Architecture

- `cas_parser/cli.py` — command entrypoint, password (PAN) prompt, source
  selection, Rich table presentation, optional JSON export.
- `cas_parser/extractor.py` — source-agnostic raw PDF extraction (encryption
  detect/decrypt with multi-candidate passwords, page text/words/tables,
  metadata).
- `cas_parser/models.py` — Pydantic models: `CasStatement` (root), `CasMeta`,
  `DematAccount`/`DematHolding`, `MfFolio`/`MfScheme`, `CasTransaction`,
  `CasSummary`, `CasReconciliation`.
- `cas_parser/parsers/base.py` — `CasParser` abstract base + the shared
  `_attach_reconciliation()` helper.
- `cas_parser/parsers/registry.py` — parser registry keyed by source slug.
- `cas_parser/parsers/factory.py` — wrapper around the registry (no
  auto-detection — caller passes the source).
- `cas_parser/parsers/{nsdl_cas,cdsl_ecas}.py` — per-source coordinators.
- `cas_parser/parsers/sections/` — shared section readers (`splitter`,
  `demat`, `mf_folio`, `summary`) composed by the coordinators.
- `cas_parser/parsers/reconciliation.py` — holdings reconciliation.
- `cas_parser/parsers/utils/` — shared `dates`, `amounts` (Decimal), `isin`
  (validation + asset-class inference).
- `cas_parser/parsers/extractors/` — `wordlines` (group words into lines),
  `tables` (cell cleaning).

## Sources & Registry

Both sources are *full* SEBI consolidated statements (demat holdings + mutual
funds + NPS); they differ only in issuer/layout. Identify by the PDF metadata,
falling back to the page-1 title.

- `nsdl` — the NSDL CAS ("National Securities Depository Limited" /
  "Consolidated Account Statement for the month of …"). Contains demat sections
  (NSDL and CDSL), MF folios, and NPS where held. Each `DematAccount` is tagged
  with the depository inferred from its DP-ID prefix (`IN...` → NSDL, numeric →
  CDSL).
- `cdsl` — the CDSL CAS ("Central Depository Services (India) Limited" /
  "CONSOLIDATED ACCOUNT STATEMENT (CAS) FOR SECURITIES HELD IN DEMAT FORM AND
  INVESTMENTS IN MUTUAL FUNDS"). Demat holdings + mutual funds + NPS.

The two statements cover overlapping holdings, so an investor typically receives
one. **Running both parsers for the same period and summing double-counts.**
Deduplication is out of scope; the caller chooses the right parser. Anchor on
the **English** text — the Devanagari duplicate headings extract garbled.

## Source Detection

The source is detected automatically, not passed by the caller
(`factory.detect_source`), with a two-tier scheme:

1. **PRIMARY — PDF metadata.** Each parser declares `metadata_markers`,
   distinctive multi-word strings from the PDF's `/Title`/`/Author`/`/Creator`/
   etc. (joined into one case-insensitive blob). Metadata is structured and
   survives garbled/encrypted text layers, so it is checked first. The NSDL
   `/Keywords` value lists both "NSDL" and "CDSL", so the markers are
   deliberately multi-word and issuer-specific
   (`"NSDL-Consolidated Account Statement"`, `"NSDL-CAS Team"`,
   `"Central Depository Services (India)"`) and must not cross-match.
2. **FALLBACK — page-1 issuer title.** Each parser declares `title_markers`,
   matched against the first page's text. Used only when the metadata does not
   resolve a single issuer. Anchor these on the English text; the Devanagari
   title duplicate extracts garbled.

This is document-level identification — reading the depository's own
metadata/letterhead — so it is reliable and does not reopen the double-counting
concern: it picks exactly one issuer's parser. It is **not** a content heuristic
over holdings (an NSDL CAS embeds CDSL securities, but it is still issued by
NSDL). Detection raises if neither signal resolves a single issuer.

## Parser Contract

All parsers extend `CasParser` and override `parse(raw_data) -> CasStatement`,
setting `source = "<slug>"`. At the end of `parse()`, call
`self._attach_reconciliation(statement)` so the reconciliation is computed
consistently from the populated accounts/folios/summary.

Coordinators should compose the shared section readers in `parsers/sections/`
rather than duplicating extraction logic, since NSDL and CDSL share most section
shapes.

## Correctness / Reconciliation Principles

- Parse the **summary first** (it sits near the front of an NSDL CAS) to capture
  the reconciliation targets, then verify the detail sections against it.
- The primary signal is: `sum(holding values) == reported total`, per demat
  account and per folio, and `sum(scopes) == summary.grand_total`.
- `reconciliation.portfolio_ok` (and per-scope `ok`) being true is the parse's
  correctness check.
- Treat reconciliation as observability; do not silently coerce values to make
  totals match.

## Output Modes

- default run: prints tables only, no JSON file.
- `-v`: writes parsed JSON.
- `-vv`: writes `{ parsed, debug }`.
- `-vvv`: writes `{ parsed, debug, raw }`.

## Types

- Money / units / NAV / price are `Decimal`. Dates are `datetime.date`. Both
  serialize cleanly via `model_dump(mode="json")` / `model_dump_json()` (Decimal
  becomes a JSON string — no float drift).
- Use `parsers/utils/dates.py` for date parsing and `parsers/utils/amounts.py`
  (`parse_decimal`/`extract_decimal`) for numbers. Do not introduce floats.

## Privacy and Safety Rules

- Never commit real CAS PDFs or raw personal data (PAN, names, account/folio
  numbers, holdings).
- Never add sample values copied from real statements to comments or tests.
- Keep logs and docs generic and template-focused.

## Change Workflow

When modifying parser logic:

1. Keep source-specific behavior in the coordinator; keep shared shapes in
   `parsers/sections/`.
2. Preserve output schema compatibility.
3. Validate with `-vvv` output and verify `reconciliation.portfolio_ok` is true
   (and per-scope `ok`).
4. Update `README.md` when behavior changes.
5. Run `uv run ruff check cas_parser/` and `uv run ty check cas_parser/`.
6. Run `uv run pytest`.

## Adding / Implementing Source Parsers

Follow the skill at `.agents/skills/add-cas-parser/SKILL.md`. It walks through:

1. Studying the codebase and schema.
2. Extracting raw PDF data (mandatory — do not guess from visual layout).
3. Splitting sections via the `SectionSplitter` anchors.
4. Writing the section readers + coordinator.
5. Registering the source.
6. Testing for `reconciliation.portfolio_ok`.

## Coding Conventions

- Use typed Python signatures.
- Add docstrings with `Args`/`Returns` for non-trivial functions.
- Prefer pure helper functions for parsing steps.
- Keep CLI presentation logic out of parser core logic.
- Python 3.14 syntax is allowed; do not "fix" valid 3.14 forms just for style.

## Non-Goals

- OCR / scanned image-only CAS PDFs (text-layer PDFs only).
- Cross-source deduplication of the NSDL/CDSL overlap.
- Content-heuristic detection of holdings/asset types beyond the ISIN prefix
  (source detection itself is by PDF metadata / issuer title — see Source
  Detection).
- Storing statement data in this repository.

## Known Limitations & Gotchas

- **No tagged structure.** Sections are separated only by visual header strings;
  use `SectionSplitter` anchors. The `nsdl`/`cdsl` anchors are confirmed against
  real statements; when adding a new source, confirm its anchor wording against a
  real, PII-scrubbed sample.
- **Summary is at the front, not the end** of an NSDL CAS. Parse it first.
- **Long, multi-account PDFs.** A holder with several DPs and many folios can
  produce a 60–80 page CAS; table rows split across page boundaries — reuse the
  column mapping from the first header and handle continuation rows.
- **Extraction quality varies.** Older NSDL PDFs use different generators; some
  have poor text layers. Log the extraction method and page counts; a demat
  section yielding zero holdings on a multi-page PDF is a red flag.
- **ISIN may be absent** for SGBs / unlisted bonds in older formats — `isin` is
  nullable and `notes` holds raw instrument text that did not parse.
- **MF folios are RTA data**, not depository accounts — they live in
  `folios`, separate from `accounts`, by design.
- **NPS is part of the CAS** (confirmed in the CDSL statement: "…AND INVESTMENTS
  IN MUTUAL FUNDS" plus an NPS section). The schema has no NPS container yet —
  design one (likely a top-level `nps` list with PRAN / tier / scheme holdings)
  once a real layout is captured. Until then, NPS holdings are not extracted.
- **Devanagari extracts garbled.** Every heading is printed in English and Hindi;
  the Hindi text layer comes out as mojibake. Anchor only on the English strings.
