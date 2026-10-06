"""
Monthly PDF report generator.

Produces a single-page A4 summary for a given month, pulling data from the
SQLite database.  The report is designed as a quick bird's-eye view that
highlights where money went so the user can then dive deeper in the ODS.

If an advisor response exists for the month, it's appended as additional pages.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .constants import (
    DEFAULT_DB,
    DEFAULT_DESC_NOTES,
    DEFAULT_REPORTS,
    DEFAULT_ADVISOR_DIR,
)
from .db import NOT_SAVINGS_SQL, REFUND_SQL, SPEND_SQL, is_refund, is_savings

# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

# Categories considered "essential" for the summary section.
# Users can tweak this list in code if their category names differ.
ESSENTIAL_CATEGORIES = [
    "Housing",
    "Utilities",
    "Subscriptions",
    "Insurance",
    "Health",
    "Childcare",
    "Transport",
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
        f"""
        SELECT month, category, SUM({SPEND_SQL}) as total
        FROM transactions
        WHERE category IS NOT NULL AND category != ''
          AND (direction = 'out' OR {REFUND_SQL}) AND {NOT_SAVINGS_SQL}
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

        if is_savings(tx):
            # Money moved to or from savings is neither spending nor income
            continue
        if is_refund(tx):
            # Refunds reduce their category's spending; they are not income
            total_expenses -= amount
            cat_amounts[category] -= amount
        elif direction == "in":
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


def _next_month(month: str) -> str:
    """Given a YYYY-MM string, return the next month's YYYY-MM."""
    parts = month.split("-")
    year, m = int(parts[0]), int(parts[1])
    if m == 12:
        return f"{year + 1}-01"
    return f"{year}-{m + 1:02d}"


def _parse_report_month(filename: str) -> str | None:
    """Extract YYYY-MM from a report-YYYY-MM.pdf filename."""
    if not filename.startswith("report-") or not filename.endswith(".pdf"):
        return None
    month = filename[len("report-") : -len(".pdf")]
    parts = month.split("-")
    if len(parts) != 2:
        return None
    try:
        year, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 1 <= m <= 12:
        return month
    return None


def existing_report_months(reports_dir: Path = DEFAULT_REPORTS) -> set[str]:
    """Return the set of YYYY-MM months that already have report PDFs."""
    if not reports_dir.exists():
        return set()
    months: set[str] = set()
    for pdf_file in reports_dir.glob("report-*.pdf"):
        month = _parse_report_month(pdf_file.name)
        if month:
            months.add(month)
    return months


def months_to_generate(
    reports_dir: Path = DEFAULT_REPORTS,
    through_month: str | None = None,
) -> list[str]:
    """Return months that need PDF reports up to through_month.

    Fills gaps after the most recent report that is strictly before
    through_month.  If every month in that range already has a report,
    returns through_month so the previous month can be refreshed.
    """
    if through_month is None:
        through_month = previous_month_label()

    existing = existing_report_months(reports_dir)
    if not existing:
        return [through_month]

    prior = [m for m in existing if m < through_month]
    if not prior:
        return [through_month]

    anchor = max(prior)
    months: list[str] = []
    current = _next_month(anchor)
    while current <= through_month:
        if current not in existing:
            months.append(current)
        current = _next_month(current)

    return months if months else [through_month]


def generate_missing_monthly_pdfs(
    db_path: Path = DEFAULT_DB,
    reports_dir: Path = DEFAULT_REPORTS,
    through_month: str | None = None,
    desc_notes_path: Path = DEFAULT_DESC_NOTES,
) -> list[Path]:
    """Generate PDF reports for all missing months up to through_month."""
    generated: list[Path] = []
    for month in months_to_generate(reports_dir, through_month):
        generated.append(
            generate_monthly_pdf(
                db_path=db_path,
                month=month,
                output_path=reports_dir / f"report-{month}.pdf",
                desc_notes_path=desc_notes_path,
            )
        )
    return generated


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
_CLR_DARK = (26, 26, 46)  # near-black text
_CLR_ACCENT = (37, 99, 235)  # blue accent
_CLR_MUTED = (100, 100, 120)  # muted text
_CLR_BG_LIGHT = (243, 244, 246)  # light grey backgrounds
_CLR_GREEN = (22, 163, 74)  # positive / income
_CLR_RED = (220, 38, 38)  # negative / expenses
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


