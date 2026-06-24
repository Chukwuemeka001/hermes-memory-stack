---
name: memory-rewrite
description: "Area 3 of memory-stack onboarding — consume an Area 2 audit and produce reviewable pointer-rewrite/consolidation proposals + proposed MEMORY/USER files. Dry-run by default; never mutates live; archive-first; no fabricated destinations."
version: 1.0.0
triggers:
  - memory rewrite
  - pointer rewrite
  - consolidate memory
  - condense MEMORY.md
  - merge duplicate memory entries
  - shrink hot memory
metadata:
  hermes:
    tags: [memory, rewrite, pointers, consolidation, onboarding, dry-run, archive-first]
---

# Pointer Rewrite & Consolidation (Area 3)

The third onboarding step. It consumes the **Area 2 audit** (`memory_audit.py`
JSON) and turns each per-entry recommendation into a concrete `old → new`
proposal: condensing content dumps to one-line pointers, consolidating
duplicates, archiving stale status, and flagging anything that needs a human.

> **Dry-run by default; never mutates live.** `plan` writes nothing but its own
> `--out` report. `render` writes only under `--out-dir` (proposed files +
> archived originals + manifest). `apply` exists but **refuses without
> `--confirm-apply`** and archives live originals first.

Script: `scripts/memory_rewrite.py` (stdlib only; reuses `memory_audit` for
parsing/classification — no LLM, no network).

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# 1) dry-run plan from an audit JSON (or --home to run the audit internally)
python3 scripts/memory_rewrite.py plan --audit /tmp/mem-audit.json
python3 scripts/memory_rewrite.py plan --home ~/.hermes --out /tmp/rewrite-plan.json

# 2) render proposals to a directory (originals preserved; live untouched)
python3 scripts/memory_rewrite.py render --audit /tmp/mem-audit.json \
    --out-dir /tmp/memory-proposed

# 3) re-audit the proposed output to confirm it improved + parses
python3 scripts/memory_audit.py --memory /tmp/memory-proposed/MEMORY.proposed.md \
    --user /tmp/memory-proposed/USER.proposed.md
```

## How each audit action is handled

| Audit action | Rewrite behaviour | Recoverable? |
|---|---|---|
| `keep` | byte-for-byte preserved | n/a |
| `rewrite_to_pointer` | condensed to `Topic: summary. Full context: <path>.` **only if a real referenced file exists**; else → review (no fabricated path) | original in manifest (+ archive in render) |
| `archive_to_note` / `move_to_note` / `move_to_skill` | replaced with a `↪ … → archived <date>. Find: …` pointer; original written to an archive file (render) | manifest + archive file |
| `merge` | the lower-quality near-duplicate is absorbed into the higher-quality survivor and dropped from hot memory | survivor's archive + manifest hold the absorbed text |
| `remove_after_archive` | removed from hot memory **only after** the original is archived | manifest + archive file |
| `verify_current` / `user_review` | preserved unchanged, flagged for a human (no truth claim) | n/a (kept) |

**Never-lose guarantee:** every entry that is changed, merged, or removed has its
full ORIGINAL text recorded in the manifest, and (in `render`) written to a file
under the archive dir. Live `MEMORY.md`/`USER.md` are never modified by
`plan`/`render`.

**No hallucinated destinations:** a pointer's `Full context:` path is only ever a
path the entry already referenced. If none exists, the entry is left for review.

**USER.md preferences** are preserved — the audit marks durable prefs `keep`, and
a long preference with no real destination degrades to `review`, never a useless
pointer.

## CLI

| Command | Writes | Notes |
|---|---|---|
| `plan` | only `--out` (refuses a live input path) | JSON (`--json`) or markdown; estimated char reduction |
| `render` | only under `--out-dir` (+ `--archive-dir`) | `MEMORY.proposed.md`, `USER.proposed.md`, `archive/`, `manifest.json` |
| `apply` | **live files** (+ temporal provenance) | refuses without `--confirm-apply`; archives originals first; auto-records provenance (below) |

Inputs (all commands): `--audit FILE` (an audit JSON) **or** `--home` (+
`--memory`/`--user`/`--user-home`) to run the audit internally read-only.

## Automatic temporal provenance (INTEG-3)

When `apply` succeeds **and a temporal layer already exists** for the home, it
automatically replays the rewrite manifest into that layer
(`temporal_migrate_onboard.record_rewrite`), recording each change as a
`source="area3-rewrite"` event on the **same fact key** as the original entry —
preserving the `old → new` provenance chain (and the archive path for removed
entries).

- **Gated by the same `--confirm-apply`** that authorises the live write (the
  recording only runs after the atomic write succeeds).
- **No temporal layer ⇒ skip, don't fabricate.** If neither
  `memories/_versions/history.jsonl` nor `memory_versions.db` exists, `apply`
  prints a one-line note and skips the recording — it never creates a temporal
  layer as a side effect, and the rewrite still succeeds.
- **Never fails the rewrite.** Any error while recording is logged as a warning;
  the (already-applied) rewrite is kept. The outcome is reported in `apply`'s
  output as `temporal provenance: …` and in the returned dict under `temporal`.
- **Idempotent.** Re-running an already-recorded manifest records nothing, so a
  later explicit `temporal_migrate_onboard.py record-rewrite` is a safe no-op.

> **Ordering matters.** For the provenance chain to attach to existing facts (and
> for the layer to stay byte-exact), seed the temporal baseline *before* the
> rewrite — `temporal_migrate_onboard.py sync --confirm-apply` first, then
> `apply`. `scripts/memory_onboard.py` does this for you (steps 5 → 7).

## Output

- **Plan JSON / markdown:** per-entry `old → new` with rationale + estimated
  char/entry reduction.
- **Proposed files:** `MEMORY.proposed.md` / `USER.proposed.md` — same `\n§\n`
  format and order, parseable by `memory_audit.py`.
- **Manifest:** SHA-256 of source + proposed text, every proposal's original
  text, and the archive list (the recoverability record).

## Verify

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/memory_rewrite.py
python3 scripts/memory_rewrite.py --help
python3 -m unittest tests.test_memory_rewrite -v        # 23 tests, synthetic only
python3 -m unittest tests.test_memory_audit tests.test_memory_rewrite -v
```

## Related

- **`memory-onboarding` skill / `scripts/memory_onboard.py`** — the one command
  that runs Areas 1→5 (seed → render → apply → reconcile → verify) in the correct
  order, so you don't drive these steps by hand.
- `memory-audit` skill — Area 2 (produces the input this consumes).
- `state-db-remediation` skill — Area 1.
- `plans/memory-onboarding-remediation.md` — Areas 1–5.
- **Area 4 — temporal migration:** `apply` now records provenance there
  automatically (see above); `temporal_migrate_onboard.py verify` confirms the
  layer still reconstructs live byte-exact.
```
