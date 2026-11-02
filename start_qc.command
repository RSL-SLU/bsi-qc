#!/bin/bash
# Double-click on macOS (or run in a terminal). Starts the app and opens the browser.
cd "$(dirname "$0")"
python3 bsi_qc.py serve "$@"
