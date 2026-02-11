#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
python bank_ingest.py