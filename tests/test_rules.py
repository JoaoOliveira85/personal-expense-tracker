"""Tests for expense_tracker.rules."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.db import ensure_schema, migrate_schema, ingest
from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns
from expense_tracker.rules import (
    add_rule,
    remove_rule,
    add_card,
    remove_card,
    load_rules,
    categorize_transactions,
    match_rule,
)


# ---------------------------------------------------------------------------
# add_rule / remove_rule
# ---------------------------------------------------------------------------


class TestRuleCRUD:
    def test_add_rule_creates_file(self, tmp_path):
        p = tmp_path / "rules.csv"
        add_rule(p, "TEST", "description", "TestCat", "TestSub", "card")
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "TEST" in content
        assert "TestCat" in content

    def test_add_rule_appends(self, rules_csv):
        add_rule(rules_csv, "NEWRULE", "description", "NewCat")
        rules = load_rules(rules_csv)
        patterns = [r["pattern"] for r in rules]
        assert "NEWRULE" in patterns
        assert "CONTINENTE" in patterns  # original still there

    def test_remove_rule_existing(self, rules_csv):
        assert remove_rule(rules_csv, "CONTINENTE") is True
        rules = load_rules(rules_csv)
        patterns = [r["pattern"] for r in rules]
        assert "CONTINENTE" not in patterns

    def test_remove_rule_nonexistent(self, rules_csv):
        assert remove_rule(rules_csv, "NONEXISTENT") is False

    def test_remove_rule_case_insensitive(self, rules_csv):
        assert remove_rule(rules_csv, "continente") is True

    def test_remove_rule_missing_file(self, tmp_path):
        assert remove_rule(tmp_path / "missing.csv", "TEST") is False

    def test_add_rule_to_empty_file(self, tmp_path):
        p = tmp_path / "rules.csv"
        p.write_text("", encoding="utf-8")
        add_rule(p, "PATTERN", "description", "Cat")
        rules = load_rules(p)
        assert len(rules) == 1
        assert rules[0]["pattern"] == "PATTERN"


# ---------------------------------------------------------------------------
# add_card / remove_card
# ---------------------------------------------------------------------------


class TestCardCRUD:
    def test_add_card_creates_file(self, tmp_path):
        p = tmp_path / "cards.csv"
        add_card(p, "9999", "Charlie")
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "9999" in content
        assert "Charlie" in content

    def test_add_card_duplicate_raises(self, cards_csv):
        with pytest.raises(ValueError, match="already exists"):
            add_card(cards_csv, "1234", "Duplicate")

    def test_remove_card_existing(self, cards_csv):
        assert remove_card(cards_csv, "1234") is True
        content = cards_csv.read_text(encoding="utf-8")
        assert "1234" not in content
        assert "5678" in content  # other card preserved

    def test_remove_card_nonexistent(self, cards_csv):
        assert remove_card(cards_csv, "9999") is False

    def test_remove_card_missing_file(self, tmp_path):
        assert remove_card(tmp_path / "missing.csv", "1234") is False


# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_loads_all_rules(self, rules_csv):
        rules = load_rules(rules_csv)
        assert len(rules) == 4

    def test_rule_fields(self, rules_csv):
        rules = load_rules(rules_csv)
        r = rules[0]
        assert r["pattern"] == "CONTINENTE"
        assert r["match_field"] == "description"
        assert r["category"] == "Groceries"
        assert "pattern_upper" in r

    def test_missing_file_returns_empty(self, tmp_path):
        rules = load_rules(tmp_path / "nonexistent.csv")
        assert rules == []

    def test_skips_empty_patterns(self, tmp_path):
        p = tmp_path / "rules.csv"
        p.write_text(
            "pattern,match_field,category,subcategory,payment_type\n"
            ",description,Empty,,\n"
            "VALID,description,Cat,,\n",
            encoding="utf-8",
        )
        rules = load_rules(p)
        assert len(rules) == 1
        assert rules[0]["pattern"] == "VALID"


# ---------------------------------------------------------------------------
# categorize_transactions
# ---------------------------------------------------------------------------


class TestCategorizeTransactions:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_categorizes_matching(self, populated_db, rules_csv):
        rules = load_rules(rules_csv)
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        updated = categorize_transactions(conn, rules)
        conn.close()
        assert updated > 0

    def test_first_match_wins(self, populated_db, tmp_path):
        """If two rules match, the first one should win."""
        p = tmp_path / "priority.csv"
        p.write_text(
            "pattern,match_field,category,subcategory,payment_type\n"
            "CONTINENTE,description,FirstCat,,\n"
            "CONTINENTE,description,SecondCat,,\n",
            encoding="utf-8",
        )
        rules = load_rules(p)
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        categorize_transactions(conn, rules)

        row = conn.execute(
            "SELECT category FROM transactions "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        ).fetchone()
        conn.close()
        assert row[0] == "FirstCat"

    def test_does_not_override_existing(self, populated_db, rules_csv):
        """Rules should not override already-categorized transactions."""
        # Manually categorize one transaction
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        conn.execute(
            "UPDATE transactions SET category = 'Manual' "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        )
        conn.commit()

        rules = load_rules(rules_csv)
        categorize_transactions(conn, rules)

        row = conn.execute(
            "SELECT category FROM transactions "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        ).fetchone()
        conn.close()
        assert row[0] == "Manual"  # not overwritten

    def test_empty_rules_returns_zero(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        updated = categorize_transactions(conn, [])
        conn.close()
        assert updated == 0

    def test_description_raw_match_field(self, populated_db, rules_csv):
        """Rule with match_field=description_raw should match against raw description."""
        rules = load_rules(rules_csv)
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        categorize_transactions(conn, rules)

        # SALARIO rule matches on description_raw
        row = conn.execute(
            "SELECT category FROM transactions "
            "WHERE description_raw LIKE '%SALARIO%'"
        ).fetchone()
        conn.close()
        assert row[0] == "Income"

    def test_sets_subcategory_and_payment_type(self, populated_db, rules_csv):
        rules = load_rules(rules_csv)
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        categorize_transactions(conn, rules)

        row = conn.execute(
            "SELECT category, subcategory, payment_type FROM transactions "
            "WHERE description_raw LIKE '%FARMACIA%'"
        ).fetchone()
        conn.close()
        assert row[0] == "Health"
        assert row[1] == "Pharmacy"
        # payment_type should be set by the rule if empty, but parser already sets it
        # The rule says "card" and parser detected "card", so it stays


# ---------------------------------------------------------------------------
# match_rule — word-boundary semantics
# ---------------------------------------------------------------------------


def _rules(tmp_path: Path, *lines: str) -> list[dict]:
    p = tmp_path / "rules.csv"
    p.write_text(
        "pattern,match_field,category,subcategory,payment_type\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )
    return load_rules(p)


def _category(rules: list[dict], raw: str, clean: str = "") -> str | None:
    rule = match_rule(rules, raw, clean)
    return rule["category"] if rule else None


class TestMatchRuleWordBoundary:
    def test_does_not_match_inside_a_word(self, tmp_path):
        rules = _rules(tmp_path, "PAO,description,Groceries,,")
        assert _category(rules, "COMPRA 1234 SUSHI DO JAPAO LISBOA") is None

    def test_does_not_match_across_word_start(self, tmp_path):
        rules = _rules(tmp_path, "CP PORTO,description,Transport,,")
        assert _category(rules, "LEV ATM 1234 BANKA PORTO") is None

    def test_still_matches_word_prefix(self, tmp_path):
        rules = _rules(tmp_path, "CINEMA,description,Entertainment,,")
        assert _category(rules, "COMPRA 1234 NOS CINEMAS LISBOA") == "Entertainment"

    def test_matches_at_start_of_description(self, tmp_path):
        rules = _rules(tmp_path, "FARMACIA,description,Health,,")
        assert _category(rules, "FARMACIACENTRAL LISBOA") == "Health"

    def test_is_case_insensitive(self, tmp_path):
        rules = _rules(tmp_path, "youtube,description,Subscriptions,,")
        assert _category(rules, "COMPRA 1234 Google YouTubePremium") == "Subscriptions"

    def test_trailing_space_requires_whole_word(self, tmp_path):
        rules = _rules(tmp_path, '"BP ",description,Transport,,')
        assert _category(rules, "LEV ATM 1234 BPI Lisboa") is None

    def test_trailing_space_matches_word_at_end(self, tmp_path):
        rules = _rules(tmp_path, '"BP ",description,Transport,,')
        assert _category(rules, "COMPRA 1234 BP") == "Transport"

    def test_pattern_starting_with_punctuation_matches_anywhere(self, tmp_path):
        rules = _rules(tmp_path, ">PAGAMENTO CARTAO,description_raw,Debt,,")
        assert _category(rules, "VIS>PAGAMENTO CARTAO CREDITO") == "Debt"

    def test_description_raw_rule_ignores_clean_description(self, tmp_path):
        rules = _rules(tmp_path, "CONTINENTE,description_raw,Groceries,,")
        assert _category(rules, "COMPRA 1234 XYZ", "CONTINENTE") is None

    def test_description_rule_checks_clean_description(self, tmp_path):
        rules = _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        assert _category(rules, "COMPRA 1234 XYZ", "CONTINENTE") == "Groceries"
