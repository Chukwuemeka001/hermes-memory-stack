# Cheap Dynamic Search Map — Design Note — 2026-06-25

## STATUS: BUILT (2026-06-25)

`scripts/memory_search_map.py` now implements this design. It ships in the semantic and remediation install tiers and is covered by `tests/test_memory_search_map.py`.

```bash
# compact map (JSON default; --markdown for the <=1000-token human view)
python3 scripts/memory_search_map.py build  --home ~/.hermes --json
python3 scripts/memory_search_map.py build  --home ~/.hermes --markdown

# route a query to the right lane(s) + exact next commands
python3 scripts/memory_search_map.py query  --home ~/.hermes --query "provider failover history" --json

# validate store freshness/health
python3 scripts/memory_search_map.py doctor --home ~/.hermes --json
```

Shipped behavior:
- `build` / `query` / `doctor` commands.
- stdlib only; no LLM, embedding load, or ChromaDB import. Semantic health uses a short daemon ping.
- READ-ONLY everywhere; SQLite is opened read-only/immutable where applicable.
- Search lanes: `memory-entry`, `session-semantic`, `temporal`, `notes-canonical`, `source-code`, `spine`.
- Ranking blends intent heuristics, lexical topic overlap, and store availability.
- Safety: emitted strings are secret-scrubbed; labels/keys/match terms only, never raw memory bodies.

The rest of this note is the original design rationale, kept for provenance.

## User requirement
When the agent lacks enough current context, going deeper should stay cheap. Before expensive semantic/vector/session reads, the agent should inspect a tiny dynamic map that tells it where knowledge lives, then route directly to the right search lane using relevance, recency/time, authority, and verification signals.

## Do we already have this?
Partially.

Existing pieces:

| Piece | Location | What it gives | Gap |
|---|---|---|---|
| Notes table of contents | `~/.hermes/notes/INDEX.md` | Human-maintained topic map for notes | Not automatically refreshed, no scores/counts/freshness/search routing |
| Master context index | `~/.hermes/notes/MASTER_CONTEXT_INDEX.md` | Broad source-of-truth map | Human doc, not machine-ranking/search-router |
| Memory Spine | `~/.hermes/scripts/hermes_memory_spine.py`, `~/.hermes/memory_spine/memory_spine.sqlite` | Evidence ledger + indexed artifacts with authority/freshness/verification schema | Not wired into Memory Stack package as the cheap first-pass map; may be stale/separate |
| Hot-memory projection | `scripts/memory_project.py` | Per-entry scoring, pins, semantic relevance, recency | Works over hot memory only; not a global map over notes/sessions/artifacts |
| Session semantic retrieval | `semantic_query.py` / `session_search` | Concept search over sessions | Retrieval lane, not a map |
| Per-entry memory index | `memory_entry_index.py` | Concept search over hot-memory entries | Retrieval lane, not a map |

Conclusion: **we have the raw ingredients, not the productized cheap dynamic map.**

## Proposed minimal map

Build `memory_search_map.py` later as a cheap routing index that emits a compact JSON/Markdown map under ~500–1000 tokens.

### Inputs
- `~/.hermes/memories/MEMORY.md`
- `~/.hermes/memories/USER.md`
- `~/.hermes/notes/INDEX.md`
- `~/.hermes/notes/MASTER_CONTEXT_INDEX.md`
- `~/.hermes/memory_spine/memory_spine.sqlite` if present
- Chroma collection counts (`sessions`, `memories`)
- Temporal DB fact counts/currentness if present
- Recent shadow reports and health reports

### Output sketch

```json
{
  "generated_at": "...",
  "budget_tokens": 800,
  "health": {"sessions": 224, "memories": 61, "notes": 80},
  "lanes": [
    {
      "id": "memory-stack",
      "summary": "Hermes Memory Stack / projection / semantic retrieval work",
      "where": [
        "~/.hermes/packages/hermes-memory-stack/",
        "~/.hermes/notes/memory-stack/",
        "~/.hermes/skills/hermes/agent-memory-stack-engineering/"
      ],
      "search_first": [
        "memory_entry_index search",
        "session_search semantic",
        "search_files notes"
      ],
      "freshness": "hot",
      "authority": "verified/local",
      "last_seen": "2026-06-25"
    }
  ],
  "routing_rules": [
    {"if": "project status", "use": "notes index then source file"},
    {"if": "past conversation", "use": "session_search hybrid"},
    {"if": "hot preference/fact", "use": "memory_entry_index search"},
    {"if": "procedure", "use": "skills_list/skill_view"}
  ]
}
```

### Why this is cheaper
The agent first reads a tiny map and chooses a lane instead of spraying expensive broad searches. The map should answer:

1. What domains exist?
2. Which source is canonical?
3. Which search tool should be used first?
4. Is the lane fresh/stale?
5. What is the cheapest next lookup?

## Relationship to shadow report
`memory_shadow_report.py` is the rollout gate for projection quality. The cheap map is the future routing layer *before* retrieval/projection. Shadow reports should feed the map with health signals:

- semantic source health
- frequently skipped refs
- frequently used missing refs
- domains with poor projection coverage
- latest PASS/WARN/FAIL status

## Next build after shadow report
Build `scripts/memory_search_map.py`:

1. `build` command: generate JSON + Markdown map.
2. `query` command: given a user query, output top lanes + exact next search commands.
3. `refresh` command: update from notes index, Memory Spine, semantic counts, and recent shadow reports.
4. Tests with synthetic notes/memory/spine DB.

Acceptance bar:
- <1000-token Markdown output by default.
- No LLM/API calls.
- Includes source paths and routing commands.
- Detects stale/missing indexes.
- Can explain why it picked a lane.
