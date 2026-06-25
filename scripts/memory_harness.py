#!/usr/bin/env python3
"""Memory Projection Honesty Harness — Phase B (Tier 1: deterministic, no-LLM).

The projection engine (``memory_project.py``) claims it can shrink per-turn memory
overhead WITHOUT losing the context a task needs. That is a claim about *answer
quality*, and a token-savings number alone does not prove it — a projection that
saves 90% of the tokens by dropping the one entry the task needed is WORSE, not
better. This harness exists to keep that claim honest.

It runs each representative task two ways and compares them against a HAND-LABELLED
gold standard, so we can answer, per task, the five questions Phase B was built for:

    1. Does projection include the memory entries the task actually needs?   (recall)
    2. Do pinned safety / identity / operational rules survive projection?   (pins)
    3. How many tokens does projection save vs full "inject everything"?      (savings)
    4. Where does projection MISS needed context — and is that reported?      (misses)
    5. Can all of this run with no model / API / network?                    (Tier 1)

Two tiers, by design:

  * TIER 1 (this module, the default): deterministic, stdlib-only, no network.
    Coverage/recall/pin/savings metrics computed against gold labels. Runs in CI
    and in the unit suite. This is what proves the engine does not silently drop
    needed entries.

  * TIER 2 (optional, gated, NOT required here): a model-backed grader that reads
    the FULL vs PROJECTED memory block and scores actual answer quality. The seam
    is defined below (``AnswerGrader`` / ``NullGrader``); a real LLM grader plugs
    in behind an explicit flag. Tier 2 is never invoked by the default path and
    never imported at module load — so the default tests need no API key.

HONESTY PRINCIPLES (enforced in code, not just documented — see the tests):

  H1  Recall is graded against a hand-labelled gold set, never against what the
      engine itself selected. No circular "it kept what it kept, so it passed".
  H2  Query relevance is derived from the (query, entry-text) pair ONLY, by a
      transparent lexical function that is structurally blind to the gold labels
      (it is handed entry text + ref, never the required/pin flags). Tier 1 uses
      NO oracle. The lexical proxy is a stand-in for the real semantic index, and
      a deliberately weak one — see LIMITATIONS.
  H3  Token savings NEVER upgrades a task's status. Status is decided by pin
      survival and required-recall only. Savings and precision are reported as
      separate numbers and can only *annotate*, never *pass*, a task.
  H4  The harness validates its OWN fixtures first: every required entry must be
      selectable under unlimited budget, or the task FAILs as "fixture-invalid"
      (a mislabelled gold set cannot masquerade as an engine result).
  H5  Pin survival is probed at budget=0, so a pin only "survives" because the pin
      mechanism protected it — not because it happened to score well at the task's
      nominal budget.
  H6  Deterministic: fixed ``today``, synthetic fixtures, a pure lexical function,
      stable sort orders → byte-identical metrics across runs (CI-able).

LIMITATIONS (read these before quoting any number):

  * The Tier-1 "lexical" relevance proxy is token overlap, NOT the shipped
    embedding model. It is intentionally simple and will both miss paraphrases the
    real semantic index would catch AND match shallow word overlaps the real index
    would rank lower. Tier-1 recall is therefore a *floor-ish sanity signal*, not a
    measurement of production retrieval quality. The honest upgrade is Tier 2.
  * Recall against a gold set is a PROXY for answer quality, not answer quality
    itself. "The needed entry was present" is necessary, not sufficient. Only
    Tier 2 grades the actual answer.
  * Fixtures are synthetic and few. They are designed to be representative and
    adversarial, not exhaustive. Passing here means "did not regress on these
    cases", not "correct on all real memory".

READ-ONLY w.r.t. live data: never reads or writes the real ~/.hermes. Every task
runs in a throwaway temp home built from synthetic fixtures and removed after.

Run:
    python3 scripts/memory_harness.py                         # human markdown summary
    python3 scripts/memory_harness.py --json > harness.json   # full structured report
    python3 scripts/memory_harness.py --markdown > harness.md
    python3 scripts/memory_harness.py --mode static           # static-only (no query)
    python3 scripts/memory_harness.py --strict                # exit 1 on WARN too

Exit code: 0 if no task FAILs (WARN allowed), 1 if any FAIL (or any WARN under
--strict), 2 on a usage/fixture-load error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import memory_project as MP  # noqa: E402  the engine under test
import temporal_memory as TM  # noqa: E402  content_hash join key (pure, deterministic)
from memory_signals import ENTRY_DELIMITER  # noqa: E402

TOOL_VERSION = "1.0.0"

# Default fixture set ships next to the harness (mirrors the auto-extract fixtures).
DEFAULT_TASKS_PATH = os.path.join(_HERE, "memory_harness_tasks.json")
# A deterministic "today" so recency decay is reproducible. Overridable per-run and
# per-fixture-file; this is only the last-resort default.
DEFAULT_TODAY = _dt.date(2026, 6, 24)

# Status vocabulary (worst-wins).
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_STATUS_RANK = {PASS: 0, WARN: 1, FAIL: 2}

# Below this required-recall fraction a task FAILs; at/above it (but < 1.0) it WARNs.
# Conservative: missing *any* required entry is at least a WARN; missing *most* is a
# FAIL. 1.0 is the only PASS.
DEFAULT_RECALL_WARN_FLOOR = 0.5
# A "huge" budget for the fixture self-check / full-injection control. Larger than
# any realistic hot-memory file, so "everything fits".
CONTROL_BUDGET = 10_000_000

VALID_MODES = ("static", "lexical")
VALID_PINS = ("safety", "identity", "operational")


# --------------------------------------------------------------------------- #
# Lexical relevance proxy (H2): a transparent stand-in for the semantic index.  #
#                                                                               #
# It is handed ONLY (query, [{entry_ref, text}, ...]) — never the gold/pin       #
# flags — so it cannot cheat by retrieving "the required ones". Score is IDF-     #
# weighted query-term overlap, normalised to [0, 1]: 1.0 when the entry contains  #
# every distinctive query term, fractional for partial overlap, 0.0 for none.    #
# Deterministic and stdlib-only. This is a WEAK proxy on purpose (see LIMITATIONS #
# in the module docstring) — Tier 2 is the real-model upgrade.                   #
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A small, transparent stopword set. Kept short so the proxy stays inspectable;
# distinctive task terms (telegram, watchdog, nclex, pharmacology, ...) survive.
_STOPWORDS = frozenset("""
a an and are as at be but by do for from has have how i if in into is it its my no
not of on or so that the their then there these this to up use used user using was
what when where which who why will with you your me again keep need needs should
""".split())


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, length >= 2, minus stopwords. Pure."""
    return [t for t in _TOKEN_RE.findall((text or "").lower())
            if len(t) >= 2 and t not in _STOPWORDS]


