"""
PDF bank statement parser.

Extracts transactions from the PDF statements that the bank sends by email
or makes available for download in the homebanking portal. Produces the same
list[dict] output as parse_utf16_csv() so the rest of the pipeline doesn't
care whether the input was CSV or PDF.

Requires: pdfplumber
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .constants import DEFAULT_CARDS, DOW_NAMES, account_type
from .parser import (
    clean_description,
    detect_card,
    detect_payment_type,
    load_card_holders,
    parse_amount,
)

# Set up logging for parsing anomalies
logger = logging.getLogger(__name__)

# Default credit patterns file
DEFAULT_CREDIT_PATTERNS = Path("data/credit-patterns.csv")

# ---------------------------------------------------------------------------
# Date / amount patterns
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{2}[./-]\d{2}[./-]\d{4}$")
DATE_RANGE_RE = re.compile(
    r"(\d{2}[./-]\d{2}[./-]\d{4})\s*(?:a|até|to|-)\s*(\d{2}[./-]\d{2}[./-]\d{4})"
)
# Also match date ranges like "EXTRATO DE 2026/02/02 A 2026/02/27"
DATE_RANGE_RE_ALT = re.compile(
    r"EXTRATO\s+DE\s+(\d{4}[./-]\d{2}[./-]\d{2})\s*(?:a|A)\s*(\d{4}[./-]\d{2}[./-]\d{2})"
)
AMOUNT_RE = re.compile(r"^-?\d[\d\s.]*[.,]\d{2}$")

# Pattern for text-based transaction lines (combined statement format)
# Format: "2.02 2.02 COMPRA 1234 DESCRIPTION 3.00 1 500.00"
# or: "2.02 2.02 DESCRIPTION 3.00 1 500.00"
TEXT_TX_RE = re.compile(
    r"^(\d{1,2}\.\d{2})\s+(\d{1,2}\.\d{2})\s+(.+?)\s+"
    r"(\d[\d\s.]*,\d{2})\s+"
    r"(\d[\d\s.]*,\d{2})$"
)

# The opening balance and the page carry-over of a text statement: the label
# is the whole description (after the dates, if any). The last figure is the
# balance, whether or not another one stands before it.
CARRY_OVER_RE = re.compile(
    r"^(?:\d{1,2}\.\d{2}\s+\d{1,2}\.\d{2}\s+)?"
    r"(?:SALDO INICIAL|TRANSPORTE)"
    r"(?:\s+\d{1,3}(?:\s\d{3})*\.\d{2})?"
    r"\s+(\d{1,3}(?:\s\d{3})*\.\d{2})$",
    re.IGNORECASE,
)

# An amount as the statement prints it: "0.40", "150.00", "1 529.13". A group
# with a leading zero ("050.00") only occurs after another group.
PRINTED_AMOUNT_RE = re.compile(r"^(?:0|[1-9]\d{0,2}(?:\s\d{3})*)\.\d{2}$")

# Default credit patterns (used if no config file exists)
DEFAULT_CREDIT_PATTERN_LIST = [
    "TRF. P/O",  # Transfer TO us (incoming)
    "TRANSFERENCIA - VENCIMENTO",  # Salary
    "CRED.",  # Credit/refund to card
    "DEVOL.",  # Devolution/refund
    "DEP NUM",  # Cash deposit
    "DEP ",  # Deposit (generic)
    "DEPOSITO",
    "REEMBOLSO",  # Reimbursement
]


def load_credit_patterns(path: Path = DEFAULT_CREDIT_PATTERNS) -> list[str]:
    """Load credit patterns from CSV file, or return defaults if not found."""
    if not path.exists():
        return DEFAULT_CREDIT_PATTERN_LIST.copy()

    patterns = []
    try:
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("pattern"):
                    patterns.append(row["pattern"])
    except Exception as e:
        logger.warning(f"Could not load credit patterns from {path}: {e}")
        return DEFAULT_CREDIT_PATTERN_LIST.copy()

    return patterns if patterns else DEFAULT_CREDIT_PATTERN_LIST.copy()


# Known header labels (case-insensitive) that identify the transaction table
TABLE_HEADERS = {
    "data",
    "data lanc",
    "data lançamento",
    "data lancamento",
    "data valor",
    "descrição",
    "descricao",
    "montante",
    "débito",
    "debito",
    "crédito",
    "credito",
    "saldo",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> Optional[date]:
    """Parse a date string in dd-mm-YYYY, dd/mm/YYYY, or dd.mm.YYYY format."""
    s = s.strip()
    for sep in ("-", "/", "."):
        fmt = f"%d{sep}%m{sep}%Y"
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(s: str) -> Optional[float]:
    """Parse an amount ('-45,50', '1.234,56', '3.00'); None if not one."""
    if not s or not s.strip():
        return None
    try:
        return parse_amount(s)
    except ValueError:
        return None


def _is_header_row(row: list[str]) -> bool:
    """Check if a table row looks like a header (contains known column names)."""
    text = " ".join((cell or "").lower() for cell in row)
    matches = sum(1 for h in TABLE_HEADERS if h in text)
    return matches >= 2


def _find_column_mapping(header_row: list[str]) -> dict[str, int]:
    """
    Map semantic column names to indices based on the header row text.

    Returns a dict with keys: 'date_posted', 'date_value', 'description',
    'amount', 'debit', 'credit', 'type', 'balance'.
    Not all keys may be present.
    """
    mapping: dict[str, int] = {}
    for i, cell in enumerate(header_row):
        cell_lower = (cell or "").strip().lower()

        if cell_lower in (
            "data",
            "data lanc",
            "data lançamento",
            "data lancamento",
            "data lanç.",
            "data lanc.",
        ):
            mapping.setdefault("date_posted", i)
        elif cell_lower in ("data valor", "data val", "data val.", "valor"):
            mapping["date_value"] = i
        elif cell_lower in (
            "descrição",
            "descricao",
            "descrição do movimento",
            "descricao do movimento",
            "descrição movimento",
        ):
            mapping["description"] = i
        elif cell_lower in ("montante", "valor", "importância", "importancia"):
            mapping["amount"] = i
        elif cell_lower in ("débito", "debito"):
            mapping["debit"] = i
        elif cell_lower in ("crédito", "credito"):
            mapping["credit"] = i
        elif cell_lower in ("tipo", "tipo mov", "tipo mov."):
            mapping["type"] = i
        elif cell_lower in ("saldo", "saldo contab.", "saldo contabilístico"):
            mapping["balance"] = i

    return mapping


def _extract_text_date_range(full_text: str) -> tuple[Optional[date], Optional[date]]:
    """Try to extract the statement date range from the full PDF text."""
    # Try standard format first: "01-01-2026 a 31-01-2026"
    m = DATE_RANGE_RE.search(full_text)
    if m:
        d1 = _parse_date(m.group(1))
        d2 = _parse_date(m.group(2))
        if d1 and d2:
            return d1, d2

    # Try alternative format: "EXTRATO DE 2026/02/02 A 2026/02/27"
    m = DATE_RANGE_RE_ALT.search(full_text)
    if m:
        d1 = _parse_date_ymd(m.group(1))
        d2 = _parse_date_ymd(m.group(2))
        if d1 and d2:
            return d1, d2

    return None, None


def _parse_date_ymd(s: str) -> Optional[date]:
    """Parse a date string in YYYY-mm-dd, YYYY/mm/dd format."""
    s = s.strip()
    for sep in ("-", "/", "."):
        fmt = f"%Y{sep}%m{sep}%d"
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_pdf_statement(
    path: Path,
    cards_path: Path = DEFAULT_CARDS,
    credit_patterns_path: Path = DEFAULT_CREDIT_PATTERNS,
) -> list[dict]:
    """
    Parse a PDF bank statement.

    Extracts transaction rows from tables in the PDF using pdfplumber,
    then applies the same cleaning, card detection, and payment type
    inference as the CSV parser.

    Returns the same list[dict] format as parse_utf16_csv().
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF parsing. "
            "Install it with: pip install pdfplumber"
        )

    card_owners = load_card_holders(cards_path)
    known_cards = set(card_owners.keys())
    credit_patterns = load_credit_patterns(credit_patterns_path)

    rows: list[dict] = []

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        # First, try to extract the statement year from the date range
        statement_year = None
        d1, d2 = _extract_text_date_range(full_text)
        if d1:
            statement_year = d1.year

        # Try table-based extraction first
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Find the header row
                header_idx = None
                for idx, row in enumerate(table):
                    if _is_header_row(row):
                        header_idx = idx
                        break

                if header_idx is None:
                    continue

                col_map = _find_column_mapping(table[header_idx])

                # We need at least a date and a description
                if "date_posted" not in col_map and "description" not in col_map:
                    continue

                # Process data rows
                for row in table[header_idx + 1 :]:
                    parsed = _parse_table_row(
                        row,
                        col_map,
                        known_cards,
                        card_owners,
                        path.name,
                    )
                    if parsed:
                        rows.append(parsed)

        # If no rows from tables, try text-based extraction
        if not rows:
            rows = _parse_text_transactions(
                full_text,
                statement_year,
                known_cards,
                card_owners,
                path.name,
                credit_patterns,
                period=(d1, d2) if d1 and d2 else None,
            )

    # Validation: check for anomalies
    _validate_parsed_transactions(rows, path.name)

    return rows


