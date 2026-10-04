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


class TestEmptyPlaceholderOds:
    """DEPLOYMENT.md has users `touch expense-report.ods` for the bind mount."""

    def test_sync_ignores_empty_file(self, populated_db, tmp_path):
        ods = tmp_path / "expense-report.ods"
        ods.touch()

        assert sync_from_ods(populated_db, ods, tmp_path / "notes.csv") == 0

    def test_generate_writes_full_report_over_empty_file(
        self, populated_db, rules_csv, tmp_path
    ):
        ods = tmp_path / "expense-report.ods"
        ods.touch()

        generate_ods(populated_db, rules_csv, ods, tmp_path / "notes.csv")

        names = [
            s.getAttribute("name")
            for s in load_ods(str(ods)).spreadsheet.getElementsByType(Table)
        ]
        assert "Data" in names and "Dashboard" in names


def _set_ods_cell(ods_path: Path, tid: str, col: int, value: str) -> None:
    """Simulate a user editing one cell of a transaction's Data-sheet row."""
    doc = load_ods(str(ods_path))
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == "Data"
    )
    for row in sheet.getElementsByType(TableRow)[1:]:
        cells = _cells(row)
        if len(cells) > COL_TRANSACTION_ID and str(cells[COL_TRANSACTION_ID]) == tid:
            cell = cells[col]
            for p in cell.getElementsByType(P):
                cell.removeChild(p)
            cell.addElement(P(text=value))
            doc.save(str(ods_path))
            return
    raise AssertionError(f"transaction {tid} not found in ODS")


class TestMerchantNoteSync:
    @pytest.fixture
    def two_visits(self, tmp_path, rules_csv):
        """Two transactions of the same merchant, with a merchant note."""
        from expense_tracker.db import ingest

        from .conftest import make_utf16_csv

        db = tmp_path / "ledger.sqlite"
        statement = make_utf16_csv(
            tmp_path / "2026-01.csv",
            [
                ("20-01-2026", "20-01-2026", "MERCEARIA DO BAIRRO", "-8,00", "Compra", "992,00"),
                ("05-01-2026", "05-01-2026", "MERCEARIA DO BAIRRO", "-12,00", "Compra", "1000,00"),
            ],
        )
        ingest(db, [statement], cards_path=tmp_path / "none.csv")
        notes = tmp_path / "description-notes.csv"
        notes.write_text(
            "description_clean,merchant_note\nMERCEARIA DO BAIRRO,old note\n",
            encoding="utf-8",
        )
        ods = tmp_path / "report.ods"
        generate_ods(db, rules_csv, ods, notes)
        conn = sqlite3.connect(str(db))
        tids = [
            r[0]
            for r in conn.execute(
                "SELECT transaction_id FROM transactions ORDER BY date_posted"
            )
        ]
        conn.close()
        return db, ods, notes, tids  # tids: oldest first

    @pytest.mark.parametrize("edited", [0, 1], ids=["oldest-row", "newest-row"])
    def test_edit_on_any_row_updates_merchant_note(self, two_visits, edited):
        from expense_tracker.constants import COL_MERCHANT_NOTE
        from expense_tracker.ods import _load_description_notes

        db, ods, notes, tids = two_visits
        _set_ods_cell(ods, tids[edited], COL_MERCHANT_NOTE, "new note")

        sync_from_ods(db, ods, notes)

        assert _load_description_notes(notes) == {"MERCEARIA DO BAIRRO": "new note"}


def _row_values(db: Path, tid: str) -> tuple:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT category, subcategory, notes, category_source "
            "FROM transactions WHERE transaction_id = ?",
            (tid,),
        ).fetchone()
    finally:
        conn.close()


