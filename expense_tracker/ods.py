"""
ODS report generation.

Hybrid approach:
- Always replaces the "Data" and "Rules" sheets.
- On first run, generates starter analysis sheets with real spreadsheet formulas.
- On subsequent runs, preserves all user-modified sheets.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import csv

from .constants import (
    DATA_COLUMNS, DATA_HEADERS, DEFAULT_DESC_NOTES,
    COL_DESCRIPTION_CLEAN, COL_CATEGORY, COL_SUBCATEGORY,
    COL_TRANSACTION_ID, COL_NOTES, COL_MERCHANT_NOTE,
)
from .db import migrate_schema, fetch_all_transactions
from .rules import load_rules


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


def sync_from_ods(
    db_path: Path,
    ods_path: Path,
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> int:
    """
    Read the Data sheet from an existing ODS file and sync:
    1. category/subcategory/notes edits back into SQLite (per-transaction)
    2. merchant notes back to description-notes.csv (per-merchant)

    Returns the number of transactions updated in SQLite.
    """
    if not ods_path.exists():
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
        if desc_clean and merchant_note:
            merchant_notes_from_ods[desc_clean] = merchant_note

        if tx_id:
            edits.append((tx_id, category, subcategory, notes))

    # Sync merchant notes to description-notes.csv
    if merchant_notes_from_ods:
        existing_notes = _load_description_notes(desc_notes_path)
        existing_notes.update(merchant_notes_from_ods)
        _save_description_notes(existing_notes, desc_notes_path)

    if not edits:
        return 0

    # Write per-transaction edits back to SQLite
    conn = sqlite3.connect(str(db_path))
    updated = 0
    try:
        migrate_schema(conn)
        for tx_id, category, subcategory, notes in edits:
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
                updated += 1

        conn.commit()
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
        from odf.table import Table
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

    is_first_run = not ods_path.exists()

    if is_first_run:
        doc = OpenDocumentSpreadsheet()
        _setup_styles(doc)
        _write_intro_sheet(doc)
        _write_data_sheet(doc, transactions)
        _write_rules_sheet(doc, rules)
        _write_dashboard_sheet(doc, transactions)
        _write_monthly_summary_sheet(doc, transactions)
        _write_monthly_trend_sheet(doc, transactions)
        _write_category_breakdown_sheet(doc, transactions)
        _write_subcategory_breakdown_sheet(doc, transactions)
        _write_tags_sheet(doc, transactions)
    else:
        doc = load_ods(str(ods_path))
        _setup_styles(doc)

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

        intro_sheet = _build_intro_sheet(doc)
        data_sheet = _build_data_sheet(doc, transactions)
        rules_sheet = _build_rules_sheet(doc, rules)

        if first_existing:
            doc.spreadsheet.insertBefore(rules_sheet, first_existing)
            doc.spreadsheet.insertBefore(data_sheet, rules_sheet)
            doc.spreadsheet.insertBefore(intro_sheet, data_sheet)
        else:
            doc.spreadsheet.addElement(intro_sheet)
            doc.spreadsheet.addElement(data_sheet)
            doc.spreadsheet.addElement(rules_sheet)

    ods_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(ods_path))
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


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _setup_styles(doc):
    """Set up reusable styles for the ODS document."""
    from odf.style import Style, TableCellProperties, TextProperties
    from odf.style import TableColumnProperties

    styles = [
        ("header", "table-cell", {
            "cell": {"backgroundcolor": "#2c3e50", "padding": "0.08in"},
            "text": {"color": "#ffffff", "fontweight": "bold", "fontsize": "10pt"},
        }),
        ("categorized", "table-cell", {
            "cell": {"backgroundcolor": "#d5f5e3", "padding": "0.04in"},
            "text": {"fontsize": "9pt"},
        }),
        ("uncategorized", "table-cell", {
            "cell": {"backgroundcolor": "#fef9e7", "padding": "0.04in"},
            "text": {"fontsize": "9pt"},
        }),
        ("normal", "table-cell", {
            "cell": {"padding": "0.04in"},
            "text": {"fontsize": "9pt"},
        }),
        ("section_header", "table-cell", {
            "cell": {"backgroundcolor": "#34495e", "padding": "0.06in"},
            "text": {"color": "#ffffff", "fontweight": "bold", "fontsize": "11pt"},
        }),
    ]

    for name, family, props in styles:
        style = Style(name=name, family=family)
        if "cell" in props:
            style.addElement(TableCellProperties(**props["cell"]))
        if "text" in props:
            style.addElement(TextProperties(**props["text"]))
        doc.automaticstyles.addElement(style)

    col_widths = [
        ("col_currency", "1.2in"),
        ("col_wide", "2.5in"),
        ("col_narrow", "0.9in"),
        ("col_medium", "1.4in"),
    ]
    for name, width in col_widths:
        style = Style(name=name, family="table-column")
        style.addElement(TableColumnProperties(columnwidth=width))
        doc.automaticstyles.addElement(style)


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------


def _make_cell(value, style_name="normal", value_type=None, formula=None):
    """Create a table cell with proper typing."""
    from odf.table import TableCell
    from odf.text import P

    attrs = {}
    if style_name:
        attrs["stylename"] = style_name

    if formula:
        attrs["formula"] = formula
        attrs["valuetype"] = value_type or "float"
        cell = TableCell(**attrs)
        cell.addElement(P(text=str(value) if value else ""))
        return cell

    if value is None or value == "":
        cell = TableCell(**attrs)
        cell.addElement(P(text=""))
        return cell

    if isinstance(value, (int, float)):
        attrs["valuetype"] = "float"
        attrs["value"] = str(value)
        cell = TableCell(**attrs)
        cell.addElement(
            P(text=f"{value:.2f}" if isinstance(value, float) else str(value))
        )
    else:
        attrs["valuetype"] = "string"
        cell = TableCell(**attrs)
        cell.addElement(P(text=str(value)))

    return cell


def _make_header_row(headers):
    """Create a header row with header styling."""
    from odf.table import TableRow

    row = TableRow()
    for h in headers:
        row.addElement(_make_cell(h, style_name="header"))
    return row


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------


def _build_data_sheet(doc, transactions):
    """Build the Data sheet table element."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Data")

    col_styles = [
        "col_narrow", "col_narrow", "col_narrow",    # Date, Month, Day
        "col_wide", "col_wide",                       # Desc raw, Desc clean
        "col_currency", "col_narrow",                 # Amount, Direction
        "col_medium", "col_medium", "col_medium",     # Category, Subcategory, PayType
        "col_narrow", "col_narrow", "col_medium",     # Who, Card, Status
        "col_medium", "col_currency", "col_medium",   # Account, Balance, Source
        "col_medium",                                 # ID (transaction_id)
        "col_wide",                                   # Notes
        "col_wide",                                   # Merchant Note
    ]
    for cs in col_styles:
        table.addElement(TableColumn(stylename=cs))

    table.addElement(_make_header_row(DATA_HEADERS))

    for tx in transactions:
        row = TableRow()
        style = "categorized" if tx.get("category") else "uncategorized"
        for col in DATA_COLUMNS:
            val = tx.get(col, "") or ""
            row.addElement(_make_cell(val, style_name=style))
        table.addElement(row)

    return table


