# Memory Shadow Report Gate — 2026-06-25

## Purpose
`memory_shadow_report.py` summarizes append-only `memory_shadow.py` JSONL telemetry and decides whether projected memory is safe enough for any live rollout.

It is intentionally cheap:
- stdlib only
- reads JSONL only
- no LLM calls
- no ChromaDB imports
- no raw memory block reads unless shadow logs already included them

## Command

```bash
python3 scripts/memory_shadow_report.py \
  ~/.hermes/notes/memory-stack/shadow-projection-2026-06-25.jsonl \
  --out reports/shadow-report-2026-06-25.md

python3 scripts/memory_shadow_report.py \
  ~/.hermes/notes/memory-stack/shadow-projection-2026-06-25.jsonl \
  --json > reports/shadow-report-2026-06-25.json
```

## Gates

Hard failures:
- malformed/missing input rows
- no shadow events
- `active_block != full`
- raw memory blocks logged
- projected over budget
- safety pinned entry dropped
- deterministic replay group produced different projected hashes
- answer used a missing safety/identity/operational pinned entry

Warnings:
- savings below threshold
- non-semantic/static relevance source
- no `answer_usage` telemetry
- used-missing rate above threshold
- skipped pinned entries requiring inspection

## Real run result

Input:
```text
~/.hermes/notes/memory-stack/shadow-projection-2026-06-25.jsonl
```

Result:
```text
status: WARN
shadow_events: 7
avg_savings: 74.5%
semantic_source_rate: 100%
raw_block_events: 0
over_budget_events: 0
safety_pin_drops: 0
determinism_violations: 0
used_missing_count: 0
warning: no answer_usage telemetry; cannot verify used-but-skipped context
```

Interpretation: projection has strong token savings and semantic retrieval is active, but this is **not a live-rollout PASS** because the current shadow batch did not include final answer text. The next shadow batch should pass `--answer-file` or `--answer-text` so the report can detect used-but-skipped context.

## Next improvement
Build `memory_search_map.py`: a tiny dynamic routing map that lets the agent inspect where knowledge lives before spending tokens/tool calls on deeper search.
