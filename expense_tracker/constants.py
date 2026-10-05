"""Shared constants and configuration."""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_RAW = Path("raw")
DEFAULT_DB = Path("data/ledger.sqlite")
DEFAULT_RULES = Path("data/rules.csv")
DEFAULT_CARDS = Path("data/account-holders.csv")
DEFAULT_ODS = Path("expense-report.ods")
DEFAULT_CSV = Path("data/ledger.csv")
DEFAULT_NOISE_WORDS = Path("data/noise-words.txt")
DEFAULT_CLEANING_PATTERNS = Path("data/cleaning-patterns.csv")
DEFAULT_DESC_NOTES = Path("data/description-notes.csv")
DEFAULT_BACKUPS = Path("backups")
DEFAULT_REPORTS = Path("reports")
DEFAULT_ADVISOR_DIR = Path("data/advisor")

# ---------------------------------------------------------------------------
# Day-of-week names (Python weekday() index -> abbreviation)
# ---------------------------------------------------------------------------

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

# Categories whose incoming money is real income or a move between accounts.
# An incoming transaction in any other category is a refund: it reduces that
# category's spending instead of counting as income.
NON_SPENDING_CATEGORIES = ("Income", "Transfers")

# Money moved into or out of savings products (e.g. a savings or
# investment account): deposits are not spending and withdrawals are not income.
SAVINGS_CATEGORIES = ("Savings",)

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
    "merchant_note",
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
    "Merchant Note",
]

# Column indices in the Data sheet (0-based) used for sync-back
COL_DESCRIPTION_CLEAN = 4  # E
COL_CATEGORY = 7           # H
COL_SUBCATEGORY = 8        # I
COL_TRANSACTION_ID = 16    # Q
COL_NOTES = 17             # R
COL_MERCHANT_NOTE = 18     # S


# ---------------------------------------------------------------------------
# Account label
# ---------------------------------------------------------------------------

DEFAULT_ACCOUNT_TYPE = "checkings_account"


def account_type() -> str:
    """Account label written on imported rows (EXPENSE_TRACKER_ACCOUNT_TYPE)."""
    return os.environ.get("EXPENSE_TRACKER_ACCOUNT_TYPE", DEFAULT_ACCOUNT_TYPE)
