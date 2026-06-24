---
name: memory-audit
description: "Area 2 of memory-stack onboarding — read-only quality audit of MEMORY.md/USER.md: parse § entries, classify, score, recommend per-entry actions, detect duplicates/contradictions/broken pointers. Never mutates."
version: 1.0.0
triggers:
  - memory audit
  - MEMORY.md quality
  - hot memory cleanup
  - memory bloat
  - duplicate memory entries
  - memory capacity
metadata:
  hermes:
    tags: [memory, audit, quality, pointers, duplicates, onboarding, read-only]
---

# MEMORY.md / USER.md Audit (Area 2)

The second onboarding step. After `state.db` is cleaned (Area 1), this assesses
the **hot memory** files — the §-delimited `MEMORY.md` (hot pointers) and
`USER.md` (durable preferences) injected into every turn. It produces a
per-entry quality report and recommended actions so Area 3 (pointer rewrite) has
a reviewed work-list.

> **Read-only.** `memory_audit.py` never rewrites `MEMORY.md`/`USER.md`. The only
> write is an explicit `--out` report path. It is the batch / read-time
> counterpart to the write-time `hermes_memory_intake_gate.py`.

Script: `scripts/memory_audit.py` (stdlib only; no LLM, no network).

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# audit the default hot files (read-only) -> markdown
python3 scripts/memory_audit.py

# machine-readable, written to a file
python3 scripts/memory_audit.py --json --out /tmp/mem-audit.json

# explicit files + stricter thresholds
python3 scripts/memory_audit.py --memory ~/.hermes/memories/MEMORY.md \
    --user ~/.hermes/memories/USER.md --strict
```

## CLI

| Flag | Default | Meaning |
|---|---|---|
| `--home` | `$HERMES_HOME` or `~/.hermes` | locates default `memories/MEMORY.md` + `USER.md` |
| `--memory` / `--user` | `<home>/memories/…` | explicit file paths |
| `--user-home` | real `$HOME` | base for resolving `~/` paths during existence checks (override for tests) |
| `--json` / `--markdown` | markdown | output format (default markdown; `--out` infers from extension) |
| `--out PATH` | — | write the report here (the only write this tool makes) |
| `--max-entry-chars` | 350 | flag entries longer than this as "long" for a hot pointer |
| `--strict` | off | tighter duplicate + length thresholds (more findings) |
| `--stale-after-days` | 30 | dated entries older than this raise staleness risk |
| `--semantic` | off | **also** run embedding-backed near-dup detection via the semantic daemon (INTEG-9); falls back to token Jaccard with a warning if the daemon is down |
| `--semantic-cosine` | 0.85 | cosine threshold for a semantic near-duplicate |
| `--semantic-prefilter` | 0.30 | only embed/compare pairs with token Jaccard above this (cheap pre-filter) |

## What it checks

**Per entry — classification (`kind`):** `header`, `pointer`, `preference_fact`,
`content_dump`, `status_update`, `debugging_finding`, `project_progress`,
`todo_temporary`, `malformed`.

**Per entry — six 0..1 scores:** durability, hot-memory fit, pointer quality,
specificity/actionability, staleness risk, and a blended overall quality.

**Per entry — one recommended action:** `keep`, `rewrite_to_pointer`,
`archive_to_note`, `merge`, `verify_current`, `move_to_skill`, `move_to_note`,
`remove_after_archive`, `user_review`.

**Cross-references (conservative, to avoid false positives):**
- **Broken pointers** — only paths that clearly name a *file* (known extension)
  are existence-checked; directories and ambiguous/space-truncated tokens are
  never flagged. `~/` resolves against the real OS home.
- **Near-duplicates** — deterministic token-Jaccard over a normalized
  bag-of-words (dates/paths normalized, archived-pointer boilerplate dropped so
  distinct `↪` pointers don't collide). Reports pairs + a `merge` on the weaker.
  With **`--semantic`** (INTEG-9), pairs with weak lexical overlap (token Jaccard
  above `--semantic-prefilter`) are additionally checked by **embedding cosine**
  via the warm semantic daemon: pairs at cosine ≥ `--semantic-cosine` are reported
  under `semantic_duplicates` (with an `added_over_token` count of finds the token
  pass missed). The caller stays pure-stdlib — it asks the daemon to embed over the
  socket. If the daemon isn't running, the audit warns and uses token Jaccard only;
  `--semantic` is a strict enhancement, never required.
- **Possible contradictions** — only obvious `default`-vs-`default` and
  `enabled`-vs-`paused` conflicts between entries sharing enough subject tokens.
  Labelled `possible_contradiction` and routed to `user_review` — **never
  asserted as truth**.

**File level:** char count vs budget (`MEMORY.md` 15000, `USER.md` 6000),
capacity % with WARNING/CRITICAL flags, entry count vs the 25-target / 35-ceiling
intake policy, lowest-quality entries, dup clusters, contradictions, broken
pointers.

## Safety & philosophy

- Read-only; input file SHA-256 is recorded and never changes (a test asserts
  this by hashing before/after).
- Store-aware: `USER.md` durable preferences are allowed to be fuller paragraphs;
  the audit will not tell you to delete a useful preference just for length.
- Deterministic: same input → same report. No model, no network, no spend.
- This audit only *recommends*. Nothing is rewritten until Area 3, which acts on
  this report (dry-run first, archive-first).

## Verify

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/memory_audit.py
python3 scripts/memory_audit.py --help
python3 -m unittest tests.test_memory_audit -v    # 38 tests, synthetic files only
```

## Related

- `hermes_memory_intake_gate.py` — write-time gate (the per-candidate counterpart).
- `state-db-remediation` skill — Area 1 (runs before this).
- `plans/memory-onboarding-remediation.md` — Areas 1–5.
- Next: **Area 3 — pointer rewrite & consolidation** (acts on this report).
