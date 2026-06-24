---
name: memory-maintenance
description: "Area 5 of memory-stack onboarding — self-monitoring + maintenance handoff: a read-only health score (green/yellow/red) and a single consolidated 6-step maintenance pass (temporal_sync→temporal_verify→auto_extract→audit→state_db_remediate→capacity). Script-only, no provider/gateway/Telegram dependency, exit-0 even when alerting."
version: 1.0.0
triggers:
  - memory health
  - memory maintenance
  - memory stack monitoring
  - memory drift watchdog
  - memory capacity alert
  - memory cron
metadata:
  hermes:
    tags: [memory, maintenance, monitoring, health, cron, drift, no-agent, read-only]
---

# Memory Maintenance & Self-Monitoring (Area 5)

The final onboarding step: wire all memory-stack components into one coordinated,
**safe, self-monitoring** maintenance system. Two scripts, both stdlib-only,
script-only (no_agent), read-only over hot memory, and **exit 0 on success even
when they raise alerts** (the lesson from the broken capacity monitor — severity
lives in the content + score, never the exit code).

- `scripts/memory_health.py` — unified green/yellow/red status (the daily check).
- `scripts/memory_maintenance.py` — one consolidated maintenance pass.

## Cron safety (hard rules honored)

- **No gateway restart, no `launchctl kickstart`, no Telegram interference** —
  none of these scripts touch the gateway, any profile, or Telegram.
- **Script-only / `no_agent: true`** — no provider/LLM/network call in the
  default path (the auto-extractor is reported-from-disk by default; invoking it
  is opt-in via `--run-extract`).
- **Exit 0 on success** even when red — the cron scheduler will not misread an
  alert as a failed job. (This is exactly why the old Capacity Monitor showed
  `error`: it propagated the curator's `exit 2` = critical. Fixed in
  `~/.hermes/scripts/memory_curator_monitor.py`; superseded by the daily health
  check below.)
- **Individually pausable**, each step **skippable** (`--skip <step>`), and
  **dry-run by default**.

## memory_health.py (daily, read-only)

```bash
python3 scripts/memory_health.py --home ~/.hermes            # markdown
python3 scripts/memory_health.py --home ~/.hermes --json     # machine-readable
python3 scripts/memory_health.py --summary                   # one line
```

Checks (read-only): hot-file capacity vs budget (15000/6000) + entry pressure
(25 target / 35 ceiling); temporal drift (runs the Area 4 verify against a
**copy** of the temporal DB so the live `memory_versions.db` is never even
re-indexed); memory-cron `last_status` from `cron/jobs.json` (flags non-paused
errors); live `state.db` sizes (warn ≥50 MB, critical ≥200 MB; remediation plan ≥30 MB); semantic daemon presence
(optional); latest auto-extraction candidates. Rolls up to **green/yellow/red**
(semantic/auto-extract are informational and never drive red).

## memory_maintenance.py (weekly, read-only reporter)

```bash
python3 scripts/memory_maintenance.py --home ~/.hermes               # dry-run report
python3 scripts/memory_maintenance.py --home ~/.hermes --apply-temporal-sync
python3 scripts/memory_maintenance.py --home ~/.hermes --json --skip auto_extract
```

One pass, in order (6 steps): **temporal_sync → temporal_verify → auto_extract →
audit → state_db_remediate → capacity**, then a consolidated report (JSON or
markdown). `state_db_remediate` flags oversized `state.db` files (read-only); a
dead/erroring auto-extractor reports as an **alert**, never green. Each step is skippable and
**partial-failure tolerant** (one step crashing never aborts the rest). It
**NEVER writes MEMORY.md/USER.md** — every report records
`hot_files_untouched: true` (a SHA check), and `main` hard-fails if that's ever
false. The only optional write is `--apply-temporal-sync`, which appends drift
events to the **temporal layer only**.

### Where the actual writes happen (all separately gated)
Maintenance only *reports*. Hot-memory changes go through:
- Memory Curator daily sweep (existing cron) — stale cleanup with pointers.
- `memory_auto_extract.py --write` — not enabled; dry-run only until trusted.
- `temporal_migrate_onboard.py sync --confirm-apply` — temporal layer only.
- `memory_rewrite.py apply --confirm-apply` — Area 3 hot-file rewrite.

## Cron definitions (defined here, NOT installed)

Two no_agent, local-delivery crons consolidate the read-only memory checks into
one daily + one weekly pass. **What the weekly pass actually does vs the three
package crons it stands in for:**

- **temporal-ingest** → *subsumed*: `--apply-temporal-sync` captures hot-memory
  drift into the temporal layer — but **weekly, not nightly**. If you need daily
  bi-temporal granularity, keep a nightly temporal ingest too.
- **auto-extraction-dry-run** → *monitored, not performed*: the pass reports the
  **last** candidates from disk; it does not re-run the extractor (that needs a
  provider) unless you pass `--run-extract`.
- **semantic-reindex** → *monitored, not performed*: the pass reports whether the
  daemon socket is present; it does **not** rebuild ChromaDB. If you want fresh
  vectors, keep the semantic-reindex cron (it needs `chromadb`).

So this is a **monitor + temporal-capture** consolidation, not a full replacement
for active reindex/extraction. The two crons:

| Cron file | Schedule | Calls | Purpose |
|---|---|---|---|
| `crons/memory-health-daily.json` | `0 6 * * *` | `memory_health_cron.sh` | daily health (replaces the every-6h capacity monitor) |
| `crons/memory-temporal-sync.json` | `0 5 * * 6` (Sat) | `memory_maintenance_cron.sh` | weekly maintenance + temporal drift capture |

**To install (operator step — left to Hermes, not done automatically):**
1. Copy `scripts/memory_health.py`, `memory_maintenance.py`,
   `memory_health_cron.sh`, `memory_maintenance_cron.sh` (+ Areas 1–4 scripts)
   into `~/.hermes/scripts/`.
2. Register the two cron JSONs.
3. **Pause** the every-6h Capacity Monitor (`6aabc8745056`) — superseded by the
   daily health check. (Its exit-code bug is already fixed, so it's harmless if
   kept, just redundant.)
4. Do **not** install the standalone `semantic-reindex` / `auto-extraction-dry-run`
   / `temporal-ingest` crons — their work is integrated into the weekly pass.

6 AM (health) and Sat 5 AM (maintenance) deliberately avoid the 3:35–4:00 AM
memory-cron cluster and the Sunday 4 AM consolidation.

## Verify

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/memory_health.py scripts/memory_maintenance.py
python3 scripts/memory_health.py --help
python3 scripts/memory_maintenance.py --help
python3 -m unittest tests.test_memory_health tests.test_memory_maintenance -v   # 27 tests, no live data
```

## Related

- `temporal-migration` (Area 4), `memory-rewrite` (Area 3), `memory-audit`
  (Area 2), `state-db-remediation` (Area 1).
- `plans/memory-onboarding-remediation.md` — Areas 1–5 (this completes the set).
- Next: run the full Area 1→5 pipeline against the **Atlas autonomous profile**
  (`~/.hermes/profiles/autonomous/`) as the first non-Emeka export validation.
```