def lexical_relevance_hits(query: str, entries: list[dict], *, top_n: int = 20) -> list[dict]:
    """Return memory_entry_index-shaped relevance hits for ``query`` over ``entries``.

    ``entries`` items need ONLY ``text`` (and optionally ``entry_ref``); this
    function is deliberately blind to any gold/required/pin field so Tier-1 recall
    cannot be circular (H2). Output hits carry ``content_hash`` (the robust join
    key the engine matches first), ``entry_ref``, and a ``score`` in [0, 1].
    """
    q = set(_tokens(query))
    if not q or not entries:
        return []
    # IDF across the provided entries (document frequency of each token).
    df: dict[str, int] = {}
    docs: list[tuple[str, str, set]] = []
    for e in entries:
        text = e.get("text", "")
        ref = e.get("entry_ref", "")
        toks = set(_tokens(text))
        docs.append((ref, text, toks))
        for t in toks:
            df[t] = df.get(t, 0) + 1
    n = len(entries)

    def idf(t: str) -> float:
        # Distinctive tokens (in few entries) weigh more than common ones. A query
        # token absent from every entry gets the max weight ln(1+n).
        d = df.get(t, 0)
        return math.log(1.0 + (n / d if d else float(n)))

    q_weight = sum(idf(t) for t in q) or 1.0
    hits: list[dict] = []
    for ref, text, toks in docs:
        shared = q & toks
        if not shared:
            continue
        score = MP.clamp(sum(idf(t) for t in shared) / q_weight)
        hits.append({
            "content_hash": TM.content_hash(text),
            "entry_ref": ref,
            "score": round(score, 4),
        })
    # Deterministic order: score desc, then content_hash for a stable tiebreak.
    hits.sort(key=lambda h: (-h["score"], h["content_hash"]))
    return hits[:top_n]


# --------------------------------------------------------------------------- #
# Fixture loading + validation                                                  #
# --------------------------------------------------------------------------- #
class FixtureError(ValueError):
    """A task fixture is malformed or self-contradictory (not an engine result)."""


