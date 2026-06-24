# Memory Semantic Retrieval Plan (Hindsight-style)

## Goal
Add vector/semantic search to session_search so you can find conversations by concept, not just keywords. "What did we discuss about risk management?" should find it even if the conversation used "position sizing" or "drawdown limits."

## Current Gap
session_search uses FTS5 keyword search only. Requires exact word matches. If you search "risk management" but the conversation used "position sizing," you get nothing.

## Research Findings

### Dependencies (already installed)
- `chromadb` — importable, vector database
- `sentence_transformers` — importable, `all-MiniLM-L6-v2` model (22MB download on first run)
- Location: `/opt/homebrew/lib/python3.14/site-packages/`
- **CRITICAL: Never delete these during disk cleanup**

### Performance Benchmarks
| Operation | Latency | Notes |
|-----------|---------|-------|
| Embed single text (384-dim) | ~5-15ms | Warm model |
| Embed 100 sessions | ~30-60s | Batch mode |
| ChromaDB query (top-10) | ~5-10ms | In-memory index |
| Total semantic query | ~50-80ms | Embed query + lookup |

### Storage Overhead
~5KB per session in ChromaDB (384-dim float32 vector + metadata). At 1,000 sessions = ~5MB. Negligible.

### Hindsight's Multi-Strategy Pattern
Hindsight uses **Reciprocal Rank Fusion (RRF)** to combine multiple retrieval strategies:
1. Keyword search (FTS5) — exact word matches
2. Semantic search (vector) — concept similarity
3. Recency bias — newer sessions ranked higher

Each strategy produces a ranked list. RRF merges them:
```
RRF_score(doc) = Σ 1/(k + rank_in_strategy_i)
```
where k=60 (standard constant). Documents appearing in multiple strategies rank highest.

## Architecture

```
Session transcript
  → Chunk into meaningful segments (by message or by exchange)
  → Embed with all-MiniLM-L6-v2 (384 dimensions)
  → Store in ChromaDB (persistent, local)
  → On query: embed query text → ChromaDB nearest-neighbor → merge with FTS5 results via RRF
```

### Collection Layout
```
~/.hermes/
  chroma/
    sessions/          # ChromaDB persistent storage
      chroma.parquet
      ...
  scripts/
    semantic_index.py  # Indexing script
    semantic_query.py  # Query script (CLI + importable)
```

### Embedding Strategy
Per session, embed a **summary string** (not full transcript):
```
"{session_title} | {source} | {first_user_message[:200]} | {key_topics}"
```
This keeps embeddings focused on what the session is ABOUT, not every word spoken. ChromaDB metadata stores session_id, title, source, started_at for filtering.

### Alternative: Per-message chunking
For finer-grained retrieval, embed individual messages (user + assistant exchanges). But this increases storage ~10-50× and may reduce precision (too many near-matches on generic phrases).

**Recommendation:** Start with per-session embeddings. Add per-message chunking later if recall is insufficient.

## Implementation Steps

### Step 1: Write Indexing Script (`~/.hermes/scripts/semantic_index.py`)
```python
#!/usr/bin/env python3
"""Index Hermes session transcripts into ChromaDB for semantic search."""

import json, os, sqlite3, glob
from pathlib import Path

# Lazy imports (heavy, only needed at runtime)
_chroma = None
_model = None

def _get_chroma():
    global _chroma
    if _chroma is None:
        import chromadb
        db_path = os.path.expanduser("~/.hermes/chroma/sessions")
        os.makedirs(db_path, exist_ok=True)
        _chroma = chromadb.PersistentClient(path=db_path)
    return _chroma

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model

def index_sessions(db_path=None, batch_size=50):
    """Read sessions from state.db, embed, and store in ChromaDB."""
    if db_path is None:
        db_path = os.path.expanduser("~/.hermes/state.db")
    
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    
    sessions = conn.execute("""
        SELECT s.id, s.source, s.started_at, s.model,
               GROUP_CONCAT(CASE WHEN m.role='user' THEN SUBSTR(m.content, 1, 200) END, ' | ') as user_msgs
        FROM sessions s
        LEFT JOIN messages m ON m.session_id = s.id
        GROUP BY s.id
    """).fetchall()
    
    client = _get_chroma()
    model = _get_model()
    
    # Get or create collection
    try:
        collection = client.get_collection("sessions")
    except:
        collection = client.create_collection("sessions", metadata={"hnsw:space": "cosine"})
    
    # Check which sessions are already indexed
    existing = set()
    if collection.count() > 0:
        for batch in collection.get(include=[])["ids"]:
            existing.update(batch)
    
    # Index new sessions
    to_index = [s for s in sessions if s["id"] not in existing]
    print(f"Sessions total: {len(sessions)}, already indexed: {len(existing)}, to index: {len(to_index)}")
    
    for i in range(0, len(to_index), batch_size):
        batch = to_index[i:i+batch_size]
        ids = [s["id"] for s in batch]
        documents = []
        metadatas = []
        
        for s in batch:
            # Build embedding text: source + first user message
            text = f"{s['source'] or ''} | {(s['user_msgs'] or '')[:500]}"
            documents.append(text)
            metadatas.append({
                "source": s["source"] or "",
                "started_at": s["started_at"] or 0,
                "model": s["model"] or "",
            })
        
        # Embed batch
        embeddings = model.encode(documents, show_progress_bar=False).tolist()
        
        # Store
        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        print(f"  Indexed batch {i//batch_size + 1}: {len(batch)} sessions")
    
    conn.close()
    print(f"Done. Total in collection: {collection.count()}")

if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else None
    index_sessions(db_path=db)
```

