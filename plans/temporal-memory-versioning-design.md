# Temporal Memory Versioning — Design

**Status:** Design + implementation (v1.1 — adversarial-review-hardened, shipped & migrated 2026-06-23)
**Author:** Hermes Agent + Claude Opus 4.8 (UltraCode)
**Date:** 2026-06-23
**Scope:** Add a time axis to Hermes hot-memory (`MEMORY.md` / `USER.md`) without touching the wire format, `state.db` schema, `config.yaml`, or the running gateway.
**Siblings:** `memory-auto-extraction-plan.md`, `memory-semantic-retrieval-plan.md`, `memory-stack-packaging-design.md`, `memory-curator-design-2026-06-18.md`.

---

## 1. Problem

Hermes memory is a flat, `§`-delimited pointer file (`~/.hermes/memories/MEMORY.md`) injected into the system prompt every turn. When a fact changes, the new value **overwrites** the old one (manual edit, the nightly curator, or the planned auto-extractor). The *evolution* of knowledge is lost:

- The curator's `↪` archive pointers preserve the *last* archived snapshot, but not the chain of intermediate edits, and only for entries it evicts for **capacity** — not for in-place value changes.
- An in-place edit ("Xiaomi is now DEFAULT", replacing "Anthropic-first") leaves **no trace** of the prior value or when it changed.
- The auto-extraction plan is explicitly *append-only* yet its dedup pass emits `UPDATE`/`DELETE` actions — a contradiction with nowhere safe to land (see §9).

We cannot answer:
- *What did memory say last Tuesday?* (point-in-time)
- *How did the routing fact evolve?* (history)
- *When did this config change, and what was it before?* (diff)
- *Recover the line the 3:50 AM sweep clobbered.* (restore)

These are the exact gaps named in `agent-memory-systems-comparison-2026.md`: Zep/Graphiti track how facts change over time; Hermes' weakness is "old facts overwritten, history lost."

## 2. Goals & non-goals

**Goals**
1. Version memory entries instead of overwriting — full, recoverable history.
2. Bi-temporal: track both *when a fact was true* and *when Hermes learned it*.
3. Fast `current()`; on-demand `history()`, `diff()`, `point_in_time()`, `restore()`.
4. Backward-compatible: `MEMORY.md` wire format is **byte-identical**; the layer is a pure side-car.
5. Queryable by the agent (CLI emits JSON; a SQLite DB is available for ad-hoc SQL).
6. Low overhead (KB-scale, no 10× storage blow-up), never-lose guarantee.
7. Integrate with the curator and the auto-extractor **without modifying them** (zero-touch pull mode), with documented optional push hooks for precision.

**Non-goals (explicit, per task scope)**
- No changes to `state.db` schema, `config.yaml`, the dreaming system, the running gateway, or how `MEMORY.md` is injected.
- No modification of the auto-extraction script or the curator script (we *consume* their outputs and document hooks they *may* later call).
- No new package installs — Python **stdlib only** (`sqlite3`, `json`, `hashlib`, `datetime`, `difflib`, `argparse`, `re`, `pathlib`, `fcntl`).
- Not a knowledge graph (no entity/relation extraction). This is *entry-level* temporal versioning; a Graphiti-style KG can later consume the version log.

## 3. Key concepts

### 3.1 Bi-temporal model (from Zep/Graphiti)
Every version carries **two** independent time intervals:

| Axis | Fields | Answers |
|------|--------|---------|
| **Valid time** (real-world) | `valid_from`, `valid_to` | "In May I used X; in June, Y" — when the fact was *true*. |
| **Transaction time** (system) | `recorded_at`, `superseded_at` | "Hermes recorded this on Jun 15" — when the agent *learned/wrote* it. |

For most facts the two coincide (we record a fact when it becomes true). Keeping them separate handles backdating ("I realised in June that the May strategy was X") and lets us answer both "what was true at T" (valid axis) and "what did Hermes believe at T" (transaction axis). `point_in_time()` accepts a `--time-axis valid|transaction` switch; default is `valid`.

