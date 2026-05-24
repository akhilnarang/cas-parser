"""Reader for the NSDL CAS footnote/marker legend.

Unlike the CDSL CAS — which carries a free-text ``Note:`` block of doubled-
punctuation markers (``!! @@ ##``) — the NSDL CAS encodes its markers inline:

1. **Asset-class codes.** The PORTFOLIO COMPOSITION table prints each asset class
   with a parenthetical code (``Equities (E)``, ``Mutual Fund Folios (F)``,
   ``Government Securities (G)``, ``Sovereign Gold Bonds (SGB)``, ...). These
   codes ARE the document's own asset-class legend, parsed dynamically here into
   ``{code: "Asset class - <label>"}`` so the meanings are never hard-coded.
2. **Listing markers on holdings.** Each NSDL demat holding's ISIN cell carries a
   second line that is either the NSE/BSE stock symbol (e.g. ``SIEMENS.NSE``) or
   the standalone marker ``NOT LISTED`` for an unlisted security. ``NOT LISTED``
   is recorded as a holding flag with its meaning taken from the statement's own
   valuation note ("for unlisted securities, face value has been considered").

The parsed legend then drives which markers are recognised on each holding (see
``nsdl_demat.py``); only markers this reader defines are ever recorded.
"""

from __future__ import annotations

import re
from typing import Any

# "Equities (E) 90,307.50 38.20%" — the parenthetical code is the asset-class
# marker; the leading text is its human-readable label.
_COMPOSITION_CODE_RE = re.compile(
    r"^(?P<label>.+?)\s*\((?P<code>[A-Z]{1,3})\)\s+[\d,]+\.\d{2}\s+[\d.]+%\s*$"
)

# The unlisted-security marker printed in a holding's ISIN/stock-symbol cell.
NOT_LISTED_MARKER = "NOT LISTED"
# The statement's own explanation of the unlisted-security valuation basis.
_NOT_LISTED_DEFINITION = (
    "Not listed on a stock exchange; face value considered for valuation"
)


def parse_legend(pages: list[dict[str, Any]]) -> dict[str, str]:
    """Parse the NSDL marker legend into a ``{marker: definition}`` map.

    Collects the asset-class codes from the PORTFOLIO COMPOSITION rows (every
    class the statement lists, valued or not) plus the ``NOT LISTED`` holding
    marker.

    Args:
        pages: Page payloads to scan (the composition tables live near the
            front).

    Returns:
        A ``{marker: definition}`` map. Asset-class codes resolve to
        ``"Asset class - <label>"``; ``NOT LISTED`` resolves to the unlisted-
        security valuation note.
    """
    legend: dict[str, str] = {}
    for page in pages:
        for raw_line in page.get("text", "").split("\n"):
            match = _COMPOSITION_CODE_RE.match(raw_line.strip())
            if not match:
                continue
            code = match.group("code")
            label = match.group("label").strip()
            legend.setdefault(code, f"Asset class - {label}")

    legend.setdefault(NOT_LISTED_MARKER, _NOT_LISTED_DEFINITION)
    return legend


__all__ = ["NOT_LISTED_MARKER", "parse_legend"]
