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
from .db import (
    IngestError, ingest, ingested_source_files, migrate_schema,
    reclean_descriptions,
)
from .parser import auto_rename_csv, load_card_holders
from .pdf_parser import extract_pdf_date_range
from .rules import (
    load_rules, categorize_transactions,
    add_rule, remove_rule,
    add_card, remove_card, _load_cards_raw,
)
from .starter_rules import import_starter_rules
from .ods import generate_ods, sync_from_ods
from .xlsx import generate_xlsx
from .export import export_csv
from .pdf_report import generate_monthly_pdf, previous_month_label, DEFAULT_REPORTS
from .suggest import analyze_patterns, format_suggestions, accept_suggestion
from .advisor import (
    generate_prompt, save_prompt, load_context, save_context,
    ingest_response, get_response_path, update_context_interactive,
    is_first_run, DEFAULT_CONTEXT_FILE,
    find_next_catchup_month, find_months_without_responses,
)
from .constants import DEFAULT_ADVISOR_DIR
from .email_fetch import (
    load_email_config, create_email_config, fetch_and_report,
    DEFAULT_EMAIL_CONFIG,
)
from .parsers import detect_parser, get_parser_by_id, get_registered_parsers


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


def _is_other_bank_csv(path: Path, bank_id: str | None) -> bool:
    """True for a CSV that goes to a parser other than the UTF-16 one."""
    parser = get_parser_by_id(bank_id) if bank_id else detect_parser(path)
    return parser is not None and parser.bank_id != "utf16"


def _rename_files(files: list[Path], bank_id: str | None = None) -> list[Path]:
    """Auto-rename a list of bank statement files based on their date range.
    Supports CSV and PDF. Returns the (possibly renamed) paths, skipping any that fail.

    The CSV rename reads the UTF-16 CSV header, so a CSV of another bank
    (``bank_id``, or the detected format) keeps its name and is returned as is."""
    result = []
    for f in files:
        try:
            if f.suffix.lower() == ".pdf":
                result.append(_auto_rename_pdf(f))
            elif _is_other_bank_csv(f, bank_id):
                result.append(f)
            else:
                result.append(auto_rename_csv(f))
        except ValueError as e:
            print(f"  Warning: {e}")
            print(f"  Skipping {f.name}.")
    return result


def _auto_rename_pdf(path: Path) -> Path:
    """Auto-rename a PDF statement based on its date range (mirrors auto_rename_csv logic)."""
    date_from, date_to = extract_pdf_date_range(path)

    if date_from.year != date_to.year or date_from.month != date_to.month:
        print(f"  Date range spans multiple months ({date_from} to {date_to}), "
              f"keeping original filename: {path.name}")
        return path

    target_name = date_from.strftime("%Y-%m") + ".pdf"
    target_path = path.parent / target_name

    if path.resolve() == target_path.resolve():
        return path

    if not target_path.exists():
        path.rename(target_path)
        print(f"  Renamed {path.name} -> {target_name} "
              f"(covers {date_from} to {date_to})")
        return target_path

    # Target exists — just keep original name to avoid conflicts
    print(f"  {target_name} already exists, keeping original: {path.name}")
    return path


