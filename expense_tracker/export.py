"""CSV export functionality."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .constants import DATA_COLUMNS, DATA_HEADERS
from .db import fetch_all_transactions, migrate_schema


def export_csv(db_path: Path, out_path: Path) -> None:
    """Export the ledger to a clean UTF-8 CSV."""
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_schema(conn)
        transactions = fetch_all_transactions(conn)
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(DATA_HEADERS)
        for tx in transactions:
            w.writerow([tx.get(col, "") or "" for col in DATA_COLUMNS])

    print(f"Exported {len(transactions)} transactions to {out_path}")
