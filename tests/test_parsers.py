"""Tests for the multi-bank parser framework."""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import pytest

from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns
from expense_tracker.parsers import (
    detect_parser,
    parse_statement,
    get_registered_parsers,
    get_parser_by_id,
)
from expense_tracker.parsers.utf16_csv import Utf16CsvParser
from expense_tracker.parsers.utf8_csv import Utf8CsvParser

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_cleaning(tmp_path):
    noise = tmp_path / "noise-words.txt"
    noise.write_text("CONTACTLESS\nPT\nPORTO\n", encoding="utf-8")
    patterns = tmp_path / "cleaning-patterns.csv"
    patterns.write_text(
        "type,pattern,description\n"
        "prefix,COMPRA\\s+\\d{4}\\s*,Card purchase prefix\n"
        "prefix,DD\\s+,Direct debit prefix\n",
        encoding="utf-8",
    )
    reset_cleaning_cache()
    _get_cleaning_patterns(noise, patterns)
    yield
    reset_cleaning_cache()


@pytest.fixture
def cards_csv(tmp_path):
    p = tmp_path / "account-holders.csv"
    p.write_text("card_last4,name\n1234,Alice\n5678,Bob\n", encoding="utf-8")
    return p


def _make_utf16_csv(path: Path) -> Path:
    """Create a minimal UTF-16 CSV."""
    lines = [
        "Data de:;01-01-2026",
        "Data até:;31-01-2026",
        "Conta:;123456789",
        "",
        "Data lançamento;Data valor;Descrição;Montante;Tipo;Saldo",
        "15-01-2026;15-01-2026;COMPRA 1234 CONTINENTE PORTO;-45,50;Compra;1234,56",
        "10-01-2026;10-01-2026;TRANSFERENCIA - SALARIO;2500,00;Crédito;1478,85",
        "",
    ]
    content = "\n".join(lines)
    path.write_bytes(content.encode("utf-16-le"))
    return path


def _make_utf8_csv(path: Path) -> Path:
    """Create a minimal UTF8 CSV."""
    lines = [
        "Período: 01-01-2026 a 31-01-2026",
        "Conta: 987654321",
        "",
        "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico",
        "15-01-2026;15-01-2026;COMPRA 1234 CONTINENTE PORTO;45,50;;1234,56",
        "10-01-2026;10-01-2026;TRANSFERENCIA - SALARIO;;2500,00;3734,56",
        "",
    ]
    content = "\n".join(lines)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_parsers_registered(self):
        parsers = get_registered_parsers()
        assert len(parsers) >= 2
        ids = {p.bank_id for p in parsers}
        assert "utf16" in ids
        assert "utf8" in ids

    def test_get_parser_by_id_utf16(self):
        parser = get_parser_by_id("utf16")
        assert parser is not None
        assert parser.name == "UTF-16 CSV"

    def test_get_parser_by_id_utf8(self):
        parser = get_parser_by_id("utf8")
        assert parser is not None
        assert parser.name == "UTF-8 CSV"

    def test_get_parser_by_id_unknown(self):
        assert get_parser_by_id("nonexistent") is None


# ---------------------------------------------------------------------------
# Auto-detection tests
# ---------------------------------------------------------------------------


class TestAutoDetection:
    def test_detects_utf16(self, tmp_path):
        path = _make_utf16_csv(tmp_path / "utf16_test.csv")
        parser = detect_parser(path)
        assert parser is not None
        assert parser.bank_id == "utf16"

    def test_detects_utf8(self, tmp_path):
        path = _make_utf8_csv(tmp_path / "utf8_test.csv")
        parser = detect_parser(path)
        assert parser is not None
        assert parser.bank_id == "utf8"

    def test_returns_none_for_unknown(self, tmp_path):
        path = tmp_path / "unknown.csv"
        path.write_text("random,data,file\n1,2,3\n", encoding="utf-8")
        parser = detect_parser(path)
        assert parser is None

    def test_returns_none_for_non_csv(self, tmp_path):
        path = tmp_path / "test.txt"
        path.write_text("hello world", encoding="utf-8")
        parser = detect_parser(path)
        assert parser is None


