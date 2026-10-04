"""Tests for expense_tracker.advisor data extraction."""
from __future__ import annotations

import sqlite3

import pytest

from expense_tracker.advisor import _fetch_historical_summary, _fetch_monthly_summary
from tests.test_pdf_report import _insert


@pytest.fixture
def conn(test_db):
    conn = sqlite3.connect(str(test_db))
    _insert(conn, "1", "2026-01", "out", "Health", 100)
    _insert(conn, "2", "2026-01", "in", "Health", 30)
    _insert(conn, "3", "2026-01", "in", "Income", 2000)
    _insert(conn, "4", "2026-01", "out", "Savings", 3000)
    _insert(conn, "5", "2026-01", "in", "Savings", 500)
    yield conn
    conn.close()


class TestRefundsInSummaries:
    def test_monthly_summary_nets_refunds(self, conn):
        summary = _fetch_monthly_summary(conn, "2026-01")
        assert summary["income"] == 2000
        assert summary["expenses"] == 70
        assert summary["categories"] == [{"name": "Health", "total": 70, "count": 1}]

    def test_historical_summary_nets_refunds(self, conn):
        summary = _fetch_historical_summary(conn, "2026-02")
        assert summary["total_income"] == 2000
        assert summary["total_expenses"] == 70
        assert summary["categories"][0]["total"] == 70


class TestUncategorizedSpending:
    """A transaction without a category has NULL, or '' once its row was
    edited in the spreadsheet (a note added, a category cleared)."""

    @pytest.fixture
    def conn(self, test_db):
        conn = sqlite3.connect(str(test_db))
        _insert(conn, "1", "2026-01", "out", None, 100)
        _insert(conn, "2", "2026-01", "out", "", 40)
        _insert(conn, "3", "2026-01", "out", "Health", 10)
        yield conn
        conn.close()

    def test_monthly_summary_has_one_uncategorized_line(self, conn):
        summary = _fetch_monthly_summary(conn, "2026-01")
        assert summary["categories"] == [
            {"name": "Uncategorized", "total": 140, "count": 2},
            {"name": "Health", "total": 10, "count": 1},
        ]

    def test_historical_summary_has_one_uncategorized_line(self, conn):
        summary = _fetch_historical_summary(conn, "2026-02")
        assert [(c["name"], c["total"]) for c in summary["categories"]] == [
            ("Uncategorized", 140),
            ("Health", 10),
        ]
