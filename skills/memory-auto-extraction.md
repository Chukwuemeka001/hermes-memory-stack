---
name: memory-auto-extraction
description: "Mem0-style nightly auto-extraction of durable personal facts/preferences/corrections from Hermes conversations into MEMORY.md, without the user saying 'remember this'. Precision-first: a local Phi-4 proposes, layered deterministic guards + a verification pass dispose. Load when running, tuning, reviewing, or wiring the nightly extraction pass."
version: 1.0.0
author: Hermes Agent + Claude Opus 4.8
platforms: [macos, linux]
metadata:
  hermes:
    tags: [memory, extraction, mem0, facts, preferences, nightly, phi-4, precision]
---

# Memory Auto-Extraction — Mem0-style Nightly Fact Harvester

Captures durable personal facts the user states in passing ("I actually prefer
dark roast", "never place a live trade without my approval", "I'm allergic to
penicillin") that would otherwise be lost. Runs nightly, **DRY-RUN by default**,
and is **precision-first**: it would rather miss a fact tonight (it recurs) than
inject one wrong line into the hot pointer file that every turn pays for.

## Design philosophy: small model proposes, deterministic layers dispose

The local Phi-4 (3.8B) is a weak judge — by itself it hallucinates preferences
from jokes/status/complaints and echoes its own few-shot examples. So it is
wrapped in layers that do NOT trust it:

```
state.db sessions (last N days, human-origin sources only)
  → PRE-FILTER       role=user; drop automation-injected pseudo-user msgs
                     ([IMPORTANT:/[ASYNC/[Replying to:/cron prompts); require
                     >=2 real user turns AND a signal word
  → LLM EXTRACT      local Phi-4, forced JSON schema → {"facts":[...]}
  → GROUNDING veto   every substantive token of the fact (minus scaffolding like
                     "prefers") must appear in the transcript; hyphenated
                     compounds ("colour-blind") must appear CONTIGUOUSLY
  → SOURCE veto      third-party ("my coworker Dave is…"), scope/expiry ("for
                     this PR","for now","until X") unless a permanence override,
                     hypothetical ("what if"), quote ("my mentor said"), roleplay
  → META veto        joke/sarcasm narration shapes in the fact text
  → NEGATION         "User is not …" → REVIEW (surface, never auto-add)
  → INTAKE GATE      reuse hermes_memory_intake_gate: vetoes transient/status/
                     metric/dated facts; classifies durable
  → DEDUP            token containment(0.7)+Jaccard vs MEMORY.md + CLAUDE.md/USER.md
  → VERIFY pass      focused per-candidate yes/no (Phi-4), FAIL-CLOSED
  → CAPS             5 facts/session, 10 facts/night
  → OUTPUT           candidates JSON; ALLOW = auto-add, REVIEW = surfaced, REJECT
```

Each veto exists because something leaked without it (see "Why each layer" below).

## Files

| Path | Role |
|---|---|
| `~/.hermes/scripts/memory_auto_extract.py` | the extractor (CONFIG dict at top is the tuning surface) |
| `~/.hermes/scripts/memory_auto_extract_cron.sh` | nightly wrapper, DRY-RUN, ensures Phi-4 is up |
| `~/.hermes/scripts/memory_auto_extract_eval.py` | labeled precision/recall harness |
| `~/.hermes/scripts/memory_auto_extract_fixtures*.jsonl` | golden / holdout / adversarial fixtures |
| `~/.hermes/scripts/memory_auto_extract_sample_real.py` | builds a real-transcript precision corpus |
| `~/.hermes/memories/_auto_extract/` | candidates-*.json output, append-log, cron.log |

## Usage

```bash
# Dry-run on the last day's sessions (writes nothing; default)
python3 ~/.hermes/scripts/memory_auto_extract.py --dry-run
python3 ~/.hermes/scripts/memory_auto_extract.py --days 7        # wider window
python3 ~/.hermes/scripts/memory_auto_extract.py --json          # full report

# WRITE mode — appends ONLY ALLOW (durable, novel, verified) facts to MEMORY.md,
# logs provenance to _auto_extract/append-log.jsonl. Use deliberately.
python3 ~/.hermes/scripts/memory_auto_extract.py --write

# Evaluate quality (precision/recall) against labeled fixtures
python3 ~/.hermes/scripts/memory_auto_extract_eval.py                                   # golden
python3 ~/.hermes/scripts/memory_auto_extract_eval.py --fixtures-file <holdout|adversarial>.jsonl
python3 ~/.hermes/scripts/memory_auto_extract_eval.py --real --days 7                   # real precision
```

The cron wrapper runs DRY-RUN only (Phase 1). Promotion to `--write` is a
deliberate Phase-2 step. Example schedule (after the dream cycle, not auto-installed):
`30 3 * * *  ~/.hermes/scripts/memory_auto_extract_cron.sh`

## Tuning surface (CONFIG dict)

`include_sources`/`exclude_sources` (cron+subagent excluded — machine prompts,
not Emeka), `min_session_turns` (2), `require_signal_word`, `signal_words`,
`max_facts_per_session/night` (5/10), `gate_mode` (`veto_dedup`), `min_grounding`
(0.34), `dup_overlap` (0.7 — **do not lower**, it over-dedups short facts against
the large CLAUDE.md prose), `verify_pass`/`verify_fail_open` (on/closed),
`accept_verdicts` (`ALLOW`). Prompts: `EXTRACTION_SYSTEM` (few-shot, grounding
protects against bleed) and `VERIFY_SYSTEM`.

## Why each layer (lessons from the review loop)

These came from an adversarial multi-agent review that ran the LIVE pipeline and
reproduced false positives the synthetic fixtures missed. Do not remove a layer
without re-running the adversarial battery (`memory_auto_extract_fixtures_adversarial.jsonl`).

- **Grounding** — without it Phi-4 echoes few-shot examples ("dark roast coffee"
  appeared on a session that never mentioned coffee). Hyphen-contiguity added
  because `colour-blind` → {colour, blind} was satisfied by unrelated words.
- **Source veto** — the extractor strips the disqualifier ("for this PR", "my
  coworker") when phrasing the fact, so the veto must scan the SOURCE turn, not
  the fact. Permanence override ("permanent","going forward") protects genuine
  standing rules. (An earlier `always.*ever` bug matched "always…never" because
  "never" ends in "ever" — keep PERMANENCE markers specific.)
- **Verify FAIL-CLOSED** — the gate is permissive, so a fail-open verify at cron
  time (llama-server hiccup) would auto-accept everything. Closed = drop on error.
- **Negation → REVIEW** — auto-adding "User is not allergic to X" from a chart-fix
  task is too subtle to trust; surface it instead.

## Quality bar & known limitation

Bar: **>80% of ALLOW facts genuinely durable+personal+user-stated.** Final eval:
golden 100% precision / 100% recall, holdout (novel) 100% / 100%, adversarial
battery 100% genuine-precision / 100% recall, real state.db + 19 real transcripts
0 false positives.

**Known residual (P1, not a precision violation):** token-containment dedup can't
catch loose *paraphrases* of injected CLAUDE.md identity facts (e.g. "RPN
transitioning to trading" vs "RPN leaving nursing through trading") without
over-deduping genuine short facts. The fix is **semantic dedup via the existing
chroma index** (`~/.hermes/chroma/`) — a Phase-2 follow-up. The leaked item is a
true fact already known to the agent, so it is duplication/context-bloat, not noise.

## Constraints
- Reads state.db only; never writes to it. Default DRY-RUN; `--write` appends to
  MEMORY.md and never rewrites existing pointers (append-only, `\n§\n` delimited).
- Uses local Phi-4 (`http://localhost:8080`, launchd `com.emeka.hermes.local-llm`)
  — no API credits. Stdlib only; installs nothing.
- Related: [[memory-curator]] (cleanup/capacity), `hermes_memory_intake_gate.py`
  (the reused durability classifier), semantic-session-retrieval (chroma index).
