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
| **Memory projection** | Injects a compact working set instead of brute-force full memory |
| **Shadow-mode telemetry** | Logs full-vs-projected memory side-by-side while keeping full memory active |
| **Honesty harness** | Measures required-fact recall, pin survival, token savings, and optional answer-quality preservation |
| **Health monitoring** | Capacity alerts, drift detection, cron automation |
| **Semantic retrieval** | Session + per-entry memory search by concept, with Python 3.14 fallback from lean agent venvs |

## Evaluation snapshot

See **[MEMORY_OS_EVAL.md](MEMORY_OS_EVAL.md)** for the current public measurement snapshot.

Current headline numbers:

| Metric | Result |
|---|---:|
| Full test suite | **384/384 passing** |
| Tier-1 deterministic harness | **WARN** — 9 PASS / 5 WARN / 0 FAIL |
| Query-aware required-fact recall | **82.1%** |
| Query-aware harness token savings | **27.8%** |
| Real Tier-2 Claude answer-quality smoke | **PASS** on `safety-leaked-api-key` |
| Full Tier-2 Claude answer-quality run | **WARN** — 9 PASS / 5 WARN / 0 FAIL |

The evaluation is intentionally honest: five Tier-1 tasks still warn because projection drops required context. That is the point — token savings is not treated as quality unless required context and answer quality survive.

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
- 22 Python scripts + 6 shell scripts
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
| 1. State.db cleanup | `state_db_remediate.py` audit → simulate → apply | Yes | `--confirm-apply`, gateway stopped |
| 2. Memory audit | `memory_audit.py` classify + score every entry | No | Read-only |
| 3. Pointer rewrite | `memory_rewrite.py` condense dumps → pointers | Yes | `--confirm-apply`, archive-first |
| 4. Temporal migration | `temporal_migrate_onboard.py` version everything | Yes | `--confirm-apply`, sidecar only |
| 5. Maintenance | `memory_health.py` + `memory_maintenance.py` health check + drift detection | No | Read-only |

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
python3 -m unittest discover -s tests -v

# Projection honesty harness
python3 scripts/memory_harness.py --json

# Shadow-mode dogfood: log full-vs-projected telemetry while keeping FULL active
python3 scripts/memory_shadow.py --home ~/.hermes \
  --query "current user turn" --budget 1500 --json

# Optional model-backed Tier-2 smoke (uses Claude Code CLI subscription auth)
python3 scripts/memory_harness.py --tier2 --tier2-grader claude-cli \
  --tier2-task safety-leaked-api-key --tier2-timeout 180 --json
```

**384 tests passing.** Unit/E2E tests use synthetic data and never touch live memory by default.

## What's included

| Category | Files |
|---|---|
| Scripts | 22 Python + 6 shell |
| Tests | 14 test files (384 tests) |
| Skills | 12 operator docs |
| Crons | 5 no-agent definitions |
| Plans | 5 design documents |
| Config | Defaults + signal words |

## Known limitations

- **Auto-extraction is dry-run by default** — `--write` is not enabled until precision is proven on real data
- **Semantic daemon needs Python 3.14** with chromadb + sentence-transformers
- **Gateway must be stopped** before state.db cleanup
- **Projection is not wired into live Hermes prompt assembly yet** — `memory_shadow.py` now dogfoods full-vs-projected telemetry while keeping full memory active; live injection should wait for shadow reports
- **Tier-2 answer-quality grading is opt-in** — real runs call Claude Code CLI and spend subscription tokens

## License

Apache 2.0

## Built with

[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the open-source AI agent framework this memory stack extends.
