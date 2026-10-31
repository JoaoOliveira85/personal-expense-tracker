"""Refunds in spreadsheet reports: formulas must net them against spending.

No spreadsheet engine is available to evaluate the formulas, so these tests
pin the generated formula text.
"""

from __future__ import annotations

import sqlite3

import pytest
from odf.opendocument import load as load_ods
from odf.table import Table, TableCell, TableRow
from odf.text import P
from openpyxl import load_workbook

from expense_tracker.db import migrate_schema
from expense_tracker.ods import generate_ods
from expense_tracker.parser import _get_cleaning_patterns, reset_cleaning_cache
from expense_tracker.rules import categorize_transactions, load_rules
from expense_tracker.xlsx import generate_xlsx

N = 6  # last Data row: 5 sample transactions + header

XLSX_NEUTRAL = ',Data!H:H,"<>Savings",Data!H:H,"<>Ignored"'
XLSX_REFUNDS = (
    'Data!G:G,"in",Data!H:H,"<>",Data!H:H,"<>Income",Data!H:H,"<>Transfers"'
    + XLSX_NEUTRAL
)
ODS_NEUTRAL = f'*([.Data.H2:.Data.H{N}]<>"Savings")*([.Data.H2:.Data.H{N}]<>"Ignored")'
ODS_SPEND = (
    f'(([.Data.G2:.Data.G{N}]="out"){ODS_NEUTRAL}'
    f'-([.Data.G2:.Data.G{N}]="in")*([.Data.H2:.Data.H{N}]<>"")'
    f'*([.Data.H2:.Data.H{N}]<>"Income")*([.Data.H2:.Data.H{N}]<>"Transfers")'
    f"{ODS_NEUTRAL})"
    f"*[.Data.F2:.Data.F{N}]"
)


@pytest.fixture
def db(populated_db, rules_csv, noise_words, cleaning_patterns):
    """Sample DB, categorized, with the EXEMPLO payment turned into an
    Insurance refund (the only Insurance transaction) and the VODAFONE
    payment into a Savings deposit."""
    reset_cleaning_cache()
    _get_cleaning_patterns(noise_words, cleaning_patterns)
    conn = sqlite3.connect(str(populated_db))
    migrate_schema(conn)
    categorize_transactions(conn, load_rules(rules_csv))
    conn.execute(
        "UPDATE transactions SET direction = 'in', category = 'Insurance' "
        "WHERE description_raw LIKE '%EXEMPLO%'"
    )
    conn.execute(
        "UPDATE transactions SET category = 'Savings' "
        "WHERE description_raw LIKE '%VODAFONE%'"
    )
    conn.commit()
    conn.close()
    yield populated_db
    reset_cleaning_cache()


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------


@pytest.fixture
def workbook(db, rules_csv, tmp_path):
    path = tmp_path / "report.xlsx"
    generate_xlsx(db, rules_csv, path, tmp_path / "notes.csv")
    return load_workbook(path)


def _row_by_label(ws, label: str) -> int:
    for row in ws.iter_rows(min_col=1, max_col=1):
        if row[0].value == label:
            return row[0].row
    raise AssertionError(f"{label!r} not found in {ws.title}")


class TestXlsxRefunds:
    def test_category_total_nets_refunds(self, workbook):
        ws = workbook["Category Breakdown"]
        row = _row_by_label(ws, "Health")
        assert ws.cell(row=row, column=2).value == (
            '=SUMIFS(Data!F:F,Data!H:H,"Health",Data!G:G,"out")'
            '-SUMIFS(Data!F:F,Data!H:H,"Health",Data!G:G,"in")'
        )

    def test_category_with_only_refunds_is_listed(self, workbook):
        ws = workbook["Category Breakdown"]
        assert _row_by_label(ws, "Insurance")

    def test_dashboard_totals_net_refunds(self, workbook):
        ws = workbook["Dashboard"]
        income = ws.cell(row=_row_by_label(ws, "Total Income"), column=2).value
        expenses = ws.cell(row=_row_by_label(ws, "Total Expenses"), column=2).value
        assert income == (
            f'=SUMIFS(Data!F:F,Data!G:G,"in"{XLSX_NEUTRAL})'
            f"-SUMIFS(Data!F:F,{XLSX_REFUNDS})"
        )
        assert expenses == (
            f'=SUMIFS(Data!F:F,Data!G:G,"out"{XLSX_NEUTRAL})'
            f"-SUMIFS(Data!F:F,{XLSX_REFUNDS})"
        )

    def test_savings_is_not_a_spending_category(self, workbook):
        with pytest.raises(AssertionError):
            _row_by_label(workbook["Category Breakdown"], "Savings")

    def test_monthly_trend_nets_refunds(self, workbook):
        ws = workbook["Monthly Trend"]
        month = 'Data!B:B,"2026-01",'
        assert ws["B2"].value == (
            f'=SUMIFS(Data!F:F,{month}Data!G:G,"in"{XLSX_NEUTRAL})'
            f"-SUMIFS(Data!F:F,{month}{XLSX_REFUNDS})"
        )
        assert ws["C2"].value == (
            f'=SUMIFS(Data!F:F,{month}Data!G:G,"out"{XLSX_NEUTRAL})'
            f"-SUMIFS(Data!F:F,{month}{XLSX_REFUNDS})"
        )


