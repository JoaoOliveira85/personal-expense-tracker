"""Command-line interface."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .constants import (
    DEFAULT_RAW, DEFAULT_DB, DEFAULT_RULES, DEFAULT_CARDS, DEFAULT_ODS, DEFAULT_CSV,
    DEFAULT_DESC_NOTES, DEFAULT_BACKUPS,
)
from .backup import (
    create_backup, create_monthly_backup,
    previous_month_backup_exists, list_backups, format_size,
)
from .db import ingest, ingested_source_files, migrate_schema, reclean_descriptions
from .parser import auto_rename_csv, load_card_holders
from .rules import (
    load_rules, categorize_transactions,
    add_rule, remove_rule,
    add_card, remove_card, _load_cards_raw,
)
from .ods import generate_ods, sync_from_ods
from .export import export_csv


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


def _check_setup() -> None:
    """Verify that initial setup has been completed, exit with a friendly
    message if not."""
    try:
        import odf  # noqa: F401
    except ImportError:
        print("Error: Required Python packages are not installed.")
        print()
        print("  Run ./install.sh first, or manually:")
        print("    python3 -m venv .venv")
        print("    source .venv/bin/activate")
        print("    pip install -r requirements.txt")
        sys.exit(1)

    data_dir = DEFAULT_DB.parent
    if not data_dir.is_dir():
        print(f"Error: Data directory '{data_dir}/' does not exist.")
        print()
        print("  Run ./install.sh first, or create it manually:")
        print(f"    mkdir -p {data_dir}")
        sys.exit(1)


def _check_raw_dir(raw_dir: Path) -> None:
    """Verify that the raw/ directory exists."""
    if not raw_dir.is_dir():
        print(f"Error: Raw bank-statement directory '{raw_dir}/' not found.")
        print()
        print("  Run ./install.sh first, or create it manually:")
        print(f"    mkdir -p {raw_dir}")
        print()
        print("  Then copy your bank CSV exports into it.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Verbosity level: 0 = quiet, 1 = normal (default), 2 = verbose
_verbosity = 1


def _set_verbosity(args) -> None:
    """Set the global verbosity level from parsed args."""
    global _verbosity
    if getattr(args, "quiet", False):
        _verbosity = 0
    elif getattr(args, "verbose", False):
        _verbosity = 2
    else:
        _verbosity = 1


def _info(msg: str) -> None:
    """Print a message at normal verbosity (suppressed by --quiet)."""
    if _verbosity >= 1:
        print(msg)


def _detail(msg: str) -> None:
    """Print a message only at verbose level."""
    if _verbosity >= 2:
        print(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rename_files(files: list[Path]) -> list[Path]:
    """Auto-rename a list of CSV files based on their date range.
    Returns the (possibly renamed) paths, skipping any that fail."""
    result = []
    for f in files:
        try:
            result.append(auto_rename_csv(f))
        except ValueError as e:
            print(f"  Warning: {e}")
            print(f"  Skipping {f.name}.")
    return result


def _discover_new_csvs(raw_dir: Path, db_path: Path) -> list[Path]:
    """Find CSV files in raw_dir that haven't been ingested yet."""
    if not raw_dir.is_dir():
        return []
    already = ingested_source_files(db_path)
    candidates = sorted(raw_dir.glob("*.csv"))
    new = [p for p in candidates if p.name not in already]
    return new


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_auto(args):
    """Handle the 'auto' subcommand: discover, rename, ingest, and report."""
    _check_setup()
    _set_verbosity(args)
    dry = getattr(args, "dry_run", False)

    # Auto-monthly backup: create one for the previous month if missing
    if not args.no_backup and not dry:
        backup_dir = args.backup_dir
        if not previous_month_backup_exists(backup_dir):
            zp = create_monthly_backup(
                backup_dir, raw_dir=args.raw, ods_path=args.out,
            )
            _info(f"Auto-backup created: {zp} ({format_size(zp.stat().st_size)})")
        # (if it already exists we stay silent)
    
    raw_dir = args.raw
    _check_raw_dir(raw_dir)
    _info(f"Scanning {raw_dir}/ for new bank statements...")

    # Step 1: Discover new files
    new_files = _discover_new_csvs(raw_dir, args.db)

    if dry:
        if new_files:
            print(f"[dry-run] Would rename and ingest {len(new_files)} file(s):")
            for f in new_files:
                print(f"  {f.name}")
        else:
            print("[dry-run] No new files to ingest.")
        print("[dry-run] Would sync ODS edits, apply rules, and regenerate report.")
        return

    # Step 2: Auto-rename
    if new_files:
        new_files = _rename_files(new_files)
        # Re-discover after renames (names may have changed)
        new_files = _discover_new_csvs(raw_dir, args.db)

    if new_files:
        _info(f"Found {len(new_files)} new file(s): {', '.join(f.name for f in new_files)}")
        ingest(args.db, new_files)
    else:
        _info("No new files to ingest.")

    # Step 3: Sync manual edits, apply rules, regenerate report
    _detail("")
    synced = sync_from_ods(args.db, args.out, args.desc_notes)
    if synced:
        _info(f"Synced {synced} manual edit(s) from {args.out} back to database.")

    conn = sqlite3.connect(str(args.db))
    try:
        migrate_schema(conn)
        rules = load_rules(args.rules)
        if rules:
            updated = categorize_transactions(conn, rules)
            if updated:
                _detail(f"Categorized {updated} transactions using {len(rules)} rules.")
    finally:
        conn.close()

    generate_ods(args.db, args.rules, args.out, args.desc_notes)
    _info(f"\nDone! Report saved to {args.out}")