**Transaction order is authoritative** (event-sourcing). Version numbers, the supersession chain, and which version is `current` are all derived from transaction order — `(recorded_at, seq)` where `seq` is a monotonic per-event append counter that makes ordering fully deterministic (it breaks ties without resorting to wall-clock or random ids). This is what lets a reconstructed archive body (ingested *after* its live pointer) slot in correctly as an earlier version regardless of ingestion order. Valid time is a *derived* interval: a version's effective start is `COALESCE(valid_from, recorded_at)` (a fact cannot be true before Hermes recorded it, absent an explicit backdate — so an undated entry is never reported as valid before it existed), and its valid_to is closed at the successor's effective start (contiguous, no gaps); the last version stays open unless it carries an explicit valid_to (an archived-and-retired fact with no live successor). `is_current` = the latest version in transaction order that is live (not a delete tombstone, not explicitly closed).

### 3.2 Fact identity (`fact_key`)
`MEMORY.md` entries have **no IDs** — they are free-text `§` chunks. Versioning needs a stable identity for a *logical fact* that persists across edits. We assign each entry a `fact_key` slug:

- **Topic-prefix rule** (preferred): if the first line matches `^<Topic>(\(...\))?:` (e.g. `Hermes routing (updated 2026-06-21):`, `Memory Curator:`, `GBrain API key policy (2026-06-01):`), `fact_key = slugify(Topic)` → `hermes-routing`, `memory-curator`, `gbrain-api-key-policy`.
- **Fallback**: `slugify(first ~6 meaningful words)`.
- **Collisions**: if two distinct facts slugify equally, suffix `-2`, `-3`.

`slugify` and `content_hash` are **byte-identical** to the curator (`hermes_memory_curator.py:175,179`) so our keys/hashes line up with existing `_archive/curator/*.md` blocks:
```python
content_hash = sha256(text.strip().encode()).hexdigest()[:16]   # 16-hex, matches archive "Hash:"
slugify      = re.sub(r"[^a-z0-9]+","-", first_line.lower()).strip("-")[:48]
```

### 3.3 Update-vs-new matching
When a new/changed entry arrives (pull reconcile or push from the extractor), decide whether it **supersedes** an existing fact or is **brand new**:
1. Exact `content_hash` match → **DUPLICATE** (corroboration; no new version, optional `last_seen` bump).
2. Exact `fact_key` match, different hash → **UPDATE** (new version of that fact).
3. Else fuzzy: token **Jaccard ≥ 0.6** against current entries (aligns with the curator design §2d supersession threshold) → **UPDATE** of the best match.
4. Else → **NEW** (version 1).

The same `match()` function is exposed to the auto-extractor so both systems agree on identity (resolves the dedup-duplication overlap in §9).

## 4. Storage decision (A vs B vs C)

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **A. SQLite only** (`memory_versions.db`) | Fast indexed queries; agent can run SQL; named by `memory-stack-packaging-design.md`; house pattern (one DB per subsystem). | Binary, not git-diffable/inspectable; single point of corruption (WAL on FUSE/SMB — the exact reason `apply_wal_with_fallback` exists); contradicts the plaintext never-lose ethos. | Index, **not** sole truth. |
| **B. JSONL only** | Plaintext, append-only = natural event log, git-friendly, greppable, never-lose, packageable, trivial to back up. | No indexes (fine at this scale); ad-hoc SQL not possible without a loader. | **Source of truth.** |
| **C. Git-versioned `MEMORY.md`** | Free history, `git log -p`. | `~/.hermes` is **not** a git repo; line-diffs ≠ fact-diffs (entries reorder); no valid-time, no per-fact history, no point-in-time on a *fact*; curator rewrites whole file → noisy commits. | **Rejected** as primary (the `.bak.<epoch>` snapshots already provide crude file-level rollback). |

### Chosen: **A + B hybrid** (event log + rebuildable index)
- **Source of truth — append-only JSONL** at `~/.hermes/memories/_versions/history.jsonl`. One immutable JSON event per line. This is the durable, plaintext, git-friendly, never-lose record. Pruned events move to `history.archive.jsonl` (cold) — never hard-deleted.
- **Query index — SQLite** at `~/.hermes/memory_versions.db` (the path `memory-stack-packaging-design.md` already names; house conventions). A **pure derived projection**, rebuildable from JSONL at any time via `--rebuild`. Maintained incrementally on each write; a cheap event-count check auto-rebuilds on drift, so queries never read stale data.

