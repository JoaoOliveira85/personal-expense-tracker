#!/bin/bash
#
# install.sh — set up everything needed to run the expense tracker.
#
# Usage:
#   ./install.sh
#
# What it does:
#   1. Checks that Python 3.9+ is available
#   2. Creates a virtual environment (.venv) if it doesn't exist
#   3. Installs/upgrades Python dependencies from requirements.txt
#   4. Creates the raw/ and data/ directories if missing
#   5. Creates a starter rules.csv if one doesn't exist
#   6. Makes run.sh executable
#

set -e
cd "$(dirname "$0")"

# ── Colours (disable if not a terminal) ──────────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' NC=''
fi

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

# ── 1. Check Python ──────────────────────────────────────────────────────
echo "Checking dependencies..."
echo

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ] 2>/dev/null; then
            PYTHON="$cmd"
            ok "Found $cmd $version"
            break
        else
            warn "Found $cmd $version (need 3.9+, skipping)"
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.9+ is required but not found. Please install it first:
       macOS:   brew install python3
       Ubuntu:  sudo apt install python3
       Windows: https://www.python.org/downloads/"
fi

# ── 2. Virtual environment ───────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo
    echo "Creating virtual environment..."
    "$PYTHON" -m venv .venv
    ok "Created .venv"
else
    ok "Virtual environment already exists"
fi

# Activate it
source .venv/bin/activate

# ── 3. Install dependencies ──────────────────────────────────────────────
echo
echo "Installing Python packages..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "All packages installed"

# ── 4. Create directories ────────────────────────────────────────────────
echo
mkdir -p raw data
ok "Directories ready (raw/, data/)"

# ── 5. Starter config files ──────────────────────────────────────────────
if [ ! -f "rules.csv" ]; then
    cat > rules.csv << 'RULES'
pattern,match_field,category,subcategory,payment_type
RULES
    ok "Created starter rules.csv (add your categorization rules here)"
else
    ok "rules.csv already exists"
fi

if [ ! -f "account-holders.csv" ]; then
    cat > account-holders.csv << 'CARDS'
card_last4,name
CARDS
    ok "Created starter account-holders.csv (add your card mappings here)"
    echo "    Tip: run './bank_ingest.py cards add 1234 \"Your Name\"' to add a card"
else
    ok "account-holders.csv already exists"
fi

if [ ! -f "description-notes.csv" ]; then
    cat > description-notes.csv << 'DESCNOTES'
description_clean,merchant_note
DESCNOTES
    ok "Created starter description-notes.csv (merchant notes will be saved here)"
else
    ok "description-notes.csv already exists"
fi

# ── 6. Make scripts executable ───────────────────────────────────────────
chmod +x run.sh install.sh
ok "run.sh and install.sh are executable"

# ── Done ─────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}All done!${NC}"
echo
echo "To get started:"
echo "  1. Drop your bank CSV files into the raw/ folder"
echo "  2. Run ./run.sh (or: source .venv/bin/activate && python bank_ingest.py)"
echo "  3. Open expense-report.ods in LibreOffice or Google Sheets"
echo
