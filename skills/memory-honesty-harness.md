---
name: memory-honesty-harness
description: "Phase B honesty harness for the memory projection engine. Compares PROJECTED vs FULL memory injection against hand-labelled gold tasks and reports required-context recall, pin survival, and token savings — so projection can be measured honestly (savings is never mistaken for quality). Tier 1 is deterministic and needs no model/API/network."
version: 1.0.0
triggers:
  - memory harness
  - projection honesty
  - does projection lose context
  - measure projection quality
  - projection recall
  - pin survival test
  - projection regression
metadata:
  hermes:
    tags: [memory, projection, harness, evaluation, recall, pins, token-budget, read-only, no-llm]
---

# Memory Projection Honesty Harness — Phase B (Tier 1)

The projection engine (`memory_project.py`, see `skills/memory-projection.md`) claims
it shrinks per-turn memory overhead **without losing the context a task needs**. That
is a claim about *answer quality*, and a token-savings number does not prove it — a
projection that saves 90% of the tokens by dropping the one entry the task needed is
**worse**, not better. This harness keeps that claim honest.

Script: `scripts/memory_harness.py` (stdlib only; no LLM, no network).
Fixtures: `scripts/memory_harness_tasks.json` (synthetic, hand-labelled gold).
Tests: `tests/test_memory_harness.py`.

It runs each representative task two ways and grades the result against a
hand-labelled gold standard, answering the five Phase-B questions per task:

1. Does projection include the entries the task actually needs? — **required recall**
2. Do pinned safety/identity/operational rules survive projection? — **pin survival**
3. How many tokens does projection save vs full injection? — **savings**
4. Where does projection MISS needed context, and is it reported? — **findings**
5. Can all of this run with no model/API/network? — **Tier 1**

## Two tiers

| | Tier 1 (this module, default) | Tier 2 (optional, gated, not shipped here) |
|---|---|---|
| What it measures | required-context **recall**, pin survival, savings | actual **answer quality** (full vs projected) |
| How | deterministic; gold labels; lexical query proxy | a model reads each block and answers the task |
| Needs a model/network? | **No** | Yes (bring your own grader) |
| Runs in CI / unit suite? | **Yes** | No |

Tier 2's seam is defined in code (`AnswerGrader` / `NullGrader`) but **not implemented**
— Tier 1 never imports a model, so the default tests need no API key. To add Tier 2,
implement `AnswerGrader.grade(task, full_block, projected_block)` behind an explicit flag.

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# Human-readable markdown summary (default)
python3 scripts/memory_harness.py

# Full structured report
python3 scripts/memory_harness.py --json > /tmp/harness.json