**Why hybrid, not pure-A:** it mirrors the curator's proven, audited pattern — plaintext archive (`_archive/curator/*.md`) is the system of record, the SQLite **Memory Spine** is a rebuildable index. A versioning system whose *only* copy is a binary file would contradict the entire Hermes memory philosophy and be a single corruption away from total history loss. The DB is disposable; the log is forever. This also satisfies the packaging design's `memory_versions.db` intent (a rebuildable index living at that path) and Emeka's interest in *packaging the memory system for others* (a plaintext audit log is far more inspectable/trustworthy than "trust my SQLite").

## 5. Schema

### 5.1 JSONL event (one line, the source of truth)
```jsonc
{
  "event_id":      "uuid4",                 // immutable id of this version event
  "fact_key":      "hermes-routing",        // stable logical-fact identity (§3.2)
  "store":         "MEMORY.md",             // MEMORY.md | USER.md
  "version":       3,                        // monotonic per fact_key (1,2,3…)
  "op":            "update",                 // create|update|supersede|archive|delete|restore
  "title":         "Hermes routing",        // human label (topic / first line)
  "content":       "Hermes routing (updated 2026-06-21): Xiaomi …",  // full entry text at this version
  "content_hash":  "a1b2c3d4e5f6a7b8",      // sha256(text.strip())[:16] — matches curator/archive
  "taxonomy":      "project_status",        // curator taxonomy (user_preference|project_status|…)
  "valid_from":    "2026-06-21T00:00:00Z",  // real-world: became true
  "valid_to":      null,                     // real-world: stopped being true (null = current)
  "recorded_at":   "2026-06-23T11:30:00Z",  // system: when this version was logged
  "superseded_at": null,                     // system: when a newer version replaced it
  "supersedes":    "uuid-of-v2",            // prior version's event_id (null for v1)
  "source":        "migration",             // manual|curator|auto-extraction|migration|restore|ingest
  "confidence":    null,                     // 0..1 (extractor) or null
  "actor":         "temporal_migrate.py",   // run id / session id / tool name
  "reason":        "seeded from MEMORY.md",  // human reason (mirrors curator "Reason:")
  "tags":          ["routing","providers"],
  "archived_path": null                      // _archive/curator/<date>-MEMORY.md if curator-archived
}
```
Events are **immutable**. A supersession appends a *new* event and (logically) closes the prior one. The "closing" (`valid_to`/`superseded_at`/`supersedes` back-links) is materialised in the index; the JSONL keeps the closing values it knew at write time and the index recomputes authoritative links on rebuild.

### 5.2 SQLite index (derived; `memory_versions.db`)
```sql
CREATE TABLE IF NOT EXISTS versions (
  event_id      TEXT PRIMARY KEY,
  fact_key      TEXT NOT NULL,
  store         TEXT NOT NULL,
  version       INTEGER NOT NULL,
  op            TEXT NOT NULL,
  title         TEXT,
  content       TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  taxonomy      TEXT,
  valid_from    TEXT,                 -- ISO-8601 UTC
  valid_to      TEXT,                 -- null = currently valid
  recorded_at   TEXT NOT NULL,
  superseded_at TEXT,                 -- null = current transaction-time version
  supersedes    TEXT,
  superseded_by TEXT,                 -- back-link (derived)
  source        TEXT,
  confidence    REAL,
  actor         TEXT,
  reason        TEXT,
  tags          TEXT,                 -- JSON array
  archived_path TEXT,
  is_current    INTEGER NOT NULL DEFAULT 0,  -- 1 = current version of its fact_key
  tombstone     INTEGER NOT NULL DEFAULT 0   -- 1 = op=delete (fact retired)
);
CREATE INDEX IF NOT EXISTS idx_ver_key       ON versions(fact_key, version);
CREATE INDEX IF NOT EXISTS idx_ver_current   ON versions(store, is_current);
CREATE INDEX IF NOT EXISTS idx_ver_hash      ON versions(content_hash);
CREATE INDEX IF NOT EXISTS idx_ver_recorded  ON versions(recorded_at);
CREATE INDEX IF NOT EXISTS idx_ver_validfrom ON versions(valid_from);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);  -- jsonl_events count, schema rev, last_*
```
- **Current state** = `WHERE is_current=1 AND tombstone=0` (one row per live fact). Fast.
- **History** = `WHERE fact_key=? ORDER BY version`.
- **Point-in-time (valid)** = per `fact_key`, the row with `valid_from<=T AND (valid_to IS NULL OR valid_to>T)`.
- **Point-in-time (transaction)** = per `fact_key`, `recorded_at<=T AND (superseded_at IS NULL OR superseded_at>T)`.
- **Diff** = render two point-in-time selections of one `fact_key` through `difflib`.

