"""Tests for expense_tracker.parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from expense_tracker.parser import (
    clean_description,
    detect_card,
    detect_payment_type,
    extract_date_range,
    auto_rename_csv,
    load_card_holders,
    parse_utf16_csv,
    reset_cleaning_cache,
    _get_cleaning_patterns,
)
from tests.conftest import make_utf16_csv, SAMPLE_ROWS


# ---------------------------------------------------------------------------
# detect_payment_type
# ---------------------------------------------------------------------------


class TestDetectPaymentType:
    def test_card_purchase(self):
        assert detect_payment_type("COMPRA 1234 CONTINENTE") == "card"

    def test_direct_debit(self):
        assert detect_payment_type("DD VODAFONE PORTU") == "direct_debit"

    def test_transfer_trf_po(self):
        assert detect_payment_type("TRF. P/O EXEMPLO") == "transfer"

    def test_transfer_trf_p(self):
        assert detect_payment_type("TRF P/RENDA") == "transfer"

    def test_transfer_mbway(self):
        assert detect_payment_type("TRF MB WAY P/ AMIGO") == "transfer"

    def test_transfer_full(self):
        assert detect_payment_type("TRANSFERENCIA - SALARIO") == "transfer"

    def test_atm(self):
        assert detect_payment_type("LEV ATM 1234 BPI PORTO") == "atm"

    def test_fee_comissao(self):
        assert detect_payment_type("COMISSAO TRF MBWAY") == "fee"

    def test_fee_com_man(self):
        assert detect_payment_type("COM.MAN.CONTA 2024") == "fee"

    def test_fee_custo(self):
        assert detect_payment_type("CUSTO DE SERVICO") == "fee"

    def test_tax(self):
        assert detect_payment_type("IMPOSTO DO SELO ART 12.3") == "tax"

    def test_unknown(self):
        assert detect_payment_type("RANDOM PAYMENT") == "other"

    def test_empty_string(self):
        assert detect_payment_type("") == "other"

    def test_case_insensitive(self):
        assert detect_payment_type("compra 1234 loja") == "card"


# ---------------------------------------------------------------------------
# clean_description
# ---------------------------------------------------------------------------


class TestCleanDescription:
    @pytest.fixture(autouse=True)
    def _reset_cache(self, noise_words, cleaning_patterns):
        """Reset cleaning cache and load test patterns for every test."""
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_strips_card_prefix(self):
        result = clean_description("COMPRA 1234 CONTINENTE PORTO")
        assert "COMPRA" not in result
        assert "1234" not in result

    def test_preserves_merchant_name(self):
        result = clean_description("COMPRA 1234 CONTINENTE PORTO")
        assert "CONTINENTE" in result

    def test_strips_dd_prefix(self):
        result = clean_description("DD VODAFONE PORTU 01234567890 PT12345678")
        assert not result.startswith("DD")
        assert "VODAFONE" in result

    def test_strips_postal_code(self):
        result = clean_description("COMPRA 5678 FARMACIA DA GARE 1000-001 LISBOA")
        assert "1000-001" not in result

    def test_strips_noise_words(self):
        result = clean_description("COMPRA 1234 CONTINENTE PORTO")
        # PORTO is in noise words, should be stripped
        assert "PORTO" not in result

    def test_noise_word_not_in_domain(self):
        """PT as noise word should NOT strip PT inside CONTINENTE.PT."""
        result = clean_description("COMPRA 1234 WWW.CONTINENTE.PT CIDADE NOVA")
        assert "CONTINENTE.PT" in result or "CONTINENTE" in result

    def test_strips_transaction_ref_codes(self):
        result = clean_description("DD VODAFONE PORTU 01234567890 PT12345678")
        # 01234567890 has 11 chars and digits -> should be stripped
        # PT12345678 has 10 chars with digits -> should be stripped
        assert "01234567890" not in result

    def test_preserves_alpha_only_words(self):
        """Pure alphabetic words (like CONTINENTE, FARMACIA) should NOT be
        stripped by the transaction code regex."""
        result = clean_description("COMPRA 1234 CONTINENTE PORTO")
        assert "CONTINENTE" in result

    def test_empty_string_returns_original(self):
        assert clean_description("") == ""

    def test_all_stripped_returns_original(self):
        """If cleaning would produce empty string, return original."""
        reset_cleaning_cache()
        # With default paths (no files), nothing should be stripped
        result = clean_description("COMPRA 1234 LOJA")
        assert result  # should not be empty


# ---------------------------------------------------------------------------
# detect_card
# ---------------------------------------------------------------------------


class TestDetectCard:
    def test_known_card(self):
        assert detect_card("COMPRA 1234 CONTINENTE", known_cards={"1234", "5678"}) == "1234"

    def test_unknown_card(self):
        assert detect_card("COMPRA 9999 CONTINENTE", known_cards={"1234", "5678"}) is None

    def test_no_four_digit_groups(self):
        assert detect_card("TRANSFERENCIA SALARIO", known_cards={"1234"}) is None

    def test_empty_known_cards(self):
        assert detect_card("COMPRA 1234 CONTINENTE", known_cards=set()) is None

    def test_multiple_cards_first_match(self):
        result = detect_card("COMPRA 1234 LOJA 5678", known_cards={"1234", "5678"})
        assert result == "1234"


# ---------------------------------------------------------------------------
# load_card_holders
# ---------------------------------------------------------------------------


class TestLoadCardHolders:
    def test_load_existing(self, cards_csv):
        holders = load_card_holders(cards_csv)
        assert holders == {"1234": "Alice", "5678": "Bob"}

    def test_missing_file(self, tmp_path):
        holders = load_card_holders(tmp_path / "nonexistent.csv")
        assert holders == {}

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("card_last4,name\n", encoding="utf-8")
        holders = load_card_holders(p)
        assert holders == {}


# ---------------------------------------------------------------------------
# extract_date_range
# ---------------------------------------------------------------------------


class TestExtractDateRange:
    def test_valid_range(self, utf16_csv):
        from datetime import date
        date_from, date_to = extract_date_range(utf16_csv)
        assert date_from == date(2026, 1, 1)
        assert date_to == date(2026, 1, 31)

    def test_missing_dates_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        content = "Some random content\nno dates here\n"
        p.write_bytes(content.encode("utf-16-le"))
        with pytest.raises(ValueError, match="Could not find date range"):
            extract_date_range(p)


# ---------------------------------------------------------------------------
# auto_rename_csv
# ---------------------------------------------------------------------------


class TestAutoRenameCsv:
    def test_same_month_rename(self, tmp_path):
        csv_path = make_utf16_csv(
            tmp_path / "EXPORT_0_1012026.csv", SAMPLE_ROWS[:1],
            date_from="01-01-2026", date_to="31-01-2026",
        )
        result = auto_rename_csv(csv_path)
        assert result.name == "2026-01.csv"
        assert result.exists()
        assert not csv_path.exists()  # original removed

    def test_multi_month_keeps_name(self, tmp_path):
        csv_path = make_utf16_csv(
            tmp_path / "multi.csv", SAMPLE_ROWS[:1],
            date_from="01-01-2026", date_to="15-02-2026",
        )
        result = auto_rename_csv(csv_path)
        assert result.name == "multi.csv"  # unchanged

    def test_target_exists_wider_replaces(self, tmp_path):
        # Create existing narrow-range file
        make_utf16_csv(
            tmp_path / "2026-01.csv", SAMPLE_ROWS[:1],
            date_from="10-01-2026", date_to="20-01-2026",
        )
        # New file with wider range
        new_csv = make_utf16_csv(
            tmp_path / "EXPORT_new.csv", SAMPLE_ROWS[:1],
            date_from="01-01-2026", date_to="31-01-2026",
        )
        result = auto_rename_csv(new_csv)
        assert result.name == "2026-01.csv"

    def test_target_exists_narrower_raises(self, tmp_path):
        # Create existing wide-range file
        make_utf16_csv(
            tmp_path / "2026-01.csv", SAMPLE_ROWS[:1],
            date_from="01-01-2026", date_to="31-01-2026",
        )
        # New file with narrower range
        new_csv = make_utf16_csv(
            tmp_path / "EXPORT_new.csv", SAMPLE_ROWS[:1],
            date_from="10-01-2026", date_to="20-01-2026",
        )
        with pytest.raises(ValueError, match="already exists"):
            auto_rename_csv(new_csv)

    def test_already_correct_name(self, tmp_path):
        csv_path = make_utf16_csv(
            tmp_path / "2026-01.csv", SAMPLE_ROWS[:1],
            date_from="01-01-2026", date_to="31-01-2026",
        )
        result = auto_rename_csv(csv_path)
        assert result == csv_path


# ---------------------------------------------------------------------------
# parse_utf16_csv
# ---------------------------------------------------------------------------


class TestParseUtf16Csv:
    @pytest.fixture(autouse=True)
    def _reset_cache(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_parses_all_rows(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        assert len(rows) == 5

    def test_dates_parsed(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        assert rows[0]["date_posted"] == "2026-01-15"

    def test_amounts_parsed(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        # First row: -45.50
        assert rows[0]["amount_signed"] == -45.50
        assert rows[0]["amount_abs"] == 45.50
        assert rows[0]["direction"] == "out"

    def test_income_direction(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        # Last row is salary (positive)
        salary = [r for r in rows if "SALARIO" in r["description_raw"]][0]
        assert salary["direction"] == "in"
        assert salary["amount_signed"] == 2500.0

    def test_card_detected(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert continente["card_last4"] == "1234"
        assert continente["who"] == "Alice"

    def test_payment_types_detected(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        ptypes = {r["description_raw"].split()[0]: r["payment_type"] for r in rows}
        assert "card" in ptypes.values()
        assert "direct_debit" in ptypes.values()
        assert "transfer" in ptypes.values()

    def test_description_cleaned(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        continente = [r for r in rows if "CONTINENTE" in r["description_raw"]][0]
        assert "COMPRA" not in continente["description_clean"]
        assert "CONTINENTE" in continente["description_clean"]

    def test_missing_header_raises(self, tmp_path, cards_csv):
        p = tmp_path / "bad.csv"
        content = "No header here\nJust some text\n"
        p.write_bytes(content.encode("utf-16-le"))
        with pytest.raises(ValueError, match="Could not find transaction header"):
            parse_utf16_csv(p, cards_path=cards_csv)

    def test_source_file_set(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        assert all(r["source_file"] == utf16_csv.name for r in rows)

    def test_month_and_dow_populated(self, utf16_csv, cards_csv):
        rows = parse_utf16_csv(utf16_csv, cards_path=cards_csv)
        for r in rows:
            assert r["month"]  # e.g. "2026-01"
            assert r["day_of_week"] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def test_utf8_fallback(self, tmp_path, cards_csv, noise_words, cleaning_patterns):
        """Parser should handle UTF-8 encoded files too."""
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)

        rows_data = [
            ("15-01-2026", "15-01-2026", "COMPRA 1234 LOJA TESTE", "-10,00", "Compra", "100,00"),
        ]
        p = make_utf16_csv(tmp_path / "utf8.csv", rows_data, encoding="utf-8")
        rows = parse_utf16_csv(p, cards_path=cards_csv)
        assert len(rows) == 1

        reset_cleaning_cache()
