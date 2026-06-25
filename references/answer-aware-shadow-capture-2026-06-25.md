# Answer-Aware Shadow Capture — 2026-06-25

## Purpose
`memory_shadow_capture.py` closes the gap found by the first real shadow report: the log had projection telemetry but no final answer text, so `memory_shadow_report.py` could not check whether the answer used context that projection skipped.

The capture wrapper does one cheap operational step after a real answer:

1. Run `memory_shadow.run_shadow(...)` with the current query and final answer.
2. Append the event to the shadow JSONL file.
3. Regenerate Markdown + JSON rollout reports.
4. Keep `active_block: full`; this is still shadow-only.

## Command

```bash
python3 scripts/memory_shadow_capture.py --home ~/.hermes \
  --query "CURRENT USER TURN" \
  --answer-file /tmp/final-answer.txt \
  --out ~/.hermes/notes/memory-stack/shadow-projection-$(date +%F).jsonl
```

Optional strict gate:

```bash
python3 scripts/memory_shadow_capture.py --home ~/.hermes \
  --query "CURRENT USER TURN" \
  --answer-file /tmp/final-answer.txt \
  --strict
```

## Safety rules

- Requires answer text; otherwise exits 2.
- Does not log raw full/projected memory blocks.
- Keeps FULL memory as the active answer source.
- Uses the existing report hard gates for safety pins, raw blocks, over-budget, deterministic replay, and used-missing critical pins.
- Requires at least 5 answer-aware turns by default before the report can PASS; fewer answer turns remain WARN for rollout confidence.

## Why this matters
A shadow report without answer text can prove token savings and semantic retrieval health, but it cannot prove recall preservation. Answer-aware capture adds the missing recall signal: `answer_usage.used_missing_from_projection`.

## Next gate
Collect enough answer-aware real turns, then rerun:

```bash
python3 scripts/memory_shadow_report.py \
  ~/.hermes/notes/memory-stack/shadow-projection-$(date +%F).jsonl \
  --out reports/shadow-report-$(date +%F).md
```

Do not enable live projected memory until answer-aware reports pass with zero critical used-missing entries.
