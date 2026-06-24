---
name: semantic-session-retrieval
description: "Concept-based (vector) recall over Hermes sessions. Hybrid FTS5+semantic search via session_search, a ChromaDB index, and a warm embedding daemon. Use when keyword search misses or to operate/troubleshoot the index."
version: 1.0.0
author: Hermes
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [memory, search, retrieval, sessions, embeddings, chromadb, session_search]
---

# Semantic Session Retrieval

Adds **vector / semantic search** to Hermes session recall so conversations are
findable by *concept*, not just exact keywords. A search for "risk management"
now surfaces a session that only ever said "position sizing" or "drawdown".

It is **additive** to the existing FTS5 keyword search in the `session_search`
tool: both run, results merge via **Reciprocal Rank Fusion (RRF, k=60)**, and
every hit is tagged `keyword` | `semantic` | `both`. If anything semantic is
unavailable, `session_search` silently returns exactly what pure FTS5 returned.

## When to use this skill

- A `session_search` keyword query returns nothing but you believe the topic was
  discussed (different words were used). Try the concept, e.g.
  `semantic_query.py "trading strategy"` finds liquidity/POI/inducement sessions.
- You need to **operate or troubleshoot** the index: (re)build it, start/check the
  warm daemon, add a new profile, or diagnose why semantic results are missing.
- You are extending retrieval (per-message chunking, new metadata, re-ranking).

## Architecture (read this before touching anything)

```
state.db (default + each profile)
   └─ semantic_index.py  ──embed (all-MiniLM-L6-v2, 384-dim)──>  ChromaDB
                                                                 ~/.hermes/chroma/sessions/
session_search tool (agent venv, Python 3.11 — NO chromadb)
   ├─ FTS5 keyword search          (in-process, unchanged)
   └─ semantic via Unix socket ──> semantic_query.py --serve  (Python 3.14 daemon, warm model)
                                     ~/.hermes/chroma/semantic.sock   (~10–80 ms/query)
   merge both rankings via RRF → tagged results
```

**The critical constraint:** `chromadb` + `sentence_transformers` are installed
**only under system Python 3.14** (`/opt/homebrew/lib/python3.14/site-packages`).
The Hermes agent runs in a **venv on Python 3.11** that *cannot* import them. So
the tool never imports the heavy deps — it talks to a long-lived **daemon**
(Python 3.14, model loaded once) over a Unix-domain socket using only stdlib.
Cold one-shot subprocess embedding is ~20 s; the warm daemon is ~10–80 ms.

Run the scripts with `python3` / `python3.14`, **never** the agent venv.

## Components

| File | Role |
|------|------|
| `~/.hermes/scripts/semantic_index.py` | Build/refresh the ChromaDB index from every state.db (incremental; `--reset` to rebuild). |
| `~/.hermes/scripts/semantic_query.py` | Query API + CLI, **and** the warm daemon (`--serve`). |
| `~/.hermes/scripts/semantic_reindex.sh` | Cron wrapper: reindex, then bounce the daemon. |
| `~/.hermes/hermes-agent/tools/session_search_tool.py` | Hybrid integration (`_discover` + `_semantic_*` helpers). |
| `~/.hermes/chroma/sessions/` | ChromaDB persistent store. |
| `~/.hermes/chroma/semantic.sock` / `semantic.pid` | Daemon socket + pidfile. |

Chroma ids are **composite `{profile}::{session_id}`** because session ids are
*not* globally unique across profiles (e.g. `nclexclaude` was branched from
`lowcredit` and shares 10 ids). Each row stores `db_path` so the tool can scope
semantic results to the profile it is running in. Embedding text per session:
`"{source} | {title} | {first_user_message[:500]}"`. Hidden sources
(`subagent`, `tool`) are excluded, matching `session_search` visibility.

## Operations

**Build / refresh the index** (first run downloads the 22 MB model):
```bash
python3 ~/.hermes/scripts/semantic_index.py            # incremental, all DBs
python3 ~/.hermes/scripts/semantic_index.py --reset    # full rebuild
python3 ~/.hermes/scripts/semantic_index.py --db ~/.hermes/profiles/X/state.db
```

