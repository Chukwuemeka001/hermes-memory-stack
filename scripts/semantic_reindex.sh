#!/usr/bin/env bash
#
# semantic_reindex.sh — refresh the Hermes semantic session index (cron-safe).
#
# Runs scripts/semantic_index.py (incremental by default) over the default
# state.db plus every profile state.db, then bounces the warm query daemon so
# the freshly-added vectors become visible. Designed for an unattended cron /
# routine entry with no_agent: true.
#
# The heavy deps (chromadb, sentence_transformers) live only under system
# Python 3.14, NOT the agent venv (Python 3.11), so this script explicitly
# resolves a 3.14 interpreter and never relies on the venv.
#
# Exit code reflects ONLY the index step — a daemon-bounce hiccup never fails
# the cron job (the session_search tool degrades to FTS5 if the daemon is down).
#
# Environment knobs:
#   SEMANTIC_REINDEX_RESET=1     full rebuild (--reset) instead of incremental
#   SEMANTIC_REINDEX_NO_DAEMON=1 skip the daemon bounce (e.g. launchd-managed)
#   HERMES_SEMANTIC_PYTHON=...    explicit python interpreter with the deps
#
# Suggested cron (after the 3:00 AM dream cycle):
#   name: "Semantic session re-index"
#   schedule: "35 3 * * *"
#   command: ~/.hermes/scripts/semantic_reindex.sh
#   no_agent: true
#   deliver: local

set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPTS_DIR="$HERMES_HOME/scripts"
LOG_DIR="$HERMES_HOME/logs"
LOG_FILE="$LOG_DIR/semantic_reindex.log"
INDEX_SCRIPT="$SCRIPTS_DIR/semantic_index.py"
QUERY_SCRIPT="$SCRIPTS_DIR/semantic_query.py"
PID_FILE="$HERMES_HOME/chroma/semantic.pid"
DAEMON_LOG="$LOG_DIR/semantic_daemon.log"

mkdir -p "$LOG_DIR" "$HERMES_HOME/chroma"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE" >&2; }

# --- Resolve a Python 3.14 interpreter that actually has the deps ------------
resolve_python() {
    local cands=(
        "${HERMES_SEMANTIC_PYTHON:-}"
        "$(command -v python3.14 2>/dev/null || true)"
        "/usr/local/bin/python3.14"
        "/opt/homebrew/bin/python3.14"
        "$(command -v python3 2>/dev/null || true)"
    )
    local p
    for p in "${cands[@]}"; do
        [ -n "$p" ] && [ -x "$p" ] || continue
        if "$p" -c "import chromadb, sentence_transformers" >/dev/null 2>&1; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

PY="$(resolve_python || true)"
if [ -z "${PY:-}" ]; then
    log "FATAL: no Python interpreter with chromadb + sentence_transformers found."
    log "       install with: python3.14 -m pip install --break-system-packages chromadb sentence-transformers"
    exit 2
fi
log "using interpreter: $PY"

if [ ! -f "$INDEX_SCRIPT" ]; then
    log "FATAL: indexer not found at $INDEX_SCRIPT"
    exit 2
fi

# --- Index ------------------------------------------------------------------
RESET_FLAG=""
if [ "${SEMANTIC_REINDEX_RESET:-0}" = "1" ]; then
    RESET_FLAG="--reset"
    log "mode: FULL REBUILD (--reset)"
else
    log "mode: incremental"
fi

log "indexing all state.db files ..."
# Bound the index step when a 'timeout' tool exists (macOS lacks GNU timeout
# unless coreutils' gtimeout is installed) so a wedged first-run model download
# cannot hang an unattended cron job forever.
TIMEOUT_BIN="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"
if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "${SEMANTIC_REINDEX_TIMEOUT:-900}" "$PY" "$INDEX_SCRIPT" $RESET_FLAG >>"$LOG_FILE" 2>&1
else
    "$PY" "$INDEX_SCRIPT" $RESET_FLAG >>"$LOG_FILE" 2>&1
fi
INDEX_RC=$?
if [ "$INDEX_RC" -eq 0 ]; then
    log "index step: OK"
else
    log "index step: FAILED (rc=$INDEX_RC)"
fi

# --- Bounce the warm daemon so new vectors are visible ----------------------
if [ "${SEMANTIC_REINDEX_NO_DAEMON:-0}" != "1" ] && [ "$INDEX_RC" -eq 0 ]; then
    if [ -f "$PID_FILE" ]; then
        OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        # Only signal a PID that is actually our daemon: a stale pidfile left by
        # an unclean death (SIGKILL/OOM/crash) could otherwise name a recycled,
        # unrelated process.
        if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null \
           && ps -p "$OLD_PID" -o command= 2>/dev/null | grep -q "semantic_query.py"; then
            log "stopping existing daemon (pid=$OLD_PID)"
            kill "$OLD_PID" 2>/dev/null || true
            # Wait until it actually exits so its cleanup runs BEFORE we rebind
            # the socket (prevents the old cleanup clobbering the new socket).
            for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.3; done
            if kill -0 "$OLD_PID" 2>/dev/null; then
                log "daemon ignored SIGTERM; escalating to SIGKILL"
                kill -9 "$OLD_PID" 2>/dev/null || true
                for _ in $(seq 1 10); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.3; done
            fi
        elif [ -n "${OLD_PID:-}" ]; then
            log "pidfile names pid=$OLD_PID which is not the daemon; not killing"
        fi
    fi
    log "starting warm daemon ..."
    nohup "$PY" "$QUERY_SCRIPT" --serve >>"$DAEMON_LOG" 2>&1 &
    NEW_PID=$!
    disown "$NEW_PID" 2>/dev/null || true
    log "daemon launched (pid=$NEW_PID); health-checking ..."
    UP=0
    for _ in $(seq 1 30); do
        if "$PY" "$QUERY_SCRIPT" --ping >/dev/null 2>&1; then UP=1; break; fi
        sleep 1
    done
    if [ "$UP" = "1" ]; then log "daemon healthy"; else log "WARN: daemon did not answer ping (tool will fall back to FTS5)"; fi
else
    log "daemon bounce skipped"
fi

log "done (index rc=$INDEX_RC)"
exit "$INDEX_RC"