# Static-only (evaluate the no-query fallback config); strict (WARN -> exit 1)
python3 scripts/memory_harness.py --mode static
python3 scripts/memory_harness.py --strict
```

Exit code: **0** if no task FAILs (WARN allowed), **1** if any FAIL (or any WARN under
`--strict`), **2** on a usage/fixture error.

## How to read the result (and how NOT to)

**Status = pin survival + required-context recall in the gating mode. Token savings
NEVER decides status.** A task that saves tokens but drops a needed entry FAILs.

- **Gating mode.** The engine ships *query-aware* (Phase 2b) and the live integration
  passes the current turn as the query, so when both modes run, **`lexical` (query-aware)
  gates** and **`static` (the no-query fallback used if the semantic index is down) is a
  reported baseline**. `--mode static` makes static the gate (you asked to evaluate the
  fallback).
- **PASS** — all pins survived and gating-mode required recall is 100%.
- **WARN** — a required entry was missed (`floor ≤ recall < 100%`), or the budget is too
  small to hold pins+required (a *config* issue, labelled as such), or a pin survived but
  under the wrong class.
- **FAIL** — a pin was dropped, or gating-mode recall fell below the floor (default 50%),
  or a fixture is invalid.

What the columns mean:

```
Req-recall   fraction of gold-required entries that survived projection (the quality proxy)
Missing      which required entries were dropped (the honest "where it misses")
Savings      (1 − projected/full) × 100  — reported, NOT graded
Precision    of the non-pinned selections, how many were gold-required (advisory)
Pins         ✓ all expected pins survived budget=0 & classified correctly / ✗ dropped
```

**Do not read a green/savings number as "projection is safe."** Read it together with
recall. The harness deliberately separates the two so they cannot be conflated.

## Honesty principles (enforced in code — see the tests)

- **H1 — graded against gold, not against itself.** Recall is measured against a
  hand-labelled gold set, never "it kept what it kept."
- **H2 — relevance is gold-blind.** The Tier-1 query proxy (`lexical_relevance_hits`) is
  handed only entry *text* + *ref*, never the required/pin flags, so recall cannot be
  inflated by leaking the answer key into retrieval.
- **H3 — savings never upgrades status.** Tested: a task with high savings but a dropped
  required entry still FAILs.
- **H4 — fixtures self-validate.** Every required entry must be selectable at unlimited
  budget, or the task FAILs as `fixture-invalid` (a mislabelled gold set cannot pose as
  an engine result).
- **H5 — pins probed at budget=0.** A pin only "survives" because the pin mechanism
  protected it, not because it scored well at the task's budget.
- **H6 — deterministic.** Fixed date + synthetic fixtures + a pure lexical function →
  byte-identical output across runs (CI-able).

## What the shipped fixtures show

Six synthetic tasks (Hermes troubleshooting, NCLEX, trading/project recall, design
recall, user-preference recall, safety/API-key guardrails). On the default run:

- **All pins survive** budget=0 and are correctly classified (safety/operational), across
  every task. This is the core safety result — **but pins surviving is not the whole
  safety story.** In the static (no-query) fallback, the `safety-leaked-api-key` task's
  three safety pins all survive yet its *required incident runbook* (a non-pin) is dropped
  (0% recall): a degraded-index turn keeps the guardrails but loses the response procedure.
  That is exactly the static-fallback recall risk Phase C item (2) below must decide on.
- **Query-awareness recovers recall the static fallback loses**: mean required-recall
  rises from ~42% (static) to ~83% (query-aware). The `hermes-telegram-poller` task is
  the clearest case — static drops both episodic entries (0%), query-aware recovers both
  (100%). The static ablation column FAILs two tasks (telegram and safety) precisely on
  the episodic/incident cases that matter most — the fallback config is materially weaker.
- **`design-landing-redesign` is a positive control** that passes in *both* modes (the
  required entries are durable + concise and the noise is genuinely low-value), proving
  the harness is not rigged to always WARN.
- **Two honest WARNs**: `nclex-pharm-rationale` (the weak lexical proxy under-ranks a
  required entry that shares few query words) and `trading-origin-candidate-v3` (at a
  tight budget, several cheap high-value off-topic entries out-score one required entry
  on the blended score). Both are reported with the specific missed entry.

Overall: **WARN** (exit 0). That is the honest verdict — projection preserves required
context + all pins on most representative tasks, with two diagnosed query-proxy
weaknesses that the stronger real semantic model (and Tier 2) should improve on.

## Limitations (read before quoting any number)

- **The Tier-1 `lexical` proxy is token overlap, NOT the shipped embedding model.** It is
  intentionally weak: it misses paraphrases the real index would catch and rewards
  shallow word overlap. Treat Tier-1 recall as an **approximate floor**, not production
  retrieval quality. The honest upgrade is Tier 2.
- **Recall against a gold set is a PROXY for answer quality** — necessary, not sufficient.
  "The needed entry was present" does not prove the answer was good. Only Tier 2 grades
  the actual answer.
- **Fixtures are synthetic and few** — representative and adversarial, not exhaustive.
  Passing means "did not regress on these cases," not "correct on all real memory."
- **Tight budgets favour many cheap entries over one long required one** (the knapsack
  maximises blended score per token). A concrete-but-numeric entry can also score low on
  the audit's *durability* dimension and be dropped. The harness surfaces these; tuning
  the relevance weight / reserve threshold (in `memory_project.py`) is a separate lever.

## Adding or editing tasks

Each task in `memory_harness_tasks.json`:

```jsonc
{
  "id": "unique-id",
  "category": "free-text",
  "query": "the live user turn (drives lexical relevance; omit for a static-only task)",
  "budget_tokens": 150,                 // set ≥ pins+required so you test SELECTION
  "memory": [                           // becomes MEMORY.md (USER.md via "user": [...])
    {"text": "...", "pin": "operational", "label": "notes-header"},  // expected pin class
    {"text": "...", "required": true,  "label": "the-gold-entry"},   // must be recalled
    {"text": "...", "noise": true,     "label": "a-distractor"}      // competes for budget
  ],
  "identity_extra": "Name"              // optional: enable an identity pin without shipping names
}
```

Rules the loader enforces: unique `id`, non-empty entry `text`, textually-unique entries
(content-hash join), valid `pin` class, and at least one `required` or `pin` per task.
**Label gold honestly** — `required` means a good answer genuinely needs it. Do **not**
tune `query` to game the proxy; set realistic budgets and report what happens.

Two automated guards push back on careless/circular gold authoring (the `required`/`noise`
labels are otherwise author-trust): (1) the H4 self-check rejects a gold set that is
unselectable even at unlimited budget; (2) a **gold-uncontested advisory** fires when every
required entry survives *and* no labelled distractor was dropped — meaning the budget never
forced a contested choice, so the task asserts little. If you see that advisory, tighten the
budget so the task actually stresses projection. (Caveat: these are heuristics, not proof —
a determined author can still mislabel gold; the harness measures the engine, it does not
police the fixture author.)

## Files

- `scripts/memory_harness.py` — Tier-1 engine + CLI; `AnswerGrader`/`NullGrader` Tier-2 seam.
- `scripts/memory_harness_tasks.json` — synthetic hand-labelled task fixtures.
- `tests/test_memory_harness.py` — proxy gold-blindness, fixture validation, stable
  outcomes, pin survival, and the "fails loudly" suite (dropped required/pin, savings
  never rescues, budget-impossibility attribution).
- Engine under test: `scripts/memory_project.py` (`skills/memory-projection.md`).

## Phase C handoff (not done here)

This harness measures projection in isolation. Phase C is wiring projection into live
Hermes prompt assembly. Before that: (1) add Tier 2 to confirm recall→answer-quality on a
handful of real-shaped tasks; (2) decide the production budget + whether the live turn is
always available as a query (and the static-fallback recall you accept when it is not);
(3) keep this harness green in CI as the regression gate.