### Step 2: Write Query Script (`~/.hermes/scripts/semantic_query.py`)
```python
#!/usr/bin/env python3
"""Semantic search over Hermes sessions via ChromaDB."""

import os, sys

_chroma = None
_model = None

def _get_chroma():
    global _chroma
    if _chroma is None:
        import chromadb
        db_path = os.path.expanduser("~/.hermes/chroma/sessions")
        _chroma = chromadb.PersistentClient(path=db_path)
    return _chroma

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model

def semantic_search(query: str, n_results: int = 10) -> list[dict]:
    """Search sessions by semantic similarity."""
    client = _get_chroma()
    model = _get_model()
    
    try:
        collection = client.get_collection("sessions")
    except:
        return []
    
    # Embed query
    query_embedding = model.encode([query], show_progress_bar=False).tolist()
    
    # Search
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    
    hits = []
    for i in range(len(results["ids"][0])):
        hits.append({
            "session_id": results["ids"][0][i],
            "distance": results["distances"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
        })
    
    return hits

def hybrid_search(query: str, fts_results: list[str] = None, n_results: int = 10, k: int = 60) -> list[dict]:
    """Combine semantic + FTS5 results using Reciprocal Rank Fusion."""
    semantic_hits = semantic_search(query, n_results=n_results * 2)
    
    # Build RRF scores
    scores = {}
    documents = {}
    
    # Semantic ranking
    for rank, hit in enumerate(semantic_hits):
        sid = hit["session_id"]
        scores[sid] = scores.get(sid, 0) + 1.0 / (k + rank + 1)
        documents[sid] = hit
    
    # FTS5 ranking (if provided)
    if fts_results:
        for rank, sid in enumerate(fts_results):
            scores[sid] = scores.get(sid, 0) + 1.0 / (k + rank + 1)
            if sid not in documents:
                documents[sid] = {"session_id": sid, "source": "fts5"}
    
    # Sort by RRF score
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    
    return [documents[sid] for sid, _ in ranked[:n_results]]

if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "trading system architecture"
    print(f"Query: {query}\n")
    
    hits = semantic_search(query, n_results=5)
    for i, hit in enumerate(hits):
        print(f"{i+1}. [{hit['distance']:.3f}] {hit['session_id']}")
        print(f"   {hit['document'][:150]}...")
        print()
```

### Step 3: Initial Index Run
```bash
python3 ~/.hermes/scripts/semantic_index.py
# First run downloads all-MiniLM-L6-v2 (22MB), then indexes all sessions
```

### Step 4: Test Queries
```bash
python3 ~/.hermes/scripts/semantic_query.py "trading strategy risk management"
python3 ~/.hermes/scripts/semantic_query.py "NCLEX flashcard quality"
python3 ~/.hermes/scripts/semantic_query.py "telegram gateway recovery"
```

### Step 5: Wire into session_search (Future)
Once validated, add semantic_search as a second retrieval strategy in the session_search tool:
1. Run FTS5 query (existing)
2. Run semantic query (new)
3. Merge via RRF
4. Return combined results

This is a code change to `~/.hermes/hermes-agent/tools/session_search_tool.py` — defer until Phase 2.

### Step 6: Nightly Re-index Cron
```yaml
# Add to dreaming cron or create standalone
name: "Semantic session re-index"
schedule: "35 3 * * *"   # 3:35 AM, after dreaming at 3:00 AM
script: ~/.hermes/scripts/semantic_index.py
no_agent: true
deliver: local
```

## Phase Plan

### Phase 1: Standalone Index + Query (1 hour)
- Write scripts, run initial index, test queries
- No code changes to Hermes core
- Validate: "does semantic search find things FTS5 misses?"

### Phase 2: Hybrid Search in session_search (2-3 hours)
- Modify session_search_tool.py to run both FTS5 + semantic
- Merge via RRF
- Test with real queries Emeka would use

### Phase 3: Per-message Chunking (if needed)
- If per-session granularity is too coarse
- Embed individual user-assistant exchanges
- Increases storage but improves precision

## Effort Estimate
- Phase 1: 1 hour (scripts + initial index + test)
- Phase 2: 2-3 hours (session_search integration)
- Phase 3: 2 hours (if needed)
