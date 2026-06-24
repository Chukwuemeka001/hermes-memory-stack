# Memory Auto-Extraction Plan (Mem0-style)

## Goal
Automatically extract facts, preferences, and corrections from Hermes conversations — without the user or agent explicitly saying "remember this."

## Current Gap
MEMORY.md intake is manual or curator-driven. If Emeka casually mentions "I prefer X over Y" or corrects the agent ("no, I use dark roast"), it's lost next session unless explicitly saved.

## Research Findings

### Mem0's Approach
Two-pass LLM pipeline:
1. **Pass 1 — Fact Extraction:** System prompt instructs LLM to extract atomic facts from conversation. Output: `{"facts": ["fact1", "fact2", ...]}`. Uses a "FACT_RETRIEVAL" prompt that asks for self-contained, complete statements.
2. **Pass 2 — Dedup/Action:** Each extracted fact is compared against existing memories. LLM assigns one of: `ADD` (new fact), `UPDATE` (existing fact needs revision), `DELETE` (fact is now false), `NOOP` (duplicate or irrelevant).

No explicit confidence scores in open-source Mem0. Filtering is done via:
- Prompt discipline ("only extract facts the user explicitly stated")
- Semantic dedup (don't add if similar fact exists)
- Atomic extraction (one fact per item, not summaries)

### Risk: Noise
Mem0's biggest criticism is false positives — extracting "facts" from hypotheticals, jokes, context-dependent statements. Our curated pointer policy exists to prevent this.

## Implementation: Nightly Extraction Pass

### Architecture
```
Session transcripts (JSONL) 
  → Pre-filter (signal words, minimum turns)
  → LLM extraction (Haiku — cheap, fast)
  → Dedup against existing MEMORY.md + notes
  → Confidence filter (rule-based + LLM)
  → Append to MEMORY.md or write to notes/
```

### Key Design Decisions
1. **Nightly batch, not real-time** — reduces noise risk, batches cost, allows curator to review
2. **Use cheapest model** — Haiku or local Phi-4 for extraction (not Opus)
3. **5 facts/session cap, 10 facts/night cap** — prevents flooding
4. **2-turn minimum threshold** — skip very short sessions
5. **Rule-based pre-filter** — look for signal words before calling LLM:
   - Correction signals: "actually", "no, I", "I don't", "wrong", "correction", "prefer", "I use", "switch to"
   - Preference signals: "I like", "I want", "my preference", "always", "never"
   - Identity signals: "my name", "I work", "I live", "I'm a"
6. **No new infrastructure** — string overlap for dedup, MEMORY.md for storage
7. **Append-only** — never rewrite existing MEMORY.md lines, only append new pointers

### Extraction Prompt
```
You are a fact extractor. Given a conversation between a user and an AI assistant, 
extract ONLY facts that:
1. The user explicitly stated (not inferred or hypothetical)
2. Are durable (will still be true in 30+ days)
3. Are personal to this user (preferences, corrections, identity, workflow)
4. Are NOT: task progress, session outcomes, temporary state, jokes, hypotheticals

Output JSON: {"facts": ["fact1", "fact2"]}
If no durable facts found: {"facts": []}
Maximum 5 facts. Each fact must be a single, self-contained sentence.
```

### Dedup Prompt
```
New fact: "{new_fact}"
Existing memory: "{existing_memory}"
Are these the same or conflicting? 
Reply: NEW (add it), UPDATE (replace with new), DUPLICATE (skip), CONFLICT (flag for review)
```

### Integration Points

1. **Session collection** — read session JSONL files from `~/.claude/projects/` or `~/.hermes/sessions/`
2. **Pre-filter** — Python script scans for signal words, skips sessions < 2 turns
3. **Extraction** — LLM call with extraction prompt
4. **Dedup** — Compare against current MEMORY.md content (string overlap + LLM for ambiguous cases)
5. **Write** — Append to MEMORY.md with pointer format: `§ <fact> → source: session <id>, <date>`
6. **Report** — Log what was extracted for nightly delivery (Telegram notification)

### Config Addition
```yaml
# ~/.hermes/config.yaml
memory:
  auto_extract:
    enabled: true
    model: auto           # use cheapest available
    max_facts_per_session: 5
    max_facts_per_night: 10
    min_session_turns: 2
    signal_words:
      - "actually"
      - "I prefer"
      - "I don't"
      - "I use"
      - "switch to"
      - "correction"
      - "wrong"
      - "always"
      - "never"
      - "my preference"
```

### Implementation Steps

1. **Write the extraction script** (`~/.hermes/scripts/memory_auto_extract.py`)
   - Collect today's sessions (JSONL files modified in last 24h)
   - Pre-filter by signal words + minimum turns
   - Call LLM with extraction prompt
   - Call LLM with dedup prompt for each fact vs existing MEMORY.md
   - Output: list of facts to add (dry-run mode first)

2. **Test with dry-run** — run the script, review extracted facts, validate quality
3. **Wire into dreaming cron** — add as a step in the nightly dream cycle
4. **Add Telegram notification** — "3 new facts extracted tonight: [list]"
5. **Monitor for 1 week** — check false positive rate, adjust signal words and caps

### Phase 1: Manual Dry-Run (NO writes)
Run extraction on last 7 days of sessions. Output to stdout. Emeka reviews quality. Adjust prompt/filters based on results. This validates the approach before any automated writes.

### Phase 2: Nightly Automated (with notification)
Wire into dreaming cron. Extract → dedup → append → notify. Emeka can review and revert.

### Phase 3: Confidence Scoring
After Phase 2 runs for 2+ weeks, analyze false positive rate. If high, add LLM-based confidence scoring. If low, increase caps.

## Effort Estimate
- Phase 1: 2-3 hours (script + prompt + dry-run)
- Phase 2: 1 hour (cron wiring + notification)
- Phase 3: 2 hours (confidence scoring if needed)
