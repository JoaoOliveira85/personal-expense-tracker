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
        assert "source_format" in cols
        assert "suspected_of" in cols
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

    def test_pdf_line_the_balance_disproves_fails_the_file(
        self, test_db, tmp_path, capsys
    ):
        from tests.test_pdf_parser import _make_text_pdf

        path = _make_text_pdf(
            tmp_path / "statement.pdf",
            [
                "EXTRATO DE 2026/02/02 A 2026/02/27",
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.06 2.06 TRF MB WAY DE ALICE 20.00 1 559.13",
            ],
        )

        with pytest.raises(IngestError):
            ingest(test_db, [path], cards_path=tmp_path / "none.csv")

        out = capsys.readouterr().out
        assert "Error: statement.pdf was not imported" in out
        assert "line 3" in out

    @pytest.mark.parametrize("dry_run, prefix", [(False, ""), (True, "[dry-run] ")])
    def test_pdf_signs_from_credit_patterns_are_counted(
        self, test_db, tmp_path, capsys, dry_run, prefix
    ):
        """Issue #17: the first line has no balance before it, the third
        none on it; the second is signed by the balance."""
        from tests.test_pdf_parser import _make_text_pdf

        path = _make_text_pdf(
            tmp_path / "statement.pdf",
            [
                "EXTRATO DE 2026/02/02 A 2026/02/27",
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.06 2.06 TRF MB WAY DE ALICE 20.00 1 549.13",
                "2.07 2.07 >PAGAMENTO CARTAO DE CREDITO 4.68",
            ],
        )

        result = ingest(
            test_db, [path], cards_path=tmp_path / "none.csv", dry_run=dry_run
        )

        assert result.files[0].signs_guessed == 2
        assert result.signs_guessed == 2
        out = capsys.readouterr().out
        assert "statement.pdf: 3 parsed" in out
        assert "2 sign(s) from credit patterns" in out
        assert f"{prefix}2 amount sign(s) could not be read from the running" in out

    def test_no_guessed_sign_no_line(self, test_db, utf16_csv, cards_csv, capsys):
        result = ingest(test_db, [utf16_csv], cards_path=cards_csv)

        assert result.signs_guessed == 0
        assert "sign(s)" not in capsys.readouterr().out

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


