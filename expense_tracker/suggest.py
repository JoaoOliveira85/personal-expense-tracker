"""
Automatic category pattern detection — analyzes transaction history to
discover merchant clusters and suggest categorization rules.

Uses frequency analysis, text similarity, and temporal patterns to group
uncategorized merchants into logical clusters. No ML dependencies required.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from .constants import DEFAULT_DB
from .rules import add_rule

# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def _fetch_uncategorized(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all uncategorized outgoing transactions."""
    rows = conn.execute("""
        SELECT description_clean, description_raw, amount_abs, date_posted,
               payment_type
        FROM transactions
        WHERE direction = 'out'
          AND (category IS NULL OR category = '')
        ORDER BY date_posted
        """).fetchall()
    return [
        {
            "description_clean": r[0] or "",
            "description_raw": r[1] or "",
            "amount_abs": r[2],
            "date_posted": r[3],
            "payment_type": r[4] or "",
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Merchant analysis
# ---------------------------------------------------------------------------


def _merchant_stats(transactions: list[dict]) -> dict[str, dict]:
    """
    Compute statistics per merchant (cleaned description).

    Returns a dict: merchant_name -> {
        count, total, avg, min, max, months, payment_types, raw_samples
    }
    """
    merchants: dict[str, dict] = {}

    for tx in transactions:
        name = tx["description_clean"]
        if not name:
            continue

        if name not in merchants:
            merchants[name] = {
                "count": 0,
                "total": 0.0,
                "amounts": [],
                "months": set(),
                "payment_types": Counter(),
                "raw_samples": set(),
            }

        m = merchants[name]
        m["count"] += 1
        m["total"] += tx["amount_abs"]
        m["amounts"].append(tx["amount_abs"])
        m["months"].add(tx["date_posted"][:7])
        m["payment_types"][tx["payment_type"]] += 1
        if len(m["raw_samples"]) < 3:
            m["raw_samples"].add(tx["description_raw"])

    # Compute derived stats
    for name, m in merchants.items():
        amounts = m["amounts"]
        m["avg"] = m["total"] / m["count"] if m["count"] else 0
        m["min"] = min(amounts) if amounts else 0
        m["max"] = max(amounts) if amounts else 0
        m["amount_stddev"] = _stddev(amounts)
        m["months_count"] = len(m["months"])
        m["primary_payment_type"] = (
            m["payment_types"].most_common(1)[0][0] if m["payment_types"] else ""
        )
        del m["amounts"]  # Clean up

    return merchants


def _stddev(values: list[float]) -> float:
    """Simple standard deviation (population)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return variance**0.5


# ---------------------------------------------------------------------------
# Text similarity clustering
# ---------------------------------------------------------------------------


def _similarity(a: str, b: str) -> float:
    """Compute text similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.upper(), b.upper()).ratio()


def _cluster_by_name(
    merchant_names: list[str],
    threshold: float = 0.65,
) -> list[list[str]]:
    """
    Group merchant names into clusters based on text similarity.

    Uses a simple greedy approach: iterate through names, assign each
    to the first cluster whose representative is similar enough,
    or start a new cluster.
    """
    clusters: list[list[str]] = []
    representatives: list[str] = []

    # Sort by name for determinism
    sorted_names = sorted(merchant_names)

    for name in sorted_names:
        placed = False
        for i, rep in enumerate(representatives):
            if _similarity(name, rep) >= threshold:
                clusters[i].append(name)
                placed = True
                break
        if not placed:
            clusters.append([name])
            representatives.append(name)

    return clusters


def _extract_common_prefix(names: list[str], min_len: int = 3) -> str:
    """Find the longest common prefix among a list of names."""
    if not names:
        return ""
    prefix = names[0].upper()
    for name in names[1:]:
        upper = name.upper()
        i = 0
        while i < len(prefix) and i < len(upper) and prefix[i] == upper[i]:
            i += 1
        prefix = prefix[:i]
    # Trim to word boundary
    prefix = prefix.rstrip()
    if len(prefix) < min_len:
        return ""
    return prefix


# ---------------------------------------------------------------------------
# Pattern detection strategies
# ---------------------------------------------------------------------------


def detect_recurring(
    merchants: dict[str, dict],
    min_months: int = 3,
    max_amount_stddev_pct: float = 0.15,
) -> list[dict]:
    """
    Detect recurring/subscription-like merchants.

    Criteria: appears in N+ months with low amount variation.
    """
    suggestions = []
    for name, stats in merchants.items():
        if stats["months_count"] < min_months:
            continue
        if stats["avg"] == 0:
            continue
        # Check if amount is fairly consistent
        relative_stddev = stats["amount_stddev"] / stats["avg"] if stats["avg"] else 1
        if relative_stddev <= max_amount_stddev_pct:
            suggestions.append(
                {
                    "merchants": [name],
                    "pattern": name,
                    "suggested_category": "Subscriptions",
                    "reason": (
                        f"Appears in {stats['months_count']} months with "
                        f"consistent amount (~{stats['avg']:.2f}€)"
                    ),
                    "confidence": "high",
                    "count": stats["count"],
                    "total": stats["total"],
                }
            )
    return suggestions


def detect_similar_merchants(
    merchants: dict[str, dict],
    threshold: float = 0.65,
    min_cluster_size: int = 2,
) -> list[dict]:
    """
    Detect groups of similar merchants that likely belong to the same category.

    Groups merchants by name similarity and returns clusters with 2+ members.
    """
    names = list(merchants.keys())
    clusters = _cluster_by_name(names, threshold)

    suggestions = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue

        common_prefix = _extract_common_prefix(cluster)
        total_count = sum(merchants[n]["count"] for n in cluster)
        total_amount = sum(merchants[n]["total"] for n in cluster)

        suggestions.append(
            {
                "merchants": cluster,
                "pattern": common_prefix if common_prefix else cluster[0],
                "suggested_category": None,  # User needs to name this
                "reason": (
                    f"{len(cluster)} similar merchants "
                    f"({total_count} transactions, {total_amount:.2f}€ total)"
                ),
                "confidence": "medium",
                "count": total_count,
                "total": total_amount,
            }
        )

    return suggestions


def detect_frequent_merchants(
    merchants: dict[str, dict],
    min_count: int = 5,
) -> list[dict]:
    """Detect frequently-occurring merchants that should probably have a rule."""
    suggestions = []
    for name, stats in merchants.items():
        if stats["count"] >= min_count:
            suggestions.append(
                {
                    "merchants": [name],
                    "pattern": name,
                    "suggested_category": None,
                    "reason": f"{stats['count']} transactions totaling {stats['total']:.2f}€",
                    "confidence": "low",
                    "count": stats["count"],
                    "total": stats["total"],
                }
            )
    return suggestions


# ---------------------------------------------------------------------------
# Main suggestion engine
# ---------------------------------------------------------------------------


def analyze_patterns(
    db_path: Path = DEFAULT_DB,
    min_months_recurring: int = 3,
    similarity_threshold: float = 0.65,
    min_count_frequent: int = 5,
) -> dict:
    """
    Analyze the transaction database and return pattern-based suggestions.

    Returns a dict with:
    - 'recurring': subscription-like patterns
    - 'similar': merchant name clusters
    - 'frequent': high-frequency uncategorized merchants
    - 'stats': summary statistics
    """
    conn = sqlite3.connect(str(db_path))
    try:
        uncategorized = _fetch_uncategorized(conn)
    finally:
        conn.close()

    if not uncategorized:
        return {
            "recurring": [],
            "similar": [],
            "frequent": [],
            "stats": {
                "total_uncategorized": 0,
                "unique_merchants": 0,
            },
        }

    merchants = _merchant_stats(uncategorized)

    recurring = detect_recurring(merchants, min_months=min_months_recurring)
    similar = detect_similar_merchants(merchants, threshold=similarity_threshold)
    frequent = detect_frequent_merchants(merchants, min_count=min_count_frequent)

    # Filter out merchants already covered by recurring/similar suggestions
    covered = set()
    for s in recurring:
        covered.update(s["merchants"])
    for s in similar:
        covered.update(s["merchants"])
    frequent = [s for s in frequent if s["pattern"] not in covered]

    return {
        "recurring": sorted(recurring, key=lambda s: s["total"], reverse=True),
        "similar": sorted(similar, key=lambda s: s["total"], reverse=True),
        "frequent": sorted(frequent, key=lambda s: s["total"], reverse=True),
        "stats": {
            "total_uncategorized": len(uncategorized),
            "unique_merchants": len(merchants),
        },
    }


def accept_suggestion(
    rules_path: Path,
    pattern: str,
    category: str,
    subcategory: str = "",
    match_field: str = "description",
) -> None:
    """Accept a suggestion by adding it as a rule."""
    add_rule(rules_path, pattern, match_field, category, subcategory)


# ---------------------------------------------------------------------------
# Display helpers (for CLI output)
# ---------------------------------------------------------------------------


def format_suggestions(results: dict) -> str:
    """Format analysis results as a human-readable string."""
    lines = []
    stats = results["stats"]

    lines.append(
        f"Analysis of {stats['total_uncategorized']} uncategorized transactions "
        f"({stats['unique_merchants']} unique merchants)"
    )
    lines.append("")

    # Recurring
    if results["recurring"]:
        lines.append("=" * 60)
        lines.append("RECURRING / SUBSCRIPTIONS (high confidence)")
        lines.append("=" * 60)
        for i, s in enumerate(results["recurring"], 1):
            lines.append(f"  {i}. {s['pattern']}")
            lines.append(f"     {s['reason']}")
            if s["suggested_category"]:
                lines.append(f"     Suggested: {s['suggested_category']}")
        lines.append("")

    # Similar
    if results["similar"]:
        lines.append("=" * 60)
        lines.append("SIMILAR MERCHANT GROUPS (medium confidence)")
        lines.append("=" * 60)
        for i, s in enumerate(results["similar"], 1):
            lines.append(f"  {i}. Group: {', '.join(s['merchants'][:5])}")
            if len(s["merchants"]) > 5:
                lines.append(f"     ... and {len(s['merchants']) - 5} more")
            lines.append(f"     {s['reason']}")
            if s["pattern"]:
                lines.append(f"     Common prefix: \"{s['pattern']}\"")
        lines.append("")

    # Frequent
    if results["frequent"]:
        lines.append("=" * 60)
        lines.append("FREQUENT UNCATEGORIZED MERCHANTS")
        lines.append("=" * 60)
        for i, s in enumerate(results["frequent"][:20], 1):
            lines.append(f"  {i}. {s['pattern']}")
            lines.append(f"     {s['reason']}")
        lines.append("")

    if not any(results[k] for k in ("recurring", "similar", "frequent")):
        lines.append("No patterns detected. You may need more transaction history,")
        lines.append("or most transactions are already categorized.")

    return "\n".join(lines)
