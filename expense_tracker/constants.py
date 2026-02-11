"""Shared constants and configuration."""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_RAW = Path("raw")
DEFAULT_DB = Path("data/ledger.sqlite")
DEFAULT_RULES = Path("rules.csv")
DEFAULT_CARDS = Path("account-holders.csv")
DEFAULT_ODS = Path("expense-report.ods")
DEFAULT_CSV = Path("data/ledger.csv")

# ---------------------------------------------------------------------------
# Day-of-week names (Python weekday() index -> abbreviation)
# ---------------------------------------------------------------------------

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ---------------------------------------------------------------------------
# Data sheet column definitions
# ---------------------------------------------------------------------------

# Internal column keys (match SQLite column names / dict keys)
DATA_COLUMNS = [
    "date_posted",
    "month",
    "day_of_week",
    "description_raw",
    "description_clean",
    "amount_abs",
    "direction",
    "category",
    "subcategory",
    "payment_type",
    "who",
    "card_last4",
    "status",
    "account",
    "balance",
    "source_file",
    "transaction_id",
    "notes",
]

# Pretty headers shown in the ODS Data sheet and CSV export
DATA_HEADERS = [
    "Date",
    "Month",
    "Day",
    "Description (raw)",
    "Description",
    "Amount",
    "Direction",
    "Category",
    "Subcategory",
    "Payment Type",
    "Who",
    "Card",
    "Status",
    "Account",
    "Balance",
    "Source File",
    "ID",
    "Notes",
]

# Column indices in the Data sheet (0-based) used for sync-back
COL_CATEGORY = 7       # H
COL_SUBCATEGORY = 8    # I
COL_TRANSACTION_ID = 16  # Q
COL_NOTES = 17           # R
