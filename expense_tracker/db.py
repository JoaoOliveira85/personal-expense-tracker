"""SQLite storage: schema, ingestion, migrations, queries."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple

from .constants import IGNORED_CATEGORIES, NEUTRAL_CATEGORIES, NON_SPENDING_CATEGORIES
from .parser import CARD_RE, extract_account_id, load_card_holders, parse_utf16_csv
from .parsers import resolve_parser

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
            who               TEXT, -- unused: see apply_card_holders()
            category          TEXT,
            subcategory       TEXT,
            category_source   TEXT,
            notes             TEXT,
            source_file       TEXT NOT NULL,
            imported_at       TEXT NOT NULL,
            source_format     TEXT,
            suspected_of      TEXT
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
        # Written by imports before issue #4; never read (apply_card_holders)
        ("who", "TEXT"),
        ("notes", "TEXT"),
        # 'rule' | 'manual' | NULL (uncategorized); see categorize_transactions
        ("category_source", "TEXT"),
        # The parser that read the row (bank_id, or 'pdf'); NULL before it
        # was recorded, and such rows are never matched by _flag_suspected
        ("source_format", "TEXT"),
        # transaction_id of the row, imported from another format, this one
        # looks like; NULL when not suspected or cleared by the user
        ("suspected_of", "TEXT"),
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

    # One row per account per import that added transactions; coverage()
    # reads it. A ledger from before the table gets one pseudo-import per
    # statement file, spanning that file's earliest to latest transaction.
    has_imports = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'imports'"
    ).fetchone()
    if not has_imports:
        conn.execute("""
            CREATE TABLE imports (
                id            INTEGER PRIMARY KEY,
                source_file   TEXT NOT NULL,
                account       TEXT NOT NULL,
                period_from   TEXT NOT NULL,
                period_to     TEXT NOT NULL,
                imported_at   TEXT NOT NULL,
                rows_parsed   INTEGER NOT NULL,
                rows_inserted INTEGER NOT NULL
            )
            """)
        conn.execute("""
            INSERT INTO imports (
                source_file, account, period_from, period_to, imported_at,
                rows_parsed, rows_inserted
            )
            SELECT source_file, account, MIN(date_posted), MAX(date_posted),
                   MIN(imported_at), COUNT(*), COUNT(*)
            FROM transactions
            GROUP BY source_file, account
            ORDER BY MIN(imported_at), source_file, account
            """)

    # The salt of the account fingerprints (see _account_fingerprint): one
    # random value per installation, created with the table.
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
    ).fetchone()
    if not has_settings:
        conn.execute("""
            CREATE TABLE settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('fingerprint_salt', ?)",
            (secrets.token_hex(32),),
        )
    _backfill_card_last4(conn)
    # Per account label, the fingerprint of the account identifier of the
    # first statement imported into it that printed one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_fingerprints (
            account     TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """)
    # The currency of each account label (see set_account_currency). A ledger
    # from before the table gets its labels with the currency of their rows.
    has_accounts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
    ).fetchone()
    if not has_accounts:
        conn.execute("""
            CREATE TABLE accounts (
                label    TEXT PRIMARY KEY,
                currency TEXT NOT NULL
            )
            """)
        conn.execute("""
            INSERT INTO accounts (label, currency)
            SELECT account, MIN(currency) FROM transactions GROUP BY account
            """)
    conn.commit()


def _backfill_card_last4(conn: sqlite3.Connection) -> None:
    """Once per ledger: store the card digits of rows imported while only
    known cards were kept (card_last4 NULL), from their raw description.

    Recorded in ``settings``, so a row with no four-digit group is not
    scanned again on every migration.
    """
    done = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'card_last4_backfilled'"
    ).fetchone()
    if done:
        return
    rows = conn.execute(
        "SELECT rowid, description_raw FROM transactions WHERE card_last4 IS NULL"
    ).fetchall()
    for rowid, description in rows:
        m = CARD_RE.search(description or "")
        if m:
            conn.execute(
                "UPDATE transactions SET card_last4 = ? WHERE rowid = ?",
                (m.group(1), rowid),
            )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('card_last4_backfilled', '1')"
    )


# ---------------------------------------------------------------------------
# Account currency
# ---------------------------------------------------------------------------


class CurrencyError(ValueError):
    """A currency code that is not valid, one that may not be set, or no
    single currency to report on. ``currencies`` lists the ledger's, when
    the error is that several need a choice."""

    def __init__(self, message: str, currencies: list[str] | None = None) -> None:
        self.currencies = currencies or []
        super().__init__(message)


def normalize_currency(code: str, source: str = "currency") -> str:
    """``code`` upper-cased; CurrencyError unless it is three letters."""
    normalized = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized):
        raise CurrencyError(
            f"{source} '{code}' is not a currency code: use three letters, "
            "such as EUR or USD"
        )
    return normalized


def _default_currency() -> str:
    from .constants import default_currency

    return normalize_currency(default_currency(), "EXPENSE_TRACKER_CURRENCY")


def _rows_currencies(conn: sqlite3.Connection, account: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT currency FROM transactions WHERE account = ? "
        "ORDER BY currency",
        (account,),
    ).fetchall()
    return [code for (code,) in rows]


def _account_currency(conn: sqlite3.Connection, account: str, default: str) -> str:
    """The currency of ``account``, which is created with the currency of
    its rows (when some were stored without an ``accounts`` row) or else
    ``default``. In the caller's transaction: a failed import creates none."""
    stored = conn.execute(
        "SELECT currency FROM accounts WHERE label = ?", (account,)
    ).fetchone()
    if stored:
        return stored[0]
    currency = (_rows_currencies(conn, account) or [default])[0]
    conn.execute(
        "INSERT INTO accounts (label, currency) VALUES (?, ?)", (account, currency)
    )
    return currency


