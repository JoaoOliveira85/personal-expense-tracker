"""
Monthly PDF report generator.

Produces a single-page A4 summary for a given month, pulling data from the
SQLite database.  The report is designed as a quick bird's-eye view that
highlights where money went so the user can then dive deeper in the ODS.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .constants import DEFAULT_DB, DEFAULT_DESC_NOTES, DEFAULT_REPORTS


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

# Categories considered "essential" for the summary section.
# Users can tweak this list in code if their category names differ.
ESSENTIAL_CATEGORIES = [
    "Housing", "Utilities", "Subscriptions", "Insurance",
    "Health", "Childcare", "Transport",
]


def _fetch_month_transactions(
    conn: sqlite3.Connection,
    month: str,
) -> list[dict]:
    """Fetch all transactions for a given month (YYYY-MM)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            date_posted, description_clean, amount_signed, amount_abs,
            direction, category, subcategory, payment_type, who, notes
        FROM transactions
        WHERE month = ?
        ORDER BY date_posted ASC
        """,
        (month,),
    ).fetchall()
    conn.row_factory = None
    return [dict(r) for r in rows]


def _fetch_historical_category_totals(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, float]]:
    """Fetch per-month, per-category spending totals for all history.

    Returns: { month: { category: total_amount } }
    """
    rows = conn.execute(
        """
        SELECT month, category, SUM(amount_abs) as total
        FROM transactions
        WHERE direction = 'out' AND category IS NOT NULL AND category != ''
        GROUP BY month, category
        """,
    ).fetchall()

    result: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for month, category, total in rows:
        result[month][category] = total
    return dict(result)


def _load_merchant_notes(desc_notes_path: Path) -> dict[str, str]:
    """Load merchant notes for tag detection."""
    import csv

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


def _extract_tags(text: str) -> list[str]:
    """Extract #tags from a text string."""
    if not text:
        return []
    return [w for w in text.split() if w.startswith("#") and len(w) > 1]


def _compute_stats(
    transactions: list[dict],
    merchant_notes: dict[str, str] | None = None,
    prev_month_totals: dict[str, float] | None = None,
    category_averages: dict[str, float] | None = None,
) -> dict:
    """Compute summary statistics for a set of transactions.

    Returns a dict with:
      - total_income, total_expenses, net_balance, tx_count
      - by_category: list of (category, amount, pct, count, prev_diff, avg_diff)
        sorted by amount desc. prev_diff/avg_diff are floats or None.
      - top_merchants: list of (merchant, amount, count) top 10
      - uncategorized_count
      - tag_totals: dict of tag -> (amount, count) for outgoing
      - essentials: list of (category, amount) for essential categories
    """
    if prev_month_totals is None:
        prev_month_totals = {}
    if category_averages is None:
        category_averages = {}
    total_income = 0.0
    total_expenses = 0.0
    cat_amounts: dict[str, float] = defaultdict(float)
    cat_counts: dict[str, int] = defaultdict(int)
    merchant_amounts: dict[str, float] = defaultdict(float)
    merchant_counts: dict[str, int] = defaultdict(int)
    uncategorized = 0
    tag_amounts: dict[str, float] = defaultdict(float)
    tag_counts: dict[str, int] = defaultdict(int)

    if merchant_notes is None:
        merchant_notes = {}

    for tx in transactions:
        amount = tx["amount_abs"]
        direction = tx["direction"]
        category = tx.get("category") or ""
        desc = tx.get("description_clean") or ""

        if direction == "in":
            total_income += amount
        else:
            total_expenses += amount

            # Category breakdown (outgoing only)
            cat_label = category if category else "Uncategorized"
            cat_amounts[cat_label] += amount
            cat_counts[cat_label] += 1

            if not category:
                uncategorized += 1

            # Merchant breakdown (outgoing only)
            merchant_label = desc if desc else "(unknown)"
            merchant_amounts[merchant_label] += amount
            merchant_counts[merchant_label] += 1

        # Tag tracking (all directions but amounts only for outgoing)
        all_tags: set[str] = set()
        for tag in _extract_tags(tx.get("notes") or ""):
            all_tags.add(tag)
        for tag in _extract_tags(merchant_notes.get(desc, "")):
            all_tags.add(tag)
        for tag in all_tags:
            tag_counts[tag] += 1
            if direction == "out":
                tag_amounts[tag] += amount

    # Build sorted lists
    by_category = []
    for cat, amt in sorted(cat_amounts.items(), key=lambda x: -x[1]):
        pct = (amt / total_expenses * 100) if total_expenses > 0 else 0

        # Month-over-month difference
        prev_amt = prev_month_totals.get(cat)
        prev_diff = (amt - prev_amt) if prev_amt is not None else None

        # vs average difference
        avg_amt = category_averages.get(cat)
        avg_diff = (amt - avg_amt) if avg_amt is not None else None

        by_category.append((cat, amt, pct, cat_counts[cat], prev_diff, avg_diff))

    top_merchants = [
        (m, merchant_amounts[m], merchant_counts[m])
        for m in sorted(merchant_amounts, key=lambda x: -merchant_amounts[x])
    ][:10]

    tag_totals = {
        tag: (tag_amounts.get(tag, 0.0), tag_counts[tag])
        for tag in sorted(tag_counts, key=lambda x: -tag_amounts.get(x, 0.0))
    }

    # Essential spending
    essentials = []
    for ess_cat in ESSENTIAL_CATEGORIES:
        if ess_cat in cat_amounts:
            essentials.append((ess_cat, cat_amounts[ess_cat]))

    return {
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net_balance": total_income - total_expenses,
        "tx_count": len(transactions),
        "by_category": by_category,
        "top_merchants": top_merchants,
        "uncategorized_count": uncategorized,
        "tag_totals": tag_totals,
        "essentials": essentials,
    }


