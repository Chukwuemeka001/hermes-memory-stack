# Hermes Memory Stack

**A memory operating system for AI agents.**

Your agent's memory breaks silently. MEMORY.md grows to 95%. state.db hits 500MB. Sessions balloon. Context gets lost. Nobody cleans it up because nobody built the tools.

This is the tool.

## What it does

| Capability | What it means |
|---|---|
| **Onboarding pipeline** | Audits your messy memory, cleans it up, and hands off to maintenance |
| **State.db remediation** | Finds and fixes bloated session databases (trigram FTS = 49% of every DB) |
| **Memory audit** | Finds duplicates, content dumps, stale entries, broken pointers |
| **Pointer rewrite** | Condenses verbose entries into concise pointers |
| **Temporal versioning** | Tracks every memory change with rollback and diff |
| **Memory projection** | Reduces token usage by 58% — injects only the most relevant entries |
| **Health monitoring** | Capacity alerts, drift detection, cron automation |
| **Semantic retrieval** | Find memories by concept, not just keywords |

## Quick start

```bash
# Clone
git clone https://github.com/Chukwuemeka001/hermes-memory-stack ~/.hermes/packages/hermes-memory-stack
cd ~/.hermes/packages/hermes-memory-stack

# Install everything
bash install.sh all

# Preview the cleanup (never modifies anything)
python3 scripts/memory_onboard.py --home ~/.hermes

# Apply when ready (asks before each mutation)
python3 scripts/memory_onboard.py --home ~/.hermes --apply
```

That's it. One install. One command. Messy → clean → maintained.

## For AI agents: automated setup

If you're an agent setting this up for a user, follow these steps exactly:

### Step 1: Install the package

```bash
git clone https://github.com/Chukwuemeka001/hermes-memory-stack ~/.hermes/packages/hermes-memory-stack
cd ~/.hermes/packages/hermes-memory-stack
bash install.sh all
```

This installs:
- 21 Python scripts + 6 shell scripts
- Semantic retrieval daemon (ChromaDB)
- Auto-extraction (dry-run by default)
- Temporal versioning
- Remediation tools (Areas 1-5)
- Health monitoring
- 5 cron definitions

### Step 2: Run the onboard (dry-run first)

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 scripts/memory_onboard.py --home ~/.hermes
```

This previews the full cleanup without modifying anything. Review the output:
- How much state.db can shrink (trigram drop)
- How many duplicates/dumps/stale entries exist
- What the rewrite proposals look like
- What the projected token savings are

### Step 3: Apply the cleanup

```bash
python3 scripts/memory_onboard.py --home ~/.hermes --apply
```

The onboard will ask before each mutation step. The gateway should be stopped before the state.db cleanup step.

### Step 4: Verify

```bash
python3 scripts/memory_health.py --home ~/.hermes --summary
python3 scripts/memory_maintenance.py --home ~/.hermes --dry-run
```

### Step 5: Project token-optimized memory

```bash
python3 scripts/memory_project.py --home ~/.hermes --budget 2000
```

Shows how much token savings the projection engine delivers.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Agent Turn                       │
│  ┌──────────────┐    ┌───────────────────────┐  │
│  │ Hot-Hot      │    │ Semantic Retrieval    │  │
│  │ (500 tokens) │    │ (on-demand)           │  │
│  │ injected     │    │ ChromaDB 384-dim      │  │
│  └──────────────┘    └───────────────────────┘  │
│          │                      │                │
│          ▼                      ▼                │
│  ┌──────────────────────────────────────────┐   │
│  │ MEMORY.md (full, not injected)           │   │
│  │ §-delimited entries                      │   │
│  └──────────────────────────────────────────┘   │
│          │                                       │
│          ▼                                       │
│  ┌──────────────────────────────────────────┐   │
│  │ Temporal Layer (bi-temporal)             │   │
│  │ JSONL source of truth + SQLite index     │   │
│  └──────────────────────────────────────────┘   │
│          │                                       │
│          ▼                                       │
│  ┌──────────────────────────────────────────┐   │
│  │ state.db (sessions + messages + FTS)     │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### Memory tiers

| Tier | What | Size | Injection |
|---|---|---|---|
| Hot-hot | Projected block | ~500 tokens | Every turn |
| Hot | MEMORY.md + USER.md | ~2,500 tokens | Available for retrieval |
| Warm | Notes + temporal | — | Queried via semantic search |
| Cold | state.db + sessions | — | Indexed, not injected |

## The onboarding pipeline

The pipeline runs 5 areas sequentially:

```bash
# One command runs all 5 areas
python3 scripts/memory_onboard.py --home ~/.hermes --apply
```

| Area | What | Mutates? | Gate |
|---|---|---|---|
| 1. State.db cleanup | Audit → simulate → apply | Yes | `--confirm-apply`, gateway stopped |
| 2. Memory audit | Classify + score every entry | No | Read-only |
| 3. Pointer rewrite | Condense dumps → pointers | Yes | `--confirm-apply`, archive-first |
| 4. Temporal migration | Version everything | Yes | `--confirm-apply`, sidecar only |
| 5. Maintenance | Health check + drift detection | No | Read-only |

Safety rules:
- **Archive-first**: every mutation archives the original with SHA-256
- **Dry-run default**: nothing modifies live data without explicit `--apply`
- **Never permanently delete**: entries are archived, not destroyed
- **Exit 0 on alerts**: health checks succeed even when alerting

## The projection engine

Reduces token usage by selecting only the most relevant entries:

```bash
python3 scripts/memory_project.py --home ~/.hermes --budget 2000
```

Real results on a production profile:
- Before: 4,731 tokens/turn (brute-force injection)
- After: 1,976 tokens/turn (projected)
- Savings: **58%**

Scoring model:
- Importance (30%): durability + pointer quality
- Recency (20%): temporal freshness, 30-day decay
- Specificity (20%): concrete vs vague
- Hot-fit (15%): ideal length for a hot pointer
- Always-inject (15%): user preferences, routing config

## Testing

```bash
cd ~/.hermes/packages/hermes-memory-stack

# Full test suite
python3 -m unittest tests.test_install tests.test_state_db_remediate tests.test_memory_audit \
    tests.test_memory_rewrite tests.test_temporal_migrate_onboard tests.test_memory_health \
    tests.test_memory_maintenance tests.test_e2e_pipeline tests.test_temporal_memory \
    tests.test_memory_auto_extract tests.test_consistency tests.test_onboard tests.test_memory_project

# Generate a synthetic messy profile to inspect
python3 tests/synthetic_profile.py /tmp/messy-profile --level stress --seed 42
```

**265 tests passing.** All synthetic, never touches live data.

## What's included

| Category | Files |
|---|---|
| Scripts | 21 Python + 6 shell |
| Tests | 12 test files (265 tests) |
| Skills | 9 operator docs |
| Crons | 5 no-agent definitions |
| Plans | 5 design documents |
| Config | Defaults + signal words |

## Known limitations

- **Auto-extraction is dry-run by default** — `--write` is not enabled until precision is proven on real data
- **Semantic daemon needs Python 3.14** with chromadb + sentence-transformers
- **Gateway must be stopped** before state.db cleanup
- **Projection is static (Phase 1)** — context-aware projection (Phase 2) is in design

## License

Apache 2.0

## Built with

[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the open-source AI agent framework this memory stack extends.