def cmd_ingest(args):
    """Handle the 'ingest' subcommand."""
    _check_setup()
    dry = getattr(args, "dry_run", False)
    files = list(args.files)

    if dry:
        print(f"[dry-run] Would ingest {len(files)} file(s):")
        for f in files:
            print(f"  {f.name}")
        if not args.no_rename:
            print("[dry-run] Would auto-rename files based on date ranges.")
        return

    # Auto-rename files based on date range (unless --no-rename)
    if not args.no_rename:
        files = _rename_files(files)

    if files:
        ingest(args.db, files)


def cmd_report(args):
    """Handle the 'report' subcommand."""
    _check_setup()
    _set_verbosity(args)

    # Step 1: Sync back any manual edits from the existing ODS
    if not args.no_sync:
        synced = sync_from_ods(args.db, args.out, args.desc_notes)
        if synced:
            _info(f"Synced {synced} manual edit(s) from {args.out} back to database.")
    else:
        _detail("Skipping sync (--no-sync flag).")

    # Step 2: Apply rules to uncategorized transactions
    conn = sqlite3.connect(str(args.db))
    try:
        migrate_schema(conn)
        rules = load_rules(args.rules)
        if rules:
            updated = categorize_transactions(conn, rules)
            _detail(f"Categorized {updated} transactions using {len(rules)} rules.")
    finally:
        conn.close()

    # Step 3: Regenerate ODS (delete first if --fresh to force full regeneration)
    if args.fresh and args.out.exists():
        zp = create_backup(args.backup_dir, raw_dir=args.raw, ods_path=args.out)
        _info(f"Pre-fresh backup: {zp} ({format_size(zp.stat().st_size)})")
        args.out.unlink()
        _info(f"Deleted existing {args.out} (--fresh flag).")

    generate_ods(args.db, args.rules, args.out, args.desc_notes)