def _discover_new_files(raw_dir: Path, db_path: Path) -> list[Path]:
    """Find CSV and PDF files in raw_dir that haven't been ingested yet."""
    if not raw_dir.is_dir():
        return []
    already = ingested_source_files(db_path)
    candidates = sorted(
        list(raw_dir.glob("*.csv")) + list(raw_dir.glob("*.pdf")),
        key=lambda p: p.name,
    )
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

    # Step 1: Discover new files (CSV and PDF)
    new_files = _discover_new_files(raw_dir, args.db)

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
        renamed = _rename_files(new_files)
        # Re-discover after renames (names may have changed). Keep the renamed
        # files too: a wider statement that replaced YYYY-MM.csv takes a name
        # already in the DB, and rows are deduplicated on insert anyway.
        new_files = list(dict.fromkeys(
            renamed + _discover_new_files(raw_dir, args.db)
        ))

    ingest_error = None
    if new_files:
        _info(f"Found {len(new_files)} new file(s): {', '.join(f.name for f in new_files)}")
        try:
            ingest(args.db, new_files)
        except IngestError as e:
            # The other files are in: still update the report, then fail
            ingest_error = e
            print(f"Error: {e}")
    else:
        _info("No new files to ingest.")

    # Step 3: Sync manual edits, apply rules, regenerate report
    _detail("")
    
    output_format = getattr(args, "format", "xlsx")
    
    # Sync from ODS if it exists (for backward compatibility)
    ods_path = args.out.with_suffix(".ods") if args.out.suffix == ".xlsx" else args.out
    if ods_path.exists():
        synced = sync_from_ods(args.db, ods_path, args.desc_notes)
        if synced:
            _info(f"Synced {synced} manual edit(s) from {ods_path} back to database.")

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

    # Generate report in the specified format
    if output_format == "xlsx":
        xlsx_path = args.out if args.out.suffix == ".xlsx" else args.out.with_suffix(".xlsx")
        generate_xlsx(args.db, args.rules, xlsx_path, args.desc_notes)
        _info(f"\nDone! Report saved to {xlsx_path}")
    elif output_format == "ods":
        generate_ods(args.db, args.rules, ods_path, args.desc_notes)
        _info(f"\nDone! Report saved to {ods_path}")
    elif output_format == "both":
        generate_xlsx(args.db, args.rules, args.out.with_suffix(".xlsx"), args.desc_notes)
        generate_ods(args.db, args.rules, ods_path, args.desc_notes)
        _info(f"\nDone! Reports saved to {args.out.with_suffix('.xlsx')} and {ods_path}")

    if ingest_error is not None:
        sys.exit(1)


def cmd_ingest(args):
    """Handle the 'ingest' subcommand."""
    _check_setup()
    dry = getattr(args, "dry_run", False)
    files = list(args.files)

    # PDFs never reach the parser registry, so a mistyped --bank would be
    # ignored for them; and a CSV would be renamed before it is rejected.
    bank_id = getattr(args, "bank", None)
    if bank_id and get_parser_by_id(bank_id) is None:
        available = ", ".join(p.bank_id for p in get_registered_parsers())
        print(f"Error: Unknown bank '{bank_id}'. Available: {available or 'none'}")
        sys.exit(1)

    if dry:
        print(f"[dry-run] Would ingest {len(files)} file(s):")
        for f in files:
            print(f"  {f.name}")
        if not args.no_rename:
            print("[dry-run] Would auto-rename files based on date ranges.")
        return

    # Auto-rename files based on date range (unless --no-rename)
    named = len(files)
    if not args.no_rename:
        files = _rename_files(files, bank_id)

    if files:
        try:
            ingest(args.db, files, bank_id=bank_id)
        except IngestError as e:
            print(f"Error: {e}")
            sys.exit(1)

    if len(files) < named:
        # The rename step printed why; a file left out is not a success
        print(
            f"Error: {named - len(files)} of {named} file(s) were skipped and "
            f"not imported (see the warnings above)."
        )
        sys.exit(1)


