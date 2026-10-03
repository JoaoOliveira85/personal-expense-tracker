"""Tests for expense_tracker.cli command handlers."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from expense_tracker import cli
from expense_tracker.db import ingest
from expense_tracker.parser import reset_cleaning_cache

from .conftest import make_utf16_csv

FIRST_HALF = [
    ("15-01-2026", "15-01-2026", "COMPRA 1234 CONTINENTE", "-45,50", "Compra", "954,50"),
    ("10-01-2026", "10-01-2026", "COMPRA 1234 PINGO DOCE", "-10,00", "Compra", "1000,00"),
]
SECOND_HALF = [
    ("28-01-2026", "28-01-2026", "COMPRA 1234 LIDL", "-20,00", "Compra", "924,50"),
    ("20-01-2026", "20-01-2026", "COMPRA 1234 FARMACIA", "-10,00", "Compra", "944,50"),
]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with data/ and raw/, used as the working directory."""
    (tmp_path / "data").mkdir()
    (tmp_path / "raw").mkdir()
    monkeypatch.chdir(tmp_path)
    reset_cleaning_cache()
    yield tmp_path
    reset_cleaning_cache()


def _auto_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        raw=root / "raw",
        db=root / "data" / "ledger.sqlite",
        rules=root / "data" / "rules.csv",
        out=root / "expense-report.xlsx",
        format="xlsx",
        desc_notes=root / "data" / "description-notes.csv",
        backup_dir=root / "backups",
        no_backup=True,
        dry_run=False,
        quiet=True,
        verbose=False,
    )


def _dates(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT date_posted FROM transactions ORDER BY date_posted"
            )
        ]
    finally:
        conn.close()


class TestAutoWiderStatement:
    def test_full_month_after_partial_month_is_ingested(self, workspace: Path):
        """A wider statement renamed onto an imported name still gets imported."""
        args = _auto_args(workspace)
        partial = make_utf16_csv(
            args.raw / "2026-01.csv", FIRST_HALF, date_to="15-01-2026"
        )
        ingest(args.db, [partial], cards_path=workspace / "none.csv")

        make_utf16_csv(args.raw / "EXPORT_0_1022026.csv", FIRST_HALF + SECOND_HALF)
        cli.cmd_auto(args)

        assert _dates(args.db) == [
            "2026-01-10", "2026-01-15", "2026-01-20", "2026-01-28",
        ]
        assert sorted(p.name for p in args.raw.iterdir()) == ["2026-01.csv"]

    def test_second_run_is_idempotent(self, workspace: Path):
        args = _auto_args(workspace)
        make_utf16_csv(args.raw / "EXPORT_0_1022026.csv", FIRST_HALF + SECOND_HALF)

        cli.cmd_auto(args)
        cli.cmd_auto(args)

        assert len(_dates(args.db)) == 4


class TestDefaultCommand:
    @pytest.mark.parametrize("flag", ["-q", "--quiet"])
    def test_global_flag_kept_when_defaulting_to_auto(self, flag, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "cmd_auto", lambda args: seen.append(args))
        monkeypatch.setattr("sys.argv", ["bank_ingest.py", flag])

        cli.main()

        assert len(seen) == 1
        assert seen[0].quiet
