# Hermes Memory Stack

A modular memory OS layer for Hermes Agent: semantic session retrieval, conservative auto-extraction, temporal versioning, and a planned first-install remediation pipeline for users who already have memory overload.

## Current Package Status

| Component | Status | Included files |
|---|---:|---|
| Semantic retrieval | ✅ Built + verified | `scripts/semantic_index.py`, `semantic_query.py`, `semantic_reindex.sh` |
| Auto-extraction | ✅ Built + verified | `scripts/memory_auto_extract*.py`, fixtures, `hermes_memory_intake_gate.py` |
| Temporal versioning | ✅ Built + verified | `scripts/temporal_memory.py`, `temporal_migrate.py` |
| Export installer | 🟡 Draft, syntax-checked | `install.sh` |
| Curator/dreaming maintenance | 🟡 Existing in Emeka's environment, not bundled yet | Remediation plan only |
| Remediation Area 1: state.db | ✅ Built + tested, **explicit-run only** | `scripts/state_db_remediate.py`, `tests/`, `skills/state-db-remediation.md` |
| Remediation Area 2: MEMORY.md audit | ✅ Built + tested, **read-only** | `scripts/memory_audit.py`, `tests/test_memory_audit.py`, `skills/memory-audit.md` |
| Remediation Area 3: pointer rewrite | ✅ Built + tested, **dry-run/render only** | `scripts/memory_rewrite.py`, `tests/test_memory_rewrite.py`, `skills/memory-rewrite.md` |
| Remediation Area 4: temporal migration | ✅ Built + tested, **verify read-only; sync/record gated** | `scripts/temporal_migrate_onboard.py`, `tests/test_temporal_migrate_onboard.py`, `skills/temporal-migration.md` |
| Remediation Area 5: maintenance & monitoring | ✅ Built + tested, **read-only, no_agent, exit-0** | `scripts/memory_health.py`, `scripts/memory_maintenance.py`, `crons/memory-health-daily.json`, `crons/memory-temporal-sync.json`, `skills/memory-maintenance.md` |
| E2E shipping gate | ✅ Built + tested, **synthetic profile, subprocess-driven** | `tests/synthetic_profile.py`, `tests/test_e2e_pipeline.py`, `skills/memory-e2e-testing.md` |

**Important:** the onboarding remediation pipeline (Areas 1–5) plus the end-to-end shipping gate (normal + stress levels) are implemented and tested (**205 tests**, incl. a cross-file consistency gate). The E2E harness drives Areas 1→5 through the real CLIs on synthetic messy profiles (never touching live data). Remaining before public ship: an end-to-end run against a real non-Emeka profile (the Atlas autonomous profile).

## Philosophy

Most users who need this already have a memory problem. The stack must eventually do two things:

1. **Remediate first install:** audit, archive, clean, consolidate, pointer-rewrite, version, then hand off.
2. **Maintain continuously:** semantic indexing, dry-run extraction, temporal ingest, curator/dreaming, alerts.

The safety rule is: **archive first, never permanently delete by default.**

## Install Tiers

```bash
# Syntax-checked installer; use carefully while package is still pre-release.
./install.sh semantic     # Tier 1: semantic retrieval only
./install.sh extraction   # Tier 2: auto-extraction only, dry-run by default
./install.sh temporal     # Tier 3: temporal versioning only
./install.sh remediation  # Areas 1–5: state.db / audit / rewrite / health / maintenance scripts
./install.sh crons        # copy the no_agent cron JSONs (none are auto-registered)
./install.sh config       # copy config/ defaults + signal words
./install.sh all          # everything above, in order, then runs `verify`
./install.sh verify       # report what is installed (no changes)
```

`all` installs the full stack (Tiers 1–3 + Areas 1–5 + crons + config) and then
verifies — it does **not** skip anything. The onboarding remediation pipeline
needs the `remediation` tier, which `all` includes (run `./install.sh remediation`
alone if you only want the Area 1–5 CLIs).

## Verification Commands

