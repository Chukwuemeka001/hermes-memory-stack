# Optimized Semantic Retrieval Flow — 2026-06-25

## Decision

After the cheap dynamic search map chooses a lane, semantic retrieval is the targeted deep lane. The optimization is mostly infrastructure, not more model intelligence: keep retrieval relevance-only, make the warm daemon serve both `sessions` and `memories`, return compact handles/snippets, then let projection own recency/authority/budget decisions.

## Correct flow

```text
User query / missing context
  ↓
memory_search_map.py query       # cheap stdlib scout, no embeddings
  ↓
lane + filters
  ↓
semantic_query.py daemon          # warm model, shared Chroma root
  ├─ sessions collection
  └─ memories collection
  ↓
handle-only memory/session hits   # no raw body dump over socket
  ↓
memory_project.py                 # relevance feeds only W_RELEVANCE
  ↓
memory_shadow_capture/report      # answer-aware gate
  ↓
live projection / reinforcement   # only after gates pass
```

## Phase 1 shipped

Phase 1 warms the expensive `memory-entry` lane:

- `semantic_query.py` now supports named collections via the daemon request contract:
  - `collection: "sessions" | "memories"`
  - `where` metadata filter pass-through
  - `fields: "handle"` to strip `document` from hot-path responses
- `memory_entry_index.py search_memories()` now tries the daemon first and tags results with `__search_source: daemon`; cold direct Chroma/model loading remains as fallback.
- `memory_project.py build_relevance_index()` now prefers the daemon-backed memory search and demotes the 45s subprocess path to last resort.

## Why not heavier algorithms yet

Rejected for now:

- cross-encoder rerank: too heavy for ~60 memory entries / ~224 sessions
- LLM query rewriting: violates determinism/token discipline
- multi-vector field weighting: memory entries are short pointer-style facts, not rich title/body/tag documents
- freshness/authority in retrieval score: projection already owns recency/importance; adding them to retrieval would double-count
- MMR: possible later, but deterministic tie-breaking must be proven first

## Live verification

```text
semantic daemon ping: sessions=224, memories=61
memories daemon query: ok, collection=memories, handle-only, no document field
memory_entry_index search: 0.067s, source=daemon, document=false
memory_project query: relevance_source='memories-index:20 hits via daemon'
```

## Next phases

1. **Phase 2 telemetry:** add retrieval latency/path/pool telemetry into shadow reports and gate on daemon path rate.
2. **Phase 3 router integration:** map sends lane/filter packet into optimized retrieval before projection.
3. **Phase 4 live projection pilot:** feature flag, default profile only, full fallback.
4. **Phase 5 reinforcement:** answer-used refs become session-hot; unused refs decay.
