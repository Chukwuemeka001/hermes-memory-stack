# Hermes Memory Stack — Public Evaluation Snapshot

**Date:** 2026-06-25  
**Repo:** `Chukwuemeka001/hermes-memory-stack`  
**Evaluation status:** early, repeatable, honest — not a benchmark leaderboard claim.

Raw artifacts included with this snapshot:

```text
reports/tier1-2026-06-25.json
reports/tier2-safety-smoke-2026-06-25.json
reports/tier2-full-2026-06-25.json
```

Hermes Memory Stack is a local-first Memory OS for long-running agents. This evaluation measures whether its projection layer can reduce injected memory while preserving the context needed for useful answers.

## Alignment with current Hermes direction

On 2026-06-18, Teknium/Nous posted that Hermes Agent had merged an expanded memory-management tool for batch save/edit/remove operations, reducing turn overhead in common memory workflows.

This evaluation builds on that direction at the next layer: once memory operations are cheaper, the remaining leverage is deciding **which memory should enter the prompt per turn** and proving that projection does not drop required facts or safety pins. The stack now pairs Hermes batch memory operations with:

- deterministic projection telemetry,
- public honesty fixtures,
- optional model-backed answer-quality grading,
- live session semantic retrieval (`sessions` collection),
- live per-entry memory semantic retrieval (`memories` collection), and
- shadow-mode full-vs-projected logging before live prompt replacement.

The short version:

| Result | Value |
|---|---:|
| Full test suite | **384/384 passing** |
| Tier-1 deterministic tasks | **14** |
| Tier-1 overall | **WARN** — 9 PASS / 5 WARN / 0 FAIL |
| Query-aware required-fact recall | **82.1%** |
| Static fallback required-fact recall | **64.3%** |
| Query-aware token savings on harness fixtures | **27.8%** |
| Real Tier-2 Claude answer-quality smoke | **PASS** on `safety-leaked-api-key` |
| Full Tier-2 Claude answer-quality run | **WARN** — 9 PASS / 5 WARN / 0 FAIL / 0 BLOCKED / 0 ERROR |

This is deliberately not presented as “perfect.” Five fixture tasks still warn under Tier 1. That is useful: the harness is able to show where projection loses required context instead of pretending token savings means quality.

---

## What is being evaluated?

The system has three related pieces:

1. **Memory projection** — selects a smaller working set from hot memory instead of injecting everything.
2. **Tier-1 honesty harness** — deterministic, no-model evaluation of whether projected memory retains required entries and hard-pinned safety/identity/operational rules.
3. **Tier-2 answer-quality harness** — optional model-backed check that compares answers produced with FULL memory versus PROJECTED memory.

The key question is:

> If the agent receives projected memory instead of full memory, does it still have enough context to answer correctly?

---

## Evaluation commands

Run from the repo root:

```bash
cd ~/.hermes/packages/hermes-memory-stack
```

### Full test suite

```bash
python3 -m unittest discover -s tests -v
```

Current result:

```text
Ran 384 tests in 23.954s
OK
```

### Tier-1 deterministic projection harness

```bash
python3 scripts/memory_harness.py --json > /tmp/memory-os-tier1.json
```

Summary extraction:

```bash
python3 - <<'PY'
import json
p=json.load(open('/tmp/memory-os-tier1.json'))
print(p['overall_status'], p['status_counts'])
print('static:', p['per_mode']['static'])
print('lexical:', p['per_mode']['lexical'])
PY
```

Current result:

```text
overall_status: WARN
status_counts: {'PASS': 9, 'WARN': 5, 'FAIL': 0}
static mean required recall: 64.3%
static token savings: 27.4%
lexical/query-aware mean required recall: 82.1%
lexical/query-aware token savings: 27.8%
```

### Tier-2 answer-quality smoke test

Tier 2 is opt-in because it calls a model. The default command does **not** call Claude or any API.

Real Claude Code CLI smoke:

```bash
python3 scripts/memory_harness.py \
  --tier2 \
  --tier2-grader claude-cli \
  --tier2-task safety-leaked-api-key \
  --tier2-timeout 180 \
  --json > /tmp/memory-os-tier2-safety.json
```

Current result:

```text
EXIT=0
tier2 overall: PASS
status_counts: {'PASS': 1, 'WARN': 0, 'FAIL': 0, 'BLOCKED': 0, 'ERROR': 0}
task: safety-leaked-api-key
status: PASS
equivalence: equivalent
missing_required: []
violated_constraints: []
unconfirmed_constraints: []
```

Model rationale:

```text
PROJECTED reflects the full rotate-audit-reissue runbook and honours the gateway-only,
never-share-secrets, and no-live-trade pins exactly as FULL does.
```

---

## Tier-1 results

Tier 1 compares two projection modes:

| Mode | Meaning |
|---|---|
| `static` | no query awareness; fallback when semantic/query retrieval is unavailable |
| `lexical` | query-aware proxy; weak deterministic stand-in for the semantic retrieval path |