def _prev_month(month: str) -> str:
    """Given a YYYY-MM string, return the previous month's YYYY-MM."""
    parts = month.split("-")
    year, m = int(parts[0]), int(parts[1])
    if m == 1:
        return f"{year - 1}-12"
    return f"{year}-{m - 1:02d}"


def previous_month_label() -> str:
    """Return the YYYY-MM label for the previous month."""
    today = date.today()
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    return last_month.strftime("%Y-%m")


def month_display_name(month: str) -> str:
    """Convert YYYY-MM to a nice display name like 'January 2026'."""
    import calendar

    parts = month.split("-")
    year = int(parts[0])
    m = int(parts[1])
    return f"{calendar.month_name[m]} {year}"


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

# Colour palette
_CLR_DARK = (26, 26, 46)       # near-black text
_CLR_ACCENT = (37, 99, 235)    # blue accent
_CLR_MUTED = (100, 100, 120)   # muted text
_CLR_BG_LIGHT = (243, 244, 246)  # light grey backgrounds
_CLR_GREEN = (22, 163, 74)     # positive / income
_CLR_RED = (220, 38, 38)       # negative / expenses
_CLR_WHITE = (255, 255, 255)


def _fmt_eur(amount: float) -> str:
    """Format a euro amount with thousands separator."""
    return f"{amount:,.2f} EUR"