class TestSuspectedDuplicates:
    """A month imported from a CSV and from a PDF: the second format's rows
    are flagged for review, never dropped (issue #15)."""

    @pytest.fixture(autouse=True)
    def _setup(self, noise_words, cleaning_patterns, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _pdf(self, tmp_path):
        """January again, as a PDF that prints no balance: no id matches."""
        from tests.test_pdf_parser import SAMPLE_PDF_ROWS, _make_pdf_statement

        rows = [r[:5] + ("",) for r in SAMPLE_PDF_ROWS]
        return _make_pdf_statement(tmp_path / "january.pdf", rows)

    def _rows(self, db_path) -> list[tuple]:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(
                "SELECT transaction_id, source_format, suspected_of, date_posted, "
                "amount_signed FROM transactions ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()

    def test_rows_store_their_format(self, test_db, utf16_csv, cards_csv, tmp_path):
        ingest(test_db, [utf16_csv, self._pdf(tmp_path)], cards_path=cards_csv)

        formats = [r[1] for r in self._rows(test_db)]
        assert formats == ["utf16"] * 5 + ["pdf"] * 5

    def test_same_month_from_a_pdf_is_flagged(
        self, test_db, utf16_csv, cards_csv, tmp_path, capsys
    ):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)
        capsys.readouterr()

        result = ingest(test_db, [self._pdf(tmp_path)], cards_path=cards_csv)

        rows = self._rows(test_db)
        csv_rows, pdf_rows = rows[:5], rows[5:]
        assert len(rows) == 10  # nothing dropped or merged
        assert all(r[2] is None for r in csv_rows)
        csv_id = {(r[3], r[4]): r[0] for r in csv_rows}
        assert [r[2] for r in pdf_rows] == [csv_id[(r[3], r[4])] for r in pdf_rows]
        assert result.suspected == 5
        assert result.files[0].suspected == 5
        out = capsys.readouterr().out
        assert "5 suspected duplicate(s) of rows from another format" in out

    def test_flag_leaves_totals_unchanged(
        self, test_db, utf16_csv, cards_csv, tmp_path
    ):
        ingest(test_db, [utf16_csv, self._pdf(tmp_path)], cards_path=cards_csv)

        conn = sqlite3.connect(str(test_db))
        txs = fetch_all_transactions(conn)
        conn.close()
        assert len(txs) == 10
        assert sum(t["spend"] for t in txs) == pytest.approx(2 * 244.29)

    def test_same_format_twice_is_not_flagged(self, test_db, tmp_path, cards_csv):
        first = make_utf16_csv(tmp_path / "first.csv", SAMPLE_ROWS[:1])
        # Same day and amount, another purchase: the same format lists both
        other = (
            SAMPLE_ROWS[0][:2] + ("COMPRA 1234 LIDL",) + SAMPLE_ROWS[0][3:5] + ("1,00",)
        )
        second = make_utf16_csv(tmp_path / "second.csv", [other])

        result = ingest(test_db, [first, second], cards_path=cards_csv)

        assert [r[2] for r in self._rows(test_db)] == [None, None]
        assert result.suspected == 0

    def test_rows_from_before_the_format_column_are_not_matched(
        self, test_db, tmp_path, cards_csv
    ):
        from tests.conftest import add_transaction

        add_transaction(test_db, "2026-01-15", "COMPRA 1234 CONTINENTE", 45.50)

        result = ingest(test_db, [self._pdf(tmp_path)], cards_path=cards_csv)

        assert result.suspected == 0

    def test_dry_run_reports_the_same_count(
        self, test_db, utf16_csv, cards_csv, tmp_path, capsys
    ):
        ingest(test_db, [utf16_csv], cards_path=cards_csv)
        capsys.readouterr()

        result = ingest(
            test_db, [self._pdf(tmp_path)], cards_path=cards_csv, dry_run=True
        )

        assert result.suspected == 5
        out = capsys.readouterr().out
        assert "[dry-run] 5 suspected duplicate(s) of rows from another format" in out
        assert all(r[2] is None for r in self._rows(test_db))

    def test_clearing_the_flag_keeps_the_row(
        self, test_db, utf16_csv, cards_csv, tmp_path
    ):
        from expense_tracker.db import clear_suspected

        ingest(test_db, [utf16_csv, self._pdf(tmp_path)], cards_path=cards_csv)
        flagged = [r[0] for r in self._rows(test_db) if r[2]]

        conn = sqlite3.connect(str(test_db))
        try:
            assert clear_suspected(conn, flagged[:2]) == 2
        finally:
            conn.close()

        rows = self._rows(test_db)
        assert len(rows) == 10
        assert sum(1 for r in rows if r[2]) == 3
        assert all(r[2] is None for r in rows if r[0] in flagged[:2])


ACCOUNT_A = "PT50 0002 0123 1234 5678 9015 4"
ACCOUNT_B = "PT50 0035 0999 8888 7777 6666 1"


def _account_statement(path, identifier: str | None, rows=None, **kwargs):
    """A UTF-16 statement whose header names ``identifier`` (or no account)."""
    make_utf16_csv(path, rows or SAMPLE_ROWS, **kwargs)
    text = path.read_bytes().decode("utf-16-le")
    line = f"Conta:;{identifier}\n" if identifier is not None else ""
    path.write_bytes(text.replace("Conta:;123456789\n", line).encode("utf-16-le"))
    return path


FEBRUARY = [("03-02-2026", "03-02-2026", "COMPRA LOJA", "-1,00", "Compra", "9,00")]


class TestAccountFingerprint:
    """A statement's account identifier is checked against the account
    label it is imported into; only a salted fingerprint is kept (#12)."""

    @pytest.fixture(autouse=True)
    def _setup(self, noise_words, cleaning_patterns, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _fingerprints(self, db_path) -> dict[str, str]:
        conn = sqlite3.connect(str(db_path))
        try:
            return dict(
                conn.execute("SELECT account, fingerprint FROM account_fingerprints")
            )
        finally:
            conn.close()

    def _count(self, db_path) -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    def _feb(self, tmp_path, identifier):
        return _account_statement(
            tmp_path / "feb.csv",
            identifier,
            FEBRUARY,
            date_from="01-02-2026",
            date_to="28-02-2026",
        )

    def test_first_import_records_a_fingerprint(self, test_db, tmp_path, cards_csv):
        jan = _account_statement(tmp_path / "jan.csv", ACCOUNT_A)

        ingest(test_db, [jan], cards_path=cards_csv)

        fingerprint = self._fingerprints(test_db)["checkings_account"]
        assert len(fingerprint) == 64
        assert int(fingerprint, 16) >= 0  # hex

    def test_same_identifier_matches(self, test_db, tmp_path, cards_csv):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )
        # Spacing and case are not part of the identifier
        feb = self._feb(tmp_path, ACCOUNT_A.replace(" ", "").lower())

        ingest(test_db, [feb], cards_path=cards_csv)

        assert self._count(test_db) == 6

    def test_other_identifier_is_refused(self, test_db, tmp_path, cards_csv):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )
        before = self._fingerprints(test_db)

        with pytest.raises(IngestError) as error:
            ingest(test_db, [self._feb(tmp_path, ACCOUNT_B)], cards_path=cards_csv)

        message = str(error.value)
        assert "feb.csv" in message
        assert "'checkings_account'" in message
        assert "--force" in message
        assert self._count(test_db) == 5
        assert self._fingerprints(test_db) == before

    def test_force_imports_and_keeps_the_fingerprint(
        self, test_db, tmp_path, cards_csv
    ):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )
        before = self._fingerprints(test_db)

        ingest(
            test_db, [self._feb(tmp_path, ACCOUNT_B)], cards_path=cards_csv, force=True
        )

        assert self._count(test_db) == 6
        assert self._fingerprints(test_db) == before

    def test_statement_without_an_identifier_skips_the_check(
        self, test_db, tmp_path, cards_csv
    ):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )

        ingest(test_db, [self._feb(tmp_path, None)], cards_path=cards_csv)

        assert self._count(test_db) == 6

    def test_each_label_has_its_own_fingerprint(
        self, test_db, tmp_path, cards_csv, monkeypatch
    ):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "savings_account")

        ingest(test_db, [self._feb(tmp_path, ACCOUNT_B)], cards_path=cards_csv)

        fingerprints = self._fingerprints(test_db)
        assert set(fingerprints) == {"checkings_account", "savings_account"}
        assert len(set(fingerprints.values())) == 2

    def test_dry_run_refuses_like_the_real_run(
        self, test_db, tmp_path, cards_csv, capsys
    ):
        ingest(
            test_db, [_account_statement(tmp_path / "jan.csv", ACCOUNT_A)], cards_csv
        )
        feb = self._feb(tmp_path, ACCOUNT_B)
        capsys.readouterr()

        with pytest.raises(IngestError) as dry:
            ingest(test_db, [feb], cards_path=cards_csv, dry_run=True)
        dry_out = capsys.readouterr().out
        with pytest.raises(IngestError) as real:
            ingest(test_db, [feb], cards_path=cards_csv)
        real_out = capsys.readouterr().out

        assert str(dry.value) == str(real.value)
        error_line = next(x for x in real_out.splitlines() if "Error:" in x)
        assert error_line in dry_out

    def test_identifier_is_never_stored_printed_or_logged(
        self, test_db, tmp_path, cards_csv, capsys, caplog
    ):
        import logging

        caplog.set_level(logging.DEBUG)
        jan = _account_statement(tmp_path / "jan.csv", ACCOUNT_A)
        feb = self._feb(tmp_path, ACCOUNT_B)
        ingest(test_db, [jan], cards_path=cards_csv)
        ingest(test_db, [jan], cards_path=cards_csv, dry_run=True)
        for dry_run in (True, False):
            with pytest.raises(IngestError):
                ingest(test_db, [feb], cards_path=cards_csv, dry_run=dry_run)
        ingest(test_db, [feb], cards_path=cards_csv, force=True)

        captured = capsys.readouterr()
        logged = "\n".join(
            r.getMessage() + (caplog.text if r.exc_info else "") for r in caplog.records
        )
        texts = {
            "stdout": captured.out,
            "stderr": captured.err,
            "log": logged + caplog.text,
        }
        database = test_db.read_bytes()
        for identifier in (ACCOUNT_A, ACCOUNT_B):
            compact = identifier.replace(" ", "")
            forms = {identifier, compact, compact.lower(), compact[4:]}
            for form in forms:
                for name, text in texts.items():
                    assert form not in text, f"{form!r} in {name}"
                for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                    assert form.encode(encoding) not in database, form


