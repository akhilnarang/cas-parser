"""Raw PDF extraction utilities.

This module is intentionally source-agnostic. It only handles:
- encrypted PDF detection/decryption (depository CAS PDFs are password
  protected — the password is the holder's PAN),
- metadata extraction,
- page-level text/word/table extraction.

CAS-specific parsing and normalization live under cas_parser/parsers/.
"""

import io
from pathlib import Path
from typing import Any, NamedTuple

import fitz
import pdfplumber
from pypdf import PdfReader, PdfWriter


def metadata_from_pypdf(reader: PdfReader) -> dict[str, str]:
    """Return normalized PDF metadata as string key/value pairs.

    Args:
        reader: Initialized `PdfReader` instance.

    Returns:
        Metadata dictionary with string keys and string values.
    """
    raw_metadata = reader.metadata or {}
    metadata: dict[str, str] = {}
    for key, value in raw_metadata.items():
        metadata[str(key)] = "" if value is None else str(value)
    return metadata


def is_pdf_encrypted(pdf_path: Path) -> bool:
    """Check whether a PDF requires a password.

    Args:
        pdf_path: Path to input PDF.

    Returns:
        True if the PDF is encrypted, else False.
    """
    reader = PdfReader(str(pdf_path))
    return bool(reader.is_encrypted)


def _decrypt_with_candidates(reader: PdfReader, passwords: list[str]) -> None:
    """Try each candidate password in order until one decrypts the reader.

    CAS password conventions vary slightly (PAN as entered vs uppercased), so
    the caller passes a list and the first one that works wins.

    Args:
        reader: Encrypted `PdfReader` instance.
        passwords: Candidate passwords to try in order.

    Raises:
        ValueError: If no candidate decrypts the PDF.
    """
    for password in passwords:
        if password and reader.decrypt(password) != 0:
            return
    raise ValueError("Failed to decrypt PDF. Check the password.")


class PreparedPdf(NamedTuple):
    """A (possibly encrypted) PDF readied for extraction.

    Attributes:
        pdf_bytes: Decrypted in-memory PDF bytes, or None when the input was not
            encrypted (in that case read it from disk directly).
        is_encrypted: Whether the source PDF was encrypted.
        was_decrypted: Whether decryption was performed.
        metadata: Normalized pypdf document metadata.
    """

    pdf_bytes: bytes | None
    is_encrypted: bool
    was_decrypted: bool
    metadata: dict[str, str]


def prepare_pdf_bytes_if_encrypted(
    pdf_path: Path, passwords: list[str] | None
) -> PreparedPdf:
    """Decrypt an encrypted PDF, returning in-memory bytes plus metadata.

    Args:
        pdf_path: Path to input PDF.
        passwords: Candidate passwords for encrypted PDFs (tried in order).

    Returns:
        A `PreparedPdf`; its `pdf_bytes` is None for non-encrypted inputs.

    Raises:
        ValueError: If the PDF is encrypted and no candidate password works.
    """
    reader = PdfReader(str(pdf_path))

    if not reader.is_encrypted:
        return PreparedPdf(
            pdf_bytes=None,
            is_encrypted=False,
            was_decrypted=False,
            metadata=metadata_from_pypdf(reader),
        )

    if not passwords:
        raise ValueError("PDF is encrypted. Password is required.")

    _decrypt_with_candidates(reader, passwords)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)

    return PreparedPdf(
        pdf_bytes=buffer.getvalue(),
        is_encrypted=True,
        was_decrypted=True,
        metadata=metadata_from_pypdf(reader),
    )


def extract_raw_pdf(pdf_path: Path, passwords: list[str] | None) -> dict[str, Any]:
    """Extract raw document structure used by parser modules.

    Args:
        pdf_path: Path to input PDF.
        passwords: Candidate passwords used only if the PDF is encrypted.

    Returns:
        Raw extraction dictionary with metadata and per-page content.
    """
    prepared = prepare_pdf_bytes_if_encrypted(pdf_path, passwords)

    document: dict[str, Any] = {
        "file": pdf_path.name,
        "source": "pdfplumber+pymupdf+pypdf",
        "metadata": {"pypdf": prepared.metadata},
        "encryption": {
            "is_encrypted": prepared.is_encrypted,
            "was_decrypted": prepared.was_decrypted,
        },
        "pages": [],
    }

    if prepared.pdf_bytes is None:
        plumber_context = pdfplumber.open(str(pdf_path))
        fitz_context = fitz.open(str(pdf_path))
    else:
        plumber_context = pdfplumber.open(io.BytesIO(prepared.pdf_bytes))
        fitz_context = fitz.open(stream=prepared.pdf_bytes, filetype="pdf")

    with plumber_context as plumber_doc, fitz_context as fitz_doc:
        document["page_count"] = len(plumber_doc.pages)
        document["metadata"]["pymupdf"] = fitz_doc.metadata

        for page_index, page in enumerate(plumber_doc.pages):
            document["pages"].append(
                {
                    "page_number": page_index + 1,
                    "width": page.width,
                    "height": page.height,
                    "text": page.extract_text() or "",
                    "words": page.extract_words() or [],
                    "tables": page.extract_tables() or [],
                }
            )

    return document


__all__ = [
    "PreparedPdf",
    "extract_raw_pdf",
    "is_pdf_encrypted",
    "metadata_from_pypdf",
    "prepare_pdf_bytes_if_encrypted",
]
