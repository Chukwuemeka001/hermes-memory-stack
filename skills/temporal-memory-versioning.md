---
name: temporal-memory-versioning
description: "Version history and point-in-time recall for Hermes hot-memory (MEMORY.md / USER.md). Snapshots every memory mutation into an append-only log, reconstructs how a fact evolved, diffs across time, and restores a clobbered entry — without touching the live wire format. Load when you need to see how memory changed, recover a lost/overwritten entry, answer 'what was true at time X', operate/troubleshoot the version store, or wire auto-extraction to it."
version: 1.0.0
author: Hermes Agent + Claude Opus 4.8
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [memory, versioning, temporal, history, snapshots, point-in-time, curator]
    related_skills: [memory-curator, semantic-session-retrieval]
---

# Temporal Memory Versioning — A Time Axis for Hot Memory

Hermes hot-memory (`~/.hermes/memories/MEMORY.md`, `USER.md`) is a flat, `§`-delimited
pointer file injected into the system prompt every turn. When a fact changes it gets
**overwritten** — the history of how knowledge evolved is lost. This layer adds a **time
axis**: every memory mutation is snapshotted, so you can ask "what did memory say last
Tuesday?", see how the routing fact evolved, diff two points in time, and restore a
specific prior entry — all without ever changing the live file's bytes.

It is **additive** to the `memory-curator`: the curator decides WHAT to keep in hot memory;
this layer records HOW memory changed over time. If versioning is unavailable, normal memory
reads/writes are completely unaffected (pure side-car).

**Model (bi-temporal, Zep/Graphiti-style):** every version carries two intervals —
*valid time* (`valid_from`/`valid_to`: when the fact was true in the world) and
*transaction time* (`recorded_at`/`superseded_at`: when Hermes learned it).

**Storage (A+B hybrid):** the **source of truth** is an append-only JSONL event log
(`~/.hermes/memories/_versions/history.jsonl`); a **derived, rebuildable** SQLite index
(`~/.hermes/memory_versions.db`) serves fast queries. The DB is disposable — `rebuild`
replays the log. Plaintext log = never-lose + git-friendly + inspectable.

## When to Load

- You need point-in-time recall ("what was in MEMORY.md before the 3:50 AM sweep?").
- An entry was clobbered, wrongly archived, or overwritten and you want it back.
- You need to answer "how did this fact change?" / "when did this config change?".
- You are operating or troubleshooting the version store (migrate, rebuild, prune, inspect).
- You are wiring **auto-extraction** or the **curator** to record versions (see Integration).
- **Don't use for:** routine memory edits (use the `memory` tool) or capacity cleanup
  (use the `memory-curator` skill). This layer never decides what stays in hot memory.

## Quick Reference

```bash
TM="python3 ~/.hermes/scripts/temporal_memory.py"

$TM stats --json                                  # store health: facts, versions, with-history
$TM current --store MEMORY.md --json              # every live fact's current version
$TM history --key hermes-routing --json           # full version history of one fact
$TM at --date 2026-06-20 --json                   # whole memory AS IT STOOD on that day
$TM at --date 2026-06-20 --key local-llm --json   # one fact, as of a date
$TM diff --key local-llm --from 2026-06-19 --to 2026-06-23   # what changed, when
$TM restore --key local-llm --version 1           # PRINT a prior version (non-destructive)
```
Global flags (`--home`, `--db`, `--jsonl`, `--json`) work **before or after** the subcommand.
All read commands accept `--json`; `stats`/`ingest`/`diff`/`prune` always emit JSON.

## Architecture (read before touching anything)

```
 any memory writer (manual edit / curator sweep / auto-extractor)
        │
        ▼  (PULL, default)                         (PUSH, optional/precise)
  temporal_memory.py ingest  ──reconcile──┐    callers invoke record() PRE-write
        │                                 │
        ▼                                 ▼
  history.jsonl  ── append-only event log (SOURCE OF TRUTH) ──┐
   (~/.hermes/memories/_versions/)                            │ rebuild (replay)
        │                                                     ▼
        └────────────────────────────────────────►  memory_versions.db  (derived index)
                                                       (~/.hermes/, fast queries)
 queries (current/history/at/diff/restore) read the index; MEMORY.md is NEVER mutated
 except by `restore --apply` (which goes through the curator-exact atomic write + .bak).
```

**Critical constraints (do not violate):**
- The wire format is byte-sacred: `§`-delimited, **no trailing newline**, injected every turn.
  This layer keeps all bookkeeping out-of-band; it never writes `MEMORY.md` except `restore --apply`.
- `content_hash` (`sha256(text.strip())[:16]`) and `slugify` are **identical to the curator**,
  so version identity lines up with existing `_archive/curator/*.md` blocks (`Hash:` field).
- The SQLite DB is a cache. If in doubt, `rebuild`. The JSONL is the truth.
- stdlib-only Python; runs under any `python3` (tested on 3.14). No venv needed.

## Components

