# Expense Tracker

A personal expense tracking pipeline for **UTF-16 CSV** bank statements. It parses CSV exports from the bank, stores them in a local SQLite database, applies categorization rules, and generates an ODS spreadsheet report you can open in LibreOffice or Google Sheets.

> Built with [Cursor](https://cursor.com) + **Claude 4.6 Opus** (Anthropic). See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture details and developer documentation.

---

## Quick Start

```bash
# 1. First-time setup
./install.sh

# 2. Drop your bank CSV(s) into the raw/ folder

# 3. Import and generate the report
./run.sh

# 4. Open expense-report.ods in LibreOffice or Google Sheets
```

---

## Project Structure

```
expense-tracking/
├── raw/                    # Drop bank CSVs here (e.g. EXPORT_0_1022026.csv)
├── data/
│   ├── ledger.sqlite       # Source of truth — all transactions
│   ├── ledger.csv          # Optional CSV export
│   ├── rules.csv           # Categorization rules (editable)
│   ├── account-holders.csv # Card-to-owner mappings (editable)
│   ├── noise-words.txt     # Words to strip from descriptions (editable)
│   ├── cleaning-patterns.csv # Regex patterns for description cleaning
│   └── description-notes.csv # Merchant notes (editable, per-merchant)
├── expense-report.ods      # Generated report (open in LibreOffice)
├── bank_ingest.py          # Main entry point (convenience wrapper)
├── expense_tracker/        # Python package (the actual code)
│   ├── cli.py              # Command-line interface
│   ├── parser.py           # UTF-16 CSV parser + auto-rename
│   ├── db.py               # SQLite storage and queries
│   ├── rules.py            # Rule & card management + categorization
│   ├── ods.py              # ODS report orchestration + sync-back
│   ├── ods_sheets.py       # ODS sheet builders (styles, cells, all sheets)
│   ├── export.py           # CSV export
│   ├── backup.py           # Backup utilities (zip archives)
│   └── constants.py        # Shared configuration
├── backups/                # Backup archives (auto + manual)
├── tests/                  # Test suite (115 tests)
│   ├── conftest.py         # Shared fixtures + synthetic CSV builder
│   ├── test_parser.py      # Parser tests (39)
│   ├── test_db.py          # Database tests (19)
│   ├── test_rules.py       # Rules tests (17)
│   ├── test_export.py      # Export tests (4)
│   ├── test_backup.py      # Backup tests (16)
│   └── test_integration.py # End-to-end tests (4)
├── install.sh              # First-time setup script
├── run.sh                  # Import new data + regenerate report
├── update.sh               # Pull latest code from GitHub
├── requirements.txt        # Python dependencies
├── pytest.ini              # Test configuration
└── CONTRIBUTING.md         # Developer docs (architecture, decisions, extending)
```

---

## Shell Scripts

### `install.sh` — First-Time Setup

Run this once when you first clone the project (or on a new computer).

```bash
./install.sh
```

What it does:
1. Checks that Python 3.9+ is installed
2. Creates a virtual environment (`.venv/`)
3. Installs Python dependencies (`odfpy`)
4. Creates the `raw/`, `data/`, and `backups/` directories
5. Creates starter config files (`rules.csv`, `account-holders.csv`, etc.) in `data/` if they don't exist
6. Makes the shell scripts executable

### `run.sh` — Import Data & Generate Report

The everyday script. Your wife can double-click this (or run it from Terminal).

```bash
./run.sh
```

What it does:
1. Scans `raw/` for any new CSV files not yet imported
2. Auto-renames them (e.g. `EXPORT_0_1022026.csv` → `2026-01.csv`)
3. Imports new transactions into the SQLite database
4. Syncs any manual category edits from the ODS back into the database
5. Applies categorization rules from `data/rules.csv`
6. Regenerates the `expense-report.ods` report

### `update.sh` — Pull Latest Code from GitHub

Run this occasionally to get the latest improvements.

```bash
./update.sh
```

What it does:
1. Fetches changes from the GitHub remote
2. If there are updates, pulls them safely (fast-forward only)
3. Re-runs `install.sh` if dependencies changed
4. If already up to date, does nothing

---

## Python CLI Reference

The shell scripts use `bank_ingest.py` under the hood. You can also use it directly for more control.

**Global flags** (work with any subcommand):

| Flag | Effect |
|------|--------|
| `-q` / `--quiet` | Suppress all output except errors |
| `-v` / `--verbose` | Show detailed progress information |

### `auto` (default)

Scan for new files, import, and regenerate the report. This is what `run.sh` calls.

```bash
python bank_ingest.py              # same as 'auto'
python bank_ingest.py auto
python bank_ingest.py auto --raw path/to/csvs
python bank_ingest.py auto --no-backup    # skip automatic monthly backup
python bank_ingest.py auto --dry-run      # show what would happen, change nothing
```

The `auto` command automatically creates a monthly backup (e.g. `backups/backup-2026-01.zip`) for the previous month if one doesn't already exist. Use `--no-backup` to skip this.

Use `--dry-run` to preview what would happen without making any changes.

### `ingest`

Manually import specific CSV files.

```bash
python bank_ingest.py ingest raw/EXPORT_0_1022026.csv
python bank_ingest.py ingest raw/*.csv
python bank_ingest.py ingest --no-rename raw/some-file.csv   # skip auto-rename
python bank_ingest.py ingest --dry-run raw/EXPORT_0_1022026.csv  # preview only
```

The `ingest` command auto-renames files based on the date range inside the CSV (e.g. `EXPORT_0_1022026.csv` → `2026-01.csv`). Use `--no-rename` to skip this.

### `report`

Apply rules and regenerate the ODS report (without importing new data).

```bash
python bank_ingest.py report
python bank_ingest.py report --fresh       # regenerate ALL sheets from scratch
python bank_ingest.py report --no-sync     # skip syncing manual edits from ODS
python bank_ingest.py report --fresh --no-sync   # full nuclear reset
```

| Flag | Effect |
|------|--------|
| `--fresh` | Creates a backup, then deletes the existing ODS and regenerates all sheets (including Dashboard, Monthly Summary/Trend, Category/Subcategory Breakdown, and any custom sheets). Manual edits are still saved to the database first. |
| `--no-sync` | Skips reading manual edits from the ODS back into the database. |

### `cards`

Manage card-holder mappings (which debit card belongs to whom).

```bash
python bank_ingest.py cards                          # list all card holders
python bank_ingest.py cards add 1234 Alice           # add a card
python bank_ingest.py cards add 5678 Bob             # add another
python bank_ingest.py cards remove 1234              # remove a card
```

Card mappings are stored in `data/account-holders.csv` and are used to populate the "Who" column when transactions are imported. See the [Account Holders](#account-holders-dataaccount-holderscsv) section below.

### `rules`

Manage categorization rules from the command line.

```bash
python bank_ingest.py rules                                          # list all rules
python bank_ingest.py rules add CONTINENTE Groceries                 # add a simple rule
python bank_ingest.py rules add FARMACIA Health --sub Pharmacy --payment card  # with subcategory
python bank_ingest.py rules add "TRF P/ Renda" Housing --sub Rent --field description_raw
python bank_ingest.py rules remove CONTINENTE                        # remove a rule by pattern
```

| Flag | Default | Description |
|------|---------|-------------|
| `--field` | `description` | Field to match against: `description` (cleaned) or `description_raw` |
| `--sub` | *(empty)* | Subcategory |
| `--payment` | *(empty)* | Payment type: `card`, `transfer`, `direct_debit`, `fee`, `tax` |

Rules are appended to the end of `data/rules.csv`. See the [Categorization Rules](#categorization-rules-datarulescsv) section below.

### `export`

Export all transactions to a clean UTF-8 CSV file.

```bash
python bank_ingest.py export
python bank_ingest.py export --out my-data.csv
```

### `reclean`

Recompute all cleaned descriptions from the raw bank text using the current cleaning patterns. Use this after editing `data/noise-words.txt` or `data/cleaning-patterns.csv`.

```bash
python bank_ingest.py reclean
```

What it does:
1. Creates a backup (in case the reclean changes something you didn't expect)
2. Re-runs the description cleaner on every transaction in the database
3. Updates only the rows where the cleaned description changed
4. Regenerates the ODS report

All categories, notes, and other data are preserved.

### `backup`

Create a zip archive of all your data (database, config files, raw CSVs, and the ODS report).

```bash
python bank_ingest.py backup                   # save to backups/ (default)
python bank_ingest.py backup --out ~/Desktop    # save to a custom location
```

The zip file is named with a timestamp (e.g. `backup-2026-02-11T14-30-00.zip`) and contains:
- `data/` — SQLite database, rules, account holders, cleaning patterns, merchant notes
- `raw/` — original bank CSV files
- `expense-report.ods` — the generated report

---

## Merchant Notes (`data/description-notes.csv`)

Merchant notes are annotations that apply to **all transactions** with the same cleaned description. Unlike per-transaction notes (the "Notes" column), a merchant note is shared across every occurrence of that merchant.

### How it works

1. In the ODS Data sheet, find the **Merchant Note** column (S)
2. Type a note on any row (e.g. "Weekly groceries" next to a Continente transaction)
3. Run `./run.sh` — the note is saved to `data/description-notes.csv`
4. On the next regeneration, **every** row with that same merchant description gets the note

### Format

```csv
description_clean,merchant_note
FARMACIA DA GARE,Pharmacy
CONTINENTE,Main weekly grocery shop
```

You can edit this file directly or let it be populated from the ODS.

---

## Account Holders (`data/account-holders.csv`)

This file maps debit card last-4 digits to owner names, so the "Who" column in the report shows who made each purchase.

### Format

```csv
card_last4,name
1234,Alice
5678,Bob
```

You can edit this file directly or use the CLI:

```bash
python bank_ingest.py cards add 1234 Alice
python bank_ingest.py cards remove 1234
python bank_ingest.py cards               # list all
```

Transactions made with a card not in this file will show "Joint" as the owner.

---

## The ODS Report (`expense-report.ods`)

Open this file in **LibreOffice Calc** or upload it to **Google Sheets**.

### Sheets

| Sheet | Regenerated? | Description |
|-------|-------------|-------------|
| **Intro** | Yes, always | Overview of the file and what each sheet does |
| **Data** | Yes, always | All transactions — one row per transaction. You can manually edit the **Category**, **Subcategory**, **Notes**, and **Merchant Note** columns; edits are saved back on the next run. |
| **Rules** | Yes, always | A read-only view of the current categorization rules |
| **Dashboard** | First run only | Key summary figures: total income/expenses, net balance, average monthly spend, uncategorized count, top categories, top 10 merchants, and uncategorized preview. |
| **Monthly Summary** | First run only | Spending by month and category in a grid with formulas. |
| **Monthly Trend** | First run only | Income vs expenses vs net per month, with a running balance column. |
| **Category Breakdown** | First run only | Total spent, % of total, avg/month, and transaction count per category. |
| **Subcategory Breakdown** | First run only | Detailed breakdown within each category (e.g. "Eating Out / Restaurant" vs "Eating Out / Cafe"). |
| **Tags** | First run only | Track spending by #tags used in the Notes or Merchant Note columns. Pre-filled with example tags and empty slots — just type a tag and the formulas do the rest. |
| **Recurring Merchants** | First run only | Merchants that appear 3+ times — helps spot subscriptions, regular bills, and habitual spending. Shows count, total, average, date range, and category. |
| *Your custom sheets* | Never touched | Add as many sheets as you want. The script will never modify or remove them. |

### Manual Categorization & Notes

If a transaction isn't caught by the rules (shows as "uncategorized"), you can:

1. **Add a rule** — edit `data/rules.csv` (or use `python bank_ingest.py rules add ...`) to catch similar transactions automatically in the future
2. **Edit directly in the ODS** — change the Category/Subcategory columns in the Data sheet. The next time you run `./run.sh`, those edits are synced back to the database.

You can also use the **Notes** column (R) to add personal annotations to any transaction (e.g. "birthday dinner", "reimbursed by insurance"). Notes are synced back to the database just like categories.

The **Merchant Note** column (S) works differently: a note here applies to **all** transactions with the same merchant. Write it once, see it everywhere. See the [Merchant Notes](#merchant-notes-description-notescsv) section for details.

For recurring patterns, adding a rule is better. For one-off expenses, editing in the ODS is fine.

### Using #Tags

You can add **#tags** anywhere in the **Notes** (R) or **Merchant Note** (S) columns to label transactions for tracking. Tags are just words starting with `#` — no special setup required.

#### Examples

| Column | Value | Effect |
|--------|-------|--------|
| Notes (R) | `Birthday dinner #gift #shared` | Tags this single transaction |
| Merchant Note (S) | `#recurring #essential` | Tags **all** transactions from this merchant |

Some useful tag ideas: `#recurring`, `#reimbursable`, `#gift`, `#shared`, `#splurge`, `#essential`, `#one-off`

#### The Tags sheet

The starter **Tags** sheet (generated on first run) automatically counts and sums tagged transactions. It comes with a few example tags — just replace them with your own or add more in the empty rows below.

| Column | What it shows |
|--------|---------------|
| Tag | The #tag to search for (e.g. `#recurring`) |
| # Transactions | Number of transactions containing the tag (in either Notes or Merchant Note) |
| Total Spent | Sum of amounts for tagged outgoing transactions |
| % of Total Spend | What percentage of your total spending this tag represents |

You can also build your own tag formulas in custom sheets. For example, to count all transactions tagged `#gift`:

```
=COUNTIF(Data.R:R, "*#gift*") + COUNTIF(Data.S:S, "*#gift*")
```

Or to sum the amounts of tagged outgoing transactions:

```
=SUMPRODUCT((ISNUMBER(SEARCH("#gift", Data.R2:R1000))+ISNUMBER(SEARCH("#gift", Data.S2:S1000))>0)*(Data.G2:G1000="out")*Data.F2:F1000)
```

---

## Categorization Rules (`data/rules.csv`)

Rules are defined in a simple CSV file that anyone can edit in a spreadsheet or text editor.

### Format

```csv
pattern,match_field,category,subcategory,payment_type
CONTINENTE,description,Groceries,,card
FARMACIA,description,Health,Pharmacy,card
TRF P/ Renda,description,Housing,Rent,transfer
```

### Columns

| Column | Required | Description |
|--------|----------|-------------|
| `pattern` | Yes | Text to search for (case-insensitive) |
| `match_field` | Yes | Which field to search: `description` (cleaned) or `description_raw` (original) |
| `category` | Yes | Main category (e.g. Groceries, Health, Transport) |
| `subcategory` | No | Optional sub-category (e.g. Pharmacy, Rent, Fast Food) |
| `payment_type` | No | Optional override: `card`, `transfer`, `direct_debit`, `fee`, `tax` |

### How Rules Work

- Rules are applied **in order** — the first matching rule wins
- Matching is **case-insensitive** and uses substring matching
- Rules only apply to **uncategorized** transactions (they won't override manual edits or previous rules)
- After adding new rules, run `./run.sh` to apply them

---

## Cleaning Patterns

When bank descriptions are imported, the parser cleans them up by stripping prefixes, noise words, and reference codes. These patterns are loaded from two external files so you can tweak them without touching Python code.

### `data/noise-words.txt` — Simple word list

A plain text file with one word per line. These words are stripped from descriptions when they appear as standalone words (surrounded by spaces). Case-insensitive.

```
CONTACTLESS
PT
Portugal
PORTO
LISBOA
```

- Add city names if you move to a different area
- Remove words if they're being stripped from merchant names you want to keep
- Lines starting with `#` are comments

### `data/cleaning-patterns.csv` — Regex patterns (advanced)

A CSV file for power users who need finer control over how descriptions are cleaned. Each row has a type, a regex pattern, and a description.

| Column | Description |
|--------|-------------|
| `type` | `prefix` (stripped from the start) or `noise` (stripped anywhere) |
| `pattern` | A regular expression (compiled with case-insensitive flag) |
| `description` | Human-readable explanation of what the pattern does |

**Important:** If your regex pattern contains commas (e.g. `{8,12}`), wrap the entire row in quotes.

You normally won't need to edit this file unless you're seeing unexpected cleaning results.

---

## Backups

The system creates backups automatically and on demand. Backups are zip archives stored in `backups/` (added to `.gitignore`).

### Automatic backups

Every time you run `./run.sh` (or `python bank_ingest.py auto`), the system checks if a backup for the **previous month** exists. If not, it creates one automatically (e.g. `backups/backup-2026-01.zip` when running in February). This happens silently if a backup already exists.

Use `--no-backup` to skip this check.

### Pre-destructive backups

Before potentially destructive operations, a timestamped backup is created automatically:
- `report --fresh` — backs up before deleting and regenerating the ODS
- `reclean` — backs up before modifying descriptions in the database

### Manual backups

```bash
python bank_ingest.py backup                   # save to backups/
python bank_ingest.py backup --out ~/Desktop    # save somewhere else
```

### What's included

Each backup zip contains all personal data:
- `data/` — SQLite database, rules, account holders, cleaning patterns, merchant notes, CSV export
- `raw/` — original bank CSV files
- `expense-report.ods` — the generated report

---

## Data Architecture

```
Bank CSV (raw/)  →  Python parser  →  SQLite (data/ledger.sqlite)  →  ODS report
                         ↑                    ↑                            |
              data/account-holders.csv   data/rules.csv                    |
                                              ↑                            |
                                              └── manual edits synced back ┘
```

- **Raw CSVs** (`raw/`) — immutable bank exports, kept as-is for reference
- **Account holders** (`data/account-holders.csv`) — maps card last-4 digits to owner names
- **Rules** (`data/rules.csv`) — defines how transactions get categorized
- **SQLite database** (`data/ledger.sqlite`) — the single source of truth for all transactions, deduplicated and normalized
- **ODS report** (`expense-report.ods`) — a generated spreadsheet for viewing, analysis, and manual edits

The Python script handles all the "intelligence" (parsing, cleaning, deduplication, categorization). The spreadsheet is purely for presentation and custom analysis.

---

## Requirements

- **Python 3.9+**
- **macOS or Linux** (the shell scripts use bash)
- **LibreOffice** or **Google Sheets** (to view the ODS report)

---

## All CLI Commands at a Glance

| Command | Description |
|---------|-------------|
| `python bank_ingest.py` | Auto-detect, import, and report (default) |
| `python bank_ingest.py auto` | Same as above |
| `python bank_ingest.py ingest <files>` | Import specific CSV files |
| `python bank_ingest.py report` | Regenerate the ODS report |
| `python bank_ingest.py export` | Export to UTF-8 CSV |
| `python bank_ingest.py cards` | List card holders |
| `python bank_ingest.py cards add <last4> <name>` | Add a card holder |
| `python bank_ingest.py cards remove <last4>` | Remove a card holder |
| `python bank_ingest.py rules` | List categorization rules |
| `python bank_ingest.py rules add <pattern> <category>` | Add a rule |
| `python bank_ingest.py rules remove <pattern>` | Remove a rule |
| `python bank_ingest.py reclean` | Re-clean all descriptions and regenerate report |
| `python bank_ingest.py backup` | Create a zip backup of all data |

---

## Development

For architecture details, design decisions, module reference, and how to extend the project, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

### Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

115 tests covering the parser, database, rules engine, export, backup, and end-to-end workflows. Runs in under a second using synthetic UTF-16 CSV fixtures (no real bank data needed).

---

## Privacy & Git

This project includes a `.gitignore` that keeps all personal/financial data out of version control:

| Ignored | Why |
|---------|-----|
| `raw/` | Raw bank statement CSVs — contain account numbers and transaction details |
| `data/` | SQLite database, config files, and CSV exports — contain all your transactions and personal data (rules, card mappings, merchant notes, cleaning patterns) |
| `expense-report.ods` | The generated report — contains all your financial data |
| `backups/` | Zip archives of all the above — also contain financial data |

**Only code and documentation are committed.** When you first clone the repo on a new machine, run `./install.sh` to recreate the directory structure and starter config files.

The system creates automatic and manual backups in `backups/` (see the [Backups](#backups) section). For offsite backup, copy the `backups/` folder to a private cloud folder or encrypted archive — **never commit data files to Git**.

---

## Troubleshooting

**"No new files to ingest"** — All CSV files in `raw/` have already been imported. Drop a new one and run again.

**"Could not find date range"** — The CSV file doesn't have the expected UTF-16 header format. Use `ingest --no-rename` to import it with its original filename.

**"File already exists with range..."** — You're trying to import a CSV for a month that's already been imported with the same or wider date range. If you need to re-import, delete the existing file from `raw/` first.

**Formulas show `#VALUE!`** — Make sure you're opening the file in LibreOffice Calc (not a text editor). If the issue persists, run `python bank_ingest.py report --fresh` to regenerate all sheets.

**"externally-managed-environment" error** — You're trying to install packages on system Python. Run `./install.sh` to set up a virtual environment instead.
