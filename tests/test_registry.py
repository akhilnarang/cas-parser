"""Registry / factory wiring tests."""

from __future__ import annotations

import pytest

from cas_parser.parsers.cdsl_ecas import CdslEcasParser
from cas_parser.parsers.factory import get_parser
from cas_parser.parsers.nsdl_cas import NsdlCasParser
from cas_parser.parsers.registry import create_parser, get_supported_source_slugs


def test_supported_slugs() -> None:
    assert get_supported_source_slugs() == ("nsdl", "cdsl")


def test_factory_returns_parser_instances() -> None:
    assert isinstance(get_parser("nsdl"), NsdlCasParser)
    assert isinstance(get_parser("cdsl"), CdslEcasParser)


def test_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported CAS source"):
        create_parser("bogus")


def test_nsdl_parser_handles_empty_payload_gracefully() -> None:
    # The NSDL parser is implemented (see tests/test_nsdl.py for the full
    # fixture). On a trivial payload it must not raise — it returns an empty but
    # valid statement with reconciliation attached.
    statement = get_parser("nsdl").parse({"file": "empty.pdf", "pages": []})
    assert statement.meta.source == "nsdl"
    assert statement.accounts == []
    assert statement.folios == []
    assert statement.reconciliation is not None
