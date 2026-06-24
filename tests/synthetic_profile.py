#!/usr/bin/env python3
"""Synthetic *messy* Hermes home builder — for the Area 1→5 E2E pipeline test.

Builds a complete, realistic-but-fake Hermes home in a temp dir so the whole
memory-stack pipeline can be exercised end-to-end without ever touching live
data. Content is deterministic given a fixed root + --seed (entries embed the
absolute note paths under `root`, so the path strings vary with the temp dir;
everything else is seed-stable). Also handy for demos.

Two intensity levels (`--level`):
  * normal  — a moderately messy home (~47 MEMORY entries at ~100% capacity, a
              ~20 MB state.db). This is the default and is byte-stable: the Area
              1→5 E2E suite builds it and asserts exact behaviour.
  * stress  — a DRAMATICALLY worse home meant to break a naive memory system.
              The harness asserts only FLOORS: 200+ MEMORY entries / 40 000+
              chars (well over the 15k budget), USER 12 000+ chars, and a 50 MB+
              / 100+ session / 5 000+ message state.db. A seed-42 build lands
              ~211 entries / ~67k chars (~4x), USER ~3x over budget, ~60 MB /
              142 sessions / 5 376 messages. Everything `normal` plants, but many
              multiples of it,
              plus very long pure dumps, 15+ duplicate pairs, 10+ contradictions,
              and status-shaped durable preferences. NOTE: the *archivable* and
              *kept* categories (content dumps, debugging findings, projects,
              pointers, preferences) are deliberately bounded — each leaves a
              non-vanishing survivor in MEMORY (a ~280-char breadcrumb for an
              archived dump/finding, or the kept entry itself), so packing in the
              "30+/20+/15+" of each that a real mess would have makes "under the
              15k budget in ONE pass" mathematically impossible. The bulk is
              therefore carried by status updates (removed wholesale) and duplicate
              pairs (merged). See skills/memory-e2e-testing.md for the full note.

What it plants (so each pipeline stage has something real to find/fix):
  * memories/MEMORY.md — a header; content dumps (with and without real note
    targets) incl. 1000+ char pure dumps; dated status updates; paraphrased AND
    near-exact duplicate pairs; default-vs-default contradiction pairs (pairwise
    token-disjoint subjects, each an old value vs a "now" value); debugging
    findings; dated project-progress entries; stale dated entries; already-curated
    ↪ pointers + archive-pointers; durable preferences; TODOs; broken pointers;
    and status-shaped entries that are really durable preferences (a regression).
  * memories/USER.md — durable preferences (incl. the metric-bearing regression
    "...risk capped at 2%..."), content dumps, status updates, and (stress) a few
    duplicates of MEMORY.md entries.
  * state.db — a bloated DB (via tests/synthetic_db.py): sessions + messages, FTS5
    + trigram, compression parents with children, unclosed + aged sessions
    spanning 6+ months, sources telegram/cli/cron/subagent(/discord).
  * notes/ — real note files so the good/rewrite pointers resolve.
  * memories/_versions/ — empty (simulates a fresh user with no temporal layer).
  * memories/_auto_extract/ — a stale candidates file.
  * cron/jobs.json — memory-related cron jobs (curator ok, capacity monitor error,
    and at stress also weekly LLM consolidation / semantic reindex / auto-extract).

Usage:
    python3 tests/synthetic_profile.py /tmp/messy-profile  --level normal --seed 42
    python3 tests/synthetic_profile.py /tmp/stress-profile --level stress --seed 42
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import synthetic_db as SD  # noqa: E402

DELIM = "\n§\n"

_WORDS = ("system gateway memory pointer trading session model provider failover "
         "routing capacity audit temporal verify drift archive curator semantic "
         "extraction pipeline candidate threshold reconciliation execution venue "
         "timeframe order block liquidity inducement signal journal watchdog cron "
         "delivery telegram discord context window compression transcript index "
         "trigram unicode fts vacuum prune retention snapshot manifest provenance "
         "reconstruction baseline merge duplicate contradiction status preference").split()


def _para(rnd: random.Random, n_words: int, salt: str = "") -> str:
    # `salt` makes each paragraph's tokens distinct so synthetic content dumps are
    # NOT mutual near-duplicates (a tiny shared word pool would otherwise make
    # every dump look like a duplicate of every other one).
    return " ".join(rnd.choice(_WORDS) + salt for _ in range(n_words))


# --------------------------------------------------------------------------- #
# Level specs — `normal` reproduces the byte-stable baseline; `stress` is the    #
# break-a-naive-system profile. Each key scales one generator dimension.        #
# --------------------------------------------------------------------------- #
LEVELS = {
    "normal": {
        "stress": False,
        "mem_pad_to": 13500, "mem_pad_cap": 20,
        "user_pad_to": 5700, "user_pad_min_entries": 15,
        "extra_notes": 0,
        "db_comp": 10, "db_open": 20, "db_closed": 25, "db_msg": [32, 44],
        "db_text_words": 95, "db_sources": ["telegram", "cli", "cron", "subagent"],
        "db_age_choices": [5, 40, 95, 150, 210],
        "extra_crons": False,
    },
    "stress": {
        "stress": True,
        # bulk counts (these are ON TOP of the byte-stable normal base). KEY insight:
        # Area 3 *removes* status updates and *merges* duplicates wholesale (no
        # survivor), but archives content dumps / debugging findings into ~280-char
        # findable breadcrumbs (they shrink, not vanish). So the input char-bulk is
        # carried by MANY status updates + duplicate pairs (which disappear), while
        # archivable entries (large dumps, debugging) are present in force but bounded
        # so their accumulated breadcrumbs still fit under the 15k budget in one pass.
        "dumps_with_path": 6, "dumps_no_path": 4, "long_dumps": 2,
        "status": 70, "dup_pairs": 12, "contra_pairs": 9,
        "debugging": 10, "projects": 8, "pointers": 5, "prefs": 5,
        "broken": 3, "todos": 3, "status_like_prefs": 3, "stale": 3,
        "user_prefs": 6, "user_dumps": 7, "user_status": 18,
        "user_dups": 3, "user_status_like_prefs": 2,
        "mem_pad_to": 13500, "mem_pad_cap": 20,
        # USER bulk is carried by compressible dumps(->pointer)/status(->removed),
        # NOT pad-preferences (which survive). Keep the pad target low so it only
        # fills a tiny gap; the dumps/status above already push input well over 12k.
        "user_pad_to": 9000, "user_pad_min_entries": 40,
        "extra_notes": 30,
        "db_comp": 20, "db_open": 56, "db_closed": 46, "db_msg": [36, 50],
        "db_text_words": 118,
        "db_sources": ["telegram", "cli", "cron", "subagent", "discord"],
        "db_age_choices": [4, 25, 55, 95, 130, 170, 210],
        "extra_crons": True,
    },
}
# `extreme` = stress with the dial turned further (handy for manual torture tests).
LEVELS["extreme"] = dict(LEVELS["stress"], **{
    "dumps_with_path": 30, "dumps_no_path": 22, "long_dumps": 10,
    "status": 30, "dup_pairs": 24, "contra_pairs": 12, "debugging": 35,
    "projects": 22, "pointers": 22, "prefs": 22, "stale": 18,
    "user_prefs": 22, "user_dumps": 16,
    "extra_notes": 50,
    "db_comp": 30, "db_open": 80, "db_closed": 70, "db_text_words": 130,
})


# --------------------------------------------------------------------------- #
# notes/ targets                                                              #
# --------------------------------------------------------------------------- #
NOTE_FILES = [
    "INDEX.md", "trading/spec.md", "nclex/status.md", "hermes/curator.md",
    "design/resources.md", "personal/contacts.md", "planning/roadmap.md",
    "ops/runbook.md", "research/notes.md", "gbrain/keys.md", "hermes/routing.md",
    "trading/definitions.md",
]


def _auto_note(i: int) -> str:
    return f"auto/topic-{i:02d}.md"


def build_notes(root: str, spec: dict) -> int:
    notes = os.path.join(root, "notes")
    rels = list(NOTE_FILES)
    rels += [_auto_note(i) for i in range(spec.get("extra_notes", 0))]
    for k, rel in enumerate(rels):
        p = os.path.join(notes, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # mark some auto notes stale-looking in content (older roadmap snapshots)
        stamp = "2026-03-01 (stale snapshot)" if rel.startswith("auto/") and k % 3 == 0 else "current"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"# {rel}\n\nFull long-form context for {rel} lives here. Status: {stamp}.\n")
    return len(rels)


# --------------------------------------------------------------------------- #
# MEMORY.md                                                                   #
# --------------------------------------------------------------------------- #
def _dump(rnd: random.Random, topic: str, path: str | None, salt: str, words: int = 150) -> str:
    body = _para(rnd, words, salt)
    tail = f" Canonical doc {path}." if path else ""
    return f"{topic}: {body}.{tail}"


# Distinct subsystems so status/project/todo entries are NOT mutual duplicates
# (clusters of near-identical lines would form spurious dup pairs that one
# rewrite pass can't fully collapse).
_STATUS_SUBSYS = ["Gateway", "Telegram poller", "Discord bridge", "Cron scheduler",
                  "Semantic daemon", "Watchdog", "Kanban board", "Spine ingest", "Dream cycle"]
_STATUS_DATES = ["2026-01-05", "2026-01-12", "2026-02-01", "2026-02-09", "2026-02-20",
                 "2026-03-03", "2026-03-11", "2026-03-19", "2026-04-02"]

# Pools for stress bulk (pairwise token-disjoint so generated entries don't
# accidentally collide as duplicates / cross-subject contradictions).
# Contradiction subjects are a dedicated two-word pool whose tokens are disjoint
# from each other AND from the byte-stable base contradiction tokens (coding/model,
# trading/provider/alpha/beta, embedding/backend/foo/bar/e1/e2) — otherwise a stress
# subject like "beta router" would cross-match the base value "Beta" and leave a
# residual contradiction that both-sides-have-"now" resolution can't clear.
_CONTRA_SUBJECTS = ["payload encoder", "queue broker", "vector shard", "warm cache",
                    "retry policy", "rate limiter", "log shipper", "metric sink",
                    "token bucket", "shadow buffer", "replay guard", "fanout worker"]
_DEBUG_CAUSES = [
    "the gateway crashed because of a stale .pyc cache after a hot reload",
    "state.db grew to 506MB because a broken hygiene import skipped pruning",
    "the telegram poller dropped events from an unawaited coroutine",
    "the semantic daemon OOMed when the embedding batch size was unbounded",
    "duplicate cron fires came from two enabled schedules sharing one id",
    "the watchdog looped because the restart guard failed to clear its lockfile",
    "FTS queries slowed 40x once the trigram index was left un-vacuumed",
    "the curator double-archived entries after a partial write left no manifest",
    "a timezone-naive timestamp shifted retention by a full day at the boundary",
    "the discord bridge reconnected in a tight loop on a malformed gateway frame",
]
_PROJECTS = ["NCLEX trainer", "Trading brain", "Memory stack", "POIWatcher",
             "Digital agency", "Curator v2", "Spine ingest", "Semantic search",
             "Temporal layer", "Auto-extraction", "Kanban", "Dream cycle",
             "Routing layer", "Failover", "Watchdog", "Delivery"]
_PREF_VERBS = ["always prefers", "never wants", "requires", "insists on", "expects"]
_PREF_OBJECTS = [
    "dry-run output before any destructive change", "archive-first on every cleanup",
    "blunt ROI-focused correction over reassurance", "exact pinned dependency versions",
    "a brief plan then immediate action", "real verification of delegated output",
    "plain terminal output with no option trees", "full absolute paths in commands",
    "comparative local-vs-cloud analysis before deciding", "no automated gateway restarts",
    "double verification on safety-critical work", "no scaling before a proof point",
]


def _stress_memory_bulk(rnd: random.Random, spec: dict, n) -> list[str]:
    """Generate the heavy stress-only MEMORY bulk (everything beyond the base)."""
    e: list[str] = []
    # 1000+ char pure content dumps (no path) -> archived. These (and the dumps
    # below) carry the char bulk while compressing to ~nothing in the pipeline.
    for i in range(spec["long_dumps"]):
        e.append(f"Deep dump {i}: " + _para(rnd, rnd.randint(200, 260), f"ld{i}") + ".")
    # large content dumps WITH a real (auto) note path -> rewrite_to_pointer
    for i in range(spec["dumps_with_path"]):
        rel = _auto_note(i % max(1, spec["extra_notes"]))
        e.append(_dump(rnd, f"Subsystem deep dive {i}", n(rel), salt=f"sd{i}",
                       words=rnd.randint(150, 230)))
    # large content dumps WITHOUT a path -> move_to_note
    for i in range(spec["dumps_no_path"]):
        e.append(f"Operational knowledge {i}: " + _para(rnd, rnd.randint(140, 200), f"ok{i}") + ".")
    # dated status updates -> remove_after_archive. Must match the status_update
    # pattern ("<subject> <completion-verb> on <date>: ..., deployed and verified")
    # so they are REMOVED, not kept as project_progress ("milestone"/"phase"/"in
    # progress" would classify as kept project entries and never compress).
    _verbs = ["fixed", "completed", "deployed", "resolved", "shipped", "patched"]
    for i in range(spec["status"]):
        e.append(f"Subsystem-{i} {_verbs[i % len(_verbs)]} on 2026-{1 + i % 6:02d}-{1 + i % 27:02d}: "
                 + _para(rnd, 12, f"ms{i}") + ", deployed and verified.")
    # near-exact duplicate pairs (salted core so pairs don't cross-match) -> merged
    for i in range(spec["dup_pairs"]):
        core = f"Persistent fact {i}: " + _para(rnd, 10, f"dc{i}")
        e.append(core + ".")
        e.append(core + " — confirmed, still applies.")
    # contradiction pairs on pairwise-disjoint subjects (old value vs "now" value)
    for i in range(spec["contra_pairs"]):
        subj = _CONTRA_SUBJECTS[i % len(_CONTRA_SUBJECTS)]
        e.append(f"Default {subj} is Legacy{i}.")
        e.append(f"Fresh{i} is now the default {subj}.")
    # debugging findings -> move_to_note
    for i in range(spec["debugging"]):
        e.append(f"Root cause #{i}: {_DEBUG_CAUSES[i % len(_DEBUG_CAUSES)]}; "
                 f"fixed and verified. {_para(rnd, 16, f'rc{i}')}.")
    # dated project-progress entries -> verify_current (kept). A distinct salted
    # clause per entry keeps them from collapsing into a near-duplicate cluster
    # (which would inflate the dup count instead of exercising the kept-project path).
    for i in range(spec["projects"]):
        proj = _PROJECTS[i % len(_PROJECTS)]
        e.append(f"{proj} status (2026-{1 + i % 6:02d}-{2 + i % 26:02d}): "
                 f"phase {1 + i % 4} of 4, {10 + i} items done on {_para(rnd, 5, f'pj{i}')}, "
                 f"next milestone pending.")
    # stale dated entries (old months) -> stale
    for i in range(spec.get("stale", 0)):
        mon = ["03", "04"][i % 2]
        e.append(f"As of 2026-{mon}-{1 + i % 27:02d}, the {_PROJECTS[i % len(_PROJECTS)]} "
                 f"snapshot recorded {_para(rnd, 10, f'stale{i}')} (may be outdated).")
    # already-curated ↪ pointers -> keep
    for i in range(spec["pointers"]):
        rel = _auto_note((i + 7) % max(1, spec["extra_notes"]))
        e.append(f"↪ Topic {i}: full context {n(rel)}.")
    # durable preferences -> keep. Each carries a distinct salted clause so the set
    # does NOT collapse into a mutual-duplicate cluster (12 recycled objects would).
    for i in range(spec["prefs"]):
        e.append(f"User {_PREF_VERBS[i % len(_PREF_VERBS)]} "
                 f"{_PREF_OBJECTS[i % len(_PREF_OBJECTS)]} on {_para(rnd, 4, f'pf{i}')} (rule {i}).")
    # status-SHAPED durable preferences (regression: must NOT be archived as status)
    for i in range(spec["status_like_prefs"]):
        e.append(f"Since 2026-01-{1 + i:02d}, user permanently requires "
                 f"{_PREF_OBJECTS[i % len(_PREF_OBJECTS)]} for {_para(rnd, 4, f'slp{i}')} "
                 f"— a standing rule, not a status.")
    # broken pointers -> verify_current
    for i in range(spec["broken"]):
        e.append(f"Legacy note {i}: see {n(f'deleted/missing-{i}.md')} for the old approach.")
    # todos -> user_review (kept)
    for i in range(spec["todos"]):
        e.append(f"TODO {i}: follow up on {_PROJECTS[i % len(_PROJECTS)]} next session.")
    return e


def build_memory_entries(root: str, rnd: random.Random, spec: dict) -> list[str]:
    n = lambda rel: os.path.join(root, "notes", rel)  # noqa: E731
    e: list[str] = []
    # ---- BASE (byte-stable; the normal E2E asserts on these exact entries) ---- #
    # header
    e.append(f"Long-form notes live in `{n('INDEX.md')}`. Read it first to find topics.")
    # content dumps WITH a real path -> rewrite_to_pointer (5, large)
    for k, (topic, rel) in enumerate((("Trading architecture", "trading/spec.md"),
                                      ("NCLEX pipeline", "nclex/status.md"),
                                      ("Curator design", "hermes/curator.md"),
                                      ("Design resources", "design/resources.md"),
                                      ("GBrain key policy", "gbrain/keys.md"))):
        e.append(_dump(rnd, topic, n(rel), salt=f"d{k}", words=120))
    # content dumps WITHOUT a path -> move_to_note / move_to_skill (3)
    e.append("Deployment knowledge: " + _para(rnd, 115, "dep") + ".")
    e.append("How to recover the watchdog: first stop, then pull, then restart, then verify, "
             + _para(rnd, 110, "rec") + ".")
    e.append("Architecture overview dump: " + _para(rnd, 115, "arch") + ".")
    # status updates (9 DISTINCT subsystems, dated + completion verb, SALTED bodies
    # so they are not mutual duplicates) -> remove_after_archive
    for i, (sub, d) in enumerate(zip(_STATUS_SUBSYS, _STATUS_DATES)):
        e.append(f"{sub} fixed on {d}: " + _para(rnd, 12, f"st{i}") + ", deployed and verified.")
    # duplicate pair A (paraphrase) -> merged
    e.append("User prefers concise plain terminal output, no fluff, no option trees, ever.")
    e.append("Emeka likes concise plain terminal output and hates fluff and option trees.")
    # duplicate pair B (near-exact) -> merged
    e.append("Trading uses order blocks and liquidity inducement for POI identification.")
    e.append("Trading uses order blocks and liquidity inducement for POI identification and entries.")
    # contradiction pairs (3) — within each pair, differing declared defaults on a
    # shared subject (old value vs "now" value). The three SUBJECTS are pairwise
    # token-disjoint (coding model / trading provider / embedding backend) so that the
    # three current sides are mutually consistent: keeping all of them and dropping the
    # three stale sides leaves exactly 0 contradictions. Flagged user_review by Area 3
    # (never auto-resolved); the E2E human step resolves them.
    e.append("Default coding model is Foo-7B.")
    e.append("Bar-9000 is now the default coding model.")
    e.append("Default trading provider is Alpha.")
    e.append("Beta is now the default trading provider.")
    e.append("Default embedding backend is E1.")
    e.append("E2 is now the default embedding backend.")
    # debugging findings (2) -> move_to_note
    for i in range(2):
        e.append(f"Root cause #{i}: the gateway crashed because of a stale module import; "
                 f"traceback showed an ImportError; fixed by a restart. {_para(rnd, 18, f'dbg{i}')}.")
    # project progress (2 distinct) -> verify_current (kept)
    e.append("NCLEX project status (2026-01-15): phase 2 in progress, 12/30 cards done, milestone pending.")
    e.append("Trading brain status (2026-01-20): origin candidate V3 in progress, backtests pending.")
    # todos (2 distinct) -> user_review (kept)
    e.append("TODO: wire the semantic reindex cron next session.")
    e.append("TODO: backfill the trading definitions dictionary.")
    # durable preferences (5) -> keep
    for pref in ("User prefers blunt, ROI-focused correction over reassurance, always.",
                 "User wants brief plan then action; implement when the next step is obvious.",
                 "User requires real verification of delegated output before trusting it.",
                 "User prefers dry-run and archive-first for any destructive operation.",
                 "User never wants the gateway restarted by automated jobs."):
        e.append(pref)
    # already-curated ↪ pointers (5) -> keep. This is the STEADY-STATE input shape: any
    # home the curator has touched is full of ↪ pointers, and the audit has dedicated
    # handling for the sigil. The pipeline must preserve these untouched, not re-flag
    # them, and round-trip the unicode ↪ byte-exact through temporal reconstruction.
    for topic, rel in (("Trading", "trading/spec.md"), ("NCLEX", "nclex/status.md"),
                       ("Roadmap", "planning/roadmap.md"), ("Runbook", "ops/runbook.md"),
                       ("Routing", "hermes/routing.md")):
        e.append(f"↪ {topic}: full context {n(rel)}.")
    # one archive-pointer (curator output: a summarized+archived entry) -> keep
    e.append('↪ Dream cycle: weekly summary → archived 2026-03-01. '
             f'Find: session_search("dream cycle") or {n("hermes/curator.md")}.')
    # broken pointers (2) -> verify_current
    e.append(f"Old plan: see {n('deleted/gone.md')} for the original approach.")
    e.append(f"Legacy spec: details in {n('archive/missing-spec.md')}.")
    # ---- STRESS BULK (only at stress/extreme) ---- #
    if spec.get("stress"):
        e += _stress_memory_bulk(rnd, spec, n)
    # pad with a few large dumps ONLY if needed to reach the level's char target
    # (at stress the bulk is already far over, so this never runs).
    i = 0
    pad_targets = ["research/notes.md", "ops/runbook.md", "planning/roadmap.md"]
    while len(DELIM.join(e)) < spec["mem_pad_to"]:
        rel = pad_targets[i % len(pad_targets)]
        e.append(_dump(rnd, f"Background dump {i}", n(rel), salt=f"p{i}", words=110))
        i += 1
        if i > spec["mem_pad_cap"]:
            break
    return e


# --------------------------------------------------------------------------- #
# USER.md                                                                     #
# --------------------------------------------------------------------------- #
def build_user_entries(root: str, rnd: random.Random, spec: dict,
                       mem_entries: list[str] | None = None) -> list[str]:
    n = lambda rel: os.path.join(root, "notes", rel)  # noqa: E731
    e: list[str] = []
    # ---- BASE (byte-stable) ---- #
    # durable preferences -> keep (incl the metric-bearing regression)
    e.append("Emeka Okpara-Ogbonnia: trader-engineer hybrid, BSO RPN, money-minded, anti-pivot.")
    e.append("Core execution: blunt correction, brief plan then action, plain terminal output.")
    e.append("User prefers risk capped at 2% of account per trade, always.")  # metric pref regression
    e.append("Always pin dependencies to v2.0.1 style exact versions for reproducibility.")
    e.append("Requires at least 80% test coverage before merging anything to main.")
    e.append("Model routing: auto-delegate complex debugging to Anthropic without setup.")
    e.append("Prefers SSH git auth with an HTTPS PAT fallback; report credential issues.")
    e.append("Wants outputs accessible on his phone for in-person client pitches.")
    e.append("Values comparative analysis (local vs cloud, model A vs B) before decisions.")
    e.append("Never modify .zshrc or shell config without explicit permission; use full paths.")
    # content dumps (>700, no path) -> content_dump -> archived (3)
    for i in range(3):
        e.append(f"Workflow detail dump {i}: " + _para(rnd, 130, f"u{i}") + ".")
    # status updates (2)
    e.append("Updated routing on 2026-02-15: switched default provider, now works, verified.")
    e.append("Fixed credential loop on 2026-02-18: SSH key added, push works now.")
    # ---- STRESS BULK ---- #
    if spec.get("stress"):
        # extra durable preferences -> keep (distinct salted clause; no dup cluster)
        for i in range(spec["user_prefs"]):
            e.append(f"User {_PREF_VERBS[i % len(_PREF_VERBS)]} "
                     f"{_PREF_OBJECTS[(i + 3) % len(_PREF_OBJECTS)]} for "
                     f"{_para(rnd, 4, f'upf{i}')} (rule U{i}).")
        # content dumps -> archived (carry char bulk; compress to ~breadcrumbs)
        for i in range(spec["user_dumps"]):
            e.append(f"Delegation pattern dump {i}: " + _para(rnd, rnd.randint(120, 180), f"ud{i}") + ".")
        # status updates -> removed (must match the removable status_update pattern,
        # "<subject> <completion-verb> on <date>: ..., deployed and verified")
        _uverbs = ["fixed", "completed", "deployed", "resolved", "shipped", "patched"]
        for i in range(spec["user_status"]):
            e.append(f"Workflow-{i} {_uverbs[i % len(_uverbs)]} on 2026-{2 + i % 5:02d}-{3 + i % 25:02d}: "
                     + _para(rnd, 10, f"us{i}") + ", deployed and verified.")
        # status-SHAPED durable preferences (regression)
        for i in range(spec["user_status_like_prefs"]):
            e.append(f"As of 2026-01-{2 + i:02d}, user permanently prefers "
                     f"{_PREF_OBJECTS[(i + 6) % len(_PREF_OBJECTS)]} — standing rule, not status.")
        # a few exact duplicates of MEMORY entries (cross-file duplicates)
        if mem_entries:
            picks = [seg for seg in mem_entries if seg.startswith("User ")][:spec["user_dups"]]
            e.extend(picks)
    # pad durable prefs until the level's char/entry target
    extra = ["Prefers staged surgical passes with double verification on complex work.",
             "Tracks AI/tech updates, especially provider fallback stability and Obsidian.",
             "Lead with a direct 3-5 line answer; offer detail on request; never dump research."]
    i = 0
    while len(DELIM.join(e)) < spec["user_pad_to"] or len(e) < spec["user_pad_min_entries"]:
        if i < len(extra):
            e.append(extra[i])
        else:
            e.append(f"Additional durable preference {i}: " + _para(rnd, 18, f"up{i}") + ".")
        i += 1
        if i > 60:
            break
    return e


# --------------------------------------------------------------------------- #
# state.db (bloated)                                                          #
# --------------------------------------------------------------------------- #
def build_state_db(path: str, rnd: random.Random, spec: dict, now: float | None = None) -> dict:
    db = SD.SyntheticDB(path, now=now)
    sources = spec["db_sources"]
    ages = spec["db_age_choices"]
    words = spec["db_text_words"]
    mlo, mhi = spec["db_msg"]
    n_msgs = 0
    n_sessions = 0

    def mk(sid, **kw):
        nonlocal n_msgs, n_sessions
        kw.setdefault("text", _para(rnd, words, salt=sid.replace("-", "")))
        db.add_session(sid, **kw)
        n_msgs += kw.get("n_messages", 3)
        n_sessions += 1

    # compression parents, each with a surviving child
    for i in range(spec["db_comp"]):
        mk(f"comp-parent-{i}", source=sources[i % len(sources)], days_ago=100 + i, ended=99 + i,
           end_reason="compression", n_messages=rnd.randint(36, 46))
        mk(f"comp-child-{i}", source=sources[i % len(sources)], days_ago=99 + i, ended=False,
           parent_id=f"comp-parent-{i}", n_messages=rnd.randint(3, 6))
    # unclosed (mix of ages, several > 90 days)
    for i in range(spec["db_open"]):
        mk(f"open-{i}", source=sources[i % len(sources)], days_ago=rnd.choice(ages),
           ended=False, n_messages=rnd.randint(mlo, mhi))
    # closed (mix of ages)
    for i in range(spec["db_closed"]):
        age = rnd.choice(ages)
        mk(f"closed-{i}", source=sources[i % len(sources)], days_ago=age, ended=max(1, age - 1),
           n_messages=rnd.randint(mlo, mhi))
    db.set_meta("last_auto_prune", "")  # never pruned -> unbounded growth
    db.close()
    return {"sessions": n_sessions, "messages": n_msgs, "bytes": os.path.getsize(path)}


# --------------------------------------------------------------------------- #
# aux dirs                                                                    #
# --------------------------------------------------------------------------- #
def build_aux(root: str, spec: dict) -> None:
    # empty temporal layer (fresh user)
    os.makedirs(os.path.join(root, "memories", "_versions"), exist_ok=True)
    # stale auto-extract candidates
    ae = os.path.join(root, "memories", "_auto_extract")
    os.makedirs(ae, exist_ok=True)
    cand = os.path.join(ae, "candidates-20260101T000000.json")
    with open(cand, "w", encoding="utf-8") as fh:
        json.dump({"candidates": ["stale candidate one", "stale candidate two"]}, fh)
    os.utime(cand, (time.time() - 30 * 86400, time.time() - 30 * 86400))  # stale mtime
    # cron registry with memory jobs
    jobs = [
        {"id": "aaa", "name": "Memory Curator — Daily Sweep", "no_agent": True,
         "schedule": {"kind": "cron", "expr": "50 3 * * *"}, "state": "enabled",
         "last_status": "ok", "last_run_at": "2026-06-23T03:50:00"},
        {"id": "bbb", "name": "Memory Curator — Capacity Monitor", "no_agent": True,
         "schedule": {"kind": "cron", "expr": "0 */6 * * *"}, "state": "enabled",
         "last_status": "error", "last_run_at": "2026-06-23T18:00:00"},
    ]
    if spec.get("extra_crons"):
        jobs += [
            {"id": "ccc", "name": "Memory — Weekly LLM Consolidation", "no_agent": True,
             "schedule": {"kind": "cron", "expr": "0 5 * * 6"}, "state": "enabled",
             "last_status": "ok", "last_run_at": "2026-06-21T05:00:00"},
            {"id": "ddd", "name": "Memory — Semantic Reindex", "no_agent": True,
             "schedule": {"kind": "cron", "expr": "30 4 * * *"}, "state": "enabled",
             "last_status": "ok", "last_run_at": "2026-06-23T04:30:00"},
            {"id": "eee", "name": "Memory — Auto-Extraction Dry-Run", "no_agent": True,
             "schedule": {"kind": "cron", "expr": "15 2 * * *"}, "state": "enabled",
             "last_status": "ok", "last_run_at": "2026-06-23T02:15:00"},
        ]
    crondir = os.path.join(root, "cron")
    os.makedirs(crondir, exist_ok=True)
    with open(os.path.join(crondir, "jobs.json"), "w", encoding="utf-8") as fh:
        json.dump({"jobs": jobs}, fh)


# --------------------------------------------------------------------------- #
# orchestrate                                                                 #
# --------------------------------------------------------------------------- #
def build_profile(root: str, seed: int = 42, now: float | None = None,
                  level: str = "normal") -> dict:
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; choose from {sorted(LEVELS)}")
    spec = LEVELS[level]
    root = os.path.abspath(os.path.expanduser(root))
    os.makedirs(os.path.join(root, "memories"), exist_ok=True)

    notes_n = build_notes(root, spec)
    mem_entries = build_memory_entries(root, random.Random(seed + 1), spec)
    usr_entries = build_user_entries(root, random.Random(seed + 2), spec, mem_entries=mem_entries)
    mem_text = DELIM.join(mem_entries)
    usr_text = DELIM.join(usr_entries)
    with open(os.path.join(root, "memories", "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(mem_text)
    with open(os.path.join(root, "memories", "USER.md"), "w", encoding="utf-8") as fh:
        fh.write(usr_text)

    db_info = build_state_db(os.path.join(root, "state.db"), random.Random(seed + 3),
                             spec, now=now)
    build_aux(root, spec)

    return {
        "root": root, "seed": seed, "level": level,
        "memory_entries": len(mem_entries), "memory_chars": len(mem_text),
        "memory_capacity_pct": round(100 * len(mem_text) / 15000, 1),
        "user_entries": len(usr_entries), "user_chars": len(usr_text),
        "user_capacity_pct": round(100 * len(usr_text) / 6000, 1),
        "notes_created": notes_n,
        "state_db": db_info,
        "state_db_mb": round(db_info["bytes"] / (1024 * 1024), 2),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a synthetic messy Hermes home for E2E testing.")
    ap.add_argument("root", help="directory to build the profile in (created if absent)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--level", choices=sorted(LEVELS), default="normal",
                    help="normal (byte-stable baseline) | stress (3x over budget) | extreme")
    args = ap.parse_args(argv)
    info = build_profile(args.root, seed=args.seed, level=args.level)
    print(json.dumps(info, indent=2))
    print(f"\n[level={info['level']}]")
    print(f"MEMORY.md: {info['memory_entries']} entries, {info['memory_chars']} chars "
          f"({info['memory_capacity_pct']}%)")
    print(f"USER.md:   {info['user_entries']} entries, {info['user_chars']} chars "
          f"({info['user_capacity_pct']}%)")
    print(f"state.db:  {info['state_db_mb']} MB, {info['state_db']['sessions']} sessions, "
          f"{info['state_db']['messages']} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
