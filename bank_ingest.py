#!/usr/bin/env python3
"""
Expense tracking pipeline for Portuguese bank statements.

This is a convenience wrapper. The actual code lives in the
expense_tracker/ package. You can also run:

    python -m expense_tracker ingest raw/2025-12.csv
    python -m expense_tracker report
    python -m expense_tracker export --out data/ledger.csv
"""

from expense_tracker.cli import main

if __name__ == "__main__":
    main()
