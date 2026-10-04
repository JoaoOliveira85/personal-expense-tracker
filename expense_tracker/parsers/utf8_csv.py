"""
UTF-8 CSV bank statement parser.

UTF8 CSV exports differ from UTF-16:
- UTF-8 encoding (usually)
- Different header: "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico;..."
- Separate debit/credit columns instead of a single amount
- Different date separators (may use dd-mm-YYYY or dd/mm/YYYY)
- Semicolon delimiters (same as UTF-16)
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..constants import DEFAULT_CARDS, DOW_NAMES
from ..parser import (
    clean_description, detect_card, detect_payment_type, load_card_holders,
    parse_amount,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UTF8-specific patterns
# ---------------------------------------------------------------------------

# Multiple possible header formats
UTF8_HEADERS = [
    "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo",
    "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico",
    "Data movimento;Data valor;Descrição;Débito;Crédito;Saldo",
]

UTF8_HEADER_KEYWORDS = {"data mov", "débito", "crédito", "saldo"}

ROW_RE = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{4};")
DATE_RANGE_RE = re.compile(
    r"(?:Período|Periodo|De)\s*:?\s*(\d{2}[-/]\d{2}[-/]\d{4})\s*(?:a|até|-)\s*(\d{2}[-/]\d{2}[-/]\d{4})"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_utf8(path: Path) -> list[str]:
    """Read a UTF8 CSV file and return decoded lines."""
    raw_bytes = path.read_bytes()

    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(encoding, errors="strict")
            text_lower = text.lower()
            if "débito" in text_lower or "debito" in text_lower:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        text = raw_bytes.decode("utf-8", errors="replace")

    return [ln.strip() for ln in text.splitlines()]


def _is_utf8_header(line: str) -> bool:
    """Check if a line looks like a UTF8 table header."""
    lower = line.lower()
    matches = sum(1 for kw in UTF8_HEADER_KEYWORDS if kw in lower)
    return matches >= 3


def _parse_date(s: str) -> Optional[date]:
    """Parse a date in dd-mm-YYYY or dd/mm/YYYY format."""
    for sep in ("-", "/"):
        try:
            return datetime.strptime(s.strip(), f"%d{sep}%m{sep}%Y").date()
        except ValueError:
            continue
    return None


def _parse_amount(s: str) -> Optional[float]:
    """Parse an amount ('1.234,56', '45,50', '45.50'); None if empty or invalid."""
    if not s.strip():
        return None
    try:
        return parse_amount(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class Utf8CsvParser:
    """Parser for UTF-8 CSV CSV exports."""

    @property
    def name(self) -> str:
        return "UTF-8 CSV"

    @property
    def bank_id(self) -> str:
        return "utf8"

    def can_parse(self, path: Path) -> bool:
        """Check if the file looks like a UTF8 CSV export."""
        if path.suffix.lower() != ".csv":
            return False
        try:
            lines = _decode_utf8(path)
            return any(_is_utf8_header(ln) for ln in lines[:20])
        except Exception:
            return False

    def parse(self, path: Path, cards_path: Path = DEFAULT_CARDS) -> list[dict]:
        """Parse a UTF8 CSV export into transaction dicts."""
        card_owners = load_card_holders(cards_path)
        known_cards = set(card_owners.keys())

        lines = _decode_utf8(path)

        # Find header line
        header_idx = None
        for i, ln in enumerate(lines):
            if _is_utf8_header(ln):
                header_idx = i
                break

        if header_idx is None:
            raise ValueError(f"Could not find UTF8 header row in {path.name}")

        # Parse header to find column indices
        header_parts = [h.strip().lower() for h in lines[header_idx].split(";")]
        col_map = self._map_columns(header_parts)

        rows: list[dict] = []
        for line_no, ln in enumerate(lines[header_idx + 1:], start=header_idx + 2):
            if not ln:
                continue
            if not ROW_RE.match(ln):
                # Could be footer or empty line
                if rows:  # We've already parsed some rows
                    unread = sum(1 for later in lines[line_no:] if ROW_RE.match(later))
                    if unread:
                        logger.warning(
                            "%s line %d: stopped reading at %r; %d later line(s) "
                            "that look like transactions were not imported",
                            path.name, line_no, ln[:60], unread,
                        )
                    break
                continue

            parts = next(csv.reader([ln], delimiter=";"))
            tx = self._parse_row(
                parts, col_map, known_cards, card_owners, path.name, line_no
            )
            if tx:
                rows.append(tx)

        return rows

    def extract_date_range(self, path: Path) -> tuple[date, date]:
        """Extract the statement date range from the UTF8 CSV."""
        lines = _decode_utf8(path)
        full_text = "\n".join(lines[:20])  # Date range is typically in the header

        m = DATE_RANGE_RE.search(full_text)
        if m:
            d1 = _parse_date(m.group(1))
            d2 = _parse_date(m.group(2))
            if d1 and d2:
                return d1, d2

        # Fall back to min/max transaction dates
        rows = self.parse(path)
        if not rows:
            raise ValueError(f"Could not find date range in {path.name}")
        dates = [datetime.fromisoformat(r["date_posted"]).date() for r in rows]
        return min(dates), max(dates)

    def _map_columns(self, header_parts: list[str]) -> dict[str, int]:
        """Map column names to indices."""
        mapping: dict[str, int] = {}
        for i, h in enumerate(header_parts):
            if h in ("data mov.", "data mov", "data movimento"):
                mapping["date_posted"] = i
            elif h in ("data valor", "data val", "data val."):
                mapping["date_value"] = i
            elif h in ("descrição", "descricao", "descrição do movimento"):
                mapping["description"] = i
            elif h in ("débito", "debito"):
                mapping["debit"] = i
            elif h in ("crédito", "credito"):
                mapping["credit"] = i
            elif h.startswith("saldo"):
                mapping.setdefault("balance", i)  # take first saldo column
        return mapping

    def _parse_row(
        self,
        parts: list[str],
        col_map: dict[str, int],
        known_cards: set[str],
        card_owners: dict[str, str],
        source_file: str,
        line_no: int = 0,
    ) -> Optional[dict]:
        """Parse a single CSV row into a transaction dict.

        A row that cannot be imported is reported with a warning and skipped.
        """

        def _get(key: str) -> str:
            idx = col_map.get(key)
            if idx is None or idx >= len(parts):
                return ""
            return parts[idx].strip()

        def _skip(reason: str) -> None:
            logger.warning(
                "%s line %d: row not imported (%s): %s",
                source_file, line_no, reason, ";".join(parts),
            )

        # Date
        posted = _parse_date(_get("date_posted"))
        if not posted:
            return _skip("invalid date")

        value_str = _get("date_value")
        value = _parse_date(value_str) if value_str else posted
        if not value:
            return _skip("invalid value date")

        # Description
        desc = _get("description")
        if not desc:
            return _skip("no description")

        # Amount (UTF8 uses separate debit/credit columns)
        debit_s, credit_s = _get("debit"), _get("credit")
        debit = _parse_amount(debit_s)
        credit = _parse_amount(credit_s)
        if debit:
            amount = -abs(debit)
        elif credit:
            amount = abs(credit)
        elif (debit_s and debit is None) or (credit_s and credit is None):
            return _skip("unreadable amount")
        elif debit_s or credit_s:
            return _skip("zero amount")
        else:
            return _skip("no amount")

        # Balance
        balance_s = _get("balance")
        balance = _parse_amount(balance_s) if balance_s else 0.0

        # Enrichment (shared logic)
        card_last4 = detect_card(desc, known_cards)
        payment_type = detect_payment_type(desc)
        description_clean = clean_description(desc)
        direction = "out" if amount < 0 else "in"
        who = card_owners.get(card_last4, "Joint") if card_last4 else "Joint"

        return {
            "date_posted": posted.isoformat(),
            "date_value": value.isoformat(),
            "month": posted.strftime("%Y-%m"),
            "day_of_week": DOW_NAMES[posted.weekday()],
            "description_raw": desc,
            "description_clean": description_clean,
            "amount_signed": amount,
            "amount_abs": abs(amount),
            "direction": direction,
            "tx_type": "",
            "balance": balance or 0.0,
            "currency": "EUR",
            "account": "checkings_account",
            "card_last4": card_last4,
            "payment_type": payment_type,
            "who": who,
            "source_file": source_file,
        }
