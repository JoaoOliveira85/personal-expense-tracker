# Developer Guide

This document covers the architecture, design decisions, and internals of the Expense Tracker project. It's intended for future development — whether that's you coming back in six months, or anyone else looking to extend the codebase.

---

## Architecture Overview

The system follows a **three-layer pipeline** architecture:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Raw CSVs    │────>│ Auto-detect  │────>│ Bank Parser  │────>│   SQLite DB  │────>│  ODS Report  │
│  (raw/)      │     │ (parsers/)   │     │ (UTF-16/UTF8/...)│     │  (ledger.db) │     │  (odfpy)     │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                           │                     │                     │
                     ┌─────┴──────┐        ┌─────┴──────┐       ┌─────┴──────┐
                     │ noise-     │        │ rules.csv  │       │ Manual     │
                     │ words.txt  │        │ (categorize│       │ edits sync │
                     │ cleaning-  │        │  on read)  │       │ back to DB │
                     │ patterns.csv│       └────────────┘       └────────────┘
                     └────────────┘
```

### Data flow

1. **Ingest**: Raw bank CSVs are auto-detected (or explicitly specified), parsed by the appropriate bank parser, cleaned, and inserted into SQLite with SHA-1 deduplication
2. **Categorize**: Rules from `data/rules.csv` are applied to every transaction not categorized by hand (longest matching pattern wins)
3. **Sync back**: Before regenerating the report, manual edits (category, subcategory, notes, merchant notes) are read from the existing ODS and written back to the DB or `description-notes.csv`
4. **Generate**: The ODS report is created/updated using `odfpy` — Intro/Data/Rules sheets are always rebuilt, analysis sheets are generated only on the first run and then preserved

### Key design principle: SQLite as the source of truth

The database is the canonical store for all transaction data. The ODS is a **view** that happens to support manual edits. Every run syncs edits from the ODS into the DB before regenerating, so the DB always has the latest state.

---

## Module Reference

### `bank_ingest.py`

A thin convenience wrapper that imports and calls `expense_tracker.cli.main()`. This exists so users can run `python bank_ingest.py` from the project root without knowing about the package structure.

### `expense_tracker/constants.py`

Centralizes all default paths and column definitions. Every other module imports from here rather than hardcoding paths. This makes it easy to change the project layout or test with custom paths.

**Key exports:**
- `DEFAULT_*` paths — all configurable file locations
- `DATA_COLUMNS` / `DATA_HEADERS` — column order for the Data sheet and CSV export (these two lists must stay in sync)
- `COL_*` — zero-based column indices for ODS sync-back (must match `DATA_COLUMNS` order)

**If you add a new column:** Update `DATA_COLUMNS`, `DATA_HEADERS`, and add a `COL_*` constant if it needs sync-back. Also update `_build_data_sheet()` in `ods.py` (column styles) and `fetch_all_transactions()` in `db.py` (SELECT query).

### `expense_tracker/parsers/` — Multi-bank parser framework

A plugin-based parser architecture that supports multiple bank statement formats. Each bank gets its own parser module that implements the `BankParser` protocol.

#### `expense_tracker/parsers/__init__.py`

Defines the core framework: the `BankParser` protocol, the parser registry, and the auto-detection/dispatch logic.

**Key exports:**
- `BankParser` — A `Protocol` class defining the interface for all bank parsers. Methods: `name`, `bank_id`, `can_parse(path)`, `parse(path, cards_path)`, `extract_date_range(path)`.
- `register_parser(parser)` — Register a `BankParser` instance in the global registry.
- `get_registered_parsers()` — Returns all registered parsers as a dict of `{bank_id: parser}`.
- `detect_parser(path)` — Auto-detects which parser can handle a file by calling `can_parse()` on each registered parser.
- `parse_statement(path, cards_path, bank_id)` — Unified entry point. If `bank_id` is given, uses that parser; otherwise auto-detects. Raises `ValueError` if no parser matches.

#### `expense_tracker/parsers/utf16_csv.py`

Implements `BankParser` for UTF-16 CSV files. Wraps the existing logic in `expense_tracker.parser` (UTF-16 LE, semicolons, Portuguese dates/numbers).

#### `expense_tracker/parsers/utf8_csv.py`

Implements `BankParser` for UTF-8 CSV CSV files. Handles UTF8's specific format: UTF-8 encoding, different header patterns, separate debit/credit columns.

### `expense_tracker/parser.py` (legacy)

The original UTF-16 CSV parser. Still used internally by `parsers/utf16_csv.py` — all the parsing logic lives here. If you're adding a new bank, you don't need to touch this file; create a new module in `parsers/` instead.

**Key functions:**
- `parse_utf16_csv(path, cards_path)` — Main entry point. Reads UTF-16 LE CSV, finds the header row, parses each transaction line, detects payment types, cleans descriptions, identifies card holders.
- `clean_description(raw)` — Strips bank prefixes, noise words, transaction codes from raw descriptions. Uses patterns loaded from `data/noise-words.txt` and `data/cleaning-patterns.csv`.
- `detect_payment_type(description)` — Pattern-matches the raw description to infer `card`, `transfer`, `direct_debit`, `atm`, `fee`, `tax`, or `other`.
- `detect_card(description, known_cards)` — Finds 4-digit card numbers in the description and matches against known card holders.
- `auto_rename_csv(path)` — Renames bank CSVs from their original names (e.g. `EXPORT_0_1022026.csv`) to date-based names (e.g. `2026-01.csv`) using the date range in the file header.
- `_get_cleaning_patterns()` — Loads and compiles regexes from the external config files. Results are cached in a module-level global; call `reset_cleaning_cache()` to force a reload.

**UTF-16 CSV format quirks:**
- UTF-16 LE encoding (sometimes with BOM, sometimes without)
- Semicolon delimiters, not commas
- Non-tabular header lines before the actual data (date range, account info)
- Portuguese date format: `dd-mm-YYYY`
- Portuguese decimal format: comma separator (`-45,50`)
- Descriptions can contain semicolons (handled via csv.reader quoting)

**Cleaning pattern architecture:**
- `noise-words.txt`: Simple word list, matched with space boundaries (so "PT" won't match inside "CONTINENTE.PT")
- `cleaning-patterns.csv`: Regex patterns, split into `prefix` (anchored to start) and `noise` (matched anywhere). Patterns are combined into two compiled regexes for performance.
- The transaction code regex `\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,12}\b` uses a lookahead to require at least one digit, preventing pure alphabetic merchant names from being stripped.

### `expense_tracker/db.py`

SQLite storage layer. Handles schema creation, migrations, ingestion, and queries.

**Key functions:**
- `tx_id(row)` — Generates a 16-character hex hash (SHA-1) for deduplication. The hash key includes account, dates, description, amount, and balance.
- `ensure_schema(conn)` — Creates the `transactions` table if it doesn't exist.
- `migrate_schema(conn)` — Adds columns that may be missing from an older schema (for forward compatibility), and the `ods_baseline` table used by ODS sync-back.
- `ingest(db_path, paths, cards_path, bank_id)` — Parses files using the multi-bank parser framework (auto-detection or explicit `bank_id`) and inserts rows with `INSERT OR IGNORE` for deduplication. PDFs go to the PDF parser; a file that is neither `.csv` nor `.pdf` goes to the legacy UTF-16 parser when no `bank_id` is given. Each file is imported in its own transaction: a file that fails is reported and skipped, and `IngestError` is raised once the others are in.
- `reclean_descriptions(db_path)` — Re-runs `clean_description()` on all rows and updates only those that changed.
- `fetch_all_transactions(conn)` — Returns all transactions as a list of dicts, sorted by date descending. Adds a computed `status` field.

**Schema migration approach:** Simple `ALTER TABLE ADD COLUMN` for each missing column. No migration versioning — the code checks which columns exist and adds any that are missing. This is sufficient for a personal project but would need a proper migration system at scale.

### `expense_tracker/rules.py`

Rule and card-holder management, plus the categorization engine.

**Key functions:**
- `add_rule()` / `remove_rule()` — CRUD for `data/rules.csv`. Creates the file with headers if it doesn't exist.
- `add_card()` / `remove_card()` — CRUD for `data/account-holders.csv`. `add_card` raises `ValueError` on duplicates.
- `load_rules(path)` — Reads rules CSV, pre-computes upper-case patterns for matching.
- `categorize_transactions(conn, rules)` — Applies rules via `match_rule()` to uncategorized and rule-categorized transactions, recording `category_source = 'rule'`; rule rows no rule matches any more become uncategorized. Never touches `category_source = 'manual'` rows (set by ODS sync-back). On first run, categories predating `category_source` are adopted as `rule` only if they equal what the old first-substring-match algorithm gives with the current rules, otherwise `manual`.
- `match_rule(rules, desc_raw, desc_clean)` — Returns the winning rule or `None`. Patterns are case-insensitive and must start at a word boundary (a trailing space also requires one at the end). The longest matching pattern wins; ties fall back to file order.

**Matching behavior:** When `match_field` is `description` (default), the rule pattern is matched against **both** the raw and cleaned descriptions (concatenated). When `match_field` is `description_raw`, only the raw description is searched.

### `expense_tracker/ods.py`

ODS report orchestration and sync-back. Uses `odfpy` to create/update OpenDocument Spreadsheets. Sheet builders have been extracted to `ods_sheets.py` for maintainability.

**Key functions:**
- `generate_ods(db_path, rules_path, ods_path, desc_notes_path)` — Main entry point. On first run, generates all sheets. On subsequent runs, replaces only Intro/Data/Rules and preserves everything else.
- `sync_from_ods(db_path, ods_path, desc_notes_path)` — Reads manual edits from the ODS Data sheet back into the DB (category, subcategory, notes) and into `description-notes.csv` (merchant notes). A cell only counts as an edit if it differs from the `ods_baseline` value recorded when the ODS was last generated or synced, so a stale ODS never overwrites newer DB changes.

### `expense_tracker/ods_sheets.py`

All ODS sheet builders, styles, and cell helpers. Split from `ods.py` for maintainability.

**Key exports:**
- `setup_styles(doc)` — Registers cell and column styles (header, categorized, uncategorized, etc.)
- `make_cell(value, style_name, value_type, formula)` — Creates a typed ODF cell element
- `make_header_row(headers)` — Creates a styled header row
- `build_data_sheet` / `build_rules_sheet` / `build_intro_sheet` — Return Table elements (used for insertion in update mode)
- `write_*_sheet(doc, ...)` — Each adds a sheet directly to the document (used in first-run mode)
- Sheets: Intro, Data, Rules, Dashboard (with top merchants + uncategorized preview), Monthly Summary, Monthly Trend, Category Breakdown, Subcategory Breakdown, Tags, Recurring Merchants

**Hybrid regeneration strategy:**
- **Always rebuilt:** Intro, Data, Rules — these reflect the current state of the DB
- **First run only:** Dashboard, Monthly Summary, Monthly Trend, Category Breakdown, Subcategory Breakdown, Tags, Recurring Merchants — generated with starter formulas/data, then never touched again
- **Never touched:** Any sheets the user adds manually

This means users can customize the analysis sheets (change formulas, add charts, reformat) without losing their work. The `--fresh` flag forces a full regeneration by deleting the ODS first.

**Formula notation:** ODS uses OpenFormula with bracket notation: `[.Data.B2:.Data.B100]`. This is different from what you see in LibreOffice's formula bar (`Data.B2:B100`). All formulas in the code use the `of:=` prefix for OpenFormula.

### `expense_tracker/export.py`

Simple CSV export — reads all transactions from the DB and writes a UTF-8 CSV using `DATA_COLUMNS`/`DATA_HEADERS` from constants.

### `expense_tracker/pdf_report.py`

Monthly PDF report generator using `fpdf2`. Produces a single-page A4 summary for a given month.

**Key functions:**
- `generate_monthly_pdf(db_path, month, output_path, desc_notes_path)` — Main entry point. Fetches transactions for the given month, computes stats, and renders a PDF.
- `_compute_stats(transactions, merchant_notes)` — Computes all summary statistics: income/expenses, category breakdown, top merchants, tag totals, uncategorized count. Refunds (`db.is_refund`: incoming money in a category outside `NON_SPENDING_CATEGORIES` and `SAVINGS_CATEGORIES`) are subtracted from their category and from expenses, not added to income. `SAVINGS_CATEGORIES` transactions count as neither spending nor income (`db.is_savings`, `counts_as_spending`, `is_income`). The same rules are implemented as `SPEND_SQL`/`REFUND_SQL`/`INCOME_SQL`/`NOT_SAVINGS_SQL` (advisor, PDF history), `_spend()`/`_income()` formula builders in `xlsx.py` and `ods_sheets.py`, and the `spend` field added by `fetch_all_transactions()` (GUI).
- `previous_month_label()` — Returns the YYYY-MM label for the previous month.
- `_setup_fonts(pdf)` — Tries to register a system Unicode font; falls back to built-in Helvetica.

**PDF layout:** Title bar, 3 summary boxes (income/expenses/net), two-column layout with categories on the left and merchants + tags on the right, optional per-person breakdown at the bottom.

### `expense_tracker/backup.py`

Backup utilities using Python's `zipfile` module (no external dependencies).

**Key functions:**
- `create_backup(backup_dir, label, raw_dir, data_dir, ods_path)` — Creates a zip archive of `data/`, `raw/`, and `expense-report.ods`. If `label` is given, it's used as the filename stem; otherwise a timestamp is generated.
- `previous_month_backup_exists(backup_dir)` — Checks whether a `backup-YYYY-MM.zip` for the previous month already exists.
- `create_monthly_backup(backup_dir, **kwargs)` — Convenience wrapper that calls `create_backup()` with the previous-month label.
- `list_backups(backup_dir)` — Returns sorted list of `backup-*.zip` files.
- `format_size(bytes)` — Human-readable file size string (e.g. "2.3 MB").

**Naming convention:**
- Monthly auto-backups: `backup-YYYY-MM.zip` (named after the previous month)
- Manual / pre-destructive: `backup-YYYY-MM-DDThh-mm-ss.zip` (timestamped)

### `expense_tracker/starter_rules.py`

A curated collection of ~100 categorization rules tailored for Portuguese merchants and services. These cover supermarkets (Continente, Pingo Doce, Lidl, etc.), utilities (EDP, Galp), telecoms (NOS, MEO, Vodafone), transport (CP, VIVA, Uber), health, insurance, eating out, subscriptions, taxes, and bank fees.

**Key exports:**
- `STARTER_RULES` — The master list of `(pattern, match_field, category, subcategory, payment_type)` tuples
- `get_starter_rules()` — Returns the list as dicts (matching the `rules.csv` format)
- `import_starter_rules(rules_path, dry_run=False)` — Appends starter rules to an existing `rules.csv`, skipping any patterns that already exist (case-insensitive). Returns `(added_count, skipped_count)`.

### `expense_tracker/suggest.py`

Analyzes transaction data to detect patterns and suggest categorization rules. Uses frequency analysis, text similarity (via `difflib.SequenceMatcher`), and temporal analysis to find uncategorized merchants that could benefit from rules.

**Key functions:**
- `analyze_patterns(db_path, min_months_recurring, similarity_threshold, min_count_frequent)` — Main entry point. Returns a dict with three lists: `recurring`, `similar_merchants`, and `frequent`.
- `detect_recurring(transactions, min_months)` — Finds uncategorized merchants appearing in N+ distinct months.
- `detect_similar_merchants(transactions, threshold)` — Clusters merchant descriptions by text similarity to spot variants of the same merchant.
- `detect_frequent_merchants(transactions, min_count)` — Finds uncategorized merchants by raw transaction count.
- `format_suggestions(results)` — Formats the analysis results as human-readable text.
- `accept_suggestion(results, key, rules_path)` — Accepts a suggestion by type:index (e.g. `recurring:0`) and appends it as a new rule.

**Internal helpers:**
- `_merchant_stats(transactions)` — Computes per-merchant frequency, totals, average, stddev, and date range.
- `_similarity(a, b)` — Text similarity ratio (0-1) using `SequenceMatcher`.
- `_cluster_by_name(names, threshold)` — Groups similar strings into clusters.
- `_extract_common_prefix(names)` — Finds the longest common prefix of a list of strings.

### `expense_tracker/pdf_parser.py`

Parses UTF-16 CSV monthly PDF bank statements using `pdfplumber`. The PDF parser extracts transaction tables from each page, detects column mappings, and produces the same `list[dict]` output format as the CSV parser.

**Key functions:**
- `parse_pdf_statement(path, cards_path)` — Main entry point. Extracts tables from the PDF, identifies transaction rows, parses dates/amounts, and cleans descriptions (reuses `clean_description`, `detect_card`, `detect_payment_type` from `parser.py`).
- `extract_pdf_date_range(path)` — Extracts the statement date range from the PDF text (used for auto-renaming).
- `_parse_date(text)` / `_parse_amount(text)` — Helpers for Portuguese date and number formats.
- `_find_column_mapping(header_cells)` — Auto-detects column positions in the extracted table.

**PDF format handling:**
- Uses `pdfplumber` to extract tables page by page
- Handles Portuguese date/number formats (dd/mm/YYYY, comma decimals)
- Auto-detects header rows and column layout
- Skips summary/balance rows and non-transaction data

### `expense_tracker/email_fetch.py`

Handles fetching bank statement attachments from email via IMAP. Uses only Python standard library modules (`imaplib`, `email`).

**Key functions:**
- `load_email_config(path)` — Loads and validates `data/email-config.json` (IMAP host, email, password, bank senders).
- `create_email_config(path, ...)` — Creates the config file interactively or programmatically.
- `fetch_statements(config, output_dir, days_back, dry_run)` — Connects to the IMAP server, searches for emails from known bank senders with relevant subjects, downloads PDF/CSV attachments to `raw/`, skipping files that already exist. Returns list of downloaded paths.

**Internal helpers:**
- `_decode_header_value(raw)` — Decodes RFC 2047 encoded email headers.
- `_matches_bank_sender(from_addr, senders)` — Checks if an email's From address matches any configured bank sender.
- `_is_statement_attachment(filename)` — Checks if a filename looks like a bank statement (PDF/CSV with relevant keywords).
- `_matches_subject(subject)` — Filters emails by subject line (looking for "extrato", "movimentos", "statement", etc.).

**Security note:** The email config file (`data/email-config.json`) is gitignored. It stores IMAP credentials in plain text — users should use app-specific passwords where possible.

### `expense_tracker/gui.py`

A Streamlit-based web interface that provides graphical access to the expense tracker's core functionality. The GUI is launched as a subprocess by the CLI (`gui` command) and shares the same SQLite database.

**Pages:**
- **Dashboard** — Overview charts: spending by category (pie), monthly trend (bar), key figures (total income/expenses/net, uncategorized count).
- **Transactions** — Full transaction table with filters for date range, category, subcategory, payment type, and amount range. Supports search by description.
- **Categorize** — Bulk categorization workflow: groups uncategorized transactions by merchant, lets you assign a category to all at once, and optionally creates a rule for future imports.
- **Rules** — View all categorization rules in a table, add new rules, or remove existing ones.
- **Import** — File uploader for CSV/PDF files. Uploads are saved to `raw/` and ingested into the database.
- **Tools** — Email configuration (setup/update IMAP settings), fetch from email, sync ODS back to DB, download ODS and PDF reports, create backups, export CSV, import Portuguese starter rules.
- **Manual** — Renders all project documentation (README, DEPLOYMENT, CONTRIBUTING) in tabbed view with section search. Docs are always in sync with the source markdown files.

**Key implementation details:**
- Uses `@st.cache_data(ttl=5)` for data loading to balance freshness with performance.
- Calls existing backend functions (`fetch_all_transactions`, `ingest`, `add_rule`, `remove_rule`, `categorize_transactions`) — no business logic is duplicated.
- The `cmd_gui` function in `cli.py` launches Streamlit via `subprocess.run`.

### `expense_tracker/cli.py`

`argparse`-based CLI. Each subcommand has its own `cmd_*` function. The `auto` command is the default (used by `run.sh`) and orchestrates the full pipeline: discover → rename → ingest → sync → categorize → generate.

**Pre-flight checks:** `_check_setup()` verifies that `odfpy` is installed and the `data/` directory exists, providing friendly error messages that point to `install.sh`.

**Automatic backups:** The `auto` command creates a monthly backup for the previous month if one doesn't exist. The `report --fresh` and `reclean` commands create timestamped backups before destructive operations.

**Output control:** Global `-q`/`--quiet` and `-v`/`--verbose` flags control verbosity. The `_info()` and `_detail()` helpers respect the verbosity level. Dry-run mode (`--dry-run` on `auto` and `ingest`) previews actions without making changes.

---

## Testing

### Running tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

### Test structure

```
tests/
├── conftest.py          # Shared fixtures and UTF-16 CSV builder
├── test_parser.py       # 39 tests: cleaning, detection, parsing, renaming
├── test_db.py           # 19 tests: schema, ingestion, dedup, reclean, queries
├── test_rules.py        # 17 tests: CRUD, loading, categorization logic
├── test_export.py       #  4 tests: CSV export
├── test_backup.py       # 16 tests: zip creation, monthly checks, formatting
├── test_pdf_report.py   # 26 tests: stats computation, tag extraction, PDF generation
├── test_integration.py  #  4 tests: end-to-end workflows
├── test_starter_rules.py # 14 tests: starter rules data integrity + import logic
├── test_suggest.py      # 28 tests: pattern detection, clustering, suggestions
├── test_pdf_parser.py   # 34 tests: PDF parsing, date/amount helpers, column detection
├── test_email_fetch.py  # 26 tests: email config, IMAP fetch, attachment filtering
├── test_gui.py          #  6 tests: GUI data loading, module structure
└── test_parsers.py      # 29 tests: parser registry, auto-detection, UTF-16 + UTF8 parsing
```

**Total: 285 tests** (runs in under a second)

### Test design principles

- **No real bank data**: All tests use synthetic UTF-16 CSV files created by `make_utf16_csv()` in `conftest.py`. This helper generates valid UTF-16 LE files with the correct UTF-16 header format.
- **Full isolation**: Every test uses pytest's `tmp_path` for file I/O. The parser's cleaning cache is reset between tests via `reset_cleaning_cache()`.
- **The `populated_db` fixture**: Creates a database with 5 sample transactions, ready for categorization/export tests.
- **Cleaning pattern injection**: Tests load their own `noise-words.txt` and `cleaning-patterns.csv` via fixtures, avoiding dependency on project-level config files.

### Adding new tests

1. If your test needs a database with transactions, use the `populated_db` fixture
2. If your test involves description cleaning, add the `noise_words` and `cleaning_patterns` fixtures and call `reset_cleaning_cache()` + `_get_cleaning_patterns()` in setup
3. For new CSV format tests, use `make_utf16_csv()` to create test files
4. Keep tests focused — one assertion per concept, descriptive names

---

## Design Decisions

### Why ODS (not XLSX)?

ODS is an open ISO standard. It works natively in LibreOffice (the primary target), can be uploaded to Google Sheets, and has a Python library (`odfpy`) that doesn't require a commercial dependency. XLSX would require `openpyxl`, which is fine but ODS was chosen for openness.

### Why SQLite (not just CSV)?

- **Deduplication**: SHA-1 hashing prevents duplicate imports even if you re-import the same file
- **Manual edits persistence**: Categories and notes survive report regeneration because they live in the DB
- **Query flexibility**: Easy to add new analysis without re-parsing raw files
- **Atomic operations**: No risk of corrupted partial writes

### Why external cleaning patterns?

Originally, cleaning regexes were hardcoded in `parser.py`. They were externalized to `noise-words.txt` and `cleaning-patterns.csv` so users can tweak cleaning behavior without editing Python code. The `reclean` command lets you reprocess existing data after changing patterns.

### Why two-level notes (per-transaction + per-merchant)?

Per-transaction notes (column R) are for one-off annotations ("birthday dinner"). Merchant notes (column S, backed by `description-notes.csv`) apply to ALL transactions from that merchant, reducing repetition for recurring expenses. Both are synced back independently.

### Why first-run-only analysis sheets?

Analysis sheets (Dashboard, Monthly Summary, etc.) contain starter formulas that reference the Data sheet. If they were regenerated every run, any user customizations (formatting, extra formulas, charts) would be lost. The "first run only" approach generates useful starting points that users can then customize freely.

### Why `INSERT OR IGNORE` for deduplication?

The `tx_id()` hash includes enough fields (account, dates, description, amount, balance) to make collisions extremely unlikely for real bank data. `INSERT OR IGNORE` makes re-importing idempotent — the same file can be imported multiple times safely.

### Why a Protocol-based parser architecture?

The `BankParser` protocol (in `parsers/__init__.py`) defines the interface that all bank parsers must implement. This was chosen over an abstract base class (ABC) because:
- **Duck typing**: Existing code doesn't need to inherit from anything — any class with the right methods works.
- **Plugin-friendly**: New parsers just implement the protocol and register themselves. No changes to existing code needed.
- **Auto-detection**: Each parser implements `can_parse()` so the system can automatically identify which bank a file belongs to.
- **Backward compatibility**: The original `parser.py` continues to work unchanged; `parsers/utf16_csv.py` wraps it.

### Why the `cards_path` parameter on `ingest()`?

Added for test isolation. Without it, `ingest()` would always read from `data/account-holders.csv`, making tests depend on project-level config files. The parameter defaults to `None` (which falls back to the default path) so existing callers don't need to change.

---

## Extending the Project

### Adding a new bank parser

The project uses a plugin architecture — adding a new bank requires no changes to existing code.

1. Create `expense_tracker/parsers/newbank.py` with a class implementing the `BankParser` protocol:
   - `name` (property) — Human-readable name (e.g. "Novo Banco")
   - `bank_id` (property) — Short ID (e.g. "nb")
   - `can_parse(path)` — Return `True` if the file looks like this bank's format (check encoding, headers, etc.)
   - `parse(path, cards_path)` — Parse the file and return `list[dict]` with the standard fields (`date`, `description_raw`, `amount`, etc.)
   - `extract_date_range(path)` — Return `(start_date, end_date)` from the file
2. Register the parser in `expense_tracker/parsers/__init__.py` by importing and calling `register_parser(NewBankParser())`
3. Add tests in `tests/test_parsers.py` or a dedicated test file
4. Run `python bank_ingest.py banks` to verify it shows up

The parser will automatically work with `ingest` (auto-detected or via `--bank newbank`).

### Adding a new Data sheet column

1. Add the column key to `DATA_COLUMNS` in `constants.py`
2. Add the header label to `DATA_HEADERS` in `constants.py` (same index)
3. Add a `COL_*` constant if you need sync-back
4. Update `_build_data_sheet()` in `ods.py` (add a column style)
5. Update `fetch_all_transactions()` in `db.py` (add to SELECT)
6. If the column is stored in the DB, add it to `ensure_schema()` and `migrate_schema()`
7. If it needs sync-back, update `sync_from_ods()` in `ods.py`

### Adding a new analysis sheet

1. Create a `write_new_sheet(doc, transactions)` function in `ods_sheets.py`
2. Import it in `ods.py` and call it from `generate_ods()` inside the `if is_first_run:` block
3. Add it to the Intro sheet's instructions list in `build_intro_sheet()` in `ods_sheets.py`
4. Document it in both README files

### Adding a new CLI command

1. Create a `cmd_newcmd(args)` function in `cli.py`
2. Add a subparser in `main()`
3. Add tests if the command has non-trivial logic
4. Update the "All CLI Commands" table in both READMEs

---

## File Descriptions

| File | Lines | Purpose |
|------|-------|---------|
| `bank_ingest.py` | 16 | Entry point wrapper — imports and runs `cli.main()` |
| `expense_tracker/__init__.py` | 1 | Package marker |
| `expense_tracker/__main__.py` | 4 | Allows `python -m expense_tracker` |
| `expense_tracker/constants.py` | 83 | Default paths, column definitions, column indices |
| `expense_tracker/parsers/__init__.py` | ~120 | BankParser protocol, registry, auto-detection, unified parse_statement |
| `expense_tracker/parsers/utf16_csv.py` | ~60 | BankParser implementation for UTF-16 CSV (wraps parser.py) |
| `expense_tracker/parsers/utf8_csv.py` | ~180 | BankParser implementation for UTF-8 CSV |
| `expense_tracker/parser.py` | 380 | (legacy) UTF-16 CSV parser, description cleaning, card/payment detection |
| `expense_tracker/pdf_parser.py` | ~250 | PDF statement parser using pdfplumber |
| `expense_tracker/db.py` | 232 | SQLite schema, ingestion, dedup, reclean, queries |
| `expense_tracker/rules.py` | 194 | Rule/card CRUD, rule loading, categorization engine |
| `expense_tracker/ods.py` | ~280 | ODS orchestration (generate/update), sync-back, description notes |
| `expense_tracker/ods_sheets.py` | ~700 | ODS sheet builders: styles, cell helpers, 10 sheet generators |
| `expense_tracker/export.py` | 29 | Simple CSV export |
| `expense_tracker/backup.py` | 120 | Backup utilities — zip creation, monthly checks, size formatting |
| `expense_tracker/starter_rules.py` | ~120 | Curated Portuguese starter rules (~100 rules) + import logic |
| `expense_tracker/suggest.py` | ~300 | Pattern detection: recurring, similar, frequent merchants |
| `expense_tracker/email_fetch.py` | ~200 | Email statement fetcher (IMAP, attachment download) |
| `expense_tracker/gui.py` | ~850 | Streamlit web interface (7 pages: Dashboard, Transactions, Categorize, Rules, Import, Tools, Manual) |
| `expense_tracker/cli.py` | ~700 | argparse CLI with 13 subcommands |
| `Dockerfile` | ~30 | Docker image definition (Python 3.12 slim, deps, GUI entrypoint) |
| `docker-compose.yml` | ~40 | Orchestrates GUI + cron services with bind mounts |
| `cron/daily-sync.sh` | ~80 | Automated daily fetch + ingest + report generation script |
| `DEPLOYMENT.md` | ~540 | Docker & home server (NUC) deployment guide |
| `install.sh` | ~200 | First-time setup (venv, deps, directories, starter configs, supports --branch) |
| `run.sh` | ~20 | Everyday script — activates venv, launches GUI (or forwards CLI commands) |
| `update.sh` | ~120 | Merge code from the upstream dev repo + re-run install if needed |
| `pytest.ini` | 3 | pytest configuration |
| `tests/conftest.py` | 100 | Shared fixtures and synthetic UTF-16 CSV builder |
| `tests/test_parser.py` | 230 | Parser unit tests (39 tests) |
| `tests/test_db.py` | 200 | Database unit tests (19 tests) |
| `tests/test_rules.py` | 180 | Rules unit tests (17 tests) |
| `tests/test_export.py` | 50 | Export unit tests (4 tests) |
| `tests/test_backup.py` | 200 | Backup unit tests (16 tests) |
| `tests/test_pdf_report.py` | 280 | PDF report unit tests (33 tests) |
| `tests/test_integration.py` | 120 | End-to-end integration tests (4 tests) |
| `tests/test_starter_rules.py` | ~180 | Starter rules tests (14 tests) |
| `tests/test_suggest.py` | ~300 | Pattern detection tests (28 tests) |
| `tests/test_pdf_parser.py` | ~350 | PDF parser unit + integration tests (34 tests) |
| `tests/test_email_fetch.py` | ~280 | Email fetch tests (26 tests) |
| `tests/test_gui.py` | ~60 | GUI module tests (6 tests) |
| `tests/test_parsers.py` | ~350 | Multi-bank parser tests: registry, auto-detect, UTF-16 + UTF8 (29 tests) |

---

## Dependencies

| Package | Version | Why |
|---------|---------|-----|
| `odfpy` | >=1.4.1 | ODS file creation and reading (OpenDocument Spreadsheets) |
| `pdfplumber` | >=0.10 | PDF table extraction for bank statement parsing |
| `streamlit` | >=1.30 | Web interface framework for the GUI |
| `pandas` | >=2.0 | Data manipulation for the GUI (used by Streamlit) |
| `pytest` | >=7.0 | Test framework (dev dependency) |

Everything else is Python standard library: `sqlite3`, `csv`, `re`, `hashlib`, `pathlib`, `datetime`, `argparse`.
