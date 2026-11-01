"""Tests for expense_tracker.cli command handlers."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from expense_tracker import cli
from expense_tracker.db import ingest
from expense_tracker.parser import reset_cleaning_cache

from .conftest import allow_categories, make_utf16_csv

FIRST_HALF = [
    (
        "15-01-2026",
        "15-01-2026",
        "COMPRA 1234 CONTINENTE",
        "-45,50",
        "Compra",
        "954,50",
    ),
    (
        "10-01-2026",
        "10-01-2026",
        "COMPRA 1234 PINGO DOCE",
        "-10,00",
        "Compra",
        "1000,00",
    ),
]
RULES_HEADER = "pattern,match_field,category,subcategory,payment_type\n"
SECOND_HALF = [
    ("28-01-2026", "28-01-2026", "COMPRA 1234 LIDL", "-20,00", "Compra", "924,50"),
    ("20-01-2026", "20-01-2026", "COMPRA 1234 FARMACIA", "-10,00", "Compra", "944,50"),
]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with data/ and raw/, used as the working directory."""
    (tmp_path / "data").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "data" / "rules.csv").write_text(RULES_HEADER, encoding="utf-8")
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
            "2026-01-10",
            "2026-01-15",
            "2026-01-20",
            "2026-01-28",
        ]
        assert sorted(p.name for p in args.raw.iterdir()) == [
            "2026-01.csv",
            "2026-01.csv.replaced",
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

        allow_categories(args.rules, "Groceries")
        _set_ods_category(args.out, tid, "Groceries")  # edited, not yet synced
        cli.cmd_reclean(args)

        assert _category(args.db, tid) == "Groceries"


class TestAutoReportsSyncConflicts:
    """An ODS edit of a value the database changed since is printed and not
    applied (issue #14)."""

    def test_conflict_is_printed_with_both_values(self, workspace: Path, capsys):
        from expense_tracker.ods import generate_ods

        from .test_ods_sync import _category, _set_db_category, _set_ods_category

        args = _auto_args(workspace)
        ods = workspace / "expense-report.ods"
        statement = make_utf16_csv(workspace / "2026-01.csv", FIRST_HALF)
        ingest(args.db, [statement], cards_path=workspace / "none.csv")
        generate_ods(args.db, args.rules, ods, args.desc_notes)
        conn = sqlite3.connect(str(args.db))
        tid = conn.execute(
            "SELECT transaction_id FROM transactions "
            "WHERE description_raw LIKE '%CONTINENTE%'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE transactions SET category_source = 'manual' "
            "WHERE transaction_id = ?",
            (tid,),
        )
        conn.commit()
        conn.close()
        allow_categories(args.rules, "Groceries", "Supermarket")
        _set_db_category(args.db, tid, "Supermarket")
        _set_ods_category(ods, tid, "Groceries")
        capsys.readouterr()

        cli.cmd_auto(args)

        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if tid in ln)
        assert "'Groceries'" in line and "'Supermarket'" in line
        assert "1 spreadsheet edit(s) not applied" in out
        assert _category(args.db, tid) == "Supermarket"


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


def _ingest_args(
    root: Path, files: list[Path], bank: str | None = None
) -> argparse.Namespace:
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

    def test_file_skipped_by_the_rename_step_exits_non_zero(
        self, workspace: Path, capsys
    ):
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


UTF8_STATEMENT = (
    "Data Mov.;Data Valor;Descrição;Débito;Crédito;Saldo Contabilístico\n"
    "15-01-2026;15-01-2026;COMPRA CONTINENTE;45,50;;954,50\n"
    "16-01-2026;16-01-2026;COMPRA LIDL;12,50;;942,00\n"
)