def _validate_parsed_transactions(rows: list[dict], source_file: str) -> None:
    """Warn about parsing anomalies: no rows, or amounts the balance disproves."""
    if not rows:
        logger.warning(f"{source_file}: No transactions parsed")
        return

    # Check for unusual patterns
    large_amounts = [r for r in rows if abs(r["amount_signed"]) > 10000]
    if large_amounts:
        for r in large_amounts:
            logger.debug(
                f"{source_file}: Large amount {r['amount_signed']:.2f} - {r['description_raw'][:50]}"
            )

    # A wrong sign (text lines are signed by keyword) or a misread amount
    # shows up as a balance that moved by something else.
    for row, change in _balance_mismatches(rows):
        logger.warning(
            "%s: amount does not match the balance on %s: %s is %+.2f but the "
            "balance changed by %+.2f",
            source_file,
            row["date_posted"],
            row["description_raw"][:40],
            row["amount_signed"],
            change,
        )


def _balance_mismatches(rows: list[dict]) -> list[tuple[dict, float]]:
    """Rows whose amount differs from the change in the running balance.

    Statements list transactions oldest first or newest first; the order
    that explains more amounts is used, and the dates decide when both
    explain as many (neighbours of equal amounts). A balance of 0 stands
    for "not printed" (see the row parsers), so only neighbours that both
    carry a balance are compared.
    """
    pairs = [
        (prev, cur)
        for prev, cur in zip(rows, rows[1:])
        if prev.get("balance") and cur.get("balance")
    ]

    def explains(row: dict, change: float) -> bool:
        # Magnitudes only: the sign is one of the things being checked
        return abs(abs(change) - row["amount_abs"]) < 0.005

    oldest_first = sum(
        explains(cur, cur["balance"] - prev["balance"]) for prev, cur in pairs
    )
    newest_first = sum(
        explains(prev, prev["balance"] - cur["balance"]) for prev, cur in pairs
    )

    if newest_first == oldest_first and rows:
        # ISO dates: a first row dated after the last one is newest first
        newest_first += rows[0]["date_posted"] > rows[-1]["date_posted"]

    mismatches = []
    for prev, cur in pairs:
        if newest_first > oldest_first:
            row, change = prev, prev["balance"] - cur["balance"]
        else:
            row, change = cur, cur["balance"] - prev["balance"]
        if abs(change - row["amount_signed"]) >= 0.005:
            mismatches.append((row, change))
    return mismatches