def _write_data_sheet(doc, transactions):
    doc.spreadsheet.addElement(_build_data_sheet(doc, transactions))


def _build_rules_sheet(doc, rules):
    """Build the Rules sheet table element."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Rules")

    headers = ["Pattern", "Match Field", "Category", "Subcategory", "Payment Type"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))

    table.addElement(_make_header_row(headers))

    for rule in rules:
        row = TableRow()
        for key in ("pattern", "match_field", "category", "subcategory", "payment_type"):
            row.addElement(_make_cell(rule[key], style_name="normal"))
        table.addElement(row)

    return table


def _write_rules_sheet(doc, rules):
    doc.spreadsheet.addElement(_build_rules_sheet(doc, rules))


def _write_monthly_summary_sheet(doc, transactions):
    """Generate a Monthly Summary sheet with spreadsheet formulas."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Monthly Summary")

    months = sorted(set(tx["month"] for tx in transactions if tx["month"]))
    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and tx["direction"] == "out"
    ))
    if not categories:
        categories = ["(no categories yet)"]

    headers = ["Month"] + categories + ["Total"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(_make_header_row(headers))

    n = len(transactions) + 1  # last data row (1-indexed)

    for i, month in enumerate(months):
        row_num = i + 2
        row = TableRow()
        row.addElement(_make_cell(month, style_name="normal"))

        for cat in categories:
            formula = (
                f'of:=SUMPRODUCT('
                f'([.Data.B2:.Data.B{n}]="{month}")'
                f'*([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.G2:.Data.G{n}]="out")'
                f'*[.Data.F2:.Data.F{n}])'
            )
            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=formula))

        if categories:
            last_col = chr(ord("B") + len(categories) - 1)
            row.addElement(_make_cell(
                "", style_name="normal", value_type="float",
                formula=f"of:=SUM([.B{row_num}:.{last_col}{row_num}])",
            ))

        table.addElement(row)

    # Total row
    if months:
        total_row = TableRow()
        total_row.addElement(_make_cell("TOTAL", style_name="header"))
        last_data_row = len(months) + 1
        for col_idx in range(len(categories)):
            col_letter = chr(ord("B") + col_idx)
            total_row.addElement(_make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col_letter}2:.{col_letter}{last_data_row}])",
            ))
        gt_col = chr(ord("B") + len(categories))
        total_row.addElement(_make_cell(
            "", style_name="header", value_type="float",
            formula=f"of:=SUM([.{gt_col}2:.{gt_col}{last_data_row}])",
        ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def _write_category_breakdown_sheet(doc, transactions):
    """Generate a Category Breakdown sheet with formulas."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Category Breakdown")

    headers = ["Category", "Total Spent", "% of Total", "Avg / Month", "# Transactions"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(_make_header_row(headers))

    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and tx["direction"] == "out"
    ))
    n_months = len(set(tx["month"] for tx in transactions if tx["month"])) or 1
    n = len(transactions) + 1

    for i, cat in enumerate(categories):
        row_num = i + 2
        row = TableRow()

        row.addElement(_make_cell(cat, style_name="normal"))

        # Total Spent
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])'
        )))

        # % of Total
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=[.B{row_num}]/'
            f'SUMPRODUCT(([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])*100'
        )))

        # Avg / Month
        row.addElement(_make_cell("", style_name="normal", value_type="float",
                                  formula=f"of:=[.B{row_num}]/{n_months}"))

        # # Transactions
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out"))'
        )))

        table.addElement(row)

    # Total row
    if categories:
        total_row = TableRow()
        total_row.addElement(_make_cell("TOTAL", style_name="header"))
        last = len(categories) + 1
        for col in ("B", "C", "D", "E"):
            total_row.addElement(_make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def _write_dashboard_sheet(doc, transactions):
    """Generate a Dashboard sheet with key summary figures."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Dashboard")
    table.addElement(TableColumn(stylename="col_wide"))
    table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(TableColumn(stylename="col_medium"))

    n = len(transactions) + 1  # last data row (1-indexed, row 1 = header)
    n_months = len(set(tx["month"] for tx in transactions if tx["month"])) or 1

    # --- Title ---
    row = TableRow()
    row.addElement(_make_cell("DASHBOARD", style_name="section_header"))
    row.addElement(_make_cell("", style_name="section_header"))
    row.addElement(_make_cell("", style_name="section_header"))
    table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- All-time summary ---
    table.addElement(_make_header_row(["All-Time Summary", "Amount", ""]))

    # Total Income
    row = TableRow()
    row.addElement(_make_cell("Total Income", style_name="normal"))
    row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=SUMPRODUCT(([.Data.G2:.Data.G{n}]="in")*[.Data.F2:.Data.F{n}])'
    )))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    # Total Expenses
    row = TableRow()
    row.addElement(_make_cell("Total Expenses", style_name="normal"))
    row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=SUMPRODUCT(([.Data.G2:.Data.G{n}]="out")*[.Data.F2:.Data.F{n}])'
    )))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    # Net Balance
    row = TableRow()
    row.addElement(_make_cell("Net Balance (Income - Expenses)", style_name="normal"))
    row.addElement(_make_cell("", style_name="normal", value_type="float",
                              formula="of:=[.B4]-[.B5]"))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    # Avg Monthly Spend
    row = TableRow()
    row.addElement(_make_cell("Average Monthly Spend", style_name="normal"))
    row.addElement(_make_cell("", style_name="normal", value_type="float",
                              formula=f"of:=[.B5]/{n_months}"))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    # Total Transactions
    row = TableRow()
    row.addElement(_make_cell("Total Transactions", style_name="normal"))
    row.addElement(_make_cell(len(transactions), style_name="normal"))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    # Uncategorized Count
    row = TableRow()
    row.addElement(_make_cell("Uncategorized Transactions", style_name="normal"))
    row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=COUNTIF([.Data.M2:.Data.M{n}],"uncategorized")'
    )))
    row.addElement(_make_cell("", style_name="normal"))
    table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- Top categories ---
    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and tx["direction"] == "out"
    ))

    table.addElement(_make_header_row(["Top Categories", "Total Spent", "# Transactions"]))

    for cat in categories[:10]:  # top 10 max
        row = TableRow()
        row.addElement(_make_cell(cat, style_name="normal"))
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])'
        )))
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out"))'
        )))
        table.addElement(row)

    doc.spreadsheet.addElement(table)


