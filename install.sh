#!/usr/bin/env bash
# Hermes Memory Stack — Installer
# Installs semantic retrieval, auto-extraction, temporal versioning,
# remediation (Areas 1-5), health/maintenance, and cron automation.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SEMANTIC_PYTHON="${HERMES_SEMANTIC_PYTHON:-}"
if [ -z "$SEMANTIC_PYTHON" ]; then
    if command -v python3.14 >/dev/null 2>&1; then
        SEMANTIC_PYTHON="$(command -v python3.14)"
    elif [ -x /opt/homebrew/bin/python3.14 ]; then
        SEMANTIC_PYTHON="/opt/homebrew/bin/python3.14"
    else
        SEMANTIC_PYTHON="$(command -v python3)"
    fi
fi
SCRIPTS_DIR="$HERMES_HOME/scripts"
SKILLS_DIR="$HERMES_HOME/skills/memory-stack"
CRONS_DIR="$HERMES_HOME/crons"
LOGS_DIR="$HERMES_HOME/logs"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[memory-stack]${NC} $*"; }
warn() { echo -e "${YELLOW}[memory-stack]${NC} $*"; }
err() { echo -e "${RED}[memory-stack]${NC} $*" >&2; }

# ── Shared setup ────────────────────────────────────────────────
ensure_dirs() {
    mkdir -p "$SCRIPTS_DIR" "$SKILLS_DIR" "$CRONS_DIR" "$LOGS_DIR" "$HERMES_HOME/config"
}

# ── Tier 1: Semantic Retrieval ──────────────────────────────────
install_semantic() {
    log "Installing semantic retrieval..."

    # Check dependencies in the semantic interpreter — Hermes' agent venv may be Python 3.11
    # without ChromaDB, while semantic retrieval is intentionally installed under Python 3.14.
    if ! "$SEMANTIC_PYTHON" -c "import chromadb, sentence_transformers" 2>/dev/null; then
        warn "chromadb/sentence-transformers not found for $SEMANTIC_PYTHON. Installing via module pip..."
        "$SEMANTIC_PYTHON" -m pip install --user chromadb sentence-transformers 2>/dev/null || \
            "$SEMANTIC_PYTHON" -m pip install chromadb sentence-transformers
    fi

    # Copy scripts
    cp "$SCRIPT_DIR/scripts/semantic_index.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/semantic_query.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_entry_index.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_project.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_shadow.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_harness.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_harness_tasks.json" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/semantic_reindex.sh" "$SCRIPTS_DIR/"
    chmod +x "$SCRIPTS_DIR/semantic_index.py"
    chmod +x "$SCRIPTS_DIR/semantic_query.py"
    chmod +x "$SCRIPTS_DIR/memory_entry_index.py"
    chmod +x "$SCRIPTS_DIR/memory_project.py"
    chmod +x "$SCRIPTS_DIR/memory_shadow.py"
    chmod +x "$SCRIPTS_DIR/memory_harness.py"
    chmod +x "$SCRIPTS_DIR/semantic_reindex.sh"
    cp "$SCRIPT_DIR/skills/"*.md "$SKILLS_DIR/" 2>/dev/null || true

    # Create chroma directory
    mkdir -p "$HERMES_HOME/chroma/sessions"

    # Run initial index (HERMES_HOME is exported at top of installer)
    log "Running initial session index..."
    "$SEMANTIC_PYTHON" "$SCRIPTS_DIR/semantic_index.py" || \
        warn "Initial session index failed — may need chromadb deps. Run manually after install."

    log "Running initial memory-entry index..."
    "$SEMANTIC_PYTHON" "$SCRIPTS_DIR/memory_entry_index.py" index --home "$HERMES_HOME" || \
        warn "Initial memory-entry index failed — run manually after install."

    # Start daemon (nohup so it survives shell exit)
    log "Starting semantic daemon..."
    nohup "$SEMANTIC_PYTHON" "$SCRIPTS_DIR/semantic_query.py" --serve \
        > "$LOGS_DIR/semantic_daemon.log" 2>&1 &
    disown
    sleep 3

    # Verify (HERMES_HOME is exported, scripts read it from env)
    if "$SEMANTIC_PYTHON" "$SCRIPTS_DIR/semantic_query.py" --ping >/dev/null 2>&1; then
        log "✓ Semantic retrieval installed and daemon running"
    else
        warn "Daemon started but ping failed — check $LOGS_DIR/semantic_daemon.log"
    fi
}

