"""Tests for expense_tracker.db."""

from __future__ import annotations

import sqlite3

import pytest

from expense_tracker.db import (
    AccountCoverage,
    IngestError,
    coverage,
    ensure_schema,
    fetch_all_transactions,
    ingest,
    ingested_source_files,
    is_income,
    is_refund,
    migrate_schema,
    reclean_descriptions,
    spend_amount,
    tx_id,
)
from expense_tracker.parser import _get_cleaning_patterns, reset_cleaning_cache
from tests.conftest import SAMPLE_ROWS, make_utf16_csv

# ---------------------------------------------------------------------------
# tx_id
# ---------------------------------------------------------------------------


class TestTxId:
    def test_deterministic(self):
        row = {
            "account": "checkings_account",
            "date_posted": "2026-01-15",
            "date_value": "2026-01-15",
            "description_raw": "COMPRA 1234 CONTINENTE",
            "amount_signed": -45.50,
            "balance": 1234.56,
        }
        assert tx_id(row) == tx_id(row)

    def test_different_for_different_rows(self):
        row1 = {
            "account": "checkings_account",
            "date_posted": "2026-01-15",
            "date_value": "2026-01-15",
            "description_raw": "COMPRA 1234 CONTINENTE",
            "amount_signed": -45.50,
            "balance": 1234.56,
        }
        row2 = {**row1, "amount_signed": -50.00}
        assert tx_id(row1) != tx_id(row2)

    def test_returns_16_hex_chars(self):
        row = {
            "account": "checkings_account",
            "date_posted": "2026-01-15",
            "date_value": "2026-01-15",
            "description_raw": "TEST",
            "amount_signed": -10.0,
            "balance": 100.0,
        }
        result = tx_id(row)
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_first_occurrence_keeps_the_plain_id(self):
        row = {
            "account": "checkings_account",
            "date_posted": "2026-01-15",
            "date_value": "2026-01-15",
            "description_raw": "TEST",
            "amount_signed": -10.0,
            "balance": 0.0,
        }
        assert tx_id(row, occurrence=1) == tx_id(row)
        # The id every ledger already holds for this row
        assert tx_id(row) == "e45ba8ee15386067"

    def test_later_occurrences_get_their_own_id(self):
        row = {
            "account": "checkings_account",
            "date_posted": "2026-01-15",
            "date_value": "2026-01-15",
            "description_raw": "TEST",
            "amount_signed": -10.0,
            "balance": 0.0,
        }
        ids = {tx_id(row), tx_id(row, occurrence=2), tx_id(row, occurrence=3)}
        assert len(ids) == 3


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_ensure_schema_creates_table(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert ("transactions",) in tables
        conn.close()

    def test_ensure_schema_idempotent(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        ensure_schema(conn)
        ensure_schema(conn)  # should not raise
        conn.close()

    def test_migrate_schema_adds_columns(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        # Create a minimal table missing some columns
        conn.execute("""
            CREATE TABLE transactions (
                transaction_id TEXT PRIMARY KEY,
                date_posted TEXT NOT NULL,
                date_value TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                amount_signed REAL NOT NULL,
                amount_abs REAL NOT NULL,
                direction TEXT NOT NULL,
                tx_type TEXT,
                balance REAL,
                currency TEXT NOT NULL,
                account TEXT NOT NULL,
                card_last4 TEXT,
                category TEXT,
                subcategory TEXT,
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
        """)
        conn.commit()

        migrate_schema(conn)

        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        assert "month" in cols
        assert "day_of_week" in cols
        assert "description_clean" in cols
        assert "payment_type" in cols
        assert "who" in cols
        assert "notes" in cols
        conn.close()

    def test_migrate_schema_idempotent(self, test_db):
        conn = sqlite3.connect(str(test_db))
        migrate_schema(conn)
        migrate_schema(conn)  # should not raise
        conn.close()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class TestIngest:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_ingest_inserts_rows(self, test_db, utf16_csv, cards_csv):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)

        conn = sqlite3.connect(str(test_db))
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        assert count == 5

    def test_deduplication(self, test_db, utf16_csv, cards_csv):
        """Ingesting the same file twice should not create duplicates."""
        ingest(test_db, [utf16_csv], cards_path=cards_csv)
        ingest(test_db, [utf16_csv], cards_path=cards_csv)

        conn = sqlite3.connect(str(test_db))
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        assert count == 5

    def test_creates_db_directory(self, tmp_path, utf16_csv, cards_csv):
        db_path = tmp_path / "new" / "nested" / "ledger.sqlite"
        ingest(db_path, [utf16_csv], cards_path=cards_csv)
        assert db_path.exists()

    def test_empty_file_list(self, test_db, cards_csv):
        ingest(test_db, [], cards_path=cards_csv)
        conn = sqlite3.connect(str(test_db))
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        assert count == 0


class TestIngestIdenticalRows:
    """Identical purchases in one statement without a balance are kept apart."""

    UTF8_HEADER = "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
    COFFEE = "15-01-2026;15-01-2026;COMPRA CAFE CENTRAL;1,20;;\n"
    LUNCH = "15-01-2026;15-01-2026;COMPRA TASCA;9,50;;\n"

    def _statement(self, path, *lines: str):
        path.write_text(self.UTF8_HEADER + "".join(lines), encoding="utf-8")
        return path

    def _count(self, db_path) -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    def test_two_identical_rows_are_both_stored(self, test_db, tmp_path):
        path = self._statement(tmp_path / "jan.csv", self.COFFEE, self.COFFEE)

        ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        assert self._count(test_db) == 2

    def test_same_file_twice_stores_each_row_once(self, test_db, tmp_path, capsys):
        path = self._statement(tmp_path / "jan.csv", self.COFFEE, self.COFFEE)

        ingest(test_db, [path], cards_path=tmp_path / "none.csv")
        capsys.readouterr()
        ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        assert self._count(test_db) == 2
        assert "Inserted 0 new transactions" in capsys.readouterr().out

    def test_wider_statement_adds_only_the_extra_occurrence(self, test_db, tmp_path):
        first = self._statement(tmp_path / "a.csv", self.COFFEE, self.COFFEE)
        wider = self._statement(
            tmp_path / "b.csv", self.COFFEE, self.LUNCH, self.COFFEE, self.COFFEE
        )

        ingest(test_db, [first], cards_path=tmp_path / "none.csv")
        ingest(test_db, [wider], cards_path=tmp_path / "none.csv")

        conn = sqlite3.connect(str(test_db))
        try:
            rows = conn.execute(
                "SELECT description_raw, source_file FROM transactions "
                "ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [
            ("COMPRA CAFE CENTRAL", "a.csv"),
            ("COMPRA CAFE CENTRAL", "a.csv"),
            ("COMPRA TASCA", "b.csv"),
            ("COMPRA CAFE CENTRAL", "b.csv"),
        ]

    def test_unique_rows_keep_their_id(self, test_db, utf16_csv, cards_csv):
        from expense_tracker.parser import parse_utf16_csv

        expected = {tx_id(r) for r in parse_utf16_csv(utf16_csv, cards_csv)}

        ingest(test_db, [utf16_csv], cards_path=cards_csv)

        conn = sqlite3.connect(str(test_db))
        try:
            stored = {
                r[0] for r in conn.execute("SELECT transaction_id FROM transactions")
            }
        finally:
            conn.close()
        assert stored == expected


class TestIngestResult:
    """ingest() returns per-file counts, and a dry run returns the same ones."""

    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _counts(self, result) -> list[tuple]:
        return [
            (f.path.name, f.parsed, f.inserted, f.already_present)
            + (f.period_from, f.period_to)
            for f in result.files
        ]

    def test_result_counts_each_file(self, test_db, tmp_path, cards_csv):
        first = make_utf16_csv(tmp_path / "first.csv", SAMPLE_ROWS[:3])
        overlap = make_utf16_csv(tmp_path / "overlap.csv", SAMPLE_ROWS[1:])

        result = ingest(test_db, [first, overlap], cards_path=cards_csv)

        assert self._counts(result) == [
            ("first.csv", 3, 3, 0, "2026-01-13", "2026-01-15"),
            ("overlap.csv", 4, 2, 2, "2026-01-10", "2026-01-14"),
        ]
        assert (result.parsed, result.inserted, result.already_present) == (7, 5, 2)
        assert result.failures == []
        assert not result.dry_run

    def test_summary_lists_each_file(self, test_db, utf16_csv, cards_csv, capsys):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)

        out = capsys.readouterr().out
        assert (
            f"  {utf16_csv.name}: 5 parsed, 5 new, 0 already present, "
            "2026-01-10 to 2026-01-15" in out
        )

    def test_dry_run_counts_what_the_real_run_inserts(
        self, test_db, tmp_path, cards_csv
    ):
        first = make_utf16_csv(tmp_path / "first.csv", SAMPLE_ROWS[:3])
        ingest(test_db, [first], cards_path=cards_csv)
        overlap = make_utf16_csv(tmp_path / "overlap.csv", SAMPLE_ROWS[1:])
        again = make_utf16_csv(tmp_path / "again.csv", SAMPLE_ROWS)

        dry = ingest(test_db, [overlap, again], cards_path=cards_csv, dry_run=True)
        real = ingest(test_db, [overlap, again], cards_path=cards_csv)

        assert dry.dry_run
        assert self._counts(dry) == self._counts(real)
        assert self._counts(dry)[1][2] == 0  # sees the rows of the file before it

    def test_dry_run_leaves_the_database_byte_identical(
        self, test_db, utf16_csv, cards_csv, capsys
    ):
        before = test_db.read_bytes()

        result = ingest(test_db, [utf16_csv], cards_path=cards_csv, dry_run=True)

        assert result.inserted == 5
        assert test_db.read_bytes() == before
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "Nothing was written" in out

    def test_dry_run_does_not_migrate_an_old_ledger(
        self, tmp_path, utf16_csv, cards_csv
    ):
        db_path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(str(db_path))
        # Before month, day_of_week, description_clean, ... and ods_baseline
        conn.execute("""
            CREATE TABLE transactions (
                transaction_id TEXT PRIMARY KEY,
                date_posted TEXT NOT NULL,
                date_value TEXT NOT NULL,
                description_raw TEXT NOT NULL,
                amount_signed REAL NOT NULL,
                amount_abs REAL NOT NULL,
                direction TEXT NOT NULL,
                tx_type TEXT,
                balance REAL,
                currency TEXT NOT NULL,
                account TEXT NOT NULL,
                card_last4 TEXT,
                category TEXT,
                subcategory TEXT,
                source_file TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        before = db_path.read_bytes()

        ingest(db_path, [utf16_csv], cards_path=cards_csv, dry_run=True)

        assert db_path.read_bytes() == before

    def test_dry_run_without_a_ledger_creates_none(
        self, tmp_path, utf16_csv, cards_csv
    ):
        db_path = tmp_path / "data" / "ledger.sqlite"

        result = ingest(db_path, [utf16_csv], cards_path=cards_csv, dry_run=True)

        assert result.inserted == 5
        assert not db_path.parent.exists()

    def test_bad_file_fails_the_same_way_in_a_dry_run(
        self, test_db, tmp_path, cards_csv, capsys
    ):
        good = make_utf16_csv(tmp_path / "good.csv", SAMPLE_ROWS)
        bad = tmp_path / "junk.csv"
        bad.write_text("not a bank statement\n", encoding="utf-8")
        before = test_db.read_bytes()

        with pytest.raises(IngestError) as dry:
            ingest(test_db, [good, bad], cards_path=cards_csv, dry_run=True)
        dry_out = capsys.readouterr().out
        assert test_db.read_bytes() == before
        with pytest.raises(IngestError) as real:
            ingest(test_db, [good, bad], cards_path=cards_csv)
        real_out = capsys.readouterr().out

        assert str(dry.value) == str(real.value)
        error_line = next(x for x in real_out.splitlines() if "Error:" in x)
        assert error_line in dry_out
        assert dry.value.result.dry_run
        assert self._counts(dry.value.result) == self._counts(real.value.result)


class TestIngestBadFile:
    """One file that cannot be read must not undo, or block, the others."""

    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _sources(self, db_path) -> list[tuple[str, int]]:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(
                "SELECT source_file, COUNT(*) FROM transactions "
                "GROUP BY source_file ORDER BY source_file"
            ).fetchall()
        finally:
            conn.close()

    def test_files_around_a_bad_one_are_imported(
        self, test_db, tmp_path, cards_csv, capsys
    ):
        first = make_utf16_csv(tmp_path / "first.csv", SAMPLE_ROWS[:2])
        bad = tmp_path / "junk.csv"
        bad.write_text("not a bank statement\n", encoding="utf-8")
        last = make_utf16_csv(tmp_path / "last.csv", SAMPLE_ROWS[2:])

        with pytest.raises(IngestError) as error:
            ingest(test_db, [first, bad, last], cards_path=cards_csv)

        assert [p.name for p, _ in error.value.failures] == ["junk.csv"]
        assert "1 of 3" in str(error.value)
        assert "junk.csv" in str(error.value)
        assert self._sources(test_db) == [("first.csv", 2), ("last.csv", 3)]
        out = capsys.readouterr().out
        assert "junk.csv was not imported" in out
        assert "Parsed 5 transactions from 2 file(s)." in out

    def test_row_error_in_one_file_keeps_the_other_file(
        self, test_db, tmp_path, cards_csv
    ):
        good = make_utf16_csv(tmp_path / "good.csv", SAMPLE_ROWS[:2])
        broken = make_utf16_csv(
            tmp_path / "broken.csv",
            [
                SAMPLE_ROWS[2],
                (
                    "12-01-2026",
                    "12-01-2026",
                    "COMPRA 1234 LOJA",
                    "12,50 D",
                    "Compra",
                    "1,00",
                ),
            ],
        )

        with pytest.raises(IngestError, match="broken.csv"):
            ingest(test_db, [good, broken], cards_path=cards_csv)

        assert self._sources(test_db) == [("good.csv", 2)]


class TestIngestParserChoice:
    """--bank is an instruction, and a failed parse is not retried as UTF-16."""

    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _count(self, db_path) -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    def test_unknown_bank_is_an_error(self, test_db, utf16_csv, cards_csv):
        with pytest.raises(IngestError, match="Unknown bank 'nonexistent'"):
            ingest(test_db, [utf16_csv], cards_path=cards_csv, bank_id="nonexistent")

        assert self._count(test_db) == 0

    def test_chosen_parser_is_not_replaced_by_utf16(
        self, test_db, utf16_csv, cards_csv
    ):
        with pytest.raises(IngestError, match="Could not find UTF8 header row"):
            ingest(test_db, [utf16_csv], cards_path=cards_csv, bank_id="utf8")

        assert self._count(test_db) == 0

    def test_unrecognised_csv_is_reported_as_unrecognised(
        self, test_db, tmp_path, cards_csv
    ):
        path = tmp_path / "random.csv"
        path.write_text("not,a,bank,statement\n", encoding="utf-8")

        with pytest.raises(IngestError, match="Could not detect bank format"):
            ingest(test_db, [path], cards_path=cards_csv)

    def test_utf16_statement_under_another_extension_still_imports(
        self, test_db, tmp_path, cards_csv
    ):
        path = make_utf16_csv(tmp_path / "movs.txt", SAMPLE_ROWS)

        ingest(test_db, [path], cards_path=cards_csv)

        assert self._count(test_db) == 5


class TestIngestWarnings:
    """Parser warnings are printed with the run and counted in its summary."""

    UTF8_HEADER = "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"

    def test_skipped_row_is_printed_and_counted(self, test_db, tmp_path, capsys):
        path = tmp_path / "utf8.csv"
        path.write_text(
            self.UTF8_HEADER
            + "15-01-2026;15-01-2026;COMPRA CONTINENTE;45,50;;954,50\n"
            + "16-01-2026;16-01-2026;COMPRA LIDL;12,50 D;;942,00\n",
            encoding="utf-8",
        )

        ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        out = capsys.readouterr().out
        assert "Warning: utf8.csv line 3" in out
        assert "1 warning(s)" in out
        conn = sqlite3.connect(str(test_db))
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_pdf_balance_mismatch_is_counted(self, test_db, tmp_path, capsys):
        from tests.test_pdf_parser import _make_text_pdf

        path = _make_text_pdf(
            tmp_path / "statement.pdf",
            [
                "EXTRATO DE 2026/02/02 A 2026/02/27",
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.06 2.06 TRF MB WAY DE ALICE 20.00 1 549.13",
            ],
        )

        ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        out = capsys.readouterr().out
        assert "Warning: statement.pdf" in out
        assert "1 warning(s)" in out

    def test_unread_pdf_line_is_counted(self, test_db, tmp_path, capsys):
        from tests.test_pdf_parser import _make_text_pdf

        path = _make_text_pdf(
            tmp_path / "statement.pdf",
            [
                "EXTRATO DE 2026/02/02 A 2026/02/27",
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.05 2.05 COMPRA CONTINENTE 12,50 1 516,63",
            ],
        )

        ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        out = capsys.readouterr().out
        assert "Warning: statement.pdf: line not imported" in out
        assert "2.05 2.05 COMPRA CONTINENTE 12,50 1 516,63" in out
        assert "1 warning(s)" in out

    def test_clean_run_has_no_warning_line(self, test_db, utf16_csv, cards_csv, capsys):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)

        out = capsys.readouterr().out
        assert "Warning:" not in out
        assert "warning(s)" not in out


# ---------------------------------------------------------------------------
# ingested_source_files
# ---------------------------------------------------------------------------


UTF8_HEADER = "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"


def _utf8_statement(path, *dates: str):
    """A UTF-8 statement with one purchase per dd-mm-YYYY date."""
    lines = [f"{d};{d};COMPRA LOJA {i};1,00;;\n" for i, d in enumerate(dates)]
    path.write_text(UTF8_HEADER + "".join(lines), encoding="utf-8")
    return path


def _imports(db_path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT source_file, account, period_from, period_to, "
            "rows_parsed, rows_inserted FROM imports ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class TestImportsTable:
    """Each import that adds rows is recorded with the period it covers."""

    @pytest.fixture(autouse=True)
    def _account(self, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")

    def test_import_is_recorded(self, test_db, tmp_path):
        jan = _utf8_statement(tmp_path / "jan.csv", "20-01-2026", "05-01-2026")

        ingest(test_db, [jan], cards_path=tmp_path / "none.csv")

        assert _imports(test_db) == [
            ("jan.csv", "checkings_account", "2026-01-05", "2026-01-20", 2, 2)
        ]

    def test_reimport_that_adds_nothing_records_nothing(self, test_db, tmp_path):
        jan = _utf8_statement(tmp_path / "jan.csv", "05-01-2026")
        ingest(test_db, [jan], cards_path=tmp_path / "none.csv")

        ingest(test_db, [jan], cards_path=tmp_path / "none.csv")

        assert len(_imports(test_db)) == 1

    def test_failed_import_records_nothing(self, test_db, tmp_path):
        bad = tmp_path / "junk.csv"
        bad.write_text("not a bank statement\n", encoding="utf-8")

        with pytest.raises(IngestError):
            ingest(test_db, [bad], cards_path=tmp_path / "none.csv")

        assert _imports(test_db) == []

    def test_dry_run_records_nothing(self, test_db, tmp_path):
        jan = _utf8_statement(tmp_path / "jan.csv", "05-01-2026")

        ingest(test_db, [jan], cards_path=tmp_path / "none.csv", dry_run=True)

        assert _imports(test_db) == []

    def test_old_ledger_is_seeded_from_its_transactions(
        self, tmp_path, utf16_csv, cards_csv, noise_words, cleaning_patterns
    ):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        db_path = tmp_path / "old.sqlite"
        ingest(db_path, [utf16_csv], cards_path=cards_csv)
        jan = _utf8_statement(tmp_path / "feb.csv", "03-02-2026", "17-02-2026")
        ingest(db_path, [jan], cards_path=cards_csv)
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE imports")  # a ledger from before the table
        conn.commit()

        migrate_schema(conn)
        migrate_schema(conn)  # seeds only when it creates the table
        conn.close()
        reset_cleaning_cache()

        assert sorted(_imports(db_path)) == [
            (utf16_csv.name, "checkings_account", "2026-01-10", "2026-01-15", 5, 5),
            ("feb.csv", "checkings_account", "2026-02-03", "2026-02-17", 2, 2),
        ]


class TestCoverage:
    """Months covered by imported statements, and the gaps between them."""

    @pytest.fixture(autouse=True)
    def _account(self, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")

    def _coverage(self, db_path) -> list[AccountCoverage]:
        conn = sqlite3.connect(str(db_path))
        try:
            return coverage(conn)
        finally:
            conn.close()

    def _ingest(self, db_path, tmp_path, name, *dates):
        path = _utf8_statement(tmp_path / name, *dates)
        ingest(db_path, [path], cards_path=tmp_path / "none.csv")

    def test_missing_month_between_two_statements(self, test_db, tmp_path):
        self._ingest(test_db, tmp_path, "jan.csv", "02-01-2026", "30-01-2026")
        self._ingest(test_db, tmp_path, "mar.csv", "03-03-2026", "28-03-2026")

        assert self._coverage(test_db) == [
            AccountCoverage(
                account="checkings_account",
                covered=[("2026-01", "2026-01"), ("2026-03", "2026-03")],
                missing=["2026-02"],
            )
        ]

    def test_adding_the_missing_month_closes_the_gap(self, test_db, tmp_path):
        self._ingest(test_db, tmp_path, "jan.csv", "02-01-2026")
        self._ingest(test_db, tmp_path, "mar.csv", "03-03-2026")
        self._ingest(test_db, tmp_path, "feb.csv", "10-02-2026")

        assert self._coverage(test_db) == [
            AccountCoverage(
                account="checkings_account",
                covered=[("2026-01", "2026-03")],
                missing=[],
            )
        ]

    def test_statement_spanning_months_and_overlaps_merge(self, test_db, tmp_path):
        self._ingest(test_db, tmp_path, "a.csv", "20-11-2025", "10-01-2026")
        self._ingest(test_db, tmp_path, "b.csv", "05-12-2025", "15-12-2025")
        self._ingest(test_db, tmp_path, "c.csv", "01-04-2026")

        assert self._coverage(test_db) == [
            AccountCoverage(
                account="checkings_account",
                covered=[("2025-11", "2026-01"), ("2026-04", "2026-04")],
                missing=["2026-02", "2026-03"],
            )
        ]

    def test_accounts_are_listed_separately(self, test_db, tmp_path, monkeypatch):
        self._ingest(test_db, tmp_path, "jan.csv", "02-01-2026")
        self._ingest(test_db, tmp_path, "mar.csv", "03-03-2026")
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "savings_account")
        self._ingest(test_db, tmp_path, "s-feb.csv", "10-02-2026")

        assert self._coverage(test_db) == [
            AccountCoverage(
                account="checkings_account",
                covered=[("2026-01", "2026-01"), ("2026-03", "2026-03")],
                missing=["2026-02"],
            ),
            AccountCoverage(
                account="savings_account",
                covered=[("2026-02", "2026-02")],
                missing=[],
            ),
        ]

    def test_empty_ledger_has_no_coverage(self, test_db):
        assert self._coverage(test_db) == []


class TestIngestedSourceFiles:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_returns_source_files(self, test_db, utf16_csv, cards_csv):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)
        files = ingested_source_files(test_db)
        assert utf16_csv.name in files

    def test_missing_db_returns_empty(self, tmp_path):
        files = ingested_source_files(tmp_path / "nonexistent.db")
        assert files == set()


# ---------------------------------------------------------------------------
# reclean_descriptions
# ---------------------------------------------------------------------------


class TestRecleanDescriptions:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_no_changes_when_clean(self, populated_db):
        updated = reclean_descriptions(populated_db)
        assert updated == 0

    def test_updates_stale_descriptions(self, populated_db):
        # Manually corrupt a description_clean to simulate stale data
        conn = sqlite3.connect(str(populated_db))
        conn.execute(
            "UPDATE transactions SET description_clean = 'OLD VALUE' "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        )
        conn.commit()
        conn.close()

        updated = reclean_descriptions(populated_db)
        assert updated >= 1

        # Verify the description was actually re-cleaned
        conn = sqlite3.connect(str(populated_db))
        row = conn.execute(
            "SELECT description_clean FROM transactions "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        ).fetchone()
        conn.close()
        assert row[0] != "OLD VALUE"
        assert "CONTINENTE" in row[0]


# ---------------------------------------------------------------------------
# fetch_all_transactions
# ---------------------------------------------------------------------------


class TestFetchAllTransactions:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_returns_all_rows(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        txs = fetch_all_transactions(conn)
        conn.close()
        assert len(txs) == 5

    def test_sorted_by_date_desc(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        txs = fetch_all_transactions(conn)
        conn.close()
        dates = [t["date_posted"] for t in txs]
        assert dates == sorted(dates, reverse=True)

    def test_status_field(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        txs = fetch_all_transactions(conn)
        conn.close()
        # All should be uncategorized initially
        assert all(t["status"] == "uncategorized" for t in txs)

    def test_miscellaneous_is_categorized_spending(self, populated_db):
        """Miscellaneous is an ordinary spending category (issue #9)."""
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        conn.execute(
            "UPDATE transactions SET category = 'Miscellaneous' "
            "WHERE description_raw LIKE '%FARMACIA%'"
        )
        conn.commit()
        txs = fetch_all_transactions(conn)
        conn.close()
        (misc,) = [t for t in txs if t["category"] == "Miscellaneous"]
        assert misc["status"] == "categorized"
        assert misc["spend"] == misc["amount_abs"]

    def test_spend_field(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        conn.execute(
            "UPDATE transactions SET direction = 'in', category = 'Health' "
            "WHERE description_raw LIKE '%FARMACIA%'"
        )
        txs = fetch_all_transactions(conn)
        conn.close()
        by_desc = {t["description_raw"]: t["spend"] for t in txs}
        assert by_desc["COMPRA 1234 CONTINENTE PORTO"] == 45.50
        assert by_desc["COMPRA 5678 FARMACIA DA GARE 1000-001 LISBOA"] == -12.80
        assert by_desc["TRANSFERENCIA - SALARIO"] == 0

    def test_empty_db(self, test_db):
        conn = sqlite3.connect(str(test_db))
        txs = fetch_all_transactions(conn)
        conn.close()
        assert txs == []


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def _t(direction: str, category: str | None, amount: float = 10.0) -> dict:
    return {"direction": direction, "category": category, "amount_abs": amount}


class TestRefunds:
    @pytest.mark.parametrize(
        "tx, expected",
        [
            (_t("in", "Health"), True),
            (_t("in", "Income"), False),
            (_t("in", "Transfers"), False),
            (_t("in", ""), False),
            (_t("in", None), False),
            (_t("out", "Health"), False),
            (_t("in", "Savings"), False),
            (_t("in", "Ignored"), False),
        ],
    )
    def test_is_refund(self, tx, expected):
        assert is_refund(tx) is expected

    @pytest.mark.parametrize(
        "tx, expected",
        [
            (_t("out", "Health", 30), 30),
            (_t("out", "", 30), 30),
            (_t("in", "Health", 30), -30),
            (_t("in", "Income", 30), 0),
            (_t("in", "", 30), 0),
            (_t("out", "Savings", 30), 0),
            (_t("in", "Savings", 30), 0),
            (_t("out", "Ignored", 30), 0),
            (_t("in", "Ignored", 30), 0),
        ],
    )
    def test_spend_amount(self, tx, expected):
        assert spend_amount(tx) == expected

    @pytest.mark.parametrize(
        "tx, expected",
        [
            (_t("in", "Income"), True),
            (_t("in", "Transfers"), True),
            (_t("in", ""), True),
            (_t("in", "Health"), False),
            (_t("in", "Savings"), False),
            (_t("in", "Ignored"), False),
            (_t("out", "Income"), False),
        ],
    )
    def test_is_income(self, tx, expected):
        assert is_income(tx) is expected


class TestSqlHelpers:
    """The SQL helpers agree with the Python ones, Ignored included (#6)."""

    @pytest.mark.parametrize(
        "direction, category, spend, income",
        [
            ("out", "Health", 30, 0),
            ("in", "Health", -30, 0),
            ("in", "Income", 0, 30),
            ("in", "Transfers", 0, 30),
            ("out", "Transfers", 30, 0),
            ("out", "Savings", 0, 0),
            ("in", "Savings", 0, 0),
            ("out", "Ignored", 0, 0),
            ("in", "Ignored", 0, 0),
        ],
    )
    def test_spend_and_income(self, direction, category, spend, income):
        from expense_tracker.db import INCOME_SQL, SPEND_SQL

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE transactions (direction TEXT, category TEXT, amount_abs REAL)"
        )
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, 30)", (direction, category)
        )
        got = conn.execute(
            f"SELECT {SPEND_SQL}, CASE WHEN {INCOME_SQL} THEN amount_abs ELSE 0 END "
            "FROM transactions"
        ).fetchone()
        conn.close()
        assert got == (spend, income)
        assert spend_amount(_t(direction, category, 30)) == spend
        assert is_income(_t(direction, category)) is bool(income)