def cmd_report(args):
    """Handle the 'report' subcommand."""
    _check_setup()
    _set_verbosity(args)
    
    output_format = getattr(args, "format", "xlsx")
    
    # Determine output paths based on format
    out_path = args.out
    xlsx_path = out_path if out_path.suffix == ".xlsx" else out_path.with_suffix(".xlsx")
    ods_path = out_path if out_path.suffix == ".ods" else out_path.with_suffix(".ods")

    # Step 1: Sync back any manual edits from the existing ODS (if it exists)
    if not args.no_sync and ods_path.exists():
        synced = sync_from_ods(args.db, ods_path, args.desc_notes)
        if synced:
            _info(f"Synced {synced} manual edit(s) from {ods_path} back to database.")
    else:
        _detail("Skipping sync (--no-sync flag or no ODS file found).")

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

    # Step 3: Generate report(s)
    if output_format == "ods":
        if args.fresh and ods_path.exists():
            zp = create_backup(args.backup_dir, raw_dir=args.raw, ods_path=ods_path)
            _info(f"Pre-fresh backup: {zp} ({format_size(zp.stat().st_size)})")
            ods_path.unlink()
            _info(f"Deleted existing {ods_path} (--fresh flag).")
        generate_ods(args.db, args.rules, ods_path, args.desc_notes)
    
    elif output_format == "xlsx":
        if args.fresh and xlsx_path.exists():
            xlsx_path.unlink()
            _info(f"Deleted existing {xlsx_path} (--fresh flag).")
        generate_xlsx(args.db, args.rules, xlsx_path, args.desc_notes)
    
    elif output_format == "both":
        if args.fresh:
            if ods_path.exists():
                zp = create_backup(args.backup_dir, raw_dir=args.raw, ods_path=ods_path)
                _info(f"Pre-fresh backup: {zp} ({format_size(zp.stat().st_size)})")
                ods_path.unlink()
            if xlsx_path.exists():
                xlsx_path.unlink()
            _info(f"Deleted existing files (--fresh flag).")
        generate_ods(args.db, args.rules, ods_path, args.desc_notes)
        generate_xlsx(args.db, args.rules, xlsx_path, args.desc_notes)


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

    elif action == "import-starter":
        dry = args.extra and args.extra[0] == "--dry-run"
        added, skipped = import_starter_rules(rules_path, dry_run=dry)
        if dry:
            print(f"[dry-run] Would add {added} starter rule(s), "
                  f"skip {skipped} already present.")
        elif added:
            print(f"Imported {added} Portuguese starter rule(s) into {rules_path}.")
            if skipped:
                print(f"  ({skipped} rule(s) skipped — pattern already exists)")
        else:
            print("All starter rules are already present. Nothing to import.")


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

    # The report below is regenerated from the DB: save unsynced ODS edits first
    synced = sync_from_ods(args.db, args.out, args.desc_notes)
    if synced:
        _info(f"Synced {synced} manual edit(s) from {args.out} back to database.")

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


def cmd_reset(args):
    """Handle the 'reset' subcommand: backup everything and start fresh."""
    import shutil
    from datetime import datetime
    
    # Paths to clean
    data_dir = DEFAULT_DB.parent  # data/
    raw_dir = args.raw
    reports_dir = DEFAULT_REPORTS
    ods_path = DEFAULT_ODS
    xlsx_path = Path("expense-report.xlsx")
    
    # Check if there's anything to reset
    has_data = data_dir.is_dir() and any(data_dir.iterdir())
    has_raw = raw_dir.is_dir() and any(raw_dir.iterdir())
    has_reports = reports_dir.is_dir() and any(reports_dir.iterdir())
    has_ods = ods_path.is_file()
    has_xlsx = xlsx_path.is_file()
    
    if not any([has_data, has_raw, has_reports, has_ods, has_xlsx]):
        print("Nothing to reset - workspace is already clean.")
        return
    
    # Show what will be affected
    print("This will backup and then DELETE the following:")
    if has_data:
        file_count = sum(1 for _ in data_dir.rglob("*") if _.is_file())
        print(f"  - data/ ({file_count} files)")
    if has_raw:
        file_count = sum(1 for _ in raw_dir.rglob("*") if _.is_file())
        print(f"  - raw/ ({file_count} files)")
    if has_reports:
        file_count = sum(1 for _ in reports_dir.rglob("*") if _.is_file())
        print(f"  - reports/ ({file_count} files)")
    if has_ods:
        print(f"  - {ods_path}")
    if has_xlsx:
        print(f"  - {xlsx_path}")
    print()
    
    # Confirm unless --yes flag
    if not args.yes:
        response = input("Are you sure? Type 'yes' to confirm: ")
        if response.lower() != "yes":
            print("Aborted.")
            return
    
    # Create backup first
    backup_dir = args.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    label = f"pre-reset-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}"
    
    print()
    print("Creating backup...")
    zp = create_backup(backup_dir, label=label, raw_dir=raw_dir, ods_path=ods_path)
    size = format_size(zp.stat().st_size)
    print(f"  Backup saved: {zp} ({size})")
    
    # Now delete everything
    print()
    print("Cleaning up...")
    
    if has_data:
        # Keep the data/ directory but remove contents (except advisor context if --keep-context)
        for item in data_dir.iterdir():
            if args.keep_context and item.name == "advisor":
                print(f"  Keeping {item}/")
                continue
            if item.is_dir():
                shutil.rmtree(item)
                print(f"  Removed {item}/")
            else:
                item.unlink()
                print(f"  Removed {item}")
    
    if has_raw:
        # Keep the raw/ directory but remove contents
        for item in raw_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        print(f"  Cleared {raw_dir}/")
    
    if has_reports:
        # Keep the reports/ directory but remove contents
        for item in reports_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        print(f"  Cleared {reports_dir}/")
    
    if has_ods:
        ods_path.unlink()
        print(f"  Removed {ods_path}")
    
    if has_xlsx:
        xlsx_path.unlink()
        print(f"  Removed {xlsx_path}")
    
    print()
    print("Reset complete! Workspace is now clean.")
    print(f"  To restore, unzip: {zp}")