# ── Tier 2: Auto-Extraction ─────────────────────────────────────
install_auto_extract() {
    log "Installing auto-extraction..."

    cp "$SCRIPT_DIR/scripts/memory_signals.py" "$SCRIPTS_DIR/"   # shared signals — intake-gate dependency (INTEG-8)
    cp "$SCRIPT_DIR/scripts/memory_auto_extract.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_auto_extract_cron.sh" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_auto_extract_eval.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_auto_extract_sample_real.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_auto_extract_fixtures"*.jsonl "$SCRIPTS_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/scripts/hermes_memory_intake_gate.py" "$SCRIPTS_DIR/"
    chmod +x "$SCRIPTS_DIR/memory_auto_extract.py"
    chmod +x "$SCRIPTS_DIR/memory_auto_extract_cron.sh"
    chmod +x "$SCRIPTS_DIR/memory_auto_extract_eval.py"
    chmod +x "$SCRIPTS_DIR/memory_auto_extract_sample_real.py"

    # Copy signal words config
    cp "$SCRIPT_DIR/config/signal-words.txt" "$HERMES_HOME/config/" 2>/dev/null || true

    log "✓ Auto-extraction installed (dry-run mode by default)"
    log "  Run: python3 $SCRIPTS_DIR/memory_auto_extract.py --dry-run --home $HERMES_HOME"
}

# ── Tier 3: Temporal Versioning ─────────────────────────────────
install_temporal() {
    log "Installing temporal versioning..."

    cp "$SCRIPT_DIR/scripts/memory_signals.py" "$SCRIPTS_DIR/"   # shared signals — temporal_memory dependency (INTEG-8)
    cp "$SCRIPT_DIR/scripts/temporal_memory.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/temporal_migrate.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/temporal_migrate_onboard.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/temporal_ingest.sh" "$SCRIPTS_DIR/"
    chmod +x "$SCRIPTS_DIR/temporal_memory.py"
    chmod +x "$SCRIPTS_DIR/temporal_migrate.py"
    chmod +x "$SCRIPTS_DIR/temporal_migrate_onboard.py"
    chmod +x "$SCRIPTS_DIR/temporal_ingest.sh"

    # Run migration (idempotent, sidecar-only — never modifies MEMORY.md)
    log "Migrating existing MEMORY.md to versioned format..."
    python3 "$SCRIPTS_DIR/temporal_migrate.py" --home "$HERMES_HOME" || \
        warn "Temporal migration had issues — run manually after install."

    log "✓ Temporal versioning installed"
}

# ── Tier 4: Remediation (Areas 1-5) ────────────────────────────
install_remediation() {
    log "Installing remediation (Areas 1-5)..."

    # Core remediation scripts
    cp "$SCRIPT_DIR/scripts/memory_signals.py" "$SCRIPTS_DIR/"   # shared signals — memory_audit dependency (INTEG-8)
    cp "$SCRIPT_DIR/scripts/state_db_remediate.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_audit.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_rewrite.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_health.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_project.py" "$SCRIPTS_DIR/"  # projection footer / Phase 1 engine
    cp "$SCRIPT_DIR/scripts/memory_shadow.py" "$SCRIPTS_DIR/"   # shadow-mode projection telemetry
    cp "$SCRIPT_DIR/scripts/memory_harness.py" "$SCRIPTS_DIR/"  # Phase B projection honesty harness
    cp "$SCRIPT_DIR/scripts/memory_harness_tasks.json" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_health_cron.sh" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_maintenance.py" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_maintenance_cron.sh" "$SCRIPTS_DIR/"
    cp "$SCRIPT_DIR/scripts/memory_onboard.py" "$SCRIPTS_DIR/"   # one-command Area 1→5 driver (INTEG-10)
    chmod +x "$SCRIPTS_DIR/state_db_remediate.py"
    chmod +x "$SCRIPTS_DIR/memory_audit.py"
    chmod +x "$SCRIPTS_DIR/memory_rewrite.py"
    chmod +x "$SCRIPTS_DIR/memory_health.py"
    chmod +x "$SCRIPTS_DIR/memory_project.py"
    chmod +x "$SCRIPTS_DIR/memory_shadow.py"
    chmod +x "$SCRIPTS_DIR/memory_harness.py"
    chmod +x "$SCRIPTS_DIR/memory_health_cron.sh"
    chmod +x "$SCRIPTS_DIR/memory_maintenance.py"
    chmod +x "$SCRIPTS_DIR/memory_maintenance_cron.sh"
    chmod +x "$SCRIPTS_DIR/memory_onboard.py"
    cp "$SCRIPT_DIR/skills/"*.md "$SKILLS_DIR/" 2>/dev/null || true

    log "✓ Remediation installed (Areas 1-5)"
    log "  Onboard: python3 $SCRIPTS_DIR/memory_onboard.py --home $HERMES_HOME           # one command, dry-run by default"
    log "  Audit:   python3 $SCRIPTS_DIR/memory_audit.py --home $HERMES_HOME --json"
    log "  Health:  python3 $SCRIPTS_DIR/memory_health.py --home $HERMES_HOME --summary"
    log "  State DB: python3 $SCRIPTS_DIR/state_db_remediate.py audit --home $HERMES_HOME"
}