def cmd_cards(args):
    """Handle the 'cards' subcommand: list/add/remove card holders."""
    action = args.action or "list"
    cards_path = args.cards

    if action == "list":
        holders = load_card_holders(cards_path)
        if not holders:
            print(f"No card holders defined. Add one with:\n"
                  f"  python bank_ingest.py cards add <last4> <name>")
            return
        print(f"Card holders ({cards_path}):\n")
        print(f"  {'Last 4':>6}  Name")
        print(f"  {'─' * 6}  {'─' * 20}")
        for last4, name in holders.items():
            print(f"  {last4:>6}  {name}")

    elif action == "add":
        if len(args.extra) < 2:
            print("Usage: cards add <last4> <name>")
            print("  Example: cards add 1234 Alice")
            return
        last4, name = args.extra[0], " ".join(args.extra[1:])
        if not last4.isdigit() or len(last4) != 4:
            print(f"Error: '{last4}' is not a valid 4-digit card number.")
            return
        try:
            add_card(cards_path, last4, name)
            print(f"Added card {last4} -> {name}")
        except ValueError as e:
            print(f"Error: {e}")

    elif action == "remove":
        if not args.extra:
            print("Usage: cards remove <last4>")
            return
        last4 = args.extra[0]
        if remove_card(cards_path, last4):
            print(f"Removed card {last4}")
        else:
            print(f"Card {last4} not found.")


def cmd_rules(args):
    """Handle the 'rules' subcommand: list/add/remove categorization rules."""
    action = args.action or "list"
    rules_path = args.rules

    if action == "list":
        rules = load_rules(rules_path)
        if not rules:
            print(f"No rules defined. Add one with:\n"
                  f"  python bank_ingest.py rules add <pattern> <category>")
            return
        print(f"Categorization rules ({rules_path}):\n")
        print(f"  {'#':>3}  {'Pattern':<30} {'Field':<16} {'Category':<20} {'Subcategory':<20} {'Payment'}")
        print(f"  {'─' * 3}  {'─' * 30} {'─' * 16} {'─' * 20} {'─' * 20} {'─' * 12}")
        for i, r in enumerate(rules, 1):
            print(f"  {i:>3}  {r['pattern']:<30} {r['match_field']:<16} "
                  f"{r['category']:<20} {r.get('subcategory', ''):<20} "
                  f"{r.get('payment_type', '')}")

    elif action == "add":
        if len(args.extra) < 2:
            print("Usage: rules add <pattern> <category> [--field X] [--sub Y] [--payment Z]")
            print("  Example: rules add CONTINENTE Groceries")
            print("  Example: rules add FARMACIA Health --sub Pharmacy --payment card")
            return
        pattern = args.extra[0]
        category = args.extra[1]
        add_rule(
            rules_path, pattern,
            match_field=args.field,
            category=category,
            subcategory=args.sub or "",
            payment_type=args.payment or "",
        )
        print(f"Added rule: {pattern} -> {category}"
              + (f" / {args.sub}" if args.sub else ""))

    elif action == "remove":
        if not args.extra:
            print("Usage: rules remove <pattern>")
            return
        pattern = args.extra[0]
        if remove_rule(rules_path, pattern):
            print(f"Removed rule matching '{pattern}'")
        else:
            print(f"No rule found matching '{pattern}'.")


def cmd_export(args):
    """Handle the 'export' subcommand."""
    _check_setup()
    export_csv(args.db, args.out)


def cmd_reclean(args):
    """Handle the 'reclean' subcommand: recompute cleaned descriptions."""
    _check_setup()
    _set_verbosity(args)

    # Pre-destructive backup
    zp = create_backup(args.backup_dir, ods_path=args.out)
    _info(f"Pre-reclean backup: {zp} ({format_size(zp.stat().st_size)})")

    _info("Re-cleaning all descriptions using current patterns...")
    updated = reclean_descriptions(args.db)
    if updated:
        _info(f"Updated {updated} description(s).")
    else:
        _info("All descriptions are already up to date.")

    # Regenerate the report with fresh descriptions
    _detail("")
    generate_ods(args.db, args.rules, args.out, args.desc_notes)
    _info(f"\nDone! Report saved to {args.out}")


