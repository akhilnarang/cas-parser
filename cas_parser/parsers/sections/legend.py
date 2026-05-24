"""Dynamic reader for the CDSL CAS footnote legend ("Note:" block).

The statement attaches terse footnote markers to securities (next to the ISIN,
e.g. ``INE0...!!``) and explains them in a small free-text legend introduced by a
standalone ``Note:`` line. The markers are doubled punctuation pairs
(``!! @@ $$ ** ##``, occasionally a single char) and each is followed by its
plain-English meaning.

This reader parses that block **dynamically** into a ``{marker: definition}``
map; nothing about the meanings is hard-coded. The parsed marker set then drives
which footnotes are recognised on each holding (see ``demat.py`` /
``mf_folio.py``), so a punctuation mark is treated as a marker *only* when the
legend actually defines it — the ``#`` AMC/scheme separator, which the legend
never defines, stays part of the security name.

Layout & parsing strategy
-------------------------
The legend prints as a multi-column grid: each ``marker definition`` cell sits in
a column band and its definition can wrap onto further lines within that band,
so adjacent columns interleave in the linear text layer. Two strategies handle
this:

1. **Coordinate strategy (preferred).** When the page exposes a positioned
   ``words`` layer, words are grouped into column bands by the x-position of the
   marker tokens, and each marker's definition is read down its own band until
   the next marker in that band. This reconstructs wrapped, interleaved
   definitions exactly.
2. **Text fallback.** When no ``words`` layer is present, the ``Note:`` text
   block is split on marker tokens in reading order. This is exact for a simple
   one-marker-per-line legend and degrades gracefully otherwise.
"""

from __future__ import annotations

import re
from typing import Any

# A standalone "Note" / "Note:" anchor line/word that opens the legend block.
# It must be the whole token — the document's numbered "Notes:" / "NOTES TO CAS"
# blocks carry trailing text and are deliberately not matched.
_NOTE_ANCHOR_RE = re.compile(r"^\s*Note\s*:?\s*$", re.IGNORECASE)

# Footnote marker tokens, longest first so doubled pairs win over singles.
# Doubled punctuation pairs are the CDSL convention; single chars are allowed for
# forward-compatibility. ``#`` is included so a legend that *defines* ``#`` is
# honoured, but it is only ever recorded when the parsed legend contains it.
_MARKER_ALTERNATION = (
    r"!!|@@|\$\$|\*\*|##|\^\^|~~|\+\+|"  # doubled pairs
    r"!|@|\$|\*|#|\^|~|\+"  # singles
)
_MARKER_AT_START_RE = re.compile(rf"^({_MARKER_ALTERNATION})")
# Column-band clustering tolerance (points): marker x0s within this are one band.
_BAND_TOLERANCE = 40.0


def _is_garbled(text: str) -> bool:
    """True when a token carries non-Latin glyphs (Devanagari / bilingual junk).

    The legend grid is clean ASCII; the bilingual boilerplate that follows it in
    the text layer is interleaved Devanagari, which marks the end of the legend.
    """
    return any(ord(ch) > 0x2000 for ch in text)


def _parse_from_words(words: list[dict[str, Any]], note_top: float) -> dict[str, str]:
    """Reconstruct the legend from a positioned ``words`` layer.

    Groups legend words into column bands by the x-position of the marker tokens
    and reads each marker's definition down its own band, so wrapped and
    column-interleaved definitions are recovered exactly.

    Args:
        words: The page's positioned word dicts (``text``/``x0``/``top``).
        note_top: The ``top`` coordinate of the ``Note:`` anchor word.

    Returns:
        A ``{marker: definition}`` map for this page's legend block.
    """
    # Legend words sit below the anchor; stop at the first garbled (bilingual)
    # line, which ends the legend grid.
    garbled_tops = sorted(
        w["top"] for w in words if w["top"] > note_top and _is_garbled(w["text"])
    )
    cutoff = garbled_tops[0] if garbled_tops else float("inf")
    region = [
        w
        for w in words
        if w["top"] > note_top - 1.0
        and w["top"] < cutoff
        and not _NOTE_ANCHOR_RE.match(w["text"])
        and not _is_garbled(w["text"])
    ]
    if not region:
        return {}

    marker_words = [
        (w, m.group(1)) for w in region if (m := _MARKER_AT_START_RE.match(w["text"]))
    ]
    if not marker_words:
        return {}

    # Cluster marker x0s into column bands, then map each band to an x-range.
    marker_xs = sorted({mw["x0"] for mw, _ in marker_words})
    bands: list[list[float]] = []
    for x in marker_xs:
        if bands and x - bands[-1][-1] <= _BAND_TOLERANCE:
            bands[-1].append(x)
        else:
            bands.append([x])
    band_ranges: list[tuple[float, float]] = []
    for i, band in enumerate(bands):
        lo = min(band) - 5.0
        hi = (min(bands[i + 1]) - 5.0) if i + 1 < len(bands) else float("inf")
        band_ranges.append((lo, hi))

    def band_of(x: float) -> tuple[float, float]:
        for lo, hi in band_ranges:
            if lo <= x < hi:
                return (lo, hi)
        return band_ranges[-1]

    # Bucket every region word into its column band, ordered top-to-bottom.
    by_band: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for w in region:
        by_band.setdefault(band_of(w["x0"]), []).append(w)
    for band_words in by_band.values():
        band_words.sort(key=lambda w: (round(w["top"], 0), w["x0"]))

    legend: dict[str, str] = {}
    for mw, marker in marker_words:
        band_words = by_band[band_of(mw["x0"])]
        # Read down this band from the marker until the next marker below it.
        lower_marker_tops = sorted(
            w["top"]
            for w in band_words
            if w["top"] > mw["top"] + 1.0 and _MARKER_AT_START_RE.match(w["text"])
        )
        stop_top = lower_marker_tops[0] if lower_marker_tops else float("inf")
        chosen = [w for w in band_words if mw["top"] - 0.5 <= w["top"] < stop_top]
        chosen.sort(key=lambda w: (round(w["top"], 1), w["x0"]))

        tokens: list[str] = []
        for w in chosen:
            text = w["text"]
            if w is mw:
                # Strip the leading marker from its own first word.
                text = text[len(marker) :]
            if text:
                tokens.append(text)
        definition = " ".join(tokens).strip()
        if definition:
            legend.setdefault(marker, definition)
    return legend


