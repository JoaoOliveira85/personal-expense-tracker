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
