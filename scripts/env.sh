#!/bin/bash
#
# env.sh — shared environment setup for all shell scripts.
#
# Source this file at the start of any script that needs Python:
#   source "$(dirname "$0")/scripts/env.sh"
#   # or from subdirectory:
#   source "$(dirname "$0")/../scripts/env.sh"
#
# Provides:
#   $PROJECT_ROOT — absolute path to the project directory
#   $VENV_PY      — path to the virtual environment Python binary
#   py()          — function to run Python (use instead of bare 'python')
#

# Determine project root (where .venv lives)
if [ -n "$PROJECT_ROOT" ]; then
    : # Already set, use it
elif [ -d "$(dirname "${BASH_SOURCE[0]}")/../.venv" ]; then
    PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
elif [ -d ".venv" ]; then
    PROJECT_ROOT="$(pwd)"
else
    echo "Error: Cannot find project root (no .venv directory found)" >&2
    exit 1
fi

# Activate virtual environment
source "$PROJECT_ROOT/.venv/bin/activate"

# Set explicit Python path (works even when PATH doesn't include venv)
VENV_PY="$PROJECT_ROOT/.venv/bin/python"

# Convenience function to run Python
py() {
    "$VENV_PY" "$@"
}

export PROJECT_ROOT VENV_PY