def cmd_pdf(args):
    """Handle the 'pdf' subcommand: generate a monthly PDF report."""
    _check_setup()
    _set_verbosity(args)
    
    if args.all:
        # Generate PDFs for all months with data
        conn = sqlite3.connect(str(args.db))
        try:
            rows = conn.execute(
                "SELECT DISTINCT month FROM transactions ORDER BY month ASC"
            ).fetchall()
            months = [r[0] for r in rows if r[0]]
        finally:
            conn.close()
        
        if not months:
            print("No transactions found in database.")
            return
        
        _info(f"Generating PDFs for {len(months)} month(s)...")
        generated = 0
        for month in months:
            try:
                pdf_path = generate_monthly_pdf(
                    db_path=args.db,
                    month=month,
                    output_path=None,  # Use default path
                    desc_notes_path=args.desc_notes,
                )
                _detail(f"  {month} -> {pdf_path.name}")
                generated += 1
            except Exception as e:
                _info(f"  {month} -> Error: {e}")
        
        print(f"\nGenerated {generated} PDF report(s) in reports/")
    else:
        # Generate for a single month
        month = args.month or previous_month_label()
        pdf_path = generate_monthly_pdf(
            db_path=args.db,
            month=month,
            output_path=args.out,
            desc_notes_path=args.desc_notes,
        )
        print(f"PDF report saved to {pdf_path}")


def cmd_suggest(args):
    """Handle the 'suggest' subcommand: analyze patterns and suggest categories."""
    _check_setup()
    _set_verbosity(args)

    _info("Analyzing transaction patterns...")
    results = analyze_patterns(
        db_path=args.db,
        min_months_recurring=args.min_months,
        similarity_threshold=args.similarity,
        min_count_frequent=args.min_count,
    )

    print()
    print(format_suggestions(results))

    # Summary
    total = (len(results["recurring"]) + len(results["similar"])
             + len(results["frequent"]))
    if total:
        print(f"\nFound {total} suggestion(s). To accept a suggestion as a rule:")
        print(f"  python bank_ingest.py rules add <PATTERN> <CATEGORY>")


def cmd_fetch(args):
    """Handle the 'fetch' subcommand: download statements from email."""
    _check_setup()
    _set_verbosity(args)

    if args.setup:
        # Interactive setup
        print("Email configuration setup")
        print("=" * 40)
        print()
        print("You'll need an app-specific password (not your main password).")
        print("For Gmail: https://myaccount.google.com/apppasswords")
        print("For Outlook: https://account.live.com/proofs/AppPassword")
        print()
        imap_host = input("IMAP host (e.g. imap.gmail.com): ").strip()
        email_addr = input("Email address: ").strip()
        password = input("App password: ").strip()
        imap_port = input("IMAP port [993]: ").strip() or "993"
        folder = input("Mailbox folder [INBOX]: ").strip() or "INBOX"

        config_path = args.config
        create_email_config(
            config_path,
            imap_host=imap_host,
            email_addr=email_addr,
            password=password,
            imap_port=int(imap_port),
            folder=folder,
        )
        print(f"\nConfig saved to {config_path}")
        print("You can now run: python bank_ingest.py fetch")
        return

    dry = getattr(args, "dry_run", False)

    try:
        downloaded = fetch_and_report(
            config_path=args.config,
            output_dir=args.raw,
            days_back=args.days,
            dry_run=dry,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        # Exit non-zero: the cron sync tells a failed fetch by its status
        print(f"Error fetching email: {e}")
        sys.exit(1)

    if not downloaded:
        _info("No new statement attachments found.")
        return

    if dry:
        print(f"\n[dry-run] Would download {len(downloaded)} file(s).")
    else:
        _info(f"Downloaded {len(downloaded)} file(s) to {args.raw}/:")
        for f in downloaded:
            _info(f"  {f.name}")

        if not args.no_ingest:
            _info("\nProceeding to ingest downloaded files...")
            # Trigger auto ingest
            try:
                ingest(args.db, downloaded)
            except IngestError as e:
                print(f"Error: {e}")
                sys.exit(1)


def cmd_gui(args):
    """Handle the 'gui' subcommand: launch the Streamlit web interface."""
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parent.parent
    gui_path = Path(__file__).parent / "gui.py"
    port = args.port

    print(f"Starting Expense Tracker GUI on http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run",
        str(gui_path),
        "--server.port", str(port),
        # Streamlit listens on every interface unless told otherwise; the URL
        # printed above says localhost, and this serves the whole ledger.
        "--server.address", "localhost",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ], cwd=project_root)


