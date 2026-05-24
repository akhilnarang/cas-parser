"""Parser registry and supported source slug helpers."""

from __future__ import annotations

from cas_parser.parsers.base import CasParser
from cas_parser.parsers.cdsl_ecas import CdslEcasParser
from cas_parser.parsers.nsdl_cas import NsdlCasParser

PARSER_REGISTRY: dict[str, type[CasParser]] = {
    "nsdl": NsdlCasParser,
    "cdsl": CdslEcasParser,
}


def get_supported_source_slugs() -> tuple[str, ...]:
    """Return registered source slugs in CLI/display order."""
    return tuple(PARSER_REGISTRY.keys())


def create_parser(source: str) -> CasParser:
    """Instantiate a parser from the registry."""
    try:
        parser_class = PARSER_REGISTRY[source]
    except KeyError as error:
        raise ValueError(f"Unsupported CAS source: {source}") from error
    return parser_class()


__all__ = [
    "PARSER_REGISTRY",
    "create_parser",
    "get_supported_source_slugs",
]
