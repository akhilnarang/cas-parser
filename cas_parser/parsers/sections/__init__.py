"""Shared section readers.

NSDL CAS and CDSL eCAS share the same logical sections (summary, demat
holdings, MF folios) even though their page layouts differ. The source
coordinators (`nsdl_cas`, `cdsl_ecas`) compose these readers rather than
duplicating section-parsing logic.
"""

from cas_parser.parsers.sections.splitter import Section, SectionSplitter

__all__ = ["Section", "SectionSplitter"]
