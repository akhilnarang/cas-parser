"""Source-detection tests.

Detection is metadata-primary with a page-1 title-text fallback. The metadata
strings below are generic depository boilerplate (no PII).
"""

from __future__ import annotations

import pytest

from cas_parser.parsers.factory import detect_source


def _raw_text(page1_text: str) -> dict:
    """Payload with page-1 text only (no metadata) — exercises the fallback."""
    return {"pages": [{"text": page1_text}]}


def _raw_metadata(pypdf: dict[str, str]) -> dict:
    """Payload with PDF metadata only (no page text) — exercises the primary."""
    return {"metadata": {"pypdf": pypdf}, "pages": []}


# --- PRIMARY: PDF metadata --------------------------------------------------


def test_detect_cdsl_via_metadata() -> None:
    raw = _raw_metadata({"/Title": "Central Depository Services (India) Ltd"})
    assert detect_source(raw) == "cdsl"


def test_detect_via_pymupdf_metadata_when_pypdf_empty() -> None:
    # Fix 6 regression: detection must join *all* metadata sub-dicts, so a title
    # carried only by the pymupdf block still resolves when pypdf is empty.
    nsdl = {
        "metadata": {
            "pypdf": {},
            "pymupdf": {"title": "NSDL-Consolidated Account Statement"},
        },
        "pages": [],
    }
    assert detect_source(nsdl) == "nsdl"

    cdsl = {
        "metadata": {
            "pypdf": {},
            "pymupdf": {"title": "Central Depository Services (India) Ltd"},
        },
        "pages": [],
    }
    assert detect_source(cdsl) == "cdsl"


def test_detect_nsdl_via_metadata_with_cdsl_keyword_trap() -> None:
    # The real NSDL /Keywords value lists BOTH "NSDL" and "CDSL"; the
    # distinctive multi-word markers must keep this unambiguous (-> "nsdl").
    raw = _raw_metadata(
        {
            "/Author": "NSDL",
            "/Creator": "NSDL-CAS Team",
            "/Title": "NSDL-Consolidated Account Statement",
            "/Subject": "This is Consolidated Account Statement",
            "/Keywords": "Metadata, Summary, SOH, SOT, MF, NSDL, CDSL",
        }
    )
    assert detect_source(raw) == "nsdl"


# --- FALLBACK: page-1 title text --------------------------------------------


def test_detect_nsdl_via_title_fallback() -> None:
    text = (
        "National Securities Depository Limited\n"
        "Consolidated Account Statement for the month of April 2026"
    )
    assert detect_source(_raw_text(text)) == "nsdl"


def test_detect_cdsl_via_title_fallback() -> None:
    text = (
        "Central Depository Services (India) Limited\n"
        "CONSOLIDATED ACCOUNT STATEMENT (CAS) FOR SECURITIES HELD IN DEMAT FORM "
        "AND INVESTMENTS IN MUTUAL FUNDS"
    )
    assert detect_source(_raw_text(text)) == "cdsl"


def test_metadata_takes_precedence_over_text() -> None:
    # CDSL metadata + NSDL title text: metadata is the primary signal.
    raw = {
        "metadata": {
            "pypdf": {"/Title": "Central Depository Services (India) Ltd"}
        },
        "pages": [{"text": "National Securities Depository Limited"}],
    }
    assert detect_source(raw) == "cdsl"


# --- None / ambiguous -------------------------------------------------------


def test_detect_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Could not identify"):
        detect_source(_raw_text("some unrelated document"))

    with pytest.raises(ValueError, match="Could not identify"):
        detect_source({"pages": []})