**Start / check / stop the warm daemon:**
```bash
nohup python3.14 ~/.hermes/scripts/semantic_query.py --serve \
      > ~/.hermes/logs/semantic_daemon.log 2>&1 &      # start
python3.14 ~/.hermes/scripts/semantic_query.py --ping  # {"ok": true, "collection_count": N}
kill "$(cat ~/.hermes/chroma/semantic.pid)"            # stop
```

**Query from the CLI** (works with or without the daemon — cold ~20 s, warm fast):
```bash
python3 ~/.hermes/scripts/semantic_query.py "telegram gateway recovery"
python3 ~/.hermes/scripts/semantic_query.py "nclex flashcard quality" --n 5
python3 ~/.hermes/scripts/semantic_query.py "trading" --json --db ~/.hermes/state.db
python3 ~/.hermes/scripts/semantic_query.py "trading" --hybrid --fts ID1,ID2  # RRF demo
```

**Importable API:**
```python
import sys; sys.path.insert(0, "/Users/emeka/.hermes/scripts")
import semantic_query as sq
sq.semantic_search("risk management", n_results=10, db_path=None)
sq.hybrid_search("risk management", fts_results=["id1","id2"], n_results=10)  # RRF, k=60
```

**Keep it fresh (cron / routine, after the 3 AM dream cycle):**
```yaml
name: "Semantic session re-index"
schedule: "35 3 * * *"
command: ~/.hermes/scripts/semantic_reindex.sh
no_agent: true
deliver: local
```

## How `session_search` uses it

Discovery (`session_search(query=...)`) now runs FTS5 **and** semantic, merges via
RRF, and returns the usual result shape **plus**: `retrieval` per result
(`keyword`/`semantic`/`both`), `semantic_score` on semantic hits,
`retrieval_strategy` (`hybrid`/`keyword`), and `semantic_used` (bool). Scroll,
read, and browse shapes are untouched. Semantic runs **even when FTS5 returns
zero** — that is the point.

## Environment knobs

| Var | Default | Effect |
|-----|---------|--------|
| `HERMES_SESSION_SEMANTIC` | `1` | `0` → pure FTS5 (exact-original behavior; safety switch). |
| `HERMES_SEMANTIC_SOCK` | `~/.hermes/chroma/semantic.sock` | Daemon socket path. |
| `HERMES_SEMANTIC_TIMEOUT` | `8` | Socket query timeout (seconds). |
| `HERMES_SEMANTIC_SUBPROCESS` | `0` | `1` → allow slow one-shot subprocess when the daemon is down. |
| `HERMES_SEMANTIC_PYTHON` | auto | Explicit Python 3.14 interpreter with the deps. |
| `SEMANTIC_REINDEX_RESET` | `0` | `1` → reindex does a full `--reset`. |
| `SEMANTIC_REINDEX_NO_DAEMON` | `0` | `1` → reindex skips the daemon bounce. |

## Troubleshooting

- **Semantic results missing in `session_search`:** check the daemon —
  `semantic_query.py --ping`. If down, start it (above) or set
  `HERMES_SEMANTIC_SUBPROCESS=1`. The tool degrades to FTS5 meanwhile.
- **`ModuleNotFoundError: chromadb`:** you ran it under the agent venv (3.11). Use
  `python3` / `python3.14`.
- **New sessions not found:** run `semantic_index.py` then bounce the daemon (a
  long-lived daemon caches the collection; `semantic_reindex.sh` does both).
- **New profile added:** `semantic_index.py` auto-discovers `profiles/*/state.db`;
  just reindex.
- **Verify health:** `--ping` shows `collection_count`; it should equal the total
  non-hidden sessions across all DBs.

## Known limits / future work

- **Per-session granularity** (one vector per session, from the first user
  message + title). For finer recall, add per-message/exchange chunking
  (increases storage ~10–50×).
- **Daemon keepalive:** no launchd/watchdog yet. `semantic_reindex.sh` restarts it
  nightly; for always-on, add a LaunchAgent or a watchdog and set
  `SEMANTIC_REINDEX_NO_DAEMON=1` so cron doesn't fight it.
- **Re-embedding:** incremental indexing skips already-indexed sessions; it does
  not re-embed on title/content edits. Use `--reset` to fully rebuild.
- Model: `all-MiniLM-L6-v2`, cosine space, normalized embeddings. Keep index and
  query on the **same** model or vectors become incomparable.
