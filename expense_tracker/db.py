"""SQLite storage: schema, ingestion, migrations, queries."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .constants import IGNORED_CATEGORIES, NEUTRAL_CATEGORIES, NON_SPENDING_CATEGORIES
from .parser import parse_utf16_csv
from .parsers import parse_statement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transaction ID (for deduplication)
# ---------------------------------------------------------------------------


def _id_fields(row: dict) -> str:
    return "|".join(
        [
            row["account"],
            row["date_posted"],
            row["date_value"],
            row["description_raw"],
            f"{row['amount_signed']:.2f}",
            f"{row['balance']:.2f}",
        ]
    )


def tx_id(row: dict, occurrence: int = 1) -> str:
    """Stable hash for deduplication. Includes balance to reduce collisions.

    ``occurrence`` is the row's 1-based position among the rows of its file
    that share all the hashed fields: two identical purchases on a statement
    that prints no balance. Only later occurrences add it to the key, so the
    first keeps the id it always had.
    """
    key = _id_fields(row)
    if occurrence > 1:
        key += f"|{occurrence}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the transactions table if it doesn't exist."""
    conn.execute("""
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
        """)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ods_baseline (
            transaction_id TEXT PRIMARY KEY,
            category       TEXT NOT NULL,
            subcategory    TEXT NOT NULL,
            notes          TEXT NOT NULL
        )
        """)
    conn.commit()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def _parse_file(path: Path, cards_path: Path, bank_id: str | None = None) -> list[dict]:
    """Parse one statement with the parser its extension and bank_id call for.

    An error from that parser is the answer: it is not retried with the
    UTF-16 parser, which would hide an unknown or mistaken bank_id and report
    a missing UTF-16 header for a file that is not UTF-16 at all.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_parser import parse_pdf_statement

        return parse_pdf_statement(path, cards_path=cards_path)
    if bank_id is None and suffix != ".csv":
        # The registered parsers only claim .csv files: a UTF-16 export saved
        # under another extension still goes to the legacy UTF-16 parser.
        return parse_utf16_csv(path, cards_path=cards_path)
    return parse_statement(path, cards_path=cards_path, bank_id=bank_id)


@dataclass
class FileImport:
    """What importing one statement file did, or in a dry run would do.

    ``period_from``/``period_to`` are the earliest and latest ``date_posted``
    of the parsed rows (``None`` for a file with no rows). ``error`` is set
    for a file that could not be imported; its counts are then 0.
    """

    path: Path
    parsed: int = 0
    inserted: int = 0
    period_from: str | None = None
    period_to: str | None = None
    error: Exception | None = None

    @property
    def already_present(self) -> int:
        """Parsed rows that were in the ledger already (INSERT OR IGNORE)."""
        return self.parsed - self.inserted

    def summary(self) -> str:
        """The per-file line of the import summary (same in a dry run)."""
        line = (
            f"{self.path.name}: {self.parsed} parsed, {self.inserted} new, "
            f"{self.already_present} already present"
        )
        if self.period_from:
            line += f", {self.period_from} to {self.period_to}"
        return line


@dataclass
class IngestResult:
    """Per-file outcome of ingest(), in file order, plus run-wide counts."""

    files: list[FileImport] = field(default_factory=list)
    warnings: int = 0
    dry_run: bool = False

    @property
    def imported(self) -> list[FileImport]:
        return [f for f in self.files if f.error is None]

    @property
    def failures(self) -> list[tuple[Path, Exception]]:
        return [(f.path, f.error) for f in self.files if f.error is not None]

    @property
    def parsed(self) -> int:
        return sum(f.parsed for f in self.files)

    @property
    def inserted(self) -> int:
        return sum(f.inserted for f in self.files)

    @property
    def already_present(self) -> int:
        return sum(f.already_present for f in self.files)


class IngestError(Exception):
    """Some statement files could not be imported; the others were.

    ``failures`` lists each such file with the exception it raised;
    ``result`` is the IngestResult of the whole run, when there is one.
    """

    def __init__(
        self,
        failures: list[tuple[Path, Exception]],
        total: int,
        result: IngestResult | None = None,
    ) -> None:
        self.failures = failures
        self.result = result
        details = "; ".join(f"{p.name}: {_describe(e)}" for p, e in failures)
        super().__init__(
            f"{len(failures)} of {total} file(s) could not be imported ({details})"
        )


def _describe(error: Exception) -> str:
    """Parsers raise ValueError with a readable message; name anything else."""
    if isinstance(error, ValueError):
        return str(error)
    return f"{type(error).__name__}: {error}"