class TestIngestOtherBank:
    """Renaming to YYYY-MM.csv reads the UTF-16 CSV header. A statement
    from another bank keeps its name and is imported all the same."""

    @pytest.fixture
    def utf8_csv(self, workspace: Path) -> Path:
        path = workspace / "raw" / "export-b.csv"
        path.write_text(UTF8_STATEMENT, encoding="utf-8")
        return path

    def test_detected_utf8_statement_is_imported_under_its_own_name(
        self, workspace: Path, utf8_csv: Path, capsys
    ):
        args = _ingest_args(workspace, [utf8_csv])

        cli.cmd_ingest(args)

        assert _dates(args.db) == ["2026-01-15", "2026-01-16"]
        assert utf8_csv.exists()
        assert "Skipping" not in capsys.readouterr().out

    def test_bank_option_imports_a_utf8_statement(
        self, workspace: Path, utf8_csv: Path
    ):
        """The README's `ingest --bank utf8 raw/export-b.csv`."""
        args = _ingest_args(workspace, [utf8_csv], bank="utf8")

        cli.cmd_ingest(args)

        assert _dates(args.db) == ["2026-01-15", "2026-01-16"]
        assert utf8_csv.exists()

    def test_auto_imports_a_utf8_statement_without_a_warning(
        self, workspace: Path, utf8_csv: Path, capsys
    ):
        args = _auto_args(workspace)

        cli.cmd_auto(args)

        assert _dates(args.db) == ["2026-01-15", "2026-01-16"]
        out = capsys.readouterr().out
        assert "Skipping" not in out
        assert "Could not find date range" not in out

    def test_utf16_statement_forced_to_utf8_is_left_alone(
        self, workspace: Path, capsys
    ):
        utf16 = make_utf16_csv(workspace / "raw" / "MOVS.csv", FIRST_HALF)
        args = _ingest_args(workspace, [utf16], bank="utf8")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert utf16.exists()  # not renamed for a bank it was not parsed as
        assert "UTF8 header" in capsys.readouterr().out


class TestIngestUnknownBank:
    """A --bank value that names no parser is an error for every file,
    before anything is renamed or imported."""

    def test_unknown_bank_is_rejected_for_a_pdf(self, workspace: Path, capsys):
        from .test_pdf_parser import _make_text_pdf

        pdf = _make_text_pdf(
            workspace / "raw" / "statement.pdf",
            [
                "EXTRATO DE 2026/02/02 A 2026/02/27",
                "2.02 2.02 COMPRA KIOSK 3.00 1 529.13",
            ],
        )
        args = _ingest_args(workspace, [pdf], bank="typo")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert "Unknown bank 'typo'. Available: utf16, utf8" in capsys.readouterr().out
        assert pdf.exists()
        assert not args.db.exists()

    def test_unknown_bank_is_rejected_before_a_csv_is_renamed(
        self, workspace: Path, capsys
    ):
        utf16 = make_utf16_csv(workspace / "raw" / "MOVS.csv", FIRST_HALF)
        args = _ingest_args(workspace, [utf16], bank="typo")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert "Unknown bank 'typo'" in capsys.readouterr().out
        assert utf16.exists()

    def test_unknown_bank_is_rejected_in_a_dry_run(self, workspace: Path, capsys):
        utf16 = make_utf16_csv(workspace / "raw" / "MOVS.csv", FIRST_HALF)
        args = _ingest_args(workspace, [utf16], bank="typo")
        args.dry_run = True

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1


def _file_lines(out: str) -> list[str]:
    """The per-file count lines of an import, without the file name."""
    return [
        line.split(": ", 1)[1]
        for line in out.splitlines()
        if " parsed, " in line and " already present" in line
    ]