def set_account_currency(conn: sqlite3.Connection, label: str, code: str) -> str:
    """Give the account ``label`` the currency ``code`` (three letters, any
    case); return it upper-cased.

    An account that does not exist yet is created, so its first import uses
    the currency. Once it holds transactions its currency cannot change
    (CurrencyError); setting the one it has is accepted.
    """
    currency = normalize_currency(code)
    held = _rows_currencies(conn, label)
    if held and held != [currency]:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE account = ?", (label,)
        ).fetchone()[0]
        raise CurrencyError(
            f"account '{label}' holds {count} transaction(s) in "
            f"{', '.join(held)}: its currency cannot be changed"
        )
    conn.execute(
        "INSERT OR REPLACE INTO accounts (label, currency) VALUES (?, ?)",
        (label, currency),
    )
    conn.commit()
    return currency


def list_accounts(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Each account as (label, currency, transaction count), by label."""
    return conn.execute("""
        SELECT a.label, a.currency, COUNT(t.transaction_id)
        FROM accounts a LEFT JOIN transactions t ON t.account = a.label
        GROUP BY a.label, a.currency
        ORDER BY a.label
        """).fetchall()


def ledger_currencies(conn: sqlite3.Connection) -> list[str]:
    """The currencies the ledger's transactions are in, sorted."""
    rows = conn.execute(
        "SELECT DISTINCT currency FROM transactions ORDER BY currency"
    ).fetchall()
    return [code for (code,) in rows]


def resolve_currency(conn: sqlite3.Connection, currency: str | None = None) -> str:
    """The one currency a report shows: ``currency`` if given, else the
    ledger's only one (the default currency for an empty ledger).

    Amounts of different currencies are never added together, so a ledger
    with several needs a choice (CurrencyError, with ``currencies``), and
    a currency it holds no transaction in is refused.
    """
    held = ledger_currencies(conn)
    if currency:
        code = normalize_currency(currency)
        if held and code not in held:
            raise CurrencyError(
                f"the ledger holds no transaction in {code} (it holds "
                f"{', '.join(held)})",
                held,
            )
        return code
    if len(held) > 1:
        raise CurrencyError(
            f"the ledger holds transactions in several currencies "
            f"({', '.join(held)}), and their amounts are never added "
            "together: choose one",
            held,
        )
    return held[0] if held else _default_currency()


# ---------------------------------------------------------------------------
# Card holders
# ---------------------------------------------------------------------------

JOINT = "Joint"


def apply_card_holders(rows: list[dict], cards_path: Path | None = None) -> None:
    """Set each row's ``who`` from the card-holder file, as it is now.

    A row whose ``card_last4`` is empty or has no holder is ``Joint``. Every
    read path calls this, so adding, renaming or removing a mapping changes
    old transactions too; the ``who`` column stored by older imports is not
    read.
    """
    if cards_path is None:
        from .constants import DEFAULT_CARDS

        cards_path = DEFAULT_CARDS
    holders = load_card_holders(cards_path)
    for r in rows:
        r["who"] = holders.get(r.get("card_last4") or "", JOINT)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


PDF_FORMAT = "pdf"
UTF16_FORMAT = "utf16"


class _ParsedFile(NamedTuple):
    rows: list[dict]
    # The parser's bank_id, or 'pdf'
    source_format: str
    # From the statement's header; never stored, printed or logged
    account_id: str | None
    # Signs the running balance could not decide (PDF text lines): set
    # from the credit patterns
    signs_guessed: int = 0


def _parse_file(
    path: Path, cards_path: Path, bank_id: str | None = None
) -> _ParsedFile:
    """Parse one statement with the parser its extension and bank_id call for.

    Returns the rows, the format that read them (the parser's bank_id, or
    ``pdf``) and the account identifier the statement prints, if any.

    An error from that parser is the answer: it is not retried with the
    UTF-16 parser, which would hide an unknown or mistaken bank_id and report
    a missing UTF-16 header for a file that is not UTF-16 at all.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_parser import extract_pdf_account_id, read_pdf_statement

        rows, signs_guessed = read_pdf_statement(path)
        return _ParsedFile(
            rows, PDF_FORMAT, extract_pdf_account_id(path), signs_guessed
        )
    if bank_id is None and suffix != ".csv":
        # The registered parsers only claim .csv files: a UTF-16 export saved
        # under another extension still goes to the legacy UTF-16 parser.
        rows = parse_utf16_csv(path, cards_path=cards_path)
        return _ParsedFile(rows, UTF16_FORMAT, extract_account_id(path))
    parser = resolve_parser(path, bank_id)
    rows = parser.parse(path, cards_path=cards_path)
    extract = getattr(parser, "extract_account_id", None)
    return _ParsedFile(rows, parser.bank_id, extract(path) if extract else None)


@dataclass
class FileImport:
    """What importing one statement file did, or in a dry run would do.

    ``period_from``/``period_to`` are the earliest and latest ``date_posted``
    of the parsed rows (``None`` for a file with no rows). ``error`` is set
    for a file that could not be imported; its counts are then 0.
    ``signs_guessed`` counts the rows whose sign came from the credit
    patterns because no running balance could decide it (PDF text lines).
    """

    path: Path
    parsed: int = 0
    inserted: int = 0
    period_from: str | None = None
    period_to: str | None = None
    suspected: int = 0
    signs_guessed: int = 0
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
        if self.suspected:
            line += f", {self.suspected} suspected duplicate(s)"
        if self.signs_guessed:
            line += f", {self.signs_guessed} sign(s) from credit patterns"
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

    @property
    def suspected(self) -> int:
        return sum(f.suspected for f in self.files)

    @property
    def signs_guessed(self) -> int:
        return sum(f.signs_guessed for f in self.files)


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


class AccountMismatchError(ValueError):
    """A statement's account identifier differs from the one recorded for
    the account label it is being imported into. The message names the
    label and the file, never the identifier."""

    def __init__(self, account: str, file_name: str) -> None:
        self.account = account
        super().__init__(
            f"the statement is for another account than the one imported as "
            f"'{account}' (its account number differs). Check the account "
            f"label (EXPENSE_TRACKER_ACCOUNT_TYPE), or import {file_name} with "
            "--force (in the GUI: 'Import even if the account does not match') "
            "to import it anyway"
        )


def _account_fingerprint(conn: sqlite3.Connection, identifier: str) -> str:
    """SHA-256 of the installation's salt and the identifier, spaces removed
    and upper-cased: the only form in which an identifier is kept."""
    (salt,) = conn.execute(
        "SELECT value FROM settings WHERE key = 'fingerprint_salt'"
    ).fetchone()
    normalized = re.sub(r"\s+", "", identifier).upper()
    return hashlib.sha256(f"{salt}:{normalized}".encode("utf-8")).hexdigest()


def _check_account(
    conn: sqlite3.Connection,
    rows: list[dict],
    identifier: str,
    path: Path,
    force: bool,
    now: str,
) -> None:
    """Compare the statement's account with the one each label holds.

    The first statement with an identifier imported into a label records
    its fingerprint; later ones must match it. A mismatch raises
    AccountMismatchError unless ``force``, which imports anyway and keeps
    the recorded fingerprint.
    """
    fingerprint = _account_fingerprint(conn, identifier)
    for account in sorted({r["account"] for r in rows}):
        stored = conn.execute(
            "SELECT fingerprint FROM account_fingerprints WHERE account = ?",
            (account,),
        ).fetchone()
        if stored is None:
            conn.execute(
                "INSERT INTO account_fingerprints (account, fingerprint, "
                "recorded_at) VALUES (?, ?, ?)",
                (account, fingerprint, now),
            )
        elif stored[0] != fingerprint:
            if not force:
                raise AccountMismatchError(account, path.name)
            print(
                f"  Note: {path.name} is for another account than '{account}'; "
                "imported anyway (--force)."
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


def _insert_rows(
    conn: sqlite3.Connection, rows: list[dict], imported_at: str
) -> list[dict]:
    """Insert one file's rows, skipping ids already stored; return the rows
    that were new."""
    inserted = []
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
                currency, account, card_last4, payment_type,
                category, subcategory,
                source_file, imported_at, source_format
            ) VALUES (
                :transaction_id, :date_posted, :date_value, :month, :day_of_week,
                :description_raw, :description_clean,
                :amount_signed, :amount_abs, :direction, :tx_type, :balance,
                :currency, :account, :card_last4, :payment_type,
                NULL, NULL,
                :source_file, :imported_at, :source_format
            )
            """,
            r,
        )
        if cur.rowcount > 0:
            inserted.append(r)
    return inserted


