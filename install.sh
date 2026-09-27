#!/bin/bash
#
# install.sh — set up everything needed to run the expense tracker.
#
# Usage:
#   ./install.sh                  # install from current branch
#   ./install.sh --branch main    # checkout a specific branch first
#   ./install.sh -b integration   # short form
#
# What it does:
#   1. Optionally checks out a specific git branch
#   2. Checks that Python 3.9+ is available
#   3. Creates a virtual environment (.venv) if it doesn't exist
#   4. Installs/upgrades Python dependencies from requirements.txt
#   5. Creates the raw/ and data/ directories if missing
#   6. Creates a starter rules.csv if one doesn't exist
#   7. Makes run.sh executable
#

set -e
cd "$(dirname "$0")"

# ── Parse arguments ──────────────────────────────────────────────────────
BRANCH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--branch)
            BRANCH="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./install.sh [--branch <branch-name>]"
            exit 1
            ;;
    esac
done

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

# ── 0. Checkout branch (if requested) ────────────────────────────────────
if [ -n "$BRANCH" ]; then
    if ! command -v git &>/dev/null; then
        fail "git is not installed — cannot switch branches."
    fi
    if ! git rev-parse --is-inside-work-tree &>/dev/null; then
        fail "Not a git repository — cannot switch branches."
    fi
    CURRENT=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    if [ "$CURRENT" = "$BRANCH" ]; then
        ok "Already on branch '$BRANCH'"
    else
        echo "Switching to branch '$BRANCH'..."
        git fetch origin "$BRANCH" --quiet 2>/dev/null || true
        git checkout "$BRANCH" --quiet
        ok "Switched to branch '$BRANCH'"
    fi
    echo
fi

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

VENV_PY="$(pwd)/.venv/bin/python"

# ── 3. Install dependencies ──────────────────────────────────────────────
echo
echo "Installing Python packages..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r requirements.txt --quiet

# ── 3b. Create scripts directory if needed ───────────────────────────────
mkdir -p scripts
ok "All packages installed"

# ── 4. Create directories ────────────────────────────────────────────────
echo
mkdir -p raw data data/advisor backups reports
ok "Directories ready (raw/, data/, data/advisor/, backups/, reports/)"

# ── 5. Starter config files ──────────────────────────────────────────────
if [ ! -f "data/rules.csv" ]; then
    cat > data/rules.csv << 'RULES'
pattern,match_field,category,subcategory,payment_type
RULES
    ok "Created starter data/rules.csv (add your categorization rules here)"
else
    ok "data/rules.csv already exists"
fi

if [ ! -f "data/account-holders.csv" ]; then
    cat > data/account-holders.csv << 'CARDS'
card_last4,name
CARDS
    ok "Created starter data/account-holders.csv (add your card mappings here)"
    echo "    Tip: run './bank_ingest.py cards add 1234 \"Your Name\"' to add a card"
else
    ok "data/account-holders.csv already exists"
fi

if [ ! -f "data/noise-words.txt" ]; then
    cat > data/noise-words.txt << 'NOISE'
# Noise words — removed from bank descriptions
# One word per line, case-insensitive.
CONTACTLESS
PT
Portugal
IE
LU
PORTO
LISBOA
NOISE
    ok "Created starter data/noise-words.txt (add noise words to strip from descriptions)"
else
    ok "data/noise-words.txt already exists"
fi

if [ ! -f "data/cleaning-patterns.csv" ]; then
    cat > data/cleaning-patterns.csv << 'PATTERNS'
type,pattern,description
prefix,COMPRA\s+\d{4}\s*,Card purchase with card number
prefix,DD\s+,Direct debit
prefix,TRF\.\s*P/O\s*,Transfer on behalf of
prefix,TRF\.\s*,Transfer
prefix,PAG\.\s*,Payment
prefix,MBWAY\s+\d{9}\s*,MB Way with phone number
"noise","\b(?=[A-Z0-9]*\d)[A-Z0-9]{8,12}\b","Transaction reference codes (requires at least one digit)"
noise,\b\d{4}-\d{3}\b,Portuguese postal codes
noise,\bLUXEMBOURG\s*LU\b,Luxembourg + country code
PATTERNS
    ok "Created starter data/cleaning-patterns.csv (regex patterns for description cleaning)"
else
    ok "data/cleaning-patterns.csv already exists"
fi

if [ ! -f "data/description-notes.csv" ]; then
    cat > data/description-notes.csv << 'DESCNOTES'
description_clean,merchant_note
DESCNOTES
    ok "Created starter data/description-notes.csv (merchant notes will be saved here)"
else
    ok "data/description-notes.csv already exists"
fi

# ── 6. Make scripts executable ───────────────────────────────────────────
chmod +x run.sh install.sh update.sh 2>/dev/null || true
ok "Shell scripts are executable"

# ── Done ─────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}All done!${NC}"
echo
echo "To get started:"
echo "  1. Run ./run.sh to launch the GUI (opens at http://localhost:8501)"
echo "  2. Use the Import page to upload bank CSV/PDF files"
echo "  3. Or use the CLI:  ./run.sh auto  (after dropping files in raw/)"
echo