def _new_pdf():
    """A4 document that prints "?" for characters its font cannot encode.

    The built-in Helvetica covers latin-1 only, and fpdf2 raises on any
    other character: one merchant name in another alphabet, a euro sign or
    a typographic dash would otherwise cost the whole report wherever no
    system font was found.
    """
    from fpdf import FPDF

    class ReportPDF(FPDF):
        def normalize_text(self, text):
            if not self.is_ttf_font and self.core_fonts_encoding:
                encoding = self.core_fonts_encoding
                text = text.encode(encoding, "replace").decode(encoding)
            return super().normalize_text(text)

    return ReportPDF(orientation="P", unit="mm", format="A4")


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

    # Compute per-category monthly averages over the months before this one.
    # Later months are no part of its history: the report is usually written
    # a few days into the next month, and old months can be regenerated.
    category_averages: dict[str, float] = defaultdict(float)
    category_month_counts: dict[str, int] = defaultdict(int)
    for m, cats in historical.items():
        if m >= month:
            continue
        for cat, total in cats.items():
            category_averages[cat] += total
            category_month_counts[cat] += 1
    for cat in list(category_averages):
        if category_month_counts[cat] > 0:
            category_averages[cat] /= category_month_counts[cat]

    merchant_notes = _load_merchant_notes(desc_notes_path)
    stats = _compute_stats(
        transactions,
        merchant_notes,
        prev_month_totals,
        dict(category_averages),
    )

    # Create PDF
    pdf = _new_pdf()
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
    pdf.cell(
        usable_w / 2, 10, "Monthly Expense Report", new_x=XPos.RIGHT, new_y=YPos.TOP
    )
    pdf.set_font(f, "", 14)
    pdf.set_xy(page_w - margin - 80, 8)
    pdf.cell(
        80, 8, month_display_name(month), new_x=XPos.RIGHT, new_y=YPos.TOP, align="R"
    )

    # Small subtitle
    pdf.set_font(f, "", 8)
    pdf.set_xy(margin, 18)
    pdf.cell(
        usable_w,
        5,
        f"{stats['tx_count']} transactions",
        new_x=XPos.RIGHT,
        new_y=YPos.TOP,
    )

    y = 34

    # --- Summary boxes ---
    box_w = usable_w / 3
    box_h = 20
    summaries = [
        ("Income", stats["total_income"], _CLR_GREEN),
        ("Expenses", stats["total_expenses"], _CLR_RED),
        (
            "Net Balance",
            stats["net_balance"],
            _CLR_GREEN if stats["net_balance"] >= 0 else _CLR_RED,
        ),
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
        is_uncat = cat == "Uncategorized"
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
            (
                _fmt_diff(prev_diff),
                "R",
                (
                    _CLR_RED
                    if prev_diff is not None and prev_diff > 0
                    else (
                        _CLR_GREEN
                        if prev_diff is not None and prev_diff < 0
                        else _CLR_MUTED
                    )
                ),
            ),
            (
                _fmt_diff(avg_diff),
                "R",
                (
                    _CLR_RED
                    if avg_diff is not None and avg_diff > 0
                    else (
                        _CLR_GREEN
                        if avg_diff is not None and avg_diff < 0
                        else _CLR_MUTED
                    )
                ),
            ),
        ]
        for i, (text, align, color) in enumerate(row_data):
            pdf.set_xy(col_left_x + sum(cat_col_w[:i]), left_y)
            pdf.set_text_color(*color)
            pdf.cell(
                w=cat_col_w[i],
                h=4.5,
                text=text,
                new_x=XPos.RIGHT,
                new_y=YPos.TOP,
                fill=True,
                align=align,
            )
        left_y += 4.5

    # If there are more categories, show a note
    remaining = len(stats["by_category"]) - max_cat_rows
    if remaining > 0:
        pdf.set_font(f, "I", 6)
        pdf.set_text_color(*_CLR_MUTED)
        pdf.set_xy(col_left_x, left_y + 1)
        pdf.cell(
            col_left_w,
            4,
            f"+ {remaining} more categories (see spreadsheet)",
            new_x=XPos.RIGHT,
            new_y=YPos.TOP,
        )
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
            col_left_w - 4,
            6,
            f"{stats['uncategorized_count']} uncategorized transaction(s) "
            f"- open the spreadsheet to categorize them",
            new_x=XPos.RIGHT,
            new_y=YPos.TOP,
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
    for i, (header, w) in enumerate(zip(["Merchant", "Amount", "Times"], merch_col_w)):
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
            pdf.cell(
                w, 4.5, text, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align
            )
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
        for i, (header, w) in enumerate(zip(["Tag", "Amount", "# Txns"], tag_col_w)):
            pdf.set_xy(col_right_x + sum(tag_col_w[:i]), right_y)
            align = "L" if i == 0 else "R"
            pdf.cell(
                w, 5, header, new_x=XPos.RIGHT, new_y=YPos.TOP, fill=True, align=align
            )
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
                pdf.cell(
                    w,
                    4.5,
                    text,
                    new_x=XPos.RIGHT,
                    new_y=YPos.TOP,
                    fill=True,
                    align=align,
                )
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
            pdf.cell(
                box_w_ess - 6, 6, _fmt_eur(total), new_x=XPos.RIGHT, new_y=YPos.TOP
            )

    # --- Footer ---
    pdf.set_font(f, "I", 6)
    pdf.set_text_color(*_CLR_MUTED)
    pdf.set_xy(margin, 287)
    pdf.cell(
        usable_w,
        4,
        f"Generated from ledger data  |  For details open expense-report.ods",
        new_x=XPos.RIGHT,
        new_y=YPos.TOP,
    )

    # --- Handle no-data case ---
    if not transactions:
        pdf.set_font(f, "", 14)
        pdf.set_text_color(*_CLR_MUTED)
        pdf.set_xy(margin, 80)
        pdf.cell(
            usable_w,
            20,
            f"No transactions found for {month_display_name(month)}.",
            new_x=XPos.RIGHT,
            new_y=YPos.TOP,
            align="C",
        )
        pdf.set_font(f, "", 10)
        pdf.set_xy(margin, 100)
        pdf.cell(
            usable_w,
            10,
            "Import bank statements with ./run.sh and try again.",
            new_x=XPos.RIGHT,
            new_y=YPos.TOP,
            align="C",
        )

    # --- Advisor Notes (if response exists) ---
    advisor_response_path = DEFAULT_ADVISOR_DIR / f"response-{month}.md"
    if advisor_response_path.exists():
        _add_advisor_pages(pdf, advisor_response_path, f, margin, usable_w)

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