# ── Tier 5: Cron Automation ─────────────────────────────────────
install_crons() {
    log "Installing cron definitions..."

    cp "$SCRIPT_DIR/crons/"*.json "$CRONS_DIR/" 2>/dev/null || true

    log "✓ Cron definitions copied to $CRONS_DIR/"
    log "  Register with Hermes: hermes cron create (or register manually)"
    log "  Available crons:"
    for f in "$CRONS_DIR"/*.json; do
        [ -f "$f" ] && log "    - $(basename "$f" .json)"
    done
}

# ── Tier 6: Config ──────────────────────────────────────────────
install_config() {
    log "Installing config..."

    cp "$SCRIPT_DIR/config/memory-defaults.yaml" "$HERMES_HOME/config/memory-stack-defaults.yaml" 2>/dev/null || true

    # Check if memory section exists in config.yaml
    if ! grep -q "semantic:" "$HERMES_HOME/config.yaml" 2>/dev/null; then
        warn "Add these to ~/.hermes/config.yaml:"
        echo ""
        echo "memory:"
        echo "  semantic:"
        echo "    enabled: true"
        echo "  auto_extract:"
        echo "    enabled: true"
        echo "    dry_run: true"
        echo "  temporal:"
        echo "    enabled: true"
        echo ""
    fi
}

# ── Verify ──────────────────────────────────────────────────────
install_verify() {
    log "Running post-install verification..."

    local fail=0

    # Check all core scripts exist and are importable
    for script in \
        state_db_remediate memory_audit memory_rewrite \
        memory_health memory_maintenance memory_project memory_shadow \
        temporal_memory temporal_migrate temporal_migrate_onboard \
        memory_auto_extract hermes_memory_intake_gate \
        memory_entry_index semantic_index semantic_query; do
        if [ -f "$SCRIPTS_DIR/${script}.py" ]; then
            if python3 -c "import sys; sys.path.insert(0, '$SCRIPTS_DIR'); import ${script}" 2>/dev/null; then
                log "  ✓ ${script}.py"
            else
                warn "  ⚠ ${script}.py exists but import failed"
                fail=1
            fi
        else
            err "  ✗ ${script}.py MISSING"
            fail=1
        fi
    done

    # Check cron wrappers
    for wrapper in memory_health_cron.sh memory_maintenance_cron.sh memory_auto_extract_cron.sh temporal_ingest.sh semantic_reindex.sh; do
        if [ -f "$SCRIPTS_DIR/$wrapper" ] && [ -x "$SCRIPTS_DIR/$wrapper" ]; then
            log "  ✓ $wrapper"
        else
            warn "  ⚠ $wrapper missing or not executable"
            fail=1
        fi
    done

    # Check health script runs
    if python3 "$SCRIPTS_DIR/memory_health.py" --home "$HERMES_HOME" --summary >/dev/null 2>&1; then
        log "  ✓ memory_health.py runs successfully"
    else
        warn "  ⚠ memory_health.py failed — check dependencies"
        fail=1
    fi

    if [ "$fail" -eq 0 ]; then
        log "✓ All verification checks passed"
    else
        warn "Some checks had warnings — review above"
    fi
}

# ── Main ────────────────────────────────────────────────────────
main() {
    echo ""
    echo "╔══════════════════════════════════════════════╗"
    echo "║      Hermes Memory Stack Installer          ║"
    echo "╚══════════════════════════════════════════════╝"
    echo ""
    log "HERMES_HOME=$HERMES_HOME"
    log "SCRIPTS_DIR=$SCRIPTS_DIR"
    echo ""

    TIER="${1:-all}"

    ensure_dirs

    case "$TIER" in
        semantic|1)     install_semantic ;;
        extraction|2)   install_auto_extract ;;
        temporal|3)     install_temporal ;;
        remediation|4)  install_remediation ;;
        crons|5)        install_crons ;;
        config)         install_config ;;
        verify)         install_verify ;;
        all)
            install_semantic
            echo ""
            install_auto_extract
            echo ""
            install_temporal
            echo ""
            install_remediation
            echo ""
            install_crons
            echo ""
            install_config
            echo ""
            install_verify
            ;;
        *)
            err "Unknown tier: $TIER"
            echo "Usage: $0 [all|semantic|extraction|temporal|remediation|crons|config|verify]"
            exit 1
            ;;
    esac

    echo ""
    log "Installation complete!"
    log "Run '$0 verify' to check all components."
}

main "$@"
