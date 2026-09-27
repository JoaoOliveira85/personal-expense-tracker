"""Tests for syncing manual edits from the ODS report back to SQLite."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from odf.opendocument import load as load_ods
from odf.table import Table, TableCell, TableRow
from odf.text import P

from expense_tracker.constants import COL_CATEGORY, COL_TRANSACTION_ID
from expense_tracker.db import migrate_schema
from expense_tracker.ods import generate_ods, sync_from_ods
from expense_tracker.rules import categorize_transactions, load_rules


def _cells(row) -> list:
    """Data-sheet cells of a row, expanding ODF column repeats."""
    cells = []
    for cell in row.getElementsByType(TableCell):
        repeat = cell.getAttribute("numbercolumnsrepeated")
        cells.extend([cell] * (int(repeat) if repeat else 1))
    return cells


def _set_ods_category(ods_path: Path, tid: str, value: str) -> None:
    """Simulate a user editing the Category cell of one transaction."""
    doc = load_ods(str(ods_path))
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == "Data"
    )
    for row in sheet.getElementsByType(TableRow)[1:]:
        cells = _cells(row)
        if len(cells) > COL_TRANSACTION_ID and str(cells[COL_TRANSACTION_ID]) == tid:
            cell = cells[COL_CATEGORY]
            for p in cell.getElementsByType(P):
                cell.removeChild(p)
            cell.addElement(P(text=value))
            doc.save(str(ods_path))
            return
    raise AssertionError(f"transaction {tid} not found in ODS")


def _category(db: Path, tid: str) -> str | None:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT category FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
    finally:
        conn.close()


def _set_db_category(db: Path, tid: str, value: str) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE transactions SET category = ? WHERE transaction_id = ?", (value, tid)
    )
    conn.commit()
    conn.close()


@pytest.fixture
def report(populated_db, rules_csv, tmp_path):
    """A categorized DB, its generated ODS, and the CONTINENTE transaction id."""
    conn = sqlite3.connect(str(populated_db))
    migrate_schema(conn)
    categorize_transactions(conn, load_rules(rules_csv))
    tid = conn.execute(
        "SELECT transaction_id FROM transactions "
        "WHERE description_raw LIKE '%CONTINENTE%'"
    ).fetchone()[0]
    conn.close()

    ods = tmp_path / "report.ods"
    notes = tmp_path / "description-notes.csv"
    generate_ods(populated_db, rules_csv, ods, notes)
    return populated_db, ods, notes, tid


class TestSyncFromOds:
    def test_applies_category_edited_in_spreadsheet(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")

        assert sync_from_ods(db, ods, notes) == 1
        assert _category(db, tid) == "Household"

    def test_unedited_rows_are_not_synced(self, report):
        db, ods, notes, _ = report
        assert sync_from_ods(db, ods, notes) == 0

    def test_stale_spreadsheet_does_not_revert_newer_db_value(self, report):
        """The DB changed after the ODS was written (e.g. rules re-applied from
        the GUI, or the xlsx report is in use): the ODS still shows the old
        value, which is not an edit and must not overwrite the DB."""
        db, ods, notes, tid = report
        _set_db_category(db, tid, "Changed Later")

        assert sync_from_ods(db, ods, notes) == 0
        assert _category(db, tid) == "Changed Later"

    def test_edit_is_applied_only_once(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")
        sync_from_ods(db, ods, notes)
        _set_db_category(db, tid, "Changed Later")

        assert sync_from_ods(db, ods, notes) == 0
        assert _category(db, tid) == "Changed Later"


def _source(db: Path, tid: str) -> str | None:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT category_source FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
    finally:
        conn.close()


class TestSyncMarksProvenance:
    def test_spreadsheet_edit_is_marked_manual(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")
        sync_from_ods(db, ods, notes)
        assert _source(db, tid) == "manual"

    def test_clearing_category_makes_row_uncategorized(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "")
        sync_from_ods(db, ods, notes)
        assert _source(db, tid) is None
