"""
Streamlit GUI for the expense tracker.

A web-based interface for the most common operations:
- View and filter transactions
- Categorize uncategorized transactions
- Manage rules
- View spending summaries and charts
- Import new statement files
- Tools: email setup, sync, downloads, fetch from email

Launch with: streamlit run expense_tracker/gui.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# When Streamlit runs this file directly, relative imports fail because there
# is no parent package context.  Ensure the project root is on sys.path so
# absolute imports work in that scenario.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_project_root = str(_PROJECT_ROOT)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# All DEFAULT_* paths are relative to the project root.
os.chdir(_PROJECT_ROOT)

import pandas as pd
import streamlit as st

from expense_tracker.constants import (
    DEFAULT_DB, DEFAULT_RULES, DEFAULT_ODS, DEFAULT_RAW, DEFAULT_DESC_NOTES,
    DEFAULT_BACKUPS, DEFAULT_REPORTS, SAVINGS_CATEGORIES,
)
from expense_tracker.db import (
    ensure_schema, migrate_schema, fetch_all_transactions, ingest,
    ingested_source_files,
)
from expense_tracker.rules import load_rules, categorize_transactions, add_rule, remove_rule
from expense_tracker.ods import generate_ods, sync_from_ods
from expense_tracker.email_fetch import (
    load_email_config, create_email_config, DEFAULT_EMAIL_CONFIG,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Expense Tracker",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


@st.cache_data(ttl=5)
def _load_transactions(db_path: str) -> pd.DataFrame:
    """Load all transactions into a pandas DataFrame."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        migrate_schema(conn)
        rows = fetch_all_transactions(conn)
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date_posted" in df.columns:
        df["date_posted"] = pd.to_datetime(df["date_posted"])
    if "amount_abs" in df.columns:
        df["amount_abs"] = df["amount_abs"].astype(float)
    if "amount_signed" in df.columns:
        df["amount_signed"] = df["amount_signed"].astype(float)
    return df


def _get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    migrate_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

db_path = str(DEFAULT_DB)

page = st.sidebar.radio(
    "Navigation",
    ["Dashboard", "Transactions", "Categorize", "Rules", "Import", "Tools", "Manual"],
    index=0,
)

st.sidebar.divider()
st.sidebar.caption("Expense Tracker GUI")
st.sidebar.caption(f"Database: {db_path}")


# ---------------------------------------------------------------------------
# Page: Dashboard
# ---------------------------------------------------------------------------

if page == "Dashboard":
    st.title("Dashboard")

    df = _load_transactions(db_path)

    if df.empty:
        st.info("No transactions found. Import some bank statements to get started.")
        st.stop()

    # Summary metrics
    expenses = df[df["direction"] == "out"]
    # Refunds (spend < 0) reduce spending instead of counting as income;
    # savings deposits and withdrawals are neither
    savings = df["category"].isin(SAVINGS_CATEGORIES)
    spending = df[df["spend"] != 0]
    income = df[(df["direction"] == "in") & (df["spend"] == 0) & ~savings]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Expenses", f"{df['spend'].sum():,.2f} €")
    col2.metric("Total Income", f"{income['amount_abs'].sum():,.2f} €")
    col3.metric("Net", f"{df['amount_signed'].sum():,.2f} €")
    col4.metric("Transactions", len(df))

    st.divider()

    # Categorization status
    uncat = len(expenses[expenses["category"].isna() | (expenses["category"] == "")])
    total_exp = len(expenses)
    pct = ((total_exp - uncat) / total_exp * 100) if total_exp else 0

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Categorization Progress")
        st.progress(pct / 100)
        st.caption(f"{total_exp - uncat} / {total_exp} expenses categorized ({pct:.0f}%)")
        if uncat:
            st.warning(f"{uncat} uncategorized transaction(s) remaining")

    with col_b:
        st.subheader("Monthly Spending")
        if not spending.empty:
            monthly = (
                spending.groupby(spending["date_posted"].dt.to_period("M"))["spend"]
                .sum()
                .reset_index()
            )
            monthly.columns = ["Month", "Amount"]
            monthly["Month"] = monthly["Month"].astype(str)
            st.bar_chart(monthly.set_index("Month"))

    st.divider()

    # Spending by category
    col_c, col_d = st.columns(2)
    with col_c:
        st.subheader("Spending by Category")
        categorized = spending[spending["category"].notna() & (spending["category"] != "")]
        if not categorized.empty:
            by_cat = (
                categorized.groupby("category")["spend"]
                .sum()
                .sort_values(ascending=True)
            )
            st.bar_chart(by_cat)
        else:
            st.info("No categorized expenses yet.")

    with col_d:
        st.subheader("Top 10 Merchants")
        spent = expenses[~expenses["category"].isin(SAVINGS_CATEGORIES)]
        if not spent.empty:
            top = (
                spent.groupby("description_clean")["amount_abs"]
                .sum()
                .nlargest(10)
                .sort_values(ascending=True)
            )
            st.bar_chart(top)


