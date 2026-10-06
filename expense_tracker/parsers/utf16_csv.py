"""
UTF-16 CSV bank statement parser.

Wraps the existing parser.py functions into the BankParser protocol.
This is the original parser — all existing functionality is preserved.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..constants import DEFAULT_CARDS
from ..parser import (
    HEADER_PREFIX,
    _decode_utf16,
    extract_date_range,
    parse_utf16_csv,
)


class Utf16CsvParser:
    """Parser for UTF-16 CSV exports."""

    @property
    def name(self) -> str:
        return "UTF-16 CSV"

    @property
    def bank_id(self) -> str:
        return "utf16"

    def can_parse(self, path: Path) -> bool:
        """
        Check if the file looks like a UTF-16 CSV export.

        Looks for the characteristic header line and encoding.
        """
        if path.suffix.lower() != ".csv":
            return False
        try:
            lines = _decode_utf16(path)
            return any(ln.startswith(HEADER_PREFIX) for ln in lines)
        except Exception:
            return False

    def parse(self, path: Path, cards_path: Path = DEFAULT_CARDS) -> list[dict]:
        """Parse a UTF-16 CSV file into transaction dicts."""
        return parse_utf16_csv(path, cards_path=cards_path)

    def extract_date_range(self, path: Path) -> tuple[date, date]:
        """Extract the statement date range from the UTF-16 CSV header."""
        return extract_date_range(path)