## 6. Query & write interface

Python module `temporal_memory.py` exposes `TemporalMemory` with a CLI (all read commands emit `--json`):

| Command | API | Purpose |
|---------|-----|---------|
| `current [--store S] [--key K]` | `current()` | Live set, or one fact's current version. |
| `history --key K` | `history(key)` | All versions of a fact, ordered, with valid/txn intervals. |
| `diff --key K --from D1 --to D2` | `diff(key,d1,d2)` | Unified diff of the versions active at D1 vs D2. |
| `at --date D [--key K] [--time-axis valid\|transaction]` | `point_in_time(date,…)` | Memory (or one fact) as it stood at D. |
| `record --store S --key K --content … --op … --source …` | `record(event)` | **Write path** — append a version (PRE-WRITE by callers). |
| `match --text …` | `match(text)` | NEW/UPDATE/DUPLICATE decision + best `fact_key` (for the extractor). |
| `ingest [--store …]` | `ingest_files()` | **Pull mode** — reconcile current file state vs last-known versions. |
| `ingest-archives` | `ingest_archives()` | Reconstruct prior versions from `_archive/curator/*.md` blocks. |
| `rebuild` | `rebuild()` | Replay JSONL → SQLite from scratch (idempotent). |
| `prune [--days 90] [--keep-per-key 10]` | `prune()` | Move stale superseded versions to cold log (never delete). |
| `restore --key K [--at D \| --version N]` | `restore()` | Emit a prior version's content (non-destructive; print + `.bak`). |
| `stats` | `stats()` | Counts, capacity, last ingest. |

## 7. Integration — dual mode

### 7.1 Pull mode (zero-touch, default, ships now)
`temporal_memory.py ingest MEMORY.md USER.md` parses the live files (same `§` split as the curator) and reconciles each entry against the last-known version via `match()`:
- new hash, new key → `record(op=create)`
- new hash, matched key/fuzzy → `record(op=update)` (prior version auto-closed)
- entry vanished from the file → look it up in `_archive/curator/*.md` by `content_hash`; if found → `record(op=archive)` with `valid_to`=archived date, `archived_path`, and the archive block's `Reason`; if not found → `record(op=delete)` tombstone.

This captures changes from **any** writer — manual edits, the curator, the extractor — *without* modifying them. Wire it as one line in the existing nightly dreaming cron (documented in the SKILL; not auto-created here).

`ingest-archives` additionally walks the curator archive blocks and, for each `↪` pointer currently in `MEMORY.md`, reconstructs the **prior full-content version** (from the archive block, `valid_from`=original date … `valid_to`=archived date) beneath the current pointer-stub version — giving real history for the 13 already-archived facts, not just a flat snapshot.

### 7.2 Push mode (precise, optional, documented)
For exact `source`/`confidence`/`valid_from`, callers invoke `record()` **before** they mutate `MEMORY.md`. Hook points (from the curator/extractor code, to be wired later — not in this task):
- Curator `do_apply()` pre-write backup (`hermes_memory_curator.py:~1209`) and archive block (`~1214`).
- Auto-extractor Pass-2 `UPDATE`/`DELETE` branch — **the cardinal rule: snapshot-old, then write-new** (§9).
- The agent `memory` tool's `MemoryStore.replace()/remove()` (the §8.3 reactive hook the curator design specified but never wired).

