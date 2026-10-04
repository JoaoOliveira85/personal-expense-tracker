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


# ---------------------------------------------------------------------------
# Pages that write files: run them against a throw-away project directory
# ---------------------------------------------------------------------------

GUI_PATH = Path(__file__).parent.parent / "expense_tracker" / "gui.py"


@pytest.fixture
def project(tmp_path, monkeypatch, noise_words, cleaning_patterns, cards_csv, rules_csv):
    """The GUI's default paths, pointed at an empty project under tmp_path."""
    from types import SimpleNamespace

    import expense_tracker.constants as constants
    import expense_tracker.email_fetch as email_fetch
    from expense_tracker.parser import _get_cleaning_patterns, reset_cleaning_cache

    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    paths = SimpleNamespace(
        root=root,
        db=root / "data" / "ledger.sqlite",
        rules=rules_csv,
        ods=root / "expense-report.ods",
        raw=root / "raw",
        notes=root / "data" / "description-notes.csv",
        email_config=root / "data" / "email-config.json",
    )
    monkeypatch.setattr(constants, "DEFAULT_DB", paths.db)
    monkeypatch.setattr(constants, "DEFAULT_RULES", paths.rules)
    monkeypatch.setattr(constants, "DEFAULT_CARDS", cards_csv)
    monkeypatch.setattr(constants, "DEFAULT_ODS", paths.ods)
    monkeypatch.setattr(constants, "DEFAULT_RAW", paths.raw)
    monkeypatch.setattr(constants, "DEFAULT_DESC_NOTES", paths.notes)
    monkeypatch.setattr(constants, "DEFAULT_BACKUPS", root / "backups")
    monkeypatch.setattr(constants, "DEFAULT_REPORTS", root / "reports")
    monkeypatch.setattr(constants, "DEFAULT_CSV", root / "data" / "ledger.csv")
    monkeypatch.setattr(email_fetch, "DEFAULT_EMAIL_CONFIG", paths.email_config)
    reset_cleaning_cache()
    _get_cleaning_patterns(noise_words, cleaning_patterns)
    monkeypatch.chdir(tmp_path)  # gui.py chdirs; restore cwd afterwards
    yield paths
    reset_cleaning_cache()


def _open_page(name: str):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(GUI_PATH), default_timeout=30).run()
    at.sidebar.radio[0].set_value(name).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _statement(tmp_path: Path, name: str, rows=None) -> bytes:
    """Bytes of a synthetic statement."""
    from .conftest import SAMPLE_ROWS, make_utf16_csv

    return make_utf16_csv(tmp_path / name, rows or SAMPLE_ROWS).read_bytes()


class TestImportUploadPath:
    """The name of an uploaded file is chosen by the browser that sends it."""

    def test_upload_is_saved_in_raw(self, project, tmp_path):
        data = _statement(tmp_path, "january.csv")
        at = _open_page("Import")
        at.file_uploader[0].upload("january.csv", data, "text/csv").run()

        assert not at.exception
        assert (project.raw / "january.csv").read_bytes() == data

    @pytest.mark.parametrize(
        "name",
        ["../../escaped.csv", "..\\..\\escaped.csv", "sub/dir/escaped.csv"],
    )
    def test_directories_in_the_name_are_dropped(self, project, tmp_path, name):
        data = _statement(tmp_path, "january.csv")
        at = _open_page("Import")
        at.file_uploader[0].upload(name, data, "text/csv").run()

        assert not at.exception
        assert (project.raw / "escaped.csv").read_bytes() == data
        assert sorted(p.name for p in project.raw.iterdir()) == ["escaped.csv"]
        assert not list(tmp_path.glob("escaped.csv"))
        assert not list(project.root.glob("escaped.csv"))

    def test_absolute_name_stays_in_raw(self, project, tmp_path):
        data = _statement(tmp_path, "january.csv")
        target = tmp_path / "elsewhere" / "absolute.csv"
        target.parent.mkdir()
        at = _open_page("Import")
        at.file_uploader[0].upload(str(target), data, "text/csv").run()

        assert not at.exception
        assert not target.exists()
        assert (project.raw / "absolute.csv").read_bytes() == data
