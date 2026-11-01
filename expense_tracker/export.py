"""CSV export functionality."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .constants import DATA_COLUMNS, DATA_HEADERS
from .db import fetch_all_transactions, migrate_schema, resolve_currency


def export_csv(db_path: Path, out_path: Path, currency: str | None = None) -> None:
    """Export the ledger's transactions in one currency to a clean UTF-8 CSV.

    ``currency`` may be left out when the ledger holds one (see
    db.resolve_currency); the CSV has no currency column.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_schema(conn)
        currency = resolve_currency(conn, currency)
        transactions = fetch_all_transactions(conn, currency=currency)
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(DATA_HEADERS)
        for tx in transactions:
            w.writerow([tx.get(col, "") or "" for col in DATA_COLUMNS])

    print(f"Exported {len(transactions)} transactions to {out_path}")
