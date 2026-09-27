#!/bin/bash
#
# run.sh — launch the Expense Tracker GUI.
#
# Usage:
#   ./run.sh          # start the GUI (default)
#   ./run.sh auto     # run the CLI auto command instead (ingest + report)
#   ./run.sh <cmd>    # pass any command to bank_ingest.py
#
cd "$(dirname "$0")"
source scripts/env.sh

if [ $# -eq 0 ]; then
    py bank_ingest.py gui
else
    py bank_ingest.py "$@"
fi
