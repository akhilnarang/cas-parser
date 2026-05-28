"""CDSL CAS parser.

The CDSL Consolidated Account Statement is a *full* consolidated statement —
titled "CONSOLIDATED ACCOUNT STATEMENT (CAS) FOR SECURITIES HELD IN DEMAT FORM
AND INVESTMENTS IN MUTUAL FUNDS" — covering demat holdings, mutual-fund
investments, and NPS. It is not a CDSL-demat-only document.

Layout (confirmed against a real, PII-scrubbed statement)
---------------------------------------------------------
- Page 1: issuer title + "Summary of Investments" (per-portfolio breakdown).
- An "Asset Class / Value / Percentage" table (asset-class totals + grand
  total) — the reconciliation target — sits before the detail sections.
- An "Account Details" block lists every demat account
  (``DP Name : ... DP ID : ... CLIENT ID : ...``) and the MF folios
  (``AMC Name : ... Folio No : ...``).
- Per-account "STATEMENT OF TRANSACTIONS" + "HOLDING STATEMENT" blocks. Each
  holding block is a nine-column ISIN/Value table ending in a
  ``Portfolio Value ` <total>`` row (the reported per-account total).
- A "MUTUAL FUND UNITS HELD WITH MF/RTA" table for the MF folios.

Text-layer note: the English headings extract cleanly; the Devanagari
duplicates and the bold tab labels come out garbled, so anchor on the clean
English strings only ("Summary of Investments", "DP Name"). The detail sections
are located by their distinctive *table shapes* rather than their garbled
headers.

NPS note: this statement's title and notes mention NPS, but it carries no NPS
holdings table (no PRAN positions). There is therefore nothing to parse, and
the schema has no NPS container yet. See AGENTS.md "NPS".
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from cas_parser.models import CasMeta, CasStatement
from cas_parser.parsers.base import CasParser
from cas_parser.parsers.sections import SectionSplitter
from cas_parser.parsers.sections.demat import parse_demat_accounts
from cas_parser.parsers.sections.legend import parse_legend
from cas_parser.parsers.sections.mf_folio import parse_mf_folios
from cas_parser.parsers.sections.summary import parse_summary
from cas_parser.parsers.sections.transactions import parse_transactions
from cas_parser.parsers.utils.dates import parse_date

# Distinctive PDF-metadata strings for this document. PRIMARY detect_source
# signal. The metadata /Title reads "...(India) Ltd" (abbreviated) whereas the
# page-1 title reads "...(India) Limited" (spelled out), so the metadata needs
# its own marker. Kept to the shared, non-abbreviated prefix so both forms
# match; multi-word and CDSL-specific, so it cannot match an NSDL blob.
METADATA_MARKERS = ("Central Depository Services (India)",)

# Page-1 identifiers for this document (English text layer). FALLBACK
# detect_source signal, used when the metadata markers do not resolve.
TITLE_MARKERS = (
    "Central Depository Services (India) Limited",
    "CONSOLIDATED ACCOUNT STATEMENT (CAS) FOR SECURITIES HELD IN DEMAT",
)

# Section anchors in the raw page text. Anchor only on clean English strings:
# "Summary of Investments" opens the front matter; "DP Name :" opens the
# account-details / detail block. The garbled Devanagari/bold headers
# ("HOLDING STATEMENT", "STATEMENT OF TRANSACTIONS", "MUTUAL FUND UNITS") are
# unreliable, so the detail readers self-locate by table shape from the demat
# anchor onward.
SUMMARY_ANCHORS = ("Summary of Investments",)
DEMAT_ANCHORS = ("DP Name :",)

# Page-1 metadata patterns (English layer).
_PAN_RE = re.compile(r"PAN\s*:\s*([A-Z]{5}\d{4}[A-Z])", re.IGNORECASE)
_PERIOD_RE = re.compile(
    # Older CDSL PDFs render this as "FOR THE PERIOD\nFROM <date> TO <date>"
    # with a real newline between PERIOD and FROM; newer ones keep it on one
    # line. Match either with \s+ between PERIOD and FROM.
    r"PERIOD\s+FROM\s*(\d{2}-\d{2}-\d{4}).*?TO\s*(\d{2}-\d{2}-\d{4})",
    re.IGNORECASE | re.DOTALL,
)
_NAME_RE = re.compile(r"single name of\s*\n\s*(.+?)\s*\(\s*PAN", re.IGNORECASE)
# PDF metadata "D:YYYYMMDDHHmmSS..." creation-date stamp (the statement's
# generation/print date — the CDSL CAS body carries no printed generation date).
_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


class CdslEcasParser(CasParser):
    """Parser for the CDSL consolidated CAS PDF."""

    source = "cdsl"
    metadata_markers = METADATA_MARKERS
    title_markers = TITLE_MARKERS

    def parse(self, raw_data: dict[str, Any]) -> CasStatement:
        """Parse a raw CDSL CAS payload into a `CasStatement`.

        Args:
            raw_data: Raw extraction payload from the extractor module.

        Returns:
            A populated `CasStatement` with reconciliation attached.
        """
        pages: list[dict[str, Any]] = raw_data.get("pages", [])
        file_name = raw_data.get("file", "")
        metadata: dict[str, Any] = raw_data.get("metadata", {})

        meta = self._parse_meta(pages, metadata)

        splitter = SectionSplitter(pages)
        sections = splitter.split({"summary": SUMMARY_ANCHORS, "demat": DEMAT_ANCHORS})
        section_by_name = {section.name: section for section in sections}

        demat_section = section_by_name.get("demat")
        demat_start = demat_section.start_page if demat_section else 0
        # The asset-class summary table sits between the front-matter summary
        # and the detail block, so feed the summary reader everything up to the
        # demat anchor.
        summary_pages = pages[:demat_start] if demat_start else pages
        # The detail readers (demat / MF / transactions) all live from the
        # account-details block onward; they self-locate their table shapes.
        detail_pages = pages[demat_start:] if demat_start else pages

        # The footnote legend ("Note:" block) is parsed first so its marker set
        # drives which footnotes are recognised on holdings/schemes. It sits in
        # the detail section, but parse over all pages so it is never missed.
        legend = parse_legend(pages)

        summary = parse_summary(summary_pages)
        # Collect any holding-attribution warning (CDSL attributes holdings to
        # accounts order-based on the "Portfolio Value" markers; a marker/header
        # count mismatch is surfaced here rather than silently mis-attributing).
        attribution_warnings: list[str] = []
        accounts = parse_demat_accounts(detail_pages, legend, attribution_warnings)
        folios = parse_mf_folios(detail_pages, legend)
        transactions = parse_transactions(detail_pages)

        # TODO(nps): this statement's title/notes reference NPS, but it carries
        # no NPS holdings/PRAN table to parse. If a future CDSL CAS includes one,
        # design a top-level NPS container in models.py (PRAN / tier / scheme
        # positions) before extracting it — do not overload demat/MF here.

        statement = CasStatement(
            file=file_name,
            meta=meta,
            accounts=accounts,
            folios=folios,
            transactions=transactions,
            summary=summary,
            legend=legend,
        )
        self._attach_reconciliation(statement)
        if statement.reconciliation is not None:
            statement.reconciliation.warnings.extend(attribution_warnings)
        return statement

    def _parse_meta(
        self, pages: list[dict[str, Any]], metadata: dict[str, Any]
    ) -> CasMeta:
        """Extract investor name, PAN, statement period and generation date.

        The statement period comes from the page-1 "PERIOD FROM ... TO ..."
        line. The CDSL CAS body prints no generation/print date, so
        ``generated_on`` is taken from the PDF's ``/CreationDate`` metadata —
        the date the depository produced the statement.

        Args:
            pages: All page payloads.
            metadata: Extractor metadata (``{"pypdf": {...}, "pymupdf": {...}}``).

        Returns:
            The populated `CasMeta`.
        """
        text = "\n".join(page.get("text", "") for page in pages[:2])

        pan_match = _PAN_RE.search(text)
        name_match = _NAME_RE.search(text)
        period_match = _PERIOD_RE.search(text)

        start = end = None
        if period_match:
            start = parse_date(period_match.group(1))
            end = parse_date(period_match.group(2))

        return CasMeta(
            source=self.source,
            investor_name=name_match.group(1).strip() if name_match else None,
            pan=pan_match.group(1).upper() if pan_match else None,
            statement_period_start=start,
            statement_period_end=end,
            generated_on=self._parse_generated_on(metadata),
        )

    @staticmethod
    def _parse_generated_on(metadata: dict[str, Any]) -> date | None:
        """Parse the statement generation date from the PDF ``/CreationDate``.

        The CDSL CAS prints no generation date in the document body, so the most
        faithful source is the PDF metadata creation stamp
        (``D:YYYYMMDDHHmmSS+TZ``), which records when the depository generated
        the statement.

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