def _setup_fonts(pdf) -> None:
    """Try to register a Unicode font for better character support.

    Falls back silently to the built-in Helvetica (which only supports latin-1)
    if no suitable system font is found.
    """
    import platform
    from pathlib import Path as _P

    candidates: list[tuple[_P, int | None]] = []

    if platform.system() == "Darwin":
        # macOS: prefer Helvetica Neue, then Helvetica (TTC with font_index)
        candidates = [
            (_P("/System/Library/Fonts/HelveticaNeue.ttc"), 0),
            (_P("/System/Library/Fonts/Helvetica.ttc"), 0),
        ]
    elif platform.system() == "Linux":
        candidates = [
            (_P("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), None),
            (_P("/usr/share/fonts/TTF/DejaVuSans.ttf"), None),
        ]

    for font_path, font_index in candidates:
        if font_path.exists():
            try:
                kwargs: dict = {"fname": str(font_path)}
                if font_index is not None:
                    kwargs["font_index"] = font_index

                pdf.add_font("CustomSans", "", **kwargs)
                pdf.add_font("CustomSans", "B", **kwargs)
                pdf.add_font("CustomSans", "I", **kwargs)
                return  # success
            except Exception:
                continue  # try next candidate


def _font(pdf) -> str:
    """Return the best available font family name."""
    if "CustomSans" in pdf.fonts:
        return "CustomSans"
    return "Helvetica"


def generate_monthly_pdf(
    db_path: Path = DEFAULT_DB,
    month: str | None = None,
    output_path: Path | None = None,
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> Path:
    """Generate a single-page monthly PDF report.

    Args:
        db_path: Path to the SQLite database.
        month: The month to report on (YYYY-MM). Defaults to previous month.
        output_path: Where to save the PDF. Defaults to report-YYYY-MM.pdf.
        desc_notes_path: Path to the merchant notes CSV.

    Returns:
        The path to the generated PDF file.
    """
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    if month is None:
        month = previous_month_label()

    if output_path is None:
        output_path = DEFAULT_REPORTS / f"report-{month}.pdf"

    # Fetch data
    conn = sqlite3.connect(str(db_path))
    try:
        transactions = _fetch_month_transactions(conn, month)
        historical = _fetch_historical_category_totals(conn)
    finally:
        conn.close()

    # Compute previous month totals
    prev = _prev_month(month)
    prev_month_totals = historical.get(prev, {})

    # Compute per-category monthly averages (excluding the current month)
    category_averages: dict[str, float] = defaultdict(float)
    category_month_counts: dict[str, int] = defaultdict(int)
    for m, cats in historical.items():
        if m == month:
            continue
        for cat, total in cats.items():
            category_averages[cat] += total
            category_month_counts[cat] += 1
    for cat in list(category_averages):
        if category_month_counts[cat] > 0:
            category_averages[cat] /= category_month_counts[cat]

    merchant_notes = _load_merchant_notes(desc_notes_path)
    stats = _compute_stats(
        transactions, merchant_notes, prev_month_totals, dict(category_averages),
    )

    # Create PDF
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    _setup_fonts(pdf)
    f = _font(pdf)
    pdf.add_page()

    page_w = 210
    margin = 12
    usable_w = page_w - 2 * margin

    # --- Title bar ---
    pdf.set_fill_color(*_CLR_ACCENT)
    pdf.rect(0, 0, page_w, 28, "F")
    pdf.set_font(f, "B", 18)
    pdf.set_text_color(*_CLR_WHITE)
    pdf.set_xy(margin, 6)
    pdf.cell(usable_w / 2, 10, "Monthly Expense Report", new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.set_font(f, "", 14)
    pdf.set_xy(page_w - margin - 80, 8)
    pdf.cell(80, 8, month_display_name(month), new_x=XPos.RIGHT, new_y=YPos.TOP, align="R")

    # Small subtitle
    pdf.set_font(f, "", 8)
    pdf.set_xy(margin, 18)
    pdf.cell(usable_w, 5, f"{stats['tx_count']} transactions", new_x=XPos.RIGHT, new_y=YPos.TOP)

    y = 34

    # --- Summary boxes ---
    box_w = usable_w / 3
    box_h = 20
    summaries = [
        ("Income", stats["total_income"], _CLR_GREEN),
        ("Expenses", stats["total_expenses"], _CLR_RED),
        ("Net Balance", stats["net_balance"],
         _CLR_GREEN if stats["net_balance"] >= 0 else _CLR_RED),
    ]

    for i, (label, amount, color) in enumerate(summaries):
        x = margin + i * box_w
        # Box background
        pdf.set_fill_color(*_CLR_BG_LIGHT)
        pdf.rect(x + 1, y, box_w - 2, box_h, "F")
        # Label
        pdf.set_font(f, "", 8)
        pdf.set_text_color(*_CLR_MUTED)
        pdf.set_xy(x + 4, y + 2)
        pdf.cell(box_w - 8, 5, label, new_x=XPos.RIGHT, new_y=YPos.TOP)
        # Amount
        pdf.set_font(f, "B", 14)
        pdf.set_text_color(*color)
        pdf.set_xy(x + 4, y + 8)
        pdf.cell(box_w - 8, 8, _fmt_eur(amount), new_x=XPos.RIGHT, new_y=YPos.TOP)

    y += box_h + 6

    # --- Two-column layout: Categories (left) + Merchants & Tags (right) ---
    col_left_w = usable_w * 0.55
    col_right_w = usable_w * 0.45
    col_left_x = margin
    col_right_x = margin + col_left_w + 2

    # === LEFT COLUMN: Spending by Category ===
    left_y = y
    pdf.set_font(f, "B", 11)
    pdf.set_text_color(*_CLR_DARK)
    pdf.set_xy(col_left_x, left_y)
    pdf.cell(col_left_w, 7, "Spending by Category", new_x=XPos.RIGHT, new_y=YPos.TOP)
    left_y += 8

    # Table header
    cat_col_w = [
        col_left_w * 0.32,  # Category
        col_left_w * 0.22,  # Amount
        col_left_w * 0.10,  # %
        col_left_w * 0.18,  # vs Prev
        col_left_w * 0.18,  # vs Avg
    ]
    pdf.set_fill_color(*_CLR_ACCENT)
    pdf.set_text_color(*_CLR_WHITE)
    pdf.set_font(f, "B", 6.5)
    headers = ["Category", "Amount", "%", "vs Prev Mo", "vs Avg"]
    for i, (header, w) in enumerate(zip(headers, cat_col_w)):
        pdf.set_xy(col_left_x + sum(cat_col_w[:i]), left_y)
        align = "L" if i == 0 else "R"
        pdf.cell(w, 5, header, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
    left_y += 5

    # Table rows (fit as many as we can)
    max_cat_rows = min(len(stats["by_category"]), 15)
    pdf.set_font(f, "", 6.5)
    for idx in range(max_cat_rows):
        cat, amt, pct, count, prev_diff, avg_diff = stats["by_category"][idx]
        is_uncat = (cat == "Uncategorized")
        if idx % 2 == 0:
            pdf.set_fill_color(250, 250, 252)
        else:
            pdf.set_fill_color(*_CLR_WHITE)

        # Format variation strings with +/- prefix
        def _fmt_diff(diff):
            if diff is None:
                return "-"
            sign = "+" if diff >= 0 else ""
            return f"{sign}{diff:,.0f}"

        row_data = [
            (cat[:24], "L", _CLR_RED if is_uncat else _CLR_DARK),
            (_fmt_eur(amt), "R", _CLR_RED if is_uncat else _CLR_DARK),
            (f"{pct:.1f}%", "R", _CLR_RED if is_uncat else _CLR_DARK),
            (_fmt_diff(prev_diff), "R",
             _CLR_RED if prev_diff is not None and prev_diff > 0
             else _CLR_GREEN if prev_diff is not None and prev_diff < 0
             else _CLR_MUTED),
            (_fmt_diff(avg_diff), "R",
             _CLR_RED if avg_diff is not None and avg_diff > 0
             else _CLR_GREEN if avg_diff is not None and avg_diff < 0
             else _CLR_MUTED),
        ]
        for i, (text, align, color) in enumerate(row_data):
            pdf.set_xy(col_left_x + sum(cat_col_w[:i]), left_y)
            pdf.set_text_color(*color)
            pdf.cell(w=cat_col_w[i], h=4.5, text=text, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
        left_y += 4.5

    # If there are more categories, show a note
    remaining = len(stats["by_category"]) - max_cat_rows
    if remaining > 0:
        pdf.set_font(f, "I", 6)
        pdf.set_text_color(*_CLR_MUTED)
        pdf.set_xy(col_left_x, left_y + 1)
        pdf.cell(col_left_w, 4, f"+ {remaining} more categories (see spreadsheet)", new_x=XPos.RIGHT, new_y=YPos.TOP)
        left_y += 5

    # Uncategorized alert
    if stats["uncategorized_count"] > 0:
        left_y += 3
        pdf.set_fill_color(255, 251, 235)  # warm yellow bg
        pdf.rect(col_left_x, left_y, col_left_w, 8, "F")
        pdf.set_font(f, "B", 7)
        pdf.set_text_color(217, 119, 6)  # amber
        pdf.set_xy(col_left_x + 2, left_y + 1)
        pdf.cell(
            col_left_w - 4, 6,
            f"{stats['uncategorized_count']} uncategorized transaction(s) "
            f"- open the spreadsheet to categorize them",
            new_x=XPos.RIGHT, new_y=YPos.TOP,
        )
        left_y += 10

    # === RIGHT COLUMN: Top Merchants + Tags ===
    right_y = y
    pdf.set_font(f, "B", 11)
    pdf.set_text_color(*_CLR_DARK)
    pdf.set_xy(col_right_x, right_y)
    pdf.cell(col_right_w, 7, "Top Merchants", new_x=XPos.RIGHT, new_y=YPos.TOP)
    right_y += 8

    # Merchants table
    merch_col_w = [col_right_w * 0.55, col_right_w * 0.30, col_right_w * 0.15]
    pdf.set_fill_color(*_CLR_ACCENT)
    pdf.set_text_color(*_CLR_WHITE)
    pdf.set_font(f, "B", 7)
    for i, (header, w) in enumerate(
        zip(["Merchant", "Amount", "Times"], merch_col_w)
    ):
        pdf.set_xy(col_right_x + sum(merch_col_w[:i]), right_y)
        align = "L" if i == 0 else "R"
        pdf.cell(w, 5, header, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
    right_y += 5

    max_merch_rows = min(len(stats["top_merchants"]), 10)
    pdf.set_font(f, "", 7)
    for idx in range(max_merch_rows):
        merch, amt, count = stats["top_merchants"][idx]
        if idx % 2 == 0:
            pdf.set_fill_color(250, 250, 252)
        else:
            pdf.set_fill_color(*_CLR_WHITE)
        pdf.set_text_color(*_CLR_DARK)

        row_data = [
            (merch[:22], "L"),
            (_fmt_eur(amt), "R"),
            (str(count), "R"),
        ]
        for i, ((text, align), w) in enumerate(zip(row_data, merch_col_w)):
            pdf.set_xy(col_right_x + sum(merch_col_w[:i]), right_y)
            pdf.cell(w, 4.5, text, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
        right_y += 4.5

    # --- Tags section (if any) ---
    if stats["tag_totals"]:
        right_y += 6
        pdf.set_font(f, "B", 11)
        pdf.set_text_color(*_CLR_DARK)
        pdf.set_xy(col_right_x, right_y)
        pdf.cell(col_right_w, 7, "#Tag Summary", new_x=XPos.RIGHT, new_y=YPos.TOP)
        right_y += 8

        tag_col_w = [col_right_w * 0.40, col_right_w * 0.35, col_right_w * 0.25]
        pdf.set_fill_color(*_CLR_ACCENT)
        pdf.set_text_color(*_CLR_WHITE)
        pdf.set_font(f, "B", 7)
        for i, (header, w) in enumerate(
            zip(["Tag", "Amount", "# Txns"], tag_col_w)
        ):
            pdf.set_xy(col_right_x + sum(tag_col_w[:i]), right_y)
            align = "L" if i == 0 else "R"
            pdf.cell(w, 5, header, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
        right_y += 5

        max_tag_rows = min(len(stats["tag_totals"]), 8)
        pdf.set_font(f, "", 7)
        for idx, (tag, (amt, count)) in enumerate(
            list(stats["tag_totals"].items())[:max_tag_rows]
        ):
            if idx % 2 == 0:
                pdf.set_fill_color(250, 250, 252)
            else:
                pdf.set_fill_color(*_CLR_WHITE)
            pdf.set_text_color(*_CLR_DARK)

            row_data = [
                (tag, "L"),
                (_fmt_eur(amt), "R"),
                (str(count), "R"),
            ]
            for i, ((text, align), w) in enumerate(zip(row_data, tag_col_w)):
                pdf.set_xy(col_right_x + sum(tag_col_w[:i]), right_y)
                pdf.cell(w, 4.5, text, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align)
            right_y += 4.5

    # --- Essential Spending section ---
    essentials = stats.get("essentials", [])
    if essentials:
        bottom_y = max(left_y, right_y) + 6
        pdf.set_font(f, "B", 11)
        pdf.set_text_color(*_CLR_DARK)
        pdf.set_xy(margin, bottom_y)
        pdf.cell(usable_w, 7, "Essential Spending", new_x=XPos.RIGHT, new_y=YPos.TOP)
        bottom_y += 8

        max_boxes = min(len(essentials), 6)
        box_w_ess = usable_w / max_boxes
        for i, (cat, total) in enumerate(essentials[:max_boxes]):
            x = margin + i * box_w_ess
            pdf.set_fill_color(*_CLR_BG_LIGHT)
            pdf.rect(x + 1, bottom_y, box_w_ess - 2, 14, "F")
            pdf.set_font(f, "", 7)
            pdf.set_text_color(*_CLR_MUTED)
            pdf.set_xy(x + 3, bottom_y + 1)
            pdf.cell(box_w_ess - 6, 5, cat, new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_font(f, "B", 10)
            pdf.set_text_color(*_CLR_DARK)
            pdf.set_xy(x + 3, bottom_y + 6)
            pdf.cell(box_w_ess - 6, 6, _fmt_eur(total), new_x=XPos.RIGHT, new_y=YPos.TOP)

    # --- Footer ---
    pdf.set_font(f, "I", 6)
    pdf.set_text_color(*_CLR_MUTED)
    pdf.set_xy(margin, 287)
    pdf.cell(
        usable_w, 4,
        f"Generated from ledger data  |  For details open expense-report.ods",
        new_x=XPos.RIGHT, new_y=YPos.TOP,
    )

    # --- Handle no-data case ---
    if not transactions:
        pdf.set_font(f, "", 14)
        pdf.set_text_color(*_CLR_MUTED)
        pdf.set_xy(margin, 80)
        pdf.cell(
            usable_w, 20,
            f"No transactions found for {month_display_name(month)}.",
            new_x=XPos.RIGHT, new_y=YPos.TOP, align="C",
        )
        pdf.set_font(f, "", 10)
        pdf.set_xy(margin, 100)
        pdf.cell(
            usable_w, 10,
            "Import bank statements with ./run.sh and try again.",
            new_x=XPos.RIGHT, new_y=YPos.TOP, align="C",
        )

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path
