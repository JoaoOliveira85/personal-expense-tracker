"""
UTF-16 CSV bank statement parser.

Handles the quirky format: UTF-16 LE encoding, semicolon delimiters,
non-tabular header/footer lines, Portuguese dates and amounts.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .constants import (
    DEFAULT_CARDS,
    DEFAULT_CLEANING_PATTERNS,
    DEFAULT_NOISE_WORDS,
    DOW_NAMES,
    account_type,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------

HEADER_PREFIX = "Data lançamento;Data valor;Descrição;Montante;Tipo;Saldo"
ROW_RE = re.compile(r"^\d{2}-\d{2}-\d{4};\d{2}-\d{2}-\d{4};")
DATE_FROM_RE = re.compile(r"^Data de:\s*;?\s*(\d{2}-\d{2}-\d{4})")
DATE_TO_RE = re.compile(r"^Data até:\s*;?\s*(\d{2}-\d{2}-\d{4})")

# ---------------------------------------------------------------------------
# Payment-type detection patterns (checked in order)
# ---------------------------------------------------------------------------

PAYMENT_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^COMPRA\b", re.IGNORECASE), "card"),
    (re.compile(r"^DD\b", re.IGNORECASE), "direct_debit"),
    (re.compile(r"^TRF\s*MB\s*WAY\b", re.IGNORECASE), "transfer"),
    (re.compile(r"^TRF\.?\s*P/O?\b", re.IGNORECASE), "transfer"),
    (re.compile(r"^TRF\s*P/\b", re.IGNORECASE), "transfer"),
    (re.compile(r"^TRANSFERENCIA\b", re.IGNORECASE), "transfer"),
    (re.compile(r"^LEV\s*ATM\b", re.IGNORECASE), "atm"),
    (re.compile(r"^COMISSAO\b", re.IGNORECASE), "fee"),
    (re.compile(r"^COM\.MAN\.CONTA\b", re.IGNORECASE), "fee"),
    (re.compile(r"^CUSTO\b", re.IGNORECASE), "fee"),
    (re.compile(r"^IMPOSTO\b", re.IGNORECASE), "tax"),
]

# ---------------------------------------------------------------------------
# Description cleaning — loaded from external files
# ---------------------------------------------------------------------------

CARD_RE = re.compile(r"\b(\d{4})\b")

# Cached compiled regexes (populated on first call to _get_cleaning_patterns)
_cleaning_cache: dict[str, re.Pattern | None] | None = None


def _get_cleaning_patterns(
    noise_words_path: Path = DEFAULT_NOISE_WORDS,
    cleaning_patterns_path: Path = DEFAULT_CLEANING_PATTERNS,
) -> dict[str, re.Pattern | None]:
    """
    Load and compile cleaning patterns from external files.

    Returns a dict with keys 'prefix' and 'noise', each mapping to a
    compiled regex (or None if no patterns were found).

    Results are cached after the first call.
    """
    global _cleaning_cache
    if _cleaning_cache is not None:
        return _cleaning_cache

    prefix_parts: list[str] = []
    noise_parts: list[str] = []

    # --- Load noise words from text file (space-bounded matching) ---
    if noise_words_path.exists():
        words: list[str] = []
        for line in noise_words_path.read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                words.append(word)
        if words:
            escaped = [re.escape(w) for w in words]
            # Match only when surrounded by whitespace or string boundaries
            # so "PT" won't match inside "CONTINENTE.PT"
            noise_parts.append(r"(?<!\S)(?:" + "|".join(escaped) + r")(?!\S)")

    # --- Load regex patterns from CSV ---
    if cleaning_patterns_path.exists():
        with cleaning_patterns_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ptype = row.get("type", "").strip().lower()
                pattern = row.get("pattern", "").strip()
                if not pattern:
                    continue
                if ptype == "prefix":
                    prefix_parts.append(pattern)
                elif ptype == "noise":
                    noise_parts.append(pattern)

    # --- Compile ---
    prefix_re = None
    if prefix_parts:
        prefix_re = re.compile(r"^(?:" + "|".join(prefix_parts) + r")", re.IGNORECASE)

    noise_re = None
    if noise_parts:
        noise_re = re.compile("|".join(noise_parts), re.IGNORECASE)

    _cleaning_cache = {"prefix": prefix_re, "noise": noise_re}
    return _cleaning_cache


def reset_cleaning_cache() -> None:
    """Clear the cached cleaning patterns (useful for testing)."""
    global _cleaning_cache
    _cleaning_cache = None


# ---------------------------------------------------------------------------
# Card-holder loading
# ---------------------------------------------------------------------------


def load_card_holders(cards_path: Path = DEFAULT_CARDS) -> dict[str, str]:
    """
    Load card-holder mappings from a CSV file.

    Returns a dict mapping card last-4 digits to owner name,
    e.g. {"1234": "Alice", "5678": "Bob"}.
    """
    if not cards_path.exists():
        return {}
    holders: dict[str, str] = {}
    with cards_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            last4 = row.get("card_last4", "").strip()
            name = row.get("name", "").strip()
            if last4 and name:
                holders[last4] = name
    return holders


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


# The amount formats of the supported statements. Digits are grouped in
# threes or not at all, and there are at most two decimals; anything else
# (comma thousands, exponents, "nan", a trailing "D") is not an amount.
_SIGN = r"[+-]?\s*"
_COMMA_DECIMAL_RE = re.compile(  # -45,50  1.234,56  1 234,56
    _SIGN + r"(?:[0-9]{1,3}(?:[. \u00a0][0-9]{3})+|[0-9]+),[0-9]{1,2}"
)
_DOT_DECIMAL_RE = re.compile(  # 45.50  1 529.13  -.50
    _SIGN + r"(?:[0-9]{1,3}(?:[ \u00a0][0-9]{3})+|[0-9]*)\.[0-9]{1,2}"
)
_NO_DECIMALS_RE = re.compile(  # 45  1.234  1 234
    _SIGN + r"(?:[0-9]{1,3}(?:[. \u00a0][0-9]{3})+|[0-9]+)"
)


def parse_amount(text: str) -> float:
    """Parse a statement amount: '-45,50', '1.234,56', '45.50' or '1 529.13'.

    With a comma, the comma is the decimal mark and dots group thousands.
    Without one, a dot followed by one or two final digits is the decimal
    mark; otherwise dots group thousands. Raises ValueError for anything
    that is not written in one of those forms: a wrong guess is a wrong sum.
    """
    s = text.strip()
    if _COMMA_DECIMAL_RE.fullmatch(s):
        number = re.sub(r"[.\s]", "", s).replace(",", ".")
    elif _DOT_DECIMAL_RE.fullmatch(s):
        number = re.sub(r"\s", "", s)
    elif _NO_DECIMALS_RE.fullmatch(s):
        number = re.sub(r"[.\s]", "", s)
    else:
        raise ValueError(f"not an amount: {text!r}")
    return float(number)


def detect_payment_type(description: str) -> str:
    """Infer payment type from the raw bank description."""
    for pattern, ptype in PAYMENT_TYPE_PATTERNS:
        if pattern.search(description):
            return ptype
    return "other"


def clean_description(raw: str) -> str:
    """
    Clean a raw bank description into a human-readable merchant/payee name.

    Strips prefixes (COMPRA NNNN, DD, TRF P/, etc.), noise tokens
    (CONTACTLESS, PT, postal codes, transaction hashes), and normalizes
    whitespace.

    Patterns are loaded from noise-words.txt and cleaning-patterns.csv.
    """
    patterns = _get_cleaning_patterns()

    cleaned = raw
    if patterns["prefix"]:
        cleaned = patterns["prefix"].sub("", cleaned).strip()
    if patterns["noise"]:
        cleaned = patterns["noise"].sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.strip(" ,-/")
    return cleaned if cleaned else raw


def detect_card(description: str, known_cards: set[str] | None = None) -> Optional[str]:
    """Extract known card last-4 from description."""
    if known_cards is None:
        known_cards = set(load_card_holders().keys())
    for m in CARD_RE.finditer(description):
        if m.group(1) in known_cards:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Shared text decoding
# ---------------------------------------------------------------------------


def _decode_utf16(path: Path) -> list[str]:
    """Read a UTF-16 CSV file and return decoded, stripped lines."""
    raw_bytes = path.read_bytes()

    for encoding in ("utf-16-le", "utf-16", "utf-8", "latin-1"):
        try:
            text = raw_bytes.decode(encoding, errors="strict")
            if HEADER_PREFIX in text or "Data lan" in text:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        text = raw_bytes.decode("utf-16-le", errors="replace")

    return [ln.strip() for ln in text.splitlines()]


# ---------------------------------------------------------------------------
# Date-range extraction and auto-rename
# ---------------------------------------------------------------------------


def extract_date_range(path: Path) -> tuple[date, date]:
    """
    Extract the 'Data de' and 'Data até' date range from a UTF-16 CSV header.
    Returns (date_from, date_to).
    """
    lines = _decode_utf16(path)

    date_from = None
    date_to = None
    for ln in lines:
        m = DATE_FROM_RE.match(ln)
        if m:
            date_from = datetime.strptime(m.group(1), "%d-%m-%Y").date()
        m = DATE_TO_RE.match(ln)
        if m:
            date_to = datetime.strptime(m.group(1), "%d-%m-%Y").date()
        if date_from and date_to:
            break

    if not date_from or not date_to:
        raise ValueError(f"Could not find date range in {path.name}")

    return date_from, date_to


def _set_aside(path: Path) -> Path:
    """Rename a statement to <name>.replaced (or .replaced-N) and return that.

    The new name no longer ends in .csv, so the file is not picked up as a
    statement to import again; backups still include it.
    """
    kept = path.with_name(path.name + ".replaced")
    n = 1
    while kept.exists():
        n += 1
        kept = path.with_name(f"{path.name}.replaced-{n}")
    path.rename(kept)
    return kept


def auto_rename_csv(path: Path) -> Path:
    """
    Auto-rename a UTF-16 CSV file based on its date range.

    Rules:
    - If both dates are in the same month → target name is YYYY-MM.csv
    - If target doesn't exist → rename
    - If target exists and new file has a wider date range → replace; the
      file it replaces is kept beside it as YYYY-MM.csv.replaced
    - If target exists and new file doesn't extend the range → raise error
    - If dates span multiple months → leave filename as-is

    Returns the (possibly renamed) path.
    """
    date_from, date_to = extract_date_range(path)

    # Check if both dates are in the same month
    if date_from.year != date_to.year or date_from.month != date_to.month:
        print(
            f"  Date range spans multiple months ({date_from} to {date_to}), "
            f"keeping original filename: {path.name}"
        )
        return path

    target_name = date_from.strftime("%Y-%m") + ".csv"
    target_path = path.parent / target_name

    # Already has the right name
    if path.resolve() == target_path.resolve():
        return path

    if not target_path.exists():
        path.rename(target_path)
        print(
            f"  Renamed {path.name} -> {target_name} "
            f"(covers {date_from} to {date_to})"
        )
        return target_path

    # Target exists — compare date ranges
    existing_from, existing_to = extract_date_range(target_path)

    new_is_wider = (
        date_from <= existing_from
        and date_to >= existing_to
        and (date_from < existing_from or date_to > existing_to)
    )

    if new_is_wider:
        # Statements in raw/ are the bank's own record: never delete one.
        # "Wider" is judged by the declared range alone, so the old file may
        # still hold rows the new one lacks.
        kept = _set_aside(target_path)
        path.rename(target_path)
        print(
            f"  Replaced {target_name} with {path.name} "
            f"(wider range: {date_from} to {date_to}, "
            f"was {existing_from} to {existing_to}); "
            f"previous file kept as {kept.name}"
        )
        return target_path

    raise ValueError(
        f"File {target_name} already exists with range {existing_from} to {existing_to}. "
        f"New file {path.name} covers {date_from} to {date_to} which doesn't extend it. "
        f"Remove the existing file manually if you want to replace it."
    )


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_utf16_csv(path: Path, cards_path: Path = DEFAULT_CARDS) -> list[dict]:
    """
    Parse a UTF-16 CSV export.

    Handles:
    - UTF-16 LE encoding (with or without BOM)
    - Semicolon delimiters
    - Non-tabular header/footer lines
    - Portuguese date format (dd-mm-YYYY)
    - Comma or dot decimal separators
    """
    card_owners = load_card_holders(cards_path)
    known_cards = set(card_owners.keys())

    lines = _decode_utf16(path)

    # Find the table header line
    try:
        start_idx = next(
            i for i, ln in enumerate(lines) if ln.startswith(HEADER_PREFIX)
        )
    except StopIteration:
        raise ValueError(f"Could not find transaction header row in {path.name}")

    rows: list[dict] = []
    for line_no, ln in enumerate(lines[start_idx + 1 :], start=start_idx + 2):
        if not ln:
            continue
        if not ROW_RE.match(ln):
            # The footer ends the table; anything dated after it is a loss
            unread = sum(1 for later in lines[line_no:] if ROW_RE.match(later))
            if unread:
                logger.warning(
                    "%s line %d: stopped reading at %r; %d later line(s) that "
                    "look like transactions were not imported",
                    path.name,
                    line_no,
                    ln[:60],
                    unread,
                )
            break

        # Handle quoted fields (descriptions can contain semicolons)
        parts = next(csv.reader([ln], delimiter=";"))
        if len(parts) < 6:
            logger.warning(
                "%s line %d: row not imported (%d of 6 fields): %s",
                path.name,
                line_no,
                len(parts),
                ln,
            )
            continue

        date_posted_s, date_value_s, desc, amount_s, tx_type, balance_s = parts[:6]

        posted = datetime.strptime(date_posted_s, "%d-%m-%Y").date()
        value = datetime.strptime(date_value_s, "%d-%m-%Y").date()

        amount = parse_amount(amount_s)
        balance = parse_amount(balance_s)

        card_last4 = detect_card(desc, known_cards)
        payment_type = detect_payment_type(desc)
        description_clean = clean_description(desc)
        direction = "out" if amount < 0 else "in"
        who = card_owners.get(card_last4, "Joint") if card_last4 else "Joint"

        rows.append(
            {
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
                "balance": balance,
                "currency": "EUR",
                "account": account_type(),
                "card_last4": card_last4,
                "payment_type": payment_type,
                "who": who,
                "source_file": path.name,
            }
        )

    return rows
