---
name: temporal-migration
description: "Area 4 of memory-stack onboarding — wire hot memory into the bi-temporal layer: verify the temporal DB reconstructs MEMORY.md/USER.md exactly, sync (first-migrate / detect drift), and record an Area 3 rewrite as provenance-preserving temporal events. Never writes live hot files."
version: 1.0.0
triggers:
  - temporal migration
  - memory provenance
  - verify temporal reconstruction
  - memory drift detection
  - version hot memory
  - temporal onboarding
metadata:
  hermes:
    tags: [memory, temporal, bi-temporal, provenance, migration, drift, onboarding]
---

# Temporal Migration (Area 4)

The fourth onboarding step. It connects the §-delimited hot files (`MEMORY.md` /
`USER.md`) to the bi-temporal layer (`temporal_memory.py`) so future audits and
rewrites can **diff, roll back, and trace provenance**. Built on top of the
existing temporal engine (append-only `history.jsonl` + rebuildable
`memory_versions.db`).

> **Never writes `MEMORY.md`/`USER.md`.** Area 4 only ever appends to the
> *temporal layer*, and only with `--confirm-apply`. `verify` is fully read-only.

Script: `scripts/temporal_migrate_onboard.py` (stdlib only; uses
`temporal_memory.TemporalMemory`; no LLM, no network).

## Commands

| Command | What it does | Writes? |
|---|---|---|
| `verify` | reconstruct current `MEMORY.md`/`USER.md` from the temporal DB (replay events → current facts in original order) and confirm it matches the live files **exactly** (onboarding rule #6) | read-only |
| `sync` | compare live files vs the temporal DB: first-migrate an empty store, else detect **drift** (entries added/changed/removed outside the temporal layer) | dry-run unless `--confirm-apply` (writes temporal only) |
| `record-rewrite` | take an Area 3 render `manifest.json` and record the rewrite as temporal events (baseline snapshot → update / merge / delete), preserving the full provenance chain | dry-run unless `--confirm-apply` (writes temporal only) |

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# Does the temporal DB faithfully reconstruct the live hot files? (read-only)
python3 scripts/temporal_migrate_onboard.py verify --home ~/.hermes --json

# First migration, or capture drift from external edits (dry-run first)
python3 scripts/temporal_migrate_onboard.py sync --home ~/.hermes
python3 scripts/temporal_migrate_onboard.py sync --home ~/.hermes --confirm-apply

# Record an accepted Area 3 rewrite into temporal history (provenance)
python3 scripts/temporal_migrate_onboard.py record-rewrite --home ~/.hermes \
    --manifest /tmp/memory-proposed/manifest.json --confirm-apply
```

## How reconstruction works (rule #6)

Each fact's **create-event `seq`** records its original file position. `verify`
reconstructs a store as its current (live, non-tombstoned) facts joined by
`\n§\n` **in first-seen order** — so a faithful migration round-trips
byte-for-byte. `verify` reports:

- `exact_match` — reconstruction is byte-identical to the live file (the goal).
- `content_set_match` / `order_differs` — same entries, different order.
- `entries_only_in_live` — present live but **not** captured in temporal (drift).
- `entries_only_in_temporal` — removed from live but still current (drift).

Exit code: `0` if every store matches exactly, `1` otherwise (drift).

## How a rewrite is recorded (provenance)

For each non-`keep` Area 3 proposal, an event is recorded **under the original
fact's key** (so the chain is `old → new`):

- `rewrite_to_pointer` / `archive_pointer` → `update` (current becomes the
  pointer; the dump survives as a prior version).
- `merge_absorb` → `delete` on the loser (tombstoned; tagged `merged_into:<ref>`).
- `remove` → `delete` (tombstoned; archive path recorded).

A **baseline** snapshot of the pre-rewrite text is recorded first iff the
temporal current doesn't already hold it — so the pre-rewrite state is always in
history. Query the chain with `temporal_memory.py history --key <key>`.

## Drift & exportability

- `sync` detects drift because hot files can be edited outside the temporal layer
  (the live profile here shows exactly this: entries added/removed since the last
  migration). Run `sync --confirm-apply` to capture those edits as events, then
  `verify` matches again.
- Everything is keyed off `--home`, so it works for **any** Hermes user/profile
  (e.g. the Atlas autonomous profile) — no hardcoded paths.

## Safety

- `verify` never writes anything. `sync`/`record-rewrite` are dry-run by default
  and, with `--confirm-apply`, write **only** the temporal layer
  (`history.jsonl` + the rebuildable `memory_versions.db`) — never the live
  `MEMORY.md`/`USER.md`.
- The temporal log is append-only; the SQLite index is a rebuildable projection.

## Verify

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/temporal_migrate_onboard.py
python3 scripts/temporal_migrate_onboard.py --help
python3 -m unittest tests.test_temporal_migrate_onboard -v   # 19 tests, no live data
```

## Related

- `temporal_memory.py` — the bi-temporal engine (stats/current/history/at/diff).
- `memory-rewrite` skill — Area 3 (produces the manifest this records).
- `plans/memory-onboarding-remediation.md` — Areas 1–5.
- Next: **Area 5 — maintenance handoff & self-monitoring**, then the Atlas
  autonomous-profile memory test.
```
