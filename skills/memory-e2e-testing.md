---
name: memory-e2e-testing
description: "The shipping gate for the memory-stack onboarding pipeline. Builds a deterministic, fully synthetic messy Hermes home and drives Areas 1→5 through the real CLIs via subprocess, asserting per-step expectations plus rollback, byte-exact temporal reconstruction, and failure tolerance. No live data, ever."
version: 1.0.0
triggers:
  - memory e2e test
  - memory pipeline test
  - synthetic profile
  - shipping gate
  - end to end memory
  - test onboarding pipeline
metadata:
  hermes:
    tags: [memory, testing, e2e, synthetic, pipeline, shipping-gate, read-only, subprocess]
---

# Memory Stack End-to-End Test (the shipping gate)

This is the single test that proves the whole onboarding remediation pipeline
(Areas 1→5) actually works **on a realistic mess** before any real user runs it.
It is the gate to run before shipping a change that touches any remediation script.

Two files:

- `tests/synthetic_profile.py` — builds a deterministic, **fully synthetic** messy
  Hermes home. No live data is read; nothing here resembles a real person's memory.
- `tests/test_e2e_pipeline.py` — drives Areas 1→5 **through the real CLIs via
  `subprocess`** (the shipped entrypoints, not imports) and asserts expectations at
  every step, then rollback, temporal reconstruction, and failure tolerance.

```bash
cd ~/.hermes/packages/hermes-memory-stack

# inspect a profile by hand (deterministic with --seed). --level normal | stress | extreme
python3 tests/synthetic_profile.py /tmp/messy-profile  --level normal --seed 42
python3 tests/synthetic_profile.py /tmp/stress-profile --level stress --seed 42   # 200+ entries, 50MB+ db

# run the gate (builds its own temp profiles; cleans up on success, KEEPS them on failure)
python3 -m unittest tests.test_e2e_pipeline -v        # 16 tests (11 pipeline + 3 failure-tolerance + 1 routing + 1 stress)

# full stack sweep (every test file — the whole suite)
python3 -m unittest \
  tests.test_install tests.test_state_db_remediate tests.test_memory_audit \
  tests.test_memory_rewrite tests.test_temporal_migrate_onboard tests.test_temporal_memory \
  tests.test_memory_health tests.test_memory_maintenance tests.test_memory_auto_extract \
  tests.test_consistency tests.test_e2e_pipeline      # 205 tests
```

## Levels (`--level`)

- **normal** (default) — byte-stable baseline: ~47 MEMORY entries at ~100% capacity,
  ~20 MB state.db. The normal E2E (pipeline steps 00–10) asserts exact behaviour against it.
- **stress** — break-a-naive-system: floors of **200+ MEMORY entries / 40k+ chars (a
  seed-42 build lands ~211 / ~67k, ~4x over the 15k budget)**, USER ~3x over budget,
  a **~60 MB / 142-session / 5,376-message**
  state.db. `TestStressPipeline` drives Areas 1→5 and asserts the KEY outcomes:
  MEMORY comes **under the 15k budget**, USER under 6k, **duplicates → 0**,
  **contradictions → 0**, plus integrity-checked state.db shrink and byte-exact
  temporal reconstruction on the large cleaned file.
- **extreme** — stress with the dial turned further, for manual torture runs.

**Design note (load-bearing):** Area 3 *removes* status updates and *merges*
duplicates with no survivor, but *archives* every content dump / debugging finding
into a ~280-char findable breadcrumb (`↪ … → archived …`) that embeds the note's
absolute path. Two consequences the stress test is built around:

1. **Char-bulk must be compressible.** A real mess's "30+ dumps / 20+ debugging"
   would each leave a ~280-char breadcrumb, so packing all of them in makes "under
   the 15k budget in ONE pass" mathematically impossible (breadcrumbs alone exceed
   it). The profile therefore carries its bulk in status updates (removed) and
   duplicate pairs (merged), and bounds the archivable/kept categories. That cap is
   itself the finding: **one pass cannot budget-compress an arbitrarily rich home.**
   The test honestly asserts the home stays *over* the 35-entry ceiling afterward
   (health rates it `red` on entry count) while the *char* budget is met.
2. **The budget assertion is path-normalized.** Because breadcrumbs embed the
   absolute note path, raw file size grows with the temp-dir length. The test
   subtracts the temp-root contribution (`len(text) − text.count(root)·len(root)`)
   so the gate measures the *pipeline's* compression, not the temp dir — robust from
   a 10-char `/tmp` root to a 150-char nested CI root. The profile is also built
   under a short `/tmp` root so the literal on-disk size is a faithful check too.

## Safety (hard rules honored)

- **Never touches live `~/.hermes`.** Every profile is built under a fresh
  `tempfile.mkdtemp()`; every CLI is invoked with `--home`/`--user-home` pointing at
  that temp dir. Live data is neither read nor written.
- **Real entrypoints.** Scripts run via `subprocess.run([sys.executable, script,
  ...])`, so the test exercises argument parsing, exit codes, and stdout/JSON
  contracts — the actual operator experience, not internal functions.
