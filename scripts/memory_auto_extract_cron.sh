#!/bin/bash
# Hermes Memory Auto-Extraction — nightly cron wrapper (Phase 1: DRY-RUN).
#
# Runs memory_auto_extract.py over the last day's sessions, in DRY-RUN mode by
# default (writes NOTHING to MEMORY.md). Candidates land in
# ~/.hermes/memories/_auto_extract/ for review. Promotion to --write is a
# deliberate Phase-2 step, NOT enabled here.
#
# Schedule (example, do not auto-install): nightly after the dream cycle, e.g.
#   30 3 * * *  $HOME/.hermes/scripts/memory_auto_extract_cron.sh
#
# Safety: ensures the local Phi-4 server is up before running; degrades
# gracefully (logs and exits 0) if the model never comes up, so cron stays quiet.
set -uo pipefail

# Ensure HOME is set/exported (cron usually passes it). Fall back to the invoking
# user's real home via ~ (getpwuid) — never a hardcoded username.
export HOME="${HOME:-$(cd ~ && pwd)}"
SCRIPT_DIR="$HOME/.hermes/scripts"
LOG_DIR="$HOME/.hermes/memories/_auto_extract"
LOG="$LOG_DIR/cron.log"
LLM_URL="http://localhost:8080/v1/models"
LLM_LAUNCHD="com.emeka.hermes.local-llm"
DAYS="${1:-1}"            # lookback window; default 1 day (override: arg 1)
PY="$(command -v python3 || echo /usr/bin/python3)"

mkdir -p "$LOG_DIR"
stamp() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(stamp)] $*" | tee -a "$LOG"; }

log "=== memory_auto_extract cron start (days=$DAYS, DRY-RUN) ==="

# 1. Ensure the local LLM is reachable; start it via launchd if not.
if ! curl -sf -m 5 "$LLM_URL" >/dev/null 2>&1; then
    log "local LLM not responding — attempting launchctl start $LLM_LAUNCHD"
    launchctl start "$LLM_LAUNCHD" 2>>"$LOG" || log "launchctl start failed (continuing to poll)"
    for i in $(seq 1 30); do
        sleep 2
        if curl -sf -m 5 "$LLM_URL" >/dev/null 2>&1; then
            log "local LLM up after ${i} polls"
            break
        fi
    done
fi

if ! curl -sf -m 5 "$LLM_URL" >/dev/null 2>&1; then
    log "ERROR: local LLM still down — skipping extraction this run."
    log "=== cron end (skipped) ==="
    exit 0
fi

# 2. Run the extractor (DRY-RUN). Candidates JSON is written by the script.
cd "$SCRIPT_DIR" || { log "ERROR: cannot cd $SCRIPT_DIR"; exit 0; }
OUT="$("$PY" memory_auto_extract.py --dry-run --days "$DAYS" 2>>"$LOG")"
RC=$?
echo "$OUT" | tee -a "$LOG"
log "extractor exit code: $RC"

# 3. (Phase 2, disabled) Notify Telegram/Discord with the accepted summary.
#    Left intentionally off — wiring delivery is a separate, reviewed step and
#    must not touch the running gateway. To enable later, parse the latest
#    candidates-*.json 'accepted' list and post it.

log "=== cron end ==="
exit 0