Run from the package root after install or against the live scripts:

```bash
# Semantic retrieval
python3 ~/.hermes/scripts/semantic_query.py --ping
python3 ~/.hermes/scripts/semantic_query.py "memory system architecture" --n 5

# Auto-extraction (safe; writes nothing)
python3 ~/.hermes/scripts/memory_auto_extract.py --dry-run --days 1 --json
python3 ~/.hermes/scripts/memory_auto_extract_eval.py
python3 ~/.hermes/scripts/memory_auto_extract_eval.py --fixtures-file ~/.hermes/scripts/memory_auto_extract_fixtures_holdout.jsonl
python3 ~/.hermes/scripts/memory_auto_extract_eval.py --fixtures-file ~/.hermes/scripts/memory_auto_extract_fixtures_adversarial.jsonl

# Temporal versioning
python3 ~/.hermes/scripts/temporal_memory.py stats --json
python3 ~/.hermes/scripts/temporal_memory.py history --key local-llm --json
python3 ~/.hermes/scripts/temporal_memory.py diff --key local-llm --from 2026-06-21 --to 2026-06-23
```

## Cron Policy

Safe default cron posture:

- semantic reindex: safe to run nightly
- auto-extraction: **dry-run only** until reviewed for at least a week
- temporal ingest: safe to run nightly, because it records sidecar history only
- `--write` for auto-extraction: do **not** enable until precision is trusted on the user's actual data

See `crons/*.json` for draft no-agent cron definitions.

## Remediation Area 1: state.db (explicit-run only)

The first onboarding step. New installs usually arrive with a bloated `state.db`
(unbounded growth: `auto_prune` ships off, own-content FTS5 indexes duplicate
every message, the trigram index alone can be ~50% of the file, compression is
additive). `scripts/state_db_remediate.py` audits this and — only when explicitly
asked — cleans it conservatively.

**Nothing runs automatically.** `install.sh` does not invoke it and no cron is
wired. `audit`/`plan`/`simulate` are read-only/copy-only; only `apply` mutates,
and only with `--confirm-apply` after archiving the original.

```bash
# read-only inventory of every state.db under a home
python3 scripts/state_db_remediate.py audit --home ~/.hermes --json

# turn explicit decisions into a reviewable policy (dry-run)
python3 scripts/state_db_remediate.py plan --home ~/.hermes \
    --retention-days 90 --prune-closed yes --prune-unclosed no \
    --drop-trigram no --delete-compression-parents no --vacuum yes \
    --out /tmp/policy.json

# test the policy on a COPY (original untouched); reports real before/after + integrity
python3 scripts/state_db_remediate.py simulate --db ~/.hermes/state.db \
    --policy /tmp/policy.json --workdir /tmp/sim

# apply (stop the gateway/holder first); archives + verifies + atomic swap
python3 scripts/state_db_remediate.py apply --db /path/to/state.db \
    --policy /tmp/policy.json --archive-dir ~/.hermes/archives/remediation --confirm-apply

# 32 safety tests, no live data:
python3 -m unittest tests.test_state_db_remediate -v
```

Safety model: dry-run default · archive-first (tar + SHA-256 manifest + `RESTORE.md`)
· test-on-copy · `PRAGMA integrity_check` + `foreign_key_check` before swap · every
destructive option off by default · schema-variance tolerant · refuses non-session
DBs and busy DBs. Full details: `skills/state-db-remediation.md`.

## Remediation Area 2: MEMORY.md / USER.md audit (read-only)

The second onboarding step. `scripts/memory_audit.py` assesses the hot-memory
files (`§`-delimited `MEMORY.md` pointers + `USER.md` preferences) and produces a
per-entry quality report + recommended actions for Area 3. It **never** rewrites
the inputs — the only write is an explicit `--out`.

```bash
python3 scripts/memory_audit.py                          # read-only -> markdown
python3 scripts/memory_audit.py --json --out /tmp/mem-audit.json
python3 scripts/memory_audit.py --strict --max-entry-chars 280
python3 -m unittest tests.test_memory_audit -v           # 38 tests, synthetic only
```