class TestCardHolders:
    """``who`` is looked up from the card-holder file when transactions are
    read, so a mapping changed later applies to old rows too (issue #4)."""

    @pytest.fixture(autouse=True)
    def _setup(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def _who(self, db_path, cards_path) -> dict[str, str]:
        conn = sqlite3.connect(str(db_path))
        try:
            txs = fetch_all_transactions(conn, cards_path=cards_path)
        finally:
            conn.close()
        return {t["description_raw"]: t["who"] for t in txs}

    def test_mapping_changes_apply_to_imported_rows(self, test_db, utf16_csv, tmp_path):
        from expense_tracker.rules import add_card, remove_card

        cards = tmp_path / "holders.csv"
        continente = "COMPRA 1234 CONTINENTE PORTO"
        ingest(test_db, [utf16_csv], cards_path=cards)  # no holder known yet
        assert self._who(test_db, cards)[continente] == "Joint"

        add_card(cards, "1234", "Alice")
        assert self._who(test_db, cards)[continente] == "Alice"

        cards.write_text("card_last4,name\n1234,Alicia\n", encoding="utf-8")
        assert self._who(test_db, cards)[continente] == "Alicia"

        remove_card(cards, "1234")
        who = self._who(test_db, cards)
        assert who[continente] == "Joint"
        assert set(who.values()) == {"Joint"}

    def test_card_without_a_holder_is_stored(self, test_db, utf16_csv, tmp_path):
        ingest(test_db, [utf16_csv], cards_path=tmp_path / "none.csv")

        conn = sqlite3.connect(str(test_db))
        cards = dict(
            conn.execute("SELECT description_raw, card_last4 FROM transactions")
        )
        conn.close()
        assert cards["COMPRA 1234 CONTINENTE PORTO"] == "1234"
        assert cards["TRANSFERENCIA - SALARIO"] is None

    def test_old_rows_get_their_card_once(self, test_db, tmp_path):
        from tests.conftest import add_transaction

        cards = tmp_path / "holders.csv"
        cards.write_text("card_last4,name\n4321,Bob\n", encoding="utf-8")
        add_transaction(test_db, "2026-01-15", "COMPRA 4321 LOJA", 5.0)
        add_transaction(test_db, "2026-01-16", "TRANSFERENCIA SALARIO", 9.0)
        conn = sqlite3.connect(str(test_db))
        # A ledger from before the backfill: unknown cards were stored as NULL
        conn.execute("DELETE FROM settings WHERE key = 'card_last4_backfilled'")
        conn.execute("UPDATE transactions SET who = 'Joint', card_last4 = NULL")
        conn.commit()

        def cards_stored() -> dict:
            return dict(
                conn.execute("SELECT description_raw, card_last4 FROM transactions")
            )

        migrate_schema(conn)
        assert cards_stored() == {
            "COMPRA 4321 LOJA": "4321",
            "TRANSFERENCIA SALARIO": None,
        }
        assert self._who(test_db, cards)["COMPRA 4321 LOJA"] == "Bob"

        # Once only: a later migration leaves the column alone
        conn.execute("UPDATE transactions SET card_last4 = NULL")
        conn.commit()
        migrate_schema(conn)
        assert set(cards_stored().values()) == {None}
        conn.close()


class TestCardHoldersInReports:
    """The workbook, the CSV export and the PDF report's data read ``who``
    from the card-holder file as it is now (issue #4)."""

    @pytest.fixture
    def ledger(self, test_db, utf16_csv, tmp_path, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        ingest(test_db, [utf16_csv], cards_path=tmp_path / "none.csv")
        yield test_db
        reset_cleaning_cache()

    @pytest.fixture
    def holders(self, tmp_path, monkeypatch):
        """DEFAULT_CARDS, mapping 1234 to Alicia: added after the import."""
        import expense_tracker.constants as constants

        cards = tmp_path / "holders.csv"
        cards.write_text("card_last4,name\n1234,Alicia\n", encoding="utf-8")
        monkeypatch.setattr(constants, "DEFAULT_CARDS", cards)
        return cards

    def test_workbook(self, ledger, holders, tmp_path):
        from openpyxl import load_workbook

        from expense_tracker.xlsx import generate_xlsx

        path = tmp_path / "report.xlsx"
        generate_xlsx(ledger, tmp_path / "rules.csv", path, tmp_path / "notes.csv")

        sheet = load_workbook(path)["Data"]
        header = [c.value for c in sheet[1]]
        who = header.index("Who")
        desc = header.index("Description (raw)")
        values = {r[desc]: r[who] for r in sheet.iter_rows(min_row=2, values_only=True)}
        assert values["COMPRA 1234 CONTINENTE PORTO"] == "Alicia"
        assert values["COMPRA 5678 FARMACIA DA GARE 1000-001 LISBOA"] == "Joint"

    def test_csv_export(self, ledger, holders, tmp_path):
        import csv

        from expense_tracker.export import export_csv

        out = tmp_path / "export.csv"
        export_csv(ledger, out)

        with out.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        who = {r["Description (raw)"]: r["Who"] for r in rows}
        assert who["COMPRA 1234 CONTINENTE PORTO"] == "Alicia"

    def test_pdf_report_data(self, ledger, holders):
        from expense_tracker.pdf_report import _fetch_month_transactions

        conn = sqlite3.connect(str(ledger))
        try:
            txs = _fetch_month_transactions(conn, "2026-01")
        finally:
            conn.close()
        assert sorted(t["who"] for t in txs) == ["Alicia"] + ["Joint"] * 4


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
