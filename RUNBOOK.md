# Hermes Memory Stack — Operator Runbook (Areas 1→5, start to finish)

The single golden path for cleaning up a memory-overloaded Hermes home and handing
it off to continuous maintenance. Every command below is copy-paste runnable and
was verified against the real CLIs. Follow the steps **in order** — each one
produces a file the next step consumes.

## Fastest path — one command

The driver below runs this entire runbook for you, in the right order, with a
confirmation gate at each mutation. Prefer it; the manual steps that follow are
the per-step reference (and for when you want to tune one step by hand).

```bash
cd ~/.hermes/packages/hermes-memory-stack

# PREVIEW (default) — never modifies anything; writes reviewable proposals under
# <home>/.onboard/proposed/. Safe against any profile.
python3 scripts/memory_onboard.py --home ~/.hermes

# APPLY — asks before each mutation (or --auto to ask only before destructive steps,
# or --apply --yes for non-interactive). Stops cleanly on the first failure; resume
# with --from-step N.
python3 scripts/memory_onboard.py --home ~/.hermes --apply
```

See `skills/memory-onboarding.md` for modes, the step table, and the ordering
rationale. The rest of this file is the manual equivalent.

## Before you start

- **Everything defaults to dry-run / read-only.** The only steps that mutate live
  data are the two `apply` commands and the temporal `--confirm-apply` commands,
  all of which **archive first** and refuse to run without `--confirm-apply`.
- **Set your home once.** These examples use `~/.hermes`; override with
  `HERMES_HOME` or `--home` everywhere if you run against another profile.
- **Work files** live in `/tmp` here so you can inspect them. Nothing is written
  back to live memory except by the explicit apply/confirm steps.
- **Stop the gateway before Step 3** (the only step that rewrites `state.db`).

```bash
export HERMES_HOME=~/.hermes          # the home you are remediating
mkdir -p /tmp/memrun                  # scratch dir for hand-off files
```

File hand-offs at a glance:

| Step | Command | Writes | Next step consumes |
|---|---|---|---|
| 2 | `state_db_remediate plan --out` | `/tmp/memrun/policy.json` | Steps 2–3 `--policy` |
| 4 | `memory_audit --out` | `/tmp/memrun/mem-audit.json` | Steps 5,7 `--audit` |
| 5 | `memory_rewrite render --out-dir` | `/tmp/memrun/proposed/manifest.json` (+ proposed files) | Step 8 `--manifest` |

---

## Area 1 — state.db cleanup

### Step 1 — Audit state.db (read-only)

```bash
python3 ~/.hermes/scripts/state_db_remediate.py audit --home ~/.hermes --json
```

Read the report: total size, the trigram-index share, prunable closed/unclosed
sessions, compression parents. Decide what you are comfortable cleaning.

### Step 2 — Build + simulate a cleanup policy (dry-run, on a COPY)

```bash
# turn explicit decisions into a reviewable policy (writes /tmp/memrun/policy.json)
python3 ~/.hermes/scripts/state_db_remediate.py plan --home ~/.hermes \
    --retention-days 90 --prune-closed yes --prune-unclosed no \
    --drop-trigram no --delete-compression-parents no --vacuum yes \
    --out /tmp/memrun/policy.json

# test that policy on a COPY (original untouched); reports real before/after + integrity
python3 ~/.hermes/scripts/state_db_remediate.py simulate \
    --db ~/.hermes/state.db --policy /tmp/memrun/policy.json --workdir /tmp/memrun/sim
```

Confirm the simulation reports `integrity_check OK` and an acceptable shrink
before continuing. Re-run Step 2 with different decisions until you are happy.

### Step 3 — Apply the cleanup (STOP the gateway first)

```bash
# 1) stop Hermes/the gateway so nothing else holds state.db open, THEN:
python3 ~/.hermes/scripts/state_db_remediate.py apply \
    --db ~/.hermes/state.db --policy /tmp/memrun/policy.json \
    --archive-dir ~/.hermes/archives/remediation --confirm-apply
# archives a tar + SHA-256 manifest + RESTORE.md, verifies integrity, atomic-swaps.
# 2) restart the gateway.
```

---

## Area 2 — hot-memory audit

### Step 4 — Audit MEMORY.md / USER.md (read-only)

```bash
# writes the machine-readable audit that Area 3 consumes
python3 ~/.hermes/scripts/memory_audit.py --home ~/.hermes --json --out /tmp/memrun/mem-audit.json
```

This never modifies the hot files. The report classifies each entry, scores it,
flags near-duplicates / broken pointers / possible contradictions, and recommends
a per-entry action for Area 3.

---

## Area 3 — pointer rewrite & consolidation

