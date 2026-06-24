---
name: memory-onboarding
description: "The single command that runs the whole memory-stack cleanup (Areas 1→5) end to end: audit + clean state.db, audit + rewrite hot memory, record temporal provenance, verify, and hand off to maintenance. Dry-run by default; never mutates live without --apply."
version: 1.0.0
triggers:
  - onboard memory
  - clean up my memory
  - memory onboarding
  - run the whole memory pipeline
  - messy memory to clean
  - memory stack onboarding
metadata:
  hermes:
    tags: [memory, onboarding, pipeline, driver, dry-run, areas-1-5, one-command]
---

# Memory Onboarding — one command, Areas 1→5

`scripts/memory_onboard.py` takes a memory-overloaded Hermes home from **messy →
clean → maintenance-enabled** in a single command. It is the shippable form of
`RUNBOOK.md`: it runs every step in order, hands each step's artifact to the next,
gates every mutation behind an explicit confirmation, and stops cleanly on the
first failure so nothing is left half-done.

> **You do not need to read the other Area skills to onboard a home.** Run the
> dry-run, read the proposals it writes, then run `--apply`. The per-area skills
> (`state-db-remediation`, `memory-audit`, `memory-rewrite`,
> `temporal-migration`, `memory-maintenance`) are reference for when you want to
> drive or tune one step by hand.

## Quick start

```bash
cd ~/.hermes/packages/hermes-memory-stack

# 1) PREVIEW (default) — never modifies anything. Writes reviewable proposals.
python3 scripts/memory_onboard.py --home ~/.hermes

# 2) Review what WOULD change:
#    <home>/.onboard/proposed/{MEMORY.proposed.md,USER.proposed.md,manifest.json}
#    <home>/.onboard/mem-audit.json   <home>/.onboard/policy.json

# 3) APPLY for real — asks before each mutation:
python3 scripts/memory_onboard.py --home ~/.hermes --apply

# Semi-automatic (auto-confirm safe steps, ask only before destructive ones):
python3 scripts/memory_onboard.py --home ~/.hermes --auto

# Fully non-interactive (automation/tests):
python3 scripts/memory_onboard.py --home ~/.hermes --apply --yes
```

## The ten steps

| # | Area | Step | Kind |
|---:|---|---|---|
| 1 | 1 | Audit `state.db` | read-only |
| 2 | 1 | Plan + simulate a cleanup policy (on a **copy**) | dry-run |
| 3 | 1 | Apply `state.db` cleanup *(stop the gateway first)* | **destructive** |
| 4 | 2 | Audit hot memory → `mem-audit.json` | read-only |
| 5 | 4a | **Seed** the temporal baseline (`sync` first-migration) | append-only |
| 6 | 3 | Render rewrite proposals → `proposed/manifest.json` | dry-run |
| 7 | 3 | Apply rewrite **(auto-records `area3-rewrite` provenance)** | **destructive** |
| 8 | 4b | Reconcile temporal drift (`sync`) | append-only |
| 9 | 4 | Verify temporal reconstructs live byte-exact | read-only |
| 10 | 5 | Maintenance + health pass | read-only |

**Why seed (step 5) before the rewrite (step 7):** the temporal layer must hold
the full *pre-rewrite* memory first, so the rewrite records `old → new` provenance
on the **same fact keys** and the layer stays byte-exact. Recording the rewrite
into an *un-seeded* layer would only capture the changed entries and leave the
layer unable to reconstruct live. This driver supersedes the naive
"apply → record → sync" ordering for exactly that reason (INTEG-3).

**Artifact hand-offs:** step 2 writes `policy.json` (→ step 3); step 4 writes
`mem-audit.json` (→ steps 6 **and** 7); step 6 writes `proposed/manifest.json`
(the reviewable record). Everything lives under `--workdir` (default
`<home>/.onboard`), so a partial run resumes with `--from-step N`.

## Modes & safety

- **Dry-run is the default.** With no apply flag, only the read-only / on-a-copy
  steps run and each mutation is printed as "would run". Live
  `MEMORY.md`/`USER.md`/`state.db` are never touched, and no temporal events are
  recorded (a read-only `verify` that instantiates an empty index is cleaned up so
  the run leaves a pristine footprint). **`--dry-run` against any profile is safe.**
- **`--apply`** performs mutations and asks before each one. **`--auto`**
  auto-confirms the append-only temporal steps and still asks before the two
  destructive ones (`state.db` rewrite, hot-memory rewrite). **`--yes`** answers
  yes to everything (non-interactive). A non-interactive shell with no `--yes`
  *skips* (never hangs) any step that would prompt.
- **Archive-first / recoverable:** step 3 archives the old `state.db` (tar + SHA +
  `RESTORE.md`); step 7 archives the pre-rewrite `MEMORY.md`/`USER.md`.
- **Stops on first failure**, prints a resume hint, preserves earlier artifacts.
- Every child CLI is passed `--home` and inherits `HERMES_HOME` pointed at the
  same home, so a dropped flag degrades to the right home, never a stray one.

## CLI

| Flag | Default | Meaning |
|---|---|---|
| `--home` | `$HERMES_HOME` or `~/.hermes` | the home to onboard |
| `--user-home` | real `$HOME` | base for resolving `~/` paths in entries (set to the profile root for self-contained profiles) |
| `--db` | `<home>/state.db` | state.db path |
| `--workdir` | `<home>/.onboard` | where step artifacts live (review + resume) |
| `--dry-run` | **on** | preview only; never mutates |
| `--apply` / `--auto` / `--yes` | off | enable mutations (see Modes) |
| `--from-step N` / `--to-step N` | `1` / `10` | resume / stop at a step |
| `--retention-days`, `--prune-closed`, `--prune-unclosed`, `--drop-trigram`, `--delete-compression-parents`, `--vacuum` | conservative | state.db cleanup policy knobs (`--drop-trigram yes` reclaims the most space) |

## Verify

```bash
cd ~/.hermes/packages/hermes-memory-stack
python3 -m py_compile scripts/memory_onboard.py
python3 scripts/memory_onboard.py --help
python3 -m unittest tests.test_onboard -v          # dry-run safety, full apply, resume, partial failure
# safe preview against any profile (read-only):
python3 scripts/memory_onboard.py --home ~/.hermes --dry-run
```

## Related

- Per-area skills: `state-db-remediation` (1), `memory-audit` (2),
  `memory-rewrite` (3), `temporal-migration` (4), `memory-maintenance` (5).
- `RUNBOOK.md` — the same golden path as a manual, copy-paste operator guide.
- `tests/test_e2e_pipeline.py` — the shipping-gate end-to-end test.
