---
name: hermes-memory-stack
description: "Memory system for Hermes Agent — hot/warm/cold memory, semantic retrieval, conservative auto-extraction, temporal versioning, and a 5-area first-install remediation pipeline. (Curator + dreaming are existing Hermes features, NOT bundled here — see the Status table.)"
version: 1.0.0
author: Emeka + Hermes Agent
license: MIT
triggers:
  - memory system
  - agent memory
  - persistent memory
  - session search
  - semantic search
  - auto-extraction
  - temporal memory
  - memory remediation
metadata:
  hermes:
    tags: [memory, retrieval, semantic, extraction, temporal, remediation]
---

# Hermes Memory Stack

A modular memory system for Hermes Agent: hot pointers, semantic session
retrieval, conservative auto-extraction, temporal versioning, and a first-install
remediation pipeline (Areas 1–5) for homes that already have memory overload.

**Single source of truth:** every command below is copy-paste runnable. For the
full end-to-end operator workflow see **[RUNBOOK.md](RUNBOOK.md)**; for design
detail per area see `skills/*.md`; for status see `README.md`.

## What It Does (and what is bundled)

| Tier | Feature | What It Does | Bundled? |
|------|---------|-------------|----------|
| 0 | Hot Pointers | `MEMORY.md` auto-injected every turn | Built-in to Hermes |
| 0 | Warm Notes | Topic-indexed markdown loaded on demand | Built-in to Hermes |
| 0 | Cold Search | FTS5 keyword search over sessions | Built-in to Hermes |
| 1 | Semantic Retrieval | Find sessions by concept, not keywords | ✅ this package (`pip install chromadb sentence-transformers`) |
| 2 | Auto-Extraction | Captures corrections/preferences (needs a local LLM) | ✅ this package |
| 3 | Temporal Versioning | Track how knowledge evolves over time | ✅ this package (stdlib/SQLite) |
| — | Remediation Areas 1–5 | First-install cleanup: state.db → audit → rewrite → temporal → maintenance | ✅ this package |
| 4 | Curator / Dreaming | Nightly consolidation + automated stale cleanup | ❌ **NOT bundled** — existing Hermes feature; the Area 1–5 pipeline replaces its first-install cleanup |

## Quick Start (full stack)

```bash
# 1. Tier-1 dependency (semantic retrieval only; Tiers 2-3 + Areas 1-5 are stdlib)
pip install chromadb sentence-transformers

# 2. Install everything (Tiers 1-3 + Areas 1-5 + crons + config, then verify)
cd ~/.hermes/packages/hermes-memory-stack && ./install.sh all
#   (or a single tier: ./install.sh semantic | extraction | temporal | remediation)

# 3. Verify
python3 ~/.hermes/scripts/semantic_query.py --ping                 # semantic daemon health
python3 ~/.hermes/scripts/memory_health.py --home ~/.hermes --summary   # one-line health
python3 ~/.hermes/scripts/memory_auto_extract.py --dry-run --days 1     # needs a local LLM; writes nothing
```

> Auto-extraction (Tier 2) needs a local LLM reachable at `CONFIG['llm_endpoint']`
> (default `localhost:8080`). If it can't reach the model it now exits non-zero and
> says so loudly — it will not silently report "0 facts".

## Quick Start (individual components)

```bash
# Semantic retrieval only
pip install chromadb sentence-transformers
./install.sh semantic
python3 ~/.hermes/scripts/semantic_query.py "memory system architecture" --n 5

# Auto-extraction only (dry-run; needs a local LLM)
./install.sh extraction
python3 ~/.hermes/scripts/memory_auto_extract.py --dry-run

# Temporal versioning only
./install.sh temporal
python3 ~/.hermes/scripts/temporal_memory.py stats --json
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Agent Turn                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Hot Ptrs │  │ Warm     │  │ Cold Search  │  │
│  │ (MEMORY) │  │ Notes    │  │ (FTS5+Vec)   │  │
│  └──────────┘  └──────────┘  └──────────────┘  │
│       ▲              ▲               ▲          │
│  ┌────┴──────────────┴───────────────┴────────┐ │
│  │           Memory Layer                      │ │
│  │  ┌─────────────┐  ┌─────────────────────┐  │ │
│  │  │ Temporal DB  │  │ ChromaDB Vectors   │  │ │
│  │  │ (versions)   │  │ (semantic index)   │  │ │
│  │  └─────────────┘  └─────────────────────┘  │ │
│  └────────────────────────────────────────────┘ │
│       ▲                                          │
│  ┌────┴──────────┐   ┌──────────────┐           │
│  │ Auto-Extract  │   │ Remediation  │ ← bundled │
│  │ (capture)     │   │ Areas 1-5    │           │
│  └───────────────┘   └──────────────┘           │
│  ( Curator / Dreaming = existing Hermes feature, │
│    NOT bundled in this package )                 │
└─────────────────────────────────────────────────┘
```

## How Each Tier Works