# ---------------------------------------------------------------------------
# ODS
# ---------------------------------------------------------------------------


@pytest.fixture
def ods_doc(db, rules_csv, tmp_path):
    path = tmp_path / "report.ods"
    generate_ods(db, rules_csv, path, tmp_path / "notes.csv")
    return load_ods(str(path))


def _ods_row(doc, sheet_name: str, label: str) -> list:
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


class TestOdsRefunds:
    def test_category_total_nets_refunds(self, ods_doc):
        cells = _ods_row(ods_doc, "Category Breakdown", "Health")
        assert cells[1].getAttribute("formula") == (
            f'of:=SUMPRODUCT(([.Data.H2:.Data.H{N}]="Health")*{ODS_SPEND})'
        )

    def test_category_with_only_refunds_is_listed(self, ods_doc):
        assert _ods_row(ods_doc, "Category Breakdown", "Insurance")

    def test_savings_is_not_a_spending_category(self, ods_doc):
        with pytest.raises(AssertionError):
            _ods_row(ods_doc, "Category Breakdown", "Savings")

    def test_dashboard_total_expenses_nets_refunds(self, ods_doc):
        cells = _ods_row(ods_doc, "Dashboard", "Total Expenses")
        assert cells[1].getAttribute("formula") == f"of:=SUMPRODUCT({ODS_SPEND})"


# ---------------------------------------------------------------------------
# Miscellaneous (issue #9): an ordinary, categorized spending category
# ---------------------------------------------------------------------------


@pytest.fixture
def misc_db(db):
    """The FARMACIA payment deliberately filed under Miscellaneous."""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE transactions SET category = 'Miscellaneous', "
        "category_source = 'manual' WHERE description_raw LIKE '%FARMACIA%'"
    )
    conn.commit()
    conn.close()
    return db


class TestMiscellaneous:
    def test_xlsx_lists_it_and_counts_it_as_categorized(
        self, misc_db, rules_csv, tmp_path
    ):
        from expense_tracker.constants import COL_CATEGORY, DATA_COLUMNS

        status = DATA_COLUMNS.index("status")
        path = tmp_path / "report.xlsx"
        generate_xlsx(misc_db, rules_csv, path, tmp_path / "notes.csv")
        wb = load_workbook(path)

        ws = wb["Category Breakdown"]
        assert ws.cell(row=_row_by_label(ws, "Miscellaneous"), column=2).value == (
            '=SUMIFS(Data!F:F,Data!H:H,"Miscellaneous",Data!G:G,"out")'
            '-SUMIFS(Data!F:F,Data!H:H,"Miscellaneous",Data!G:G,"in")'
        )
        rows = [
            (r[COL_CATEGORY].value, r[status].value)
            for r in wb["Data"].iter_rows(min_row=2)
        ]
        assert ("Miscellaneous", "categorized") in rows

    def test_ods_lists_it_and_leaves_it_out_of_uncategorized(
        self, misc_db, rules_csv, tmp_path
    ):
        path = tmp_path / "report.ods"
        generate_ods(misc_db, rules_csv, path, tmp_path / "notes.csv")
        doc = load_ods(str(path))

        assert _ods_row(doc, "Category Breakdown", "Miscellaneous")
        dashboard = next(
            s
            for s in doc.spreadsheet.getElementsByType(Table)
            if s.getAttribute("name") == "Dashboard"
        )
        texts = [str(p) for p in dashboard.getElementsByType(P)]
        preview = texts[texts.index("Uncategorized Preview (up to 15)") :]
        assert "All transactions are categorized!" in preview