def _parse_text_transactions(
    full_text: str,
    statement_year: Optional[int],
    known_cards: set[str],
    card_owners: dict[str, str],
    source_file: str,
    credit_patterns: list[str] = None,
    period: Optional[tuple[date, date]] = None,
) -> list[dict]:
    """
    Parse transactions from text-based PDF format (combined statement).

    Lines carry only month.day; with the statement ``period`` known, each
    date gets the year that puts it nearest the period (see _nearest_date).
    With neither a period nor a year, each date gets the latest year that
    does not put it after today (see _latest_date), and the guess is reported.

    Handles lines like:
    "2.02 2.02 COMPRA 1234 TIGER LISBOA 3.00 1 500.00"
    """
    rows: list[dict] = []

    assumed_end: Optional[date] = None
    if statement_year is None:
        statement_year = date.today().year
        if period is None:
            # Today's year would put a December statement read in January
            # eleven months into the future
            assumed_end = date.today()

    if credit_patterns is None:
        credit_patterns = DEFAULT_CREDIT_PATTERN_LIST

    # Split into lines and process each
    lines = full_text.split("\n")

    current_month = None
    # The balance before the line being read: tells an amount from a number
    # that ends the description (see _settle_amount)
    prev_balance: Optional[float] = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        carry_over = CARRY_OVER_RE.match(line)
        if carry_over:
            prev_balance = _parse_amount_pdf_text(carry_over.group(1))
            continue

        # Try to parse as a transaction line (it reports a line that starts
        # like one and cannot be read)
        parsed = _parse_text_transaction_line(
            line,
            statement_year,
            current_month,
            known_cards,
            card_owners,
            source_file,
            credit_patterns,
            period=period,
            prev_balance=prev_balance,
            not_after=assumed_end,
        )
        if parsed:
            rows.append(parsed)
            # Update current month from parsed transaction
            current_month = int(parsed["date_posted"].split("-")[1])
            if parsed["balance"]:  # 0 stands for "not printed"
                prev_balance = parsed["balance"]

    if assumed_end and rows:
        logger.warning(
            "%s: no statement period found, and its lines carry no year: "
            "dated as the 12 months up to %s; check the dates if the "
            "statement is older than that",
            source_file,
            assumed_end.isoformat(),
        )

    return rows