### Tier 0: Basic Memory (built-in to Hermes)
`MEMORY.md` is injected into every system prompt. Notes load on demand. FTS5
searches session transcripts. Zero setup.

### Tier 1: Semantic Retrieval
Embeds each session as a 384-dim vector (`all-MiniLM-L6-v2`), stores in ChromaDB,
and at query time fuses vector search with FTS5 via Reciprocal Rank Fusion (RRF).
Runs as a daemon for fast queries.

### Tier 2: Auto-Extraction
Scans recent session transcripts for correction/preference signals, uses a cheap
local LLM to extract atomic facts, runs them through the write-time intake gate
(`hermes_memory_intake_gate.py`) and dedup, and — only in `--write` mode —
appends accepted facts to `MEMORY.md` (atomic + locked + archived).

### Tier 3: Temporal Versioning
Every memory entry gets a version history: when a fact changed, its previous
value, and when each version was active. Supports "what was true at time X".

### Remediation Areas 1–5 (first-install cleanup)
A read-only-by-default pipeline for homes that already have memory overload.
See **[RUNBOOK.md](RUNBOOK.md)** for the exact ordered commands and file
hand-offs. Briefly:

1. **Area 1 — state.db** (`state_db_remediate.py`): audit/plan/simulate/apply for a bloated `state.db`. Explicit-run only.
2. **Area 2 — hot-memory audit** (`memory_audit.py`): read-only quality report + per-entry recommended actions.
3. **Area 3 — pointer rewrite** (`memory_rewrite.py`): turns the audit into reviewable `old → new` proposals; apply is gated.
4. **Area 4 — temporal migration** (`temporal_migrate_onboard.py`): wires the cleaned hot files into the temporal layer.
5. **Area 5 — maintenance** (`memory_health.py`, `memory_maintenance.py`): one consolidated read-only health pass.

## Configuration

See `config/memory-defaults.yaml`. Capacity + state.db thresholds there MIRROR
the authoritative values in `scripts/memory_health.py` (capacity WARN 80% /
CRIT 90%; state.db WARN 50 MB / CRIT 200 MB). Key toggles:

```yaml
memory:
  semantic: { enabled: true }
  auto_extract: { enabled: true, dry_run: true }   # dry-run until trusted
  temporal: { enabled: true }
```

## File Reference (scripts bundled in this package)

| File | Purpose |
|------|---------|
| `scripts/memory_signals.py` | **Shared** format constants + durability regexes (imported by audit / intake-gate / temporal) |
| `scripts/semantic_index.py` | Index sessions into ChromaDB |
| `scripts/semantic_query.py` | Query engine + daemon (`--ping`, `--serve`) |
| `scripts/semantic_reindex.sh` | Nightly re-index cron wrapper |
| `scripts/memory_auto_extract.py` | Auto-extraction from conversations (Tier 2) |
| `scripts/hermes_memory_intake_gate.py` | Write-time intake classifier (ALLOW/REJECT/REVIEW) |
| `scripts/temporal_memory.py` | Temporal versioning DB + queries (Tier 3) |
| `scripts/temporal_migrate.py` / `temporal_migrate_onboard.py` | Migrate MEMORY.md into the versioned/temporal layer |
| `scripts/state_db_remediate.py` | **Area 1** — audit/plan/simulate/apply for a bloated `state.db` |
| `scripts/memory_audit.py` | **Area 2** — read-only hot-memory quality audit |
| `scripts/memory_rewrite.py` | **Area 3** — pointer rewrite proposals (dry-run/render) |
| `scripts/memory_health.py` / `memory_maintenance.py` | **Area 5** — health score + consolidated maintenance pass |

## Troubleshooting

**Semantic search returns no results:**
```bash
python3 ~/.hermes/scripts/semantic_query.py --ping
# if the daemon is down, restart it:
python3 ~/.hermes/scripts/semantic_query.py --serve --home ~/.hermes &
```

**Auto-extraction reports a model error / "COULD NOT REACH THE MODEL":**
```bash
# the extractor exits non-zero when it cannot reach the LLM. Check the endpoint
# (CONFIG['llm_endpoint'], default localhost:8080) and your local llama-server,
# then re-run the dry-run:
python3 ~/.hermes/scripts/memory_auto_extract.py --dry-run --days 1
```

**Memory growing too large (check capacity):**
```bash
python3 ~/.hermes/scripts/memory_health.py --home ~/.hermes            # green/yellow/red
python3 ~/.hermes/scripts/memory_audit.py  --home ~/.hermes --json     # per-entry recommendations
# then follow RUNBOOK.md (Areas 2-3) to rewrite/condense.
```

## What Makes This Different

Most agent memory systems are vector-only (Mem0), OS-inspired servers (Letta), or
heavy knowledge graphs (Zep/Cognee). This stack combines hot pointers, semantic
retrieval, temporal versioning, and a safe first-install remediation pipeline into
a single self-hosted, zero-infra system that works with any Hermes setup.
