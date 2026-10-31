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

import logging
import os
import sqlite3
import sys
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
    BUILTIN_CATEGORIES,
    DEFAULT_BACKUPS,
    DEFAULT_DB,
    DEFAULT_DESC_NOTES,
    DEFAULT_ODS,
    DEFAULT_RAW,
    DEFAULT_REPORTS,
    DEFAULT_RULES,
    IGNORED_CATEGORIES,
    MISCELLANEOUS,
    NEUTRAL_CATEGORIES,
)
from expense_tracker.db import (
    IngestError,
    coverage,
    ensure_schema,
    fetch_all_transactions,
    ingest,
    ingested_source_files,
    migrate_schema,
)
from expense_tracker.email_fetch import (
    DEFAULT_EMAIL_CONFIG,
    create_email_config,
    load_email_config,
)
from expense_tracker.ods import generate_ods, sync_from_ods
from expense_tracker.rules import (
    SHORT_PATTERN_LENGTH,
    CategoryError,
    DuplicateRuleError,
    RulesFileError,
    add_category,
    add_rule,
    categories_file,
    categorize_transactions,
    load_categories,
    load_rules,
    remove_category,
    remove_rule,
    short_pattern_warning,
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


def _add_rule(
    pattern: str, match_field: str, category: str, subcategory: str = ""
) -> bool:
    """add_rule(), keeping its short-pattern warning for after the rerun.

    A rule it refuses (a duplicate, a category not in the list, or an
    unreadable rules file) is shown as an error and False is returned:
    nothing was written.
    """
    try:
        add_rule(DEFAULT_RULES, pattern, match_field, category, subcategory)
    except (DuplicateRuleError, CategoryError, RulesFileError) as e:
        st.error(f"{e}. The rule was not added.")
        return False
    if warning := short_pattern_warning(pattern):
        st.session_state["rule_warning"] = warning
    return True


# The select-box entry that asks for a new category name
ADD_CATEGORY = "Add category..."


def _categories() -> list[str]:
    """The category list, seeded from the rules and the ledger on first use."""
    path = categories_file(DEFAULT_RULES)
    try:
        return load_categories(path, DEFAULT_RULES, DEFAULT_DB)
    except RulesFileError:
        # Already shown where the rules are used; seed from the rest
        return load_categories(path, db_path=DEFAULT_DB)


def _category_select(
    key: str,
    categories: list[str],
    none_option: str | None = None,
    label: str = "Category",
    **kwargs,
) -> str | None:
    """A select box over the category list, ending in ADD_CATEGORY.

    Returns the choice (ADD_CATEGORY included), or None for `none_option`
    or nothing chosen yet.
    """
    options = ([none_option] if none_option else []) + categories + [ADD_CATEGORY]
    choice = st.selectbox(
        label,
        options,
        key=key,
        index=0 if none_option else None,
        placeholder="Choose a category",
        **kwargs,
    )
    return None if choice == none_option else choice


def _chosen_category(choice: str, new_name_key: str) -> str | None:
    """The category to use for `choice`: for ADD_CATEGORY, the name typed in
    the `new_name_key` input, added to the list first. On a refusal the
    error is shown and None returned."""
    if choice != ADD_CATEGORY:
        return choice
    try:
        return add_category(
            categories_file(DEFAULT_RULES),
            st.session_state.get(new_name_key, ""),
            DEFAULT_RULES,
            DEFAULT_DB,
        )
    except (CategoryError, RulesFileError) as e:
        st.error(f"{e}. The rule was not added.")
        return None


def _load_rules() -> list[dict]:
    """load_rules(), or stop the page with the error: an unreadable rules
    file must not be applied as an empty rule set."""
    try:
        return load_rules(DEFAULT_RULES)
    except RulesFileError as e:
        st.error(f"{e}. Stopped: nothing was imported or categorized.")
        st.stop()


def _show_rule_warning() -> None:
    if warning := st.session_state.pop("rule_warning", None):
        st.warning(warning)


def _upload_name(name: str) -> str:
    """Last path component of an uploaded file's name ("" if it has none).

    The name is chosen by the browser that sends the upload: used as it
    comes, "../x.csv" or an absolute path would be written outside raw/.
    """
    base = Path(name.replace("\\", "/")).name
    return "" if base in (".", "..") else base


def _save_upload(raw_dir: Path, name: str, data: bytes) -> Path:
    """Write an upload to raw_dir without replacing a different file.

    Banks reuse download names, so a file of that name may already hold
    another statement: this one then becomes "name (2).ext". The page saves
    its uploads again on every rerun, so the same content is written once.
    """
    dest = raw_dir / name
    n = 1
    while dest.exists():
        if dest.is_file() and dest.read_bytes() == data:
            return dest
        n += 1
        dest = raw_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
    dest.write_bytes(data)
    return dest


class _ParserWarnings(logging.Handler):
    """Collects what the package (parsers, ODS sync) reports with
    logger.warning()."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _ingest_files(paths: list[Path]) -> dict:
    """Run ingest() and return what it only prints to the server console.

    ingest() imports the files it can, reports rows it had to skip and
    balances that do not add up as warnings, and raises IngestError for the
    files it could not import: none of that may pass for a clean import.
    """
    collected = _ParserWarnings()
    package_logger = logging.getLogger("expense_tracker")
    package_logger.addHandler(collected)
    error = ""
    try:
        ingest(DEFAULT_DB, paths)
    except IngestError as e:
        error = str(e)
    finally:
        package_logger.removeHandler(collected)
    return {"files": len(paths), "error": error, "warnings": collected.messages}


def _sync_ods() -> tuple[int, list[str]]:
    """sync_from_ods() against the category list: the number of rows it
    updated, and the edits it skipped (categories not in the list)."""
    categories = _categories()
    collected = _ParserWarnings()
    package_logger = logging.getLogger("expense_tracker")
    package_logger.addHandler(collected)
    try:
        synced = sync_from_ods(
            DEFAULT_DB, DEFAULT_ODS, DEFAULT_DESC_NOTES, categories=categories
        )
    finally:
        package_logger.removeHandler(collected)
    return synced, collected.messages


def _show_sync_warnings(messages: list[str]) -> None:
    for message in messages:
        st.warning(message)


def _show_import_result(result: dict) -> None:
    """Render the outcome of _ingest_files() once the report is regenerated."""
    if result["error"]:
        st.error(f"Import failed: {result['error']}. The report was regenerated.")
    if result["warnings"]:
        st.warning(
            f"{len(result['warnings'])} warning(s) while importing: "
            "transactions may be missing or wrong. Check them against the statement."
        )
        for message in result["warnings"]:
            st.text(message)
    if not result["error"] and not result["warnings"]:
        st.success(f"Imported {result['files']} file(s) and regenerated report.")
    _show_sync_warnings(result.get("sync_warnings", []))


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
    # savings and ignored transactions are neither
    neutral = df["category"].isin(NEUTRAL_CATEGORIES)
    spending = df[df["spend"] != 0]
    income = df[(df["direction"] == "in") & (df["spend"] == 0) & ~neutral]

    col1, col2, col3, col4 = st.columns(4)
    total_expenses = df["spend"].sum()
    total_income = income["amount_abs"].sum()
    col1.metric("Total Expenses", f"{total_expenses:,.2f} €")
    col2.metric("Total Income", f"{total_income:,.2f} €")
    col3.metric("Net", f"{total_income - total_expenses:,.2f} €")
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
        st.caption(
            f"{total_exp - uncat} / {total_exp} expenses categorized ({pct:.0f}%)"
        )
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
        categorized = spending[
            spending["category"].notna() & (spending["category"] != "")
        ]
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
        spent = expenses[~expenses["category"].isin(NEUTRAL_CATEGORIES)]
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
        mask = filtered["description_clean"].str.contains(
            search, case=False, na=False
        ) | filtered["description_raw"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]

    st.caption(f"Showing {len(filtered)} of {len(df)} transactions")

    # Display
    display_cols = [
        "date_posted",
        "description_clean",
        "amount_abs",
        "direction",
        "category",
        "subcategory",
        "payment_type",
        "who",
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
    _show_rule_warning()

    df = _load_transactions(db_path)
    if df.empty:
        st.info("No transactions found.")
        st.stop()

    expenses = df[df["direction"] == "out"]
    uncategorized = expenses[expenses["category"].isna() | (expenses["category"] == "")]

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
    st.caption(
        "Categorize by merchant — all transactions with the same description get the rule."
    )

    # The category list (with Miscellaneous for a merchant with no better
    # home, and Ignored for one to leave out of every figure)
    categories = _categories()

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
                selected = _category_select(
                    f"cat_{merchant}",
                    categories,
                    none_option="-- Select --",
                    label_visibility="collapsed",
                )
                ready = selected is not None
                if selected == ADD_CATEGORY:
                    ready = bool(
                        st.text_input("New category name", key=f"new_{merchant}")
                    )
                if ready and st.button("Add Rule", key=f"btn_{merchant}"):
                    category = _chosen_category(selected, f"new_{merchant}")
                    if category and _add_rule(merchant, "description", category):
                        rules = _load_rules()
                        conn = _get_connection(db_path)
                        categorize_transactions(conn, rules)
                        conn.close()
                        _load_transactions.clear()
                        st.rerun()

            st.divider()

    # Quick: apply all rules
    st.subheader("Apply Rules")
    if st.button("Re-apply all categorization rules"):
        rules = _load_rules()
        conn = _get_connection(db_path)
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
    _show_rule_warning()

    try:
        rules = load_rules(DEFAULT_RULES)
    except RulesFileError as e:
        # Still offer the form below: adding a rule creates a missing file
        st.error(str(e))
        rules = []
    else:
        if not rules:
            st.info("No rules defined yet.")

    if rules:
        rules_df = pd.DataFrame(rules)
        display_cols = [
            "pattern",
            "match_field",
            "category",
            "subcategory",
            "payment_type",
        ]
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

    # Add rule form (not an st.form: choosing "Add category..." must show
    # the name input at once)
    st.subheader("Add a Rule")
    categories = _categories()
    col1, col2 = st.columns(2)
    with col1:
        pattern = st.text_input("Pattern (text to match)", key="rule_pattern")
        match_field = st.selectbox(
            "Match field", ["description", "description_raw"], key="rule_field"
        )
    with col2:
        category = _category_select(
            "rule_category",
            categories,
            help=(
                f"Use {MISCELLANEOUS} for transactions with no better "
                f"category, and {IGNORED_CATEGORIES[0]} for ones to leave out "
                "of every total (e.g. transfers between your own accounts)."
            ),
        )
        if category == ADD_CATEGORY:
            st.text_input("New category name", key="rule_category_new")
        subcategory = st.text_input("Subcategory (optional)", key="rule_subcategory")

    if st.button("Add Rule"):
        if not pattern or not category:
            st.error("Pattern and category are required.")
        elif (chosen := _chosen_category(category, "rule_category_new")) and (
            _add_rule(pattern, match_field, chosen, subcategory)
        ):
            st.success(f"Added rule: {pattern} -> {chosen}")
            st.rerun()

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

    st.divider()

    # The category list
    st.subheader("Categories")
    st.caption(
        "Rules, the merchant panel and spreadsheet edits pick from this list. "
        "Built-in categories, and categories a rule or transaction uses, "
        "cannot be removed."
    )
    if message := st.session_state.pop("category_message", None):
        st.success(message)
    st.write(", ".join(categories))
    cat_col1, cat_col2 = st.columns(2)
    with cat_col1:
        new_name = st.text_input("Category name", key="category_add_name")
        if st.button("Add Category"):
            try:
                added = add_category(
                    categories_file(DEFAULT_RULES), new_name, DEFAULT_RULES, DEFAULT_DB
                )
            except (CategoryError, RulesFileError) as e:
                st.error(str(e))
            else:
                st.session_state["category_message"] = f"Added category: {added}"
                st.rerun()
    with cat_col2:
        removable = [c for c in categories if c not in BUILTIN_CATEGORIES]
        to_drop = st.selectbox(
            "Category to remove", ["-- Select --"] + removable, key="category_remove"
        )
        if to_drop != "-- Select --" and st.button("Remove Category"):
            try:
                removed = remove_category(
                    categories_file(DEFAULT_RULES), to_drop, DEFAULT_RULES, DEFAULT_DB
                )
            except (CategoryError, RulesFileError) as e:
                st.error(str(e))
            else:
                st.session_state["category_message"] = f"Removed category: {removed}"
                st.rerun()


# ---------------------------------------------------------------------------
# Page: Import
# ---------------------------------------------------------------------------

elif page == "Import":
    st.title("Import Bank Statements")

    # Outcome of the import that ended in the rerun below
    if import_result := st.session_state.pop("import_result", None):
        _show_import_result(import_result)

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
            name = _upload_name(f.name)
            if not name:
                st.error(f"Not saved: {f.name!r} is not a file name.")
                continue
            dest = _save_upload(raw_dir, name, f.getvalue())
            saved_files.append(dest)
            st.caption(f"Saved: {dest.name}")

        if st.button("Ingest uploaded files"):
            rules = _load_rules()
            with st.spinner("Importing..."):
                import_result = _ingest_files(saved_files)
                # Save unsynced ODS edits before the report is regenerated
                import_result["sync_warnings"] = _sync_ods()[1]
                conn = _get_connection(db_path)
                categorize_transactions(conn, rules)
                conn.close()
                generate_ods(DEFAULT_DB, DEFAULT_RULES, DEFAULT_ODS, DEFAULT_DESC_NOTES)
                _load_transactions.clear()
            # A message written here would be wiped by the rerun
            st.session_state["import_result"] = import_result
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

    st.subheader("Statement Coverage")
    st.caption(
        "Months covered by imported statements, per account, and the months "
        "missing between the first and the last. A statement covers each month "
        "from its first to its last transaction; a month covered only in part "
        "is not flagged."
    )
    accounts = []
    if DEFAULT_DB.exists():
        conn = _get_connection(db_path)
        try:
            accounts = coverage(conn)
        finally:
            conn.close()
    if accounts:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Account": a.account,
                        "Months covered": ", ".join(a.ranges()),
                        "Missing months": ", ".join(a.missing) or "none",
                    }
                    for a in accounts
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.caption("No statements imported yet.")


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
            col1.text_input(
                "IMAP Host", value=config.get("imap_host", ""), disabled=True
            )
            col2.text_input("Email", value=config.get("email", ""), disabled=True)
            col3, col4 = st.columns(2)
            col3.text_input(
                "Port", value=str(config.get("imap_port", 993)), disabled=True
            )
            col4.text_input(
                "Folder", value=config.get("folder", "INBOX"), disabled=True
            )
        except Exception as e:
            st.error(f"Error reading config: {e}")
            has_config = False

    if has_config:
        if st.checkbox("Update email settings"):
            _show_email_form = True
        else:
            _show_email_form = False
    else:
        st.info(
            "No email configuration found. Set up below to enable automatic statement fetching."
        )
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
                password = st.text_input(
                    "App Password", type="password", key="email_pass"
                )
            with ec2:
                imap_port = st.number_input("IMAP Port", value=993, key="email_port")
                folder = st.text_input(
                    "Mailbox Folder", value="INBOX", key="email_folder"
                )
                bank_senders = st.text_input(
                    "Bank sender patterns (comma-separated)",
                    placeholder="mybank.example",
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
            days_back = st.number_input(
                "Days to look back", value=60, min_value=1, max_value=365
            )
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
                            rules = _load_rules()
                            with st.spinner("Ingesting downloaded files..."):
                                import_result = _ingest_files(downloaded)
                                # Save unsynced ODS edits before regenerating
                                import_result["sync_warnings"] = _sync_ods()[1]
                                conn = _get_connection(db_path)
                                categorize_transactions(conn, rules)
                                conn.close()
                                generate_ods(
                                    DEFAULT_DB,
                                    DEFAULT_RULES,
                                    DEFAULT_ODS,
                                    DEFAULT_DESC_NOTES,
                                )
                                _load_transactions.clear()
                            _show_import_result(import_result)
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
            rules = _load_rules()
            with st.spinner("Syncing and regenerating..."):
                # Sync manual edits from ODS
                synced, skipped = _sync_ods()

                # Apply rules
                conn = _get_connection(db_path)
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
            _show_sync_warnings(skipped)

    with sync_col2:
        if st.button("Re-apply All Rules"):
            rules = _load_rules()
            with st.spinner("Applying rules..."):
                conn = _get_connection(db_path)
                updated = categorize_transactions(conn, rules)
                conn.close()
                _load_transactions.clear()
            st.success(
                f"Applied {len(rules)} rules, categorized {updated} transaction(s)."
            )

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
    st.caption(
        "Create a zip archive of all your data (database, rules, raw files, and reports)."
    )

    bk_col1, bk_col2 = st.columns(2)
    with bk_col1:
        if st.button("Create Backup"):
            with st.spinner("Creating backup..."):
                try:
                    from expense_tracker.backup import create_backup, format_size

                    zp = create_backup(
                        DEFAULT_BACKUPS, raw_dir=DEFAULT_RAW, ods_path=DEFAULT_ODS
                    )
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
                from expense_tracker.constants import DEFAULT_CSV
                from expense_tracker.export import export_csv

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
        "Import ~200 curated categorization rules for common Portuguese merchants "
        "and services (supermarkets, utilities, telecoms, fuel, transport, etc.)."
    )

    if st.button("Import Starter Rules"):
        with st.spinner("Importing starter rules..."):
            try:
                from expense_tracker.starter_rules import import_starter_rules

                added, skipped, short = import_starter_rules(DEFAULT_RULES)
                if added:
                    st.success(
                        f"Imported {added} starter rule(s). "
                        f"({skipped} skipped — already present)"
                    )
                    if short:
                        st.warning(
                            f"{short} of them are shorter than "
                            f"{SHORT_PATTERN_LENGTH} characters without a "
                            "trailing space, so they can match inside longer "
                            "words. End a pattern with a space on the Rules "
                            "page to match only the whole word."
                        )
                    # Re-apply rules after import
                    rules = _load_rules()
                    conn = _get_connection(db_path)
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
                        st.markdown(
                            "\n\n---\n\n".join(matching), unsafe_allow_html=True
                        )
                    else:
                        st.info(f'No sections found matching "{search_term}".')
                else:
                    st.markdown(content, unsafe_allow_html=True)

            except Exception as e:
                st.error(f"Could not load {doc_path.name}: {e}")