def _parse_from_text(text: str) -> dict[str, str]:
    """Fallback legend parse from the ``Note:`` block's linear text.

    Splits the post-``Note:`` text on marker tokens in reading order. Exact for a
    one-marker-per-line legend; a reasonable best-effort when columns interleave.

    Args:
        text: The full page text containing the ``Note:`` anchor.

    Returns:
        A ``{marker: definition}`` map.
    """
    lines = text.split("\n")
    note_index = next(
        (i for i, line in enumerate(lines) if _NOTE_ANCHOR_RE.match(line)), None
    )
    if note_index is None:
        return {}
    block_lines: list[str] = []
    for line in lines[note_index + 1 :]:
        if _is_garbled(line):
            break  # bilingual boilerplate ends the legend
        block_lines.append(line.strip())
    block = " ".join(filter(None, block_lines))
    if not block:
        return {}

    split_re = re.compile(rf"({_MARKER_ALTERNATION})")
    parts = split_re.split(block)
    legend: dict[str, str] = {}
    i = 1  # parts[0] is text before the first marker
    while i < len(parts) - 1:
        marker = parts[i]
        definition = parts[i + 1].strip()
        if definition:
            legend.setdefault(marker, definition)
        i += 2
    return legend


def detect_markers(cell: str, legend: dict[str, str]) -> tuple[list[str], str]:
    """Find legend markers attached to a cell and return them plus the clean cell.

    Only markers the parsed ``legend`` actually defines are recognised, so a
    character such as the ``#`` AMC/scheme separator is treated as a marker *only*
    when the legend defines ``#`` — otherwise it stays in the returned text. This
    keeps undefined punctuation from being stripped out of names/ISINs.

    Markers are matched longest-first (so ``!!`` wins over ``!``) and may repeat;
    each distinct marker is reported once, in the order the legend lists them.

    Args:
        cell: The raw ISIN / name / scheme cell text.
        legend: The parsed ``{marker: definition}`` map.

    Returns:
        ``(flags, cleaned)`` — the distinct markers found (in legend order) and
        the cell text with every found marker occurrence removed.
    """
    if not cell or not legend:
        return [], cell
    # Longest markers first so a doubled pair is consumed before its single char.
    markers: list[str] = list(legend)
    markers.sort(key=len, reverse=True)
    cleaned = cell
    found: set[str] = set()
    for marker in markers:
        if marker in cleaned:
            found.add(marker)
            cleaned = cleaned.replace(marker, "")
    # Report in the legend's own ordering for stable, meaningful output.
    flags = [marker for marker in legend if marker in found]
    return flags, cleaned


def resolve_notes(flags: list[str], legend: dict[str, str]) -> str | None:
    """Resolve a holding's markers into a human-readable note from the legend.

    Args:
        flags: Markers found on the holding (from :func:`detect_markers`).
        legend: The parsed ``{marker: definition}`` map.

    Returns:
        The joined legend definitions for the markers, or None when there are no
        markers (or none resolve).
    """
    notes = [legend[flag] for flag in flags if flag in legend]
    return "; ".join(notes) if notes else None


def parse_legend(pages: list[dict[str, Any]]) -> dict[str, str]:
    """Parse the CDSL footnote legend into a ``{marker: definition}`` map.

    Locates the standalone ``Note:`` anchor and reconstructs the marker
    definitions, preferring the positioned ``words`` layer (exact for wrapped /
    interleaved columns) and falling back to linear text when no words exist.

    Args:
        pages: Page payloads to scan (the legend lives on one of them).

    Returns:
        The parsed legend; empty when no legend block is present.
    """
    legend: dict[str, str] = {}
    for page in pages:
        words = page.get("words") or []
        note_word = next(
            (w for w in words if _NOTE_ANCHOR_RE.match(w["text"])), None
        )
        page_legend: dict[str, str] = {}
        if note_word is not None:
            page_legend = _parse_from_words(words, note_word["top"])
        if not page_legend:
            page_legend = _parse_from_text(page.get("text", ""))
        for marker, definition in page_legend.items():
            legend.setdefault(marker, definition)
    return legend


__all__ = ["detect_markers", "parse_legend", "resolve_notes"]
