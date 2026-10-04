"""Tests for expense_tracker.pdf_parser."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns
from expense_tracker.pdf_parser import (
    _parse_date,
    _parse_amount,
    _is_header_row,
    _find_column_mapping,
    _extract_text_date_range,
    parse_pdf_statement,
    extract_pdf_date_range,
)


# ---------------------------------------------------------------------------
# Synthetic PDF builder using fpdf2
# ---------------------------------------------------------------------------


def _make_pdf_statement(
    path: Path,
    rows: list[tuple[str, str, str, str, str, str]],
    date_from: str = "01-01-2026",
    date_to: str = "31-01-2026",
    header_labels: tuple[str, ...] = (
        "Data Lançamento", "Data Valor", "Descrição", "Montante", "Tipo", "Saldo",
    ),
) -> Path:
    """
    Create a synthetic PDF statement with a transaction table.

    Each row is (date_posted, date_value, description, amount, type, balance).
    """
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)

    # Header with date range
    pdf.cell(0, 10, f"Extrato de Conta - {date_from} a {date_to}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, "Conta: 123456789", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Transaction table
    col_widths = [28, 28, 60, 25, 20, 25]
    line_height = 7

    # Header row
    pdf.set_font("Helvetica", "B", 9)
    for i, label in enumerate(header_labels):
        pdf.cell(col_widths[i], line_height, label, border=1)
    pdf.ln()

    # Data rows
    pdf.set_font("Helvetica", size=9)
    for row in rows:
        for i, cell in enumerate(row):
            w = col_widths[i] if i < len(col_widths) else 25
            pdf.cell(w, line_height, cell, border=1)
        pdf.ln()

    pdf.output(str(path))
    return path


# Reusable sample rows matching conftest.py SAMPLE_ROWS
SAMPLE_PDF_ROWS = [
    ("15-01-2026", "15-01-2026", "COMPRA 1234 CONTINENTE PORTO", "-45,50", "Compra", "1234,56"),
    ("14-01-2026", "14-01-2026", "COMPRA 5678 FARMACIA DA GARE", "-12,80", "Compra", "1280,06"),
    ("13-01-2026", "13-01-2026", "DD VODAFONE PORTU", "-35,99", "Debito", "1292,86"),
    ("12-01-2026", "12-01-2026", "TRF. P/O EXEMPLO", "-150,00", "Transf.", "1328,85"),
    ("10-01-2026", "10-01-2026", "TRANSFERENCIA - SALARIO", "2500,00", "Credito", "1478,85"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pdf_statement(tmp_path):
    """A synthetic PDF statement with 5 sample transactions."""
    return _make_pdf_statement(
        tmp_path / "statement.pdf",
        SAMPLE_PDF_ROWS,
    )


@pytest.fixture
def cards_csv(tmp_path):
    p = tmp_path / "account-holders.csv"
    p.write_text("card_last4,name\n1234,Alice\n5678,Bob\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _setup_cleaning(tmp_path):
    noise = tmp_path / "noise-words.txt"
    noise.write_text("CONTACTLESS\nPT\nPORTO\n", encoding="utf-8")
    patterns = tmp_path / "cleaning-patterns.csv"
    patterns.write_text(
        'type,pattern,description\n'
        'prefix,COMPRA\\s+\\d{4}\\s*,Card purchase prefix\n'
        'prefix,DD\\s+,Direct debit prefix\n',
        encoding="utf-8",
    )
    reset_cleaning_cache()
    _get_cleaning_patterns(noise, patterns)
    yield
    reset_cleaning_cache()


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_dash_format(self):
        assert _parse_date("15-01-2026") == date(2026, 1, 15)

    def test_slash_format(self):
        assert _parse_date("15/01/2026") == date(2026, 1, 15)

    def test_dot_format(self):
        assert _parse_date("15.01.2026") == date(2026, 1, 15)

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


class TestParseAmount:
    def test_negative_comma_decimal(self):
        assert _parse_amount("-45,50") == -45.50

    def test_positive_comma_decimal(self):
        assert _parse_amount("2500,00") == 2500.00

    def test_thousands_separator(self):
        assert _parse_amount("1.234,56") == 1234.56

    @pytest.mark.parametrize(
        "text, expected",
        [("3.00", 3.00), ("-45.50", -45.50), ("1 529.13", 1529.13), ("1.234", 1234.0)],
    )
    def test_dot_decimal(self, text, expected):
        """PDFs print dot decimals and space thousands: '3.00' is not 300."""
        assert _parse_amount(text) == expected

    def test_empty_returns_none(self):
        assert _parse_amount("") is None

    def test_none_returns_none(self):
        assert _parse_amount(None) is None

    def test_invalid_returns_none(self):
        assert _parse_amount("abc") is None


class TestIsHeaderRow:
    def test_utf16_header(self):
        assert _is_header_row(
            ["Data Lançamento", "Data Valor", "Descrição", "Montante", "Tipo", "Saldo"]
        )

    def test_partial_header(self):
        assert _is_header_row(["Data", "Descrição", "Montante"])

    def test_non_header(self):
        assert not _is_header_row(["15-01-2026", "15-01-2026", "CONTINENTE"])

    def test_empty_row(self):
        assert not _is_header_row([])


class TestFindColumnMapping:
    def test_standard_utf16_headers(self):
        headers = ["Data Lançamento", "Data Valor", "Descrição", "Montante", "Tipo", "Saldo"]
        mapping = _find_column_mapping(headers)
        assert mapping["date_posted"] == 0
        assert mapping["date_value"] == 1
        assert mapping["description"] == 2
        assert mapping["amount"] == 3
        assert mapping["type"] == 4
        assert mapping["balance"] == 5

    def test_debit_credit_columns(self):
        headers = ["Data", "Descrição", "Débito", "Crédito", "Saldo"]
        mapping = _find_column_mapping(headers)
        assert "debit" in mapping
        assert "credit" in mapping


class TestExtractTextDateRange:
    def test_standard_range(self):
        text = "Extrato de Conta - 01-01-2026 a 31-01-2026\nConta: 123"
        d1, d2 = _extract_text_date_range(text)
        assert d1 == date(2026, 1, 1)
        assert d2 == date(2026, 1, 31)

    def test_range_with_ate(self):
        text = "Periodo: 01/02/2026 até 28/02/2026"
        d1, d2 = _extract_text_date_range(text)
        assert d1 == date(2026, 2, 1)
        assert d2 == date(2026, 2, 28)

    def test_no_range_returns_none(self):
        d1, d2 = _extract_text_date_range("No dates here")
        assert d1 is None
        assert d2 is None


# ---------------------------------------------------------------------------
# Integration tests: full PDF parsing
# ---------------------------------------------------------------------------


class TestParsePdfStatement:
    def test_parses_all_rows(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        assert len(rows) == 5

    def test_dates_parsed(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        assert rows[0]["date_posted"] == "2026-01-15"
        assert rows[0]["date_value"] == "2026-01-15"

    def test_amounts_parsed(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        amounts = {r["description_raw"]: r["amount_signed"] for r in rows}
        # CONTINENTE should be -45.50
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert continente["amount_signed"] == -45.50

    def test_income_direction(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        salary = [r for r in rows if "SALARIO" in r["description_raw"]][0]
        assert salary["direction"] == "in"
        assert salary["amount_signed"] == 2500.00

    def test_expense_direction(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        expense = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert expense["direction"] == "out"

    def test_payment_types_detected(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        types = {r["description_raw"]: r["payment_type"] for r in rows}
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert continente["payment_type"] == "card"
        dd = [r for r in rows if "VODAFONE" in r["description_raw"]][0]
        assert dd["payment_type"] == "direct_debit"

    def test_description_cleaned(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        # After cleaning, the card prefix and noise words should be stripped
        assert "COMPRA 1234" not in continente["description_clean"]
        assert "CONTINENTE" in continente["description_clean"]

    def test_source_file_set(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        assert all(r["source_file"] == "statement.pdf" for r in rows)

    def test_month_and_dow_populated(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        for r in rows:
            assert r["month"]
            assert r["day_of_week"]

    def test_card_detected(self, pdf_statement, cards_csv):
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert continente["card_last4"] == "1234"
        assert continente["who"] == "Alice"

    def test_empty_pdf_returns_empty(self, tmp_path, cards_csv):
        """A PDF with no transaction table should return empty list."""
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 10, "This is not a bank statement")
        path = tmp_path / "empty.pdf"
        pdf.output(str(path))
        rows = parse_pdf_statement(path, cards_path=cards_csv)
        assert rows == []


class TestExtractPdfDateRange:
    def test_extracts_from_text(self, pdf_statement):
        d1, d2 = extract_pdf_date_range(pdf_statement)
        assert d1 == date(2026, 1, 1)
        assert d2 == date(2026, 1, 31)

    def test_falls_back_to_transaction_dates(self, tmp_path, cards_csv):
        """When no explicit date range is in the text, use min/max transaction dates."""
        path = _make_pdf_statement(
            tmp_path / "no-range.pdf",
            SAMPLE_PDF_ROWS,
            date_from="",  # No date range in header
            date_to="",
        )
        # This should fall back to transaction dates
        d1, d2 = extract_pdf_date_range(path)
        assert d1 == date(2026, 1, 10)  # earliest transaction
        assert d2 == date(2026, 1, 15)  # latest transaction

    def test_empty_pdf_raises(self, tmp_path):
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 10, "Nothing here")
        path = tmp_path / "empty.pdf"
        pdf.output(str(path))
        with pytest.raises(ValueError, match="Could not find"):
            extract_pdf_date_range(path)


# ---------------------------------------------------------------------------
# Text-layout statements (the path real PDFs take)
# ---------------------------------------------------------------------------


def _text_rows(text: str, year: int = 2026, credit_patterns=None) -> list[dict]:
    from expense_tracker.pdf_parser import _parse_text_transactions

    return _parse_text_transactions(
        text, year, set(), {}, "statement.pdf", credit_patterns
    )


class TestTextCreditPatterns:
    def test_configured_credit_pattern_makes_amount_positive(self):
        rows = _text_rows(
            "2.03 2.03 VENDA OLX BICICLETA 50.00 1 050.00",
            credit_patterns=["VENDA OLX"],
        )
        assert [r["amount_signed"] for r in rows] == [50.00]
        assert rows[0]["direction"] == "in"

    def test_default_pattern_absent_from_config_is_not_credit(self):
        rows = _text_rows(
            "2.03 2.03 TRF. P/O EXEMPLO 84.00 966.00",
            credit_patterns=["VENCIMENTO"],
        )
        assert [r["amount_signed"] for r in rows] == [-84.00]

    def test_loaded_from_file_by_parse_pdf_statement(self, tmp_path):
        from expense_tracker.pdf_parser import load_credit_patterns

        path = tmp_path / "credit-patterns.csv"
        path.write_text("pattern\nVENDA OLX\n", encoding="utf-8")
        rows = _text_rows(
            "2.03 2.03 VENDA OLX BICICLETA 50.00 1 050.00",
            credit_patterns=load_credit_patterns(path),
        )
        assert rows[0]["amount_signed"] == 50.00


def _make_text_pdf(path: Path, lines: list[str]) -> Path:
    """A PDF with plain text lines and no table, like real statements."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    for ln in lines:
        pdf.cell(0, 6, ln, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


class TestTextStatementYear:
    """Text lines carry only month.day; the year comes from the period."""

    def _dates(self, tmp_path, header: str, lines: list[str]) -> list[tuple[str, str]]:
        path = _make_text_pdf(tmp_path / "statement.pdf", [header] + lines)
        rows = parse_pdf_statement(path, cards_path=tmp_path / "none.csv")
        return [(r["date_posted"], r["date_value"]) for r in rows]

    def test_statement_spanning_new_year(self, tmp_path):
        dates = self._dates(
            tmp_path,
            "EXTRATO DE 2025/12/29 A 2026/01/28",
            [
                "12.30 12.30 COMPRA CONTINENTE 10.00 990.00",
                "1.05 1.05 COMPRA PINGO DOCE 20.00 970.00",
            ],
        )
        assert dates == [
            ("2025-12-30", "2025-12-30"),
            ("2026-01-05", "2026-01-05"),
        ]

    def test_value_date_in_previous_year(self, tmp_path):
        dates = self._dates(
            tmp_path,
            "EXTRATO DE 2026/01/02 A 2026/01/30",
            ["1.02 12.31 COMPRA LIDL 5.00 965.00"],
        )
        assert dates == [("2026-01-02", "2025-12-31")]

    def test_single_month_statement_unchanged(self, tmp_path):
        dates = self._dates(
            tmp_path,
            "EXTRATO DE 2026/02/02 A 2026/02/27",
            ["2.03 2.03 COMPRA KIOSK 3.00 1 529.13"],
        )
        assert dates == [("2026-02-03", "2026-02-03")]

    def test_leap_day_in_statement_whose_midpoint_is_not_a_leap_year(self, tmp_path):
        """Nov 2023 to Feb 2024: the midpoint is in 2023, which has no 29 Feb."""
        dates = self._dates(
            tmp_path,
            "EXTRATO DE 2023/11/01 A 2024/02/29",
            [
                "11.02 11.02 COMPRA CONTINENTE 10.00 990.00",
                "2.29 2.29 COMPRA LIDL 5.00 985.00",
            ],
        )
        assert dates == [
            ("2023-11-02", "2023-11-02"),
            ("2024-02-29", "2024-02-29"),
        ]


class TestBalanceCheck:
    """The running balance says what each amount must be: a row that does
    not fit is reported, not left in a debug log."""

    def _warnings(self, caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]

    def _text_statement(self, tmp_path, lines: list[str]) -> Path:
        return _make_text_pdf(
            tmp_path / "statement.pdf", ["EXTRATO DE 2026/02/02 A 2026/02/27"] + lines
        )

    def test_amount_signed_against_the_balance_is_reported(self, tmp_path, caplog):
        """MB WAY money received: no credit keyword, so it is booked as spent."""
        path = self._text_statement(
            tmp_path,
            [
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.06 2.06 TRF MB WAY DE ALICE 20.00 1 549.13",
                "2.07 2.07 COMPRA CAFE 0.40 1 548.73",
            ],
        )
        rows = parse_pdf_statement(path, cards_path=tmp_path / "none.csv")

        assert [r["amount_signed"] for r in rows] == [-3.00, -20.00, -0.40]
        messages = self._warnings(caplog)
        assert len(messages) == 1
        assert "statement.pdf" in messages[0]
        assert "TRF MB WAY DE ALICE" in messages[0]
        assert "-20.00" in messages[0] and "+20.00" in messages[0]

    def test_wrong_sign_on_a_small_amount_is_reported(self, tmp_path, caplog):
        path = self._text_statement(
            tmp_path,
            [
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.06 2.06 JUROS CREDORES 0.40 1 529.53",
            ],
        )
        parse_pdf_statement(path, cards_path=tmp_path / "none.csv")

        assert len(self._warnings(caplog)) == 1

    def test_consistent_statement_reports_nothing(self, tmp_path, caplog):
        path = self._text_statement(
            tmp_path,
            [
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
                "2.03 2.03 TRANSFERENCIA - VENCIMENTO 1 000.00 2 529.13",
                "2.04 2.04 COMPRA CAFE 0.40 2 528.73",
            ],
        )
        parse_pdf_statement(path, cards_path=tmp_path / "none.csv")

        assert self._warnings(caplog) == []

    def test_newest_first_statement_reports_nothing(self, pdf_statement, cards_csv, caplog):
        """The table fixture lists the latest transaction first."""
        rows = parse_pdf_statement(pdf_statement, cards_path=cards_csv)

        assert len(rows) == 5
        assert self._warnings(caplog) == []

    def test_amount_that_does_not_explain_the_balance_is_reported(
        self, tmp_path, cards_csv, caplog
    ):
        rows = list(SAMPLE_PDF_ROWS)
        rows[1] = ("14-01-2026", "14-01-2026", "COMPRA 5678 FARMACIA DA GARE", "-1.280,00", "Compra", "1280,06")
        path = _make_pdf_statement(tmp_path / "statement.pdf", rows)
        parse_pdf_statement(path, cards_path=cards_csv)

        messages = self._warnings(caplog)
        assert len(messages) == 1
        assert "FARMACIA" in messages[0]