# ---------------------------------------------------------------------------
# Ignored (issue #6): neither income nor spending, in no total or ranking
# ---------------------------------------------------------------------------


@pytest.fixture
def ignored_db(test_db):
    """Three months of a transfer to and from an own account, filed under
    Ignored and tagged, next to ordinary groceries and a salary."""
    from .conftest import add_transaction

    for month in ("2026-01", "2026-02", "2026-03"):
        add_transaction(
            test_db, f"{month}-05", "OWN ACCOUNT", 900, "out", "Ignored", notes="#x"
        )
        add_transaction(test_db, f"{month}-07", "GROCER", 40, "out", "Groceries")
    add_transaction(test_db, "2026-01-06", "OWN ACCOUNT", 300, "in", "Ignored")
    add_transaction(test_db, "2026-01-25", "SALARY", 2000, "in", "Income")
    return test_db


def _labels(ws) -> list:
    return [row[0].value for row in ws.iter_rows(min_col=1, max_col=1)]


def _ods_labels(doc, sheet_name: str) -> list[str]:
    sheet = next(
        s
        for s in doc.spreadsheet.getElementsByType(Table)
        if s.getAttribute("name") == sheet_name
    )
    labels = []
    for row in sheet.getElementsByType(TableRow):
        cells = row.getElementsByType(TableCell)
        if cells:
            labels.append("".join(str(p) for p in cells[0].getElementsByType(P)))
    return labels


class TestIgnoredInXlsx:
    @pytest.fixture
    def wb(self, ignored_db, rules_csv, tmp_path):
        path = tmp_path / "report.xlsx"
        generate_xlsx(ignored_db, rules_csv, path, tmp_path / "notes.csv")
        return load_workbook(path)

    def test_totals_leave_it_out(self, wb):
        ws = wb["Dashboard"]
        expenses = ws.cell(row=_row_by_label(ws, "Total Expenses"), column=2).value
        assert expenses.startswith(f'=SUMIFS(Data!F:F,Data!G:G,"out"{XLSX_NEUTRAL})')

    def test_no_category_or_merchant_row(self, wb):
        assert "Ignored" not in _labels(wb["Category Breakdown"])
        assert "Ignored" not in _labels(wb["Dashboard"])
        assert "OWN ACCOUNT" not in _labels(wb["Dashboard"])
        assert "Ignored" not in [c.value for c in wb["Monthly Summary"][1]]

    def test_not_a_recurring_merchant_or_tag_total(self, wb):
        assert "GROCER" in _labels(wb["Recurring Merchants"])
        assert "OWN ACCOUNT" not in _labels(wb["Recurring Merchants"])
        assert "#x" not in _labels(wb["Tags"])

    def test_still_in_the_ledger(self, wb):
        descriptions = [c.value for c in wb["Data"]["E"]]
        assert descriptions.count("OWN ACCOUNT") == 4


class TestIgnoredInOds:
    @pytest.fixture
    def doc(self, ignored_db, rules_csv, tmp_path):
        path = tmp_path / "report.ods"
        generate_ods(ignored_db, rules_csv, path, tmp_path / "notes.csv")
        return load_ods(str(path))

    def test_totals_leave_it_out(self, doc):
        cells = _ods_row(doc, "Dashboard", "Total Expenses")
        assert '[.Data.H2:.Data.H9]<>"Ignored"' in cells[1].getAttribute("formula")

    def test_no_category_or_merchant_row(self, doc):
        assert "Ignored" not in _ods_labels(doc, "Category Breakdown")
        assert "OWN ACCOUNT" not in _ods_labels(doc, "Dashboard")

    def test_not_a_recurring_merchant_or_tag_total(self, doc):
        assert "GROCER" in _ods_labels(doc, "Recurring Merchants")
        assert "OWN ACCOUNT" not in _ods_labels(doc, "Recurring Merchants")
        cells = _ods_row(doc, "Tags", "#recurring")
        for total in (cells[2], cells[3]):
            assert '[.Data.H2:.Data.H9]<>"Ignored"' in total.getAttribute("formula")