| File | Role |
|------|------|
| `~/.hermes/scripts/temporal_memory.py` | Library + CLI: record / ingest / query / diff / restore / prune / rebuild. |
| `~/.hermes/scripts/temporal_migrate.py` | One-shot, idempotent migration — seeds history from MEMORY.md/USER.md + curator archives. |
| `~/.hermes/memories/_versions/history.jsonl` | Append-only event log — **source of truth**. |
| `~/.hermes/memories/_versions/history.archive.jsonl` | Cold log of pruned versions (never deleted). |
| `~/.hermes/memory_versions.db` | Derived SQLite query index (rebuildable). |
| `~/.hermes/plans/temporal-memory-versioning-design.md` | Full design doc (schema, trade-offs, integration). |

## Operations

```bash
TM="python3 ~/.hermes/scripts/temporal_memory.py"

# First-time setup (idempotent — safe to re-run): seed history from current memory + archives
python3 ~/.hermes/scripts/temporal_migrate.py

# Capture changes since last run (PULL mode — catches manual edits, curator sweeps, extractor writes)
$TM ingest MEMORY.md USER.md --source nightly

# Reconstruct prior full-content versions from the curator's archive blocks
$TM ingest-archives

# Record a version explicitly (PUSH mode — call this BEFORE overwriting a fact)
$TM record --key hermes-routing --op update --source manual \
   --content "Hermes routing (2026-06-23): Claude Opus default." --reason "switched default"

# Rebuild the index from the JSONL (after any doubt, manual log edit, or DB loss)
$TM rebuild

# Retention: move stale superseded versions to the cold log (NEVER deletes; keeps v1 + current)
$TM prune --days 90 --keep-per-key 10
```

### Querying (all emit JSON with `--json`)

```bash
$TM current --key nclex-pn-app          # current version of one fact
$TM history --key nclex-pn-app          # every version, oldest→newest, with valid/txn intervals
$TM at --date 2026-06-01                # full memory snapshot as it stood that day (valid axis)
$TM at --date 2026-06-01 --time-axis transaction   # what Hermes *believed* that day
$TM diff --key hermes-routing --from 2026-06-10 --to 2026-06-23   # unified diff between two times
```

### Restore (non-destructive by default)

```bash
$TM restore --key local-llm --version 1            # PRINTS the old content; writes nothing
$TM restore --key local-llm --at 2026-06-20        # the version valid on that date
$TM restore --key local-llm --version 1 --apply    # splice back into MEMORY.md (.bak.<epoch> first)
```
`--apply` is the only write path to `MEMORY.md`; it snapshots a `.bak.<epoch>` first and uses the
curator-exact atomic write (no trailing newline). Default (no `--apply`) only prints — safe to explore.

## Integration

### Auto-extraction (the parallel `memory-auto-extraction-plan.md`)
The extractor is *append-only* yet its dedup pass emits `UPDATE`/`DELETE`. This layer resolves that
contradiction. **The cardinal rule: snapshot-old, then write-new.** The extractor should:

1. For each candidate fact, call `match()` to get the shared identity decision:
   ```bash
   python3 ~/.hermes/scripts/temporal_memory.py match --text "<candidate fact>" --json
   #  -> {"action":"NEW|UPDATE|DUPLICATE", "fact_key":"...", "score":0.0-1.0}
   ```
2. On **UPDATE** (replacing an existing line): call `record --op update --source auto-extraction
   --confidence <c>` **before** rewriting MEMORY.md — the old value is preserved as a version.
