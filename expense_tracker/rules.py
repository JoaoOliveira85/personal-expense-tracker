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
    Apply rules to every transaction whose category was not set by hand.

    Rows categorized by a rule (category_source = 'rule') are re-evaluated, so
    editing or removing a rule corrects them; one that no rule matches any
    more becomes uncategorized again. Manual categories are never touched.

    Returns the number of transactions whose category or subcategory changed.
    """
    if not rules:
        # Also stops a missing rules file from wiping every rule category.
        return 0

    _adopt_legacy_categories(conn, rules)

    rows = conn.execute(
        """
        SELECT transaction_id, description_raw, description_clean, payment_type,
               category, subcategory
        FROM transactions
        WHERE category IS NULL OR category = '' OR category_source = 'rule'
        """
    ).fetchall()

    updated = 0
    for tid, desc_raw, desc_clean, existing_ptype, old_cat, old_sub in rows:
        rule = match_rule(rules, desc_raw, desc_clean)
        if rule is None:
            if not old_cat:
                continue
            new_cat, new_sub, source = None, None, None
        else:
            new_cat, new_sub, source = rule["category"], rule["subcategory"] or None, "rule"

        if (old_cat or None, old_sub or None) == (new_cat, new_sub):
            continue

        conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, "
            "category_source = ? WHERE transaction_id = ?",
            (new_cat, new_sub, source, tid),
        )
        if rule and rule["payment_type"] and not existing_ptype:
            conn.execute(
                "UPDATE transactions SET payment_type = ? WHERE transaction_id = ?",
                (rule["payment_type"], tid),
            )
        updated += 1

    conn.commit()
    return updated


def _adopt_legacy_categories(conn: sqlite3.Connection, rules: list[dict]) -> None:
    """Give a category_source to rows categorized before it existed.

    A row whose category and subcategory equal what the old matcher (first
    substring match in file order) picks from the current rules was set by a
    rule, so it becomes rule-owned and gets re-evaluated. Anything else was
    edited by hand, or its rule has changed since, and is kept as manual.
    """
    rows = conn.execute(
        """
        SELECT transaction_id, description_raw, description_clean,
               category, subcategory
        FROM transactions
        WHERE category <> '' AND category_source IS NULL
        """
    ).fetchall()

    for tid, desc_raw, desc_clean, category, subcategory in rows:
        rule = _legacy_match(rules, desc_raw, desc_clean)
        by_rule = (
            rule is not None
            and rule["category"] == category
            and rule["subcategory"] == (subcategory or "")
        )
        conn.execute(
            "UPDATE transactions SET category_source = ? WHERE transaction_id = ?",
            ("rule" if by_rule else "manual", tid),
        )


def _legacy_match(
    rules: list[dict], desc_raw: str | None, desc_clean: str | None
) -> dict | None:
    """The matcher used before word boundaries: first substring hit wins."""
    raw, both = _match_targets(desc_raw, desc_clean)
    for rule in rules:
        target = raw if rule["match_field"] == "description_raw" else both
        if rule["pattern_upper"] in target:
            return rule
    return None


def _match_targets(desc_raw: str | None, desc_clean: str | None) -> tuple[str, str]:
    """Uppercased text searched by description_raw rules and by all others."""
    raw = (desc_raw or "").upper()
    # Default: match against both raw and clean descriptions
    return raw, raw + " " + (desc_clean or "").upper()


def match_rule(
    rules: list[dict], desc_raw: str | None, desc_clean: str | None
) -> dict | None:
    """Return the rule that categorizes a transaction, or None.

    The most specific (longest) matching pattern wins, so "UBER EATS" beats
    "UBER" regardless of where each sits in the file. Ties go to file order.
    """
    raw, both = _match_targets(desc_raw, desc_clean)

    best = None
    for rule in rules:
        target = raw if rule["match_field"] == "description_raw" else both
        # The substring test is a cheap prefilter: every word-boundary match
        # is also a substring match, and most rules match neither.
        if rule["pattern_upper"] in target and rule["regex"].search(target):
            if best is None or len(rule["pattern"]) > len(best["pattern"]):
                best = rule
    return best