def _flag_suspected(conn: sqlite3.Connection, inserted: list[dict]) -> int:
    """Flag each new row that looks like one stored from another format.

    The CSV export and the PDF statement of a month print the same
    transaction differently, so their ids never match. A new row with the
    account, posting date and amount of a row read by another parser gets
    that row's id (the earliest stored) in ``suspected_of``. It is kept and
    counted as usual: the user decides. Returns how many were flagged.
    """
    flagged = 0
    for r in inserted:
        match = conn.execute(
            """
            SELECT transaction_id FROM transactions
            WHERE account = ? AND date_posted = ?
              AND ROUND(amount_signed, 2) = ROUND(?, 2)
              AND source_format IS NOT NULL AND source_format <> ?
            ORDER BY imported_at, rowid
            LIMIT 1
            """,
            (r["account"], r["date_posted"], r["amount_signed"], r["source_format"]),
        ).fetchone()
        if match:
            conn.execute(
                "UPDATE transactions SET suspected_of = ? WHERE transaction_id = ?",
                (match[0], r["transaction_id"]),
            )
            flagged += 1
    return flagged


def clear_suspected(conn: sqlite3.Connection, transaction_ids: Iterable[str]) -> int:
    """Clear the suspected-duplicate flag of these rows; return how many
    were flagged. The rows themselves are kept."""
    cleared = 0
    for tid in transaction_ids:
        cur = conn.execute(
            "UPDATE transactions SET suspected_of = NULL "
            "WHERE transaction_id = ? AND suspected_of IS NOT NULL",
            (tid,),
        )
        cleared += cur.rowcount
    conn.commit()
    return cleared