## 8. Migration

`temporal_migrate.py` (thin wrapper over the library, idempotent):
1. `ingest-archives` first — reconstruct prior full-content versions from `_archive/curator/*.md` (so already-archived facts get real `v1` history before the present pointer-stub `v2`).
2. `ingest MEMORY.md USER.md` — seed/close versions for all current live entries (`source=migration`).
3. Print a report: facts seeded, prior versions reconstructed from archives, unmatched pointers.

Idempotent: re-running records nothing new (same `content_hash` → DUPLICATE). Safe to run repeatedly and before auto-extraction goes live (gives the first `UPDATE` a baseline to supersede).

## 9. Overlap with auto-extraction (resolved)

`memory-auto-extraction-plan.md` is **append-only** ("never rewrite existing MEMORY.md lines", decision #7) yet its Pass-2 dedup emits `UPDATE` and `DELETE`. That contradiction is exactly the seam this layer fills:

- **UPDATE**: extractor calls `record(op=update)` **before** rewriting the line → old value preserved in history, MEMORY.md safely mutated. Order is mandatory: *snapshot-old → write-new*.
- **DELETE / "fact now false"**: `record(op=delete)` writes a tombstone (old value + retired-at) rather than a silent drop.
- **CONFLICT**: `record` with `reason="pending-review"` so Emeka/curator adjudicates without data loss.
- **Shared identity**: both use the same `match()`/`fact_key`/`content_hash`, so they never disagree about "the same fact."
- **35-entry cap eviction**: when an `ADD` force-evicts an entry (one-in-one-out), that eviction is *also* an `archive`/`delete` event the layer must capture — pull-mode `ingest` catches it automatically even if the extractor forgets.
- **Retention conflict**: three independent prune policies exist (extractor caps 5/session·10/night; intake 35-entry ceiling; versioning `prune_after_days:90`, `max_versions:10`). They must not erase a fact's only record. Versioning prune **never** removes `v1` (birth) or the current version, and never hard-deletes (cold-log move). Documented ordering in the nightly window: **extract → version-snapshot → write → curator**, under a shared lock.

## 10. Retention & never-lose

- `prune(days=90, keep_per_key=10)` (matches `memory-stack-packaging-design.md`): for each `fact_key`, superseded versions older than `days` **and** beyond the most-recent `keep_per_key` are **moved** to `history.archive.jsonl`, then dropped from the index. `v1` and the current version are always kept.
- Never-lose chain (mirrors curator): JSONL source of truth + cold archive JSONL + the curator's own `_archive/curator/*.md` + `.bak.<epoch>` + (optionally) the Memory Spine FTS index.
- Integration with the curator's cleanup: documented cron line / weekly-consolidation hook; **not** auto-created (cron infra is out of scope to mutate here).

## 11. Concurrency & format safety (hard risks)

- **Locking**: acquire the *same* advisory lock the curator/memory-tool use — `flock` on `~/.hermes/memories/MEMORY.md.lock` (and `USER.md.lock`) — before reconcile; `flock` on a dedicated `history.jsonl.lock` for the event-log append. The 0-byte lock files are advisory targets; never infer lock state from their mtime/size.
- **Idempotent writes**: dedup on `content_hash`; replaying JSONL is safe.
- **Wire-format inviolable**: the layer **never writes `MEMORY.md`** except via `restore` (which goes through the curator's exact `read_entries`/`write_entries_atomic` semantics: `§`-split, **no trailing newline**, temp-file + fsync + `os.replace`, and a `.bak.<epoch>` first). Versioning bookkeeping lives entirely out-of-band so it can never leak into the every-turn system-prompt injection.
- **Delimiter safety**: entry text must never contain a bare `§` line or raw newline (guaranteed by going through `read_entries`).
- **Self-contained**: best-effort import of `get_hermes_home`/`apply_wal_with_fallback`; clean stdlib fallback (`HERMES_HOME` env → `~/.hermes`; guarded `PRAGMA journal_mode=WAL`) so the tool runs even if `hermes-agent` isn't importable.

## 12. File layout (created by this task)

```
~/.hermes/plans/temporal-memory-versioning-design.md            # this doc
~/.hermes/scripts/temporal_memory.py                            # library + CLI (storage+query+ingest+prune+restore)
~/.hermes/scripts/temporal_migrate.py                           # idempotent migration wrapper
~/.hermes/skills/hermes/temporal-memory-versioning/SKILL.md     # operator skill
# created at runtime by the tool:
~/.hermes/memories/_versions/history.jsonl                      # append-only source of truth
~/.hermes/memories/_versions/history.archive.jsonl              # cold (pruned) events
~/.hermes/memory_versions.db                                    # derived SQLite index (rebuildable)
```

## 13. Verification (acceptance)
1. `ingest`/`record` write versioned entries; `rebuild` reproduces the index from JSONL exactly.
2. `current()`, `history(key)`, `diff(key,d1,d2)`, `point_in_time(date)` all return correct results on real `MEMORY.md` data.
3. Migration versions every current `MEMORY.md`/`USER.md` entry and reconstructs prior versions for archived facts.
4. `MEMORY.md` is byte-unchanged after a full migrate (side-car only).
5. Adversarial review (multi-agent) confirms bi-temporal query correctness, never-lose, idempotency, and format safety.

## 14. Review hardening (v1.1, 2026-06-23)

A 4-lens adversarial agent panel (bi-temporal / integrity / format-concurrency / reconciliation) reproduced and confirmed 32 findings against the v1.0 draft; all High/Medium and the cheap Low/Nit were fixed and regression-tested (40/40). The substantive changes:

- **Versioning ordered by transaction time, not valid_from.** v1.0 sorted versions by `valid_from`, which (a) made `current` empty for a live fact whose reconstructed archive body carried a later date (the real `local-llm` break), and (b) let a `uuid` tiebreak randomise order. Fixed by ordering on `(recorded_at, seq)` with a deterministic monotonic `seq`.
- **No time-travel on the valid axis.** A NULL `valid_from` was treated as −∞; now `eff_valid_from = COALESCE(valid_from, recorded_at)` and valid intervals are contiguous (no empty gap between an archive's close and its pointer's start).
- **Source-of-truth robustness.** `_read_events` quarantines corrupt lines (`history.jsonl.corrupt`) instead of crashing; `rebuild` de-dupes by `event_id` and refuses to wipe a populated index when the log has vanished (use `--force`); the constructor never lets a bad log brick read commands.
- **Idempotent reversion vs replay.** `record()` dedups only against the *current* version (so a genuine A→B→A reversion is recorded), while archive re-ingestion is made idempotent by an explicit content-hash guard.
- **`restore --apply` hardened** (the only MEMORY.md write path): lock-first, drift-check, identity-match the live entry by `fact_key` (not content equality), reject `§`/over-length content, atomic write preserving mode and resolving symlinks, `.bak.<epoch>` first.
- **Concurrency:** all read-decide-write critical sections (record, prune, ingest reads, restore) run under the curator-compatible `<file>.lock`; prune fsyncs the cold log before shrinking the hot log (save-then-remove); the index auto-rebuilds on a size/mtime/count fingerprint mismatch.
- **Matching:** pointer stubs bypass the fuzzy branch (they were stealing the wrong key); the archive-block regex anchors its boundary to `## …\nStatus:` so an inner `## ` in a body can't split it; the nav header is identified by content sentinel, not position.
- **Known/accepted limits:** a pruned *middle* version leaves the queryable window (birth + current always retained; full history stays in the cold log); back-filled archive versions use the archived date as `recorded_at`, so transaction-axis queries on reconstructed history are approximate (documented).

**Real migration (2026-06-23):** 51 live entries (36 MEMORY.md + 15 USER.md) + 30 curator archive blocks → **79 facts / 81 versions**, 2 with reconstructed body→pointer history. MEMORY.md and USER.md byte-identical; side-car ~81 KB JSONL + 114 KB SQLite. A migrated v1's `content_hash` matches the curator's archive `Hash:` byte-for-byte (e.g. `local-llm` = `e68a76ba22c25de8`).