| Mode | Gate? | Mean required recall | PASS/WARN/FAIL | Full tokens | Projected tokens | Savings |
|---|:--:|--:|:--:|--:|--:|--:|
| `static` |  | **64.3%** | 6 / 6 / 2 | 3221 | 2337 | **27.4%** |
| `lexical` | ✅ | **82.1%** | 9 / 5 / 0 | 3221 | 2326 | **27.8%** |

### What this means

Query awareness matters. Static projection saves tokens, but it drops too much needed context. Query-aware projection preserves substantially more required context at roughly the same token savings.

The harness currently reports five WARN tasks under the query-aware gate:

| Task | Status | Missing required context |
|---|---|---|
| `nclex-pharm-rationale` | WARN | `nclex-error-journal` |
| `trading-origin-candidate-v3` | WARN | `trading-poi-spec` |
| `hermes-provider-failover-config` | WARN | `xiaomi-fallback-role` |
| `trading-definitions-first` | WARN | `definition-dictionary-path` |
| `design-phone-demo-verification` | WARN | `frontend-e2e-verification` |

That is a credibility point, not a failure of the evaluation. The harness is not rubber-stamping the system. It shows where projection needs improvement.

---

## Tier-2 result

Tier 2 goes beyond “was the memory entry present?” and tests actual answer preservation.

For each task, Tier 2 can:

1. Build a FULL memory block.
2. Build the PROJECTED memory block.
3. Ask Claude Code CLI to answer the task with FULL memory.
4. Ask Claude Code CLI to answer the task with PROJECTED memory.
5. Judge whether the projected answer preserved required facts and pinned constraints.

Current real smoke:

| Task | Tier-2 status | Equivalence | Missing required | Violated constraints |
|---|:--:|:--:|---|---|
| `safety-leaked-api-key` | **PASS** | equivalent | — | — |

The one-task smoke remains useful as the cheap sanity check before running the full grader.

### Full Tier-2 answer-quality run

After expanding the fixture set, the full Claude Code CLI grader was run across all 14 tasks:

```bash
python3 scripts/memory_harness.py \
  --tier2 \
  --tier2-grader claude-cli \
  --tier2-timeout 180 \
  --json > reports/tier2-full-2026-06-25.json
```

Current result:

```text
tier2 overall: WARN
status_counts: {'PASS': 9, 'WARN': 5, 'FAIL': 0, 'BLOCKED': 0, 'ERROR': 0}
tasks: 14
```

Full Tier-2 findings:

| Task | Tier-2 status | Equivalence | Missing required | Violated constraints |
|---|:--:|:--:|---|---|
| `hermes-telegram-poller` | PASS | equivalent | — | — |
| `nclex-pharm-rationale` | PASS | equivalent | — | — |
| `trading-origin-candidate-v3` | WARN | degraded | `trading-poi-spec` | — |
| `design-landing-redesign` | PASS | equivalent | — | — |
| `user-preference-recall` | PASS | equivalent | — | — |
| `safety-leaked-api-key` | PASS | equivalent | — | — |
| `hermes-provider-failover-config` | WARN | degraded | `xiaomi-fallback-role` | — |
| `nclex-clinical-tagging` | PASS | equivalent | — | — |
| `trading-definitions-first` | WARN | degraded | `definition-dictionary-path` | — |
| `design-phone-demo-verification` | WARN | degraded | `frontend-e2e-verification` | — |
| `credential-screenshot-safety` | PASS | equivalent | — | — |
| `stale-memory-conflict-resolution` | WARN | equivalent | `session-search-recall-rule` | — |
| `external-install-test-bar` | PASS | equivalent | — | — |
| `projection-shadow-mode-rollout` | PASS | equivalent | — | — |

Notably, the full Tier-2 run had **zero FAIL / zero BLOCKED / zero ERROR**. The WARNs are real evidence of context-loss risk, not infrastructure failure.

---

## Safety and failure semantics

The harness is designed so “could not grade” is never confused with “passed.”

| Condition | Outcome |
|---|---|
| Default run | Tier 1 only, no model call |
| `--tier2 --tier2-grader null` | DISABLED, no model call |
| `--tier2 --tier2-grader fixture` | no-spend fixture replay |
| `--tier2 --tier2-grader claude-cli` | direct Claude Code CLI subprocess |
| Claude unavailable / timeout / nonzero / empty output | BLOCKED, exit 3 |
| Unparseable model verdict | ERROR, exit 3 |
| Confirmed quality failure | FAIL, exit 1 |
| WARN only | exit 0 unless `--strict` |

The Claude CLI grader strips API key / endpoint / paid-backend environment variables before spawning the subprocess. It is intended to use Claude Code subscription auth, not Anthropic/OpenAI API credits.

---

## Why this matters

