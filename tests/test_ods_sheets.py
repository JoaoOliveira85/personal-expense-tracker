"""Analysis sheets of the ODS report: the formulas written on the first run.

No spreadsheet engine is available to evaluate the formulas, so these tests
pin the generated formula text.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from odf.opendocument import load as load_ods
from odf.table import Table, TableCell, TableRow
from odf.text import P

from expense_tracker.db import ingest, migrate_schema
from expense_tracker.ods import generate_ods, retarget_data_ranges
from expense_tracker.ods_sheets import col_letter
from expense_tracker.rules import categorize_transactions, load_rules

from .conftest import add_transaction, make_utf16_csv


def _sheet(doc, name: str):
    return next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == name
    )


def _row(doc, sheet_name: str, label: str) -> list:
    """Cells of the row whose first cell reads `label`."""
    for row in _sheet(doc, sheet_name).getElementsByType(TableRow):
        cells = row.getElementsByType(TableCell)
        if cells and "".join(str(p) for p in cells[0].getElementsByType(P)) == label:
            return cells
    raise AssertionError(f"{label!r} not found in {sheet_name}")


def _formula(doc, sheet_name: str, label: str, column: int = 1) -> str:
    return _row(doc, sheet_name, label)[column].getAttribute("formula")


def _categorize(db: Path, rules_csv: Path) -> None:
    conn = sqlite3.connect(str(db))
    migrate_schema(conn)
    categorize_transactions(conn, load_rules(rules_csv))
    conn.close()


def _import_one_more(db: Path, cards_csv: Path, tmp_path: Path) -> None:
    """A sixth transaction, newer than the five sample ones."""
    csv = make_utf16_csv(
        tmp_path / "EXPORT_0_1022026.csv",
        [("03-02-2026", "03-02-2026", "COMPRA 1234 CONTINENTE PORTO",
          "-20,00", "Compra", "1214,56")],
        date_from="01-02-2026",
        date_to="28-02-2026",
    )
    ingest(db, [csv], cards_path=cards_csv)


@pytest.fixture
def report(populated_db, rules_csv, tmp_path):
    """A categorized DB (5 transactions: Data rows 2-6) and its first-run ODS."""
    _categorize(populated_db, rules_csv)
    ods = tmp_path / "report.ods"
    notes = tmp_path / "notes.csv"
    generate_ods(populated_db, rules_csv, ods, notes)
    return populated_db, ods, notes


class TestRangesFollowTheData:
    """The analysis sheets are written once and kept; the Data sheet under
    them is rebuilt on every run, newest transaction first."""

    def test_first_run_covers_every_row(self, report):
        _, ods, _ = report
        formula = _formula(load_ods(str(ods)), "Dashboard", "Total Expenses")
        assert "[.Data.F2:.Data.F6]" in formula

    def test_new_transactions_are_included_in_the_totals(
        self, report, rules_csv, cards_csv, tmp_path
    ):
        db, ods, notes = report
        _import_one_more(db, cards_csv, tmp_path)

        generate_ods(db, rules_csv, ods, notes)

        doc = load_ods(str(ods))
        assert len(_sheet(doc, "Data").getElementsByType(TableRow)) == 7
        for sheet, label, column in [
            ("Dashboard", "Total Income", 1),
            ("Dashboard", "Total Expenses", 1),
            ("Dashboard", "Uncategorized Transactions", 1),
            ("Dashboard", "Groceries", 1),
            ("Dashboard", "Groceries", 2),
            ("Monthly Summary", "2026-01", 1),
            ("Monthly Trend", "2026-01", 1),
            ("Monthly Trend", "2026-01", 2),
            ("Category Breakdown", "Groceries", 1),
            ("Category Breakdown", "Groceries", 4),
            ("Subcategory Breakdown", "Groceries", 2),
            ("Tags", "#recurring", 1),
            ("Tags", "#recurring", 2),
            ("Tags", "#recurring", 3),
        ]:
            formula = _formula(doc, sheet, label, column)
            last_rows = set(re.findall(r":\.Data\.[A-Z]+(\d+)\]", formula))
            assert last_rows == {"7"}, f"{sheet}/{label}: {formula}"

    def test_fewer_transactions_shrink_the_ranges(self, report, rules_csv):
        db, ods, notes = report
        conn = sqlite3.connect(str(db))
        conn.execute(
            "DELETE FROM transactions WHERE description_raw LIKE '%VODAFONE%'"
        )
        conn.commit()
        conn.close()

        generate_ods(db, rules_csv, ods, notes)

        formula = _formula(load_ods(str(ods)), "Dashboard", "Total Expenses")
        assert "[.Data.F2:.Data.F5]" in formula

    def test_other_cells_of_an_analysis_sheet_are_kept(
        self, report, rules_csv, cards_csv, tmp_path
    ):
        """Only the row range moves: what the user changed stays."""
        db, ods, notes = report
        doc = load_ods(str(ods))
        cells = _row(doc, "Dashboard", "Net Balance (Income - Expenses)")
        cells[1].setAttribute("formula", "of:=[.B4]-[.B5]-100")
        for p in cells[0].getElementsByType(P):
            cells[0].removeChild(p)
        cells[0].addElement(P(text="Net after rent"))
        doc.save(str(ods))
        _import_one_more(db, cards_csv, tmp_path)

        generate_ods(db, rules_csv, ods, notes)

        doc = load_ods(str(ods))
        assert _formula(doc, "Dashboard", "Net after rent") == "of:=[.B4]-[.B5]-100"

    def test_sheets_added_by_the_user_are_not_touched(
        self, report, rules_csv, cards_csv, tmp_path
    ):
        db, ods, notes = report
        doc = load_ods(str(ods))
        mine = Table(name="Mine")
        row = TableRow()
        cell = TableCell(formula="of:=SUM([.Data.F2:.Data.F6])", valuetype="float")
        row.addElement(cell)
        mine.addElement(row)
        doc.spreadsheet.addElement(mine)
        doc.save(str(ods))
        _import_one_more(db, cards_csv, tmp_path)

        generate_ods(db, rules_csv, ods, notes)

        cell = _sheet(load_ods(str(ods)), "Mine").getElementsByType(TableCell)[0]
        assert cell.getAttribute("formula") == "of:=SUM([.Data.F2:.Data.F6])"


class TestRetargetDataRanges:
    """References to Data rows, as this tool writes them and as LibreOffice
    writes them back when the file is saved."""

    @pytest.mark.parametrize(
        "before, after",
        [
            ("[.Data.F2:.Data.F6]", "[.Data.F2:.Data.F9]"),
            ("[Data.F2:Data.F6]", "[Data.F2:Data.F9]"),
            ("[$Data.F2:.F6]", "[$Data.F2:.F9]"),
            ("[Data.F2:.F6]", "[Data.F2:.F9]"),
            ("[$Data.$F$2:.$F$6]", "[$Data.$F$2:.$F$9]"),
            ("[$'Data'.F2:.F6]", "[$'Data'.F2:.F9]"),
            ("[$Data.A2:.S6]", "[$Data.A2:.S9]"),
            ("[.Data.F2:.Data.F1234]", "[.Data.F2:.Data.F9]"),
        ],
    )
    def test_range_from_row_2_ends_at_the_last_row(self, before, after):
        formula = f'of:=SUMPRODUCT(({before}="out")*{before})'
        assert retarget_data_ranges(formula, 9) == (
            f'of:=SUMPRODUCT(({after}="out")*{after})'
        )

    @pytest.mark.parametrize(
        "reference",
        [
            "[.B2:.B6]",                    # same sheet
            "[$'Monthly Trend'.B2:.B6]",    # another sheet
            "[$MyData.F2:.F6]",             # a sheet whose name ends in Data
            "[$Data.F$1:.F$1048576]",       # a whole column (Data.F:F)
            "[$Data.F3:.F6]",               # not from the first data row
            "[$Data.F2]",                   # a single cell
            "[.Data.F20:.Data.F60]",
        ],
    )
    def test_other_references_are_left_alone(self, reference):
        formula = f"of:=SUM({reference})"
        assert retarget_data_ranges(formula, 9) == formula

    def test_empty_data_sheet_keeps_a_valid_range(self):
        assert retarget_data_ranges("of:=SUM([.Data.F2:.Data.F6])", 1) == (
            "of:=SUM([.Data.F2:.Data.F2])"
        )


class TestMonthlySummaryColumns:
    """One column per spending category: the sheet runs past column Z as soon
    as there are 25 of them (A is the month, the total comes last)."""

    @pytest.fixture
    def doc(self, test_db, rules_csv, tmp_path):
        for i in range(1, 31):
            add_transaction(test_db, "2026-01-10", f"SHOP {i}", 10.0, category=f"Cat{i:02d}")
        ods = tmp_path / "report.ods"
        generate_ods(test_db, rules_csv, ods, tmp_path / "notes.csv")
        return load_ods(str(ods))

    def test_row_total_sums_all_30_category_columns(self, doc):
        cells = _row(doc, "Monthly Summary", "2026-01")
        assert len(cells) == 32  # month + 30 categories + total
        assert cells[31].getAttribute("formula") == "of:=SUM([.B2:.AE2])"

    def test_total_row_uses_the_letters_of_its_own_columns(self, doc):
        cells = _row(doc, "Monthly Summary", "TOTAL")
        assert cells[25].getAttribute("formula") == "of:=SUM([.Z2:.Z2])"
        assert cells[26].getAttribute("formula") == "of:=SUM([.AA2:.AA2])"
        assert cells[30].getAttribute("formula") == "of:=SUM([.AE2:.AE2])"
        assert cells[31].getAttribute("formula") == "of:=SUM([.AF2:.AF2])"

    @pytest.mark.parametrize(
        "index, letters",
        [(1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"), (702, "ZZ"), (703, "AAA")],
    )
    def test_col_letter(self, index, letters):
        assert col_letter(index) == letters


class TestDashboardCounts:
    def test_total_transactions_is_counted_by_a_formula(
        self, report, rules_csv, cards_csv, tmp_path
    ):
        """A number written on the first run would never change again."""
        db, ods, notes = report
        cells = _row(load_ods(str(ods)), "Dashboard", "Total Transactions")
        assert cells[1].getAttribute("formula") == "of:=COUNTA([.Data.Q2:.Data.Q6])"

        _import_one_more(db, cards_csv, tmp_path)
        generate_ods(db, rules_csv, ods, notes)

        cells = _row(load_ods(str(ods)), "Dashboard", "Total Transactions")
        assert cells[1].getAttribute("formula") == "of:=COUNTA([.Data.Q2:.Data.Q7])"

    def test_uncategorized_count_separates_arguments_with_semicolons(self, report):
        """OpenFormula separates function arguments with ';' (a comma is the
        Excel spelling and not valid in an ODS formula)."""
        _, ods, _ = report
        cells = _row(load_ods(str(ods)), "Dashboard", "Uncategorized Transactions")
        assert cells[1].getAttribute("formula") == (
            'of:=COUNTIF([.Data.M2:.Data.M6];"uncategorized")'
        )