# ---------------------------------------------------------------------------
# UTF-16 CSV parser (via registry)
# ---------------------------------------------------------------------------


class TestUtf16Parser:
    def test_can_parse_utf16(self, tmp_path):
        path = _make_utf16_csv(tmp_path / "test.csv")
        parser = Utf16CsvParser()
        assert parser.can_parse(path)

    def test_cannot_parse_utf8(self, tmp_path):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf16CsvParser()
        assert not parser.can_parse(path)

    def test_parse_returns_transactions(self, tmp_path, cards_csv):
        path = _make_utf16_csv(tmp_path / "test.csv")
        parser = Utf16CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        assert len(rows) == 2

    def test_extract_date_range(self, tmp_path):
        path = _make_utf16_csv(tmp_path / "test.csv")
        parser = Utf16CsvParser()
        d1, d2 = parser.extract_date_range(path)
        assert d1 == date(2026, 1, 1)
        assert d2 == date(2026, 1, 31)

    def test_properties(self):
        parser = Utf16CsvParser()
        assert parser.name == "UTF-16 CSV"
        assert parser.bank_id == "utf16"


# ---------------------------------------------------------------------------
# UTF8 parser
# ---------------------------------------------------------------------------


class TestUtf8CsvParser:
    def test_can_parse_utf8(self, tmp_path):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        assert parser.can_parse(path)

    def test_cannot_parse_utf16(self, tmp_path):
        path = _make_utf16_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        assert not parser.can_parse(path)

    def test_parse_returns_transactions(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        assert len(rows) == 2

    def test_expense_parsed(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        expense = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert expense["amount_signed"] == -45.50
        assert expense["direction"] == "out"
        assert expense["amount_abs"] == 45.50

    def test_income_parsed(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        income = [r for r in rows if "SALARIO" in r["description_raw"]][0]
        assert income["amount_signed"] == 2500.00
        assert income["direction"] == "in"

    def test_dates_parsed(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        assert rows[0]["date_posted"] == "2026-01-15"

    def test_description_cleaned(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert "CONTINENTE" in continente["description_clean"]

    def test_extract_date_range(self, tmp_path):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        d1, d2 = parser.extract_date_range(path)
        assert d1 == date(2026, 1, 1)
        assert d2 == date(2026, 1, 31)

    def test_source_file_set(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        assert all(r["source_file"] == "test.csv" for r in rows)

    def test_standard_fields_present(self, tmp_path, cards_csv):
        """All standard transaction dict fields should be present."""
        path = _make_utf8_csv(tmp_path / "test.csv")
        parser = Utf8CsvParser()
        rows = parser.parse(path, cards_path=cards_csv)
        expected_keys = {
            "date_posted",
            "date_value",
            "month",
            "day_of_week",
            "description_raw",
            "description_clean",
            "amount_signed",
            "amount_abs",
            "direction",
            "tx_type",
            "balance",
            "currency",
            "account",
            "card_last4",
            "payment_type",
            "who",
            "source_file",
        }
        for r in rows:
            assert expected_keys.issubset(
                r.keys()
            ), f"Missing keys: {expected_keys - r.keys()}"

    def test_properties(self):
        parser = Utf8CsvParser()
        assert parser.name == "UTF-8 CSV"
        assert parser.bank_id == "utf8"


# ---------------------------------------------------------------------------
# parse_statement() integration
# ---------------------------------------------------------------------------


class TestParseStatement:
    def test_auto_detect_utf16(self, tmp_path, cards_csv):
        path = _make_utf16_csv(tmp_path / "test.csv")
        rows = parse_statement(path, cards_path=cards_csv)
        assert len(rows) == 2

    def test_auto_detect_utf8(self, tmp_path, cards_csv):
        path = _make_utf8_csv(tmp_path / "test.csv")
        rows = parse_statement(path, cards_path=cards_csv)
        assert len(rows) == 2

    def test_explicit_bank_id(self, tmp_path, cards_csv):
        path = _make_utf16_csv(tmp_path / "test.csv")
        rows = parse_statement(path, cards_path=cards_csv, bank_id="utf16")
        assert len(rows) == 2

    def test_unknown_bank_id_raises(self, tmp_path, cards_csv):
        path = _make_utf16_csv(tmp_path / "test.csv")
        with pytest.raises(ValueError, match="Unknown bank"):
            parse_statement(path, cards_path=cards_csv, bank_id="nonexistent")

    def test_undetectable_file_raises(self, tmp_path, cards_csv):
        path = tmp_path / "random.csv"
        path.write_text("not,a,bank,statement\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Could not detect"):
            parse_statement(path, cards_path=cards_csv)


class TestUtf8Amounts:
    @pytest.mark.parametrize(
        "debit, balance, expected_amount, expected_balance",
        [
            ("1.234,56", "10.000,00", -1234.56, 10000.00),
            ("45.50", "1234.56", -45.50, 1234.56),
        ],
    )
    def test_amount_formats(
        self, tmp_path, debit, balance, expected_amount, expected_balance
    ):
        path = tmp_path / "utf8.csv"
        path.write_text(
            "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
            f"15-01-2026;15-01-2026;COMPRA CONTINENTE;{debit};;{balance}\n",
            encoding="utf-8",
        )
        rows = Utf8CsvParser().parse(path, cards_path=tmp_path / "none.csv")
        assert [(r["amount_signed"], r["balance"]) for r in rows] == [
            (expected_amount, expected_balance)
        ]


class TestUtf8SkippedRows:
    """A row that cannot be imported must be reported, never dropped quietly."""

    HEADER = "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
    GOOD = "15-01-2026;15-01-2026;COMPRA CONTINENTE;45,50;;954,50\n"

    def _parse(self, tmp_path, body: str) -> list[dict]:
        path = tmp_path / "utf8.csv"
        path.write_text(self.HEADER + body, encoding="utf-8")
        return Utf8CsvParser().parse(path, cards_path=tmp_path / "none.csv")

    @pytest.mark.parametrize(
        "debit, reason",
        [("12,50 D", "unreadable amount"), ("0,00", "zero amount"), ("", "no amount")],
    )
    def test_row_without_a_usable_amount_is_reported(
        self, tmp_path, caplog, debit, reason
    ):
        with caplog.at_level(logging.WARNING, logger="expense_tracker"):
            rows = self._parse(
                tmp_path,
                self.GOOD + f"16-01-2026;16-01-2026;COMPRA LIDL;{debit};;942,00\n",
            )

        assert [r["description_raw"] for r in rows] == ["COMPRA CONTINENTE"]
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "utf8.csv line 3" in messages[0]
        assert reason in messages[0]
        assert "COMPRA LIDL" in messages[0]

    def test_row_with_an_invalid_value_date_is_reported(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="expense_tracker"):
            rows = self._parse(
                tmp_path,
                self.GOOD + "16-01-2026;31-02-2026;COMPRA LIDL;12,50;;942,00\n",
            )

        assert len(rows) == 1
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "utf8.csv line 3" in messages[0]
        assert "invalid value date" in messages[0]

    def test_rows_after_a_non_transaction_line_are_reported(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="expense_tracker"):
            rows = self._parse(
                tmp_path,
                self.GOOD
                + "continuation of the description above\n"
                + "16-01-2026;16-01-2026;COMPRA LIDL;12,50;;942,00\n",
            )

        assert len(rows) == 1
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1
        assert "utf8.csv line 3" in messages[0]
        assert "1 later line" in messages[0]

    def test_clean_statement_reports_nothing(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="expense_tracker"):
            rows = self._parse(
                tmp_path, self.GOOD + ";;;;;Saldo contabilístico;954,50 EUR\n"
            )

        assert len(rows) == 1
        assert caplog.records == []
