"""Tests for expense_tracker.export."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from expense_tracker.export import export_csv
from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns


class TestExportCsv:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_exports_all_rows(self, populated_db, tmp_path):
        out = tmp_path / "export.csv"
        export_csv(populated_db, out)
        assert out.exists()

        with out.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)

        assert len(rows) == 6  # header + 5 data rows

    def test_header_row(self, populated_db, tmp_path):
        out = tmp_path / "export.csv"
        export_csv(populated_db, out)

        with out.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)

        assert "Date" in header
        assert "Amount" in header
        assert "Category" in header
        assert "Notes" in header

    def test_creates_parent_directory(self, populated_db, tmp_path):
        out = tmp_path / "nested" / "deep" / "export.csv"
        export_csv(populated_db, out)
        assert out.exists()

    def test_empty_db(self, test_db, tmp_path):
        out = tmp_path / "empty.csv"
        export_csv(test_db, out)
        assert out.exists()

        with out.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)

        assert len(rows) == 1  # header only