def _add_advisor_pages(
    pdf,
    response_path: Path,
    font: str,
    margin: float,
    usable_w: float,
) -> None:
    """Add advisor response as additional pages to the PDF."""
    from fpdf.enums import XPos, YPos

    response_text = response_path.read_text(encoding="utf-8")
    if not response_text.strip():
        return

    page_w = 210
    page_h = 297

    # Add a new page for advisor notes
    pdf.add_page()

    # Header
    pdf.set_fill_color(*_CLR_ACCENT)
    pdf.rect(0, 0, page_w, 20, "F")
    pdf.set_font(font, "B", 14)
    pdf.set_text_color(*_CLR_WHITE)
    pdf.set_xy(margin, 6)
    pdf.cell(usable_w, 8, "Financial Advisor Notes", new_x=XPos.RIGHT, new_y=YPos.TOP)

    y = 28

    # Parse and render markdown-ish content
    pdf.set_text_color(*_CLR_DARK)

    lines = response_text.split("\n")
    for line in lines:
        # Check if we need a new page
        if y > page_h - 20:
            pdf.add_page()
            y = 15

        stripped = line.strip()

        # Headers
        if stripped.startswith("### "):
            pdf.set_font(font, "B", 10)
            pdf.set_text_color(*_CLR_ACCENT)
            pdf.set_xy(margin, y)
            pdf.multi_cell(usable_w, 5, stripped[4:], new_x=XPos.LEFT, new_y=YPos.NEXT)
            y = pdf.get_y() + 2
            pdf.set_text_color(*_CLR_DARK)
        elif stripped.startswith("## "):
            y += 3
            pdf.set_font(font, "B", 12)
            pdf.set_text_color(*_CLR_ACCENT)
            pdf.set_xy(margin, y)
            pdf.multi_cell(usable_w, 6, stripped[3:], new_x=XPos.LEFT, new_y=YPos.NEXT)
            y = pdf.get_y() + 3
            pdf.set_text_color(*_CLR_DARK)
        elif stripped.startswith("# "):
            y += 4
            pdf.set_font(font, "B", 14)
            pdf.set_text_color(*_CLR_ACCENT)
            pdf.set_xy(margin, y)
            pdf.multi_cell(usable_w, 7, stripped[2:], new_x=XPos.LEFT, new_y=YPos.NEXT)
            y = pdf.get_y() + 4
            pdf.set_text_color(*_CLR_DARK)
        # Bullet points
        elif stripped.startswith("- ") or stripped.startswith("* "):
            pdf.set_font(font, "", 9)
            pdf.set_xy(margin + 4, y)
            # The built-in font has no bullet glyph
            bullet = "•" if font == "CustomSans" else "-"
            pdf.cell(4, 4, bullet, new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_xy(margin + 10, y)
            pdf.multi_cell(
                usable_w - 10, 4, stripped[2:], new_x=XPos.LEFT, new_y=YPos.NEXT
            )
            y = pdf.get_y() + 1
        # Numbered lists
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
            pdf.set_font(font, "", 9)
            pdf.set_xy(margin + 4, y)
            pdf.cell(6, 4, stripped[:2], new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_xy(margin + 12, y)
            pdf.multi_cell(
                usable_w - 12, 4, stripped[2:].strip(), new_x=XPos.LEFT, new_y=YPos.NEXT
            )
            y = pdf.get_y() + 1
        # Bold text (simple **text** handling)
        elif stripped.startswith("**") and "**" in stripped[2:]:
            pdf.set_font(font, "B", 9)
            # Extract bold portion
            end_idx = stripped.index("**", 2)
            bold_text = stripped[2:end_idx]
            rest = stripped[end_idx + 2 :].lstrip(": ")
            pdf.set_xy(margin, y)
            pdf.cell(
                pdf.get_string_width(bold_text) + 2,
                4,
                bold_text,
                new_x=XPos.RIGHT,
                new_y=YPos.TOP,
            )
            if rest:
                pdf.set_font(font, "", 9)
                pdf.multi_cell(
                    usable_w - pdf.get_x() + margin,
                    4,
                    ": " + rest if rest else "",
                    new_x=XPos.LEFT,
                    new_y=YPos.NEXT,
                )
                y = pdf.get_y() + 1
            else:
                y += 5
        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            y += 3
            pdf.set_draw_color(*_CLR_MUTED)
            pdf.line(margin, y, margin + usable_w, y)
            y += 5
        # Empty line
        elif not stripped:
            y += 3
        # Regular text
        else:
            pdf.set_font(font, "", 9)
            pdf.set_xy(margin, y)
            pdf.multi_cell(usable_w, 4, stripped, new_x=XPos.LEFT, new_y=YPos.NEXT)
            y = pdf.get_y() + 1
