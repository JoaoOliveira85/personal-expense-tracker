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
        assert sorted(p.name for p in args.raw.iterdir()) == [
            "2026-01.csv", "2026-01.csv.replaced",
        ]

    def test_second_run_is_idempotent(self, workspace: Path):
        args = _auto_args(workspace)
        make_utf16_csv(args.raw / "EXPORT_0_1022026.csv", FIRST_HALF + SECOND_HALF)

        cli.cmd_auto(args)
        cli.cmd_auto(args)

        assert len(_dates(args.db)) == 4


class TestBadFile:
    """A file that cannot be read is reported; it does not stop the run."""

    def test_auto_imports_the_rest_and_exits_non_zero(self, workspace: Path, capsys):
        args = _auto_args(workspace)
        make_utf16_csv(args.raw / "2026-01.csv", FIRST_HALF + SECOND_HALF)
        (args.raw / "junk.csv").write_text("not a bank statement\n", encoding="utf-8")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_auto(args)

        assert exit_info.value.code == 1
        assert len(_dates(args.db)) == 4
        assert args.out.exists()  # the report is still regenerated
        assert "junk.csv" in capsys.readouterr().out

    def test_ingest_exits_non_zero_without_a_traceback(self, workspace: Path, capsys):
        junk = workspace / "raw" / "junk.csv"
        junk.write_text("not a bank statement\n", encoding="utf-8")
        args = argparse.Namespace(
            db=workspace / "data" / "ledger.sqlite",
            files=[junk],
            bank=None,
            no_rename=True,
            dry_run=False,
        )

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert "junk.csv" in capsys.readouterr().out


class TestDefaultCommand:
    @pytest.mark.parametrize("flag", ["-q", "--quiet"])
    def test_global_flag_kept_when_defaulting_to_auto(self, flag, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "cmd_auto", lambda args: seen.append(args))
        monkeypatch.setattr("sys.argv", ["bank_ingest.py", flag])

        cli.main()

        assert len(seen) == 1
        assert seen[0].quiet


class TestRecleanKeepsOdsEdits:
    def test_unsynced_ods_edit_survives_reclean(self, workspace: Path):
        from expense_tracker.ods import generate_ods

        from .test_ods_sync import _category, _set_ods_category

        args = argparse.Namespace(
            db=workspace / "data" / "ledger.sqlite",
            rules=workspace / "data" / "rules.csv",
            out=workspace / "expense-report.ods",
            desc_notes=workspace / "data" / "description-notes.csv",
            backup_dir=workspace / "backups",
            quiet=True,
            verbose=False,
        )
        statement = make_utf16_csv(workspace / "raw" / "2026-01.csv", FIRST_HALF)
        ingest(args.db, [statement], cards_path=workspace / "none.csv")
        generate_ods(args.db, args.rules, args.out, args.desc_notes)
        conn = sqlite3.connect(str(args.db))
        tid = conn.execute(
            "SELECT transaction_id FROM transactions "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        ).fetchone()[0]
        conn.close()

        _set_ods_category(args.out, tid, "Groceries")  # edited, not yet synced
        cli.cmd_reclean(args)

        assert _category(args.db, tid) == "Groceries"


class TestFetchFailure:
    """The cron sync runs `fetch || echo warning`: a failure must not exit 0."""

    def _args(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            setup=False,
            config=root / "data" / "email-config.json",
            raw=root / "raw",
            db=root / "data" / "ledger.sqlite",
            days=60,
            dry_run=False,
            no_ingest=False,
            quiet=True,
            verbose=False,
        )

    def test_failed_fetch_exits_non_zero(self, workspace: Path, monkeypatch, capsys):
        def refuse(**kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(cli, "fetch_and_report", refuse)

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_fetch(self._args(workspace))

        assert exit_info.value.code == 1
        assert "connection refused" in capsys.readouterr().out

    def test_missing_config_exits_non_zero(self, workspace: Path, capsys):
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_fetch(self._args(workspace))

        assert exit_info.value.code == 1
        assert "Email config not found" in capsys.readouterr().out

    def test_nothing_to_download_is_not_a_failure(self, workspace: Path, monkeypatch):
        monkeypatch.setattr(cli, "fetch_and_report", lambda **kwargs: [])

        cli.cmd_fetch(self._args(workspace))


def _ingest_args(root: Path, files: list[Path], bank: str | None = None) -> argparse.Namespace:
    """`ingest FILES` as typed: the rename step is on."""
    return argparse.Namespace(
        db=root / "data" / "ledger.sqlite",
        files=files,
        bank=bank,
        no_rename=False,
        dry_run=False,
    )


class TestIngestRenameStep:
    """`ingest` renames statements before importing them. A file named on
    the command line that the rename step leaves out is not a success."""

    def test_file_skipped_by_the_rename_step_exits_non_zero(self, workspace: Path, capsys):
        junk = workspace / "raw" / "junk.csv"
        junk.write_text("not a bank statement\n", encoding="utf-8")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(_ingest_args(workspace, [junk]))

        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert "Skipping junk.csv" in out
        assert "1 of 1 file(s)" in out

    def test_the_other_files_are_still_imported(self, workspace: Path, capsys):
        junk = workspace / "raw" / "junk.csv"
        junk.write_text("not a bank statement\n", encoding="utf-8")
        good = make_utf16_csv(workspace / "raw" / "MOVS.csv", FIRST_HALF + SECOND_HALF)
        args = _ingest_args(workspace, [junk, good])

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert len(_dates(args.db)) == 4
        assert "1 of 2 file(s)" in capsys.readouterr().out

    def test_narrower_statement_for_an_imported_month_exits_non_zero(
        self, workspace: Path, capsys
    ):
        raw = workspace / "raw"
        make_utf16_csv(raw / "2026-01.csv", FIRST_HALF + SECOND_HALF)
        narrower = make_utf16_csv(
            raw / "MOVS.csv", FIRST_HALF, date_from="05-01-2026", date_to="20-01-2026"
        )

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(_ingest_args(workspace, [narrower]))

        assert exit_info.value.code == 1
        assert "already exists" in capsys.readouterr().out
        assert narrower.exists()

    def test_renamed_statement_is_imported_with_exit_zero(self, workspace: Path):
        good = make_utf16_csv(workspace / "raw" / "MOVS.csv", FIRST_HALF + SECOND_HALF)
        args = _ingest_args(workspace, [good])

        cli.cmd_ingest(args)

        assert len(_dates(args.db)) == 4
        assert (workspace / "raw" / "2026-01.csv").exists()
