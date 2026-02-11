#!/bin/bash
#
# update.sh — pull the latest code from GitHub and re-install if needed.
#
# Usage:
#   ./update.sh
#
# What it does:
#   1. Fetches the latest changes from the remote repository
#   2. If there are new changes, pulls them and runs install.sh
#   3. If already up to date, does nothing
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

# ── Check git is available ───────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    fail "git is not installed. Please install it first."
fi

# ── Check we're in a git repo ────────────────────────────────────────────
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    fail "This directory is not a git repository. Run 'git init' and set up a remote first."
fi

# ── Check there's a remote ──────────────────────────────────────────────
REMOTE=$(git remote | head -n1)
if [ -z "$REMOTE" ]; then
    fail "No git remote configured. Add one with: git remote add origin <url>"
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
ok "On branch '$BRANCH', remote '$REMOTE'"

# ── Fetch latest ─────────────────────────────────────────────────────────
echo
echo "Fetching latest changes..."
git fetch "$REMOTE" "$BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "$REMOTE/$BRANCH")

if [ "$LOCAL" = "$REMOTE_HEAD" ]; then
    ok "Already up to date — nothing to do."
    exit 0
fi

# ── Show what's incoming ─────────────────────────────────────────────────
echo
echo "New changes available:"
git log --oneline "$LOCAL..$REMOTE_HEAD"
echo

# ── Pull ─────────────────────────────────────────────────────────────────
echo "Pulling changes..."
git pull --ff-only "$REMOTE" "$BRANCH"
ok "Code updated"

# ── Re-install if dependencies or install script changed ─────────────────
echo
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE_HEAD")

if echo "$CHANGED" | grep -qE '(requirements\.txt|install\.sh)'; then
    echo "Dependencies or install script changed — running install.sh..."
    echo
    ./install.sh
else
    ok "No dependency changes — skipping install"
fi

echo
echo -e "${GREEN}Update complete!${NC}"
echo "Run ./run.sh to process any new bank statements."
