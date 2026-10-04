"""
ODS report generation.

Hybrid approach:
- Always replaces the "Data" and "Rules" sheets.
- On first run, generates starter analysis sheets with real spreadsheet formulas.
- On subsequent runs, preserves all user-modified sheets.

Sheet builders live in ods_sheets.py; this module handles orchestration and
sync-back of manual edits from the ODS into SQLite / description-notes.csv.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from .constants import (
    DEFAULT_DESC_NOTES,
    COL_DESCRIPTION_CLEAN, COL_CATEGORY, COL_SUBCATEGORY,
    COL_TRANSACTION_ID, COL_NOTES, COL_MERCHANT_NOTE,
)
from .db import migrate_schema, fetch_all_transactions
from .ods_sheets import (
    setup_styles,
    build_data_sheet, build_rules_sheet, build_intro_sheet,
    write_intro_sheet, write_data_sheet, write_rules_sheet,
    write_dashboard_sheet,
    write_monthly_summary_sheet, write_monthly_trend_sheet,
    write_category_breakdown_sheet, write_subcategory_breakdown_sheet,
    write_tags_sheet, write_recurring_sheet,
)
from .rules import load_rules


# ---------------------------------------------------------------------------
# Analysis sheets: keep their references to the Data sheet on the data
# ---------------------------------------------------------------------------

# Written on the first run, then kept (see generate_ods).
ANALYSIS_SHEETS = (
    "Dashboard", "Monthly Summary", "Monthly Trend", "Category Breakdown",
    "Subcategory Breakdown", "Tags", "Recurring Merchants",
)

# A range of Data-sheet rows that starts at the first data row:
# [.Data.F2:.Data.F6] as ods_sheets writes it, [Data.F2:Data.F6] or
# [$Data.$F$2:.$F$6] once LibreOffice has saved the file. Group 1 is
# everything up to the number of the last row.
_DATA_ROWS_RANGE = re.compile(
    r"(\[\.?\$?(?:Data|'Data')\.\$?[A-Z]+\$?2:"
    r"(?:\.?\$?(?:Data|'Data'))?\.\$?[A-Z]+\$?)\d+(?=\])"
)


def retarget_data_ranges(formula: str, last_row: int) -> str:
    """Make every Data-sheet range that starts at row 2 end at `last_row`.

    The Data sheet is rebuilt on every run, newest transaction first, while
    the formulas of the analysis sheets are written once: a range left at
    the row count of the first run stops covering the oldest transactions
    as soon as new ones are imported.
    """
    return _DATA_ROWS_RANGE.sub(
        lambda m: f"{m.group(1)}{max(last_row, 2)}", formula
    )


# ---------------------------------------------------------------------------
# Sync-back: read manual edits from ODS into SQLite
# ---------------------------------------------------------------------------


def _load_description_notes(
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> dict[str, str]:
    """Load merchant notes from description-notes.csv.

    Returns a dict mapping description_clean -> merchant_note.
    """
    if not desc_notes_path.exists():
        return {}
    notes: dict[str, str] = {}
    with desc_notes_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("description_clean", "").strip()
            val = row.get("merchant_note", "").strip()
            if key and val:
                notes[key] = val
    return notes


def _save_description_notes(
    notes: dict[str, str],
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> None:
    """Write merchant notes dict to description-notes.csv."""
    with desc_notes_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["description_clean", "merchant_note"])
        for key in sorted(notes):
            writer.writerow([key, notes[key]])


def _load_baseline(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """Per-transaction (category, subcategory, notes) last seen in the ODS."""
    rows = conn.execute(
        "SELECT transaction_id, category, subcategory, notes FROM ods_baseline"
    ).fetchall()
    return {tid: (cat, sub, notes) for tid, cat, sub, notes in rows}


def _save_baseline(
    conn: sqlite3.Connection, values: list[tuple[str, str, str, str]]
) -> None:
    """Replace the baseline with (transaction_id, category, subcategory, notes)."""
    conn.execute("DELETE FROM ods_baseline")
    conn.executemany("INSERT OR REPLACE INTO ods_baseline VALUES (?, ?, ?, ?)", values)
    conn.commit()


def _is_missing_or_empty(ods_path: Path) -> bool:
    """No report yet. An empty file is the placeholder created with `touch`
    so Docker can bind-mount it (see DEPLOYMENT.md)."""
    return not ods_path.exists() or ods_path.stat().st_size == 0


def sync_from_ods(
    db_path: Path,
    ods_path: Path,
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> int:
    """
    Read the Data sheet from an existing ODS file and sync:
    1. category/subcategory/notes edits back into SQLite (per-transaction)
    2. merchant notes back to description-notes.csv (per-merchant)

    A cell only counts as an edit if it differs from the value the ODS held
    when it was last generated or synced, so a stale ODS never reverts newer
    changes in the DB (rules re-applied from the GUI, xlsx-only reports).

    Returns the number of transactions updated in SQLite.
    """
    if _is_missing_or_empty(ods_path):
        return 0

    try:
        from odf.opendocument import load as load_ods
        from odf.table import Table, TableRow, TableCell
        from odf.text import P
    except ImportError:
        return 0

    doc = load_ods(str(ods_path))

    # Find the Data sheet
    data_sheet = None
    for sheet in doc.spreadsheet.getElementsByType(Table):
        if sheet.getAttribute("name") == "Data":
            data_sheet = sheet
            break
    if data_sheet is None:
        return 0

    rows = data_sheet.getElementsByType(TableRow)
    if len(rows) < 2:
        return 0

    max_col = max(COL_TRANSACTION_ID, COL_NOTES, COL_MERCHANT_NOTE)

    # Parse each data row (skip header at index 0)
    # Each edit: (tx_id, category, subcategory, notes)
    edits: list[tuple[str, str, str, str]] = []
    # Merchant notes: description_clean -> merchant_note
    merchant_notes_from_ods: dict[str, str] = {}
    # Every row of a merchant shows its note; a row whose note differs from
    # the stored one is the edit, whichever row the user typed it on.
    existing_notes = _load_description_notes(desc_notes_path)

    for row in rows[1:]:
        cells = row.getElementsByType(TableCell)

        # Handle repeated cells (ODF optimization for empty cells)
        expanded: list[str] = []
        for cell in cells:
            repeat = cell.getAttribute("numbercolumnsrepeated")
            n = int(repeat) if repeat else 1
            # Extract text content from the cell
            text_parts = []
            for p_elem in cell.getElementsByType(P):
                # Get all text content from the P element
                text = ""
                for child in p_elem.childNodes:
                    if hasattr(child, "data"):
                        text += child.data
                    elif hasattr(child, "__str__"):
                        text += str(child)
                text_parts.append(text)
            cell_text = "".join(text_parts).strip()
            expanded.extend([cell_text] * n)
            # Stop expanding if we have enough columns
            if len(expanded) > max_col:
                break

        if len(expanded) <= COL_TRANSACTION_ID:
            continue

        tx_id = expanded[COL_TRANSACTION_ID].strip()
        category = expanded[COL_CATEGORY].strip()
        subcategory = expanded[COL_SUBCATEGORY].strip()
        notes = expanded[COL_NOTES].strip() if len(expanded) > COL_NOTES else ""

        # Read merchant note + description_clean for the merchant notes sync
        desc_clean = (
            expanded[COL_DESCRIPTION_CLEAN].strip()
            if len(expanded) > COL_DESCRIPTION_CLEAN else ""
        )
        merchant_note = (
            expanded[COL_MERCHANT_NOTE].strip()
            if len(expanded) > COL_MERCHANT_NOTE else ""
        )
        if (desc_clean and merchant_note
                and merchant_note != existing_notes.get(desc_clean, "")):
            merchant_notes_from_ods[desc_clean] = merchant_note

        if tx_id:
            edits.append((tx_id, category, subcategory, notes))

    # Sync merchant notes to description-notes.csv
    if merchant_notes_from_ods:
        existing_notes.update(merchant_notes_from_ods)
        _save_description_notes(existing_notes, desc_notes_path)

    if not edits:
        return 0

    # Write per-transaction edits back to SQLite
    conn = sqlite3.connect(str(db_path))
    updated = 0
    try:
        migrate_schema(conn)
        baseline = _load_baseline(conn)
        for tx_id, category, subcategory, notes in edits:
            if baseline.get(tx_id) == (category, subcategory, notes):
                continue

            existing = conn.execute(
                "SELECT category, subcategory, notes FROM transactions "
                "WHERE transaction_id = ?",
                (tx_id,),
            ).fetchone()

            if existing is None:
                continue

            existing_cat = (existing[0] or "").strip()
            existing_subcat = (existing[1] or "").strip()
            existing_notes = (existing[2] or "").strip()

            if (category != existing_cat or subcategory != existing_subcat
                    or notes != existing_notes):
                conn.execute(
                    "UPDATE transactions SET category = ?, subcategory = ?, notes = ? "
                    "WHERE transaction_id = ?",
                    (category, subcategory, notes, tx_id),
                )
                if category != existing_cat or subcategory != existing_subcat:
                    # Clearing the category hands the row back to the rules
                    conn.execute(
                        "UPDATE transactions SET category_source = ? "
                        "WHERE transaction_id = ?",
                        ("manual" if category else None, tx_id),
                    )
                updated += 1

        conn.commit()
        _save_baseline(conn, edits)
    finally:
        conn.close()

    return updated


def generate_ods(
    db_path: Path,
    rules_path: Path,
    ods_path: Path,
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> None:
    """Generate (or update) an ODS expense report."""
    try:
        from odf.opendocument import OpenDocumentSpreadsheet, load as load_ods
        from odf.table import Table, TableCell
    except ImportError:
        print("Error: odfpy is required for ODS generation.")
        print("Install it with: pip install odfpy")
        return

    conn = sqlite3.connect(str(db_path))
    try:
        migrate_schema(conn)
        transactions = fetch_all_transactions(conn)
    finally:
        conn.close()

    rules = load_rules(rules_path) if rules_path.exists() else []

    # Inject merchant notes into each transaction dict
    desc_notes = _load_description_notes(desc_notes_path)
    for tx in transactions:
        tx["merchant_note"] = desc_notes.get(tx.get("description_clean", ""), "")

    is_first_run = _is_missing_or_empty(ods_path)

    if is_first_run:
        doc = OpenDocumentSpreadsheet()
        setup_styles(doc)
        write_intro_sheet(doc)
        write_data_sheet(doc, transactions)
        write_rules_sheet(doc, rules)
        write_dashboard_sheet(doc, transactions)
        write_monthly_summary_sheet(doc, transactions)
        write_monthly_trend_sheet(doc, transactions)
        write_category_breakdown_sheet(doc, transactions)
        write_subcategory_breakdown_sheet(doc, transactions)
        write_tags_sheet(doc, transactions)
        write_recurring_sheet(doc, transactions)
    else:
        doc = load_ods(str(ods_path))
        setup_styles(doc)

        # Remove existing Data and Rules sheets, preserve everything else
        sheets_to_remove = []
        for sheet in doc.spreadsheet.getElementsByType(Table):
            name = sheet.getAttribute("name")
            if name in ("Data", "Rules", "Intro"):
                sheets_to_remove.append(sheet)
        for sheet in sheets_to_remove:
            doc.spreadsheet.removeChild(sheet)

        # Re-add Intro, Data, and Rules at the beginning
        existing_sheets = doc.spreadsheet.getElementsByType(Table)
        first_existing = existing_sheets[0] if existing_sheets else None

        intro_sheet = build_intro_sheet(doc)
        data_sheet = build_data_sheet(doc, transactions)
        rules_sheet = build_rules_sheet(doc, rules)

        if first_existing:
            doc.spreadsheet.insertBefore(rules_sheet, first_existing)
            doc.spreadsheet.insertBefore(data_sheet, rules_sheet)
            doc.spreadsheet.insertBefore(intro_sheet, data_sheet)
        else:
            doc.spreadsheet.addElement(intro_sheet)
            doc.spreadsheet.addElement(data_sheet)
            doc.spreadsheet.addElement(rules_sheet)

        # The kept analysis sheets must go on covering every Data row
        last_row = len(transactions) + 1  # row 1 is the header
        for sheet in existing_sheets:
            if sheet.getAttribute("name") not in ANALYSIS_SHEETS:
                continue
            for cell in sheet.getElementsByType(TableCell):
                formula = cell.getAttribute("formula")
                if formula:
                    retargeted = retarget_data_ranges(formula, last_row)
                    if retargeted != formula:
                        cell.setAttribute("formula", retargeted)

    ods_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(ods_path))

    conn = sqlite3.connect(str(db_path))
    try:
        _save_baseline(
            conn,
            [
                (
                    tx["transaction_id"],
                    (tx.get("category") or "").strip(),
                    (tx.get("subcategory") or "").strip(),
                    (tx.get("notes") or "").strip(),
                )
                for tx in transactions
            ],
        )
    finally:
        conn.close()
    print(f"ODS report saved to {ods_path}")
    if is_first_run:
        print("  (First run: generated starter analysis sheets with formulas)")
    else:
        print("  (Updated Data and Rules sheets; preserved all other sheets)")

    categorized = sum(1 for tx in transactions if tx.get("category"))
    total = len(transactions)
    print(
        f"  {total} transactions total, {categorized} categorized, "
        f"{total - categorized} uncategorized"
    )