def cmd_banks(args):
    """Handle the 'banks' subcommand: list supported bank parsers."""
    parsers = get_registered_parsers()
    if not parsers:
        print("No bank parsers registered.")
        return
    print(f"Supported banks ({len(parsers)}):\n")
    print(f"  {'ID':<10} {'Bank Name'}")
    print(f"  {'─' * 10} {'─' * 30}")
    for p in parsers:
        print(f"  {p.bank_id:<10} {p.name}")
    print()
    print("The system auto-detects the bank format. Use --bank <ID> to override.")


def cmd_advisor(args):
    """Handle the 'advisor' subcommand: generate LLM prompts for financial advice."""
    _check_setup()
    
    action = args.action
    
    # Handle catchup mode - find next month without response
    if action == "catchup":
        missing = find_months_without_responses(args.db, args.out)
        if not missing:
            print("All months are up to date! No catch-up needed.")
            return
        
        print(f"Found {len(missing)} month(s) without advisor responses:")
        for m in missing:
            print(f"  - {m}")
        print()
        
        # Use the oldest missing month
        month = missing[0]
        print(f"Generating prompt for oldest missing month: {month}")
        action = "generate"  # Fall through to generate
    else:
        month = args.month or previous_month_label()
    
    if action == "generate":
        # Generate the prompt
        _info(f"Generating advisor prompt for {month}...")
        prompt = generate_prompt(
            db_path=args.db,
            context_path=args.context,
            target_month=month,
        )
        
        # Save to file
        prompt_path = save_prompt(prompt, month, args.out)
        _info(f"\nPrompt saved to: {prompt_path}")
        
        # Show instructions
        response_path = get_response_path(month, args.out)
        print(f"""
Next steps:
  1. Open {prompt_path}
  2. Copy the contents and paste into your LLM (ChatGPT, Claude, etc.)
  3. Save the LLM's response to: {response_path}
  4. Run: ./run.sh advisor ingest --month {month}
""")
        
        context = load_context(args.context)
        if is_first_run(context):
            print("Note: This is your first advisor session. The prompt includes")
            print("      discovery questions to establish your financial context.")
    
    elif action == "ingest":
        # Ingest a response
        response_path = args.response or get_response_path(month, args.out)
        
        if not response_path.exists():
            print(f"Error: Response file not found: {response_path}")
            print(f"\nSave your LLM's response to this file and try again.")
            return
        
        _info(f"Ingesting response from: {response_path}")
        ingest_response(response_path, args.context, month)
        _info("Response recorded in context.")
        
        # Offer interactive context update
        if not args.no_interactive:
            try:
                update = input("\nWould you like to update your context now? [y/N] ").strip().lower()
                if update == "y":
                    update_context_interactive(args.context)
            except (EOFError, KeyboardInterrupt):
                print()
    
    elif action == "context":
        # Show or update context
        context = load_context(args.context)
        
        if args.edit:
            update_context_interactive(args.context)
        else:
            # Display current context
            import json
            print(json.dumps(context, indent=2, ensure_ascii=False))
    
    elif action == "status":
        # Show advisor status
        context = load_context(args.context)
        missing = find_months_without_responses(args.db, args.out)
        
        print("Advisor Status")
        print("=" * 40)
        
        if is_first_run(context):
            print("Status: Not yet initialized")
            print("\nRun './run.sh advisor' to generate your first prompt.")
        else:
            print(f"Context file: {args.context}")
            print(f"Created: {context.get('created_at', 'Unknown')}")
            print(f"Last updated: {context.get('last_updated', 'Unknown')}")
            
            goals = context.get("goals", {})
            total_goals = sum(len(g) for g in goals.values())
            print(f"Goals tracked: {total_goals}")
            
            insights = context.get("insights", [])
            print(f"Insights recorded: {len(insights)}")
            
            responses = context.get("responses", {})
            print(f"Months with responses: {len(responses)}")
            if responses:
                print(f"  Latest: {max(responses.keys())}")
        
        # Show missing months
        if missing:
            print(f"\nMonths needing catch-up: {len(missing)}")
            if len(missing) <= 5:
                for m in missing:
                    print(f"  - {m}")
            else:
                for m in missing[:3]:
                    print(f"  - {m}")
                print(f"  ... and {len(missing) - 3} more")
            print(f"\nRun './run.sh advisor catchup' to start from {missing[0]}")
        else:
            print("\nAll months up to date!")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Expense tracking pipeline for Portuguese bank statements."
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
        "--out", type=Path, default=Path("expense-report.xlsx"),
        help="Output file path (default: expense-report.xlsx)",
    )
    ap_auto.add_argument(
        "--format", type=str, default="xlsx",
        choices=["ods", "xlsx", "both"],
        help="Output format: xlsx (default), ods (LibreOffice), or both",
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
        "ingest", help="Parse specific bank statement files (CSV or PDF) and store in SQLite"
    )
    ap_ingest.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_ingest.add_argument(
        "--bank", type=str, default=None,
        help="Bank format to use (e.g. 'utf16', 'utf8'). Auto-detected if omitted.",
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
        "files", nargs="+", type=Path, help="Bank statement files to ingest (CSV or PDF)",
    )
    ap_ingest.set_defaults(func=cmd_ingest)

    # -- report --
    ap_report = sub.add_parser(
        "report", help="Apply rules and generate expense report (ODS, XLSX, or both)"
    )
    ap_report.add_argument(
        "--format", type=str, default="xlsx",
        choices=["ods", "xlsx", "both"],
        help="Output format: xlsx (default, works with Excel/Numbers/Sheets), ods (LibreOffice), or both",
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
        "--out", type=Path, default=Path("expense-report.xlsx"),
        help="Output file path (default: expense-report.xlsx)",
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
        choices=["list", "add", "remove", "import-starter"],
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

    # -- reset --
    ap_reset = sub.add_parser(
        "reset",
        help="Backup everything and delete all data for a fresh start",
    )
    ap_reset.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    ap_reset.add_argument(
        "--keep-context", action="store_true",
        help="Keep the advisor context file (data/advisor/context.json)",
    )
    ap_reset.add_argument(
        "--raw", type=Path, default=DEFAULT_RAW,
        help=f"Directory containing raw bank files (default: {DEFAULT_RAW})",
    )
    ap_reset.add_argument(
        "--backup-dir", type=Path, default=DEFAULT_BACKUPS,
        help=f"Directory to store the backup zip (default: {DEFAULT_BACKUPS})",
    )
    ap_reset.set_defaults(func=cmd_reset)

    # -- pdf --
    ap_pdf = sub.add_parser(
        "pdf", help="Generate a single-page monthly PDF summary report"
    )
    ap_pdf.add_argument(
        "--month", type=str, default=None,
        help="Month to report on (YYYY-MM format, default: previous month)",
    )
    ap_pdf.add_argument(
        "--all", action="store_true",
        help="Generate PDFs for all months with transaction data",
    )
    ap_pdf.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_pdf.add_argument(
        "--out", type=Path, default=None,
        help="Output PDF file path (default: reports/report-YYYY-MM.pdf)",
    )
    ap_pdf.add_argument(
        "--desc-notes", type=Path, default=DEFAULT_DESC_NOTES,
        help=f"Merchant notes CSV (default: {DEFAULT_DESC_NOTES})",
    )
    ap_pdf.set_defaults(func=cmd_pdf)

    # -- suggest --
    ap_suggest = sub.add_parser(
        "suggest",
        help="Analyze transaction patterns and suggest categorization rules",
    )
    ap_suggest.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_suggest.add_argument(
        "--min-months", type=int, default=3,
        help="Minimum months for recurring detection (default: 3)",
    )
    ap_suggest.add_argument(
        "--similarity", type=float, default=0.65,
        help="Name similarity threshold 0.0-1.0 (default: 0.65)",
    )
    ap_suggest.add_argument(
        "--min-count", type=int, default=5,
        help="Minimum transaction count for frequent merchants (default: 5)",
    )
    ap_suggest.set_defaults(func=cmd_suggest)

    # -- fetch --
    ap_fetch = sub.add_parser(
        "fetch",
        help="Download bank statements from email via IMAP",
    )
    ap_fetch.add_argument(
        "--setup", action="store_true",
        help="Interactive setup: create email configuration file",
    )
    ap_fetch.add_argument(
        "--config", type=Path, default=DEFAULT_EMAIL_CONFIG,
        help=f"Email config JSON file (default: {DEFAULT_EMAIL_CONFIG})",
    )
    ap_fetch.add_argument(
        "--raw", type=Path, default=DEFAULT_RAW,
        help=f"Directory to save downloaded files (default: {DEFAULT_RAW})",
    )
    ap_fetch.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_fetch.add_argument(
        "--days", type=int, default=60,
        help="How many days back to search (default: 60)",
    )
    ap_fetch.add_argument(
        "--no-ingest", action="store_true",
        help="Download only, don't auto-ingest the files",
    )
    ap_fetch.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be downloaded without saving anything",
    )
    ap_fetch.set_defaults(func=cmd_fetch)

    # -- gui --
    ap_gui = sub.add_parser(
        "gui", help="Launch the Streamlit web interface"
    )
    ap_gui.add_argument(
        "--port", type=int, default=8501,
        help="Port for the web server (default: 8501)",
    )
    ap_gui.set_defaults(func=cmd_gui)

    # -- banks --
    ap_banks = sub.add_parser(
        "banks", help="List supported bank statement formats"
    )
    ap_banks.set_defaults(func=cmd_banks)

    # -- advisor --
    ap_advisor = sub.add_parser(
        "advisor",
        help="Generate LLM prompts for financial advice and track context",
    )
    ap_advisor.add_argument(
        "action", nargs="?", default="generate",
        choices=["generate", "ingest", "context", "status", "catchup"],
        help="Action: generate prompt (default), ingest response, show/edit context, status, or catchup (oldest missing month)",
    )
    ap_advisor.add_argument(
        "--month", type=str, default=None,
        help="Month to analyze (YYYY-MM format, default: previous month)",
    )
    ap_advisor.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    ap_advisor.add_argument(
        "--out", type=Path, default=DEFAULT_ADVISOR_DIR,
        help=f"Output directory for prompts/responses (default: {DEFAULT_ADVISOR_DIR})",
    )
    ap_advisor.add_argument(
        "--context", type=Path, default=DEFAULT_CONTEXT_FILE,
        help=f"Context JSON file (default: {DEFAULT_CONTEXT_FILE})",
    )
    ap_advisor.add_argument(
        "--response", type=Path, default=None,
        help="Path to response file (for 'ingest' action)",
    )
    ap_advisor.add_argument(
        "--no-interactive", action="store_true",
        help="Skip interactive context update after ingesting",
    )
    ap_advisor.add_argument(
        "--edit", action="store_true",
        help="Edit context interactively (for 'context' action)",
    )
    ap_advisor.set_defaults(func=cmd_advisor)

    args = ap.parse_args()

    # Default to 'auto' when no subcommand is given (keeping -q / -v)
    if args.cmd is None:
        args = ap.parse_args(sys.argv[1:] + ["auto"])

    args.func(args)
