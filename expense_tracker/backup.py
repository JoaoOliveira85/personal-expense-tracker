"""Backup utilities — zip data/, raw/, and expense-report.ods."""

from __future__ import annotations

import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .constants import DEFAULT_BACKUPS, DEFAULT_DB, DEFAULT_ODS, DEFAULT_RAW

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_backup(
    backup_dir: Path = DEFAULT_BACKUPS,
    *,
    label: str | None = None,
    raw_dir: Path = DEFAULT_RAW,
    data_dir: Path | None = None,
    ods_path: Path = DEFAULT_ODS,
) -> Path:
    """Create a zip archive of all user data.

    Parameters
    ----------
    backup_dir : Path
        Directory to store the backup zip (created if missing).
    label : str | None
        If given, used as the filename stem (e.g. ``backup-2026-01``).
        If *None*, a timestamped name is generated automatically.
    raw_dir : Path
        Directory containing raw bank CSVs.
    data_dir : Path | None
        Directory containing SQLite DB, rules, config files.
        Defaults to the parent of ``DEFAULT_DB``.
    ods_path : Path
        Path to the ODS report file.

    Returns
    -------
    Path
        The path to the created zip file.
    """
    if data_dir is None:
        data_dir = DEFAULT_DB.parent

    backup_dir.mkdir(parents=True, exist_ok=True)

    if label is None:
        label = f"backup-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}"

    zip_path = backup_dir / f"{label}.zip"
    # Build under a temporary name: a half-written zip must never look like a
    # finished backup (a monthly one would then never be retried).
    tmp_path = backup_dir / f".{label}.zip.partial"

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # data/ folder
            if data_dir.is_dir():
                for p in sorted(data_dir.rglob("*")):
                    if p.is_file():
                        zf.write(p, p)

            # raw/ folder
            if raw_dir.is_dir():
                for p in sorted(raw_dir.rglob("*")):
                    if p.is_file():
                        zf.write(p, p)

            # expense-report.ods
            if ods_path.is_file():
                zf.write(ods_path, ods_path)
        tmp_path.replace(zip_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return zip_path


def previous_month_backup_exists(backup_dir: Path = DEFAULT_BACKUPS) -> bool:
    """Return True if a monthly backup for the previous month already exists.

    Monthly backups are named ``backup-YYYY-MM.zip``.
    """
    name = _previous_month_label()
    return (backup_dir / f"{name}.zip").is_file()


def create_monthly_backup(
    backup_dir: Path = DEFAULT_BACKUPS,
    **kwargs,
) -> Path:
    """Create a backup with the previous-month naming convention.

    E.g. if today is March 3 2026, creates ``backup-2026-02.zip``.
    """
    label = _previous_month_label()
    return create_backup(backup_dir, label=label, **kwargs)


def list_backups(backup_dir: Path = DEFAULT_BACKUPS) -> list[Path]:
    """Return a sorted list of zip files in the backup directory."""
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob("backup-*.zip"))


def format_size(size_bytes: int) -> str:
    """Return a human-friendly size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _previous_month_label() -> str:
    """Return ``backup-YYYY-MM`` for the month before today."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    return f"backup-{last_month.strftime('%Y-%m')}"
