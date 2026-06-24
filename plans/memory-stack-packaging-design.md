# Hermes Memory Stack — Export Packaging Design

## What This Is

A complete, modular memory system for Hermes Agent that gives any user:
- **Three-tier memory** (hot pointers → warm notes → cold session search)
- **Semantic retrieval** (find sessions by concept, not just keywords)
- **Auto-extraction** (automatically captures corrections and preferences)
- **Temporal versioning** (track how knowledge evolves over time)
- **Automated hygiene** (curator + dreaming for self-cleaning memory)

## Packaging Philosophy

**Modular, not monolithic.** Users pick what they need:
- Tier 0: Basic memory (MEMORY.md + notes) — ships with Hermes, zero setup
- Tier 1: Semantic retrieval — needs chromadb + sentence-transformers
- Tier 2: Auto-extraction — needs a running LLM (local or API)
- Tier 3: Temporal versioning — needs the versioning DB
- Tier 4: Full stack (all of the above + dreaming + curator)

Each tier is independently installable and usable without the others.

## Install Experience

```bash
# Full stack (recommended)
hermes skills install hermes-memory-stack

# Or individual components
hermes skills install hermes-semantic-retrieval
hermes skills install hermes-auto-extraction
hermes skills install hermes-temporal-versioning
```

## Package Structure

```
hermes-memory-stack/
├── SKILL.md                    # Main skill (triggers, overview, quick start)
├── README.md                   # Detailed docs for humans
├── install.sh                  # One-command setup
├── config/
│   ├── memory-defaults.yaml    # Default config values
│   └── signal-words.txt        # Auto-extraction signal words
├── scripts/
│   ├── semantic_index.py       # ChromaDB session indexer
│   ├── semantic_query.py       # Semantic + hybrid search daemon
│   ├── semantic_reindex.sh     # Nightly re-index cron wrapper
│   ├── memory_auto_extract.py  # Auto-extraction from conversations
│   ├── memory_auto_extract_cron.sh  # Nightly extraction cron
│   ├── temporal_memory.py      # Temporal versioning DB + queries
│   ├── temporal_migrate.py     # Migrate existing MEMORY.md to versioned
│   ├── memory_curator_daily.py # Daily sweep (stale detection)
│   ├── memory_curator_monitor.py  # Capacity monitor
│   └── memory_curator_weekly.py   # Weekly LLM consolidation
├── skills/
│   ├── semantic-session-retrieval.md
│   ├── memory-auto-extraction.md
│   └── temporal-memory-versioning.md
└── crons/
    ├── semantic-reindex.json   # Cron job definition
    ├── auto-extract.json       # Cron job definition
    ├── curator-daily.json      # Cron job definition
    ├── curator-monitor.json    # Cron job definition
    └── curator-weekly.json     # Cron job definition
```

## Config Integration

Add to user's ~/.hermes/config.yaml:
```yaml
memory:
  # Tier 0: Always on (ships with Hermes)
  memory_enabled: true
  memory_char_limit: 15000
  user_char_limit: 6000
  
  # Tier 1: Semantic retrieval
  semantic:
    enabled: true
    model: all-MiniLM-L6-v2
    chroma_path: ~/.hermes/chroma/sessions
    daemon: true  # run as Unix socket daemon
    
  # Tier 2: Auto-extraction
  auto_extract:
    enabled: true
    model: auto  # use cheapest available
    max_facts_per_session: 5
    max_facts_per_night: 10
    min_session_turns: 2
    dry_run: false  # set true for first week
    
  # Tier 3: Temporal versioning
  temporal:
    enabled: true
    db_path: ~/.hermes/memory_versions.db
    max_versions_per_entry: 10
    prune_after_days: 90
    
  # Tier 4: Curator + Dreaming
  curator:
    enabled: true
    sweep_schedule: "50 3 * * *"
    monitor_schedule: "0 */6 * * *"
    weekly_schedule: "0 4 * * 0"
```

## Dependencies

| Component | Required Packages | Install |
|-----------|------------------|---------|
| Tier 0 | None (built-in) | — |
| Tier 1 | chromadb, sentence-transformers | `pip install chromadb sentence-transformers` |
| Tier 2 | None (uses existing LLM) | — |
| Tier 3 | None (SQLite) | — |
| Tier 4 | anthropic (for weekly LLM) | `pip install anthropic` |

## Export Formats

### As Hermes Skill (recommended)
```bash
hermes skills install hermes-memory-stack
# Installs to ~/.hermes/skills/hermes-memory-stack/
# Auto-registers crons on first run
# Prompts for config on first use
```

### As Standalone Scripts
```bash
git clone https://github.com/.../hermes-memory-stack
cd hermes-memory-stack
./install.sh
# Copies scripts to ~/.hermes/scripts/
# Adds crons via hermes cron create
# Updates config.yaml
```

### As Docker/Podman Container
For users who want isolation:
```bash
podman run -v ~/.hermes:/root/.hermes hermes-memory-stack
```

## What Makes This Different From Existing Solutions

| Feature | Mem0 | Letta | Zep | **Hermes Memory Stack** |
|---------|------|-------|-----|------------------------|
| Hot pointers (auto-injected) | ❌ | ✅ | ❌ | ✅ |
| Warm notes (topic-indexed) | ❌ | ❌ | ❌ | ✅ |
| Cold search (FTS5 + semantic) | Vector only | ✅ | KG | ✅ Hybrid RRF |
| Auto-extraction | ✅ | ❌ | ❌ | ✅ (with review loop) |
| Temporal versioning | ❌ | ❌ | ✅ | ✅ |
| Nightly dreaming | ❌ | ❌ | ❌ | ✅ |
| Automated curator | ❌ | ❌ | ❌ | ✅ |
| Multi-platform (Telegram/Discord/etc) | ❌ | ❌ | ❌ | ✅ |
| Self-hosted | ✅ | ✅ | Partial | ✅ |
| No new infra needed | ❌ (needs Qdrant) | ❌ (needs server) | ❌ (needs Neo4j) | ✅ (SQLite + ChromaDB) |

## Target Audience

1. **Existing Hermes users** — want better memory, zero-config install
2. **AI agent builders** — want a memory layer for their own agents
3. **Researchers** — want to study agent memory architectures

## IG Post Angle

"I built a memory system for my AI agent that works like a human brain — it thinks with hot memory, searches by concept not keywords, captures what I tell it automatically, tracks how knowledge changes over time, and dreams at night to consolidate everything. And it's open source."

Three-tier visual: 🔴 Hot → 🟡 Warm → 🔵 Cold
Plus: 🌙 Dreaming + 🧹 Curator = Self-healing memory

## Next Steps

1. ✅ Semantic retrieval (Opus done)
2. 🔨 Auto-extraction (Opus building now)
3. 🔨 Temporal versioning (Opus building now)
4. 🔨 Export packaging (this doc)
5. ❌ E2E testing with real usage
6. ❌ README + install script
7. ❌ Publish to Hermes skills hub