def _parse_amount_pdf_text(s: str) -> Optional[float]:
    """
    Parse amount from PDF text format.

    In combined statements, amounts use:
    - Space as thousands separator: "1 500.00"
    - Dot as decimal separator: "3.00"
    """
    if not s or not s.strip():
        return None
    s = s.strip()
    # Remove space thousands separators, keep the dot decimal
    cleaned = s.replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _latest_date(not_after: date, month: int, day: int) -> date:
    """The month/day on its latest occurrence up to ``not_after`` (raises ValueError)."""
    # Nine years reach the previous 29 February even across 2100
    for year in range(not_after.year, not_after.year - 9, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= not_after:
            return candidate
    raise ValueError(f"no such date: month {month}, day {day}")


def _settle_amount(
    desc: str,
    amount_s: str,
    balance: Optional[float],
    prev_balance: Optional[float],
    line: str,
    source_file: str,
) -> tuple[str, str]:
    """Decide where the description ends and the amount starts.

    A number that ends the description reads as a thousands group of the
    amount: "COMPRA LIDL 280 150.00" is 150.00 spent at "LIDL 280", or
    280 150.00 spent at "LIDL". ``amount_s`` is the longest reading. The one
    that explains the change in the running balance wins (lines are listed
    oldest first); failing that, the only one printed the way an amount is;
    failing that, the longest, as before, with a warning.
    """
    groups = amount_s.split()
    readings = [
        (" ".join([desc] + groups[:i]), " ".join(groups[i:]))
        for i in range(len(groups))
    ]
    if len(readings) == 1:
        return desc, amount_s

    if balance is not None and prev_balance is not None:
        change = abs(balance - prev_balance)
        for reading in readings:
            if abs(_parse_amount_pdf_text(reading[1]) - change) < 0.005:
                return reading

    printable = [r for r in readings if PRINTED_AMOUNT_RE.match(r[1])]
    if len(printable) == 1:
        return printable[0]

    candidates = printable or readings
    logger.warning(
        "%s: cannot tell the amount from the description in '%s': read as %s, "
        "could be %s",
        source_file,
        line,
        candidates[0][1],
        " or ".join(r[1] for r in candidates[1:]),
    )
    return candidates[0]


def _nearest_date(anchor: date, month: int, day: int) -> date:
    """The month/day in the year closest to ``anchor`` (raises ValueError)."""
    try:
        candidate = date(anchor.year, month, day)
    except ValueError:
        # 29 February when the anchor's year has none: a statement running
        # over the new year (Nov 2023 to Feb 2024) can still contain one.
        for year in (anchor.year - 1, anchor.year + 1):
            try:
                neighbour = date(year, month, day)
            except ValueError:
                continue
            if abs((neighbour - anchor).days) <= 183:
                return neighbour
        raise
    if (candidate - anchor).days > 183:
        candidate = date(anchor.year - 1, month, day)
    elif (anchor - candidate).days > 183:
        candidate = date(anchor.year + 1, month, day)
    return candidate


def _parse_text_transaction_line(
    line: str,
    year: int,
    current_month: Optional[int],
    known_cards: set[str],
    card_owners: dict[str, str],
    source_file: str,
    credit_patterns: list[str] = None,
    period: Optional[tuple[date, date]] = None,
    prev_balance: Optional[float] = None,
    not_after: Optional[date] = None,
) -> Optional[dict]:
    """
    Parse a single text line as a transaction.

    Expected format: "MONTH.DAY MONTH.DAY DESCRIPTION AMOUNT BALANCE"
    Where MONTH.DAY is like "2.03" for February 3rd (month 2, day 03)
    Amount format: "3.00" or "1 500.00" (space thousands, dot decimal)

    Some edge cases:
    - Lines with only one amount (no balance): ">PAGAMENTO CARTAO DE CREDITO 4.68"
    - Lines with amounts in description: "COMISSAO TRF MBWAY 12.34 APP MB WAY 1.00 1 429.14"
    - Credit transactions: configurable via credit_patterns parameter
    """
    if credit_patterns is None:
        credit_patterns = DEFAULT_CREDIT_PATTERN_LIST
    # Check if line starts with date pattern - if not, skip
    if not re.match(r"^\d{1,2}\.\d{2}\s+\d{1,2}\.\d{2}\s+", line):
        return None

    # Carry-over lines never get here (see CARRY_OVER_RE): a transaction
    # whose text contains "TRANSPORTE" (STCP ... TRANSPORTES) is one to keep.

    def _skip(reason: str) -> None:
        # Two dates at the start: this may be a transaction left out
        logger.warning("%s: line not imported (%s): %s", source_file, reason, line)

    # Extract dates from the beginning
    date_prefix = re.match(r"^(\d{1,2})\.(\d{2})\s+(\d{1,2})\.(\d{2})\s+", line)
    if not date_prefix:
        return None

    day1, month1, day2, month2 = date_prefix.groups()
    rest = line[date_prefix.end() :]

    # Strategy: Parse amounts from the END of the line, working backwards
    # This avoids capturing amounts that appear in descriptions (like "COMISSAO TRF MBWAY 12.34")
    #
    # Amount pattern: optional thousands (1-3 digits + space) + digits + .XX
    # We anchor to the end of the string to find the true balance and amount

    # First, try to match: DESCRIPTION AMOUNT BALANCE (two amounts at end)
    # Pattern explanation:
    # - (.+?) = description (non-greedy)
    # - \s+ = whitespace separator
    # - (\d{1,3}(?:\s\d{3})*\.\d{2}) = amount with optional space-thousands
    # - \s+ = whitespace separator
    # - (\d{1,3}(?:\s\d{3})*\.\d{2}) = balance with optional space-thousands
    # - $ = end of string
    two_amounts = re.match(
        r"^(.+?)\s+(\d{1,3}(?:\s\d{3})*\.\d{2})\s+(\d{1,3}(?:\s\d{3})*\.\d{2})$", rest
    )

    if two_amounts:
        desc = two_amounts.group(1).strip()
        amount_s = two_amounts.group(2)
        balance_s = two_amounts.group(3)
    else:
        # Try single amount (no balance) - some lines like ">PAGAMENTO CARTAO DE CREDITO 4.68"
        one_amount = re.match(r"^(.+?)\s+(\d{1,3}(?:\s\d{3})*\.\d{2})$", rest)
        if one_amount:
            desc = one_amount.group(1).strip()
            amount_s = one_amount.group(2)
            balance_s = "0.00"
        else:
            return _skip("no amount at the end")

    if not desc:
        return _skip("no description")

    # Parse dates - format is MONTH.DAY (e.g., "2.03" = Feb 3rd, month 2, day 03)
    try:
        month_int = int(day1)  # First number is month
        day_int = int(month1)  # Second number is day
        month2_int = int(day2)
        day2_int = int(month2)
        if period is not None:
            # A statement can span the new year (Dec 29 to Jan 28), and a
            # value date can fall in the year before its posting date.
            start, end = period
            posted = _nearest_date(start + (end - start) / 2, month_int, day_int)
            value = _nearest_date(posted, month2_int, day2_int)
        elif not_after is not None:
            # No period and no year: a statement holds nothing from the future
            posted = _latest_date(not_after, month_int, day_int)
            value = _nearest_date(posted, month2_int, day2_int)
        else:
            posted = date(year, month_int, day_int)
            value = date(year, month2_int, day2_int)
    except ValueError:
        return _skip("invalid date")

    # Parse amount
    desc, amount_s = _settle_amount(
        desc,
        amount_s,
        _parse_amount_pdf_text(balance_s) if two_amounts else None,
        prev_balance,
        line,
        source_file,
    )
    amount = _parse_amount_pdf_text(amount_s)
    if amount is None:
        return _skip("invalid amount")

    # Determine if debit or credit based on configurable patterns
    # Credits are incoming money - they should be positive
    desc_upper = desc.upper()
    is_credit = any(p.upper() in desc_upper for p in credit_patterns)

    # Most transactions are expenses (negative), credits are positive
    if not is_credit:
        amount = -abs(amount)
    else:
        amount = abs(amount)

    # Parse balance
    balance = _parse_amount_pdf_text(balance_s) or 0.0

    # Apply same enrichment as CSV parser
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
        "balance": balance,
        "currency": "EUR",
        "account": account_type(),
        "card_last4": card_last4,
        "payment_type": payment_type,
        "who": who,
        "source_file": source_file,
    }


