"""Parser selection and source detection.

The CAS issuer is detected with a two-tier, document-level scheme — reading the
depository's own identity rather than running a content heuristic over holdings,
so it is reliable: an NSDL CAS is produced by NSDL and a CDSL CAS by CDSL
regardless of whose securities they list.

1. PRIMARY: PDF metadata. Each parser declares ``metadata_markers`` — distinctive
   multi-word strings from the PDF's ``/Title``/``/Author``/``/Creator``/etc.
   Metadata is structured and survives even when the page text layer is garbled
   or encrypted, so it is the first signal checked.
2. FALLBACK: page-1 title text. Each parser declares ``title_markers`` matched
   against the extracted page-1 text. Used only when the metadata does not
   resolve the issuer.
"""

from typing import Any

from cas_parser.parsers.base import CasParser
from cas_parser.parsers.registry import (
    PARSER_REGISTRY,
    create_parser,
    get_supported_source_slugs,
)


def get_parser(source: str) -> CasParser:
    """Return parser instance for the given CAS source slug."""
    return create_parser(source)


def detect_source(raw_data: dict[str, Any]) -> str:
    """Identify the CAS issuer ("nsdl" | "cdsl") from PDF metadata, then text.

    Detection is two-tier and document-level:

    1. PRIMARY — PDF metadata. The values of *every* ``raw_data["metadata"]``
       sub-dict (``pypdf``, ``pymupdf``, ...) are joined into one searchable blob
       and matched (case-insensitively) against each parser's
       ``metadata_markers``. Metadata is structured and survives
       garbled/encrypted text layers, so it is tried first; joining all blocks
       means detection still resolves when ``pypdf`` is empty but another block
       carries the title. If exactly one parser matches, its slug is returned.
    2. FALLBACK — page-1 title text. When the metadata does not resolve a single
       issuer, the extracted page-1 text is matched against each parser's
       ``title_markers``.

    Args:
        raw_data: Raw extractor payload.

    Returns:
        The detected source slug.

    Raises:
        ValueError: If neither signal resolves a single issuer (no match or an
            ambiguous match).
    """
    metadata_match = _match_by_metadata(raw_data)
    if metadata_match is not None:
        return metadata_match

    text_match = _match_by_title_text(raw_data)
    if text_match is not None:
        return text_match

    raise ValueError(
        "Could not identify the CAS issuer "
        f"(expected one of {get_supported_source_slugs()}). "
        "Tried PDF metadata markers and the page-1 title text, but neither "
        "resolved a single issuer (no match or ambiguous). "
        "Use --raw-only to inspect the extracted metadata and text."
    )


def _match_by_metadata(raw_data: dict[str, Any]) -> str | None:
    """Return the slug whose ``metadata_markers`` match the PDF metadata.

    Builds one case-insensitive blob from the values of *every* metadata
    sub-dict (``pypdf``, ``pymupdf``, and any others) and returns the matching
    slug only when exactly one parser matches; ``None`` for no match or an
    ambiguous (multi-parser) match, so detection can fall back to the page-1
    title text. Joining all blocks means detection still works when ``pypdf`` is
    empty but another extractor (e.g. ``pymupdf``) carries the title/creator.
    """
    metadata = raw_data.get("metadata") or {}
    values: list[str] = []
    for block in metadata.values():
        if isinstance(block, dict):
            values.extend(str(value) for value in block.values())
    blob = " ".join(values).upper()
    if not blob:
        return None

    matches = [
        slug
        for slug, parser_class in PARSER_REGISTRY.items()
        if any(m.upper() in blob for m in parser_class.metadata_markers)
    ]
    return matches[0] if len(matches) == 1 else None


def _match_by_title_text(raw_data: dict[str, Any]) -> str | None:
    """Return the slug whose ``title_markers`` match the page-1 text.

    Returns the matching slug only when exactly one parser matches; ``None`` for
    no match or an ambiguous (multi-parser) match.
    """
    pages = raw_data.get("pages") or []
    first_page_text = (pages[0].get("text", "") if pages else "").upper()
    if not first_page_text:
        return None

    matches = [
        slug
        for slug, parser_class in PARSER_REGISTRY.items()
        if any(m.upper() in first_page_text for m in parser_class.title_markers)
    ]
    return matches[0] if len(matches) == 1 else None


def get_parser_for(raw_data: dict[str, Any]) -> CasParser:
    """Detect the source from a raw payload and return its parser."""
    return get_parser(detect_source(raw_data))


__all__ = ["detect_source", "get_parser", "get_parser_for"]