def cmd_backup(args):
    """Handle the 'backup' subcommand: create a manual backup."""
    _check_setup()
    zp = create_backup(args.out, raw_dir=args.raw, ods_path=args.ods)
    size = format_size(zp.stat().st_size)
    print(f"Backup created: {zp} ({size})")

    existing = list_backups(args.out)
    if len(existing) > 1:
        print(f"  ({len(existing)} backups in {args.out}/)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Expense tracking pipeline for UTF-16 CSV bank statements."
    )
    ap.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress all output except errors",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed progress information",
    )
    sub = ap.add_subparsers(dest="cmd")

    # -- auto (default) --
    ap_auto = sub.add_parser(
        "auto",
        help="Scan raw/ for new CSVs, ingest them, and regenerate the report (default)",
    )
    ap_auto.add_argument(
        "--raw", type=Path, default=DEFAULT_RAW,
        help=f"Directory containing raw bank CSV files (default: {DEFAULT_RAW})",
    )
    ap_auto.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_auto.add_argument(
        "--rules", type=Path, default=DEFAULT_RULES,
        help=f"Categorization rules CSV (default: {DEFAULT_RULES})",
    )
    ap_auto.add_argument(
        "--out", type=Path, default=DEFAULT_ODS,
        help=f"Output ODS file path (default: {DEFAULT_ODS})",
    )
    ap_auto.add_argument(
        "--desc-notes", type=Path, default=DEFAULT_DESC_NOTES,
        help=f"Merchant notes CSV (default: {DEFAULT_DESC_NOTES})",
    )
    ap_auto.add_argument(
        "--backup-dir", type=Path, default=DEFAULT_BACKUPS,
        help=f"Directory for backup archives (default: {DEFAULT_BACKUPS})",
    )
    ap_auto.add_argument(
        "--no-backup", action="store_true",
        help="Skip the automatic monthly backup",
    )
    ap_auto.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making any changes",
    )
    ap_auto.set_defaults(func=cmd_auto)

    # -- ingest --
    ap_ingest = sub.add_parser(
        "ingest", help="Parse specific bank CSV files and store in SQLite"
    )
    ap_ingest.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_ingest.add_argument(
        "--no-rename", action="store_true",
        help="Skip auto-renaming CSV files based on their date range",
    )
    ap_ingest.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making any changes",
    )
    ap_ingest.add_argument(
        "files", nargs="+", type=Path, help="Bank CSV export files to ingest",
    )
    ap_ingest.set_defaults(func=cmd_ingest)

    # -- report --
    ap_report = sub.add_parser(
        "report", help="Apply rules and generate ODS expense report"
    )
    ap_report.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_report.add_argument(
        "--rules", type=Path, default=DEFAULT_RULES,
        help=f"Categorization rules CSV (default: {DEFAULT_RULES})",
    )
    ap_report.add_argument(
        "--out", type=Path, default=DEFAULT_ODS,
        help=f"Output ODS file path (default: {DEFAULT_ODS})",
    )
    ap_report.add_argument(
        "--fresh", action="store_true",
        help="Delete existing ODS and regenerate all sheets from scratch "
             "(manual category edits are still synced to DB first unless --no-sync)",
    )
    ap_report.add_argument(
        "--no-sync", action="store_true",
        help="Skip syncing manual edits from the ODS back to the database",
    )
    ap_report.add_argument(
        "--desc-notes", type=Path, default=DEFAULT_DESC_NOTES,
        help=f"Merchant notes CSV (default: {DEFAULT_DESC_NOTES})",
    )
    ap_report.add_argument(
        "--backup-dir", type=Path, default=DEFAULT_BACKUPS,
        help=f"Directory for backup archives (default: {DEFAULT_BACKUPS})",
    )
    ap_report.add_argument(
        "--raw", type=Path, default=DEFAULT_RAW,
        help=f"Directory containing raw bank CSV files (default: {DEFAULT_RAW})",
    )
    ap_report.set_defaults(func=cmd_report)

    # -- cards --
    ap_cards = sub.add_parser(
        "cards", help="List, add, or remove card-holder mappings"
    )
    ap_cards.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "add", "remove"],
        help="Action to perform (default: list)",
    )
    ap_cards.add_argument(
        "extra", nargs="*",
        help="Additional arguments (e.g. last4 digits and name for 'add')",
    )
    ap_cards.add_argument(
        "--cards", type=Path, default=DEFAULT_CARDS,
        help=f"Card holders CSV (default: {DEFAULT_CARDS})",
    )
    ap_cards.set_defaults(func=cmd_cards)

    # -- rules --
    ap_rules = sub.add_parser(
        "rules", help="List, add, or remove categorization rules"
    )
    ap_rules.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "add", "remove"],
        help="Action to perform (default: list)",
    )
    ap_rules.add_argument(
        "extra", nargs="*",
        help="Additional arguments (e.g. pattern and category for 'add')",
    )
    ap_rules.add_argument(
        "--rules", type=Path, default=DEFAULT_RULES,
        help=f"Rules CSV (default: {DEFAULT_RULES})",
    )
    ap_rules.add_argument(
        "--field", default="description",
        choices=["description", "description_raw"],
        help="Field to match against (default: description)",
    )
    ap_rules.add_argument(
        "--sub", default="",
        help="Subcategory (for 'add')",
    )
    ap_rules.add_argument(
        "--payment", default="",
        help="Payment type override (for 'add'): card, transfer, direct_debit, fee, tax",
    )
    ap_rules.set_defaults(func=cmd_rules)

    # -- export --
    ap_export = sub.add_parser(
        "export", help="Export ledger to a clean UTF-8 CSV"
    )
    ap_export.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_export.add_argument(
        "--out", type=Path, default=DEFAULT_CSV,
        help=f"Output CSV path (default: {DEFAULT_CSV})",
    )
    ap_export.set_defaults(func=cmd_export)

    # -- reclean --
    ap_reclean = sub.add_parser(
        "reclean",
        help="Re-clean all descriptions using current patterns and regenerate report",
    )
    ap_reclean.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_reclean.add_argument(
        "--rules", type=Path, default=DEFAULT_RULES,
        help=f"Categorization rules CSV (default: {DEFAULT_RULES})",
    )
    ap_reclean.add_argument(
        "--out", type=Path, default=DEFAULT_ODS,
        help=f"Output ODS file path (default: {DEFAULT_ODS})",
    )
    ap_reclean.add_argument(
        "--desc-notes", type=Path, default=DEFAULT_DESC_NOTES,
        help=f"Merchant notes CSV (default: {DEFAULT_DESC_NOTES})",
    )
    ap_reclean.add_argument(
        "--backup-dir", type=Path, default=DEFAULT_BACKUPS,
        help=f"Directory for backup archives (default: {DEFAULT_BACKUPS})",
    )
    ap_reclean.set_defaults(func=cmd_reclean)

    # -- backup --
    ap_backup = sub.add_parser(
        "backup", help="Create a zip backup of all data (data/, raw/, ODS report)"
    )
    ap_backup.add_argument(
        "--out", type=Path, default=DEFAULT_BACKUPS,
        help=f"Directory to store the backup zip (default: {DEFAULT_BACKUPS})",
    )
    ap_backup.add_argument(
        "--raw", type=Path, default=DEFAULT_RAW,
        help=f"Directory containing raw bank CSV files (default: {DEFAULT_RAW})",
    )
    ap_backup.add_argument(
        "--ods", type=Path, default=DEFAULT_ODS,
        help=f"ODS report file path (default: {DEFAULT_ODS})",
    )
    ap_backup.set_defaults(func=cmd_backup)

    args = ap.parse_args()

    # Default to 'auto' when no subcommand is given
    if args.cmd is None:
        args = ap.parse_args(["auto"])

    args.func(args)
