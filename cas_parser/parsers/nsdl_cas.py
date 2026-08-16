"""NSDL Consolidated Account Statement (CAS) parser.

The NSDL CAS is the *combined* statement: it contains NSDL demat sections,
CDSL demat sections (DP-IDs starting `120`/`130` vs NSDL's `IN3...`), and the
mutual-fund folio section sourced from the RTAs (CAMS/KFintech). Each
`DematAccount` is tagged with the depository inferred from its DP-ID so the
combined PDF is attributed correctly.

Layout (confirmed against a real, PII-scrubbed statement)
---------------------------------------------------------
- Page 1: issuer letterhead.
- Page 2: the account-summary table — ``Your Demat Account and Mutual Fund
  Folios`` grouped by holder ("In the joint Names of" / "In the Single Name
  of"), each row carrying a per-account ``Value in ```; a per-group ``Total`` and
  a single ``Grand Total`` (the reconciliation target).
- A per-group ``PORTFOLIO COMPOSITION`` table (asset-class totals with the class
  code in parentheses, e.g. ``Equities (E)``) precedes that group's holdings.
- Holdings: per-account blocks introduced by ``<NSDL|CDSL> Demat Account ... DP
  ID: ... Client ID: ...`` headers. NSDL accounts use a six-column equity table
  (``ISIN / Stock Symbol | Company Name | Face Value | No. of Shares | Market
  Price | Value``); CDSL accounts use a seven-column balance table. MF folios use
  a ten-column RTA table (``ISIN/UCC | ISIN Description | Folio No. | Units | ...
  | Current Value``).
- Transactions: a per-account "Summary of Transactions" block in two layouts —
  NSDL eight-column (``Date | Order No | Description | Instruction Details |
  Opening | Debit | Credit | Closing``) and CDSL five-column (``Date |
  Transaction Particulars | Credit | Debit | Current Balance``), each anchored by
  ``ISIN : <isin> - <name>`` rows. Ends at ``***End of Statement***``.

This differs from the CDSL CAS, whose holdings/transactions are uniform
nine-column tables and whose legend is a free-text ``Note:`` block; the NSDL CAS
encodes its legend inline (asset-class codes + ``NOT LISTED`` markers) and prints
two depository-specific table shapes. The NSDL section readers therefore live in
their own ``sections/nsdl_*`` modules, reusing the shared utils (dates / amounts
/ isin / transaction_codes), the demat depository-tagging helper, and the legend
note-resolution helper.

Text-layer note: the English headings extract cleanly; the Devanagari duplicate
title comes out doubled ("NNaattiioonnaall ..."), so anchor on English only.

NPS note: this statement lists "National Pension System (N)" in the composition
with a zero value and carries no NPS holdings table, so there is nothing to
parse; the schema has no NPS container yet (see AGENTS.md "NPS").
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from cas_parser.models import CasMeta, CasStatement
from cas_parser.parsers.base import CasParser
from cas_parser.parsers.sections.nsdl_demat import parse_demat_accounts
from cas_parser.parsers.sections.nsdl_legend import parse_legend
from cas_parser.parsers.sections.nsdl_mf_folio import parse_mf_folios
from cas_parser.parsers.sections.nsdl_summary import (
    parse_account_summary,
    parse_summary,
)
from cas_parser.parsers.sections.nsdl_transactions import parse_transactions
from cas_parser.parsers.utils.dates import parse_date

# Distinctive PDF-metadata strings for this document. PRIMARY detect_source
# signal. Anchor on multi-word, NSDL-specific values: bare "NSDL" would also
# appear in this PDF's /Keywords ("...MF, NSDL, CDSL"), which itself lists
# "CDSL" — so distinctive phrases are required to avoid cross-matching CDSL.
METADATA_MARKERS = (
    "NSDL-Consolidated Account Statement",  # /Title
    "NSDL-CAS Team",  # /Creator
)

# Page-1 identifiers for this document. FALLBACK detect_source signal. The
# English text layer extracts cleanly; the Devanagari duplicate does not —
# anchor on English only.
TITLE_MARKERS = (
    "National Securities Depository Limited",
    "Consolidated Account Statement for the month",
)

# Page-text anchors. The transaction section opens at the first "Summary of
# Transactions" block and closes at "***End of Statement***".
_TRANSACTION_ANCHOR = "Summary of Transactions"
_END_ANCHOR = "End of Statement"

# Page-text / metadata patterns.
_PAN_RE = re.compile(r"\(PAN\s*:\s*([A-Z0-9X]{10})\)", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"period from\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*to\s*(\d{2}-[A-Za-z]{3}-\d{4})",
    re.IGNORECASE,
)
# "In the joint Names of\n<name>\n<name> (PAN..." / "In the Single Name of\n<name>"
_JOINT_NAME_RE = re.compile(
    r"In the joint Names of\s*\n\s*(?P<name>.+?)\s*\(", re.IGNORECASE
)
_SINGLE_NAME_RE = re.compile(
    r"In the Single Name of\s*\n\s*(?P<name>.+?)\s*\(", re.IGNORECASE
)
# PDF metadata "D:YYYYMMDDHHmmSS..." creation-date stamp.
_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


class NsdlCasParser(CasParser):
    """Parser for the NSDL consolidated CAS PDF."""

    source = "nsdl"
    metadata_markers = METADATA_MARKERS
    title_markers = TITLE_MARKERS

    def parse(self, raw_data: dict[str, Any]) -> CasStatement:
        """Parse a raw NSDL CAS payload into a `CasStatement`.

        Parses the front-matter summary first (asset-class totals + grand total +
        per-account reported values), then the demat accounts (both NSDL and CDSL
        layouts), MF folios and transactions, and attaches reconciliation.

        Args:
            raw_data: Raw extraction payload from the extractor module.

        Returns:
            A populated `CasStatement` with reconciliation attached.
        """
        pages: list[dict[str, Any]] = raw_data.get("pages", [])
        file_name = raw_data.get("file", "")
        metadata: dict[str, Any] = raw_data.get("metadata", {})

        meta = self._parse_meta(pages, metadata)

        txn_start = self._find_page(pages, _TRANSACTION_ANCHOR)
        # Holdings live before the transaction section; the front-matter summary
        # (account table + composition tables) is interleaved among them, so the
        # summary reader scans all holdings pages.
        holdings_pages = pages[:txn_start] if txn_start is not None else pages
        txn_pages = self._transaction_pages(pages, txn_start)

        legend = parse_legend(pages)
        summary = parse_summary(holdings_pages)
        account_totals, _, _ = parse_account_summary(holdings_pages)
        accounts = parse_demat_accounts(holdings_pages, legend, account_totals)
        folios = parse_mf_folios(pages, legend)
        transactions = parse_transactions(txn_pages)

        # TODO(nps): the composition lists "National Pension System (N)" at zero
        # value with no NPS holdings/PRAN table to parse. If a future NSDL CAS
        # includes one, design a top-level NPS container in models.py (PRAN /
        # tier / scheme positions) before extracting it — do not overload
        # demat/MF here.

        statement = CasStatement(
            file=file_name,
            meta=meta,
            accounts=accounts,
            folios=folios,
            transactions=transactions,
            summary=summary,
            legend=legend,
        )
        return self._attach_reconciliation(statement)

    @staticmethod
    def _find_page(pages: list[dict[str, Any]], anchor: str) -> int | None:
        """Return the index of the first page whose text contains ``anchor``."""
        upper = anchor.upper()
        for index, page in enumerate(pages):
            if upper in page.get("text", "").upper():
                return index
        return None

    def _transaction_pages(
        self, pages: list[dict[str, Any]], txn_start: int | None
    ) -> list[dict[str, Any]]:
        """Return the page slice covering the transaction section.

        Runs from the first "Summary of Transactions" page through the page
        carrying "***End of Statement***" (inclusive). Empty when no transaction
        section is present.

        Args:
            pages: All page payloads.
            txn_start: Index of the first transaction page, or None.

        Returns:
            The transaction-section pages.
        """
        if txn_start is None:
            return []
        end = self._find_page(pages, _END_ANCHOR)
        end_index = (end + 1) if end is not None else len(pages)
        return pages[txn_start:end_index]

    def _parse_meta(
        self, pages: list[dict[str, Any]], metadata: dict[str, Any]
    ) -> CasMeta:
        """Extract investor name, PAN, statement period and generation date.

        The investor name and PAN come from the page-2 holder lines (the first
        holder of the first grouping); the statement period from the "period
        from ... to ..." line; the generation date from the PDF
        ``/CreationDate`` stamp (the NSDL CAS body prints no generation date).

        Args:
            pages: All page payloads.
            metadata: Extractor metadata (``{"pypdf": {...}, "pymupdf": {...}}``).

        Returns:
            The populated `CasMeta`.
        """
        text = "\n".join(page.get("text", "") for page in pages[:3])

        pan_match = _PAN_RE.search(text)
        period_match = _PERIOD_RE.search(text)
        name_match = _JOINT_NAME_RE.search(text) or _SINGLE_NAME_RE.search(text)

        start = end = None
        if period_match:
            start = parse_date(period_match.group(1))
            end = parse_date(period_match.group(2))

        return CasMeta(
            source=self.source,
            investor_name=name_match.group("name").strip() if name_match else None,
            pan=pan_match.group(1).upper() if pan_match else None,
            statement_period_start=start,
            statement_period_end=end,
            generated_on=self._parse_generated_on(metadata),
        )

    @staticmethod
    def _parse_generated_on(metadata: dict[str, Any]) -> date | None:
        """Parse the statement generation date from the PDF ``/CreationDate``.

        Args:
            metadata: Extractor metadata block.

        Returns:
            The creation date, or None when no parseable stamp is present.
        """
        pypdf_meta = metadata.get("pypdf", {})
        stamp = pypdf_meta.get("/CreationDate", "")
        match = _PDF_DATE_RE.match(str(stamp))
        if not match:
            return None
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
