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
from expense_tracker.ods import SyncConflict, generate_ods, sync_from_ods
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

        assert sync_from_ods(db, ods, notes).updated == 1
        assert _category(db, tid) == "Household"

    def test_unedited_rows_are_not_synced(self, report):
        db, ods, notes, _ = report
        assert sync_from_ods(db, ods, notes).updated == 0

    def test_stale_spreadsheet_does_not_revert_newer_db_value(self, report):
        """The DB changed after the ODS was written (e.g. rules re-applied from
        the GUI, or the xlsx report is in use): the ODS still shows the old
        value, which is not an edit and must not overwrite the DB."""
        db, ods, notes, tid = report
        _set_db_category(db, tid, "Changed Later")

        assert sync_from_ods(db, ods, notes).updated == 0
        assert _category(db, tid) == "Changed Later"

    def test_edit_is_applied_only_once(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")
        sync_from_ods(db, ods, notes)
        _set_db_category(db, tid, "Changed Later")

        assert sync_from_ods(db, ods, notes).updated == 0
        assert _category(db, tid) == "Changed Later"


def _tid(db: Path, like: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT transaction_id FROM transactions WHERE description_raw LIKE ?",
            (f"%{like}%",),
        ).fetchone()[0]
    finally:
        conn.close()


def _set_db_notes(db: Path, tid: str, value: str) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE transactions SET notes = ? WHERE transaction_id = ?", (value, tid)
    )
    conn.commit()
    conn.close()


