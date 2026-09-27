"""SQLite storage: schema, ingestion, migrations, queries."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .constants import NON_SPENDING_CATEGORIES, SAVINGS_CATEGORIES
from .parser import parse_utf16_csv
from .parsers import parse_statement, detect_parser


# ---------------------------------------------------------------------------
# Transaction ID (for deduplication)
# ---------------------------------------------------------------------------


def tx_id(row: dict) -> str:
    """Stable hash for deduplication. Includes balance to reduce collisions."""
    key = "|".join(
        [
            row["account"],
            row["date_posted"],
            row["date_value"],
            row["description_raw"],
            f"{row['amount_signed']:.2f}",
            f"{row['balance']:.2f}",
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the transactions table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id    TEXT PRIMARY KEY,
            date_posted       TEXT NOT NULL,
            date_value        TEXT NOT NULL,
            month             TEXT NOT NULL,
            day_of_week       TEXT NOT NULL,
            description_raw   TEXT NOT NULL,
            description_clean TEXT,
            amount_signed     REAL NOT NULL,
            amount_abs        REAL NOT NULL,
            direction         TEXT NOT NULL,
            tx_type           TEXT,
            balance           REAL,
            currency          TEXT NOT NULL,
            account           TEXT NOT NULL,
            card_last4        TEXT,
            payment_type      TEXT,
            who               TEXT,
            category          TEXT,
            subcategory       TEXT,
            category_source   TEXT,
            notes             TEXT,
            source_file       TEXT NOT NULL,
            imported_at       TEXT NOT NULL
        );
        """
    )
    conn.commit()


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from an older schema."""
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
    }
    migrations = [
        ("month", "TEXT NOT NULL DEFAULT ''"),
        ("day_of_week", "TEXT NOT NULL DEFAULT ''"),
        ("description_clean", "TEXT"),
        ("payment_type", "TEXT"),
        ("who", "TEXT"),
        ("notes", "TEXT"),
        # 'rule' | 'manual' | NULL (uncategorized); see categorize_transactions
        ("category_source", "TEXT"),
    ]
    for col, typedef in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {typedef}")

    # Values last written to or read from the ODS report. sync_from_ods()
    # compares against these rather than the live DB, so only cells the user
    # actually changed count as edits.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ods_baseline (
            transaction_id TEXT PRIMARY KEY,
            category       TEXT NOT NULL,
            subcategory    TEXT NOT NULL,
            notes          TEXT NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _parse_file(path: Path, cards_path: Path) -> list[dict]:
    """Route to the correct parser based on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_parser import parse_pdf_statement
        return parse_pdf_statement(path, cards_path=cards_path)
    else:
        return parse_utf16_csv(path, cards_path=cards_path)


