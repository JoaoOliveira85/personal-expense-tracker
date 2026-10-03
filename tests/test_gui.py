"""Tests for expense_tracker.gui (data access and importability).

Note: Streamlit apps are tested primarily via browser-based testing.
These tests verify the data layer and that the module structure is correct.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.db import ensure_schema, migrate_schema, fetch_all_transactions


# ---------------------------------------------------------------------------
# We can't import gui.py directly (it runs Streamlit page config at import
# time), so we test the underlying data functions it uses.
# ---------------------------------------------------------------------------


class TestGuiDataLayer:
    """Test the data access functions used by the GUI."""

    def _populate_db(self, db_path: Path, count: int = 5) -> None:
        """Insert test transactions."""
        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        for i in range(count):
            cat = "Groceries" if i % 2 == 0 else ""
            conn.execute(
                """
                INSERT INTO transactions (
                    transaction_id, date_posted, date_value, month, day_of_week,
                    description_raw, description_clean,
                    amount_signed, amount_abs, direction, tx_type, balance,
                    currency, account, source_file, imported_at, category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"tx{i:04d}",
                    f"2026-01-{i+10:02d}",
                    f"2026-01-{i+10:02d}",
                    "2026-01",
                    "Wed",
                    f"PURCHASE {i}",
                    f"SHOP {i}",
                    -(i + 1) * 10.0,
                    (i + 1) * 10.0,
                    "out",
                    "",
                    1000.0,
                    "EUR",
                    "checkings_account",
                    "test.csv",
                    "2026-01-15T10:00:00",
                    cat,
                ),
            )
        conn.commit()
        conn.close()

    def test_fetch_transactions_returns_dicts(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)
        self._populate_db(db_path)

        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        rows = fetch_all_transactions(conn)
        conn.close()

        assert len(rows) == 5
        assert "transaction_id" in rows[0]
        assert "category" in rows[0]
        assert "status" in rows[0]

    def test_status_field_set(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)
        self._populate_db(db_path)

        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        rows = fetch_all_transactions(conn)
        conn.close()

        categorized = [r for r in rows if r["status"] == "categorized"]
        uncategorized = [r for r in rows if r["status"] == "uncategorized"]
        assert len(categorized) > 0
        assert len(uncategorized) > 0

    def test_empty_db(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        rows = fetch_all_transactions(conn)
        conn.close()
        assert rows == []

    def test_pandas_conversion(self, tmp_path):
        """Verify transactions can be loaded into a pandas DataFrame."""
        import pandas as pd

        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)
        self._populate_db(db_path, count=3)

        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        rows = fetch_all_transactions(conn)
        conn.close()

        df = pd.DataFrame(rows)
        assert len(df) == 3
        assert "date_posted" in df.columns
        assert "amount_abs" in df.columns
        assert "category" in df.columns


class TestGuiModuleStructure:
    """Test that the GUI module file exists and has expected structure."""

    def test_gui_file_exists(self):
        gui_path = Path(__file__).parent.parent / "expense_tracker" / "gui.py"
        assert gui_path.exists()

    def test_gui_file_has_pages(self):
        gui_path = Path(__file__).parent.parent / "expense_tracker" / "gui.py"
        content = gui_path.read_text(encoding="utf-8")
        # Verify all expected pages are present
        assert "Dashboard" in content
        assert "Transactions" in content
        assert "Categorize" in content
        assert "Rules" in content
        assert "Import" in content


class TestDashboardTotals:
    """Run the Streamlit page against a temporary database."""

    def _insert(self, conn, tid, direction, amount, category):
        signed = amount if direction == "in" else -amount
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, date_posted, date_value, month, day_of_week,
                description_raw, description_clean,
                amount_signed, amount_abs, direction, tx_type, balance,
                currency, account, source_file, imported_at, category
            ) VALUES (?, '2026-01-10', '2026-01-10', '2026-01', 'Sat', ?, ?,
                      ?, ?, ?, '', 0, 'EUR', 'checkings_account', 't.csv',
                      '2026-01-15T10:00:00', ?)
            """,
            (tid, tid, tid, signed, amount, direction, category),
        )

    def test_net_is_income_minus_expenses(self, tmp_path, monkeypatch):
        """Savings deposits are neither spending nor income (README), so Net
        must not move with them either."""
        from streamlit.testing.v1 import AppTest

        import expense_tracker.constants as constants

        db_path = tmp_path / "ledger.sqlite"
        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        self._insert(conn, "salary", "in", 2000.0, "Income")
        self._insert(conn, "groceries", "out", 500.0, "Groceries")
        self._insert(conn, "deposit", "out", 300.0, "Savings")
        conn.commit()
        conn.close()

        monkeypatch.setattr(constants, "DEFAULT_DB", db_path)
        monkeypatch.chdir(tmp_path)  # gui.py chdirs; restore cwd afterwards
        gui_path = Path(__file__).parent.parent / "expense_tracker" / "gui.py"
        at = AppTest.from_file(str(gui_path), default_timeout=30).run()

        metrics = {m.label: m.value for m in at.metric}
        assert metrics["Total Expenses"] == "500.00 €"
        assert metrics["Total Income"] == "2,000.00 €"
        assert metrics["Net"] == "1,500.00 €"
