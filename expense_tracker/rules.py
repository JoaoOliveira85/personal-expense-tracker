"""Categorization rules: loading, editing, and applying to transactions."""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Rule management (add / remove)
# ---------------------------------------------------------------------------

RULES_HEADER = ["pattern", "match_field", "category", "subcategory", "payment_type"]


def add_rule(
    rules_path: Path,
    pattern: str,
    match_field: str,
    category: str,
    subcategory: str = "",
    payment_type: str = "",
) -> None:
    """Append a new rule to the rules CSV file."""
    is_new = not rules_path.exists() or rules_path.stat().st_size == 0
    with rules_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(RULES_HEADER)
        writer.writerow([pattern, match_field, category, subcategory, payment_type])


def remove_rule(rules_path: Path, pattern: str) -> bool:
    """Remove the first rule matching `pattern` (case-insensitive).
    Returns True if a rule was removed."""
    if not rules_path.exists():
        return False
    with rules_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    found = False
    new_rows = []
    for row in rows:
        if not found and row.get("pattern", "").strip().upper() == pattern.upper():
            found = True
            continue
        new_rows.append(row)

    if not found:
        return False

    with rules_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RULES_HEADER)
        writer.writeheader()
        writer.writerows(new_rows)
    return True


# ---------------------------------------------------------------------------
# Card-holder management (add / remove)
# ---------------------------------------------------------------------------

CARDS_HEADER = ["card_last4", "name"]


def add_card(cards_path: Path, last4: str, name: str) -> None:
    """Add a card-holder mapping to the CSV file."""
    # Check for duplicates first
    existing = _load_cards_raw(cards_path)
    for row in existing:
        if row["card_last4"] == last4:
            raise ValueError(f"Card {last4} already exists (mapped to '{row['name']}')")

    is_new = not cards_path.exists() or cards_path.stat().st_size == 0
    with cards_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(CARDS_HEADER)
        writer.writerow([last4, name])


def remove_card(cards_path: Path, last4: str) -> bool:
    """Remove a card-holder by last-4 digits. Returns True if removed."""
    existing = _load_cards_raw(cards_path)
    if not existing:
        return False

    new_rows = [r for r in existing if r["card_last4"] != last4]
    if len(new_rows) == len(existing):
        return False

    with cards_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CARDS_HEADER)
        writer.writeheader()
        writer.writerows(new_rows)
    return True


def _load_cards_raw(cards_path: Path) -> list[dict]:
    """Read all rows from the cards CSV."""
    if not cards_path.exists():
        return []
    with cards_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def load_rules(rules_path: Path) -> list[dict]:
    """
    Load categorization rules from a CSV file.

    Expected columns: pattern, match_field, category, subcategory, payment_type
    The longest matching pattern wins; file order breaks ties (see match_rule).

    Patterns match case-insensitively at the start of a word, so "PAO" matches
    "PAO QUENTE" and "PAOZINHO" but not "JAPAO". A trailing space in the
    pattern ("BP ") also requires the match to end at a word boundary.
    """
    if not rules_path.exists():
        print(f"Warning: rules file not found at {rules_path}, skipping categorization.")
        return []

    rules = []
    with rules_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_pattern = row.get("pattern", "")
            pattern = raw_pattern.strip()
            if not pattern:
                continue
            rules.append(
                {
                    "pattern": pattern,
                    "pattern_upper": pattern.upper(),
                    "regex": _compile_pattern(
                        pattern, whole_word=raw_pattern != raw_pattern.rstrip()
                    ),
                    "match_field": row.get("match_field", "description").strip(),
                    "category": row.get("category", "").strip(),
                    "subcategory": row.get("subcategory", "").strip(),
                    "payment_type": row.get("payment_type", "").strip(),
                }
            )
    return rules


def _compile_pattern(pattern: str, whole_word: bool) -> re.Pattern[str]:
    """Anchor a literal pattern to word boundaries (see load_rules)."""
    regex = re.escape(pattern.upper())
    # Only anchor on word characters: a pattern like ">PAGAMENTO" already
    # starts at a natural boundary and may follow a letter in bank text.
    if pattern[0].isalnum():
        regex = r"(?<!\w)" + regex
    if whole_word:
        regex += r"(?!\w)"
    return re.compile(regex)


def categorize_transactions(conn: sqlite3.Connection, rules: list[dict]) -> int:
    """
    Apply rules to all uncategorized transactions in the database.
    Returns the number of transactions updated.
    """
    if not rules:
        return 0

    rows = conn.execute(
        """
        SELECT transaction_id, description_raw, description_clean, payment_type
        FROM transactions
        WHERE category IS NULL OR category = ''
        """
    ).fetchall()

    updated = 0
    for tid, desc_raw, desc_clean, existing_ptype in rows:
        rule = match_rule(rules, desc_raw, desc_clean)
        if rule is None:
            continue

        updates = {"category": rule["category"], "tid": tid}
        set_clauses = ["category = :category"]

        if rule["subcategory"]:
            updates["subcategory"] = rule["subcategory"]
            set_clauses.append("subcategory = :subcategory")

        if rule["payment_type"] and not existing_ptype:
            updates["payment_type"] = rule["payment_type"]
            set_clauses.append("payment_type = :payment_type")

        conn.execute(
            f"UPDATE transactions SET {', '.join(set_clauses)} "
            f"WHERE transaction_id = :tid",
            updates,
        )
        updated += 1

    conn.commit()
    return updated


def match_rule(
    rules: list[dict], desc_raw: str | None, desc_clean: str | None
) -> dict | None:
    """Return the rule that categorizes a transaction, or None.

    The most specific (longest) matching pattern wins, so "UBER EATS" beats
    "UBER" regardless of where each sits in the file. Ties go to file order.
    """
    desc_raw_upper = (desc_raw or "").upper()
    desc_clean_upper = (desc_clean or "").upper()

    best = None
    for rule in rules:
        if rule["match_field"] == "description_raw":
            target = desc_raw_upper
        else:
            # Default: match against both raw and clean descriptions
            target = desc_raw_upper + " " + desc_clean_upper

        if rule["regex"].search(target):
            if best is None or len(rule["pattern"]) > len(best["pattern"]):
                best = rule
    return best