Per entry it assigns a kind (pointer / preference_fact / content_dump /
status_update / debugging_finding / project_progress / todo_temporary /
malformed), six 0..1 scores, and one action (keep / rewrite_to_pointer /
archive_to_note / merge / verify_current / move_to_skill / move_to_note /
remove_after_archive / user_review). Cross-checks: file-path existence (broken
pointers), deterministic token-Jaccard near-duplicates, and conservative
`default`-vs-`default` / `enabled`-vs-`paused` contradiction flags (labelled
"possible", never asserted). Full details: `skills/memory-audit.md`.

## Remediation Area 3: pointer rewrite & consolidation (dry-run/render only)

The third onboarding step. `scripts/memory_rewrite.py` consumes the Area 2 audit
and produces reviewable `old → new` proposals + proposed `MEMORY.proposed.md` /
`USER.proposed.md`. It **never** modifies live files in `plan`/`render`; an
`apply` exists but is gated by `--confirm-apply` and archives originals first.

```bash
python3 scripts/memory_rewrite.py plan   --audit /tmp/mem-audit.json          # dry-run
python3 scripts/memory_rewrite.py render --audit /tmp/mem-audit.json --out-dir /tmp/proposed
python3 -m unittest tests.test_memory_rewrite -v                              # 23 tests, synthetic only
```

Per audit action: keep (byte-exact), rewrite_to_pointer (only with a real
referenced file — never fabricated), archive_to_note/move_to_note/move_to_skill
(↪ pointer + original archived), merge (absorb the weaker duplicate into the
stronger), remove_after_archive (drop only after archiving), verify_current /
user_review (preserved + flagged). **Never-lose:** every changed/removed entry's
original is in the manifest (+ an archive file in render). Proposed output
re-audits cleanly with `memory_audit.py`. Full details: `skills/memory-rewrite.md`.

## Remediation Area 4: temporal migration (verify read-only; sync/record gated)

The fourth onboarding step. `scripts/temporal_migrate_onboard.py` wires hot
memory into the bi-temporal layer (`temporal_memory.py`) for diff/rollback/
provenance. It **never** writes `MEMORY.md`/`USER.md` — only the temporal layer
(`history.jsonl` + the rebuildable `memory_versions.db`), and only with
`--confirm-apply`.

```bash
python3 scripts/temporal_migrate_onboard.py verify --home ~/.hermes --json   # read-only
python3 scripts/temporal_migrate_onboard.py sync   --home ~/.hermes          # dry-run drift report
python3 scripts/temporal_migrate_onboard.py sync   --home ~/.hermes --confirm-apply
python3 scripts/temporal_migrate_onboard.py record-rewrite --home ~/.hermes \
    --manifest /tmp/proposed/manifest.json --confirm-apply
python3 -m unittest tests.test_temporal_migrate_onboard -v                    # 19 tests, no live data
```

