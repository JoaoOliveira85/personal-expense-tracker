"""Reports show one currency at a time and never add currencies together.

A ledger with a second account in another currency must give, for the
first currency, exactly the reports the ledger without it gives.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest

from expense_tracker import advisor, cli, pdf_report, suggest
from expense_tracker.db import CurrencyError, ingest, set_account_currency
from expense_tracker.export import export_csv
from expense_tracker.ods import generate_ods
from expense_tracker.xlsx import generate_xlsx

from .conftest import add_transaction

RULES = "pattern,match_field,category,subcategory,payment_type\n"

EUR_ROWS = [
    ("2026-01-05", "SALARY", 2000.0, "in", "Income"),
    ("2026-01-06", "LIDL", 30.0, "out", "Groceries"),
    ("2026-01-07", "NETFLIX", 12.99, "out", None),
    ("2026-02-05", "SALARY", 2000.0, "in", "Income"),
    ("2026-02-06", "LIDL", 40.0, "out", "Groceries"),
    ("2026-02-07", "NETFLIX", 12.99, "out", None),
    ("2026-03-07", "NETFLIX", 12.99, "out", None),
]
# Same merchants and categories: a total that mixed currencies would show
USD_ROWS = [
    ("2026-01-10", "SALARY", 1000.0, "in", "Income"),
    ("2026-02-11", "LIDL", 50.0, "out", "Groceries"),
    ("2026-02-12", "NETFLIX", 15.99, "out", None),
    ("2026-03-12", "NETFLIX", 15.99, "out", None),
]


def _add(db_path: Path, rows, account: str, currency: str) -> None:
    for date, desc, amount, direction, category in rows:
        tid = add_transaction(db_path, date, desc, amount, direction, category)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE transactions SET account = ?, currency = ? "
            "WHERE transaction_id = ?",
            (account, currency, tid),
        )
        conn.commit()
        conn.close()


def _ledger(root: Path, with_usd: bool) -> Path:
    root.mkdir()
    db_path = root / "ledger.sqlite"
    _add(db_path, EUR_ROWS, "checkings_account", "EUR")
    if with_usd:
        _add(db_path, USD_ROWS, "usd_account", "USD")
    (root / "rules.csv").write_text(RULES, encoding="utf-8")
    return db_path


@pytest.fixture
def eur_only(tmp_path: Path) -> Path:
    return _ledger(tmp_path / "eur", with_usd=False)


@pytest.fixture
def two_currencies(tmp_path: Path) -> Path:
    return _ledger(tmp_path / "both", with_usd=True)


def _xlsx(db_path: Path, currency: str | None = None) -> dict:
    from openpyxl import load_workbook

    out = db_path.parent / f"report-{currency}.xlsx"
    generate_xlsx(db_path, db_path.parent / "rules.csv", out, currency=currency)
    return {
        ws.title: [[c.value for c in row] for row in ws.iter_rows()]
        for ws in load_workbook(out).worksheets
    }


def _ods(db_path: Path, currency: str | None = None) -> str:
    out = db_path.parent / f"report-{currency}.ods"
    generate_ods(db_path, db_path.parent / "rules.csv", out, currency=currency)
    with zipfile.ZipFile(out) as z:
        content = z.read("content.xml").decode()
    # odfpy declares the namespaces it has seen in this process so far
    return re.sub(r' xmlns:\w+="[^"]*"', "", content)


def _pdf(db_path: Path, month: str, currency: str | None = None) -> str:
    import pdfplumber

    out = db_path.parent / f"report-{month}-{currency}.pdf"
    pdf_report.generate_monthly_pdf(
        db_path, month, out, db_path.parent / "none.csv", currency=currency
    )
    with pdfplumber.open(str(out)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _prompt(db_path: Path, currency: str | None = None) -> str:
    return advisor.generate_prompt(
        db_path, db_path.parent / "context.json", "2026-02", currency=currency
    )


def _csv(db_path: Path, currency: str | None = None) -> str:
    out = db_path.parent / f"ledger-{currency}.csv"
    export_csv(db_path, out, currency=currency)
    return out.read_text(encoding="utf-8")


def _suggestions(db_path: Path, currency: str | None = None) -> str:
    results = suggest.analyze_patterns(
        db_path=db_path, min_months_recurring=2, min_count_frequent=2, currency=currency
    )
    return suggest.format_suggestions(results)


REPORTS = {
    "xlsx": _xlsx,
    "ods": _ods,
    "pdf": lambda db, currency=None: _pdf(db, "2026-02", currency),
    "advisor": _prompt,
    "csv": _csv,
    "suggest": _suggestions,
}


@pytest.mark.parametrize("report", sorted(REPORTS))
class TestEveryReport:
    def test_other_currency_leaves_the_report_unchanged(
        self, report, eur_only, two_currencies
    ):
        make = REPORTS[report]

        assert make(two_currencies, "EUR") == make(eur_only)

    def test_single_currency_needs_no_choice(self, report, eur_only):
        make = REPORTS[report]

        assert make(eur_only, "EUR") == make(eur_only)

    def test_several_currencies_need_a_choice(self, report, two_currencies):
        with pytest.raises(CurrencyError, match="EUR, USD"):
            REPORTS[report](two_currencies)


class TestOtherCurrencyFigures:
    """The USD report holds the USD figures alone, labelled USD."""

    def test_pdf(self, two_currencies):
        text = _pdf(two_currencies, "2026-02", "USD")

        assert "0.00 USD 65.99 USD -65.99 USD" in text
        assert "EUR" not in text

    def test_advisor_prompt(self, two_currencies):
        prompt = _prompt(two_currencies, "USD")

        assert "- **Expenses**: 65.99 USD" in prompt
        assert "- **Income**: 1,000.00 USD" in prompt
        assert "€" not in prompt

    def test_euro_advisor_prompt_keeps_the_euro_sign(self, two_currencies):
        assert "- **Expenses**: €52.99" in _prompt(two_currencies, "EUR")

    def test_workbook_data_sheet(self, two_currencies):
        data = _xlsx(two_currencies, "USD")["Data"]

        assert sorted(row[5] for row in data[1:]) == [15.99, 15.99, 50.0, 1000.0]

    def test_csv_export(self, two_currencies):
        lines = _csv(two_currencies, "USD").splitlines()

        assert len(lines) == 1 + len(USD_ROWS)

    def test_suggestions(self, two_currencies):
        text = _suggestions(two_currencies, "USD")

        assert "15.99 USD" in text
        assert "€" not in text

    def test_lower_case_choice(self, two_currencies):
        assert _csv(two_currencies, "usd") == _csv(two_currencies, "USD")

    def test_currency_the_ledger_does_not_hold(self, two_currencies):
        with pytest.raises(CurrencyError, match="GBP"):
            _csv(two_currencies, "GBP")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "data" / "rules.csv").write_text(RULES, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXPENSE_TRACKER_CURRENCY", raising=False)
    db_path = tmp_path / "data" / "ledger.sqlite"
    _add(db_path, EUR_ROWS, "checkings_account", "EUR")
    return tmp_path


def _args(root: Path, **kwargs) -> argparse.Namespace:
    values = dict(
        db=root / "data" / "ledger.sqlite",
        rules=root / "data" / "rules.csv",
        out=root / "expense-report.xlsx",
        format="xlsx",
        desc_notes=root / "data" / "description-notes.csv",
        backup_dir=root / "backups",
        raw=root / "raw",
        fresh=False,
        no_sync=True,
        quiet=True,
        verbose=False,
        currency=None,
    )
    values.update(kwargs)
    return argparse.Namespace(**values)


def _add_usd(root: Path) -> None:
    _add(root / "data" / "ledger.sqlite", USD_ROWS, "usd_account", "USD")


def _data_amounts(path: Path) -> list[float]:
    from openpyxl import load_workbook

    return sorted(row[5].value for row in load_workbook(path)["Data"].iter_rows(2))


class TestCommandLine:
    def test_report_on_a_single_currency_ledger(self, workspace: Path):
        cli.cmd_report(_args(workspace))

        assert len(_data_amounts(workspace / "expense-report.xlsx")) == len(EUR_ROWS)

    def test_report_without_a_choice_stops(self, workspace: Path, capsys):
        _add_usd(workspace)

        with pytest.raises(SystemExit) as excinfo:
            cli.cmd_report(_args(workspace))

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "EUR, USD" in out and "--currency" in out
        assert not (workspace / "expense-report.xlsx").exists()

    def test_report_of_the_chosen_currency(self, workspace: Path):
        _add_usd(workspace)

        cli.cmd_report(_args(workspace, currency="USD"))

        assert _data_amounts(workspace / "expense-report.xlsx") == [
            15.99,
            15.99,
            50.0,
            1000.0,
        ]

    def test_pdf_without_a_choice_stops(self, workspace: Path, capsys):
        _add_usd(workspace)

        with pytest.raises(SystemExit):
            cli.cmd_pdf(_args(workspace, month="2026-02", all=False, out=None))

        assert "EUR, USD" in capsys.readouterr().out

    def test_pdf_all_months_of_the_chosen_currency(self, workspace: Path, capsys):
        _add_usd(workspace)

        cli.cmd_pdf(_args(workspace, month=None, all=True, out=None, currency="USD"))

        assert sorted(p.name for p in (workspace / "reports").glob("*.pdf")) == [
            "report-2026-01.pdf",
            "report-2026-02.pdf",
            "report-2026-03.pdf",
        ]

    def test_export_without_a_choice_stops(self, workspace: Path, capsys):
        _add_usd(workspace)

        with pytest.raises(SystemExit):
            cli.cmd_export(_args(workspace, out=workspace / "x.csv"))

        assert "EUR, USD" in capsys.readouterr().out
        assert not (workspace / "x.csv").exists()

    def test_advisor_without_a_choice_stops(self, workspace: Path, capsys):
        _add_usd(workspace)

        with pytest.raises(SystemExit):
            cli.cmd_advisor(
                _args(
                    workspace,
                    action="generate",
                    month="2026-02",
                    context=workspace / "data" / "context.json",
                    out=workspace / "data" / "advisor",
                )
            )

        assert "EUR, USD" in capsys.readouterr().out

    def test_suggest_without_a_choice_stops(self, workspace: Path, capsys):
        _add_usd(workspace)

        with pytest.raises(SystemExit):
            cli.cmd_suggest(
                _args(workspace, min_months=3, similarity=0.65, min_count=5)
            )

        assert "EUR, USD" in capsys.readouterr().out

    def test_auto_imports_and_then_asks_for_a_currency(
        self, workspace: Path, capsys, monkeypatch
    ):
        """An account in another currency imports with it; the report then
        needs a choice, and the import is kept."""
        db_path = workspace / "data" / "ledger.sqlite"
        conn = sqlite3.connect(str(db_path))
        set_account_currency(conn, "usd_account", "USD")
        conn.close()
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "usd_account")
        (workspace / "raw" / "us.csv").write_text(
            "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
            "03-02-2026;03-02-2026;COMPRA SHOP;25,00;;\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as excinfo:
            cli.cmd_auto(_args(workspace, no_backup=True, dry_run=False))

        assert excinfo.value.code == 1
        assert "EUR, USD" in capsys.readouterr().out
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT account, currency, amount_abs FROM transactions "
            "WHERE currency = 'USD'"
        ).fetchall()
        conn.close()
        assert rows == [("usd_account", "USD", 25.0)]

        cli.cmd_auto(_args(workspace, no_backup=True, dry_run=False, currency="USD"))

        assert _data_amounts(workspace / "expense-report.xlsx") == [25.0]

    def test_parser_has_the_option(self, workspace: Path, monkeypatch, capsys):
        _add_usd(workspace)
        monkeypatch.setattr(
            "sys.argv",
            ["bank_ingest.py", "export", "--currency", "USD", "--out", "u.csv"],
        )

        cli.main()

        assert len((workspace / "u.csv").read_text().splitlines()) == 1 + len(USD_ROWS)


def test_ingest_import_is_unaffected_by_report_currency(tmp_path, monkeypatch):
    """Sanity: ingest needs no currency choice on a multi-currency ledger."""
    db_path = _ledger(tmp_path / "l", with_usd=True)
    monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")
    statement = tmp_path / "jan.csv"
    statement.write_text(
        "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
        "20-03-2026;20-03-2026;COMPRA X;1,00;;\n",
        encoding="utf-8",
    )

    result = ingest(db_path, [statement], cards_path=tmp_path / "none.csv")

    assert result.inserted == 1
