"""Tests for the PDF report generator."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from expense_tracker.pdf_report import (
    _compute_stats,
    _fetch_historical_category_totals,
    _extract_tags,
    _fmt_eur,
    _next_month,
    _parse_report_month,
    _prev_month,
    generate_monthly_pdf,
    month_display_name,
    months_to_generate,
    previous_month_label,
    ESSENTIAL_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Helper to build transaction dicts for testing
# ---------------------------------------------------------------------------


def _tx(
    desc: str = "TEST MERCHANT",
    amount: float = 10.0,
    direction: str = "out",
    category: str = "",
    notes: str = "",
    who: str = "Joint",
    month: str = "2026-01",
) -> dict:
    return {
        "date_posted": f"{month}-15",
        "description_clean": desc,
        "amount_signed": -amount if direction == "out" else amount,
        "amount_abs": amount,
        "direction": direction,
        "category": category,
        "subcategory": "",
        "payment_type": "card",
        "who": who,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# _extract_tags
# ---------------------------------------------------------------------------


class TestExtractTags:
    def test_basic_tags(self):
        assert _extract_tags("#gift #shared dinner") == ["#gift", "#shared"]

    def test_no_tags(self):
        assert _extract_tags("just a regular note") == []

    def test_empty_string(self):
        assert _extract_tags("") == []

    def test_none(self):
        assert _extract_tags(None) == []

    def test_hash_alone_ignored(self):
        assert _extract_tags("# not a tag") == []

    def test_tags_with_dashes(self):
        assert _extract_tags("#one-off purchase") == ["#one-off"]


# ---------------------------------------------------------------------------
# _fmt_eur
# ---------------------------------------------------------------------------


class TestFmtEur:
    def test_basic(self):
        result = _fmt_eur(1234.56)
        assert "1,234.56" in result
        assert "EUR" in result

    def test_zero(self):
        result = _fmt_eur(0.0)
        assert "0.00" in result

    def test_large_number(self):
        result = _fmt_eur(1234567.89)
        assert "1,234,567.89" in result


# ---------------------------------------------------------------------------
# month_display_name
# ---------------------------------------------------------------------------


class TestMonthDisplayName:
    def test_january(self):
        assert month_display_name("2026-01") == "January 2026"

    def test_december(self):
        assert month_display_name("2025-12") == "December 2025"


# ---------------------------------------------------------------------------
# previous_month_label
# ---------------------------------------------------------------------------


class TestPreviousMonthLabel:
    def test_format(self):
        label = previous_month_label()
        # Should be YYYY-MM format
        assert len(label) == 7
        assert label[4] == "-"
        year, month = label.split("-")
        assert 2020 <= int(year) <= 2100
        assert 1 <= int(month) <= 12


# ---------------------------------------------------------------------------
# _compute_stats
# ---------------------------------------------------------------------------


class TestComputeStats:
    def test_empty_transactions(self):
        stats = _compute_stats([])
        assert stats["total_income"] == 0
        assert stats["total_expenses"] == 0
        assert stats["net_balance"] == 0
        assert stats["tx_count"] == 0
        assert stats["by_category"] == []
        assert stats["top_merchants"] == []
        assert stats["uncategorized_count"] == 0

    def test_income_and_expenses(self):
        txns = [
            _tx(amount=100, direction="out", category="Groceries"),
            _tx(amount=50, direction="out", category="Health"),
            _tx(amount=2000, direction="in"),
        ]
        stats = _compute_stats(txns)
        assert stats["total_income"] == 2000
        assert stats["total_expenses"] == 150
        assert stats["net_balance"] == 1850
        assert stats["tx_count"] == 3

    def test_refund_reduces_its_category_and_total_spending(self):
        txns = [
            _tx(amount=100, direction="out", category="Health"),
            _tx(amount=30, direction="in", category="Health"),
        ]
        stats = _compute_stats(txns)
        assert stats["by_category"][0][:2] == ("Health", 70)
        assert stats["total_expenses"] == 70
        assert stats["total_income"] == 0

    def test_savings_are_neither_spending_nor_income(self):
        txns = [
            _tx(desc="SAVINGS ACCOUNT", amount=2000, direction="out", category="Savings"),
            _tx(desc="SAVINGS ACCOUNT", amount=500, direction="in", category="Savings"),
            _tx(amount=40, direction="out", category="Groceries"),
        ]
        stats = _compute_stats(txns)
        assert stats["total_expenses"] == 40
        assert stats["total_income"] == 0
        assert [c[0] for c in stats["by_category"]] == ["Groceries"]
        assert [m[0] for m in stats["top_merchants"]] == ["TEST MERCHANT"]

    def test_income_and_uncategorized_inflows_are_income(self):
        txns = [
            _tx(amount=1000, direction="in", category="Income"),
            _tx(amount=50, direction="in"),
        ]
        stats = _compute_stats(txns)
        assert stats["total_income"] == 1050
        assert stats["by_category"] == []

    def test_category_breakdown(self):
        txns = [
            _tx(amount=100, direction="out", category="Groceries"),
            _tx(amount=80, direction="out", category="Groceries"),
            _tx(amount=50, direction="out", category="Health"),
        ]
        stats = _compute_stats(txns)
        # Sorted by amount descending; tuple is (cat, amt, pct, count, prev_diff, avg_diff)
        assert stats["by_category"][0][0] == "Groceries"
        assert stats["by_category"][0][1] == 180  # total
        assert stats["by_category"][0][3] == 2    # count
        assert stats["by_category"][0][4] is None  # no prev data
        assert stats["by_category"][0][5] is None  # no avg data
        assert stats["by_category"][1][0] == "Health"

    def test_uncategorized_count(self):
        txns = [
            _tx(amount=100, direction="out", category=""),
            _tx(amount=50, direction="out", category=""),
            _tx(amount=30, direction="out", category="Groceries"),
        ]
        stats = _compute_stats(txns)
        assert stats["uncategorized_count"] == 2
        # Uncategorized should appear as a category too
        cat_names = [c[0] for c in stats["by_category"]]
        assert "Uncategorized" in cat_names

    def test_top_merchants(self):
        txns = [
            _tx(desc="MERCHANT A", amount=500, direction="out"),
            _tx(desc="MERCHANT B", amount=300, direction="out"),
            _tx(desc="MERCHANT C", amount=100, direction="out"),
        ]
        stats = _compute_stats(txns)
        assert len(stats["top_merchants"]) == 3
        assert stats["top_merchants"][0][0] == "MERCHANT A"
        assert stats["top_merchants"][0][1] == 500

    def test_top_merchants_max_10(self):
        txns = [
            _tx(desc=f"MERCHANT {i}", amount=i * 10, direction="out")
            for i in range(15)
        ]
        stats = _compute_stats(txns)
        assert len(stats["top_merchants"]) == 10

    def test_tag_totals(self):
        txns = [
            _tx(amount=100, direction="out", notes="#gift birthday"),
            _tx(amount=50, direction="out", notes="#gift #shared"),
            _tx(amount=200, direction="out", notes="no tags"),
        ]
        stats = _compute_stats(txns)
        assert "#gift" in stats["tag_totals"]
        assert stats["tag_totals"]["#gift"] == (150.0, 2)
        assert "#shared" in stats["tag_totals"]
        assert stats["tag_totals"]["#shared"] == (50.0, 1)

    def test_merchant_note_tags(self):
        txns = [
            _tx(desc="CONTINENTE", amount=100, direction="out"),
            _tx(desc="OTHER", amount=50, direction="out"),
        ]
        merchant_notes = {"CONTINENTE": "#recurring #essential"}
        stats = _compute_stats(txns, merchant_notes)
        assert "#recurring" in stats["tag_totals"]
        assert stats["tag_totals"]["#recurring"] == (100.0, 1)

    def test_income_not_in_category_breakdown(self):
        txns = [
            _tx(amount=2000, direction="in", category="Income"),
            _tx(amount=100, direction="out", category="Groceries"),
        ]
        stats = _compute_stats(txns)
        cat_names = [c[0] for c in stats["by_category"]]
        assert "Income" not in cat_names  # only outgoing in categories
        assert "Groceries" in cat_names

    def test_variation_vs_previous_month(self):
        txns = [
            _tx(amount=200, direction="out", category="Groceries"),
        ]
        prev = {"Groceries": 150.0}
        stats = _compute_stats(txns, prev_month_totals=prev)
        cat = stats["by_category"][0]
        assert cat[0] == "Groceries"
        assert cat[4] == 50.0  # 200 - 150 = +50

    def test_variation_vs_average(self):
        txns = [
            _tx(amount=200, direction="out", category="Groceries"),
        ]
        avgs = {"Groceries": 250.0}
        stats = _compute_stats(txns, category_averages=avgs)
        cat = stats["by_category"][0]
        assert cat[0] == "Groceries"
        assert cat[5] == -50.0  # 200 - 250 = -50

    def test_essentials(self):
        txns = [
            _tx(amount=500, direction="out", category="Housing"),
            _tx(amount=100, direction="out", category="Utilities"),
            _tx(amount=50, direction="out", category="Shopping"),
        ]
        stats = _compute_stats(txns)
        ess_cats = [e[0] for e in stats["essentials"]]
        assert "Housing" in ess_cats
        assert "Utilities" in ess_cats
        assert "Shopping" not in ess_cats

    def test_essentials_amounts(self):
        txns = [
            _tx(amount=500, direction="out", category="Housing"),
            _tx(amount=200, direction="out", category="Housing"),
            _tx(amount=30, direction="out", category="Insurance"),
        ]
        stats = _compute_stats(txns)
        ess_dict = {e[0]: e[1] for e in stats["essentials"]}
        assert ess_dict["Housing"] == 700
        assert ess_dict["Insurance"] == 30


# ---------------------------------------------------------------------------
# _prev_month
# ---------------------------------------------------------------------------


class TestPrevMonth:
    def test_normal(self):
        assert _prev_month("2026-03") == "2026-02"

    def test_january_wraps(self):
        assert _prev_month("2026-01") == "2025-12"

    def test_december(self):
        assert _prev_month("2026-12") == "2026-11"


# ---------------------------------------------------------------------------
# _next_month / months_to_generate
# ---------------------------------------------------------------------------


class TestNextMonth:
    def test_normal(self):
        assert _next_month("2026-02") == "2026-03"

    def test_december_wraps(self):
        assert _next_month("2026-12") == "2027-01"


class TestParseReportMonth:
    def test_valid(self):
        assert _parse_report_month("report-2026-03.pdf") == "2026-03"

    def test_invalid(self):
        assert _parse_report_month("summary-2026-03.pdf") is None
        assert _parse_report_month("report-2026-13.pdf") is None


class TestMonthsToGenerate:
    def test_no_reports_defaults_to_through_month(self, tmp_path: Path):
        assert months_to_generate(tmp_path, "2026-06") == ["2026-06"]

    def test_fills_gap_after_latest_prior_report(self, tmp_path: Path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "report-2026-02.pdf").write_bytes(b"%PDF-1.4")

        assert months_to_generate(reports, "2026-06") == [
            "2026-03",
            "2026-04",
            "2026-05",
            "2026-06",
        ]

    def test_skips_existing_and_fills_only_missing(self, tmp_path: Path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "report-2026-02.pdf").write_bytes(b"%PDF-1.4")
        (reports / "report-2026-06.pdf").write_bytes(b"%PDF-1.4")

        assert months_to_generate(reports, "2026-06") == [
            "2026-03",
            "2026-04",
            "2026-05",
        ]

    def test_all_present_refreshes_through_month(self, tmp_path: Path):
        reports = tmp_path / "reports"
        reports.mkdir()
        for month in ("2026-03", "2026-04", "2026-05", "2026-06"):
            (reports / f"report-{month}.pdf").write_bytes(b"%PDF-1.4")

        assert months_to_generate(reports, "2026-06") == ["2026-06"]


# ---------------------------------------------------------------------------
# generate_monthly_pdf (integration)
# ---------------------------------------------------------------------------


class TestGenerateMonthlyPdf:
    def test_generates_pdf_file(self, populated_db, tmp_path):
        output = tmp_path / "test-report.pdf"
        desc_notes = tmp_path / "desc-notes.csv"
        desc_notes.write_text(
            "description_clean,merchant_note\n", encoding="utf-8"
        )

        result = generate_monthly_pdf(
            db_path=populated_db,
            month="2026-01",
            output_path=output,
            desc_notes_path=desc_notes,
        )
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0
        # Basic PDF header check
        header = output.read_bytes()[:5]
        assert header == b"%PDF-"

    def test_empty_month_generates_pdf(self, populated_db, tmp_path):
        output = tmp_path / "empty-report.pdf"
        desc_notes = tmp_path / "desc-notes.csv"
        desc_notes.write_text(
            "description_clean,merchant_note\n", encoding="utf-8"
        )

        result = generate_monthly_pdf(
            db_path=populated_db,
            month="2099-12",  # no data for this month
            output_path=output,
            desc_notes_path=desc_notes,
        )
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_default_output_path(self, populated_db, tmp_path, monkeypatch):
        """When no output path given, defaults to reports/report-YYYY-MM.pdf."""
        monkeypatch.chdir(tmp_path)
        desc_notes = tmp_path / "desc-notes.csv"
        desc_notes.write_text(
            "description_clean,merchant_note\n", encoding="utf-8"
        )

        result = generate_monthly_pdf(
            db_path=populated_db,
            month="2026-01",
            desc_notes_path=desc_notes,
        )
        assert result.name == "report-2026-01.pdf"
        assert result.parent.name == "reports"
        assert result.exists()

    def test_with_merchant_notes_and_tags(self, populated_db, tmp_path):
        output = tmp_path / "tagged-report.pdf"
        desc_notes = tmp_path / "desc-notes.csv"
        desc_notes.write_text(
            "description_clean,merchant_note\n"
            "CONTINENTE,#recurring weekly groceries\n",
            encoding="utf-8",
        )

        result = generate_monthly_pdf(
            db_path=populated_db,
            month="2026-01",
            output_path=output,
            desc_notes_path=desc_notes,
        )
        assert result == output
        assert output.exists()

    def test_creates_parent_directory(self, populated_db, tmp_path):
        output = tmp_path / "subdir" / "nested" / "report.pdf"
        desc_notes = tmp_path / "desc-notes.csv"
        desc_notes.write_text(
            "description_clean,merchant_note\n", encoding="utf-8"
        )

        result = generate_monthly_pdf(
            db_path=populated_db,
            month="2026-01",
            output_path=output,
            desc_notes_path=desc_notes,
        )
        assert output.exists()


# ---------------------------------------------------------------------------
# _fetch_historical_category_totals
# ---------------------------------------------------------------------------


def _insert(conn, tid, month, direction, category, amount):
    conn.execute(
        "INSERT INTO transactions (transaction_id, date_posted, date_value, month, "
        "day_of_week, description_raw, amount_signed, amount_abs, direction, "
        "currency, account, category, source_file, imported_at) "
        "VALUES (?, ?, ?, ?, 'Mon', 'X', ?, ?, ?, 'EUR', 'a', ?, 'f', 'now')",
        (tid, f"{month}-01", f"{month}-01", month,
         -amount if direction == "out" else amount, amount, direction, category),
    )


class TestHistoricalCategoryTotals:
    def test_nets_refunds_against_category(self, test_db):
        conn = sqlite3.connect(str(test_db))
        _insert(conn, "1", "2026-01", "out", "Health", 100)
        _insert(conn, "2", "2026-01", "in", "Health", 30)
        _insert(conn, "3", "2026-01", "in", "Income", 2000)
        _insert(conn, "4", "2026-01", "out", "Savings", 3000)
        totals = _fetch_historical_category_totals(conn)
        conn.close()
        assert totals == {"2026-01": {"Health": 70}}


def _pdf_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


class TestCategoryAverages:
    """'vs Avg' compares a month with the months before it."""

    def _report_row(self, test_db, tmp_path, month: str, category: str) -> str:
        output = tmp_path / f"report-{month}.pdf"
        generate_monthly_pdf(
            db_path=test_db,
            month=month,
            output_path=output,
            desc_notes_path=tmp_path / "notes.csv",
        )
        # The merchants table shares the line: keep the six category cells
        return " ".join(next(
            line for line in _pdf_text(output).splitlines()
            if line.startswith(category)
        ).split()[:6])

    def test_later_months_do_not_count(self, test_db, tmp_path):
        """A report is usually written a few days into the next month, whose
        first transactions are already in the ledger; an old month can also
        be regenerated at any time."""
        from .conftest import add_transaction

        add_transaction(test_db, "2026-01-10", "SHOP", 100.0, category="Groceries")
        add_transaction(test_db, "2026-02-10", "SHOP", 200.0, category="Groceries")
        add_transaction(test_db, "2026-03-02", "SHOP", 600.0, category="Groceries")

        row = self._report_row(test_db, tmp_path, "2026-02", "Groceries")

        # amount, share, vs previous month (200 - 100), vs average (200 - 100)
        assert row == "Groceries 200.00 EUR 100.0% +100 +100"

    def test_first_month_has_no_average(self, test_db, tmp_path):
        from .conftest import add_transaction

        add_transaction(test_db, "2026-01-10", "SHOP", 100.0, category="Groceries")
        add_transaction(test_db, "2026-02-10", "SHOP", 200.0, category="Groceries")

        row = self._report_row(test_db, tmp_path, "2026-01", "Groceries")

        assert row == "Groceries 100.00 EUR 100.0% - -"


class TestBuiltInFont:
    """Without a system Unicode font (the Docker image has none) the report
    uses the built-in Helvetica, which only covers latin-1."""

    @pytest.fixture(autouse=True)
    def no_system_font(self, monkeypatch):
        import expense_tracker.pdf_report as pdf_report

        monkeypatch.setattr(pdf_report, "_setup_fonts", lambda pdf: None)

    def test_other_alphabets_do_not_stop_the_report(self, test_db, tmp_path):
        from .conftest import add_transaction

        add_transaction(test_db, "2026-01-10", "CAFÉ “O PIPO” – ŁÓDŹ", 10.0,
                        category="Żabka €", notes="#prenda’s")
        output = tmp_path / "report.pdf"

        generate_monthly_pdf(
            db_path=test_db, month="2026-01", output_path=output,
            desc_notes_path=tmp_path / "notes.csv",
        )

        text = _pdf_text(output)
        assert "CAFÉ ?O PIPO? ? ?ÓD?" in text  # latin-1 kept, the rest marked
        assert "?abka ?" in text
        assert "10.00 EUR" in text

    def test_advisor_notes_with_a_list_do_not_stop_the_report(
        self, test_db, tmp_path, monkeypatch
    ):
        import expense_tracker.pdf_report as pdf_report

        from .conftest import add_transaction

        add_transaction(test_db, "2026-01-10", "SHOP", 10.0, category="Groceries")
        advisor_dir = tmp_path / "advisor"
        advisor_dir.mkdir()
        (advisor_dir / "response-2026-01.md").write_text(
            "## Summary\n\n- Groceries are up — cut back\n- Savings → fine\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(pdf_report, "DEFAULT_ADVISOR_DIR", advisor_dir)
        output = tmp_path / "report.pdf"

        generate_monthly_pdf(
            db_path=test_db, month="2026-01", output_path=output,
            desc_notes_path=tmp_path / "notes.csv",
        )

        text = _pdf_text(output)
        assert "Financial Advisor Notes" in text
        assert "- Groceries are up ? cut back" in text
        assert "- Savings ? fine" in text
