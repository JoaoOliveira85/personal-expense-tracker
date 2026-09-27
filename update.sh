#!/bin/bash
#
# update.sh — pull the latest code from the upstream development repo and
# re-install if needed.
#
# Usage:
#   ./update.sh
#
# Code is developed in a separate, data-free repository (the "upstream").
# This lets your own clone keep committing personal data to its own private
# remote (origin) while still receiving code updates.
#
# What it does:
#   1. Makes sure a remote points at the upstream repo (adds 'upstream' if not)
#   2. Fetches the upstream branch and merges it into the current branch
#   3. Re-runs install.sh if dependencies changed
#   4. If already up to date, does nothing
#
# Environment overrides:
#   EXPENSE_TRACKER_UPSTREAM  upstream repo URL
#                             (default: git@github.com:JoaoOliveira85/personal-expense-tracker.git)
#   EXPENSE_TRACKER_BRANCH    upstream branch to follow (default: main)
#

set -euo pipefail
cd "$(dirname "$0")"

UPSTREAM_URL="${EXPENSE_TRACKER_UPSTREAM:-git@github.com:JoaoOliveira85/personal-expense-tracker.git}"
UPSTREAM_BRANCH="${EXPENSE_TRACKER_BRANCH:-main}"

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

# ── Find (or add) the upstream remote ────────────────────────────────────
# Compare by "owner/repo" so SSH and HTTPS URLs of the same repo both match.
repo_path() { echo "$1" | sed -E 's#^.*github\.com[:/]##; s#\.git$##'; }

WANTED=$(repo_path "$UPSTREAM_URL")
REMOTE=""
for r in $(git remote); do
    if [ "$(repo_path "$(git remote get-url "$r")")" = "$WANTED" ]; then
        REMOTE="$r"
        break
    fi
done

if [ -z "$REMOTE" ]; then
    if git remote | grep -qx upstream; then
        fail "Remote 'upstream' exists but points elsewhere ($(git remote get-url upstream)). Set EXPENSE_TRACKER_UPSTREAM or fix the remote."
    fi
    git remote add upstream "$UPSTREAM_URL"
    REMOTE=upstream
    ok "Added remote 'upstream' → $UPSTREAM_URL"
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
TARGET="$REMOTE/$UPSTREAM_BRANCH"
ok "On branch '$BRANCH', following '$TARGET'"

# ── Fetch latest ─────────────────────────────────────────────────────────
echo
echo "Fetching latest changes..."
git fetch "$REMOTE" "$UPSTREAM_BRANCH" --quiet

LOCAL=$(git rev-parse HEAD)

if git merge-base --is-ancestor "$TARGET" HEAD; then
    ok "Already up to date — nothing to do."
    exit 0
fi

# ── Show what's incoming ─────────────────────────────────────────────────
echo
echo "New changes available:"
git log --oneline "HEAD..$TARGET"
echo

# ── Merge ────────────────────────────────────────────────────────────────
# Fast-forwards when possible; otherwise creates a merge commit so any
# personal commits on this branch are kept.
echo "Merging changes..."
if ! git merge --no-edit "$TARGET"; then
    git merge --abort 2>/dev/null || true
    fail "Merge conflict — nothing was changed. Resolve manually with: git merge $TARGET"
fi
ok "Code updated"

# ── Re-install if dependencies or install script changed ─────────────────
echo
CHANGED=$(git diff --name-only "$LOCAL" HEAD)

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
