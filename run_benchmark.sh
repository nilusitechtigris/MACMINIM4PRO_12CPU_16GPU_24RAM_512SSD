#!/bin/zsh
# Full benchmark: download models -> 3 rounds over all models -> summary/charts.
# Keeps the Mac awake for the whole run. Safe to re-run: finished tests are skipped.
set -e
cd "$(dirname "$0")"
mkdir -p logs results
./pull_models.sh
caffeinate -dims python3 bench.py
python3 summarize.py