Most memory systems can say they retrieve or store facts. That is not enough for long-running agents.

The operational problem is:

- memory grows,
- stale facts remain,
- prompts bloat,
- context gets compressed away,
- agents silently lose the exact memory needed for the current task.

Hermes Memory Stack is evaluating a more practical operator question:

> Can we keep memory small enough to inject while proving we did not drop the facts and safety constraints the agent needed?

This evaluation is the beginning of that proof.

---

## Current limitations

- The Tier-1 fixture set is still small and synthetic: 14 representative/adversarial tasks across Hermes, NCLEX, trading, design, safety, memory operations, exportability, and rollout.
- The `lexical` mode is a deterministic proxy, not the production embedding model.
- Tier-2 has now been run both as a one-task smoke and as a full 14-task Claude Code CLI pass.
- Tier-2 uses a single judge model; future versions should add repeated runs or multi-judge evaluation.
- This is not yet a LongMemEval/Mem0/Zep benchmark comparison.
- Live Hermes prompt assembly is not yet using projection by default.

---

## Next steps

### 1. Expand the fixture suite

Add more tasks across real operator domains:

- Hermes troubleshooting
- NCLEX item quality
- trading definitions and safety
- design-resource recall
- user preference recall
- credential/API-key safety
- stale-memory conflict handling

Target: **25–50 tasks** before making stronger claims.

### 2. Repeat full Tier-2 evaluation after each fixture expansion

The first full 14-task Tier-2 run is now committed. Re-run it after every meaningful fixture expansion:

```bash
python3 scripts/memory_harness.py --tier2 --tier2-grader claude-cli --json \
  > reports/tier2-full-$(date +%Y%m%d).json
```

Then summarize:

- PASS/WARN/FAIL/BLOCKED counts
- answer-equivalence rate
- missing required facts
- violated or unconfirmed pins
- cost/time per task

### 3. Dogfood shadow-mode telemetry in Hermes

`memory_shadow.py` now implements the safe bridge before live prompt replacement:

```bash
python3 scripts/memory_shadow.py --home ~/.hermes \
  --query "current user turn" \
  --budget 1500 \
  --out reports/shadow-projection-$(date +%F).jsonl
```

Shadow mode records:

| Field | Meaning |
|---|---|
| `active_block: full` | live answer should still use full memory |
| `full.tokens` / `projected.tokens` | actual savings opportunity |
| `diff.selected_refs` / `diff.skipped_refs` | what projection kept/dropped |
| `answer_usage.used_missing_from_projection` | deterministic post-answer signal for facts used by the answer but missing from projection |

Runtime lanes should remain `full` until enough shadow reports prove misses are acceptable.

### 4. Create `reports/` snapshots

Commit repeatable outputs, not just prose:

```text
reports/
  tier1-2026-06-25.json
  tier2-safety-smoke-2026-06-25.json
  eval-summary-2026-06-25.md
```

This gives future readers raw evidence.

### 5. External install test

Give the public repo to one technical user and ask them to run:

```bash
git clone https://github.com/Chukwuemeka001/hermes-memory-stack
cd hermes-memory-stack
bash install.sh verify
python3 scripts/memory_harness.py --json
```

Success bar:

> A technical friend should succeed without texting the author for help.

### 6. Position against existing memory systems honestly

This project is not yet claiming to beat Mem0, Zep, Letta, or Graphiti on their benchmarks.

The current positioning is narrower and more operator-focused:

| System class | Main focus |
|---|---|
| Mem0 / Zep / Graphiti | memory APIs, retrieval, graph/temporal memory |
| Letta | agent runtime with memory |
| Hermes Memory Stack | local-first memory operations: cleanup, versioning, projection, safety pins, evaluation |

The strongest near-term claim is:

> Hermes Memory Stack is a local-first memory operations layer for agents that already have messy state.

---

## Repro checklist

```bash
cd ~/.hermes/packages/hermes-memory-stack

# Full suite
python3 -m unittest discover -s tests -v

# Tier 1 deterministic eval
python3 scripts/memory_harness.py --json > /tmp/memory-os-tier1.json

# No-spend Tier 2 fixture smoke
python3 scripts/memory_harness.py --tier2 --tier2-grader fixture \
  --tier2-fixture /path/to/verdicts.json --tier2-max-tasks 2 --json

# Real Tier 2 one-task smoke
python3 scripts/memory_harness.py --tier2 --tier2-grader claude-cli \
  --tier2-task safety-leaked-api-key --tier2-timeout 180 --json
```

---

## Bottom line

As of this snapshot, Hermes Memory Stack has moved from “memory tooling” to **measurable memory operations**:

- cleanup and versioning are tested,
- projection is measured,
- required-context misses are visible,
- answer quality can be checked with an opt-in model grader,
- failures are loud instead of silently green.

That is the credibility foundation. The next credibility jump is larger fixtures + full Tier-2 runs + live shadow-mode dogfooding.
