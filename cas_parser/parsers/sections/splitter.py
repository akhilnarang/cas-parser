"""Locate logical sections within a CAS by scanning page text for anchors.

Depository CAS PDFs have no tagged structure; sections are separated only by
visual header strings ("DEPOSITORY ACCOUNT DETAILS", "MUTUAL FUND FOLIOS",
...). This splitter finds the pages where named anchors appear so a parser can
slice the document into section page-ranges.

The mechanism is source-agnostic; the actual anchor strings live with each
source coordinator (`nsdl_cas`, `cdsl_ecas`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Section:
    """A contiguous page range identified by name."""

    name: str
    start_page: int  # 0-based index into the pages list, inclusive
    end_page: int  # exclusive

    def pages(self, all_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the page payloads covered by this section."""
        return all_pages[self.start_page : self.end_page]


class SectionSplitter:
    """Split a page list into named sections by anchor-string matching.

    Args:
        pages: The `raw_data["pages"]` list from the extractor.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self._page_text = [page.get("text", "").upper() for page in pages]

    def find_page(self, anchors: tuple[str, ...], start: int = 0) -> int | None:
        """Return the index of the first page (>= start) matching any anchor."""
        for index in range(start, len(self._page_text)):
            text = self._page_text[index]
            if any(anchor.upper() in text for anchor in anchors):
                return index
        return None

    def split(self, anchors_by_name: dict[str, tuple[str, ...]]) -> list[Section]:
        """Resolve ordered sections from a name -> anchors mapping.

        Each section runs from the page where its first anchor matches up to the
        start of the next matched section (or end of document). Sections whose
        anchors are never found are omitted.

        Args:
            anchors_by_name: Insertion-ordered map of section name to the anchor
                strings that mark its start.

        Returns:
            Sections in page order.
        """
        found: list[tuple[str, int]] = []
        for name, anchors in anchors_by_name.items():
            page_index = self.find_page(anchors)
            if page_index is not None:
                found.append((name, page_index))

        found.sort(key=lambda item: item[1])

        sections: list[Section] = []
        for position, (name, start_page) in enumerate(found):
            next_start = (
                found[position + 1][1] if position + 1 < len(found) else len(self.pages)
            )
            # When two anchors resolve to the same page, the boundary page holds
            # the start of both sections — include it in each rather than
            # yielding an empty range for the earlier one.
            end_page = max(next_start, start_page + 1)
            sections.append(Section(name=name, start_page=start_page, end_page=end_page))
        return sections


__all__ = ["Section", "SectionSplitter"]
