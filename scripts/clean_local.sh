#!/usr/bin/env bash
# Clean local cache and outputs for a fresh test run.
# Usage: ./scripts/clean_local.sh

set -e
cd "$(dirname "$0")/.."

echo "Cleaning cache/ and outputs/..."
rm -rf cache/* outputs/* 2>/dev/null || true
echo "Done. Run: python -m src.main --init-db  # to re-seed DB from company_master.json"