def ingest(
    db_path: Path,
    file_paths: Iterable[Path],
    cards_path: Path | None = None,
    bank_id: str | None = None,
) -> None:
    """Parse bank statement files (CSV or PDF) and insert into SQLite with deduplication.

    Supports auto-detection of the bank format. Use bank_id to override.
    """
    from .constants import DEFAULT_CARDS

    if cards_path is None:
        cards_path = DEFAULT_CARDS

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        migrate_schema(conn)
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        total_parsed = 0
        total_inserted = 0
        paths = list(file_paths)
        for p in paths:
            # Try multi-bank parser first, fall back to legacy UTF-16 parser / PDF
            try:
                rows = parse_statement(p, cards_path=cards_path, bank_id=bank_id)
            except ValueError:
                # Fall back to extension-based detection (CSV vs PDF)
                rows = _parse_file(p, cards_path)
            total_parsed += len(rows)
            for r in rows:
                r["transaction_id"] = tx_id(r)
                r["imported_at"] = now

                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO transactions (
                        transaction_id, date_posted, date_value, month, day_of_week,
                        description_raw, description_clean,
                        amount_signed, amount_abs, direction, tx_type, balance,
                        currency, account, card_last4, payment_type, who,
                        category, subcategory,
                        source_file, imported_at
                    ) VALUES (
                        :transaction_id, :date_posted, :date_value, :month, :day_of_week,
                        :description_raw, :description_clean,
                        :amount_signed, :amount_abs, :direction, :tx_type, :balance,
                        :currency, :account, :card_last4, :payment_type, :who,
                        NULL, NULL,
                        :source_file, :imported_at
                    )
                    """,
                    r,
                )
                if cur.rowcount > 0:
                    total_inserted += 1

        conn.commit()
        print(f"Parsed {total_parsed} transactions from {len(paths)} file(s).")
        print(f"Inserted {total_inserted} new transactions into {db_path}.")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def is_savings(tx: dict) -> bool:
    """Money moved to or from savings (see SAVINGS_CATEGORIES)."""
    return (tx.get("category") or "") in SAVINGS_CATEGORIES


def is_refund(tx: dict) -> bool:
    """Incoming money in a spending category (see NON_SPENDING_CATEGORIES)."""
    category = tx.get("category") or ""
    return (
        tx.get("direction") == "in"
        and bool(category)
        and category not in NON_SPENDING_CATEGORIES
        and not is_savings(tx)
    )


def is_income(tx: dict) -> bool:
    """Incoming money that is neither a refund nor a savings withdrawal."""
    return tx.get("direction") == "in" and not is_refund(tx) and not is_savings(tx)


def counts_as_spending(tx: dict) -> bool:
    """Outgoing money other than savings deposits, or a refund."""
    return (tx.get("direction") == "out" and not is_savings(tx)) or is_refund(tx)


def spend_amount(tx: dict) -> float:
    """Contribution to spending: +amount out, -amount for a refund, else 0."""
    if not counts_as_spending(tx):
        return 0.0
    return tx["amount_abs"] if tx.get("direction") == "out" else -tx["amount_abs"]


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# SQL counterparts of the helpers above, for aggregate queries.
NOT_SAVINGS_SQL = f"COALESCE(category, '') NOT IN ({_sql_list(SAVINGS_CATEGORIES)})"
REFUND_SQL = (
    "(direction = 'in' AND COALESCE(category, '') <> '' AND category NOT IN "
    f"({_sql_list(NON_SPENDING_CATEGORIES + SAVINGS_CATEGORIES)}))"
)
INCOME_SQL = f"(direction = 'in' AND NOT {REFUND_SQL} AND {NOT_SAVINGS_SQL})"
SPEND_SQL = (
    f"CASE WHEN direction = 'out' AND {NOT_SAVINGS_SQL} THEN amount_abs "
    f"WHEN {REFUND_SQL} THEN -amount_abs ELSE 0 END"
)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def ingested_source_files(db_path: Path) -> set[str]:
    """Return the set of source_file names already present in the DB."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT DISTINCT source_file FROM transactions"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def reclean_descriptions(db_path: Path) -> int:
    """Re-run clean_description() on all transactions in the DB.

    Updates description_clean for any row where the new cleaned value
    differs from the stored one.  All other fields are preserved.

    Returns the number of rows updated.
    """
    from .parser import clean_description  # avoid circular import at top level

    conn = sqlite3.connect(str(db_path))
    updated = 0
    try:
        migrate_schema(conn)
        rows = conn.execute(
            "SELECT transaction_id, description_raw, description_clean "
            "FROM transactions"
        ).fetchall()

        for tid, raw, old_clean in rows:
            new_clean = clean_description(raw)
            if new_clean != (old_clean or ""):
                conn.execute(
                    "UPDATE transactions SET description_clean = ? "
                    "WHERE transaction_id = ?",
                    (new_clean, tid),
                )
                updated += 1

        conn.commit()
    finally:
        conn.close()

    return updated


def fetch_all_transactions(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all transactions as a list of dicts, sorted by date desc."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            transaction_id, date_posted, date_value, month, day_of_week,
            description_raw, description_clean,
            amount_signed, amount_abs, direction, tx_type, balance,
            currency, account, card_last4, payment_type, who,
            category, subcategory, notes, source_file, imported_at
        FROM transactions
        ORDER BY date_posted DESC, rowid DESC
        """
    ).fetchall()
    conn.row_factory = None

    result = []
    for r in rows:
        d = dict(r)
        d["status"] = "categorized" if d.get("category") else "uncategorized"
        d["spend"] = spend_amount(d)
        result.append(d)
    return result
