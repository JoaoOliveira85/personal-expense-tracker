# Expense Tracker

A personal expense tracking pipeline for Portuguese bank statements. It parses CSV exports from supported banks (currently **UTF-16 CSV** and **UTF-8 CSV**), stores them in a local SQLite database, applies categorization rules, and generates an ODS spreadsheet report you can open in LibreOffice or Google Sheets. New banks can be added via a plugin architecture.

> Built with [Cursor](https://cursor.com) + **Claude 4.6 Opus** (Anthropic). See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture details and developer documentation.

---

## Quick Start

```bash
# 1. First-time setup (optionally specify a branch with --branch <name>)
./install.sh

# 2. Launch the GUI
./run.sh

# 3. Use the Import page to upload bank CSV/PDF files, or drop them in raw/

# 4. View your expenses in the Dashboard, categorize with one click, and
#    download the ODS or PDF reports from the Tools page
```

Or if you prefer the CLI workflow:

```bash
# Drop your bank CSV(s) or PDF(s) into raw/, then:
./run.sh auto

# Open expense-report.ods in LibreOffice or Google Sheets
```

---

## Project Structure

```
expense-tracking/
├── raw/                    # Drop bank CSVs/PDFs here (e.g. EXPORT_0_1022026.csv)
├── data/
│   ├── ledger.sqlite       # Source of truth — all transactions
│   ├── ledger.csv          # Optional CSV export
│   ├── email-config.json   # Email fetch settings (gitignored, created via --setup)
│   ├── rules.csv           # Categorization rules (editable)
│   ├── account-holders.csv # Card-to-owner mappings (editable)
│   ├── noise-words.txt     # Words to strip from descriptions (editable)
│   ├── cleaning-patterns.csv # Regex patterns for description cleaning
│   └── description-notes.csv # Merchant notes (editable, per-merchant)
├── reports/                # Monthly PDF summaries
├── expense-report.ods      # Generated report (open in LibreOffice)
├── bank_ingest.py          # Main entry point (convenience wrapper)
├── expense_tracker/        # Python package (the actual code)
│   ├── cli.py              # Command-line interface
│   ├── parser.py           # UTF-16 CSV parser + auto-rename (legacy, still used internally)
│   ├── pdf_parser.py       # PDF statement parser
│   ├── parsers/            # Multi-bank parser framework
│   │   ├── __init__.py     # BankParser protocol, registry, auto-detection
│   │   ├── utf16_csv.py # UTF-16 CSV parser
│   │   └── utf8.py          # UTF-8 CSV parser
│   ├── db.py               # SQLite storage and queries
│   ├── rules.py            # Rule & card management + categorization
│   ├── ods.py              # ODS report orchestration + sync-back
│   ├── ods_sheets.py       # ODS sheet builders (styles, cells, all sheets)
│   ├── export.py           # CSV export
│   ├── backup.py           # Backup utilities (zip archives)
│   ├── pdf_report.py       # Monthly PDF report generator
│   ├── starter_rules.py    # Portuguese starter rules pack (~100 curated rules)
│   ├── suggest.py          # Automatic category pattern detection
│   ├── email_fetch.py      # Email statement fetcher (IMAP)
│   ├── gui.py              # Streamlit web interface
│   └── constants.py        # Shared configuration
├── backups/                # Backup archives (auto + manual)
├── cron/                   # Automation scripts for Docker/server deployment
│   └── daily-sync.sh       # Scheduled fetch + ingest + report generation
├── tests/                  # Test suite (285 tests)
│   ├── conftest.py         # Shared fixtures + synthetic CSV builder
│   ├── test_parser.py      # Parser tests (39)
│   ├── test_db.py          # Database tests (19)
│   ├── test_rules.py       # Rules tests (17)
│   ├── test_export.py      # Export tests (4)
│   ├── test_backup.py      # Backup tests (16)
│   ├── test_pdf_report.py  # PDF report tests (26)
│   ├── test_integration.py # End-to-end tests (4)
│   ├── test_starter_rules.py # Starter rules tests (14)
│   ├── test_suggest.py     # Pattern detection tests (28)
│   ├── test_pdf_parser.py  # PDF parser tests (34)
│   ├── test_email_fetch.py # Email fetch tests (26)
│   ├── test_gui.py         # GUI module tests (6)
│   └── test_parsers.py     # Multi-bank parser tests (29)
├── install.sh              # First-time setup script (supports --branch)
├── run.sh                  # Launch the GUI (or pass CLI commands)
├── update.sh               # Pull latest code from GitHub
├── Dockerfile              # Container image for deployment
├── docker-compose.yml      # Orchestrates GUI + cron services
├── DEPLOYMENT.md           # Docker & home server deployment guide
├── requirements.txt        # Python dependencies
├── pytest.ini              # Test configuration
└── CONTRIBUTING.md         # Developer docs (architecture, decisions, extending)
```

---

## Shell Scripts

### `install.sh` — First-Time Setup

Run this once when you first clone the project (or on a new computer).

```bash
./install.sh                    # install from the current branch
./install.sh --branch main      # checkout a specific branch first
./install.sh -b integration     # short form
```

| Flag | Description |
|------|-------------|
| `-b` / `--branch` | Git branch to checkout before installing (e.g. `main`, `integration`) |

What it does:
1. Optionally checks out the specified git branch (if `--branch` is given)
2. Checks that Python 3.9+ is installed
3. Creates a virtual environment (`.venv/`)
4. Installs Python dependencies (`odfpy`)
5. Creates the `raw/`, `data/`, and `backups/` directories
6. Creates starter config files (`rules.csv`, `account-holders.csv`, etc.) in `data/` if they don't exist
7. Makes the shell scripts executable

### `run.sh` — Launch the GUI

The everyday script. Launches the web-based GUI in your browser. You can also pass CLI commands directly.

```bash
./run.sh                # launch the GUI (default)
./run.sh auto           # run the CLI auto command (ingest + report)
./run.sh pdf            # generate a PDF report
./run.sh <any-command>  # pass any command to bank_ingest.py
```

By default (no arguments), it launches the Streamlit GUI at `http://localhost:8501`. The GUI provides:
- Dashboard with spending charts and summaries
- Transaction browser with filters
- Bulk categorization by merchant
- Rule management
- File import via drag-and-drop
- Tools: email setup, fetch from email, sync/regenerate, download reports, backup, export, and more

If you pass arguments, they're forwarded to `bank_ingest.py` (e.g. `./run.sh auto` runs the auto ingest pipeline).

### `update.sh` — Pull Latest Code from GitHub

Run this occasionally to get the latest improvements.

```bash
./update.sh
```

Code is developed in [`personal-expense-tracker`](https://github.com/JoaoOliveira85/personal-expense-tracker), a repository that never contains personal data. Your own clone can point `origin` at a separate private repo where you commit your data; `update.sh` pulls code from the development repo regardless.

What it does:
1. Finds the remote pointing at the development repo, or adds one named `upstream`
2. Fetches its `main` branch and merges it into your current branch (fast-forward when possible, otherwise a merge commit that keeps your personal commits)
3. Aborts cleanly on a merge conflict, leaving your clone unchanged
4. Re-runs `install.sh` if dependencies changed
5. If already up to date, does nothing

| Variable | Default | Description |
|----------|---------|-------------|
| `EXPENSE_TRACKER_UPSTREAM` | `git@github.com:JoaoOliveira85/personal-expense-tracker.git` | Development repo URL |
| `EXPENSE_TRACKER_BRANCH` | `main` | Branch to follow |

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

Manually import specific CSV or PDF files. The bank format is auto-detected for CSVs, or you can specify it explicitly.

```bash
python bank_ingest.py ingest raw/EXPORT_0_1022026.csv
python bank_ingest.py ingest raw/statement-jan.pdf           # PDF statements supported too
python bank_ingest.py ingest raw/*.csv raw/*.pdf
python bank_ingest.py ingest --bank utf8 raw/export-b.csv   # force UTF8 parser
python bank_ingest.py ingest --no-rename raw/some-file.csv   # skip auto-rename
python bank_ingest.py ingest --dry-run raw/EXPORT_0_1022026.csv  # preview only
```

| Flag | Default | Description |
|------|---------|-------------|
| `--bank` | *(auto-detect)* | Bank format to use: `utf16`, `utf8`, etc. Run `banks` to see all options. |
| `--no-rename` | — | Skip auto-renaming files based on date range |
| `--dry-run` | — | Preview what would happen, change nothing |

The `ingest` command auto-renames files based on the date range inside the CSV or PDF (e.g. `EXPORT_0_1022026.csv` → `2026-01.csv`). Use `--no-rename` to skip this.

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
python bank_ingest.py rules import-starter                           # import Portuguese starter rules
python bank_ingest.py rules import-starter --dry-run                 # preview what would be imported
```

#### Portuguese Starter Rules

The project ships with a curated pack of ~100 rules for common Portuguese merchants and services — supermarkets, utilities, telecoms, fuel, transport, health, insurance, eating out, subscriptions, shopping, taxes, bank fees, and more.

```bash
python bank_ingest.py rules import-starter
```

Rules are appended to your existing `data/rules.csv`. Any patterns that already exist (case-insensitive) are skipped, so it's safe to run multiple times. Use `--dry-run` to preview what would be added.

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

### `pdf`

Generate a single-page PDF summary for a given month — a quick bird's-eye view of where money went.

```bash
python bank_ingest.py pdf                      # previous month (default)
python bank_ingest.py pdf --month 2026-01      # specific month
python bank_ingest.py pdf --out ~/Desktop/jan.pdf  # custom output path
```

The PDF includes:
- **Summary**: total income, expenses, and net balance
- **Spending by category**: amounts, percentages, variation vs previous month, and variation vs category average
- **Top 10 merchants**: who you spent the most with
- **#Tag summary**: totals for any tags used in Notes or Merchant Notes
- **Essential spending**: quick totals for Housing, Utilities, Subscriptions, Insurance, Health, Childcare, and Transport
- **Uncategorized alert**: how many transactions still need categorizing

Output defaults to `reports/report-YYYY-MM.pdf`.

### `suggest`

Analyze your transaction data to find patterns and suggest categorization rules. The system looks for three types of patterns:

1. **Recurring merchants** — merchants that appear in 3+ different months (likely subscriptions or regular bills)
2. **Similar merchants** — groups of descriptions that look like the same merchant (e.g. "CONTINENTE PORTO" and "CONTINENTE LISBOA")
3. **Frequent merchants** — uncategorized merchants that appear 5+ times (good candidates for new rules)

```bash
python bank_ingest.py suggest                        # analyze with default thresholds
python bank_ingest.py suggest --min-months 2         # lower recurring threshold
python bank_ingest.py suggest --similarity 0.7       # stricter text similarity
python bank_ingest.py suggest --min-count 3          # lower frequency threshold
python bank_ingest.py suggest --accept recurring:0   # accept a suggestion as a new rule
```

| Flag | Default | Description |
|------|---------|-------------|
| `--db` | `data/ledger.sqlite` | Database path |
| `--min-months` | `3` | Minimum distinct months for recurring detection |
| `--similarity` | `0.65` | Text similarity threshold (0-1) for grouping |
| `--min-count` | `5` | Minimum transaction count for frequent merchants |
| `--accept` | — | Accept a suggestion by type:index (e.g. `recurring:0`) |

The output groups suggestions by type and shows transaction counts, total amounts, and recommended categories. You can then add the suggested rules manually or use `--accept` to create them automatically.

### `fetch`

Download bank statements from email via IMAP. Your bank likely sends monthly statement PDFs by email — this command fetches them automatically.

```bash
python bank_ingest.py fetch --setup          # interactive setup wizard (first time)
python bank_ingest.py fetch                  # fetch new statements and auto-ingest
python bank_ingest.py fetch --days 90        # look back 90 days (default: 60)
python bank_ingest.py fetch --no-ingest      # download only, don't import to DB
python bank_ingest.py fetch --dry-run        # preview what would be downloaded
```

| Flag | Default | Description |
|------|---------|-------------|
| `--setup` | — | Run interactive configuration wizard |
| `--config` | `data/email-config.json` | Path to email config file |
| `--raw` | `raw/` | Directory to save downloaded attachments |
| `--db` | `data/ledger.sqlite` | Database for auto-ingestion |
| `--days` | `60` | How many days back to search |
| `--no-ingest` | — | Download files but don't import them |
| `--dry-run` | — | Preview only, don't download anything |

#### Setup

Run `fetch --setup` to create `data/email-config.json` interactively. You'll need:
- Your email server (IMAP host, e.g. `imap.gmail.com`)
- Your email address and password (or app-specific password)
- The sender address your bank uses (pre-filled with UTF-16 CSV's default)

The config file is gitignored. For Gmail, you'll need an [App Password](https://myaccount.google.com/apppasswords).

### `gui`

Launch a web-based graphical interface for everyday operations. The GUI complements the CLI — it's especially useful for browsing transactions, bulk categorization, viewing charts, and performing common tasks without touching the terminal.

```bash
python bank_ingest.py gui                    # launch on default port 8501
python bank_ingest.py gui --port 8080        # use a custom port
./run.sh                                     # same as above (default behavior)
```

The GUI opens in your browser and provides seven pages:

| Page | What it does |
|------|-------------|
| **Dashboard** | Overview with spending charts, category breakdown, monthly trends |
| **Transactions** | Filterable/searchable transaction table with date range, category, and amount filters |
| **Categorize** | Bulk categorization by merchant — select a category for all transactions from the same merchant, optionally create a rule |
| **Rules** | View, add, and remove categorization rules |
| **Import** | Upload CSV/PDF files directly through the browser and ingest them |
| **Tools** | Email configuration, fetch from email, sync ODS, download reports (ODS & PDF), create backups, export CSV, import starter rules |
| **Manual** | Full project documentation (User Guide, Deployment Guide, Developer Guide) with section search |

#### Tools Page

The **Tools** page provides a one-stop shop for common operations:

| Tool | Description |
|------|-------------|
| **Email Configuration** | Set up or update IMAP settings for automatic statement fetching (host, email, app password, folder) |
| **Fetch from Email** | Connect to your email and download new bank statement attachments, with optional auto-ingest |
| **Sync & Regenerate** | Sync manual ODS edits back to the database, re-apply rules, and regenerate the report |
| **Download Reports** | Download the ODS spreadsheet and monthly PDF reports directly from the browser |
| **Generate PDF** | Generate a PDF summary for the previous month |
| **Backup** | Create a zip archive of all data with one click |
| **Export Data** | Export all transactions as a UTF-8 CSV and download it |
| **Starter Rules** | Import ~100 curated Portuguese categorization rules |

The GUI uses [Streamlit](https://streamlit.io/) and reads/writes to the same SQLite database as the CLI. Changes made in the GUI are immediately visible in the CLI and vice versa.

### `banks`

List all supported bank statement formats and their parser IDs.

```bash
python bank_ingest.py banks
```

Example output:

```
Supported bank formats:
  utf16  — UTF-16 CSV
  utf8  — UTF-8 CSV
```

Use the parser ID with `ingest --bank <id>` to force a specific parser, or omit `--bank` to let the system auto-detect the format.

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

**Refunds.** Money coming *in* with a spending category (e.g. a pharmacy refund or an insurance reimbursement categorized as Health) is treated as a refund: it is subtracted from that category's spending and from total expenses, and is **not** counted as income. Incoming transactions categorized `Income` or `Transfers`, or left uncategorized, count as income. This applies to every report (ODS, xlsx, PDF, GUI dashboard, advisor). Because the analysis sheets above are only written on the first run, regenerate an existing ODS with `python bank_ingest.py report --fresh` to pick up the refund-aware formulas.

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

- When several rules match, the **longest pattern wins** (so `UBER EATS` beats `UBER` wherever each sits in the file); equal lengths fall back to file order
- Matching is **case-insensitive** and a pattern must start at the beginning of a word: `PAO` matches "PAO QUENTE" and "PAOZINHO" but not "JAPAO"
- End a pattern with a space (write it quoted, e.g. `"BP "`) to also require it to end on a word boundary: `"BP "` matches "BP" but not "BPI"
- Rules **never override manual edits** (categories you change in the ODS report). Categories set by rules are re-checked on every run, so editing or deleting a rule fixes the transactions it had categorized; a transaction no rule matches any more goes back to uncategorized
- Clearing a category in the ODS hands that transaction back to the rules
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
Bank CSV/PDF (raw/)  →  Auto-detect bank  →  Bank parser  →  SQLite (data/ledger.sqlite)  →  ODS report
                         ↑                    ↑                            |
              data/account-holders.csv   data/rules.csv                    |
                                              ↑                            |
                                              └── manual edits synced back ┘
```

- **Raw files** (`raw/`) — immutable bank exports (CSV or PDF), kept as-is for reference. Auto-detection identifies the bank format when parsing CSV files.
- **Account holders** (`data/account-holders.csv`) — maps card last-4 digits to owner names
- **Rules** (`data/rules.csv`) — defines how transactions get categorized
- **SQLite database** (`data/ledger.sqlite`) — the single source of truth for all transactions, deduplicated and normalized
- **ODS report** (`expense-report.ods`) — a generated spreadsheet for viewing, analysis, and manual edits

The Python script handles all the "intelligence" (parsing, cleaning, deduplication, categorization). The spreadsheet is purely for presentation and custom analysis.

---

## Docker & Server Deployment

The expense tracker can run as a containerized service on a home server (NUC, Raspberry Pi, etc.) with:

- **Always-on GUI** — accessible from any device on your local network at `http://<server-ip>:8501`
- **Scheduled daily sync** — fetches statements from email, ingests, categorizes, and regenerates reports automatically
- **Shared reports** — ODS and PDF synced to a cloud folder (iCloud, Dropbox, Google Drive, Syncthing, etc.)

### Quick Start with Docker

```bash
# 1. Clone and set up
git clone -b integration https://github.com/<your-user>/personal-expense-tracker.git
cd personal-expense-tracker
./install.sh

# 2. (Optional) Configure email for automated fetching
source .venv/bin/activate && python bank_ingest.py fetch --setup

# 3. Create the ODS file (Docker needs a file, not a directory)
touch expense-report.ods

# 4. Build and start
docker compose up -d --build
```

The setup uses two containers:
- **gui** — the Streamlit web interface (always running, port 8501)
- **cron** — daily sync that fetches email, ingests, categorizes, and regenerates reports

All data lives on the host via bind mounts, so it persists across container rebuilds and is easy to back up.

For the full guide — including adding to an existing docker-compose, systemd setup, cloud folder sync, and troubleshooting — see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## Requirements

- **Python 3.9+**
- **macOS or Linux** (the shell scripts use bash)
- **LibreOffice** or **Google Sheets** (to view the ODS report)
- **Docker** (optional, for containerized deployment)

---

## All CLI Commands at a Glance

| Command | Description |
|---------|-------------|
| `./run.sh` | Launch the GUI (default) |
| `./run.sh auto` | Run auto ingest + report via CLI |
| `python bank_ingest.py` | Auto-detect, import, and report (default) |
| `python bank_ingest.py auto` | Same as above |
| `python bank_ingest.py ingest <files>` | Import specific CSV or PDF files (auto-detects bank) |
| `python bank_ingest.py report` | Regenerate the ODS report |
| `python bank_ingest.py export` | Export to UTF-8 CSV |
| `python bank_ingest.py cards` | List card holders |
| `python bank_ingest.py cards add <last4> <name>` | Add a card holder |
| `python bank_ingest.py cards remove <last4>` | Remove a card holder |
| `python bank_ingest.py rules` | List categorization rules |
| `python bank_ingest.py rules add <pattern> <category>` | Add a rule |
| `python bank_ingest.py rules remove <pattern>` | Remove a rule |
| `python bank_ingest.py rules import-starter` | Import Portuguese starter rules (~100 rules) |
| `python bank_ingest.py reclean` | Re-clean all descriptions and regenerate report |
| `python bank_ingest.py backup` | Create a zip backup of all data |
| `python bank_ingest.py pdf` | Generate a monthly PDF summary report |
| `python bank_ingest.py suggest` | Analyze patterns and suggest categorization rules |
| `python bank_ingest.py fetch` | Download bank statements from email (IMAP) |
| `python bank_ingest.py fetch --setup` | Configure email settings interactively |
| `python bank_ingest.py gui` | Launch the Streamlit web interface |
| `python bank_ingest.py banks` | List supported bank statement formats |

---

## Development

For architecture details, design decisions, module reference, and how to extend the project, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

### Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

285 tests covering the CSV parser, PDF parser, multi-bank parsers, database, rules engine, starter rules, pattern detection, email fetch, GUI, export, backup, PDF report, and end-to-end workflows. Runs in under a second using synthetic fixtures (no real bank data needed).

---

## Privacy & Git

This project includes a `.gitignore` that keeps all personal/financial data out of version control:

| Ignored | Why |
|---------|-----|
| `raw/` | Raw bank statement CSVs — contain account numbers and transaction details |
| `data/` | SQLite database, config files, email credentials, and CSV exports — contain all your transactions, personal data, and email login (rules, card mappings, merchant notes, cleaning patterns, email-config.json) |
| `expense-report.ods` | The generated report — contains all your financial data |
| `reports/` | Monthly PDF summaries — contain financial data |
| `backups/` | Zip archives of all the above — also contain financial data |

**Only code and documentation are committed.** When you first clone the repo on a new machine, run `./install.sh` to recreate the directory structure and starter config files.

The system creates automatic and manual backups in `backups/` (see the [Backups](#backups) section). For offsite backup, copy the `backups/` folder to a private cloud folder or encrypted archive — **never commit data files to Git**.

---

## Troubleshooting

**"No new files to ingest"** — All CSV/PDF files in `raw/` have already been imported. Drop a new one and run again.

**"Could not find date range"** — The CSV/PDF file doesn't have a recognized bank header format. Use `ingest --no-rename` to import it with its original filename, or use `--bank <id>` to specify the parser explicitly.

**"No parser found for file"** — The auto-detection couldn't identify the bank format. Run `python bank_ingest.py banks` to see supported formats, then use `ingest --bank <id>` to specify the parser.

**"File already exists with range..."** — You're trying to import a CSV for a month that's already been imported with the same or wider date range. If you need to re-import, delete the existing file from `raw/` first.

**Formulas show `#VALUE!`** — Make sure you're opening the file in LibreOffice Calc (not a text editor). If the issue persists, run `python bank_ingest.py report --fresh` to regenerate all sheets.

**"externally-managed-environment" error** — You're trying to install packages on system Python. Run `./install.sh` to set up a virtual environment instead.