def _record_import(
    conn: sqlite3.Connection,
    rows: list[dict],
    inserted: list[dict],
    imported_at: str,
) -> None:
    """Add an ``imports`` row per account of a file that added transactions:
    an import that adds nothing leaves the coverage as it was."""
    by_account: dict[str, list[dict]] = {}
    for r in rows:
        by_account.setdefault(r["account"], []).append(r)
    new_by_account = Counter(r["account"] for r in inserted)
    for account, account_rows in by_account.items():
        if not new_by_account[account]:
            continue
        dates = [r["date_posted"] for r in account_rows]
        conn.execute(
            """
            INSERT INTO imports (
                source_file, account, period_from, period_to, imported_at,
                rows_parsed, rows_inserted
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_rows[0]["source_file"],
                account,
                min(dates),
                max(dates),
                imported_at,
                len(account_rows),
                new_by_account[account],
            ),
        )


def ingest(
    db_path: Path,
    file_paths: Iterable[Path],
    cards_path: Path | None = None,
    bank_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> IngestResult:
    """Parse bank statement files (CSV or PDF) and insert into SQLite with deduplication.

    Supports auto-detection of the bank format. Use bank_id to override.

    Each file is imported in its own transaction. Files that cannot be
    imported are reported and skipped; once the others are in, IngestError
    is raised for them (its ``result`` holds the run's IngestResult).

    With ``dry_run`` the same run happens against an in-memory copy of the
    ledger: the counts and errors are those a real import would give, and
    the ledger file is not touched.

    A statement that prints its account identifier is checked against the
    account label it is imported into (see _check_account); ``force``
    imports one that does not match.

    ``cards_path`` is no longer read: the card digits are stored and the
    holder is looked up when transactions are read (apply_card_holders).
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
        # Checked before any file: a mistyped setting must not import some
        default_currency = _default_currency()

        paths = list(file_paths)
        for p in paths:
            outcome = FileImport(path=p)
            result.files.append(outcome)
            # One transaction per file: a file that cannot be read must not
            # undo, or block, the import of the files around it.
            try:
                parsed = _parse_file(p, cards_path, bank_id)
                rows, source_format, account_id = parsed[:3]
                if account_id is not None:
                    _check_account(conn, rows, account_id, p, force, now)
                # The parsers do not know the ledger: each row takes the
                # currency of its account, created if new
                currencies = {
                    account: _account_currency(conn, account, default_currency)
                    for account in sorted({r["account"] for r in rows})
                }
                for r in rows:
                    r["source_format"] = source_format
                    r["currency"] = currencies[r["account"]]
                inserted = _insert_rows(conn, rows, now)
                suspected = _flag_suspected(conn, inserted)
                _record_import(conn, rows, inserted, now)
                conn.commit()
            except Exception as e:
                conn.rollback()
                outcome.error = e
                logger.debug("%s could not be imported", p, exc_info=True)
                print(f"  Error: {p.name} was not imported: {_describe(e)}")
                continue
            outcome.parsed = len(rows)
            outcome.inserted = len(inserted)
            outcome.suspected = suspected
            outcome.signs_guessed = parsed.signs_guessed
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
        if result.suspected:
            print(
                f"{prefix}{result.suspected} suspected duplicate(s) of rows from "
                "another format: they are kept and counted; review them on the "
                "GUI's Transactions page."
            )
        if result.signs_guessed:
            print(
                f"{prefix}{result.signs_guessed} amount sign(s) could not be read "
                "from the running balance (no balance on the line or before it) "
                "and were set from data/credit-patterns.csv: check them against "
                "the statement."
            )
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


def currency_sql(currency: str | None) -> tuple[str, tuple]:
    """A condition to AND into a query, with its parameters: the rows in
    ``currency``, or every row for ``None``."""
    return ("currency = ?", (currency,)) if currency else ("1 = 1", ())


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


@dataclass
class AccountCoverage:
    """The months of one account that imported statements cover.

    ``covered`` holds runs of consecutive months as ``(first, last)``
    ``YYYY-MM`` pairs; ``missing`` the months between the first and the last
    covered month that no import covers.
    """

    account: str
    covered: list[tuple[str, str]]
    missing: list[str]

    def ranges(self) -> list[str]:
        """``covered`` as text: ``2026-01 to 2026-03``, or ``2026-05`` alone."""
        return [a if a == b else f"{a} to {b}" for a, b in self.covered]


def _month_index(date: str) -> int:
    """'YYYY-MM...' as a count of months, so that adjacent months differ by 1."""
    return int(date[:4]) * 12 + int(date[5:7]) - 1


def _month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def coverage(conn: sqlite3.Connection) -> list[AccountCoverage]:
    """Per account (sorted), the months covered by imports and the gaps.

    Each import covers every month from its ``period_from`` to its
    ``period_to``; overlapping and adjacent runs are merged. Months are the
    unit: a month an import covers only in part counts as covered.
    """
    rows = conn.execute("SELECT account, period_from, period_to FROM imports")
    runs_by_account: dict[str, list[tuple[int, int]]] = {}
    for account, period_from, period_to in rows:
        runs_by_account.setdefault(account, []).append(
            (_month_index(period_from), _month_index(period_to))
        )

    result = []
    for account in sorted(runs_by_account):
        merged: list[list[int]] = []
        for first, last in sorted(runs_by_account[account]):
            if merged and first <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        missing = [
            _month_label(m)
            for (_, prev_last), (next_first, _) in zip(merged, merged[1:])
            for m in range(prev_last + 1, next_first)
        ]
        result.append(
            AccountCoverage(
                account=account,
                covered=[(_month_label(a), _month_label(b)) for a, b in merged],
                missing=missing,
            )
        )
    return result


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


def fetch_all_transactions(
    conn: sqlite3.Connection,
    cards_path: Path | None = None,
    currency: str | None = None,
) -> list[dict]:
    """Fetch all transactions as a list of dicts, sorted by date desc.

    ``who`` comes from the card-holder file (``cards_path``, by default
    DEFAULT_CARDS) via apply_card_holders(), not from the stored column.
    With ``currency``, only the transactions in that currency.
    """
    where, params = ("WHERE currency = ?", (currency,)) if currency else ("", ())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT
            transaction_id, date_posted, date_value, month, day_of_week,
            description_raw, description_clean,
            amount_signed, amount_abs, direction, tx_type, balance,
            currency, account, card_last4, payment_type,
            category, subcategory, notes, source_file, imported_at,
            source_format, suspected_of
        FROM transactions
        {where}
        ORDER BY date_posted DESC, rowid DESC
        """,
        params,
    ).fetchall()
    conn.row_factory = None

    result = []
    for r in rows:
        d = dict(r)
        d["status"] = "categorized" if d.get("category") else "uncategorized"
        d["spend"] = spend_amount(d)
        result.append(d)
    apply_card_holders(result, cards_path)
    return result