class TestDryRun:
    """--dry-run parses like a real import and changes nothing."""

    def test_ingest_dry_run_prints_the_counts_of_the_real_run(
        self, workspace: Path, capsys
    ):
        raw = workspace / "raw"
        statement = make_utf16_csv(raw / "MOVS.csv", FIRST_HALF + SECOND_HALF)
        args = _ingest_args(workspace, [statement])
        args.dry_run = True

        cli.cmd_ingest(args)

        dry_out = capsys.readouterr().out
        assert [p.name for p in raw.iterdir()] == ["MOVS.csv"]
        assert not args.db.exists()
        assert "MOVS.csv: 4 parsed, 4 new, 0 already present" in dry_out

        args.dry_run = False
        cli.cmd_ingest(args)

        assert _file_lines(dry_out) == _file_lines(capsys.readouterr().out)

    def test_auto_dry_run_changes_neither_the_ledger_nor_raw(
        self, workspace: Path, capsys
    ):
        args = _auto_args(workspace)
        partial = make_utf16_csv(
            args.raw / "2026-01.csv", FIRST_HALF, date_to="15-01-2026"
        )
        ingest(args.db, [partial], cards_path=workspace / "none.csv")
        make_utf16_csv(args.raw / "EXPORT_0_1022026.csv", FIRST_HALF + SECOND_HALF)
        before = args.db.read_bytes()
        capsys.readouterr()

        args.dry_run = True
        cli.cmd_auto(args)

        dry_out = capsys.readouterr().out
        assert args.db.read_bytes() == before
        assert sorted(p.name for p in args.raw.iterdir()) == [
            "2026-01.csv",
            "EXPORT_0_1022026.csv",
        ]
        assert not args.out.exists()
        assert "EXPORT_0_1022026.csv: 4 parsed, 2 new, 2 already present" in dry_out

        args.dry_run = False
        cli.cmd_auto(args)

        assert _dates(args.db) == [
            "2026-01-10",
            "2026-01-15",
            "2026-01-20",
            "2026-01-28",
        ]

    def test_ingest_dry_run_fails_like_the_real_run(self, workspace: Path, capsys):
        junk = workspace / "raw" / "junk.csv"
        junk.write_text("not a bank statement\n", encoding="utf-8")
        args = _ingest_args(workspace, [junk])
        args.no_rename = True

        results = []
        for dry in (True, False):
            args.dry_run = dry
            with pytest.raises(SystemExit) as exit_info:
                cli.cmd_ingest(args)
            out = capsys.readouterr().out
            results.append(
                (exit_info.value.code, [x for x in out.splitlines() if "Error" in x])
            )

        assert results[0] == results[1]
        assert results[0][0] == 1

    def test_auto_dry_run_fails_on_a_bad_file(self, workspace: Path, capsys):
        args = _auto_args(workspace)
        args.dry_run = True
        (args.raw / "junk.csv").write_text("not a bank statement\n", encoding="utf-8")

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_auto(args)

        assert exit_info.value.code == 1
        assert "junk.csv was not imported" in capsys.readouterr().out
        assert not args.db.exists()


class TestGuiBindsToLocalhost:
    """`gui` prints a localhost URL, so the server must listen there only."""

    def test_streamlit_is_told_to_listen_on_localhost(self, monkeypatch, capsys):
        import subprocess

        launched: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kwargs: launched.append(list(argv))
        )

        cli.cmd_gui(argparse.Namespace(port=8765))

        (argv,) = launched
        assert "--server.address" in argv
        assert argv[argv.index("--server.address") + 1] == "localhost"
        assert "http://localhost:8765" in capsys.readouterr().out


def _rules_add(
    rules: Path, pattern: str, category: str = "Utilities"
) -> argparse.Namespace:
    """`rules add`, with Utilities in the category list next to `rules`."""
    from expense_tracker.rules import categories_file

    if not categories_file(rules).exists():
        allow_categories(rules, "Utilities")
    return argparse.Namespace(
        action="add",
        extra=[pattern, category],
        rules=rules,
        db=rules.with_name("ledger.sqlite"),
        field="description",
        sub=None,
        payment=None,
    )