class TestOnlyEditedCellsAreSynced:
    """A row has three synced cells. Editing one of them must not write the
    other two back: they still show what the ODS was generated with, and the
    DB may have moved on since (a rule added in the GUI, the xlsx report in
    use while the ODS is not regenerated)."""

    def test_note_added_on_a_stale_row_keeps_the_newer_category(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE transactions SET category = 'Supermarket', subcategory = 'Food', "
            "category_source = 'rule' WHERE transaction_id = ?",
            (tid,),
        )
        conn.commit()
        conn.close()
        _set_ods_cell(ods, tid, COL_NOTES, "birthday dinner")

        assert sync_from_ods(db, ods, notes) == 1

        assert _row_values(db, tid) == ("Supermarket", "Food", "birthday dinner", "rule")

    def test_category_edit_keeps_a_newer_note(self, report):
        db, ods, notes, tid = report
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE transactions SET notes = 'kept' WHERE transaction_id = ?", (tid,)
        )
        conn.commit()
        conn.close()
        _set_ods_category(ods, tid, "Household")

        assert sync_from_ods(db, ods, notes) == 1

        category, _, note, source = _row_values(db, tid)
        assert (category, note, source) == ("Household", "kept", "manual")

    def test_stale_row_is_not_applied_on_the_next_sync_either(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        _set_db_category(db, tid, "Supermarket")
        _set_ods_cell(ods, tid, COL_NOTES, "birthday dinner")
        sync_from_ods(db, ods, notes)

        assert sync_from_ods(db, ods, notes) == 0
        assert _category(db, tid) == "Supermarket"


def _edit_data_rows(ods_path: Path, change) -> None:
    """Apply change(sheet, rows) to the Data sheet, as the user would in
    the spreadsheet, and save."""
    doc = load_ods(str(ods_path))
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == "Data"
    )
    change(sheet, sheet.getElementsByType(TableRow))
    doc.save(str(ods_path))


def _insert_column(position: int, header: str):
    def change(sheet, rows):
        for i, row in enumerate(rows):
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=header if i == 0 else ""))
            row.insertBefore(cell, row.getElementsByType(TableCell)[position])
    return change


def _note(db: Path, tid: str) -> str | None:
    return _row_values(db, tid)[2]


