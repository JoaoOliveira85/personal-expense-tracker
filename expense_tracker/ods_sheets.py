"""
ODS sheet builders — styles, cell helpers, and individual sheet generators.

Split from ods.py for maintainability. Each _write_* function adds a sheet
to the document; each _build_* function returns a Table element.
"""
from __future__ import annotations

from collections import defaultdict

from .constants import (
    DATA_COLUMNS,
    DATA_HEADERS,
    NON_SPENDING_CATEGORIES,
    SAVINGS_CATEGORIES,
)
from .db import counts_as_spending, is_savings


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# SUMPRODUCT factors for spending net of refunds (see db.is_refund)
# ---------------------------------------------------------------------------


def _not_in(n: int, categories: tuple[str, ...]) -> str:
    """1 for rows whose category is none of `categories`, 0 otherwise."""
    return "".join(f'*([.Data.H2:.Data.H{n}]<>"{c}")' for c in categories)


def _refund(n: int) -> str:
    """1 for refund rows, 0 otherwise."""
    return (
        f'([.Data.G2:.Data.G{n}]="in")*([.Data.H2:.Data.H{n}]<>"")'
        + _not_in(n, NON_SPENDING_CATEGORIES + SAVINGS_CATEGORIES)
    )


def _spend(n: int) -> str:
    """+amount for outgoing non-savings rows, -amount for refunds, else 0."""
    return (
        f'(([.Data.G2:.Data.G{n}]="out"){_not_in(n, SAVINGS_CATEGORIES)}'
        f"-{_refund(n)})*[.Data.F2:.Data.F{n}]"
    )


def _income(n: int) -> str:
    """Amount of incoming rows that are neither refunds nor savings."""
    return (
        f'(([.Data.G2:.Data.G{n}]="in"){_not_in(n, SAVINGS_CATEGORIES)}'
        f"-{_refund(n)})*[.Data.F2:.Data.F{n}]"
    )


def setup_styles(doc):
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


def make_cell(value, style_name="normal", value_type=None, formula=None):
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


def make_header_row(headers):
    """Create a header row with header styling."""
    from odf.table import TableRow

    row = TableRow()
    for h in headers:
        row.addElement(make_cell(h, style_name="header"))
    return row


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------


def build_data_sheet(doc, transactions):
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

    table.addElement(make_header_row(DATA_HEADERS))

    for tx in transactions:
        row = TableRow()
        style = "categorized" if tx.get("category") else "uncategorized"
        for col in DATA_COLUMNS:
            val = tx.get(col, "") or ""
            row.addElement(make_cell(val, style_name=style))
        table.addElement(row)

    return table


def write_data_sheet(doc, transactions):
    doc.spreadsheet.addElement(build_data_sheet(doc, transactions))


