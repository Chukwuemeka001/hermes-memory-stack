#!/usr/bin/env bash
# Nightly temporal ingest wrapper for Hermes Memory Stack.
# Sidecar-only: records versions in history.jsonl / memory_versions.db; does not modify MEMORY.md or USER.md.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPT="$HERMES_HOME/scripts/temporal_memory.py"
LOG_DIR="$HERMES_HOME/logs"
mkdir -p "$LOG_DIR"

if [[ ! -f "$SCRIPT" ]]; then
  echo "temporal_memory.py not found at $SCRIPT" >&2
  exit 2
fi

python3 "$SCRIPT" ingest MEMORY.md USER.md --source nightly >> "$LOG_DIR/temporal_ingest.log" 2>&1
# Weekly prune belongs in a separate cron after confidence; not here.