def _parse_table_row(
    row: list[str],
    col_map: dict[str, int],
    known_cards: set[str],
    card_owners: dict[str, str],
    source_file: str,
) -> Optional[dict]:
    """Parse a single table row into a transaction dict, or return None."""

    def _get(key: str) -> str:
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    # --- Date ---
    date_posted_s = _get("date_posted")
    posted = _parse_date(date_posted_s) if date_posted_s else None
    if not posted:
        return None  # Not a data row

    def _skip(reason: str) -> None:
        # A dated row: this may be a transaction left out
        logger.warning(
            "%s: row not imported (%s): %s",
            source_file,
            reason,
            " | ".join((cell or "").strip() for cell in row),
        )

    date_value_s = _get("date_value")
    value = _parse_date(date_value_s) if date_value_s else posted

    # --- Description ---
    desc = _get("description")
    if not desc:
        return _skip("no description")

    # --- Amount ---
    amount = None
    if "amount" in col_map:
        amount = _parse_amount(_get("amount"))
    elif "debit" in col_map or "credit" in col_map:
        # Some PDFs have separate debit/credit columns
        debit = _parse_amount(_get("debit"))
        credit = _parse_amount(_get("credit"))
        if debit:
            amount = -abs(debit)
        elif credit:
            amount = abs(credit)

    if amount is None:
        return _skip("no readable amount")

    # --- Type and balance ---
    tx_type = _get("type")
    balance_s = _get("balance")
    balance = _parse_amount(balance_s) if balance_s else 0.0

    # --- Apply same enrichment as CSV parser ---
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
        "tx_type": tx_type,
        "balance": balance or 0.0,
        "currency": "EUR",
        "account": account_type(),
        "card_last4": card_last4,
        "payment_type": payment_type,
        "who": who,
        "source_file": source_file,
    }


def extract_pdf_date_range(path: Path) -> tuple[date, date]:
    """
    Extract the date range from a PDF statement.

    Looks for a date range pattern in the text (e.g., '01-01-2026 a 31-01-2026'),
    and falls back to min/max transaction dates if no explicit range is found.
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF parsing. "
            "Install it with: pip install pdfplumber"
        )

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Try explicit date range first
    d1, d2 = _extract_text_date_range(full_text)
    if d1 and d2:
        return d1, d2

    # Fall back to parsing transactions and using min/max dates
    rows = parse_pdf_statement(path)
    if not rows:
        raise ValueError(f"Could not find date range or transactions in {path.name}")

    dates = [datetime.fromisoformat(r["date_posted"]).date() for r in rows]
    return min(dates), max(dates)
