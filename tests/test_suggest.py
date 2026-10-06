"""Tests for expense_tracker.suggest."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from expense_tracker.db import ensure_schema, migrate_schema
from expense_tracker.rules import load_rules
from expense_tracker.suggest import (
    _cluster_by_name,
    _extract_common_prefix,
    _merchant_stats,
    _similarity,
    _stddev,
    accept_suggestion,
    analyze_patterns,
    detect_frequent_merchants,
    detect_recurring,
    detect_similar_merchants,
    format_suggestions,
)

# ---------------------------------------------------------------------------
# Helper to populate a test DB with transactions
# ---------------------------------------------------------------------------


def _populate_db(db_path: Path, transactions: list[dict]) -> None:
    """Insert test transactions into a fresh database."""
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    migrate_schema(conn)

    for i, tx in enumerate(transactions):
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, date_posted, date_value, month, day_of_week,
                description_raw, description_clean,
                amount_signed, amount_abs, direction, tx_type, balance,
                currency, account, source_file, imported_at,
                category, payment_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"tx{i:04d}",
                tx.get("date_posted", "2026-01-15"),
                tx.get("date_value", "2026-01-15"),
                tx.get("date_posted", "2026-01-15")[:7],
                "Wed",
                tx.get("description_raw", tx.get("description_clean", "")),
                tx.get("description_clean", ""),
                tx.get("amount_signed", -10.0),
                abs(tx.get("amount_signed", -10.0)),
                "out" if tx.get("amount_signed", -10.0) < 0 else "in",
                "",
                0.0,
                "EUR",
                "checkings_account",
                "test.csv",
                "2026-01-15T10:00:00",
                tx.get("category", ""),
                tx.get("payment_type", ""),
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestStddev:
    def test_empty(self):
        assert _stddev([]) == 0.0

    def test_single(self):
        assert _stddev([5.0]) == 0.0

    def test_uniform(self):
        assert _stddev([5.0, 5.0, 5.0]) == 0.0

    def test_varied(self):
        result = _stddev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert 1.5 < result < 2.5  # approximately 2.0


class TestSimilarity:
    def test_identical(self):
        assert _similarity("FARMACIA", "FARMACIA") == 1.0

    def test_similar(self):
        assert _similarity("FARMACIA DA GARE", "FARMACIA SAO JOAO") > 0.5

    def test_different(self):
        assert _similarity("CONTINENTE", "NETFLIX") < 0.4

    def test_case_insensitive(self):
        assert _similarity("hello", "HELLO") == 1.0


class TestClusterByName:
    def test_clusters_similar(self):
        names = [
            "FARMACIA DA GARE",
            "FARMACIA SAO JOAO",
            "FARMACIA CENTRAL",
            "CONTINENTE",
            "PINGO DOCE",
        ]
        clusters = _cluster_by_name(names, threshold=0.55)
        # Pharmacies should be clustered together
        pharmacy_cluster = None
        for c in clusters:
            if any("FARMACIA" in n for n in c):
                pharmacy_cluster = c
                break
        assert pharmacy_cluster is not None
        assert len(pharmacy_cluster) >= 2

    def test_no_clusters_below_threshold(self):
        names = ["A", "B", "C"]
        clusters = _cluster_by_name(names, threshold=0.99)
        # Each name should be its own cluster
        assert all(len(c) == 1 for c in clusters)

    def test_empty_list(self):
        assert _cluster_by_name([]) == []


class TestExtractCommonPrefix:
    def test_common_prefix(self):
        assert (
            _extract_common_prefix(
                ["FARMACIA DA GARE", "FARMACIA SAO JOAO", "FARMACIA CENTRAL"]
            )
            == "FARMACIA"
        )

    def test_no_common_prefix(self):
        assert _extract_common_prefix(["ABC", "XYZ"]) == ""

    def test_empty_list(self):
        assert _extract_common_prefix([]) == ""

    def test_single_item(self):
        result = _extract_common_prefix(["CONTINENTE"])
        assert result == "CONTINENTE"


class TestMerchantStats:
    def test_basic_stats(self):
        txs = [
            {
                "description_clean": "CONTINENTE",
                "description_raw": "COMPRA CONTINENTE",
                "amount_abs": 50.0,
                "date_posted": "2026-01-15",
                "payment_type": "card",
            },
            {
                "description_clean": "CONTINENTE",
                "description_raw": "COMPRA CONTINENTE",
                "amount_abs": 60.0,
                "date_posted": "2026-02-15",
                "payment_type": "card",
            },
        ]
        stats = _merchant_stats(txs)
        assert "CONTINENTE" in stats
        assert stats["CONTINENTE"]["count"] == 2
        assert stats["CONTINENTE"]["total"] == 110.0
        assert stats["CONTINENTE"]["avg"] == 55.0

    def test_months_tracked(self):
        txs = [
            {
                "description_clean": "NETFLIX",
                "description_raw": "NETFLIX",
                "amount_abs": 10.0,
                "date_posted": "2026-01-15",
                "payment_type": "card",
            },
            {
                "description_clean": "NETFLIX",
                "description_raw": "NETFLIX",
                "amount_abs": 10.0,
                "date_posted": "2026-02-15",
                "payment_type": "card",
            },
            {
                "description_clean": "NETFLIX",
                "description_raw": "NETFLIX",
                "amount_abs": 10.0,
                "date_posted": "2026-03-15",
                "payment_type": "card",
            },
        ]
        stats = _merchant_stats(txs)
        assert stats["NETFLIX"]["months_count"] == 3


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------


class TestDetectRecurring:
    def test_detects_subscription(self):
        merchants = {
            "NETFLIX": {
                "count": 6,
                "total": 60.0,
                "avg": 10.0,
                "min": 10.0,
                "max": 10.0,
                "amount_stddev": 0.0,
                "months_count": 6,
                "primary_payment_type": "card",
                "months": {
                    "2026-01",
                    "2026-02",
                    "2026-03",
                    "2026-04",
                    "2026-05",
                    "2026-06",
                },
                "payment_types": {"card": 6},
                "raw_samples": {"NETFLIX"},
            },
        }
        results = detect_recurring(merchants, min_months=3)
        assert len(results) == 1
        assert results[0]["pattern"] == "NETFLIX"
        assert results[0]["confidence"] == "high"

    def test_ignores_irregular(self):
        merchants = {
            "RANDOM SHOP": {
                "count": 4,
                "total": 200.0,
                "avg": 50.0,
                "min": 10.0,
                "max": 100.0,
                "amount_stddev": 30.0,  # 60% variation
                "months_count": 4,
                "primary_payment_type": "card",
                "months": {"2026-01", "2026-02", "2026-03", "2026-04"},
                "payment_types": {"card": 4},
                "raw_samples": {"RANDOM SHOP"},
            },
        }
        results = detect_recurring(merchants, min_months=3)
        assert len(results) == 0


class TestDetectSimilarMerchants:
    def test_groups_pharmacies(self):
        merchants = {
            "FARMACIA DA GARE": {"count": 3, "total": 30.0},
            "FARMACIA SAO JOAO": {"count": 5, "total": 50.0},
            "FARMACIA CENTRAL": {"count": 2, "total": 20.0},
            "CONTINENTE": {"count": 10, "total": 500.0},
        }
        results = detect_similar_merchants(merchants, threshold=0.55)
        # Should have a pharmacy cluster
        pharmacy_group = [
            r for r in results if any("FARMACIA" in m for m in r["merchants"])
        ]
        assert len(pharmacy_group) == 1
        assert len(pharmacy_group[0]["merchants"]) >= 2

    def test_no_groups_when_all_different(self):
        merchants = {
            "ABC": {"count": 1, "total": 10.0},
            "XYZ": {"count": 1, "total": 10.0},
        }
        results = detect_similar_merchants(merchants, threshold=0.9)
        assert len(results) == 0


class TestDetectFrequent:
    def test_detects_frequent(self):
        merchants = {
            "SHOP A": {"count": 10, "total": 100.0},
            "SHOP B": {"count": 2, "total": 20.0},
        }
        results = detect_frequent_merchants(merchants, min_count=5)
        assert len(results) == 1
        assert results[0]["pattern"] == "SHOP A"


# ---------------------------------------------------------------------------
# Integration: analyze_patterns
# ---------------------------------------------------------------------------


class TestAnalyzePatterns:
    def test_empty_db(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        ensure_schema(conn)
        migrate_schema(conn)
        conn.close()

        results = analyze_patterns(db_path)
        assert results["stats"]["total_uncategorized"] == 0
        assert results["recurring"] == []
        assert results["similar"] == []
        assert results["frequent"] == []

    def test_detects_patterns(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)

        # Create a year of Netflix-like subscriptions + some random purchases
        txs = []
        for month in range(1, 13):
            txs.append(
                {
                    "description_clean": "NETFLIX",
                    "description_raw": "DD NETFLIX INTL",
                    "amount_signed": -14.99,
                    "date_posted": f"2026-{month:02d}-15",
                    "payment_type": "direct_debit",
                    "category": "",
                }
            )
        # Add some frequent merchant
        for i in range(8):
            txs.append(
                {
                    "description_clean": "CAFE CENTRAL",
                    "description_raw": "COMPRA CAFE CENTRAL",
                    "amount_signed": -3.50,
                    "date_posted": f"2026-01-{i+1:02d}",
                    "payment_type": "card",
                    "category": "",
                }
            )

        _populate_db(db_path, txs)
        results = analyze_patterns(
            db_path, min_months_recurring=3, min_count_frequent=5
        )

        assert results["stats"]["total_uncategorized"] == 20
        # Netflix should be detected as recurring
        assert len(results["recurring"]) >= 1
        netflix = [r for r in results["recurring"] if "NETFLIX" in r["pattern"]]
        assert len(netflix) == 1

    def test_skips_categorized(self, tmp_path):
        db_path = tmp_path / "data" / "ledger.sqlite"
        db_path.parent.mkdir(parents=True)

        txs = []
        for month in range(1, 6):
            txs.append(
                {
                    "description_clean": "NETFLIX",
                    "amount_signed": -14.99,
                    "date_posted": f"2026-{month:02d}-15",
                    "category": "Subscriptions",  # Already categorized
                }
            )

        _populate_db(db_path, txs)
        results = analyze_patterns(db_path)
        assert results["stats"]["total_uncategorized"] == 0


class TestFormatSuggestions:
    def test_empty_results(self):
        results = {
            "recurring": [],
            "similar": [],
            "frequent": [],
            "stats": {"total_uncategorized": 0, "unique_merchants": 0},
        }
        output = format_suggestions(results)
        assert "No patterns detected" in output

    def test_with_recurring(self):
        results = {
            "recurring": [
                {
                    "merchants": ["NETFLIX"],
                    "pattern": "NETFLIX",
                    "suggested_category": "Subscriptions",
                    "reason": "Appears in 6 months",
                    "confidence": "high",
                    "count": 6,
                    "total": 90.0,
                },
            ],
            "similar": [],
            "frequent": [],
            "stats": {"total_uncategorized": 10, "unique_merchants": 5},
        }
        output = format_suggestions(results)
        assert "RECURRING" in output
        assert "NETFLIX" in output


class TestAcceptSuggestion:
    def test_creates_rule(self, tmp_path):
        rules_path = tmp_path / "rules.csv"
        accept_suggestion(rules_path, "NETFLIX", "Subscriptions", "Streaming")
        rules = load_rules(rules_path)
        assert len(rules) == 1
        assert rules[0]["pattern"] == "NETFLIX"
        assert rules[0]["category"] == "Subscriptions"
        assert rules[0]["subcategory"] == "Streaming"
