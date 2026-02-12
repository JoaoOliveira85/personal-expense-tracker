"""Tests for expense_tracker/backup.py."""
from __future__ import annotations

import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from expense_tracker.backup import (
    create_backup,
    create_monthly_backup,
    format_size,
    list_backups,
    previous_month_backup_exists,
    _previous_month_label,
)


# ---------------------------------------------------------------------------
# Helpers — seed a minimal workspace inside tmp_path
# ---------------------------------------------------------------------------


def _seed_workspace(tmp_path: Path) -> dict[str, Path]:
    """Create a minimal data/, raw/, and ODS structure and return paths."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ledger.sqlite").write_bytes(b"fake-sqlite-db")
    (data_dir / "rules.csv").write_text("pattern,category\n", encoding="utf-8")

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2026-01.csv").write_text("row1\nrow2\n", encoding="utf-8")

    ods_path = tmp_path / "expense-report.ods"
    ods_path.write_bytes(b"fake-ods-content")

    backup_dir = tmp_path / "backups"

    return {
        "data_dir": data_dir,
        "raw_dir": raw_dir,
        "ods_path": ods_path,
        "backup_dir": backup_dir,
    }


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


class TestCreateBackup:

    def test_creates_zip_with_expected_files(self, tmp_path: Path):
        ws = _seed_workspace(tmp_path)
        zp = create_backup(
            ws["backup_dir"],
            raw_dir=ws["raw_dir"],
            data_dir=ws["data_dir"],
            ods_path=ws["ods_path"],
        )
        assert zp.exists()
        assert zp.suffix == ".zip"
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
        # Should contain data/, raw/, and ODS
        assert any("ledger.sqlite" in n for n in names)
        assert any("rules.csv" in n for n in names)
        assert any("2026-01.csv" in n for n in names)
        assert any("expense-report.ods" in n for n in names)

    def test_creates_backup_dir_if_missing(self, tmp_path: Path):
        ws = _seed_workspace(tmp_path)
        assert not ws["backup_dir"].exists()
        create_backup(
            ws["backup_dir"],
            raw_dir=ws["raw_dir"],
            data_dir=ws["data_dir"],
            ods_path=ws["ods_path"],
        )
        assert ws["backup_dir"].is_dir()

    def test_custom_label(self, tmp_path: Path):
        ws = _seed_workspace(tmp_path)
        zp = create_backup(
            ws["backup_dir"],
            label="backup-2026-01",
            raw_dir=ws["raw_dir"],
            data_dir=ws["data_dir"],
            ods_path=ws["ods_path"],
        )
        assert zp.name == "backup-2026-01.zip"

    def test_default_label_is_timestamped(self, tmp_path: Path):
        ws = _seed_workspace(tmp_path)
        zp = create_backup(
            ws["backup_dir"],
            raw_dir=ws["raw_dir"],
            data_dir=ws["data_dir"],
            ods_path=ws["ods_path"],
        )
        assert zp.name.startswith("backup-")
        assert "T" in zp.stem  # timestamp format: backup-YYYY-MM-DDThh-mm-ss

    def test_skips_missing_dirs_gracefully(self, tmp_path: Path):
        """If raw/ or data/ don't exist, the zip is still created."""
        backup_dir = tmp_path / "backups"
        ods_path = tmp_path / "expense-report.ods"
        ods_path.write_bytes(b"ods-data")
        zp = create_backup(
            backup_dir,
            raw_dir=tmp_path / "nonexistent-raw",
            data_dir=tmp_path / "nonexistent-data",
            ods_path=ods_path,
        )
        assert zp.exists()
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
        assert any("expense-report.ods" in n for n in names)
        assert len(names) == 1  # only the ODS

    def test_skips_missing_ods(self, tmp_path: Path):
        """If the ODS file doesn't exist, it's simply omitted."""
        ws = _seed_workspace(tmp_path)
        ws["ods_path"].unlink()
        zp = create_backup(
            ws["backup_dir"],
            raw_dir=ws["raw_dir"],
            data_dir=ws["data_dir"],
            ods_path=ws["ods_path"],
        )
        with zipfile.ZipFile(zp) as zf:
            names = zf.namelist()
        assert not any("expense-report.ods" in n for n in names)


# ---------------------------------------------------------------------------
# Monthly backup helpers
# ---------------------------------------------------------------------------


class TestPreviousMonthLabel:

    def test_returns_previous_month(self):
        with patch("expense_tracker.backup.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            label = _previous_month_label()
        assert label == "backup-2026-02"

    def test_january_wraps_to_previous_year(self):
        with patch("expense_tracker.backup.date") as mock_date:
            mock_date.today.return_value = date(2026, 1, 5)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            label = _previous_month_label()
        assert label == "backup-2025-12"


class TestPreviousMonthBackupExists:

    def test_true_when_exists(self, tmp_path: Path):
        with patch("expense_tracker.backup.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            tmp_path.mkdir(exist_ok=True)
            (tmp_path / "backup-2026-02.zip").write_bytes(b"fake")
            assert previous_month_backup_exists(tmp_path) is True

    def test_false_when_missing(self, tmp_path: Path):
        with patch("expense_tracker.backup.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 15)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            assert previous_month_backup_exists(tmp_path) is False


class TestCreateMonthlyBackup:

    def test_uses_previous_month_label(self, tmp_path: Path):
        ws = _seed_workspace(tmp_path)
        with patch("expense_tracker.backup.date") as mock_date:
            mock_date.today.return_value = date(2026, 3, 10)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            zp = create_monthly_backup(
                ws["backup_dir"],
                raw_dir=ws["raw_dir"],
                data_dir=ws["data_dir"],
                ods_path=ws["ods_path"],
            )
        assert zp.name == "backup-2026-02.zip"
        assert zp.exists()


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


class TestListBackups:

    def test_returns_sorted_zips(self, tmp_path: Path):
        (tmp_path / "backup-2026-01.zip").write_bytes(b"a")
        (tmp_path / "backup-2026-02.zip").write_bytes(b"b")
        (tmp_path / "not-a-backup.txt").write_bytes(b"c")
        result = list_backups(tmp_path)
        assert len(result) == 2
        assert result[0].name == "backup-2026-01.zip"
        assert result[1].name == "backup-2026-02.zip"

    def test_empty_when_no_dir(self, tmp_path: Path):
        assert list_backups(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# format_size
# ---------------------------------------------------------------------------


class TestFormatSize:

    def test_bytes(self):
        assert format_size(512) == "512 B"

    def test_kilobytes(self):
        assert format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert format_size(5 * 1024 * 1024) == "5.0 MB"