3. On **NEW**: `record --op create --source auto-extraction` so the fact has lineage from birth.
4. On **DELETE / "fact now false"**: `record --op delete` writes a tombstone (never a silent drop).
5. On **CONFLICT**: `record ... --reason pending-review` so Emeka/curator adjudicates without data loss.
6. Use the SAME `fact_key`/`content_hash` as this layer (it reuses the curator's functions) so the two
   systems never disagree about "the same fact."

Even if the extractor forgets a hook, nightly `ingest` (PULL mode) reconciles the file afterwards and
captures the change — including a 35-cap one-in-one-out eviction the extractor doesn't model.

### Curator (`memory-curator`)
Zero-touch by default: `ingest` reads the live file, and `ingest-archives` reads the curator's
`_archive/curator/*.md` blocks (by `Hash:` + date + reason) to reconstruct the prior full-content
version beneath each `↪` pointer stub — giving real history for already-archived facts. To wire it
into the nightly window, add **one line** to the dreaming/curator cron (order: extract → version →
write → curator-sweep):
```bash
python3 ~/.hermes/scripts/temporal_memory.py ingest MEMORY.md USER.md --source nightly
python3 ~/.hermes/scripts/temporal_memory.py prune --days 90 --keep-per-key 10   # weekly
```

## Retention / knobs

| Knob | Default | Effect |
|------|---------|--------|
| `--days` (prune) | `90` | Superseded versions older than this become prune-eligible. |
| `--keep-per-key` (prune) | `10` | Keep at least the N most-recent versions per fact regardless of age. |
| `--threshold` (match) | `0.6` | Jaccard token-overlap cutoff for fuzzy UPDATE-vs-NEW (matches curator §2d). |
| `--time-axis` (at/diff) | `valid` | `valid` = real-world time; `transaction` = when Hermes recorded it. |
| `HERMES_HOME` env / `--home` | `~/.hermes` | Where memory + DB live (use a temp dir to sandbox-test). |

Prune **never** removes v1 (birth) or the current version, and **never** hard-deletes — it moves
events to `history.archive.jsonl`. Storage is KB-scale (the full migration of 61 entries + 30 archive
blocks is ~90 KB of JSONL).

## Troubleshooting

- **Queries look stale / empty after editing the JSONL by hand:** run `rebuild`. The DB auto-rebuilds
  when its event-count drifts from the log, but a manual edit that keeps the count equal won't trip it.
- **`current` shows an archived fact as live:** it isn't — a fact whose latest version has `valid_to`
  set is excluded from `current`. Check `history --key …` to see the closed interval.
- **A fact split into two keys (`foo` and `foo-2`):** two live entries slugified to the same key with
  different content; the second got a `-N` suffix. Expected for genuinely distinct facts.
- **Import error for `hermes_constants`/`hermes_state`:** harmless — the tool falls back to `~/.hermes`
  and a plain WAL pragma. It is fully self-contained.

## Common Pitfalls

1. **Writing to MEMORY.md directly to "restore" (CRITICAL).** Never hand-edit the live file to roll
   back. Use `restore --apply`, which snapshots a `.bak.<epoch>` and uses the curator-exact atomic
   write. Hand-edits can break the `§`/no-trailing-newline invariant and corrupt the every-turn injection.
2. **Recording AFTER overwriting (data loss).** Push-mode `record` must be called *before* the old
   value is replaced. Snapshot-old → write-new. After the overwrite, the old text is already gone
   (only nightly `ingest` + archive reconstruction can partially recover it).
3. **Treating the SQLite DB as the source of truth.** It's a derived cache. Back up / inspect / commit
   the JSONL, not the DB. `rebuild` regenerates the DB; nothing regenerates the JSONL.
4. **Running under the agent venv expecting heavy deps.** This tool is stdlib-only on purpose; don't
   add imports that need the gateway venv. Any `python3` works.
5. **Pruning with a tiny `--keep-per-key` and expecting birth gone.** Birth (v1) and current are always
   protected. That's the never-lose guarantee, not a bug.

## Verification Checklist

- [ ] `temporal_migrate.py` reports `facts_after > 0` and `MEMORY.md`/`USER.md` are byte-unchanged.
- [ ] `history --key <archived-fact>` shows ≥2 versions (full body → pointer stub) for linked archives.
- [ ] `at --date <before an archive> --key <fact>` returns the full pre-archive body.
- [ ] `diff --key <fact> --from D1 --to D2` shows the expected change with a `unified_diff`.
- [ ] `rebuild` then `stats` returns the same `total_versions` (deterministic).
- [ ] Re-running `temporal_migrate.py` creates 0 new versions (idempotent).
- [ ] `restore --key <fact> --version 1` (no `--apply`) prints content and writes nothing.
- [ ] `prune` moves only middle versions to `history.archive.jsonl`; v1 + current survive.

## Notes / evolution (dated)

**Implemented, reviewed & migrated 2026-06-23 (Opus 4.8, UltraCode):** shipped `temporal_memory.py`
+ `temporal_migrate.py`. A 4-lens adversarial agent panel found 32 issues against the first draft;
all High/Medium fixed and regression-tested (**40/40** hermetic assertions: bi-temporal point-in-time,
contiguous valid intervals, no-time-travel on NULL valid_from, reversion A→B→A, corrupt-line
resilience, duplicate-event_id survival, never-lose prune, hardened `restore --apply`, determinism,
idempotency, byte-identity). Key model fix: versions are ordered by **transaction time**
`(recorded_at, seq)`, not valid_from. **Real migration:** 51 live entries (36 MEMORY.md + 15 USER.md)
+ 30 curator archive blocks → **79 facts / 81 versions**, 2 with reconstructed body→pointer history;
MEMORY.md/USER.md byte-identical; side-car ~81 KB JSONL + 114 KB SQLite. A migrated v1 `content_hash`
matches the curator archive `Hash:` byte-for-byte (e.g. `local-llm` = `e68a76ba22c25de8`).

**Not wired (by design — left for you to enable):** no cron job was created. To automate, add the two
`ingest`/`prune` lines above to the nightly dreaming/curator cron. The push-mode hooks in the curator
and auto-extractor are documented but not patched into those scripts.

Design document: `~/.hermes/plans/temporal-memory-versioning-design.md`.
Siblings: `memory-auto-extraction-plan.md`, `memory-stack-packaging-design.md`, `memory-curator` skill.
