"""Parser interface for source-specific CAS normalization."""

from abc import ABC, abstractmethod
from typing import Any

from cas_parser.models import (
    CasStatement,
    CasSummary,
    DematAccount,
    MfFolio,
)
from cas_parser.parsers.reconciliation import build_reconciliation


class CasParser(ABC):
    """Base contract for all CAS source parser implementations."""

    source: str = "generic"
    # Distinctive PDF-metadata strings that identify this document. These are the
    # PRIMARY detect_source signal: metadata is structured and reliable even when
    # the page text layer is garbled/encrypted. Must be multi-word and unique to
    # the issuer (NSDL's /Keywords lists both "NSDL" and "CDSL", so bare slugs
    # would cross-match — see NsdlCasParser/CdslEcasParser).
    metadata_markers: tuple[str, ...] = ()
    # Page-1 title strings that identify this document. FALLBACK detect_source
    # signal, used only when the metadata markers do not resolve the issuer.
    title_markers: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, raw_data: dict[str, Any]) -> CasStatement:
        """Convert a raw extractor payload into a normalized CAS statement.

        Args:
            raw_data: Raw extraction payload from the extractor module.

        Returns:
            Normalized parser output as a `CasStatement` model.
        """
        raise NotImplementedError

    def _attach_reconciliation(self, statement: CasStatement) -> CasStatement:
        """Compute and attach the holdings reconciliation to a statement.

        Call this at the end of `parse()` once accounts, folios and summary are
        populated. `reconciliation.portfolio_ok` (and per-scope `ok`) is the
        primary correctness signal for the parse.
        """
        statement.reconciliation = build_reconciliation(
            statement.accounts,
            statement.folios,
            statement.summary,
        )
        return statement

    @staticmethod
    def _empty_statement(file_name: str, source: str) -> CasStatement:
        """Build a minimal statement shell for a parser to populate."""
        from cas_parser.models import CasMeta

        return CasStatement(
            file=file_name,
            meta=CasMeta(source=source),
            accounts=[],
            folios=[],
            summary=CasSummary(),
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Return lightweight debug details; subclasses may extend."""
        return {
            "source": self.source,
            "page_count": raw_data.get("page_count", 0),
            "extraction": raw_data.get("source"),
        }


# Re-exported for parsers that build sections directly.
__all__ = ["CasParser", "DematAccount", "MfFolio"]