class TestSpreadsheetLayoutChanges:
    """What the user can do to the Data sheet besides typing in the four
    synced columns."""

    @pytest.fixture
    def noted(self, report):
        """The report, with a note on the CONTINENTE row already synced."""
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        _set_ods_cell(ods, tid, COL_NOTES, "birthday dinner")
        assert sync_from_ods(db, ods, notes) == 1
        return db, ods, notes, tid

    def test_inserted_column_does_not_shift_what_is_synced(self, noted):
        """A column added before Notes moved Notes to S and Merchant Note to
        T. Read by position, every note looked deleted and became the
        merchant note of its row."""
        from expense_tracker.constants import COL_NOTES
        from expense_tracker.ods import _load_description_notes

        db, ods, notes, tid = noted
        _edit_data_rows(ods, _insert_column(COL_NOTES, "My column"))

        assert sync_from_ods(db, ods, notes) == 0

        assert _note(db, tid) == "birthday dinner"
        assert _category(db, tid) == "Groceries"
        assert _load_description_notes(notes) == {}

    def test_edits_are_read_from_the_columns_where_the_headers_are(self, noted):
        from expense_tracker.constants import COL_CATEGORY, COL_NOTES

        db, ods, notes, tid = noted
        _edit_data_rows(ods, _insert_column(COL_CATEGORY, "My column"))
        # Category is now one column to the right, Notes too
        _set_ods_cell_at(ods, tid, COL_CATEGORY + 1, "Household")
        _set_ods_cell_at(ods, tid, COL_NOTES + 1, "changed")

        assert sync_from_ods(db, ods, notes) == 1

        assert _category(db, tid) == "Household"
        assert _note(db, tid) == "changed"

    def test_header_row_sorted_into_the_data_does_not_hide_a_row(self, report):
        """Sorting the whole sheet moves the header row down: the first row
        is then a transaction like any other."""
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")

        def header_last(sheet, rows):
            sheet.removeChild(rows[0])
            sheet.addElement(rows[0])
        _edit_data_rows(ods, header_last)

        assert sync_from_ods(db, ods, notes) == 1
        assert _category(db, tid) == "Household"

    def test_renamed_header_is_read_by_position(self, report):
        """No header row to go by: the sheet is read as it was generated."""
        from expense_tracker.constants import COL_NOTES
        from expense_tracker.ods import _load_description_notes

        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")

        def rename(sheet, rows):
            cell = _cells(rows[0])[COL_NOTES]
            for p in cell.getElementsByType(P):
                cell.removeChild(p)
            cell.addElement(P(text="Notas"))
        _edit_data_rows(ods, rename)

        assert sync_from_ods(db, ods, notes) == 1
        assert _category(db, tid) == "Household"
        assert _load_description_notes(notes) == {}

    def test_rows_in_another_order(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")

        def reverse(sheet, rows):
            for row in rows[1:]:
                sheet.removeChild(row)
            for row in reversed(rows[1:]):
                sheet.addElement(row)
        _edit_data_rows(ods, reverse)

        assert sync_from_ods(db, ods, notes) == 1
        assert _category(db, tid) == "Household"

    def test_deleted_row_keeps_its_transaction(self, noted, rules_csv):
        db, ods, notes, tid = noted

        def delete(sheet, rows):
            for row in rows[1:]:
                if str(_cells(row)[COL_TRANSACTION_ID]) == tid:
                    sheet.removeChild(row)
        _edit_data_rows(ods, delete)

        assert sync_from_ods(db, ods, notes) == 0
        assert _row_values(db, tid)[:3] == ("Groceries", "", "birthday dinner")

        generate_ods(db, rules_csv, ods, notes)
        assert sync_from_ods(db, ods, notes) == 0
        assert _row_values(db, tid)[:3] == ("Groceries", "", "birthday dinner")

    def test_text_typed_into_the_amount_cell_is_not_synced(self, report):
        db, ods, notes, tid = report
        _set_ods_cell(ods, tid, 5, "forty-five")  # F: Amount

        assert sync_from_ods(db, ods, notes) == 0

        conn = sqlite3.connect(str(db))
        amount = conn.execute(
            "SELECT amount_abs FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
        conn.close()
        assert amount == 45.5

    def test_renamed_category_is_kept_as_a_manual_edit(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Supermercado")

        assert sync_from_ods(db, ods, notes) == 1
        assert _row_values(db, tid) == ("Supermercado", "", "", "manual")


def _set_ods_cell_at(ods_path: Path, tid: str, col: int, value: str) -> None:
    """Like _set_ods_cell, for a sheet whose columns have moved: finds the
    row by the transaction id wherever it is."""
    def change(sheet, rows):
        for row in rows[1:]:
            cells = _cells(row)
            if tid in [str(c) for c in cells]:
                cell = cells[col]
                for p in cell.getElementsByType(P):
                    cell.removeChild(p)
                cell.addElement(P(text=value))
                return
        raise AssertionError(f"transaction {tid} not found in ODS")
    _edit_data_rows(ods_path, change)


class TestSaveDescriptionNotes:
    """description-notes.csv is the only copy of the merchant notes."""

    def test_round_trip(self, tmp_path):
        from expense_tracker.ods import _load_description_notes, _save_description_notes

        path = tmp_path / "description-notes.csv"
        _save_description_notes({"CAFÉ, O PIPO": 'says "hi"', "A": "#recurring"}, path)

        assert _load_description_notes(path) == {
            "CAFÉ, O PIPO": 'says "hi"', "A": "#recurring",
        }
        assert [p.name for p in tmp_path.iterdir()] == ["description-notes.csv"]

    def test_failed_save_keeps_the_stored_notes(self, tmp_path):
        from expense_tracker.ods import _save_description_notes

        path = tmp_path / "description-notes.csv"
        _save_description_notes({"B": "#recurring"}, path)
        stored = path.read_bytes()

        with pytest.raises(UnicodeEncodeError):
            # a lone surrogate cannot be written as UTF-8
            _save_description_notes({"A": "\ud800", "B": "#recurring"}, path)

        assert path.read_bytes() == stored
        assert [p.name for p in tmp_path.iterdir()] == ["description-notes.csv"]
