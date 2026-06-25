---
name: memory-projection
description: "Memory Projection Engine (Phase 1) — given a token budget, select the highest-value MEMORY.md/USER.md entries that fit, via a multi-factor relevance score + 0/1 knapsack. Replaces brute-force 'inject everything' with budget-aware projection (~60% fewer tokens/turn). Read-only."
version: 1.0.0
triggers:
  - memory projection
  - token budget memory
  - reduce memory tokens
  - context budget
  - what to inject
  - memory knapsack
  - per-turn token overhead
metadata:
  hermes:
    tags: [memory, projection, knapsack, token-budget, injection, context, read-only]
---

# Memory Projection Engine — Phase 1 (Budget-Aware Static Projection)

Every other tool in this stack *cleans* memory. This one makes injection
**intelligent**: instead of dumping all of `MEMORY.md` + `USER.md` into the system
prompt every turn (~4,800 tokens on a real profile), it **projects** the
highest-value entries that fit a **token budget** (typically 500–2,000 tokens).

> **The core insight (deep research, 2026):** context management is a *knapsack
> problem*. Items are memory entries, value is relevance, weight is token cost,
> capacity is the budget. Confirmed independently by Welihinda ("Managing LLM
> Context Is a Knapsack Problem"), LongCodeZip, SWE-Pruner, Entroly, and TREACLE.

> **Read-only.** `memory_project.py` never modifies `MEMORY.md`, `USER.md`, or the
> temporal DB. It prints the projected block (or a `--json` scoring report) to
> stdout, ready to inject. It opens the temporal DB read-only and closes the
> handle immediately (safe to call every turn).

Script: `scripts/memory_project.py` (stdlib only; no LLM, no network).
Shadow telemetry: `scripts/memory_shadow.py` (computes full-vs-projected but keeps FULL active).
Semantic fallback: when Hermes runs under a lean Python 3.11 agent venv without ChromaDB, `memory_project.py` calls `memory_entry_index.py search` through `HERMES_SEMANTIC_PYTHON` / `python3.14` and reports `via subprocess:python3.14` in `relevance_source`.
Reuses `memory_audit.py` (scoring dimensions) and `temporal_memory.py` (recency).

## Why this is the highest-leverage feature

| | Brute-force injection | Budget-aware projection |
|---|---|---|
| Tokens/turn (real profile) | ~4,731 | ~1,976 (budget 2,000) |
| Savings | — | **~58%** |
| Over 100 turns | ~473,000 tokens | ~198,000 tokens |
| What's injected | everything, every turn | the entries that matter, ranked |

The savings compound. A 60% reduction on a 100-turn conversation saves ~225K
tokens of pure memory overhead — paid every conversation, forever.

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# Project to a 2,000-token budget → the block, ready to inject
python3 scripts/memory_project.py --home ~/.hermes --budget 2000

# Full scoring report: what was kept/skipped and WHY
python3 scripts/memory_project.py --home ~/.hermes --budget 2000 --json > /tmp/projection.json

# Deterministic run (pin the date so recency decay is reproducible)
python3 scripts/memory_project.py --home ~/.hermes --budget 1500 --today 2026-06-24
```

## How scoring works

Each entry gets a **projection score** in `[0, 1]` — a weighted blend of five
factors. The non-`always_inject` factors are **reused from `memory_audit.py`** so
there is one source of truth and no drift (the INTEG-8 lesson).

```
score = importance    * 0.25    # audit: (durability + pointer_quality) / 2
      + recency       * 0.15    # temporal: recorded_at, exp-decayed (30d half-life)
      + specificity   * 0.15    # audit: specificity_actionability
      + hot_fit       * 0.10    # audit: hot_memory_fit (ideal length for a pointer)
      + always_inject * 0.15    # binary: first-class pin / mandatory entry
      + relevance     * 0.20    # optional --query semantic relevance
```

- **importance** — fuses *durability* (is this a standing fact, or a status blip
  that's already aging?) with *pointer_quality* (does it point to something real?).
- **recency** — the most recent effective date of the entry's current version in
  the **temporal layer** (`eff_valid_from`/`recorded_at`), exponentially decayed:
  `0.5 ** (age_days / half_life)`, default half-life 30 days. 1.0 today, 0.5 at 30
  days, 0.25 at 60, never quite 0. **Graceful fallback:** no temporal record → the
  most recent date written in the entry text → a neutral default (0.5) if neither.
  The JSON report's `recency_breakdown` shows where each entry's recency came from.
- **specificity** — concrete (paths, identifiers, numbers) vs vague.
- **hot_fit** — how close the entry is to the ideal length for a hot pointer
  (long content dumps are penalised; they belong in a note, pointed to).
- **always_inject** — `1.0` if the entry's **topic** matches an operational
  ALWAYS_INJECT pattern (see below), else `0.0`.

### ALWAYS_INJECT — the mandatory set

A small, **high-precision** set the agent needs *every turn regardless of task*:

- the **notes-system header** (the navigation root — drop it and the agent can't
  find any long-form note), and
- entries whose **topic** (leading label, before the first colon) matches
  operational config: `routing`, `provider failover`, `failover automation`,
  `restart protocol`, `model routing`, `provider config`, `fallback sequence`,
- explicit **safety guardrails** such as API-key policy, live execution/trade
  prohibitions, or “do not connect/place/deploy/enable” rules, and
- install-local **identity topics** derived from `--user-home` (plus optional
  `--identity-extra REGEX`), without hardcoded personal names in the package.

Matching is deliberately narrow: operational and identity pins match the **topic**;
safety pins match imperative guardrail phrasing in the full entry. Generic words
like “credential”, “people”, “manager”, or a bare “payment UI” do not pin an entry.
Live reports expose `pinned_count`, `mandatory_tokens`, and `pin_breakdown`; on the
dogfood default profile before Slice-0 tightening, pins were 11 / 607 tokens, which
was too high. Keep this set tight. A bloated mandatory set defeats the budget.
Add per-install operational patterns with `--always-inject-extra REGEX`.

## How knapsack selection works

This is a **0/1 knapsack**: maximise total projection score subject to
`Σ token_weight ≤ budget`.

1. **Token weight** per entry = `ceil(chars / 4)` + 1 (the `\n§\n` join overhead).
   The `chars/4` estimate is accurate to ~±10% for short English entries and
   needs no `tiktoken` dependency. Both the original and the projection are
   measured the same way, so `savings_pct` is a fair comparison.
2. **Mandatory** (ALWAYS_INJECT) entries are pre-included; their weight is
   subtracted from the budget to get the **remaining capacity**.
3. The **optional** entries are selected by exact **dynamic programming** over the
   remaining capacity. Entry counts are tiny (≤35/file by intake policy), so the
   `O(n·W)` table is trivial; a fast path returns all entries when everything fits.
4. The result is **deterministic** — fixed iteration order, integer-scaled values
   (no float drift). Same input → byte-identical output.

The join-overhead token in the weight upper-bounds the rendered block, so
`Σ weights ≤ budget` **guarantees** the rendered projection is `≤ budget`
(proved in `tests/test_memory_project.py::test_projection_never_exceeds_budget`).

If the mandatory set alone exceeds the budget, all mandatory entries are still
injected and the report flags `over_budget: true` (by design — they're mandatory).

## JSON report fields

```
budget_tokens          requested budget
projected_tokens       tokens actually used (≤ budget when budget ≥ mandatory)
original_tokens        tokens if everything were injected (brute force)
entries_total          entries considered
entries_selected       entries that made the cut
entries_skipped        entries that didn't fit
savings_pct            (1 − projected/original) × 100
always_inject_count    mandatory entries
original_memory_chars  chars of the full §-joined memory (before)
projected_memory_chars chars of the projected block (after)
over_budget            true if mandatory alone exceeds the budget
recency_source         where recency came from (e.g. "temporal: 49 current facts")
recency_breakdown      per-source counts (temporal:hash / temporal:key / text-date / neutral)
per_entry[]            {entry_ref, store, kind, score, tokens, chars, selected,
                        always_inject, pin_class, relevance_reserved,
                        recency_source, relevance_source, components, reason, preview}
pinned_count           entries hard-pinned outside retrieval/budget gates
pin_breakdown          counts by class: safety / identity / operational / none
relevance_source       disabled:no-query or memories-index:N hits
relevance_breakdown    per-match-source counts (content_hash / entry_ref / none)
projected_block        the rendered block, ready to inject
```

## Shadow-mode dogfood before live injection

Before replacing live prompt memory with `projected_block`, run shadow mode:

```bash
python3 scripts/memory_shadow.py --home ~/.hermes \
  --query "current user turn" \
  --budget 1500 \
  --out reports/shadow-projection-$(date +%F).jsonl
```

Shadow mode writes append-only JSONL telemetry and explicitly records:

- `active_block: "full"` — the agent should still answer from full memory.
- `full.tokens` vs `projected.tokens` and projected `savings_pct`.
- `diff.selected_refs` / `diff.skipped_refs`.
- `answer_usage.used_missing_from_projection` if `--answer-file` or `--answer-text` is provided.

Raw memory blocks are **not** logged by default. Use `--include-blocks` only for local debugging because it writes full hot memory into the report.

Only after enough shadow reports show acceptable misses should any runtime lane flip from `full` to `projected`.

## Integrate with the agent runtime

**At injection time**, replace "read `MEMORY.md` verbatim" with:

```bash
MEMORY_BLOCK="$(python3 scripts/memory_project.py --home ~/.hermes --budget 1500)"
# inject $MEMORY_BLOCK into the system prompt instead of the raw files
```

Or in Python:

```python
import memory_project as MP
report = MP.project(home="~/.hermes", budget=1500)
system_prompt_memory = report["projected_block"]   # ready to inject
```

**After onboarding**, `memory_onboard.py --project` prints the savings footer:

```bash
python3 scripts/memory_onboard.py --home ~/.hermes --apply --project --project-budget 2000
#   Memory projection (budget 2000 tokens):
#     Your memory was 4731 tokens. Projected to 1976 tokens (58.2% savings).
#     35/58 entries kept · 5 always-inject · 7904 chars
```

### Choosing a budget

- **500–800**: aggressive — header + config + the few highest-value prefs.
- **1,500–2,000**: balanced (recommended default) — keeps durable prefs and live
  project pointers, drops dated status snapshots.
- **≥ original_tokens**: no-op — everything fits (0% savings).

Watch `over_budget` and `savings_pct` in the report to tune.

## Phase 2 / Phase 3 roadmap

Phase 1 is **static**: scores are query-independent (intrinsic value only). It
proves the concept and delivers the bulk of the savings. Next:

- **Phase 2 — context-aware projection.** **Phase 2a built:** per-entry semantic
  index in `scripts/memory_entry_index.py` stores individual MEMORY.md/USER.md
  entries in a Chroma `memories` collection keyed by `content_hash`/`fact_key`.
  **Phase 2b built:** pass `--query "current user turn"` to `memory_project.py`
  to boost entries semantically close to the live task. The boost is soft and
  gracefully falls back to static Phase 1 if the memory index/model is unavailable.
  The engine also has a budgeted turn-relevant reserve lane so highly relevant
  entries are not squeezed out by cheaper static entries. This turns static
  importance into task-conditioned relevance without an LLM round-trip.
- **Safety/identity/operational pins — built.** `pin_class` hard-pins
  non-negotiables outside retrieval gates: `safety`, `identity`, and
  `operational`. Report `pinned_count` and `pin_breakdown` so the mandatory
  surface is auditable and cannot silently bloat.
- **Phase 3 — feedback loop.** Track which projected entries the agent actually
  *used* (referenced a path, acted on a preference). Up-weight winners, down-weight
  entries that are always injected but never used (cf. Letta's self-editing memory,
  AgentDiet's reflection module). Closes the loop: projection learns from outcomes.

## Files

- `scripts/memory_project.py` — the engine + CLI (`--query` enables Phase 2b relevance).
- `scripts/memory_entry_index.py` — per-entry semantic index/search for MEMORY.md/USER.md.
- `tests/test_memory_project.py` — tests (knapsack optimality, recency decay,
  relevance boost/fallback, budget guarantee, determinism, schema, CLI).
- `tests/test_memory_entry_index.py` — per-entry index/search tests.
- Integration: `memory_onboard.py --project` (savings footer after onboarding).
