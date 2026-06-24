#!/usr/bin/env bash
# Memory Stack — Weekly Maintenance + Temporal Sync wrapper (no_agent, local).
# Read-only over MEMORY.md/USER.md; --apply-temporal-sync records drift into the
# TEMPORAL layer only. No provider calls, no gateway/Telegram interference.
# Exit 0 on success even with alerts; non-zero only on a real script failure.
set -uo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# symlink-safe resolution of this script's real directory
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  D="$(cd -P "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$D/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
exec python3 "$DIR/memory_maintenance.py" --home "$HERMES_HOME" --apply-temporal-sync