- **Deterministic.** Seeded RNG (`--seed 42`) and no wall-clock dependence in the
  fixture, so failures reproduce.
- **Keep-on-failure.** The profile is `shutil.rmtree`'d only when every step passed;
  on failure the temp path is printed for debugging.

## What the synthetic profile plants

`build_profile(root, seed=42)` writes a home that contains exactly the problems
onboarding must fix:

- **MEMORY.md** — near-full (40+ entries, ~90%+ capacity): content dumps (with and
  without pointer paths), salted-distinct status updates, duplicate pairs,
  contradiction pairs (e.g. "Default coding model is Foo-7B" vs "now Bar-9000"),
  debugging/project/todo/preference entries, valid pointers, and broken pointers.
- **USER.md** — near-full (~95%): preferences (including a metric-bearing one that
  must survive consolidation), a few dumps, status lines.
- **state.db** — bloated (~20 MB, 65 sessions / ~2,170 messages): compression
  parents + children, unclosed + closed sessions across sources, the FTS5 trigram
  index, and prunable old sessions.
- **notes/**, an **empty temporal layer** (`_versions/`), **stale `_auto_extract`
  candidates**, and a **`cron/jobs.json`** carrying the broken capacity-monitor
  `error` status.

Tune detection thresholds by changing the fixture, not the assertions: salts in
`_para()` keep generated paragraphs from colliding as accidental duplicates, and
the contradiction pairs use the `now` keyword so the human-resolution step can pick
the current side deterministically.

## Pipeline steps asserted (in order)

| Step | Area | Assertion |
|------|------|-----------|
| 00 | — | initial profile is genuinely messy (MEMORY ≥85%, USER ≥90%, state.db ≥5 MB) |
| 01 | 1 | audit→plan→simulate (integrity OK, >40% shrink) → `apply --confirm-apply` (live shrinks >40%, archived w/ SHA-256, post-swap integrity OK) |
| 02 | 2 | audit detects ≥5 dumps, ≥2 dup pairs, ≥2 contradictions, ≥3 status updates, ≥1 broken pointer |
| 03 | 3 | `render` → proposed MEMORY <70% / USER <85% that re-audits clean |
| 04 | 3 | `apply --confirm-apply` changes live hot files + archives recoverable originals + manifest |
| 05 | human | contradictions are `user_review`; the test keeps the `now` side, drops the stale one |
| 06 | 4 | `sync --confirm-apply` then `verify` → **ALL MATCH**, exit 0 |
| 07 | 5 | maintenance pass: all 6 steps run, `overall` green/yellow, `hot_files_untouched=True`; health exits 0 |
| 08 | — | final MEMORY ≤70% / USER ≤85%, 0 dups, 0 contradictions, ≤35 entries |
| 09 | — | rollback: archived pre-rewrite SHA-256 == manifest source SHA; a known original entry is recoverable |
| 10 | — | temporal `verify` still ALL MATCH (byte-exact reconstruction of cleaned files) |

## Failure tolerance (`TestFailureTolerance`)

- skipping a maintenance step (`--skip audit`) lets the other steps complete;
- a home with **no temporal layer** still runs the pass and exits 0;
- `memory_health.py` on a minimal home returns a valid green/yellow/red score.

## Coverage boundaries (what a green run does NOT yet prove)

Be honest about the edges so a green check isn't over-trusted. The gate currently
exercises a *first curation of a messy home* plus one second-sync. It does **not**
yet cover, and these are good next additions:

- **Polarity contradictions** (e.g. "X is enabled" vs "X is now paused"). Only
  default-vs-default value contradictions are planted; the audit's polarity path is
  covered by Area-2 unit tests, not by this E2E.
- **Cross-file duplicates** (the same preference in both MEMORY.md and USER.md).
- **Hostile input shapes**: non-UTF8 bytes, CRLF line endings, a single very-long
  entry, emoji/unicode beyond the `↪`/`→` already planted, a malformed/truncated
  pointer. The scripts open files as UTF-8; a stray non-UTF8 byte in real data would
  need its own degradation test.
- **Deep provenance**: `record-rewrite` (rewrite → temporal events with old→new
  provenance) is covered by Area-4 unit tests; the E2E drives `sync`/`verify` and a
  second-sync append, not the full record-rewrite chain.
- **Byte-for-byte fixture determinism**: content is seed-stable only for a fixed
  root (entries embed absolute note paths), so the suite asserts behavior, not a
  golden fixture hash.

## When this gate fails

1. Read the kept profile path printed by `tearDownClass` and inspect the actual
   files / `state.db`.
2. Re-run the specific failing step's CLI by hand against that profile with the same
   flags the test used (they're visible in `test_e2e_pipeline.py`).
3. If detection counts drift, adjust the **fixture** (`synthetic_profile.py`), not
   the thresholds — the thresholds encode the onboarding contract.
4. `MM_STEP_ORDER` in the test mirrors `memory_maintenance.STEP_ORDER`; keep them in
   sync if you add/remove a maintenance step.