def load_tasks(path: str) -> dict:
    """Load + structurally validate the task fixture file. Raises FixtureError."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as e:
        raise FixtureError(f"fixture file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise FixtureError(f"fixture file is not valid JSON ({path}): {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise FixtureError(f"fixture file must be an object with a 'tasks' list: {path}")
    seen_ids = set()
    for i, task in enumerate(data["tasks"]):
        _validate_task(task, i, seen_ids)
    return data


def _validate_task(task: dict, i: int, seen_ids: set) -> None:
    where = f"tasks[{i}]"
    if not isinstance(task, dict):
        raise FixtureError(f"{where} must be an object")
    tid = task.get("id")
    if not tid or not isinstance(tid, str):
        raise FixtureError(f"{where} missing string 'id'")
    if tid in seen_ids:
        raise FixtureError(f"duplicate task id {tid!r}")
    seen_ids.add(tid)
    if not isinstance(task.get("budget_tokens"), int) or task["budget_tokens"] < 0:
        raise FixtureError(f"{tid}: 'budget_tokens' must be an int >= 0")
    if "query" in task and task["query"] is not None and not isinstance(task["query"], str):
        raise FixtureError(f"{tid}: 'query' must be a string when present")
    entries = (task.get("memory") or []) + (task.get("user") or [])
    if not entries:
        raise FixtureError(f"{tid}: needs at least one memory/user entry")
    texts, hashes, required, pinned = [], set(), 0, 0
    for store_key in ("memory", "user"):
        for j, ent in enumerate(task.get(store_key) or []):
            ewhere = f"{tid}.{store_key}[{j}]"
            if not isinstance(ent, dict) or not isinstance(ent.get("text"), str) or not ent["text"].strip():
                raise FixtureError(f"{ewhere} must be an object with non-empty 'text'")
            h = TM.content_hash(ent["text"])
            if h in hashes:
                # H4-adjacent: identical text would collide on the content_hash join.
                raise FixtureError(f"{ewhere}: duplicate entry text collides on content_hash; "
                                   f"make fixture entries textually unique")
            hashes.add(h)
            texts.append(ent["text"])
            if ent.get("required"):
                required += 1
            pin = ent.get("pin")
            if pin is not None:
                if pin not in VALID_PINS:
                    raise FixtureError(f"{ewhere}: 'pin' must be one of {VALID_PINS} or omitted, got {pin!r}")
                pinned += 1
    if required == 0 and pinned == 0:
        raise FixtureError(f"{tid}: task labels neither a required entry nor a pin; nothing to assert")


# --------------------------------------------------------------------------- #
# Home materialization (read-only w.r.t. live data — temp dir, removed after)   #
# --------------------------------------------------------------------------- #
def _materialize_home(task: dict) -> str:
    """Write the task's synthetic MEMORY.md / USER.md into a fresh temp home."""
    root = tempfile.mkdtemp(prefix="memharness_")
    mem_dir = os.path.join(root, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    mem = [e["text"] for e in (task.get("memory") or [])]
    usr = [e["text"] for e in (task.get("user") or [])]
    with open(os.path.join(mem_dir, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(ENTRY_DELIMITER.join(mem))
    with open(os.path.join(mem_dir, "USER.md"), "w", encoding="utf-8") as fh:
        fh.write(ENTRY_DELIMITER.join(usr))
    return root


def _labelled_entries(task: dict) -> list[dict]:
    """Flatten the task's entries with their gold labels + content_hash join key."""
    out = []
    for store_key in ("memory", "user"):
        for ent in (task.get(store_key) or []):
            out.append({
                "store": store_key,
                "text": ent["text"],
                "content_hash": TM.content_hash(ent["text"]),
                "required": bool(ent.get("required")),
                "pin": ent.get("pin"),            # expected pin class or None
                "noise": bool(ent.get("noise")),  # explicit distractor (advisory only)
                "label": ent.get("label") or MP.topic_of(ent["text"])[:48],
            })
    return out


def _project(root: str, task: dict, *, mode: str, budget: int, today: _dt.date,
             identity_extra: str | None) -> dict:
    """Run the engine. In 'lexical' mode, feed gold-blind lexical relevance hits."""
    query = task.get("query") if mode == "lexical" else None
    rel_hits = None
    if mode == "lexical" and query:
        # Hand the proxy ONLY public entry data (text + ref). No gold leakage (H2).
        public = []
        for store_key in ("memory", "user"):
            for j, ent in enumerate(task.get(store_key) or []):
                public.append({"entry_ref": f"{store_key}#{j}", "text": ent["text"]})
        rel_hits = lexical_relevance_hits(query, public)
    return MP.project(
        root, budget=budget, user_home=root, today=today,
        query=query, relevance_hits=rel_hits,
        identity_extra=identity_extra,
    )


# --------------------------------------------------------------------------- #
# Per-task evaluation                                                           #
# --------------------------------------------------------------------------- #
def _selected_hashes(report: dict) -> set:
    return {e["content_hash"] for e in report["per_entry"] if e["selected"]}


def _pin_class_by_hash(report: dict) -> dict:
    return {e["content_hash"]: e.get("pin_class", "none") for e in report["per_entry"]}


def evaluate_task(task: dict, *, modes=VALID_MODES, today: _dt.date = DEFAULT_TODAY,
                  recall_warn_floor: float = DEFAULT_RECALL_WARN_FLOOR) -> dict:
    """Evaluate one task. Returns a structured result with per-mode rows + status.

    Builds a throwaway temp home, runs the fixture self-check (H4), the budget=0
    pin-survival probe (H5), and the main projection per mode (H1/H3), then scores
    everything against the gold labels. The temp home is always removed.
    """
    identity_extra = task.get("identity_extra")
    labelled = _labelled_entries(task)
    required_h = {e["content_hash"] for e in labelled if e["required"]}
    pins = [e for e in labelled if e["pin"] is not None]  # expected pins
    by_hash = {e["content_hash"]: e for e in labelled}
    budget = int(task["budget_tokens"])

    root = _materialize_home(task)
    try:
        # --- H4: fixture self-check — required entries must exist & be selectable
        #         at unlimited budget; otherwise the gold set is invalid. ---------
        control = _project(root, task, mode="static", budget=CONTROL_BUDGET,
                           today=today, identity_extra=identity_extra)
        control_sel = _selected_hashes(control)
        control_recall = _recall(required_h, control_sel)
        fixture_valid = (control_recall == 1.0)
        missing_in_control = sorted(_labels(required_h - control_sel, by_hash))
        original_tokens = control["original_tokens"]

        # --- H5: pin survival isolated at budget=0 (only pins survive there). -----
        probe = _project(root, task, mode="static", budget=0,
                         today=today, identity_extra=identity_extra)
        probe_sel = _selected_hashes(probe)
        probe_class = _pin_class_by_hash(probe)
        # Budget attribution (honest FAIL-vs-config-WARN). Pins are mandatory and
        # budget-exempt, so the capacity available to OPTIONAL entries is the budget
        # minus the pin (mandatory) tokens. A required entry that is ALSO a pin is
        # mandatory — it must NOT be double-counted as optional weight, and it can
        # never be "missing" (pins survive budget=0). We attribute a recall miss
        # PER ENTRY: a missed required entry that could individually fit in the
        # optional capacity was DROPPED BY CHOICE (a real selection failure → FAIL);
        # a missed required entry too large to fit even alone is ARITHMETIC (config
        # → WARN). One oversized required entry must never blanket-downgrade the miss
        # of a small one that the engine could have kept (the masking bug).
        probe_mandatory = probe.get("mandatory_tokens", 0)
        pin_hashes = {p["content_hash"] for p in pins}
        optional_capacity = max(0, budget - probe_mandatory)
        req_weight = {e["content_hash"]: MP.entry_weight(e["text"]) for e in labelled if e["required"]}
        # required-but-not-pin weight only (pins are exempt; no double count)
        required_optional_weight = sum(w for h, w in req_weight.items() if h not in pin_hashes)
        min_budget_for_required = probe_mandatory + required_optional_weight
        budget_can_hold_required = required_optional_weight <= optional_capacity
        pin_results = []
        for p in pins:
            h = p["content_hash"]
            pin_results.append({
                "label": p["label"], "store": p["store"],
                "expected": p["pin"], "actual": probe_class.get(h, "none"),
                "survived_budget_zero": h in probe_sel,
            })
        dropped_pins = [r for r in pin_results if not r["survived_budget_zero"]]
        misclassified_pins = [r for r in pin_results
                              if r["survived_budget_zero"] and r["actual"] != r["expected"]]

        # --- H1/H3: main projection per mode, scored vs gold. --------------------
        noise_h = {e["content_hash"] for e in labelled if e["noise"]}
        mode_rows = []
        for mode in modes:
            rep = _project(root, task, mode=mode, budget=budget,
                          today=today, identity_extra=identity_extra)
            sel = _selected_hashes(rep)
            recall = _recall(required_h, sel)
            missing_h = required_h - sel
            missing = sorted(_labels(missing_h, by_hash))
            # Per-entry attribution: a missed required entry that could have fit alone
            # in the optional capacity was dropped BY CHOICE (real selection failure);
            # one too large to fit even alone is an arithmetic/config miss.
            droppable_h = {h for h in missing_h if req_weight.get(h, 0) <= optional_capacity}
            unfittable_h = missing_h - droppable_h
            # precision over NON-pinned selections (pins are not "noise" even if not
            # task-required): of the optional entries we spent budget on, how many
            # were gold-required? advisory only (H3) — never changes status.
            optional_sel = sel - pin_hashes
            relevant_optional = optional_sel & required_h
            precision = (round(len(relevant_optional) / len(optional_sel), 4)
                         if optional_sel else None)
            # gold-uncontested guard: if every required entry survived AND no labelled
            # distractor was dropped, the budget never forced a contested choice — the
            # task asserts little (and a circularly-authored gold set would look like
            # this). Advisory only; it cannot rescue or sink a status.
            noise_dropped = bool(noise_h - sel)
            gold_uncontested = (recall == 1.0 and noise_h and not noise_dropped)
            mode_rows.append({
                "mode": mode,
                "budget_tokens": budget,
                "original_tokens": original_tokens,
                "projected_tokens": rep["projected_tokens"],
                "savings_pct": rep["savings_pct"],
                "required_total": len(required_h),
                "required_recall": recall,
                "required_recall_pct": round(recall * 100, 1) if required_h else None,
                "missing_required": missing,
                "missing_droppable": sorted(_labels(droppable_h, by_hash)),
                "missing_unfittable": sorted(_labels(unfittable_h, by_hash)),
                "entries_selected": rep["entries_selected"],
                "entries_total": rep["entries_total"],
                "selected_precision": precision,
                "gold_uncontested": gold_uncontested,
                "relevance_source": rep.get("relevance_source", ""),
            })
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The engine SHIPS query-aware (Phase 2b) and the live integration passes the
    # current turn as the query — so the task is GATED on the strongest requested
    # mode (lexical when present). The other mode ('static', i.e. the no-query
    # fallback used when the semantic index is down) is reported as a BASELINE for
    # comparison, never as the gate. Running --mode static makes static the gate
    # (you explicitly asked to evaluate the fallback). See skills doc.
    primary_mode = "lexical" if "lexical" in modes else (modes[0] if modes else "static")

    return _decide_task_status({
        "id": task["id"],
        "category": task.get("category", ""),
        "query": task.get("query", ""),
        "budget_tokens": budget,
        "original_tokens": original_tokens,
        "optional_capacity": optional_capacity,
        "min_budget_for_required": min_budget_for_required,
        "budget_can_hold_required": budget_can_hold_required,
        "primary_mode": primary_mode,
        "fixture_valid": fixture_valid,
        "control_required_recall": control_recall,
        "missing_in_control": missing_in_control,
        "pins": pin_results,
        "dropped_pins": dropped_pins,
        "misclassified_pins": misclassified_pins,
        "modes": mode_rows,
    }, recall_warn_floor=recall_warn_floor)


def _decide_task_status(result: dict, *, recall_warn_floor: float) -> dict:
    """Apply the conservative PASS/WARN/FAIL policy.

    Gate = pin survival + PRIMARY-mode required recall + a valid fixture. Savings
    NEVER upgrades status (H3). Non-primary modes are baselines: their recall is
    reported and aggregated but does not gate. A recall miss caused purely by the
    budget being too small to hold pins+required is attributed as a config issue,
    not an engine failure (honest attribution).
    """
    fail, warn = [], []
    primary = result["primary_mode"]

    # H4 — invalid fixture is a hard FAIL (and we say so plainly).
    if not result["fixture_valid"]:
        fail.append(f"fixture-invalid: required entries not selectable at unlimited "
                    f"budget (missing: {result['missing_in_control']}); the gold set "
                    f"is wrong, not the engine")

    # H5 — dropped pin is a hard, safety-level FAIL.
    for r in result["dropped_pins"]:
        fail.append(f"dropped pin [{r['expected']}] {r['label']!r}: did NOT survive "
                    f"budget=0 (engine classified it {r['actual']!r})")
    # misclassified-but-survived pin: metadata drift → WARN (survival is the safety
    # property and held). A safety rule landing as a non-safety pin is still flagged.
    for r in result["misclassified_pins"]:
        warn.append(f"pin {r['label']!r} expected class {r['expected']!r} but engine "
                    f"classified it {r['actual']!r} (survived, so non-fatal)")

    # H1 — required recall. PRIMARY mode gates; baselines only inform. A miss is
    # attributed PER ENTRY (not by a global budget sum): if ANY required entry the
    # engine could have fit was dropped, that is a genuine selection failure and the
    # floor decides FAIL/WARN. Only when EVERY missed entry is too large to fit even
    # alone is the miss a config/arithmetic WARN. This prevents one oversized required
    # entry from masking the real drop of a small, fittable one.
    cap = result["optional_capacity"]
    for row in result["modes"]:
        is_primary = (row["mode"] == primary)
        rstat = PASS
        if row["required_total"] > 0:
            rec = row["required_recall"]
            droppable = row["missing_droppable"]      # could have fit → engine's choice
            unfittable = row["missing_unfittable"]    # too big even alone → arithmetic
            if not row["missing_required"]:
                rstat = PASS
            elif droppable:
                # A required entry that fits was dropped → real selection failure.
                if rec < recall_warn_floor:
                    rstat = FAIL
                    if is_primary:
                        fail.append(f"[{row['mode']}] required recall {row['required_recall_pct']}% "
                                    f"< floor {round(recall_warn_floor*100)}%; dropped (could fit): "
                                    f"{droppable}" + (f"; too large to fit: {unfittable}" if unfittable else ""))
                else:
                    rstat = WARN
                    if is_primary:
                        warn.append(f"[{row['mode']}] required recall {row['required_recall_pct']}%; "
                                    f"dropped (could fit): {droppable}"
                                    + (f"; too large to fit: {unfittable}" if unfittable else ""))
            else:
                # Every missed required entry is too large to fit in the optional
                # capacity → arithmetic/config miss, never an engine FAIL.
                rstat = WARN
                if is_primary:
                    warn.append(f"[{row['mode']}] budget {result['budget_tokens']} tok cannot hold "
                                f"required entry/entries (too large for {cap} tok after pins): "
                                f"{unfittable}; recall {row['required_recall_pct']}% — "
                                f"raise budget or shorten the entry")
        row["status"] = rstat
        row["is_primary"] = is_primary
        # H3 — advisories: reported, never status-changing. Precision is reported as a
        # NUMBER (selected_precision) but does NOT raise an advisory: keeping relevant
        # context beyond the bare gold entries is appropriate, not waste, so a low-
        # precision flag would mislead. Only a genuine non-saving is worth flagging.
        advisories = []
        if row["budget_tokens"] < row["original_tokens"] and row["savings_pct"] <= 0:
            advisories.append("no token savings at this budget despite budget < full injection")
        if row.get("gold_uncontested"):
            advisories.append("gold-uncontested: all required entries survived AND no labelled "
                              "distractor was dropped — the budget never forced a contested "
                              "choice, so this task asserts little (a circularly-authored gold "
                              "set would also look like this). Tighten the budget.")
        row["advisories"] = advisories

    # Baseline-vs-primary recall gap, surfaced as a non-gating note (shows what
    # query-awareness buys, and the fallback risk when the index is unavailable).
    prim_row = next((r for r in result["modes"] if r["mode"] == primary), None)
    for row in result["modes"]:
        if row["mode"] != primary and prim_row and row["required_total"] > 0:
            if row["required_recall"] < prim_row["required_recall"]:
                row["baseline_note"] = (
                    f"fallback baseline: '{row['mode']}' recall {row['required_recall_pct']}% "
                    f"< primary '{primary}' {prim_row['required_recall_pct']}% "
                    f"(query-aware projection recovers this; static fallback does not)")

    status = FAIL if fail else (WARN if warn else PASS)
    result["status"] = status
    result["fail_reasons"] = fail
    result["warn_reasons"] = warn
    result["per_mode_status"] = {r["mode"]: r["status"] for r in result["modes"]}
    return result


def _recall(required: set, selected: set) -> float:
    """Fraction of required entries that were selected. Vacuously 1.0 if none."""
    if not required:
        return 1.0
    return round(len(required & selected) / len(required), 4)


def _labels(hashes: set, by_hash: dict) -> list:
    return [by_hash[h]["label"] for h in hashes if h in by_hash]


# --------------------------------------------------------------------------- #
# Suite orchestration                                                           #
# --------------------------------------------------------------------------- #
def run_harness(tasks_path: str = DEFAULT_TASKS_PATH, *, modes=VALID_MODES,
                today: _dt.date | None = None,
                recall_warn_floor: float = DEFAULT_RECALL_WARN_FLOOR) -> dict:
    """Load fixtures, evaluate every task, and aggregate. Deterministic (H6)."""
    data = load_tasks(tasks_path)
    today = today or _parse_today(data.get("today")) or DEFAULT_TODAY
    modes = tuple(m for m in modes if m in VALID_MODES) or VALID_MODES

    results = [evaluate_task(t, modes=modes, today=today,
                             recall_warn_floor=recall_warn_floor)
               for t in data["tasks"]]

    primary_mode = "lexical" if "lexical" in modes else (modes[0] if modes else "static")
    status_counts = {PASS: 0, WARN: 0, FAIL: 0}
    for r in results:
        status_counts[r["status"]] += 1

    # token-weighted savings across tasks, per mode (sum projected / sum original).
    per_mode = {}
    for mode in modes:
        orig = proj = 0
        recalls = []
        mode_counts = {PASS: 0, WARN: 0, FAIL: 0}
        for r in results:
            row = next((m for m in r["modes"] if m["mode"] == mode), None)
            if not row:
                continue
            orig += row["original_tokens"]
            proj += row["projected_tokens"]
            if row["required_total"] > 0:
                recalls.append(row["required_recall"])
            mode_counts[r["per_mode_status"].get(mode, PASS)] += 1
        per_mode[mode] = {
            "is_primary_gate": mode == primary_mode,
            "original_tokens": orig,
            "projected_tokens": proj,
            "savings_pct": round((1 - proj / orig) * 100, 1) if orig else 0.0,
            "mean_required_recall_pct": round(100 * sum(recalls) / len(recalls), 1) if recalls else None,
            "status_counts": mode_counts,
        }

    return {
        "tool": "memory_harness",
        "tool_version": TOOL_VERSION,
        "tier": 1,
        "today": today.isoformat(),
        "tasks_path": os.path.abspath(tasks_path),
        "modes": list(modes),
        "primary_mode": primary_mode,
        "recall_warn_floor": recall_warn_floor,
        "tasks_total": len(results),
        "status_counts": status_counts,
        "overall_status": (FAIL if status_counts[FAIL] else
                           WARN if status_counts[WARN] else PASS),
        "per_mode": per_mode,
        "tasks": results,
        "limitations": [
            "Tier-1 'lexical' relevance is token overlap, NOT the shipped embedding "
            "model; it is a weak floor-ish proxy, not production retrieval quality.",
            "Recall against a gold set is a PROXY for answer quality (necessary, not "
            "sufficient). Only a Tier-2 model grader scores the actual answer.",
            "Token savings is reported but NEVER decides PASS/WARN/FAIL (H3).",
            "Fixtures are synthetic and few: representative + adversarial, not exhaustive.",
        ],
    }


def _parse_today(value) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# TIER 2 SEAM (gated, optional, NOT used by the default path).                  #
#                                                                               #
# A real answer-quality harness needs a model: render the FULL memory block and  #
# the PROJECTED block, ask the model the task under each, and compare answers.   #
# That requires network/API, which Tier 1 deliberately does not. Define the seam #
# here so Tier 2 can be added later without touching Tier-1 code; ship only the  #
# NullGrader so importing this module never reaches for a model.                 #
# --------------------------------------------------------------------------- #
class AnswerGrader:
    """Interface a Tier-2 grader implements. Bring your own model behind a flag."""

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        raise NotImplementedError


class NullGrader(AnswerGrader):
    """Default: does nothing. Tier 2 is disabled unless a real grader is supplied."""

    def grade(self, task: dict, full_block: str, projected_block: str) -> dict:
        return {"tier2": "disabled", "reason": "no model grader configured (Tier 1 only)"}


# --------------------------------------------------------------------------- #
# Rendering                                                                     #
# --------------------------------------------------------------------------- #
def _fmt_pct(v) -> str:
    return "n/a" if v is None else f"{v:g}%"


def render_markdown(report: dict) -> str:
    L = []
    add = L.append
    primary = report["primary_mode"]
    add(f"# Memory Projection Honesty Harness — Tier {report['tier']} report")
    add("")
    add(f"- **Overall:** {report['overall_status']}  "
        f"(PASS {report['status_counts'][PASS]} · "
        f"WARN {report['status_counts'][WARN]} · "
        f"FAIL {report['status_counts'][FAIL]} of {report['tasks_total']} tasks)")
    add(f"- **Gating mode:** `{primary}`  ·  "
        f"**Modes run:** {', '.join(report['modes'])}  ·  "
        f"**Recall warn floor:** {round(report['recall_warn_floor']*100)}%")
    add(f"- **Date (recency anchor):** {report['today']}  ·  "
        f"**Fixtures:** `{report['tasks_path']}`")
    add("")
    add("> **Read this first.** A task's PASS/WARN/FAIL is decided by (1) pin survival "
        "and (2) required-context recall in the **gating mode** only. **Token savings "
        "never decides status** — a projection that saves tokens but drops a needed "
        f"entry FAILs. The gating mode is `{primary}`: when both modes run, `lexical` "
        "(query-aware, the shipped Phase-2b config) gates and `static` (the no-query "
        "fallback used if the semantic index is down) is reported as a baseline. The "
        "`lexical` proxy here is weak token-overlap, NOT the real embedding model, so "
        "treat its recall as an approximate floor. See *Limitations*.")
    add("")

    # Recall (the quality proxy) is shown BEFORE savings so the eye lands on it first.
    add("## Per-mode summary  (recall is the quality proxy; savings is only reported)")
    add("")
    add("| Mode | Gate? | Mean req-recall | PASS/WARN/FAIL* | Σ full tok | Σ projected | Savings |")
    add("|---|:--:|--:|:--:|--:|--:|--:|")
    for mode in report["modes"]:
        pm = report["per_mode"][mode]
        sc = pm["status_counts"]
        add(f"| {mode} | {'✓' if pm['is_primary_gate'] else ''} | "
            f"{_fmt_pct(pm['mean_required_recall_pct'])} | "
            f"{sc[PASS]}/{sc[WARN]}/{sc[FAIL]} | {pm['original_tokens']} | "
            f"{pm['projected_tokens']} | {pm['savings_pct']}% |")
    add("")
    add("_*per-mode PASS/WARN/FAIL is an ablation: how each mode would score if IT were "
        "the gate. Only the gating mode decides the task status above._")
    add("")
    # The headline finding: what query-awareness buys (and the fallback risk).
    if len(report["modes"]) > 1 and "static" in report["per_mode"] and "lexical" in report["per_mode"]:
        s = report["per_mode"]["static"]["mean_required_recall_pct"]
        x = report["per_mode"]["lexical"]["mean_required_recall_pct"]
        if s is not None and x is not None:
            add(f"> **What query-awareness buys:** mean required-recall rises from "
                f"**{s}%** (static fallback) to **{x}%** (query-aware). When the semantic "
                f"index is unavailable, expect the lower number.")
            add("")

    # Per-task / per-mode rows.
    add("## Per-task results")
    add("")
    add("| Task | Category | Mode | Gate? | Budget | Req-recall | Missing | Pins | Full→Proj | Savings | Prec | Status |")
    add("|---|---|---|:--:|--:|--:|---|:--:|--:|--:|--:|:--:|")
    for t in report["tasks"]:
        pins_cell = _pins_cell(t)
        for row in t["modes"]:
            missing = ", ".join(row["missing_required"]) if row["missing_required"] else "—"
            gate = "✓" if row.get("is_primary") else ""
            add(f"| {t['id']} | {t['category']} | {row['mode']} | {gate} | {row['budget_tokens']} | "
                f"{_fmt_pct(row['required_recall_pct'])} | {missing} | {pins_cell} | "
                f"{row['original_tokens']}→{row['projected_tokens']} | {row['savings_pct']}% | "
                f"{_fmt_pct(_pct(row['selected_precision']))} | {row['status']} |")
    add("")

    # Findings (fail/warn/baseline) — the honest "where it misses" section.
    flagged = [t for t in report["tasks"]
               if t["status"] != PASS or t["warn_reasons"]
               or any(r.get("baseline_note") or r.get("advisories") for r in t["modes"])]
    add("## Findings  (where projection misses, and why)")
    add("")
    if not flagged:
        add("_No FAIL/WARN findings or advisories on this fixture set._")
    for t in flagged:
        add(f"### {t['id']} — {t['status']}  ·  _{t['category']}_")
        for r in t["fail_reasons"]:
            add(f"- **FAIL:** {r}")
        for r in t["warn_reasons"]:
            add(f"- **WARN:** {r}")
        for row in t["modes"]:
            if row.get("baseline_note"):
                add(f"- _baseline: {row['baseline_note']}_")
            for a in row.get("advisories", []):
                add(f"- _advisory [{row['mode']}]: {a}_")
    add("")

    add("## Limitations")
    add("")
    for lim in report["limitations"]:
        add(f"- {lim}")
    add("")
    return "\n".join(L)


def _pins_cell(t: dict) -> str:
    if not t["pins"]:
        return "—"
    if t["dropped_pins"]:
        return f"✗ {len(t['dropped_pins'])} dropped"
    if t["misclassified_pins"]:
        return f"⚠ {len(t['misclassified_pins'])} misclass"
    return f"✓ {len(t['pins'])}"


def _pct(v):
    return None if v is None else round(v * 100, 1)


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_harness.py",
        description="Honesty harness for the memory projection engine (Tier 1: "
                    "deterministic, no LLM/network). Compares projected vs full memory "
                    "injection against hand-labelled gold tasks: required-context recall, "
                    "pin survival, token savings. READ-ONLY w.r.t. live ~/.hermes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="EXAMPLES:\n"
               "  python3 scripts/memory_harness.py                       # markdown summary\n"
               "  python3 scripts/memory_harness.py --json > harness.json # structured report\n"
               "  python3 scripts/memory_harness.py --mode static         # static-only\n"
               "  python3 scripts/memory_harness.py --strict              # exit 1 on WARN too\n")
    p.add_argument("--tasks", default=DEFAULT_TASKS_PATH,
                   help="path to the task fixture JSON (default: shipped fixtures)")
    p.add_argument("--mode", choices=("both", *VALID_MODES), default="both",
                   help="static (no query), lexical (gold-blind query proxy), or both (default)")
    p.add_argument("--today", help="recency anchor YYYY-MM-DD (default: fixture's, else 2026-06-24)")
    p.add_argument("--recall-warn-floor", type=float, default=DEFAULT_RECALL_WARN_FLOOR,
                   help=f"below this required-recall fraction a task FAILs (default {DEFAULT_RECALL_WARN_FLOOR})")
    p.add_argument("--json", action="store_true", help="emit the full structured JSON report")
    p.add_argument("--markdown", action="store_true", help="emit the markdown report (default human view)")
    p.add_argument("--out", help="write the report to this file instead of stdout")
    p.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as FAIL")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = None
    if args.today:
        today = _parse_today(args.today)
        if today is None:
            print(f"error: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 2
    if not (0.0 <= args.recall_warn_floor <= 1.0):
        print("error: --recall-warn-floor must be in [0, 1]", file=sys.stderr)
        return 2
    modes = VALID_MODES if args.mode == "both" else (args.mode,)

    try:
        report = run_harness(args.tasks, modes=modes, today=today,
                             recall_warn_floor=args.recall_warn_floor)
    except FixtureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    text = (json.dumps(report, indent=2, ensure_ascii=False) if args.json
            else render_markdown(report))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + ("\n" if not text.endswith("\n") else ""))
        print(f"wrote {args.out} ({report['overall_status']})", file=sys.stderr)
    else:
        print(text)

    if report["status_counts"][FAIL]:
        return 1
    if args.strict and report["status_counts"][WARN]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