### Step 5 — Render rewrite proposals (dry-run; nothing live changes)

```bash
# consumes the Step-4 audit; writes proposals + archived originals + manifest.json
python3 ~/.hermes/scripts/memory_rewrite.py render \
    --audit /tmp/memrun/mem-audit.json --out-dir /tmp/memrun/proposed
```

Produces `/tmp/memrun/proposed/MEMORY.proposed.md`, `USER.proposed.md`, archived
originals, and `manifest.json` (the full `old → new` record Area 4 will replay).

### Step 6 — Review the proposals (human gate)

```bash
# read what WOULD change before any live write
diff <(cat ~/.hermes/memories/MEMORY.md) /tmp/memrun/proposed/MEMORY.proposed.md | less
diff <(cat ~/.hermes/memories/USER.md)   /tmp/memrun/proposed/USER.proposed.md   | less
```

Contradictions are flagged `user_review`, never auto-resolved — decide which side
to keep here. Optionally re-audit the proposed file to confirm it comes back clean:

```bash
python3 ~/.hermes/scripts/memory_audit.py --memory /tmp/memrun/proposed/MEMORY.proposed.md --json | tail -20
```

### Step 7 — Apply the rewrite (archives originals first)

```bash
python3 ~/.hermes/scripts/memory_rewrite.py apply \
    --audit /tmp/memrun/mem-audit.json \
    --archive-dir ~/.hermes/archives/rewrite --confirm-apply
```

---

## Area 4 — temporal migration

> **Ordering note (INTEG-3).** `record-rewrite` records only the *changed* entries,
> so it must record into a temporal layer that already holds the full pre-rewrite
> memory — otherwise the later `sync` invents orphan facts and the layer no longer
> reconstructs live. Two correct ways to do Area 4:
>
> - **Provenance + byte-exact (recommended):** seed the baseline **before** Area 3
>   — run `temporal_migrate_onboard.py sync --home ~/.hermes --confirm-apply` *before*
>   Step 7 (the rewrite apply). The rewrite then auto-records `area3-rewrite`
>   provenance on the existing keys, and Step 9's `sync` finds no drift. This is
>   what `scripts/memory_onboard.py` does (its steps 5 → 7 → 8). On a pre-seeded
>   layer, Step 8 below is an idempotent backstop.
> - **Clean snapshot (no provenance):** skip Step 8 and run only Step 9's `sync`
>   first-migration of the cleaned files.

### Step 8 — Record the rewrite in the temporal layer

```bash
# Records the Area-3 manifest as update/merge/delete events on the EXISTING baseline
# facts (seed the layer first — see the ordering note above). Idempotent.
python3 ~/.hermes/scripts/temporal_migrate_onboard.py record-rewrite \
    --home ~/.hermes --manifest /tmp/memrun/proposed/manifest.json --confirm-apply
```

### Step 9 — Sync any remaining drift into temporal

```bash
python3 ~/.hermes/scripts/temporal_migrate_onboard.py sync --home ~/.hermes            # dry-run drift report
python3 ~/.hermes/scripts/temporal_migrate_onboard.py sync --home ~/.hermes --confirm-apply
```

### Step 10 — Verify temporal reconstructs the live files byte-exact

```bash
python3 ~/.hermes/scripts/temporal_migrate_onboard.py verify --home ~/.hermes --json
# expect "all_match": true — the temporal layer can rebuild MEMORY.md/USER.md exactly.
```

---

## Area 5 — maintenance handoff

### Step 11 — Run the consolidated health + maintenance pass

```bash
python3 ~/.hermes/scripts/memory_health.py      --home ~/.hermes              # green / yellow / red
python3 ~/.hermes/scripts/memory_maintenance.py --home ~/.hermes             # one read-only consolidated pass
# capacity should be back under budget (WARN 80% / CRIT 90%) and state.db under 50 MB.
```

From here, register the two no_agent crons (`crons/memory-health-daily.json`,
`crons/memory-temporal-sync.json`) per `skills/memory-maintenance.md` for ongoing
monitoring. Maintenance is read-only and exits 0 even when it raises alerts.

---

## If something looks wrong

- **Malformed work file** (e.g. a truncated `/tmp/memrun/policy.json`): the tools
  now print an actionable `error: … is not valid JSON …` and exit non-zero — fix
  or regenerate the file from the step that produced it.
- **Rollback Area 1:** restore from `~/.hermes/archives/remediation/` using the
  `RESTORE.md` written next to the archive.
- **Rollback Area 3:** the pre-rewrite originals are in `~/.hermes/archives/rewrite/`
  and inside the Step-5 `proposed/` manifest.
- **Full reference:** `README.md` (per-area detail) and `skills/*.md` (one per area).
```
