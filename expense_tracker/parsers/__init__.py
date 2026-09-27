"""
Multi-bank parser framework.

Provides a BankParser protocol, a registry of available parsers,
and auto-detection to route statement files to the correct parser.

Each bank has its own parser module under expense_tracker.parsers.*.
The parsers produce a common list[dict] transaction format consumed
by the rest of the pipeline (database, rules, reports).

Usage:
    from expense_tracker.parsers import detect_parser, parse_statement

    parser = detect_parser(path)        # auto-detect bank
    rows = parser.parse(path)           # parse transactions
    d1, d2 = parser.extract_date_range(path)  # date range
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ..constants import DEFAULT_CARDS


# ---------------------------------------------------------------------------
# Parser protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BankParser(Protocol):
    """Protocol that all bank parsers must implement."""

    @property
    def name(self) -> str:
        """Human-readable bank name (e.g. 'UTF-16 CSV')."""
        ...

    @property
    def bank_id(self) -> str:
        """Short identifier for CLI (e.g. 'utf16', 'utf8')."""
        ...

    def can_parse(self, path: Path) -> bool:
        """Return True if this parser can handle the given file."""
        ...

    def parse(self, path: Path, cards_path: Path = DEFAULT_CARDS) -> list[dict]:
        """Parse the file and return a list of transaction dicts."""
        ...

    def extract_date_range(self, path: Path) -> tuple[date, date]:
        """Extract the statement date range from the file."""
        ...


# ---------------------------------------------------------------------------
# Parser registry
# ---------------------------------------------------------------------------

_registry: list[BankParser] = []


def register_parser(parser: BankParser) -> None:
    """Register a bank parser in the global registry."""
    _registry.append(parser)


def get_registered_parsers() -> list[BankParser]:
    """Return all registered parsers."""
    return list(_registry)


def get_parser_by_id(bank_id: str) -> Optional[BankParser]:
    """Look up a parser by its bank_id."""
    for p in _registry:
        if p.bank_id == bank_id:
            return p
    return None


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def detect_parser(path: Path) -> Optional[BankParser]:
    """
    Auto-detect which bank parser can handle the given file.

    Tries each registered parser's can_parse() in registration order.
    Returns None if no parser matches.
    """
    for parser in _registry:
        try:
            if parser.can_parse(path):
                return parser
        except Exception:
            continue
    return None


def parse_statement(
    path: Path,
    cards_path: Path = DEFAULT_CARDS,
    bank_id: str | None = None,
) -> list[dict]:
    """
    Parse a bank statement file using auto-detection or a specific bank.

    Args:
        path: Path to the statement file (CSV, PDF, etc.)
        cards_path: Path to the card holders CSV.
        bank_id: Optional bank identifier to skip auto-detection.

    Returns:
        List of transaction dicts in the standard format.

    Raises:
        ValueError: If no parser can handle the file.
    """
    if bank_id:
        parser = get_parser_by_id(bank_id)
        if parser is None:
            available = ", ".join(p.bank_id for p in _registry)
            raise ValueError(
                f"Unknown bank '{bank_id}'. Available: {available or 'none'}"
            )
    else:
        parser = detect_parser(path)

    if parser is None:
        available = ", ".join(p.bank_id for p in _registry)
        raise ValueError(
            f"Could not detect bank format for {path.name}. "
            f"Available parsers: {available or 'none'}. "
            f"Use --bank to specify explicitly."
        )

    return parser.parse(path, cards_path=cards_path)


# ---------------------------------------------------------------------------
# Auto-register built-in parsers
# ---------------------------------------------------------------------------

def _auto_register() -> None:
    """Import and register all built-in bank parsers."""
    from .utf16_csv import Utf16CsvParser
    from .utf8_csv import Utf8CsvParser

    register_parser(Utf16CsvParser())
    register_parser(Utf8CsvParser())


_auto_register()
