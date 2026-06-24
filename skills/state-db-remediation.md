---
name: state-db-remediation
description: "Area 1 of memory-stack onboarding — conservatively audit and (explicit-run only) clean a bloated Hermes state.db: archive-first, dry-run by default, test-on-copy, integrity-verified, rollback-capable."
version: 1.0.0
triggers:
  - state.db remediation
  - state.db cleanup
  - clean session database
  - prune sessions
  - drop trigram index
  - memory onboarding remediation
  - state.db too large
metadata:
  hermes:
    tags: [state.db, remediation, cleanup, fts, trigram, pruning, onboarding, safety]
---

# state.db Remediation (Area 1)

The first step of memory-stack onboarding. New users almost always arrive with a
bloated `state.db` (see the `state-db-bloat-forensics` skill for the *why*). This
tool **audits** the damage and, only when explicitly told to, **cleans it up
safely** — archive first, never silently delete.

> **This is explicit-run only.** Nothing here runs from `install.sh` and no cron
> is wired. `audit` / `plan` / `simulate` never modify any database. Only
> `apply` mutates, and only with `--confirm-apply`.

Script: `scripts/state_db_remediate.py` (stdlib only).

## The safety model

| Stage | Mutates? | Gate |
|-------|----------|------|
| `audit` | no | read-only (`mode=ro`) inventory of every `state.db` under `--home` |
| `plan` | no | turns explicit decisions into a reviewable `policy.json` + shows projected deletions |
| `simulate` | no | runs the policy **on a copy**, reports real before/after sizes + integrity + search impact |
| `apply` | **yes** | refuses without `--confirm-apply`; **archives the original first**; cleans a copy, runs `PRAGMA integrity_check`, then atomically swaps; aborts → original untouched |

Every destructive option defaults **off**. A clean copy must pass
`integrity_check` **and** `foreign_key_check` before it can replace the original.
On any failure, the original is left untouched and the archive path is reported.

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# 1) See what you have (read-only, safe).
python3 scripts/state_db_remediate.py audit --home ~/.hermes
python3 scripts/state_db_remediate.py audit --home ~/.hermes --json > /tmp/audit.json

# 2) Decide a policy and write it to a file (dry-run; nothing executed).
python3 scripts/state_db_remediate.py plan --home ~/.hermes \
    --retention-days 90 \
    --prune-closed yes --prune-unclosed no \
    --delete-compression-parents no --drop-trigram no \
    --vacuum yes --out /tmp/policy.json

# 3) Test it on a COPY of one DB (original never touched). Keep the copy to poke.
python3 scripts/state_db_remediate.py simulate \
    --db ~/.hermes/state.db --policy /tmp/policy.json --workdir /tmp/sim

# 4) Apply — ONLY after the gateway/holder for that DB is stopped.
python3 scripts/state_db_remediate.py apply \
    --db /path/to/state.db --policy /tmp/policy.json \
    --archive-dir ~/.hermes/archives/remediation --confirm-apply
