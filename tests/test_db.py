"""Tests for expense_tracker.db."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.db import (
    tx_id,
    ensure_schema,
    migrate_schema,
    ingest,
    ingested_source_files,
    reclean_descriptions,
    fetch_all_transactions,
    is_refund,
    spend_amount,
)
from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns
from tests.conftest import make_utf16_csv, SAMPLE_ROWS


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

        cols = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
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


# ---------------------------------------------------------------------------
# ingested_source_files
# ---------------------------------------------------------------------------


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
        ],
    )
    def test_spend_amount(self, tx, expected):
        assert spend_amount(tx) == expected