class _WarningTally(logging.Handler):
    """Print parser warnings as they happen and count them for the summary."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        print(f"  Warning: {record.getMessage()}")


def _open_ledger(db_path: Path, dry_run: bool) -> sqlite3.Connection:
    """The ledger to import into: the file itself, or for a dry run an
    in-memory copy of it, so that nothing a run does (schema migrations
    included) can reach the file, and a missing ledger is not created."""
    if not dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(db_path))
    conn = sqlite3.connect(":memory:")
    if db_path.exists():
        source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            source.backup(conn)
        finally:
            source.close()
    return conn


def _insert_rows(conn: sqlite3.Connection, rows: list[dict], imported_at: str) -> int:
    """Insert one file's rows, skipping ids already stored; return how many
    were new."""
    inserted = 0
    seen: Counter[str] = Counter()
    for r in rows:
        fields = _id_fields(r)
        seen[fields] += 1
        r["transaction_id"] = tx_id(r, seen[fields])
        r["imported_at"] = imported_at

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
            inserted += 1
    return inserted


def ingest(
    db_path: Path,
    file_paths: Iterable[Path],
    cards_path: Path | None = None,
    bank_id: str | None = None,
    dry_run: bool = False,
) -> IngestResult:
    """Parse bank statement files (CSV or PDF) and insert into SQLite with deduplication.

    Supports auto-detection of the bank format. Use bank_id to override.

    Each file is imported in its own transaction. Files that cannot be
    imported are reported and skipped; once the others are in, IngestError
    is raised for them (its ``result`` holds the run's IngestResult).

    With ``dry_run`` the same run happens against an in-memory copy of the
    ledger: the counts and errors are those a real import would give, and
    the ledger file is not touched.
    """
    from .constants import DEFAULT_CARDS

    if cards_path is None:
        cards_path = DEFAULT_CARDS

    conn = _open_ledger(db_path, dry_run)
    # Parsers report rows they could not import with logger.warning()
    warnings = _WarningTally()
    package_logger = logging.getLogger(__package__)
    package_logger.addHandler(warnings)
    result = IngestResult(dry_run=dry_run)
    prefix = "[dry-run] " if dry_run else ""
    try:
        ensure_schema(conn)
        migrate_schema(conn)
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        paths = list(file_paths)
        for p in paths:
            outcome = FileImport(path=p)
            result.files.append(outcome)
            # One transaction per file: a file that cannot be read must not
            # undo, or block, the import of the files around it.
            try:
                rows = _parse_file(p, cards_path, bank_id)
                inserted = _insert_rows(conn, rows, now)
                conn.commit()
            except Exception as e:
                conn.rollback()
                outcome.error = e
                logger.debug("%s could not be imported", p, exc_info=True)
                print(f"  Error: {p.name} was not imported: {_describe(e)}")
                continue
            outcome.parsed = len(rows)
            outcome.inserted = inserted
            if rows:
                dates = [r["date_posted"] for r in rows]
                outcome.period_from, outcome.period_to = min(dates), max(dates)
            print(f"  {outcome.summary()}")

        result.warnings = warnings.count
        print(
            f"{prefix}Parsed {result.parsed} transactions "
            f"from {len(result.imported)} file(s)."
        )
        if dry_run:
            print(
                f"{prefix}Would insert {result.inserted} new transactions into "
                f"{db_path} ({result.already_present} already present). "
                "Nothing was written."
            )
        else:
            print(f"Inserted {result.inserted} new transactions into {db_path}.")
        if warnings.count:
            print(f"{warnings.count} warning(s) while parsing: see the lines above.")
        if result.failures:
            raise IngestError(result.failures, len(paths), result)
        return result
    finally:
        package_logger.removeHandler(warnings)
        conn.close()


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def is_neutral(tx: dict) -> bool:
    """Neither income nor spending: savings or ignored (NEUTRAL_CATEGORIES)."""
    return (tx.get("category") or "") in NEUTRAL_CATEGORIES


def is_ignored(tx: dict) -> bool:
    """Left out of every figure (see IGNORED_CATEGORIES)."""
    return (tx.get("category") or "") in IGNORED_CATEGORIES


def is_refund(tx: dict) -> bool:
    """Incoming money in a spending category (see NON_SPENDING_CATEGORIES)."""
    category = tx.get("category") or ""
    return (
        tx.get("direction") == "in"
        and bool(category)
        and category not in NON_SPENDING_CATEGORIES
        and not is_neutral(tx)
    )


def is_income(tx: dict) -> bool:
    """Incoming money that is neither a refund nor neutral (see is_neutral)."""
    return tx.get("direction") == "in" and not is_refund(tx) and not is_neutral(tx)


def counts_as_spending(tx: dict) -> bool:
    """Outgoing money that is not neutral (see is_neutral), or a refund."""
    return (tx.get("direction") == "out" and not is_neutral(tx)) or is_refund(tx)


def spend_amount(tx: dict) -> float:
    """Contribution to spending: +amount out, -amount for a refund, else 0."""
    if not counts_as_spending(tx):
        return 0.0
    return tx["amount_abs"] if tx.get("direction") == "out" else -tx["amount_abs"]


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# SQL counterparts of the helpers above, for aggregate queries.
NOT_NEUTRAL_SQL = f"COALESCE(category, '') NOT IN ({_sql_list(NEUTRAL_CATEGORIES)})"
REFUND_SQL = (
    "(direction = 'in' AND COALESCE(category, '') <> '' AND category NOT IN "
    f"({_sql_list(NON_SPENDING_CATEGORIES + NEUTRAL_CATEGORIES)}))"
)
INCOME_SQL = f"(direction = 'in' AND NOT {REFUND_SQL} AND {NOT_NEUTRAL_SQL})"
SPEND_SQL = (
    f"CASE WHEN direction = 'out' AND {NOT_NEUTRAL_SQL} THEN amount_abs "
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
        rows = conn.execute("SELECT DISTINCT source_file FROM transactions").fetchall()
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
    rows = conn.execute("""
        SELECT
            transaction_id, date_posted, date_value, month, day_of_week,
            description_raw, description_clean,
            amount_signed, amount_abs, direction, tx_type, balance,
            currency, account, card_last4, payment_type, who,
            category, subcategory, notes, source_file, imported_at
        FROM transactions
        ORDER BY date_posted DESC, rowid DESC
        """).fetchall()
    conn.row_factory = None

    result = []
    for r in rows:
        d = dict(r)
        d["status"] = "categorized" if d.get("category") else "uncategorized"
        d["spend"] = spend_amount(d)
        result.append(d)
    return result
