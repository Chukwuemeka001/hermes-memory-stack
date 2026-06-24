#!/usr/bin/env bash
# Memory Stack — Daily Health Check wrapper (no_agent, local delivery).
# Read-only. Exit 0 on success even when health is RED (alerts are in content);
# exit non-zero ONLY if the health script itself fails to run.
set -uo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# symlink-safe resolution of this script's real directory
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  D="$(cd -P "$(dirname "$SOURCE")" && pwd)"; SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$D/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
exec python3 "$DIR/memory_health.py" --home "$HERMES_HOME" --markdown