def build_rules_sheet(doc, rules):
    """Build the Rules sheet table element."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Rules")

    headers = ["Pattern", "Match Field", "Category", "Subcategory", "Payment Type"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))

    table.addElement(make_header_row(headers))

    for rule in rules:
        row = TableRow()
        for key in ("pattern", "match_field", "category", "subcategory", "payment_type"):
            row.addElement(make_cell(rule[key], style_name="normal"))
        table.addElement(row)

    return table


def write_rules_sheet(doc, rules):
    doc.spreadsheet.addElement(build_rules_sheet(doc, rules))


def write_monthly_summary_sheet(doc, transactions):
    """Generate a Monthly Summary sheet with spreadsheet formulas."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Monthly Summary")

    months = sorted(set(tx["month"] for tx in transactions if tx["month"]))
    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and counts_as_spending(tx)
    ))
    if not categories:
        categories = ["(no categories yet)"]

    headers = ["Month"] + categories + ["Total"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(make_header_row(headers))

    n = len(transactions) + 1  # last data row (1-indexed)

    for i, month in enumerate(months):
        row_num = i + 2
        row = TableRow()
        row.addElement(make_cell(month, style_name="normal"))

        for cat in categories:
            formula = (
                f'of:=SUMPRODUCT('
                f'([.Data.B2:.Data.B{n}]="{month}")'
                f'*([.Data.H2:.Data.H{n}]="{cat}")'
                f'*{_spend(n)})'
            )
            row.addElement(make_cell("", style_name="normal", value_type="float", formula=formula))

        if categories:
            last_col = chr(ord("B") + len(categories) - 1)
            row.addElement(make_cell(
                "", style_name="normal", value_type="float",
                formula=f"of:=SUM([.B{row_num}:.{last_col}{row_num}])",
            ))

        table.addElement(row)

    # Total row
    if months:
        total_row = TableRow()
        total_row.addElement(make_cell("TOTAL", style_name="header"))
        last_data_row = len(months) + 1
        for col_idx in range(len(categories)):
            col_letter = chr(ord("B") + col_idx)
            total_row.addElement(make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col_letter}2:.{col_letter}{last_data_row}])",
            ))
        gt_col = chr(ord("B") + len(categories))
        total_row.addElement(make_cell(
            "", style_name="header", value_type="float",
            formula=f"of:=SUM([.{gt_col}2:.{gt_col}{last_data_row}])",
        ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def write_category_breakdown_sheet(doc, transactions):
    """Generate a Category Breakdown sheet with formulas."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Category Breakdown")

    headers = ["Category", "Total Spent", "% of Total", "Avg / Month", "# Transactions"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(make_header_row(headers))

    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and counts_as_spending(tx)
    ))
    n_months = len(set(tx["month"] for tx in transactions if tx["month"])) or 1
    n = len(transactions) + 1

    for i, cat in enumerate(categories):
        row_num = i + 2
        row = TableRow()

        row.addElement(make_cell(cat, style_name="normal"))

        # Total Spent
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*{_spend(n)})'
        )))

        # % of Total
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=[.B{row_num}]/'
            f'SUMPRODUCT({_spend(n)})*100'
        )))

        # Avg / Month
        row.addElement(make_cell("", style_name="normal", value_type="float",
                                 formula=f"of:=[.B{row_num}]/{n_months}"))

        # # Transactions
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out"))'
        )))

        table.addElement(row)

    # Total row
    if categories:
        total_row = TableRow()
        total_row.addElement(make_cell("TOTAL", style_name="header"))
        last = len(categories) + 1
        for col in ("B", "C", "D", "E"):
            total_row.addElement(make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def write_dashboard_sheet(doc, transactions):
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
    row.addElement(make_cell("DASHBOARD", style_name="section_header"))
    row.addElement(make_cell("", style_name="section_header"))
    row.addElement(make_cell("", style_name="section_header"))
    table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- All-time summary ---
    table.addElement(make_header_row(["All-Time Summary", "Amount", ""]))

    # Total Income
    row = TableRow()
    row.addElement(make_cell("Total Income", style_name="normal"))
    row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=SUMPRODUCT({_income(n)})'
    )))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    # Total Expenses
    row = TableRow()
    row.addElement(make_cell("Total Expenses", style_name="normal"))
    row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=SUMPRODUCT({_spend(n)})'
    )))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    # Net Balance
    row = TableRow()
    row.addElement(make_cell("Net Balance (Income - Expenses)", style_name="normal"))
    row.addElement(make_cell("", style_name="normal", value_type="float",
                             formula="of:=[.B4]-[.B5]"))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    # Avg Monthly Spend
    row = TableRow()
    row.addElement(make_cell("Average Monthly Spend", style_name="normal"))
    row.addElement(make_cell("", style_name="normal", value_type="float",
                             formula=f"of:=[.B5]/{n_months}"))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    # Total Transactions
    row = TableRow()
    row.addElement(make_cell("Total Transactions", style_name="normal"))
    row.addElement(make_cell(len(transactions), style_name="normal"))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    # Uncategorized Count
    row = TableRow()
    row.addElement(make_cell("Uncategorized Transactions", style_name="normal"))
    row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
        f'of:=COUNTIF([.Data.M2:.Data.M{n}],"uncategorized")'
    )))
    row.addElement(make_cell("", style_name="normal"))
    table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- Top categories ---
    categories = sorted(set(
        tx["category"] for tx in transactions
        if tx.get("category") and counts_as_spending(tx)
    ))

    table.addElement(make_header_row(["Top Categories", "Total Spent", "# Transactions"]))

    for cat in categories[:10]:  # top 10 max
        row = TableRow()
        row.addElement(make_cell(cat, style_name="normal"))
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*{_spend(n)})'
        )))
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.H2:.Data.H{n}]="{cat}")'
            f'*([.Data.G2:.Data.G{n}]="out"))'
        )))
        table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- Top 10 Merchants (by total spend) ---
    merchant_totals: dict[str, float] = {}
    for tx in transactions:
        if tx.get("direction") == "out" and not is_savings(tx):
            desc = tx.get("description_clean") or tx.get("description_raw") or ""
            if desc:
                amt = 0.0
                try:
                    amt = float(tx.get("amount_abs") or 0)
                except (ValueError, TypeError):
                    pass
                merchant_totals[desc] = merchant_totals.get(desc, 0) + amt

    top_merchants = sorted(merchant_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    table.addElement(make_header_row(["Top 10 Merchants", "Total Spent", "# Transactions"]))

    for merchant, total in top_merchants:
        row = TableRow()
        row.addElement(make_cell(merchant, style_name="normal"))
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.E2:.Data.E{n}]="{merchant}")'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])'
        )))
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.E2:.Data.E{n}]="{merchant}")'
            f'*([.Data.G2:.Data.G{n}]="out"))'
        )))
        table.addElement(row)

    table.addElement(TableRow())  # blank row

    # --- Uncategorized Preview (first 15 uncategorized transactions) ---
    uncategorized = [
        tx for tx in transactions
        if not tx.get("category") and tx.get("direction") == "out"
    ]

    table.addElement(make_header_row(["Uncategorized Preview (up to 15)", "Amount", "Date"]))

    for tx in uncategorized[:15]:
        row = TableRow()
        desc = tx.get("description_clean") or tx.get("description_raw") or ""
        row.addElement(make_cell(desc, style_name="uncategorized"))
        row.addElement(make_cell(tx.get("amount_abs", ""), style_name="uncategorized"))
        row.addElement(make_cell(tx.get("date_posted", ""), style_name="uncategorized"))
        table.addElement(row)

    if not uncategorized:
        row = TableRow()
        row.addElement(make_cell("All transactions are categorized!", style_name="normal"))
        table.addElement(row)

    doc.spreadsheet.addElement(table)


def write_monthly_trend_sheet(doc, transactions):
    """Generate a Monthly Trend sheet (income vs expenses vs net over time)."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Monthly Trend")

    headers = ["Month", "Income", "Expenses", "Net", "Running Balance"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(make_header_row(headers))

    months = sorted(set(tx["month"] for tx in transactions if tx["month"]))
    n = len(transactions) + 1

    for i, month in enumerate(months):
        row_num = i + 2
        row = TableRow()

        row.addElement(make_cell(month, style_name="normal"))

        # Income
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.B2:.Data.B{n}]="{month}")'
            f'*{_income(n)})'
        )))

        # Expenses
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=SUMPRODUCT('
            f'([.Data.B2:.Data.B{n}]="{month}")'
            f'*{_spend(n)})'
        )))

        # Net (Income - Expenses)
        row.addElement(make_cell("", style_name="normal", value_type="float",
                                 formula=f"of:=[.B{row_num}]-[.C{row_num}]"))

        # Running Balance (cumulative net)
        if i == 0:
            formula = f"of:=[.D{row_num}]"
        else:
            formula = f"of:=[.E{row_num - 1}]+[.D{row_num}]"
        row.addElement(make_cell("", style_name="normal", value_type="float",
                                 formula=formula))

        table.addElement(row)

    # Total row
    if months:
        total_row = TableRow()
        total_row.addElement(make_cell("TOTAL", style_name="header"))
        last = len(months) + 1
        for col in ("B", "C", "D"):
            total_row.addElement(make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        total_row.addElement(make_cell("", style_name="header"))  # Running balance N/A
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def write_subcategory_breakdown_sheet(doc, transactions):
    """Generate a Subcategory Breakdown sheet."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Subcategory Breakdown")

    headers = ["Category", "Subcategory", "Total Spent", "% of Category", "# Transactions"]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))
    table.addElement(make_header_row(headers))

    # Collect unique (category, subcategory) pairs for outgoing transactions
    pairs = sorted(set(
        (tx["category"], tx.get("subcategory") or "")
        for tx in transactions
        if tx.get("category") and counts_as_spending(tx)
    ))

    n = len(transactions) + 1

    for i, (cat, subcat) in enumerate(pairs):
        row_num = i + 2
        row = TableRow()

        row.addElement(make_cell(cat, style_name="normal"))
        row.addElement(make_cell(subcat or "(none)", style_name="normal"))

        if subcat:
            # Total Spent for this category+subcategory
            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="{subcat}")'
                f'*{_spend(n)})'
            )))

            # % of Category
            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=[.C{row_num}]/'
                f'SUMPRODUCT(([.Data.H2:.Data.H{n}]="{cat}")'
                f'*{_spend(n)})*100'
            )))

            # # Transactions
            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="{subcat}")'
                f'*([.Data.G2:.Data.G{n}]="out"))'
            )))
        else:
            # No subcategory -- sum everything in category with empty subcategory
            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="")'
                f'*{_spend(n)})'
            )))

            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=[.C{row_num}]/'
                f'SUMPRODUCT(([.Data.H2:.Data.H{n}]="{cat}")'
                f'*{_spend(n)})*100'
            )))

            row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
                f'of:=SUMPRODUCT('
                f'([.Data.H2:.Data.H{n}]="{cat}")'
                f'*([.Data.I2:.Data.I{n}]="")'
                f'*([.Data.G2:.Data.G{n}]="out"))'
            )))

        table.addElement(row)

    # Total row
    if pairs:
        total_row = TableRow()
        total_row.addElement(make_cell("TOTAL", style_name="header"))
        total_row.addElement(make_cell("", style_name="header"))
        last = len(pairs) + 1
        for col in ("C", "D", "E"):
            total_row.addElement(make_cell(
                "", style_name="header", value_type="float",
                formula=f"of:=SUM([.{col}2:.{col}{last}])",
            ))
        table.addElement(total_row)

    doc.spreadsheet.addElement(table)


def write_tags_sheet(doc, transactions):
    """Generate a Tags analysis sheet for #tag tracking in Notes / Merchant Note."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Tags")

    for _ in range(4):
        table.addElement(TableColumn(stylename="col_medium"))

    # -- Title --
    title_row = TableRow()
    for _ in range(4):
        title_row.addElement(make_cell("", style_name="section_header"))
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
        row.addElement(make_cell(text, style_name="normal"))
        table.addElement(row)

    table.addElement(TableRow())  # blank

    # -- Header (row 7) --
    table.addElement(make_header_row(
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
        row.addElement(make_cell(tag, style_name="normal"))

        # B: # Transactions (unique rows where Notes OR Merchant Note contains the tag)
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF([.A{row_num}]="";"";'
            f'SUMPRODUCT('
            f'(ISNUMBER(SEARCH([.A{row_num}];[.Data.R2:.Data.R{n}]))'
            f'+ISNUMBER(SEARCH([.A{row_num}];[.Data.S2:.Data.S{n}])))'
            f'>0))'
        )))

        # C: Total Spent (outgoing only)
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF([.A{row_num}]="";"";'
            f'SUMPRODUCT('
            f'((ISNUMBER(SEARCH([.A{row_num}];[.Data.R2:.Data.R{n}]))'
            f'+ISNUMBER(SEARCH([.A{row_num}];[.Data.S2:.Data.S{n}])))'
            f'>0)'
            f'*([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}]))'
        )))

        # D: % of Total Spend
        row.addElement(make_cell("", style_name="normal", value_type="float", formula=(
            f'of:=IF(OR([.A{row_num}]="";[.C{row_num}]=0);"";'
            f'[.C{row_num}]/'
            f'SUMPRODUCT(([.Data.G2:.Data.G{n}]="out")'
            f'*[.Data.F2:.Data.F{n}])*100)'
        )))

        table.addElement(row)

    doc.spreadsheet.addElement(table)