class TestRulesAddShortPattern:
    """A short pattern is added, with a warning that it is broad."""

    def test_short_pattern_warns_after_adding(self, tmp_path: Path, capsys):
        rules = tmp_path / "rules.csv"
        cli.cmd_rules(_rules_add(rules, "EDP"))

        out = capsys.readouterr().out
        assert out.index("Added rule: EDP -> Utilities") < out.index("'EDP'")
        assert "longer words" in out
        assert [r["pattern"] for r in cli.load_rules(rules)] == ["EDP"]

    @pytest.mark.parametrize("pattern", ["EDP ", "EDPX"])
    def test_anchored_or_long_pattern_adds_without_warning(
        self, tmp_path: Path, capsys, pattern
    ):
        rules = tmp_path / "rules.csv"
        cli.cmd_rules(_rules_add(rules, pattern))

        out = capsys.readouterr().out
        assert out.startswith("Added rule:")
        assert "longer words" not in out
        assert [r["pattern"] for r in cli.load_rules(rules)] == [pattern.strip()]

    def test_import_starter_reports_short_patterns(self, tmp_path: Path, capsys):
        args = argparse.Namespace(
            action="import-starter",
            extra=[],
            rules=tmp_path / "rules.csv",
            db=tmp_path / "ledger.sqlite",
        )
        cli.cmd_rules(args)

        out = capsys.readouterr().out
        assert "shorter than 4 characters" in out


class TestRulesAddDuplicate:
    """A pattern that folds to an existing rule's is refused (issue #8)."""

    def test_duplicate_is_an_error_and_not_written(self, tmp_path: Path, capsys):
        rules = tmp_path / "rules.csv"
        cli.cmd_rules(_rules_add(rules, "Café"))
        before = rules.read_text(encoding="utf-8")
        capsys.readouterr()

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_rules(_rules_add(rules, "CAFE"))

        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert out.startswith("Error:")
        assert "Café" in out
        assert "Added rule" not in out
        assert rules.read_text(encoding="utf-8") == before


class TestRulesAddCategory:
    """`rules add` takes a category from the list only (issue #5)."""

    def test_unknown_category_is_an_error_and_not_written(self, tmp_path: Path, capsys):
        rules = tmp_path / "rules.csv"
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_rules(_rules_add(rules, "PINGO DOCE", "Utilties"))

        assert exit_info.value.code == 1
        out = capsys.readouterr().out
        assert out.startswith("Error:") and "Utilties" in out
        assert not rules.exists()

    def test_other_case_is_normalised(self, tmp_path: Path, capsys):
        rules = tmp_path / "rules.csv"
        cli.cmd_rules(_rules_add(rules, "EDPX", "utilities"))

        assert "Added rule: EDPX -> Utilities" in capsys.readouterr().out
        assert cli.load_rules(rules)[0]["category"] == "Utilities"

    def test_list_is_seeded_from_the_ledger_first(self, tmp_path: Path, capsys):
        from .conftest import add_transaction

        rules = tmp_path / "rules.csv"
        db = tmp_path / "ledger.sqlite"
        add_transaction(db, "2026-01-02", "PET SHOP", 10, "out", "Pets")
        args = _rules_add(rules, "PET SHOP", "pets")
        (tmp_path / "categories.csv").unlink()

        cli.cmd_rules(args)

        assert cli.load_rules(rules)[0]["category"] == "Pets"


def _categories_cmd(tmp_path: Path, action: str, *extra: str) -> argparse.Namespace:
    return argparse.Namespace(
        action=action,
        extra=list(extra),
        rules=tmp_path / "rules.csv",
        db=tmp_path / "ledger.sqlite",
    )


class TestCategoriesCommand:
    def test_list_shows_the_seeded_list(self, tmp_path: Path, capsys, rules_csv):
        cli.cmd_categories(_categories_cmd(tmp_path, "list"))

        out = capsys.readouterr().out
        for name in ("Groceries", "Health", "Ignored", "Miscellaneous", "Savings"):
            assert name in out

    def test_add_then_remove(self, tmp_path: Path, capsys):
        cli.cmd_categories(_categories_cmd(tmp_path, "add", "Pets"))
        assert "Pets" in cli.load_categories(tmp_path / "categories.csv")

        cli.cmd_categories(_categories_cmd(tmp_path, "remove", "pets"))
        assert "Pets" not in cli.load_categories(tmp_path / "categories.csv")
        out = capsys.readouterr().out
        assert "Added category: Pets" in out and "Removed category: Pets" in out

    @pytest.mark.parametrize(
        "action, name", [("add", "groceries"), ("remove", "Groceries")]
    )
    def test_refusal_is_an_error(self, tmp_path: Path, capsys, rules_csv, action, name):
        """Groceries exists, and a rule uses it."""
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_categories(_categories_cmd(tmp_path, action, name))

        assert exit_info.value.code == 1
        assert capsys.readouterr().out.startswith("Error:")
        assert "Groceries" in cli.load_categories(tmp_path / "categories.csv")