- `verify` reconstructs `MEMORY.md`/`USER.md` from current facts in first-seen
  (original file) order and confirms a byte-exact round-trip (rule #6); reports
  drift otherwise (entries only-in-live / only-in-temporal).
- `sync` first-migrates an empty store or detects drift from external edits;
  `--confirm-apply` captures the deltas as events.
- `record-rewrite` records an Area 3 rewrite as `update`/`merge`/`delete` events
  under the original fact key (baseline snapshot first), preserving the full
  `old → new` provenance chain. Full details: `skills/temporal-migration.md`.

## Remediation Area 5: maintenance & self-monitoring (read-only, no_agent, exit-0)

The final onboarding step ties everything together safely. Two stdlib-only,
script-only scripts that **never** touch the gateway/Telegram and **exit 0 on
success even when alerting** (severity is in the content + score, not the exit
code — the bug that made the old Capacity Monitor show `error`, now fixed):

```bash
python3 scripts/memory_health.py --home ~/.hermes --json          # green/yellow/red
python3 scripts/memory_maintenance.py --home ~/.hermes            # one consolidated read-only pass
python3 scripts/memory_maintenance.py --home ~/.hermes --apply-temporal-sync   # +capture drift (temporal only)
python3 -m unittest tests.test_memory_health tests.test_memory_maintenance -v  # 27 tests, no live data
```

`memory_health.py` rolls up hot-file capacity + entry pressure, temporal drift
(verified against a **copy** so live is never re-indexed), memory-cron statuses,
state.db sizes, and semantic/auto-extract into one score. `memory_maintenance.py`
runs `temporal_sync → verify → auto_extract → audit → state_db_remediate → capacity` (6 steps) in one pass,
partial-failure tolerant, and asserts `hot_files_untouched`. Two no_agent crons
(`crons/memory-health-daily.json` @ 6 AM, `crons/memory-temporal-sync.json` @
Sat 5 AM) consolidate the read-only checks and replace the noisy every-6h
monitor. The weekly pass *subsumes* temporal-ingest (drift capture, weekly) and
*monitors* (does not perform) semantic reindex + active auto-extraction — keep
those crons if you need fresh vectors / nightly extraction. **Defined, not
installed** — see `skills/memory-maintenance.md` for operator install + the
recommendation to pause cron `6aabc8745056`.

## End-to-End Shipping Gate (synthetic messy profile)

The one test that proves the whole onboarding pipeline works on a realistic *mess*
before any real user runs it. It builds a fully synthetic, deterministic messy
Hermes home (no live data, ever) and drives Areas 1→5 **through the real CLIs via
subprocess** — exactly as an operator would — asserting expectations at every
step plus rollback, temporal reconstruction, and failure tolerance.

```bash
# build a messy profile by hand to inspect it (MEMORY ~full, USER ~full, ~20MB state.db)
python3 tests/synthetic_profile.py /tmp/messy-profile  --level normal --seed 42

# build the STRESS profile (seed 42) — 211 MEMORY entries / ~67k chars (~4x over
# the 15k budget), USER ~3x over budget, a ~60 MB / 142-session / 5,376-message state.db
# (the harness asserts only the floors: 200+ entries, 40k+ chars, 50MB+ state.db)
python3 tests/synthetic_profile.py /tmp/stress-profile --level stress --seed 42

# the gate itself (builds its own temp profiles, cleans up on success, keeps on failure)
python3 -m unittest tests.test_e2e_pipeline -v        # 16 tests (11 pipeline + 3 failure-tolerance + 1 routing + 1 stress)
```

`synthetic_profile.py` plants the exact problems onboarding must fix: a near-full
MEMORY.md (40+ entries, content dumps, status updates, duplicate pairs,
contradiction pairs, broken pointers), a near-full USER.md, a bloated `state.db`
(65 sessions / ~2,170 messages / ~20 MB with the trigram index and prunable
sessions), `notes/`, an empty temporal layer, stale auto-extract candidates, and a
`cron/jobs.json` carrying the broken-capacity-monitor `error`. The harness then,
in order:

1. **Area 1** — audit → plan → simulate-on-copy (integrity OK, >40% shrink) →
   `apply --confirm-apply` (live `state.db` shrinks >40%, archived with SHA-256).
2. **Area 2** — audit detects ≥5 dumps, ≥2 dup pairs, ≥2 contradictions, ≥3 status
   updates, ≥1 broken pointer.
3. **Area 3** — `render` proposes MEMORY <70% / USER <85% that re-audits clean;
   `apply --confirm-apply` rewrites live and archives recoverable originals.
4. **Human gate** — contradictions are flagged `user_review` (never auto-resolved);
   the test simulates the human keeping the current side and dropping the stale one.
5. **Area 4** — `sync --confirm-apply` then `verify` returns **ALL MATCH**
   (temporal reconstructs the cleaned hot files byte-exact).
6. **Area 5** — one maintenance pass: all 6 steps run, `overall` green/yellow,
   `hot_files_untouched=True`; `memory_health.py` exits 0.
7. **Final + rollback + reconstruction** — final MEMORY ≤70% / USER ≤85%, 0 dups,
   0 contradictions, ≤35 entries; archived pre-rewrite SHA-256 matches the manifest
   and a known original entry is recoverable; temporal `verify` still ALL MATCH.

A separate `TestFailureTolerance` proves graceful degradation: skipping a step lets
the others complete, a home with **no temporal layer** still exits 0, and health on
a minimal home returns a valid score. Everything runs in temp dirs; live
`~/.hermes` is never read or written. See `skills/memory-e2e-testing.md`.

### Stress level (`--level stress`)

`TestStressPipeline` runs the same Areas 1→5 against a profile built to break a
naive memory system: **211 MEMORY entries / ~67k chars (~4x over the 15k budget)**,
a USER.md ~3x over budget, and a **~60 MB / 142-session / 5,376-message** `state.db`
(trigram index, compression parents/children, 6-month span, five sources incl.
discord). One pass takes it to: **MEMORY ~13k chars / ~12.4k path-normalized (91
entries — char budget met, still over the 35-entry ceiling), USER under 6k,
duplicates 33→0, contradictions 12→0, state.db ~60 MB→~12 MB.**

Calibrating this surfaced a real pipeline property worth knowing: Area 3 *removes*
status updates and *merges* duplicates wholesale, but *archives* each content
dump / debugging finding into a ~280-char findable breadcrumb (`↪ … → archived …`)
— so archivable entries shrink, they don't vanish. The stress profile therefore
carries its char-bulk in status + duplicates (which disappear) so a **single pass**
can land the char budget; packing in the "30+ dumps / 20+ debugging" a real mess
would have makes under-budget-in-one-pass impossible (breadcrumbs alone exceed it),
so the test honestly asserts the home stays **over the 35-entry ceiling** afterward
(health rates it `red` on entries) even though the char budget is met. The budget
gate is **path-normalized** (it subtracts the temp-root portion of embedded note
paths) so it measures the pipeline's compression, not the temp-dir length — robust
from a 10-char `/tmp` root to a 150-char nested CI root.

## Known Gaps Before Public Ship

1. Implement remaining onboarding remediation areas:
   - ~~state.db cleanup with decision gates~~ ✅ done (Area 1, explicit-run only)
   - ~~MEMORY.md audit/quality scoring~~ ✅ done (Area 2, read-only)
   - ~~pointer rewrite/consolidation~~ ✅ done (Area 3, dry-run/render only)
   - ~~temporal migration after cleanup~~ ✅ done (Area 4, verify read-only; sync/record gated)
   - ~~maintenance handoff/self-monitoring~~ ✅ done (Area 5, read-only no_agent crons)
   - ~~synthetic messy-profile E2E harness~~ ✅ done (`tests/test_e2e_pipeline.py`, 16 tests)
   - **All 5 onboarding areas + the E2E shipping gate (normal + stress) implemented and tested (205 tests).** Remaining: a real Atlas-profile dry-run before public ship.
2. Add semantic dedup to auto-extraction so paraphrased known facts do not bloat memory.
3. Add launchd/keepalive or watchdog for semantic daemon.
4. Validate installer with `HERMES_HOME` pointing to a temporary profile, not Emeka's live profile.
5. Decide how much of curator/dreaming should be bundled vs treated as existing Hermes features.

## Internal Docs

- `plans/memory-stack-packaging-design.md`
- `plans/memory-onboarding-remediation.md`
- `plans/memory-auto-extraction-plan.md`
- `plans/memory-semantic-retrieval-plan.md`
- `plans/temporal-memory-versioning-design.md`

## External Comparison Direction

Use our docs as a reference, not source of truth. Compare against Mem0, Letta/MemGPT, Zep/Graphiti, Cognee, LangMem/LangGraph, LlamaIndex memory, and hybrid retrieval systems. The goal is not to prove Hermes is right — it is to build the best practical memory OS for agents.
