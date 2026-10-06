"""The account label on imported rows is configurable, not hardcoded."""

from __future__ import annotations

from expense_tracker.constants import account_type
from expense_tracker.parser import parse_utf16_csv


def test_defaults_to_checkings_account(monkeypatch):
    monkeypatch.delenv("EXPENSE_TRACKER_ACCOUNT_TYPE", raising=False)
    assert account_type() == "checkings_account"


def test_environment_overrides_the_default(monkeypatch):
    monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "shared_account")
    assert account_type() == "shared_account"


def test_imported_rows_carry_the_configured_account(utf16_csv, monkeypatch):
    monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "shared_account")
    rows = parse_utf16_csv(utf16_csv)
    assert rows and {r["account"] for r in rows} == {"shared_account"}


def test_imported_rows_default_account(utf16_csv, monkeypatch):
    monkeypatch.delenv("EXPENSE_TRACKER_ACCOUNT_TYPE", raising=False)
    rows = parse_utf16_csv(utf16_csv)
    assert {r["account"] for r in rows} == {"checkings_account"}