def _write_monthly_trend_sheet(doc, transactions):
    """Generate a Monthly Trend sheet (income vs expenses vs net over time)."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Monthly Trend")

    headers = ["Month", "Income", "Expenses", "Net", "Running Balance"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(_make_header_row(headers))

    months = sorted(set(tx["month"] for tx in transactions if tx["month"]))
    n = len(transactions) + 1

    for i, month in enumerate(months):
        row_num = i + 2
        row = TableRow()

        row.addElement(_make_cell(month, style_name="normal"))

        # Income
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.B2:.Data.B{n}]="{month}")'
            f'*([.Data.G2:.Data.G{n}]="in")'
            f'*[.Data.F2:.Data.F{n}])'
        )))

        # Expenses
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.B2:.Data.B{n}]="{month}")'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])'
        )))

        # Net (Income - Expenses)
        row.addElement(_make_cell("", style_name="normal", value_type="float",
                                  formula=f"of:=[.B{row_num}]-[.C{row_num}]"))

        # Running Balance (cumulative net)
        if i == 0:
            formula = f"of:=[.D{row_num}]"
        else:
            formula = f"of:=[.E{row_num - 1}]+[.D{row_num}]"
        row.addElement(_make_cell("", style_name="normal", value_type="float",
                                  formula=formula))

        table.addElement(row)

    # Total row
    if months:
        total_row = TableRow()
        total_row.addElement(_make_cell("TOTAL", style_name="header"))
        last = len(months) + 1
        for col in ("B", "C", "D"):
            total_row.addElement(_make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        total_row.addElement(_make_cell("", style_name="header"))  # Running balance N/A
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def _write_subcategory_breakdown_sheet(doc, transactions):
    """Generate a Subcategory Breakdown sheet."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Subcategory Breakdown")

    headers = ["Category", "Subcategory", "Total Spent", "% of Category", "# Transactions"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(_make_header_row(headers))

    # Collect unique (category, subcategory) pairs for outgoing transactions
    pairs = sorted(set(
        (tx["category"], tx.get("subcategory") or "")
        for tx in transactions
        if tx.get("category") and tx["direction"] == "out"
    ))

    n = len(transactions) + 1

    for i, (cat, subcat) in enumerate(pairs):
        row_num = i + 2
        row = TableRow()

        row.addElement(_make_cell(cat, style_name="normal"))
        row.addElement(_make_cell(subcat or "(none)", style_name="normal"))

        if subcat:
            # Total Spent for this category+subcategory
            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="{subcat}")'
                f'*([.Data.G2:.Data.G{n}]="out")'
                f'*[.Data.F2:.Data.F{n}])'
            )))

            # % of Category
            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=[.C{row_num}]/'
                f'SUMPRODUCT(([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.G2:.Data.G{n}]="out")'
                f'*[.Data.F2:.Data.F{n}])*100'
            )))

            # # Transactions
            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="{subcat}")'
                f'*([.Data.G2:.Data.G{n}]="out"))'
            )))
        else:
            # No subcategory -- sum everything in category with empty subcategory
            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="")'
                f'*([.Data.G2:.Data.G{n}]="out")'
                f'*[.Data.F2:.Data.F{n}])'
            )))

            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=[.C{row_num}]/'
                f'SUMPRODUCT(([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.G2:.Data.G{n}]="out")'
                f'*[.Data.F2:.Data.F{n}])*100'
            )))

            row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="")'
                f'*([.Data.G2:.Data.G{n}]="out"))'
            )))

        table.addElement(row)

    # Total row
    if pairs:
        total_row = TableRow()
        total_row.addElement(_make_cell("TOTAL", style_name="header"))
        total_row.addElement(_make_cell("", style_name="header"))
        last = len(pairs) + 1
        for col in ("C", "D", "E"):
            total_row.addElement(_make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def _write_tags_sheet(doc, transactions):
    """Generate a Tags analysis sheet for #tag tracking in Notes / Merchant Note."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Tags")

    for _ in range(4):
        table.addElement(TableColumn(stylename="col_medium"))

    # -- Title --
    title_row = TableRow()
    for _ in range(4):
        title_row.addElement(_make_cell("", style_name="section_header"))
    # Overwrite first cell with title
    title_row.childNodes[0].addElement(
        __import__("odf.text", fromlist=["P"]).P(text="TAGS ANALYSIS")
    )
    table.addElement(title_row)

    table.addElement(TableRow())  # blank

    # -- Instructions --
    for text in [
        "Use #tags in the Notes (R) or Merchant Note (S) columns to tag transactions.",
        "Type your tags below \u2014 formulas count matches and sum amounts automatically.",
        "Examples: #recurring, #reimbursable, #gift, #shared, #splurge",
    ]:
        row = TableRow()
        row.addElement(_make_cell(text, style_name="normal"))
        table.addElement(row)

    table.addElement(TableRow())  # blank

    # -- Header (row 7) --
    table.addElement(_make_header_row(
        ["Tag", "# Transactions", "Total Spent", "% of Total Spend"]
    ))

    n = len(transactions) + 1  # last Data row (1-indexed)

    # Pre-filled example tags + empty formula slots
    tags = ["#recurring", "#reimbursable", "#splurge", "#shared",
            "", "", "", "", "", ""]

    data_start_row = 8  # rows 1-7 are title/instructions/blank/header

    for i, tag in enumerate(tags):
        row_num = data_start_row + i
        row = TableRow()

        # A: Tag
        row.addElement(_make_cell(tag, style_name="normal"))

        # B: # Transactions (unique rows where Notes OR Merchant Note contains the tag)
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF([.A{row_num}]="";"";'
            f'SUMPRODUCT('
            f'(ISNUMBER(SEARCH([.A{row_num}];[.Data.R2:.Data.R{n}]))'
            f'+ISNUMBER(SEARCH([.A{row_num}];[.Data.S2:.Data.S{n}])))'
            f'>0))'
        )))

        # C: Total Spent (outgoing only)
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF([.A{row_num}]="";"";'
            f'SUMPRODUCT('
            f'((ISNUMBER(SEARCH([.A{row_num}];[.Data.R2:.Data.R{n}]))'
            f'+ISNUMBER(SEARCH([.A{row_num}];[.Data.S2:.Data.S{n}])))'
            f'>0)'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}]))'
        )))

        # D: % of Total Spend
        row.addElement(_make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF(OR([.A{row_num}]="";[.C{row_num}]=0);"";'
            f'[.C{row_num}]/'
            f'SUMPRODUCT(([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])*100)'
        )))

        table.addElement(row)

    doc.spreadsheet.addElement(table)


def _build_intro_sheet(doc):
    """Build the Intro sheet table element."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Intro")
    table.addElement(TableColumn(stylename="col_wide"))

    instructions = [
        "EXPENSE TRACKER - How to use",
        "",
        "SHEETS OVERVIEW:",
        "  Intro - This page (auto-generated)",
        "  Data - All transactions (auto-generated, but you CAN edit Category, Subcategory, Notes & Merchant Note!)",
        "  Rules - Active categorization rules (auto-generated, DO NOT EDIT)",
        "  Dashboard - Key summary figures at a glance (YOUR sheet - edit freely!)",
        "  Monthly Summary - Spending by month and category (YOUR sheet - edit freely!)",
        "  Monthly Trend - Income vs expenses over time (YOUR sheet - edit freely!)",
        "  Category Breakdown - Totals per category (YOUR sheet - edit freely!)",
        "  Subcategory Breakdown - Detailed breakdown within categories (YOUR sheet - edit freely!)",
        "  Tags - Track spending by #tags in Notes / Merchant Note (YOUR sheet - edit freely!)",
        "",
        "MANUAL CATEGORIZATION:",
        "  You can manually edit the Category, Subcategory, Notes, and Merchant Note columns in the Data sheet.",
        "  When you next run ./run.sh, your manual edits are synced back to the database",
        "  BEFORE the sheet is regenerated -- so they persist across runs!",
        "  (This is great for one-off expenses that don't match any rule.)",
        "",
        "#TAGS:",
        "  Add #tags anywhere in the Notes (R) or Merchant Note (S) columns, e.g.:",
        "    #recurring  #reimbursable  #gift  #shared  #splurge",
        "  The Tags sheet automatically counts and sums tagged transactions.",
        "  Merchant Notes apply the same tags to ALL transactions from that merchant.",
        "",
        "EVERYDAY WORKFLOW:",
        "  1. Download bank CSV from UTF-16 CSV -> save to raw/ folder",
        "  2. Double-click ./run.sh (or run it from Terminal)",
        "  3. Open this file - your analysis sheets are preserved with fresh data!",
        "",
        "MANAGING RULES & CARDS (from the command line):",
        "  python bank_ingest.py rules                       # list all rules",
        "  python bank_ingest.py rules add PATTERN CATEGORY   # add a rule",
        "  python bank_ingest.py rules remove PATTERN         # remove a rule",
        "  python bank_ingest.py cards                        # list card holders",
        "  python bank_ingest.py cards add 1234 Name          # add a card",
        "  python bank_ingest.py cards remove 1234            # remove a card",
        "",
        "YOUR CUSTOM SHEETS:",
        "  You can add new sheets, columns, formulas, charts - anything!",
        "  Reference the Data sheet for formulas, e.g.:",
        '    =SUMPRODUCT((Data.B:B="2025-12")*(Data.H:H="Groceries")*(Data.G:G="out")*Data.F:F)',
        "  Only the Intro, Data, and Rules sheets are replaced on each run.",
        "  All other sheets (including the starter analysis sheets) are yours to customize.",
        "",
        "DATA SHEET COLUMNS:",
        "  A: Date        B: Month       C: Day        D: Description (raw)",
        "  E: Description  F: Amount      G: Direction  H: Category",
        "  I: Subcategory  J: Payment Type K: Who       L: Card",
        "  M: Status       N: Account     O: Balance    P: Source File",
        "  Q: ID (transaction ID - do not edit, used for sync)",
        "  R: Notes (free text - your annotations, synced back to DB)",
        "  S: Merchant Note (shared note for all transactions from same merchant)",
    ]

    for line in instructions:
        row = TableRow()
        style = "section_header" if line.startswith("EXPENSE TRACKER") else "normal"
        row.addElement(_make_cell(line, style_name=style))
        table.addElement(row)

    return table


def _write_intro_sheet(doc):
    doc.spreadsheet.addElement(_build_intro_sheet(doc))