# ---------------------------------------------------------------------------
# Page: Transactions
# ---------------------------------------------------------------------------

elif page == "Transactions":
    st.title("Transactions")

    df = _load_transactions(db_path)
    if df.empty:
        st.info("No transactions found.")
        st.stop()

    # Filters
    with st.expander("Filters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            months = sorted(df["month"].unique(), reverse=True)
            selected_months = st.multiselect("Month", months, default=[])

        with col2:
            directions = ["All", "Expenses (out)", "Income (in)"]
            direction = st.selectbox("Direction", directions)

        with col3:
            categories = sorted(
                [c for c in df["category"].dropna().unique() if c], key=str.lower
            )
            categories = ["All", "Uncategorized"] + categories
            category = st.selectbox("Category", categories)

    # Apply filters
    filtered = df.copy()
    if selected_months:
        filtered = filtered[filtered["month"].isin(selected_months)]
    if direction == "Expenses (out)":
        filtered = filtered[filtered["direction"] == "out"]
    elif direction == "Income (in)":
        filtered = filtered[filtered["direction"] == "in"]
    if category == "Uncategorized":
        filtered = filtered[filtered["category"].isna() | (filtered["category"] == "")]
    elif category != "All":
        filtered = filtered[filtered["category"] == category]

    # Search
    search = st.text_input("Search descriptions", "")
    if search:
        mask = (
            filtered["description_clean"].str.contains(search, case=False, na=False)
            | filtered["description_raw"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]

    st.caption(f"Showing {len(filtered)} of {len(df)} transactions")

    # Display
    display_cols = [
        "date_posted", "description_clean", "amount_abs", "direction",
        "category", "subcategory", "payment_type", "who",
    ]
    available = [c for c in display_cols if c in filtered.columns]
    st.dataframe(
        filtered[available],
        use_container_width=True,
        hide_index=True,
        column_config={
            "date_posted": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "description_clean": "Description",
            "amount_abs": st.column_config.NumberColumn("Amount", format="%.2f €"),
            "direction": "Direction",
            "category": "Category",
            "subcategory": "Subcategory",
            "payment_type": "Payment",
            "who": "Who",
        },
    )


# ---------------------------------------------------------------------------
# Page: Categorize
# ---------------------------------------------------------------------------

elif page == "Categorize":
    st.title("Categorize Transactions")

    df = _load_transactions(db_path)
    if df.empty:
        st.info("No transactions found.")
        st.stop()

    expenses = df[df["direction"] == "out"]
    uncategorized = expenses[
        expenses["category"].isna() | (expenses["category"] == "")
    ]

    if uncategorized.empty:
        st.success("All expenses are categorized!")
        st.stop()

    st.info(f"{len(uncategorized)} uncategorized expense(s)")

    # Group by merchant for bulk categorization
    merchant_groups = (
        uncategorized.groupby("description_clean")
        .agg(count=("amount_abs", "size"), total=("amount_abs", "sum"))
        .sort_values("total", ascending=False)
        .reset_index()
    )

    st.subheader("Uncategorized Merchants")
    st.caption("Categorize by merchant — all transactions with the same description get the rule.")

    # Get existing categories for the selectbox
    existing_cats = sorted(
        [c for c in df["category"].dropna().unique() if c], key=str.lower
    )

    for _, row in merchant_groups.head(20).iterrows():
        merchant = row["description_clean"]
        count = int(row["count"])
        total = row["total"]

        with st.container():
            col1, col2, col3, col4 = st.columns([3, 1, 1, 2])
            col1.write(f"**{merchant}**")
            col2.write(f"{count} txn(s)")
            col3.write(f"{total:.2f} €")

            with col4:
                category_options = ["-- Select --"] + existing_cats + ["+ New category"]
                selected = st.selectbox(
                    "Category",
                    category_options,
                    key=f"cat_{merchant}",
                    label_visibility="collapsed",
                )

                if selected == "+ New category":
                    new_cat = st.text_input("New category name", key=f"new_{merchant}")
                    if new_cat and st.button("Add Rule", key=f"btn_{merchant}"):
                        add_rule(DEFAULT_RULES, merchant, "description", new_cat)
                        conn = _get_connection(db_path)
                        rules = load_rules(DEFAULT_RULES)
                        categorize_transactions(conn, rules)
                        conn.close()
                        _load_transactions.clear()
                        st.rerun()

                elif selected != "-- Select --":
                    if st.button("Add Rule", key=f"btn_{merchant}"):
                        add_rule(DEFAULT_RULES, merchant, "description", selected)
                        conn = _get_connection(db_path)
                        rules = load_rules(DEFAULT_RULES)
                        categorize_transactions(conn, rules)
                        conn.close()
                        _load_transactions.clear()
                        st.rerun()

            st.divider()

    # Quick: apply all rules
    st.subheader("Apply Rules")
    if st.button("Re-apply all categorization rules"):
        conn = _get_connection(db_path)
        rules = load_rules(DEFAULT_RULES)
        updated = categorize_transactions(conn, rules)
        conn.close()
        _load_transactions.clear()
        st.success(f"Applied {len(rules)} rules, categorized {updated} transaction(s).")
        st.rerun()


# ---------------------------------------------------------------------------
# Page: Rules
# ---------------------------------------------------------------------------

elif page == "Rules":
    st.title("Categorization Rules")

    rules = load_rules(DEFAULT_RULES)

    if not rules:
        st.info("No rules defined yet.")
    else:
        rules_df = pd.DataFrame(rules)
        display_cols = ["pattern", "match_field", "category", "subcategory", "payment_type"]
        available = [c for c in display_cols if c in rules_df.columns]
        st.dataframe(
            rules_df[available],
            use_container_width=True,
            hide_index=True,
            column_config={
                "pattern": "Pattern",
                "match_field": "Match Field",
                "category": "Category",
                "subcategory": "Subcategory",
                "payment_type": "Payment Type",
            },
        )

    st.divider()

    # Add rule form
    st.subheader("Add a Rule")
    with st.form("add_rule"):
        col1, col2 = st.columns(2)
        with col1:
            pattern = st.text_input("Pattern (text to match)")
            match_field = st.selectbox("Match field", ["description", "description_raw"])
        with col2:
            category = st.text_input("Category")
            subcategory = st.text_input("Subcategory (optional)")

        submitted = st.form_submit_button("Add Rule")
        if submitted and pattern and category:
            add_rule(DEFAULT_RULES, pattern, match_field, category, subcategory)
            st.success(f"Added rule: {pattern} -> {category}")
            st.rerun()
        elif submitted:
            st.error("Pattern and category are required.")

    st.divider()

    # Remove rule
    st.subheader("Remove a Rule")
    if rules:
        patterns = [r["pattern"] for r in rules]
        to_remove = st.selectbox("Select rule to remove", ["-- Select --"] + patterns)
        if to_remove != "-- Select --" and st.button("Remove Rule"):
            remove_rule(DEFAULT_RULES, to_remove)
            st.success(f"Removed rule: {to_remove}")
            st.rerun()


# ---------------------------------------------------------------------------
# Page: Import
# ---------------------------------------------------------------------------

elif page == "Import":
    st.title("Import Bank Statements")

    st.subheader("Upload Files")
    uploaded = st.file_uploader(
        "Upload CSV or PDF bank statements",
        type=["csv", "pdf"],
        accept_multiple_files=True,
    )

    if uploaded:
        raw_dir = DEFAULT_RAW
        raw_dir.mkdir(parents=True, exist_ok=True)

        saved_files = []
        for f in uploaded:
            dest = raw_dir / f.name
            dest.write_bytes(f.getvalue())
            saved_files.append(dest)
            st.caption(f"Saved: {f.name}")

        if st.button("Ingest uploaded files"):
            with st.spinner("Importing..."):
                ingest(DEFAULT_DB, saved_files)
                conn = _get_connection(db_path)
                rules = load_rules(DEFAULT_RULES)
                if rules:
                    categorize_transactions(conn, rules)
                conn.close()
                generate_ods(DEFAULT_DB, DEFAULT_RULES, DEFAULT_ODS, DEFAULT_DESC_NOTES)
                _load_transactions.clear()
            st.success(f"Imported {len(saved_files)} file(s) and regenerated report.")
            st.rerun()

    st.divider()

    # Show existing files in raw/
    st.subheader("Files in raw/")
    raw_dir = DEFAULT_RAW
    if raw_dir.is_dir():
        files = sorted(raw_dir.iterdir())
        if files:
            already = ingested_source_files(DEFAULT_DB)
            for f in files:
                status = "imported" if f.name in already else "new"
                icon = "✅" if status == "imported" else "🆕"
                st.caption(f"{icon} {f.name} ({status})")
        else:
            st.caption("No files in raw/ directory.")
    else:
        st.caption("raw/ directory not found.")


# ---------------------------------------------------------------------------
# Page: Tools
# ---------------------------------------------------------------------------

elif page == "Tools":
    st.title("Tools")

    # ── Email Configuration ───────────────────────────────────────────────
    st.subheader("📧 Email Configuration")
    st.caption(
        "Configure email (IMAP) settings to automatically fetch bank statement "
        "attachments. You'll need an app-specific password."
    )

    config_path = DEFAULT_EMAIL_CONFIG
    has_config = config_path.exists()

    if has_config:
        try:
            config = load_email_config(config_path)
            st.success("Email is configured.")
            col1, col2 = st.columns(2)
            col1.text_input("IMAP Host", value=config.get("imap_host", ""), disabled=True)
            col2.text_input("Email", value=config.get("email", ""), disabled=True)
            col3, col4 = st.columns(2)
            col3.text_input("Port", value=str(config.get("imap_port", 993)), disabled=True)
            col4.text_input("Folder", value=config.get("folder", "INBOX"), disabled=True)
        except Exception as e:
            st.error(f"Error reading config: {e}")
            has_config = False

    if has_config:
        if st.checkbox("Update email settings"):
            _show_email_form = True
        else:
            _show_email_form = False
    else:
        st.info("No email configuration found. Set up below to enable automatic statement fetching.")
        _show_email_form = True

    if _show_email_form:
        with st.form("email_config"):
            st.caption(
                "For Gmail, use an [App Password](https://myaccount.google.com/apppasswords). "
                "For Outlook, use an [App Password](https://account.live.com/proofs/AppPassword)."
            )
            ec1, ec2 = st.columns(2)
            with ec1:
                imap_host = st.text_input(
                    "IMAP Host",
                    value="imap.gmail.com",
                    key="email_imap_host",
                )
                email_addr = st.text_input("Email Address", key="email_addr")
                password = st.text_input("App Password", type="password", key="email_pass")
            with ec2:
                imap_port = st.number_input("IMAP Port", value=993, key="email_port")
                folder = st.text_input("Mailbox Folder", value="INBOX", key="email_folder")
                bank_senders = st.text_input(
                    "Bank sender patterns (comma-separated)",
                    value="bank-a.example, bank-a.example",
                    key="email_senders",
                )

            if st.form_submit_button("Save Email Configuration"):
                if imap_host and email_addr and password:
                    senders = [s.strip() for s in bank_senders.split(",") if s.strip()]
                    create_email_config(
                        config_path,
                        imap_host=imap_host,
                        email_addr=email_addr,
                        password=password,
                        imap_port=int(imap_port),
                        bank_senders=senders or None,
                        folder=folder,
                    )
                    st.success(f"Email configuration saved to {config_path}")
                    st.rerun()
                else:
                    st.error("IMAP host, email address, and password are required.")

    st.divider()

    # ── Fetch from Email ──────────────────────────────────────────────────
    st.subheader("📥 Fetch Statements from Email")
    st.caption("Download new bank statement attachments from your email account.")

    if not config_path.exists():
        st.warning("Configure email settings above first.")
    else:
        fetch_col1, fetch_col2 = st.columns(2)
        with fetch_col1:
            days_back = st.number_input("Days to look back", value=60, min_value=1, max_value=365)
        with fetch_col2:
            auto_ingest = st.checkbox("Auto-ingest downloaded files", value=True)

        if st.button("Fetch from Email"):
            with st.spinner("Connecting to email server..."):
                try:
                    from expense_tracker.email_fetch import fetch_and_report
                    downloaded = fetch_and_report(
                        config_path=config_path,
                        output_dir=DEFAULT_RAW,
                        days_back=days_back,
                    )
                    if downloaded:
                        st.success(f"Downloaded {len(downloaded)} file(s):")
                        for f in downloaded:
                            st.caption(f"  {f.name}")
                        if auto_ingest:
                            with st.spinner("Ingesting downloaded files..."):
                                ingest(DEFAULT_DB, downloaded)
                                conn = _get_connection(db_path)
                                rules = load_rules(DEFAULT_RULES)
                                if rules:
                                    categorize_transactions(conn, rules)
                                conn.close()
                                generate_ods(DEFAULT_DB, DEFAULT_RULES, DEFAULT_ODS, DEFAULT_DESC_NOTES)
                                _load_transactions.clear()
                            st.success("Files ingested and report regenerated.")
                    else:
                        st.info("No new statement attachments found.")
                except FileNotFoundError as e:
                    st.error(f"Config error: {e}")
                except Exception as e:
                    st.error(f"Email fetch failed: {e}")

    st.divider()

    # ── Sync & Regenerate ─────────────────────────────────────────────────
    st.subheader("🔄 Sync & Regenerate")
    st.caption(
        "Sync manual edits from the ODS spreadsheet back to the database, "
        "re-apply all categorization rules, and regenerate the report."
    )

    sync_col1, sync_col2 = st.columns(2)
    with sync_col1:
        if st.button("Sync ODS & Regenerate Report"):
            with st.spinner("Syncing and regenerating..."):
                # Sync manual edits from ODS
                synced = sync_from_ods(DEFAULT_DB, DEFAULT_ODS, DEFAULT_DESC_NOTES)

                # Apply rules
                conn = _get_connection(db_path)
                rules = load_rules(DEFAULT_RULES)
                updated = 0
                if rules:
                    updated = categorize_transactions(conn, rules)
                conn.close()

                # Regenerate ODS
                generate_ods(DEFAULT_DB, DEFAULT_RULES, DEFAULT_ODS, DEFAULT_DESC_NOTES)
                _load_transactions.clear()

            msgs = []
            if synced:
                msgs.append(f"Synced {synced} edit(s) from ODS")
            if updated:
                msgs.append(f"categorized {updated} transaction(s)")
            msgs.append("report regenerated")
            st.success("Done! " + ", ".join(msgs) + ".")

    with sync_col2:
        if st.button("Re-apply All Rules"):
            with st.spinner("Applying rules..."):
                conn = _get_connection(db_path)
                rules = load_rules(DEFAULT_RULES)
                updated = categorize_transactions(conn, rules)
                conn.close()
                _load_transactions.clear()
            st.success(f"Applied {len(rules)} rules, categorized {updated} transaction(s).")

    st.divider()

    # ── Download Reports ──────────────────────────────────────────────────
    st.subheader("📊 Download Reports")

    dl_col1, dl_col2 = st.columns(2)

    with dl_col1:
        st.caption("**ODS Spreadsheet Report**")
        ods_path = DEFAULT_ODS
        if ods_path.exists():
            ods_bytes = ods_path.read_bytes()
            st.download_button(
                label=f"Download {ods_path.name}",
                data=ods_bytes,
                file_name=ods_path.name,
                mime="application/vnd.oasis.opendocument.spreadsheet",
            )
            import os
            size_kb = os.path.getsize(ods_path) / 1024
            st.caption(f"Size: {size_kb:.0f} KB")
        else:
            st.info("No ODS report found. Run Sync & Regenerate first.")

    with dl_col2:
        st.caption("**Monthly PDF Reports**")
        if pdf_msg := st.session_state.pop("pdf_generation_message", None):
            st.success(pdf_msg)

        reports_dir = DEFAULT_REPORTS
        if reports_dir.is_dir():
            pdfs = sorted(reports_dir.glob("*.pdf"), reverse=True)
            if pdfs:
                for pdf_file in pdfs:
                    pdf_bytes = pdf_file.read_bytes()
                    st.download_button(
                        label=f"Download {pdf_file.name}",
                        data=pdf_bytes,
                        file_name=pdf_file.name,
                        mime="application/pdf",
                        key=f"dl_{pdf_file.name}",
                    )
            else:
                st.info("No PDF reports found.")
        else:
            st.info("No reports/ directory found.")

        st.caption("")  # spacer
        st.caption(
            "Creates any missing monthly PDFs since your last report, "
            "up through the previous calendar month."
        )
        if st.button("Generate PDF for Previous Month"):
            with st.spinner("Generating PDF..."):
                try:
                    from expense_tracker.pdf_report import (
                        generate_missing_monthly_pdfs,
                        months_to_generate,
                    )
                    pending = months_to_generate(DEFAULT_REPORTS)
                    pdf_paths = generate_missing_monthly_pdfs(
                        db_path=DEFAULT_DB,
                        reports_dir=DEFAULT_REPORTS,
                        desc_notes_path=DEFAULT_DESC_NOTES,
                    )
                    if len(pdf_paths) == 1:
                        st.session_state["pdf_generation_message"] = (
                            f"PDF saved to {pdf_paths[0]}"
                        )
                    else:
                        names = ", ".join(p.name for p in pdf_paths)
                        st.session_state["pdf_generation_message"] = (
                            f"Generated {len(pdf_paths)} PDFs: {names} "
                            f"(months: {', '.join(pending)})"
                        )
                    st.rerun()
                except Exception as e:
                    st.error(f"PDF generation failed: {e}")

    st.divider()

    # ── Backup ────────────────────────────────────────────────────────────
    st.subheader("💾 Backup")
    st.caption("Create a zip archive of all your data (database, rules, raw files, and reports).")

    bk_col1, bk_col2 = st.columns(2)
    with bk_col1:
        if st.button("Create Backup"):
            with st.spinner("Creating backup..."):
                try:
                    from expense_tracker.backup import create_backup, format_size
                    zp = create_backup(DEFAULT_BACKUPS, raw_dir=DEFAULT_RAW, ods_path=DEFAULT_ODS)
                    size = format_size(zp.stat().st_size)
                    st.success(f"Backup created: {zp.name} ({size})")
                except Exception as e:
                    st.error(f"Backup failed: {e}")

    with bk_col2:
        # List existing backups
        if DEFAULT_BACKUPS.is_dir():
            backups = sorted(DEFAULT_BACKUPS.glob("*.zip"), reverse=True)
            if backups:
                st.caption(f"{len(backups)} backup(s) in {DEFAULT_BACKUPS}/")
                for bk in backups[:5]:
                    bk_size = bk.stat().st_size / 1024
                    st.caption(f"  {bk.name} ({bk_size:.0f} KB)")
                if len(backups) > 5:
                    st.caption(f"  ... and {len(backups) - 5} more")

    st.divider()

    # ── Data Export ───────────────────────────────────────────────────────
    st.subheader("📤 Export Data")
    st.caption("Export all transactions as a UTF-8 CSV file for use in other tools.")

    if st.button("Export to CSV"):
        with st.spinner("Exporting..."):
            try:
                from expense_tracker.export import export_csv
                from expense_tracker.constants import DEFAULT_CSV
                export_csv(DEFAULT_DB, DEFAULT_CSV)
                st.success(f"Exported to {DEFAULT_CSV}")

                csv_bytes = DEFAULT_CSV.read_bytes()
                st.download_button(
                    label=f"Download {DEFAULT_CSV.name}",
                    data=csv_bytes,
                    file_name=DEFAULT_CSV.name,
                    mime="text/csv",
                )
            except Exception as e:
                st.error(f"Export failed: {e}")

    st.divider()

    # ── Starter Rules ─────────────────────────────────────────────────────
    st.subheader("🇵🇹 Portuguese Starter Rules")
    st.caption(
        "Import ~100 curated categorization rules for common Portuguese merchants "
        "and services (supermarkets, utilities, telecoms, fuel, transport, etc.)."
    )

    if st.button("Import Starter Rules"):
        with st.spinner("Importing starter rules..."):
            try:
                from expense_tracker.starter_rules import import_starter_rules
                added, skipped = import_starter_rules(DEFAULT_RULES)
                if added:
                    st.success(
                        f"Imported {added} starter rule(s). "
                        f"({skipped} skipped — already present)"
                    )
                    # Re-apply rules after import
                    conn = _get_connection(db_path)
                    rules = load_rules(DEFAULT_RULES)
                    categorize_transactions(conn, rules)
                    conn.close()
                    _load_transactions.clear()
                else:
                    st.info("All starter rules are already present. Nothing to import.")
            except Exception as e:
                st.error(f"Import failed: {e}")


# ---------------------------------------------------------------------------
# Page: Manual
# ---------------------------------------------------------------------------

elif page == "Manual":
    st.title("Manual")
    st.caption(
        "Full project documentation, always in sync with the source files. "
        "Use the tabs below to browse different sections."
    )

    # Discover markdown docs from the project root
    _docs_dir = Path(__file__).resolve().parent.parent
    _doc_files = {
        "User Guide": _docs_dir / "README.md",
        "Deployment (Docker / NUC)": _docs_dir / "DEPLOYMENT.md",
        "Developer Guide": _docs_dir / "CONTRIBUTING.md",
    }

    # Build tabs for each doc that exists
    available_docs = {name: path for name, path in _doc_files.items() if path.exists()}

    if not available_docs:
        st.warning("No documentation files found.")
        st.stop()

    tabs = st.tabs(list(available_docs.keys()))

    for tab, (doc_name, doc_path) in zip(tabs, available_docs.items()):
        with tab:
            try:
                content = doc_path.read_text(encoding="utf-8")

                # Add a quick-search feature
                search_term = st.text_input(
                    "Search in this document",
                    key=f"search_{doc_name}",
                    placeholder="Type to filter sections...",
                )

                if search_term:
                    # Split into sections and filter
                    sections = content.split("\n## ")
                    matching = []
                    for i, section in enumerate(sections):
                        if search_term.lower() in section.lower():
                            prefix = "" if i == 0 else "## "
                            matching.append(prefix + section)

                    if matching:
                        st.caption(
                            f"Showing {len(matching)} section(s) matching "
                            f"**{search_term}**"
                        )
                        st.markdown("\n\n---\n\n".join(matching), unsafe_allow_html=True)
                    else:
                        st.info(f"No sections found matching \"{search_term}\".")
                else:
                    st.markdown(content, unsafe_allow_html=True)

            except Exception as e:
                st.error(f"Could not load {doc_path.name}: {e}")