def _categories(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT description_raw, category, category_source FROM transactions "
            "ORDER BY description_raw"
        ).fetchall()
    finally:
        conn.close()


class TestRulesFile:
    """An empty rules file is an empty rule set; a missing one is an error."""

    @pytest.fixture
    def categorized(self, workspace: Path) -> Path:
        """FIRST_HALF imported: CONTINENTE by rule, PINGO DOCE by hand."""
        db = workspace / "data" / "ledger.sqlite"
        statement = make_utf16_csv(workspace / "raw" / "2026-01.csv", FIRST_HALF)
        ingest(db, [statement], cards_path=workspace / "none.csv")
        conn = sqlite3.connect(str(db))
        try:
            cli.migrate_schema(conn)
            rules = workspace / "continente.csv"
            rules.write_text(
                RULES_HEADER + "CONTINENTE,description,Groceries,,\n", encoding="utf-8"
            )
            cli.categorize_transactions(conn, cli.load_rules(rules))
            conn.execute(
                "UPDATE transactions SET category = 'Groceries', "
                "category_source = 'manual' WHERE description_raw LIKE '%PINGO%'"
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def _main(self, monkeypatch, *argv: str) -> None:
        monkeypatch.setattr("sys.argv", ["bank_ingest.py", "-q", *argv])
        cli.main()

    def test_empty_rules_file_uncategorizes_rule_rows(
        self, workspace: Path, categorized: Path, monkeypatch
    ):
        self._main(monkeypatch, "report")

        assert _categories(categorized) == [
            ("COMPRA 1234 CONTINENTE", None, None),
            ("COMPRA 1234 PINGO DOCE", "Groceries", "manual"),
        ]

    def test_missing_rules_file_stops_report_and_changes_nothing(
        self, workspace: Path, categorized: Path, monkeypatch, capsys
    ):
        (workspace / "data" / "rules.csv").unlink()
        before = _categories(categorized)

        with pytest.raises(SystemExit) as exit_info:
            self._main(monkeypatch, "report")

        assert exit_info.value.code == 1
        assert "rules.csv" in capsys.readouterr().out
        assert _categories(categorized) == before
        assert not (workspace / "expense-report.xlsx").exists()

    def test_missing_rules_file_stops_auto_before_importing(
        self, workspace: Path, monkeypatch, capsys
    ):
        (workspace / "data" / "rules.csv").unlink()
        make_utf16_csv(workspace / "raw" / "2026-01.csv", FIRST_HALF)

        with pytest.raises(SystemExit) as exit_info:
            self._main(monkeypatch, "auto", "--no-backup")

        assert exit_info.value.code == 1
        assert "rules.csv" in capsys.readouterr().out
        assert not (workspace / "data" / "ledger.sqlite").exists()


MARCH = [("05-03-2026", "05-03-2026", "COMPRA 1234 LIDL", "-20,00", "Compra", "1,00")]


class TestCoverageCommand:
    """`coverage` lists the months imported per account and the gaps."""

    @pytest.fixture(autouse=True)
    def _account(self, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")

    def test_prints_ranges_and_missing_months(
        self, workspace: Path, monkeypatch, capsys
    ):
        raw = workspace / "raw"
        db = workspace / "data" / "ledger.sqlite"
        jan = make_utf16_csv(raw / "2026-01.csv", FIRST_HALF)
        mar = make_utf16_csv(
            raw / "2026-03.csv", MARCH, date_from="01-03-2026", date_to="31-03-2026"
        )
        ingest(db, [jan, mar], cards_path=workspace / "none.csv")
        capsys.readouterr()
        monkeypatch.setattr("sys.argv", ["bank_ingest.py", "coverage", "--db", str(db)])

        cli.main()

        assert capsys.readouterr().out.splitlines() == [
            "checkings_account",
            "  Covered: 2026-01, 2026-03",
            "  Missing: 2026-02",
        ]

    def test_contiguous_months_print_as_one_range(self, workspace: Path, capsys):
        raw = workspace / "raw"
        db = workspace / "data" / "ledger.sqlite"
        statement = make_utf16_csv(
            raw / "q1.csv",
            FIRST_HALF + MARCH,
            date_from="01-01-2026",
            date_to="31-03-2026",
        )
        ingest(db, [statement], cards_path=workspace / "none.csv")
        capsys.readouterr()

        cli.cmd_coverage(argparse.Namespace(db=db))

        assert capsys.readouterr().out.splitlines() == [
            "checkings_account",
            "  Covered: 2026-01 to 2026-03",
            "  Missing: none",
        ]

    def test_no_ledger_is_reported_and_not_created(self, workspace: Path, capsys):
        db = workspace / "data" / "ledger.sqlite"

        cli.cmd_coverage(argparse.Namespace(db=db))

        assert "No statements imported yet." in capsys.readouterr().out
        assert not db.exists()


class TestAccountMismatch:
    """A statement of another account stops ingest and auto unless --force
    (issue #12)."""

    @pytest.fixture(autouse=True)
    def _account(self, monkeypatch):
        monkeypatch.setenv("EXPENSE_TRACKER_ACCOUNT_TYPE", "checkings_account")

    def _statement(self, path: Path, identifier: str, rows) -> Path:
        make_utf16_csv(path, rows)
        text = path.read_bytes().decode("utf-16-le")
        text = text.replace("Conta:;123456789", f"Conta:;{identifier}")
        path.write_bytes(text.encode("utf-16-le"))
        return path

    def _setup(self, workspace: Path) -> Path:
        raw = workspace / "raw"
        first = self._statement(raw / "a.csv", "111 222 333", FIRST_HALF)
        ingest(workspace / "data" / "ledger.sqlite", [first], cards_path=raw / "x")
        return self._statement(raw / "b.csv", "999 888 777", SECOND_HALF)

    def test_ingest_refuses_then_force_imports(self, workspace: Path, capsys):
        other = self._setup(workspace)
        args = _ingest_args(workspace, [other])
        args.no_rename = True

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)
        out = capsys.readouterr().out
        assert exit_info.value.code == 1
        assert "b.csv" in out and "'checkings_account'" in out
        assert len(_dates(args.db)) == 2

        args.force = True
        cli.cmd_ingest(args)
        out += capsys.readouterr().out
        assert len(_dates(args.db)) == 4
        for identifier in ("111 222 333", "999 888 777"):
            assert identifier not in out
            assert identifier.replace(" ", "") not in out

    def test_ingest_dry_run_refuses_too(self, workspace: Path, capsys):
        other = self._setup(workspace)
        args = _ingest_args(workspace, [other])
        args.dry_run = True

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_ingest(args)

        assert exit_info.value.code == 1
        assert "another account" in capsys.readouterr().out

    def test_auto_refuses_then_force_imports(self, workspace: Path, capsys):
        self._setup(workspace)
        args = _auto_args(workspace)

        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_auto(args)
        assert exit_info.value.code == 1
        assert "another account" in capsys.readouterr().out
        assert len(_dates(args.db)) == 2

        args.force = True
        cli.cmd_auto(args)
        assert len(_dates(args.db)) == 4

    def test_force_flag_is_parsed(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_ingest", lambda a: seen.update(i=a.force))
        monkeypatch.setattr(cli, "cmd_auto", lambda a: seen.update(a=a.force))
        monkeypatch.setattr("sys.argv", ["x", "ingest", "--force", "f.csv"])
        cli.main()
        monkeypatch.setattr("sys.argv", ["x", "auto", "--force"])
        cli.main()
        assert seen == {"i": True, "a": True}