class TestSyncConflicts:
    """An edited cell is applied only while the database still holds the
    value the sheet was written with; otherwise it is a conflict, reported
    with both values and not applied (issue #14)."""

    def test_edit_with_an_unchanged_database_applies(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Household")

        result = sync_from_ods(db, ods, notes)

        assert (result.updated, result.conflicts) == (1, [])
        assert _category(db, tid) == "Household"

    def test_edit_after_a_rule_changed_the_row_is_reported_not_applied(self, report):
        db, ods, notes, tid = report
        _set_db_category(db, tid, "Supermarket")  # a rule run, after the sheet
        _set_ods_category(ods, tid, "Household")

        result = sync_from_ods(db, ods, notes)

        assert result.updated == 0
        assert result.conflicts == [
            SyncConflict(tid, "category", "Household", "Supermarket")
        ]
        assert _category(db, tid) == "Supermarket"

    def test_notes_edited_from_another_route_conflict(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        _set_db_notes(db, tid, "from the GUI")
        _set_ods_cell(ods, tid, COL_NOTES, "from the sheet")

        result = sync_from_ods(db, ods, notes)

        assert result.conflicts == [
            SyncConflict(tid, "notes", "from the sheet", "from the GUI")
        ]
        assert _note(db, tid) == "from the GUI"

    def test_unedited_cell_next_to_a_conflicting_one_is_not_touched(self, report):
        db, ods, notes, tid = report
        _set_db_category(db, tid, "Supermarket")
        _set_db_notes(db, tid, "newer note")
        _set_ods_category(ods, tid, "Household")

        sync_from_ods(db, ods, notes)

        assert _category(db, tid) == "Supermarket"
        assert _note(db, tid) == "newer note"

    def test_other_edits_of_the_row_and_the_sheet_still_apply(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        other = _tid(db, "FARMACIA")
        _set_db_category(db, tid, "Supermarket")
        _set_ods_category(ods, tid, "Household")
        _set_ods_cell(ods, tid, COL_NOTES, "birthday dinner")
        _set_ods_category(ods, other, "Pharmacy")

        result = sync_from_ods(db, ods, notes)

        assert result.updated == 2
        assert [c.transaction_id for c in result.conflicts] == [tid]
        assert _category(db, tid) == "Supermarket"
        assert _note(db, tid) == "birthday dinner"
        assert _category(db, other) == "Pharmacy"

    def test_conflict_is_reported_again_until_resolved(self, report):
        db, ods, notes, tid = report
        _set_db_category(db, tid, "Supermarket")
        _set_ods_category(ods, tid, "Household")
        sync_from_ods(db, ods, notes)

        again = sync_from_ods(db, ods, notes)
        assert again.conflicts == [
            SyncConflict(tid, "category", "Household", "Supermarket")
        ]

        # The cell set to the database value: nothing left to apply
        _set_ods_category(ods, tid, "Supermarket")
        assert sync_from_ods(db, ods, notes) == (0, [])
        assert sync_from_ods(db, ods, notes) == (0, [])
        assert _category(db, tid) == "Supermarket"

    def test_conflict_names_the_transaction_and_both_values(self):
        message = SyncConflict("abc123", "category", "Household", "").describe()

        assert "abc123" in message
        assert "'Household'" in message
        assert "(empty)" in message


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


class TestMiscellaneousIsSettled:
    """Set by hand, Miscellaneous is a manual category: rule runs and
    re-imports keep it (issue #9)."""

    def test_survives_rules_and_reimport(self, report, rules_csv, utf16_csv, cards_csv):
        from expense_tracker.db import ingest

        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Miscellaneous")
        sync_from_ods(db, ods, notes)

        ingest(db, [utf16_csv], cards_path=cards_csv)
        conn = sqlite3.connect(str(db))
        categorize_transactions(conn, load_rules(rules_csv))  # CONTINENTE rule
        conn.close()

        assert _category(db, tid) == "Miscellaneous"
        assert _source(db, tid) == "manual"


class TestSyncValidatesCategories:
    """With the category list given, an edited Category cell must name a
    category in it (issue #5)."""

    CATEGORIES = ["Groceries", "Health", "Household", "Income", "Utilities"]

    def test_unknown_category_is_skipped_and_reported(self, report, caplog):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Housewares")

        with caplog.at_level("WARNING", logger="expense_tracker"):
            assert (
                sync_from_ods(db, ods, notes, categories=self.CATEGORIES).updated == 0
            )

        assert _category(db, tid) == "Groceries"
        assert any(
            "Housewares" in r.getMessage() and tid in r.getMessage()
            for r in caplog.records
        )

    def test_other_case_is_normalised_to_the_list(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, " household ")

        assert sync_from_ods(db, ods, notes, categories=self.CATEGORIES).updated == 1
        assert _category(db, tid) == "Household"

    def test_clearing_is_still_allowed(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "")

        assert sync_from_ods(db, ods, notes, categories=self.CATEGORIES).updated == 1
        assert _source(db, tid) is None

    def test_other_edits_of_the_row_still_apply(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Housewares")
        _set_ods_cell(ods, tid, COL_NOTES, "kept")

        assert sync_from_ods(db, ods, notes, categories=self.CATEGORIES).updated == 1
        assert _category(db, tid) == "Groceries"
        conn = sqlite3.connect(str(db))
        note = conn.execute(
            "SELECT notes FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
        conn.close()
        assert note == "kept"


def _category_validation(ods_path: Path):
    """The Data sheet's Category drop-down: (validation condition, names of
    the validations its cells use)."""
    from odf.table import ContentValidation

    doc = load_ods(str(ods_path))
    validations = {
        v.getAttribute("name"): v.getAttribute("condition")
        for v in doc.getElementsByType(ContentValidation)
    }
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == "Data"
    )
    used = {
        _cells(row)[COL_CATEGORY].getAttribute("contentvalidationname")
        for row in sheet.getElementsByType(TableRow)[1:]
    }
    return validations, used


class TestCategoryDropDown:
    """The generated Data sheet offers the category list on its Category
    column (issue #5)."""

    def test_category_cells_offer_the_list(self, report, rules_csv):
        from expense_tracker.rules import categories_file, load_categories

        _, ods, _, _ = report
        validations, used = _category_validation(ods)

        assert len(used) == 1
        (name,) = used
        expected = ";".join(
            f'"{c}"' for c in load_categories(categories_file(rules_csv))
        )
        assert validations[name] == f"of:cell-content-is-in-list({expected})"

    def test_update_replaces_the_list(self, report, rules_csv):
        from expense_tracker.rules import add_category, categories_file

        db, ods, notes, _ = report
        add_category(categories_file(rules_csv), 'Say "cheese"')
        generate_ods(db, rules_csv, ods, notes)

        validations, used = _category_validation(ods)
        (name,) = used
        assert list(validations) == [name]
        assert '"Say ""cheese"""' in validations[name]


class TestEmptyPlaceholderOds:
    """DEPLOYMENT.md has users `touch expense-report.ods` for the bind mount."""

    def test_sync_ignores_empty_file(self, populated_db, tmp_path):
        ods = tmp_path / "expense-report.ods"
        ods.touch()

        assert sync_from_ods(populated_db, ods, tmp_path / "notes.csv").updated == 0

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
                (
                    "20-01-2026",
                    "20-01-2026",
                    "MERCEARIA DO BAIRRO",
                    "-8,00",
                    "Compra",
                    "992,00",
                ),
                (
                    "05-01-2026",
                    "05-01-2026",
                    "MERCEARIA DO BAIRRO",
                    "-12,00",
                    "Compra",
                    "1000,00",
                ),
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

        assert sync_from_ods(db, ods, notes).updated == 1

        assert _row_values(db, tid) == (
            "Supermarket",
            "Food",
            "birthday dinner",
            "rule",
        )

    def test_category_edit_keeps_a_newer_note(self, report):
        db, ods, notes, tid = report
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE transactions SET notes = 'kept' WHERE transaction_id = ?", (tid,)
        )
        conn.commit()
        conn.close()
        _set_ods_category(ods, tid, "Household")

        assert sync_from_ods(db, ods, notes).updated == 1

        category, _, note, source = _row_values(db, tid)
        assert (category, note, source) == ("Household", "kept", "manual")

    def test_stale_row_is_not_applied_on_the_next_sync_either(self, report):
        from expense_tracker.constants import COL_NOTES

        db, ods, notes, tid = report
        _set_db_category(db, tid, "Supermarket")
        _set_ods_cell(ods, tid, COL_NOTES, "birthday dinner")
        sync_from_ods(db, ods, notes)

        assert sync_from_ods(db, ods, notes).updated == 0
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
        assert sync_from_ods(db, ods, notes).updated == 1
        return db, ods, notes, tid

    def test_inserted_column_does_not_shift_what_is_synced(self, noted):
        """A column added before Notes moved Notes to S and Merchant Note to
        T. Read by position, every note looked deleted and became the
        merchant note of its row."""
        from expense_tracker.constants import COL_NOTES
        from expense_tracker.ods import _load_description_notes

        db, ods, notes, tid = noted
        _edit_data_rows(ods, _insert_column(COL_NOTES, "My column"))

        assert sync_from_ods(db, ods, notes).updated == 0

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

        assert sync_from_ods(db, ods, notes).updated == 1

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

        assert sync_from_ods(db, ods, notes).updated == 1
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

        assert sync_from_ods(db, ods, notes).updated == 1
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

        assert sync_from_ods(db, ods, notes).updated == 1
        assert _category(db, tid) == "Household"

    def test_deleted_row_keeps_its_transaction(self, noted, rules_csv):
        db, ods, notes, tid = noted

        def delete(sheet, rows):
            for row in rows[1:]:
                if str(_cells(row)[COL_TRANSACTION_ID]) == tid:
                    sheet.removeChild(row)

        _edit_data_rows(ods, delete)

        assert sync_from_ods(db, ods, notes).updated == 0
        assert _row_values(db, tid)[:3] == ("Groceries", "", "birthday dinner")

        generate_ods(db, rules_csv, ods, notes)
        assert sync_from_ods(db, ods, notes).updated == 0
        assert _row_values(db, tid)[:3] == ("Groceries", "", "birthday dinner")

    def test_text_typed_into_the_amount_cell_is_not_synced(self, report):
        db, ods, notes, tid = report
        _set_ods_cell(ods, tid, 5, "forty-five")  # F: Amount

        assert sync_from_ods(db, ods, notes).updated == 0

        conn = sqlite3.connect(str(db))
        amount = conn.execute(
            "SELECT amount_abs FROM transactions WHERE transaction_id = ?", (tid,)
        ).fetchone()[0]
        conn.close()
        assert amount == 45.5

    def test_renamed_category_is_kept_as_a_manual_edit(self, report):
        db, ods, notes, tid = report
        _set_ods_category(ods, tid, "Supermercado")

        assert sync_from_ods(db, ods, notes).updated == 1
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
            "CAFÉ, O PIPO": 'says "hi"',
            "A": "#recurring",
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
