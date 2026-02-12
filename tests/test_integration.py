"""End-to-end integration tests for the expense tracking pipeline."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.db import (
    ingest,
    ingested_source_files,
    reclean_descriptions,
    fetch_all_transactions,
    migrate_schema,
)
from expense_tracker.export import export_csv
from expense_tracker.parser import reset_cleaning_cache, _get_cleaning_patterns
from expense_tracker.rules import (
    add_rule,
    load_rules,
    categorize_transactions,
)
from tests.conftest import make_utf16_csv


class TestFullWorkflow:
    """Test the complete pipeline: ingest -> categorize -> export."""

    @pytest.fixture(autouse=True)
    def _setup_cleaning(self, noise_words, cleaning_patterns):
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)
        yield
        reset_cleaning_cache()

    def test_ingest_categorize_export(
        self, test_db, utf16_csv, cards_csv, rules_csv, tmp_path
    ):
        # 1. Ingest
        ingest(test_db, [utf16_csv], cards_path=cards_csv)
        assert len(ingested_source_files(test_db)) == 1

        # 2. Categorize
        rules = load_rules(rules_csv)
        conn = sqlite3.connect(str(test_db))
        migrate_schema(conn)
        updated = categorize_transactions(conn, rules)
        assert updated > 0

        # 3. Verify categorization
        txs = fetch_all_transactions(conn)
        conn.close()

        categorized = [t for t in txs if t["status"] == "categorized"]
        assert len(categorized) >= 3  # CONTINENTE, FARMACIA, VODAFONE at minimum

        # Check specific categories
        cat_map = {t["description_raw"]: t["category"] for t in txs}
        continente = [v for k, v in cat_map.items() if "CONTINENTE" in k]
        assert continente[0] == "Groceries"

        # 4. Export
        out = tmp_path / "final.csv"
        export_csv(test_db, out)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "Groceries" in content

    def test_multiple_csv_ingestion(self, test_db, tmp_path, cards_csv):
        # Create two separate CSV files for different months
        jan_rows = [
            ("15-01-2026", "15-01-2026", "COMPRA 1234 CONTINENTE PORTO",
             "-45,50", "Compra", "1000,00"),
        ]
        feb_rows = [
            ("15-02-2026", "15-02-2026", "COMPRA 1234 PINGO DOCE PORTO",
             "-30,00", "Compra", "970,00"),
        ]

        jan_csv = make_utf16_csv(
            tmp_path / "jan.csv", jan_rows,
            date_from="01-01-2026", date_to="31-01-2026",
        )
        feb_csv = make_utf16_csv(
            tmp_path / "feb.csv", feb_rows,
            date_from="01-02-2026", date_to="28-02-2026",
        )

        ingest(test_db, [jan_csv, feb_csv], cards_path=cards_csv)

        conn = sqlite3.connect(str(test_db))
        migrate_schema(conn)
        txs = fetch_all_transactions(conn)
        conn.close()

        assert len(txs) == 2
        months = {t["month"] for t in txs}
        assert "2026-01" in months
        assert "2026-02" in months

    def test_reclean_after_pattern_change(
        self, populated_db, tmp_path, noise_words, cleaning_patterns
    ):
        """Changing cleaning patterns and running reclean should update descriptions."""
        # Get current descriptions
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        before = {
            t["transaction_id"]: t["description_clean"]
            for t in fetch_all_transactions(conn)
        }
        conn.close()

        # Add a new noise word
        current = noise_words.read_text(encoding="utf-8")
        noise_words.write_text(current + "GARE\n", encoding="utf-8")

        # Reset cache so new patterns are loaded
        reset_cleaning_cache()
        _get_cleaning_patterns(noise_words, cleaning_patterns)

        updated = reclean_descriptions(populated_db)

        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        after = {
            t["transaction_id"]: t["description_clean"]
            for t in fetch_all_transactions(conn)
        }
        conn.close()

        # At least the FARMACIA DA GARE transaction should have changed
        # (GARE is now a noise word)
        farmacia_changed = any(
            before[tid] != after[tid]
            for tid in before
            if "FARMACIA" in before[tid]
        )
        assert farmacia_changed or updated > 0

    def test_add_rule_and_categorize(self, populated_db, tmp_path):
        """Adding a rule via the API and running categorize should work."""
        rules_path = tmp_path / "new_rules.csv"
        add_rule(rules_path, "EXEMPLO", "description", "Insurance", "Health Insurance")

        rules = load_rules(rules_path)
        conn = sqlite3.connect(str(populated_db))
        migrate_schema(conn)
        updated = categorize_transactions(conn, rules)

        row = conn.execute(
            "SELECT category, subcategory FROM transactions "
            "WHERE description_raw LIKE '%EXEMPLO%'"
        ).fetchone()
        conn.close()

        assert updated >= 1
        assert row[0] == "Insurance"
        assert row[1] == "Health Insurance"
