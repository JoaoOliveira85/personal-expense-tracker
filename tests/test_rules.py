"""Tests for expense_tracker.rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.db import migrate_schema
from expense_tracker.parser import _get_cleaning_patterns, reset_cleaning_cache
from expense_tracker.rules import (
    BUILTIN_CATEGORIES,
    CategoryError,
    DuplicateRuleError,
    RulesFileError,
    add_card,
    add_category,
    add_rule,
    categories_file,
    categorize_transactions,
    fold,
    load_categories,
    load_rules,
    match_rule,
    remove_card,
    remove_category,
    remove_rule,
    short_pattern_warning,
)
from tests.conftest import allow_categories as _allow

# ---------------------------------------------------------------------------
# add_rule / remove_rule
# ---------------------------------------------------------------------------


class TestRuleCRUD:
    def test_add_rule_creates_file(self, tmp_path):
        p = tmp_path / "rules.csv"
        _allow(p, "TestCat")
        add_rule(p, "TEST", "description", "TestCat", "TestSub", "card")
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "TEST" in content
        assert "TestCat" in content

    def test_add_rule_appends(self, rules_csv):
        _allow(rules_csv, "NewCat")
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

    def test_remove_rule_ignores_accents(self, tmp_path):
        p = tmp_path / "rules.csv"
        _allow(p, "Eating Out")
        add_rule(p, "Café", "description", "Eating Out")
        assert remove_rule(p, "CAFE") is True
        assert load_rules(p) == []

    def test_remove_rule_missing_file(self, tmp_path):
        assert remove_rule(tmp_path / "missing.csv", "TEST") is False

    @pytest.mark.parametrize("duplicate", ["CAFE", "café", " Cafe ", "CAFÉ"])
    def test_add_rule_refuses_same_pattern_ignoring_case_and_accents(
        self, tmp_path, duplicate
    ):
        p = tmp_path / "rules.csv"
        _allow(p, "Eating Out", "Groceries")
        add_rule(p, "Café", "description", "Eating Out")
        before = p.read_text(encoding="utf-8")

        with pytest.raises(DuplicateRuleError, match="Café"):
            add_rule(p, duplicate, "description", "Groceries")

        assert p.read_text(encoding="utf-8") == before

    def test_add_rule_accepts_a_different_pattern(self, tmp_path):
        p = tmp_path / "rules.csv"
        _allow(p, "Eating Out")
        add_rule(p, "Café", "description", "Eating Out")
        add_rule(p, "CAFETERIA", "description", "Eating Out")
        assert [r["pattern"] for r in load_rules(p)] == ["Café", "CAFETERIA"]

    def test_add_rule_to_empty_file(self, tmp_path):
        p = tmp_path / "rules.csv"
        p.write_text("", encoding="utf-8")
        _allow(p, "Cat")
        add_rule(p, "PATTERN", "description", "Cat")
        rules = load_rules(p)
        assert len(rules) == 1
        assert rules[0]["pattern"] == "PATTERN"


# ---------------------------------------------------------------------------
# The category list (issue #5)
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path, *categories: str | None) -> Path:
    from tests.conftest import add_transaction

    db = tmp_path / "ledger.sqlite"
    for i, category in enumerate(categories):
        add_transaction(db, f"2026-01-{i + 1:02d}", f"SHOP {i}", 10, "out", category)
    return db


class TestCategoryList:
    def test_lives_next_to_the_rules_file(self, tmp_path):
        assert categories_file(tmp_path / "rules.csv") == tmp_path / "categories.csv"

    def test_builtins(self):
        assert set(BUILTIN_CATEGORIES) == {
            "Income",
            "Transfers",
            "Savings",
            "Ignored",
            "Miscellaneous",
        }

    def test_fresh_list_holds_the_builtins(self, tmp_path):
        path = tmp_path / "categories.csv"
        assert load_categories(path) == sorted(BUILTIN_CATEGORIES)
        assert path.exists()

    def test_seeded_from_rules_and_ledger(self, tmp_path, rules_csv):
        db = _ledger(tmp_path, "Household", "groceries ", None, "")
        path = categories_file(rules_csv)

        categories = load_categories(path, rules_csv, db)

        # rules_csv: Groceries, Health, Utilities, Income; the ledger's
        # "groceries " is the same category as the rules' Groceries
        assert categories == sorted(
            set(BUILTIN_CATEGORIES) | {"Groceries", "Health", "Utilities", "Household"}
        )
        assert load_categories(path) == categories  # stored

    def test_seed_skips_a_missing_ledger_without_creating_it(self, tmp_path):
        db = tmp_path / "missing.sqlite"
        load_categories(tmp_path / "categories.csv", tmp_path / "rules.csv", db)
        assert not db.exists()

    def test_stored_list_is_not_reseeded(self, tmp_path, rules_csv):
        path = categories_file(rules_csv)
        path.write_text("Pets\n", encoding="utf-8")
        assert load_categories(path, rules_csv) == sorted(
            set(BUILTIN_CATEGORIES) | {"Pets"}
        )

    def test_add_keeps_the_stored_spelling(self, tmp_path):
        path = tmp_path / "categories.csv"
        assert add_category(path, "  Eating Out ") == "Eating Out"
        assert "Eating Out" in load_categories(path)

    @pytest.mark.parametrize("name", ["eating out", " EATING OUT ", "Eating Out"])
    def test_names_are_unique_ignoring_case_and_spaces(self, tmp_path, name):
        path = tmp_path / "categories.csv"
        add_category(path, "Eating Out")
        before = path.read_text(encoding="utf-8")

        with pytest.raises(CategoryError, match="Eating Out"):
            add_category(path, name)
        assert path.read_text(encoding="utf-8") == before

    def test_empty_name_is_refused(self, tmp_path):
        with pytest.raises(CategoryError):
            add_category(tmp_path / "categories.csv", "  ")

    def test_name_with_a_comma_round_trips(self, tmp_path):
        path = tmp_path / "categories.csv"
        add_category(path, "Books, music")
        assert "Books, music" in load_categories(path)

    def test_remove_unused(self, tmp_path, rules_csv):
        path = categories_file(rules_csv)
        add_category(path, "Pets")
        assert remove_category(path, "pets", rules_csv) == "Pets"
        assert "Pets" not in load_categories(path)

    def test_remove_refuses_a_category_a_rule_uses(self, tmp_path, rules_csv):
        path = categories_file(rules_csv)
        load_categories(path, rules_csv)
        with pytest.raises(CategoryError, match="rule"):
            remove_category(path, "health", rules_csv)
        assert "Health" in load_categories(path)

    def test_remove_refuses_a_category_a_transaction_uses(self, tmp_path, rules_csv):
        db = _ledger(tmp_path, "household")
        path = categories_file(rules_csv)
        add_category(path, "Household")
        with pytest.raises(CategoryError, match="transaction"):
            remove_category(path, "Household", rules_csv, db)
        assert "Household" in load_categories(path)

    @pytest.mark.parametrize("name", ["Income", "Ignored", "miscellaneous"])
    def test_remove_refuses_a_builtin(self, tmp_path, name):
        path = tmp_path / "categories.csv"
        with pytest.raises(CategoryError, match="built in"):
            remove_category(path, name)

    def test_remove_refuses_an_unknown_name(self, tmp_path):
        with pytest.raises(CategoryError, match="Nope"):
            remove_category(tmp_path / "categories.csv", "Nope")


class TestAddRuleCategory:
    def test_unknown_category_is_refused(self, rules_csv):
        before = rules_csv.read_text(encoding="utf-8")
        with pytest.raises(CategoryError, match="Grocery"):
            add_rule(rules_csv, "PINGO DOCE", "description", "Grocery")
        assert rules_csv.read_text(encoding="utf-8") == before

    def test_other_case_is_normalised_to_the_list(self, rules_csv):
        assert (
            add_rule(rules_csv, "PINGO DOCE", "description", " groceries ")
            == "Groceries"
        )
        assert load_rules(rules_csv)[-1]["category"] == "Groceries"

    def test_builtin_is_accepted_in_a_fresh_project(self, tmp_path):
        p = tmp_path / "rules.csv"
        add_rule(p, "OWN ACCOUNT", "description", "Ignored")
        assert load_rules(p)[0]["category"] == "Ignored"


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

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(RulesFileError, match="not found"):
            load_rules(tmp_path / "nonexistent.csv")

    def test_header_only_file_is_an_empty_rule_set(self, tmp_path):
        p = tmp_path / "rules.csv"
        p.write_text("pattern,match_field,category,subcategory,payment_type\n")
        assert load_rules(p) == []

    @pytest.mark.parametrize(
        "content",
        [b"", b"name,category\nCONTINENTE,Groceries\n", b"pattern\n\xff\xfe\n"],
        ids=["empty", "no-pattern-column", "not-utf8"],
    )
    def test_unreadable_file_is_an_error(self, tmp_path, content):
        p = tmp_path / "rules.csv"
        p.write_bytes(content)
        with pytest.raises(RulesFileError, match="rules.csv"):
            load_rules(p)

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

    def test_empty_rules_uncategorize_rule_rows(self, populated_db, rules_csv):
        """With no rules left, nothing justifies a rule category any more;
        manual categories stay."""
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        categorize_transactions(conn, load_rules(rules_csv))
        conn.execute(
            "UPDATE transactions SET category = 'Household', category_source = 'manual' "
            "WHERE description_raw LIKE '%FARMACIA%'"
        )
        rule_rows = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_source = 'rule'"
        ).fetchone()[0]

        updated = categorize_transactions(conn, [])

        rows = conn.execute(
            "SELECT category, category_source FROM transactions "
            "WHERE category IS NOT NULL"
        ).fetchall()
        conn.close()
        assert rule_rows > 0
        assert updated == rule_rows
        assert rows == [("Household", "manual")]

    def test_empty_rules_on_uncategorized_ledger_changes_nothing(self, populated_db):
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


class TestMatchRuleAccents:
    """Pattern and descriptions are compared with accents removed."""

    def test_fold_removes_accents_and_upper_cases(self):
        assert fold("Minipreço Café") == "MINIPRECO CAFE"

    def test_plain_pattern_matches_accented_description(self, tmp_path):
        rules = _rules(tmp_path, "CAFE,description,Eating Out,,")
        assert _category(rules, "COMPRA 1234 CAFÉ CENTRAL") == "Eating Out"

    def test_accented_pattern_matches_plain_description(self, tmp_path):
        rules = _rules(tmp_path, "Café,description,Eating Out,,")
        assert _category(rules, "COMPRA 1234 CAFE CENTRAL") == "Eating Out"

    def test_accented_pattern_matches_clean_description(self, tmp_path):
        rules = _rules(tmp_path, "Café,description,Eating Out,,")
        assert _category(rules, "COMPRA 1234", "Café Central") == "Eating Out"

    def test_whole_word_pattern_does_not_match_longer_word(self, tmp_path):
        rules = _rules(tmp_path, '"CAFE ",description,Eating Out,,')
        assert _category(rules, "COMPRA 1234 CAFETERIA") is None

    def test_whole_word_pattern_matches_accented_word(self, tmp_path):
        rules = _rules(tmp_path, '"CAFE ",description,Eating Out,,')
        assert _category(rules, "COMPRA 1234 CAFÉ") == "Eating Out"


class TestMatchRuleDescriptionsSeparately:
    """A description rule tests raw and clean text apart, never their join."""

    def test_does_not_match_across_raw_and_clean(self, tmp_path):
        rules = _rules(tmp_path, "LISBOA PINGO,description,Groceries,,")
        assert _category(rules, "COMPRA 1234 LISBOA", "PINGO DOCE") is None

    def test_matches_both_words_in_raw_alone(self, tmp_path):
        rules = _rules(tmp_path, "LISBOA PINGO,description,Groceries,,")
        assert _category(rules, "COMPRA LISBOA PINGO DOCE", "XYZ") == "Groceries"

    def test_matches_both_words_in_clean_alone(self, tmp_path):
        rules = _rules(tmp_path, "LISBOA PINGO,description,Groceries,,")
        assert _category(rules, "COMPRA 1234", "LISBOA PINGO DOCE") == "Groceries"


class TestShortPatternWarning:
    """A short pattern without a trailing space matches longer words too."""

    @pytest.mark.parametrize("pattern", ["EDP", "MEO", "AB", "H&M"])
    def test_short_unanchored_pattern_warns(self, pattern):
        message = short_pattern_warning(pattern)
        assert message is not None
        assert f"'{pattern}'" in message
        assert "longer words" in message
        assert f"'{pattern} '" in message  # how to restrict it

    @pytest.mark.parametrize("pattern", ["EDP ", "BP ", "CAFE", "UBER EATS"])
    def test_anchored_or_long_pattern_does_not_warn(self, pattern):
        assert short_pattern_warning(pattern) is None


class TestMatchRuleSpecificity:
    def test_longer_pattern_beats_earlier_shorter_one(self, tmp_path):
        rules = _rules(
            tmp_path,
            "UBER,description,Transport,,",
            "UBER EATS,description,Eating Out,,",
        )
        assert _category(rules, "COMPRA 1234 UBER EATS LISBOA") == "Eating Out"

    def test_shorter_pattern_still_matches_on_its_own(self, tmp_path):
        rules = _rules(
            tmp_path,
            "UBER,description,Transport,,",
            "UBER EATS,description,Eating Out,,",
        )
        assert _category(rules, "COMPRA 1234 UBER TRIP") == "Transport"

    def test_equal_length_patterns_fall_back_to_file_order(self, tmp_path):
        rules = _rules(
            tmp_path,
            "LIDL,description,First,,",
            "PORT,description,Second,,",
        )
        assert _category(rules, "COMPRA 1234 LIDL PORTO") == "First"


# ---------------------------------------------------------------------------
# categorize_transactions — category provenance
# ---------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, like: str) -> tuple:
    return conn.execute(
        "SELECT category, subcategory, category_source FROM transactions "
        "WHERE description_raw LIKE ?",
        (f"%{like}%",),
    ).fetchone()


class TestCategoryProvenance:
    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    @pytest.fixture
    def conn(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        yield conn
        conn.close()

    def test_rule_categorization_is_marked_as_rule(self, conn, tmp_path):
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        )
        assert _row(conn, "CONTINENTE") == ("Groceries", None, "rule")

    def test_changed_rule_recategorizes_its_rows(self, conn, tmp_path):
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        )
        updated = categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Food,Supermarket,")
        )
        assert updated == 1
        assert _row(conn, "CONTINENTE") == ("Food", "Supermarket", "rule")

    def test_removed_rule_uncategorizes_its_rows(self, conn, tmp_path):
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,Supermarket,")
        )
        categorize_transactions(conn, _rules(tmp_path, "FARMACIA,description,Health,,"))
        assert _row(conn, "CONTINENTE") == (None, None, None)

    def test_manual_category_is_never_overridden(self, conn, tmp_path):
        conn.execute(
            "UPDATE transactions SET category = 'Household', category_source = 'manual' "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        )
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        )
        assert _row(conn, "CONTINENTE") == ("Household", None, "manual")

    def test_unchanged_rows_are_not_counted(self, conn, tmp_path):
        rules = _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        categorize_transactions(conn, rules)
        assert categorize_transactions(conn, rules) == 0


class TestLegacyCategoryAdoption:
    """Rows categorized before category_source existed have no provenance."""

    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    @pytest.fixture
    def conn(self, populated_db):
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        yield conn
        conn.close()

    def _legacy(self, conn, like, category, subcategory=None):
        conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, "
            "category_source = NULL WHERE description_raw LIKE ?",
            (category, subcategory, f"%{like}%"),
        )

    def test_category_the_old_matcher_produced_is_adopted_as_rule(self, conn, tmp_path):
        # Old substring matching put "FARMACIA" under
        # the "ARMA" rule; the new word-boundary matcher does not.
        self._legacy(conn, "FARMACIA", "Bars")
        categorize_transactions(conn, _rules(tmp_path, "ARMA,description,Bars,,"))
        assert _row(conn, "FARMACIA") == (None, None, None)

    def test_category_the_old_matcher_would_not_produce_is_manual(self, conn, tmp_path):
        self._legacy(conn, "CONTINENTE", "Household")
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,,")
        )
        assert _row(conn, "CONTINENTE") == ("Household", None, "manual")

    def test_manually_changed_subcategory_is_manual(self, conn, tmp_path):
        self._legacy(conn, "CONTINENTE", "Groceries", "Bulk Buy")
        categorize_transactions(
            conn, _rules(tmp_path, "CONTINENTE,description,Groceries,Supermarket,")
        )
        assert _row(conn, "CONTINENTE") == ("Groceries", "Bulk Buy", "manual")