```

## The decision gates (policy)

All default to the safe / no-op choice. `plan` writes them into `policy.json`;
`simulate`/`apply` consume that file.

| Policy key | Flag | Default | What it does |
|---|---|---|---|
| `retention_days` | `--retention-days` | none | age threshold for pruning, by **last activity** (30/60/90/180/365/custom). Required if any prune is on. |
| `prune_closed` | `--prune-closed` | `no` | delete **closed** sessions inactive longer than retention (safest prune). |
| `prune_unclosed` | `--prune-unclosed` | `no` | also delete **unclosed** sessions inactive longer than retention. ~85% of sessions never close — these may be resumable/abandoned work. |
| `delete_compression_parents` | `--delete-compression-parents` | `no` | delete `end_reason='compression'` parents **that have a child surviving this run**. Drops the ORIGINAL transcript (recoverable only from the archive). Parents with **no** child — or whose only child is also being pruned — are **kept** so a conversation is never fully erased. Multi-level chains collapse **one generation per run**. |
| `drop_trigram` | `--drop-trigram` | `no` | drop the trigram FTS index (often ~50% of the file). Loses substring/typo-tolerant search; word-level (unicode61) search stays. An FTS-health check verifies the surviving word index keeps its maintenance triggers. |
| `vacuum` | `--vacuum` | `no` | VACUUM after cleanup so the file actually shrinks (otherwise space becomes reusable free pages). |
| `include_dormant_profiles` | `--include-dormant` | `no` | include profiles idle > `dormant_days` when planning across `--home`. |
| `include_snapshots` | `--include-snapshots` | `no` | include pre-update **backup snapshots** when planning across `--home` (your rollback net — off by default). |
| `protect_sources` | `--protect-sources` | `[]` | comma-separated session `source` values to **never** prune (e.g. `telegram`). |
| `protect_recent_days` | `--protect-recent-days` | `2` | never touch sessions **active** within N days (measured by latest message, not start time). |

## What `audit` reports (per DB)

- path, profile, role (`default`/`profile`/`snapshot`), dormant flag, WAL-pending flag
- file size, sidecar (`-wal`/`-shm`/`-journal`) sizes, page/freelist, journal mode, schema version
- sessions / messages counts, unclosed vs ended
- session age distribution: `<7d, 7-30d, 30-90d, 90-180d, >180d`
- `source` breakdown (cross-tool awareness)
- compression parents: total, deletable (with child) vs keep (no child)
- FTS tables + trigram tables, with a footprint estimate per index
- reclaim estimates (vacuum-only / drop-trigram / compression-parents)
- related sibling artifacts (`.bak`) for awareness — never targeted

`dbstat` is used for exact physical sizing when the SQLite build supports it
(most macOS Python builds do **not**); otherwise a clearly-labelled logical
estimate is used. `simulate` always gives the real before/after numbers.

## Schema-variance & cross-profile safety

- Every column/table is introspected before use; older schemas (no `end_reason`,
  no trigram, fewer columns) audit gracefully with warnings instead of crashing.
- Only DBs with **both** `sessions` and `messages` tables are remediable; other
  SQLite files (`kanban.db`, `importance.db`, …) are audit-only and `apply`
  refuses them.
- Symlinked directories are **not** followed by default and are reported either
  way (`--follow-symlinks` to opt in). An explicit `--db` symlink is resolved to
  its real path before any archive/clean/swap, so a link is never severed.
- Pre-update **snapshots** (`state-snapshots`/`pre-update` paths) are skipped by
  `plan` and refused by `apply` unless you pass `--allow-snapshot` /
  `--include-snapshots` — they are your rollback net.
- The tool's own working dirs (`.remediate_work_*`) are excluded from discovery.

### ⚠️ Stop the gateway before `apply`

`apply` runs a **non-mutating** liveness guard: it refuses if a `-wal`/`-journal`
is pending (live or killed-mid-write) and runs a read-only busy probe. It also
re-checks the original is byte-stable just before the atomic swap. **But a
running yet *idle* WAL-mode gateway holds no write lock and is NOT fully
detectable** — if you apply while Hermes is live, writes made after the swap land
on the orphaned old inode and are lost. **Always stop the gateway/holder first.**
`--allow-busy` overrides the guard (not recommended).

## Recovery

Each `apply` writes an archive dir containing `original.tar.gz` (db + sidecars),
`manifest.json` (per-file SHA-256), and `RESTORE.md`. To roll back:

```bash
cd "$(dirname /path/to/state.db)"
rm -f state.db-wal state.db-shm state.db-journal   # remove current sidecars first
tar xzf /path/to/archive/.../original.tar.gz -C .
```

## Verify the tool itself

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/state_db_remediate.py
python3 scripts/state_db_remediate.py --help
python3 -m unittest tests.test_state_db_remediate -v     # 32 safety tests, no live data
# generate a throwaway messy DB to explore:
python3 tests/synthetic_db.py /tmp/messy/state.db --messy
python3 scripts/state_db_remediate.py audit --db /tmp/messy/state.db
```

## Related

- `state-db-bloat-forensics` skill — the diagnosis this remediates.
- `~/.hermes/notes/hermes/state-db-forensics-2026-06-23.md` — full forensic report.
- `plans/memory-onboarding-remediation.md` — Areas 1–5 of onboarding.
