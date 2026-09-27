"""Tests for expense_tracker.starter_rules."""
from __future__ import annotations

from pathlib import Path

import pytest

from expense_tracker.starter_rules import (
    STARTER_RULES,
    get_starter_rules,
    import_starter_rules,
)
from expense_tracker.rules import load_rules, add_rule, match_rule


# ---------------------------------------------------------------------------
# Starter rules data integrity
# ---------------------------------------------------------------------------


class TestStarterRulesData:
    def test_all_rules_have_five_fields(self):
        for i, rule in enumerate(STARTER_RULES):
            assert len(rule) == 5, f"Rule {i} has {len(rule)} fields, expected 5: {rule}"

    def test_all_patterns_non_empty(self):
        for i, (pattern, *_rest) in enumerate(STARTER_RULES):
            assert pattern.strip(), f"Rule {i} has empty pattern"

    def test_all_match_fields_valid(self):
        valid = {"description", "description_raw"}
        for i, (_pattern, match_field, *_rest) in enumerate(STARTER_RULES):
            assert match_field in valid, (
                f"Rule {i} has invalid match_field '{match_field}'"
            )

    def test_all_categories_non_empty(self):
        for i, (_pattern, _field, category, *_rest) in enumerate(STARTER_RULES):
            assert category.strip(), f"Rule {i} has empty category"

    def test_no_duplicate_patterns(self):
        patterns = [p.upper() for p, *_ in STARTER_RULES]
        seen = set()
        for p in patterns:
            assert p not in seen, f"Duplicate pattern: {p}"
            seen.add(p)

    def test_get_starter_rules_returns_dicts(self):
        rules = get_starter_rules()
        assert len(rules) == len(STARTER_RULES)
        for r in rules:
            assert "pattern" in r
            assert "match_field" in r
            assert "category" in r
            assert "subcategory" in r
            assert "payment_type" in r

    def test_has_key_portuguese_categories(self):
        """Verify the starter pack covers essential Portuguese categories."""
        categories = {r[2] for r in STARTER_RULES}
        for expected in ["Groceries", "Utilities", "Transport", "Health"]:
            assert expected in categories, f"Missing category: {expected}"


# ---------------------------------------------------------------------------
# Import logic
# ---------------------------------------------------------------------------


class TestImportStarterRules:
    def test_import_into_empty_file(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        added, skipped = import_starter_rules(rules_path)
        assert added == len(STARTER_RULES)
        assert skipped == 0
        # Verify the file is valid and loadable
        rules = load_rules(rules_path)
        assert len(rules) == len(STARTER_RULES)

    def test_import_skips_existing_patterns(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        # Pre-add a rule that matches a starter pattern
        add_rule(rules_path, "CONTINENTE", "description", "MyGroceries")
        added, skipped = import_starter_rules(rules_path)
        assert skipped >= 1
        assert added == len(STARTER_RULES) - skipped
        # The original rule should still be there, not overwritten
        rules = load_rules(rules_path)
        continente_rules = [r for r in rules if r["pattern"] == "CONTINENTE"]
        assert len(continente_rules) == 1
        assert continente_rules[0]["category"] == "MyGroceries"

    def test_import_case_insensitive_skip(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        add_rule(rules_path, "continente", "description", "MyGroceries")
        added, skipped = import_starter_rules(rules_path)
        assert skipped >= 1

    def test_import_idempotent(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        added1, _ = import_starter_rules(rules_path)
        assert added1 == len(STARTER_RULES)
        # Import again — should add nothing
        added2, skipped2 = import_starter_rules(rules_path)
        assert added2 == 0
        assert skipped2 == len(STARTER_RULES)

    def test_dry_run_does_not_modify(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        added, skipped = import_starter_rules(rules_path, dry_run=True)
        assert added == len(STARTER_RULES)
        assert not rules_path.exists()

    def test_dry_run_with_existing(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        add_rule(rules_path, "CONTINENTE", "description", "MyGroceries")
        original_content = rules_path.read_text(encoding="utf-8")
        added, skipped = import_starter_rules(rules_path, dry_run=True)
        assert added == len(STARTER_RULES) - skipped
        # File should be unchanged
        assert rules_path.read_text(encoding="utf-8") == original_content

    def test_preserves_existing_rules_order(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        add_rule(rules_path, "MY_CUSTOM", "description", "Custom")
        add_rule(rules_path, "ANOTHER", "description_raw", "Other")
        import_starter_rules(rules_path)
        rules = load_rules(rules_path)
        # Original rules should come first
        assert rules[0]["pattern"] == "MY_CUSTOM"
        assert rules[1]["pattern"] == "ANOTHER"


# ---------------------------------------------------------------------------
# Starter rules applied end-to-end (CSV round trip -> load_rules -> match_rule)
# ---------------------------------------------------------------------------


class TestStarterRulesMatching:
    @pytest.fixture
    def rules(self, tmp_path):
        p = tmp_path / "rules.csv"
        import_starter_rules(p)
        return load_rules(p)

    @pytest.mark.parametrize(
        "description, expected",
        [
            ("COMISSAO LEVANTAMENTO NUMERARIO DEBITO", "Bank Fees"),
            ("LEV ATM 1234 BPI Lisboa", "Cash"),
            ("COMPRA 1234 UBER EATS LISBOA", "Eating Out"),
            ("COMPRA 1234 BOLT FOOD LISBOA", "Eating Out"),
            ("COMPRA 1234 BP LISBOA", "Transport"),
            ("COMPRA 1234 SUSHI DO JAPAO LISBOA", None),
        ],
    )
    def test_categorizes_real_world_descriptions(self, rules, description, expected):
        rule = match_rule(rules, description, "")
        assert (rule["category"] if rule else None) == expected