def write_recurring_sheet(doc, transactions):
    """Generate a Recurring Transactions sheet -- merchants appearing 3+ times."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Recurring Merchants")

    headers = [
        "Merchant", "Times", "Total Spent", "Avg Amount",
        "First Seen", "Last Seen", "Category",
    ]
    for _ in headers:
        table.addElement(TableColumn(stylename="col_medium"))

    # -- Title --
    title_row = TableRow()
    for _ in headers:
        title_row.addElement(make_cell("", style_name="section_header"))
    title_row.childNodes[0].addElement(
        __import__("odf.text", fromlist=["P"]).P(text="RECURRING TRANSACTIONS")
    )
    table.addElement(title_row)

    table.addElement(TableRow())  # blank

    for text in [
        "Merchants that appear 3 or more times in your transactions.",
        "Use this to spot subscriptions, regular bills, and habitual spending.",
    ]:
        row = TableRow()
        row.addElement(make_cell(text, style_name="normal"))
        table.addElement(row)

    table.addElement(TableRow())  # blank

    table.addElement(make_header_row(headers))

    # Pre-compute recurring merchants from outgoing transactions
    merchant_data: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "total": 0.0, "dates": [], "category": "",
    })
    for tx in transactions:
        if tx.get("direction") != "out":
            continue
        desc = tx.get("description_clean") or ""
        if not desc:
            continue
        amt = 0.0
        try:
            amt = float(tx.get("amount_abs") or 0)
        except (ValueError, TypeError):
            pass
        md = merchant_data[desc]
        md["count"] += 1
        md["total"] += amt
        if tx.get("date_posted"):
            md["dates"].append(tx["date_posted"])
        if tx.get("category") and not md["category"]:
            md["category"] = tx["category"]

    # Filter to 3+ occurrences, sort by count descending then total descending
    recurring = [
        (desc, info) for desc, info in merchant_data.items()
        if info["count"] >= 3
    ]
    recurring.sort(key=lambda x: (-x[1]["count"], -x[1]["total"]))

    for desc, info in recurring:
        row = TableRow()
        row.addElement(make_cell(desc, style_name="normal"))
        row.addElement(make_cell(info["count"], style_name="normal"))
        row.addElement(make_cell(round(info["total"], 2), style_name="normal"))
        avg = round(info["total"] / info["count"], 2) if info["count"] else 0
        row.addElement(make_cell(avg, style_name="normal"))
        dates_sorted = sorted(info["dates"]) if info["dates"] else []
        row.addElement(make_cell(dates_sorted[0] if dates_sorted else "", style_name="normal"))
        row.addElement(make_cell(dates_sorted[-1] if dates_sorted else "", style_name="normal"))
        row.addElement(make_cell(info["category"], style_name="normal"))
        table.addElement(row)

    if not recurring:
        row = TableRow()
        row.addElement(make_cell(
            "No recurring merchants detected yet (need 3+ occurrences).",
            style_name="normal",
        ))
        table.addElement(row)

    doc.spreadsheet.addElement(table)


def build_intro_sheet(doc):
    """Build the Intro sheet table element."""
    from odf.table import Table, TableColumn, TableRow

    table = Table(name="Intro")
    table.addElement(TableColumn(stylename="col_wide"))

    instructions = [
        "EXPENSE TRACKER - Quick Intro",
        "",
        "For a complete guide (with pictures and examples), open README.html in your browser.",
        "",
        "IMPORTANT - FORMULA RECALCULATION:",
        "  If formulas show as 0 or don't update, press Ctrl+Shift+F9 (or Cmd+Shift+F9 on Mac)",
        "  to force recalculation of all formulas. This is normal for externally-generated files.",
        "",
        "SHEETS OVERVIEW:",
        "  Intro - This page (auto-generated)",
        "  Data - All transactions (auto-generated, but you CAN edit Category, Subcategory, Notes & Merchant Note!)",
        "  Rules - Active categorization rules (auto-generated, DO NOT EDIT)",
        "  Dashboard - Key summary figures + top merchants + uncategorized preview (YOUR sheet)",
        "  Monthly Summary - Spending by month and category (YOUR sheet - edit freely!)",
        "  Monthly Trend - Income vs expenses over time (YOUR sheet - edit freely!)",
        "  Category Breakdown - Totals per category (YOUR sheet - edit freely!)",
        "  Subcategory Breakdown - Detailed breakdown within categories (YOUR sheet - edit freely!)",
        "  Tags - Track spending by #tags in Notes / Merchant Note (YOUR sheet - edit freely!)",
        "  Recurring Merchants - Merchants appearing 3+ times (YOUR sheet - edit freely!)",
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
        "FORMATTING TIPS:",
        "  - Freeze the top row in the Data sheet for easier scrolling (View > Freeze Rows and Columns)",
        "  - Use AutoFilter (Data > AutoFilter) to filter by category, month, or merchant",
        "  - Conditional formatting can highlight large amounts or specific categories",
        "  - Charts can be added to any analysis sheet (Insert > Chart)",
        "  - The Amount column (F) is numeric -- you can use SUM, AVERAGE, etc. directly",
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
        row.addElement(make_cell(line, style_name=style))
        table.addElement(row)

    return table


def write_intro_sheet(doc):
    doc.spreadsheet.addElement(build_intro_sheet(doc))
